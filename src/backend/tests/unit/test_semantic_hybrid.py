"""Tests for the Stage 3 hybrid retrieval additions in SemanticMemory.

Covers:
  - `embed_texts` returns a (dense, sparse) tuple
  - `_sparse_dot` static method
  - `_sparse_to_json` / `_sparse_from_json` round-trip
  - `search_hybrid` returns results with both _score_dense and _score_sparse
  - back-compat: `search` still works (pure dense, unchanged signature)
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ---- Unit tests on pure functions (no model load needed) ----


def test_sparse_to_json_roundtrip():
    """Sparse <-> JSON round-trip preserves the dict."""
    from yuanwork.memory.semantic import SemanticMemory

    sparse = {"token_a": 0.7, "token_b": 0.3, "token_c": 0.0}
    s = SemanticMemory._sparse_to_json(sparse)
    assert isinstance(s, str)
    assert SemanticMemory._sparse_from_json(s) == {"token_a": 0.7, "token_b": 0.3}


def test_sparse_to_json_filters_zero_weights():
    """Zero-weight tokens are dropped to keep the column small."""
    from yuanwork.memory.semantic import SemanticMemory

    sparse = {"a": 0.5, "b": 0.0, "c": 0.1}
    s = SemanticMemory._sparse_to_json(sparse)
    out = SemanticMemory._sparse_from_json(s)
    assert "b" not in out
    assert out == {"a": 0.5, "c": 0.1}


def test_sparse_to_json_empty():
    """None / empty / zero-only → empty string → empty dict."""
    from yuanwork.memory.semantic import SemanticMemory

    assert SemanticMemory._sparse_to_json(None) == ""
    assert SemanticMemory._sparse_to_json({}) == ""
    assert SemanticMemory._sparse_from_json("") == {}
    assert SemanticMemory._sparse_from_json(None) == {}


def test_sparse_to_json_invalid_input_graceful():
    """Bad JSON doesn't crash; returns empty dict."""
    from yuanwork.memory.semantic import SemanticMemory

    assert SemanticMemory._sparse_from_json("not json") == {}
    assert SemanticMemory._sparse_from_json("[]") == {}  # not a dict
    assert SemanticMemory._sparse_from_json('{"a": "bad"}') == {}


def test_sparse_dot_shared_tokens():
    """Sparse dot = sum of q[w] * d[w] for shared tokens."""
    from yuanwork.memory.semantic import SemanticMemory

    q = {"金额": 0.8, "1280": 0.6}
    d = {"金额": 0.5, "1280": 0.4, "日期": 0.3}
    # shared: 金额, 1280
    expected = 0.8 * 0.5 + 0.6 * 0.4  # 0.40 + 0.24 = 0.64
    assert abs(SemanticMemory._sparse_dot(q, d) - expected) < 1e-9


def test_sparse_dot_disjoint_tokens():
    """No shared tokens → 0."""
    from yuanwork.memory.semantic import SemanticMemory

    assert SemanticMemory._sparse_dot({"a": 1.0}, {"b": 1.0}) == 0.0
    assert SemanticMemory._sparse_dot({}, {"a": 1.0}) == 0.0
    assert SemanticMemory._sparse_dot({"a": 1.0}, {}) == 0.0


def test_sparse_dot_order_independent():
    """The dot product is symmetric in the inputs."""
    from yuanwork.memory.semantic import SemanticMemory

    a = {"x": 0.5, "y": 0.3}
    b = {"x": 0.4, "z": 0.2}
    assert SemanticMemory._sparse_dot(a, b) == SemanticMemory._sparse_dot(b, a)


# ---- Integration tests with a fake embedder (no real model load) ----


