"""The induction - what the recent interactions add up to.

* the entities it finds take effect on their own
* the rules it finds wait for the user (ADR-039)
* the key points go to the page they belong to
* one induction that fails is skipped - the round goes on
* every result carries the events it was induced from (ADR-030)

* AC-F-017 - the consolidation induces entities, rules and page updates
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from yuanwork.consolidation.engine import ConsolidationEngine
from yuanwork.consolidation.induction import Inducer
from yuanwork.db.sqlite import SQLiteStore
from yuanwork.memory.episodic import EpisodicMemory
from yuanwork.memory.profile import ProfileStore
from yuanwork.wiki.entity_extractor import entity_id_for


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
        self.error: Exception | None = None
        self.tasks: list[Any] = []

    async def complete(self, task):
        self.tasks.append(task)
        if self.error is not None:
            raise self.error
        prompt = task.messages[0].content
        if "记忆归纳助手" not in prompt:
            # The profile extraction asks something else - it gets
            # nothing.
            return type("R", (), {"content": "[]"})()
        return type("R", (), {"content": self.answer})()


def make_engine(store, llm=None, **kwargs) -> ConsolidationEngine:
    return ConsolidationEngine(
        episodic_memory=EpisodicMemory(store),
        semantic_memory=None,  # type: ignore[arg-type]
        profile_store=ProfileStore(store),
        sqlite_store=store,
        llm_gateway=llm,
        **kwargs,
    )


async def add_event(
    store,
    detail: str = "差旅住宿费的上限是多少？",
    topic: str = "差旅报销",
    kind: str = "ask",
    subject: str = "",
    session: str = "s1",
) -> int:
    """Write one interaction down."""
    return await store.add_episodic_event(
        occurred_at=datetime.now(timezone.utc).isoformat(),
        session_id=session,
        kind=kind,
        subject=subject,
        detail=detail,
        topic=topic,
        importance=0.5,
    )


def induction_answer(**sections) -> str:
    """The model's answer, as JSON."""
    return json.dumps(sections)


# --------------------------------------------------------------------------
# the entities
# --------------------------------------------------------------------------


async def test_the_induced_entities_are_written_into_the_graph(store):
    llm = FakeLLM(induction_answer(
        entities=[{"name": "华润", "type": "org"}],
    ))
    engine = make_engine(store, llm)
    await add_event(store, detail="华润的合同怎么约定的")

    result = await engine.consolidate()

    assert result["entities_induced"] == 1
    entities = await store.list_entities(status="active")
    assert [e["name"] for e in entities] == ["华润"]


async def test_an_induced_entity_takes_effect_on_its_own(store):
    """An entity is a thing that exists in the documents - it does not
    wait for the user (ADR-039)."""
    llm = FakeLLM(induction_answer(
        entities=[{"name": "华润", "type": "org"}],
    ))
    engine = make_engine(store, llm)
    await add_event(store)

    await engine.consolidate()

    stored = await store.get_entity(entity_id_for("华润"))
    assert stored["status"] == "active"


async def test_an_induced_entity_carries_the_events_it_came_from(store):
    llm = FakeLLM(induction_answer(
        entities=[{"name": "华润", "type": "org"}],
    ))
    engine = make_engine(store, llm)
    event_id = await add_event(store, detail="华润的合同怎么约定的")

    await engine.consolidate()

    stored = await store.get_entity(entity_id_for("华润"))
    assert stored["source_event_ids"] == [event_id]


async def test_the_events_the_model_points_at_are_the_ones_recorded(store):
    """The model points at the numbers it was shown."""
    first = await add_event(store, detail="第一个问题")
    second = await add_event(store, detail="第二个问题")
    llm = FakeLLM(induction_answer(
        entities=[{"name": "华润", "type": "org", "event_ids": [1]}],
    ))
    engine = make_engine(store, llm)

    await engine.consolidate()

    stored = await store.get_entity(entity_id_for("华润"))
    assert stored["source_event_ids"] == [first]
    assert second != first


