"""The episodic recorder - what happened, not what was said.

The dialogue lives in the `episodes` table. This records the
*interactions*: asking a question, citing a document, accepting or
rejecting an answer, adding or removing a file. They are different
facts, and the consolidation layer needs the second kind - "the user
rejected the answer about the reimbursement standard" says something
about how the knowledge base is used that the words of the answer do
not (ADR-013).

Only interactions inside the application are recorded. Nothing from
outside it is collected - that is what makes the "everything stays on
this machine" promise keepable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from yuanwork.db.sqlite import SQLiteStore

logger = logging.getLogger(__name__)


#: The six interactions between the user and the knowledge base.
#: 提问 / 引用 / 采纳 / 否定 / 增文件 / 删文件
EVENT_KINDS = ("ask", "cite", "accept", "reject", "add_file", "remove_file")

#: What the kinds are called in the interface.
EVENT_KIND_LABELS = {
    "ask": "提问",
    "cite": "引用",
    "accept": "采纳",
    "reject": "否定",
    "add_file": "增文件",
    "remove_file": "删文件",
}

#: How many events are kept (ADR-038).
#:
#: The events grow with every interaction, and they carry what the
#: user asked - a disk that grows and a privacy exposure that grows
#: with it. Five thousand events is far more than the consolidation
#: layer ever reads.
DEFAULT_MAX_EVENTS = 5000

#: How old the oldest kept event may be (ADR-038).
#:
#: The count limit catches a burst; the age limit catches the slow
#: accumulation of a machine that is barely used.
DEFAULT_MAX_AGE_DAYS = 180


@dataclass
class EpisodicEvent:
    """One interaction."""

    kind: str
    session_id: str
    subject: str = ""
    detail: str = ""
    topic: str = ""
    importance: float = 0.5
    occurred_at: str = field(default_factory=lambda: _now())


def _now() -> str:
    """The current moment, with the time zone - an event without one
    cannot be placed in time."""
    return datetime.now(timezone.utc).isoformat()


class EpisodicRecorder:
    """Records the interactions and hands them back for the memory
    engineering layer to read."""

    def __init__(
        self,
        sqlite_store: SQLiteStore,
        max_events: int = DEFAULT_MAX_EVENTS,
        max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    ):
        self.sqlite = sqlite_store
        #: How many events are kept (ADR-038).
        self.max_events = max_events
        #: How old the oldest kept event may be (ADR-038).
        self.max_age_days = max_age_days

    async def record(self, event: EpisodicEvent) -> int:
        """Record one interaction.

        A failure is logged and swallowed: an interaction that cannot
        be written down must not break the interaction itself.
        """
        if event.kind not in EVENT_KINDS:
            logger.warning(
                f"Unknown episodic event kind {event.kind!r}; not recorded"
            )
            return 0

        try:
            event_id = await self.sqlite.add_episodic_event(
                occurred_at=event.occurred_at or _now(),
                session_id=event.session_id,
                kind=event.kind,
                subject=event.subject,
                detail=event.detail,
                topic=event.topic,
                importance=event.importance,
            )
        except Exception as e:  # noqa: BLE001 - a lost event is not a broken chat
            logger.warning(f"Could not record the episodic event: {e}")
            return 0

        # The cleanup runs after the event was recorded - never instead
        # of it (ADR-038).
        await self._prune_if_needed()
        return event_id

    async def prune(self) -> int:
        """Evict the events that are over the limits.

        The oldest go first: whatever is over the count limit or over
        the age limit goes. Only the number of evicted events comes
        out - the events themselves are gone, and no copy of them is
        kept (ADR-038).
        """
        try:
            pruned = await self.sqlite.prune_episodic_events(
                max_count=self.max_events,
                cutoff=self._cutoff(),
            )
        except Exception as e:  # noqa: BLE001 - a late cleanup is not a broken one
            logger.warning(f"The episodic event cleanup failed: {e}")
            return 0

        if pruned:
            try:
                await self.sqlite.record_episodic_prune(pruned)
            except Exception as e:  # noqa: BLE001 - the number is a nicety
                logger.warning(f"The cleanup count was not written down: {e}")
        return pruned

    async def prune_stats(self, limit: int = 20) -> list[dict[str, Any]]:
        """The cleanups that happened - a number each, never the events."""
        return await self.sqlite.list_episodic_prune_stats(limit=limit)

    async def total_pruned(self) -> int:
        """How many events were evicted in all."""
        return await self.sqlite.total_pruned_events()

    async def _prune_if_needed(self) -> int:
        """Prune when the events are over the limits.

        The trigger: more events than the limit, or an event that has
        grown too old.
        """
        try:
            count = await self.sqlite.count_episodic_events()
            if count > self.max_events:
                return await self.prune()

            oldest = await self.sqlite.oldest_episodic_event()
            if oldest is not None and self._is_too_old(
                str(oldest.get("occurred_at") or "")
            ):
                return await self.prune()
        except Exception as e:  # noqa: BLE001 - the check must not break a record
            logger.warning(f"The episodic event cleanup check failed: {e}")
        return 0

    def _cutoff(self) -> str:
        """The moment before which an event has grown too old."""
        return (
            datetime.now(timezone.utc) - timedelta(days=self.max_age_days)
        ).isoformat()

    def _is_too_old(self, occurred_at: str) -> bool:
        """Whether an event has grown too old."""
        if not occurred_at:
            return False
        try:
            moment = datetime.fromisoformat(occurred_at)
        except ValueError:
            # A timestamp nobody can read is not one we throw away.
            return False
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment < datetime.now(timezone.utc) - timedelta(
            days=self.max_age_days
        )

    async def list_events(
        self,
        session_id: str | None = None,
        kind: str | None = None,
        topic: str | None = None,
        subject: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """The events, by time, topic or subject - anything can be
        left out."""
        return await self.sqlite.list_episodic_events(
            session_id=session_id,
            kind=kind,
            topic=topic,
            subject=subject,
            since=since,
            until=until,
            limit=limit,
        )

    async def count(self) -> int:
        """How many events are stored."""
        return await self.sqlite.count_episodic_events()
