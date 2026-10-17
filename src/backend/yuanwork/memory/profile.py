"""Profile Store - user facts and preferences."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from yuanwork.db.sqlite import SQLiteStore

logger = logging.getLogger(__name__)


class ProfileStore:
    """User profile store (structured facts)."""

    def __init__(self, sqlite_store: SQLiteStore):
        self.sqlite = sqlite_store

    async def set(
        self,
        key: str,
        value: str,
        source_episode_id: int | None = None,
        confidence: float = 1.0,
        expires_at: datetime | None = None,
    ) -> None:
        """Set a profile entry."""
        await self.sqlite.upsert_profile(
            key=key,
            value=value,
            source_episode_id=source_episode_id,
            confidence=confidence,
            expires_at=expires_at,
        )

    async def get(self, key: str) -> dict[str, Any] | None:
        """Get a profile entry."""
        return await self.sqlite.get_profile(key)

    async def get_many(self, keys: list[str]) -> dict[str, dict[str, Any]]:
        """Get multiple profile entries."""
        result = {}
        for key in keys:
            entry = await self.sqlite.get_profile(key)
            if entry:
                result[key] = entry
        return result

    async def list_all(self, prefix: str | None = None) -> list[dict[str, Any]]:
        """List all profile entries, optionally filtered by key prefix."""
        return await self.sqlite.list_profile(prefix=prefix)

    async def delete(self, key: str) -> bool:
        """Delete a profile entry."""
        return await self.sqlite.delete_profile(key)
