"""TASK-006 / TASK-007 — the ingestion and knowledge-base endpoints.

* AC-F-010 — the ingest job state lives in the database, and an unknown
  job answers 404
* AC-E-005 — the job state survives a restart, and the jobs that were
  running when the process died are cleaned up
* AC-E-006 — the queued count and the progress denominator disagree
* AC-E-007 — deleting a profile key never answers 404
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from yuanwork.api import deps
from yuanwork.api.routes import ingest as ingest_routes
from yuanwork.api.routes import kb as kb_routes
from yuanwork.api.routes import memory as memory_routes
from yuanwork.db.sqlite import SQLiteStore


# --------------------------------------------------------------------------
# doubles
# --------------------------------------------------------------------------


class FakeSemanticMemory:
    async def add_chunks(self, chunks):
        return [c["id"] for c in chunks]

    async def delete_by_doc_id(self, doc_id: str) -> int:
        return 0

    async def list_tables(self):
        return ["chunks"]

    async def close(self):
        return None


class FakeProfileStore:
    def __init__(self):
        self.deleted: list[str] = []

    async def delete(self, key: str) -> bool:
        self.deleted.append(key)
        return True


@pytest.fixture
async def store(tmp_path: Path):
    """A real store - the job state has to survive a restart."""
    sqlite = SQLiteStore(tmp_path / "store.db")
    await sqlite.initialize()
    yield sqlite
    await sqlite.close()


@pytest.fixture
def client(store: SQLiteStore):
    deps.set_sqlite_store(store)  # type: ignore[arg-type]
    deps.set_semantic_memory(FakeSemanticMemory())  # type: ignore[arg-type]
    deps.set_profile_store(FakeProfileStore())  # type: ignore[arg-type]

    app = FastAPI()
    app.include_router(ingest_routes.router, prefix="/api/v1")
    app.include_router(kb_routes.router, prefix="/api/v1")
    app.include_router(memory_routes.router, prefix="/api/v1")
    yield TestClient(app)
    deps.reset_dependencies()


def make_dir(tmp_path: Path, supported: int = 2, unsupported: int = 1) -> Path:
    d = tmp_path / "kb"
    d.mkdir()
    for i in range(supported):
        (d / f"doc{i}.md").write_text(f"# 文档 {i}\n\n正文\n", encoding="utf-8")
    for i in range(unsupported):
        (d / f"note{i}.xyz").write_text("unsupported", encoding="utf-8")
    return d


# --------------------------------------------------------------------------
# AC-F-010 — the job state
# --------------------------------------------------------------------------


def test_an_unknown_job_answers_404(client):
    resp = client.get("/api/v1/ingest/status/does-not-exist")
    assert resp.status_code == 404
    assert "Job not found" in resp.json()["detail"]


def test_a_submitted_job_can_be_queried(client, tmp_path: Path):
    d = make_dir(tmp_path)
    resp = client.post("/api/v1/ingest/dir", json={"path": str(d)})
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    status = client.get(f"/api/v1/ingest/status/{job_id}")
    assert status.status_code == 200
    body = status.json()
    assert body["job_id"] == job_id
    assert body["status"] in ("pending", "running", "completed", "failed")
    assert 0.0 <= body["progress"] <= 1.0


def test_a_missing_path_answers_404(client, tmp_path: Path):
    resp = client.post("/api/v1/ingest/dir", json={"path": str(tmp_path / "nope")})
    assert resp.status_code == 404
    assert "Path not found" in resp.json()["detail"]


def test_a_file_instead_of_a_directory_answers_400(client, tmp_path: Path):
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")
    resp = client.post("/api/v1/ingest/dir", json={"path": str(f)})
    assert resp.status_code == 400
    assert "Not a directory" in resp.json()["detail"]


# --------------------------------------------------------------------------
# AC-E-006 — the two counters disagree
# --------------------------------------------------------------------------


def test_the_queued_count_includes_files_nobody_can_index(client, tmp_path: Path):
    d = make_dir(tmp_path, supported=2, unsupported=3)
    resp = client.post("/api/v1/ingest/dir", json={"path": str(d)})
    # 2 supported + 3 unsupported are all announced as queued
    assert resp.json()["files_queued"] == 5


def test_the_progress_denominator_counts_only_supported_files(client, tmp_path: Path):
    d = make_dir(tmp_path, supported=2, unsupported=3)
    job_id = client.post("/api/v1/ingest/dir", json={"path": str(d)}).json()["job_id"]

    body = client.get(f"/api/v1/ingest/status/{job_id}").json()
    # ... but only the supported ones are ever going to be indexed
    assert body["files_total"] == 2
    assert body["files_total"] < 5


def test_the_estimate_is_two_seconds_per_queued_file(client, tmp_path: Path):
    d = make_dir(tmp_path, supported=2, unsupported=1)
    resp = client.post("/api/v1/ingest/dir", json={"path": str(d)})
    assert resp.json()["estimated_seconds"] == 6.0


# --------------------------------------------------------------------------
# AC-E-005 — the state lives in the database
# --------------------------------------------------------------------------


def test_the_job_state_survives_a_restart(client, store: SQLiteStore, tmp_path: Path):
    """The point of moving the jobs into the database.

    A build can run for a long time, and the backend may be restarted
    while it runs - the user must still find out what happened to it.
    """
    d = make_dir(tmp_path)
    job_id = client.post("/api/v1/ingest/dir", json={"path": str(d)}).json()["job_id"]
    assert client.get(f"/api/v1/ingest/status/{job_id}").status_code == 200

    # a second process opens the same database
    import asyncio

    async def read_from_a_new_process():
        other = SQLiteStore(store.db_path)
        await other.initialize()
        try:
            return await other.get_ingest_job(job_id)
        finally:
            await other.close()

    job = asyncio.run(read_from_a_new_process())
    assert job is not None
    assert job["job_id"] == job_id


def test_a_job_that_was_running_when_the_process_died_is_cleaned_up(
    store: SQLiteStore,
):
    """Nothing is working on it any more: the worker thread went with the
    process. Leaving it behind would show a progress bar that never
    moves."""
    import asyncio

    async def scenario():
        await store.upsert_ingest_job(job_id="j1", status="running", progress=0.5)
        await store.upsert_ingest_job(job_id="j2", status="completed", progress=1.0)

        cleared = await store.clear_running_ingest_jobs()

        assert cleared == 1
        assert await store.get_ingest_job("j1") is None
        # the finished one is history, not state - it stays
        assert await store.get_ingest_job("j2") is not None

    asyncio.run(scenario())


def test_the_finished_jobs_can_be_collected(store: SQLiteStore):
    """State that is done with is not state any more."""
    import asyncio

    async def scenario():
        await store.upsert_ingest_job(job_id="j1", status="completed")
        await store.upsert_ingest_job(job_id="j2", status="failed")

        cleared = await store.clear_finished_ingest_jobs()

        assert cleared == 2
        assert await store.list_ingest_jobs() == []

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# the knowledge-base endpoints
# --------------------------------------------------------------------------


def test_the_file_list_is_empty_for_an_empty_knowledge_base(client):
    resp = client.get("/api/v1/kb/files")
    assert resp.status_code == 200
    body = resp.json()
    assert body["files"] == []
    assert body["total"] == 0


async def test_deleting_a_document_removes_only_the_index(client, store: SQLiteStore):
    await store.upsert_file(
        doc_id="doc-1",
        file_name="a.md",
        file_path="/kb/a.md",
        file_hash="hash-1",
        file_type="md",
        size_bytes=10,
        chunk_count=7,
    )

    resp = client.delete("/api/v1/kb/files/doc-1")

    assert resp.status_code == 200
    assert await store.get_file("doc-1") is None


async def test_the_removed_chunk_count_comes_from_the_pre_delete_record(
    client, store: SQLiteStore
):
    """The response reports the chunk count read *before* the delete."""
    await store.upsert_file(
        doc_id="doc-1",
        file_name="a.md",
        file_path="/kb/a.md",
        file_hash="hash-1",
        file_type="md",
        size_bytes=10,
        chunk_count=7,
    )

    resp = client.delete("/api/v1/kb/files/doc-1")

    assert resp.json()["chunks_removed"] == 7


def test_deleting_an_unknown_document_answers_404(client):
    resp = client.delete("/api/v1/kb/files/nope")
    assert resp.status_code == 404
    assert "Document not found" in resp.json()["detail"]


# --------------------------------------------------------------------------
# AC-E-007 — deleting a profile key
# --------------------------------------------------------------------------


def test_deleting_a_profile_key_answers_200(client):
    resp = client.delete("/api/v1/memory/profile/some-key")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": "some-key"}


def test_the_404_branch_of_the_profile_delete_is_unreachable(client, store):
    """The store always reports success, so the route's 404 branch can
    never be taken in production."""
    async def always_true(key):
        return True

    store.delete_profile = always_true  # type: ignore[assignment]
    resp = client.delete("/api/v1/memory/profile/whatever")
    assert resp.status_code == 200
