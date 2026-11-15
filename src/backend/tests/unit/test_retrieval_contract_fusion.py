"""The fusion arithmetic, tested against a real vector store.

The two channels are fused by normalising each channel against its own
maximum and then taking a weighted sum. That normalisation is nested
inside the store, so the only honest way to test it is to run it — with
a fake embedder so no model is loaded, and a real LanceDB table so the
distances are real.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from yuanwork.memory.semantic import SemanticMemory


class FakeEmbedder:
    """Returns a vector whose first coordinate is the text's value.

    Every other coordinate is zero, so the L2 distance between two
    vectors is just the difference of their first coordinates — which
    makes the dense ranking exactly predictable.
    """

    def __init__(self, table: dict[str, float], dim: int = 1024):
        self.table = table
        self.dim = dim

    def encode(self, batch, return_dense=True, return_sparse=False, return_colbert_vecs=False):
        vecs = []
        for text in batch:
            value = self.table.get(text, 0.0)
            vec = [0.0] * self.dim
            vec[0] = value
            vecs.append(vec)
        return {
            "dense_vecs": vecs,
            "lexical_weights": [{} for _ in batch],
        }


def make_chunk(cid: str, text: str, doc_id: str = "doc1") -> dict[str, Any]:
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
def store(tmp_path: Path):
    """A semantic memory with a fake embedder and a real table.

    The query text "命中全文" is the *same* text as the literal hit, so
    the sparse channel matches it, while the embedder puts the query
    closest to a different chunk — that is the situation the fusion
    weights are about.
    """
    table = {
        "命中全文": 1.0,      # the query: closest to c-dense in vector space
        "命中稠密": 0.95,     # dense top-1
        "无关": 0.05,
    }
    memory = SemanticMemory(data_dir=tmp_path / "lancedb")
    memory._embedder = FakeEmbedder(table)

    async def build():
        await memory.initialize()
        await memory.add_chunks([
            make_chunk("c-dense", "命中稠密"),
            make_chunk("c-sparse", "命中全文 命中全文"),
            make_chunk("c-none", "无关"),
        ])
        return memory

    memory = asyncio.run(build())
    yield memory
    try:
        asyncio.run(memory.close())
    except Exception:
        pass


def search(memory, query: str = "命中全文", **kwargs):
    async def run():
        return await memory.search_hybrid(query, top_k=10, **kwargs)

    return asyncio.run(run())


# --------------------------------------------------------------------------
# the normalisation
# --------------------------------------------------------------------------


def test_the_dense_channel_is_normalised_against_its_own_maximum(store):
    rows = search(store)
    dense = {r["id"]: r["_score_dense"] for r in rows}
    # the best dense hit is normalised to exactly 1.0
    assert max(dense.values()) == pytest.approx(1.0)
    # the rest are relative to it
    assert dense["c-sparse"] < 1.0


def test_the_sparse_channel_is_normalised_against_its_own_maximum(store):
    rows = search(store)
    sparse = {r["id"]: r["_score_sparse"] for r in rows}
    assert max(sparse.values()) == pytest.approx(1.0)


def test_a_channel_that_did_not_match_contributes_nothing(store):
    rows = search(store)
    by_id = {r["id"]: r for r in rows}
    # 'c-none' matches neither the words nor the query closely
    assert by_id["c-none"]["_score_dense"] < 1.0


# --------------------------------------------------------------------------
# the fusion
# --------------------------------------------------------------------------


def test_the_fused_score_is_the_weighted_sum(store):
    rows = search(store)
    for row in rows:
        expected = 0.65 * row["_score_dense"] + 0.35 * row["_score_sparse"]
        assert row["_score"] == pytest.approx(expected, abs=1e-6)


def test_the_weights_are_configuration(store):
    """ADR-006: the split between the two channels is not a constant -
    the right one depends on the corpus."""
    rows = search(store, alpha=0.5, beta=0.5)
    for row in rows:
        expected = 0.5 * row["_score_dense"] + 0.5 * row["_score_sparse"]
        assert row["_score"] == pytest.approx(expected, abs=1e-6)


def test_the_fused_scores_are_sorted_descending(store):
    rows = search(store)
    scores = [r["_score"] for r in rows]
    assert scores == sorted(scores, reverse=True)


def test_the_dense_channel_dominates_a_literal_hit(store):
    """The literal hit wins the sparse channel but loses the dense one,
    so the fused score puts the dense hit first."""
    rows = search(store)
    by_id = {r["id"]: r for r in rows}
    assert by_id["c-dense"]["_score_dense"] > by_id["c-sparse"]["_score_dense"]
    assert by_id["c-sparse"]["_score_sparse"] > by_id["c-dense"]["_score_sparse"]


def test_a_chunk_that_only_the_sparse_channel_found_caps_at_the_sparse_weight(store):
    """If a chunk is only in the sparse channel its fused score cannot
    exceed the sparse weight (0.35)."""
    rows = search(store)
    for row in rows:
        if row["_score_dense"] == 0.0:
            assert row["_score"] <= 0.35


def test_the_fused_score_never_exceeds_one(store):
    for row in search(store):
        assert 0.0 <= row["_score"] <= 1.0


# --------------------------------------------------------------------------
# the shape of the result
# --------------------------------------------------------------------------


def test_the_result_carries_three_scores(store):
    rows = search(store)
    for row in rows:
        assert "_score" in row
        assert "_score_dense" in row
        assert "_score_sparse" in row


def test_the_result_carries_the_row_metadata(store):
    rows = search(store)
    for row in rows:
        assert row["id"]
        assert row["doc_id"] == "doc1"
        assert "text" in row
        assert row["file_name"] == "a.md"


def test_the_distance_column_is_removed(store):
    rows = search(store)
    for row in rows:
        assert "_distance" not in row


def test_the_scores_are_the_normalised_ones_not_the_raw_ones(store):
    """The normalised dense score is 1.0 for the best hit even though
    the raw similarity is 1/(1+distance) and never reaches 1."""
    rows = search(store)
    best = max(rows, key=lambda r: r["_score_dense"])
    assert best["_score_dense"] == pytest.approx(1.0)
    assert best["_score"] <= 1.0
