"""Tests for the tantivy+jieba FTS index (Stage 3+ sparse channel)."""

from __future__ import annotations

from pathlib import Path

import pytest


# Skip the whole module if tantivy / jieba aren't available — they're
# added in Stage 3+ and the project should still import on a minimal
# install. (They're in the core deps now, so this is mostly defensive.)
tantivy = pytest.importorskip("tantivy")
jieba = pytest.importorskip("jieba")


@pytest.fixture
def fts_index(temp_dir: Path):
    from yuanwork.retrieval.fts import FTSIndex

    idx = FTSIndex(temp_dir / "fts")
    yield idx
    idx.close()


def test_fts_add_and_search_english(fts_index):
    """Plain English text is tokenized by the default tokenizer."""
    fts_index.add_documents([
        {"id": "1", "doc_id": "d1", "text": "invoice amount 1280 RMB"},
        {"id": "2", "doc_id": "d1", "text": "date 2026-10-15"},
        {"id": "3", "doc_id": "d1", "text": "service consulting"},
        {"id": "4", "doc_id": "d1", "text": "amount 5000"},
    ])
    results = fts_index.search("amount", top_k=5)
    ids = [r[0] for r in results]
    assert "1" in ids and "4" in ids  # both contain "amount"
    # "1" appears earlier in the doc (BM25 slight boost for shorter docs)
    assert ids[0] in ("1", "4")


def test_fts_search_pure_number(fts_index):
    """Pure-number queries find chunks with that number."""
    fts_index.add_documents([
        {"id": "a", "doc_id": "d1", "text": "金额 1280元"},
        {"id": "b", "doc_id": "d1", "text": "金额 5000元"},
    ])
    results = fts_index.search("1280", top_k=5)
    assert len(results) >= 1
    top_id = results[0][0]
    assert top_id == "a", f"expected 'a' on top for query '1280', got {top_id}"


def test_fts_search_cjk(fts_index):
    """CJK queries (segmented via jieba) match correctly."""
    fts_index.add_documents([
        {"id": "1", "doc_id": "d1", "text": "发票金额 1280元"},
        {"id": "2", "doc_id": "d1", "text": "开票日期 2026年10月15日"},
        {"id": "3", "doc_id": "d1", "text": "收款单位 上海元工作科技"},
    ])
    # "金额" should match doc 1
    r = fts_index.search("金额", top_k=3)
    assert r and r[0][0] == "1"
    # "开票" should match doc 2
    r = fts_index.search("开票", top_k=3)
    assert r and r[0][0] == "2"
    # "上海" should match doc 3
    r = fts_index.search("上海", top_k=3)
    assert r and r[0][0] == "3"


def test_fts_search_mixed_cjk_and_number(fts_index):
    """Queries with mixed CJK + numbers segment correctly."""
    fts_index.add_documents([
        {"id": "a", "doc_id": "d1", "text": "项目 2024-Q3 总结"},
        {"id": "b", "doc_id": "d1", "text": "项目 2024-Q4 总结"},
        {"id": "c", "doc_id": "d1", "text": "项目 2025-Q1 计划"},
    ])
    r = fts_index.search("2024-Q3", top_k=3)
    assert r and r[0][0] == "a"
    r = fts_index.search("2024", top_k=5)
    ids = [x[0] for x in r]
    assert "a" in ids and "b" in ids
    assert "c" not in ids  # 2024 doesn't appear in c


def test_fts_delete_by_ids(fts_index):
    """delete_by_ids removes the right chunks."""
    fts_index.add_documents([
        {"id": "1", "doc_id": "d1", "text": "first"},
        {"id": "2", "doc_id": "d1", "text": "second"},
        {"id": "3", "doc_id": "d1", "text": "third"},
    ])
    assert fts_index.count() == 3
    fts_index.delete_by_ids(["2"])
    assert fts_index.count() == 2
    r = fts_index.search("second", top_k=5)
    assert not r, f"expected no hits after delete, got {r}"


def test_fts_delete_by_doc_id(fts_index):
    """delete_by_doc_id removes all chunks of a doc."""
    fts_index.add_documents([
        {"id": "1", "doc_id": "d1", "text": "alpha"},
        {"id": "2", "doc_id": "d2", "text": "beta"},
        {"id": "3", "doc_id": "d1", "text": "gamma"},
    ])
    fts_index.delete_by_doc_id("d1")
    r = fts_index.search("alpha", top_k=5)
    assert not r
    r = fts_index.search("gamma", top_k=5)
    assert not r
    # d2 still indexed
    r = fts_index.search("beta", top_k=5)
    assert r and r[0][0] == "2"


def test_fts_reindex_from_chunks(fts_index):
    """reindex_from_chunks rebuilds from scratch."""
    fts_index.add_documents([
        {"id": "old", "doc_id": "d1", "text": "stale content"},
    ])
    fts_index.reindex_from_chunks([
        {"id": "1", "doc_id": "d2", "text": "fresh content"},
        {"id": "2", "doc_id": "d2", "text": "another fresh"},
    ])
    # The old entry should be gone
    r = fts_index.search("stale", top_k=5)
    assert not r
    # The new entries should be there
    r = fts_index.search("fresh", top_k=5)
    assert len(r) == 2
    assert fts_index.count() == 2


