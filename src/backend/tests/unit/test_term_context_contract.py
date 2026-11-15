"""TASK-049 — the context of a term comes from the original text.

The target user may not know the professional terms in their own
documents. Two ways to help: let the model write an explanation, or
show the term in the text it stands for. The model writes unverifiable
text - and the whole value of the product is that what the reader is
told can be checked. So the context comes from the documents, with the
place it came from, and a term that is not in the documents has no
context at all.

* AC-F-030 — the term's context in the original text, with its
  position; nothing is generated, and a term that is not there is
  marked as not found
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from yuanwork.api import deps
from yuanwork.api.routes import kb as kb_routes
from yuanwork.retrieval.hybrid import TermContext, context_around


# --------------------------------------------------------------------------
# the pieces
# --------------------------------------------------------------------------


class FakeRetriever:
    """A retriever that hands back what it was given."""

    def __init__(self, chunks: list[dict[str, Any]] | None = None):
        self.chunks = chunks or []
        self.asked: list[str] = []

    async def search_term(self, term: str, top_k: int = 5):
        self.asked.append(term)
        return self.chunks[:top_k]


def chunk(text: str, **kwargs) -> dict[str, Any]:
    """A chunk from the knowledge base."""
    base = {
        "id": "c1",
        "doc_id": "d1",
        "file_name": "采购合同.md",
        "text": text,
        "meta": {"page": 12},
    }
    base.update(kwargs)
    return base


class FakeSemanticMemory:
    """A knowledge base that holds nothing."""

    async def search_hybrid(self, **kwargs):
        return []

    async def search(self, **kwargs):
        return []

    async def close(self):
        return None


@pytest.fixture
async def store(tmp_path: Path):
    from yuanwork.db.sqlite import SQLiteStore

    s = SQLiteStore(tmp_path / "store.db")
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
def client() -> TestClient:
    """The knowledge base routes, on their own."""
    deps.set_semantic_memory(FakeSemanticMemory())  # type: ignore[arg-type]
    app = FastAPI()
    app.include_router(kb_routes.router, prefix="/api/v1")
    yield TestClient(app)
    deps.reset_dependencies()


# --------------------------------------------------------------------------
# the passage around the term
# --------------------------------------------------------------------------


def test_the_context_is_the_sentence_the_term_stands_in():
    text = "第十二条 履约保证金为合同金额的百分之五。逾期十五日仍未支付的，视为违约。"

    snippet = context_around(text, "履约保证金")

    assert "履约保证金为合同金额的百分之五" in snippet


def test_a_very_long_sentence_is_cut_around_the_term():
    text = "前置说明。" + ("这是一段很长的说明文字。" * 40) + "关键词在这里。" + ("后面还有很多话。" * 40)

    snippet = context_around(text, "关键词")

    assert "关键词在这里" in snippet
    assert len(snippet) < 400


def test_a_term_that_is_not_in_the_text_has_no_context():
    assert context_around("这里没有那个词。", "履约保证金") == ""


# --------------------------------------------------------------------------
# the lookup
# --------------------------------------------------------------------------


async def test_the_lookup_returns_the_context_and_the_place():
    """The passage and where it came from."""
    retriever = FakeRetriever([
        chunk("第十二条 履约保证金为合同金额的百分之五。"),
    ])

    result = await TermContext(retriever).lookup("履约保证金")

    assert result["found"] is True
    assert result["term"] == "履约保证金"
    assert len(result["contexts"]) == 1

    context = result["contexts"][0]
    assert "履约保证金" in context["text"]
    assert context["file_name"] == "采购合同.md"
    assert context["location"] == "第 12 页"
    assert context["page"] == 12


async def test_a_term_that_is_not_in_the_knowledge_base_is_marked_as_not_found():
    """Nothing is made up for it."""
    retriever = FakeRetriever([])

    result = await TermContext(retriever).lookup("不存在的术语")

    assert result["found"] is False
    assert result["contexts"] == []


async def test_an_empty_term_finds_nothing():
    retriever = FakeRetriever([])

    result = await TermContext(retriever).lookup("   ")

    assert result["found"] is False
    assert retriever.asked == []


async def test_a_lookup_that_fails_finds_nothing():
    """A failed lookup is not a found term."""

    class BrokenRetriever:
        async def search_term(self, term, top_k=5):
            raise RuntimeError("the store is gone")

    result = await TermContext(BrokenRetriever()).lookup("履约保证金")

    assert result["found"] is False
    assert result["contexts"] == []


async def test_the_context_comes_from_the_original_text():
    """The passage is the document's own words (ADR-041)."""
    text = "第九条 不可抗力是指不能预见、不能避免且不能克服的客观情况。"
    retriever = FakeRetriever([chunk(text)])

    result = await TermContext(retriever).lookup("不可抗力")

    assert "不能预见、不能避免且不能克服" in result["contexts"][0]["text"]


