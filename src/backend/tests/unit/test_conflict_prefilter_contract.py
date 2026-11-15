"""TASK-048 — not every answer has to be checked against the documents.

Checking an answer costs a model call and a second of waiting. An
answer that says nothing specific - and a question that is not about
rules, contracts or money - cannot contradict the documents in a way
the user would act on. Those are skipped, and the reason is written
down. But when in doubt the check runs.

* AC-F-029 — an answer without a specific claim, asked without
  reference to rules, contracts or money, skips the conflict check;
  everything else is checked
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from yuanwork.consolidation.conflict import ConflictJudge


# --------------------------------------------------------------------------
# the rules
# --------------------------------------------------------------------------


def test_an_answer_with_a_number_is_checked():
    """A number can be contradicted."""
    decision = ConflictJudge().should_judge(
        answer="违约金为每日万分之三。", question="违约金怎么算"
    )

    assert decision["judge"] is True
    assert "answer_has_number" in decision["matched"]


def test_an_answer_with_a_date_is_checked():
    decision = ConflictJudge().should_judge(
        answer="合同自 2026 年 1 月 1 日起生效。", question="合同什么时候生效"
    )

    assert decision["judge"] is True


def test_an_answer_with_an_amount_is_checked():
    decision = ConflictJudge().should_judge(
        answer="住宿费上限是 500 元。", question="住宿费多少"
    )

    assert decision["judge"] is True
    assert "answer_has_number" in decision["matched"]


def test_an_answer_with_normative_wording_is_checked():
    """The words that make an answer a rule."""
    for word in ("规定", "应当", "必须"):
        decision = ConflictJudge().should_judge(
            answer=f"制度{word}员工承担差旅费用。", question="差旅怎么算"
        )
        assert decision["judge"] is True, word
        assert any(m.startswith("answer_normative") for m in decision["matched"])


def test_a_question_about_a_contract_is_checked():
    """Even an answer that says nothing specific."""
    decision = ConflictJudge().should_judge(
        answer="以合同为准。", question="这份合同怎么规定的"
    )

    assert decision["judge"] is True
    assert any(m.startswith("question_subject") for m in decision["matched"])


def test_a_question_about_a_rule_is_checked():
    decision = ConflictJudge().should_judge(
        answer="按公司规定执行。", question="报销制度是怎样的"
    )

    assert decision["judge"] is True


def test_a_question_about_an_amount_is_checked():
    decision = ConflictJudge().should_judge(
        answer="按合同执行。", question="付款金额是多少"
    )

    assert decision["judge"] is True


def test_an_answer_without_a_specific_claim_is_skipped():
    """Nothing to contradict."""
    decision = ConflictJudge().should_judge(
        answer="这个项目的背景我在文档里找了一下。", question="这个项目是怎么开始的"
    )

    assert decision["judge"] is False
    assert decision["matched"] == []


def test_the_reason_for_skipping_is_given():
    """The reason is written down - not just the fact."""
    decision = ConflictJudge().should_judge(
        answer="我在文档里找了一下。", question="这个项目是怎么开始的"
    )

    assert decision["judge"] is False
    assert decision["reason"]
    assert "不含具体断言" in decision["reason"]


def test_a_broken_rule_judges_anyway():
    """A rule that cannot be read must not skip the check."""

    class BrokenJudge(ConflictJudge):
        def _decide(self, answer: str, question: str):
            raise RuntimeError("the rule table is empty")

    decision = BrokenJudge().should_judge(answer="随便说说", question="随便问问")

    assert decision["judge"] is True
    assert decision["reason"]


# --------------------------------------------------------------------------
# in the agent
# --------------------------------------------------------------------------


EVIDENCE = [
    {
        "id": "c1",
        "file_name": "手册.md",
        "text": "差旅住宿费上限是 500 元。",
        "meta": {},
    }
]


def _agent():
    from yuanwork.orchestrator.agent import AgentLoop

    return AgentLoop(
        llm_gateway=None,  # type: ignore[arg-type]
        semantic_memory=None,  # type: ignore[arg-type]
        episodic_memory=None,  # type: ignore[arg-type]
    )


def _state(query: str, answer: str):
    from yuanwork.orchestrator.agent import AgentState

    agent = _agent()
    state = AgentState(query=query, session_id="s1")
    state.answer = answer
    state.evidence = EVIDENCE
    state.citations = agent._build_citations(state.evidence)
    return state


async def test_the_check_does_not_run_for_an_answer_without_claims(
    monkeypatch,
):
    """The model is not asked at all - that is the point."""
    calls: list[str] = []

    async def _never(**kwargs):
        calls.append("called")
        return {"verdict": "consistent", "conflict_claims": []}

    monkeypatch.setattr("yuanwork.consolidation.conflict.detect_conflict", _never)

    state = _state("这个项目是怎么开始的", "我在文档里找了一下这个项目的背景。")
    await _agent()._step_check_conflicts(state)

    assert calls == []
    assert state.conflicts == []


async def test_the_reason_for_skipping_is_recorded(monkeypatch):
    """The skip is written down, so it can be looked at later."""
    monkeypatch.setattr(
        "yuanwork.consolidation.conflict.detect_conflict",
        lambda **kwargs: None,
    )

    state = _state("这个项目是怎么开始的", "我在文档里找了一下这个项目的背景。")
    await _agent()._step_check_conflicts(state)

    entries = [e for e in state.trace if e.get("step") == "check_conflicts"]
    assert entries
    assert entries[-1]["skipped"] is True
    assert entries[-1]["skip_reason"]


async def test_a_skipped_check_leaves_the_answer_alone(monkeypatch):
    """Skipping is not finding a conflict - the answer stands."""
    monkeypatch.setattr(
        "yuanwork.consolidation.conflict.detect_conflict",
        lambda **kwargs: None,
    )

    answer = "我在文档里找了一下这个项目的背景。"
    state = _state("这个项目是怎么开始的", answer)
    await _agent()._step_check_conflicts(state)

    assert state.answer == answer
    assert state.conflicts == []


async def test_the_check_runs_for_an_answer_with_a_number(monkeypatch):
    """A number is a statement - it has to be checked."""
    calls: list[str] = []

    async def _consistent(**kwargs):
        calls.append("called")
        return {
            "verdict": "consistent",
            "conflict_claims": [],
            "explanation": "no conflict",
        }

    monkeypatch.setattr("yuanwork.consolidation.conflict.detect_conflict", _consistent)

    state = _state("住宿费多少", "住宿费上限是 500 元。")
    await _agent()._step_check_conflicts(state)

    assert calls == ["called"]

    entries = [e for e in state.trace if e.get("step") == "check_conflicts"]
    assert entries[-1]["skipped"] is False
    assert entries[-1]["verdict"] == "consistent"


async def test_the_check_runs_for_a_question_about_a_contract(monkeypatch):
    """A high-risk subject is enough on its own."""
    calls: list[str] = []

    async def _consistent(**kwargs):
        calls.append("called")
        return {
            "verdict": "consistent",
            "conflict_claims": [],
            "explanation": "no conflict",
        }

    monkeypatch.setattr("yuanwork.consolidation.conflict.detect_conflict", _consistent)

    state = _state("这份合同怎么规定的", "以合同为准。")
    await _agent()._step_check_conflicts(state)

    assert calls == ["called"]
