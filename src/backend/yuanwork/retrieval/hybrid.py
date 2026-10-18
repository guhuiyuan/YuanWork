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

logger = logging.getLogger(__name__)


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
        use_reranker: bool = True,
        top_k_initial: int = 50,
        use_hybrid: bool = True,
        alpha: float = 0.65,
        beta: float = 0.35,
    ):
        self.semantic = semantic_memory
        self.reranker = reranker
        self.use_reranker_flag = use_reranker
        self.top_k_initial = top_k_initial
        self.use_hybrid_flag = use_hybrid
        # Defaults to the same weights used inside SemanticMemory.override
        # these per-instance for experimentation.
        self.alpha = alpha
        self.beta = beta

    async def search(
        self,
        query: str,
        top_k: int = 10,
        doc_ids: list[str] | None = None,
        file_types: list[str] | None = None,
        min_score: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Search with optional filters.

        Args:
            query: Search query
            top_k: Number of results to return
            doc_ids: Optional list of doc_ids to restrict to
            file_types: Optional file type filter (e.g., ["pptx", "pdf"])
            min_score: Minimum similarity score (applied after rerank)

        Returns:
            List of chunks with scores, sorted by relevance
        """
        # Build filter expression
        filter_parts = []
        if doc_ids:
            # Use IN clause
            ids_str = ", ".join(f"'{d}'" for d in doc_ids)
            filter_parts.append(f"doc_id IN ({ids_str})")
        if file_types:
            types_str = ", ".join(f"'{t}'" for t in file_types)
            filter_parts.append(f"file_type IN ({types_str})")

        filter_expr = " AND ".join(filter_parts) if filter_parts else None

        # Overfetch for reranking / filtering
        recall_k = max(top_k, self.top_k_initial) if self.use_reranker_flag else top_k * 2

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
                results = await self.reranker.rerank(query, results, top_k=top_k)
            except Exception as e:
                logger.warning(f"Reranker failed, falling back to vector order: {e}")
                results = results[:top_k]
        else:
            results = results[:top_k]

        return results

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
