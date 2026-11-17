"""切片的长度用嵌入模型读得下的 token 量（ADR-046）。

四件事要钉住：

1. **切出来的每一块都读得下**——模型一次读 512 token（加两个特殊 token
   占位置），所以内容上限是 510
2. **内容不丢**——切开的块拼回去盖住原文
3. **没有 tokenizer 时也不丢**——估算兜底（故意往大估）
4. **切块器与嵌入器用同一个数**——不是各自写死一个
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yuanwork.ingestion.chunker.structural_chunker import StructuralChunker
from yuanwork.ingestion.chunker.token_counter import (
    SPECIAL_TOKENS,
    TokenCounter,
    content_token_limit,
    estimate_tokens,
)
from yuanwork.ingestion.parser.base import RawBlock

CHINESE = "这是一段中文测试文本，用来检查切片的长度是不是按 token 量的。"
LONG_CHINESE = "采购管理办法第%d条规定，供应商应当提交营业执照、资质证书与近三年财务报表。"


def block(text: str) -> RawBlock:
    return RawBlock(doc_id="d1", block_id="b1", type="paragraph", content=text, meta={})


def long_chinese(paragraphs: int = 40) -> str:
    return "\n\n".join(LONG_CHINESE % (n + 1) for n in range(paragraphs))


# ===== the limit is the model's read length =====


def test_the_content_limit_leaves_room_for_the_special_tokens():
    """The model adds a token at each end; the content gets two fewer."""
    assert SPECIAL_TOKENS == 2
    assert content_token_limit(512) == 510


def test_a_chunk_never_exceeds_the_limit():
    counter = TokenCounter()
    chunker = StructuralChunker(max_tokens=100, token_counter=counter)
    chunks = chunker.chunk([block(long_chinese())], "d1")
    assert len(chunks) > 1
    for chunk in chunks:
        assert counter.count(chunk["text"]) <= 100


def test_the_default_limit_is_the_models_read_length():
    """512 token is the model's read length; the content gets 510."""
    chunker = StructuralChunker()
    assert chunker.max_tokens == content_token_limit(512)


def test_a_short_block_is_left_alone():
    chunker = StructuralChunker(max_tokens=510, token_counter=TokenCounter())
    chunks = chunker.chunk([block(CHINESE)], "d1")
    assert len(chunks) == 1
    assert chunks[0]["text"] == CHINESE


# ===== nothing is lost =====


def test_splitting_keeps_all_of_the_content():
    counter = TokenCounter()
    chunker = StructuralChunker(max_tokens=100, token_counter=counter)
    text = long_chinese()
    chunks = chunker.chunk([block(text)], "d1")
    joined = "".join(chunk["text"] for chunk in chunks)
    # The overlap repeats a bit; every sentence of the original is in there.
    for n in range(40):
        assert f"第{n + 1}条规定" in joined


def test_the_pieces_overlap_so_a_fact_on_the_border_is_in_both():
    counter = TokenCounter()
    chunker = StructuralChunker(max_tokens=100, token_counter=counter)
    chunks = chunker.chunk([block(long_chinese())], "d1")
    assert len(chunks) > 1
    first = chunks[0]["text"]
    second = chunks[1]["text"]
    # The tail of the first piece is in the second one too.
    assert first[-20:] in second or second[:20] in first


def test_the_chunk_ids_stay_unique_after_a_split():
    chunker = StructuralChunker(max_tokens=100, token_counter=TokenCounter())
    chunks = chunker.chunk([block(long_chinese())], "d1")
    ids = [chunk["id"] for chunk in chunks]
    assert len(set(ids)) == len(ids)


# ===== without a tokenizer =====


def test_the_estimate_is_used_when_there_is_no_tokenizer():
    counter = TokenCounter(model_name=None)
    assert not counter.exact
    assert counter.count(CHINESE) > 0


def test_the_estimate_does_not_undercount_chinese():
    """A chunk that is too big loses its tail - so err on the high side."""
    chinese = "采购管理办法规定，供应商应当提交营业执照、资质证书与近三年财务报表。" * 4
    assert estimate_tokens(chinese) >= len(chinese) * 0.5


def test_the_fallback_still_cuts_and_keeps_the_content():
    counter = TokenCounter(model_name=None)
    chunker = StructuralChunker(max_tokens=100, token_counter=counter)
    text = long_chinese()
    chunks = chunker.chunk([block(text)], "d1")
    assert len(chunks) > 1
    for chunk in chunks:
        assert estimate_tokens(chunk["text"]) <= 100
    joined = "".join(chunk["text"] for chunk in chunks)
    for n in range(40):
        assert f"第{n + 1}条规定" in joined


def test_the_estimate_survives_an_empty_text():
    assert estimate_tokens("") == 0
    assert TokenCounter(model_name=None).count("") == 0


# ===== one number for both =====


