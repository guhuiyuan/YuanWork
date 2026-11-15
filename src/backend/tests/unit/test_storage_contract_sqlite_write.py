"""The relational store's write semantics.

* an upsert is an update, not a second row
* the content-hash lookup is what de-duplication relies on
* every write commits on its own
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yuanwork.db.sqlite import SQLiteStore


@pytest.fixture
async def store(tmp_path: Path):
    s = SQLiteStore(tmp_path / "store.db")
    await s.initialize()
    yield s
    await s.close()


async def add_file(store, doc_id="d1", name="a.md", path="/kb/a.md",
                   file_hash="hash-1", file_type="md", size=10, chunks=3,
                   meta=None):
    await store.upsert_file(
        doc_id=doc_id,
        file_name=name,
        file_path=path,
        file_hash=file_hash,
        file_type=file_type,
        size_bytes=size,
        chunk_count=chunks,
        meta=meta,
    )


# --------------------------------------------------------------------------
# the upsert
# --------------------------------------------------------------------------


async def test_a_new_file_is_inserted(store):
    await add_file(store)
    row = await store.get_file("d1")
    assert row is not None
    assert row["file_name"] == "a.md"
    assert row["chunk_count"] == 3
    assert row["size_bytes"] == 10


async def test_writing_the_same_document_updates_it(store):
    await add_file(store, name="a.md", chunks=3)
    await add_file(store, name="renamed.md", chunks=9)

    assert await store.count_files() == 1
    row = await store.get_file("d1")
    assert row["file_name"] == "renamed.md"
    assert row["chunk_count"] == 9


async def test_the_meta_column_is_stored_as_json(store):
    await add_file(store, meta={"title": "标题"})
    row = await store.get_file("d1")
    assert row["meta"] == {"title": "标题"}


async def test_a_missing_file_reads_as_none(store):
    assert await store.get_file("nope") is None


# --------------------------------------------------------------------------
# the hash lookup
# --------------------------------------------------------------------------


async def test_a_file_can_be_found_by_its_content_hash(store):
    await add_file(store, file_hash="hash-1")
    found = await store.get_file_by_hash("hash-1")
    assert found is not None
    assert found["doc_id"] == "d1"


async def test_an_unknown_hash_reads_as_none(store):
    assert await store.get_file_by_hash("nope") is None


async def test_two_files_with_the_same_hash_are_both_findable(store):
    """The hash index is not unique — the de-duplication happens one
    layer up, in the pipeline."""
    await add_file(store, doc_id="d1", file_hash="same")
    await add_file(store, doc_id="d2", file_hash="same")
    assert await store.count_files() == 2
    assert await store.get_file_by_hash("same") is not None


# --------------------------------------------------------------------------
# the commit behaviour
# --------------------------------------------------------------------------


async def test_a_write_is_committed_by_the_method_itself(store):
    """The store commits inside every method, so a reader on another
    connection sees the row."""
    import aiosqlite

    await add_file(store)
    other = await aiosqlite.connect(str(store.db_path))
    try:
        async with other.execute("SELECT COUNT(*) FROM files") as cur:
            row = await cur.fetchone()
        assert row[0] == 1
    finally:
        await other.close()


async def test_a_delete_is_committed_by_the_method_itself(store):
    import aiosqlite

    await add_file(store)
    assert await store.delete_file("d1") is True

    other = await aiosqlite.connect(str(store.db_path))
    try:
        async with other.execute("SELECT COUNT(*) FROM files") as cur:
            row = await cur.fetchone()
        assert row[0] == 0
    finally:
        await other.close()


async def test_deleting_an_unknown_file_still_reports_success(store):
    """`delete_file` always returns True — the caller decides what to
    do with it."""
    assert await store.delete_file("nope") is True


# --------------------------------------------------------------------------
# the filters
# --------------------------------------------------------------------------


async def test_files_can_be_filtered_by_type(store):
    await add_file(store, doc_id="d1", file_type="md")
    await add_file(store, doc_id="d2", file_type="pdf")
    assert len(await store.list_files(file_type="md")) == 1
    assert len(await store.list_files(file_type="pdf")) == 1
    assert await store.count_files(file_type="md") == 1


async def test_files_can_be_searched_by_name_or_path(store):
    await add_file(store, doc_id="d1", name="发票.md", path="/kb/发票.md")
    await add_file(store, doc_id="d2", name="other.md", path="/kb/other.md")
    assert len(await store.list_files(search="发票")) == 1
    assert len(await store.list_files(search="/kb/")) == 2


async def test_the_search_uses_a_wildcard_pattern(store):
    """The pattern is not escaped, so `%` matches everything."""
    await add_file(store, doc_id="d1", name="a.md", path="/kb/a.md")
    await add_file(store, doc_id="d2", name="b.md", path="/kb/b.md")
    assert len(await store.list_files(search="%")) == 2


async def test_the_files_can_be_paged(store):
    for i in range(5):
        await add_file(store, doc_id=f"d{i}", name=f"{i}.md")
    assert len(await store.list_files(limit=2)) == 2
    assert len(await store.list_files(limit=2, offset=2)) == 2


async def test_the_files_can_be_counted_by_type(store):
    await add_file(store, doc_id="d1", file_type="md")
    await add_file(store, doc_id="d2", file_type="md")
    await add_file(store, doc_id="d3", file_type="pdf")
    assert await store.count_files_by_type() == {"md": 2, "pdf": 1}


# --------------------------------------------------------------------------
# the profile and the conflicts
# --------------------------------------------------------------------------


async def test_a_profile_row_is_overwritten_and_its_expiry_cleared(store):
    from datetime import datetime

    await store.upsert_profile("name", "张三", source_episode_id=None,
                               confidence=0.9, expires_at=datetime(2030, 1, 1))
    await store.upsert_profile("name", "李四", source_episode_id=None,
                               confidence=0.5, expires_at=None)
    row = await store.get_profile("name")
    assert row["value"] == "李四"
    assert row["confidence"] == 0.5
    assert row["expires_at"] is None


async def test_a_conflict_row_round_trips(store):
    await store.add_conflict(
        episode_id=None,
        knowledge_chunk_id="c1",
        conflict_type="llm_vs_kb",
        llm_answer="模型说 A",
        kb_evidence="知识库说 B",
        resolution="以知识库为准",
    )
    conflicts = await store.list_conflicts()
    assert len(conflicts) == 1
    assert conflicts[0]["llm_answer"] == "模型说 A"
    assert conflicts[0]["user_acknowledged"] in (0, False)


async def test_the_acknowledged_conflicts_can_be_excluded(store):
    await store.add_conflict(
        episode_id=None, knowledge_chunk_id="c1", conflict_type="llm_vs_kb",
        llm_answer="a", kb_evidence="b", resolution=None,
    )
    assert len(await store.list_conflicts(include_acknowledged=False)) == 1
    assert len(await store.list_conflicts(include_acknowledged=True)) == 1
