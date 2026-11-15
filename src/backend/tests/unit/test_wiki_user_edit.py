"""The user edits a page.

The paragraphs the user wrote are theirs: a later automatic recompile
replaces the model's paragraphs and leaves theirs alone.

* AC-F-013 - a page the user edited is not overwritten
* AC-F-027 - a recompile replaces the model's paragraphs only
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
from yuanwork.wiki.page_generator import (
    WikiPageGenerator,
    render_blocks,
    split_paragraphs,
)


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


MODEL_PAGE = (
    "# 违约金条款\n\n"
    "## 摘要\n\n违约金按合同总额的三分之一计算。\n\n"
    "## 要点\n"
    "- 违约金上限是合同总额的三分之一\n"
    "- 违约金与实际损失可以同时主张\n"
    "- 争议先协商再诉讼\n\n"
    "## 出处\n"
    "[1] 《合同.pdf》第 12 页\n"
)


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


@pytest.fixture
def client(store) -> TestClient:
    deps.set_sqlite_store(store)
    app = FastAPI()
    app.include_router(wiki_routes.router, prefix="/api/v1")
    yield TestClient(app)
    deps.reset_dependencies()


# --------------------------------------------------------------------------
# the paragraphs
# --------------------------------------------------------------------------


def test_a_text_is_split_into_paragraphs():
    assert split_paragraphs("一\n\n二\n\n三") == ["一", "二", "三"]


def test_an_empty_text_has_no_paragraphs():
    assert split_paragraphs("") == []
    assert split_paragraphs("   \n\n  ") == []


def test_the_blocks_make_the_text_again():
    blocks = [{"text": "一"}, {"text": "二"}]
    assert render_blocks(blocks) == "一\n\n二"


# --------------------------------------------------------------------------
# the blocks of a compiled page
# --------------------------------------------------------------------------


async def test_a_compiled_page_is_made_of_blocks(store):
    llm = FakeLLM(MODEL_PAGE)
    generator = WikiPageGenerator(sqlite_store=store, llm_gateway=llm)

    await generator.compile_page(title="违约金条款", chunks=chunks())

    blocks = await store.list_page_blocks("违约金条款")
    assert len(blocks) == 4
    assert all(block["owner"] == "model" for block in blocks)


async def test_the_blocks_are_the_paragraphs_of_the_page(store):
    llm = FakeLLM(MODEL_PAGE)
    generator = WikiPageGenerator(sqlite_store=store, llm_gateway=llm)

    await generator.compile_page(title="违约金条款", chunks=chunks())

    blocks = await store.list_page_blocks("违约金条款")
    page = await store.get_wiki_page("违约金条款")
    assert render_blocks(blocks) == page["markdown"]


# --------------------------------------------------------------------------
# the edit
# --------------------------------------------------------------------------


async def test_the_user_can_edit_a_page(client, store):
    await store.upsert_wiki_page(
        page_id="p1", title="违约金条款", markdown="# 违约金条款\n\n旧内容。",
        summary="旧摘要",
    )

    resp = client.put(
        "/api/v1/wiki/pages/p1", json={"markdown": "# 违约金条款\n\n我改过的段落。"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert "我改过的段落。" in body["markdown"]


async def test_the_edited_paragraphs_belong_to_the_user(client, store):
    await store.upsert_wiki_page(
        page_id="p1", title="违约金条款", markdown="# 违约金条款\n\n旧内容。",
        summary="旧摘要",
    )

    client.put(
        "/api/v1/wiki/pages/p1", json={"markdown": "# 违约金条款\n\n我改过的段落。"}
    )

    blocks = await store.list_page_blocks("p1")
    assert [block["owner"] for block in blocks] == ["user"]


async def test_the_paragraphs_the_user_did_not_touch_keep_their_owner(client, store):
    """Only what the user changed becomes theirs."""
    llm = FakeLLM(MODEL_PAGE)
    generator = WikiPageGenerator(sqlite_store=store, llm_gateway=llm)
    await generator.compile_page(title="违约金条款", chunks=chunks())

    edited = MODEL_PAGE.replace(
        "违约金按合同总额的三分之一计算。", "我改过的摘要。"
    )
    client.put("/api/v1/wiki/pages/违约金条款", json={"markdown": edited})

    blocks = await store.list_page_blocks("违约金条款")
    owners = [block["owner"] for block in blocks]
    assert owners.count("user") == 1
    assert owners.count("model") == len(blocks) - 1


async def test_the_page_is_marked_as_edited_by_the_user(client, store):
    await store.upsert_wiki_page(
        page_id="p1", title="违约金条款", markdown="# 违约金条款\n\n旧内容。",
        summary="旧摘要",
    )

    client.put(
        "/api/v1/wiki/pages/p1", json={"markdown": "# 违约金条款\n\n我改过的段落。"}
    )

    page = await store.get_wiki_page("p1")
    assert page["user_edited"]


async def test_the_edit_keeps_the_sources_of_the_page(client, store):
    await store.upsert_wiki_page(
        page_id="p1", title="违约金条款", markdown="# 违约金条款", summary="摘要",
        source_chunk_ids=["c1", "c2"], source_doc_ids=["d1"],
    )

    client.put("/api/v1/wiki/pages/p1", json={"markdown": "# 违约金条款\n\n新内容。"})

    page = await store.get_wiki_page("p1")
    assert page["source_chunk_ids"] == ["c1", "c2"]
    assert page["source_doc_ids"] == ["d1"]


async def test_editing_a_page_that_is_not_there_is_an_error(client):
    resp = client.put("/api/v1/wiki/pages/nope", json={"markdown": "内容"})
    assert resp.status_code == 404


async def test_an_empty_edit_is_refused(client, store):
    await store.upsert_wiki_page(
        page_id="p1", title="违约金条款", markdown="# 违约金条款", summary="摘要",
    )

    resp = client.put("/api/v1/wiki/pages/p1", json={"markdown": "   "})

    assert resp.status_code == 400


# --------------------------------------------------------------------------
# the recompile
# --------------------------------------------------------------------------


async def test_a_recompile_keeps_the_paragraphs_the_user_wrote(client, store):
    llm = FakeLLM(MODEL_PAGE)
    generator = WikiPageGenerator(sqlite_store=store, llm_gateway=llm)
    await generator.compile_page(title="违约金条款", chunks=chunks())

    client.put(
        "/api/v1/wiki/pages/违约金条款",
        json={"markdown": "# 违约金条款\n\n## 摘要\n\n我改过的摘要。\n\n## 要点\n- 旧的要点\n"},
    )

    llm.content = (
        "# 违约金条款\n\n"
        "## 摘要\n\n模型写的新摘要。\n\n"
        "## 要点\n- 模型写的新要点\n\n"
        "## 出处\n[1] 《合同.pdf》第 12 页\n"
    )
    await generator.compile_page(title="违约金条款", chunks=chunks())

    page = await store.get_wiki_page("违约金条款")
    assert "我改过的摘要。" in page["markdown"]
    assert "模型写的新摘要。" not in page["markdown"]


async def test_a_recompile_replaces_the_paragraphs_of_the_model(client, store):
    llm = FakeLLM(MODEL_PAGE)
    generator = WikiPageGenerator(sqlite_store=store, llm_gateway=llm)
    await generator.compile_page(title="违约金条款", chunks=chunks())

    llm.content = (
        "# 违约金条款\n\n"
        "## 摘要\n\n模型写的新摘要。\n\n"
        "## 要点\n- 模型写的新要点\n\n"
        "## 出处\n[1] 《合同.pdf》第 12 页\n"
    )
    await generator.compile_page(title="违约金条款", chunks=chunks())

    page = await store.get_wiki_page("违约金条款")
    assert "模型写的新摘要。" in page["markdown"]
    assert "违约金按合同总额的三分之一计算。" not in page["markdown"]


async def test_a_recompile_of_a_page_the_user_never_touched_is_the_new_one(store):
    llm = FakeLLM(MODEL_PAGE)
    generator = WikiPageGenerator(sqlite_store=store, llm_gateway=llm)
    await generator.compile_page(title="违约金条款", chunks=chunks())

    llm.content = (
        "# 违约金条款\n\n"
        "## 摘要\n\n模型写的新摘要。\n\n"
        "## 要点\n- 模型写的新要点\n\n"
        "## 出处\n[1] 《合同.pdf》第 12 页\n"
    )
    await generator.compile_page(title="违约金条款", chunks=chunks())

    blocks = await store.list_page_blocks("违约金条款")
    assert all(block["owner"] == "model" for block in blocks)
    page = await store.get_wiki_page("违约金条款")
    assert "我改过的" not in page["markdown"]


async def test_the_user_paragraph_keeps_its_place(client, store):
    llm = FakeLLM(MODEL_PAGE)
    generator = WikiPageGenerator(sqlite_store=store, llm_gateway=llm)
    await generator.compile_page(title="违约金条款", chunks=chunks())

    client.put(
        "/api/v1/wiki/pages/违约金条款",
        json={"markdown": "# 违约金条款\n\n## 摘要\n\n我改过的摘要。\n\n## 要点\n- 旧的要点\n"},
    )

    llm.content = (
        "# 违约金条款\n\n"
        "## 摘要\n\n模型写的新摘要。\n\n"
        "## 要点\n- 模型写的新要点\n\n"
        "## 出处\n[1] 《合同.pdf》第 12 页\n"
    )
    await generator.compile_page(title="违约金条款", chunks=chunks())

    blocks = await store.list_page_blocks("违约金条款")
    owners = [block["owner"] for block in blocks]
    assert owners.count("user") == 2
    # The user's paragraph is still where it was: right after the title.
    assert blocks[1]["text"] == "## 摘要\n\n我改过的摘要。"


async def test_the_page_stays_marked_as_edited_after_a_recompile(client, store):
    llm = FakeLLM(MODEL_PAGE)
    generator = WikiPageGenerator(sqlite_store=store, llm_gateway=llm)
    await generator.compile_page(title="违约金条款", chunks=chunks())

    client.put(
        "/api/v1/wiki/pages/违约金条款",
        json={"markdown": "# 违约金条款\n\n我改过的段落。\n"},
    )
    await generator.compile_page(title="违约金条款", chunks=chunks())

    page = await store.get_wiki_page("违约金条款")
    assert page["user_edited"]


async def test_a_page_the_user_never_edited_is_not_marked_as_edited(store):
    llm = FakeLLM(MODEL_PAGE)
    generator = WikiPageGenerator(sqlite_store=store, llm_gateway=llm)

    await generator.compile_page(title="违约金条款", chunks=chunks())

    page = await store.get_wiki_page("违约金条款")
    assert not page["user_edited"]
