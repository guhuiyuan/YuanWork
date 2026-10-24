"""Tests for the ingestion job's progress reporting.

The frontend polls `/ingest/status/{job_id}` while indexing. The
counter used to stay at 0/N for the whole run, so the UI showed
"正在索引…（0/12）" until the job finished - it looked broken.

These tests pin down that the callback fires per file and that the
job state actually moves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


@pytest.mark.asyncio
async def test_the_pipeline_reports_progress_per_file(tmp_path: Path):
    """The callback fires once per file, with a running count."""
    from yuanwork.ingestion.pipeline import IngestionPipeline

    for name in ("a.md", "b.md", "c.md"):
        (tmp_path / name).write_text(f"# {name}\n\n内容\n", encoding="utf-8")

    pipeline = IngestionPipeline.__new__(IngestionPipeline)
    pipeline.sqlite = _FakeSqlite()
    pipeline.semantic = _FakeSemantic()
    pipeline.embedder = _FakeEmbedder()
    pipeline.chunker = _FakeChunker()
    pipeline.dispatcher = _FakeDispatcher(["md"])

    seen: list[tuple[int, int, str]] = []
    await pipeline.ingest_directory(
        tmp_path, recursive=True, on_progress=lambda d, t, c: seen.append((d, t, c))
    )

    assert [d for d, _, _ in seen] == [1, 2, 3]
    assert {t for _, t, _ in seen} == {3}
    assert seen[-1][2].endswith("c.md")


@pytest.mark.asyncio
async def test_the_job_state_moves_as_files_are_indexed(tmp_path: Path):
    """The endpoint's job record shows a moving counter."""
    from yuanwork.api.routes import ingest as ingest_routes

    for name in ("a.md", "b.md"):
        (tmp_path / name).write_text(f"# {name}\n\n内容\n", encoding="utf-8")

    job_id = "test-job"
    ingest_routes._jobs[job_id] = {
        "status": "running",
        "progress": 0.0,
        "current_file": None,
        "files_done": 0,
        "files_total": 2,
        "created_at": "2026-01-01T00:00:00",
        "error": None,
    }

    def _on_progress(done: int, total: int, current: str) -> None:
        job = ingest_routes._jobs[job_id]
        job["files_done"] = done
        job["files_total"] = total
        job["current_file"] = current
        job["progress"] = (done / total) if total else 1.0

    _on_progress(1, 2, str(tmp_path / "a.md"))
    assert ingest_routes._jobs[job_id]["files_done"] == 1
    assert ingest_routes._jobs[job_id]["progress"] == 0.5
    assert ingest_routes._jobs[job_id]["current_file"].endswith("a.md")

    _on_progress(2, 2, str(tmp_path / "b.md"))
    assert ingest_routes._jobs[job_id]["files_done"] == 2
    assert ingest_routes._jobs[job_id]["progress"] == 1.0


def test_the_status_endpoint_reports_the_job(monkeypatch):
    """The endpoint the frontend polls returns the job state."""
    import json

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from yuanwork.api.routes import ingest as ingest_routes

    ingest_routes._jobs["abc"] = {
        "status": "running",
        "progress": 0.5,
        "current_file": "C:/docs/x.pdf",
        "files_done": 1,
        "files_total": 2,
        "created_at": "2026-01-01T00:00:00",
        "error": None,
    }

    app = FastAPI()
    app.include_router(ingest_routes.router, prefix="/api/v1")
    with TestClient(app) as client:
        resp = client.get("/api/v1/ingest/status/abc")

    assert resp.status_code == 200
    body = json.loads(resp.text)
    assert body["status"] == "running"
    assert body["files_done"] == 1
    assert body["files_total"] == 2
    assert body["progress"] == 0.5


# ===== Test doubles =====


class _FakeSqlite:
    async def get_file_by_hash(self, file_hash: str) -> None:
        return None

    async def upsert_file(self, **kwargs: Any) -> None:
        return None


class _FakeSemantic:
    async def add_chunks(self, chunks: list[dict[str, Any]]) -> None:
        return None


class _FakeEmbedder:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 4 for _ in texts]


class _FakeChunker:
    def chunk(self, blocks, doc_id: str) -> list[dict[str, Any]]:
        return [
            {
                "id": f"c{i}",
                "doc_id": doc_id,
                "text": getattr(b, "content", ""),
                "meta": {},
            }
            for i, b in enumerate(blocks)
        ]


