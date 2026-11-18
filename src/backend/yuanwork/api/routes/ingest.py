"""Document ingestion endpoints.

The state of a build lives in the database, not in the process: a build
can run for a long time, and the backend may be restarted while it runs.
A job that was still running when the process died is cleaned up at
startup - nothing is working on it any more.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from yuanwork.api.schemas import (
    FailedFile,
    IngestDirRequest,
    IngestFileRequest,
    IngestJobResponse,
    IngestStatusResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()

#: How many failed files a job keeps.
#:
#: A workspace can hold a hundred thousand files that nobody can read -
#: the list is capped so the job row stays a job row, and the real
#: number is kept beside it (AC-E-004).
MAX_FAILED_FILES = 300

#: The reasons that are not a failure: the file is in the index, or it
#: changed and the old version is kept until the user asks for a rebuild
#: (AC-E-003).
_NOT_A_FAILURE = ("already_indexed", "modified")


def _failure_reason(entry: dict) -> str:
    """Why this file did not make it into the index - in words."""
    if entry.get("status") == "error":
        return str(entry.get("error") or "解析失败")
    reason = str(entry.get("reason") or "")
    if reason == "no_content":
        return "文件里没有可读的文字（可能是扫描件）"
    return reason or "未知原因"


def _collect_failed_files(result: dict) -> tuple[list[dict], int]:
    """The files the build could not read, and how many there were.

    The files that could not be *read* come first: they are the ones the
    user has to know about. The unsupported ones come after - there can
    be a hundred thousand of them, and they are all saying the same
    thing (AC-E-004).
    """
    broken: list[dict] = []
    unsupported: list[dict] = []

    for entry in result.get("unsupported_files") or []:
        unsupported.append(
            {
                "path": str(entry.get("path") or ""),
                "error": str(entry.get("reason") or "不支持的文件类型"),
            }
        )

    for entry in result.get("files") or []:
        status = entry.get("status")
        if status == "error":
            broken.append(
                {
                    "path": str(entry.get("path") or ""),
                    "error": _failure_reason(entry),
                }
            )
        elif status == "skipped" and entry.get("reason") not in _NOT_A_FAILURE:
            broken.append(
                {
                    "path": str(entry.get("path") or ""),
                    "error": _failure_reason(entry),
                }
            )

    every = broken + unsupported
    return every[:MAX_FAILED_FILES], len(every)


def _parse_failed_files(raw: object) -> list[FailedFile]:
    """The failed files a job kept - whatever shape they were stored in."""
    if raw is None or raw == "":
        return []
    entries = raw
    if isinstance(raw, str):
        try:
            entries = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
    if not isinstance(entries, list):
        return []

    files: list[FailedFile] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path") or "")
        if not path:
            continue
        files.append(
            FailedFile(path=path, error=str(entry.get("error") or "未知原因"))
        )
    return files


async def _create_job(
    request: Request,
    job_id: str,
    files_total: int,
    current_file: str | None = None,
) -> None:
    """Register a new job in the database."""
    from yuanwork.api.deps import get_sqlite_store

    await get_sqlite_store().upsert_ingest_job(
        job_id=job_id,
        status="pending",
        progress=0.0,
        files_done=0,
        files_total=files_total,
        error=None,
        current_file=current_file,
    )


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
    await _create_job(request, job_id, len(files))

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
    await _create_job(request, job_id, 1, current_file=str(path))

    background_tasks.add_task(_run_ingest_file_job, request.app, job_id, path)

    return IngestJobResponse(job_id=job_id, files_queued=1, estimated_seconds=2.0)


@router.post("/ingest/rebuild", response_model=IngestJobResponse)
async def rebuild_workspace(
    request: Request,
    background_tasks: BackgroundTasks,
) -> IngestJobResponse:
    """Build the knowledge base from the workspace.

    The workspace is the scope: the folders the user added and the
    single files added by hand. This kicks off a background job over
    exactly those files - poll /ingest/status/{job_id} for progress,
    the same job shape as the folder build (TASK-051).
    """
    from yuanwork.ingestion.pipeline import IngestionPipeline

    # How much work there is - the files the build will actually read.
    # The workspace covers many more files than that (a code project is
    # mostly source), and the number that goes on the progress bar has
    # to be the work, not the files that are merely there.
    pipeline = IngestionPipeline.from_app(request.app)
    total = await pipeline.workspace_build_size()

    job_id = str(uuid.uuid4())
    await _create_job(request, job_id, total)

    background_tasks.add_task(_run_rebuild_job, request.app, job_id)

    return IngestJobResponse(
        job_id=job_id,
        files_queued=total,
        estimated_seconds=total * 2.0,  # rough estimate
    )


@router.get("/ingest/status/{job_id}", response_model=IngestStatusResponse)
async def get_ingest_status(job_id: str) -> IngestStatusResponse:
    """The status of an ingestion job.

    `latest` is the job that ran last. The app reads it when it starts:
    the files the last build could not read are still there after a
    restart, and the user has to see them (ADR-022, AC-E-004).
    """
    from yuanwork.api.deps import get_sqlite_store

    store = get_sqlite_store()
    if job_id == "latest":
        job = await store.get_latest_ingest_job()
    else:
        job = await store.get_ingest_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    return IngestStatusResponse(
        job_id=job["job_id"],
        status=job["status"],
        progress=job["progress"],
        current_file=job.get("current_file"),
        files_done=job["files_done"],
        files_total=job["files_total"],
        error=job.get("error"),
        failed_files=_parse_failed_files(job.get("failed_files")),
        failed_total=int(job.get("failed_total") or 0),
    )


async def _run_ingest_job(app, job_id: str, path, recursive: bool, force: bool) -> None:
    """Background task to process a directory ingestion job."""
    from yuanwork.api.deps import get_sqlite_store
    from yuanwork.ingestion.pipeline import IngestionPipeline

    sqlite = get_sqlite_store()
    await sqlite.upsert_ingest_job(job_id=job_id, status="running")

    async def _on_progress(done: int, total: int, current: str) -> None:
        # The queue counted every file in the directory, but the pipeline
        # only processes the supported ones - so the real total is
        # reported here.
        await sqlite.upsert_ingest_job(
            job_id=job_id,
            status="running",
            progress=(done / total) if total else 1.0,
            files_done=done,
            files_total=total,
            current_file=current,
        )

    try:
        pipeline = IngestionPipeline.from_app(app)

        # Report the real number of files that will actually be indexed
        # before the first one is parsed. Counting every file in the
        # directory would show a denominator that never matches.
        try:
            total = len(pipeline.supported_files(path, recursive=recursive))
            await sqlite.upsert_ingest_job(
                job_id=job_id, status="running", files_total=total
            )
        except Exception:  # noqa: BLE001 - the count is a nicety, not a requirement
            pass

        # The build reads in parallel and writes in batches; the core
        # layer's pages compile in the background (AC-F-026).
        result = await pipeline.build_directory(
            path, recursive=recursive, force=force, on_progress=_on_progress
        )

        indexed = result.get("indexed", 0)
        errors = result.get("errors", 0)
        failed, failed_total = _collect_failed_files(result)

        if errors and not indexed:
            # Every file failed to parse - that is not a successful run.
            await sqlite.upsert_ingest_job(
                job_id=job_id,
                status="failed",
                progress=1.0,
                error=f"{errors} 个文件都没能解析",
                failed_files=json.dumps(failed, ensure_ascii=False),
                failed_total=failed_total,
            )
        else:
            await sqlite.upsert_ingest_job(
                job_id=job_id,
                status="completed",
                progress=1.0,
                current_file=None,
                failed_files=json.dumps(failed, ensure_ascii=False),
                failed_total=failed_total,
            )
    except Exception as e:
        logger.exception(f"Ingest job {job_id} failed")
        await sqlite.upsert_ingest_job(
            job_id=job_id, status="failed", error=str(e)
        )


async def _run_rebuild_job(app, job_id: str) -> None:
    """Background task: build the knowledge base from the workspace.

    The files the workspace covers are read in parallel and written in
    batches - the same build a folder gets (TASK-045, TASK-051).
    """
    from yuanwork.api.deps import get_sqlite_store
    from yuanwork.ingestion.pipeline import IngestionPipeline

    sqlite = get_sqlite_store()
    await sqlite.upsert_ingest_job(job_id=job_id, status="running")

    async def _on_progress(done: int, total: int, current: str) -> None:
        # The denominator is what the workspace covers, so the progress
        # bar counts the files that cannot be indexed as well.
        await sqlite.upsert_ingest_job(
            job_id=job_id,
            status="running",
            progress=(done / total) if total else 1.0,
            files_done=done,
            files_total=total,
            current_file=current,
        )

    try:
        pipeline = IngestionPipeline.from_app(app)
        result = await pipeline.build_workspace(on_progress=_on_progress)

        indexed = result.get("indexed", 0)
        errors = result.get("errors", 0)
        failed, failed_total = _collect_failed_files(result)

        if errors and not indexed:
            # Every file failed to parse - that is not a successful run.
            await sqlite.upsert_ingest_job(
                job_id=job_id,
                status="failed",
                progress=1.0,
                error=f"{errors} 个文件都没能解析",
                failed_files=json.dumps(failed, ensure_ascii=False),
                failed_total=failed_total,
            )
        else:
            await sqlite.upsert_ingest_job(
                job_id=job_id,
                status="completed",
                progress=1.0,
                current_file=None,
                failed_files=json.dumps(failed, ensure_ascii=False),
                failed_total=failed_total,
            )
    except Exception as e:
        logger.exception(f"Rebuild job {job_id} failed")
        await sqlite.upsert_ingest_job(
            job_id=job_id, status="failed", error=str(e)
        )


async def _run_ingest_file_job(app, job_id: str, path) -> None:
    """Background task to process a single-file ingestion job."""
    from yuanwork.api.deps import get_sqlite_store
    from yuanwork.ingestion.pipeline import IngestionPipeline

    sqlite = get_sqlite_store()
    await sqlite.upsert_ingest_job(job_id=job_id, status="running")

    try:
        pipeline = IngestionPipeline.from_app(app)
        await pipeline.ingest_file(path)

        await sqlite.upsert_ingest_job(
            job_id=job_id,
            status="completed",
            progress=1.0,
            files_done=1,
            current_file=None,
        )
    except Exception as e:
        logger.exception(f"Ingest job {job_id} failed")
        await sqlite.upsert_ingest_job(
            job_id=job_id, status="failed", error=str(e)
        )
