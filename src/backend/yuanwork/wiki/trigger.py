"""Wiki Page proactive compile trigger.

Decides when a Wiki page should be compiled from the chunks surfaced during
a chat turn. Stage 2 differentiator: the KB chunks are the "raw notes" and
the Wiki page is the "compiled note" — compiling opportunistically as users
ask related questions makes the wiki self-organizing.

Trigger conditions (ALL must be met):
  1. Evidence has at least MIN_EVIDENCE_CHUNKS chunks.
  2. Evidence spans at least MIN_DISTINCT_DOCS distinct source docs
     (multi-doc synthesis, not single-doc lookup).
  3. Top-1 evidence score >= MIN_TOP_SCORE (don't compile from weak evidence).
  4. Topic is not generic (skip "general" intent / no-topic queries).
  5. Same topic hasn't been compiled within COOLDOWN_SECONDS (throttle).

Errors during compile are logged and swallowed — the trigger is fire-and-
forget and must never fail the chat.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from yuanwork.wiki.page_generator import WikiPageGenerator

if TYPE_CHECKING:
    from yuanwork.db.sqlite import SQLiteStore
    from yuanwork.llm.gateway import LLMGateway


logger = logging.getLogger(__name__)


# ===== Tunables =====
MIN_EVIDENCE_CHUNKS = 4
MIN_DISTINCT_DOCS = 2
MIN_TOP_SCORE = 0.55
COOLDOWN_SECONDS = 30 * 60  # 30 min
GENERIC_INTENTS = {"general", "闲聊", "chitchat", "casual"}


class WikiCompileTrigger:
    """Decides and fires proactive Wiki page compilation."""

    def __init__(
        self,
        sqlite_store: "SQLiteStore",
        llm_gateway: "LLMGateway | None" = None,
        page_generator: WikiPageGenerator | None = None,
        min_evidence_chunks: int = MIN_EVIDENCE_CHUNKS,
        min_distinct_docs: int = MIN_DISTINCT_DOCS,
        min_top_score: float = MIN_TOP_SCORE,
        cooldown_seconds: int = COOLDOWN_SECONDS,
    ):
        self.sqlite = sqlite_store
        self.llm = llm_gateway
        self.min_evidence_chunks = min_evidence_chunks
        self.min_distinct_docs = min_distinct_docs
        self.min_top_score = min_top_score
        self.cooldown_seconds = cooldown_seconds
        self._generator = page_generator or WikiPageGenerator(
            sqlite_store=sqlite_store,
            llm_gateway=llm_gateway,
        )
        # In-process throttle: topic -> last compile timestamp
        self._last_compile: dict[str, float] = {}
        # In-process locks to avoid parallel compiles of the same topic
        self._topic_locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    def should_trigger(
        self,
        evidence: list[dict[str, Any]],
        understanding: dict[str, Any] | None,
    ) -> tuple[bool, str]:
        """Decide whether to trigger a compile for this turn.

        Returns:
            (should_compile, reason). reason is a short tag for tracing.
        """
        if not evidence:
            return False, "no_evidence"
        if len(evidence) < self.min_evidence_chunks:
            return False, "too_few_chunks"

        # Distinct docs in evidence
        doc_ids = {c.get("doc_id") for c in evidence if c.get("doc_id")}
        if len(doc_ids) < self.min_distinct_docs:
            return False, "single_doc"

        # Top score
        scores = [c.get("_score") for c in evidence if c.get("_score") is not None]
        if not scores:
            return False, "no_scores"
        if max(scores) < self.min_top_score:
            return False, "low_top_score"

        # Topic from understanding
        topic = (understanding or {}).get("topic") or ""
        if not topic:
            return False, "no_topic"
        intent = (understanding or {}).get("intent") or ""
        if intent in GENERIC_INTENTS:
            return False, "generic_intent"

        # Throttle: same topic within cooldown
        last = self._last_compile.get(topic)
        if last and (time.monotonic() - last) < self.cooldown_seconds:
            return False, "throttled"

        return True, "ok"

    def extract_topic(self, understanding: dict[str, Any] | None) -> str:
        """Extract a stable topic key from the understanding step."""
        topic = (understanding or {}).get("topic") or ""
        return self._slugify(topic) or "untitled"

    def derive_title(self, understanding: dict[str, Any] | None) -> str:
        """Derive a human-readable title for the Wiki page."""
        topic = (understanding or {}).get("topic") or "未命名主题"
        # Strip very long topics
        title = topic.strip()
        if len(title) > 60:
            title = title[:60].rstrip() + "…"
        return title

    def derive_category(self, understanding: dict[str, Any] | None) -> str:
        """Map intent to a Wiki page category."""
        intent = (understanding or {}).get("intent") or "general"
        mapping = {
            "contract": "contract",
            "law": "law",
            "project": "project",
            "concept": "concept",
            "person": "person",
        }
        return mapping.get(intent, "general")

    async def _get_topic_lock(self, topic_key: str) -> asyncio.Lock:
        """Get or create a per-topic lock (avoids parallel compiles of the same topic)."""
        async with self._locks_guard:
            lock = self._topic_locks.get(topic_key)
            if lock is None:
                lock = asyncio.Lock()
                self._topic_locks[topic_key] = lock
            return lock

    async def maybe_compile(
        self,
        evidence: list[dict[str, Any]],
        understanding: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Decide whether to compile, and compile if so.

        Returns a small dict suitable for the agent trace:
            { "triggered": bool, "reason": str, "page_id": str|None,
              "title": str|None, "error": str|None }
        """
        should, reason = self.should_trigger(evidence, understanding)
        result: dict[str, Any] = {
            "triggered": False,
            "reason": reason,
            "page_id": None,
            "title": None,
            "error": None,
        }
        if not should:
            return result

        topic_key = self.extract_topic(understanding)
        title = self.derive_title(understanding)
        category = self.derive_category(understanding)
        page_id = f"auto/{topic_key}"

        lock = await self._get_topic_lock(topic_key)
        async with lock:
            # Re-check throttle inside the lock (another coroutine may have just compiled)
            last = self._last_compile.get(topic_key)
            if last and (time.monotonic() - last) < self.cooldown_seconds:
                result["reason"] = "throttled_after_lock"
                return result

            try:
                compiled = await self._generator.compile_page(
                    title=title,
                    chunks=evidence,
                    category=category,
                    page_id=page_id,
                )
                if "error" in compiled:
                    result["error"] = compiled["error"]
                else:
                    result["triggered"] = True
                    result["page_id"] = compiled.get("page_id")
                    result["title"] = compiled.get("title")
                    self._last_compile[topic_key] = time.monotonic()
            except Exception as e:
                logger.warning(f"Wiki compile trigger failed: {e}")
                result["error"] = str(e)

        return result

    @staticmethod
    def _slugify(text: str) -> str:
        slug = re.sub(r"[^\w\s-]", "", text.lower())
        slug = re.sub(r"[-\s]+", "-", slug).strip("-")
        return slug[:64]
