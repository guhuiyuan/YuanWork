"""TASK-001 — the workspace: its entries, its four endpoints, and the
pipeline that walks them.

The workspace is the scope of the knowledge base: a folder plus whatever
single files were added by hand. Everything the user takes back out has to
leave the index with it, and everything that cannot be indexed has to be
*reported* - a file that silently disappears is a file the user will keep
asking about.

* AC-F-001 — an entry per folder and per single file; the entries can be
  listed and removed
* AC-F-002 — the build walks the workspace entries
* AC-E-001 — an empty knowledge base says so, and says what to do about it
* AC-E-002 — removing an entry clears what it brought in
* AC-E-003 — a changed file keeps the indexed version until a rebuild
* AC-E-004 — an unsupported file is listed with the reason
* AC-E-005 — a broken file is recorded and the rest still gets indexed
* AC-E-006 — a file above the size limit is skipped instead of read
* AC-E-012 — the same file added twice is indexed once
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from yuanwork.api import deps
from yuanwork.api.routes import workspace as workspace_routes
from yuanwork.db.sqlite import SQLiteStore
from yuanwork.ingestion.parser.dispatcher import ParserDispatcher
from yuanwork.ingestion.pipeline import IngestionPipeline
from yuanwork.workspace import Workspace
from unit.ingestion_fixtures import RecordingChunker, RecordingParser


# --------------------------------------------------------------------------
# doubles
# --------------------------------------------------------------------------


class FakeSemantic:
    """A vector store that remembers what went in and what was taken out."""

    def __init__(self) -> None:
        self.added: list[dict] = []
        self.deleted: list[str] = []

    async def add_chunks(self, chunks: list[dict]) -> list[str]:
        self.added.extend(chunks)
        return [c["id"] for c in chunks]

    async def delete_by_doc_id(self, doc_id: str) -> int:
        self.deleted.append(doc_id)
        return 1

    async def count(self) -> int:
        return len(self.added)


class FailingParser(RecordingParser):
    """A parser that chokes on files whose name starts with ``broken``."""

    async def parse(self, file_path: Path, doc_id: str):
        if file_path.name.startswith("broken"):
            raise ValueError("文件已损坏，无法解析")
        return await super().parse(file_path, doc_id)


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture
async def store(tmp_path: Path):
    """A real SQLite store - the workspace table is part of what is tested."""
    sqlite = SQLiteStore(tmp_path / "store.db")
    await sqlite.initialize()
    yield sqlite
    await sqlite.close()


@pytest.fixture
def semantic() -> FakeSemantic:
    return FakeSemantic()


@pytest.fixture
def client(store: SQLiteStore, semantic: FakeSemantic):
    """The four workspace endpoints, with the stores injected."""
    deps.set_sqlite_store(store)
    deps.set_semantic_memory(semantic)  # type: ignore[arg-type]

    app = FastAPI()
    app.include_router(workspace_routes.router, prefix="/api/v1")
    yield TestClient(app)
    deps.reset_dependencies()


@pytest.fixture
def workspace(store: SQLiteStore) -> Workspace:
    return Workspace(store)


@pytest.fixture
def pipeline(store: SQLiteStore, semantic: FakeSemantic) -> IngestionPipeline:
    """A pipeline whose parser only understands Markdown."""
    pipe = IngestionPipeline(sqlite_store=store, semantic_memory=semantic)  # type: ignore[arg-type]
    pipe.dispatcher = ParserDispatcher([RecordingParser()])
    pipe.chunker = RecordingChunker()
    return pipe


@pytest.fixture
def agent_store():
    """The file store the agent's localize step reaches for."""
    deps.set_sqlite_store(_EmptySQLite())  # type: ignore[arg-type]
    yield
    deps.reset_dependencies()


