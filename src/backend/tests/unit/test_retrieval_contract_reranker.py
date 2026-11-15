"""The reranker: how it scores, and how it fails.

The reranker is **not** wired into the chat path (the orchestrator
builds the retriever without one), but the module is still there and
its failure behaviour is what the retriever's fallback relies on.
"""

from __future__ import annotations

import asyncio

import pytest

from yuanwork.retrieval.reranker import Reranker


class FakeModel:
    """Stands in for the cross-encoder."""

    def __init__(self, scores=None, fail: bool = False):
        self.scores = scores
        self.fail = fail
        self.pairs: list[tuple[str, str]] = []

    def compute_score(self, pairs, normalize=True):
        self.pairs = list(pairs)
        if self.fail:
            raise RuntimeError("模型不可用")
        if self.scores is not None:
            return self.scores
        # score by length, so the order is predictable
        return [float(len(p[1])) for p in pairs]


def make_reranker(model) -> Reranker:
    reranker = Reranker(model_name="fake/model")
    reranker._model = model
    return reranker


def chunks(*texts: str) -> list[dict]:
    return [{"id": f"c{i}", "text": t} for i, t in enumerate(texts)]


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------


def test_an_empty_input_is_returned_unchanged():
    reranker = make_reranker(FakeModel())
    assert asyncio.run(reranker.rerank("q", [], top_k=5)) == []


def test_the_chunks_are_sorted_by_score():
    model = FakeModel(scores=[0.1, 0.9, 0.5])
    reranker = make_reranker(model)
    out = asyncio.run(reranker.rerank("q", chunks("a", "bb", "ccc"), top_k=3))
    assert [c["id"] for c in out] == ["c1", "c2", "c0"]


def test_each_chunk_gets_a_rerank_score():
    model = FakeModel(scores=[0.1, 0.9])
    reranker = make_reranker(model)
    out = asyncio.run(reranker.rerank("q", chunks("a", "b"), top_k=2))
    assert out[0]["rerank_score"] == 0.9


def test_the_input_dictionaries_are_not_mutated():
    model = FakeModel(scores=[0.1, 0.9])
    reranker = make_reranker(model)
    original = chunks("a", "b")
    asyncio.run(reranker.rerank("q", original, top_k=2))
    assert "rerank_score" not in original[0]
    assert "rerank_score" not in original[1]


def test_the_top_k_caps_the_result():
    model = FakeModel(scores=[0.1, 0.9, 0.5])
    reranker = make_reranker(model)
    out = asyncio.run(reranker.rerank("q", chunks("a", "bb", "ccc"), top_k=2))
    assert len(out) == 2


def test_the_query_and_the_text_are_paired():
    model = FakeModel()
    reranker = make_reranker(model)
    asyncio.run(reranker.rerank("问题", chunks("答案"), top_k=1))
    assert model.pairs == [("问题", "答案")]


def test_a_chunk_without_text_gets_an_empty_string():
    model = FakeModel()
    reranker = make_reranker(model)
    asyncio.run(reranker.rerank("q", [{"id": "c1"}], top_k=1))
    assert model.pairs == [("q", "")]


def test_a_citation_shaped_chunk_is_accepted():
    model = FakeModel()
    reranker = make_reranker(model)
    asyncio.run(
        reranker.rerank("q", [{"id": "c1", "citation_text": "引文"}], top_k=1)
    )
    assert model.pairs == [("q", "引文")]


# --------------------------------------------------------------------------
# the failure path
# --------------------------------------------------------------------------


def test_a_failing_model_falls_back_to_the_input_order():
    model = FakeModel(fail=True)
    reranker = make_reranker(model)
    out = asyncio.run(reranker.rerank("q", chunks("a", "b", "c"), top_k=2))
    assert [c["id"] for c in out] == ["c0", "c1"]


def test_a_failing_model_adds_no_rerank_score():
    model = FakeModel(fail=True)
    reranker = make_reranker(model)
    out = asyncio.run(reranker.rerank("q", chunks("a"), top_k=1))
    assert "rerank_score" not in out[0]


# --------------------------------------------------------------------------
# the wiring
# --------------------------------------------------------------------------


def test_the_model_is_not_loaded_until_it_is_needed():
    reranker = Reranker(model_name="fake/model")
    assert reranker.is_loaded() is False


def test_the_default_model_is_the_cross_encoder():
    reranker = Reranker()
    assert "bge-reranker-v2-m3" in reranker.model_name
