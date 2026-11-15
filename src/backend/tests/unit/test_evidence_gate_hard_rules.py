"""The evidence gate - the hard rule half of it.

The gate stands between "we found something" and "we may answer". Two
conditions have to hold before an answer is allowed:

  - there are enough pieces of evidence
  - every piece of evidence can be pinned to a position in the original
    document

The second one is the point of the whole exercise: evidence the user
cannot locate cannot be checked, so it cannot back a conclusion either.

* AC-F-007 - too few pieces, or a piece that cannot be located, refuse
* AC-E-007 - enough pieces but one of them cannot be located, refuses
* AC-E-008 - the more conservative of the two gates decides
* AC-E-011 - an empty knowledge base refuses even when the model is sure
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from yuanwork.retrieval.evidence_gate import (
    DEFAULT_MIN_EVIDENCE,
    REASON_INSUFFICIENT,
    REASON_MODEL_INSUFFICIENT,
    REASON_MODEL_UNAVAILABLE,
    REASON_PASSED,
    REASON_UNLOCATABLE,
    EvidenceGate,
    ModelAssessment,
    check_hard_rule,
)


# --------------------------------------------------------------------------
# the evidence the tests work with
# --------------------------------------------------------------------------


def located(count: int = 3) -> list[dict[str, Any]]:
    """Evidence that can be pinned to a page."""
    return [
        {
            "id": f"c{i}",
            "doc_id": "d1",
            "text": f"原文 {i}",
            "file_name": "a.pdf",
            "meta": {"anchor": {"kind": "page", "page": 10 + i}},
            "_score": 0.9 - i * 0.05,
        }
        for i in range(count)
    ]


def unlocatable(count: int = 3) -> list[dict[str, Any]]:
    """Evidence with no position at all."""
    return [
        {
            "id": f"c{i}",
            "doc_id": "d1",
            "text": f"原文 {i}",
            "file_name": "a.pdf",
            "_score": 0.9 - i * 0.05,
        }
        for i in range(count)
    ]


# --------------------------------------------------------------------------
# the hard rule: enough evidence
# --------------------------------------------------------------------------


def test_no_evidence_at_all_does_not_pass():
    result = check_hard_rule([])

    assert result.ok is False
    assert result.reason == REASON_INSUFFICIENT
    assert result.count == 0


def test_one_piece_of_evidence_is_not_enough():
    result = check_hard_rule(located(1))

    assert result.ok is False
    assert result.reason == REASON_INSUFFICIENT


def test_two_pieces_of_evidence_are_enough():
    result = check_hard_rule(located(2))

    assert result.ok is True
    assert result.reason == REASON_PASSED
    assert result.count == 2


def test_the_threshold_is_two_by_default():
    assert DEFAULT_MIN_EVIDENCE == 2


def test_the_threshold_can_be_raised():
    result = check_hard_rule(located(2), min_evidence=3)

    assert result.ok is False
    assert result.reason == REASON_INSUFFICIENT


# --------------------------------------------------------------------------
# the hard rule: every piece has to be locatable
# --------------------------------------------------------------------------


def test_evidence_without_a_position_does_not_pass():
    """The count is fine - one piece just cannot be located."""
    result = check_hard_rule(unlocatable(3))

    assert result.ok is False
    assert result.reason == REASON_UNLOCATABLE


def test_the_unlocatable_pieces_are_named():
    """A refusal has to say which piece was the problem."""
    evidence = located(2) + unlocatable(1)

    result = check_hard_rule(evidence)

    assert result.reason == REASON_UNLOCATABLE
    assert result.unlocatable == ["c0"]


def test_a_piece_without_an_id_is_still_nameable():
    evidence = located(1) + [{"text": "没有标识的片段"}]

    result = check_hard_rule(evidence)

    assert result.unlocatable == ["#2"]


def test_one_bad_piece_ruins_the_whole_set():
    """Every piece has to be locatable - not just most of them."""
    evidence = located(2)
    evidence.append({"id": "bad", "text": "无处可寻"})

    result = check_hard_rule(evidence)

    assert result.ok is False


# --------------------------------------------------------------------------
# the five kinds of position
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "anchor",
    [
        {"kind": "page", "page": 12},
        {"kind": "slide", "slide": 3},
        {"kind": "section", "section": "第三章 费用"},
        {"kind": "sheet_row", "sheet": "进度表", "row": 7},
        {"kind": "heading_path", "heading_path": ["第一章", "1.1 概述"]},
    ],
)
def test_every_kind_of_anchor_counts_as_located(anchor):
    evidence = [
        {"id": "c1", "text": "原文", "meta": {"anchor": anchor}},
        {"id": "c2", "text": "原文", "meta": {"anchor": anchor}},
    ]

    assert check_hard_rule(evidence).ok is True


def test_an_anchor_without_its_coordinate_is_not_a_position():
    """`{"kind": "page"}` says what kind of position it is, but not
    where - so the piece still cannot be located."""
    evidence = [
        {"id": "c1", "text": "原文", "meta": {"anchor": {"kind": "page"}}},
        {"id": "c2", "text": "原文", "meta": {"anchor": {"kind": "page"}}},
    ]

    assert check_hard_rule(evidence).ok is False


def test_the_legacy_columns_still_locate_a_piece():
    """A row indexed before the anchor existed keeps its position in the
    three old columns - it still counts."""
    evidence = [
        {"id": "c1", "text": "原文", "page": 12},
        {"id": "c2", "text": "原文", "page": 13},
    ]

    assert check_hard_rule(evidence).ok is True


def test_the_anchor_column_of_the_vector_store_counts():
    """The store keeps the anchor as a JSON string."""
    evidence = [
        {"id": "c1", "text": "原文", "anchor": '{"kind": "page", "page": 12}'},
        {"id": "c2", "text": "原文", "anchor": '{"kind": "page", "page": 13}'},
    ]

    assert check_hard_rule(evidence).ok is True


# --------------------------------------------------------------------------
# the gate as a whole
# --------------------------------------------------------------------------


def test_the_gate_passes_when_the_hard_rule_passes():
    gate = EvidenceGate()

    result = asyncio.run(gate.evaluate(located(3), "问题"))

    assert result.passed is True
    assert result.hard_rule_ok is True
    assert result.reason == REASON_PASSED
    assert result.sufficient_count == 3


def test_the_gate_refuses_when_the_hard_rule_refuses():
    gate = EvidenceGate()

    result = asyncio.run(gate.evaluate(unlocatable(3), "问题"))

    assert result.passed is False
    assert result.hard_rule_ok is False
    assert result.reason == REASON_UNLOCATABLE


def test_both_verdicts_are_visible():
    """A refusal has to be explainable: which gate said no."""
    gate = EvidenceGate()

    result = asyncio.run(gate.evaluate(located(1), "问题"))

    assert result.hard_rule_ok is False
    assert result.model_ok is True
    assert result.model_evaluated is False


# --------------------------------------------------------------------------
# AC-E-008 - the more conservative side decides
# --------------------------------------------------------------------------


def test_the_model_gate_can_veto_a_passing_hard_rule():
    async def model_gate(evidence, question):
        return ModelAssessment(sufficient=False, reason="证据与问题无关")

    gate = EvidenceGate(model_gate=model_gate)

    result = asyncio.run(gate.evaluate(located(3), "问题"))

    assert result.passed is False
    assert result.hard_rule_ok is True
    assert result.model_ok is False
    assert result.reason == REASON_MODEL_INSUFFICIENT


def test_the_model_gate_cannot_veto_a_failing_hard_rule():
    """The reverse direction: the model is sure, the rules say no."""
    async def model_gate(evidence, question):
        return ModelAssessment(sufficient=True)

    gate = EvidenceGate(model_gate=model_gate)

    result = asyncio.run(gate.evaluate(unlocatable(3), "问题"))

    assert result.passed is False
    assert result.hard_rule_ok is False
    assert result.reason == REASON_UNLOCATABLE


def test_both_gates_passing_means_the_answer_is_allowed():
    async def model_gate(evidence, question):
        return ModelAssessment(sufficient=True)

    gate = EvidenceGate(model_gate=model_gate)

    result = asyncio.run(gate.evaluate(located(3), "问题"))

    assert result.passed is True
    assert result.hard_rule_ok is True
    assert result.model_ok is True
    assert result.model_evaluated is True


def test_a_model_gate_that_crashes_refuses():
    """A model that cannot be reached cannot vouch for the evidence."""

    async def model_gate(evidence, question):
        raise RuntimeError("boom")

    gate = EvidenceGate(model_gate=model_gate)

    result = asyncio.run(gate.evaluate(located(3), "问题"))

    assert result.passed is False
    assert result.model_ok is False
    assert result.model_failed is True
    assert result.reason == REASON_MODEL_UNAVAILABLE


def test_the_model_gate_is_not_asked_when_the_rules_already_decided():
    """A refusal the rules can settle does not need a model call."""
    asked: list[str] = []

    async def model_gate(evidence, question):
        asked.append(question)
        return ModelAssessment(sufficient=True)

    gate = EvidenceGate(model_gate=model_gate)

    asyncio.run(gate.evaluate(unlocatable(3), "问题"))

    assert asked == []


# --------------------------------------------------------------------------
# AC-E-011 - an empty knowledge base
# --------------------------------------------------------------------------


def test_an_empty_knowledge_base_refuses_even_when_the_model_is_sure():
    """The model may be confident - there is still nothing to answer
    from, and the answer would not be checkable."""
    async def model_gate(evidence, question):
        return ModelAssessment(sufficient=True)

    gate = EvidenceGate(model_gate=model_gate)

    result = asyncio.run(gate.evaluate([], "问题"))

    assert result.passed is False
    assert result.reason == REASON_INSUFFICIENT
    assert result.sufficient_count == 0


# --------------------------------------------------------------------------
# the gate in the agent
# --------------------------------------------------------------------------


class _FakeLLM:
    def __init__(self, content: str = '{"intent": "general", "topic": "主题"}'):
        self.content = content

    async def complete(self, task):
        return type("R", (), {"content": self._reply(task)})()

    async def stream(self, task):
        for piece in self._reply(task):
            yield piece

    def _reply(self, task) -> str:
        """Every step asks the model something different."""
        task_type = getattr(task, "task_type", "")
        if task_type == "evidence_gate":
            return '{"sufficient": true, "reason": "证据覆盖了问题"}'
        if task_type == "answer":
            return "答案 [1]。"
        return self.content


class _FakeSQLite:
    async def list_files(self, limit=100, offset=0, file_type=None, search=None):
        return []


class _FakeSemantic:
    async def count(self) -> int:
        return 12

    async def search(self, query, top_k=50, filter_expr=None):
        return []


class _FakeRetriever:
    def __init__(self, evidence):
        self.evidence = list(evidence)

    async def search(self, query, top_k=10, doc_ids=None, file_types=None, min_score=0.0):
        return list(self.evidence)


@pytest.fixture(autouse=True)
def _sqlite_store():
    from yuanwork.api import deps

    deps.set_sqlite_store(_FakeSQLite())  # type: ignore[arg-type]
    yield
    deps.reset_dependencies()


def make_agent(evidence):
    from yuanwork.orchestrator.agent import AgentLoop

    agent = AgentLoop(
        llm_gateway=_FakeLLM(),  # type: ignore[arg-type]
        semantic_memory=_FakeSemantic(),  # type: ignore[arg-type]
        episodic_memory=None,  # type: ignore[arg-type]
    )
    agent.retriever = _FakeRetriever(evidence)  # type: ignore[assignment]
    return agent


def run(agent, query: str = "问题") -> dict:
    return asyncio.run(agent.run(query=query, session_id="s1"))


def test_the_agent_refuses_when_there_is_too_little_evidence():
    agent = make_agent(located(1))

    result = run(agent)

    assert result["refused"] is True
    assert result["refusal_reason"] == REASON_INSUFFICIENT


def test_the_agent_refuses_when_a_piece_cannot_be_located():
    agent = make_agent(unlocatable(3))

    result = run(agent)

    assert result["refused"] is True
    assert result["refusal_reason"] == REASON_UNLOCATABLE


def test_the_agent_answers_when_the_evidence_is_enough_and_locatable():
    agent = make_agent(located(3))

    result = run(agent)

    assert result["refused"] is False
    assert result["answer"]


def test_the_gate_verdict_is_in_the_trace():
    """The refusal has to be explainable."""
    agent = make_agent(located(1))

    result = run(agent)

    gate_entries = [t for t in result["trace"] if t.get("step") == "gate"]
    assert len(gate_entries) == 1
    assert gate_entries[0]["gate"]["passed"] is False
    assert gate_entries[0]["gate"]["reason"] == REASON_INSUFFICIENT


def test_a_refusal_clears_the_citations():
    agent = make_agent(located(1))

    result = run(agent)

    assert result["citations"] == []
