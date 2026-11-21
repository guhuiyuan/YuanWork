"""SQLite database store for relational data (files, profiles, episodes, conflicts, wiki).

Uses native sqlite3 + aiosqlite for async operations.
WAL mode enabled for better concurrency.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

#: "The caller did not mention this field" - different from "set it to
#: None", which clears it.
_UNSET = object()


# ===== Schema definitions =====

SCHEMA_V1 = """
-- Document file metadata
CREATE TABLE IF NOT EXISTS files (
    doc_id TEXT PRIMARY KEY,
    file_name TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    file_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    indexed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_modified TIMESTAMP NOT NULL,
    -- Which rules the file was cut with. When the rules change, what
    -- was cut with the old ones is stale - and a build has to cut it
    -- again instead of skipping it (ADR-046).
    chunk_version TEXT NOT NULL DEFAULT '',
    meta TEXT DEFAULT '{}'  -- JSON
);

CREATE INDEX IF NOT EXISTS idx_files_file_hash ON files(file_hash);
CREATE INDEX IF NOT EXISTS idx_files_file_type ON files(file_type);
CREATE INDEX IF NOT EXISTS idx_files_indexed_at ON files(indexed_at DESC);
-- The build looks a file up by its path for every file of the work
-- domain - without this index each lookup is a scan of the whole
-- table (TASK-089).
CREATE INDEX IF NOT EXISTS idx_files_file_path ON files(file_path COLLATE NOCASE);

-- Episodic memory (conversation history)
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content TEXT NOT NULL,
    topic_tags TEXT DEFAULT '[]',  -- JSON array
    entities TEXT DEFAULT '[]',    -- JSON array
    embedding BLOB,                -- 1024-dim float32 vector
    importance_score REAL DEFAULT 0.5,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_episodes_session ON episodes(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_episodes_created ON episodes(created_at DESC);

-- Episodic events: what happened, not what was said.
--
-- The `episodes` table keeps the dialogue; this table keeps the
-- interactions - asking, citing, accepting, rejecting, adding and
-- removing files. The events are what the consolidation layer reads:
-- an interaction says what the user did with the knowledge base, which
-- is a different fact from what was written down (ADR-013).
CREATE TABLE IF NOT EXISTS episodic_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    session_id TEXT NOT NULL,
    -- ask / cite / accept / reject / add_file / remove_file
    kind TEXT NOT NULL CHECK (kind IN (
        'ask', 'cite', 'accept', 'reject', 'add_file', 'remove_file'
    )),
    subject TEXT,
    detail TEXT,
    topic TEXT,
    importance REAL DEFAULT 0.5
);

CREATE INDEX IF NOT EXISTS idx_events_occurred ON episodic_events(occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_session ON episodic_events(session_id);
CREATE INDEX IF NOT EXISTS idx_events_kind ON episodic_events(kind);
CREATE INDEX IF NOT EXISTS idx_events_topic ON episodic_events(topic);
CREATE INDEX IF NOT EXISTS idx_events_subject ON episodic_events(subject);

-- How many events the cleanup threw away (ADR-038).
--
-- Only the number is kept. The events that were evicted are gone - a
-- copy of them would undo the point of evicting them.
CREATE TABLE IF NOT EXISTS episodic_prune_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pruned_at TEXT NOT NULL,
    pruned_count INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_prune_stats_at ON episodic_prune_stats(pruned_at DESC);

-- Episode summaries (consolidated)
CREATE TABLE IF NOT EXISTS episode_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    key_points TEXT DEFAULT '[]',
    related_files TEXT DEFAULT '[]',
    importance_score REAL DEFAULT 0.5,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_summaries_session ON episode_summaries(session_id);

-- User profile (facts and preferences)
CREATE TABLE IF NOT EXISTS user_profile (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    source_episode_id INTEGER,
    confidence REAL DEFAULT 1.0,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP,
    FOREIGN KEY (source_episode_id) REFERENCES episodes(id) ON DELETE SET NULL
);

-- Working memory (active project contexts)
CREATE TABLE IF NOT EXISTS working_memory (
    project_key TEXT PRIMARY KEY,
    context TEXT DEFAULT '{}',  -- JSON
    last_touched TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Conflict log (LLM vs KB)
CREATE TABLE IF NOT EXISTS conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER,
    knowledge_chunk_id TEXT,
    conflict_type TEXT NOT NULL,
    llm_answer TEXT NOT NULL,
    kb_evidence TEXT NOT NULL,
    topic TEXT,
    citation TEXT,
    resolution TEXT,
    user_acknowledged BOOLEAN DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (episode_id) REFERENCES episodes(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_conflicts_created ON conflicts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_conflicts_ack ON conflicts(user_acknowledged);

-- Wiki pages
CREATE TABLE IF NOT EXISTS wiki_pages (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    markdown TEXT NOT NULL,
    summary TEXT NOT NULL,
    -- The key points of the page - one of the four parts a compiled
    -- page has (title, summary, key points, sources).
    key_points TEXT DEFAULT '[]',        -- JSON
    source_chunk_ids TEXT DEFAULT '[]',  -- JSON
    source_doc_ids TEXT DEFAULT '[]',    -- JSON
    category TEXT NOT NULL DEFAULT 'general',
    tags TEXT DEFAULT '[]',              -- JSON
    -- The user has edited this page - it is not overwritten by an
    -- automatic recompile (AC-F-013).
    user_edited INTEGER NOT NULL DEFAULT 0,
    -- normal / entity / concept / comparison
    page_type TEXT NOT NULL DEFAULT 'normal',
    -- core / longtail - which layer the document came from (ADR-031).
    tier TEXT NOT NULL DEFAULT 'longtail',
    version INTEGER DEFAULT 1,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_compiled_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_wiki_category ON wiki_pages(category);
CREATE INDEX IF NOT EXISTS idx_wiki_updated ON wiki_pages(updated_at DESC);

-- The blocks of a page: one paragraph each, with an owner. A block the
-- user wrote is theirs - a later automatic recompile replaces the
-- model's blocks and leaves these alone (AC-F-027).
CREATE TABLE IF NOT EXISTS wiki_page_blocks (
    id TEXT PRIMARY KEY,
    page_id TEXT NOT NULL,
    order_index INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL,
    -- model = the model wrote it ; user = the user wrote it
    owner TEXT NOT NULL DEFAULT 'model' CHECK (owner IN ('model', 'user')),
    conclusion_id TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_page_blocks_page ON wiki_page_blocks(page_id);

-- The conclusions of a page, and where each one comes from (ADR-032).
-- A conclusion without an anchor is not written here at all: a claim
-- that cannot be pinned to a place in the original cannot be checked,
-- and so it cannot be used as evidence either.
CREATE TABLE IF NOT EXISTS page_conclusions (
    id TEXT PRIMARY KEY,
    page_id TEXT NOT NULL,
    text TEXT NOT NULL,
    -- The position in the original document (JSON anchor).
    anchor TEXT NOT NULL DEFAULT '{}',
    source_doc_id TEXT NOT NULL DEFAULT '',
    -- The hash of the source content, so a conclusion knows which
    -- version of the document it was read from.
    source_hash TEXT NOT NULL DEFAULT '',
    -- Which segment of the whole document it came from.
    segment_index INTEGER NOT NULL DEFAULT 0,
    -- The group of conclusions that contradict each other. Empty means
    -- there is no contradiction; inside a group the conclusions are
    -- each other's counterpart.
    conflict_group TEXT NOT NULL DEFAULT '',
    -- Whether this is the version in effect. Inside a conflict group at
    -- most one conclusion is active.
    active INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_conclusions_page ON page_conclusions(page_id);
CREATE INDEX IF NOT EXISTS idx_conclusions_active ON page_conclusions(active);

-- Wiki bidirectional links
CREATE TABLE IF NOT EXISTS wiki_links (
    source_id TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type IN ('page', 'chunk', 'doc', 'episode')),
    target_id TEXT NOT NULL,
    target_type TEXT NOT NULL CHECK (target_type IN ('page', 'chunk', 'doc', 'episode')),
    link_type TEXT NOT NULL DEFAULT 'related',
    weight REAL DEFAULT 1.0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source_id, source_type, target_id, target_type, link_type)
);

CREATE INDEX IF NOT EXISTS idx_links_target ON wiki_links(target_id, target_type);
CREATE INDEX IF NOT EXISTS idx_links_source ON wiki_links(source_id, source_type);

-- Entities (NER-extracted concepts, people, projects)
CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('person', 'project', 'concept', 'contract', 'law', 'org', 'location', 'other')),
    aliases TEXT DEFAULT '[]',
    properties TEXT DEFAULT '{}',
    mention_count INTEGER DEFAULT 0,
    -- The episodic events the entity was induced from, so a conclusion
    -- can be traced back to what happened.
    source_event_ids TEXT DEFAULT '[]',
    -- active = in effect ; pending = waiting for the user to confirm
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'pending')),
    first_seen TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type);
CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);
-- The status index is created by the migration: a database from before
-- the column existed would fail to create it here.

-- Entity relations: what one entity is to another.
CREATE TABLE IF NOT EXISTS entity_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    source_event_ids TEXT DEFAULT '[]',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (source_id, target_id, relation_type),
    FOREIGN KEY (source_id) REFERENCES entities(id) ON DELETE CASCADE,
    FOREIGN KEY (target_id) REFERENCES entities(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_relations_source ON entity_relations(source_id);
CREATE INDEX IF NOT EXISTS idx_relations_target ON entity_relations(target_id);

-- Entity mentions (which chunks/pages reference which entities)
CREATE TABLE IF NOT EXISTS entity_mentions (
    entity_id TEXT NOT NULL,
    page_id TEXT,
    chunk_id TEXT,
    doc_id TEXT,
    context TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (entity_id, page_id, chunk_id),
    FOREIGN KEY (entity_id) REFERENCES entities(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_mentions_entity ON entity_mentions(entity_id);
CREATE INDEX IF NOT EXISTS idx_mentions_page ON entity_mentions(page_id);

-- The key points the consolidation induced for a wiki page. They are
-- candidates: the page itself is compiled from documents and edited by
-- the user, so an induced point is a suggestion waiting to be applied
-- (design §3.1 - the consolidation writes entities, relations and pages
-- into the semantic memory).
CREATE TABLE IF NOT EXISTS page_update_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id TEXT NOT NULL DEFAULT '',
    topic TEXT NOT NULL DEFAULT '',
    point TEXT NOT NULL,
    source_event_ids TEXT DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'applied', 'rejected')),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (page_id, point)
);

CREATE INDEX IF NOT EXISTS idx_page_updates_status
    ON page_update_candidates(status);

