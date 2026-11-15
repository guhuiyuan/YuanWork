"""The tier promises, end to end.

The core layer is the documents other documents are read against. The
promises about it are these:

* a core document is compiled into pages when it comes in, and every
  conclusion says where in the original it was read (AC-F-022, AC-F-023)
* when a revision contradicts what the page said, both versions are
  kept (AC-F-024)
* a question looks at the core layer first, and only goes to the long
  tail when the core layer cannot answer (AC-F-025)
* when the two layers disagree, the core layer wins and the answer says
  so (AC-E-016)

Everything here is real except the model: the store, the parsers, the
pipeline, the compiler, the retrieval and the agent are the ones the
product runs with.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from yuanwork.api import deps
from yuanwork.db.sqlite import SQLiteStore
from yuanwork.ingestion.pipeline import IngestionPipeline
from yuanwork.orchestrator.agent import AgentLoop
from yuanwork.retrieval.tiered import TieredRetriever

from unit.ingestion_fixtures import make_pdf_file

from .harness import (
    RecordingLongTail,
    RecordingSemantic,
    TieredLLM,
    make_tiered_agent,
)


@pytest.fixture
async def store(tmp_path: Path):
    s = SQLiteStore(tmp_path / "store.db")
    await s.initialize()
    yield s
    await s.close()


async def build(store, tmp_path: Path, llm: TieredLLM) -> dict[str, Any]:
    """Ingest a core document and return what came out of it."""
    semantic = RecordingSemantic()
    pipeline = IngestionPipeline(
        sqlite_store=store,
        semantic_memory=semantic,
        llm_gateway=llm,
    )

    # A contract: two pages, so the conclusions have a page to point at.
    contract = make_pdf_file(
        tmp_path,
        name="采购合同.pdf",
        pages=[
            "甲方为华润置地有限公司。验收期限为 30 日。",
            "验收期限自交付之日起计算。违约金为合同金额的百分之五。",
        ],
    )

    result = await pipeline.ingest_file(contract)
    return {"pipeline": pipeline, "result": result, "semantic": semantic}


def make_agent(store, llm, longtail: RecordingLongTail) -> AgentLoop:
    """An agent with the real steps and a faked model."""
    return make_tiered_agent(store, llm, longtail)


# --------------------------------------------------------------------------
# AC-F-022 + AC-F-023 — the core document becomes a page
# --------------------------------------------------------------------------


async def test_a_core_document_is_compiled_into_pages(store, tmp_path: Path):
    """The document is read when it comes in, and the pages are there."""
    llm = TieredLLM()
    built = await build(store, tmp_path, llm)

    assert built["result"]["status"] == "indexed"
    assert built["result"]["tier"] == "core"
    assert built["result"]["compiled"] is True

    pages = await store.list_wiki_pages()
    assert len(pages) == 1
    assert pages[0]["page_type"] == "concept"
    assert pages[0]["tier"] == "core"


async def test_the_conclusion_says_which_page_it_was_read_from(store, tmp_path: Path):
    """AC-F-023 - the reader can go back to the page it came from."""
    llm = TieredLLM()
    await build(store, tmp_path, llm)

    conclusions = await store.list_page_conclusions("core/验收期限")
    assert len(conclusions) == 2
    assert conclusions[0]["anchor"] == {"kind": "page", "page": 1}
    assert conclusions[0]["source_doc_id"]


# --------------------------------------------------------------------------
# AC-F-025 — the core layer answers first
# --------------------------------------------------------------------------


async def test_a_question_is_answered_from_the_core_layer(store, tmp_path: Path):
    """The compiled conclusions are the evidence."""
    llm = TieredLLM()
    await build(store, tmp_path, llm)

    longtail = RecordingLongTail()
    agent = make_agent(store, llm, longtail)

    result = await agent.run(query="验收期限是多久？", session_id="s1")

    assert result["refused"] is False
    assert result["citations"], "the answer has no citation"
    citation = result["citations"][0]
    assert citation["file_name"] == "采购合同.pdf"
    # The page it was read from - the reader can open it there.
    assert citation["page"] == 1
    assert citation["location"] == "第 1 页"
    assert citation["anchor"]["kind"] == "page"


async def test_the_long_tail_is_not_searched_when_the_core_answers(store, tmp_path: Path):
    """AC-F-025 - the core layer is enough, so the long tail is not
    touched."""
    llm = TieredLLM()
    await build(store, tmp_path, llm)

    longtail = RecordingLongTail(
        [{"id": "c1", "text": "验收期限在附件里", "meta": {"page": 3}}]
    )
    agent = make_agent(store, llm, longtail)

    await agent.run(query="验收期限是多久？", session_id="s1")

    assert longtail.calls == [], "the long tail was searched anyway"


async def test_the_long_tail_answers_when_the_core_cannot(store, tmp_path: Path):
    """The core layer is silent about this - the long tail answers."""
    llm = TieredLLM()
    await build(store, tmp_path, llm)

    longtail = RecordingLongTail(
        [
            {
                "id": "c1",
                "text": "验收期限在附件的第三页",
                "file_name": "附件.pdf",
                "file_path": "/kb/附件.pdf",
                "meta": {"page": 3},
                "_score": 0.8,
            },
            {
                "id": "c2",
                "text": "验收期限的说明也在附件里",
                "file_name": "附件.pdf",
                "file_path": "/kb/附件.pdf",
                "meta": {"page": 4},
                "_score": 0.7,
            },
        ]
    )
    agent = make_agent(store, llm, longtail)

    result = await agent.run(query="今天午饭吃什么？", session_id="s1")

    assert longtail.calls, "the long tail was never searched"
    assert result["refused"] is False
    assert result["citations"][0]["file_name"] == "附件.pdf"


# --------------------------------------------------------------------------
# AC-F-024 + AC-E-016 — the two versions and the two layers
# --------------------------------------------------------------------------


async def test_a_revision_keeps_both_versions(store, tmp_path: Path):
    """AC-F-024 - the old conclusion is not quietly replaced."""
    llm = TieredLLM()
    built = await build(store, tmp_path, llm)
    doc_id = built["result"]["doc_id"]

    # The contract is revised and indexed again.
    llm.conclusions = [{"text": "验收期限为 15 日", "segment": 0}]
    llm.contradictions = {
        "contradictions": [
            {
                "old_index": 0,
                "new_index": 0,
                "topic": "验收期限",
                "reason": "旧版 30 日，新版 15 日",
            }
        ]
    }

    revised = make_pdf_file(
        tmp_path,
        name="采购合同.pdf",
        pages=["甲方为华润置地有限公司。验收期限为 15 日。"],
    )
    await built["pipeline"].ingest_file(revised, force=True)

    conclusions = await store.list_page_conclusions("core/验收期限", active_only=False)
    texts = {c["text"] for c in conclusions}
    assert "验收期限为 30 日" in texts
    assert "验收期限为 15 日" in texts

    # And the difference is put side by side.
    tables = [p for p in await store.list_wiki_pages() if p["page_type"] == "comparison"]
    assert len(tables) == 1
    assert "30 日" in tables[0]["markdown"]
    assert "15 日" in tables[0]["markdown"]
    assert doc_id


async def test_the_core_layer_wins_when_the_two_layers_disagree(store, tmp_path: Path):
    """AC-E-016 - the core layer is the answer, and the answer says so."""
    # One core conclusion: it is a hint, not an answer - so the long
    # tail is searched too, and the two layers are compared.
    llm = TieredLLM(conclusions=[{"text": "验收期限为 30 日", "segment": 0}])
    await build(store, tmp_path, llm)

    # A slice of the old version is still lying around in the long tail.
    longtail = RecordingLongTail(
        [
            {
                "id": "c1",
                "text": "验收期限为 15 日",
                "file_name": "旧合同.pdf",
                "file_path": "/kb/旧合同.pdf",
                "meta": {"page": 1},
                "_score": 0.8,
            },
            {
                "id": "c2",
                "text": "验收期限的旧规定",
                "file_name": "旧合同.pdf",
                "file_path": "/kb/旧合同.pdf",
                "meta": {"page": 2},
                "_score": 0.7,
            },
        ]
    )
    agent = make_agent(store, llm, longtail)

    result = await agent.run(query="验收期限是多久？", session_id="s1")

    # The core conclusion is the first piece of evidence - it is the
    # one the answer leans on.
    assert result["citations"], "the answer has no citation"
    assert result["citations"][0]["file_name"] == "采购合同.pdf"
    assert "30 日" in result["citations"][0]["text"]

    # And the reader is told that the two layers disagree.
    extract = [step for step in result["trace"] if step.get("step") == "extract"]
    assert extract and extract[0]["tier_conflicts"] == 1, "the divergence was not seen"
