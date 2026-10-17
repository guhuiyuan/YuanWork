"""Episodic Memory - conversation history stored in SQLite.

Records what the user said, what the assistant said, and metadata
for later retrieval (e.g., "上次我们讨论过的方案三").
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from yuanwork.db.sqlite import SQLiteStore

logger = logging.getLogger(__name__)


class EpisodicMemory:
    """Episodic memory interface backed by SQLiteStore."""

    def __init__(self, sqlite_store: SQLiteStore):
        self.sqlite = sqlite_store

    async def add(
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
        return await self.sqlite.add_episode(
            session_id=session_id,
            role=role,
            content=content,
            topic_tags=topic_tags,
            entities=entities,
            embedding=embedding,
            importance_score=importance_score,
        )

    async def count(self) -> int:
        """Count total episodic entries."""
        return await self.sqlite.count_episodes()

    async def list_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """List recent episodes."""
        return await self.sqlite.list_recent_episodes(limit=limit)

    async def search_by_topic(
        self, topic: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Search episodes by topic tag."""
        return await self.sqlite.search_episodes_by_topic(topic, limit=limit)

    async def list_by_session(self, session_id: str) -> list[dict[str, Any]]:
        """List all episodes in a session, chronologically."""
        return await self.sqlite.list_episodes_by_session(session_id)

    async def get_context_window(
        self, session_id: str, max_turns: int = 10
    ) -> list[dict[str, Any]]:
        """Get the most recent N turns from a session for context."""
        episodes = await self.sqlite.list_episodes_by_session(session_id)
        return episodes[-max_turns:]

    async def delete_session(self, session_id: str) -> int:
        """Delete all episodes in a session."""
        # TODO: implement when needed
        raise NotImplementedError("delete_session not yet implemented")
