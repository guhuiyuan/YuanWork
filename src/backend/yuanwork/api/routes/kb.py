"""Knowledge base management endpoints."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request

from yuanwork.api.deps import get_sqlite_store
from yuanwork.api.schemas import (
    DocumentTierRequest,
    FileType,
    KBFileInfo,
    KBFilesResponse,
    TermContextItem,
    TermContextResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _infer_file_type(path: str) -> FileType:
    """Infer FileType from path extension."""
    suffix = Path(path).suffix.lower().lstrip(".")
    mapping = {
        "pdf": FileType.PDF,
        "docx": FileType.DOCX,
        "doc": FileType.DOCX,
        "pptx": FileType.PPTX,
        "ppt": FileType.PPTX,
        "xlsx": FileType.XLSX,
        "xls": FileType.XLSX,
        "md": FileType.MD,
        "markdown": FileType.MD,
        "txt": FileType.TXT,
        "png": FileType.IMAGE,
        "jpg": FileType.IMAGE,
        "jpeg": FileType.IMAGE,
        "webp": FileType.IMAGE,
    }
    return mapping.get(suffix, FileType.UNKNOWN)


@router.get("/kb/files", response_model=KBFilesResponse)
async def list_kb_files(
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    file_type: FileType | None = Query(default=None),
    search: str | None = Query(default=None),
) -> KBFilesResponse:
    """List all documents in the knowledge base."""
    sqlite = get_sqlite_store()

    files = await sqlite.list_files(
        limit=limit,
        offset=offset,
        file_type=file_type.value if file_type else None,
        search=search,
    )
    total = await sqlite.count_files(
        file_type=file_type.value if file_type else None,
        search=search,
    )

    return KBFilesResponse(
        files=[
            KBFileInfo(
                doc_id=f["doc_id"],
                file_name=f["file_name"],
                file_path=f["file_path"],
                file_type=_infer_file_type(f["file_path"]),
                file_hash=f["file_hash"],
                chunk_count=f["chunk_count"],
                size_bytes=f["size_bytes"],
                indexed_at=f["indexed_at"],
                last_modified=f["last_modified"],
            )
            for f in files
        ],
        total=total,
    )


@router.delete("/kb/files/{doc_id}")
async def delete_kb_file(doc_id: str, request: Request) -> dict:
    """Delete a document and all its chunks."""
    from yuanwork.api.deps import get_semantic_memory

    sqlite = get_sqlite_store()
    semantic = get_semantic_memory()

    # Check if exists
    file_info = await sqlite.get_file(doc_id)
    if not file_info:
        raise HTTPException(status_code=404, detail=f"Document not found: {doc_id}")

    # Delete from SQLite
    await sqlite.delete_file(doc_id)

    # Delete chunks from LanceDB
    await semantic.delete_by_doc_id(doc_id)

    return {"deleted": doc_id, "chunks_removed": file_info.get("chunk_count", 0)}


@router.get("/kb/terms/{term}/context", response_model=TermContextResponse)
async def get_term_context(
    term: str,
    top_k: int = Query(default=5, ge=1, le=20),
) -> TermContextResponse:
    """Where a term appears in the knowledge base.

    The context comes from the original text, with the place it came
    from - not from a model. A term that is not in the documents comes
    back empty and is marked as not found (ADR-041).
    """
    from yuanwork.api.deps import get_semantic_memory
    from yuanwork.retrieval.hybrid import HybridRetriever, TermContext

    retriever = HybridRetriever(get_semantic_memory())
    result = await TermContext(retriever).lookup(term, top_k=top_k)

    return TermContextResponse(
        term=result["term"],
        found=result["found"],
        contexts=[
            TermContextItem(
                text=item["text"],
                file_name=item["file_name"],
                doc_id=item["doc_id"],
                chunk_id=item["chunk_id"],
                location=item["location"],
                page=item["page"],
            )
            for item in result["contexts"]
        ],
    )


@router.get("/kb/stats")
async def get_kb_stats(request: Request) -> dict:
    """Get knowledge base statistics.

    The ledger says how many files were indexed; the index says how
    many slices are really there. The two can differ - a build that was
    interrupted leaves files that are "indexed" with nothing in the
    index - and then the difference is reported, because a count that
    lies is how a user ends up looking for a file that cannot be found
    (TASK-088).
    """
    sqlite = get_sqlite_store()

    total_files = await sqlite.count_files()
    total_chunks = await sqlite.count_chunks()
    by_type = await sqlite.count_files_by_type()

    index_chunks: int | None = None
    from yuanwork.api.deps import get_semantic_memory

    semantic = get_semantic_memory()
    counter = getattr(semantic, "count", None)
    if counter is not None:
        try:
            index_chunks = int(await counter())
        except Exception:  # noqa: BLE001 - the count is a nicety
            index_chunks = None

    return {
        "total_files": total_files,
        "total_chunks": total_chunks,
        "by_type": by_type,
        # What the index really holds, and what it is missing.
        "index_chunks": index_chunks,
        "missing_chunks": (
            max(0, total_chunks - index_chunks) if index_chunks is not None else None
        ),
    }


@router.post("/kb/files/{doc_id}/tier")
async def set_document_tier(doc_id: str, body: DocumentTierRequest) -> dict:
    """Put a document in a layer by hand.

    The user's choice is the last word: the rules and the model do not
    overrule it (AC-F-021).
    """
    sqlite = get_sqlite_store()

    file_info = await sqlite.get_file(doc_id)
    if not file_info:
        raise HTTPException(status_code=404, detail=f"Document not found: {doc_id}")

    record = await sqlite.set_doc_tier_by_user(doc_id, body.tier)

    return {
        "doc_id": doc_id,
        "tier": record.get("tier", body.tier),
        "decided_by": record.get("decided_by", "user"),
    }


@router.get("/kb/tiers")
async def list_document_tiers(
    request: Request,
    tier: str | None = Query(default=None, description="core | longtail"),
) -> dict:
    """The documents of each layer, and where the decision came from."""
    sqlite = get_sqlite_store()

    records = await sqlite.list_doc_tiers(tier=tier)

    return {
        "tiers": [
            {
                "doc_id": r["doc_id"],
                "tier": r["tier"],
                "decided_by": r["decided_by"],
                "decided_at": r["decided_at"],
                "compile_status": r["compile_status"],
                "stale": bool(r.get("stale")),
            }
            for r in records
        ],
        "total": len(records),
    }
