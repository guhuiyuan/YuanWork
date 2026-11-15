"""TASK-003 — identifier derivation and content-hash de-duplication.

The identifiers are the backbone of the whole system: the SQLite file
row, the LanceDB chunk row and the full-text index entry are all joined
by them, so they have to be *derivable*, never random.

* AC-F-005 — chunk id derivation
* AC-F-006 — de-duplication is by content hash, and it happens before
  the file is parsed
* AC-E-009 — the same content at two paths is indexed once, and only the
  first path is recorded
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from yuanwork.ingestion.chunker.structural_chunker import StructuralChunker
from yuanwork.ingestion.parser.base import RawBlock
from yuanwork.ingestion.parser.dispatcher import ParserDispatcher
from yuanwork.ingestion.pipeline import IngestionPipeline
from unit.ingestion_fixtures import (
    RecordingChunker,
    RecordingParser,
    RecordingSQLite,
    RecordingSemanticMemory,
    make_markdown_file,
    make_text_file,
)


# --------------------------------------------------------------------------
# AC-F-005 — chunk id derivation
# --------------------------------------------------------------------------


def test_chunk_id_is_derived_from_doc_block_index_and_text():
    chunker = StructuralChunker()
    block = RawBlock(
        block_id="doc-1_b1", doc_id="doc-1", content="一些正文内容", type="text", meta={}
    )
    chunks = chunker.chunk([block], "doc-1")

    assert len(chunks) == 1
    chunk = chunks[0]
    expected = hashlib.sha256(
        f"doc-1|doc-1_b1|1|一些正文内容".encode()
    ).hexdigest()[:16]
    assert chunk["id"] == expected
    assert len(chunk["id"]) == 16


def test_chunk_id_is_stable_across_runs():
    chunker = StructuralChunker()
    block = RawBlock(
        block_id="b1", doc_id="d1", content="同样的内容", type="text", meta={}
    )
    first = chunker.chunk([block], "d1")[0]["id"]
    second = StructuralChunker().chunk([block], "d1")[0]["id"]
    assert first == second


def test_chunk_id_changes_when_the_text_changes():
    chunker = StructuralChunker()
    a = chunker.chunk(
        [RawBlock(block_id="b1", doc_id="d1", content="AAA", type="text", meta={})], "d1"
    )[0]["id"]
    b = chunker.chunk(
        [RawBlock(block_id="b1", doc_id="d1", content="BBB", type="text", meta={})], "d1"
    )[0]["id"]
    assert a != b


def test_chunk_index_starts_at_one():
    chunker = StructuralChunker()
    chunks = chunker.chunk(
        [
            RawBlock(block_id="b1", doc_id="d1", content="第一段", type="text", meta={}),
            RawBlock(block_id="b2", doc_id="d1", content="第二段", type="text", meta={}),
        ],
        "d1",
    )
    assert [c["chunk_idx"] for c in chunks] == [1, 2]


# --------------------------------------------------------------------------
# AC-F-006 — de-duplication by content hash
# --------------------------------------------------------------------------


async def test_already_indexed_content_is_skipped_before_parsing(temp_dir: Path):
    events: list[str] = []
    store = RecordingSQLite(
        existing={"doc_id": "already-here"}, events=events
    )
    pipeline = IngestionPipeline(
        sqlite_store=store, semantic_memory=RecordingSemanticMemory(events=events)
    )
    parser = RecordingParser(events)
    pipeline.dispatcher = ParserDispatcher([parser])
    pipeline.chunker = RecordingChunker(events)

    path = make_markdown_file(temp_dir, "doc.md", "# 标题\n\n正文\n")
    result = await pipeline.ingest_file(path)

    assert result == {
        "status": "skipped",
        "reason": "already_indexed",
        "doc_id": "already-here",
    }
    # the file was never parsed and never embedded
    assert "parse" not in events
    assert "embed" not in events
    assert "upsert" not in events
    # the lookup used the *content* hash, not the path
    expected_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    assert store.hash_lookups == [expected_hash]


async def test_force_reingests_even_when_the_hash_matches(temp_dir: Path):
    events: list[str] = []
    store = RecordingSQLite(
        existing={"doc_id": "already-here"}, events=events
    )
    pipeline = IngestionPipeline(
        sqlite_store=store, semantic_memory=RecordingSemanticMemory(events=events)
    )
    pipeline.dispatcher = ParserDispatcher([RecordingParser(events)])
    pipeline.chunker = RecordingChunker(events)

    path = make_markdown_file(temp_dir, "doc.md", "# 标题\n\n正文\n")
    result = await pipeline.ingest_file(path, force=True)

    assert result["status"] == "indexed"
    assert store.hash_lookups == []  # the lookup is skipped entirely
    assert len(store.upserts) == 1


# --------------------------------------------------------------------------
# AC-E-009 — same content, two paths
# --------------------------------------------------------------------------


async def test_same_content_at_two_paths_is_indexed_once(temp_dir: Path):
    events: list[str] = []
    store = RecordingSQLite(events=events)
    pipeline = IngestionPipeline(
        sqlite_store=store, semantic_memory=RecordingSemanticMemory(events=events)
    )
    pipeline.dispatcher = ParserDispatcher([RecordingParser(events)])
    pipeline.chunker = RecordingChunker(events)

    content = "# 标题\n\n完全一样的内容\n"
    first = make_markdown_file(temp_dir / "a", "doc.md", content)
    second = make_markdown_file(temp_dir / "b", "doc.md", content)

    first_result = await pipeline.ingest_file(first)
    assert first_result["status"] == "indexed"

    # the store now holds the first path's hash
    store.existing = {"doc_id": first_result["doc_id"]}
    second_result = await pipeline.ingest_file(second)

    assert second_result["status"] == "skipped"
    assert second_result["reason"] == "already_indexed"
    # only the first path is recorded
    assert len(store.upserts) == 1
    assert store.upserts[0]["file_path"] == str(first)


async def test_the_same_file_ingested_twice_is_idempotent(temp_dir: Path):
    events: list[str] = []
    store = RecordingSQLite(events=events)
    pipeline = IngestionPipeline(
        sqlite_store=store, semantic_memory=RecordingSemanticMemory(events=events)
    )
    pipeline.dispatcher = ParserDispatcher([RecordingParser(events)])
    pipeline.chunker = RecordingChunker(events)

    path = make_markdown_file(temp_dir, "doc.md", "# 标题\n\n正文\n")
    first = await pipeline.ingest_file(path)
    # the store now knows this content
    store.existing = {"doc_id": first["doc_id"]}
    second = await pipeline.ingest_file(path)

    assert second["status"] == "skipped"
    assert second["doc_id"] == first["doc_id"]
    # the second run found the hash and did not write again
    assert len(store.upserts) == 1


async def test_different_content_at_two_paths_gets_two_records(temp_dir: Path):
    events: list[str] = []
    store = RecordingSQLite(events=events)
    pipeline = IngestionPipeline(
        sqlite_store=store, semantic_memory=RecordingSemanticMemory(events=events)
    )
    pipeline.dispatcher = ParserDispatcher([RecordingParser(events)])
    pipeline.chunker = RecordingChunker(events)

    a = make_text_file(temp_dir / "a", "doc.md", "第一份内容")
    b = make_text_file(temp_dir / "b", "doc.md", "第二份内容")

    first = await pipeline.ingest_file(a)
    store.existing = None
    second = await pipeline.ingest_file(b)

    assert first["doc_id"] != second["doc_id"]
    assert len(store.upserts) == 2
