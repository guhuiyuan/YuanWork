"""The ledger and the index must agree.

A build writes the ledger *before* the slices are flushed - so a build
that is interrupted (the application is killed, the backend dies with
it) leaves files that are "indexed" with nothing in the index. The
reader then asks about one of those files and is told the knowledge
base does not have the answer - which is a different thing from what
happened.

* AC-F-032 - the reader is told the truth, and a rebuild puts the
  files back
* AC-F-002 - the knowledge base really covers the files of the
  workspace
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from yuanwork.db.sqlite import SQLiteStore
from yuanwork.ingestion.pipeline import IngestionPipeline
from yuanwork.orchestrator.steps.answer import REFUSAL_OPENINGS
from yuanwork.retrieval.tiered import TieredRetriever


# --------------------------------------------------------------------------
# the build: the index decides, not the ledger
# --------------------------------------------------------------------------


class _Semantic:
    """An index that holds the documents it is told about."""

    embedding_max_tokens = 512
    embedding_model_name = "BAAI/bge-m3"

    def __init__(self, doc_ids: set[str] | None = None):
        self.doc_ids = doc_ids or set()

    async def indexed_doc_ids(self) -> set[str]:
        return set(self.doc_ids)


class _Store:
    """A ledger that claims a file is indexed."""

    def __init__(self, record: dict[str, Any] | None):
        self.record = record

    async def get_file_by_hash(self, file_hash: str) -> dict | None:
        return self.record


def _pipeline(store: _Store, semantic: _Semantic) -> IngestionPipeline:
    return IngestionPipeline(sqlite_store=store, semantic_memory=semantic)


def test_a_file_the_index_really_holds_is_skipped(tmp_path: Path):
    pipeline = _pipeline(_Store(None), _Semantic({"d1"}))
    pipeline.sqlite = _Store(
        {"doc_id": "d1", "chunk_version": pipeline.chunker.version}
    )

    state = asyncio.run(pipeline.resume_state(tmp_path / "a.md", "hash"))

    assert state["state"] == "already_indexed"
    assert state["doc_id"] == "d1"


def test_a_file_the_ledger_has_but_the_index_does_not_is_read_again(
    tmp_path: Path,
):
    """The case that made a file unfindable *and* unfixable.

    The ledger says the file is indexed; the index says it is not.
    Trusting the ledger meant the build skipped it - so it could never
    be repaired either (TASK-088)."""
    pipeline = _pipeline(_Store(None), _Semantic(set()))
    pipeline.sqlite = _Store(
        {"doc_id": "d1", "chunk_version": pipeline.chunker.version}
    )

    state = asyncio.run(pipeline.resume_state(tmp_path / "a.md", "hash"))

    assert state["state"] == "new"
    assert state["doc_id"] is None


def test_a_store_that_cannot_say_leaves_the_ledger_in_charge(tmp_path: Path):
    """A stand-in without the question keeps the old behaviour."""
    class _Blind:
        embedding_max_tokens = 512
        embedding_model_name = "BAAI/bge-m3"

    pipeline = _pipeline(_Store(None), _Blind())
    pipeline.sqlite = _Store(
        {"doc_id": "d1", "chunk_version": pipeline.chunker.version}
    )

    state = asyncio.run(pipeline.resume_state(tmp_path / "a.md", "hash"))

    assert state["state"] == "already_indexed"


# --------------------------------------------------------------------------
# the answer: the truth, not "证据不足"
# --------------------------------------------------------------------------


class _FakeSemantic:
    """An index that holds nothing."""

    def __init__(self, chunks: list[dict[str, Any]] | None = None):
        self.chunks = chunks or []

    async def chunks_of_doc(self, doc_id: str, limit: int = 12):
        return list(self.chunks)


class _LedgerStore:
    def __init__(self, files: dict[str, dict[str, Any]] | None = None):
        self.files = files or {}

    async def get_file(self, doc_id: str) -> dict[str, Any] | None:
        return self.files.get(doc_id)

    async def list_active_conclusions(self, limit: int = 200):
        return []

    async def list_wiki_pages(self, limit: int = 500):
        return []


class _LongTail:
    def __init__(self, semantic):
        self.semantic = semantic

    async def search(self, query, top_k=10, doc_ids=None, **kwargs):
        return []


async def test_a_file_missing_from_the_index_is_reported(tmp_path: Path):
    """The question names a file; the ledger has it; the index does not."""
    store = SQLiteStore(tmp_path / "store.db")
    await store.initialize()
    await store.upsert_file(
        doc_id="d1",
        file_name="Vibe Coding.md",
        file_path="D:/kb/Vibe Coding.md",
        file_hash="h1",
        file_type="md",
        size_bytes=100,
        chunk_count=13,
    )
    tiered = TieredRetriever(store, _LongTail(_FakeSemantic()))

    result = await tiered.search(
        "总结一下Vibe Coding.md中的内容", scope_doc_ids=["d1"], summary=True
    )

    assert result["evidence"] == []
    assert result["index_missing"] is True
    await store.close()


async def test_a_file_the_index_has_is_not_reported(tmp_path: Path):
    store = SQLiteStore(tmp_path / "store.db")
    await store.initialize()
    await store.upsert_file(
        doc_id="d1",
        file_name="示例讲义.pdf",
        file_path="D:/kb/示例讲义.pdf",
        file_hash="h1",
        file_type="pdf",
        size_bytes=100,
        chunk_count=13,
    )
    semantic = _FakeSemantic([{"id": "c1", "doc_id": "d1", "text": "x"}])
    tiered = TieredRetriever(store, _LongTail(semantic))

    result = await tiered.search(
        "总结一下第五讲的内容", scope_doc_ids=["d1"], summary=True
    )

    assert result.get("index_missing") is not True
    await store.close()


async def test_a_question_without_a_file_is_never_about_a_missing_file(
    tmp_path: Path,
):
    """Without a file named, there is nothing to be missing."""
    store = SQLiteStore(tmp_path / "store.db")
    await store.initialize()
    tiered = TieredRetriever(store, _LongTail(_FakeSemantic()))

    result = await tiered.search("合同违约金怎么算")

    assert result.get("index_missing") is not True
    await store.close()


# --------------------------------------------------------------------------
# the wording
# --------------------------------------------------------------------------


def test_the_refusal_says_the_file_is_missing_from_the_index():
    """Not "the documents are silent" - the file is not in the index."""
    opening = REFUSAL_OPENINGS["index_missing"]
    assert "索引" in opening
    assert "账本" in opening


def test_the_refusal_template_tells_the_reader_to_index_the_file_again():
    from yuanwork.orchestrator.steps.answer import _jinja_env

    template = _jinja_env.get_template("refusal.j2")
    out = template.render(
        opening="x",
        reason="index_missing",
        positions=[{"file_name": "Vibe Coding.md", "location": ""}],
    )

    assert "重新索引这份文件" in out


# --------------------------------------------------------------------------
# the agent: the reason the reader is given
# --------------------------------------------------------------------------


async def test_the_agent_says_the_file_is_missing_from_the_index():
    """The gate said "there is no evidence"; the truth is "the file is
    not in the index" - and that is what the reader is told."""
    from yuanwork.orchestrator.agent import AgentLoop, AgentState
    from yuanwork.retrieval.evidence_gate import EvidenceGateResult

    class _LLM:
        async def complete(self, task):
            return type("R", (), {"content": "{}"})()

    class _Semantic:
        async def count(self) -> int:
            return 10

    agent = AgentLoop(
        llm_gateway=_LLM(),
        semantic_memory=_Semantic(),  # type: ignore[arg-type]
        episodic_memory=None,  # type: ignore[arg-type]
        consolidation_engine=None,
        wiki_compile_trigger=None,
    )
    state = AgentState(
        query="总结一下Vibe Coding.md中的内容",
        session_id="s1",
        index_missing=True,
        gate=EvidenceGateResult(
            passed=False,
            hard_rule_ok=False,
            model_ok=True,
            sufficient_count=0,
            reason="insufficient_evidence",
        ),
    )

    await agent._apply_refusal(state)

    assert state.refused is True
    assert state.refusal_reason == "index_missing"
    assert "索引" in state.answer
