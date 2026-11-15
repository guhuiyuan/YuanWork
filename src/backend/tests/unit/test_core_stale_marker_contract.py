"""A page whose file changed says so.

When a contract is revised on disk, the page compiled from the old
version is kept - it is what the document said - and it is marked. The
reader has to know that the file in front of him is not the one the
page was read from, until he asks for a recompile.

* AC-E-015 - the old page is kept and marked when its source file
  changes
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from yuanwork.db.sqlite import SQLiteStore


@pytest.fixture
async def store(tmp_path: Path):
    s = SQLiteStore(tmp_path / "store.db")
    await s.initialize()
    yield s
    await s.close()


class FakeLLM:
    """A model that compiles a document."""

    def __init__(self):
        self.calls: list[str] = []

    async def complete(self, task):
        self.calls.append(task.messages[0].content)
        answer = {
            "entity_pages": [
                {
                    "title": "验收期限",
                    "summary": "验收期限怎么算。",
                    "key_points": ["验收期限怎么约定"],
                    "conclusions": [{"text": "验收期限为 30 日", "segment": 0}],
                }
            ],
            "concept_pages": [],
            "comparison_tables": [],
        }
        return type("R", (), {"content": json.dumps(answer, ensure_ascii=False)})()


class FakeSemantic:
    async def add_chunks(self, chunks):
        return None

    async def delete_by_doc_id(self, doc_id):
        return None


async def pipeline_for(store, llm):
    from yuanwork.ingestion.pipeline import IngestionPipeline

    return IngestionPipeline(
        sqlite_store=store,
        semantic_memory=FakeSemantic(),
        llm_gateway=llm,
    )


# --------------------------------------------------------------------------
# the detection
# --------------------------------------------------------------------------


async def test_a_changed_file_marks_its_pages(store, tmp_path: Path):
    """The page is kept, and it is marked."""
    llm = FakeLLM()
    pipeline = await pipeline_for(store, llm)

    path = tmp_path / "采购合同.md"
    path.write_text("# 采购合同\n\n验收期限为 30 日。\n", encoding="utf-8")

    first = await pipeline.ingest_file(path)
    assert first["status"] == "indexed"
    doc_id = first["doc_id"]

    pages = await store.list_wiki_pages()
    assert len(pages) == 1
    assert pages[0]["source_doc_ids"] == [doc_id]

    # The file changes on disk.
    path.write_text("# 采购合同\n\n验收期限为 15 日。\n", encoding="utf-8")

    second = await pipeline.ingest_file(path)

    assert second["status"] == "skipped"
    assert second["reason"] == "modified"

    record = await store.get_doc_tier(doc_id)
    assert record["stale"] == 1

    # The old page is still there - it is what the document said.
    pages = await store.list_wiki_pages()
    assert len(pages) == 1


async def test_the_page_says_its_file_changed(store, tmp_path: Path):
    """The reader is told, in the page list and in the page itself."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from yuanwork.api import deps
    from yuanwork.api.routes import wiki as wiki_routes

    llm = FakeLLM()
    pipeline = await pipeline_for(store, llm)

    path = tmp_path / "采购合同.md"
    path.write_text("# 采购合同\n\n验收期限为 30 日。\n", encoding="utf-8")
    await pipeline.ingest_file(path)

    path.write_text("# 采购合同\n\n验收期限为 15 日。\n", encoding="utf-8")
    await pipeline.ingest_file(path)

    deps.set_sqlite_store(store)
    app = FastAPI()
    app.include_router(wiki_routes.router, prefix="/api/v1")
    with TestClient(app) as client:
        listing = client.get("/api/v1/wiki/pages")
        detail = client.get("/api/v1/wiki/pages/core%2F验收期限")
    deps.reset_dependencies()

    assert listing.json()["pages"][0]["stale"] is True
    assert detail.json()["stale"] is True


async def test_a_page_whose_file_did_not_change_is_not_marked(store, tmp_path: Path):
    llm = FakeLLM()
    pipeline = await pipeline_for(store, llm)

    path = tmp_path / "采购合同.md"
    path.write_text("# 采购合同\n\n验收期限为 30 日。\n", encoding="utf-8")
    await pipeline.ingest_file(path)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from yuanwork.api import deps
    from yuanwork.api.routes import wiki as wiki_routes

    deps.set_sqlite_store(store)
    app = FastAPI()
    app.include_router(wiki_routes.router, prefix="/api/v1")
    with TestClient(app) as client:
        listing = client.get("/api/v1/wiki/pages")
    deps.reset_dependencies()

    assert listing.json()["pages"][0]["stale"] is False


# --------------------------------------------------------------------------
# until the user asks for a recompile
# --------------------------------------------------------------------------


async def test_the_page_is_kept_until_the_user_recompiles(store, tmp_path: Path):
    """Nothing is recompiled on its own - the old page stays."""
    llm = FakeLLM()
    pipeline = await pipeline_for(store, llm)

    path = tmp_path / "采购合同.md"
    path.write_text("# 采购合同\n\n验收期限为 30 日。\n", encoding="utf-8")
    await pipeline.ingest_file(path)
    calls_after_first = len(llm.calls)

    path.write_text("# 采购合同\n\n验收期限为 15 日。\n", encoding="utf-8")
    await pipeline.ingest_file(path)

    # The model was not asked again - the user has not asked for it.
    assert len(llm.calls) == calls_after_first

    pages = await store.list_wiki_pages()
    assert len(pages) == 1
    assert "验收期限为 30 日" in pages[0]["markdown"]


async def test_an_explicit_rebuild_compiles_the_new_version(store, tmp_path: Path):
    """The user asks for it - the new version is compiled."""
    llm = FakeLLM()
    pipeline = await pipeline_for(store, llm)

    path = tmp_path / "采购合同.md"
    path.write_text("# 采购合同\n\n验收期限为 30 日。\n", encoding="utf-8")
    await pipeline.ingest_file(path)

    path.write_text("# 采购合同\n\n验收期限为 15 日。\n", encoding="utf-8")
    result = await pipeline.ingest_file(path, force=True)

    assert result["status"] == "indexed"
    assert result["compiled"] is True

    pages = await store.list_wiki_pages()
    assert len(pages) == 1
    # The page was written again - it is the new version now.
    assert pages[0]["version"] == 2


async def test_the_old_version_is_marked_and_the_new_one_is_not(store, tmp_path: Path):
    """The new page is the one in effect; the old record is history."""
    llm = FakeLLM()
    pipeline = await pipeline_for(store, llm)

    path = tmp_path / "采购合同.md"
    path.write_text("# 采购合同\n\n验收期限为 30 日。\n", encoding="utf-8")
    first = await pipeline.ingest_file(path)

    path.write_text("# 采购合同\n\n验收期限为 15 日。\n", encoding="utf-8")
    second = await pipeline.ingest_file(path, force=True)

    old = await store.get_doc_tier(first["doc_id"])
    assert old["stale"] == 1

    new = await store.get_doc_tier(second["doc_id"])
    assert new["stale"] == 0
