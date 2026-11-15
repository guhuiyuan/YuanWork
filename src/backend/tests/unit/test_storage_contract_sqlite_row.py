"""The relational store's connection settings and JSON columns.

* the three PRAGMA settings
* the eleven columns that are transparently deserialised
* what happens to a column whose JSON is broken
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yuanwork.db.sqlite import SQLiteStore


JSON_COLUMNS = (
    "meta", "topic_tags", "entities", "key_points", "related_files",
    "source_chunk_ids", "source_doc_ids", "tags", "context", "aliases",
    "properties",
)


@pytest.fixture
async def store(tmp_path: Path):
    s = SQLiteStore(tmp_path / "store.db")
    await s.initialize()
    yield s
    await s.close()


async def pragma(store: SQLiteStore, name: str):
    async with store.conn.execute(f"PRAGMA {name}") as cur:
        row = await cur.fetchone()
        return row[0]


# --------------------------------------------------------------------------
# the connection settings
# --------------------------------------------------------------------------


async def test_the_journal_mode_is_wal(store):
    assert (await pragma(store, "journal_mode")).lower() == "wal"


async def test_foreign_keys_are_on(store):
    assert await pragma(store, "foreign_keys") == 1


async def test_the_synchronous_level_is_normal(store):
    """NORMAL is 1; FULL would be 2."""
    assert await pragma(store, "synchronous") == 1


async def test_the_wal_files_exist_next_to_the_database(tmp_path: Path):
    s = SQLiteStore(tmp_path / "store.db")
    await s.initialize()
    await s.conn.execute(
        "INSERT INTO files (doc_id, file_name, file_path, file_hash, file_type, last_modified) "
        "VALUES ('d1', 'a.md', '/a.md', 'h', 'md', '2026-01-01')"
    )
    await s.conn.commit()
    assert (tmp_path / "store.db-wal").exists()
    await s.close()


# --------------------------------------------------------------------------
# the JSON columns
# --------------------------------------------------------------------------


async def test_a_json_column_is_deserialised(store):
    await store.conn.execute(
        "INSERT INTO files (doc_id, file_name, file_path, file_hash, file_type, "
        "last_modified, meta) VALUES ('d1', 'a.md', '/a.md', 'h', 'md', '2026-01-01', ?)",
        (json.dumps({"title": "标题", "pages": 3}),),
    )
    await store.conn.commit()

    row = await store.get_file("d1")
    assert row["meta"] == {"title": "标题", "pages": 3}
    assert isinstance(row["meta"], dict)


async def test_the_topic_tags_column_is_deserialised(store):
    await store.conn.execute(
        "INSERT INTO episodes (session_id, role, content, topic_tags, entities) "
        "VALUES ('s', 'user', 'x', ?, ?)",
        (json.dumps(["发票", "合同"]), json.dumps(["元工作"])),
    )
    await store.conn.commit()

    async with store.conn.execute("SELECT * FROM episodes") as cur:
        rows = await cur.fetchall()
    from yuanwork.db.sqlite import _row_to_dict

    row = _row_to_dict(rows[0])
    assert row["topic_tags"] == ["发票", "合同"]
    assert row["entities"] == ["元工作"]


async def test_a_broken_json_column_keeps_its_raw_text(store):
    """A column that does not parse stays a string instead of raising."""
    await store.conn.execute(
        "INSERT INTO files (doc_id, file_name, file_path, file_hash, file_type, "
        "last_modified, meta) VALUES ('d1', 'a.md', '/a.md', 'h', 'md', '2026-01-01', ?)",
        ("{not json",),
    )
    await store.conn.commit()

    row = await store.get_file("d1")
    assert row["meta"] == "{not json"
    assert isinstance(row["meta"], str)


async def test_the_json_column_list_is_the_documented_one():
    from yuanwork.db.sqlite import _row_to_dict
    import inspect

    source = inspect.getsource(_row_to_dict)
    for column in JSON_COLUMNS:
        assert f'"{column}"' in source


async def test_a_wiki_page_round_trips_its_json_columns(store):
    await store.upsert_wiki_page(
        page_id="p1",
        title="标题",
        markdown="# 正文",
        summary="摘要",
        source_chunk_ids=["c1", "c2"],
        source_doc_ids=["d1"],
        category="概念",
        tags=["a", "b"],
    )
    page = await store.get_wiki_page("p1")
    assert page["source_chunk_ids"] == ["c1", "c2"]
    assert page["source_doc_ids"] == ["d1"]
    assert page["tags"] == ["a", "b"]