def make_file(dir_path: Path, name: str, content: str | None = None) -> Path:
    """A file whose content differs per name - same content means one index."""
    dir_path.mkdir(parents=True, exist_ok=True)
    path = dir_path / name
    if content is None:
        content = f"# {name}\n\n正文\n"
    path.write_text(content, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# AC-F-001 — the entries and the four endpoints
# --------------------------------------------------------------------------


async def test_a_folder_becomes_an_entry(client: TestClient, tmp_path: Path):
    folder = tmp_path / "docs"
    folder.mkdir()

    resp = client.post("/api/v1/workspace/folders", json={"path": str(folder)})

    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "folder"
    assert body["path"] == str(folder.resolve())
    assert body["included"] is True
    assert body["id"]


async def test_a_single_file_becomes_an_entry(client: TestClient, tmp_path: Path):
    doc = make_file(tmp_path, "a.md")

    resp = client.post("/api/v1/workspace/files", json={"path": str(doc)})

    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "file"
    assert body["path"] == str(doc.resolve())


async def test_an_entry_can_be_added_without_indexing_it(
    client: TestClient, tmp_path: Path
):
    """The `included` flag is honoured - not accepted and then dropped."""
    doc = make_file(tmp_path, "a.md")

    resp = client.post(
        "/api/v1/workspace/files", json={"path": str(doc), "included": False}
    )

    assert resp.status_code == 200
    assert resp.json()["included"] is False

    entries = client.get("/api/v1/workspace/entries").json()["entries"]
    assert entries[0]["included"] is False


async def test_the_entries_can_be_listed(client: TestClient, tmp_path: Path):
    folder = tmp_path / "docs"
    folder.mkdir()
    doc = make_file(tmp_path, "a.md")

    client.post("/api/v1/workspace/folders", json={"path": str(folder)})
    client.post("/api/v1/workspace/files", json={"path": str(doc)})

    resp = client.get("/api/v1/workspace/entries")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert {e["kind"] for e in body["entries"]} == {"folder", "file"}


async def test_an_entry_can_be_removed(client: TestClient, tmp_path: Path):
    doc = make_file(tmp_path, "a.md")
    entry = client.post("/api/v1/workspace/files", json={"path": str(doc)}).json()

    resp = client.delete(f"/api/v1/workspace/entries/{entry['id']}")

    assert resp.status_code == 200
    assert resp.json()["removed"] == entry["id"]
    assert client.get("/api/v1/workspace/entries").json()["total"] == 0


async def test_the_same_path_is_only_registered_once(
    client: TestClient, tmp_path: Path
):
    """Adding the same path twice must not double it."""
    doc = make_file(tmp_path, "a.md")

    first = client.post("/api/v1/workspace/files", json={"path": str(doc)}).json()
    second = client.post("/api/v1/workspace/files", json={"path": str(doc)}).json()

    assert first["id"] == second["id"]
    assert client.get("/api/v1/workspace/entries").json()["total"] == 1


async def test_a_missing_path_is_rejected(client: TestClient, tmp_path: Path):
    resp = client.post(
        "/api/v1/workspace/folders", json={"path": str(tmp_path / "nope")}
    )
    assert resp.status_code == 404


async def test_a_file_is_not_a_folder(client: TestClient, tmp_path: Path):
    doc = make_file(tmp_path, "a.md")
    resp = client.post("/api/v1/workspace/folders", json={"path": str(doc)})
    assert resp.status_code == 400


async def test_removing_an_unknown_entry_answers_404(client: TestClient):
    resp = client.delete("/api/v1/workspace/entries/nope")
    assert resp.status_code == 404


async def test_the_entries_are_listed_in_the_order_they_were_added(
    workspace: Workspace, tmp_path: Path
):
    first = make_file(tmp_path, "a.md")
    second = make_file(tmp_path, "b.md")

    await workspace.add_path(first)
    await workspace.add_path(second)

    entries = await workspace.list_entries()
    assert [e["path"] for e in entries] == [
        str(first.resolve()),
        str(second.resolve()),
    ]


# --------------------------------------------------------------------------
# AC-F-002 — the build walks the workspace
# --------------------------------------------------------------------------


async def test_the_build_indexes_the_files_of_the_workspace(
    workspace: Workspace, pipeline: IngestionPipeline, tmp_path: Path
):
    folder = tmp_path / "docs"
    folder.mkdir()
    make_file(folder, "a.md")
    make_file(folder, "b.md")
    loose = make_file(tmp_path, "c.md")

    await workspace.add_path(folder)
    await workspace.add_path(loose)

    result = await pipeline.ingest_workspace()

    assert result["indexed"] == 3
    assert result["errors"] == 0
    rows = await pipeline.sqlite.list_files()
    assert {row["file_name"] for row in rows} == {"a.md", "b.md", "c.md"}


async def test_the_build_reports_progress_over_the_workspace(
    workspace: Workspace, pipeline: IngestionPipeline, tmp_path: Path
):
    folder = tmp_path / "docs"
    folder.mkdir()
    make_file(folder, "a.md")
    await workspace.add_path(folder)

    seen: list[tuple[int, int]] = []
    await pipeline.ingest_workspace(
        on_progress=lambda done, total, current: seen.append((done, total))
    )

    assert seen == [(1, 1)]


# --------------------------------------------------------------------------
# AC-E-012 — the same file twice
# --------------------------------------------------------------------------


async def test_a_file_covered_twice_is_indexed_once(
    workspace: Workspace, pipeline: IngestionPipeline, tmp_path: Path
):
    """The file sits in an added folder *and* was added by hand."""
    folder = tmp_path / "docs"
    folder.mkdir()
    doc = make_file(folder, "a.md")

    await workspace.add_path(folder)
    await workspace.add_path(doc)

    files = await workspace.list_files()
    assert len(files) == 1

    result = await pipeline.ingest_workspace()
    assert result["indexed"] == 1
    assert len(await pipeline.sqlite.list_files()) == 1


# --------------------------------------------------------------------------
# AC-E-002 — removing an entry clears the index
# --------------------------------------------------------------------------


async def test_removing_a_file_entry_clears_its_index(
    client: TestClient,
    workspace: Workspace,
    pipeline: IngestionPipeline,
    semantic: FakeSemantic,
    tmp_path: Path,
):
    doc = make_file(tmp_path, "a.md")
    await workspace.add_path(doc)
    await pipeline.ingest_workspace()

    entry = (await workspace.list_entries())[0]
    assert len(await pipeline.sqlite.list_files()) == 1

    resp = client.delete(f"/api/v1/workspace/entries/{entry['id']}")

    assert resp.status_code == 200
    assert resp.json()["indexed_files_removed"] == [str(doc.resolve())]
    assert await pipeline.sqlite.list_files() == []
    assert len(semantic.deleted) == 1


async def test_removing_a_folder_keeps_a_file_another_entry_covers(
    client: TestClient,
    workspace: Workspace,
    pipeline: IngestionPipeline,
    tmp_path: Path,
):
    """A file added by hand survives the removal of the folder around it."""
    folder = tmp_path / "docs"
    folder.mkdir()
    doc = make_file(folder, "a.md")

    await workspace.add_path(folder)
    await workspace.add_path(doc)
    await pipeline.ingest_workspace()

    folder_entry = next(
        e for e in await workspace.list_entries() if e["kind"] == "folder"
    )
    resp = client.delete(f"/api/v1/workspace/entries/{folder_entry['id']}")

    assert resp.status_code == 200
    assert resp.json()["indexed_files_removed"] == []
    assert len(await pipeline.sqlite.list_files()) == 1


# --------------------------------------------------------------------------
# AC-E-003 — a changed file keeps the indexed version
# --------------------------------------------------------------------------


async def test_a_changed_file_keeps_the_indexed_version(
    workspace: Workspace, pipeline: IngestionPipeline, tmp_path: Path
):
    doc = make_file(tmp_path, "a.md", "# 标题\n\n原来的内容\n")
    await workspace.add_path(doc)
    await pipeline.ingest_workspace()

    first_row = (await pipeline.sqlite.list_files())[0]

    doc.write_text("# 标题\n\n改过之后的内容\n", encoding="utf-8")
    result = await pipeline.ingest_workspace()

    assert result["files"][0]["status"] == "skipped"
    assert result["files"][0]["reason"] == "modified"
    # the old version is still the indexed one
    rows = await pipeline.sqlite.list_files()
    assert len(rows) == 1
    assert rows[0]["file_hash"] == first_row["file_hash"]


async def test_a_rebuild_picks_the_changed_file_up(
    workspace: Workspace, pipeline: IngestionPipeline, tmp_path: Path
):
    doc = make_file(tmp_path, "a.md", "# 标题\n\n原来的内容\n")
    await workspace.add_path(doc)
    await pipeline.ingest_workspace()

    first_hash = (await pipeline.sqlite.list_files())[0]["file_hash"]

    doc.write_text("# 标题\n\n改过之后的内容\n", encoding="utf-8")
    result = await pipeline.ingest_workspace(force=True)

    assert result["indexed"] == 1
    rows = await pipeline.sqlite.list_files()
    assert len(rows) == 1
    assert rows[0]["file_hash"] != first_hash


# --------------------------------------------------------------------------
# AC-E-004 — an unsupported file is reported
# --------------------------------------------------------------------------


async def test_an_unsupported_file_is_listed_with_the_reason(
    workspace: Workspace, pipeline: IngestionPipeline, tmp_path: Path
):
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "note.xyz").write_text("unsupported", encoding="utf-8")

    await workspace.add_path(folder)
    result = await pipeline.ingest_workspace()

    assert result["unsupported"] == 1
    assert len(result["unsupported_files"]) == 1
    entry = result["unsupported_files"][0]
    assert entry["path"].endswith("note.xyz")
    assert ".xyz" in entry["reason"]


