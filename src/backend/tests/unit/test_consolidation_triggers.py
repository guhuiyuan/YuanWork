"""The three ways a consolidation round starts (ADR-015).

* idle - nobody has asked anything for a while
* turns - enough turns have piled up
* manual - the user asks for it

* AC-F-018 - the three conditions each trigger a round
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from yuanwork.api import deps
from yuanwork.api.routes import memory as memory_routes
from yuanwork.consolidation.engine import ConsolidationEngine
from yuanwork.db.sqlite import SQLiteStore
from yuanwork.memory.episodic import EpisodicMemory
from yuanwork.memory.profile import ProfileStore


# --------------------------------------------------------------------------
# the fixtures
# --------------------------------------------------------------------------


@pytest.fixture
async def store(tmp_path: Path):
    s = SQLiteStore(tmp_path / "store.db")
    await s.initialize()
    yield s
    await s.close()


class FakeLLM:
    """A model that answers the induction and nothing else."""

    def __init__(self, answer: str | None = None):
        self.answer = answer or json.dumps({
            "entities": [{"name": "华润", "type": "org"}],
        })
        self.tasks: list[Any] = []
        #: The calls that were the induction - the profile extraction
        #: asks the model something else.
        self.induction_calls = 0

    async def complete(self, task):
        self.tasks.append(task)
        prompt = task.messages[0].content
        if "记忆归纳助手" not in prompt:
            return type("R", (), {"content": "[]"})()
        self.induction_calls += 1
        return type("R", (), {"content": self.answer})()


class FakeClock:
    """A clock the test moves by hand."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance_minutes(self, minutes: float) -> None:
        self.now += minutes * 60

    def advance_seconds(self, seconds: float) -> None:
        self.now += seconds


class OffsetClock:
    """A clock that runs, but can be pushed forward."""

    def __init__(self):
        self.offset = 0.0

    def __call__(self) -> float:
        return time.monotonic() + self.offset

    def advance_minutes(self, minutes: float) -> None:
        self.offset += minutes * 60


def make_engine(
    store,
    llm=None,
    clock=None,
    trigger_turns: int = 10,
    trigger_idle_minutes: int = 30,
) -> ConsolidationEngine:
    return ConsolidationEngine(
        episodic_memory=EpisodicMemory(store),
        semantic_memory=None,  # type: ignore[arg-type]
        profile_store=ProfileStore(store),
        sqlite_store=store,
        llm_gateway=llm,
        trigger_turns=trigger_turns,
        trigger_idle_minutes=trigger_idle_minutes,
        clock=clock,
    )


async def turn(engine, store, question: str = "差旅住宿费的上限是多少？") -> None:
    """One turn of a chat: an interaction recorded, and the question.

    This is what the agent does at the end of a turn - the question
    goes into the dialogue, the interaction goes into the event log.
    The round reads the second one; the trigger counts the first.
    """
    from datetime import datetime, timezone

    await store.add_episodic_event(
        occurred_at=datetime.now(timezone.utc).isoformat(),
        session_id="s1",
        kind="ask",
        subject="",
        detail=question,
        topic="差旅报销",
        importance=0.5,
    )
    await engine.record_episode("s1", "user", question)


@pytest.fixture
async def with_event(store):
    """One interaction to induce from."""
    from datetime import datetime, timezone

    await store.add_episodic_event(
        occurred_at=datetime.now(timezone.utc).isoformat(),
        session_id="s1",
        kind="ask",
        subject="",
        detail="差旅住宿费的上限是多少？",
        topic="差旅报销",
        importance=0.5,
    )
    return store


# --------------------------------------------------------------------------
# the turns trigger
# --------------------------------------------------------------------------


async def test_nothing_fires_before_the_threshold(store):
    engine = make_engine(store, FakeLLM(), trigger_turns=3, trigger_idle_minutes=1000)
    await engine.record_episode("s1", "user", "第一个问题")

    assert await engine.maybe_consolidate() is None