def test_fts_empty_query_returns_empty(fts_index):
    """Empty / whitespace queries are short-circuited (no parse error)."""
    fts_index.add_documents([{"id": "1", "doc_id": "d1", "text": "anything"}])
    assert fts_index.search("") == []
    assert fts_index.search("   ") == []


def test_fts_score_is_positive(fts_index):
    """Returned scores are non-negative (BM25 convention)."""
    fts_index.add_documents([
        {"id": "1", "doc_id": "d1", "text": "金额 1280"},
        {"id": "2", "doc_id": "d1", "text": "其他内容"},
    ])
    r = fts_index.search("金额", top_k=5)
    assert r
    for cid, score in r:
        assert score > 0, f"non-positive score for {cid}: {score}"


@pytest.mark.asyncio
async def test_the_new_version_of_a_chunk_survives_the_flush(sem_with_fts):
    """The delete of the old version and the write of the new one
    share one flush - and the new one has to be the one that is left.

    The delete comes first in the writer's queue, so the old version
    is the one that goes. If the order were the other way round, a
    re-indexed document would disappear from the full-text index while
    still being in the vector store.

    The flush is the build's job (TASK-064): the write leaves the
    documents in the index's hands, and the build ends with one flush.
    """
    await sem_with_fts.add_chunks(
        [{"id": "c1", "doc_id": "d1", "chunk_idx": 0, "text": "第一版 金额 1280"}]
    )
    await sem_with_fts.add_chunks(
        [{"id": "c1", "doc_id": "d1", "chunk_idx": 0, "text": "第二版 金额 1280"}]
    )
    await sem_with_fts.flush_fts()

    fts = sem_with_fts._get_fts_index()
    assert fts.count() == 1, "the chunk was written twice"
    assert fts.search("第二版", top_k=5), "the new version is not findable"
    assert not fts.search("第一版", top_k=5), "the old version is still there"


@pytest.mark.asyncio
async def test_a_single_file_is_searchable_right_away(sem_with_fts, temp_dir: Path):
    """A file indexed on its own has to be findable right away.

    The index keeps the documents in its hands until it decides to
    flush them (ADR-043) - a build flushes once when it is done
    (TASK-064). A single file has no build to wait for: it is
    indexed, and then it has to be searchable.
    """
    from yuanwork.db.sqlite import SQLiteStore
    from yuanwork.ingestion.parser.base import RawBlock
    from yuanwork.ingestion.pipeline import IngestionPipeline

    store = SQLiteStore(temp_dir / "store.db")
    await store.initialize()
    pipeline = IngestionPipeline(
        sqlite_store=store, semantic_memory=sem_with_fts, llm_gateway=None
    )
    blocks = [
        RawBlock(
            block_id="b1",
            doc_id="d9",
            content="合同金额 9999元",
            type="paragraph",
            meta={},
        )
    ]
    document = temp_dir / "合同.md"
    document.write_text("合同金额 9999元", encoding="utf-8")
    await pipeline.index_blocks(
        document, "d9", "hash1", blocks, batch=None, compile_now=False
    )

    fts = sem_with_fts._get_fts_index()
    assert fts.search("9999", top_k=5), "the file is not searchable yet"
    await store.close()


# ---- The flush (TASK-058) ----


def test_the_documents_are_not_searchable_before_the_flush(temp_dir: Path):
    """A commit is what makes a document findable.

    Until the writer is flushed, tantivy keeps the documents to
    itself - so a write that is not flushed yet is not searchable.
    That is what makes the flush worth batching.
    """
    from yuanwork.retrieval.fts import FTSIndex

    idx = FTSIndex(temp_dir / "fts")
    try:
        idx.add_documents(
            [{"id": "1", "doc_id": "d1", "text": "金额 1280元"}], commit=False
        )
        assert not idx.search("1280", top_k=5), (
            "the document was searchable before the flush"
        )
        idx.commit()
        assert idx.search("1280", top_k=5), "the flush did not make it findable"
    finally:
        idx.close()


def test_a_flush_makes_everything_findable(temp_dir: Path):
    """Many documents, one flush."""
    from yuanwork.retrieval.fts import FTSIndex

    idx = FTSIndex(temp_dir / "fts")
    try:
        for batch in range(4):
            idx.add_documents(
                [
                    {"id": f"{batch}-{i}", "doc_id": "d1", "text": f"金额 {batch}{i}元"}
                    for i in range(50)
                ],
                commit=False,
            )
        idx.commit()
        assert idx.count() == 200
        assert idx.search("金额", top_k=10)
    finally:
        idx.close()


