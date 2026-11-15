"""A document that did not compile is not evidence.

When a core document cannot be compiled, the file is marked as "to be
compiled again" and the build goes on with the rest. Its content is not
retrieved: the page says one thing and the file says another, and a
reader who checks one against the other would find a contradiction
nobody can explain.

* AC-E-014 - the failed file is marked, the build continues, and the
  file's content is not retrieved
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from yuanwork.db.sqlite import SQLiteStore
from yuanwork.retrieval.tiered import TieredRetriever


@pytest.fixture
async def store(tmp_path: Path):
    s = SQLiteStore(tmp_path / "store.db")
    await s.initialize()
    yield s
    await s.close()


class FakeLLM:
    """A model that fails on the document that says so."""

    def __init__(self, broken: str = "会让模型挂掉"):
        self.broken = broken
        self.calls: list[str] = []

    async def complete(self, task):
        prompt = task.messages[0].content
        self.calls.append(prompt)
        if self.broken in prompt:
            raise RuntimeError("provider 挂了")
        answer = {
            "entity_pages": [
                {
                    "title": "验收期限",
                    "summary": "验收期限怎么算。",
                    "key_points": ["验收期限怎么约定"],
                    "conclusions": [{"text": "验收期限为 30 日", "segment": 0}],
                }
            ],
            "concept_pages": [],
            "comparison_tables": [],
        }
        return type("R", (), {"content": json.dumps(answer, ensure_ascii=False)})()


class FakeRetriever:
    """The long tail."""

    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    async def search(self, query, top_k=10, doc_ids=None, **kwargs):
        self.calls.append(
            {
                "query": query,
                "doc_ids": doc_ids,
                "exclude_doc_ids": kwargs.get("exclude_doc_ids"),
            }
        )
        return []


class FakeSemantic:
    """The vectors are not the point of this test."""

    async def add_chunks(self, chunks):
        return None


async def ingest(pipeline, path: Path) -> dict[str, Any]:
    return await pipeline.ingest_file(path)


# --------------------------------------------------------------------------
# AC-E-014 — the marker
# --------------------------------------------------------------------------


async def test_a_failed_compilation_marks_the_file(store, tmp_path: Path):
    """The file is marked, so somebody can compile it again later."""
    from yuanwork.ingestion.pipeline import IngestionPipeline

    llm = FakeLLM()
    pipeline = IngestionPipeline(
        sqlite_store=store,
        semantic_memory=FakeSemantic(),
        llm_gateway=llm,
    )

    path = tmp_path / "坏掉的合同.md"
    path.write_text("# 坏掉的合同\n\n这条内容会让模型挂掉。\n", encoding="utf-8")

    result = await ingest(pipeline, path)

    assert result["status"] == "indexed"
    assert result["compiled"] is False

    record = await store.get_doc_tier(result["doc_id"])
    assert record["compile_status"] == "pending_recompile"


async def test_the_build_goes_on_with_the_rest(store, tmp_path: Path):
    """One document that fails does not stop the others."""
    from yuanwork.ingestion.pipeline import IngestionPipeline

    llm = FakeLLM()
    pipeline = IngestionPipeline(
        sqlite_store=store,
        semantic_memory=FakeSemantic(),
        llm_gateway=llm,
    )

    broken = tmp_path / "坏掉的合同.md"
    broken.write_text("# 坏掉的合同\n\n这条内容会让模型挂掉。\n", encoding="utf-8")
    good = tmp_path / "好合同.md"
    good.write_text("# 好合同\n\n验收期限为 30 日。\n", encoding="utf-8")

    result = await pipeline.ingest_directory(tmp_path)

    assert result["indexed"] == 2
    assert result["errors"] == 0

    # The good one has a page; the broken one has a marker.
    pages = await store.list_wiki_pages()
    assert len(pages) == 1

    tiers = await store.list_doc_tiers()
    statuses = {r["compile_status"] for r in tiers}
    assert "pending_recompile" in statuses
    assert "compiled" in statuses


# --------------------------------------------------------------------------
# AC-E-014 — the content is not retrieved
# --------------------------------------------------------------------------


async def test_the_failed_document_is_not_retrieved(store):
    """Its content is not evidence while it is out of date."""
    await store.upsert_doc_tier(
        doc_id="broken",
        tier="core",
        decided_by="rule",
        compile_status="pending_recompile",
    )

    tail = FakeRetriever()
    tiered = TieredRetriever(store, tail)

    await tiered.search("验收期限是多久")

    assert tail.calls, "the long tail was never searched"
    assert "broken" in tail.calls[0]["exclude_doc_ids"]


async def test_a_document_that_compiled_is_retrieved(store):
    await store.upsert_doc_tier(
        doc_id="good",
        tier="longtail",
        decided_by="rule",
        compile_status="compiled",
    )

    tail = FakeRetriever()
    tiered = TieredRetriever(store, tail)

    await tiered.search("验收期限是多久")

    assert "good" not in (tail.calls[0]["exclude_doc_ids"] or [])