# --------------------------------------------------------------------------
# AC-E-005 — a broken file does not stop the others
# --------------------------------------------------------------------------


async def test_a_broken_file_is_recorded_and_the_rest_still_gets_indexed(
    workspace: Workspace, pipeline: IngestionPipeline, tmp_path: Path
):
    folder = tmp_path / "docs"
    folder.mkdir()
    make_file(folder, "broken.md")
    make_file(folder, "good.md")

    await workspace.add_path(folder)
    pipeline.dispatcher = ParserDispatcher([FailingParser()])

    result = await pipeline.ingest_workspace()

    assert result["errors"] == 1
    assert result["indexed"] == 1
    broken = next(f for f in result["files"] if "broken" in f["path"])
    assert broken["status"] == "error"
    assert "损坏" in broken["error"]
    rows = await pipeline.sqlite.list_files()
    assert [row["file_name"] for row in rows] == ["good.md"]


# --------------------------------------------------------------------------
# AC-E-006 — a file above the size limit is skipped
# --------------------------------------------------------------------------


async def test_a_file_above_the_size_limit_is_skipped(
    workspace: Workspace, pipeline: IngestionPipeline, tmp_path: Path
):
    doc = make_file(tmp_path, "a.md", "# 标题\n\n" + "正文" * 500 + "\n")
    await workspace.add_path(doc)
    pipeline.max_file_size = 10  # bytes

    result = await pipeline.ingest_workspace()

    assert result["skipped"] == 1
    assert result["indexed"] == 0
    assert "体积上限" in result["files"][0]["reason"]
    assert await pipeline.sqlite.list_files() == []


