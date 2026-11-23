"""Episodic Memory - conversation history stored in SQLite.

Records what the user said, what the assistant said, and metadata
for later retrieval (e.g., "上次我们讨论过的方案三").
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from yuanwork.db.sqlite import SQLiteStore

logger = logging.getLogger(__name__)


class EpisodicMemory:
    """Episodic memory interface backed by SQLiteStore."""

    #: The same two limits as the events (ADR-038): how many records
    #: are kept, and how old the oldest kept one may be. The dialogue
    #: grows turn by turn - it is the same kind of growth as the
    #: events, so it gets the same bounds.
    DEFAULT_MAX_RECORDS = 5000
    DEFAULT_MAX_AGE_DAYS = 180

    def __init__(
        self,
        sqlite_store: SQLiteStore,
        max_records: int | None = None,
        max_age_days: int | None = None,
    ):
        self.sqlite = sqlite_store
        self.max_records = (
            max_records if max_records is not None else self.DEFAULT_MAX_RECORDS
        )
        self.max_age_days = (
            max_age_days if max_age_days is not None else self.DEFAULT_MAX_AGE_DAYS
        )

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
        episode_id = await self.sqlite.add_episode(
            session_id=session_id,
            role=role,
            content=content,
            topic_tags=topic_tags,
            entities=entities,
            embedding=embedding,
            importance_score=importance_score,
        )
        # The record is written first - a cleanup that fails is not a
        # reason to lose the turn.
        await self._prune_if_needed()
        return episode_id

    async def prune(self) -> int:
        """Evict the records that are over the limits.

        The oldest go first. Only the number of evicted records comes
        out - the records themselves are gone, and no copy of them is
        kept (ADR-038).
        """
        try:
            pruned = await self.sqlite.prune_episodes(
                max_count=self.max_records,
                cutoff=self._cutoff(),
            )
        except Exception as e:  # noqa: BLE001 - a late cleanup is not a broken one
            logger.warning(f"The conversation record cleanup failed: {e}")
            return 0

        if pruned:
            try:
                await self.sqlite.record_episodic_prune(pruned)
            except Exception as e:  # noqa: BLE001 - the number is a nicety
                logger.warning(f"The cleanup count was not written down: {e}")
        return pruned

    async def _prune_if_needed(self) -> int:
        """Prune when the records are over the limits.

        The trigger: more records than the limit, or a record that has
        grown too old.
        """
        try:
            count = await self.sqlite.count_episodes()
            if count > self.max_records:
                return await self.prune()

            oldest = await self.sqlite.oldest_episode()
            if oldest is not None and self._is_too_old(
                str(oldest.get("created_at") or "")
            ):
                return await self.prune()
        except Exception as e:  # noqa: BLE001 - the check must not break a record
            logger.warning(f"The conversation record cleanup check failed: {e}")
        return 0

    def _cutoff(self) -> str:
        """The moment before which a record has grown too old."""
        return (
            datetime.now(timezone.utc) - timedelta(days=self.max_age_days)
        ).isoformat()

    def _is_too_old(self, created_at: str) -> bool:
        """Whether a record has grown too old."""
        if not created_at:
            return False
        try:
            moment = datetime.fromisoformat(created_at)
        except ValueError:
            # A timestamp nobody can read is not one we throw away.
            return False
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment < datetime.now(timezone.utc) - timedelta(
            days=self.max_age_days
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
