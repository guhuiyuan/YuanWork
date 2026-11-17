"""Tests for the ingestion job's progress reporting.

The frontend polls `/ingest/status/{job_id}` while indexing. The
counter used to stay at 0/N for the whole run, so the UI showed
"正在索引…（0/12）" until the job finished - it looked broken.

These tests pin down that the callback fires per file, that the job
state actually moves, and that the state lives in the database - so it
survives a restart of the backend.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
async def store(tmp_path: Path):
    """A real store - the job state has to survive a restart."""
    from yuanwork.db.sqlite import SQLiteStore

    sqlite = SQLiteStore(tmp_path / "store.db")
    await sqlite.initialize()
    yield sqlite
    await sqlite.close()


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
async def test_the_job_state_moves_as_files_are_indexed(store):
    """The endpoint's job record shows a moving counter."""
    job_id = "test-job"
    await store.upsert_ingest_job(
        job_id=job_id, status="running", progress=0.0, files_done=0, files_total=2
    )

    async def _on_progress(done: int, total: int, current: str) -> None:
        await store.upsert_ingest_job(
            job_id=job_id,
            status="running",
            progress=(done / total) if total else 1.0,
            files_done=done,
            files_total=total,
            current_file=current,
        )

    await _on_progress(1, 2, "C:/docs/a.md")
    job = await store.get_ingest_job(job_id)
    assert job["files_done"] == 1
    assert job["progress"] == 0.5
    assert job["current_file"].endswith("a.md")

    await _on_progress(2, 2, "C:/docs/b.md")
    job = await store.get_ingest_job(job_id)
    assert job["files_done"] == 2
    assert job["progress"] == 1.0


@pytest.mark.asyncio
async def test_the_status_endpoint_reports_the_job(store):
    """The endpoint the frontend polls returns the job state."""
    import json

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from yuanwork.api import deps
    from yuanwork.api.routes import ingest as ingest_routes

    await store.upsert_ingest_job(
        job_id="abc",
        status="running",
        progress=0.5,
        files_done=1,
        files_total=2,
        current_file="C:/docs/x.pdf",
    )
    deps.set_sqlite_store(store)

    app = FastAPI()
    app.include_router(ingest_routes.router, prefix="/api/v1")
    with TestClient(app) as client:
        resp = client.get("/api/v1/ingest/status/abc")

    deps.reset_dependencies()
    assert resp.status_code == 200
    body = json.loads(resp.text)
    assert body["status"] == "running"
    assert body["files_done"] == 1
    assert body["files_total"] == 2
    assert body["progress"] == 0.5
    assert body["current_file"] == "C:/docs/x.pdf"


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
    #: Which rules it cuts with (the index compares it - ADR-046).
    version = "fake-v1"

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
async def test_the_job_reports_the_supported_count_up_front(
    monkeypatch, store, tmp_path: Path
):
    """files_total is corrected before the first file is parsed."""
    from yuanwork.api import deps
    from yuanwork.api.routes import ingest as ingest_routes

    (tmp_path / "a.md").write_text("# a", encoding="utf-8")
    (tmp_path / "b.md").write_text("# b", encoding="utf-8")
    (tmp_path / "c.exe").write_text("binary", encoding="utf-8")

    job_id = "count-job"
    await store.upsert_ingest_job(
        job_id=job_id,
        status="pending",
        progress=0.0,
        files_done=0,
        files_total=3,  # the queue count: every file in the directory
    )
    deps.set_sqlite_store(store)

    seen_totals: list[int] = []

    class _FakePipeline:
        def __init__(self):
            self.dispatcher = _FakeDispatcher(["md"])

        def supported_files(self, dir_path, recursive=True):
            return [f for f in sorted(tmp_path.iterdir()) if f.suffix == ".md"]

        async def build_directory(self, dir_path, recursive=True, force=False, on_progress=None):
            job = await store.get_ingest_job(job_id)
            seen_totals.append(job["files_total"])
            return {"indexed": 2, "skipped": 0, "errors": 0, "files": []}

    monkeypatch.setattr(
        "yuanwork.ingestion.pipeline.IngestionPipeline.from_app",
        classmethod(lambda cls, app: _FakePipeline()),
    )

    await ingest_routes._run_ingest_job(None, job_id, tmp_path, True, False)
    deps.reset_dependencies()

    # The count was already corrected when the pipeline started.
    assert seen_totals == [2]
    job = await store.get_ingest_job(job_id)
    assert job["files_total"] == 2
    assert job["status"] == "completed"


@pytest.mark.asyncio
async def test_a_run_where_every_file_failed_is_not_reported_as_completed(
    monkeypatch, store, tmp_path: Path
):
    """Files that could not be parsed must not look like a success."""
    from yuanwork.api import deps
    from yuanwork.api.routes import ingest as ingest_routes

    job_id = "fail-job"
    await store.upsert_ingest_job(
        job_id=job_id, status="pending", progress=0.0, files_done=0, files_total=1
    )
    deps.set_sqlite_store(store)

    class _FakePipeline:
        def __init__(self):
            self.dispatcher = _FakeDispatcher(["md"])

        def supported_files(self, dir_path, recursive=True):
            return []

        async def build_directory(self, dir_path, recursive=True, force=False, on_progress=None):
            return {"indexed": 0, "skipped": 0, "errors": 3, "files": []}

    monkeypatch.setattr(
        "yuanwork.ingestion.pipeline.IngestionPipeline.from_app",
        classmethod(lambda cls, app: _FakePipeline()),
    )

    await ingest_routes._run_ingest_job(None, job_id, tmp_path, True, False)
    deps.reset_dependencies()

    job = await store.get_ingest_job(job_id)
    assert job["status"] == "failed"
    assert job["error"]