class _FakeEmbedder:
    """Tiny fake BGEM3FlagModel that returns deterministic dense + sparse.

    Both channels use a shared bag-of-tokens representation (CJK char-level
    + word-level) so that lexically-similar texts get similar dense vectors
    AND meaningful sparse weights. This is enough to exercise the
    search_hybrid fusion without loading a 2.3GB model.
    """

    @staticmethod
    def _bag_of_tokens(text: str) -> dict[str, float]:
        """CJK char-level + word-level token bag."""
        weights: dict[str, float] = {}
        for ch in text:
            if "一" <= ch <= "鿿":
                weights[ch] = weights.get(ch, 0.0) + 1.0
        for word in text.replace(":", " ").replace("，", " ").replace(",", " ").split():
            if word.strip():
                weights[word] = weights.get(word, 0.0) + 0.5
        return weights

    def encode(self, texts, return_dense=True, return_sparse=False, return_colbert_vecs=False):
        import numpy as np

        # Build a shared vocab so dense vectors live in the same space
        vocab: dict[str, int] = {}
        for t in texts:
            for tok in self._bag_of_tokens(t):
                if tok not in vocab:
                    vocab[tok] = len(vocab)
        dim = max(1024, len(vocab))

        dense = []
        for t in texts:
            v = np.zeros(dim, dtype=np.float32)
            for tok, w in self._bag_of_tokens(t).items():
                v[vocab[tok]] = w
            norm = np.linalg.norm(v) or 1.0
            v = v / norm
            # Pad / truncate to 1024
            if len(v) > 1024:
                v = v[:1024]
            else:
                pad = np.zeros(1024 - len(v), dtype=np.float32)
                v = np.concatenate([v, pad])
            dense.append(v.tolist())
        result = {"dense_vecs": np.array(dense, dtype=np.float32)}

        if return_sparse:
            sparse_list = [self._bag_of_tokens(t) for t in texts]
            result["lexical_weights"] = sparse_list
        return result


@pytest.fixture
async def fake_semantic_memory(temp_dir: Path):
    """SemanticMemory with the real embedder swapped for a fake."""
    from yuanwork.memory.semantic import SemanticMemory

    sm = SemanticMemory(data_dir=temp_dir, embedding_model="BAAI/bge-m3")
    await sm.initialize()
    sm._embedder = _FakeEmbedder()
    return sm


@pytest.mark.asyncio
async def test_embed_texts_returns_dense_and_sparse(fake_semantic_memory):
    """embed_texts should return (dense, sparse) when sparse is enabled."""
    sm = fake_semantic_memory
    dense, sparse = await sm.embed_texts(["发票金额 1280元"], return_sparse=True)
    assert len(dense) == 1
    assert len(dense[0]) == 1024
    assert isinstance(sparse, list) and len(sparse) == 1
    # Sparse dict should contain token weights
    assert "1280" in sparse[0] or any("1280" in str(k) for k in sparse[0])


@pytest.mark.asyncio
async def test_embed_texts_sparse_optional(fake_semantic_memory):
    """When return_sparse=False, sparse list is None."""
    sm = fake_semantic_memory
    dense, sparse = await sm.embed_texts(["hello"], return_sparse=False)
    assert len(dense) == 1
    assert sparse is None


@pytest.mark.asyncio
async def test_add_chunks_leaves_the_sparse_column_empty_by_default(
    fake_semantic_memory,
):
    """The bge-m3 sparse channel is not read by the retrieval side.

    The sparse channel of the retrieval is the full-text index (BM25),
    so the weights are not computed on the way in - and the column
    stays empty. Computing them on every slice was a big part of the
    embedding bill on the CPU (TASK-055).
    """
    sm = fake_semantic_memory
    chunks = [
        {"id": "c1", "doc_id": "d1", "chunk_idx": 0, "text": "金额:1280元"},
        {"id": "c2", "doc_id": "d1", "chunk_idx": 1, "text": "日期:2026-10-15"},
    ]
    await sm.add_chunks(chunks)
    # Inspect the underlying table rows
    table = sm.get_or_create_chunks_table()
    # LanceTable: use to_pandas() for full scan
    rows = table.to_pandas().to_dict("records")
    assert len(rows) == 2
    by_id = {r["id"]: r for r in rows}
    assert "sparse_str" in by_id["c1"]
    assert not by_id["c1"]["sparse_str"]


