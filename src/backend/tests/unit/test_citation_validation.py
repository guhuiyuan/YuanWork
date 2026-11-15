"""The citation check.

The model is asked to mark every fact with the number of the piece of
evidence it came from. That marking is the only thing that makes an
answer checkable - so it has to be right:

* a number that points at nothing is a broken citation
* an answer that cites nothing is an answer the user cannot check
* the citations the user gets are the ones the answer actually used

* AC-F-005 - every conclusion carries a citation with a file name and
  a position
* AC-F-020 - the model does not get to add claims the knowledge base
  does not support
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from yuanwork.orchestrator.steps.answer import validate_citations


# --------------------------------------------------------------------------
# the check itself
# --------------------------------------------------------------------------


def test_a_citation_that_points_at_evidence_is_fine():
    result = validate_citations("违约金为每日万分之三 [1]。", 3)

    assert result.ok is True
    assert result.cited == [1]


def test_several_citations_are_read():
    result = validate_citations("第一 [1]，第二 [2]，第三 [1]。", 3)

    assert result.ok is True
    assert result.cited == [1, 2]


def test_a_number_that_points_at_nothing_is_broken():
    result = validate_citations("这是结论 [5]。", 3)

    assert result.ok is False
    assert result.reason == "citation_out_of_range"


def test_zero_is_not_a_citation():
    result = validate_citations("这是结论 [0]。", 3)

    assert result.ok is False
    assert result.reason == "citation_out_of_range"


def test_an_answer_without_citations_is_broken():
    """Nothing to check - and nothing that may stand as a conclusion."""
    result = validate_citations("这是结论，但没有引用。", 3)

    assert result.ok is False
    assert result.reason == "no_citations"


def test_an_empty_answer_has_no_citations():
    result = validate_citations("", 3)

    assert result.ok is False
    assert result.reason == "no_citations"


def test_a_number_zero_is_not_a_citation():
    result = validate_citations("这是结论 [0]。", 3)

    assert result.ok is False


def test_citations_written_side_by_side_are_read():
    result = validate_citations("第一 [1][2]，第二 [3]。", 3)

    assert result.ok is True
    assert result.cited == [1, 2, 3]


def test_citations_written_with_commas_are_read():
    result = validate_citations("第一 [1,2]。", 3)

    assert result.ok is True
    assert result.cited == [1, 2]


def test_brackets_that_are_not_citations_are_ignored():
    """A markdown link is not a citation."""
    result = validate_citations("见 [说明](http://example.com) 与 [1]。", 3)

    assert result.ok is True
    assert result.cited == [1]


def test_the_same_citation_twice_is_counted_once():
    result = validate_citations("第一 [1]，还是第一 [1]。", 3)

    assert result.cited == [1]


# --------------------------------------------------------------------------
# the agent
# --------------------------------------------------------------------------


class FakeLLM:
    def __init__(self, answer: str):
        self.answer = answer
        self.tasks: list[Any] = []

    async def complete(self, task):
        self.tasks.append(task)
        content = self._reply(task)
        return type("R", (), {"content": content})()

    async def stream(self, task):
        self.tasks.append(task)
        for piece in self._reply(task):
            yield piece

    def _reply(self, task) -> str:
        if getattr(task, "task_type", "") == "evidence_gate":
            return '{"sufficient": true, "reason": "证据覆盖了问题"}'
        return self.answer


class FakeSQLite:
    async def list_files(self, limit=100, offset=0, file_type=None, search=None):
        return []


class FakeSemantic:
    async def count(self) -> int:
        return 12

    async def search(self, query, top_k=50, filter_expr=None):
        return []


class FakeRetriever:
    def __init__(self, evidence):
        self.evidence = list(evidence)

    async def search(self, query, top_k=10, doc_ids=None, file_types=None, min_score=0.0):
        return list(self.evidence)


@pytest.fixture(autouse=True)
def _sqlite_store():
    from yuanwork.api import deps

    deps.set_sqlite_store(FakeSQLite())  # type: ignore[arg-type]
    yield
    deps.reset_dependencies()


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


def make_agent(answer: str, rows=None):
    from yuanwork.orchestrator.agent import AgentLoop

    agent = AgentLoop(
        llm_gateway=FakeLLM(answer),  # type: ignore[arg-type]
        semantic_memory=FakeSemantic(),  # type: ignore[arg-type]
        episodic_memory=None,  # type: ignore[arg-type]
    )
    agent.retriever = FakeRetriever(rows if rows is not None else evidence())  # type: ignore[assignment]
    return agent


def run(agent) -> dict:
    return asyncio.run(agent.run(query="违约金是多少？", session_id="s1"))


# --------------------------------------------------------------------------
# the citations the user gets
# --------------------------------------------------------------------------


def test_only_the_cited_evidence_becomes_a_citation():
    """The evidence the answer did not use is not a citation - the user
    would be checking a claim against the wrong source."""
    agent = make_agent("违约金为每日万分之三 [2]。")

    result = run(agent)

    assert result["refused"] is False
    assert len(result["citations"]) == 1
    assert result["citations"][0]["index"] == 2


def test_a_citation_keeps_the_number_the_answer_used():
    """[2] in the answer has to be [2] in the citation list."""
    agent = make_agent("第一 [1]，第二 [2]。")

    result = run(agent)

    assert [c["index"] for c in result["citations"]] == [1, 2]


def test_a_citation_carries_the_file_and_the_position():
    agent = make_agent("违约金为每日万分之三 [1]。")

    result = run(agent)

    citation = result["citations"][0]
    assert citation["file_name"] == "a.pdf"
    assert citation["page"] == 12
    assert citation["location"] == "第 12 页"


def test_every_citation_is_a_real_piece_of_evidence():
    agent = make_agent("第一 [1]，第二 [3]。")

    result = run(agent)

    assert [c["chunk_id"] for c in result["citations"]] == ["c0", "c2"]


# --------------------------------------------------------------------------
# the failures
# --------------------------------------------------------------------------


def test_an_out_of_range_citation_turns_into_a_refusal():
    agent = make_agent("这是结论 [5]。")

    result = run(agent)

    assert result["refused"] is True
    assert result["refusal_reason"] == "citation_invalid"
    assert "没有找到足够明确的证据" not in result["answer"]
    assert "对不上" in result["answer"]


def test_an_answer_without_citations_turns_into_a_refusal():
    agent = make_agent("这是结论，没有任何引用。")

    result = run(agent)

    assert result["refused"] is True
    assert result["refusal_reason"] == "citation_invalid"


def test_a_refused_answer_still_shows_the_evidence():
    """The evidence is real - the user can go and look at it."""
    agent = make_agent("这是结论 [5]。")

    result = run(agent)

    assert len(result["citations"]) == 3


def test_the_refusal_says_where_to_look():
    agent = make_agent("这是结论 [5]。")

    result = run(agent)

    assert "最可能相关的位置" in result["answer"]
    assert "《a.pdf》" in result["answer"]


# --------------------------------------------------------------------------
# the streaming path
# --------------------------------------------------------------------------


def test_the_streaming_path_checks_the_citations_too():
    agent = make_agent("这是结论 [5]。")

    async def _events():
        return [event async for event in agent.stream(query="问题", session_id="s1")]

    events = asyncio.run(_events())

    refusals = [e for e in events if e["type"] == "refusal"]
    assert len(refusals) == 1
    assert refusals[0]["data"]["reason"] == "citation_invalid"

    done = [e for e in events if e["type"] == "done"][0]
    assert done["data"]["response"]["refused"] is True
