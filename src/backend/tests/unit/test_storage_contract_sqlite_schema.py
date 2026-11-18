"""The relational schema: tables and indexes.

The three stores are joined by identifiers, so the shape of the
relational side is part of the contract. This pins which tables exist,
which indexes exist, and which tables are never touched.
"""

from __future__ import annotations

import inspect
import json
import sqlite3
from pathlib import Path

import pytest

from yuanwork.db.sqlite import SCHEMA_V1, SQLiteStore


EXPECTED_TABLES = {
    "files",
    "episodes",
    "episodic_events",
    "episodic_prune_stats",
    "episode_summaries",
    "user_profile",
    "working_memory",
    "conflicts",
    "wiki_pages",
    "wiki_links",
    "entities",
    "entity_mentions",
    "entity_relations",
    "ingest_jobs",
    "workspace_entries",
    "page_update_candidates",
    "wiki_page_blocks",
    "doc_tiers",
    "page_conclusions",
}

EXPECTED_INDEXES = {
    "idx_files_file_hash",
    "idx_files_file_type",
    "idx_files_indexed_at",
    "idx_episodes_session",
    "idx_episodes_created",
    "idx_events_occurred",
    "idx_events_session",
    "idx_events_kind",
    "idx_events_topic",
    "idx_events_subject",
    "idx_prune_stats_at",
    "idx_summaries_session",
    "idx_conflicts_created",
    "idx_conflicts_ack",
    "idx_wiki_category",
    "idx_wiki_updated",
    "idx_links_target",
    "idx_links_source",
    "idx_entities_type",
    "idx_entities_name",
    "idx_entities_status",
    "idx_mentions_entity",
    "idx_mentions_page",
    "idx_relations_source",
    "idx_relations_target",
    "idx_workspace_added",
    "idx_page_updates_status",
    "idx_page_blocks_page",
    "idx_doc_tiers_tier",
    "idx_conclusions_page",
    "idx_conclusions_active",
}


@pytest.fixture
async def store(tmp_path: Path):
    s = SQLiteStore(tmp_path / "store.db")
    await s.initialize()
    yield s
    await s.close()


async def table_names(store: SQLiteStore) -> set[str]:
    async with store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ) as cur:
        return {row[0] for row in await cur.fetchall()}


async def index_names(store: SQLiteStore) -> set[str]:
    async with store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ) as cur:
        return {row[0] for row in await cur.fetchall()}


# --------------------------------------------------------------------------
# the inventory
# --------------------------------------------------------------------------


async def test_the_nineteen_tables_exist(store):
    names = await table_names(store)
    assert EXPECTED_TABLES <= names
    # sqlite adds its own bookkeeping tables
    assert len(EXPECTED_TABLES) == 19


async def test_the_thirty_one_indexes_exist(store):
    names = await index_names(store)
    assert EXPECTED_INDEXES <= names
    assert len(EXPECTED_INDEXES) == 31


async def test_the_schema_constant_matches_what_is_created(store):
    """The constant and the live database agree."""
    created = await table_names(store)
    declared = {
        line.split("(")[0].split()[-1]
        for line in SCHEMA_V1.splitlines()
        if line.strip().upper().startswith("CREATE TABLE")
    }
    assert declared == EXPECTED_TABLES


async def test_initializing_twice_is_harmless(tmp_path: Path):
    s = SQLiteStore(tmp_path / "store.db")
    await s.initialize()
    await s.initialize()
    names = await table_names(s)
    assert EXPECTED_TABLES <= names
    await s.close()


# --------------------------------------------------------------------------
# the tables that are never used
# --------------------------------------------------------------------------


async def test_two_tables_are_never_queried(store):
    """`episode_summaries` and `working_memory` exist but nothing in the
    codebase reads or writes them."""
    from yuanwork.db import sqlite as sqlite_module

    source = inspect.getsource(sqlite_module)
    for table in ("episode_summaries", "working_memory"):
        # the table is only *created* — no statement touches it
        statements = [
            line for line in source.splitlines()
            if table in line and not line.strip().upper().startswith("CREATE ")
        ]
        assert statements == [], f"{table} is used: {statements}"


