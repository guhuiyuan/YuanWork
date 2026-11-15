"""Consolidation Engine - the integration layer.

Responsibilities:
- Write control: new conversations → profile updates
- Conflict resolution: LLM training knowledge vs KB
- Reflection: periodic deduplication, summarization, profile updates
- Induction: the recent interactions → entities, rules, page updates
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from yuanwork.consolidation.induction import (
    DEFAULT_WINDOW,
    InducedEntity,
    InducedPagePoint,
    InducedRule,
    Inducer,
)
from yuanwork.wiki.entity_extractor import entity_id_for

if TYPE_CHECKING:
    from yuanwork.db.sqlite import SQLiteStore
    from yuanwork.memory.episodic import EpisodicMemory
    from yuanwork.memory.profile import ProfileStore
    from yuanwork.memory.semantic import SemanticMemory
    from yuanwork.llm.gateway import LLMGateway


logger = logging.getLogger(__name__)


#: How often the background watcher looks at the idle clock.
IDLE_CHECK_INTERVAL_SECONDS = 60.0


class UnknownRule(ValueError):
    """There is no such rule."""

    def __init__(self, entity_id: str):
        super().__init__(f"No such rule: {entity_id}")
        self.entity_id = entity_id


class RuleNotPending(ValueError):
    """The rule is not waiting for the user."""

    def __init__(self, entity_id: str):
        super().__init__(f"The rule is not waiting for review: {entity_id}")
        self.entity_id = entity_id


async def approve_rule(
    sqlite: "SQLiteStore", entity_id: str
) -> dict[str, Any]:
    """The user confirms a rule - it takes effect from now on.

    A rule is a conclusion drawn across interactions. Until the user
    says so it waits, and a waiting rule does not count as evidence
    (ADR-039). The events it was induced from stay with it: a
    conclusion that cannot be traced back to what happened is not a
    conclusion, it is a guess.

    Raises:
        UnknownRule - there is no such rule, or it is not a rule.
        RuleNotPending - the rule is not waiting for the user.
    """
    entity = await sqlite.get_entity(entity_id)
    if entity is None:
        raise UnknownRule(entity_id)
    if (entity.get("properties") or {}).get("kind") != "rule":
        raise UnknownRule(entity_id)
    if entity.get("status") != "pending":
        raise RuleNotPending(entity_id)

    await sqlite.set_entity_status(entity_id, "active")
    approved = await sqlite.get_entity(entity_id)
    return approved or {}


class ConsolidationEngine:
    """Integrates episodic memory with semantic memory."""

    def __init__(
        self,
        episodic_memory: "EpisodicMemory",
        semantic_memory: "SemanticMemory",
        profile_store: "ProfileStore",
        sqlite_store: "SQLiteStore",
        llm_gateway: "LLMGateway | None" = None,
        trigger_turns: int | None = None,
        trigger_idle_minutes: int | None = None,
        induction_window: int | None = None,
        inducer: Inducer | None = None,
        clock: Callable[[], float] | None = None,
        idle_check_interval: float = IDLE_CHECK_INTERVAL_SECONDS,
    ):
        self.episodic = episodic_memory
        self.semantic = semantic_memory
        self.profile = profile_store
        self.sqlite = sqlite_store
        self.llm = llm_gateway
        # When the integration may fire: after so many turns, or after
        # so long without one (ADR-015).
        from yuanwork.config import get_settings

        settings = get_settings()
        self.trigger_turns = (
            settings.reflection_trigger_turns if trigger_turns is None else trigger_turns
        )
        self.trigger_idle_minutes = (
            settings.reflection_trigger_idle_minutes
            if trigger_idle_minutes is None
            else trigger_idle_minutes
        )
        # How many recent interactions are read for one round of
        # induction. The window is what the induction can see at all -
        # older interactions have nothing to add to it (ADR-032).
        self.induction_window = (
            induction_window if induction_window is not None else DEFAULT_WINDOW
        )
        self.inducer = inducer or Inducer(
            llm_gateway=llm_gateway, window=self.induction_window
        )
        # The trigger state (ADR-015). It lives in this process: a
        # round is a batch job of this process, not a promise that
        # survives a restart.
        self._clock = clock or time.monotonic
        self.idle_check_interval = idle_check_interval
        self._turns_since_last = 0
        self._last_activity = self._clock()
        # The background rounds are kept here so they are not garbage
        # collected while they run.
        self._background_tasks: set[asyncio.Task] = set()
        # Two rounds never run at the same time.
        self._round_lock = asyncio.Lock()

    def should_trigger(
        self,
        turns_since_last: int,
        minutes_since_last: float,
    ) -> bool:
        """Whether the integration may fire now.

        Either condition is enough - enough turns, or enough quiet time
        (ADR-015).
        """
        if turns_since_last >= self.trigger_turns:
            return True
        if minutes_since_last >= self.trigger_idle_minutes:
            return True
        return False

    # --------------------------------------------------------------------------
    # the three triggers (ADR-015)
    # --------------------------------------------------------------------------

    def note_activity(self) -> None:
        """A turn happened.

        The idle clock restarts and the turn counter goes up - the two
        automatic triggers watch these.
        """
        self._turns_since_last += 1
        self._last_activity = self._clock()

    def minutes_since_activity(self) -> float:
        """How long nobody has asked anything."""
        return (self._clock() - self._last_activity) / 60.0

    def turns_since_last(self) -> int:
        """How many turns have passed since the last round."""
        return self._turns_since_last

    def pending_trigger(self) -> str | None:
        """Which automatic trigger fires now.

        "turns" - enough turns have piled up; "idle" - nobody has asked
        anything for a while; None - nothing fires yet.
        """
        if self._turns_since_last >= self.trigger_turns:
            return "turns"
        if self.minutes_since_activity() >= self.trigger_idle_minutes:
            return "idle"
        return None

    def trigger_state(self) -> dict[str, Any]:
        """How close the next round is - for the interface."""
        return {
            "turns_since_last": self._turns_since_last,
            "turns_threshold": self.trigger_turns,
            "minutes_since_last": round(self.minutes_since_activity(), 2),
            "idle_minutes_threshold": self.trigger_idle_minutes,
            "pending_trigger": self.pending_trigger(),
        }

    async def maybe_consolidate(self) -> dict[str, Any] | None:
        """Run a round if one of the automatic triggers fires.

        Returns the stats of the round, or None when no trigger fired.
        """
        reason = self.pending_trigger()
        if reason is None:
            return None
        logger.info(f"The {reason} trigger fires a consolidation round")
        return await self.consolidate(reason)

    def schedule_consolidation(self) -> None:
        """Run a round in the background, if a trigger says so.

        The round is a batch job - it must not sit on the path of a
        question.
        """
        try:
            task = asyncio.ensure_future(self.maybe_consolidate())
        except RuntimeError as e:
            # No running loop - nothing to schedule the round on.
            logger.debug(f"Could not schedule a consolidation round: {e}")
            return
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def watch_idle(
        self, interval_seconds: float | None = None
    ) -> None:
        """Watch the idle clock.

        Somebody has to notice that the user stopped asking questions.
        Without this, the idle trigger would only ever be noticed by
        the next question - which is not what "idle" means.
        """
        interval = (
            interval_seconds
            if interval_seconds is not None
            else self.idle_check_interval
        )
        while True:
            await asyncio.sleep(interval)
            try:
                await self.maybe_consolidate()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - the watcher is not a chat
                logger.warning(f"The idle consolidation failed: {e}")

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
            # A turn happened: the turn counter and the idle clock
            # follow it (ADR-015).
            self.note_activity()
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
        topic: str | None = None,
        citation: str | None = None,
    ) -> int:
        """Log an LLM-vs-KB conflict.

        ``topic`` says what the divergence is about and ``citation``
        says where the knowledge base's side comes from.
        """
        return await self.sqlite.add_conflict(
            conflict_type="llm_vs_kb",
            llm_answer=llm_answer,
            kb_evidence=kb_evidence,
            episode_id=episode_id,
            knowledge_chunk_id=knowledge_chunk_id,
            resolution=resolution,
            topic=topic,
            citation=citation,
        )

    async def run(self, reason: str = "manual", force: bool = False) -> dict[str, Any]:
        """Run consolidation (reflection).

        ``force`` is accepted and ignored: the trigger decides *when*
        the round happens, not *what* the round does (ADR-015).

        Returns stats dict.
        """
        return await self.consolidate(reason)

    async def consolidate(self, reason: str = "manual") -> dict[str, Any]:
        """Consolidate: induce entities, rules and page updates.

        The recent interactions are read, the model is asked what they
        add up to, and the answers are written into the semantic
        memory: the entities into the graph, the rules into the graph
        as *waiting for the user* (ADR-039), the key points into the
        pages they belong to.

        One induction that fails is skipped - the round goes on
        (design §3.1).

        Two rounds never run at the same time: the turn trigger and
        the idle watcher could both notice the same reason to
        consolidate.
        """
        async with self._round_lock:
            return await self._consolidate(reason)

    async def _consolidate(self, reason: str) -> dict[str, Any]:
        """One round of consolidation."""
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

        # 5. The induction: what the recent interactions add up to.
        events = await self._recent_events()
        induced = await self.inducer.induce(events)

        entities_induced = 0
        rules_induced = 0
        page_points_induced = 0
        induction_failures = 0

        for entity in induced.entities:
            try:
                await self._write_entity(entity)
                entities_induced += 1
            except Exception as e:  # noqa: BLE001 - one bad entity is not a broken round
                induction_failures += 1
                logger.warning(f"Could not write the induced entity: {e}")

        for rule in induced.rules:
            try:
                await self._write_rule(rule)
                rules_induced += 1
            except Exception as e:  # noqa: BLE001 - one bad rule is not a broken round
                induction_failures += 1
                logger.warning(f"Could not write the induced rule: {e}")

        for point in induced.page_points:
            try:
                await self._write_page_point(point)
                page_points_induced += 1
            except Exception as e:  # noqa: BLE001 - one bad point is not a broken round
                induction_failures += 1
                logger.warning(f"Could not write the induced page point: {e}")

        duration_ms = (datetime.now() - start).total_seconds() * 1000

        # The round is the consolidation: the turn counter starts over
        # and the idle clock restarts, so the next round waits for the
        # next reason.
        self._turns_since_last = 0
        self._last_activity = self._clock()

        result = {
            "episodes_processed": episodes_processed,
            "duplicates_merged": duplicates_merged,
            "profile_updates": profile_updates,
            "conflicts_archived": conflicts_archived,
            "events_read": len(events),
            "entities_induced": entities_induced,
            "rules_induced": rules_induced,
            "page_points_induced": page_points_induced,
            "induction_failures": induction_failures,
            "induction_source": induced.source,
            "duration_ms": duration_ms,
        }

        logger.info(
            f"Consolidation ({reason}): {episodes_processed} episodes, "
            f"{duplicates_merged} merged, {profile_updates} profile updates, "
            f"{entities_induced} entities, {rules_induced} rules, "
            f"{page_points_induced} page points in {duration_ms:.0f}ms"
        )

        return result

    # --------------------------------------------------------------------------
    # the induction
    # --------------------------------------------------------------------------

    # --------------------------------------------------------------------------
    # the review of the rules (ADR-039)
    # --------------------------------------------------------------------------

    async def list_pending_rules(self, limit: int = 50) -> list[dict[str, Any]]:
        """The rules that are waiting for the user.

        These are the ones the user has to look at: a rule is a
        conclusion drawn across interactions, and it does not count as
        evidence until the user says so.
        """
        entities = await self.sqlite.list_entities(status="pending", limit=limit)
        return [
            entity
            for entity in entities
            if (entity.get("properties") or {}).get("kind") == "rule"
        ]

    async def list_active_rules(self, limit: int = 50) -> list[dict[str, Any]]:
        """The rules that count as evidence.

        A rule that is still waiting is not in here - it has not been
        confirmed by the user yet.
        """
        entities = await self.sqlite.list_entities(status="active", limit=limit)
        return [
            entity
            for entity in entities
            if (entity.get("properties") or {}).get("kind") == "rule"
        ]

    async def approve(self, entity_id: str) -> dict[str, Any]:
        """The user confirms a rule - it takes effect from now on.

        The events it was induced from stay with it (ADR-030).
        """
        return await approve_rule(self.sqlite, entity_id)

    async def provenance(self, event_id: int) -> dict[str, Any]:
        """What was concluded from one interaction.

        The other end of the trace (ADR-030): a conclusion carries the
        interactions it came from; this carries the conclusions one
        interaction led to - the entities, the rules and the key
        points, with the interactions behind them.
        """
        event = await self.sqlite.get_episodic_event(event_id)

        entities = await self.sqlite.list_entities_by_source_event(event_id)
        page_points = await self.sqlite.list_page_updates_by_source_event(event_id)

        rules = [
            entity
            for entity in entities
            if (entity.get("properties") or {}).get("kind") == "rule"
        ]
        induced = [
            entity
            for entity in entities
            if (entity.get("properties") or {}).get("kind") != "rule"
        ]

        # The interactions behind the conclusions - the whole chain, so
        # a conclusion can be read together with what it rests on.
        source_ids: list[int] = []
        for record in list(entities) + list(page_points):
            for source_id in record.get("source_event_ids") or []:
                if source_id not in source_ids:
                    source_ids.append(source_id)
        source_events: list[dict[str, Any]] = []
        for source_id in source_ids:
            event_row = await self.sqlite.get_episodic_event(source_id)
            if event_row is not None:
                source_events.append(event_row)

        return {
            "event": event,
            "entities": induced,
            "rules": rules,
            "page_points": page_points,
            "source_events": source_events,
        }

    async def _recent_events(self) -> list[dict[str, Any]]:
        """The recent interactions to induce from, oldest first.

        The events are read newest first; the model reads them in the
        order they happened.
        """
        events = await self.sqlite.list_episodic_events(limit=self.induction_window)
        return list(reversed(events))

    async def _write_entity(self, entity: InducedEntity) -> None:
        """Write one induced entity into the graph.

        An entity is a thing that exists in the documents, so it takes
        effect on its own (ADR-039).
        """
        await self.sqlite.upsert_entity(
            entity_id=entity_id_for(entity.name),
            name=entity.name,
            entity_type=entity.type,
            aliases=entity.aliases,
            properties={"kind": "entity"},
            source_event_ids=entity.event_ids,
            status="active",
        )

    async def _write_rule(self, rule: InducedRule) -> None:
        """Write one induced rule - it waits for the user.

        A rule is a conclusion drawn across events, so it does not
        take effect until the user confirms it, and until then it does
        not count as evidence (ADR-039).
        """
        await self.sqlite.upsert_entity(
            entity_id=f"rule-{entity_id_for(rule.statement)}",
            name=rule.statement,
            entity_type="concept",
            aliases=[],
            properties={"kind": "rule", "statement": rule.statement},
            source_event_ids=rule.event_ids,
            status="pending",
        )

    async def _write_page_point(self, point: InducedPagePoint) -> None:
        """Write one induced key point for the page it belongs to."""
        page_id = point.page_id or await self._page_id_for_topic(point.topic)
        await self.sqlite.add_page_update_candidate(
            page_id=page_id,
            point=point.point,
            topic=point.topic,
            source_event_ids=point.event_ids,
        )

    async def _page_id_for_topic(self, topic: str) -> str:
        """The page a topic belongs to, if there is one already."""
        if not topic:
            return ""
        slug = entity_id_for(topic)
        pages = await self.sqlite.list_wiki_pages(limit=200)
        for page in pages:
            if page["id"] == slug or page["title"] == topic:
                return page["id"]
        return ""
