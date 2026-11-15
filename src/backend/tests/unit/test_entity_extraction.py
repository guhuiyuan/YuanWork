"""The entity extraction.

Two things have to hold:

* the model path works - the prompt carries a JSON example, and the
  braces in it used to be read as format placeholders, so the call
  died before the model was ever reached
* the graph only grows the eight categories it knows - a model that
  invents a ninth does not get to add one

* AC-F-015 - the entities can be found and browsed
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from yuanwork.db.sqlite import SQLiteStore
from yuanwork.wiki.entity_extractor import (
    ENTITY_CATEGORIES,
    EntityExtractor,
    normalize_category,
)


@pytest.fixture
async def store(tmp_path: Path):
    s = SQLiteStore(tmp_path / "store.db")
    await s.initialize()
    yield s
    await s.close()


class FakeLLM:
    """A model that answers whatever the test tells it to."""

    def __init__(self, content: str = "[]"):
        self.content = content
        self.error: Exception | None = None
        self.tasks: list[Any] = []

    async def complete(self, task):
        self.tasks.append(task)
        if self.error is not None:
            raise self.error
        return type("R", (), {"content": self.content})()


# --------------------------------------------------------------------------
# the model path
# --------------------------------------------------------------------------


async def test_the_model_path_calls_the_model(store):
    """The prompt carries a JSON example, so its braces have to be
    escaped - otherwise `.format()` blows up before the model is
    called."""
    llm = FakeLLM("[]")
    extractor = EntityExtractor(sqlite_store=store, llm_gateway=llm)

    assert await extractor.extract("一段文本") == []
    assert llm.tasks, "the model was never called"


async def test_the_model_path_reads_the_entities(store):
    llm = FakeLLM(json.dumps([{"name": "华润", "type": "org"}]))
    extractor = EntityExtractor(sqlite_store=store, llm_gateway=llm)

    found = await extractor.extract("华润的条款")

    assert [f["name"] for f in found] == ["华润"]
    assert found[0]["type"] == "org"


async def test_the_model_path_reads_a_wrapped_answer(store):
    """The prompt asks for {"entities": [...]}, but a model that answers
    with a bare list is answering the same question."""
    llm = FakeLLM(json.dumps({"entities": [{"name": "甲", "type": "person"}]}))
    extractor = EntityExtractor(sqlite_store=store, llm_gateway=llm)

    found = await extractor.extract("文本")

    assert [f["name"] for f in found] == ["甲"]


async def test_the_model_path_reads_a_fenced_answer(store):
    llm = FakeLLM('```json\n[{"name": "甲", "type": "person"}]\n```')
    extractor = EntityExtractor(sqlite_store=store, llm_gateway=llm)

    found = await extractor.extract("文本")

    assert [f["name"] for f in found] == ["甲"]


async def test_a_model_that_falls_over_falls_back(store):
    """The extraction is a side job - a model that cannot answer must
    not stop the page from being compiled."""
    llm = FakeLLM("not json at all")
    extractor = EntityExtractor(sqlite_store=store, llm_gateway=llm)

    found = await extractor.extract("他说这是「重要条款」")

    assert found
    assert found[0]["type"] == "other"


async def test_a_model_that_cannot_be_reached_falls_back(store):
    llm = FakeLLM()
    llm.error = RuntimeError("provider 挂了")
    extractor = EntityExtractor(sqlite_store=store, llm_gateway=llm)

    found = await extractor.extract("他说这是「重要条款」")

    assert found


# --------------------------------------------------------------------------
# the eight categories
# --------------------------------------------------------------------------


def test_there_are_eight_categories():
    assert len(ENTITY_CATEGORIES) == 8
    assert set(ENTITY_CATEGORIES) == {
        "person", "project", "concept", "contract",
        "law", "org", "location", "other",
    }


@pytest.mark.parametrize("category", ENTITY_CATEGORIES)
def test_every_category_is_known(category):
    assert normalize_category(category) == category


def test_a_category_the_graph_does_not_know_is_not_normalized():
    """A model that invents a ninth category does not get to add one."""
    assert normalize_category("alien") is None
    assert normalize_category("员工") is None
    assert normalize_category("") is None
    assert normalize_category(None) is None
    assert normalize_category(42) is None


def test_a_category_written_in_chinese_is_recognised():
    """The prompt lists the categories in Chinese too."""
    assert normalize_category("人物") == "person"
    assert normalize_category("公司") == "org"
    assert normalize_category("合同") == "contract"


def test_a_category_in_the_wrong_case_is_recognised():
    assert normalize_category("PERSON") == "person"
    assert normalize_category(" Person ") == "person"


async def test_an_entity_with_an_unknown_category_is_not_written(store):
    """The graph keeps its eight categories; the entity is dropped."""
    llm = FakeLLM(json.dumps([{"name": "未知", "type": "alien"}]))
    extractor = EntityExtractor(sqlite_store=store, llm_gateway=llm)

    found = await extractor.extract_and_store("文本")

    assert found == []
    async with store.conn.execute("SELECT COUNT(*) FROM entities") as cur:
        rows = await cur.fetchall()
    assert rows[0][0] == 0


async def test_one_bad_entity_does_not_take_the_others_with_it(store):
    llm = FakeLLM(json.dumps([
        {"name": "张三", "type": "person"},
        {"name": "未知", "type": "alien"},
        {"name": "合同", "type": "contract"},
    ]))
    extractor = EntityExtractor(sqlite_store=store, llm_gateway=llm)

    found = await extractor.extract_and_store("文本")

    assert [f["name"] for f in found] == ["张三", "合同"]
    async with store.conn.execute("SELECT COUNT(*) FROM entities") as cur:
        rows = await cur.fetchall()
    assert rows[0][0] == 2


async def test_a_category_written_in_chinese_is_stored_as_the_graph_knows_it(store):
    llm = FakeLLM(json.dumps([{"name": "张三", "type": "人物"}]))
    extractor = EntityExtractor(sqlite_store=store, llm_gateway=llm)

    await extractor.extract_and_store("文本")

    stored = await store.get_entity(extractor._entity_id("张三"))
    assert stored["type"] == "person"


# --------------------------------------------------------------------------
# the fallback
# --------------------------------------------------------------------------


async def test_the_fallback_path_works_without_a_model(store):
    """Without a model the extractor falls back and does write."""
    extractor = EntityExtractor(sqlite_store=store, llm_gateway=None)

    found = await extractor.extract_and_store("他说这是「重要条款」")

    assert found
    async with store.conn.execute("SELECT COUNT(*) FROM entities") as cur:
        rows = await cur.fetchall()
    assert rows[0][0] == 1


async def test_the_fallback_finds_quoted_text(store):
    extractor = EntityExtractor(sqlite_store=store, llm_gateway=None)

    found = await extractor.extract("他说这是「重要条款」")

    assert found
    assert found[0]["type"] == "other"


async def test_the_fallback_finds_english_proper_nouns(store):
    extractor = EntityExtractor(sqlite_store=store, llm_gateway=None)

    found = await extractor.extract("这是 Acme Corporation 的合同")

    assert any(f["name"] == "Acme Corporation" for f in found)


async def test_the_fallback_returns_nothing_for_plain_text(store):
    extractor = EntityExtractor(sqlite_store=store, llm_gateway=None)
    assert await extractor.extract("一段没有任何标记的普通文本") == []


# --------------------------------------------------------------------------
# writing them down
# --------------------------------------------------------------------------


async def test_the_entities_are_written_with_their_mentions(store):
    llm = FakeLLM(json.dumps([{"name": "华润", "type": "org"}]))
    extractor = EntityExtractor(sqlite_store=store, llm_gateway=llm)

    await extractor.extract_and_store("华润的条款", chunk_id="c1", doc_id="d1")

    async with store.conn.execute(
        "SELECT * FROM entity_mentions WHERE chunk_id = ?", ("c1",)
    ) as cur:
        rows = await cur.fetchall()
    assert len(rows) == 1


async def test_the_entity_id_is_a_truncated_slug(store):
    extractor = EntityExtractor(sqlite_store=store, llm_gateway=None)
    entity_id = extractor._entity_id("很长的实体名称" * 10)
    assert len(entity_id) <= 64