async def test_the_turns_trigger_fires_a_round(store):
    engine = make_engine(store, FakeLLM(), trigger_turns=3, trigger_idle_minutes=1000)
    for i in range(3):
        await turn(engine, store, f"第 {i} 个问题")

    result = await engine.maybe_consolidate()

    assert result is not None
    assert result["entities_induced"] == 1


async def test_the_turn_counter_starts_over_after_a_round(store):
    engine = make_engine(store, FakeLLM(), trigger_turns=3, trigger_idle_minutes=1000)
    for i in range(3):
        await engine.record_episode("s1", "user", f"第 {i} 个问题")
    await engine.maybe_consolidate()

    assert engine.turns_since_last() == 0

    await engine.record_episode("s1", "user", "又一个问题")
    assert await engine.maybe_consolidate() is None


async def test_the_assistant_turn_is_not_a_turn(store):
    """A turn is something the user did."""
    engine = make_engine(store, FakeLLM(), trigger_turns=2, trigger_idle_minutes=1000)
    await engine.record_episode("s1", "assistant", "回答")

    assert engine.turns_since_last() == 0


async def test_the_trigger_state_says_which_one_is_coming(store):
    engine = make_engine(store, FakeLLM(), trigger_turns=3, trigger_idle_minutes=1000)

    state = engine.trigger_state()

    assert state["turns_since_last"] == 0
    assert state["turns_threshold"] == 3
    assert state["idle_minutes_threshold"] == 1000
    assert state["pending_trigger"] is None


async def test_two_rounds_do_not_run_at_the_same_time(store):
    """The turn trigger and the idle watcher could notice the same
    reason to consolidate."""
    engine = make_engine(store, FakeLLM(), trigger_turns=1, trigger_idle_minutes=1000)
    running = 0
    overlaps = 0

    async def slow_round(reason):
        nonlocal running, overlaps
        running += 1
        if running > 1:
            overlaps += 1
        await asyncio.sleep(0.05)
        running -= 1
        return {}

    engine._consolidate = slow_round  # type: ignore[assignment]

    await asyncio.gather(engine.consolidate(), engine.consolidate())

    assert overlaps == 0


# --------------------------------------------------------------------------
# the idle trigger
# --------------------------------------------------------------------------


async def test_the_idle_trigger_fires_a_round(store):
    clock = FakeClock()
    engine = make_engine(
        store, FakeLLM(), clock=clock, trigger_turns=1000, trigger_idle_minutes=30
    )
    await turn(engine, store)

    assert await engine.maybe_consolidate() is None

    clock.advance_minutes(31)
    result = await engine.maybe_consolidate()

    assert result is not None
    assert result["entities_induced"] == 1


async def test_a_turn_restarts_the_idle_clock(store):
    clock = FakeClock()
    engine = make_engine(
        store, FakeLLM(), clock=clock, trigger_turns=1000, trigger_idle_minutes=30
    )
    clock.advance_minutes(20)
    await engine.record_episode("s1", "user", "一个问题")

    clock.advance_minutes(20)

    assert engine.minutes_since_activity() == 20
    assert await engine.maybe_consolidate() is None


async def test_the_idle_watcher_notices_the_idle_time(store):
    """Somebody has to look at the clock - the next question would
    notice it too late."""
    clock = OffsetClock()
    llm = FakeLLM()
    engine = make_engine(
        store, llm, clock=clock, trigger_turns=10**6, trigger_idle_minutes=0.02
    )
    await turn(engine, store)
    clock.advance_minutes(1)

    watcher = asyncio.create_task(engine.watch_idle(interval_seconds=0.01))
    try:
        await asyncio.sleep(0.2)
    finally:
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass

    assert llm.induction_calls == 1


async def test_the_idle_watcher_survives_a_broken_round(store):
    """A round that fails does not take the watcher down with it."""
    clock = OffsetClock()
    engine = make_engine(
        store, FakeLLM(), clock=clock, trigger_turns=10**6, trigger_idle_minutes=0.02
    )
    clock.advance_minutes(1)

    async def boom():
        raise RuntimeError("这一轮炸了")

    engine.consolidate = boom  # type: ignore[assignment]

    watcher = asyncio.create_task(engine.watch_idle(interval_seconds=0.01))
    try:
        await asyncio.sleep(0.1)
    finally:
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass

    assert watcher.done() or watcher.cancelled()


