"""Which layer a document belongs to.

Two layers (ADR-031):

* the **core layer** holds the documents that other documents are read
  against - contracts, rules, statutes, the company's own policies and
  process specs. Their conclusions are stable, and they have to be
  compiled into pages.
* the **long tail** holds everything else - the scattered, everyday
  material. It is sliced and searched when a question comes in.

The decision is made in this order:

1. **the user decides** - an explicit choice is never overruled.
2. **the rules decide** - the path says it (a folder of contracts), or
   the size says it (a document with conclusions of its own).
3. **the model decides** - for the documents that could go either way
   and are big enough to have conclusions.

A document nobody can decide goes to the long tail, and the reason is
written down. A failed judgement must not stop the build.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


#: The two layers.
CORE = "core"
LONGTAIL = "longtail"

#: Who decided.
BY_RULE = "rule"
BY_MODEL = "model"
BY_USER = "user"

#: The words in a path that say "this is the kind of document other
#: documents are read against".
CORE_PATH_KEYWORDS = (
    "合同",
    "契约",
    "协议",
    "规章",
    "制度",
    "流程",
    "规范",
    "法规",
    "条例",
    "办法",
    "政策",
    "标准",
    "章程",
    "守则",
    "手册",
)

#: A document of at least this many characters has conclusions of its
#: own. A one-page note is a note.
DEFAULT_CORE_MIN_CHARS = 20_000

#: Below this many characters the model is not asked - a note is a
#: note, and asking costs a call per file.
DEFAULT_MODEL_MIN_CHARS = 8_000

#: How many documents the core layer may hold. A core layer with
#: thousands of documents is not a summary of anything any more, and
#: every one of them costs a model call to compile (AC-P-007).
DEFAULT_MAX_CORE_DOCS = 200

#: The file types that can hold a document with conclusions.
DOCUMENT_TYPES = ("pdf", "doc", "docx", "ppt", "pptx", "md", "txt", "rtf")


def core_min_chars() -> int:
    """The size at which a document counts as core, from the settings."""
    try:
        from yuanwork.config import get_settings

        settings = get_settings()
        return int(settings.core_min_chars)
    except Exception:  # noqa: BLE001 - the default is enough
        return DEFAULT_CORE_MIN_CHARS


def max_core_docs_setting() -> int:
    """How many documents the core layer may hold, from the settings."""
    try:
        from yuanwork.config import get_settings

        settings = get_settings()
        return int(settings.core_max_docs)
    except Exception:  # noqa: BLE001 - the default is enough
        return DEFAULT_MAX_CORE_DOCS


def model_min_chars() -> int:
    """The size at which the model is asked, from the settings."""
    try:
        from yuanwork.config import get_settings

        settings = get_settings()
        return int(settings.tier_model_min_chars)
    except Exception:  # noqa: BLE001 - the default is enough
        return DEFAULT_MODEL_MIN_CHARS


class TierClassifier:
    """Decides which layer a document belongs to."""

    def __init__(
        self,
        sqlite_store: Any,
        llm_gateway: Any | None = None,
        *,
        core_size: int | None = None,
        model_size: int | None = None,
        max_core_docs: int | None = None,
    ):
        self.sqlite = sqlite_store
        self.llm = llm_gateway
        self.core_size = core_size if core_size is not None else core_min_chars()
        self.model_size = model_size if model_size is not None else model_min_chars()
        self.max_core_docs = (
            max_core_docs if max_core_docs is not None else max_core_docs_setting()
        )

    async def classify(self, doc: dict[str, Any]) -> dict[str, Any]:
        """The layer of a document, and where the decision came from.

        The result is written down - a decision nobody can see is a
        decision nobody can correct.
        """
        doc_id = str(doc.get("doc_id") or "")
        if not doc_id:
            return {
                "doc_id": "",
                "tier": LONGTAIL,
                "decided_by": BY_RULE,
                "reason": "没有文档标识",
            }

        # 1. The user decides.
        existing = await self.sqlite.get_doc_tier(doc_id)
        if existing and existing["decided_by"] == BY_USER:
            return {
                "doc_id": doc_id,
                "tier": existing["tier"],
                "decided_by": BY_USER,
                "reason": "用户指定的分层",
            }

        path = str(doc.get("file_path") or doc.get("file_name") or "")
        name = str(doc.get("file_name") or Path(path).name)
        text_length = int(doc.get("text_length") or doc.get("char_count") or 0)

        tier, decided_by, reason = self._by_rules(path, name, text_length)

        # 2. The rules decide. When they cannot, the model is asked.
        if tier is None:
            tier, decided_by, reason = await self._by_model(doc, path, name, text_length)

        # A document nobody could decide goes to the long tail, and the
        # build goes on.
        if tier is None:
            tier, decided_by, reason = (
                LONGTAIL,
                BY_RULE,
                "判断不出来，按长尾层处理",
            )

        # The core layer has a ceiling. Past it the document is still
        # indexed - it is just not a summary any more (AC-P-007). The
        # user's own choice is not overruled by this either.
        if tier == CORE and decided_by != BY_USER:
            count = await self.sqlite.count_doc_tiers(CORE)
            if count >= self.max_core_docs:
                tier, decided_by, reason = (
                    LONGTAIL,
                    BY_RULE,
                    f"核心层文档已达上限（{self.max_core_docs} 份），按长尾层处理",
                )

        record = await self.sqlite.upsert_doc_tier(
            doc_id=doc_id,
            tier=tier,
            decided_by=decided_by,
            source_hash=str(doc.get("file_hash") or doc.get("source_hash") or ""),
        )
        return {
            "doc_id": doc_id,
            "tier": record.get("tier", tier),
            "decided_by": record.get("decided_by", decided_by),
            "reason": reason,
        }

    def _by_rules(
        self, path: str, name: str, text_length: int
    ) -> tuple[str | None, str, str]:
        """The rules: the path says it, or the size says it."""
        haystack = f"{path} {name}".lower()
        for keyword in CORE_PATH_KEYWORDS:
            if keyword.lower() in haystack:
                return CORE, BY_RULE, f"路径里有「{keyword}」"

        if text_length >= self.core_size:
            return CORE, BY_RULE, f"体量够大（{text_length} 字）"

        return None, BY_RULE, ""

    async def _by_model(
        self, doc: dict[str, Any], path: str, name: str, text_length: int
    ) -> tuple[str | None, str, str]:
        """The model decides, for the documents that could go either way."""
        if self.llm is None:
            return None, BY_RULE, ""
        if text_length < self.model_size:
            # A note is a note - and asking costs a call per file.
            return None, BY_RULE, ""
        file_type = str(doc.get("file_type") or "").lower().lstrip(".")
        if file_type and file_type not in DOCUMENT_TYPES:
            return None, BY_RULE, ""

        prompt = (
            "判断下面这份文档属于哪一层。\n\n"
            "核心层：合同、规章、法条、公司内部制度与流程规范等——"
            "需要跨文档关联、结论稳定的资料。\n"
            "长尾层：其余零散、日常的材料（会议纪要、笔记、说明书、报表等）。\n\n"
            f"文件名：{name}\n"
            f"路径：{path}\n"
            f"开头：{str(doc.get('head') or '')[:600]}\n\n"
            "只输出 JSON：{\"tier\": \"core\" 或 \"longtail\", \"reason\": \"一句话理由\"}"
        )

        try:
            from yuanwork.llm.gateway import LLMMessage, LLMTask

            task = LLMTask(
                task_type="extract",
                messages=[LLMMessage(role="user", content=prompt)],
                temperature=0.0,
                max_tokens=200,
                json_mode=True,
            )
            response = await self.llm.complete(task)
            content = response.content.strip()
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
                content = content.strip()
            data = json.loads(content)
            tier = str(data.get("tier", "")).strip().lower()
            if tier not in (CORE, LONGTAIL):
                return None, BY_RULE, ""
            return tier, BY_MODEL, str(data.get("reason") or "模型判定")
        except Exception as e:  # noqa: BLE001 - a failed judgement is not a failed build
            logger.warning(f"The tier judgement failed: {e}")
            return None, BY_RULE, ""


def document_length(text: str) -> int:
    """How long a document is, in characters."""
    return len(text or "")
