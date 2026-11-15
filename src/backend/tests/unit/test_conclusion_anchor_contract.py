"""A conclusion says where it came from.

A page is read by people who have to decide whether to believe it. A
conclusion the model wrote without a place in the original cannot be
checked - and so it cannot be used as evidence either.

* AC-F-023 - every conclusion on a compiled page carries the anchor of
  where it was read, and the reader can jump back to it
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from yuanwork.db.sqlite import SQLiteStore
from yuanwork.wiki.core_compiler import CoreCompiler


@pytest.fixture
async def store(tmp_path: Path):
    s = SQLiteStore(tmp_path / "store.db")
    await s.initialize()
    yield s
    await s.close()


class FakeLLM:
    """A model that compiles a document."""

    def __init__(self, answer: dict[str, Any] | None = None):
        self.answer = answer if answer is not None else {}
        self.tasks: list[Any] = []

    async def complete(self, task):
        self.tasks.append(task)
        return type(
            "R", (), {"content": json.dumps(self.answer, ensure_ascii=False)}
        )()


def answer_with(*conclusions: dict[str, Any]) -> dict[str, Any]:
    """A model answer with the given conclusions."""
    return {
        "entity_pages": [
            {
                "title": "验收期限",
                "summary": "验收期限怎么算。",
                "key_points": ["验收期限怎么约定"],
                "conclusions": list(conclusions),
            }
        ],
        "concept_pages": [],
        "comparison_tables": [],
    }


def document(**overrides) -> dict[str, Any]:
    """A document with two pages, each one a piece of the original."""
    doc = {
        "doc_id": "d1",
        "file_name": "采购合同.pdf",
        "file_path": "/kb/合同/采购合同.pdf",
        "source_hash": "h1",
        "text": "第一页：验收期限为 30 日。\n\n第二页：违约金为 5%。",
        "segments": [
            {"text": "第一页：验收期限为 30 日。", "anchor": {"kind": "page", "page": 12}},
            {"text": "第二页：违约金为 5%。", "anchor": {"kind": "page", "page": 13}},
        ],
    }
    doc.update(overrides)
    return doc


# --------------------------------------------------------------------------
# AC-F-023 — the anchor
# --------------------------------------------------------------------------


async def test_a_conclusion_says_where_it_was_read(store):
    """Every conclusion is pinned to a place in the original."""
    llm = FakeLLM(
        answer_with(
            {"text": "验收期限为 30 日", "segment": 0},
            {"text": "违约金为 5%", "segment": 1},
        )
    )
    compiler = CoreCompiler(store, llm)

    await compiler.compile(document())

    conclusions = await store.list_page_conclusions("core/验收期限")
    assert len(conclusions) == 2
    assert conclusions[0]["anchor"] == {"kind": "page", "page": 12}
    assert conclusions[1]["anchor"] == {"kind": "page", "page": 13}


async def test_a_conclusion_remembers_the_document_it_came_from(store):
    llm = FakeLLM(answer_with({"text": "验收期限为 30 日", "segment": 0}))
    compiler = CoreCompiler(store, llm)

    await compiler.compile(document())

    conclusion = (await store.list_page_conclusions("core/验收期限"))[0]
    assert conclusion["source_doc_id"] == "d1"
    assert conclusion["source_hash"] == "h1"
    assert conclusion["segment_index"] == 0
    assert conclusion["active"] is True


async def test_a_conclusion_the_model_mislocated_is_found_by_its_text(store):
    """The model pointed at the wrong piece - the text still knows."""
    llm = FakeLLM(
        answer_with(
            {"text": "违约金为 5%", "segment": 0},
        )
    )
    compiler = CoreCompiler(store, llm)

    await compiler.compile(document())

    conclusion = (await store.list_page_conclusions("core/验收期限"))[0]
    assert conclusion["anchor"] == {"kind": "page", "page": 13}


async def test_a_conclusion_that_cannot_be_located_is_not_written_down(store):
    """A claim nobody can check cannot be used as evidence either."""
    llm = FakeLLM(
        answer_with(
            {"text": "验收期限为 30 日", "segment": 0},
            {"text": "这条结论在原文里根本不存在", "segment": 7},
        )
    )
    compiler = CoreCompiler(store, llm)

    await compiler.compile(document())

    conclusions = await store.list_page_conclusions("core/验收期限")
    assert [c["text"] for c in conclusions] == ["验收期限为 30 日"]


async def test_a_document_without_anchors_has_no_conclusions(store):
    """Nothing to point at means nothing to check."""
    llm = FakeLLM(answer_with({"text": "验收期限为 30 日", "segment": 0}))
    compiler = CoreCompiler(store, llm)

    await compiler.compile(
        document(segments=[], text="验收期限为 30 日。")
    )

    assert await store.list_page_conclusions("core/验收期限") == []


async def test_the_block_of_a_conclusion_points_at_the_conclusion(store):
    """The page's own paragraph knows which conclusion it is."""
    llm = FakeLLM(answer_with({"text": "验收期限为 30 日", "segment": 0}))
    compiler = CoreCompiler(store, llm)

    await compiler.compile(document())

    conclusions = await store.list_page_conclusions("core/验收期限")
    blocks = await store.list_page_blocks("core/验收期限")
    linked = [b for b in blocks if b.get("conclusion_id")]
    assert len(linked) == 1
    assert linked[0]["conclusion_id"] == conclusions[0]["id"]


