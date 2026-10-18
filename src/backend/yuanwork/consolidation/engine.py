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

        Stage 2 strategy: prefer the LLM extractor (handles nuance, implicit
        facts, English+Chinese), then fall back to regex on failure or when
        no LLM gateway is configured.
        """
        if self.llm is not None:
            try:
                updated = await self._update_profile_with_llm(episode_id, content)
                if updated:
                    return
            except Exception as e:
                logger.warning(f"LLM profile extraction failed: {e}")

        await self._update_profile_with_regex(episode_id, content)

    async def _update_profile_with_llm(
        self, episode_id: int, content: str
    ) -> bool:
        """Use the LLM to extract stable user facts/preferences.

        Returns True if any profile entry was written.
        """
        from yuanwork.llm.gateway import LLMMessage, LLMTask

        prompt = (
            "你是一个用户画像抽取助手。从下面这条用户发言中抽取稳定的事实/偏好，"
            "输出 JSON 数组（无事实则返回 []）。\n\n"
            "要求：\n"
            "1. 只抽取未来对话仍会相关的稳定事实（职业、偏好、习惯、长期目标、"
            "重要人物、关键日期等），不要抽取一次性提问。\n"
            "2. 每条用 `key` (kebab-case, 短) + `value` (短句) 表示。\n"
            "3. 排除敏感字段（身份证、电话、密码等）。\n\n"
            "输出 JSON 格式：\n"
            '[{"key": "user.role", "value": "律师"}, {"key": "user.preference.like", "value": "喝茶"}]\n\n'
            f"用户发言：\n{content}\n\n"
            "JSON 数组："
        )
        try:
            task = LLMTask(
                task_type="reflection",
                messages=[LLMMessage(role="user", content=prompt)],
                temperature=0.0,
                max_tokens=400,
                json_mode=True,
            )
            response = await self.llm.complete(task)
            import json

            content_str = response.content.strip()
            if content_str.startswith("```"):
                content_str = content_str.split("```")[1]
                if content_str.startswith("json"):
                    content_str = content_str[4:]
                content_str = content_str.strip()
            facts = json.loads(content_str)
            if not isinstance(facts, list):
                return False

            written = False
            for fact in facts:
                if not isinstance(fact, dict):
                    continue
                key = (fact.get("key") or "").strip()
                value = (fact.get("value") or "").strip()
                if not key or not value:
                    continue
                if len(key) > 60 or len(value) > 200:
                    continue
                # Skip sensitive patterns
                low = value.lower()
                if any(t in low for t in ("身份证", "手机号", "密码", "ssn", "password")):
                    continue
                await self.profile.set(
                    key=key,
                    value=value,
                    source_episode_id=episode_id,
                    confidence=0.75,
                )
                written = True
                logger.debug(f"Profile updated (LLM): {key} = {value}")
            return written
        except Exception as e:
            logger.debug(f"LLM profile extraction parse failed: {e}")
            return False

    async def _update_profile_with_regex(
        self, episode_id: int, content: str
    ) -> None:
        """Regex-based fallback: catches a small set of obvious preferences."""
        patterns = [
            ("我喜欢", "user.preference.like"),
            ("我更喜欢", "user.preference.prefer"),
            ("我讨厌", "user.preference.dislike"),
            ("我不喜欢", "user.preference.dislike"),
        ]

        for pattern, key_prefix in patterns:
            if pattern in content:
                idx = content.find(pattern) + len(pattern)
                value = content[idx:idx + 100].split("。")[0].split("，")[0].strip()
                if value:
                    key = f"{key_prefix}.{value[:20]}"
                    await self.profile.set(
                        key=key,
                        value=value,
                        source_episode_id=episode_id,
                        confidence=0.6,
                    )
                    logger.debug(f"Profile updated (regex): {key} = {value}")
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
