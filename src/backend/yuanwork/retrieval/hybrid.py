"""Hybrid retrieval - vector + metadata filter + reranking.

Stage 1 implementation: vector similarity with metadata filter.
Stage 2 will add BM25 sparse retrieval and proper hybrid scoring.
"""

from __future__ import annotations

import logging
from typing import Any

from yuanwork.memory.semantic import SemanticMemory

logger = logging.getLogger(__name__)


class HybridRetriever:
    """Hybrid retrieval combining vector + metadata filtering.

    Stage 1: vector search with optional LanceDB filter expression.
    Stage 2: add BM25 + cross-encoder reranking.
    """

    def __init__(self, semantic_memory: SemanticMemory):
        self.semantic = semantic_memory

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

        # Run search (overfetch by 2x for filtering)
        results = await self.semantic.search(
            query=query,
            top_k=top_k * 2,
            filter_expr=filter_expr,
        )

        # Filter by min_score
        results = [r for r in results if r.get("_score", 0) >= min_score]

        # Trim to top_k
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
