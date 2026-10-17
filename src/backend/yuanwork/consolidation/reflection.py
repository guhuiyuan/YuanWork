"""Reflection - periodic memory consolidation.

Stage 1: simple deduplication + profile update.
Stage 2: LLM-powered summarization + cross-episode analysis.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from yuanwork.db.sqlite import SQLiteStore
    from yuanwork.llm.gateway import LLMGateway
    from yuanwork.memory.episodic import EpisodicMemory


logger = logging.getLogger(__name__)


class ReflectionScheduler:
    """Triggers reflection based on conditions."""

    def __init__(
        self,
        episodic_memory: "EpisodicMemory",
        sqlite_store: "SQLiteStore",
        llm_gateway: "LLMGateway | None" = None,
        trigger_turns: int = 10,
        trigger_idle_minutes: int = 30,
    ):
        self.episodic = episodic_memory
        self.sqlite = sqlite_store
        self.llm = llm_gateway
        self.trigger_turns = trigger_turns
        self.trigger_idle_minutes = trigger_idle_minutes
        self._last_reflection: datetime | None = None

    async def should_reflect(self, session_id: str) -> bool:
        """Check if reflection should be triggered."""
        # Trigger 1: turn threshold
        episodes = await self.episodic.list_recent(limit=self.trigger_turns + 1)
        if len(episodes) >= self.trigger_turns:
            return True

        # Trigger 2: idle time
        if self._last_reflection is not None:
            idle = datetime.now() - self._last_reflection
            if idle > timedelta(minutes=self.trigger_idle_minutes):
                return True

        return False

    async def reflect(self) -> dict[str, Any]:
        """Run reflection: deduplicate, summarize, update profile.

        Stage 1: simple deduplication
        Stage 2: LLM-powered cross-episode summarization
        """
        from yuanwork.consolidation.engine import ConsolidationEngine
        from yuanwork.memory.profile import ProfileStore
        from yuanwork.memory.semantic import SemanticMemory

        engine = ConsolidationEngine(
            episodic_memory=self.episodic,
            semantic_memory=SemanticMemory(
                data_dir=self.sqlite.db_path.parent / "lancedb",
            ),
            profile_store=ProfileStore(self.sqlite),
            sqlite_store=self.sqlite,
            llm_gateway=self.llm,
        )

        result = await engine.run(reason="scheduled")
        self._last_reflection = datetime.now()
        return result