def test_the_pipeline_hands_the_chunker_the_models_read_length():
    """The chunker and the embedder must not have their own numbers."""
    from yuanwork.ingestion.pipeline import IngestionPipeline

    class _Semantic:
        embedding_max_tokens = 512
        embedding_model_name = "BAAI/bge-m3"

    pipeline = IngestionPipeline(sqlite_store=None, semantic_memory=_Semantic())
    assert pipeline.chunker.max_tokens == content_token_limit(512)


def test_the_pipeline_follows_a_different_read_length():
    from yuanwork.ingestion.pipeline import IngestionPipeline

    class _Semantic:
        embedding_max_tokens = 1024
        embedding_model_name = "BAAI/bge-m3"

    pipeline = IngestionPipeline(sqlite_store=None, semantic_memory=_Semantic())
    assert pipeline.chunker.max_tokens == content_token_limit(1024)


# ===== the rules are part of the index =====


def test_the_version_says_which_rules_and_which_limit():
    """The index compares it: a change of the rules has to cut the
    files again instead of skipping them as "already indexed"."""
    chunker = StructuralChunker(max_tokens=510)
    assert chunker.version == f"{StructuralChunker.RULES_VERSION}-510"
    other = StructuralChunker(max_tokens=256)
    assert other.version != chunker.version


def test_a_file_cut_with_other_rules_is_cut_again(tmp_path, monkeypatch):
    """The whole point of the version: the old chunks are stale."""
    import asyncio

    from yuanwork.ingestion.pipeline import IngestionPipeline

    class _Semantic:
        embedding_max_tokens = 512
        embedding_model_name = "BAAI/bge-m3"

    class _Store:
        """A store that knows one file - cut with the old rules."""

        def __init__(self) -> None:
            self.record = {
                "doc_id": "old-doc",
                "chunk_version": "chars-2000",
            }

        async def get_file_by_hash(self, file_hash: str) -> dict | None:
            return self.record

        async def upsert_file(self, **kwargs) -> str:
            self.record = dict(kwargs)
            return kwargs.get("doc_id", "")

    store = _Store()
    pipeline = IngestionPipeline(sqlite_store=store, semantic_memory=_Semantic())

    state = asyncio.run(pipeline.resume_state(tmp_path / "x.md", "hash"))

    assert state["state"] == "stale"
    assert state["doc_id"] == "old-doc"


def test_a_file_cut_with_the_same_rules_is_skipped(tmp_path, monkeypatch):
    import asyncio

    from yuanwork.ingestion.pipeline import IngestionPipeline

    class _Semantic:
        embedding_max_tokens = 512
        embedding_model_name = "BAAI/bge-m3"

    class _Store:
        def __init__(self, version: str) -> None:
            self.record = {"doc_id": "same-doc", "chunk_version": version}

        async def get_file_by_hash(self, file_hash: str) -> dict | None:
            return self.record

    pipeline = IngestionPipeline(sqlite_store=None, semantic_memory=_Semantic())

    class _Store2(_Store):
        def __init__(self) -> None:
            super().__init__(pipeline.chunker.version)

    pipeline.sqlite = _Store2()

    state = asyncio.run(pipeline.resume_state(tmp_path / "x.md", "hash"))

    assert state["state"] == "already_indexed"
    assert state["doc_id"] == "same-doc"


# ===== the real tokenizer, when it is there =====


@pytest.fixture(scope="module")
def real_counter() -> TokenCounter:
    """The real tokenizer, loaded once for the whole file.

    Loading it takes seconds (it reads a 17 MB tokenizer file), so it
    is not loaded again for every test.
    """
    counter = TokenCounter(model_name="BAAI/bge-m3")
    if not counter.exact:
        pytest.skip("本机没有嵌入模型的 tokenizer")
    return counter


def test_the_real_tokenizer_measures_what_it_says(real_counter: TokenCounter):
    text = "采购管理办法规定，供应商应当提交营业执照与资质证书。"
    encoded = real_counter.count(text)
    assert encoded > 0


def test_the_real_tokenizer_cuts_at_the_limit(real_counter: TokenCounter):
    chunker = StructuralChunker(max_tokens=100, token_counter=real_counter)
    chunks = chunker.chunk([block(long_chinese())], "d1")
    assert len(chunks) > 1
    for chunk in chunks:
        assert real_counter.count(chunk["text"]) <= 100


def test_the_real_tokenizer_prefers_a_sentence_boundary(real_counter: TokenCounter):
    chunker = StructuralChunker(max_tokens=100, token_counter=real_counter)
    text = "\n\n".join(f"第{n}条 这是第{n}条的规定内容。" for n in range(30))
    chunks = chunker.chunk([block(text)], "d1")
    # Most pieces end where a sentence ends.
    endings = sum(1 for chunk in chunks if chunk["text"].endswith("。"))
    assert endings >= len(chunks) - 1