async def test_the_ingest_job_table_is_used_by_the_ingest_route():
    """The jobs live in the database, so they survive a restart."""
    from yuanwork.api.routes import ingest

    source = inspect.getsource(ingest)
    assert "ingest_job" in source
    assert "_jobs" not in source


# --------------------------------------------------------------------------
# the columns
# --------------------------------------------------------------------------


async def column_names(store: SQLiteStore, table: str) -> list[str]:
    async with store.conn.execute(f"PRAGMA table_info({table})") as cur:
        return [row[1] for row in await cur.fetchall()]


async def test_the_files_table_columns(store):
    assert await column_names(store, "files") == [
        "doc_id", "file_name", "file_path", "file_hash", "file_type",
        "size_bytes", "chunk_count", "indexed_at", "last_modified",
        "chunk_version", "meta",
    ]


async def test_the_ingest_job_table_columns(store):
    """The job keeps the files it could not read, and how many.

    A count of failures says that something went wrong; the user needs
    to know *which* file and *why* (AC-E-004).
    """
    assert await column_names(store, "ingest_jobs") == [
        "job_id", "status", "progress", "files_done", "files_total", "error",
        "current_file", "failed_files", "failed_total", "unsupported_total",
        "created_at", "updated_at",
    ]


async def test_the_failed_files_survive_a_restart(store: SQLiteStore, tmp_path: Path):
    """They are kept in the database, so the app can still show them."""
    await store.upsert_ingest_job(
        job_id="j1",
        status="completed",
        failed_files='[{"path": "D:/kb/a.docx", "error": "解析失败"}]',
        failed_total=1,
    )

    other = SQLiteStore(store.db_path)
    await other.initialize()
    try:
        job = await other.get_ingest_job("j1")
    finally:
        await other.close()

    assert job is not None
    assert "a.docx" in job["failed_files"]
    assert job["failed_total"] == 1


async def test_the_episodes_table_columns(store):
    assert await column_names(store, "episodes") == [
        "id", "session_id", "role", "content", "topic_tags", "entities",
        "embedding", "importance_score", "created_at",
    ]


async def test_the_user_profile_table_columns(store):
    assert await column_names(store, "user_profile") == [
        "key", "value", "source_episode_id", "confidence", "updated_at",
        "expires_at",
    ]


async def test_the_wiki_pages_table_columns(store):
    assert await column_names(store, "wiki_pages") == [
        "id", "title", "markdown", "summary", "key_points", "source_chunk_ids",
        "source_doc_ids", "category", "tags", "user_edited", "page_type",
        "tier", "version", "created_at", "updated_at", "last_compiled_at",
    ]


async def test_the_role_column_is_constrained(store):
    """A role outside the three allowed values is rejected."""
    with pytest.raises(sqlite3.IntegrityError):
        await store.conn.execute(
            "INSERT INTO episodes (session_id, role, content) VALUES ('s', 'robot', 'x')"
        )


async def test_the_entity_type_column_is_constrained(store):
    with pytest.raises(sqlite3.IntegrityError):
        await store.conn.execute(
            "INSERT INTO entities (id, name, type) VALUES ('e1', 'x', 'alien')"
        )


async def test_the_link_source_type_column_is_constrained(store):
    with pytest.raises(sqlite3.IntegrityError):
        await store.conn.execute(
            "INSERT INTO wiki_links (source_id, source_type, target_id, target_type) "
            "VALUES ('a', 'planet', 'b', 'page')"
        )


async def test_the_files_table_has_a_primary_key_on_doc_id(store):
    """Writing the same doc_id twice is an update, not a second row."""
    await store.conn.execute(
        "INSERT INTO files (doc_id, file_name, file_path, file_hash, file_type, last_modified) "
        "VALUES ('d1', 'a.md', '/a.md', 'h1', 'md', '2026-01-01')"
    )
    await store.conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        await store.conn.execute(
            "INSERT INTO files (doc_id, file_name, file_path, file_hash, file_type, last_modified) "
            "VALUES ('d1', 'b.md', '/b.md', 'h2', 'md', '2026-01-01')"
        )
