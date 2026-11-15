"""Tests for the location metadata of a citation.

The bug these guard against: a chunk carries its location in two
different shapes depending on where it is in the pipeline.

- right after chunking the parser's metadata is still nested:
  ``{"id": ..., "meta": {"page": 12}}``
- after retrieval the location fields are flat columns:
  ``{"id": ..., "page": 12}``

The retrieval side is what the orchestrator and the prompt builder see,
and both used to read the nested shape - so the page number silently
became ``None``. The prompt demands "第 N 页" / "Slide N" citations, so
a missing page number means the model is asked to do something it
cannot do, and the user gets a citation that says only *which file*.
"""

from __future__ import annotations

import json

from typing import Any

import pytest


# ===== The two shapes =====

CHUNKER_SHAPE = {
    "id": "c1",
    "doc_id": "d1",
    "text": "第十二条 违约金为每日万分之三。",
    "type": "text",
    "meta": {"page": 12, "page_total": 30},
}

RETRIEVAL_SHAPE = {
    "id": "c1",
    "doc_id": "d1",
    "text": "第十二条 违约金为每日万分之三。",
    "file_name": "contract.pdf",
    "file_path": "/kb/contract.pdf",
    "page": 12,
    "slide_number": None,
    "section": None,
}


# ===== chunk_meta =====


def test_chunk_meta_reads_the_nested_shape():
    from yuanwork.utils.chunk_meta import chunk_meta

    assert chunk_meta(CHUNKER_SHAPE)["page"] == 12


def test_chunk_meta_reads_the_flat_shape():
    from yuanwork.utils.chunk_meta import chunk_meta

    assert chunk_meta(RETRIEVAL_SHAPE)["page"] == 12


def test_chunk_meta_reads_the_slide_number():
    from yuanwork.utils.chunk_meta import chunk_meta

    chunk = {"slide_number": 7}
    assert chunk_meta(chunk)["slide_number"] == 7
    chunk = {"meta": {"slide_number": 7}}
    assert chunk_meta(chunk)["slide_number"] == 7


def test_chunk_meta_survives_a_missing_or_broken_meta():
    from yuanwork.utils.chunk_meta import chunk_meta

    assert chunk_meta({"page": 3})["page"] == 3
    assert chunk_meta({"meta": None, "page": 3})["page"] == 3
    assert chunk_meta({"meta": "not a dict"}) == {}
    assert chunk_meta({}) == {}


def test_chunk_location_is_human_readable():
    from yuanwork.utils.chunk_meta import chunk_location

    assert chunk_location(RETRIEVAL_SHAPE) == "第 12 页"
    assert chunk_location({"slide_number": 4}) == "Slide 4"
    assert chunk_location({}) == ""


# ===== The chunker really produces the nested shape =====


def test_the_chunker_keeps_the_page_nested():
    """If the chunker changed shape, this test fails and the fix above
    has to be revisited."""
    from yuanwork.ingestion.chunker.structural_chunker import StructuralChunker
    from yuanwork.ingestion.parser.base import RawBlock

    block = RawBlock(
        block_id="d1_p12_text",
        doc_id="d1",
        content="第十二条 违约金为每日万分之三。" * 8,
        type="text",
        meta={"page": 12},
    )
    chunk = StructuralChunker().chunk([block], "d1")[0]

    assert chunk["meta"]["page"] == 12
    # And the flat key is NOT there - that is exactly why the ingestion
    # side has to look at the nested shape.
    assert chunk.get("page") is None


# ===== The citation builder =====


def test_build_citations_keeps_the_page_of_a_retrieved_chunk():
    from yuanwork.orchestrator.agent import AgentLoop

    agent = AgentLoop(
        llm_gateway=None,  # type: ignore[arg-type]
        semantic_memory=None,  # type: ignore[arg-type]
        episodic_memory=None,  # type: ignore[arg-type]
    )
    citations = agent._build_citations([RETRIEVAL_SHAPE])

    assert len(citations) == 1
    assert citations[0]["page"] == 12
    assert citations[0]["file_name"] == "contract.pdf"


def test_build_citations_keeps_the_page_of_a_fresh_chunk():
    from yuanwork.orchestrator.agent import AgentLoop

    agent = AgentLoop(
        llm_gateway=None,  # type: ignore[arg-type]
        semantic_memory=None,  # type: ignore[arg-type]
        episodic_memory=None,  # type: ignore[arg-type]
    )
    citations = agent._build_citations([CHUNKER_SHAPE])

    assert citations[0]["page"] == 12


def test_build_citations_keeps_the_section_of_a_docx_chunk():
    from yuanwork.orchestrator.agent import AgentLoop

    agent = AgentLoop(
        llm_gateway=None,  # type: ignore[arg-type]
        semantic_memory=None,  # type: ignore[arg-type]
        episodic_memory=None,  # type: ignore[arg-type]
    )
    citations = agent._build_citations([{"meta": {"heading": "违约责任"}}])

    assert citations[0]["section"] == "违约责任"


# ===== The prompt builder =====


def test_the_answer_prompt_carries_the_page_number():
    """The prompt tells the model to cite "第 N 页" - so the page number
    has to be in the prompt."""
    from yuanwork.orchestrator.steps.answer import _build_answer_task

    task = _build_answer_task(
        query="违约金是多少？",
        evidence=[RETRIEVAL_SHAPE],
        confidence={"confidence": 0.9},
    )
    prompt = task.messages[0].content

    assert "第 12 页" in prompt
    assert "contract.pdf" in prompt


def test_the_answer_prompt_carries_the_slide_number():
    from yuanwork.orchestrator.steps.answer import _build_answer_task

    task = _build_answer_task(
        query="核心功能？",
        evidence=[{"file_name": "roadmap.pptx", "text": "...", "slide_number": 4}],
        confidence={"confidence": 0.9},
    )
    prompt = task.messages[0].content

    assert "Slide 4" in prompt


# ===== The ingestion side =====


@pytest.mark.asyncio
async def test_add_chunks_stores_the_anchor_from_the_nested_shape(monkeypatch):
    """The chunker hands the position over nested - the store has to
    carry it into the anchor column, or it is lost for good."""
    from yuanwork.memory.semantic import SemanticMemory

    stored: dict[str, Any] = {}

    class _FakeTable:
        def delete(self, *args, **kwargs):
            pass

        def add(self, records):
            stored["records"] = records

    class _FakeDB:
        def open_table(self, name):
            return _FakeTable()

    semantic = SemanticMemory.__new__(SemanticMemory)
    semantic._db = _FakeDB()
    monkeypatch.setattr(
        SemanticMemory, "get_or_create_chunks_table", lambda self: _FakeDB().open_table("chunks")
    )
    monkeypatch.setattr(
        SemanticMemory, "embed_texts", _fake_embed_texts
    )
    monkeypatch.setattr(SemanticMemory, "_index_sparse", _fake_index_sparse, raising=False)

    await semantic.add_chunks([CHUNKER_SHAPE])

    records = stored["records"]
    assert len(records) == 1
    anchor = json.loads(records[0]["anchor"])
    assert anchor["kind"] == "page"
    assert anchor["page"] == 12


async def _fake_embed_texts(self, texts, return_sparse=False):
    return [[0.0] * 1024 for _ in texts], [None for _ in texts]


async def _fake_index_sparse(self, *args, **kwargs):
    return None