class _FakeDispatcher:
    def __init__(self, extensions: list[str]):
        self._extensions = extensions

    def supported_extensions(self) -> set[str]:
        return set(self._extensions)

    def get_parser(self, file_path: Path):
        return _FakeParser()


class _FakeParser:
    async def parse(self, file_path: Path, doc_id: str):
        from yuanwork.ingestion.parser.base import RawBlock

        return [
            RawBlock(
                block_id="b1",
                doc_id=doc_id,
                content=file_path.read_text(encoding="utf-8"),
                type="text",
                meta={},
            )
        ]


def test_supported_files_counts_only_what_can_be_indexed(tmp_path: Path):
    """The progress denominator must not count files nobody can index."""
    from yuanwork.ingestion.pipeline import IngestionPipeline

    (tmp_path / "a.md").write_text("# a", encoding="utf-8")
    (tmp_path / "b.md").write_text("# b", encoding="utf-8")
    (tmp_path / "c.exe").write_text("binary", encoding="utf-8")

    pipeline = IngestionPipeline.__new__(IngestionPipeline)
    pipeline.dispatcher = _FakeDispatcher(["md"])

    files = pipeline.supported_files(tmp_path)

    assert len(files) == 2
    assert all(f.suffix == ".md" for f in files)


@pytest.mark.asyncio
async def test_the_job_reports_the_supported_count_up_front(monkeypatch, tmp_path: Path):
    """files_total is corrected before the first file is parsed."""
    from yuanwork.api.routes import ingest as ingest_routes

    (tmp_path / "a.md").write_text("# a", encoding="utf-8")
    (tmp_path / "b.md").write_text("# b", encoding="utf-8")
    (tmp_path / "c.exe").write_text("binary", encoding="utf-8")

    job_id = "count-job"
    ingest_routes._jobs[job_id] = {
        "status": "pending",
        "progress": 0.0,
        "current_file": None,
        "files_done": 0,
        "files_total": 3,  # the queue count: every file in the directory
        "indexed": 0,
        "errors": 0,
        "created_at": "2026-01-01T00:00:00",
        "error": None,
    }

    seen_totals: list[int] = []

    class _FakePipeline:
        def __init__(self):
            self.dispatcher = _FakeDispatcher(["md"])

        def supported_files(self, dir_path, recursive=True):
            return [f for f in sorted(tmp_path.iterdir()) if f.suffix == ".md"]

        async def ingest_directory(self, dir_path, recursive=True, force=False, on_progress=None):
            seen_totals.append(ingest_routes._jobs[job_id]["files_total"])
            return {"indexed": 2, "skipped": 0, "errors": 0, "files": []}

    monkeypatch.setattr(
        "yuanwork.ingestion.pipeline.IngestionPipeline.from_app",
        classmethod(lambda cls, app: _FakePipeline()),
    )

    await ingest_routes._run_ingest_job(None, job_id, tmp_path, True, False)

    # The count was already corrected when the pipeline started.
    assert seen_totals == [2]
    assert ingest_routes._jobs[job_id]["files_total"] == 2
    assert ingest_routes._jobs[job_id]["status"] == "completed"


@pytest.mark.asyncio
async def test_a_run_where_every_file_failed_is_not_reported_as_completed(
    monkeypatch, tmp_path: Path
):
    """Files that could not be parsed must not look like a success."""
    from yuanwork.api.routes import ingest as ingest_routes

    job_id = "fail-job"
    ingest_routes._jobs[job_id] = {
        "status": "pending",
        "progress": 0.0,
        "current_file": None,
        "files_done": 0,
        "files_total": 1,
        "indexed": 0,
        "errors": 0,
        "created_at": "2026-01-01T00:00:00",
        "error": None,
    }

    class _FakePipeline:
        def __init__(self):
            self.dispatcher = _FakeDispatcher(["md"])

        def supported_files(self, dir_path, recursive=True):
            return []

        async def ingest_directory(self, dir_path, recursive=True, force=False, on_progress=None):
            return {"indexed": 0, "skipped": 0, "errors": 3, "files": []}

    monkeypatch.setattr(
        "yuanwork.ingestion.pipeline.IngestionPipeline.from_app",
        classmethod(lambda cls, app: _FakePipeline()),
    )

    await ingest_routes._run_ingest_job(None, job_id, tmp_path, True, False)

    assert ingest_routes._jobs[job_id]["status"] == "failed"
    assert ingest_routes._jobs[job_id]["error"]
