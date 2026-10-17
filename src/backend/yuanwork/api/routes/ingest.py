"""Document ingestion endpoints."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from yuanwork.api.schemas import (
    IngestDirRequest,
    IngestFileRequest,
    IngestJobResponse,
    IngestStatusResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# In-memory job tracking (replace with proper queue in production)
_jobs: dict[str, dict] = {}


@router.post("/ingest/dir", response_model=IngestJobResponse)
async def ingest_directory(
    request: Request,
    body: IngestDirRequest,
    background_tasks: BackgroundTasks,
) -> IngestJobResponse:
    """Ingest all documents from a directory.

    This kicks off a background job. Poll /ingest/status/{job_id} for progress.
    """
    from pathlib import Path

    path = Path(body.path)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Path not found: {body.path}")
    if not path.is_dir():
        raise HTTPException(status_code=400, detail=f"Not a directory: {body.path}")

    # Count files (basic implementation - improve with proper scanner)
    files = list(path.rglob("*") if body.recursive else path.glob("*"))
    files = [f for f in files if f.is_file()]

    job_id = str(uuid.uuid4())
    _jobs[job_id] = {
        "status": "pending",
        "progress": 0.0,
        "current_file": None,
        "files_done": 0,
        "files_total": len(files),
        "created_at": datetime.now().isoformat(),
        "error": None,
    }

    # Schedule background processing
    background_tasks.add_task(
        _run_ingest_job,
        request.app,
        job_id,
        path,
        body.recursive,
        body.force,
    )

    return IngestJobResponse(
        job_id=job_id,
        files_queued=len(files),
        estimated_seconds=len(files) * 2.0,  # rough estimate
    )


@router.post("/ingest/file", response_model=IngestJobResponse)
async def ingest_file(
    request: Request,
    body: IngestFileRequest,
    background_tasks: BackgroundTasks,
) -> IngestJobResponse:
    """Ingest a single file."""
    from pathlib import Path

    path = Path(body.path)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {body.path}")
    if not path.is_file():
        raise HTTPException(status_code=400, detail=f"Not a file: {body.path}")

    job_id = str(uuid.uuid4())
    _jobs[job_id] = {
        "status": "pending",
        "progress": 0.0,
        "current_file": str(path),
        "files_done": 0,
        "files_total": 1,
        "created_at": datetime.now().isoformat(),
        "error": None,
    }

    background_tasks.add_task(_run_ingest_file_job, request.app, job_id, path)

    return IngestJobResponse(job_id=job_id, files_queued=1, estimated_seconds=2.0)


@router.get("/ingest/status/{job_id}", response_model=IngestStatusResponse)
async def get_ingest_status(job_id: str) -> IngestStatusResponse:
    """Get the status of an ingestion job."""
    if job_id not in _jobs:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    job = _jobs[job_id]
    return IngestStatusResponse(
        job_id=job_id,
        status=job["status"],
        progress=job["progress"],
        current_file=job["current_file"],
        files_done=job["files_done"],
        files_total=job["files_total"],
        error=job.get("error"),
    )


async def _run_ingest_job(app, job_id: str, path, recursive: bool, force: bool) -> None:
    """Background task to process a directory ingestion job."""
    from yuanwork.ingestion.pipeline import IngestionPipeline

    _jobs[job_id]["status"] = "running"
    try:
        pipeline = IngestionPipeline.from_app(app)
        await pipeline.ingest_directory(path, recursive=recursive, force=force)

        _jobs[job_id]["status"] = "completed"
        _jobs[job_id]["progress"] = 1.0
    except Exception as e:
        logger.exception(f"Ingest job {job_id} failed")
        _jobs[job_id]["status"] = "failed"
        _jobs[job_id]["error"] = str(e)


async def _run_ingest_file_job(app, job_id: str, path) -> None:
    """Background task to process a single-file ingestion job."""
    from yuanwork.ingestion.pipeline import IngestionPipeline

    _jobs[job_id]["status"] = "running"
    try:
        pipeline = IngestionPipeline.from_app(app)
        await pipeline.ingest_file(path)

        _jobs[job_id]["status"] = "completed"
        _jobs[job_id]["progress"] = 1.0
    except Exception as e:
        logger.exception(f"Ingest job {job_id} failed")
        _jobs[job_id]["status"] = "failed"
        _jobs[job_id]["error"] = str(e)