-- Ingestion job tracking (persistent, optional)
CREATE TABLE IF NOT EXISTS ingest_jobs (
    job_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    progress REAL DEFAULT 0.0,
    files_done INTEGER DEFAULT 0,
    files_total INTEGER DEFAULT 0,
    error TEXT,
    current_file TEXT,
    -- The files the build could not read, as JSON: a list of
    -- {"path": ..., "error": ...}. A count says that something went
    -- wrong; the user needs to know *which* file and *why* (AC-E-004).
    failed_files TEXT DEFAULT '[]',
    -- How many files there were in all. The list above is capped - a
    -- workspace can hold a hundred thousand files nobody can read, and
    -- the number has to stay true even when the list does not.
    failed_total INTEGER DEFAULT 0,
    -- How many of them are a format nobody supports - a different
    -- problem from "could not be read", and the user should not have
    -- to tell them apart.
    unsupported_total INTEGER DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Workspace entries: the folders and single files the user picked
CREATE TABLE IF NOT EXISTS workspace_entries (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('folder', 'file')),
    included INTEGER NOT NULL DEFAULT 1,
    added_at TIMESTAMP NOT NULL,
    doc_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_workspace_added ON workspace_entries(added_at);

-- Which layer a document belongs to (ADR-031): the core layer holds the
-- documents that other documents are read against - contracts, rules,
-- the company's own policies. The long tail holds everything else.
CREATE TABLE IF NOT EXISTS doc_tiers (
    doc_id TEXT PRIMARY KEY,
    -- core = the documents with conclusions of their own ; longtail = the rest
    tier TEXT NOT NULL CHECK (tier IN ('core', 'longtail')),
    -- rule = the rules decided it ; model = the model did ; user = the user did
    decided_by TEXT NOT NULL CHECK (decided_by IN ('rule', 'model', 'user')),
    decided_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source_hash TEXT DEFAULT '',
    -- compiled / pending_recompile / failed / not_ready
    compile_status TEXT NOT NULL DEFAULT 'not_ready'
        CHECK (compile_status IN ('compiled', 'pending_recompile', 'failed', 'not_ready')),
    -- The source file changed since the page was compiled.
    stale INTEGER NOT NULL DEFAULT 0,
    -- How many segments the compiler read.
    segment_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_doc_tiers_tier ON doc_tiers(tier);
"""


# ===== Helpers =====


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Convert sqlite3.Row to dict, parsing JSON columns."""
    d = dict(row)
    for key in ("meta", "topic_tags", "entities", "key_points", "related_files",
                "source_chunk_ids", "source_doc_ids", "tags", "context", "aliases",
                "properties", "source_event_ids", "anchor"):
        if key in d and isinstance(d[key], str):
            try:
                d[key] = json.loads(d[key])
            except (json.JSONDecodeError, TypeError):
                pass
    # A flag is a flag, not a number.
    if "active" in d:
        d["active"] = bool(d["active"])
    return d


def _workspace_row(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a workspace entry row, turning the flag back into a bool."""
    d = _row_to_dict(row)
    if "included" in d:
        d["included"] = bool(d["included"])
    return d


# ===== Main store class =====


class SQLiteStore:
    """Async SQLite store for YuanWork."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """Initialize database connection and schema."""
        self._conn = await aiosqlite.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.executescript(SCHEMA_V1)
        await self._conn.commit()
        await self._migrate_ingest_jobs_table()
        await self._migrate_conflicts_table()
        await self._migrate_entities_table()
        await self._migrate_wiki_pages_table()
        await self._migrate_files_table()
        logger.info(f"SQLite initialized at {self.db_path}")

    async def _migrate_files_table(self) -> None:
        """Add the columns an older database does not have yet.

        The chunk version is the important one: a file that was cut with
        the old rules has to be cut again, not skipped (ADR-046).
        """
        try:
            async with self._conn.execute("PRAGMA table_info(files)") as cursor:
                rows = await cursor.fetchall()
        except Exception:  # noqa: BLE001 - a missing table is not a failure
            return

        existing = {row[1] for row in rows}
        if "chunk_version" not in existing:
            await self._conn.execute(
                "ALTER TABLE files ADD COLUMN chunk_version TEXT NOT NULL DEFAULT ''"
            )
            await self._conn.commit()

    async def _migrate_wiki_pages_table(self) -> None:
        """Add the columns an older database does not have yet.

        A page from before the split has no key points of its own - the
        points are still in its body, but the page cannot answer "what
        are the key points" without them.
        """
        try:
            async with self._conn.execute(
                "PRAGMA table_info(wiki_pages)"
            ) as cursor:
                rows = await cursor.fetchall()
        except Exception:  # noqa: BLE001 - a missing table is not a failure
            return

        existing = {row[1] for row in rows}
        if "key_points" not in existing:
            await self._conn.execute(
                "ALTER TABLE wiki_pages ADD COLUMN key_points TEXT DEFAULT '[]'"
            )
            await self._conn.commit()
        if "user_edited" not in existing:
            await self._conn.execute(
                "ALTER TABLE wiki_pages ADD COLUMN user_edited INTEGER NOT NULL DEFAULT 0"
            )
            await self._conn.commit()
        if "page_type" not in existing:
            await self._conn.execute(
                "ALTER TABLE wiki_pages ADD COLUMN page_type TEXT NOT NULL DEFAULT 'normal'"
            )
            await self._conn.commit()
        if "tier" not in existing:
            await self._conn.execute(
                "ALTER TABLE wiki_pages ADD COLUMN tier TEXT NOT NULL DEFAULT 'longtail'"
            )
            await self._conn.commit()

    async def _migrate_entities_table(self) -> None:
        """Add the columns an older database does not have yet.

        An entity that cannot say where it came from cannot be traced
        back, and one whose status is unknown cannot be told apart from
        one that is waiting for the user.
        """
        try:
            async with self._conn.execute(
                "PRAGMA table_info(entities)"
            ) as cursor:
                rows = await cursor.fetchall()
        except Exception:  # noqa: BLE001 - a missing table is not a failure
            return

        existing = {row[1] for row in rows}
        added = False
        for column, ddl in (
            (
                "source_event_ids",
                "ALTER TABLE entities ADD COLUMN source_event_ids TEXT DEFAULT '[]'",
            ),
            (
                "status",
                "ALTER TABLE entities ADD COLUMN status TEXT NOT NULL DEFAULT 'active'",
            ),
        ):
            if column not in existing:
                await self._conn.execute(ddl)
                added = True
        if added:
            await self._conn.commit()
        # The index goes with the column.
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_entities_status ON entities(status)"
        )
        await self._conn.commit()

    async def _migrate_conflicts_table(self) -> None:
        """Add the columns an older database does not have yet.

        The conflict record carries the divergence subject and where it
        came from - a record without them says that something differed
        but not what.
        """
        try:
            async with self._conn.execute(
                "PRAGMA table_info(conflicts)"
            ) as cursor:
                rows = await cursor.fetchall()
        except Exception:  # noqa: BLE001 - a missing table is not a failure
            return

        existing = {row[1] for row in rows}
        added = False
        for column, ddl in (
            ("topic", "ALTER TABLE conflicts ADD COLUMN topic TEXT"),
            ("citation", "ALTER TABLE conflicts ADD COLUMN citation TEXT"),
        ):
            if column not in existing:
                await self._conn.execute(ddl)
                added = True
        if added:
            await self._conn.commit()

    async def _migrate_ingest_jobs_table(self) -> None:
        """Add the columns an older database does not have yet.

        `CREATE TABLE IF NOT EXISTS` leaves an existing table alone, so a
        database created before a column was added keeps the old shape.
        The column is added, never filled in: the rows that are already
        there had no value for it anyway.
        """
        try:
            async with self._conn.execute(
                "PRAGMA table_info(ingest_jobs)"
            ) as cursor:
                rows = await cursor.fetchall()
        except Exception:  # noqa: BLE001 - a missing table is not a failure
            return

        existing = {row[1] for row in rows}
        added = False
        for column, ddl in (
            ("current_file", "ALTER TABLE ingest_jobs ADD COLUMN current_file TEXT"),
            (
                "failed_files",
                "ALTER TABLE ingest_jobs ADD COLUMN failed_files TEXT DEFAULT '[]'",
            ),
            (
                "failed_total",
                "ALTER TABLE ingest_jobs ADD COLUMN failed_total INTEGER DEFAULT 0",
            ),
            (
                "unsupported_total",
                "ALTER TABLE ingest_jobs ADD COLUMN unsupported_total INTEGER DEFAULT 0",
            ),
        ):
            if column not in existing:
                await self._conn.execute(ddl)
                added = True
        if added:
            await self._conn.commit()
            logger.info("Migrated ingest_jobs table")

    async def close(self) -> None:
        """Close database connection."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        """Get the connection, raising if not initialized."""
        if self._conn is None:
            raise RuntimeError("SQLiteStore not initialized. Call initialize() first.")
        return self._conn

    # ===== Files =====

    async def upsert_file(
        self,
        doc_id: str,
        file_name: str,
        file_path: str,
        file_hash: str,
        file_type: str,
        size_bytes: int,
        chunk_count: int = 0,
        chunk_version: str = "",
        meta: dict | None = None,
    ) -> None:
        """Insert or update a file record."""
        now = datetime.now().isoformat()
        await self.conn.execute(
            """
            INSERT INTO files (doc_id, file_name, file_path, file_hash, file_type,
                               size_bytes, chunk_count, indexed_at, last_modified,
                               chunk_version, meta)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(doc_id) DO UPDATE SET
                file_name = excluded.file_name,
                file_path = excluded.file_path,
                file_hash = excluded.file_hash,
                file_type = excluded.file_type,
                size_bytes = excluded.size_bytes,
                chunk_count = excluded.chunk_count,
                indexed_at = excluded.indexed_at,
                last_modified = excluded.last_modified,
                chunk_version = excluded.chunk_version,
                meta = excluded.meta
            """,
            (doc_id, file_name, file_path, file_hash, file_type, size_bytes,
             chunk_count, now, now, chunk_version, json.dumps(meta or {})),
        )
        await self.conn.commit()

    async def get_file(self, doc_id: str) -> dict | None:
        """Get a file by doc_id."""
        async with self.conn.execute(
            "SELECT * FROM files WHERE doc_id = ?", (doc_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_dict(row) if row else None

    async def get_file_by_hash(self, file_hash: str) -> dict | None:
        """Get a file by content hash."""
        async with self.conn.execute(
            "SELECT * FROM files WHERE file_hash = ? LIMIT 1", (file_hash,)
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_dict(row) if row else None

    async def get_file_by_path(self, file_path: str) -> dict | None:
        """Get the file indexed at a path.

        The comparison is case-insensitive: the same folder can be typed as
        ``D:\\docs\\a.md`` or ``d:/docs/a.md`` and both mean the same file.
        """
        async with self.conn.execute(
            "SELECT * FROM files WHERE file_path = ? COLLATE NOCASE "
            "ORDER BY indexed_at DESC LIMIT 1",
            (file_path,),
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_dict(row) if row else None

    async def list_files_under(self, dir_path: str) -> list[dict]:
        """The indexed files inside a directory."""
        prefix = os.path.normcase(dir_path.rstrip("\\/")) + os.sep
        pattern = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        async with self.conn.execute(
            "SELECT * FROM files WHERE file_path LIKE ? ESCAPE '\\' "
            "COLLATE NOCASE ORDER BY file_path",
            (pattern + "%",),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(r) for r in rows]

    async def list_files(
        self,
        limit: int = 100,
        offset: int = 0,
        file_type: str | None = None,
        search: str | None = None,
    ) -> list[dict]:
        """List files with optional filters."""
        sql = "SELECT * FROM files WHERE 1=1"
        params: list = []

        if file_type:
            sql += " AND file_type = ?"
            params.append(file_type)

        if search:
            sql += " AND (file_name LIKE ? OR file_path LIKE ?)"
            params.extend([f"%{search}%", f"%{search}%"])

        sql += " ORDER BY indexed_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        async with self.conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(r) for r in rows]

    async def find_files_by_name(self, name: str, limit: int = 20) -> list[dict]:
        """The files whose name is this one, whatever the spacing.

        The reader types the file name the way they remember it; the
        index has it the way the file system spelled it - one space
        here and there is enough to lose the file. A question that
        names a file has to find it (TASK-085).
        """
        wanted = re.sub(r"\s+", "", name or "").strip().casefold()
        if not wanted:
            return []
        async with self.conn.execute(
            "SELECT * FROM files WHERE REPLACE(LOWER(file_name), ' ', '') = ? "
            "OR REPLACE(LOWER(file_name), ' ', '') LIKE ? "
            "ORDER BY indexed_at DESC LIMIT ?",
            (wanted, f"%{wanted}%", limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(r) for r in rows]

    async def count_files(
        self, file_type: str | None = None, search: str | None = None
    ) -> int:
        """Count files matching filters."""
        sql = "SELECT COUNT(*) FROM files WHERE 1=1"
        params: list = []
        if file_type:
            sql += " AND file_type = ?"
            params.append(file_type)
        if search:
            sql += " AND (file_name LIKE ? OR file_path LIKE ?)"
            params.extend([f"%{search}%", f"%{search}%"])

        async with self.conn.execute(sql, params) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def count_files_by_type(self) -> dict[str, int]:
        """Get file count grouped by type."""
        async with self.conn.execute(
            "SELECT file_type, COUNT(*) FROM files GROUP BY file_type"
        ) as cursor:
            rows = await cursor.fetchall()
            return {r[0]: r[1] for r in rows}

    async def delete_file(self, doc_id: str) -> bool:
        """Delete a file record."""
        await self.conn.execute("DELETE FROM files WHERE doc_id = ?", (doc_id,))
        await self.conn.commit()
        return True

    async def count_chunks(self) -> int:
        """Count total chunks across all files."""
        async with self.conn.execute(
            "SELECT COALESCE(SUM(chunk_count), 0) FROM files"
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

    # ===== Workspace =====

    async def add_workspace_entry(
        self,
        path: str,
        kind: str,
        included: bool = True,
        doc_id: str | None = None,
    ) -> dict:
        """Register a workspace entry.

        The same path is only registered once: adding it again returns the
        entry that is already there instead of a second copy.
        """
        existing = await self.get_workspace_entry_by_path(path)
        if existing is not None:
            return existing

        entry_id = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
        added_at = datetime.now().isoformat()
        await self.conn.execute(
            """
            INSERT INTO workspace_entries (id, path, kind, included, added_at, doc_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (entry_id, path, kind, 1 if included else 0, added_at, doc_id),
        )
        await self.conn.commit()
        return {
            "id": entry_id,
            "path": path,
            "kind": kind,
            "included": included,
            "added_at": added_at,
            "doc_id": doc_id,
        }

    async def get_workspace_entry(self, entry_id: str) -> dict | None:
        """Get a workspace entry by its id."""
        async with self.conn.execute(
            "SELECT * FROM workspace_entries WHERE id = ?", (entry_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return _workspace_row(row) if row else None

    async def get_workspace_entry_by_path(self, path: str) -> dict | None:
        """Get a workspace entry by its path."""
        async with self.conn.execute(
            "SELECT * FROM workspace_entries WHERE path = ? COLLATE NOCASE", (path,)
        ) as cursor:
            row = await cursor.fetchone()
            return _workspace_row(row) if row else None

    async def list_workspace_entries(self, included_only: bool = False) -> list[dict]:
        """The workspace entries, oldest first."""
        sql = "SELECT * FROM workspace_entries"
        if included_only:
            sql += " WHERE included = 1"
        sql += " ORDER BY added_at ASC, path ASC"
        async with self.conn.execute(sql) as cursor:
            rows = await cursor.fetchall()
            return [_workspace_row(r) for r in rows]

    async def remove_workspace_entry(self, entry_id: str) -> bool:
        """Remove a workspace entry. Returns whether there was one."""
        cursor = await self.conn.execute(
            "DELETE FROM workspace_entries WHERE id = ?", (entry_id,)
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def set_workspace_entry_included(self, entry_id: str, included: bool) -> bool:
        """Turn indexing on or off for a workspace entry."""
        cursor = await self.conn.execute(
            "UPDATE workspace_entries SET included = ? WHERE id = ?",
            (1 if included else 0, entry_id),
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def set_workspace_entry_doc_id(self, entry_id: str, doc_id: str) -> bool:
        """Record the document id a file entry was indexed under."""
        cursor = await self.conn.execute(
            "UPDATE workspace_entries SET doc_id = ? WHERE id = ?", (doc_id, entry_id)
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    # ===== Episodes =====

    async def add_episode(
        self,
        session_id: str,
        role: str,
        content: str,
        topic_tags: list[str] | None = None,
        entities: list[str] | None = None,
        embedding: bytes | None = None,
        importance_score: float = 0.5,
    ) -> int:
        """Add an episodic memory entry."""
        async with self.conn.execute(
            """
            INSERT INTO episodes (session_id, role, content, topic_tags, entities,
                                  embedding, importance_score)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, role, content,
             json.dumps(topic_tags or []), json.dumps(entities or []),
             embedding, importance_score),
        ) as cursor:
            episode_id = cursor.lastrowid
        await self.conn.commit()
        return episode_id or 0

    async def list_recent_episodes(self, limit: int = 50) -> list[dict]:
        """List recent episodes."""
        async with self.conn.execute(
            "SELECT * FROM episodes ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(r) for r in rows]

    # ----- the episodic events -----

    async def add_episodic_event(
        self,
        occurred_at: str,
        session_id: str,
        kind: str,
        subject: str | None = None,
        detail: str | None = None,
        topic: str | None = None,
        importance: float = 0.5,
    ) -> int:
        """Record one interaction event.

        The six kinds are the interactions between the user and the
        knowledge base - asking, citing, accepting, rejecting, adding
        and removing files. Nothing from outside the application is
        recorded (ADR-013).
        """
        async with self.conn.execute(
            """
            INSERT INTO episodic_events (occurred_at, session_id, kind,
                                         subject, detail, topic, importance)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (occurred_at, session_id, kind, subject, detail, topic, importance),
        ) as cursor:
            event_id = cursor.lastrowid
        await self.conn.commit()
        return event_id or 0

    async def list_episodic_events(
        self,
        session_id: str | None = None,
        kind: str | None = None,
        topic: str | None = None,
        subject: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """The events, filtered by time, topic and subject.

        Everything is optional: no filter means "all of them", newest
        first.
        """
        clauses: list[str] = []
        params: list[Any] = []

        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if topic is not None:
            clauses.append("topic = ?")
            params.append(topic)
        if subject is not None:
            clauses.append("subject = ?")
            params.append(subject)
        if since is not None:
            clauses.append("occurred_at >= ?")
            params.append(since)
        if until is not None:
            clauses.append("occurred_at <= ?")
            params.append(until)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)

        async with self.conn.execute(
            f"SELECT * FROM episodic_events {where} "
            "ORDER BY occurred_at DESC, id DESC LIMIT ?",
            tuple(params),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(r) for r in rows]

    async def count_episodic_events(self) -> int:
        """How many events are stored."""
        async with self.conn.execute(
            "SELECT COUNT(*) FROM episodic_events"
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def oldest_episodic_event(self) -> dict | None:
        """The oldest event that is still stored."""
        async with self.conn.execute(
            "SELECT * FROM episodic_events "
            "ORDER BY datetime(occurred_at) ASC, id ASC LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_dict(row) if row else None

    async def prune_episodic_events(
        self,
        max_count: int = 5000,
        cutoff: str | None = None,
    ) -> int:
        """Evict the events that are over the limits. The oldest go first.

        Two limits: how many events are kept, and how old the oldest
        kept event may be. Whatever is over goes - and only a number
        comes out, never the events themselves (ADR-038).
        """
        pruned = 0

        # The ones that grew too old.
        if cutoff:
            async with self.conn.execute(
                "DELETE FROM episodic_events "
                "WHERE datetime(occurred_at) < datetime(?)",
                (cutoff,),
            ) as cursor:
                pruned += cursor.rowcount or 0

        # The ones that made it over the count - the oldest first.
        count = await self.count_episodic_events()
        if count > max_count:
            async with self.conn.execute(
                "DELETE FROM episodic_events WHERE id NOT IN ("
                "  SELECT id FROM episodic_events"
                "  ORDER BY datetime(occurred_at) DESC, id DESC LIMIT ?"
                ")",
                (max_count,),
            ) as cursor:
                pruned += cursor.rowcount or 0

        await self.conn.commit()
        return pruned

    async def record_episodic_prune(
        self,
        pruned_count: int,
        pruned_at: str | None = None,
    ) -> int:
        """Write down how many events were evicted.

        A number, not the events: the cleanup must not turn into a new
        copy of what it cleaned up (ADR-038).
        """
        stamp = pruned_at or datetime.now(timezone.utc).isoformat()
        async with self.conn.execute(
            "INSERT INTO episodic_prune_stats (pruned_at, pruned_count) "
            "VALUES (?, ?)",
            (stamp, int(pruned_count)),
        ) as cursor:
            row_id = cursor.lastrowid
        await self.conn.commit()
        return row_id or 0

    async def list_episodic_prune_stats(self, limit: int = 20) -> list[dict]:
        """The cleanups that happened, newest first."""
        async with self.conn.execute(
            "SELECT * FROM episodic_prune_stats "
            "ORDER BY pruned_at DESC, id DESC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(r) for r in rows]

    async def total_pruned_events(self) -> int:
        """How many events were evicted in all."""
        async with self.conn.execute(
            "SELECT COALESCE(SUM(pruned_count), 0) FROM episodic_prune_stats"
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def search_episodes_by_topic(
        self, topic: str, limit: int = 20
    ) -> list[dict]:
        """Search episodes by topic tag (LIKE match)."""
        pattern = f"%{topic}%"
        async with self.conn.execute(
            """
            SELECT * FROM episodes
            WHERE topic_tags LIKE ? OR content LIKE ?
            ORDER BY created_at DESC LIMIT ?
            """,
            (pattern, pattern, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(r) for r in rows]

    async def count_episodes(self) -> int:
        """Count total episodes."""
        async with self.conn.execute("SELECT COUNT(*) FROM episodes") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def list_episodes_by_session(self, session_id: str) -> list[dict]:
        """List all episodes in a session, ordered chronologically."""
        async with self.conn.execute(
            "SELECT * FROM episodes WHERE session_id = ? ORDER BY created_at ASC",
            (session_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(r) for r in rows]

    # ===== Profile =====

    async def upsert_profile(
        self,
        key: str,
        value: str,
        source_episode_id: int | None = None,
        confidence: float = 1.0,
        expires_at: datetime | None = None,
    ) -> None:
        """Insert or update a profile entry."""
        now = datetime.now().isoformat()
        expires_str = expires_at.isoformat() if expires_at else None
        await self.conn.execute(
            """
            INSERT INTO user_profile (key, value, source_episode_id, confidence,
                                      updated_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                source_episode_id = COALESCE(excluded.source_episode_id, user_profile.source_episode_id),
                confidence = excluded.confidence,
                updated_at = excluded.updated_at,
                expires_at = excluded.expires_at
            """,
            (key, value, source_episode_id, confidence, now, expires_str),
        )
        await self.conn.commit()

    async def get_profile(self, key: str) -> dict | None:
        """Get a profile entry."""
        async with self.conn.execute(
            "SELECT * FROM user_profile WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_dict(row) if row else None

    async def list_profile(self, prefix: str | None = None) -> list[dict]:
        """List profile entries, optionally filtered by key prefix."""
        if prefix:
            async with self.conn.execute(
                "SELECT * FROM user_profile WHERE key LIKE ? ORDER BY updated_at DESC",
                (f"{prefix}%",),
            ) as cursor:
                rows = await cursor.fetchall()
        else:
            async with self.conn.execute(
                "SELECT * FROM user_profile ORDER BY updated_at DESC"
            ) as cursor:
                rows = await cursor.fetchall()
        return [_row_to_dict(r) for r in rows]

    async def delete_profile(self, key: str) -> bool:
        """Delete a profile entry."""
        await self.conn.execute("DELETE FROM user_profile WHERE key = ?", (key,))
        await self.conn.commit()
        return True

    # ===== Conflicts =====

    async def add_conflict(
        self,
        conflict_type: str,
        llm_answer: str,
        kb_evidence: str,
        episode_id: int | None = None,
        knowledge_chunk_id: str | None = None,
        resolution: str | None = None,
        topic: str | None = None,
        citation: str | None = None,
    ) -> int:
        """Log a conflict.

        ``topic`` says what the divergence is about and ``citation``
        says where the knowledge base's side comes from - a record
        without them says that something differed, but not what.
        """
        async with self.conn.execute(
            """
            INSERT INTO conflicts (episode_id, knowledge_chunk_id, conflict_type,
                                   llm_answer, kb_evidence, topic, citation,
                                   resolution)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (episode_id, knowledge_chunk_id, conflict_type, llm_answer,
             kb_evidence, topic, citation, resolution),
        ) as cursor:
            conflict_id = cursor.lastrowid
        await self.conn.commit()
        return conflict_id or 0

    async def list_conflicts(
        self, limit: int = 50, include_acknowledged: bool = False
    ) -> list[dict]:
        """List conflict logs."""
        if include_acknowledged:
            sql = "SELECT * FROM conflicts ORDER BY created_at DESC LIMIT ?"
            params = (limit,)
        else:
            sql = "SELECT * FROM conflicts WHERE user_acknowledged = 0 ORDER BY created_at DESC LIMIT ?"
            params = (limit,)

        async with self.conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(r) for r in rows]

    async def acknowledge_conflict(self, conflict_id: int) -> None:
        """Mark a conflict as acknowledged."""
        await self.conn.execute(
            "UPDATE conflicts SET user_acknowledged = 1 WHERE id = ?",
            (conflict_id,),
        )
        await self.conn.commit()

    # ===== Wiki Pages =====

    async def upsert_wiki_page(
        self,
        page_id: str,
        title: str,
        markdown: str,
        summary: str,
        source_chunk_ids: list[str] | None = None,
        source_doc_ids: list[str] | None = None,
        category: str = "general",
        tags: list[str] | None = None,
        key_points: list[str] | None = None,
        user_edited: bool = False,
        page_type: str = "normal",
        tier: str = "longtail",
        increment_version: bool = True,
    ) -> None:
        """Insert or update a wiki page."""
        now = datetime.now().isoformat()

        if increment_version:
            # Get current version
            async with self.conn.execute(
                "SELECT version FROM wiki_pages WHERE id = ?", (page_id,)
            ) as cursor:
                row = await cursor.fetchone()
                current_version = (row[0] if row else 0) + 1
        else:
            current_version = 1

        await self.conn.execute(
            """
            INSERT INTO wiki_pages (id, title, markdown, summary, key_points,
                                    source_chunk_ids, source_doc_ids, category,
                                    tags, user_edited, page_type, tier, version,
                                    created_at, updated_at, last_compiled_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                markdown = excluded.markdown,
                summary = excluded.summary,
                key_points = excluded.key_points,
                source_chunk_ids = excluded.source_chunk_ids,
                source_doc_ids = excluded.source_doc_ids,
                category = excluded.category,
                tags = excluded.tags,
                -- A page the user has edited stays that way.
                user_edited = MAX(wiki_pages.user_edited, excluded.user_edited),
                page_type = excluded.page_type,
                tier = excluded.tier,
                version = excluded.version,
                updated_at = excluded.updated_at,
                last_compiled_at = excluded.last_compiled_at
            """,
            (page_id, title, markdown, summary, json.dumps(key_points or []),
             json.dumps(source_chunk_ids or []), json.dumps(source_doc_ids or []),
             category, json.dumps(tags or []), 1 if user_edited else 0,
             page_type, tier, current_version, now, now, now),
        )
        await self.conn.commit()

    async def get_wiki_page(self, page_id: str) -> dict | None:
        """Get a wiki page."""
        async with self.conn.execute(
            "SELECT * FROM wiki_pages WHERE id = ?", (page_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_dict(row) if row else None

    # ===== Document tiers =====

    async def upsert_doc_tier(
        self,
        doc_id: str,
        tier: str,
        decided_by: str,
        source_hash: str = "",
        compile_status: str = "not_ready",
        stale: bool = False,
        segment_count: int = 0,
    ) -> dict[str, Any]:
        """Record which layer a document belongs to.

        A document the user has put in a layer by hand keeps that
        layer: the rules and the model may not overrule the user.
        """
        if tier not in ("core", "longtail"):
            raise ValueError(f"Unknown tier: {tier!r}")
        if decided_by not in ("rule", "model", "user"):
            raise ValueError(f"Unknown decision source: {decided_by!r}")

        existing = await self.get_doc_tier(doc_id)
        if existing and existing["decided_by"] == "user" and decided_by != "user":
            # The user's own choice stands.
            return existing

        now = datetime.now().isoformat()
        await self.conn.execute(
            """
            INSERT INTO doc_tiers (doc_id, tier, decided_by, decided_at,
                                   source_hash, compile_status, stale,
                                   segment_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(doc_id) DO UPDATE SET
                tier = excluded.tier,
                decided_by = excluded.decided_by,
                decided_at = excluded.decided_at,
                source_hash = excluded.source_hash,
                compile_status = excluded.compile_status,
                stale = excluded.stale,
                segment_count = excluded.segment_count
            """,
            (
                doc_id,
                tier,
                decided_by,
                now,
                source_hash,
                compile_status,
                1 if stale else 0,
                segment_count,
            ),
        )
        await self.conn.commit()
        record = await self.get_doc_tier(doc_id)
        return record or {}

    async def get_doc_tier(self, doc_id: str) -> dict[str, Any] | None:
        """The layer of a document, and where the decision came from."""
        async with self.conn.execute(
            "SELECT * FROM doc_tiers WHERE doc_id = ?", (doc_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_dict(row) if row else None

    async def list_doc_tiers(
        self, tier: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """The documents of one layer, or of both."""
        if tier is None:
            sql = "SELECT * FROM doc_tiers ORDER BY doc_id LIMIT ?"
            params: tuple[Any, ...] = (limit,)
        else:
            sql = "SELECT * FROM doc_tiers WHERE tier = ? ORDER BY doc_id LIMIT ?"
            params = (tier, limit)
        async with self.conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(r) for r in rows]

    async def set_doc_tier_by_user(
        self, doc_id: str, tier: str
    ) -> dict[str, Any]:
        """The user puts a document in a layer by hand.

        From here on the rules and the model do not touch it.
        """
        return await self.upsert_doc_tier(
            doc_id=doc_id, tier=tier, decided_by="user"
        )

    async def set_doc_compile_status(
        self, doc_id: str, compile_status: str
    ) -> None:
        """How far the compilation of a core document got."""
        if compile_status not in (
            "compiled",
            "pending_recompile",
            "failed",
            "not_ready",
        ):
            raise ValueError(f"Unknown compile status: {compile_status!r}")
        await self.conn.execute(
            "UPDATE doc_tiers SET compile_status = ? WHERE doc_id = ?",
            (compile_status, doc_id),
        )
        await self.conn.commit()

    async def list_docs_needing_recompile(self) -> list[str]:
        """The documents whose compilation did not finish.

        Their content must not be retrieved: the pages say one thing
        and the document says another, and a reader who checks the page
        against the file would find a contradiction (AC-E-014).
        """
        async with self.conn.execute(
            "SELECT doc_id FROM doc_tiers "
            "WHERE compile_status IN ('pending_recompile', 'failed')"
        ) as cursor:
            rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def count_doc_tiers(self, tier: str) -> int:
        """How many documents sit in a layer."""
        async with self.conn.execute(
            "SELECT COUNT(*) FROM doc_tiers WHERE tier = ?", (tier,)
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def list_core_doc_ids(self) -> list[str]:
        """The documents in the core layer.

        Their slices are not long-tail material: the core layer answers
        with its compiled pages, and a slice of a core document would
        put the two layers in the same answer (ADR-034).
        """
        async with self.conn.execute(
            "SELECT doc_id FROM doc_tiers WHERE tier = 'core'"
        ) as cursor:
            rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def list_stale_doc_ids(self) -> list[str]:
        """The documents whose file changed after they were compiled.

        The pages compiled from them are kept - they are what the
        documents said - but they are marked, so the reader knows the
        file in front of him is not the one the page was read from
        (AC-E-015).
        """
        async with self.conn.execute(
            "SELECT doc_id FROM doc_tiers WHERE stale = 1"
        ) as cursor:
            rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def set_doc_stale(self, doc_id: str, stale: bool = True) -> None:
        """The source file changed since the page was compiled."""
        await self.conn.execute(
            "UPDATE doc_tiers SET stale = ? WHERE doc_id = ?",
            (1 if stale else 0, doc_id),
        )
        await self.conn.commit()

    async def set_doc_segment_count(self, doc_id: str, segment_count: int) -> None:
        """How many segments the compiler read."""
        await self.conn.execute(
            "UPDATE doc_tiers SET segment_count = ? WHERE doc_id = ?",
            (segment_count, doc_id),
        )
        await self.conn.commit()

    # ===== Page blocks =====

    async def list_page_blocks(self, page_id: str) -> list[dict]:
        """The blocks of a page, in the order they appear."""
        async with self.conn.execute(
            "SELECT * FROM wiki_page_blocks WHERE page_id = ? ORDER BY order_index, id",
            (page_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(r) for r in rows]

    async def replace_page_blocks(
        self, page_id: str, blocks: list[dict[str, Any]]
    ) -> None:
        """Write the blocks of a page, replacing the ones that are there.

        Each block is a dict with a text and an owner ("model" or
        "user").
        """
        await self.conn.execute(
            "DELETE FROM wiki_page_blocks WHERE page_id = ?", (page_id,)
        )
        for index, block in enumerate(blocks):
            await self.conn.execute(
                """
                INSERT INTO wiki_page_blocks
                    (id, page_id, order_index, text, owner, conclusion_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    block.get("id") or f"{page_id}#{index}",
                    page_id,
                    index,
                    block.get("text", ""),
                    block.get("owner", "model"),
                    block.get("conclusion_id"),
                ),
            )
        await self.conn.commit()

    async def delete_page_blocks(self, page_id: str) -> int:
        """Forget the blocks of a page."""
        async with self.conn.execute(
            "DELETE FROM wiki_page_blocks WHERE page_id = ?", (page_id,)
        ) as cursor:
            deleted = cursor.rowcount
        await self.conn.commit()
        return deleted or 0

    async def update_wiki_page_body(
        self,
        page_id: str,
        markdown: str,
        summary: str,
        key_points: list[str] | None = None,
        user_edited: bool = True,
    ) -> bool:
        """The user edited the page body.

        The sources, the category and the tags of the page stay as they
        are - the user changed the text, not where it came from.
        """
        async with self.conn.execute(
            "UPDATE wiki_pages SET markdown = ?, summary = ?, key_points = ?, "
            "user_edited = 1, version = version + 1, updated_at = ? "
            "WHERE id = ?",
            (
                markdown,
                summary,
                json.dumps(key_points or []),
                datetime.now().isoformat(),
                page_id,
            ),
        ) as cursor:
            changed = cursor.rowcount
        await self.conn.commit()
        return bool(changed)

    async def list_wiki_pages(
        self, category: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        """List wiki pages."""
        if category:
            sql = "SELECT * FROM wiki_pages WHERE category = ? ORDER BY updated_at DESC LIMIT ? OFFSET ?"
            params = (category, limit, offset)
        else:
            sql = "SELECT * FROM wiki_pages ORDER BY updated_at DESC LIMIT ? OFFSET ?"
            params = (limit, offset)

        async with self.conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(r) for r in rows]

    async def count_wiki_pages(self, category: str | None = None) -> int:
        """Count wiki pages."""
        if category:
            sql = "SELECT COUNT(*) FROM wiki_pages WHERE category = ?"
            params = (category,)
        else:
            sql = "SELECT COUNT(*) FROM wiki_pages"
            params = ()

        async with self.conn.execute(sql, params) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def delete_wiki_page(self, page_id: str) -> bool:
        """Delete a wiki page and its links."""
        await self.conn.execute(
            "DELETE FROM wiki_links WHERE source_id = ? OR target_id = ?",
            (page_id, page_id),
        )
        await self.conn.execute(
            "DELETE FROM wiki_page_blocks WHERE page_id = ?", (page_id,)
        )
        await self.conn.execute(
            "DELETE FROM page_conclusions WHERE page_id = ?", (page_id,)
        )
        await self.conn.execute("DELETE FROM wiki_pages WHERE id = ?", (page_id,))
        await self.conn.commit()
        return True

    # ===== Page conclusions =====

    async def upsert_page_conclusion(
        self,
        conclusion_id: str,
        page_id: str,
        text: str,
        anchor: dict[str, Any],
        source_doc_id: str = "",
        source_hash: str = "",
        segment_index: int = 0,
        conflict_group: str = "",
        active: bool = True,
    ) -> None:
        """Write a conclusion and where it comes from.

        The anchor is what makes the conclusion checkable - a claim
        without a position in the original is not written down at all.
        """
        await self.conn.execute(
            """
            INSERT INTO page_conclusions
                (id, page_id, text, anchor, source_doc_id, source_hash,
                 segment_index, conflict_group, active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                page_id = excluded.page_id,
                text = excluded.text,
                anchor = excluded.anchor,
                source_doc_id = excluded.source_doc_id,
                source_hash = excluded.source_hash,
                segment_index = excluded.segment_index,
                conflict_group = excluded.conflict_group,
                active = excluded.active
            """,
            (
                conclusion_id,
                page_id,
                text,
                json.dumps(anchor or {}, ensure_ascii=False),
                source_doc_id,
                source_hash,
                segment_index,
                conflict_group,
                1 if active else 0,
            ),
        )
        await self.conn.commit()

    async def list_page_conclusions(
        self, page_id: str, active_only: bool = True
    ) -> list[dict]:
        """The conclusions of a page, in the order they were written."""
        sql = "SELECT * FROM page_conclusions WHERE page_id = ?"
        if active_only:
            sql += " AND active = 1"
        # The rowid is the order they were written in - the timestamp
        # only says the second, and two conclusions in the same second
        # would otherwise come out in an arbitrary order.
        sql += " ORDER BY rowid"

        async with self.conn.execute(sql, (page_id,)) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_dict(r) for r in rows]

    async def get_page_conclusion(self, conclusion_id: str) -> dict | None:
        """One conclusion, with its anchor."""
        async with self.conn.execute(
            "SELECT * FROM page_conclusions WHERE id = ?", (conclusion_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_dict(row) if row else None

    async def list_active_conclusions(
        self, limit: int = 200
    ) -> list[dict]:
        """Every conclusion in effect - the ones a question may lean on.

        A conclusion whose anchor is missing is left out: evidence that
        cannot be shown to the user cannot be used to answer either.
        """
        async with self.conn.execute(
            "SELECT * FROM page_conclusions WHERE active = 1 "
            "ORDER BY created_at DESC, id LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()

        from yuanwork.utils.anchor import has_anchor

        conclusions = []
        for row in rows:
            conclusion = _row_to_dict(row)
            if not has_anchor(conclusion):
                continue
            conclusions.append(conclusion)
        return conclusions

    async def delete_page_conclusions(self, page_id: str) -> int:
        """Forget the conclusions of a page."""
        async with self.conn.execute(
            "DELETE FROM page_conclusions WHERE page_id = ?", (page_id,)
        ) as cursor:
            deleted = cursor.rowcount
        await self.conn.commit()
        return deleted

    async def mark_page_conflict(
        self, conflict_group: str, new_id: str, old_ids: list[str]
    ) -> None:
        """Say that two conclusions contradict each other.

        Both are kept (ADR-033): the new one is the version in effect,
        the old one is not - but it is not thrown away either. The
        reader has to see the difference, not just the new version.
        """
        for old_id in old_ids:
            await self.conn.execute(
                "UPDATE page_conclusions SET conflict_group = ?, active = 0 "
                "WHERE id = ?",
                (conflict_group, old_id),
            )
        await self.conn.execute(
            "UPDATE page_conclusions SET conflict_group = ?, active = 1 "
            "WHERE id = ?",
            (conflict_group, new_id),
        )
        # Inside a conflict group at most one conclusion is in effect.
        await self.conn.execute(
            "UPDATE page_conclusions SET active = 0 "
            "WHERE conflict_group = ? AND id != ?",
            (conflict_group, new_id),
        )
        await self.conn.commit()

    async def delete_page_conclusions_except(
        self, page_id: str, keep_ids: list[str]
    ) -> int:
        """Forget the conclusions of a page that the new compilation
        does not have.

        A recompile replaces the conclusions of a page - the ones that
        are gone are not evidence any more. The ones that are still
        there are kept, so a page that is compiled twice does not fill
        up with copies of itself.
        """
        if keep_ids:
            placeholders = ",".join("?" for _ in keep_ids)
            sql = (
                "DELETE FROM page_conclusions "
                f"WHERE page_id = ? AND id NOT IN ({placeholders})"
            )
            params = (page_id, *keep_ids)
        else:
            sql = "DELETE FROM page_conclusions WHERE page_id = ?"
            params = (page_id,)
        async with self.conn.execute(sql, params) as cursor:
            deleted = cursor.rowcount
        await self.conn.commit()
        return deleted

    # ===== Wiki Links =====

    async def add_wiki_link(
        self,
        source_id: str,
        source_type: str,
        target_id: str,
        target_type: str,
        link_type: str = "related",
        weight: float = 1.0,
    ) -> None:
        """Add a bidirectional link."""
        await self.conn.execute(
            """
            INSERT OR IGNORE INTO wiki_links
                (source_id, source_type, target_id, target_type, link_type, weight)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (source_id, source_type, target_id, target_type, link_type, weight),
        )
        await self.conn.commit()

    async def get_wiki_backlinks(self, target_id: str) -> list[dict]:
        """Get all links pointing to a given target."""
        async with self.conn.execute(
            "SELECT * FROM wiki_links WHERE target_id = ? ORDER BY weight DESC",
            (target_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(r) for r in rows]

    async def get_wiki_outgoing_links(self, source_id: str) -> list[dict]:
        """Get all links originating from a given source."""
        async with self.conn.execute(
            "SELECT * FROM wiki_links WHERE source_id = ? ORDER BY weight DESC",
            (source_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(r) for r in rows]

    # ===== Entities =====

    async def upsert_entity(
        self,
        entity_id: str,
        name: str,
        entity_type: str,
        aliases: list[str] | None = None,
        properties: dict | None = None,
        source_event_ids: list[int] | None = None,
        status: str = "active",
    ) -> None:
        """Insert or update an entity.

        ``source_event_ids`` are the episodic events the entity was
        induced from - a conclusion that cannot be traced back to what
        happened is not a conclusion, it is a guess. Writing the entity
        again *merges* the event list instead of replacing it, so the
        trace stays complete.

        The status never goes backwards: an entity the user has
        confirmed stays in effect even when it is induced again, and a
        rule that is waiting for the user keeps waiting.
        """
        existing = await self.get_entity(entity_id)
        merged_events: list[int] = []
        for event_id in list(existing["source_event_ids"] if existing else []) + list(
            source_event_ids or []
        ):
            if event_id not in merged_events:
                merged_events.append(event_id)
        if existing and existing["status"] == "active":
            status = "active"

        now = datetime.now().isoformat()
        await self.conn.execute(
            """
            INSERT INTO entities (id, name, type, aliases, properties,
                                  source_event_ids, status,
                                  mention_count, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                type = excluded.type,
                aliases = excluded.aliases,
                properties = excluded.properties,
                source_event_ids = excluded.source_event_ids,
                status = excluded.status,
                mention_count = mention_count + 1,
                last_seen = excluded.last_seen
            """,
            (entity_id, name, entity_type,
             json.dumps(aliases or []), json.dumps(properties or {}),
             json.dumps(merged_events), status,
             now, now),
        )
        await self.conn.commit()

    async def get_entity(self, entity_id: str) -> dict | None:
        """Get an entity."""
        async with self.conn.execute(
            "SELECT * FROM entities WHERE id = ?", (entity_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_dict(row) if row else None

    async def list_entities(
        self,
        entity_type: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """The entities, by category and by status.

        A pending entity - one the user has not confirmed yet - does not
        count as evidence, so the caller filters on the status.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if entity_type is not None:
            clauses.append("type = ?")
            params.append(entity_type)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)

        async with self.conn.execute(
            f"SELECT * FROM entities {where} ORDER BY mention_count DESC, name LIMIT ?",
            tuple(params),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(r) for r in rows]

    async def set_entity_status(self, entity_id: str, status: str) -> None:
        """Move an entity between "in effect" and "waiting for the user".

        A pending entity does not count as evidence (design 3.1).
        """
        if status not in ("active", "pending"):
            raise ValueError(f"Unknown entity status: {status!r}")
        await self.conn.execute(
            "UPDATE entities SET status = ? WHERE id = ?", (status, entity_id)
        )
        await self.conn.commit()

    async def add_entity_relation(
        self,
        source_id: str,
        target_id: str,
        relation_type: str,
        source_event_ids: list[int] | None = None,
    ) -> int:
        """Record that one entity stands in some relation to another."""
        async with self.conn.execute(
            """
            INSERT OR IGNORE INTO entity_relations
                (source_id, target_id, relation_type, source_event_ids)
            VALUES (?, ?, ?, ?)
            """,
            (source_id, target_id, relation_type,
             json.dumps(source_event_ids or [])),
        ) as cursor:
            relation_id = cursor.lastrowid
        await self.conn.commit()
        return relation_id or 0

    async def list_entity_relations(
        self,
        entity_id: str | None = None,
    ) -> list[dict]:
        """The relations, of one entity or of all of them."""
        if entity_id is None:
            async with self.conn.execute(
                "SELECT * FROM entity_relations ORDER BY id"
            ) as cursor:
                rows = await cursor.fetchall()
                return [_row_to_dict(r) for r in rows]

        async with self.conn.execute(
            "SELECT * FROM entity_relations "
            "WHERE source_id = ? OR target_id = ? ORDER BY id",
            (entity_id, entity_id),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(r) for r in rows]

    async def delete_entity(self, entity_id: str) -> int:
        """Forget an entity and the relations that point at it."""
        async with self.conn.execute(
            "DELETE FROM entities WHERE id = ?", (entity_id,)
        ) as cursor:
            deleted = cursor.rowcount
        await self.conn.commit()
        return deleted or 0

    async def add_entity_mention(
        self,
        entity_id: str,
        page_id: str | None = None,
        chunk_id: str | None = None,
        doc_id: str | None = None,
        context: str | None = None,
    ) -> None:
        """Record that an entity is mentioned in a page/chunk/doc."""
        await self.conn.execute(
            """
            INSERT OR IGNORE INTO entity_mentions
                (entity_id, page_id, chunk_id, doc_id, context)
            VALUES (?, ?, ?, ?, ?)
            """,
            (entity_id, page_id, chunk_id, doc_id, context),
        )
        await self.conn.commit()

    # ===== Page update candidates =====

    async def add_page_update_candidate(
        self,
        page_id: str,
        point: str,
        topic: str = "",
        source_event_ids: list[int] | None = None,
    ) -> int:
        """Record a key point the consolidation induced for a page.

        The same point for the same page is only stored once; the
        events it was induced from are merged into the row that is
        already there, so the trace stays complete.
        """
        existing = await self._find_page_update(page_id, point)
        now = datetime.now().isoformat()

        if existing is None:
            async with self.conn.execute(
                """
                INSERT INTO page_update_candidates
                    (page_id, topic, point, source_event_ids, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (page_id, topic, point,
                 json.dumps(source_event_ids or []), now),
            ) as cursor:
                candidate_id = cursor.lastrowid
        else:
            merged: list[int] = []
            for event_id in list(existing["source_event_ids"]) + list(
                source_event_ids or []
            ):
                if event_id not in merged:
                    merged.append(event_id)
            await self.conn.execute(
                "UPDATE page_update_candidates "
                "SET source_event_ids = ?, topic = ? WHERE id = ?",
                (json.dumps(merged), topic, existing["id"]),
            )
            candidate_id = existing["id"]

        await self.conn.commit()
        return candidate_id or 0

    async def _find_page_update(
        self, page_id: str, point: str
    ) -> dict[str, Any] | None:
        """The candidate for this page and point, if it is already there."""
        async with self.conn.execute(
            "SELECT * FROM page_update_candidates "
            "WHERE page_id = ? AND point = ?",
            (page_id, point),
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_dict(row) if row else None

    async def list_page_update_candidates(
        self,
        status: str | None = None,
        page_id: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """The induced key points, by status and by page."""
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if page_id is not None:
            clauses.append("page_id = ?")
            params.append(page_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)

        async with self.conn.execute(
            f"SELECT * FROM page_update_candidates {where} "
            "ORDER BY id LIMIT ?",
            tuple(params),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(r) for r in rows]

    async def count_page_update_candidates(
        self, status: str | None = None
    ) -> int:
        """How many induced key points there are."""
        if status is None:
            sql = "SELECT COUNT(*) FROM page_update_candidates"
            params: tuple[Any, ...] = ()
        else:
            sql = "SELECT COUNT(*) FROM page_update_candidates WHERE status = ?"
            params = (status,)
        async with self.conn.execute(sql, params) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def list_entities_by_source_event(
        self, event_id: int, limit: int = 200
    ) -> list[dict]:
        """The conclusions that were induced from one interaction.

        The other end of the trace: the conclusion says which
        interactions it came from; this says what one interaction led
        to.
        """
        async with self.conn.execute(
            "SELECT * FROM entities WHERE EXISTS ("
            "  SELECT 1 FROM json_each(entities.source_event_ids) "
            "  WHERE json_each.value = ?"
            ") ORDER BY mention_count DESC, name LIMIT ?",
            (event_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(r) for r in rows]

    async def list_page_updates_by_source_event(
        self, event_id: int, limit: int = 200
    ) -> list[dict]:
        """The key points that were induced from one interaction."""
        async with self.conn.execute(
            "SELECT * FROM page_update_candidates WHERE EXISTS ("
            "  SELECT 1 FROM json_each(page_update_candidates.source_event_ids) "
            "  WHERE json_each.value = ?"
            ") ORDER BY id LIMIT ?",
            (event_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(r) for r in rows]

    async def get_episodic_event(self, event_id: int) -> dict | None:
        """One interaction, by its identifier."""
        async with self.conn.execute(
            "SELECT * FROM episodic_events WHERE id = ?", (event_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_dict(row) if row else None

    # ===== Ingest jobs =====

    async def upsert_ingest_job(
        self,
        job_id: str,
        status: Any = _UNSET,
        progress: Any = _UNSET,
        files_done: Any = _UNSET,
        files_total: Any = _UNSET,
        error: Any = _UNSET,
        current_file: Any = _UNSET,
        failed_files: Any = _UNSET,
        failed_total: Any = _UNSET,
        unsupported_total: Any = _UNSET,
    ) -> None:
        """Record the state of an ingestion job.

        The job lives in the database, not in the process: a build can
        take a long time, and the backend may restart while it runs.

        A field that is not mentioned keeps its current value - a caller
        that only wants to say "it failed" must not wipe the progress it
        reported a moment ago.
        """
        existing = await self.get_ingest_job(job_id)
        if existing is None:
            current = {
                "status": "pending",
                "progress": 0.0,
                "files_done": 0,
                "files_total": 0,
                "error": None,
                "current_file": None,
                "failed_files": "[]",
                "failed_total": 0,
                "unsupported_total": 0,
            }
        else:
            current = {
                "status": existing["status"],
                "progress": existing["progress"],
                "files_done": existing["files_done"],
                "files_total": existing["files_total"],
                "error": existing["error"],
                "current_file": existing.get("current_file"),
                "failed_files": existing.get("failed_files") or "[]",
                "failed_total": existing.get("failed_total") or 0,
                "unsupported_total": existing.get("unsupported_total") or 0,
            }

        values = {
            "status": current["status"],
            "progress": current["progress"],
            "files_done": current["files_done"],
            "files_total": current["files_total"],
            "error": current["error"],
            "current_file": current["current_file"],
            "failed_files": current["failed_files"],
            "failed_total": current["failed_total"],
            "unsupported_total": current["unsupported_total"],
        }
        for key, value in (
            ("status", status),
            ("progress", progress),
            ("files_done", files_done),
            ("files_total", files_total),
            ("error", error),
            ("current_file", current_file),
            ("failed_files", failed_files),
            ("failed_total", failed_total),
            ("unsupported_total", unsupported_total),
        ):
            if value is not _UNSET:
                values[key] = value

        now = datetime.now().isoformat()
        await self.conn.execute(
            """
            INSERT INTO ingest_jobs
                (job_id, status, progress, files_done, files_total, error,
                 current_file, failed_files, failed_total, unsupported_total,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                status = excluded.status,
                progress = excluded.progress,
                files_done = excluded.files_done,
                files_total = excluded.files_total,
                error = excluded.error,
                current_file = excluded.current_file,
                failed_files = excluded.failed_files,
                failed_total = excluded.failed_total,
                unsupported_total = excluded.unsupported_total,
                updated_at = excluded.updated_at
            """,
            (
                job_id,
                values["status"],
                values["progress"],
                values["files_done"],
                values["files_total"],
                values["error"],
                values["current_file"],
                values["failed_files"],
                values["failed_total"],
                values["unsupported_total"],
                now,
                now,
            ),
        )
        await self.conn.commit()

    async def get_ingest_job(self, job_id: str) -> dict | None:
        """The state of one ingestion job."""
        async with self.conn.execute(
            "SELECT * FROM ingest_jobs WHERE job_id = ?", (job_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_dict(row) if row else None

    async def get_latest_ingest_job(self) -> dict | None:
        """The job that ran last.

        The app reads it when it starts: the files the last build could
        not read are still there (ADR-022).
        """
        async with self.conn.execute(
            "SELECT * FROM ingest_jobs ORDER BY updated_at DESC, rowid DESC LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_dict(row) if row else None

    async def list_ingest_jobs(
        self, status: str | None = None, limit: int = 50
    ) -> list[dict]:
        """The ingestion jobs, newest first."""
        sql = "SELECT * FROM ingest_jobs"
        params: list = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        async with self.conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(r) for r in rows]

    async def clear_finished_ingest_jobs(self) -> int:
        """Drop the jobs that are done - they are history, not state."""
        cursor = await self.conn.execute(
            "DELETE FROM ingest_jobs WHERE status IN ('completed', 'failed')"
        )
        await self.conn.commit()
        return cursor.rowcount

    async def clear_running_ingest_jobs(self) -> int:
        """Drop the jobs that were running when the process died.

        Nothing is running them any more: the worker thread went with the
        process. Leaving them behind would show a progress bar that
        never moves.
        """
        cursor = await self.conn.execute(
            "DELETE FROM ingest_jobs WHERE status IN ('pending', 'running')"
        )
        await self.conn.commit()
        return cursor.rowcount

    # ===== Maintenance =====

    async def vacuum(self) -> None:
        """Run VACUUM to reclaim space."""
        await self.conn.execute("VACUUM")