# --------------------------------------------------------------------------
# the manual trigger
# --------------------------------------------------------------------------


@pytest.fixture
def client(store) -> TestClient:
    deps.set_sqlite_store(store)
    deps.set_episodic_memory(EpisodicMemory(store))
    deps.set_profile_store(ProfileStore(store))
    app = FastAPI()
    app.include_router(memory_routes.router, prefix="/api/v1")
    yield TestClient(app)
    deps.reset_dependencies()


def test_the_user_can_ask_for_a_round(client, store):
    deps.set_consolidation_engine(make_engine(store, FakeLLM()))

    resp = client.post("/api/v1/memory/consolidate", json={"reason": "manual"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["triggered"] is True
    assert body["reason"] == "manual"


def test_the_manual_round_runs_on_the_live_engine(client, with_event):
    """The engine the app already has - the one with the model.

    A second engine would have no model, and the induction would fall
    back to pattern matching: no rules, ever.
    """
    llm = FakeLLM()
    deps.set_consolidation_engine(
        make_engine(with_event, llm)
    )

    resp = client.post("/api/v1/memory/consolidate", json={"reason": "manual"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["entities_induced"] == 1
    assert llm.induction_calls, "the model was never asked"


def test_the_manual_round_reports_what_it_induced(client, store):
    deps.set_consolidation_engine(make_engine(store, FakeLLM()))

    body = client.post("/api/v1/memory/consolidate", json={"reason": "manual"}).json()

    assert "events_read" in body
    assert "entities_induced" in body
    assert "rules_induced" in body
    assert "page_points_induced" in body


def test_the_manual_trigger_says_so_when_the_memory_layer_is_off(client):
    """The layer can be switched off - the round has nowhere to run."""
    resp = client.post("/api/v1/memory/consolidate", json={"reason": "manual"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["triggered"] is False
    assert "disabled" in body["reason"]


def test_the_force_flag_is_accepted_and_ignored(client, store):
    deps.set_consolidation_engine(make_engine(store, FakeLLM()))

    without = client.post("/api/v1/memory/consolidate", json={"reason": "manual"}).json()
    with_force = client.post(
        "/api/v1/memory/consolidate", json={"reason": "manual", "force": True}
    ).json()

    assert with_force["entities_induced"] == without["entities_induced"]


# --------------------------------------------------------------------------
# the wiring
# --------------------------------------------------------------------------


async def test_the_chat_turn_schedules_a_round(store):
    """The turn is what the trigger watches."""
    from yuanwork.orchestrator.agent import AgentLoop

    deps.set_sqlite_store(store)

    class RecordingConsolidation:
        def __init__(self):
            self.scheduled = 0

        async def record_episode(self, *args, **kwargs):
            return 1

        def schedule_consolidation(self):
            self.scheduled += 1

    class FakeSemantic:
        async def search(self, query, top_k=50, filter_expr=None):
            return []

    class FakeRetriever:
        async def search(self, query, top_k=10, doc_ids=None, file_types=None,
                         min_score=0.0):
            return []

    class FakeLLMGateway:
        async def complete(self, task):
            if getattr(task, "task_type", "") == "answer":
                return type("R", (), {"content": "答案 [1]。"})()
            if getattr(task, "task_type", "") == "evidence_gate":
                return type("R", (), {"content": '{"sufficient": true}'})()
            return type("R", (), {"content": '{"intent": "general", "topic": "主题"}'})()

    recording = RecordingConsolidation()
    agent = AgentLoop(
        llm_gateway=FakeLLMGateway(),  # type: ignore[arg-type]
        semantic_memory=FakeSemantic(),  # type: ignore[arg-type]
        episodic_memory=None,  # type: ignore[arg-type]
        consolidation_engine=recording,  # type: ignore[arg-type]
        wiki_compile_trigger=None,
    )
    agent.retriever = FakeRetriever()  # type: ignore[assignment]

    await agent.run(query="问题", session_id="s1")

    assert recording.scheduled == 1
