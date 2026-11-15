"""The links between the pages, and from the pages to the source text.

A page points at the slices it was compiled from, at the document it
came from, and at the pages it mentions. Every one of those links can
be read from the other end as well - that is what a backlink is.

* AC-F-014 - the links are established, and the backlinks can be read
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from yuanwork.api import deps
from yuanwork.api.routes import wiki as wiki_routes
from yuanwork.db.sqlite import SQLiteStore
from yuanwork.wiki.linker import WikiLinker
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


def chunks(count: int = 2, doc_id: str = "d1") -> list[dict]:
    return [
        {
            "id": f"c{i}",
            "doc_id": doc_id,
            "text": f"这是第 {i} 段正文内容。",
            "file_name": "合同.pdf",
            "file_path": "/kb/合同.pdf",
            "meta": {"page": 12 + i},
        }
        for i in range(count)
    ]


@pytest.fixture
def client(store) -> TestClient:
    deps.set_sqlite_store(store)
    app = FastAPI()
    app.include_router(wiki_routes.router, prefix="/api/v1")
    yield TestClient(app)
    deps.reset_dependencies()


# --------------------------------------------------------------------------
# the links that are established
# --------------------------------------------------------------------------


async def test_a_page_points_at_the_slices_it_came_from(store):
    llm = FakeLLM("# 标题\n\n## 摘要\n\n摘要。\n")
    generator = WikiPageGenerator(sqlite_store=store, llm_gateway=llm)

    await generator.compile_page(title="标题", chunks=chunks(3))

    links = await store.get_wiki_outgoing_links("标题")
    chunk_links = [l for l in links if l["target_type"] == "chunk"]
    assert len(chunk_links) == 3
    assert all(l["link_type"] == "derived_from" for l in chunk_links)


async def test_a_page_points_at_the_document_it_came_from(store):
    llm = FakeLLM("# 标题\n\n## 摘要\n\n摘要。\n")
    generator = WikiPageGenerator(sqlite_store=store, llm_gateway=llm)

    await generator.compile_page(title="标题", chunks=chunks(2))

    links = await store.get_wiki_outgoing_links("标题")
    doc_links = [l for l in links if l["target_type"] == "doc"]
    assert len(doc_links) == 1
    assert doc_links[0]["target_id"] == "d1"


async def test_a_page_points_at_the_pages_it_mentions(store):
    llm = FakeLLM("# 标题\n\n## 摘要\n\n摘要。\n\n参见 [[违约金条款]]。\n")
    generator = WikiPageGenerator(sqlite_store=store, llm_gateway=llm)

    await generator.compile_page(title="标题", chunks=chunks())

    links = await store.get_wiki_outgoing_links("标题")
    page_links = [l for l in links if l["target_type"] == "page"]
    assert len(page_links) == 1
    assert page_links[0]["target_id"] == "违约金条款"


async def test_the_source_markers_are_not_links(store):
    """`[[来源:文件名 页码]]` says where a fact comes from - it is not
    a reference to another page."""
    llm = FakeLLM(
        "# 标题\n\n## 摘要\n\n摘要。\n\n"
        "## 要点\n- 一条要点 [[来源:合同.pdf 第 12 页]]\n"
    )
    generator = WikiPageGenerator(sqlite_store=store, llm_gateway=llm)

    await generator.compile_page(title="标题", chunks=chunks())

    links = await store.get_wiki_outgoing_links("标题")
    assert [l for l in links if l["target_type"] == "page"] == []


async def test_a_page_does_not_link_to_itself(store):
    llm = FakeLLM("# 标题\n\n## 摘要\n\n摘要。\n\n参见 [[标题]]。\n")
    generator = WikiPageGenerator(sqlite_store=store, llm_gateway=llm)

    await generator.compile_page(title="标题", chunks=chunks())

    links = await store.get_wiki_outgoing_links("标题")
    assert [l for l in links if l["target_type"] == "page"] == []


# --------------------------------------------------------------------------
# the backlinks
# --------------------------------------------------------------------------


async def test_the_backlinks_of_a_page_are_found(store):
    llm = FakeLLM("# 标题\n\n## 摘要\n\n摘要。\n\n参见 [[违约金条款]]。\n")
    generator = WikiPageGenerator(sqlite_store=store, llm_gateway=llm)
    await generator.compile_page(title="标题", chunks=chunks())

    backlinks = await store.get_wiki_backlinks("违约金条款")

    assert len(backlinks) == 1
    assert backlinks[0]["source_id"] == "标题"


async def test_the_backlinks_of_a_document_are_found(store):
    llm = FakeLLM("# 标题\n\n## 摘要\n\n摘要。\n")
    generator = WikiPageGenerator(sqlite_store=store, llm_gateway=llm)
    await generator.compile_page(title="标题", chunks=chunks(2))

    backlinks = await store.get_wiki_backlinks("d1")

    assert len(backlinks) == 1
    assert backlinks[0]["source_id"] == "标题"


async def test_the_linker_reads_the_backlinks(store):
    llm = FakeLLM("# 标题\n\n## 摘要\n\n摘要。\n\n参见 [[违约金条款]]。\n")
    generator = WikiPageGenerator(sqlite_store=store, llm_gateway=llm)
    await generator.compile_page(title="标题", chunks=chunks())

    backlinks = await WikiLinker(store).get_backlinks("违约金条款")

    assert [b["source_id"] for b in backlinks] == ["标题"]


async def test_the_backlinks_carry_the_name_of_the_other_end(client, store):
    """A page id is not something a reader can use."""
    llm = FakeLLM("# 标题\n\n## 摘要\n\n摘要。\n\n参见 [[违约金条款]]。\n")
    generator = WikiPageGenerator(sqlite_store=store, llm_gateway=llm)
    await generator.compile_page(title="标题", chunks=chunks())

    resp = client.get("/api/v1/wiki/pages/违约金条款/backlinks")

    assert resp.status_code == 200
    body = resp.json()
    assert body["backlinks"][0]["title"] == "标题"


async def test_a_page_without_backlinks_has_none(client, store):
    await store.upsert_wiki_page(
        page_id="p1", title="没有来处的页面", markdown="# 没有来处的页面", summary="",
    )

    resp = client.get("/api/v1/wiki/pages/p1/backlinks")

    assert resp.status_code == 200
    assert resp.json()["backlinks"] == []
