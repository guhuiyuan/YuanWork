"""The conflict check and the rewrite.

"知识库优先" is not a sentence the system says - it is what the system
does. When the model's answer contradicts the documents, the answer is
the documents' content; the model's own wording is kept as a labelled
comparison at the end, and the divergence is announced at the top.

When the check itself could not be run, the model's answer has not been
checked against anything, so it does not get to stand as a conclusion
either - the knowledge base is the answer then, too.

* AC-F-010 - the knowledge base wins, and the divergence is announced
* AC-F-011 - the divergence, its source and the verdict are recorded
* AC-E-009 - a model failure is not shown as "the evidence was thin"
* AC-E-010 - a failed check still answers from the knowledge base
* AC-P-004 - the check is a separate call, on a budget
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from yuanwork.consolidation.conflict import (
    detect_conflict,
    rewrite_with_knowledge_base,
)


# --------------------------------------------------------------------------
# the doubles
# --------------------------------------------------------------------------


class FakeLLM:
    """Answers the conflict check with whatever the test queued up."""

    def __init__(self, verdict: dict | None = None, error: Exception | None = None):
        self.verdict = verdict
        self.error = error
        self.tasks: list[Any] = []

    async def complete(self, task):
        self.tasks.append(task)
        if self.error is not None:
            raise self.error
        if self.verdict is not None:
            return type("R", (), {"content": json.dumps(self.verdict)})()
        return type("R", (), {"content": "{}"})()


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


class RecordingConsolidation:
    """Records the conflict records that were written."""

    def __init__(self):
        self.records: list[dict[str, Any]] = []

    async def log_conflict(self, **kwargs):
        self.records.append(kwargs)
        return len(self.records)


@pytest.fixture(autouse=True)
def _sqlite_store():
    from yuanwork.api import deps

    deps.set_sqlite_store(FakeSQLite())  # type: ignore[arg-type]
    yield
    deps.reset_dependencies()


def evidence(count: int = 2) -> list[dict]:
    return [
        {
            "id": f"c{i}",
            "doc_id": "d1",
            "text": f"知识库原文 {i}：违约金为每日万分之三。",
            "file_name": "合同.pdf",
            "file_path": "/kb/合同.pdf",
            "page": 12 + i,
            "_score": 0.9 - i * 0.05,
        }
        for i in range(count)
    ]


def make_agent(llm, rows=None, consolidation=None):
    from yuanwork.orchestrator.agent import AgentLoop

    agent = AgentLoop(
        llm_gateway=llm,  # type: ignore[arg-type]
        semantic_memory=FakeSemantic(),  # type: ignore[arg-type]
        episodic_memory=None,  # type: ignore[arg-type]
        consolidation_engine=consolidation,  # type: ignore[arg-type]
    )
    agent.retriever = FakeRetriever(rows if rows is not None else evidence())  # type: ignore[assignment]
    return agent


class AnsweringLLM:
    """A model that answers, and answers the conflict check too."""

    def __init__(self, verdict: dict | None = None, error: Exception | None = None):
        self.verdict = verdict
        self.error = error
        self.tasks: list[Any] = []

    async def complete(self, task):
        self.tasks.append(task)
        task_type = getattr(task, "task_type", "")
        if task_type == "evidence_gate":
            return type("R", (), {"content": '{"sufficient": true, "reason": "够了"}'})()
        if task_type == "conflict_check":
            if self.error is not None:
                raise self.error
            return type("R", (), {"content": json.dumps(self.verdict or {})})()
        if task_type == "answer":
            return type("R", (), {"content": "答案是每日千分之五 [1]。"})()
        return type("R", (), {"content": '{"intent": "general", "topic": "主题"}'})()

    async def stream(self, task):
        yield "答案是每日千分之五 [1]。"


def run(agent) -> dict:
    return asyncio.run(agent.run(query="违约金是多少？", session_id="s1"))


CONFLICT = {
    "verdict": "conflict",
    "conflict_claims": [
        {
            "topic": "违约金比例",
            "llm_says": "每日千分之五",
            "kb_says": "每日万分之三",
        }
    ],
    "explanation": "比例不一致",
}


# --------------------------------------------------------------------------
# the rewrite itself
# --------------------------------------------------------------------------


def test_the_rewrite_leads_with_the_divergence():
    citations = [
        {"index": 1, "file_name": "合同.pdf", "location": "第 12 页"},
    ]

    text = rewrite_with_knowledge_base(
        model_answer="每日千分之五",
        evidence=evidence(1),
        citations=citations,
        conflicts=CONFLICT["conflict_claims"],
    )

    assert text.startswith("⚠")
    assert "通用知识与知识库存在分歧" in text


def test_the_rewrite_leads_with_the_knowledge_base():
    citations = [
        {"index": 1, "file_name": "合同.pdf", "location": "第 12 页"},
    ]

    text = rewrite_with_knowledge_base(
        model_answer="每日千分之五",
        evidence=evidence(1),
        citations=citations,
        conflicts=CONFLICT["conflict_claims"],
    )

    assert "以知识库为准" in text
    assert "每日万分之三" in text
    assert "《合同.pdf》（第 12 页）" in text


def test_the_rewrite_keeps_the_model_answer_as_a_comparison():
    citations = [
        {"index": 1, "file_name": "合同.pdf", "location": "第 12 页"},
    ]

    text = rewrite_with_knowledge_base(
        model_answer="每日千分之五",
        evidence=evidence(1),
        citations=citations,
        conflicts=CONFLICT["conflict_claims"],
    )

    assert "每日千分之五" in text
    # ... and it is labelled, so nobody reads it as the answer.
    assert "仅作对照" in text
    assert text.index("以知识库为准") < text.index("仅作对照")


def test_a_failed_check_does_not_show_the_model_answer():
    """Nothing has checked it, so it does not get to stand as a
    conclusion."""
    citations = [
        {"index": 1, "file_name": "合同.pdf", "location": "第 12 页"},
    ]

    text = rewrite_with_knowledge_base(
        model_answer="每日千分之五",
        evidence=evidence(1),
        citations=citations,
        conflicts=[],
        check_failed=True,
    )

    assert "每日千分之五" not in text
    assert "未能核对" in text
    assert "每日万分之三" in text


# --------------------------------------------------------------------------
# the detection
# --------------------------------------------------------------------------


def test_a_failed_detection_is_marked_as_failed():
    """A check that did not run is not "there is no conflict"."""
    llm = FakeLLM(error=RuntimeError("provider 挂了"))

    result = asyncio.run(
        detect_conflict(llm=llm, llm_answer="答案", kb_evidence="证据")
    )

    assert result["failed"] is True
    assert result["conflict_claims"] == []


def test_a_successful_detection_is_not_marked_as_failed():
    llm = FakeLLM(verdict={"verdict": "consistent", "conflict_claims": []})

    result = asyncio.run(
        detect_conflict(llm=llm, llm_answer="答案", kb_evidence="证据")
    )

    assert result["failed"] is False


# --------------------------------------------------------------------------
# the agent
# --------------------------------------------------------------------------


def test_a_conflict_rewrites_the_answer_with_the_knowledge_base():
    agent = make_agent(AnsweringLLM(verdict=CONFLICT))

    result = run(agent)

    assert "通用知识与知识库存在分歧" in result["answer"]
    assert "每日万分之三" in result["answer"]
    assert result["conflicts"]


def test_the_divergence_is_announced_at_the_top():
    agent = make_agent(AnsweringLLM(verdict=CONFLICT))

    result = run(agent)

    assert result["answer"].startswith("⚠")


def test_the_model_answer_is_kept_as_a_comparison():
    agent = make_agent(AnsweringLLM(verdict=CONFLICT))

    result = run(agent)

    assert "每日千分之五" in result["answer"]
    assert "仅作对照" in result["answer"]


def test_a_conflict_is_recorded_with_its_topic_and_source():
    consolidation = RecordingConsolidation()
    agent = make_agent(AnsweringLLM(verdict=CONFLICT), consolidation=consolidation)

    run(agent)

    assert len(consolidation.records) == 1
    record = consolidation.records[0]
    assert record["topic"] == "违约金比例"
    assert record["llm_answer"] == "每日千分之五"
    assert record["kb_evidence"] == "每日万分之三"
    assert record["resolution"]
    assert record["citation"]


def test_a_failed_check_still_answers_from_the_knowledge_base():
    consolidation = RecordingConsolidation()
    agent = make_agent(
        AnsweringLLM(error=RuntimeError("provider 挂了")),
        consolidation=consolidation,
    )

    result = run(agent)

    assert result["refused"] is False
    assert "每日万分之三" in result["answer"]
    assert "每日千分之五" not in result["answer"]


def test_a_failed_check_is_not_reported_as_thin_evidence():
    """AC-E-009: the two are told apart."""
    agent = make_agent(AnsweringLLM(error=RuntimeError("provider 挂了")))

    result = run(agent)

    assert result["refusal_reason"] is None
    assert "没有找到足够明确的证据" not in result["answer"]


def test_no_conflict_means_no_rewrite():
    agent = make_agent(
        AnsweringLLM(verdict={"verdict": "consistent", "conflict_claims": []})
    )

    result = run(agent)

    assert "以知识库为准" not in result["answer"]
    assert "每日千分之五" in result["answer"]
    assert result["conflicts"] == []


def test_the_check_is_a_separate_call():
    """ADR-011: the judgement is its own call, not a by-product of
    the answer."""
    llm = AnsweringLLM(verdict=CONFLICT)
    agent = make_agent(llm)

    run(agent)

    task_types = [t.task_type for t in llm.tasks]
    assert task_types.count("conflict_check") == 1
    assert "answer" in task_types
