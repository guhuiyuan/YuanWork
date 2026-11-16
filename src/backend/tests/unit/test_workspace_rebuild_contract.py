"""TASK-051 — the build the workspace drives.

The desktop app no longer indexes a folder directly: the user picks a
working folder, it becomes a *workspace entry*, and the knowledge base
is built from the workspace. The build is the parallel one - the files
are read in worker processes and the slices are written in batches
(TASK-045) - and its progress is polled with the same status endpoint
the folder build uses, so the frontend needs no new polling code.

* AC-F-001 — the workspace is the scope; the entries can be read and
  the build runs over them
* AC-F-002 — the supported files of the workspace are parsed, chunked
  and written into the index; the same content is only indexed once
* AC-E-004 — an unsupported file is reported with the reason
* AC-E-006 — a file above the size limit is skipped, not read
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from yuanwork.api import deps
from yuanwork.api.routes import ingest as ingest_routes
from yuanwork.db.sqlite import SQLiteStore
from yuanwork.ingestion.batch import BatchIngestor
from yuanwork.ingestion.pipeline import IngestionPipeline
from yuanwork.workspace import Workspace


# --------------------------------------------------------------------------
# doubles
# --------------------------------------------------------------------------


class FakeSemantic:
    """A vector store that remembers what went in and what was taken out."""

    def __init__(self) -> None:
        self.added: list[dict] = []
        self.deleted: list[str] = []
        self.last_timings = {"embed": 0.0, "write": 0.0}

    async def add_chunks(self, chunks: list[dict]) -> list[str]:
        self.added.extend(chunks)
        return [c["id"] for c in chunks]

    async def delete_by_doc_id(self, doc_id: str) -> int:
        self.deleted.append(doc_id)
        return 1

    async def count(self) -> int:
        return len(self.added)


@pytest.fixture
async def store(tmp_path: Path):
    sqlite = SQLiteStore(tmp_path / "store.db")
    await sqlite.initialize()
    yield sqlite
    await sqlite.close()


@pytest.fixture
def semantic() -> FakeSemantic:
    return FakeSemantic()


@pytest.fixture
def client(store: SQLiteStore, semantic: FakeSemantic):
    """The ingest endpoints, with the stores injected."""
    deps.set_sqlite_store(store)
    deps.set_semantic_memory(semantic)  # type: ignore[arg-type]

    app = FastAPI()
    app.include_router(ingest_routes.router, prefix="/api/v1")
    yield TestClient(app)
    deps.reset_dependencies()


@pytest.fixture
def pipeline(store: SQLiteStore, semantic: FakeSemantic) -> IngestionPipeline:
    return IngestionPipeline(sqlite_store=store, semantic_memory=semantic)  # type: ignore[arg-type]


@pytest.fixture
def docs(tmp_path: Path) -> Path:
    """The folder the workspace covers.

    It is a folder of its own: the store's own files must not end up in
    the knowledge base.
    """
    folder = tmp_path / "docs"
    folder.mkdir()
    return folder


@pytest.fixture(autouse=True)
def _threads_not_processes(monkeypatch):
    """The build reads in worker *threads* here.

    The production build uses worker processes; the tests read in this
    process so the parser the pipeline holds is the one that runs.
    """

    class ThreadedBatchIngestor(BatchIngestor):
        def __init__(self, pipeline, **kwargs):
            kwargs.setdefault("executor", ThreadPoolExecutor(2))
            super().__init__(pipeline, **kwargs)

    monkeypatch.setattr(
        "yuanwork.ingestion.batch.BatchIngestor", ThreadedBatchIngestor
    )


def make_file(dir_path: Path, name: str, content: str | None = None) -> Path:
    """A file whose content differs per name - same content means one index."""
    dir_path.mkdir(parents=True, exist_ok=True)
    path = dir_path / name
    if content is None:
        content = f"# {name}\n\n正文内容\n"
    path.write_text(content, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# the endpoint
# --------------------------------------------------------------------------


async def test_the_rebuild_returns_a_job(
    client: TestClient, store: SQLiteStore, docs: Path
):
    """The rebuild answers with a job the frontend can poll."""
    await Workspace(store).add_path(make_file(docs, "a.md"))

    resp = client.post("/api/v1/ingest/rebuild")

    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"]
    assert body["files_queued"] == 1
    assert body["estimated_seconds"] >= 0


async def test_the_job_total_is_the_number_of_files_the_workspace_covers(
    client: TestClient, store: SQLiteStore, docs: Path
):
    """The progress bar gets a real denominator.

    A folder with two files plus a single file added by hand is three
    files - the same file counted once, even when the folder holds it
    too.
    """
    inside = make_file(docs, "a.md")
    make_file(docs, "b.md")
    loose = make_file(docs, "c.md")

    workspace = Workspace(store)
    await workspace.add_path(docs)
    await workspace.add_path(loose)
    # The file in the folder is added by hand as well - it is one file.
    await workspace.add_path(inside)

    resp = client.post("/api/v1/ingest/rebuild")
    body = resp.json()

    assert body["files_queued"] == 3

    job = await store.get_ingest_job(body["job_id"])
    assert job["files_total"] == 3


async def test_the_files_are_indexed_through_the_workspace_entries(
    client: TestClient, store: SQLiteStore, docs: Path
):
    """The build reads the files the workspace covers."""
    make_file(docs, "a.md")
    make_file(docs, "b.md")
    loose = make_file(docs, "c.md")

    workspace = Workspace(store)
    await workspace.add_path(docs)
    await workspace.add_path(loose)

    resp = client.post("/api/v1/ingest/rebuild")
    job_id = resp.json()["job_id"]

    rows = await store.list_files()
    assert {row["file_name"] for row in rows} == {"a.md", "b.md", "c.md"}

    status = client.get(f"/api/v1/ingest/status/{job_id}").json()
    assert status["status"] == "completed"
    assert status["progress"] == 1.0
    assert status["files_total"] == 3


async def test_a_file_that_is_already_indexed_is_not_indexed_twice(
    client: TestClient, store: SQLiteStore, docs: Path
):
    """The same content is in the index once - the build resumes."""
    doc = make_file(docs, "a.md")
    await Workspace(store).add_path(doc)

    first = client.post("/api/v1/ingest/rebuild").json()
    assert first["files_queued"] == 1

    rows = await store.list_files()
    assert len(rows) == 1

    second = client.post("/api/v1/ingest/rebuild").json()

    assert second["files_queued"] == 1
    rows = await store.list_files()
    assert len(rows) == 1, "the file was indexed a second time"


async def test_an_empty_workspace_is_not_an_error(
    client: TestClient, store: SQLiteStore
):
    """Nothing to build is not a failure."""
    resp = client.post("/api/v1/ingest/rebuild")

    assert resp.status_code == 200
    body = resp.json()
    assert body["files_queued"] == 0

    status = client.get(f"/api/v1/ingest/status/{body['job_id']}").json()
    assert status["status"] == "completed"
    assert status["error"] is None


# --------------------------------------------------------------------------
# the build itself
# --------------------------------------------------------------------------


async def test_the_build_is_the_parallel_one(
    pipeline: IngestionPipeline, store: SQLiteStore, docs: Path
):
    """The workspace build goes through the batch ingestor.

    The batch ingestor is the parallel build: the files are read in
    worker processes and the slices are written in batches (TASK-045).
    """
    used: list[str] = []
    original = BatchIngestor.ingest

    async def _record(self, *args, **kwargs):
        used.append("batch")
        return await original(self, *args, **kwargs)

    BatchIngestor.ingest = _record  # type: ignore[method-assign]
    try:
        await Workspace(store).add_path(make_file(docs, "a.md"))
        result = await pipeline.build_workspace()
    finally:
        BatchIngestor.ingest = original  # type: ignore[method-assign]

    assert used == ["batch"]
    assert result["indexed"] == 1


async def test_an_unsupported_file_is_reported_with_a_reason(
    pipeline: IngestionPipeline, store: SQLiteStore, docs: Path
):
    """A file nobody can index is reported - never dropped silently."""
    make_file(docs, "a.md")
    (docs / "note.xyz").write_text("unsupported", encoding="utf-8")
    await Workspace(store).add_path(docs)

    result = await pipeline.build_workspace()

    assert result["unsupported"] == 1
    assert len(result["unsupported_files"]) == 1
    entry = result["unsupported_files"][0]
    assert entry["path"].endswith("note.xyz")
    assert ".xyz" in entry["reason"]
    # The rest of the workspace is still indexed.
    assert result["indexed"] == 1


async def test_a_file_above_the_size_limit_is_skipped(
    pipeline: IngestionPipeline, store: SQLiteStore, docs: Path
):
    """A file too big to read is skipped - and it is said why."""
    make_file(docs, "big.md", "# 大文件\n\n" + "正文" * 500 + "\n")
    await Workspace(store).add_path(docs / "big.md")
    pipeline.max_file_size = 10  # bytes

    result = await pipeline.build_workspace()

    assert result["indexed"] == 0
    assert result["skipped"] == 1
    assert "体积上限" in result["files"][0]["reason"]
    assert await store.list_files() == []


async def test_the_progress_counts_the_work_not_the_workspace(
    pipeline: IngestionPipeline, store: SQLiteStore, docs: Path
):
    """The bar counts the files the build works on.

    A workspace is mostly files nobody can index - a code project, an
    archive. Counting those would put the bar at 97% before the first
    document was read, and the number on it would say nothing about
    how much work is left.
    """
    make_file(docs, "a.md")
    (docs / "note.xyz").write_text("unsupported", encoding="utf-8")
    await Workspace(store).add_path(docs)

    seen: list[tuple[int, int]] = []
    await pipeline.build_workspace(
        on_progress=lambda done, total, current: seen.append((done, total))
    )

    assert seen, "the build said nothing about its progress"
    assert {total for _, total in seen} == {1}, (
        "the file nobody can index was counted as work"
    )
    assert seen[-1][0] == 1


async def test_the_job_counts_the_work_not_every_file_in_the_workspace(
    client: TestClient, store: SQLiteStore, docs: Path
):
    """The number the job carries is the number of files to read.

    A workspace holds far more files than can be indexed; the job says
    how much work there is, not how much material is lying around.
    """
    make_file(docs, "a.md")
    (docs / "note.xyz").write_text("unsupported", encoding="utf-8")
    await Workspace(store).add_path(docs)

    body = client.post("/api/v1/ingest/rebuild").json()

    assert body["files_queued"] == 1, "the file nobody can index was counted"

    job = await store.get_ingest_job(body["job_id"])
    assert job["files_total"] == 1


async def test_the_progress_counts_the_files_that_are_already_indexed(
    pipeline: IngestionPipeline, store: SQLiteStore, docs: Path
):
    """A file that is already in the index is done - the progress says so.

    The build of a workspace whose files are all indexed has to reach
    100%: those files are in the index, they just were not read again.
    A progress bar that stops at 20% and then jumps to 100% at the end
    reads as a broken build.
    """
    make_file(docs, "a.md")
    make_file(docs, "b.md")
    await Workspace(store).add_path(docs)

    await pipeline.build_workspace()

    seen: list[tuple[int, int]] = []
    await pipeline.build_workspace(
        on_progress=lambda done, total, current: seen.append((done, total))
    )

    assert seen, "the build said nothing about its progress"
    assert {total for _, total in seen} == {2}
    assert seen[-1][0] == 2, "the already indexed files did not count as done"


async def test_a_single_file_entry_remembers_its_document(
    pipeline: IngestionPipeline, store: SQLiteStore, docs: Path
):
    """The file the user added by hand knows what it was indexed under."""
    doc = make_file(docs, "a.md")
    await Workspace(store).add_path(doc)

    await pipeline.build_workspace()

    entries = await Workspace(store).list_entries()
    assert len(entries) == 1
    assert entries[0]["kind"] == "file"
    assert entries[0]["doc_id"], "the entry did not remember its document"


async def test_the_build_reports_the_files_it_could_not_index(
    client: TestClient, store: SQLiteStore, docs: Path
):
    """The job finishes even when a file in the workspace cannot be read.

    The file nobody can index is not work - it goes into the result's
    list of reasons, not into the denominator of the progress (TASK-052).
    """
    make_file(docs, "a.md")
    (docs / "note.xyz").write_text("unsupported", encoding="utf-8")
    await Workspace(store).add_path(docs)

    job_id = client.post("/api/v1/ingest/rebuild").json()["job_id"]

    status = client.get(f"/api/v1/ingest/status/{job_id}").json()
    assert status["status"] == "completed"
    assert status["files_total"] == 1
    assert len(await store.list_files()) == 1
