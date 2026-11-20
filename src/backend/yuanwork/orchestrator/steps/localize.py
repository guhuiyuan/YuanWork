"""Step 2: File Localization - identify candidate documents for the query."""

from __future__ import annotations

import logging
from typing import Any

from yuanwork.db.sqlite import SQLiteStore
from yuanwork.memory.semantic import SemanticMemory

logger = logging.getLogger(__name__)


async def localize_files(
    query: str,
    understanding: dict[str, Any],
    sqlite: SQLiteStore,
    semantic: SemanticMemory,
    top_k_files: int = 5,
) -> list[dict[str, Any]]:
    """Identify candidate documents.

    Combines:
    1. File-name / title match from SQLite
    2. Vector recall to gather top chunks and aggregate their doc_ids

    Returns:
        List of {doc_id, file_name, file_path, score, reason}
    """
    candidates: list[dict[str, Any]] = []
    seen_doc_ids: set[str] = set()

    # === Method 1: File-name/title matching ===
    file_hint = understanding.get("file_hint")
    topic = understanding.get("topic", "")
    search_query = file_hint or topic

    if search_query:
        try:
            files = await sqlite.list_files(limit=top_k_files * 2, search=search_query)
            # The name the reader typed and the name in the index rarely
            # match exactly - a space is enough to lose the file
            # (TASK-085). The tolerant lookup finds it anyway.
            if file_hint:
                extra = await sqlite.find_files_by_name(file_hint)
                known = {f["doc_id"] for f in files}
                files = list(files) + [f for f in extra if f["doc_id"] not in known]
            for f in files:
                doc_id = f["doc_id"]
                if doc_id not in seen_doc_ids:
                    candidates.append({
                        "doc_id": doc_id,
                        "file_name": f["file_name"],
                        "file_path": f["file_path"],
                        "score": 0.8,  # Base score for filename match
                        "reason": f"filename_match: {search_query}",
                    })
                    seen_doc_ids.add(doc_id)
        except Exception as e:
            logger.warning(f"Filename matching failed: {e}")

    # === Method 2: Vector-based candidate aggregation ===
    try:
        # Search broadly to find which docs are most relevant
        results = await semantic.search(query, top_k=50)

        # Aggregate scores by doc_id
        doc_scores: dict[str, float] = {}
        doc_chunks: dict[str, list] = {}
        for r in results:
            doc_id = r.get("doc_id", "")
            if not doc_id:
                continue
            score = r.get("_score", 0.0)
            doc_scores[doc_id] = max(doc_scores.get(doc_id, 0.0), score)
            doc_chunks.setdefault(doc_id, []).append(r)

        # Sort and take top
        sorted_docs = sorted(doc_scores.items(), key=lambda x: x[1], reverse=True)[:top_k_files]

        for doc_id, score in sorted_docs:
            if doc_id not in seen_doc_ids:
                # Get file info
                file_info = await sqlite.get_file(doc_id)
                if file_info:
                    candidates.append({
                        "doc_id": doc_id,
                        "file_name": file_info["file_name"],
                        "file_path": file_info["file_path"],
                        "score": score,
                        "reason": "vector_recall",
                    })
                    seen_doc_ids.add(doc_id)
    except Exception as e:
        logger.warning(f"Vector candidate aggregation failed: {e}")

    # Sort by score
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:top_k_files]
