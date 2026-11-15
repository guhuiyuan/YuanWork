"""The failure paths of the orchestration.

* a failed generation becomes a refusal with a distinguishable reason
* the citations survive the failure
* an empty answer counts as a failure
* the streaming path keeps the text it already produced
* the conflict event arrives after the answer
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from yuanwork.orchestrator.agent import MODEL_UNAVAILABLE_MESSAGE, AgentLoop


class FakeLLM:
    """The four model calls of a turn, with per-call behaviour."""

    def __init__(self, understand=None, answer=None, conflict=None, stream=None,
                 stream_error=None, gate=None):
        self.understand = understand or '{"intent": "general", "topic": "主题"}'
        self.answer = answer if answer is not None else "一个答案 [1]。"
        self.conflict = conflict or json.dumps(
            {"verdict": "consistent", "conflict_claims": []}
        )
        self.gate = gate or '{"sufficient": true, "reason": "证据覆盖了问题"}'
        self.stream_text = stream
        self.stream_error = stream_error
        self.tasks: list[Any] = []

    async def complete(self, task):
        self.tasks.append(task)
        if task.task_type == "understand":
            content = self.understand
        elif task.task_type == "evidence_gate":
            content = self.gate
        elif task.task_type == "answer":
            if isinstance(self.answer, Exception):
                raise self.answer
            content = self.answer
        else:
            content = self.conflict
        return type("R", (), {"content": content})()

    async def stream(self, task):
        self.tasks.append(task)
        if isinstance(self.stream_text, Exception):
            raise self.stream_text
        for piece in self.stream_text or "":
            yield piece
        if self.stream_error is not None:
            raise self.stream_error


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


def make_agent(llm, evidence=()) -> AgentLoop:
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


def evidence(count: int = 3) -> list[dict]:
    return [
        {"id": f"c{i}", "doc_id": "d1", "text": f"证据 {i}",
         "file_name": "a.pdf", "file_path": "/a.pdf", "page": 12,
         "_score": 0.9 - i * 0.05}
        for i in range(count)
    ]


# --------------------------------------------------------------------------
# the blocking path
# --------------------------------------------------------------------------


def test_a_failed_generation_becomes_a_refusal():
    llm = FakeLLM(answer=RuntimeError("provider 挂了"))
    agent = make_agent(llm, evidence(3))
    result = asyncio.run(agent.run(query="问题", session_id="s"))

    assert result["refused"] is True
    assert result["refusal_reason"] == "model_unavailable"
    # the exception text stays in the log, not in the answer
    assert "provider 挂了" not in result["refusal_reason"]


def test_a_failed_generation_keeps_the_citations():
    llm = FakeLLM(answer=RuntimeError("provider 挂了"))
    agent = make_agent(llm, evidence(3))
    result = asyncio.run(agent.run(query="问题", session_id="s"))
    assert len(result["citations"]) == 3


def test_a_failed_generation_shows_a_fixed_message():
    llm = FakeLLM(answer=RuntimeError("provider 挂了"))
    agent = make_agent(llm, evidence(3))
    result = asyncio.run(agent.run(query="问题", session_id="s"))
    assert result["answer"] == MODEL_UNAVAILABLE_MESSAGE


def test_an_empty_answer_counts_as_a_failure():
    llm = FakeLLM(answer="   ")
    agent = make_agent(llm, evidence(3))
    result = asyncio.run(agent.run(query="问题", session_id="s"))
    assert result["refused"] is True
    # an empty answer is a model failure, and it says so the same way
    assert result["refusal_reason"] == "model_unavailable"


def test_a_generation_failure_is_distinguishable_from_an_evidence_refusal():
    """The reason says which one it was."""
    failed = make_agent(FakeLLM(answer=RuntimeError("x")), evidence(3))
    result = asyncio.run(failed.run(query="问题", session_id="s"))
    assert result["refusal_reason"] == "model_unavailable"

    refused = make_agent(FakeLLM(), evidence(1))
    result = asyncio.run(refused.run(query="问题", session_id="s"))
    assert result["refusal_reason"] == "insufficient_evidence"


def test_the_failure_is_recorded_in_the_trace():
    llm = FakeLLM(answer=RuntimeError("provider 挂了"))
    agent = make_agent(llm, evidence(3))
    result = asyncio.run(agent.run(query="问题", session_id="s"))
    answer_step = [t for t in result["trace"] if t["step"] == "answer"][0]
    assert "error" in answer_step


# --------------------------------------------------------------------------
# the streaming path
# --------------------------------------------------------------------------


def collect_events(agent, query: str = "问题") -> list[dict]:
    async def run():
        return [event async for event in agent.stream(query=query, session_id="s")]

    return asyncio.run(run())


def test_a_streaming_failure_keeps_the_text_already_produced():
    llm = FakeLLM(stream="半句答案", stream_error=RuntimeError("provider 断了"))
    agent = make_agent(llm, evidence(3))

    async def run():
        out = []
        async for event in agent.stream(query="问题", session_id="s"):
            out.append(event)
        return out

    events = asyncio.run(run())
    tokens = [e for e in events if e["type"] == "token"]
    assert tokens, "the delta was not emitted"
    assert tokens[0]["data"]["text"] == "半"

    refusals = [e for e in events if e["type"] == "refusal"]
    assert refusals
    assert refusals[0]["data"]["reason"] == "model_unavailable"


def test_a_streaming_failure_still_emits_the_citations():
    llm = FakeLLM(stream="半句答案 [1]")
    agent = make_agent(llm, evidence(3))
    events = collect_events(agent)
    citation_events = [e for e in events if e["type"] == "citations"]
    assert citation_events
    # The answer cited one piece of evidence, so that is the citation.
    assert len(citation_events[0]["data"]["citations"]) == 1


def test_a_streaming_refusal_emits_no_tokens():
    llm = FakeLLM()
    agent = make_agent(llm, evidence(1))
    events = collect_events(agent)
    assert [e for e in events if e["type"] == "token"] == []
    assert [e for e in events if e["type"] == "refusal"]


def test_the_stream_ends_with_a_done_event():
    llm = FakeLLM()
    agent = make_agent(llm, evidence(3))
    events = collect_events(agent)
    assert events[-1]["type"] == "done"
    assert events[-1]["data"]["response"] is not None


def test_the_event_order_is_trace_then_answer():
    llm = FakeLLM(stream="逐字答案 [1]")
    agent = make_agent(llm, evidence(3))
    events = collect_events(agent)
    types = [e["type"] for e in events]
    assert types[0] == "trace"
    assert "token" in types
    assert types[-1] == "done"


# --------------------------------------------------------------------------
# the conflict timing
# --------------------------------------------------------------------------


def test_the_conflict_event_comes_after_the_answer():
    """The user reads the whole answer before the conflict notice."""
    llm = FakeLLM(
        stream="金额是 1000 [1]",
        answer="金额是 1000 [1]",
        conflict=json.dumps({
            "verdict": "conflict",
            "conflict_claims": [{"topic": "金额", "llm_says": "A", "kb_says": "B"}],
            "explanation": "数字不一致",
        }),
    )
    agent = make_agent(llm, evidence(3))
    events = collect_events(agent)
    types = [e["type"] for e in events]
    assert "token" in types
    assert "conflict" in types
    assert types.index("token") < types.index("conflict")


def test_a_refusal_produces_no_conflict_event():
    llm = FakeLLM()
    agent = make_agent(llm, evidence(1))
    events = collect_events(agent)
    assert [e for e in events if e["type"] == "conflict"] == []
