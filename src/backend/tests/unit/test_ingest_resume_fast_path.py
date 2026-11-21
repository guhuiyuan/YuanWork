"""A rebuild does not have to read every file to know it is done.

Hashing a file means reading every byte of it. A rebuild over a work
domain of 4413 files hashed all of them - about a minute of the two
minutes the rebuild took - only to find out that nothing had changed.
The ledger knows the file's size and the moment it was read: if both
are the same, the content cannot have changed either.

* AC-F-002 - the knowledge base really covers the files of the
  workspace (the files that are indexed are still recognised)
* AC-E-015 - a file that changed on disk is still noticed
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from yuanwork.ingestion.pipeline import IngestionPipeline


class _Semantic:
    embedding_max_tokens = 512
    embedding_model_name = "BAAI/bge-m3"

    def __init__(self, doc_ids: set[str] | None = None):
        self.doc_ids = doc_ids or set()

    async def indexed_doc_ids(self) -> set[str]:
        return set(self.doc_ids)


class _Store:
    """A ledger that knows one file."""

    def __init__(self, record: dict[str, Any] | None):
        self.record = record
        self.hashed: list[str] = []

    async def get_file_by_path(self, path: str) -> dict[str, Any] | None:
        return self.record

    async def get_file_by_hash(self, file_hash: str) -> dict[str, Any] | None:
        self.hashed.append(file_hash)
        # The ledger knows the file by its content: a different content
        # is not in it.
        if self.record and self.record.get("file_hash") == file_hash:
            return self.record
        return None

    async def set_doc_stale(self, doc_id: str, stale: bool = True):
        return True


def _pipeline(store: _Store, semantic: _Semantic) -> IngestionPipeline:
    return IngestionPipeline(sqlite_store=store, semantic_memory=semantic)


def _record(path: Path, doc_id: str = "d1", chunk_version: str | None = None) -> dict:
    stat = path.stat()
    return {
        "doc_id": doc_id,
        "file_path": str(path),
        "file_name": path.name,
        "size_bytes": stat.st_size,
        "chunk_version": chunk_version,
        "file_hash": hashlib.sha256(path.read_bytes()).hexdigest(),
        # The moment the file was read: a file that has not been
        # touched since then cannot have changed.
        "indexed_at": datetime.now().isoformat(),
        "meta": {"size_bytes": stat.st_size, "modified_at": stat.st_mtime},
    }


def test_an_untouched_file_is_not_read_at_all(tmp_path: Path):
    """The point of the whole thing: no hash, no read."""
    path = tmp_path / "a.md"
    path.write_text("内容", encoding="utf-8")
    pipeline = _pipeline(_Store(None), _Semantic({"d1"}))
    pipeline.sqlite = _Store(_record(path))
    pipeline.sqlite.record["chunk_version"] = pipeline.chunker.version

    state = asyncio.run(pipeline.resume_state(path))

    assert state["state"] == "already_indexed"
    assert state["doc_id"] == "d1"
    # the file was never hashed
    assert pipeline.sqlite.hashed == []


def test_a_file_that_grew_is_read_and_told_it_changed(tmp_path: Path):
    """The size is not the same any more - so the content decides."""
    path = tmp_path / "a.md"
    path.write_text("内容", encoding="utf-8")
    record = _record(path)
    pipeline = _pipeline(_Store(None), _Semantic({"d1"}))
    pipeline.sqlite = _Store(record)
    pipeline.sqlite.record["chunk_version"] = pipeline.chunker.version

    path.write_text("内容多了一点", encoding="utf-8")
    state = asyncio.run(pipeline.resume_state(path))

    # The hash was computed - the file had to be read.
    assert pipeline.sqlite.hashed, "the file was not hashed"
    # The content is not the one in the ledger any more: the indexed
    # version stays and is marked (AC-E-015).
    assert state["state"] in ("modified", "new")


def test_a_file_touched_after_it_was_read_is_read_again(tmp_path: Path):
    """Same size, but touched after the ledger read it: the file may
    have changed, so it has to be looked at."""
    path = tmp_path / "a.md"
    path.write_text("内容", encoding="utf-8")
    record = _record(path)
    # The ledger read it an hour ago; the file was touched since.
    record["indexed_at"] = (datetime.now() - timedelta(hours=1)).isoformat()
    pipeline = _pipeline(_Store(None), _Semantic({"d1"}))
    pipeline.sqlite = _Store(record)
    pipeline.sqlite.record["chunk_version"] = pipeline.chunker.version
    os.utime(path, None)

    state = asyncio.run(pipeline.resume_state(path))

    assert pipeline.sqlite.hashed, "the file was not hashed"
    # The content is the same one, so it is still the indexed file.
    assert state["state"] == "already_indexed"


def test_a_file_the_ledger_knows_nothing_about_is_hashed(tmp_path: Path):
    """No record - so the content has to be looked at."""
    path = tmp_path / "a.md"
    path.write_text("内容", encoding="utf-8")
    pipeline = _pipeline(_Store(None), _Semantic())
    pipeline.sqlite = _Store(None)

    state = asyncio.run(pipeline.resume_state(path))

    assert pipeline.sqlite.hashed, "the file was not hashed"
    assert state["state"] == "new"


def test_a_record_without_the_moment_falls_back_to_the_content(tmp_path: Path):
    """A ledger row that does not say when it was read - then the
    content decides, as before."""
    path = tmp_path / "a.md"
    path.write_text("内容", encoding="utf-8")
    record = _record(path)
    record.pop("indexed_at")
    pipeline = _pipeline(_Store(None), _Semantic({"d1"}))
    pipeline.sqlite = _Store(record)
    pipeline.sqlite.record["chunk_version"] = pipeline.chunker.version

    asyncio.run(pipeline.resume_state(path))

    assert pipeline.sqlite.hashed, "the file was not hashed"


def test_a_file_read_long_ago_is_recognised(tmp_path: Path):
    """The moment the file was read does not have to be recent - a file
    that has not been touched since is still the same file."""
    path = tmp_path / "a.md"
    path.write_text("内容", encoding="utf-8")
    record = _record(path)
    record["indexed_at"] = (
        datetime.now() + timedelta(days=30)
    ).isoformat()
    pipeline = _pipeline(_Store(None), _Semantic({"d1"}))
    pipeline.sqlite = _Store(record)
    pipeline.sqlite.record["chunk_version"] = pipeline.chunker.version

    state = asyncio.run(pipeline.resume_state(path))

    assert state["state"] == "already_indexed"
    assert pipeline.sqlite.hashed == []
