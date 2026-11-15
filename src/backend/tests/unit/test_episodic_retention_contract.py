"""TASK-046 — the episodic events do not grow forever.

The events carry what the user asked, so they are both a disk that
grows and a privacy exposure that grows with it. Two limits keep both
bounded: how many events are kept, and how old the oldest kept event
may be. Whatever is over goes - the oldest first - and only a number
comes out, never the events themselves.

* AC-E-017 — the events over 5000 or older than 180 days are evicted,
  and the number evicted is recorded
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from yuanwork.db.sqlite import SQLiteStore
from yuanwork.memory.episodic_recorder import (
    DEFAULT_MAX_AGE_DAYS,
    DEFAULT_MAX_EVENTS,
    EpisodicEvent,
    EpisodicRecorder,
)


@pytest.fixture
async def store(tmp_path: Path):
    s = SQLiteStore(tmp_path / "store.db")
    await s.initialize()
    yield s
    await s.close()


def event(**kwargs) -> EpisodicEvent:
    base = {"kind": "ask", "session_id": "s1"}
    base.update(kwargs)
    return EpisodicEvent(**base)


def ago(days: int) -> str:
    """A moment `days` days ago."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


# --------------------------------------------------------------------------
# the limits
# --------------------------------------------------------------------------


def test_the_limits_are_five_thousand_events_and_one_hundred_and_eighty_days():
    """ADR-038 picked the numbers; they are not up for grabs."""
    assert DEFAULT_MAX_EVENTS == 5000
    assert DEFAULT_MAX_AGE_DAYS == 180


def test_the_recorder_uses_those_limits(store):
    recorder = EpisodicRecorder(store)
    assert recorder.max_events == 5000
    assert recorder.max_age_days == 180


# --------------------------------------------------------------------------
# the eviction
# --------------------------------------------------------------------------


async def test_the_events_over_the_count_limit_are_evicted(store):
    """More events than the limit: the extra ones go."""
    recorder = EpisodicRecorder(store, max_events=3)

    for index in range(5):
        await recorder.record(event(subject=f"主题{index}"))

    assert await recorder.count() == 3


async def test_the_oldest_events_go_first(store):
    """The newest are the ones worth keeping."""
    recorder = EpisodicRecorder(store, max_events=3)

    for index in range(5):
        await recorder.record(event(subject=f"主题{index}"))

    kept = await recorder.list_events(limit=10)
    subjects = {e["subject"] for e in kept}
    assert subjects == {"主题2", "主题3", "主题4"}


async def test_the_events_that_grew_too_old_are_evicted(store):
    """An event from last spring is not worth keeping."""
    recorder = EpisodicRecorder(store, max_age_days=180)

    await recorder.record(event(subject="去年冬天", occurred_at=ago(200)))

    assert await recorder.count() == 0


async def test_an_event_inside_the_limits_stays(store):
    """A recent event is not touched."""
    recorder = EpisodicRecorder(store, max_age_days=180)

    await recorder.record(event(subject="上周", occurred_at=ago(10)))

    assert await recorder.count() == 1


async def test_nothing_is_evicted_while_the_events_are_within_the_limits(store):
    recorder = EpisodicRecorder(store, max_events=10, max_age_days=180)

    for index in range(5):
        await recorder.record(event(subject=f"主题{index}"))

    assert await recorder.count() == 5
    assert await recorder.total_pruned() == 0


# --------------------------------------------------------------------------
# the trigger
# --------------------------------------------------------------------------


async def test_the_cleanup_runs_after_an_event_is_recorded(store):
    """The event is recorded first - the cleanup is not instead of it."""
    recorder = EpisodicRecorder(store, max_events=2)

    await recorder.record(event(subject="一"))
    await recorder.record(event(subject="二"))
    assert await recorder.count() == 2

    # The third one tips it over the limit.
    await recorder.record(event(subject="三"))

    assert await recorder.count() == 2
    assert await recorder.total_pruned() == 1


async def test_the_cleanup_runs_when_an_event_is_too_old(store):
    """The age limit is a trigger of its own."""
    recorder = EpisodicRecorder(store, max_age_days=180)

    await recorder.record(event(subject="很久以前", occurred_at=ago(400)))

    assert await recorder.count() == 0
    assert await recorder.total_pruned() == 1


async def test_an_explicit_cleanup_can_be_asked_for(store):
    """The cleanup can also be run on its own."""
    recorder = EpisodicRecorder(store, max_events=10)

    for index in range(5):
        await recorder.record(event(subject=f"主题{index}"))

    recorder.max_events = 2
    pruned = await recorder.prune()

    assert pruned == 3
    assert await recorder.count() == 2


# --------------------------------------------------------------------------
# the count of what went
# --------------------------------------------------------------------------


async def test_the_number_of_evicted_events_is_recorded(store):
    """The number is written down - so it can be said out loud."""
    recorder = EpisodicRecorder(store, max_events=2)

    for index in range(5):
        await recorder.record(event(subject=f"主题{index}"))

    stats = await recorder.prune_stats()
    assert stats
    assert sum(row["pruned_count"] for row in stats) == 3
    assert await recorder.total_pruned() == 3


async def test_the_evicted_events_are_not_kept_anywhere(store):
    """A copy of what was cleaned up would undo the point of cleaning
    it up."""
    recorder = EpisodicRecorder(store, max_events=2)

    for index in range(4):
        await recorder.record(event(subject=f"秘密{index}", detail=f"原话{index}"))

    for row in await recorder.prune_stats():
        # A number and a moment - nothing else.
        assert set(row) == {"id", "pruned_at", "pruned_count"}

    kept = await recorder.list_events(limit=10)
    # The two that were evicted are gone - with their words.
    assert {e["subject"] for e in kept} == {"秘密2", "秘密3"}
    assert {e["detail"] for e in kept} == {"原话2", "原话3"}


# --------------------------------------------------------------------------
# when it fails
# --------------------------------------------------------------------------


class BrokenCleanupStore:
    """A store whose cleanup fails."""

    def __init__(self, inner: Any):
        self.inner = inner

    async def add_episodic_event(self, **kwargs):
        return await self.inner.add_episodic_event(**kwargs)

    async def count_episodic_events(self):
        return await self.inner.count_episodic_events()

    async def oldest_episodic_event(self):
        return await self.inner.oldest_episodic_event()

    async def prune_episodic_events(self, **kwargs):
        raise RuntimeError("the disk is full")

    async def record_episodic_prune(self, *args, **kwargs):
        return await self.inner.record_episodic_prune(*args, **kwargs)


async def test_a_cleanup_that_fails_only_writes_a_log(store):
    """A cleanup that fails is a cleanup that is late - the event is
    still recorded."""
    recorder = EpisodicRecorder(BrokenCleanupStore(store), max_events=1)

    first = await recorder.record(event(subject="一"))
    second = await recorder.record(event(subject="二"))

    assert first > 0
    assert second > 0
    # Both events are still there - nothing was evicted, nothing broke.
    assert await recorder.count() == 2


async def test_a_check_that_fails_does_not_break_the_record(store):
    """The trigger check is a nicety too."""

    class BrokenCountStore(BrokenCleanupStore):
        async def count_episodic_events(self):
            raise RuntimeError("the store is gone")

    recorder = EpisodicRecorder(BrokenCountStore(store))

    event_id = await recorder.record(event())

    assert event_id > 0
