"""The hybrid retriever's steps and parameters.

The retriever is the only thing between the orchestrator and the two
recall channels, so what it passes down and what it hands back is worth
pinning:

* the recall size, the filters, the weights
* the order of the steps
* what comes back
"""

from __future__ import annotations

from typing import Any

from yuanwork.retrieval.hybrid import HybridRetriever


class RecordingSemanticMemory:
    """Records what the retriever asked for."""

    def __init__(self, results: list[dict] | None = None):
        self.results = results if results is not None else []
        self.calls: list[dict[str, Any]] = []

    async def search_hybrid(self, **kwargs):
        self.calls.append({"kind": "hybrid", **kwargs})
        return list(self.results)

    async def search(self, **kwargs):
        self.calls.append({"kind": "dense", **kwargs})
        return list(self.results)


class RecordingReranker:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    async def rerank(self, query, chunks, top_k=10):
        self.calls.append({"query": query, "top_k": top_k})
        if self.fail:
            raise RuntimeError("重排模型炸了")
        return chunks[:top_k]


# --------------------------------------------------------------------------
# the parameters
# --------------------------------------------------------------------------


def test_the_default_weights():
    retriever = HybridRetriever(semantic_memory=object())  # type: ignore[arg-type]
    assert retriever.alpha == 0.65
    assert retriever.beta == 0.35


def test_the_weights_are_passed_to_the_store():
    import asyncio

    semantic = RecordingSemanticMemory()
    retriever = HybridRetriever(semantic_memory=semantic)  # type: ignore[arg-type]
    asyncio.run(retriever.search("q", top_k=10))

    call = semantic.calls[0]
    assert call["kind"] == "hybrid"
    assert call["alpha"] == 0.65
    assert call["beta"] == 0.35


def test_the_recall_size_is_the_larger_of_top_k_and_fifty():
    import asyncio

    semantic = RecordingSemanticMemory()
    retriever = HybridRetriever(semantic_memory=semantic)  # type: ignore[arg-type]
    asyncio.run(retriever.search("q", top_k=10))

    assert semantic.calls[0]["top_k"] == 50
    assert semantic.calls[0]["recall_k"] == 50


def test_a_large_top_k_wins_over_the_initial_recall():
    import asyncio

    semantic = RecordingSemanticMemory()
    retriever = HybridRetriever(semantic_memory=semantic)  # type: ignore[arg-type]
    asyncio.run(retriever.search("q", top_k=80))

    assert semantic.calls[0]["top_k"] == 80


def test_the_recall_size_does_not_depend_on_the_reranker():
    """The first stage always fetches the configured number of
    candidates - the cross-encoder is a second stage, not a reason to
    recall less. The full-text channel draws from the same set, so a
    smaller recall would make the fusion worse."""
    import asyncio

    semantic = RecordingSemanticMemory()
    retriever = HybridRetriever(
        semantic_memory=semantic, use_reranker=False  # type: ignore[arg-type]
    )
    asyncio.run(retriever.search("q", top_k=10))

    assert semantic.calls[0]["top_k"] == 50


def test_the_hybrid_channel_can_be_switched_off():
    import asyncio

    semantic = RecordingSemanticMemory()
    retriever = HybridRetriever(
        semantic_memory=semantic, use_hybrid=False  # type: ignore[arg-type]
    )
    asyncio.run(retriever.search("q", top_k=10))

    assert semantic.calls[0]["kind"] == "dense"


# --------------------------------------------------------------------------
# the filters
# --------------------------------------------------------------------------


def test_no_filter_means_no_filter_expression():
    import asyncio

    semantic = RecordingSemanticMemory()
    retriever = HybridRetriever(semantic_memory=semantic)  # type: ignore[arg-type]
    asyncio.run(retriever.search("q", top_k=10))

    assert semantic.calls[0]["filter_expr"] is None


def test_a_document_filter_becomes_an_in_clause():
    import asyncio

    semantic = RecordingSemanticMemory()
    retriever = HybridRetriever(semantic_memory=semantic)  # type: ignore[arg-type]
    asyncio.run(retriever.search("q", top_k=10, doc_ids=["d1", "d2"]))

    assert semantic.calls[0]["filter_expr"] == "doc_id IN ('d1', 'd2')"


