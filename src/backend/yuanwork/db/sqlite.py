"""SQLite database store for relational data (files, profiles, episodes, conflicts, wiki).

Uses native sqlite3 + aiosqlite for async operations.
WAL mode enabled for better concurrency.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


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
    meta TEXT DEFAULT '{}'  -- JSON
);

CREATE INDEX IF NOT EXISTS idx_files_file_hash ON files(file_hash);
CREATE INDEX IF NOT EXISTS idx_files_file_type ON files(file_type);
CREATE INDEX IF NOT EXISTS idx_files_indexed_at ON files(indexed_at DESC);

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
    source_chunk_ids TEXT DEFAULT '[]',  -- JSON
    source_doc_ids TEXT DEFAULT '[]',    -- JSON
    category TEXT NOT NULL DEFAULT 'general',
    tags TEXT DEFAULT '[]',              -- JSON
    version INTEGER DEFAULT 1,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_compiled_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_wiki_category ON wiki_pages(category);
CREATE INDEX IF NOT EXISTS idx_wiki_updated ON wiki_pages(updated_at DESC);

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
    first_seen TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type);
CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);

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

-- Ingestion job tracking (persistent, optional)
CREATE TABLE IF NOT EXISTS ingest_jobs (
    job_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    progress REAL DEFAULT 0.0,
    files_done INTEGER DEFAULT 0,
    files_total INTEGER DEFAULT 0,
    error TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


# ===== Helpers =====


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Convert sqlite3.Row to dict, parsing JSON columns."""
    d = dict(row)
    for key in ("meta", "topic_tags", "entities", "key_points", "related_files",
                "source_chunk_ids", "source_doc_ids", "tags", "context", "aliases",
                "properties"):
        if key in d and isinstance(d[key], str):
            try:
                d[key] = json.loads(d[key])
            except (json.JSONDecodeError, TypeError):
                pass
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
        logger.info(f"SQLite initialized at {self.db_path}")

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
        meta: dict | None = None,
    ) -> None:
        """Insert or update a file record."""
        now = datetime.now().isoformat()
        await self.conn.execute(
            """
            INSERT INTO files (doc_id, file_name, file_path, file_hash, file_type,
                               size_bytes, chunk_count, indexed_at, last_modified, meta)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(doc_id) DO UPDATE SET
                file_name = excluded.file_name,
                file_path = excluded.file_path,
                file_hash = excluded.file_hash,
                file_type = excluded.file_type,
                size_bytes = excluded.size_bytes,
                chunk_count = excluded.chunk_count,
                indexed_at = excluded.indexed_at,
                last_modified = excluded.last_modified,
                meta = excluded.meta
            """,
            (doc_id, file_name, file_path, file_hash, file_type, size_bytes,
             chunk_count, now, now, json.dumps(meta or {})),
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
    ) -> int:
        """Log a conflict."""
        async with self.conn.execute(
            """
            INSERT INTO conflicts (episode_id, knowledge_chunk_id, conflict_type,
                                   llm_answer, kb_evidence, resolution)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (episode_id, knowledge_chunk_id, conflict_type, llm_answer,
             kb_evidence, resolution),
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
            INSERT INTO wiki_pages (id, title, markdown, summary, source_chunk_ids,
                                    source_doc_ids, category, tags, version,
                                    created_at, updated_at, last_compiled_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                markdown = excluded.markdown,
                summary = excluded.summary,
                source_chunk_ids = excluded.source_chunk_ids,
                source_doc_ids = excluded.source_doc_ids,
                category = excluded.category,
                tags = excluded.tags,
                version = excluded.version,
                updated_at = excluded.updated_at,
                last_compiled_at = excluded.last_compiled_at
            """,
            (page_id, title, markdown, summary,
             json.dumps(source_chunk_ids or []), json.dumps(source_doc_ids or []),
             category, json.dumps(tags or []), current_version,
             now, now, now),
        )
        await self.conn.commit()

    async def get_wiki_page(self, page_id: str) -> dict | None:
        """Get a wiki page."""
        async with self.conn.execute(
            "SELECT * FROM wiki_pages WHERE id = ?", (page_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_dict(row) if row else None

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
        await self.conn.execute("DELETE FROM wiki_pages WHERE id = ?", (page_id,))
        await self.conn.commit()
        return True

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
    ) -> None:
        """Insert or update an entity."""
        now = datetime.now().isoformat()
        await self.conn.execute(
            """
            INSERT INTO entities (id, name, type, aliases, properties,
                                  mention_count, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                type = excluded.type,
                aliases = excluded.aliases,
                properties = excluded.properties,
                mention_count = mention_count + 1,
                last_seen = excluded.last_seen
            """,
            (entity_id, name, entity_type,
             json.dumps(aliases or []), json.dumps(properties or {}),
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

    # ===== Maintenance =====

    async def vacuum(self) -> None:
        """Run VACUUM to reclaim space."""
        await self.conn.execute("VACUUM")
