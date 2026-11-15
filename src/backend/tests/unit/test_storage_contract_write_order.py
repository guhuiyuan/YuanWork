"""The order in which the three stores are written and deleted.

There is no transaction across the three stores, so the *order* is the
only thing that keeps them roughly in sync. This pins it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from yuanwork.ingestion.parser.base import BaseParser, RawBlock
from yuanwork.ingestion.parser.dispatcher import ParserDispatcher
from yuanwork.ingestion.pipeline import IngestionPipeline


class RecordingSQLite:
    def __init__(self, events: list[str]):
        self.events = events
        self.upserts: list[dict] = []

    async def get_file_by_hash(self, file_hash: str):
        self.events.append("sqlite.lookup")
        return None

    async def upsert_file(self, **kwargs):
        self.events.append("sqlite.upsert")
        self.upserts.append(kwargs)
        return kwargs["doc_id"]


class RecordingSemantic:
    """Records when the vector store (and its index mirror) is written."""

    def __init__(self, events: list[str]):
        self.events = events
        self.added: list[dict] = []

    async def add_chunks(self, chunks):
        self.events.append("vector.write")
        self.added.extend(chunks)
        return [c["id"] for c in chunks]


class RecordingParser(BaseParser):
    SUPPORTED_EXTENSIONS = ("md",)

    async def parse(self, file_path: Path, doc_id: str) -> list[RawBlock]:
        return [
            RawBlock(
                block_id=f"{doc_id}_b1",
                doc_id=doc_id,
                content="正文内容" * 10,
                type="text",
                meta={"page": 3},
            )
        ]

    async def extract_metadata(self, file_path: Path) -> dict[str, Any]:
        return {"size": 1}


@pytest.fixture
def events() -> list[str]:
    return []


def make_pipeline(events: list[str]) -> IngestionPipeline:
    pipeline = IngestionPipeline(
        sqlite_store=RecordingSQLite(events),  # type: ignore[arg-type]
        semantic_memory=RecordingSemantic(events),  # type: ignore[arg-type]
    )
    pipeline.dispatcher = ParserDispatcher([RecordingParser()])
    return pipeline


# --------------------------------------------------------------------------
# the write path
# --------------------------------------------------------------------------


async def test_the_vector_store_is_written_before_the_metadata_row(
    events, tmp_path: Path
):
    """A chunk that is in the vector store but has no metadata row is
    searchable; the other way round is not."""
    from unit.ingestion_fixtures import make_markdown_file

    path = make_markdown_file(tmp_path, "doc.md", "# 标题\n\n正文\n")
    pipeline = make_pipeline(events)

    await pipeline.ingest_file(path)

    assert events.index("vector.write") < events.index("sqlite.upsert")


async def test_the_write_reports_the_document_and_chunk_counts(events, tmp_path: Path):
    from unit.ingestion_fixtures import make_markdown_file

    path = make_markdown_file(tmp_path, "doc.md", "# 标题\n\n正文\n")
    pipeline = make_pipeline(events)

    result = await pipeline.ingest_file(path)
    assert result["status"] == "indexed"
    assert result["blocks"] == 1
    assert result["chunks"] == 1


async def test_the_metadata_row_carries_the_file_facts(events, tmp_path: Path):
    from unit.ingestion_fixtures import make_markdown_file

    path = make_markdown_file(tmp_path, name="doc.md", content="# 标题\n\n正文\n")
    pipeline = make_pipeline(events)
    await pipeline.ingest_file(path)

    row = pipeline.sqlite.upserts[0]
    assert row["file_name"] == "doc.md"
    assert row["file_type"] == "md"
    assert row["size_bytes"] == path.stat().st_size
    assert row["chunk_count"] == 1


# --------------------------------------------------------------------------
# the delete path
# --------------------------------------------------------------------------


async def test_the_metadata_row_is_deleted_before_the_chunks():
    """The knowledge-base route deletes the row first, then the chunks."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from yuanwork.api import deps
    from yuanwork.api.routes.kb import router

    events: list[str] = []

    class Store:
        async def get_file(self, doc_id):
            return {"doc_id": doc_id, "chunk_count": 5}

        async def delete_file(self, doc_id):
            events.append("sqlite.delete")
            return True

    class Semantic:
        async def delete_by_doc_id(self, doc_id):
            events.append("vector.delete")
            return 5

    deps.set_sqlite_store(Store())  # type: ignore[arg-type]
    deps.set_semantic_memory(Semantic())  # type: ignore[arg-type]

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    client = TestClient(app)
    try:
        resp = client.delete("/api/v1/kb/files/d1")
        assert resp.status_code == 200
        assert resp.json()["chunks_removed"] == 5
        assert events == ["sqlite.delete", "vector.delete"]
    finally:
        deps.reset_dependencies()


async def test_the_removed_count_comes_from_the_pre_delete_record(events, tmp_path):
    """The route reads the chunk count before it deletes the row."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from yuanwork.api import deps
    from yuanwork.api.routes.kb import router

    class Store:
        async def get_file(self, doc_id):
            return {"doc_id": doc_id, "chunk_count": 42}

        async def delete_file(self, doc_id):
            return True

    class Semantic:
        async def delete_by_doc_id(self, doc_id):
            return 0  # the vector store had nothing left

    deps.set_sqlite_store(Store())  # type: ignore[arg-type]
    deps.set_semantic_memory(Semantic())  # type: ignore[arg-type]

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    client = TestClient(app)
    try:
        resp = client.delete("/api/v1/kb/files/d1")
        assert resp.json()["chunks_removed"] == 42
    finally:
        deps.reset_dependencies()