def test_a_file_type_filter_becomes_an_in_clause():
    import asyncio

    semantic = RecordingSemanticMemory()
    retriever = HybridRetriever(semantic_memory=semantic)  # type: ignore[arg-type]
    asyncio.run(retriever.search("q", top_k=10, file_types=["pdf", "pptx"]))

    assert semantic.calls[0]["filter_expr"] == "file_type IN ('pdf', 'pptx')"


def test_both_filters_are_joined_with_and():
    import asyncio

    semantic = RecordingSemanticMemory()
    retriever = HybridRetriever(semantic_memory=semantic)  # type: ignore[arg-type]
    asyncio.run(
        retriever.search("q", top_k=10, doc_ids=["d1"], file_types=["pdf"])
    )

    expr = semantic.calls[0]["filter_expr"]
    assert "doc_id IN ('d1')" in expr
    assert "file_type IN ('pdf')" in expr
    assert " AND " in expr


# --------------------------------------------------------------------------
# what comes back
# --------------------------------------------------------------------------


def test_the_result_is_truncated_to_top_k():
    import asyncio

    rows = [{"_score": 0.9 - i * 0.01, "id": f"c{i}"} for i in range(30)]
    semantic = RecordingSemanticMemory(rows)
    retriever = HybridRetriever(semantic_memory=semantic)  # type: ignore[arg-type]
    out = asyncio.run(retriever.search("q", top_k=10))

    assert len(out) == 10


def test_the_minimum_score_filters_results():
    import asyncio

    rows = [{"_score": 0.9}, {"_score": 0.5}, {"_score": 0.1}]
    semantic = RecordingSemanticMemory(rows)
    retriever = HybridRetriever(semantic_memory=semantic)  # type: ignore[arg-type]
    out = asyncio.run(retriever.search("q", top_k=10, min_score=0.4))

    assert [r["_score"] for r in out] == [0.9, 0.5]


def test_the_default_minimum_score_filters_nothing():
    import asyncio

    rows = [{"_score": 0.01}]
    semantic = RecordingSemanticMemory(rows)
    retriever = HybridRetriever(semantic_memory=semantic)  # type: ignore[arg-type]
    out = asyncio.run(retriever.search("q", top_k=10))

    assert len(out) == 1


def test_a_result_without_a_score_is_kept_by_the_default_filter():
    import asyncio

    semantic = RecordingSemanticMemory([{"id": "c1"}])
    retriever = HybridRetriever(semantic_memory=semantic)  # type: ignore[arg-type]
    out = asyncio.run(retriever.search("q", top_k=10))

    assert len(out) == 1


# --------------------------------------------------------------------------
# the reranker branch
# --------------------------------------------------------------------------


def test_the_reranker_is_called_when_one_is_supplied():
    import asyncio

    semantic = RecordingSemanticMemory([{"_score": 0.9}, {"_score": 0.8}])
    reranker = RecordingReranker()
    retriever = HybridRetriever(
        semantic_memory=semantic, reranker=reranker  # type: ignore[arg-type]
    )
    asyncio.run(retriever.search("q", top_k=5))

    assert len(reranker.calls) == 1
    assert reranker.calls[0]["top_k"] == 5


def test_no_reranker_means_no_rerank_call():
    import asyncio

    semantic = RecordingSemanticMemory([{"_score": 0.9}])
    reranker = RecordingReranker()
    retriever = HybridRetriever(
        semantic_memory=semantic, reranker=reranker  # type: ignore[arg-type]
    )
    retriever.reranker = None
    asyncio.run(retriever.search("q", top_k=5))

    assert reranker.calls == []


def test_a_failing_reranker_falls_back_to_the_vector_order():
    import asyncio

    rows = [{"_score": 0.9, "id": "a"}, {"_score": 0.8, "id": "b"}]
    semantic = RecordingSemanticMemory(rows)
    reranker = RecordingReranker(fail=True)
    retriever = HybridRetriever(
        semantic_memory=semantic, reranker=reranker  # type: ignore[arg-type]
    )
    out = asyncio.run(retriever.search("q", top_k=5))

    assert [r["id"] for r in out] == ["a", "b"]


def test_the_search_by_document_helper_exists():
    retriever = HybridRetriever(semantic_memory=object())  # type: ignore[arg-type]
    assert hasattr(retriever, "search_by_doc")
    assert hasattr(retriever, "search_by_metadata")
