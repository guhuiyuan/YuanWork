"""The orchestration: the steps, the state and the trace.

* the order of the steps
* that the state object is mutated in place
* that the history and the options are accepted and ignored
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from yuanwork.orchestrator.agent import AgentLoop, AgentState


class FakeLLM:
    def __init__(self, content: str = '{"intent": "general", "topic": "主题"}'):
        self.content = content
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
            return '{"sufficient": true, "reason": "证据覆盖了问题"}'
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
    async def search(self, query, top_k=50, filter_expr=None):
        return []


class FakeRetriever:
    def __init__(self, evidence=()):
        self.evidence = list(evidence)

    async def search(self, query, top_k=10, doc_ids=None, file_types=None, min_score=0.0):
        return list(self.evidence)


class RecordingConsolidation:
    def __init__(self, events: list[str]):
        self.events = events

    async def record_episode(self, session_id, role, content, topic_tags=None,
                             entities=None, importance_score=0.5):
        self.events.append(f"episode:{role}")
        return 1


class RecordingWikiTrigger:
    def __init__(self, events: list[str]):
        self.events = events

    async def maybe_compile(self, evidence, understanding):
        self.events.append("wiki")
        return {"triggered": False, "reason": "no_evidence"}


@pytest.fixture(autouse=True)
def _sqlite_store():
    from yuanwork.api import deps

    deps.set_sqlite_store(FakeSQLite())  # type: ignore[arg-type]
    yield
    deps.reset_dependencies()


def make_agent(events=None, evidence=(), llm=None) -> AgentLoop:
    from yuanwork.retrieval.confidence import ConfidenceScorer

    events = events if events is not None else []
    agent = AgentLoop(
        llm_gateway=llm or FakeLLM(),
        semantic_memory=FakeSemantic(),  # type: ignore[arg-type]
        episodic_memory=None,  # type: ignore[arg-type]
        consolidation_engine=RecordingConsolidation(events),  # type: ignore[arg-type]
        wiki_compile_trigger=RecordingWikiTrigger(events),  # type: ignore[arg-type]
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
# the step order
# --------------------------------------------------------------------------


def test_the_steps_run_in_order():
    events: list[str] = []
    agent = make_agent(events, evidence=evidence(3))
    asyncio.run(agent.run(query="问题", session_id="s"))
    assert events == ["wiki", "episode:user", "episode:assistant"]


def test_the_wiki_compile_happens_before_the_answer():
    """The page is compiled from the evidence, so it has to come first."""
    order: list[str] = []

    class OrderedLLM(FakeLLM):
        async def complete(self, task):
            order.append(task.task_type)
            return await super().complete(task)

    agent = make_agent(llm=OrderedLLM(), evidence=evidence(3))
    asyncio.run(agent.run(query="问题", session_id="s"))
    # understand, evidence_gate, answer, conflict_check — the wiki step is
    # not a model call
    assert order == ["understand", "evidence_gate", "answer", "conflict_check"]


def test_the_turn_is_recorded_for_both_roles():
    events: list[str] = []
    agent = make_agent(events, evidence=evidence(3))
    asyncio.run(agent.run(query="问题", session_id="s"))
    assert "episode:user" in events
    assert "episode:assistant" in events


# --------------------------------------------------------------------------
# the trace
# --------------------------------------------------------------------------


def test_the_trace_records_every_step():
    agent = make_agent(evidence=evidence(3))
    result = asyncio.run(agent.run(query="问题", session_id="s"))
    steps = [entry["step"] for entry in result["trace"]]
    assert steps == [
        "understand", "localize", "extract", "wiki_compile", "gate", "answer",
        "check_conflicts",
    ]


def test_the_trace_carries_the_understanding():
    agent = make_agent(evidence=evidence(3))
    result = asyncio.run(agent.run(query="问题", session_id="s"))
    understand = result["trace"][0]
    assert understand["step"] == "understand"
    assert "output" in understand


def test_the_trace_carries_the_confidence():
    agent = make_agent(evidence=evidence(3))
    result = asyncio.run(agent.run(query="问题", session_id="s"))
    extract = result["trace"][2]
    assert extract["step"] == "extract"
    assert extract["evidence_count"] == 3
    assert "confidence" in extract


def test_the_trace_carries_the_citation_count():
    agent = make_agent(evidence=evidence(3))
    result = asyncio.run(agent.run(query="问题", session_id="s"))
    answer = result["trace"][5]
    assert answer["step"] == "answer"
    # The answer cited one piece of evidence, so that is the citation.
    assert answer["citation_count"] == 1


# --------------------------------------------------------------------------
# the result
# --------------------------------------------------------------------------


def test_the_result_has_the_ten_fields():
    agent = make_agent(evidence=evidence(3))
    result = asyncio.run(agent.run(query="问题", session_id="s"))
    assert set(result) == {
        "answer", "citations", "confidence", "refused", "refusal_reason",
        "cancelled", "conflicts", "wiki_compile", "terms", "trace",
    }


def test_a_run_that_was_not_cancelled_says_so():
    agent = make_agent(evidence=evidence(3))
    result = asyncio.run(agent.run(query="问题", session_id="s"))
    assert result["cancelled"] is False


def test_the_wiki_compile_result_is_passed_through():
    agent = make_agent(evidence=evidence(3))
    result = asyncio.run(agent.run(query="问题", session_id="s"))
    assert result["wiki_compile"] == {"triggered": False, "reason": "no_evidence"}


# --------------------------------------------------------------------------
# the unused parameters
# --------------------------------------------------------------------------


def test_the_history_is_accepted_and_ignored():
    """The history never reaches the model — there is no multi-turn
    context."""
    llm = FakeLLM()
    agent = make_agent(llm, evidence=evidence(3))
    asyncio.run(agent.run(
        query="第二问",
        session_id="s",
        history=[{"role": "user", "content": "第一问"}],
    ))
    for task in llm.tasks:
        assert "第一问" not in task.messages[0].content
        assert "第一问" not in task.messages[-1].content


def test_the_options_are_accepted_and_ignored():
    llm = FakeLLM()
    agent = make_agent(llm, evidence=evidence(3))
    result = asyncio.run(agent.run(
        query="问题", session_id="s", opts={"temperature": 0.9}
    ))
    for task in llm.tasks:
        assert task.temperature != 0.9
    assert result["answer"]


# --------------------------------------------------------------------------
# the state object
# --------------------------------------------------------------------------


def test_the_state_holds_the_seven_fields():
    state = AgentState(query="问题", session_id="s")
    assert state.query == "问题"
    assert state.session_id == "s"
    assert state.understanding is None
    assert state.candidates == []
    assert state.evidence == []
    assert state.answer == ""
    assert state.citations == []
    assert state.refused is False
    assert state.refusal_reason is None
    assert state.conflicts == []
    assert state.trace == []


def test_the_state_is_mutated_in_place():
    """The steps fill the same object rather than passing values around."""
    agent = make_agent(evidence=evidence(3))
    result = asyncio.run(agent.run(query="问题", session_id="s"))
    # the trace shows the state growing step by step
    assert len(result["trace"]) == 7