async def test_an_entity_without_event_ids_traces_to_the_whole_window(store):
    llm = FakeLLM(induction_answer(
        entities=[{"name": "华润", "type": "org"}],
    ))
    engine = make_engine(store, llm)
    first = await add_event(store, detail="第一个问题")
    second = await add_event(store, detail="第二个问题")

    await engine.consolidate()

    stored = await store.get_entity(entity_id_for("华润"))
    assert set(stored["source_event_ids"]) == {first, second}


async def test_an_entity_with_a_category_the_graph_does_not_know_is_dropped(store):
    """The graph has eight categories; the ninth one is a mistake, and
    it takes that one entity down - not the whole round."""
    llm = FakeLLM(induction_answer(entities=[
        {"name": "未知", "type": "alien"},
        {"name": "合同", "type": "contract"},
    ]))
    engine = make_engine(store, llm)
    await add_event(store)

    result = await engine.consolidate()

    assert result["entities_induced"] == 1
    entities = await store.list_entities()
    assert [e["name"] for e in entities] == ["合同"]


async def test_the_same_entity_induced_twice_is_one_entity(store):
    llm = FakeLLM(induction_answer(
        entities=[{"name": "华润", "type": "org"}],
    ))
    engine = make_engine(store, llm)
    await add_event(store, detail="第一个问题")
    await engine.consolidate()

    await add_event(store, detail="第二个问题")
    await engine.consolidate()

    entities = await store.list_entities()
    assert len(entities) == 1
    assert entities[0]["mention_count"] == 2


async def test_the_events_of_an_entity_are_merged_not_replaced(store):
    llm = FakeLLM(induction_answer(
        entities=[{"name": "华润", "type": "org"}],
    ))
    engine = make_engine(store, llm)
    first = await add_event(store, detail="第一个问题")
    await engine.consolidate()

    second = await add_event(store, detail="第二个问题")
    await engine.consolidate()

    stored = await store.get_entity(entity_id_for("华润"))
    assert set(stored["source_event_ids"]) == {first, second}


# --------------------------------------------------------------------------
# the rules
# --------------------------------------------------------------------------


async def test_an_induced_rule_is_written_down(store):
    llm = FakeLLM(induction_answer(
        rules=[{"statement": "差旅住宿费上限是 500 元"}],
    ))
    engine = make_engine(store, llm)
    await add_event(store)

    result = await engine.consolidate()

    assert result["rules_induced"] == 1
    stored = await store.list_entities(status="pending")
    assert [r["name"] for r in stored] == ["差旅住宿费上限是 500 元"]


async def test_an_induced_rule_waits_for_the_user(store):
    """A rule is a conclusion drawn across events - it does not count
    as evidence until the user confirms it (ADR-039, AC-F-028)."""
    llm = FakeLLM(induction_answer(
        rules=[{"statement": "差旅住宿费上限是 500 元"}],
    ))
    engine = make_engine(store, llm)
    await add_event(store)

    await engine.consolidate()

    pending = await store.list_entities(status="pending")
    assert len(pending) == 1
    assert await store.list_entities(status="active") == []


async def test_an_induced_rule_carries_the_events_it_came_from(store):
    llm = FakeLLM(induction_answer(
        rules=[{"statement": "差旅住宿费上限是 500 元"}],
    ))
    engine = make_engine(store, llm)
    event_id = await add_event(store)

    await engine.consolidate()

    stored = await store.list_entities(status="pending")
    assert stored[0]["source_event_ids"] == [event_id]


async def test_a_confirmed_rule_stays_confirmed(store):
    """The user confirmed it once - the next round does not send it
    back to the waiting list."""
    llm = FakeLLM(induction_answer(
        rules=[{"statement": "差旅住宿费上限是 500 元"}],
    ))
    engine = make_engine(store, llm)
    await add_event(store)
    await engine.consolidate()

    rule = (await store.list_entities(status="pending"))[0]
    await store.set_entity_status(rule["id"], "active")

    await add_event(store, detail="另一个问题")
    await engine.consolidate()

    assert await store.list_entities(status="pending") == []
    assert len(await store.list_entities(status="active")) == 1


async def test_a_rule_does_not_take_the_place_of_an_entity(store):
    """Entities and rules are different things: they live apart."""
    llm = FakeLLM(induction_answer(
        entities=[{"name": "华润", "type": "org"}],
        rules=[{"statement": "差旅住宿费上限是 500 元"}],
    ))
    engine = make_engine(store, llm)
    await add_event(store)

    await engine.consolidate()

    assert len(await store.list_entities(status="active")) == 1
    assert len(await store.list_entities(status="pending")) == 1


