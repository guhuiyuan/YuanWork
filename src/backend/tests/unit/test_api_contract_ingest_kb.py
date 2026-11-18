"""TASK-006 / TASK-007 — the ingestion and knowledge-base endpoints.

* AC-F-010 — the ingest job state lives in the database, and an unknown
  job answers 404
* AC-E-005 — the job state survives a restart, and the jobs that were
  running when the process died are cleaned up
* AC-E-006 — the queued count and the progress denominator disagree
* AC-E-007 — deleting a profile key never answers 404
"""

from __future__ import annotations

import json
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
# AC-E-004 — the files the build could not read
# --------------------------------------------------------------------------


async def test_the_failed_files_are_kept_and_come_back_with_the_status(
    client, store: SQLiteStore
):
    """A count of failures says that something went wrong.

    The user needs to know *which* file and *why* - so the list of
    failures is part of the job's state, not just a number.
    """
    await store.upsert_ingest_job(
        job_id="j1",
        status="completed",
        failed_files=json.dumps(
            [{"path": "D:\\kb\\扫描件.docx", "error": "文件里没有可读的文字"}]
        ),
        failed_total=1,
    )

    body = client.get("/api/v1/ingest/status/j1").json()

    assert body["failed_files"] == [
        {"path": "D:\\kb\\扫描件.docx", "error": "文件里没有可读的文字"}
    ]
    assert body["failed_total"] == 1


async def test_the_count_of_failed_files_is_the_real_one(client, store: SQLiteStore):
    """The list is capped - the count is not."""
    await store.upsert_ingest_job(
        job_id="j2",
        status="completed",
        failed_files=json.dumps([{"path": "D:\\kb\\a.mp4", "error": "不支持的文件类型"}]),
        failed_total=150000,
    )

    body = client.get("/api/v1/ingest/status/j2").json()

    assert len(body["failed_files"]) == 1
    assert body["failed_total"] == 150000


async def test_a_job_without_failures_reports_none(client, store: SQLiteStore):
    await store.upsert_ingest_job(job_id="j3", status="completed")

    body = client.get("/api/v1/ingest/status/j3").json()

    assert body["failed_files"] == []
    assert body["failed_total"] == 0


async def test_the_last_build_can_be_read_after_a_restart(client, store: SQLiteStore):
    """The app reads the last job when it starts.

    The files the last build could not read are kept in the database -
    so the user still sees them after the app was closed.
    """
    await store.upsert_ingest_job(
        job_id="old",
        status="completed",
        failed_files=json.dumps([{"path": "D:\\kb\\a.docx", "error": "解析失败"}]),
        failed_total=1,
    )
    await store.upsert_ingest_job(
        job_id="new",
        status="completed",
        failed_files=json.dumps([]),
        failed_total=0,
    )

    body = client.get("/api/v1/ingest/status/latest").json()

    assert body["job_id"] == "new"


def test_the_last_job_is_404_when_there_was_none(client, store: SQLiteStore):
    """No build yet is not an error the app needs to shout about."""
    assert client.get("/api/v1/ingest/status/latest").status_code == 404


def test_a_build_that_changed_nothing_is_not_a_failure():
    """Everything already in the index: the build did its job.

    The files it could not read are the ones it could not read before -
    not a new failure. Saying "索引失败" after a rebuild that changed
    nothing would be a lie the user has to read.
    """
    from yuanwork.api.routes.ingest import _job_outcome

    result = {
        "indexed": 0,
        "errors": 12,
        "files": [
            {"path": "D:\\kb\\a.doc", "status": "error", "error": "BadZipFile"},
            {"path": "D:\\kb\\b.md", "status": "skipped", "reason": "already_indexed"},
        ],
    }

    status, error = _job_outcome(result)

    assert status == "completed"
    assert error is None


def test_a_build_that_read_nothing_and_failed_at_everything_is_a_failure():
    from yuanwork.api.routes.ingest import _job_outcome

    result = {
        "indexed": 0,
        "errors": 3,
        "files": [
            {"path": "D:\\kb\\a.doc", "status": "error", "error": "BadZipFile"},
        ],
    }

    status, error = _job_outcome(result)

    assert status == "failed"
    assert "3" in (error or "")


def test_the_failed_files_are_collected_from_the_build_result():
    """What the build says about a file is what the app shows.

    The files that could not be *read* come first: the unsupported ones
    can be a hundred thousand, and they are all saying the same thing.
    """
    from yuanwork.api.routes.ingest import _collect_failed_files

    result = {
        "files": [
            {"path": "D:\\kb\\坏了.docx", "status": "error", "error": "ValueError('x')"},
            {"path": "D:\\kb\\空.md", "status": "skipped", "reason": "no_content"},
            {"path": "D:\\kb\\大了.pdf", "status": "skipped", "reason": "文件超过体积上限"},
            {"path": "D:\\kb\\旧的.docx", "status": "skipped", "reason": "already_indexed"},
            {"path": "D:\\kb\\改过的.docx", "status": "skipped", "reason": "modified"},
            {"path": "D:\\kb\\好的.docx", "status": "indexed"},
        ],
        "unsupported_files": [
            {"path": "D:\\kb\\a.mp4", "reason": "不支持的文件类型：.mp4"}
        ],
    }

    failed, total, unsupported = _collect_failed_files(result)

    assert total == 4
    assert unsupported == 1
    assert [f["path"] for f in failed] == [
        "D:\\kb\\坏了.docx",
        "D:\\kb\\空.md",
        "D:\\kb\\大了.pdf",
        "D:\\kb\\a.mp4",
    ]
    assert failed[0]["error"] == "ValueError('x')"
    # the reason is in words, not in a code
    assert "没有可读的文字" in failed[1]["error"]
    assert failed[3]["error"] == "不支持的文件类型：.mp4"


def test_the_old_binary_formats_say_so_in_words():
    """AC-E-004 - the reason is for the user, not for a stack trace.

    The old formats are registered as supported but cannot be read;
    the user is told which format and what to do about it.
    """
    from yuanwork.api.routes.ingest import _collect_failed_files

    result = {
        "files": [
            {
                "path": "D:\\kb\\旧文档.doc",
                "status": "error",
                "error": "BadZipFile('File is not a zip file')",
            },
            {
                "path": "D:\\kb\\旧课件.ppt",
                "status": "error",
                "error": "KeyError(\"no relationship of type 'x'\")",
            },
        ],
        "unsupported_files": [],
    }

    failed, total, unsupported = _collect_failed_files(result)

    assert total == 2
    assert unsupported == 0
    assert ".doc" in failed[0]["error"]
    assert "另存为新版" in failed[0]["error"]
    assert ".ppt" in failed[1]["error"]


def test_the_failed_file_list_is_capped_but_the_count_is_not():
    """A workspace can hold a hundred thousand files nobody can read."""
    from yuanwork.api.routes.ingest import MAX_FAILED_FILES, _collect_failed_files

    result = {
        "files": [],
        "unsupported_files": [
            {"path": f"D:\\kb\\{i}.mp4", "reason": "不支持的文件类型"} for i in range(500)
        ],
    }

    failed, total, unsupported = _collect_failed_files(result)

    assert len(failed) == MAX_FAILED_FILES
    assert total == 500
    assert unsupported == 500


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
