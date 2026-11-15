"""Refusal and citation building.

A refusal has to be actionable:

* it says *where* the answer probably is - the file and the page or
  the section, in words a reader understands, not internal ids
* it says *what to do* - how to rephrase the question, which documents
  to add
* it gives no conclusion of its own: a refusal that asserts something
  is a refusal that can be wrong

The citations are the other half: every conclusion points at the
evidence it came from.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from yuanwork.orchestrator.agent import AgentLoop


class FakeLLM:
    def __init__(
        self,
        content: str = '{"intent": "general", "topic": "主题"}',
        gate: str = '{"sufficient": true, "reason": "证据覆盖了问题"}',
    ):
        self.content = content
        self.gate = gate
        self.tasks: list[Any] = []

    async def complete(self, task):
        self.tasks.append(task)
        return type("R", (), {"content": self._reply(task)})()

    async def stream(self, task):
        self.tasks.append(task)
        for piece in self._reply(task):
            yield piece

    def _reply(self, task) -> str:
        """Every step asks the model something different."""
        task_type = getattr(task, "task_type", "")
        if task_type == "evidence_gate":
            return self.gate
        if task_type == "answer":
            # The answer cites its evidence - an uncited answer is a
            # refusal (the citation check).
            return "答案 [1]。"
        return self.content


class FakeSQLite:
    def __init__(self, files=()):
        self.files = list(files)

    async def list_files(self, limit=100, offset=0, file_type=None, search=None):
        return self.files[:limit]


class FakeSemantic:
    def __init__(self, hits=()):
        self.hits = list(hits)

    async def search(self, query, top_k=50, filter_expr=None):
        return list(self.hits)

    async def count(self) -> int:
        return 12


class FakeRetriever:
    def __init__(self, evidence=()):
        self.evidence = list(evidence)

    async def search(self, query, top_k=10, doc_ids=None, file_types=None, min_score=0.0):
        return list(self.evidence)


@pytest.fixture(autouse=True)
def _sqlite_store():
    """The localize step resolves the file store from the registry."""
    from yuanwork.api import deps

    deps.set_sqlite_store(FakeSQLite())  # type: ignore[arg-type]
    yield
    deps.reset_dependencies()


def make_agent(llm=None, evidence=(), candidates=()):
    agent = AgentLoop(
        llm_gateway=llm or FakeLLM(),
        semantic_memory=FakeSemantic(),  # type: ignore[arg-type]
        episodic_memory=None,  # type: ignore[arg-type]
        consolidation_engine=None,
        wiki_compile_trigger=None,
    )
    agent.retriever = FakeRetriever(evidence)  # type: ignore[assignment]
    return agent


#: A model that says the evidence does not answer the question.
def refusing_model() -> FakeLLM:
    return FakeLLM(gate='{"sufficient": false, "reason": "证据只讲别的事"}')


def evidence(count: int = 3) -> list[dict]:
    return [
        {
            "id": f"c{i}",
            "doc_id": "d1",
            "text": f"原文 {i}",
            "file_name": "a.pdf",
            "file_path": "/kb/a.pdf",
            "page": 12 + i,
            "_score": 0.9 - i * 0.05,
        }
        for i in range(count)
    ]


def run(agent, query: str = "问题") -> dict:
    return asyncio.run(agent.run(query=query, session_id="s1"))


# --------------------------------------------------------------------------
# the refusal
# --------------------------------------------------------------------------


def test_too_little_evidence_refuses():
    agent = make_agent(evidence=evidence(1))
    result = run(agent)
    assert result["refused"] is True
    assert result["refusal_reason"] == "insufficient_evidence"


def test_a_refusal_clears_the_citations():
    agent = make_agent(evidence=evidence(1))
    result = run(agent)
    assert result["citations"] == []


def test_the_refusal_text_comes_from_the_template():
    agent = make_agent(evidence=evidence(1))
    result = run(agent)
    assert "没有找到足够明确的证据" in result["answer"]
    assert "最可能相关的位置" in result["answer"]


def test_a_refusal_does_not_call_the_model_for_the_answer():
    """The model is only called for the understanding step - and for the
    gate, but only once the rules have passed."""
    llm = FakeLLM()
    agent = make_agent(llm=llm, evidence=evidence(1))
    run(agent)
    assert [t.task_type for t in llm.tasks] == ["understand"]


# --------------------------------------------------------------------------
# the positions
# --------------------------------------------------------------------------


def test_the_refusal_points_at_the_page_the_evidence_came_from():
    """A file name alone is not enough - the user has to be able to go
    there."""
    agent = make_agent(llm=refusing_model(), evidence=evidence(2))
    result = run(agent)
    assert result["refused"] is True
    assert "《a.pdf》" in result["answer"]
    assert "第 12 页" in result["answer"]


def test_the_refusal_uses_the_section_when_there_is_no_page():
    rows = [
        {"id": "c1", "doc_id": "d1", "text": "原文",
         "file_name": "a.docx", "file_path": "/kb/a.docx",
         "section": "第一章", "_score": 0.9},
        {"id": "c2", "doc_id": "d1", "text": "原文2",
         "file_name": "a.docx", "file_path": "/kb/a.docx",
         "section": "第二章", "_score": 0.8},
    ]
    agent = make_agent(llm=refusing_model(), evidence=rows)
    result = run(agent)
    assert result["refused"] is True
    assert "第一章" in result["answer"]


def test_the_refusal_names_no_internal_identifiers():
    """Chunk ids, doc ids and paths are not something the user can act
    on."""
    agent = make_agent(evidence=evidence(2))
    result = run(agent)
    for internal in ("c0", "d1", "/kb/a.pdf"):
        assert internal not in result["answer"]


def test_the_refusal_falls_back_to_the_candidate_files():
    """Nothing was retrieved, but the files that might hold the answer
    are still worth naming."""
    agent = make_agent(evidence=[])
    result = run(agent)
    assert result["refused"] is True
    assert "最可能相关的位置" in result["answer"]


def test_the_refusal_says_when_there_is_nowhere_to_look():
    agent = make_agent(evidence=[])
    result = run(agent)
    assert "暂时没有可定位的位置" in result["answer"]


# --------------------------------------------------------------------------
# the suggestions
# --------------------------------------------------------------------------


def test_the_refusal_says_how_to_rephrase():
    agent = make_agent(evidence=evidence(1))
    result = run(agent)
    assert "换用更具体的关键词" in result["answer"]


def test_the_refusal_says_how_to_add_documents():
    agent = make_agent(evidence=evidence(1))
    result = run(agent)
    assert "把相关文档加入知识库" in result["answer"]


def test_the_refusal_offers_four_ways_forward():
    agent = make_agent(evidence=evidence(1))
    result = run(agent)
    suggestions = [
        line for line in result["answer"].splitlines()
        if line.strip()[:2] in ("1.", "2.", "3.", "4.")
    ]
    assert len(suggestions) == 4


# --------------------------------------------------------------------------
# the wording follows the reason
# --------------------------------------------------------------------------


def test_a_different_reason_gets_a_different_opening():
    """Three problems, three fixes - the wording has to say which one
    it is."""
    agent = make_agent(evidence=evidence(1))
    thin = run(agent)["answer"]

    rows = [{"id": "c1", "doc_id": "d1", "text": "原文",
             "file_name": "a.pdf", "file_path": "/kb/a.pdf", "_score": 0.9}]
    rows.append({"id": "c2", "doc_id": "d1", "text": "原文",
                 "file_name": "a.pdf", "file_path": "/kb/a.pdf", "_score": 0.8})
    unlocatable = run(make_agent(evidence=rows))["answer"]

    assert thin != unlocatable
    assert "没有找到足够明确的证据" in thin
    assert "没有可定位的位置" in unlocatable
    assert "重建一次索引" in unlocatable


# --------------------------------------------------------------------------
# the refusal says nothing of its own
# --------------------------------------------------------------------------


def test_a_refusal_makes_no_claim_of_its_own():
    """The refusal is rendered, not written: it cannot assert anything
    the documents do not say."""
    llm = FakeLLM(content="合同违约金是每日千分之三")
    agent = make_agent(llm=llm, evidence=evidence(1))
    result = run(agent)
    assert "千分之三" not in result["answer"]


# --------------------------------------------------------------------------
# the citations
# --------------------------------------------------------------------------


def test_a_good_evidence_set_answers():
    agent = make_agent(evidence=evidence(3))
    result = run(agent)
    assert result["refused"] is False
    assert result["answer"]


def test_the_confidence_is_part_of_the_result():
    agent = make_agent(evidence=evidence(3))
    result = run(agent)
    assert 0.0 <= result["confidence"] <= 1.0


def test_only_the_cited_evidence_becomes_a_citation():
    """The evidence the answer did not use is not a citation - the user
    would be checking a claim against the wrong source."""
    agent = make_agent(evidence=evidence(3))
    result = run(agent)
    assert len(result["citations"]) == 1
    assert result["citations"][0]["index"] == 1


def test_a_citation_carries_the_location():
    agent = make_agent(evidence=evidence(2))
    result = run(agent)
    citation = result["citations"][0]
    assert citation["file_name"] == "a.pdf"
    assert citation["file_path"] == "/kb/a.pdf"
    assert citation["page"] == 12
    assert citation["chunk_id"] == "c0"


def test_a_citation_keeps_the_section():
    """Two chunks, so the gate does not refuse and clear the list."""
    rows = [
        {"id": "c1", "doc_id": "d1", "text": "原文",
         "file_name": "a.docx", "file_path": "/kb/a.docx",
         "section": "第一章", "_score": 0.9},
        {"id": "c2", "doc_id": "d1", "text": "原文2",
         "file_name": "a.docx", "file_path": "/kb/a.docx",
         "section": "第二章", "_score": 0.8},
    ]
    agent = make_agent(llm=FakeLLM(), evidence=rows)
    result = run(agent)
    assert result["citations"][0]["section"] == "第一章"


def test_the_citation_text_is_truncated_to_500_characters():
    agent = make_agent(evidence=[
        {"id": "c1", "doc_id": "d1", "text": "x" * 900,
         "file_name": "a.md", "file_path": "/a.md", "page": 1, "_score": 0.9},
        {"id": "c2", "doc_id": "d1", "text": "y" * 900,
         "file_name": "a.md", "file_path": "/a.md", "page": 2, "_score": 0.8},
    ])
    result = run(agent)
    assert len(result["citations"][0]["text"]) == 500


def test_the_citations_are_what_the_model_cited():
    """The citations and the answer line up: the numbers in the answer
    are the numbers of the citations."""
    llm = FakeLLM()
    agent = make_agent(llm=llm, evidence=evidence(3))
    result = run(agent)
    assert [c["index"] for c in result["citations"]] == [1]
    assert "[1]" in result["answer"]


def test_the_slide_number_is_carried():
    agent = make_agent(evidence=[
        {"id": "c1", "doc_id": "d1", "text": "原文",
         "file_name": "a.pptx", "file_path": "/a.pptx",
         "slide_number": 3, "_score": 0.9},
        {"id": "c2", "doc_id": "d1", "text": "原文2",
         "file_name": "a.pptx", "file_path": "/a.pptx",
         "slide_number": 4, "_score": 0.8},
    ])
    result = run(agent)
    assert result["citations"][0]["slide_number"] == 3