@pytest.mark.asyncio
async def test_the_sparse_column_is_filled_when_the_channel_is_on(
    fake_semantic_memory,
):
    """One setting away from being filled again."""
    sm = fake_semantic_memory
    sm._sparse_enabled = True
    chunks = [
        {"id": "c1", "doc_id": "d1", "chunk_idx": 0, "text": "金额:1280元"},
        {"id": "c2", "doc_id": "d1", "chunk_idx": 1, "text": "日期:2026-10-15"},
    ]
    await sm.add_chunks(chunks)
    table = sm.get_or_create_chunks_table()
    rows = table.to_pandas().to_dict("records")
    by_id = {r["id"]: r for r in rows}
    parsed = json.loads(by_id["c1"]["sparse_str"])
    assert isinstance(parsed, dict)
    # At least one CJK char should have a weight
    cjk_tokens = [t for t in parsed if "一" <= t <= "鿿" or len(t) == 1 and "一" <= t <= "鿿"]
    assert len(cjk_tokens) >= 1


@pytest.mark.asyncio
async def test_search_hybrid_returns_both_subscores(fake_semantic_memory):
    """search_hybrid should expose _score, _score_dense, and _score_sparse."""
    sm = fake_semantic_memory
    chunks = [
        {"id": "c1", "doc_id": "d1", "chunk_idx": 0, "text": "金额:1280元"},
        {"id": "c2", "doc_id": "d1", "chunk_idx": 1, "text": "日期:2026年10月15日"},
        {"id": "c3", "doc_id": "d1", "chunk_idx": 2, "text": "税率:6%"},
    ]
    await sm.add_chunks(chunks)

    results = await sm.search_hybrid("1280", top_k=3)
    assert len(results) >= 1
    r0 = results[0]
    assert "_score" in r0
    assert "_score_dense" in r0
    assert "_score_sparse" in r0
    # Combined score should be a weighted sum of dense + sparse
    assert 0.0 <= r0["_score"] <= 1.0


@pytest.mark.asyncio
async def test_search_hybrid_favors_literal_overlap(fake_semantic_memory):
    """When query contains a literal token that exactly matches a chunk,
    the hybrid search should rank that chunk highly (sparse contribution)."""
    sm = fake_semantic_memory
    chunks = [
        {"id": "c_amount", "doc_id": "d1", "chunk_idx": 0, "text": "金额:1280元"},
        {"id": "c_date", "doc_id": "d1", "chunk_idx": 1, "text": "日期:2026年10月15日"},
        {"id": "c_rate", "doc_id": "d1", "chunk_idx": 2, "text": "税率:6%"},
    ]
    await sm.add_chunks(chunks)

    results = await sm.search_hybrid("1280", top_k=3)
    top_id = results[0]["id"]
    # The chunk literally containing "1280" should be on top
    assert top_id == "c_amount", f"expected c_amount on top, got {top_id}"


@pytest.mark.asyncio
async def test_search_pure_dense_back_compat(fake_semantic_memory):
    """The original `search` method still returns dense-only results."""
    sm = fake_semantic_memory
    chunks = [
        {"id": "c1", "doc_id": "d1", "chunk_idx": 0, "text": "金额:1280元"},
        {"id": "c2", "doc_id": "d1", "chunk_idx": 1, "text": "日期:2026年10月15日"},
    ]
    await sm.add_chunks(chunks)
    results = await sm.search("1280", top_k=2)
    assert len(results) >= 1
    # search() returns _score (the dense-derived one), no sub-scores
    r0 = results[0]
    assert "_score" in r0
    # _score_dense / _score_sparse are only set by search_hybrid
    assert "_score_dense" not in r0 or r0.get("_score_dense") is None or "_score_dense" not in r0
