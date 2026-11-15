"""The profile store's own semantics, and the entity / graph layer.

* the profile row's overwrite behaviour
* the entity extractor's eight categories and its fallback
* the graph's nodes, edges and limits
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from yuanwork.db.sqlite import SQLiteStore
from yuanwork.memory.profile import ProfileStore
from yuanwork.wiki.entity_extractor import EntityExtractor
from yuanwork.wiki.graph import WikiGraph


@pytest.fixture
async def store(tmp_path: Path):
    s = SQLiteStore(tmp_path / "store.db")
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
def profile(store) -> ProfileStore:
    return ProfileStore(store)


# --------------------------------------------------------------------------
# the profile store
# --------------------------------------------------------------------------


async def test_writing_the_same_key_overwrites_it(profile):
    await profile.set("user.role", "律师", confidence=0.9)
    await profile.set("user.role", "会计", confidence=0.5)
    entry = await profile.get("user.role")
    assert entry["value"] == "会计"
    assert entry["confidence"] == 0.5


async def test_the_expiry_is_cleared_by_a_write_without_one(profile):
    from datetime import datetime

    await profile.set("user.role", "律师", expires_at=datetime(2030, 1, 1))
    await profile.set("user.role", "会计")
    entry = await profile.get("user.role")
    assert entry["expires_at"] is None


async def test_deleting_removes_the_entry(profile):
    await profile.set("user.role", "律师")
    assert await profile.delete("user.role") is True
    assert await profile.get("user.role") is None


async def test_the_entries_can_be_listed(profile):
    await profile.set("user.role", "律师")
    await profile.set("user.preference.like", "喝茶")
    assert len(await profile.list_all()) == 2
    assert len(await profile.list_all(prefix="user.role")) == 1


# --------------------------------------------------------------------------
# the entity extractor
# --------------------------------------------------------------------------


class FakeLLM:
    def __init__(self, content: str = "[]"):
        self.content = content
        self.tasks: list[Any] = []

    async def complete(self, task):
        self.tasks.append(task)
        return type("R", (), {"content": self.content})()


async def test_the_model_path_calls_the_model(store):
    """The extraction prompt contains a JSON example, and `.format()`
    treats its braces as placeholders — so the model path used to die
    with a KeyError before the model was ever called. The braces are
    escaped now, so the path works."""
    llm = FakeLLM("[]")
    extractor = EntityExtractor(sqlite_store=store, llm_gateway=llm)

    assert await extractor.extract("一段文本") == []
    assert llm.tasks, "the model was never called"


async def test_the_model_path_writes_what_the_model_returned(store):
    llm = FakeLLM(json.dumps([{"name": "华润", "type": "org"}]))
    extractor = EntityExtractor(sqlite_store=store, llm_gateway=llm)

    found = await extractor.extract_and_store("华润的条款")

    assert [f["name"] for f in found] == ["华润"]
    async with store.conn.execute("SELECT COUNT(*) FROM entities") as cur:
        rows = await cur.fetchall()
    assert rows[0][0] == 1


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


async def test_the_fallback_returns_nothing_for_plain_text(store):
    extractor = EntityExtractor(sqlite_store=store, llm_gateway=None)
    assert await extractor.extract("一段没有任何标记的普通文本") == []


async def test_an_illegal_entity_type_is_not_written(store):
    """The graph keeps its eight categories - an entity that names a
    ninth is dropped instead of blowing the write up."""
    llm = FakeLLM(json.dumps([{"name": "未知", "type": "alien"}]))
    extractor = EntityExtractor(sqlite_store=store, llm_gateway=llm)

    found = await extractor.extract_and_store("文本")

    assert found == []
    async with store.conn.execute("SELECT COUNT(*) FROM entities") as cur:
        rows = await cur.fetchall()
    assert rows[0][0] == 0


async def test_the_entity_id_is_a_truncated_slug(store):
    extractor = EntityExtractor(sqlite_store=store, llm_gateway=None)
    long_name = "很长的实体名称" * 10
    entity_id = extractor._entity_id(long_name)
    assert len(entity_id) <= 64


# --------------------------------------------------------------------------
# the graph
# --------------------------------------------------------------------------


async def test_an_empty_graph_has_no_nodes(store):
    graph = await WikiGraph(store).build_graph()
    assert graph["nodes"] == []
    assert graph["edges"] == []


async def test_the_graph_has_a_node_per_page(store):
    await store.upsert_wiki_page(
        page_id="p1", title="标题", markdown="# 标题", summary="摘要",
    )
    graph = await WikiGraph(store).build_graph()
    page_nodes = [n for n in graph["nodes"] if n["type"] == "page"]
    assert len(page_nodes) == 1
    assert page_nodes[0]["id"] == "p1"


async def test_the_graph_has_an_edge_per_page_link(store):
    await store.upsert_wiki_page(
        page_id="p1", title="甲", markdown="# 甲", summary="",
    )
    await store.upsert_wiki_page(
        page_id="p2", title="乙", markdown="# 乙", summary="",
    )
    await store.add_wiki_link(
        source_id="p1", source_type="page", target_id="p2", target_type="page",
        link_type="related",
    )
    graph = await WikiGraph(store).build_graph()
    assert len(graph["edges"]) == 1


async def test_the_related_pages_walk_is_bounded(store):
    await store.upsert_wiki_page(
        page_id="p1", title="甲", markdown="# 甲", summary="",
    )
    await store.upsert_wiki_page(
        page_id="p2", title="乙", markdown="# 乙", summary="",
    )
    await store.add_wiki_link(
        source_id="p1", source_type="page", target_id="p2", target_type="page",
        link_type="related",
    )
    related = await WikiGraph(store).get_related_pages("p1", max_depth=2)
    assert any(p["id"] == "p2" for p in related)


async def test_the_related_pages_walk_survives_a_cycle(store):
    await store.upsert_wiki_page(
        page_id="p1", title="甲", markdown="# 甲", summary="",
    )
    await store.upsert_wiki_page(
        page_id="p2", title="乙", markdown="# 乙", summary="",
    )
    await store.add_wiki_link(
        source_id="p1", source_type="page", target_id="p2", target_type="page",
        link_type="related",
    )
    await store.add_wiki_link(
        source_id="p2", source_type="page", target_id="p1", target_type="page",
        link_type="related",
    )
    related = await WikiGraph(store).get_related_pages("p1", max_depth=5)
    assert len(related) <= 2
