"""Step 3: Evidence Extraction - retrieve specific chunks within candidate files."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from yuanwork.retrieval.hybrid import HybridRetriever

if TYPE_CHECKING:
    from yuanwork.retrieval.tiered import TieredRetriever

logger = logging.getLogger(__name__)


async def extract_evidence(
    query: str,
    candidates: list[dict[str, Any]],
    retriever: HybridRetriever,
    top_k: int = 10,
    tiered: "TieredRetriever | None" = None,
) -> dict[str, Any]:
    """The evidence for a question, and where it came from.

    With a tiered retriever the core layer is searched first, and the
    long tail only if the core layer cannot answer (ADR-034). Without
    one the long tail answers on its own - the degradation the design
    allows when the core layer is not available.

    The result says which layers were searched, and whether the two of
    them disagreed: the answer has to know that (AC-E-016).
    """
    doc_ids = [c["doc_id"] for c in candidates] if candidates else None

    if tiered is not None:
        return await tiered.search(query=query, top_k=top_k, doc_ids=doc_ids)

    if not candidates:
        # No candidates - try broad search
        logger.info("No candidates, running broad search")
        results = await retriever.search(query=query, top_k=top_k)
    else:
        # Search within candidate files
        results = await retriever.search(
            query=query,
            top_k=top_k,
            doc_ids=doc_ids,
        )

    return {
        "evidence": results,
        "core_found": False,
        "longtail_searched": True,
        "conflicts": [],
    }
