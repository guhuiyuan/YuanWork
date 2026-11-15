"""Hybrid retrieval - vector + lexical + reranking.

Stage 1: vector search with optional LanceDB filter expression.
Stage 2: rerank top candidates with bge-reranker-v2-m3.
Stage 3: add bge-m3 sparse (lexical) channel and fuse with dense
         via weighted sum. This rescues literal-token queries
         ("1280", "INV-2024-001", "金额") that pure dense retrieval
         underweights.
"""

from __future__ import annotations

import logging
from typing import Any

from yuanwork.memory.semantic import SemanticMemory
from yuanwork.retrieval.reranker import Reranker
from yuanwork.utils.chunk_meta import chunk_location, chunk_meta

logger = logging.getLogger(__name__)


#: How much text around the term a context snippet holds.
CONTEXT_WIDTH = 120

#: What ends a sentence. The context of a term is a whole sentence if
#: one fits - a phrase cut out of the middle of a sentence says less.
_SENTENCE_END = "。！？!?；;\n"


def context_around(text: str, term: str, width: int = CONTEXT_WIDTH) -> str:
    """The passage of the text around the term.

    The sentence the term is in, when it is not too long - otherwise the
    text around the term, cut off with an ellipsis.
    """
    if not text or not term:
        return ""
    index = text.find(term)
    if index < 0:
        return ""

    # The sentence the term stands in.
    start = 0
    for position in range(index - 1, -1, -1):
        if text[position] in _SENTENCE_END:
            start = position + 1
            break
    end = len(text)
    for position in range(index + len(term), len(text)):
        if text[position] in _SENTENCE_END:
            end = position + 1
            break

    snippet = text[start:end].strip()
    if len(snippet) > width * 2:
        # A sentence that is too long: cut around the term instead.
        cut_start = max(0, index - width)
        cut_end = min(len(text), index + len(term) + width)
        snippet = text[cut_start:cut_end].strip()
        if cut_start > 0:
            snippet = "…" + snippet
        if cut_end < len(text):
            snippet = snippet + "…"
    return snippet


