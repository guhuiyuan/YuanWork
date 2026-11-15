"""The core layer is compiled while the document is indexed.

A core document - a contract, a rule, a policy - is read in full when
it comes in, and what it says is written down as pages: an entity page,
a concept page, or a comparison table. The pages are there before
anybody asks, so the compilation is never on the way a question is
answered.

* AC-F-022 - a core document produces at least one of the three kinds
  of page
* AC-P-008 - the compilation is finished within five minutes
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from typing import Any

import pytest

from yuanwork.db.sqlite import SQLiteStore
from yuanwork.wiki.core_compiler import (
    CORE_TIER,
    DEFAULT_COMPILE_TIMEOUT,
    PAGE_TYPE_COMPARISON,
    PAGE_TYPE_CONCEPT,
    PAGE_TYPE_ENTITY,
    CoreCompiler,
)
from yuanwork.wiki.page_generator import render_blocks


@pytest.fixture
async def store(tmp_path: Path):
    s = SQLiteStore(tmp_path / "store.db")
    await s.initialize()
    yield s
    await s.close()


class FakeLLM:
    """A model that compiles a document."""

    def __init__(self, answer: dict[str, Any] | None = None, error: Exception | None = None):
        self.answer = answer if answer is not None else {}
        self.error = error
        self.tasks: list[Any] = []

    async def complete(self, task):
        self.tasks.append(task)
        if self.error is not None:
            raise self.error
        return type("R", (), {"content": json.dumps(self.answer, ensure_ascii=False)})()


def entity_answer() -> dict[str, Any]:
    return {
        "entity_pages": [
            {
                "title": "甲方：华润置地",
                "summary": "合同的一方是华润置地。",
                "key_points": ["甲方为华润置地有限公司"],
                "conclusions": [{"text": "甲方为华润置地有限公司", "segment": 0}],
            }
        ],
        "concept_pages": [],
        "comparison_tables": [],
    }


def concept_answer() -> dict[str, Any]:
    return {
        "entity_pages": [],
        "concept_pages": [
            {
                "title": "验收期限",
                "summary": "验收期限怎么算。",
                "key_points": ["验收期限为 30 日"],
                "conclusions": [{"text": "验收期限为 30 日", "segment": 0}],
            }
        ],
        "comparison_tables": [],
    }


def comparison_answer() -> dict[str, Any]:
    return {
        "entity_pages": [],
        "concept_pages": [],
        "comparison_tables": [
            {
                "title": "付款条款新旧对比",
                "summary": "旧版按月付，新版按验收付。",
                "key_points": ["旧版：按月付款", "新版：按验收付款"],
                "conclusions": [{"text": "新版改为按验收付款", "segment": 0}],
            }
        ],
    }


def document(**overrides) -> dict[str, Any]:
    doc = {
        "doc_id": "d1",
        "file_name": "采购合同.md",
        "file_path": "/kb/合同/采购合同.md",
        "text": "第一段：甲方为华润置地有限公司。\n\n第二段：验收期限为 30 日。",
    }
    doc.update(overrides)
    return doc


# --------------------------------------------------------------------------
# the user's own paragraphs
# --------------------------------------------------------------------------


async def test_a_recompile_keeps_the_paragraph_the_user_wrote(store):
    """The user's own words stay in the page - not only in the table.

    The body and the blocks table have to agree. A body that dropped
    the user's paragraph would show the reader the model's text again,
    and the next edit - which starts from the body - would take the
    user's words away for good (AC-F-027).
    """
    llm = FakeLLM(entity_answer())
    compiler = CoreCompiler(store, llm)
    await compiler.compile(document())

    page_id = "core/甲方华润置地"
    blocks = await store.list_page_blocks(page_id)
    assert blocks, "the page has no paragraphs"

    # The user writes a paragraph of their own into the page.
    edited = [
        {
            "text": block["text"] + "\n\n我改过的段落。",
            "owner": "user",
        }
        if index == 0
        else {"text": block["text"], "owner": block.get("owner", "model")}
        for index, block in enumerate(blocks)
    ]
    await store.replace_page_blocks(page_id, edited)
    await store.upsert_wiki_page(
        page_id=page_id,
        title="甲方：华润置地",
        markdown=render_blocks(edited),
        summary="合同的一方是华润置地。",
        key_points=["甲方为华润置地有限公司"],
        source_chunk_ids=[],
        source_doc_ids=["d1"],
        category="entity",
        tags=[],
        page_type="entity",
    )

    # The document comes in again and the page is compiled again.
    await compiler.compile(document())

    page = await store.get_wiki_page(page_id)
    assert "我改过的段落。" in page["markdown"], (
        "the recompile took the user's own paragraph out of the page"
    )

    stored = await store.list_page_blocks(page_id)
    assert any(block["owner"] == "user" for block in stored)


# --------------------------------------------------------------------------
# AC-F-022 — the three kinds of page
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "answer,page_type",
    [
        (entity_answer(), PAGE_TYPE_ENTITY),
        (concept_answer(), PAGE_TYPE_CONCEPT),
        (comparison_answer(), PAGE_TYPE_COMPARISON),
    ],
)
async def test_a_core_document_produces_a_page(store, answer, page_type):
    """The model reads the document and what it says is written down."""
    llm = FakeLLM(answer)
    compiler = CoreCompiler(store, llm)

    result = await compiler.compile(document())

    assert result["compiled"] is True
    assert len(result["pages"]) == 1

    pages = await store.list_wiki_pages()
    assert len(pages) == 1
    assert pages[0]["page_type"] == page_type
    assert pages[0]["tier"] == CORE_TIER


async def test_the_page_says_where_it_came_from(store):
    """A page has to point at the document it was compiled from."""
    llm = FakeLLM(entity_answer())
    compiler = CoreCompiler(store, llm)

    await compiler.compile(document())

    pages = await store.list_wiki_pages()
    assert len(pages) == 1
    assert pages[0]["source_doc_ids"] == ["d1"]
    assert "甲方为华润置地有限公司" in pages[0]["markdown"]


async def test_the_model_reads_the_whole_document(store):
    """The model is given the document, not a slice of it."""
    llm = FakeLLM(entity_answer())
    compiler = CoreCompiler(store, llm)

    await compiler.compile(document())

    prompt = llm.tasks[0].messages[0].content
    assert "甲方为华润置地有限公司" in prompt
    assert "验收期限为 30 日" in prompt


async def test_the_three_kinds_are_asked_for(store):
    """The model is asked for all three, and takes what it has."""
    llm = FakeLLM(
        {
            "entity_pages": entity_answer()["entity_pages"],
            "concept_pages": concept_answer()["concept_pages"],
            "comparison_tables": [],
        }
    )
    compiler = CoreCompiler(store, llm)

    result = await compiler.compile(document())

    assert len(result["pages"]) == 2
    types = {page["page_type"] for page in result["pages"]}
    assert types == {PAGE_TYPE_ENTITY, PAGE_TYPE_CONCEPT}


async def test_a_document_with_nothing_in_it_produces_no_page(store):
    """The model says there is nothing to write - nothing is written."""
    llm = FakeLLM({"entity_pages": [], "concept_pages": [], "comparison_tables": []})
    compiler = CoreCompiler(store, llm)

    result = await compiler.compile(document())

    assert result["compiled"] is False
    assert await store.list_wiki_pages() == []


async def test_a_document_without_content_is_not_compiled(store):
    llm = FakeLLM(entity_answer())
    compiler = CoreCompiler(store, llm)

    result = await compiler.compile(document(text="   "))

    assert result["compiled"] is False
    assert llm.tasks == []


# --------------------------------------------------------------------------
# the compilation is written down
# --------------------------------------------------------------------------


async def test_the_compile_status_is_written_down(store):
    """A compilation nobody can see is a compilation nobody can redo."""
    llm = FakeLLM(entity_answer())
    compiler = CoreCompiler(store, llm)

    await compiler.compile(document())

    record = await store.get_doc_tier("d1")
    assert record["compile_status"] == "compiled"


async def test_a_compile_that_fails_is_marked_for_another_try(store):
    """A failed compilation does not stop the build - it is marked."""
    llm = FakeLLM(error=RuntimeError("provider 挂了"))
    compiler = CoreCompiler(store, llm)

    result = await compiler.compile(document())

    assert result["compiled"] is False
    record = await store.get_doc_tier("d1")
    assert record["compile_status"] == "pending_recompile"


# --------------------------------------------------------------------------
# AC-P-008 — five minutes
# --------------------------------------------------------------------------


def test_the_compile_budget_is_five_minutes_at_the_most():
    """The promise is a ceiling, not a target."""
    assert DEFAULT_COMPILE_TIMEOUT <= 300


def test_the_budget_cannot_be_configured_above_the_promise():
    from yuanwork.config import YuanWorkSettings

    with pytest.raises(Exception):
        YuanWorkSettings(core_compile_timeout_seconds=301)


async def test_a_compile_that_runs_over_its_budget_is_reported(store):
    """It does not hang: it gives up and says so."""
    class SlowLLM:
        async def complete(self, task):
            await asyncio.sleep(5)
            return type("R", (), {"content": "{}"})()

    compiler = CoreCompiler(store, SlowLLM(), timeout=0.05)

    result = await compiler.compile(document())

    assert result["compiled"] is False
    assert "超时" in result["error"]
    record = await store.get_doc_tier("d1")
    assert record["compile_status"] == "pending_recompile"


# --------------------------------------------------------------------------
# the compilation is not on the question path
# --------------------------------------------------------------------------


def test_the_question_path_never_compiles():
    """Answering a question does not compile anything."""
    from yuanwork.api.routes import chat
    from yuanwork.orchestrator import agent

    for module in (chat, agent):
        source = inspect.getsource(module)
        assert "core_compiler" not in source
        assert "CoreCompiler" not in source


def test_the_compiler_is_only_called_from_the_ingest_path():
    """The one place that compiles is the place that indexes."""
    from yuanwork.ingestion import pipeline

    source = inspect.getsource(pipeline)
    assert "CoreCompiler" in source
    assert "_compile_if_core" in source


# --------------------------------------------------------------------------
# the ingest path
# --------------------------------------------------------------------------


class FakeSemantic:
    """The vectors are not the point of this test."""

    def __init__(self):
        self.chunks: list[dict[str, Any]] = []

    async def add_chunks(self, chunks):
        self.chunks.extend(chunks)


async def test_a_core_document_is_compiled_when_it_is_ingested(store, tmp_path: Path):
    """The whole chain: a file goes in, a page comes out."""
    from yuanwork.ingestion.pipeline import IngestionPipeline

    llm = FakeLLM(entity_answer())
    pipeline = IngestionPipeline(
        sqlite_store=store,
        semantic_memory=FakeSemantic(),
        llm_gateway=llm,
    )

    path = tmp_path / "采购合同.md"
    path.write_text(
        "# 采购合同\n\n甲方为华润置地有限公司。\n\n验收期限为 30 日。\n",
        encoding="utf-8",
    )

    result = await pipeline.ingest_file(path)

    assert result["status"] == "indexed"
    assert result["tier"] == "core"
    assert result["compiled"] is True
    assert result["pages"] == 1
    assert llm.tasks, "the model was never asked"

    pages = await store.list_wiki_pages()
    assert len(pages) == 1
    assert pages[0]["tier"] == CORE_TIER


async def test_a_long_tail_document_is_not_compiled(store, tmp_path: Path):
    """The long tail is sliced and searched when a question comes in."""
    from yuanwork.ingestion.pipeline import IngestionPipeline

    llm = FakeLLM(entity_answer())
    pipeline = IngestionPipeline(
        sqlite_store=store,
        semantic_memory=FakeSemantic(),
        llm_gateway=llm,
    )

    path = tmp_path / "周会记录.md"
    path.write_text("# 周会记录\n\n大家吃了饭。\n", encoding="utf-8")

    result = await pipeline.ingest_file(path)

    assert result["status"] == "indexed"
    assert result["tier"] == "longtail"
    assert result["compiled"] is False
    assert llm.tasks == [], "the model was asked about a long-tail document"
    assert await store.list_wiki_pages() == []


async def test_a_core_document_that_cannot_be_compiled_still_gets_indexed(
    store, tmp_path: Path
):
    """A failed compilation does not stop the build."""
    from yuanwork.ingestion.pipeline import IngestionPipeline

    llm = FakeLLM(error=RuntimeError("provider 挂了"))
    pipeline = IngestionPipeline(
        sqlite_store=store,
        semantic_memory=FakeSemantic(),
        llm_gateway=llm,
    )

    path = tmp_path / "采购合同.md"
    path.write_text("# 采购合同\n\n甲方为华润置地有限公司。\n", encoding="utf-8")

    result = await pipeline.ingest_file(path)

    assert result["status"] == "indexed"
    assert result["compiled"] is False

    record = await store.get_doc_tier(result["doc_id"])
    assert record["compile_status"] == "pending_recompile"
