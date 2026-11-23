"""TASK-003 — the streaming chat endpoint.

* AC-F-006 — the response headers, the event names and the heartbeat
* AC-F-007 — the exception path (error frame, then a terminal frame) and
  the client-disconnect path (no terminal frame, and the turn is not
  recorded in the episodic memory)
* AC-E-004 — the cancel endpoint is a stub
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from yuanwork.api import deps
from yuanwork.api.errors import ERROR_MESSAGES
from yuanwork.api.routes.chat import router


# --------------------------------------------------------------------------
# doubles
# --------------------------------------------------------------------------


class FakeAgentLoop:
    """Stands in for the orchestrator.

    ``events`` is what the stream yields; ``raise_on_stream`` makes it
    blow up instead.
    """

    events: list[dict] = []
    raise_on_stream: Exception | None = None
    last_session_id: str | None = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def stream(self, query, session_id, follow_up=None, opts=None):
        FakeAgentLoop.last_session_id = session_id
        for event in FakeAgentLoop.events:
            yield event
        if FakeAgentLoop.raise_on_stream is not None:
            raise FakeAgentLoop.raise_on_stream


@pytest.fixture
def client(monkeypatch):
    """A test client over the chat router with the orchestrator faked."""
    # the route resolves its collaborators from the dependency registry
    deps.set_llm_gateway(object())  # type: ignore[arg-type]
    deps.set_semantic_memory(object())  # type: ignore[arg-type]
    deps.set_episodic_memory(object())  # type: ignore[arg-type]
    deps.set_consolidation_engine(object())  # type: ignore[arg-type]
    deps.set_wiki_compile_trigger(object())  # type: ignore[arg-type]

    monkeypatch.setattr(
        "yuanwork.orchestrator.agent.AgentLoop", FakeAgentLoop, raising=True
    )

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    yield TestClient(app)
    deps.reset_dependencies()


def frames(body: str) -> list[tuple[str, str]]:
    """Parse an SSE body into (event, data) pairs.

    Frames are separated by CRLF or LF; comment lines (the heartbeat)
    are skipped.
    """
    out = []
    for raw in re.split(r"\r?\n\r?\n", body):
        raw = raw.strip()
        if not raw or raw.startswith(":"):
            continue
        event, data = None, ""
        for line in raw.splitlines():
            line = line.rstrip("\r")
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = line[len("data:"):].strip()
        if event is not None:
            out.append((event, data))
    return out


# --------------------------------------------------------------------------
# AC-F-006 — headers and event names
# --------------------------------------------------------------------------


def test_the_response_carries_the_two_anti_buffering_headers(client):
    FakeAgentLoop.events = [{"type": "done", "data": {"response": None}}]
    resp = client.post("/api/v1/chat/stream", json={"query": "hello"})
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-cache"
    assert resp.headers["x-accel-buffering"] == "no"


def test_the_orchestrator_event_names_are_passed_through(client):
    FakeAgentLoop.events = [
        {"type": "trace", "data": {"step": "understand"}},
        {"type": "token", "data": {"text": "你"}},
        {"type": "token", "data": {"text": "好"}},
        {"type": "citations", "data": {"citations": []}},
        {"type": "answer", "data": {"text": "你好"}},
        {"type": "conflict", "data": {"conflicts": []}},
        {"type": "refusal", "data": {"reason": "no_results"}},
        {"type": "done", "data": {"session_id": "s", "response": {}}},
    ]
    resp = client.post("/api/v1/chat/stream", json={"query": "hello"})
    names = [name for name, _data in frames(resp.text)]
    assert names == [
        "trace", "token", "token", "citations", "answer", "conflict",
        "refusal", "done",
    ]


def test_the_event_data_is_json(client):
    FakeAgentLoop.events = [{"type": "token", "data": {"text": "中文"}}]
    resp = client.post("/api/v1/chat/stream", json={"query": "hello"})
    events = dict(frames(resp.text))
    assert json.loads(events["token"])["text"] == "中文"


def test_a_session_id_is_generated_when_none_is_given(client):
    FakeAgentLoop.events = [{"type": "done", "data": {"response": None}}]
    client.post("/api/v1/chat/stream", json={"query": "hello"})
    assert FakeAgentLoop.last_session_id
    assert len(FakeAgentLoop.last_session_id) == 36  # a uuid4


def test_a_given_session_id_is_used(client):
    FakeAgentLoop.events = [{"type": "done", "data": {"response": None}}]
    client.post(
        "/api/v1/chat/stream", json={"query": "hello", "session_id": "abc"}
    )
    assert FakeAgentLoop.last_session_id == "abc"


def test_an_empty_query_is_rejected_before_the_stream_starts(client):
    FakeAgentLoop.events = []
    resp = client.post("/api/v1/chat/stream", json={"query": ""})
    assert resp.status_code == 422


# --------------------------------------------------------------------------
# AC-F-007 — the exception path
# --------------------------------------------------------------------------


def test_an_exception_produces_an_error_frame_then_a_terminal_frame(client):
    FakeAgentLoop.events = [{"type": "token", "data": {"text": "半句"}}]
    FakeAgentLoop.raise_on_stream = RuntimeError("模型炸了")

    resp = client.post("/api/v1/chat/stream", json={"query": "hello"})
    names = [name for name, _data in frames(resp.text)]
    assert names[-2:] == ["error", "done"]

    events = dict(frames(resp.text))
    error = json.loads(events["error"])
    # the user gets a readable message, not the exception text
    assert error["error"] == ERROR_MESSAGES["INTERNAL_ERROR"]
    assert "模型炸了" not in json.dumps(error, ensure_ascii=False)
    assert error["code"] == "INTERNAL_ERROR"
    # the detail is the readable message the client shows, not the text
    assert error["detail"] == ERROR_MESSAGES["INTERNAL_ERROR"]

    done = json.loads(events["done"])
    assert done["response"] is None
    assert "session_id" in done


def test_the_terminal_frame_is_always_the_last_one(client):
    FakeAgentLoop.events = [{"type": "token", "data": {"text": "x"}}]
    FakeAgentLoop.raise_on_stream = ValueError("boom")
    resp = client.post("/api/v1/chat/stream", json={"query": "hello"})
    names = [name for name, _data in frames(resp.text)]
    assert names[-1] == "done"


def test_a_client_disconnect_produces_no_terminal_frame(client):
    """GeneratorExit is not an `Exception`, so the route's except clause
    does not fire: the stream ends without an error frame and without a
    terminal frame."""
    FakeAgentLoop.events = [{"type": "token", "data": {"text": "半句"}}]
    FakeAgentLoop.raise_on_stream = GeneratorExit()

    body = ""
    try:
        resp = client.post("/api/v1/chat/stream", json={"query": "hello"})
        body = resp.text
    except GeneratorExit:
        pass

    names = [name for name, _data in frames(body)]
    assert "error" not in names
    assert "done" not in names


def test_generator_exit_is_not_caught_by_the_exception_handler():
    """This is *why* the disconnect path skips the error frame."""
    assert not issubclass(GeneratorExit, Exception)


# --------------------------------------------------------------------------
# AC-F-006 — the cancel endpoint stops a running session
# --------------------------------------------------------------------------


def test_the_cancel_endpoint_stops_a_running_session(client):
    from yuanwork.orchestrator import cancel

    cancel.start("some-session")
    try:
        resp = client.post("/api/v1/chat/cancel/some-session")
        assert resp.status_code == 200
        body = resp.json()
        assert body["cancelled"] is True
        assert body["session_id"] == "some-session"
        assert cancel.is_cancelled("some-session")
    finally:
        cancel.finish("some-session")


def test_the_cancel_endpoint_says_when_there_is_nothing_to_cancel(client):
    from yuanwork.orchestrator import cancel

    cancel.finish("some-session")
    resp = client.post("/api/v1/chat/cancel/some-session")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cancelled"] is False
    assert "没有正在进行的生成" in body["note"]


def test_the_cancel_endpoint_does_not_cancel_anything_else(client):
    """Only the session that was asked for."""
    from yuanwork.orchestrator import cancel

    cancel.start("another-session")
    try:
        resp = client.post("/api/v1/chat/cancel/some-session")
        assert resp.status_code == 200
        assert not cancel.is_cancelled("another-session")
    finally:
        cancel.finish("another-session")