# --------------------------------------------------------------------------
# the page updates
# --------------------------------------------------------------------------


async def test_an_induced_key_point_goes_to_the_page(store):
    await store.upsert_wiki_page(
        page_id="差旅报销", title="差旅报销", markdown="# 差旅报销", summary="",
    )
    llm = FakeLLM(induction_answer(
        page_points=[{"topic": "差旅报销", "point": "住宿费上限 500 元"}],
    ))
    engine = make_engine(store, llm)
    await add_event(store)

    result = await engine.consolidate()

    assert result["page_points_induced"] == 1
    candidates = await store.list_page_update_candidates()
    assert candidates[0]["page_id"] == "差旅报销"
    assert candidates[0]["point"] == "住宿费上限 500 元"


async def test_an_induced_key_point_carries_the_events_it_came_from(store):
    llm = FakeLLM(induction_answer(
        page_points=[{"topic": "差旅报销", "point": "住宿费上限 500 元"}],
    ))
    engine = make_engine(store, llm)
    event_id = await add_event(store)

    await engine.consolidate()

    candidates = await store.list_page_update_candidates()
    assert candidates[0]["source_event_ids"] == [event_id]


async def test_a_key_point_for_a_topic_without_a_page_is_still_recorded(store):
    """The page may be compiled later - the point is not thrown away."""
    llm = FakeLLM(induction_answer(
        page_points=[{"topic": "还没有页面的主题", "point": "一条要点"}],
    ))
    engine = make_engine(store, llm)
    await add_event(store)

    await engine.consolidate()

    candidates = await store.list_page_update_candidates()
    assert candidates[0]["page_id"] == ""
    assert candidates[0]["topic"] == "还没有页面的主题"


async def test_the_same_key_point_is_only_recorded_once(store):
    llm = FakeLLM(induction_answer(
        page_points=[{"topic": "差旅报销", "point": "住宿费上限 500 元"}],
    ))
    engine = make_engine(store, llm)
    await add_event(store)
    await engine.consolidate()

    await add_event(store, detail="另一个问题")
    await engine.consolidate()

    candidates = await store.list_page_update_candidates()
    assert len(candidates) == 1


# --------------------------------------------------------------------------
# the round
# --------------------------------------------------------------------------


async def test_a_round_without_events_writes_nothing(store):
    llm = FakeLLM(induction_answer(
        entities=[{"name": "华润", "type": "org"}],
    ))
    engine = make_engine(store, llm)

    result = await engine.consolidate()

    assert result["events_read"] == 0
    assert result["entities_induced"] == 0
    assert await store.list_entities() == []


async def test_one_broken_candidate_does_not_stop_the_round(store):
    """A conclusion that cannot be written down is skipped; the rest of
    the round goes on (design §3.1)."""

    class FailingStore(SQLiteStore):
        def __init__(self, path, fail_name):
            super().__init__(path)
            self.fail_name = fail_name

        async def upsert_entity(self, entity_id, name, entity_type, **kwargs):
            if name == self.fail_name:
                raise RuntimeError("这一条写不进去")
            await super().upsert_entity(entity_id, name, entity_type, **kwargs)

    failing = FailingStore(store.db_path, "会炸的实体")
    try:
        await failing.initialize()
        llm = FakeLLM(induction_answer(entities=[
            {"name": "会炸的实体", "type": "concept"},
            {"name": "华润", "type": "org"},
        ]))
        engine = make_engine(failing, llm)
        await add_event(failing)

        result = await engine.consolidate()

        assert result["entities_induced"] == 1
        assert result["induction_failures"] == 1
        names = [e["name"] for e in await failing.list_entities()]
        assert names == ["华润"]
    finally:
        await failing.close()


async def test_a_model_that_cannot_be_reached_does_not_stop_the_round(store):
    """The model being down is a degradation, not a failure."""
    llm = FakeLLM()
    llm.error = RuntimeError("provider 挂了")
    engine = make_engine(store, llm)
    await add_event(store, detail="他说这是「重要条款」")

    result = await engine.consolidate()

    assert result["induction_source"] == "fallback"
    assert result["induction_failures"] == 0
    entities = await store.list_entities()
    assert [e["name"] for e in entities] == ["重要条款"]


