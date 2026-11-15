"""TASK-047 — the rules wait for the user.

The integration layer draws conclusions across interactions. Some of
them are facts that exist in the documents - the entities - and those
take effect on their own. The rules are conclusions drawn *across*
events, and they may be wrong. A wrong rule that goes straight into
the answer damages the accuracy promise, so a rule waits until the
user says so - and until then it is not evidence.

* AC-F-028 — a rule that the user has not confirmed is stored as
  waiting, and is not counted as evidence
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from yuanwork.api import deps
from yuanwork.api.routes import memory as memory_routes
from yuanwork.consolidation.engine import (
    ConsolidationEngine,
    RuleNotPending,
    UnknownRule,
)
from yuanwork.consolidation.induction import Inducer
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

    def __init__(self, answer: str = "{}"):
        self.answer = answer

    async def complete(self, task):
        prompt = task.messages[0].content
        if "记忆归纳助手" not in prompt:
            return type("R", (), {"content": "[]"})()
        return type("R", (), {"content": self.answer})()


def make_engine(store, llm=None) -> ConsolidationEngine:
    return ConsolidationEngine(
        episodic_memory=EpisodicMemory(store),
        semantic_memory=None,  # type: ignore[arg-type]
        profile_store=ProfileStore(store),
        sqlite_store=store,
        llm_gateway=llm,
    )


async def add_event(store, detail: str = "差旅住宿费的上限是多少？") -> int:
    return await store.add_episodic_event(
        occurred_at=datetime.now(timezone.utc).isoformat(),
        session_id="s1",
        kind="ask",
        subject="",
        detail=detail,
        topic="差旅报销",
        importance=0.5,
    )


def induction_answer(**sections) -> str:
    return json.dumps(sections)


async def induce_rule(store, statement: str = "差旅住宿费上限是 500 元") -> str:
    """Run one round that finds a rule - and return its identifier."""
    llm = FakeLLM(induction_answer(
        rules=[{"statement": statement}],
    ))
    engine = make_engine(store, llm)
    await add_event(store)
    await engine.consolidate()

    pending = await engine.list_pending_rules()
    assert pending, "the rule was not written down"
    return pending[0]["id"]


@pytest.fixture
def client(store) -> TestClient:
    """The memory routes, on their own."""
    deps.set_sqlite_store(store)
    deps.set_episodic_memory(EpisodicMemory(store))
    deps.set_profile_store(ProfileStore(store))
    app = FastAPI()
    app.include_router(memory_routes.router, prefix="/api/v1")
    yield TestClient(app)
    deps.reset_dependencies()


# --------------------------------------------------------------------------
# the waiting
# --------------------------------------------------------------------------


async def test_a_rule_waits_for_the_user(store):
    """It is written down, but it waits (ADR-039)."""
    engine = make_engine(store)
    rule_id = await induce_rule(store)

    stored = await store.get_entity(rule_id)
    assert stored["status"] == "pending"


async def test_a_waiting_rule_is_not_counted_as_evidence(store):
    """The rules that count are the ones the user confirmed."""
    engine = make_engine(store)
    await induce_rule(store)

    assert await engine.list_active_rules() == []
    assert len(await engine.list_pending_rules()) == 1


async def test_an_entity_takes_effect_on_its_own(store):
    """Only the rules wait - an entity is a thing that exists."""
    llm = FakeLLM(induction_answer(entities=[{"name": "华润", "type": "org"}]))
    engine = make_engine(store, llm)
    await add_event(store)

    await engine.consolidate()

    entities = await store.list_entities(status="active")
    assert [e["name"] for e in entities] == ["华润"]


async def test_the_rules_the_user_can_look_at_are_listed(store):
    """The waiting ones, with the events they came from."""
    llm = FakeLLM(induction_answer(
        rules=[{"statement": "差旅住宿费上限是 500 元"}],
    ))
    engine = make_engine(store, llm)
    event_id = await add_event(store)
    await engine.consolidate()

    pending = await engine.list_pending_rules()

    assert len(pending) == 1
    assert pending[0]["name"] == "差旅住宿费上限是 500 元"
    assert pending[0]["source_event_ids"] == [event_id]


# --------------------------------------------------------------------------
# the confirmation
# --------------------------------------------------------------------------


async def test_the_user_can_confirm_a_rule(store):
    """The rule takes effect from now on."""
    engine = make_engine(store)
    rule_id = await induce_rule(store)

    approved = await engine.approve(rule_id)

    assert approved["status"] == "active"
    assert len(await engine.list_active_rules()) == 1
    assert await engine.list_pending_rules() == []


async def test_a_confirmed_rule_keeps_the_events_it_came_from(store):
    """A conclusion that cannot be traced back is a guess (ADR-030)."""
    llm = FakeLLM(induction_answer(
        rules=[{"statement": "差旅住宿费上限是 500 元"}],
    ))
    engine = make_engine(store, llm)
    event_id = await add_event(store)
    await engine.consolidate()
    rule_id = (await engine.list_pending_rules())[0]["id"]

    approved = await engine.approve(rule_id)

    assert approved["source_event_ids"] == [event_id]


async def test_a_rule_that_does_not_exist_cannot_be_confirmed(store):
    """There is nothing to confirm."""
    engine = make_engine(store)

    with pytest.raises(UnknownRule):
        await engine.approve("rule-nope")


async def test_a_rule_that_is_already_in_effect_cannot_be_confirmed_twice(
    store,
):
    """The second confirmation has nothing to do."""
    engine = make_engine(store)
    rule_id = await induce_rule(store)
    await engine.approve(rule_id)

    with pytest.raises(RuleNotPending):
        await engine.approve(rule_id)


async def test_an_entity_is_not_a_rule(store):
    """Confirming an entity makes no sense - entities take effect on
    their own."""
    engine = make_engine(store)
    await store.upsert_entity(
        entity_id="e-1",
        name="华润",
        entity_type="org",
        properties={"kind": "entity"},
        source_event_ids=[1],
        status="active",
    )

    with pytest.raises(UnknownRule):
        await engine.approve("e-1")


async def test_a_confirmed_rule_is_not_thrown_back_to_waiting(store):
    """The next round must not undo what the user said."""
    llm = FakeLLM(induction_answer(
        rules=[{"statement": "差旅住宿费上限是 500 元"}],
    ))
    engine = make_engine(store, llm)
    await add_event(store)
    await engine.consolidate()
    rule_id = (await engine.list_pending_rules())[0]["id"]
    await engine.approve(rule_id)

    # The same rule is induced again.
    await add_event(store, detail="再说一次")
    await engine.consolidate()

    stored = await store.get_entity(rule_id)
    assert stored["status"] == "active"


# --------------------------------------------------------------------------
# the interface
# --------------------------------------------------------------------------


async def test_the_waiting_rules_are_listed_over_http(store, client):
    """The user has to be able to see them."""
    await induce_rule(store)

    response = client.get("/api/v1/memory/rules/pending")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["rules"][0]["statement"] == "差旅住宿费上限是 500 元"


async def test_a_rule_can_be_confirmed_over_http(store, client):
    """And to confirm one."""
    rule_id = await induce_rule(store)

    response = client.post(f"/api/v1/memory/rules/{rule_id}/approve")

    assert response.status_code == 200
    body = response.json()
    assert body["approved"] is True
    assert body["status"] == "active"

    stored = await store.get_entity(rule_id)
    assert stored["status"] == "active"


async def test_confirming_a_rule_that_is_not_there_is_a_404(store, client):
    """The interface says what went wrong."""
    response = client.post("/api/v1/memory/rules/rule-nope/approve")

    assert response.status_code == 404


async def test_confirming_a_rule_twice_is_a_400(store, client):
    """The second one has nothing to do."""
    rule_id = await induce_rule(store)
    client.post(f"/api/v1/memory/rules/{rule_id}/approve")

    response = client.post(f"/api/v1/memory/rules/{rule_id}/approve")

    assert response.status_code == 400
