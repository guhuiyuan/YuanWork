"""TASK-093 — the graph's edges, and what every one of them says.

The graph used to be **136 nodes and 0 edges**. Four things had gone
wrong at once, and the reader saw the sum of them: a page only pointed
at another page through a ``[[link]]`` the library happened to hold
(it held none), the entities were never mentioned by anybody, and the
documents the pages came from were not in the graph at all.

So the graph now draws four kinds of edge, and each one has to be
something the library knows (ADR-054):

* a page comes from a document  (``derived_from``)
* two pages come from the same slices  (``shares_evidence``)
* a page mentions an entity  (``mentions``)
* two entities turn up on the same page  (``co_occurs``)

plus the explicit ``[[link]]`` the library already holds
(``references``). An edge that cannot say what it is is not drawn, and
when there is no edge at all the picture says so rather than drawing
a scatter of dots that looks like a web.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from yuanwork.db.sqlite import SQLiteStore
from yuanwork.wiki.graph import (
    EDGE_CO_OCCURS,
    EDGE_DERIVED_FROM,
    EDGE_MENTIONS,
    EDGE_REFERENCES,
    EDGE_SHARES_EVIDENCE,
    NODE_PREFIX_DOCUMENT,
    NODE_PREFIX_ENTITY,
    WikiGraph,
)


@pytest.fixture
async def store(tmp_path: Path):
    s = SQLiteStore(tmp_path / "store.db")
    await s.initialize()
    yield s
    await s.close()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def nodes_of_type(graph: dict, node_type: str) -> list[dict]:
    return [n for n in graph["nodes"] if n["type"] == node_type]


def edges_of_type(graph: dict, edge_type: str) -> list[dict]:
    return [e for e in graph["edges"] if e["type"] == edge_type]


async def add_file(store: SQLiteStore, doc_id: str, name: str) -> None:
    await store.upsert_file(
        doc_id=doc_id,
        file_name=name,
        file_path=f"D:/kb/{name}",
        file_hash=f"hash-{doc_id}",
        file_type="markdown",
        size_bytes=100,
    )


async def add_page(
    store: SQLiteStore,
    page_id: str,
    title: str,
    *,
    markdown: str = "",
    summary: str = "",
    key_points: list[str] | None = None,
    source_chunk_ids: list[str] | None = None,
    source_doc_ids: list[str] | None = None,
) -> None:
    await store.upsert_wiki_page(
        page_id=page_id,
        title=title,
        markdown=markdown,
        summary=summary,
        key_points=key_points or [],
        source_chunk_ids=source_chunk_ids or [],
        source_doc_ids=source_doc_ids or [],
    )


async def _queries_for_one_graph(store: SQLiteStore) -> list[str]:
    """建一张图时跑了哪些查询。"""
    executed: list[str] = []
    original = store.conn.execute

    def counting(sql, *args, **kwargs):
        executed.append(str(sql))
        return original(sql, *args, **kwargs)

    store.conn.execute = counting  # type: ignore[method-assign]
    try:
        await WikiGraph(store).build_graph()
    finally:
        store.conn.execute = original  # type: ignore[method-assign]
    return executed


# --------------------------------------------------------------------------
# the page and the document it came from
# --------------------------------------------------------------------------


async def test_a_page_points_at_the_document_it_came_from(store):
    """The link the library already holds: 40 records of it were
    sitting in ``wiki_links`` the whole time, and the graph drew none
    of them."""
    await add_file(store, "doc-1", "采购合同.md")
    await add_page(
        store,
        "p1",
        "违约金",
        source_doc_ids=["doc-1"],
        markdown="# 违约金\n\n按合同总额的三分之一计算。",
    )

    graph = await WikiGraph(store).build_graph()

    docs = nodes_of_type(graph, "document")
    assert len(docs) == 1
    assert docs[0]["label"] == "采购合同.md"
    assert docs[0]["id"] == NODE_PREFIX_DOCUMENT + "doc-1"

    derived = edges_of_type(graph, EDGE_DERIVED_FROM)
    assert len(derived) == 1
    assert derived[0]["source"] == "p1"
    assert derived[0]["target"] == NODE_PREFIX_DOCUMENT + "doc-1"


async def test_the_document_link_alone_is_enough(store):
    """A page compiled by the core compiler lists its document but
    leaves no ``wiki_links`` row behind - the edge is drawn either
    way."""
    await add_file(store, "doc-1", "采购合同.md")
    await add_page(store, "p1", "违约金", source_doc_ids=["doc-1"])
    await store.add_wiki_link(
        source_id="p1",
        source_type="page",
        target_id="doc-1",
        target_type="doc",
        link_type="derived_from",
    )

    graph = await WikiGraph(store).build_graph()

    assert len(edges_of_type(graph, EDGE_DERIVED_FROM)) == 1


async def test_a_document_that_produced_many_pages_is_a_star(store):
    """One document, five pages. Connecting the pages to each other
    would make a knot; connecting them to the document makes a star -
    and that is the only way the document and its pages are related."""
    await add_file(store, "doc-1", "采购合同.md")
    for i in range(5):
        await add_page(
            store,
            f"p{i}",
            f"第{i}条",
            source_doc_ids=["doc-1"],
            source_chunk_ids=[f"chunk-{i}"],
        )

    graph = await WikiGraph(store).build_graph()

    assert len(nodes_of_type(graph, "document")) == 1
    assert len(edges_of_type(graph, EDGE_DERIVED_FROM)) == 5
    # the pages are not wired to each other
    assert edges_of_type(graph, EDGE_SHARES_EVIDENCE) == []


async def test_the_document_says_which_pages_came_from_it(store):
    await add_file(store, "doc-1", "采购合同.md")
    await add_page(store, "p1", "违约金", source_doc_ids=["doc-1"])
    await add_page(store, "p2", "保密义务", source_doc_ids=["doc-1"])

    graph = await WikiGraph(store).build_graph()

    doc = nodes_of_type(graph, "document")[0]
    assert sorted(doc["related_pages"]) == ["p1", "p2"]
    assert doc["weight"] == 2


async def test_a_document_listed_twice_still_produced_one_page(store):
    """同一页把同一份文件列了两遍 —— 由它编译出的页面还是那一页。

    核心编译器写「对比页」时，旧版与新版出自同一份文件，
    ``source_doc_ids`` 里就是同一个 doc_id 两次。
    """
    await add_file(store, "doc-1", "采购合同.md")
    await add_page(store, "p1", "违约金", source_doc_ids=["doc-1", "doc-1"])

    graph = await WikiGraph(store).build_graph()

    doc = nodes_of_type(graph, "document")[0]
    assert doc["related_pages"] == ["p1"]
    assert doc["weight"] == 1
    assert len(edges_of_type(graph, EDGE_DERIVED_FROM)) == 1


async def test_a_document_that_is_not_in_the_library_is_not_a_node(store):
    """A page may name a document the library does not hold. There is
    nothing to show for it, so it is not drawn - and the edge that
    would point at it goes with it."""
    await add_page(store, "p1", "违约金", source_doc_ids=["doc-gone"])

    graph = await WikiGraph(store).build_graph()

    assert nodes_of_type(graph, "document") == []
    assert graph["edges"] == []


# --------------------------------------------------------------------------
# two pages, one piece of material
# --------------------------------------------------------------------------


async def test_two_pages_built_from_the_same_slices_share_evidence(store):
    await add_page(
        store, "p1", "甲", source_chunk_ids=["chunk-a", "chunk-b"]
    )
    await add_page(
        store, "p2", "乙", source_chunk_ids=["chunk-b", "chunk-c"]
    )

    graph = await WikiGraph(store).build_graph()

    shared = edges_of_type(graph, EDGE_SHARES_EVIDENCE)
    assert len(shared) == 1
    assert {shared[0]["source"], shared[0]["target"]} == {"p1", "p2"}
    # 权重是一个量纲，都在 1 以上：共享的比例加一。
    assert shared[0]["weight"] == pytest.approx(1 + 1 / 3)


async def test_the_crowded_slices_are_not_part_of_the_shared_proportion(store):
    """一堆页面共用的切片不算这两页共享的材料 —— 分母里也不该有它。

    分母用与分子同一批切片：分子不数它，分母也不数它。否则「共享
    的比例」说的不是真的比例。
    """
    for i in range(20):
        await add_page(store, f"z{i}", f"页{i}", source_chunk_ids=["chunk-z"])
    await add_page(
        store, "p1", "甲", source_chunk_ids=["chunk-a", "chunk-b", "chunk-z"]
    )
    await add_page(
        store, "p2", "乙", source_chunk_ids=["chunk-b", "chunk-c", "chunk-z"]
    )

    graph = await WikiGraph(store).build_graph()

    shared = edges_of_type(graph, EDGE_SHARES_EVIDENCE)
    assert len(shared) == 1
    assert {shared[0]["source"], shared[0]["target"]} == {"p1", "p2"}
    # 共享的是 chunk-b 一片；分母是 a、b、c —— chunk-z 不算
    assert shared[0]["weight"] == pytest.approx(1 + 1 / 3)


async def test_pages_built_from_different_material_do_not_share(store):
    await add_page(store, "p1", "甲", source_chunk_ids=["chunk-a"])
    await add_page(store, "p2", "乙", source_chunk_ids=["chunk-b"])

    graph = await WikiGraph(store).build_graph()

    assert edges_of_type(graph, EDGE_SHARES_EVIDENCE) == []


async def test_a_crowd_sharing_a_slice_says_nothing_about_any_two(store):
    """Twenty pages on one slice is a coincidence, not a relation."""
    for i in range(20):
        await add_page(store, f"p{i}", f"页{i}", source_chunk_ids=["chunk-a"])

    graph = await WikiGraph(store).build_graph()

    assert edges_of_type(graph, EDGE_SHARES_EVIDENCE) == []


async def test_a_small_crowd_sharing_a_slice_is_still_a_relation(store):
    """The line is drawn where the pairs stop being readable: eight
    pages make 28 pairs, and a reader can still follow them."""
    for i in range(8):
        await add_page(store, f"p{i}", f"页{i}", source_chunk_ids=["chunk-a"])

    graph = await WikiGraph(store).build_graph()

    shared = edges_of_type(graph, EDGE_SHARES_EVIDENCE)
    assert len(shared) == 28


async def test_nine_pages_on_one_slice_is_already_a_knot(store):
    """One more page and the sharing says nothing about any two of
    them - the same reason a document becomes a node of its own."""
    for i in range(9):
        await add_page(store, f"p{i}", f"页{i}", source_chunk_ids=["chunk-a"])

    graph = await WikiGraph(store).build_graph()

    assert edges_of_type(graph, EDGE_SHARES_EVIDENCE) == []


# --------------------------------------------------------------------------
# the entity and the page that mentions it
# --------------------------------------------------------------------------


async def test_a_page_that_names_an_entity_points_at_it(store):
    await store.upsert_entity("华润", "华润", "org")
    await add_page(
        store,
        "p1",
        "采购流程",
        markdown="# 采购流程\n\n本合同由华润采购部签署。",
    )

    graph = await WikiGraph(store).build_graph()

    entities = nodes_of_type(graph, "entity_org")
    assert len(entities) == 1
    assert entities[0]["label"] == "华润"

    mentions = edges_of_type(graph, EDGE_MENTIONS)
    assert len(mentions) == 1
    assert mentions[0]["source"] == "p1"
    assert mentions[0]["target"] == NODE_PREFIX_ENTITY + "华润"


async def test_the_summary_and_the_key_points_count_as_mentioning(store):
    """The name may be in the summary or in the key points - the page
    says it either way."""
    await store.upsert_entity("华润", "华润", "org")
    await add_page(
        store,
        "p1",
        "采购流程",
        summary="由华润采购部签署。",
        key_points=["华润负责交付"],
    )

    graph = await WikiGraph(store).build_graph()

    assert len(edges_of_type(graph, EDGE_MENTIONS)) == 1


async def test_an_alias_counts_as_a_mention(store):
    await store.upsert_entity("华润", "华润", "org", aliases=["华润集团"])
    await add_page(
        store,
        "p1",
        "采购流程",
        markdown="# 采购流程\n\n华润集团负责交付。",
    )

    graph = await WikiGraph(store).build_graph()

    assert len(edges_of_type(graph, EDGE_MENTIONS)) == 1


async def test_a_name_that_is_too_short_does_not_match(store):
    """「AI」is two letters - it is inside half the words on the
    page. A name that short is not looked for (ADR-054)."""
    await store.upsert_entity("ai", "AI", "concept")
    await add_page(
        store, "p1", "路线", markdown="# 路线\n\n我们采用 AI 与人工结合的方式。"
    )

    graph = await WikiGraph(store).build_graph()

    assert edges_of_type(graph, EDGE_MENTIONS) == []


async def test_a_name_that_looks_like_a_path_does_not_match(store):
    await store.upsert_entity("src-backend", "src/backend", "other")
    await add_page(
        store,
        "p1",
        "目录",
        markdown="# 目录\n\n代码在 src/backend 下面。",
    )

    graph = await WikiGraph(store).build_graph()

    assert edges_of_type(graph, EDGE_MENTIONS) == []


async def test_a_latin_name_is_not_found_inside_another_word(store):
    """「Data」在「database」中间不算提到。

    四个字母也足以落在普通词中间 —— 拉丁名要有词边界。
    """
    await store.upsert_entity("data", "Data", "concept")
    await add_page(
        store, "p1", "数据", markdown="# 数据\n\n我们用 database 存数据。"
    )

    graph = await WikiGraph(store).build_graph()

    assert edges_of_type(graph, EDGE_MENTIONS) == []


async def test_a_latin_name_at_the_edge_of_a_word_is_found(store):
    await store.upsert_entity("data", "Data", "concept")
    await add_page(
        store, "p1", "数据", markdown="# 数据\n\nData 是我们的口径。"
    )

    graph = await WikiGraph(store).build_graph()

    assert len(edges_of_type(graph, EDGE_MENTIONS)) == 1


async def test_the_source_marker_in_the_body_is_not_a_mention(store):
    """正文里的 ``[[来源:文件名 页码]]`` 不是正文。

    文件名常常就是主题名（「采购合同.md」）；拿它去匹配实体名，
    由这份文件编译出来的每一页都会「提到」那个实体 —— 正文里根本
    没提。
    """
    await store.upsert_entity("华润", "华润", "org")
    await add_page(
        store,
        "p1",
        "采购",
        markdown="# 采购\n\n按合同履行。\n\n[[来源:采购合同.md 3]]",
    )

    graph = await WikiGraph(store).build_graph()

    assert edges_of_type(graph, EDGE_MENTIONS) == []


async def test_a_rule_waiting_for_the_user_is_not_in_the_graph(store):
    """待审的规律不是实体 —— 读者没确认过的东西不进图（ADR-039）。

    规律是一整句结论，画成「实体 · 概念」会让读者以为它已经成立；
    拿它的名字去匹配正文，还会给一条未确认的结论连出「提到」的边。
    """
    await store.upsert_entity(
        "rule-付款要在三日内",
        "付款要在三日内",
        "concept",
        properties={"kind": "rule"},
        status="pending",
    )
    await add_page(store, "p1", "付款", markdown="# 付款\n\n付款要在三日内。")

    graph = await WikiGraph(store).build_graph()

    assert nodes_of_type(graph, "entity_concept") == []
    assert edges_of_type(graph, EDGE_MENTIONS) == []


async def test_the_entities_in_the_graph_are_the_ones_the_pages_mention(
    store, monkeypatch
):
    """图里放的是**被页面提到**的实体 —— 写得多不等于图上关系多。

    「被写过多少次」是整合层写下它的次数，与图没有关系；被几页提到
    才是图上的份量。
    """
    monkeypatch.setattr("yuanwork.wiki.graph.MAX_ENTITIES", 1)
    # 写得多、只被一页提到
    for _ in range(3):
        await store.upsert_entity("一次", "提到一次", "other")
    # 写得少、被两页提到
    await store.upsert_entity("两次", "提到两次", "other")
    await add_page(store, "p1", "甲", markdown="# 甲\n\n提到一次。")
    await add_page(store, "p2", "乙", markdown="# 乙\n\n提到两次。")
    await add_page(store, "p3", "丙", markdown="# 丙\n\n提到两次。")

    graph = await WikiGraph(store).build_graph()

    labels = {n["label"] for n in nodes_of_type(graph, "entity_other")}
    assert labels == {"提到两次"}


async def test_an_entity_nobody_mentions_is_not_a_node(store):
    """没人提到的实体在图上只是一个点，读者读不出它跟什么有关。"""
    await store.upsert_entity("没人提", "没人提到", "other")
    await add_page(store, "p1", "甲", markdown="# 甲")

    graph = await WikiGraph(store).build_graph()

    assert nodes_of_type(graph, "entity_other") == []


async def test_two_chinese_characters_are_enough_to_match(store):
    await store.upsert_entity("合同", "合同", "concept")
    await add_page(store, "p1", "义务", markdown="# 义务\n\n合同一式两份。")

    graph = await WikiGraph(store).build_graph()

    assert len(edges_of_type(graph, EDGE_MENTIONS)) == 1


async def test_two_entities_on_the_same_page_turn_up_together(store):
    await store.upsert_entity("华润", "华润", "org")
    await store.upsert_entity("招商银行", "招商银行", "org")
    await add_page(
        store,
        "p1",
        "付款",
        markdown="# 付款\n\n华润向招商银行付款。",
    )

    graph = await WikiGraph(store).build_graph()

    together = edges_of_type(graph, EDGE_CO_OCCURS)
    assert len(together) == 1
    assert {together[0]["source"], together[0]["target"]} == {
        NODE_PREFIX_ENTITY + "华润",
        NODE_PREFIX_ENTITY + "招商银行",
    }


#: Nine names, none of them inside another one - to measure how big a
#: crowd is.
CROWD_NAMES = [
    "华润",
    "招商银行",
    "合同条款",
    "违约责任",
    "甲方义务",
    "乙方义务",
    "保密条款",
    "争议解决",
    "付款条件",
]


async def test_a_crowd_of_entities_on_one_page_is_still_readable(store):
    """Eight entities are 28 pairs, and a reader can still follow them -
    the same yardstick as the pages sharing one slice."""
    for name in CROWD_NAMES[:8]:
        await store.upsert_entity(name, name, "other")
    await add_page(
        store, "p1", "条款", markdown="# 条款\n\n" + "、".join(CROWD_NAMES[:8])
    )

    graph = await WikiGraph(store).build_graph()

    assert len(edges_of_type(graph, EDGE_CO_OCCURS)) == 28


async def test_nine_entities_on_one_page_is_already_a_knot(store):
    """One more and it says nothing about any two of them - the limit
    is the same as the one for the pages sharing a slice."""
    for name in CROWD_NAMES:
        await store.upsert_entity(name, name, "other")
    await add_page(
        store, "p1", "条款", markdown="# 条款\n\n" + "、".join(CROWD_NAMES)
    )

    graph = await WikiGraph(store).build_graph()

    assert edges_of_type(graph, EDGE_CO_OCCURS) == []
    # The page does mention them all - that edge does not come from
    # the co-occurrence.
    assert len(edges_of_type(graph, EDGE_MENTIONS)) == 9


async def test_the_entity_says_which_pages_mention_it(store):
    await store.upsert_entity("华润", "华润", "org", aliases=["华润集团"])
    await add_page(store, "p1", "采购", markdown="华润采购。")
    await add_page(store, "p2", "付款", markdown="华润集团付款。")

    graph = await WikiGraph(store).build_graph()

    entity = nodes_of_type(graph, "entity_org")[0]
    assert sorted(entity["related_pages"]) == ["p1", "p2"]
    assert entity["aliases"] == ["华润集团"]
    assert entity["category"] == "org"
    # 实体在图上的份量 —— 有几页提到了它。
    assert entity["weight"] == 2


# --------------------------------------------------------------------------
# the link the library already holds
# --------------------------------------------------------------------------


async def test_an_explicit_link_is_kept(store):
    await add_page(store, "p1", "甲")
    await add_page(store, "p2", "乙")
    await store.add_wiki_link(
        source_id="p1",
        source_type="page",
        target_id="p2",
        target_type="page",
        link_type="references",
    )

    graph = await WikiGraph(store).build_graph()

    refs = edges_of_type(graph, EDGE_REFERENCES)
    assert len(refs) == 1
    assert {refs[0]["source"], refs[0]["target"]} == {"p1", "p2"}


async def test_a_link_to_a_page_that_is_not_there_is_not_drawn(store):
    await add_page(store, "p1", "甲")
    await store.add_wiki_link(
        source_id="p1",
        source_type="page",
        target_id="p-gone",
        target_type="page",
        link_type="references",
    )

    graph = await WikiGraph(store).build_graph()

    assert graph["edges"] == []


async def test_a_page_link_that_is_not_an_explicit_link_is_not_drawn(store):
    """``related`` says something else, and the graph does not rename it
    to 「链接」.

    An edge has to say what it is (ADR-054). The only page-to-page
    relation the graph can say is the explicit ``[[link]]``, so the
    others are not drawn - the graph does not make up a name for them.
    """
    await add_page(store, "p1", "甲")
    await add_page(store, "p2", "乙")
    await store.add_wiki_link(
        source_id="p1",
        source_type="page",
        target_id="p2",
        target_type="page",
        link_type="related",
    )

    graph = await WikiGraph(store).build_graph()

    assert edges_of_type(graph, EDGE_REFERENCES) == []
    assert graph["edges"] == []


# --------------------------------------------------------------------------
# the shape of the whole thing
# --------------------------------------------------------------------------


async def test_every_edge_points_at_a_node_that_is_there(store):
    """The drawing library throws on a node it cannot find."""
    await add_file(store, "doc-1", "采购合同.md")
    await store.upsert_entity("华润", "华润", "org")
    await add_page(
        store,
        "p1",
        "采购",
        markdown="华润与合同。",
        source_doc_ids=["doc-1"],
        source_chunk_ids=["chunk-a"],
    )
    await add_page(store, "p2", "乙", source_chunk_ids=["chunk-a"])
    await store.add_wiki_link(
        source_id="p1",
        source_type="page",
        target_id="p-gone",
        target_type="page",
        link_type="references",
    )

    graph = await WikiGraph(store).build_graph()

    known = {n["id"] for n in graph["nodes"]}
    for edge in graph["edges"]:
        assert edge["source"] in known
        assert edge["target"] in known


async def test_no_node_id_is_used_twice(store):
    """A page called 「华润」 and an entity called 「华润」 slugify to
    the same string. Two nodes under one id is a broken picture."""
    await store.upsert_entity("华润", "华润", "org")
    await add_page(store, "华润", "华润", markdown="华润。")

    graph = await WikiGraph(store).build_graph()

    ids = [n["id"] for n in graph["nodes"]]
    assert len(ids) == len(set(ids))


async def test_a_graph_with_no_relations_has_no_edges(store):
    """Then the picture can say 「还没有关系」instead of drawing a
    scatter of dots that looks like a web."""
    await add_page(store, "p1", "甲", markdown="# 甲")
    await add_page(store, "p2", "乙", markdown="# 乙")

    graph = await WikiGraph(store).build_graph()

    assert len(graph["nodes"]) == 2
    assert graph["edges"] == []


async def test_the_same_relation_is_not_drawn_twice(store):
    """正着连与反着连是同一条关系 —— 只画一根线。"""
    await add_page(store, "p1", "甲", source_chunk_ids=["chunk-a"])
    await add_page(store, "p2", "乙", source_chunk_ids=["chunk-a"])
    await store.add_wiki_link(
        source_id="p1",
        source_type="page",
        target_id="p2",
        target_type="page",
        link_type="references",
    )
    await store.add_wiki_link(
        source_id="p2",
        source_type="page",
        target_id="p1",
        target_type="page",
        link_type="references",
    )

    graph = await WikiGraph(store).build_graph()

    refs = edges_of_type(graph, EDGE_REFERENCES)
    assert len(refs) == 1
    assert {refs[0]["source"], refs[0]["target"]} == {"p1", "p2"}


async def test_every_edge_weight_is_on_one_scale(store):
    """权重是一个量纲：都在 1 以上。

    前端拿它当线宽 —— 比值一律被夹到 1，计数却能到 3：「共享来源
    越多」的那对页面反而画得最细，只共现一次的实体边画得最粗。
    """
    await add_file(store, "doc-1", "采购合同.md")
    await store.upsert_entity("华润", "华润", "org")
    await store.upsert_entity("招商银行", "招商银行", "org")
    await add_page(
        store,
        "p1",
        "甲",
        source_doc_ids=["doc-1"],
        source_chunk_ids=["chunk-a"],
        markdown="# 甲\n\n华润向招商银行付款。",
    )
    await add_page(store, "p2", "乙", source_chunk_ids=["chunk-a"])
    await store.add_wiki_link(
        source_id="p1",
        source_type="page",
        target_id="p2",
        target_type="page",
        link_type="references",
    )

    graph = await WikiGraph(store).build_graph()

    assert graph["edges"]
    types = {edge["type"] for edge in graph["edges"]}
    assert types == {
        EDGE_DERIVED_FROM,
        EDGE_REFERENCES,
        EDGE_SHARES_EVIDENCE,
        EDGE_MENTIONS,
        EDGE_CO_OCCURS,
    }
    for edge in graph["edges"]:
        assert edge["weight"] >= 1.0, edge


async def test_the_page_carries_its_key_points(store):
    """The graph shows what the page says without opening it."""
    await add_page(
        store,
        "p1",
        "违约金",
        key_points=["按合同总额的三分之一计算", "上限 30%"],
    )

    graph = await WikiGraph(store).build_graph()

    page = nodes_of_type(graph, "page")[0]
    assert page["key_points"] == ["按合同总额的三分之一计算", "上限 30%"]


async def test_the_graph_is_not_built_with_a_query_per_page(store):
    """The number of queries does not grow with the number of pages:
    the links are read once, not once per page.

    Twenty pages and two hundred pages have to cost the same number
    of queries - a fixed budget would only prove that *this* library
    fits in it.
    """
    for i in range(20):
        await add_page(store, f"p{i}", f"页{i}")
    few = await _queries_for_one_graph(store)

    for i in range(20, 200):
        await add_page(store, f"p{i}", f"页{i}")
    many = await _queries_for_one_graph(store)

    assert len(many) == len(few), f"the queries grew with the pages: {few} -> {many}"
    link_queries = [sql for sql in many if "wiki_links" in sql]
    assert len(link_queries) == 1, f"the links are read once, not per page: {link_queries}"
    assert len(many) <= 6, f"too many queries for one graph: {many}"


async def test_the_graph_builds_the_same_way_twice(store):
    """The graph is built when it is asked for; two builds of the same
    library give the same picture."""
    await add_file(store, "doc-1", "采购合同.md")
    await store.upsert_entity("华润", "华润", "org")
    await add_page(
        store,
        "p1",
        "采购",
        markdown="华润。",
        source_doc_ids=["doc-1"],
        source_chunk_ids=["chunk-a"],
    )

    first = await WikiGraph(store).build_graph()
    second = await WikiGraph(store).build_graph()

    assert first == second


# --------------------------------------------------------------------------
# the real library
# --------------------------------------------------------------------------


#: Which library to check - the reader has to say it out loud.
#:
#: It is read **at import time**, not inside the test: the suite has an
#: autouse fixture that clears every ``YUANWORK_*`` variable before each
#: test (so one test's config does not leak into the next), and that
#: would wipe the switch before it could be read. Import happens before
#: any fixture runs, so the value is still there here.
#:
#: There is no default: running the whole test suite must not take the
#: library the reader actually uses as the thing to assert on. The
#: 「真机核对」 of TASK-093's acceptance is::
#:
#:     YUANWORK_GRAPH_DB=D:/yuanwork/data/store.db \\
#:         .venv/Scripts/python.exe -m pytest \\
#:         src/backend/tests/unit/test_wiki_graph_edges.py -k real -q
REAL_LIBRARY = os.environ.get("YUANWORK_GRAPH_DB", "").strip()


def real_library_path() -> Path | None:
    """Which library to check - ``None`` means 「没指」."""
    return Path(REAL_LIBRARY) if REAL_LIBRARY else None


def _copy_library(source: Path, target: Path) -> None:
    """Copy the library with SQLite's own backup.

    Copying the files by hand would miss the pages still sitting in
    the WAL. Opening the library itself is not an option either:
    ``SQLiteStore.initialize`` runs the schema and the migrations, and
    that is DDL written into the reader's library.
    """
    src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


async def test_the_real_graph_has_relations(tmp_path: Path):
    """The graph of the real library has edges - a graph with none is
    「图谱没有作用」.

    This is the 「真机核对」 of TASK-093's acceptance. It used to live
    only in ``scripts/bench/probe_graph_edges.py``, and pytest only
    collects ``src/backend/tests`` - so the check was never part of the
    automatic run.

    The library is checked on a **copy**: the reader's own library is
    never opened (initialising it writes the schema and the migrations
    into it). Without ``YUANWORK_GRAPH_DB`` there is nothing to check,
    so the check is skipped - a skipped check is not a passed one.
    """
    path = real_library_path()
    if path is None:
        pytest.skip("set YUANWORK_GRAPH_DB to the library to check")
    if not path.exists():
        pytest.skip(f"the library is not there: {path}")

    copy_path = tmp_path / "store.db"
    try:
        _copy_library(path, copy_path)
    except sqlite3.Error as exc:
        pytest.skip(f"the library cannot be copied: {exc}")

    store = SQLiteStore(copy_path)
    await store.initialize()
    try:
        graph = await WikiGraph(store).build_graph()
    finally:
        await store.close()

    if not any(node["type"] == "page" for node in graph["nodes"]):
        pytest.skip(f"the library holds no pages yet: {path}")

    assert graph["edges"], "the real graph has no edges at all"
    known = {str(node["id"]) for node in graph["nodes"]}
    for edge in graph["edges"]:
        assert edge["source"] in known, "an edge points at a node that is not there"
        assert edge["target"] in known, "an edge points at a node that is not there"