# --------------------------------------------------------------------------
# AC-E-001 — an empty knowledge base says so
# --------------------------------------------------------------------------


async def test_an_empty_knowledge_base_asks_the_user_to_add_files(agent_store):
    from yuanwork.orchestrator.agent import AgentLoop

    class EmptySemantic:
        async def count(self) -> int:
            return 0

    agent = AgentLoop(
        llm_gateway=None,  # type: ignore[arg-type]
        semantic_memory=EmptySemantic(),  # type: ignore[arg-type]
        episodic_memory=None,  # type: ignore[arg-type]
    )
    agent.retriever = _EmptyRetriever()  # type: ignore[assignment]
    agent.scorer = _AlwaysRefuse()  # type: ignore[assignment]

    result = await agent.run(query="报销标准是多少", session_id="s1")

    assert result["refused"] is True
    assert "知识库" in result["answer"]
    assert "添加" in result["answer"]


async def test_a_knowledge_base_with_files_gets_the_usual_refusal(agent_store):
    from yuanwork.orchestrator.agent import AgentLoop

    class NotEmptySemantic:
        async def count(self) -> int:
            return 12

    agent = AgentLoop(
        llm_gateway=None,  # type: ignore[arg-type]
        semantic_memory=NotEmptySemantic(),  # type: ignore[arg-type]
        episodic_memory=None,  # type: ignore[arg-type]
    )
    agent.retriever = _EmptyRetriever()  # type: ignore[assignment]
    agent.scorer = _AlwaysRefuse()  # type: ignore[assignment]

    result = await agent.run(query="报销标准是多少", session_id="s1")

    assert result["refused"] is True
    assert "没有找到足够明确的证据" in result["answer"]


class _EmptyRetriever:
    async def search(self, query, top_k=10, doc_ids=None, file_types=None, min_score=0.0):
        return []


class _AlwaysRefuse:
    def score(self, evidence):
        return {
            "confidence": 0.0,
            "level": "low",
            "top_score": 0.0,
            "score_gap": 0.0,
            "evidence_count": len(evidence),
            "should_refuse": True,
            "reason": "low_confidence",
        }


class _EmptySQLite:
    async def list_files(self, limit=100, offset=0, file_type=None, search=None):
        return []

    async def count_files(self, file_type=None, search=None) -> int:
        return 0
