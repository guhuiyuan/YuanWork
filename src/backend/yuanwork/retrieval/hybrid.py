"""Hybrid retrieval - vector + metadata filter + reranking.

Stage 1 implementation: vector similarity with metadata filter.
Stage 2 will add BM25 sparse retrieval and proper hybrid scoring.
"""

from __future__ import annotations

import logging
from typing import Any

from yuanwork.memory.semantic import SemanticMemory
from yuanwork.retrieval.reranker import Reranker

logger = logging.getLogger(__name__)


class HybridRetriever:
    """Hybrid retrieval: vector recall + optional cross-encoder rerank.

    Stage 1: vector search with optional LanceDB filter expression.
    Stage 2: rerank top candidates with bge-reranker-v2-m3 (when enabled).
    """

    def __init__(
        self,
        semantic_memory: SemanticMemory,
        reranker: Reranker | None = None,
        use_reranker: bool = True,
        top_k_initial: int = 50,
    ):
        self.semantic = semantic_memory
        self.reranker = reranker
        self.use_reranker_flag = use_reranker
        self.top_k_initial = top_k_initial

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
            min_score: Minimum similarity score

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

        results = await self.semantic.search(
            query=query,
            top_k=top_k,
            filter_expr=filter_expr,
        )

        return results
