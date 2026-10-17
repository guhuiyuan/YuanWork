"""Step 3: Evidence Extraction - retrieve specific chunks within candidate files."""

from __future__ import annotations

import logging
from typing import Any

from yuanwork.retrieval.hybrid import HybridRetriever

logger = logging.getLogger(__name__)


async def extract_evidence(
    query: str,
    candidates: list[dict[str, Any]],
    retriever: HybridRetriever,
    top_k: int = 10,
) -> list[dict[str, Any]]:
    """Extract relevant evidence chunks within candidate files.

    Args:
        query: User query
        candidates: List of candidate files from Step 2
        retriever: HybridRetriever instance
        top_k: Max evidence chunks to return

    Returns:
        List of chunk dicts with text, metadata, scores
    """
    if not candidates:
        # No candidates - try broad search
        logger.info("No candidates, running broad search")
        return await retriever.search(query=query, top_k=top_k)

    # Search within candidate files
    doc_ids = [c["doc_id"] for c in candidates]
    results = await retriever.search(
        query=query,
        top_k=top_k,
        doc_ids=doc_ids,
    )

    return results
