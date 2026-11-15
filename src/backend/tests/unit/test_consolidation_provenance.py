"""The trace of an induced conclusion.

Every conclusion the consolidation draws carries the interactions it
came from, and every interaction can be asked what it led to. A
conclusion that cannot be traced back is a guess (ADR-030).

* AC-F-017 - the consolidation induces entities, rules and page updates
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from yuanwork.consolidation.engine import ConsolidationEngine
from yuanwork.db.sqlite import SQLiteStore
from yuanwork.memory.episodic import EpisodicMemory
from yuanwork.memory.profile import ProfileStore
from yuanwork.wiki.entity_extractor import entity_id_for


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
            "rules": [{"statement": "差旅住宿费上限是 500 元"}],
            "page_points": [{"topic": "差旅报销", "point": "住宿费上限 500 元"}],
        })
        self.tasks: list[Any] = []

    async def complete(self, task):
        self.tasks.append(task)
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


# --------------------------------------------------------------------------
# the source fields
# --------------------------------------------------------------------------


async def test_an_induced_entity_carries_its_source_events(store):
    llm = FakeLLM()
    engine = make_engine(store, llm)
    event_id = await add_event(store)

    await engine.consolidate()

    stored = await store.get_entity(entity_id_for("华润"))
    assert stored["source_event_ids"] == [event_id]


async def test_an_induced_rule_carries_its_source_events(store):
    llm = FakeLLM()
    engine = make_engine(store, llm)
    event_id = await add_event(store)

    await engine.consolidate()

    rules = await store.list_entities(status="pending")
    assert rules[0]["source_event_ids"] == [event_id]


async def test_an_induced_key_point_carries_its_source_events(store):
    llm = FakeLLM()
    engine = make_engine(store, llm)
    event_id = await add_event(store)

    await engine.consolidate()

    candidates = await store.list_page_update_candidates()
    assert candidates[0]["source_event_ids"] == [event_id]


async def test_no_induced_result_is_without_a_source(store):
    """Every conclusion says what it rests on - a conclusion without a
    trace is a guess."""
    llm = FakeLLM()
    engine = make_engine(store, llm)
    await add_event(store)
    await add_event(store, detail="另一个问题")

    await engine.consolidate()

    for entity in await store.list_entities():
        assert entity["source_event_ids"], entity
    for candidate in await store.list_page_update_candidates():
        assert candidate["source_event_ids"], candidate


async def test_the_events_of_a_conclusion_are_merged_over_the_rounds(store):
    llm = FakeLLM()
    engine = make_engine(store, llm)
    first = await add_event(store)
    await engine.consolidate()

    second = await add_event(store, detail="另一个问题")
    await engine.consolidate()

    stored = await store.get_entity(entity_id_for("华润"))
    assert set(stored["source_event_ids"]) == {first, second}


# --------------------------------------------------------------------------
# the backtracking interface
# --------------------------------------------------------------------------


async def test_the_conclusions_of_one_interaction_are_found(store):
    llm = FakeLLM()
    engine = make_engine(store, llm)
    event_id = await add_event(store)

    await engine.consolidate()
    trace = await engine.provenance(event_id)

    assert [e["name"] for e in trace["entities"]] == ["华润"]
    assert [r["name"] for r in trace["rules"]] == ["差旅住宿费上限是 500 元"]
    assert [p["point"] for p in trace["page_points"]] == ["住宿费上限 500 元"]


async def test_the_interaction_itself_is_part_of_the_trace(store):
    llm = FakeLLM()
    engine = make_engine(store, llm)
    event_id = await add_event(store)

    await engine.consolidate()
    trace = await engine.provenance(event_id)

    assert trace["event"] is not None
    assert trace["event"]["id"] == event_id
    assert trace["event"]["detail"] == "差旅住宿费的上限是多少？"


async def test_the_interactions_behind_the_conclusions_are_part_of_the_trace(store):
    """The whole chain: the conclusion, and what it rests on."""
    llm = FakeLLM()
    engine = make_engine(store, llm)
    event_id = await add_event(store)

    await engine.consolidate()
    trace = await engine.provenance(event_id)

    assert [e["id"] for e in trace["source_events"]] == [event_id]


async def test_an_interaction_that_leaded_nowhere_has_an_empty_trace(store):
    llm = FakeLLM()
    engine = make_engine(store, llm)
    event_id = await add_event(store)

    trace = await engine.provenance(event_id)

    assert trace["event"] is not None
    assert trace["entities"] == []
    assert trace["rules"] == []
    assert trace["page_points"] == []


async def test_an_unknown_interaction_has_no_trace(store):
    llm = FakeLLM()
    engine = make_engine(store, llm)

    trace = await engine.provenance(4242)

    assert trace["event"] is None
    assert trace["entities"] == []


async def test_the_rules_and_the_entities_are_told_apart(store):
    """They are different things: one takes effect, the other waits for
    the user (ADR-039)."""
    llm = FakeLLM()
    engine = make_engine(store, llm)
    event_id = await add_event(store)

    await engine.consolidate()
    trace = await engine.provenance(event_id)

    assert all(e["id"] != entity_id_for("华润") for e in trace["rules"])
    assert all(r["id"].startswith("rule-") for r in trace["rules"])
    assert all(e["id"] == entity_id_for("华润") for e in trace["entities"])


async def test_the_store_finds_the_conclusions_of_an_interaction(store):
    """The same question asked of the store."""
    llm = FakeLLM()
    engine = make_engine(store, llm)
    event_id = await add_event(store)

    await engine.consolidate()

    entities = await store.list_entities_by_source_event(event_id)
    points = await store.list_page_updates_by_source_event(event_id)
    assert [e["name"] for e in entities] == ["华润", "差旅住宿费上限是 500 元"]
    assert [p["point"] for p in points] == ["住宿费上限 500 元"]


async def test_a_conclusion_of_one_interaction_is_not_the_trace_of_another(store):
    class ScriptedLLM:
        """A model that answers the induction differently each round."""

        def __init__(self, answers: list[str]):
            self.answers = list(answers)
            self.rounds = 0

        async def complete(self, task):
            prompt = task.messages[0].content
            if "记忆归纳助手" not in prompt:
                return type("R", (), {"content": "[]"})()
            answer = self.answers[min(self.rounds, len(self.answers) - 1)]
            self.rounds += 1
            return type("R", (), {"content": answer})()

    llm = ScriptedLLM([
        json.dumps({"entities": [
            {"name": "华润", "type": "org", "event_ids": [1]},
        ]}),
        json.dumps({"entities": [
            {"name": "中石化", "type": "org", "event_ids": [2]},
        ]}),
    ])
    engine = make_engine(store, llm)
    first = await add_event(store)
    await engine.consolidate()

    second = await add_event(store, detail="另一个问题")
    await engine.consolidate()

    trace_first = await engine.provenance(first)
    trace_second = await engine.provenance(second)

    assert [e["name"] for e in trace_first["entities"]] == ["华润"]
    assert [e["name"] for e in trace_second["entities"]] == ["中石化"]
