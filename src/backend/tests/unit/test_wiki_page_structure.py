"""The structure of a compiled page.

A compiled page has four parts: the title, the summary, the key points
and the sources. They are not one blob of markdown any more - the key
points are a list of their own, so a list of pages can show what each
page says without opening it.

* AC-F-012 - a compiled page has the four parts
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from yuanwork.db.sqlite import SQLiteStore
from yuanwork.wiki.graph import WikiGraph
from yuanwork.wiki.page_generator import WikiPageGenerator


@pytest.fixture
async def store(tmp_path: Path):
    s = SQLiteStore(tmp_path / "store.db")
    await s.initialize()
    yield s
    await s.close()


class FakeLLM:
    def __init__(self, content: str = ""):
        self.content = content
        self.tasks: list[Any] = []

    async def complete(self, task):
        self.tasks.append(task)
        return type("R", (), {"content": self.content})()


@pytest.fixture
def generator(store) -> WikiPageGenerator:
    return WikiPageGenerator(sqlite_store=store, llm_gateway=None)


def chunks(count: int = 2) -> list[dict]:
    return [
        {
            "id": f"c{i}",
            "doc_id": "d1",
            "text": f"这是第 {i} 段正文内容。",
            "file_name": "合同.pdf",
            "file_path": "/kb/合同.pdf",
            "meta": {"page": 12 + i},
        }
        for i in range(count)
    ]


MODEL_PAGE = (
    "# 违约金条款\n\n"
    "> 分类：contract\n> 标签：违约, 合同\n\n"
    "## 摘要\n\n违约金按合同总额的三分之一计算。\n\n"
    "## 要点\n"
    "- 违约金上限是合同总额的三分之一 [[来源:合同.pdf 第 12 页]]\n"
    "- 违约金与实际损失可以同时主张 [[来源:合同.pdf 第 13 页]]\n"
    "- 争议先协商再诉讼 [[来源:合同.pdf 第 13 页]]\n\n"
    "## 出处\n"
    "[1] 《合同.pdf》第 12 页\n"
)


# --------------------------------------------------------------------------
# the four parts
# --------------------------------------------------------------------------


async def test_the_compiled_page_has_the_four_parts(store):
    llm = FakeLLM(MODEL_PAGE)
    generator = WikiPageGenerator(sqlite_store=store, llm_gateway=llm)

    result = await generator.compile_page(title="违约金条款", chunks=chunks())

    assert result["title"] == "违约金条款"
    assert result["summary"] == "违约金按合同总额的三分之一计算。"
    assert len(result["key_points"]) == 3
    assert "## 出处" in result["markdown"]


async def test_the_key_points_are_the_bullets_of_the_points_section(store):
    llm = FakeLLM(MODEL_PAGE)
    generator = WikiPageGenerator(sqlite_store=store, llm_gateway=llm)

    result = await generator.compile_page(title="违约金条款", chunks=chunks())

    assert result["key_points"][0].startswith("违约金上限是合同总额的三分之一")
    assert result["key_points"][1].startswith("违约金与实际损失可以同时主张")


async def test_the_key_points_are_written_to_the_page(store):
    llm = FakeLLM(MODEL_PAGE)
    generator = WikiPageGenerator(sqlite_store=store, llm_gateway=llm)

    result = await generator.compile_page(title="违约金条款", chunks=chunks())

    page = await store.get_wiki_page(result["page_id"])
    assert page["key_points"] == result["key_points"]


async def test_the_fallback_page_has_the_four_parts(generator):
    """Without a model the page still has its parts - a page without
    key points is not a page the reader can use."""
    result = await generator.compile_page(title="违约金条款", chunks=chunks())

    assert "## 摘要" in result["markdown"]
    assert "## 要点" in result["markdown"]
    assert "## 出处" in result["markdown"]
    assert len(result["key_points"]) == 2


async def test_the_fallback_key_points_carry_the_source(generator):
    result = await generator.compile_page(title="违约金条款", chunks=chunks())

    assert "[[来源:合同.pdf" in result["key_points"][0]


async def test_the_fallback_page_lists_the_sources(generator):
    result = await generator.compile_page(title="违约金条款", chunks=chunks())

    assert "[1] 《合同.pdf》" in result["markdown"]
    assert "第 12 页" in result["markdown"]


# --------------------------------------------------------------------------
# reading the parts
# --------------------------------------------------------------------------


async def test_the_key_points_of_a_page_that_has_none_is_empty(store):
    await store.upsert_wiki_page(
        page_id="p1", title="老页面", markdown="# 老页面", summary="摘要",
    )

    page = await store.get_wiki_page("p1")

    assert page["key_points"] == []


async def test_an_older_database_gains_the_column(tmp_path: Path):
    """A database from before the split keeps working."""
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute(
        """
        CREATE TABLE wiki_pages (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            markdown TEXT NOT NULL,
            summary TEXT NOT NULL,
            source_chunk_ids TEXT DEFAULT '[]',
            source_doc_ids TEXT DEFAULT '[]',
            category TEXT NOT NULL DEFAULT 'general',
            tags TEXT DEFAULT '[]',
            version INTEGER DEFAULT 1,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_compiled_at TIMESTAMP
        )
        """
    )
    old.execute("INSERT INTO wiki_pages (id, title, markdown, summary) "
                "VALUES ('old', '旧页面', '# 旧页面', '摘要')")
    old.commit()
    old.close()

    store = SQLiteStore(path)
    try:
        await store.initialize()

        page = await store.get_wiki_page("old")
        assert page is not None
        assert page["key_points"] == []
    finally:
        await store.close()


# --------------------------------------------------------------------------
# the list and the graph
# --------------------------------------------------------------------------


async def test_the_page_list_carries_the_key_points(store):
    llm = FakeLLM(MODEL_PAGE)
    generator = WikiPageGenerator(sqlite_store=store, llm_gateway=llm)
    await generator.compile_page(title="违约金条款", chunks=chunks())

    pages = await store.list_wiki_pages()

    assert pages[0]["key_points"][0].startswith("违约金上限是合同总额的三分之一")


async def test_the_graph_node_carries_the_key_points(store):
    llm = FakeLLM(MODEL_PAGE)
    generator = WikiPageGenerator(sqlite_store=store, llm_gateway=llm)
    await generator.compile_page(title="违约金条款", chunks=chunks())

    graph = await WikiGraph(store).build_graph()
    page_nodes = [n for n in graph["nodes"] if n["type"] == "page"]

    assert len(page_nodes) == 1
    assert len(page_nodes[0]["key_points"]) == 3


async def test_the_entity_nodes_of_the_graph_have_no_key_points(store):
    graph = await WikiGraph(store).build_graph()

    assert all(node["key_points"] == [] for node in graph["nodes"])
