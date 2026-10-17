"""Consolidation Engine - the integration layer.

Responsibilities:
- Write control: new conversations → profile updates
- Conflict resolution: LLM training knowledge vs KB
- Reflection: periodic deduplication, summarization, profile updates
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from yuanwork.db.sqlite import SQLiteStore
    from yuanwork.memory.episodic import EpisodicMemory
    from yuanwork.memory.profile import ProfileStore
    from yuanwork.memory.semantic import SemanticMemory
    from yuanwork.llm.gateway import LLMGateway


logger = logging.getLogger(__name__)


class ConsolidationEngine:
    """Integrates episodic memory with semantic memory."""

    def __init__(
        self,
        episodic_memory: "EpisodicMemory",
        semantic_memory: "SemanticMemory",
        profile_store: "ProfileStore",
        sqlite_store: "SQLiteStore",
        llm_gateway: "LLMGateway | None" = None,
    ):
        self.episodic = episodic_memory
        self.semantic = semantic_memory
        self.profile = profile_store
        self.sqlite = sqlite_store
        self.llm = llm_gateway

    async def record_episode(
        self,
        session_id: str,
        role: str,
        content: str,
        topic_tags: list[str] | None = None,
        entities: list[str] | None = None,
        importance_score: float = 0.5,
    ) -> int:
        """Record an episode and trigger lightweight processing."""
        episode_id = await self.episodic.add(
            session_id=session_id,
            role=role,
            content=content,
            topic_tags=topic_tags,
            entities=entities,
            importance_score=importance_score,
        )

        # If this is a user message with strong factual claims,
        # check for profile updates (stage 2 enhancement)
        if role == "user":
            await self._maybe_update_profile(episode_id, content)

        return episode_id

    async def _maybe_update_profile(self, episode_id: int, content: str) -> None:
        """Detect user preferences/facts in message and update profile.

        Simple heuristic (stage 1): look for "I like/love/prefer/hate" patterns.
        Stage 2: use LLM for robust extraction.
        """
        # Basic pattern matching for preferences
        patterns = [
            ("我喜欢", "user.preference.like"),
            ("我更喜欢", "user.preference.prefer"),
            ("我讨厌", "user.preference.dislike"),
            ("我不喜欢", "user.preference.dislike"),
        ]

        for pattern, key_prefix in patterns:
            if pattern in content:
                # Extract the preference value
                idx = content.find(pattern) + len(pattern)
                value = content[idx:idx + 100].split("。")[0].split("，")[0].strip()
                if value:
                    key = f"{key_prefix}.{value[:20]}"
                    await self.profile.set(
                        key=key,
                        value=value,
                        source_episode_id=episode_id,
                        confidence=0.7,
                    )
                    logger.debug(f"Profile updated: {key} = {value}")
                break

    async def log_conflict(
        self,
        llm_answer: str,
        kb_evidence: str,
        episode_id: int | None = None,
        knowledge_chunk_id: str | None = None,
        resolution: str | None = None,
    ) -> int:
        """Log an LLM-vs-KB conflict."""
        return await self.sqlite.add_conflict(
            conflict_type="llm_vs_kb",
            llm_answer=llm_answer,
            kb_evidence=kb_evidence,
            episode_id=episode_id,
            knowledge_chunk_id=knowledge_chunk_id,
            resolution=resolution,
        )

    async def run(self, reason: str = "manual", force: bool = False) -> dict[str, Any]:
        """Run consolidation (reflection).

        Triggers:
        - manual: user-triggered
        - scheduled: periodic
        - threshold: too many episodes accumulated

        Returns stats dict.
        """
        start = datetime.now()
        episodes_processed = 0
        duplicates_merged = 0
        profile_updates = 0
        conflicts_archived = 0

        # 1. Get recent episodes
        recent = await self.episodic.list_recent(limit=100)

        # 2. Deduplicate similar episodes (>0.92 similarity)
        # Stage 1: simple textual similarity (Stage 2: use embeddings)
        seen_hashes: set[str] = set()
        deduped = []
        for ep in recent:
            content_hash = hash(ep["content"][:200])
            if content_hash in seen_hashes:
                duplicates_merged += 1
                continue
            seen_hashes.add(content_hash)
            deduped.append(ep)
            episodes_processed += 1

        # 3. Profile updates: scan recent user episodes for new facts
        for ep in deduped:
            if ep["role"] == "user":
                await self._maybe_update_profile(ep["id"], ep["content"])
                profile_updates += 1

        # 4. Archive old conflicts (>30 days)
        # TODO: implement when conflicts table is in active use

        duration_ms = (datetime.now() - start).total_seconds() * 1000

        result = {
            "episodes_processed": episodes_processed,
            "duplicates_merged": duplicates_merged,
            "profile_updates": profile_updates,
            "conflicts_archived": conflicts_archived,
            "duration_ms": duration_ms,
        }

        logger.info(
            f"Consolidation ({reason}): {episodes_processed} episodes, "
            f"{duplicates_merged} merged, {profile_updates} profile updates "
            f"in {duration_ms:.0f}ms"
        )

        return result
