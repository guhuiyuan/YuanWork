"""数一段文本有多少个 token，以及限长处落在哪个字符上。

切块器和嵌入器必须用**同一把尺子**：切片的长度就是嵌入模型一次能读的
长度，否则读不下的那一部分就没人找得到（ADR-046）。

尺子是嵌入模型自己的 tokenizer。它在切块时才第一次被需要（不是应用
启动时），而且只加载一次——切块在主进程里做，只有解析在工人进程里。

没有模型缓存的机器（测试、别人的机器）走**估算兜底**。估算**故意往大
了估**：块小了只慢一点，块大了丢尾巴。
"""

from __future__ import annotations

import logging
import string
from typing import Any

logger = logging.getLogger(__name__)

#: 字母、数字、空格大概占多少个 token。
#: 实测（真实工作域的 4801 个切片，逐字归桶后标定）：英文正文约 0.25，
#: 取 0.4 留余量。
LETTER_TOKENS_PER_CHAR = 0.4

#: 标点与符号大概占多少个 token。**一个标点常常就是一个 token**——
#: 代码里的下划线与点、表格里的竖线与框线都是（实测 ``_g_l_y_f.py``
#: 11 个字符 10 个 token）。
PUNCT_TOKENS_PER_CHAR = 1.5

#: 非 ASCII 字符（中文、全角标点、框线、勾叉）大概占多少个 token。
#: 汉字通常是 1 个，罕见字 2 个；取 1.5 留余量。
OTHER_TOKENS_PER_CHAR = 1.5

#: The model adds one token at the beginning and one at the end. They
#: take up two of its read length, so the content gets two fewer.
SPECIAL_TOKENS = 2

#: Room for the boundary effect: a piece is a substring, and tokenizing
#: it again can give one or two tokens more than the slice it was cut
#: from. The pieces are cut this much shorter, and the rest is caught
#: by the check afterwards.
CUT_MARGIN = 4

_ASCII_LETTERS = set(string.ascii_letters + string.digits + " \t\n\r")
_ASCII_PUNCTUATION = set(string.punctuation)

#: The tokenizers, by model name. Loading one takes seconds (it reads a
#: 17 MB file and builds the tokenizer), and the process usually needs
#: only one - but it builds many pipelines, and each of them would
#: otherwise pay for it.
_TOKENIZERS: dict[str, Any] = {}


def content_token_limit(max_tokens: int) -> int:
    """How many tokens of content fit into the model's read length."""
    return max(1, int(max_tokens) - SPECIAL_TOKENS)


def estimate_tokens(text: str) -> int:
    """Rough token count, without a tokenizer.

    Three buckets, because one number does not fit: letters and digits
    are cheap (they merge into words), punctuation is not (one
    character, one token), and Chinese is in between.

    The estimate is deliberately on the high side: a chunk that is a
    little too small costs a little speed, a chunk that is too big
    loses its tail. Measured against the real slices of the work
    domain, this under-counts 2 of 4801 (0.04%) and over-counts the
    total by 57%.
    """
    if not text:
        return 0
    letters = punctuation = other = 0
    for char in text:
        if char in _ASCII_LETTERS:
            letters += 1
        elif char in _ASCII_PUNCTUATION:
            punctuation += 1
        elif ord(char) > 127:
            other += 1
    # The ASCII characters that are neither letter nor punctuation
    # (control characters) are counted as punctuation: they are rare
    # and they are not free.
    leftover = len(text) - letters - punctuation - other
    return int(
        letters * LETTER_TOKENS_PER_CHAR
        + (punctuation + leftover) * PUNCT_TOKENS_PER_CHAR
        + other * OTHER_TOKENS_PER_CHAR
    ) + 1


class TokenCounter:
    """How long a piece of text is - and where the limit falls.

    Two questions, two answers:

    * ``count(text)`` - how many tokens is this
    * ``boundary(text, limit)`` - where does the limit-th token end,
      in characters

    The tokenizer is loaded on first use, not at construction: the
    application starts without it, and only a build needs it.
    """

    def __init__(self, model_name: str | None = None) -> None:
        self.model_name = model_name
        self._tokenizer: Any | None = None
        self._tried = False

    # ---- the tokenizer ----

    def _load(self) -> Any | None:
        """The embedding model's own tokenizer, or None if it is not there.

        Only from the local cache: the tokenizer lives in the same
        repository as the model, so it is there whenever the model is.
        (The model itself has to be in the cache too - the network
        guard does not let huggingface.co through.)
        """
        if self._tried:
            return self._tokenizer
        self._tried = True
        if not self.model_name:
            return None
        if self.model_name in _TOKENIZERS:
            self._tokenizer = _TOKENIZERS[self.model_name]
            return self._tokenizer
        try:
            from transformers import AutoTokenizer

            self._tokenizer = AutoTokenizer.from_pretrained(
                self.model_name, local_files_only=True
            )
        except Exception as exc:  # noqa: BLE001 - any failure means "no tokenizer"
            logger.info(
                "没有可用的 tokenizer（%s），切片长度改用估算：%r",
                self.model_name,
                exc,
            )
            self._tokenizer = None
        _TOKENIZERS[self.model_name] = self._tokenizer
        return self._tokenizer

    @property
    def exact(self) -> bool:
        """True when the lengths are measured, not estimated."""
        return self._load() is not None

    # ---- measuring ----

    def count(self, text: str) -> int:
        """How many tokens this text is (without the special tokens)."""
        tokenizer = self._load()
        if tokenizer is None:
            return estimate_tokens(text)
        return len(tokenizer(text, add_special_tokens=False)["input_ids"])

    def boundary(self, text: str, limit: int) -> int:
        """The character position where the limit-th token ends.

        With a tokenizer this is a token boundary - the text is never
        cut in the middle of a word. Without one it is an estimate of
        how many characters fit into `limit` tokens.
        """
        if limit <= 0 or not text:
            return 0
        tokenizer = self._load()
        if tokenizer is None:
            return self._estimated_boundary(text, limit)
        offsets = tokenizer(text, add_special_tokens=False, return_offsets_mapping=True)[
            "offset_mapping"
        ]
        if not offsets:
            return len(text)
        if len(offsets) <= limit:
            return len(text)
        return offsets[limit - 1][1]

    def _estimated_boundary(self, text: str, limit: int) -> int:
        """How many characters fit into `limit` tokens, by estimation."""
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if estimate_tokens(text[:middle]) <= limit:
                low = middle
            else:
                high = middle - 1
        return low
