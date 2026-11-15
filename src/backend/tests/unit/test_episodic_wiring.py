"""The six recording points.

Every interaction with the knowledge base leaves a trace:

* asking - the agent, when the question comes in
* citing, accepting, rejecting - the client reports them
* adding and removing files - the workspace routes

* AC-F-016 - every interaction is recorded
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from yuanwork.api import deps
from yuanwork.db.sqlite import SQLiteStore
from yuanwork.memory.episodic_recorder import EpisodicRecorder


class RecordingRecorder:
    """A recorder that keeps what it was told."""

    def __init__(self):
        self.events: list[Any] = []

    async def record(self, event) -> int:
        self.events.append(event)
        return len(self.events)

    async def count(self) -> int:
        return len(self.events)


@pytest.fixture
def recorder() -> RecordingRecorder:
    return RecordingRecorder()


class FakeSemanticMemory:
    """The vector store, seen from the workspace routes."""

    async def delete_by_doc_id(self, doc_id: str) -> int:
        return 0


@pytest.fixture(autouse=True)
def _wire(recorder, monkeypatch, tmp_path: Path):
    """The recorder is in place, and the store is a real one."""
    store = SQLiteStore(tmp_path / "store.db")
    asyncio.run(store.initialize())
    deps.set_sqlite_store(store)
    deps.set_semantic_memory(FakeSemanticMemory())  # type: ignore[arg-type]
    monkeypatch.setattr(
        "yuanwork.api.routes.interactions.optional_episodic_recorder", lambda: recorder
    )
    monkeypatch.setattr(
        "yuanwork.api.routes.workspace.optional_episodic_recorder", lambda: recorder
    )
    yield
    deps.reset_dependencies()


# --------------------------------------------------------------------------
# asking
# --------------------------------------------------------------------------


class FakeLLM:
    def __init__(self, content: str = '{"intent": "general", "topic": "报销"}'):
        self.content = content

    async def complete(self, task):
        task_type = getattr(task, "task_type", "")
        if task_type == "evidence_gate":
            return type("R", (), {"content": '{"sufficient": true, "reason": "够了"}'})()
        if task_type == "answer":
            # The answer cites its evidence - an uncited answer is a
            # refusal (the citation check).
            return type("R", (), {"content": "答案 [1]。"})()
        return type("R", (), {"content": self.content})()

    async def stream(self, task):
        yield "答案 [1]。"


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


EVIDENCE = [
    {"id": "c1", "doc_id": "d1", "text": "原文", "file_name": "a.pdf",
     "file_path": "/kb/a.pdf", "page": 12, "_score": 0.9},
    {"id": "c2", "doc_id": "d1", "text": "原文", "file_name": "a.pdf",
     "file_path": "/kb/a.pdf", "page": 13, "_score": 0.8},
]


def make_agent(recorder):
    from yuanwork.orchestrator.agent import AgentLoop

    agent = AgentLoop(
        llm_gateway=FakeLLM(),  # type: ignore[arg-type]
        semantic_memory=FakeSemantic(),  # type: ignore[arg-type]
        episodic_memory=None,  # type: ignore[arg-type]
        episodic_recorder=recorder,  # type: ignore[arg-type]
    )
    agent.retriever = FakeRetriever(EVIDENCE)  # type: ignore[assignment]
    return agent


def test_a_question_is_recorded(recorder):
    agent = make_agent(recorder)

    asyncio.run(agent.run(query="报销标准是多少？", session_id="s1"))

    assert len(recorder.events) == 1
    event = recorder.events[0]
    assert event.kind == "ask"
    assert event.session_id == "s1"
    assert "报销标准" in event.detail


def test_a_question_carries_the_topic_it_was_about(recorder):
    agent = make_agent(recorder)

    asyncio.run(agent.run(query="报销标准是多少？", session_id="s1"))

    assert recorder.events[0].topic == "报销"


def test_a_question_is_recorded_even_when_the_answer_is_refused(recorder):
    """A question the system could not answer is still a question."""
    agent = make_agent(recorder)
    agent.retriever = FakeRetriever(EVIDENCE[:1])  # type: ignore[assignment]

    asyncio.run(agent.run(query="报销标准是多少？", session_id="s1"))

    assert len(recorder.events) == 1
    assert recorder.events[0].kind == "ask"


def test_a_chat_without_a_recorder_still_works():
    """The memory engineering layer can be switched off."""
    agent = make_agent(None)

    result = asyncio.run(agent.run(query="报销标准是多少？", session_id="s1"))

    assert result["refused"] is False


# --------------------------------------------------------------------------
# citing, accepting, rejecting
# --------------------------------------------------------------------------


def client() -> TestClient:
    from yuanwork.api.routes import interactions

    app = FastAPI()
    app.include_router(interactions.router, prefix="/api/v1")
    return TestClient(app)


@pytest.mark.parametrize("kind", ["cite", "accept", "reject"])
def test_the_client_reported_interactions_are_recorded(recorder, kind):
    response = client().post(
        f"/api/v1/interactions/{kind}",
        json={
            "session_id": "s1",
            "subject": "合同.pdf",
            "detail": "第 12 页",
            "topic": "违约金",
        },
    )

    assert response.status_code == 200
    assert response.json()["recorded"] is True
    assert len(recorder.events) == 1
    event = recorder.events[0]
    assert event.kind == kind
    assert event.subject == "合同.pdf"
    assert event.session_id == "s1"


def test_an_interaction_the_user_never_had_is_rejected(recorder):
    """Only the three client-side interactions are reported by the
    client."""
    response = client().post("/api/v1/interactions/gossip", json={})

    assert response.status_code == 422
    assert recorder.events == []


def test_an_interaction_without_a_recorder_is_not_an_error(recorder, monkeypatch):
    """The memory engineering layer can be switched off - the client
    does not have to care."""
    monkeypatch.setattr(
        "yuanwork.api.routes.interactions.optional_episodic_recorder", lambda: None
    )

    response = client().post("/api/v1/interactions/cite", json={})

    assert response.status_code == 200
    assert response.json()["recorded"] is False


# --------------------------------------------------------------------------
# adding and removing files
# --------------------------------------------------------------------------


def workspace_client() -> TestClient:
    from yuanwork.api.routes import workspace

    app = FastAPI()
    app.include_router(workspace.router, prefix="/api/v1")
    return TestClient(app)


def test_adding_a_file_is_recorded(recorder, tmp_path: Path):
    a_file = tmp_path / "a.md"
    a_file.write_text("内容", encoding="utf-8")

    response = workspace_client().post(
        "/api/v1/workspace/files", json={"path": str(a_file)}
    )

    assert response.status_code == 200
    assert len(recorder.events) == 1
    event = recorder.events[0]
    assert event.kind == "add_file"
    assert event.subject == str(a_file)


def test_adding_a_folder_is_recorded(recorder, tmp_path: Path):
    folder = tmp_path / "docs"
    folder.mkdir()

    response = workspace_client().post(
        "/api/v1/workspace/folders", json={"path": str(folder)}
    )

    assert response.status_code == 200
    assert len(recorder.events) == 1
    assert recorder.events[0].kind == "add_file"


def test_removing_a_file_is_recorded(recorder, tmp_path: Path):
    a_file = tmp_path / "a.md"
    a_file.write_text("内容", encoding="utf-8")

    added = workspace_client().post(
        "/api/v1/workspace/files", json={"path": str(a_file)}
    )
    entry_id = added.json()["id"]

    response = workspace_client().delete(f"/api/v1/workspace/entries/{entry_id}")

    assert response.status_code == 200
    kinds = [e.kind for e in recorder.events]
    assert kinds == ["add_file", "remove_file"]
    assert recorder.events[1].subject == str(a_file)


def test_a_removed_file_that_was_never_there_records_nothing(recorder):
    response = workspace_client().delete("/api/v1/workspace/entries/nope")

    assert response.status_code == 404
    assert recorder.events == []