# --------------------------------------------------------------------------
# the terms of an answer
# --------------------------------------------------------------------------


def test_the_terms_of_an_answer_are_the_ones_the_knowledge_base_knows():
    """A word with no context behind it is no use to the reader."""
    from yuanwork.orchestrator.steps.answer import extract_terms

    answer = "履约保证金按合同金额的百分之五缴纳，逾期即视为违约。"

    terms = extract_terms(answer, ["履约保证金", "违约金", "不可抗力"])

    assert terms == ["履约保证金"]


def test_the_longest_term_wins():
    """「履约保证金」 before 「保证金」 - the whole term, not a part."""
    from yuanwork.orchestrator.steps.answer import extract_terms

    answer = "履约保证金按约定缴纳。"

    terms = extract_terms(answer, ["保证金", "履约保证金"])

    assert terms == ["履约保证金"]


def test_an_answer_without_terms_has_none():
    from yuanwork.orchestrator.steps.answer import extract_terms

    assert extract_terms("这个项目的背景我找了一下。", ["履约保证金"]) == []
    assert extract_terms("", ["履约保证金"]) == []


# --------------------------------------------------------------------------
# the answer carries its terms
# --------------------------------------------------------------------------


async def test_the_answer_carries_the_terms_it_uses(store):
    """The reader is told which words they can look up."""
    from yuanwork.orchestrator.agent import AgentLoop

    await store.upsert_entity(
        entity_id="e-1",
        name="履约保证金",
        entity_type="concept",
        properties={"kind": "entity"},
        source_event_ids=[1],
        status="active",
    )

    agent = AgentLoop(
        llm_gateway=None,  # type: ignore[arg-type]
        semantic_memory=None,  # type: ignore[arg-type]
        episodic_memory=None,  # type: ignore[arg-type]
        sqlite_store=store,
    )

    terms = await agent._answer_terms("履约保证金按合同金额的百分之五缴纳。")

    assert terms == ["履约保证金"]


async def test_an_answer_without_known_terms_carries_none(store):
    from yuanwork.orchestrator.agent import AgentLoop

    agent = AgentLoop(
        llm_gateway=None,  # type: ignore[arg-type]
        semantic_memory=None,  # type: ignore[arg-type]
        episodic_memory=None,  # type: ignore[arg-type]
        sqlite_store=store,
    )

    assert await agent._answer_terms("这个项目的背景我找了一下。") == []


# --------------------------------------------------------------------------
# the interface
# --------------------------------------------------------------------------


async def test_the_term_context_is_asked_for_over_http(client):
    """The reader asks for the term's context."""
    response = client.get("/api/v1/kb/terms/履约保证金/context")

    assert response.status_code == 200
    body = response.json()
    assert body["term"] == "履约保证金"
    # The knowledge base of the test is empty - the term is not found,
    # and it says so.
    assert body["found"] is False
    assert body["contexts"] == []