# --------------------------------------------------------------------------
# the conclusions a question may lean on
# --------------------------------------------------------------------------


async def test_only_conclusions_with_an_anchor_are_evidence(store):
    """A conclusion without a position is not evidence."""
    await store.upsert_page_conclusion(
        conclusion_id="c1",
        page_id="core/验收期限",
        text="验收期限为 30 日",
        anchor={"kind": "page", "page": 12},
        source_doc_id="d1",
    )
    await store.upsert_page_conclusion(
        conclusion_id="c2",
        page_id="core/验收期限",
        text="这条没有出处",
        anchor={},
        source_doc_id="d1",
    )

    evidence = await store.list_active_conclusions()

    assert [c["id"] for c in evidence] == ["c1"]


async def test_a_conclusion_that_is_not_in_effect_is_not_evidence(store):
    """Inside a conflict group only one version is in effect."""
    await store.upsert_page_conclusion(
        conclusion_id="c1",
        page_id="core/验收期限",
        text="旧版：按月付款",
        anchor={"kind": "page", "page": 12},
        active=False,
    )

    assert await store.list_active_conclusions() == []


# --------------------------------------------------------------------------
# the jump back
# --------------------------------------------------------------------------


async def test_the_endpoint_returns_the_conclusions_with_their_place(store):
    """The reader gets the file and the position to jump to."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from yuanwork.api import deps
    from yuanwork.api.routes import wiki as wiki_routes

    await store.upsert_file(
        doc_id="d1",
        file_name="采购合同.pdf",
        file_path="C:/kb/合同/采购合同.pdf",
        file_hash="h1",
        file_type="pdf",
        size_bytes=1024,
    )
    await store.upsert_page_conclusion(
        conclusion_id="c1",
        page_id="core/验收期限",
        text="验收期限为 30 日",
        anchor={"kind": "page", "page": 12},
        source_doc_id="d1",
    )
    deps.set_sqlite_store(store)

    app = FastAPI()
    app.include_router(wiki_routes.router, prefix="/api/v1")
    with TestClient(app) as client:
        resp = client.get("/api/v1/wiki/pages/core%2F验收期限/conclusions")

    deps.reset_dependencies()
    assert resp.status_code == 200
    body = resp.json()
    assert body["page_id"] == "core/验收期限"
    conclusion = body["conclusions"][0]
    assert conclusion["text"] == "验收期限为 30 日"
    assert conclusion["location"] == "第 12 页"
    assert conclusion["file_name"] == "采购合同.pdf"
    assert conclusion["file_path"] == "C:/kb/合同/采购合同.pdf"
    assert conclusion["anchor"] == {"kind": "page", "page": 12}
