"""The evidence gate - the model self-assessment half of it.

The hard rule catches what is structurally missing. The model catches
what is structurally there but does not answer the question: three
passages about travel expenses are not an answer to "how many days do
I have to submit the claim".

Everything that is not a clear "yes" counts as a "no" - and the two
kinds of "no" have to stay apart, because the user is told different
things: the documents were silent, or the model could not be reached.

* AC-F-008 - the model's verdict can refuse the answer
* AC-E-008 - the more conservative of the two gates decides
* AC-P-004 - the decision is bounded and takes a measurable time
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from yuanwork.retrieval.evidence_gate import (
    REASON_MODEL_INSUFFICIENT,
    REASON_MODEL_UNAVAILABLE,
    EvidenceGate,
)
from yuanwork.retrieval.model_gate import (
    DEFAULT_GATE_TIMEOUT,
    ModelSelfAssessGate,
)


# --------------------------------------------------------------------------
# the doubles
# --------------------------------------------------------------------------


class FakeLLM:
    """A model that answers whatever the test tells it to."""

    def __init__(self, content: str = '{"sufficient": true, "reason": "够了"}'):
        self.content = content
        self.error: Exception | None = None
        self.tasks: list[Any] = []

    async def complete(self, task):
        self.tasks.append(task)
        if self.error is not None:
            raise self.error
        return type("R", (), {"content": self.content})()


def located(count: int = 3) -> list[dict[str, Any]]:
    return [
        {
            "id": f"c{i}",
            "doc_id": "d1",
            "text": f"原文 {i}",
            "file_name": "a.pdf",
            "meta": {"anchor": {"kind": "page", "page": 10 + i}},
        }
        for i in range(count)
    ]


# --------------------------------------------------------------------------
# the verdict
# --------------------------------------------------------------------------


def test_the_model_says_the_evidence_is_enough():
    gate = ModelSelfAssessGate(FakeLLM('{"sufficient": true, "reason": "够了"}'))

    assessment = asyncio.run(gate.assess(located(3), "问题"))

    assert assessment.sufficient is True
    assert assessment.failed is False
    assert assessment.reason == "够了"


def test_the_model_says_the_evidence_is_not_enough():
    gate = ModelSelfAssessGate(
        FakeLLM('{"sufficient": false, "reason": "证据只讲差旅，没讲时限"}')
    )

    assessment = asyncio.run(gate.assess(located(3), "问题"))

    assert assessment.sufficient is False
    assert assessment.failed is False
    assert "时限" in assessment.reason


def test_a_reply_in_a_code_fence_is_read():
    """Models wrap JSON in fences even when told not to."""
    gate = ModelSelfAssessGate(
        FakeLLM('```json\n{"sufficient": false, "reason": "不相关"}\n```')
    )

    assessment = asyncio.run(gate.assess(located(3), "问题"))

    assert assessment.sufficient is False
    assert assessment.failed is False


def test_a_reply_that_is_not_json_is_a_failure():
    """Not a refusal of the evidence - a failure to judge it."""
    gate = ModelSelfAssessGate(FakeLLM("我觉得证据挺充分的"))

    assessment = asyncio.run(gate.assess(located(3), "问题"))

    assert assessment.sufficient is False
    assert assessment.failed is True


def test_a_reply_without_a_verdict_is_a_failure():
    gate = ModelSelfAssessGate(FakeLLM('{"reason": "还行"}'))

    assessment = asyncio.run(gate.assess(located(3), "问题"))

    assert assessment.sufficient is False
    assert assessment.failed is True


def test_a_call_that_fails_is_a_failure():
    llm = FakeLLM()
    llm.error = RuntimeError("provider 挂了")
    gate = ModelSelfAssessGate(llm)

    assessment = asyncio.run(gate.assess(located(3), "问题"))

    assert assessment.sufficient is False
    assert assessment.failed is True


def test_nothing_to_judge_is_not_a_failure():
    """No evidence is a plain "no", not a broken model."""
    gate = ModelSelfAssessGate(FakeLLM())

    assessment = asyncio.run(gate.assess([], "问题"))

    assert assessment.sufficient is False
    assert assessment.failed is False


# --------------------------------------------------------------------------
# the prompt
# --------------------------------------------------------------------------


def test_the_model_is_shown_the_question_and_the_evidence():
    llm = FakeLLM()
    gate = ModelSelfAssessGate(llm)

    asyncio.run(gate.assess(located(2), "报销期限是几天？"))

    task = llm.tasks[0]
    prompt = task.messages[0].content
    assert "报销期限是几天？" in prompt
    assert "原文 0" in prompt
    assert "a.pdf" in prompt


def test_the_prompt_tells_the_model_to_judge_the_evidence():
    llm = FakeLLM()
    gate = ModelSelfAssessGate(llm)

    asyncio.run(gate.assess(located(2), "问题"))

    prompt = llm.tasks[0].messages[0].content
    assert "sufficient" in prompt
    # The judge must not answer from its own knowledge.
    assert "不要用你自己的知识去补足" in prompt


def test_the_evidence_is_redacted_on_the_way_to_the_model():
    class FakeRedactor:
        def redact(self, text: str) -> str:
            return text.replace("13800138000", "***")

    llm = FakeLLM()
    gate = ModelSelfAssessGate(llm, redactor=FakeRedactor())
    evidence = located(1)
    evidence[0]["text"] = "电话 13800138000"

    asyncio.run(gate.assess(evidence, "问题"))

    prompt = llm.tasks[0].messages[0].content
    assert "13800138000" not in prompt
    assert "***" in prompt


def test_only_the_top_of_the_ranking_reaches_the_model():
    llm = FakeLLM()
    gate = ModelSelfAssessGate(llm, max_pieces=2)

    asyncio.run(gate.assess(located(5), "问题"))

    prompt = llm.tasks[0].messages[0].content
    assert "原文 0" in prompt
    assert "原文 1" in prompt
    assert "原文 2" not in prompt


# --------------------------------------------------------------------------
# the call itself
# --------------------------------------------------------------------------


def test_the_call_has_a_timeout():
    """A gate that waits forever is a gate that never answers."""
    llm = FakeLLM()
    gate = ModelSelfAssessGate(llm)

    asyncio.run(gate.assess(located(2), "问题"))

    assert llm.tasks[0].timeout is not None
    assert llm.tasks[0].timeout > 0


def test_the_default_timeout_is_bounded():
    assert 0 < DEFAULT_GATE_TIMEOUT <= 60


def test_the_call_is_a_json_call():
    llm = FakeLLM()
    gate = ModelSelfAssessGate(llm)

    asyncio.run(gate.assess(located(2), "问题"))

    task = llm.tasks[0]
    assert task.json_mode is True
    assert task.task_type == "evidence_gate"
    assert task.temperature == 0.0


# --------------------------------------------------------------------------
# the gate as a whole
# --------------------------------------------------------------------------


def test_the_gate_passes_when_the_model_vouches_for_the_evidence():
    gate = EvidenceGate(model_gate=ModelSelfAssessGate(FakeLLM()))

    result = asyncio.run(gate.evaluate(located(3), "问题"))

    assert result.passed is True
    assert result.model_ok is True
    assert result.model_evaluated is True


def test_the_model_can_refuse_an_answer_the_rules_allowed():
    """AC-F-008: the hard rule passed, the model says the evidence does
    not answer the question."""
    llm = FakeLLM('{"sufficient": false, "reason": "证据只讲差旅"}')
    gate = EvidenceGate(model_gate=ModelSelfAssessGate(llm))

    result = asyncio.run(gate.evaluate(located(3), "报销期限是几天？"))

    assert result.passed is False
    assert result.hard_rule_ok is True
    assert result.model_ok is False
    assert result.reason == REASON_MODEL_INSUFFICIENT
    assert "差旅" in result.model_reason


def test_a_model_that_cannot_be_reached_is_not_the_same_as_thin_evidence():
    """The user has to be told which one it was."""
    llm = FakeLLM()
    llm.error = RuntimeError("provider 挂了")
    gate = EvidenceGate(model_gate=ModelSelfAssessGate(llm))

    result = asyncio.run(gate.evaluate(located(3), "问题"))

    assert result.passed is False
    assert result.model_failed is True
    assert result.reason == REASON_MODEL_UNAVAILABLE
    assert result.reason != REASON_MODEL_INSUFFICIENT


# --------------------------------------------------------------------------
# the agent
# --------------------------------------------------------------------------


class _AgentLLM:
    def __init__(self, gate_reply: str = '{"sufficient": true, "reason": "够了"}'):
        self.gate_reply = gate_reply
        self.error: Exception | None = None

    async def complete(self, task):
        task_type = getattr(task, "task_type", "")
        if task_type == "evidence_gate":
            if self.error is not None:
                raise self.error
            return type("R", (), {"content": self.gate_reply})()
        if task_type == "answer":
            return type("R", (), {"content": "答案 [1]。"})()
        return type("R", (), {"content": '{"intent": "general", "topic": "主题"}'})()

    async def stream(self, task):
        yield "答案 [1]。"


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


def make_agent(llm, evidence=None):
    from yuanwork.orchestrator.agent import AgentLoop

    agent = AgentLoop(
        llm_gateway=llm,  # type: ignore[arg-type]
        semantic_memory=_FakeSemantic(),  # type: ignore[arg-type]
        episodic_memory=None,  # type: ignore[arg-type]
    )
    agent.retriever = _FakeRetriever(evidence or located(3))  # type: ignore[assignment]
    return agent


def run(agent) -> dict:
    return asyncio.run(agent.run(query="报销期限是几天？", session_id="s1"))


def test_the_agent_refuses_when_the_model_says_the_evidence_is_thin():
    llm = _AgentLLM('{"sufficient": false, "reason": "证据只讲差旅"}')

    result = run(make_agent(llm))

    assert result["refused"] is True
    assert result["refusal_reason"] == REASON_MODEL_INSUFFICIENT
    # The wording says what actually went wrong - not that the
    # documents were silent.
    assert "没有回答你问的这件事" in result["answer"]
    assert "补上另一份文档" in result["answer"]


def test_the_agent_answers_when_the_model_vouches_for_the_evidence():
    result = run(make_agent(_AgentLLM()))

    assert result["refused"] is False
    assert result["answer"]


def test_a_model_outage_is_not_reported_as_thin_evidence():
    """The user is told the model is down, not that the documents are
    silent - they would go looking in the wrong place."""
    llm = _AgentLLM()
    llm.error = RuntimeError("provider 挂了")

    result = run(make_agent(llm))

    assert result["refused"] is True
    assert result["refusal_reason"] == REASON_MODEL_UNAVAILABLE
    assert "模型暂时不可用" in result["answer"]
    assert "没有找到足够明确的证据" not in result["answer"]


def test_a_model_outage_still_shows_the_evidence():
    """The evidence is real; the user may want to look at it."""
    llm = _AgentLLM()
    llm.error = RuntimeError("provider 挂了")

    result = run(make_agent(llm))

    assert len(result["citations"]) == 3


def test_the_decision_is_timed():
    """AC-P-004: the refusal decision has to be measurable - a budget
    that is not measured is a budget that is not kept."""
    result = run(make_agent(_AgentLLM()))

    gate_entries = [t for t in result["trace"] if t.get("step") == "gate"]
    assert len(gate_entries) == 1
    assert gate_entries[0]["duration_ms"] >= 0