class HybridRetriever:
    """Hybrid retrieval: dense + sparse recall + optional cross-encoder rerank.

    Stage 3 default: calls `search_hybrid()` on the semantic store
    (which fuses bge-m3 dense + bge-m3 sparse). Then optionally
    reranks the fused top-K with a cross-encoder.
    """

    def __init__(
        self,
        semantic_memory: SemanticMemory,
        reranker: Reranker | None = None,
        use_reranker: bool | None = None,
        top_k_initial: int | None = None,
        top_k_rerank: int | None = None,
        use_hybrid: bool = True,
        alpha: float | None = None,
        beta: float | None = None,
    ):
        self.semantic = semantic_memory
        self.use_hybrid_flag = use_hybrid
        # Everything that shapes the retrieval is configuration: the
        # recall size, whether the cross-encoder runs, and the split
        # between the two channels (ADR-006).
        from yuanwork.config import get_settings

        settings = get_settings()
        if alpha is None:
            alpha = settings.fusion_alpha
        if beta is None:
            beta = settings.fusion_beta
        if use_reranker is None:
            use_reranker = settings.use_reranker
        if top_k_initial is None:
            top_k_initial = settings.top_k_initial
        if top_k_rerank is None:
            top_k_rerank = settings.top_k_rerank
        self.top_k_initial = top_k_initial
        self.top_k_rerank = top_k_rerank
        self.alpha = alpha
        self.beta = beta

        if reranker is None and use_reranker:
            # The model itself loads on the first rerank, not here.
            reranker = Reranker(model_name=settings.reranker_model)
        self.reranker = reranker
        # A reranker that was handed in is an explicit request; one that
        # was built from the setting follows it.
        self.use_reranker_flag = use_reranker or reranker is not None

    async def search(
        self,
        query: str,
        top_k: int = 10,
        doc_ids: list[str] | None = None,
        file_types: list[str] | None = None,
        min_score: float = 0.0,
        exclude_doc_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Search with optional filters.

        Args:
            query: Search query
            top_k: Number of results to return
            doc_ids: Optional list of doc_ids to restrict to
            file_types: Optional file type filter (e.g., ["pptx", "pdf"])
            min_score: Minimum similarity score (applied after rerank)
            exclude_doc_ids: Documents to leave out - a document whose
                compilation failed is not evidence (AC-E-014)

        Returns:
            List of chunks with scores, sorted by relevance
        """
        # Build filter expression
        filter_parts = []
        if doc_ids:
            # Use IN clause
            ids_str = ", ".join(f"'{d}'" for d in doc_ids)
            filter_parts.append(f"doc_id IN ({ids_str})")
        if exclude_doc_ids:
            # A document that did not compile must not be read as
            # evidence: the page says one thing and the file another.
            ids_str = ", ".join(f"'{d}'" for d in exclude_doc_ids)
            filter_parts.append(f"doc_id NOT IN ({ids_str})")
        if file_types:
            types_str = ", ".join(f"'{t}'" for t in file_types)
            filter_parts.append(f"file_type IN ({types_str})")

        filter_expr = " AND ".join(filter_parts) if filter_parts else None

        # The first stage always fetches the configured number of
        # candidates - the reranker is a second stage, not a reason to
        # recall less.
        recall_k = max(top_k, self.top_k_initial)

        if self.use_hybrid_flag:
            # Stage 3 path: dense + sparse fusion
            results = await self.semantic.search_hybrid(
                query=query,
                top_k=recall_k,
                filter_expr=filter_expr,
                alpha=self.alpha,
                beta=self.beta,
                recall_k=recall_k,
            )
        else:
            results = await self.semantic.search(
                query=query,
                top_k=recall_k,
                filter_expr=filter_expr,
            )

        # Filter by min_score
        results = [r for r in results if r.get("_score", 0) >= min_score]

        # Cross-encoder rerank (Stage 2)
        if self.use_reranker_flag and self.reranker is not None and results:
            try:
                results = await self.reranker.rerank(
                    query, results, top_k=min(top_k, self.top_k_rerank)
                )
            except Exception as e:
                logger.warning(f"Reranker failed, falling back to vector order: {e}")
                results = results[:top_k]
        else:
            results = results[:top_k]

        return results

    async def search_term(self, term: str, top_k: int = 5) -> list[dict[str, Any]]:
        """The chunks that actually hold the term.

        The hybrid search finds the passages that talk about the term;
        the term itself has to be in them - a passage that talks about
        something else is not the context of *this* term.
        """
        term = (term or "").strip()
        if not term:
            return []

        # More candidates than needed: the ones that hold the term are
        # a subset of what the search brings back.
        candidates = await self.search(term, top_k=max(top_k * 4, 20))

        hits: list[dict[str, Any]] = []
        for chunk in candidates:
            if term in (chunk.get("text") or ""):
                hits.append(chunk)
            if len(hits) >= top_k:
                break
        return hits

    async def search_by_doc(
        self, query: str, doc_id: str, top_k: int = 5
    ) -> list[dict[str, Any]]:
        """Search within a specific document."""
        return await self.search(
            query=query,
            top_k=top_k,
            doc_ids=[doc_id],
        )

    async def search_by_metadata(
        self,
        query: str,
        file_type: str | None = None,
        page: int | None = None,
        slide_number: int | None = None,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """Search with specific metadata filters."""
        filter_parts = []
        if file_type:
            filter_parts.append(f"file_type = '{file_type}'")
        if page is not None:
            filter_parts.append(f"page = {page}")
        if slide_number is not None:
            filter_parts.append(f"slide_number = {slide_number}")

        filter_expr = " AND ".join(filter_parts) if filter_parts else None

        if self.use_hybrid_flag:
            results = await self.semantic.search_hybrid(
                query=query,
                top_k=top_k,
                filter_expr=filter_expr,
                alpha=self.alpha,
                beta=self.beta,
                recall_k=max(top_k * 3, 30),
            )
        else:
            results = await self.semantic.search(
                query=query,
                top_k=top_k,
                filter_expr=filter_expr,
            )

        return results


class TermContext:
    """Where a term appears in the knowledge base (ADR-041).

    The context comes from the original text, never from a model: the
    product's whole value is that the reader can check what they are
    reading. A term that is not in the documents has no context - and
    nothing is made up for it.
    """

    def __init__(self, retriever: "HybridRetriever"):
        self.retriever = retriever

    async def lookup(self, term: str, top_k: int = 5) -> dict[str, Any]:
        """The term's context in the original text, and where it is.

        Returns:
            ``term`` - what was looked up.
            ``found`` - whether the knowledge base holds it at all.
            ``contexts`` - the passages around the term, each with the
            place it came from.
        """
        term = (term or "").strip()
        if not term:
            return {"term": term, "found": False, "contexts": []}

        try:
            chunks = await self.retriever.search_term(term, top_k=top_k)
        except Exception as e:  # noqa: BLE001 - a lookup that fails finds nothing
            logger.warning(f"The term lookup failed: {e}")
            return {"term": term, "found": False, "contexts": []}

        contexts: list[dict[str, Any]] = []
        for chunk in chunks:
            snippet = context_around(chunk.get("text") or "", term)
            if not snippet:
                continue
            meta = chunk_meta(chunk)
            contexts.append({
                "text": snippet,
                "file_name": chunk.get("file_name", ""),
                "doc_id": chunk.get("doc_id", ""),
                "chunk_id": chunk.get("id", ""),
                "location": chunk_location(chunk),
                "page": meta.get("page"),
            })

        return {"term": term, "found": bool(contexts), "contexts": contexts}
