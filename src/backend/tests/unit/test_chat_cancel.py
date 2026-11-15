"""Stopping a generation that is already running.

A user who changes their mind about a question should not have to wait
for the answer to finish - and the backend should stop asking the model
for the rest of it.

* AC-F-006 - the generation can be stopped, on both chains
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from yuanwork.orchestrator import cancel


@pytest.fixture(autouse=True)
def _clean_registry():
    """The registry is process-wide - every test starts empty."""
    cancel._events.clear()
    yield
    cancel._events.clear()


# --------------------------------------------------------------------------
# the registry
# --------------------------------------------------------------------------


async def test_a_run_registers_itself():
    event = cancel.start("s1")

    assert cancel.running_sessions() == ["s1"]
    assert not event.is_set()


async def test_cancelling_a_run_sets_its_event():
    event = cancel.start("s1")

    assert cancel.cancel("s1") is True
    assert event.is_set()
    assert cancel.is_cancelled("s1")


async def test_cancelling_a_run_that_is_not_there_says_so():
    assert cancel.cancel("nope") is False


async def test_a_finished_run_forgets_its_event():
    cancel.start("s1")
    cancel.finish("s1")

    assert cancel.running_sessions() == []
    assert cancel.cancel("s1") is False


async def test_two_runs_of_two_sessions_are_separate():
    first = cancel.start("s1")
    second = cancel.start("s2")

    cancel.cancel("s1")

    assert first.is_set()
    assert not second.is_set()


# --------------------------------------------------------------------------
# the backend stops pulling from the model
# --------------------------------------------------------------------------


async def test_the_cancelled_call_stops_the_model():
    """The call that cannot be interrupted from the inside is cancelled
    from the outside."""
    event = cancel.start("s1")
    pulled = asyncio.Event()

    async def a_long_call():
        pulled.set()
        await asyncio.sleep(30)
        return "the whole answer"

    task = asyncio.ensure_future(cancel.run_until_cancelled(a_long_call(), event))
    await pulled.wait()

    # The user changes their mind.
    cancel.cancel("s1")

    with pytest.raises(cancel.Cancelled):
        await task


async def test_a_call_that_finishes_first_is_not_cancelled():
    event = cancel.start("s1")

    async def a_short_call():
        return "the answer"

    result = await cancel.run_until_cancelled(a_short_call(), event)

    assert result == "the answer"


# --------------------------------------------------------------------------
# the endpoint
# --------------------------------------------------------------------------


@pytest.fixture
def client(store) -> "TestClient":
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from yuanwork.api import deps
    from yuanwork.api.routes import chat as chat_routes

    deps.set_sqlite_store(store)
    app = FastAPI()
    app.include_router(chat_routes.router, prefix="/api/v1")
    yield TestClient(app)
    deps.reset_dependencies()


@pytest.fixture
async def store(tmp_path: Path):
    from yuanwork.db.sqlite import SQLiteStore

    s = SQLiteStore(tmp_path / "store.db")
    await s.initialize()
    yield s
    await s.close()


def test_cancelling_a_running_session_stops_it(client):
    cancel.start("s1")

    resp = client.post("/api/v1/chat/cancel/s1")

    assert resp.status_code == 200
    body = resp.json()
    assert body["cancelled"] is True
    assert body["session_id"] == "s1"


def test_cancelling_a_session_that_is_not_running_says_so(client):
    resp = client.post("/api/v1/chat/cancel/nope")

    assert resp.status_code == 200
    body = resp.json()
    assert body["cancelled"] is False
    assert "没有正在进行的生成" in body["note"]


# --------------------------------------------------------------------------
# the run stops asking the model
# --------------------------------------------------------------------------


class SlowLLM:
    """A model that has an endless answer to give."""

    def __init__(self):
        self.pieces = 0

    async def complete(self, task):
        if getattr(task, "task_type", "") == "evidence_gate":
            return type("R", (), {"content": '{"sufficient": true}'})()
        return type("R", (), {"content": '{"intent": "general", "topic": "主题"}'})()

    async def stream(self, task):
        for i in range(100):
            self.pieces += 1
            yield f"tok{i} "


class FakeSemantic:
    async def search(self, query, top_k=50, filter_expr=None):
        return []


class FakeRetriever:
    async def search(
        self, query, top_k=10, doc_ids=None, file_types=None, min_score=0.0
    ):
        return [
            {
                "id": f"c{i}",
                "doc_id": "d1",
                "text": f"证据 {i}",
                "file_name": "a.pdf",
                "file_path": "/a.pdf",
                "page": 12,
                "_score": 0.9 - i * 0.05,
            }
            for i in range(3)
        ]


async def test_the_run_stops_asking_the_model(store):
    """The backend stops pulling from the model - the rest of the answer
    is never asked for."""
    from yuanwork.api import deps
    from yuanwork.orchestrator.agent import AgentLoop
    from yuanwork.retrieval.confidence import ConfidenceScorer

    deps.set_sqlite_store(store)
    llm = SlowLLM()
    agent = AgentLoop(
        llm_gateway=llm,  # type: ignore[arg-type]
        semantic_memory=FakeSemantic(),  # type: ignore[arg-type]
        episodic_memory=None,  # type: ignore[arg-type]
        consolidation_engine=None,
        wiki_compile_trigger=None,
    )
    agent.retriever = FakeRetriever()  # type: ignore[assignment]
    agent.scorer = ConfidenceScorer()  # type: ignore[assignment]

    events: list[dict[str, Any]] = []
    async for event in agent.stream(query="问题", session_id="s1"):
        events.append(event)
        if event["type"] == "token":
            # The user changes their mind.
            cancel.cancel("s1")
            break

    assert llm.pieces < 100, "the model was asked for the whole answer"
    assert any(event["type"] == "token" for event in events)
    deps.reset_dependencies()