def test_the_writer_flushes_itself_when_it_holds_enough(temp_dir: Path):
    """The memory of the writer is bounded: it flushes on its own.

    Without this a build that writes a million documents would hold
    them all in the writer's memory.
    """
    from yuanwork.retrieval.fts import FTSIndex

    idx = FTSIndex(temp_dir / "fts", commit_threshold=10)
    try:
        for batch in range(4):
            idx.add_documents(
                [
                    {"id": f"{batch}-{i}", "doc_id": "d1", "text": f"金额 {batch}{i}元"}
                    for i in range(10)
                ],
                commit=False,
            )
        # The writer flushed by itself - the documents are findable
        # without an explicit commit.
        assert idx.count() > 0, "the writer never flushed on its own"
    finally:
        idx.close()


# ---- Integration: SemanticMemory uses FTS automatically ----


@pytest.fixture
async def sem_with_fts(temp_dir: Path):
    """SemanticMemory with a real FTS index. No real bge-m3 model."""
    from yuanwork.memory.semantic import SemanticMemory
    from tests.unit.test_semantic_hybrid import _FakeEmbedder

    sm = SemanticMemory(data_dir=temp_dir, embedding_model="BAAI/bge-m3")
    await sm.initialize()
    sm._embedder = _FakeEmbedder()
    return sm


@pytest.mark.asyncio
async def test_semantic_memory_writes_to_fts(sem_with_fts):
    """add_chunks populates the FTS index automatically."""
    chunks = [
        {"id": "c1", "doc_id": "d1", "chunk_idx": 0, "text": "金额 1280元"},
        {"id": "c2", "doc_id": "d1", "chunk_idx": 1, "text": "日期 2026年10月15日"},
    ]
    await sem_with_fts.add_chunks(chunks)
    fts = sem_with_fts._get_fts_index()
    assert fts.count() == 2
    # FTS search should now find "1280"
    r = fts.search("1280", top_k=5)
    assert r and r[0][0] == "c1"


@pytest.mark.asyncio
async def test_semantic_memory_hybrid_uses_fts(sem_with_fts):
    """search_hybrid uses FTS for the sparse channel — pure-number queries find hits."""
    chunks = [
        {"id": "c_amount", "doc_id": "d1", "chunk_idx": 0, "text": "金额 1280元"},
        {"id": "c_date", "doc_id": "d1", "chunk_idx": 1, "text": "日期 2026年10月15日"},
        {"id": "c_rate", "doc_id": "d1", "chunk_idx": 2, "text": "税率 6%"},
    ]
    await sem_with_fts.add_chunks(chunks)

    # Hybrid search for "1280" should now find c_amount via FTS sparse
    results = await sem_with_fts.search_hybrid("1280", top_k=3)
    assert results
    # The FTS sub-score for c_amount should be > 0 (BM25 picked it up)
    c_amount = next((r for r in results if r["id"] == "c_amount"), None)
    assert c_amount is not None, "c_amount not in results"
    assert c_amount.get("_score_sparse", 0.0) > 0.0, (
        f"FTS sparse score for c_amount is 0 (FTS not wired correctly?): {c_amount}"
    )


@pytest.mark.asyncio
async def test_semantic_memory_delete_propagates_to_fts(sem_with_fts):
    """delete_by_doc_id removes chunks from both LanceDB and FTS."""
    chunks = [
        {"id": "c1", "doc_id": "d1", "chunk_idx": 0, "text": "alpha beta"},
        {"id": "c2", "doc_id": "d2", "chunk_idx": 0, "text": "gamma delta"},
    ]
    await sem_with_fts.add_chunks(chunks)
    fts = sem_with_fts._get_fts_index()
    assert fts.count() == 2

    await sem_with_fts.delete_by_doc_id("d1")
    # FTS should now only have d2's chunk
    assert fts.count() == 1
    r = fts.search("alpha", top_k=5)
    assert not r
    r = fts.search("gamma", top_k=5)
    assert r and r[0][0] == "c2"


@pytest.mark.asyncio
async def test_semantic_memory_migrates_existing_fts(sem_with_fts, temp_dir: Path):
    """A v3 KB (LanceDB only, no FTS) gets FTS rebuilt on first access."""
    chunks = [
        {"id": "c1", "doc_id": "d1", "chunk_idx": 0, "text": "项目 2024-Q3 总结"},
        {"id": "c2", "doc_id": "d1", "chunk_idx": 1, "text": "项目 2024-Q4 总结"},
    ]
    await sem_with_fts.add_chunks(chunks)
    # Now wipe the FTS index to simulate a v3 → v4 upgrade.
    # The flush comes first: the documents are in the writer's hands
    # until the index decides to flush them (TASK-064).
    import shutil
    await sem_with_fts.flush_fts()
    fts_dir = sem_with_fts._fts_index_dir
    shutil.rmtree(fts_dir)
    # Reset the lazy state so a new FTSIndex is created
    sem_with_fts._fts_index = None
    sem_with_fts._fts_needs_reindex = True

    # First FTS access should rebuild from LanceDB
    fts = sem_with_fts._get_fts_index()
    assert fts.count() == 2
    r = fts.search("2024-Q3", top_k=5)
    assert r and r[0][0] == "c1"
