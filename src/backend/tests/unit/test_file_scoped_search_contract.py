"""A question that names a file is answered from that file.

"总结一下示例讲义.pdf中的内容" is a question about *that file*. The core
layer's short circuit is about speed - it must not answer with two
conclusions from a different document and leave the file the reader
asked about unsearched.

* AC-F-006 / AC-F-012 - the reader gets an answer from the document
  they named, and the positions a refusal points at can be opened
* AC-P-009 - the core layer's short circuit still holds for questions
  that do *not* name a file
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from yuanwork.db.sqlite import SQLiteStore
from yuanwork.memory.semantic import spread_rows
from yuanwork.orchestrator.agent import AgentLoop
from yuanwork.retrieval.tiered import TieredRetriever


@pytest.fixture
async def store(tmp_path: Path):
    s = SQLiteStore(tmp_path / "store.db")
    await s.initialize()
    yield s
    await s.close()


class FakeRetriever:
    """The long tail."""

    def __init__(self, chunks: list[dict[str, Any]] | None = None):
        self.chunks = chunks if chunks is not None else []
        self.calls: list[dict[str, Any]] = []

    async def search(self, query, top_k=10, doc_ids=None, **kwargs):
        self.calls.append({"query": query, "top_k": top_k, "doc_ids": doc_ids})
        return list(self.chunks)


async def seed_file(store, doc_id: str, name: str) -> None:
    await store.upsert_file(
        doc_id=doc_id,
        file_name=name,
        file_path=f"D:/kb/{name}",
        file_hash=f"h-{doc_id}",
        file_type="pdf",
        size_bytes=100,
    )


async def seed_core(store, text: str, source_doc_id: str = "other", n: int = 0) -> None:
    await store.upsert_page_conclusion(
        conclusion_id=f"core/{source_doc_id}#{n}",
        page_id="core/版本发布记录",
        text=text,
        anchor={"kind": "page", "page": 1},
        source_doc_id=source_doc_id,
    )


# --------------------------------------------------------------------------
# the file the reader named
# --------------------------------------------------------------------------


async def test_a_file_is_found_whatever_the_spacing(store):
    """The reader types the name they remember; the index has it spelled
    by the file system. One space is enough to lose the file."""
    await seed_file(store, "d5", "人工智能导论（第五讲） 24.pdf")

    found = await store.find_files_by_name("人工智能导论（第五讲）24.pdf")

    assert [f["file_name"] for f in found] == ["人工智能导论（第五讲） 24.pdf"]


async def test_the_scope_is_the_file_the_question_named():
    """The scope is what the question asked about - and nothing else."""
    state = SimpleNamespace(
        understanding={"file_hint": "人工智能导论（第五讲）24.pdf"},
        candidates=[
            {"file_name": "人工智能导论（第五讲） 24.pdf", "doc_id": "d5"},
            {"file_name": "CHANGELOG.md", "doc_id": "d-changelog"},
        ],
    )

    scope = AgentLoop._scope_from_file_hint(None, state)

    assert scope == ["d5"]


def test_a_hint_that_matches_nothing_does_not_scope_the_search():
    """A wrong scope would lose the answer - so there is none."""
    state = SimpleNamespace(
        understanding={"file_hint": "一份不存在的文件.pdf"},
        candidates=[{"file_name": "CHANGELOG.md", "doc_id": "d-changelog"}],
    )

    assert AgentLoop._scope_from_file_hint(None, state) is None


def test_a_question_without_a_file_hint_is_not_scoped():
    state = SimpleNamespace(understanding={}, candidates=[])
    assert AgentLoop._scope_from_file_hint(None, state) is None


# --------------------------------------------------------------------------
# the short circuit
# --------------------------------------------------------------------------


async def test_the_core_layer_does_not_short_circuit_a_named_file(store):
    """Two conclusions about another document are not an answer to a
    question about this one - so the long tail is searched too."""
    await seed_file(store, "d5", "人工智能导论（第五讲） 24.pdf")
    await seed_core(store, "ES2025 版本的变更记录", source_doc_id="other", n=1)
    await seed_core(store, "版本变更记录里的 GeneratorResumeAbrupt", source_doc_id="other", n=2)
    tail = FakeRetriever(
        [{"id": "c1", "doc_id": "d5", "text": "搜索技术", "file_name": "第五讲"}]
    )
    tiered = TieredRetriever(store, tail)

    result = await tiered.search(
        "总结一下第五讲的内容", scope_doc_ids=["d5"]
    )

    assert result["longtail_searched"] is True
    assert tail.calls, "the long tail was not searched"
    assert tail.calls[0]["doc_ids"] == ["d5"]


async def test_the_short_circuit_still_holds_without_a_scope(store):
    """The speed promise is unchanged for questions that do not name a
    file (AC-P-009)."""
    await seed_file(store, "d5", "人工智能导论（第五讲） 24.pdf")
    await seed_core(store, "ES2025 版本的变更记录", source_doc_id="other", n=1)
    await seed_core(store, "版本变更记录里的 GeneratorResumeAbrupt", source_doc_id="other", n=2)
    tail = FakeRetriever([{"id": "c1", "doc_id": "d5", "text": "搜索技术"}])
    tiered = TieredRetriever(store, tail)

    result = await tiered.search("版本变更记录")

    assert result["core_found"] is True
    assert result["longtail_searched"] is False
    assert not tail.calls


async def test_the_core_evidence_of_a_scoped_search_is_that_file(store):
    """A conclusion about another document is not evidence for this
    question."""
    await seed_file(store, "d5", "示例讲义.pdf")
    await seed_core(store, "第五讲讲的是搜索技术", source_doc_id="d5")
    await seed_core(store, "ES2025 版本的变更记录", source_doc_id="other")
    tiered = TieredRetriever(store, FakeRetriever())

    result = await tiered.search("第五讲讲了什么", scope_doc_ids=["d5"])

    texts = [item.get("text", "") for item in result["evidence"]]
    assert any("搜索技术" in t for t in texts)
    assert not any("ES2025" in t for t in texts)


# --------------------------------------------------------------------------
# the summary
# --------------------------------------------------------------------------


class FakeSemantic:
    """A document with many chunks."""

    def __init__(self, chunks: list[dict[str, Any]]):
        self.chunks = chunks

    async def chunks_of_doc(self, doc_id: str, limit: int = 12):
        return spread_rows(self.chunks, limit)


async def test_a_summary_reads_the_file_spread_out(store):
    """A summary of a document needs the document's breadth - the ten
    chunks that match the question say nothing about what it is about
    (the question *is* the file name)."""
    await seed_file(store, "d5", "示例讲义.pdf")
    chunks = [
        {
            "id": f"c{i}",
            "doc_id": "d5",
            "chunk_idx": i,
            "text": f"第 {i} 段",
            "file_name": "示例讲义.pdf",
            "file_path": "D:/kb/示例讲义.pdf",
        }
        for i in range(153)
    ]

    class RetrieverWithSemantic(FakeRetriever):
        def __init__(self, semantic):
            super().__init__([])
            self.semantic = semantic

    tiered = TieredRetriever(store, RetrieverWithSemantic(FakeSemantic(chunks)))

    result = await tiered.search(
        "总结一下第五讲的内容", scope_doc_ids=["d5"], summary=True
    )

    assert result["longtail_searched"] is True
    assert len(result["evidence"]) == 20
    # spread over the document, not the first chunks
    assert result["evidence"][0]["chunk_idx"] == 0
    assert result["evidence"][-1]["chunk_idx"] == 152
    # every piece comes from the file that was named
    assert {e["doc_id"] for e in result["evidence"]} == {"d5"}


# --------------------------------------------------------------------------
# the positions a refusal points at
# --------------------------------------------------------------------------


async def test_the_positions_of_a_refusal_carry_the_path():
    """The refusal tells the reader to click the position - so the
    position has to be openable."""
    from yuanwork.orchestrator.steps.answer import refusal_positions

    positions = refusal_positions(
        [
            {
                "file_name": "示例讲义.pdf",
                "file_path": "D:/kb/示例讲义.pdf",
                "anchor": {"kind": "page", "page": 12},
            }
        ],
        [{"file_name": "CHANGELOG.md", "file_path": "D:/kb/CHANGELOG.md"}],
    )

    assert positions[0]["file_name"] == "示例讲义.pdf"
    assert positions[0]["file_path"] == "D:/kb/示例讲义.pdf"
    assert positions[0]["location"] == "第 12 页"
    assert positions[1]["file_path"] == "D:/kb/CHANGELOG.md"


def test_the_spread_covers_the_whole_document():
    """The first row, the last row, and the ones in between."""
    rows = [{"chunk_idx": i} for i in range(153)]

    picked = spread_rows(rows, 10)

    assert len(picked) == 10
    assert picked[0]["chunk_idx"] == 0
    assert picked[-1]["chunk_idx"] == 152
    assert picked[1]["chunk_idx"] == 17


def test_a_short_document_is_not_spread():
    rows = [{"chunk_idx": i} for i in range(5)]
    assert spread_rows(rows, 10) == rows
    assert spread_rows([], 10) == []
