"""Tests for the conflict heuristic and the empty-answer guard.

Two things these guard against, both found by an adversarial audit of the
streaming work:

1. The conflict heuristic used to scan the KB *evidence* as well as the
   answer. The knowledge base is supposed to contain the words "过时" /
   "冲突" - that is what a conflict looks like - so scanning it invented
   a conflict out of every document that merely discusses one, and
   overrode the detector's own "consistent" verdict.
2. An empty model completion was reported as a success: refused=False,
   answer="", plus a full citation list and no explanation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


# ===== Conflict heuristic =====

EVIDENCE_MENTIONING_CONFLICT = [
    {
        "id": "c1",
        "file_name": "合同.md",
        "text": "第十二条 冲突解决机制：发生意见冲突时，以书面补充协议为准。",
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


@pytest.mark.asyncio
async def test_a_kb_document_that_mentions_a_conflict_is_not_a_conflict(
    monkeypatch, temp_dir: Path
):
    """The evidence talking about "冲突" is not a conflict."""
    from yuanwork.orchestrator.agent import AgentState

    agent = _agent()
    state = AgentState(query="q", session_id="s1")
    state.answer = "违约金为每日万分之三。[1]"
    state.evidence = EVIDENCE_MENTIONING_CONFLICT
    state.citations = agent._build_citations(state.evidence)

    async def _consistent(**kwargs):
        return {
            "verdict": "consistent",
            "conflict_claims": [],
            "explanation": "no conflict",
        }

    monkeypatch.setattr(
        "yuanwork.consolidation.conflict.detect_conflict", _consistent
    )

    await agent._step_check_conflicts(state)

    assert state.conflicts == []
    trace = state.trace[-1]
    assert trace["verdict"] == "consistent"
    assert trace["heuristic_hit"] is None


@pytest.mark.asyncio
async def test_a_warning_in_the_answer_is_still_a_conflict(
    monkeypatch, temp_dir: Path
):
    """The heuristic still fires when the ANSWER carries the warning -
    that is the case it was written for."""
    from yuanwork.orchestrator.agent import AgentState

    agent = _agent()
    state = AgentState(query="q", session_id="s1")
    state.answer = (
        "⚠️ 注意：通用知识与知识库存在分歧。\n\n"
        "按知识库，违约金为每日万分之三。[1]"
    )
    state.evidence = EVIDENCE_MENTIONING_CONFLICT
    state.citations = agent._build_citations(state.evidence)

    async def _consistent(**kwargs):
        return {
            "verdict": "consistent",
            "conflict_claims": [],
            "explanation": "no conflict",
        }

    monkeypatch.setattr(
        "yuanwork.consolidation.conflict.detect_conflict", _consistent
    )

    await agent._step_check_conflicts(state)

    assert len(state.conflicts) == 1
    assert state.conflicts[0]["type"] == "llm_vs_kb"


@pytest.mark.asyncio
async def test_an_outdated_marker_in_the_answer_is_a_conflict(
    monkeypatch, temp_dir: Path
):
    from yuanwork.orchestrator.agent import AgentState

    agent = _agent()
    state = AgentState(query="q", session_id="s1")
    state.answer = "该条款 OUTDATED，请以最新版为准。[1]"
    state.evidence = EVIDENCE_MENTIONING_CONFLICT
    state.citations = agent._build_citations(state.evidence)

    async def _consistent(**kwargs):
        return {
            "verdict": "consistent",
            "conflict_claims": [],
            "explanation": "no conflict",
        }

    monkeypatch.setattr(
        "yuanwork.consolidation.conflict.detect_conflict", _consistent
    )

    await agent._step_check_conflicts(state)

    assert len(state.conflicts) == 1


# ===== Empty answer =====


class _EmptyGateway:
    """A gateway whose model returns nothing at all."""

    def __init__(self) -> None:
        self.stream_calls = 0
        self.complete_calls = 0

    async def stream(self, task):
        self.stream_calls += 1
        if False:  # pragma: no cover - makes this an async generator
            yield ""

    async def complete(self, task):
        self.complete_calls += 1
        from yuanwork.llm.gateway import LLMResponse

        return LLMResponse(content="", model="stub", finish_reason="stop")


EVIDENCE = [
    {
        "id": "c1",
        "file_name": "contract.pdf",
        "file_path": "/kb/contract.pdf",
        "text": "违约金为每日万分之三。",
        "meta": {"page": 3},
    }
]
CONFIDENCE = {"confidence": 0.9, "should_refuse": False, "reason": None}


def _patch_steps(agent, monkeypatch, evidence, confidence):
    async def _understand(state):
        state.understanding = {"intent": "general", "topic": "t"}
        state.trace.append({"step": "understand"})

    async def _localize(state):
        state.candidates = [
            {"file_name": "contract.pdf", "file_path": "/kb/contract.pdf", "score": 1.0}
        ]
        state.trace.append({"step": "localize"})

    async def _extract(state):
        state.evidence = evidence
        state.confidence = confidence
        state.trace.append({"step": "extract"})

    async def _conflicts(state):
        return

    monkeypatch.setattr(agent, "_step_understand", _understand)
    monkeypatch.setattr(agent, "_step_localize", _localize)
    monkeypatch.setattr(agent, "_step_extract", _extract)
    monkeypatch.setattr(agent, "_step_check_conflicts", _conflicts)
    monkeypatch.setattr(agent, "_step_maybe_compile_wiki_page", _conflicts)


@pytest.mark.asyncio
async def test_an_empty_answer_is_a_generation_error(monkeypatch, temp_dir: Path):
    """An empty completion is not an answer."""
    gateway = _EmptyGateway()
    agent = _agent()
    _patch_steps(agent, monkeypatch, EVIDENCE, CONFIDENCE)

    events = [e async for e in agent.stream(query="q", session_id="s1")]

    done = [e for e in events if e["type"] == "done"][0]
    response = done["data"]["response"]
    assert response["refused"] is True
    assert str(response["refusal_reason"]).startswith("generation_error")
    assert "生成回答时出现错误" in response["answer"]
    # The citations survive - the user can still see what was searched.
    assert len(response["citations"]) == 1


@pytest.mark.asyncio
async def test_an_empty_answer_is_a_generation_error_in_the_blocking_path(
    monkeypatch, temp_dir: Path
):
    gateway = _EmptyGateway()
    agent = _agent()
    _patch_steps(agent, monkeypatch, EVIDENCE, CONFIDENCE)

    result = await agent.run(query="q", session_id="s1")

    assert result["refused"] is True
    assert str(result["refusal_reason"]).startswith("generation_error")
    assert len(result["citations"]) == 1
