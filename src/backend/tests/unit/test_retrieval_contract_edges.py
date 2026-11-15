"""The retrieval layer's edge cases.

The interesting edges here are all about *what comes back* rather than
what goes in: an empty knowledge base, a single chunk, a query that
matches nothing, and a query made of nothing but whitespace.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from yuanwork.memory.semantic import SemanticMemory
from yuanwork.retrieval.confidence import ConfidenceScorer


class FakeEmbedder:
    """A fixed vector for everything — enough to exercise the plumbing."""

    def __init__(self, dim: int = 1024):
        self.dim = dim
        self.calls = 0

    def encode(self, batch, return_dense=True, return_sparse=False, return_colbert_vecs=False):
        self.calls += 1
        return {
            "dense_vecs": [[0.1] * self.dim for _ in batch],
            "lexical_weights": [{} for _ in batch],
        }


def make_chunk(cid: str, text: str, doc_id: str = "doc1") -> dict:
    return {
        "id": cid,
        "doc_id": doc_id,
        "chunk_idx": 1,
        "text": text,
        "type": "text",
        "meta": {},
        "file_name": "a.md",
        "file_path": f"/tmp/{cid}.md",
        "file_type": "md",
    }


@pytest.fixture
def empty_store(tmp_path: Path):
    """A semantic memory with an empty table and a fake embedder."""
    memory = SemanticMemory(data_dir=tmp_path / "lancedb")
    memory._embedder = FakeEmbedder()

    async def build():
        await memory.initialize()
        return memory

    memory = asyncio.run(build())
    yield memory
    try:
        asyncio.run(memory.close())
    except Exception:
        pass


@pytest.fixture
def one_chunk_store(empty_store):
    async def add():
        await empty_store.add_chunks([make_chunk("c1", "唯一 的 片段")])

    asyncio.run(add())
    return empty_store


def search(memory, query: str, **kwargs):
    async def run():
        return await memory.search_hybrid(query, top_k=10, **kwargs)

    return asyncio.run(run())


# --------------------------------------------------------------------------
# an empty knowledge base
# --------------------------------------------------------------------------


def test_an_empty_store_returns_nothing(empty_store):
    assert search(empty_store, "发票") == []


def test_an_empty_store_does_not_load_the_embedding_model(empty_store):
    """The model is lazy, and an empty knowledge base must not trigger
    a 2.3 GB download."""
    assert empty_store._embedder.calls == 0
    search(empty_store, "发票")
    assert empty_store._embedder.calls == 0


def test_an_empty_store_scores_zero(empty_store):
    out = ConfidenceScorer().score(search(empty_store, "发票"))
    assert out["confidence"] == 0.0
    assert out["evidence_count"] == 0


def test_an_empty_store_refuses_at_the_gate(empty_store):
    """The refusal decision lives in the evidence gate, not in the score."""
    from yuanwork.retrieval.evidence_gate import REASON_INSUFFICIENT, check_hard_rule

    out = check_hard_rule(search(empty_store, "发票"))
    assert out.ok is False
    assert out.reason == REASON_INSUFFICIENT


# --------------------------------------------------------------------------
# a single chunk
# --------------------------------------------------------------------------


def test_a_single_chunk_is_returned(one_chunk_store):
    rows = search(one_chunk_store, "唯一")
    assert len(rows) == 1
    assert rows[0]["id"] == "c1"


def test_a_single_chunk_refuses_at_the_gate(one_chunk_store):
    """One piece of evidence is not enough to back an answer, whatever
    the score says."""
    from yuanwork.retrieval.evidence_gate import REASON_INSUFFICIENT, check_hard_rule

    out = check_hard_rule(search(one_chunk_store, "唯一"))
    assert out.ok is False
    assert out.reason == REASON_INSUFFICIENT


# --------------------------------------------------------------------------
# degenerate queries
# --------------------------------------------------------------------------


def test_an_empty_query_still_returns_the_dense_neighbour(one_chunk_store):
    """Only the full-text channel guards against a degenerate query; the
    dense channel encodes whatever it is given and returns the nearest
    neighbour."""
    rows = search(one_chunk_store, "")
    assert len(rows) == 1
    assert rows[0]["id"] == "c1"
    # the full-text channel contributed nothing
    assert rows[0]["_score_sparse"] == 0.0


def test_a_whitespace_query_still_returns_the_dense_neighbour(one_chunk_store):
    rows = search(one_chunk_store, "   ")
    assert len(rows) == 1
    assert rows[0]["_score_sparse"] == 0.0


def test_a_punctuation_only_query_still_returns_the_dense_neighbour(one_chunk_store):
    rows = search(one_chunk_store, "???")
    assert len(rows) == 1
    assert rows[0]["_score_sparse"] == 0.0


def test_a_query_with_no_match_still_returns_the_dense_neighbours(one_chunk_store):
    """There is no relevance floor, so the nearest neighbour comes back
    even when it has nothing to do with the question."""
    rows = search(one_chunk_store, "完全 无关 的 问题")
    assert len(rows) == 1


# --------------------------------------------------------------------------
# the filter and the floor
# --------------------------------------------------------------------------


def test_a_document_filter_narrows_the_result(one_chunk_store):
    assert search(one_chunk_store, "唯一", filter_expr="doc_id IN ('doc1')")
    assert search(one_chunk_store, "唯一", filter_expr="doc_id IN ('other')") == []


def test_the_minimum_score_is_not_applied_by_default(one_chunk_store):
    """The production call site passes no floor, so a weak hit still
    comes back."""
    rows = search(one_chunk_store, "唯一")
    assert rows[0]["_score"] >= 0.0


# --------------------------------------------------------------------------
# the timeouts
# --------------------------------------------------------------------------


def test_the_timeouts_are_configuration(tmp_path: Path):
    """A step of the retrieval chain that runs over its budget has to
    stop - the user is waiting."""
    from yuanwork.memory.semantic import SemanticMemory

    memory = SemanticMemory(
        data_dir=tmp_path / "lancedb",
        dense_timeout=1.5,
        sparse_timeout=0.5,
        embedding_timeout=7.0,
    )

    assert memory.dense_timeout == 1.5
    assert memory.sparse_timeout == 0.5
    assert memory.embedding_timeout == 7.0


def test_the_timeouts_come_from_the_configuration(tmp_path: Path):
    from yuanwork.config import get_settings
    from yuanwork.memory.semantic import SemanticMemory

    memory = SemanticMemory(data_dir=tmp_path / "lancedb")
    settings = get_settings()

    assert memory.dense_timeout == settings.dense_timeout_seconds
    assert memory.sparse_timeout == settings.sparse_timeout_seconds
    assert memory.embedding_timeout == settings.embedding_timeout_seconds


def _slow_table(memory, delay: float = 0.5):
    """A table whose search takes longer than the budget."""
    import time

    table = memory.get_or_create_chunks_table()

    class _Slow:
        def count_rows(self):
            return table.count_rows()

        def search(self, *args, **kwargs):
            time.sleep(delay)
            return table.search(*args, **kwargs)

    memory.get_or_create_chunks_table = lambda: _Slow()


def test_a_dense_channel_that_times_out_fails_the_query(one_chunk_store):
    """The dense channel is the one that cannot be dropped - a timeout
    has to fail the query, not quietly return nothing."""
    one_chunk_store.dense_timeout = 0.01
    _slow_table(one_chunk_store)

    with pytest.raises(TimeoutError):
        search(one_chunk_store, "唯一")


def test_a_full_text_channel_that_times_out_degrades_to_dense(one_chunk_store):
    """The full-text channel can be dropped: the query still answers
    from the dense channel alone (design 4.3)."""
    import time

    one_chunk_store.sparse_timeout = 0.01

    class _SlowFTS:
        def search(self, query, limit):
            time.sleep(0.5)
            return []

    one_chunk_store._fts_index = _SlowFTS()

    rows = search(one_chunk_store, "唯一")

    assert len(rows) == 1
    assert rows[0]["id"] == "c1"
    assert rows[0]["_score_sparse"] == 0.0


def test_an_embedding_that_times_out_fails_the_query(one_chunk_store):
    """Without the query's vector there is nothing to search with."""
    import time

    one_chunk_store.embedding_timeout = 0.01

    class _SlowEmbedder:
        def encode(self, batch, **kwargs):
            time.sleep(0.5)
            return {
                "dense_vecs": [[0.0] * 1024 for _ in batch],
                "lexical_weights": [{} for _ in batch],
            }

    one_chunk_store._embedder = _SlowEmbedder()

    with pytest.raises(TimeoutError):
        search(one_chunk_store, "唯一")
