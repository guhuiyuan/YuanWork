"""Knowledge base management endpoints."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request

from yuanwork.api.deps import get_sqlite_store
from yuanwork.api.schemas import FileType, KBFileInfo, KBFilesResponse

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


@router.get("/kb/stats")
async def get_kb_stats(request: Request) -> dict:
    """Get knowledge base statistics."""
    sqlite = get_sqlite_store()

    total_files = await sqlite.count_files()
    total_chunks = await sqlite.count_chunks()
    by_type = await sqlite.count_files_by_type()

    return {
        "total_files": total_files,
        "total_chunks": total_chunks,
        "by_type": by_type,
    }
