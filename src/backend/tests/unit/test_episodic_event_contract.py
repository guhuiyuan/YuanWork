"""The episodic events: what happened, not what was said.

The dialogue lives in the `episodes` table; the interactions live in
the event table. An interaction says something the words do not - that
the user rejected an answer, that a document was removed - and the
consolidation layer reads the second kind.

* AC-F-016 - every interaction is recorded with its time and its
  context, and the events can be found by time, topic and subject
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from yuanwork.db.sqlite import SQLiteStore
from yuanwork.memory.episodic_recorder import (
    EVENT_KINDS,
    EpisodicEvent,
    EpisodicRecorder,
)


@pytest.fixture
async def store(tmp_path: Path):
    s = SQLiteStore(tmp_path / "store.db")
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
def recorder(store) -> EpisodicRecorder:
    return EpisodicRecorder(store)


def event(**kwargs) -> EpisodicEvent:
    base = {"kind": "ask", "session_id": "s1"}
    base.update(kwargs)
    return EpisodicEvent(**base)


# --------------------------------------------------------------------------
# the six kinds
# --------------------------------------------------------------------------


def test_there_are_six_kinds():
    assert len(EVENT_KINDS) == 6
    assert set(EVENT_KINDS) == {
        "ask", "cite", "accept", "reject", "add_file", "remove_file",
    }


async def test_every_kind_can_be_recorded(recorder):
    for kind in EVENT_KINDS:
        event_id = await recorder.record(event(kind=kind))
        assert event_id > 0

    assert await recorder.count() == 6


async def test_an_unknown_kind_is_not_recorded(recorder):
    """A kind nobody agreed on is not a fact."""
    event_id = await recorder.record(event(kind="gossip"))

    assert event_id == 0
    assert await recorder.count() == 0


# --------------------------------------------------------------------------
# what an event carries
# --------------------------------------------------------------------------


async def test_an_event_carries_the_time_it_happened(recorder):
    await recorder.record(event())

    stored = (await recorder.list_events())[0]
    assert stored["occurred_at"]
    # ISO 8601, with the time zone - an event without one cannot be
    # placed in time.
    parsed = datetime.fromisoformat(stored["occurred_at"])
    assert parsed.tzinfo is not None


async def test_an_event_carries_the_session_it_happened_in(recorder):
    await recorder.record(event(session_id="abc"))

    stored = (await recorder.list_events())[0]
    assert stored["session_id"] == "abc"


async def test_an_event_carries_its_subject_and_detail(recorder):
    await recorder.record(
        event(kind="cite", subject="合同.pdf", detail="第 12 页", topic="违约金")
    )

    stored = (await recorder.list_events())[0]
    assert stored["subject"] == "合同.pdf"
    assert stored["detail"] == "第 12 页"
    assert stored["topic"] == "违约金"


async def test_an_event_carries_how_much_it_matters(recorder):
    await recorder.record(event(importance=0.9))

    stored = (await recorder.list_events())[0]
    assert stored["importance"] == pytest.approx(0.9)


async def test_the_kind_is_one_of_the_six(recorder):
    """The database refuses a kind nobody agreed on."""
    import sqlite3

    await recorder.record(event(kind="ask"))
    with pytest.raises(sqlite3.IntegrityError):
        await recorder.sqlite.conn.execute(
            "INSERT INTO episodic_events (occurred_at, session_id, kind) "
            "VALUES ('2026-01-01T00:00:00+00:00', 's', 'gossip')"
        )


# --------------------------------------------------------------------------
# finding them again
# --------------------------------------------------------------------------


async def test_the_events_are_found_by_session(recorder):
    await recorder.record(event(session_id="a"))
    await recorder.record(event(session_id="b"))

    assert len(await recorder.list_events(session_id="a")) == 1
    assert len(await recorder.list_events(session_id="b")) == 1


async def test_the_events_are_found_by_kind(recorder):
    await recorder.record(event(kind="ask"))
    await recorder.record(event(kind="accept"))

    found = await recorder.list_events(kind="accept")
    assert len(found) == 1
    assert found[0]["kind"] == "accept"


async def test_the_events_are_found_by_topic(recorder):
    await recorder.record(event(kind="ask", topic="报销"))
    await recorder.record(event(kind="ask", topic="合同"))

    found = await recorder.list_events(topic="报销")
    assert len(found) == 1
    assert found[0]["topic"] == "报销"


async def test_the_events_are_found_by_subject(recorder):
    await recorder.record(event(kind="cite", subject="合同.pdf"))
    await recorder.record(event(kind="cite", subject="制度.docx"))

    found = await recorder.list_events(subject="合同.pdf")
    assert len(found) == 1
    assert found[0]["subject"] == "合同.pdf"


async def test_the_events_are_found_by_time(recorder):
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=2)).isoformat()
    new = now.isoformat()

    await recorder.record(event(occurred_at=old))
    await recorder.record(event(occurred_at=new))

    recent = await recorder.list_events(since=new)
    assert len(recent) == 1
    assert recent[0]["occurred_at"] == new

    everything = await recorder.list_events(since=old)
    assert len(everything) == 2


async def test_the_filters_combine(recorder):
    await recorder.record(event(kind="cite", subject="a.pdf", topic="报销", session_id="s1"))
    await recorder.record(event(kind="cite", subject="b.pdf", topic="报销", session_id="s2"))

    found = await recorder.list_events(kind="cite", topic="报销", session_id="s2")
    assert len(found) == 1
    assert found[0]["subject"] == "b.pdf"


async def test_the_newest_event_comes_first(recorder):
    now = datetime.now(timezone.utc)
    await recorder.record(event(occurred_at=(now - timedelta(hours=1)).isoformat()))
    await recorder.record(event(occurred_at=now.isoformat()))

    events = await recorder.list_events()
    assert events[0]["occurred_at"] == now.isoformat()


async def test_the_events_are_capped(recorder):
    for i in range(10):
        await recorder.record(event())

    assert len(await recorder.list_events(limit=3)) == 3


# --------------------------------------------------------------------------
# a failure must not break the interaction
# --------------------------------------------------------------------------


async def test_a_failed_write_does_not_raise(recorder):
    """The event is a side note - losing it must not break the chat."""

    class BrokenStore:
        async def add_episodic_event(self, **kwargs):
            raise RuntimeError("the disk is full")

    broken = EpisodicRecorder(BrokenStore())

    event_id = await broken.record(event())

    assert event_id == 0
