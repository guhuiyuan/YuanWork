"""Conflict detection and the strength of the knowledge-base-first promise.

* the detector's three verdicts
* the evidence digest that is fed to it
* the heuristic that scans the *answer* for conflict markers
* the field-name mismatch that empties the two sides of a conflict
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from yuanwork.consolidation.conflict import detect_conflict, format_conflict_message
from yuanwork.orchestrator.agent import AgentLoop


class FakeLLM:
    """Answers with whatever the caller queued up."""

    def __init__(self, answers: list[str] | None = None):
        self.answers = list(answers or [])
        self.tasks: list[Any] = []

    async def complete(self, task):
        self.tasks.append(task)
        return type("R", (), {"content": self._reply(task)})()

    async def stream(self, task):
        self.tasks.append(task)
        for piece in self._reply(task):
            yield piece

    def _reply(self, task) -> str:
        """The evidence gate asks its own question - it gets its own
        answer, so the queued answers stay in step."""
        if getattr(task, "task_type", "") == "evidence_gate":
            return '{"sufficient": true, "reason": "证据覆盖了问题"}'
        return self.answers.pop(0) if self.answers else "{}"


class FakeSQLite:
    async def list_files(self, limit=100, offset=0, file_type=None, search=None):
        return []


class FakeSemantic:
    async def search(self, query, top_k=50, filter_expr=None):
        return []


class FakeRetriever:
    def __init__(self, evidence=()):
        self.evidence = list(evidence)

    async def search(self, query, top_k=10, doc_ids=None, file_types=None, min_score=0.0):
        return list(self.evidence)


@pytest.fixture(autouse=True)
def _sqlite_store():
    from yuanwork.api import deps

    deps.set_sqlite_store(FakeSQLite())  # type: ignore[arg-type]
    yield
    deps.reset_dependencies()


def make_agent(llm, evidence) -> AgentLoop:
    from yuanwork.retrieval.confidence import ConfidenceScorer

    agent = AgentLoop(
        llm_gateway=llm,
        semantic_memory=FakeSemantic(),  # type: ignore[arg-type]
        episodic_memory=None,  # type: ignore[arg-type]
        consolidation_engine=None,
        wiki_compile_trigger=None,
    )
    agent.retriever = FakeRetriever(evidence)  # type: ignore[assignment]
    agent.scorer = ConfidenceScorer()  # type: ignore[assignment]
    return agent


UNDERSTANDING = json.dumps({"intent": "general", "topic": "主题"})


def queued(answer: str, verdict: dict) -> FakeLLM:
    """The three model calls of a turn: understanding, answer, detection.

    The answer cites its evidence - an answer without a citation is a
    refusal (the citation check).
    """
    cited = answer if "[" in answer else f"{answer} [1]"
    return FakeLLM([UNDERSTANDING, cited, json.dumps(verdict)])


def evidence(count: int = 3) -> list[dict]:
    return [
        {
            "id": f"c{i}", "doc_id": "d1", "text": f"证据 {i}",
            "file_name": "a.pdf", "file_path": "/a.pdf", "page": 12,
            "_score": 0.9 - i * 0.05,
        }
        for i in range(count)
    ]


# --------------------------------------------------------------------------
# the detector
# --------------------------------------------------------------------------


def test_the_detector_returns_the_three_verdicts():
    llm = FakeLLM([json.dumps({
        "verdict": "conflict",
        "conflict_claims": [{"topic": "金额", "llm_says": "A", "kb_says": "B"}],
        "explanation": "数字不一致",
    })])
    result = asyncio.run(detect_conflict(
        llm=llm, llm_answer="答案", kb_evidence="证据"
    ))
    assert result["verdict"] == "conflict"
    assert result["conflict_claims"][0]["topic"] == "金额"


def test_the_detector_uses_a_json_task():
    llm = FakeLLM([json.dumps({"verdict": "consistent", "conflict_claims": []})])
    asyncio.run(detect_conflict(llm=llm, llm_answer="答案", kb_evidence="证据"))
    task = llm.tasks[0]
    assert task.task_type == "conflict_check"
    assert task.temperature == 0.0
    assert task.json_mode is True


def test_a_detector_failure_becomes_unrelated():
    """A check that did not run is not "there is no conflict" - the
    caller has to be able to tell the two apart (AC-E-010)."""
    llm = FakeLLM(["not json at all"])
    result = asyncio.run(detect_conflict(
        llm=llm, llm_answer="答案", kb_evidence="证据"
    ))
    assert result["verdict"] == "unrelated"
    assert result["conflict_claims"] == []
    assert result["failed"] is True
    assert result["explanation"]


def test_the_answer_is_sent_twice():
    """Once in the system prompt, once in the user message."""
    llm = FakeLLM([json.dumps({"verdict": "consistent", "conflict_claims": []})])
    asyncio.run(detect_conflict(
        llm=llm, llm_answer="独特答案", kb_evidence="独特证据"
    ))
    task = llm.tasks[0]
    assert any("独特答案" in m.content for m in task.messages)
    assert any("独特答案" in m.content and "独特证据" in m.content for m in task.messages)


# --------------------------------------------------------------------------
# the conflict message
# --------------------------------------------------------------------------


def test_the_message_says_the_knowledge_base_wins():
    message = format_conflict_message({
        "verdict": "conflict",
        "conflict_claims": [{"topic": "金额", "llm_says": "A", "kb_says": "B"}],
    })
    assert "已以知识库为准" in message
    assert "⚠️" in message


def test_a_non_conflict_has_no_message():
    assert format_conflict_message({"verdict": "consistent", "conflict_claims": []}) == ""


# --------------------------------------------------------------------------
# the heuristic
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "marker", ["⚠️", "通用知识与知识库存在分歧", "outdated", "过时", "已弃用", "deprecated"]
)
def test_a_marker_in_the_answer_forces_a_conflict(marker: str):
    llm = queued(
        "答案里提到过时条款",
        {"verdict": "consistent", "conflict_claims": [], "explanation": "模型说一致"},
    )
    agent = make_agent(llm, evidence(3))
    result = asyncio.run(agent.run(query="问题", session_id="s"))

    # the detector said "consistent", but the answer carries a marker
    assert result["conflicts"]
    assert result["conflicts"][0]["explanation"]


def test_the_evidence_is_not_scanned_for_markers():
    """Only the *answer* is scanned — a document that merely discusses a
    conflict must not trigger one."""
    llm = queued(
        "一个正常的答案",
        {"verdict": "consistent", "conflict_claims": [], "explanation": "一致"},
    )
    agent = make_agent(llm, evidence(3))
    result = asyncio.run(agent.run(query="问题", session_id="s"))
    assert result["conflicts"] == []


# --------------------------------------------------------------------------
# the field-name mismatch
# --------------------------------------------------------------------------


def test_the_conflict_object_uses_the_response_models_field_names():
    """The two sides of the conflict have to reach the client - the
    response model wants `llm_answer` / `kb_evidence`."""
    llm = queued(
        "金额是 1000",
        {
            "verdict": "conflict",
            "conflict_claims": [{"topic": "金额", "llm_says": "模型说 A", "kb_says": "知识库说 B"}],
            "explanation": "数字不一致",
        },
    )
    agent = make_agent(llm, evidence(3))
    result = asyncio.run(agent.run(query="问题", session_id="s"))

    assert result["conflicts"], "no conflict was produced"
    conflict = result["conflicts"][0]
    assert conflict["llm_answer"] == "模型说 A"
    assert conflict["kb_evidence"] == "知识库说 B"


def test_the_response_model_keeps_the_two_sides():
    from yuanwork.api.schemas import ConflictInfo

    llm = queued(
        "金额是 1000",
        {
            "verdict": "conflict",
            "conflict_claims": [{"topic": "金额", "llm_says": "模型说 A", "kb_says": "知识库说 B"}],
            "explanation": "数字不一致",
        },
    )
    agent = make_agent(llm, evidence(3))
    result = asyncio.run(agent.run(query="问题", session_id="s"))

    conflict = result["conflicts"][0]
    parsed = ConflictInfo(**conflict)
    assert parsed.llm_answer == "模型说 A"
    assert parsed.kb_evidence == "知识库说 B"
    # ... and the text the user reads is still there
    assert "模型说 A" in conflict["message"]


def test_the_conflict_carries_the_first_citation():
    llm = queued(
        "金额是 1000",
        {
            "verdict": "conflict",
            "conflict_claims": [{"topic": "金额", "llm_says": "A", "kb_says": "B"}],
            "explanation": "x",
        },
    )
    agent = make_agent(llm, evidence(3))
    result = asyncio.run(agent.run(query="问题", session_id="s"))
    assert result["conflicts"][0]["citation"]["index"] == 1


# --------------------------------------------------------------------------
# the evidence digest
# --------------------------------------------------------------------------


def test_only_the_first_three_chunks_reach_the_detector():
    llm = queued("答案", {"verdict": "consistent", "conflict_claims": []})
    rows = [
        {"id": f"c{i}", "doc_id": "d1", "text": f"证据{i}",
         "file_name": "a.pdf", "file_path": "/a.pdf", "page": 12,
         "_score": 0.9 - i * 0.05}
        for i in range(6)
    ]
    agent = make_agent(llm, rows)
    asyncio.run(agent.run(query="问题", session_id="s"))

    conflict_task = next(t for t in llm.tasks if t.task_type == "conflict_check")
    user_message = conflict_task.messages[-1].content
    assert "证据0" in user_message
    assert "证据2" in user_message
    assert "证据3" not in user_message


def test_each_chunk_is_truncated_to_600_characters():
    llm = queued("答案", {"verdict": "consistent", "conflict_claims": []})
    rows = [
        {"id": f"c{i}", "doc_id": "d1", "text": "甲" * 900,
         "file_name": "a.pdf", "file_path": "/a.pdf", "page": 12,
         "_score": 0.9 - i * 0.05}
        for i in range(3)
    ]
    agent = make_agent(llm, rows)
    asyncio.run(agent.run(query="问题", session_id="s"))

    conflict_task = next(t for t in llm.tasks if t.task_type == "conflict_check")
    user_message = conflict_task.messages[-1].content
    assert "甲" * 600 in user_message
    assert "甲" * 601 not in user_message