async def test_the_model_is_not_asked_when_there_is_nothing_to_ask_about(store):
    llm = FakeLLM(induction_answer(entities=[{"name": "华润", "type": "org"}]))
    engine = make_engine(store, llm)

    await engine.consolidate()

    assert llm.tasks == []


async def test_the_round_reports_what_it_did(store):
    llm = FakeLLM(induction_answer(
        entities=[{"name": "华润", "type": "org"}],
        rules=[{"statement": "差旅住宿费上限是 500 元"}],
        page_points=[{"topic": "差旅报销", "point": "住宿费上限 500 元"}],
    ))
    engine = make_engine(store, llm)
    await add_event(store)

    result = await engine.consolidate()

    assert result["events_read"] == 1
    assert result["entities_induced"] == 1
    assert result["rules_induced"] == 1
    assert result["page_points_induced"] == 1
    assert result["induction_failures"] == 0
    assert result["induction_source"] == "model"
    assert result["duration_ms"] >= 0


async def test_the_run_entry_point_does_the_same_round(store):
    """`run()` is the old name of `consolidate()` (design §3.1)."""
    llm = FakeLLM(induction_answer(
        entities=[{"name": "华润", "type": "org"}],
    ))
    engine = make_engine(store, llm)
    await add_event(store)

    result = await engine.run(reason="manual")

    assert result["entities_induced"] == 1


async def test_the_events_are_read_in_the_order_they_happened(store):
    """The model reads the window in time order, oldest first."""
    llm = FakeLLM(induction_answer(entities=[]))
    engine = make_engine(store, llm)
    await add_event(store, detail="先问的")
    await add_event(store, detail="后问的")

    await engine.consolidate()

    prompt = llm.tasks[0].messages[0].content
    assert prompt.index("先问的") < prompt.index("后问的")


# --------------------------------------------------------------------------
# reading the model's answer
# --------------------------------------------------------------------------


async def test_the_inducer_reads_a_fenced_answer(store):
    llm = FakeLLM('```json\n{"entities": [{"name": "华润", "type": "org"}]}\n```')
    engine = make_engine(store, llm)
    await add_event(store)

    result = await engine.consolidate()

    assert result["entities_induced"] == 1


async def test_the_inducer_reads_a_bare_list(store):
    """The prompt asks for three sections; a bare list is the same
    answer without the sections."""
    llm = FakeLLM(json.dumps([{"name": "华润", "type": "org"}]))
    engine = make_engine(store, llm)
    await add_event(store)

    result = await engine.consolidate()

    assert result["entities_induced"] == 1


async def test_an_answer_that_is_not_json_falls_back(store):
    llm = FakeLLM("这不是 JSON")
    engine = make_engine(store, llm)
    await add_event(store, detail="他说这是「重要条款」")

    result = await engine.consolidate()

    assert result["induction_source"] == "fallback"
    assert result["induction_failures"] == 0


async def test_a_rule_can_be_written_as_a_plain_string(store):
    llm = FakeLLM(json.dumps({"rules": ["差旅住宿费上限是 500 元"]}))
    engine = make_engine(store, llm)
    await add_event(store)

    result = await engine.consolidate()

    assert result["rules_induced"] == 1
    stored = await store.list_entities(status="pending")
    assert stored[0]["name"] == "差旅住宿费上限是 500 元"


async def test_the_inducer_without_a_model_only_finds_entities():
    """A rule needs a model to draw it - pattern matching would make
    rules out of coincidence."""
    inducer = Inducer(llm_gateway=None)

    result = await inducer.induce([
        {"id": 1, "kind": "ask", "detail": "他说这是「重要条款」", "topic": "合同"},
    ])

    assert result.source == "fallback"
    assert [e.name for e in result.entities] == ["重要条款"]
    assert result.rules == []
    assert result.page_points == []


async def test_the_inducer_with_no_events_finds_nothing():
    inducer = Inducer(llm_gateway=None)

    result = await inducer.induce([])

    assert result.source == "empty"
    assert result.entities == []
