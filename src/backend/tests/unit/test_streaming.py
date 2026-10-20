"""Tests for real token streaming.

The bug these guard against: `chat_stream` used to look like it streamed
but did not. The agent awaited a full `complete()` call, the gateway
iterated litellm's blocking generator on the event loop, and the answer
arrived in one lump at the end.

The decisive test here is
`test_gateway_stream_does_not_block_event_loop` - it fails if the
generator is pulled on the event loop, and it fails if the whole answer
is buffered and flushed at the end.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest


# ===== Helpers =====


class _FakeUsage:
    def __init__(self, prompt_tokens: int = 10, completion_tokens: int = 5):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = prompt_tokens + completion_tokens


class _FakeDelta:
    def __init__(self, content: str | None):
        self.content = content


class _FakeChoice:
    def __init__(self, content: str | None):
        self.delta = _FakeDelta(content)
        self.finish_reason = None


class _FakeChunk:
    """Mimics a litellm streaming chunk."""

    def __init__(self, content: str | None, usage: _FakeUsage | None = None):
        self.choices = [_FakeChoice(content)]
        self.usage = usage


def _fake_stream(deltas: list[str], delay: float = 0.0):
    """A blocking generator that yields one chunk per delta."""

    def _gen():
        for i, text in enumerate(deltas):
            if delay:
                time.sleep(delay)
            usage = _FakeUsage(completion_tokens=len(deltas)) if i == len(deltas) - 1 else None
            yield _FakeChunk(text, usage)

    return _gen()


@pytest.fixture
def gw(temp_dir: Path):
    """An LLMGateway pointed at a temp config dir with one fake model."""
    from yuanwork.config import YuanWorkSettings
    from yuanwork.llm.gateway import LLMGateway

    (temp_dir / "models.yaml").write_text(
        """
models:
  fake-mini:
    provider: openai
    api_key: sk-not-a-real-key
    model_name: gpt-4o-mini
routing:
  general: fake-mini
  answer: fake-mini
""",
        encoding="utf-8",
    )
    settings = YuanWorkSettings(config_dir=temp_dir)
    return LLMGateway(settings)


def _task():
    from yuanwork.llm.gateway import LLMMessage, LLMTask

    return LLMTask(
        task_type="answer",
        messages=[LLMMessage(role="user", content="hello")],
    )


# ===== LLMGateway.stream() =====


@pytest.mark.asyncio
async def test_gateway_stream_yields_deltas_in_order(gw, monkeypatch):
    """Deltas are yielded one by one, in the order the provider produced them."""
    monkeypatch.setattr(
        "yuanwork.llm.gateway.litellm.completion",
        lambda **kwargs: _fake_stream(["He", "llo", " world"]),
    )

    received = [d async for d in gw.stream(_task())]

    assert received == ["He", "llo", " world"]


@pytest.mark.asyncio
async def test_gateway_stream_does_not_block_event_loop(gw, monkeypatch):
    """The event loop stays free while a stream is in flight.

    This is the test that fails if the blocking litellm generator is
    iterated directly on the loop: the heartbeat task could not run until
    the whole generation was over.
    """
    monkeypatch.setattr(
        "yuanwork.llm.gateway.litellm.completion",
        lambda **kwargs: _fake_stream(["a", "b", "c", "d", "e"], delay=0.05),
    )

    beats = 0
    stop = False

    async def heartbeat():
        nonlocal beats
        while not stop:
            await asyncio.sleep(0.005)
            beats += 1

    beat_task = asyncio.create_task(heartbeat())
    try:
        received = [d async for d in gw.stream(_task())]
    finally:
        stop = True
        await beat_task

    assert received == ["a", "b", "c", "d", "e"]
    # The generation takes ~250ms. If the loop were blocked, the heartbeat
    # would have run at most once. A free loop gets many ticks.
    assert beats >= 5, f"event loop was blocked during streaming (beats={beats})"


@pytest.mark.asyncio
async def test_gateway_stream_does_not_buffer_until_the_end(gw, monkeypatch):
    """Each delta is delivered before the provider finished producing them.

    A "stream" that collects everything and yields at the end would pass
    the ordering test above but fail this one.
    """
    deltas = ["x"] * 5
    produced_at: list[float] = []
    start = time.monotonic()

    def _completion(**kwargs):
        def _gen():
            for text in deltas:
                time.sleep(0.04)
                produced_at.append(time.monotonic())
                yield _FakeChunk(text)

        return _gen()

    monkeypatch.setattr(
        "yuanwork.llm.gateway.litellm.completion", _completion
    )

    arrival_at: list[float] = []
    async for _ in gw.stream(_task()):
        arrival_at.append(time.monotonic())

    assert len(arrival_at) == 5
    # The last delta must have been delivered before the provider was done
    # producing it - i.e. the consumer is not waiting for the end.
    assert arrival_at[-1] < produced_at[-1] + 0.02, (
        "deltas were buffered and flushed at the end "
        f"(arrival={arrival_at[-1] - start:.3f}s, produced={produced_at[-1] - start:.3f}s)"
    )


@pytest.mark.asyncio
async def test_gateway_stream_propagates_provider_error(gw, monkeypatch):
    """An error from the provider surfaces in the consumer."""

    def _boom(**kwargs):
        def _gen():
            yield _FakeChunk("partial")
            raise RuntimeError("provider exploded")

        return _gen()

    monkeypatch.setattr("yuanwork.llm.gateway.litellm.completion", _boom)

    received = []
    with pytest.raises(RuntimeError, match="provider exploded"):
        async for delta in gw.stream(_task()):
            received.append(delta)

    # The partial text before the failure was still delivered.
    assert received == ["partial"]


@pytest.mark.asyncio
async def test_gateway_stream_audit_written_once(gw, monkeypatch, temp_dir: Path):
    """A streamed call writes exactly one audit entry."""
    from yuanwork.privacy.audit import AuditLogger

    audit = AuditLogger(temp_dir / "audit.jsonl")
    gw.audit_logger = audit

    monkeypatch.setattr(
        "yuanwork.llm.gateway.litellm.completion",
        lambda **kwargs: _fake_stream(["Hi", " there"]),
    )

    async for _ in gw.stream(_task()):
        pass

    entries = audit.read_all()
    assert len(entries) == 1
    assert entries[0]["success"] is True
    assert "Hi there" in entries[0]["response_summary"]


@pytest.mark.asyncio
async def test_gateway_stream_audit_written_on_error(gw, monkeypatch, temp_dir: Path):
    """A failed stream still writes an audit entry, marked as failed."""
    from yuanwork.privacy.audit import AuditLogger

    audit = AuditLogger(temp_dir / "audit.jsonl")
    gw.audit_logger = audit

    def _boom(**kwargs):
        def _gen():
            raise RuntimeError("nope")
            yield  # pragma: no cover - unreachable, makes this a generator

        return _gen()

    monkeypatch.setattr("yuanwork.llm.gateway.litellm.completion", _boom)

    with pytest.raises(RuntimeError):
        async for _ in gw.stream(_task()):
            pass

    entries = audit.read_all()
    assert len(entries) == 1
    assert entries[0]["success"] is False


# ===== AgentLoop.stream() =====


class _StubGateway:
    """A gateway that streams a fixed list of deltas."""

    def __init__(self, deltas: list[str], fail_with: Exception | None = None):
        self.deltas = deltas
        self.fail_with = fail_with
        self.stream_calls = 0
        self.complete_calls = 0

    async def stream(self, task):
        self.stream_calls += 1
        for delta in self.deltas:
            yield delta
        if self.fail_with is not None:
            raise self.fail_with

    async def complete(self, task):
        self.complete_calls += 1
        from yuanwork.llm.gateway import LLMResponse

        return LLMResponse(
            content="".join(self.deltas), model="stub", finish_reason="stop"
        )


def _agent(gateway, tmp_path: Path, evidence=None, confidence=None):
    from yuanwork.orchestrator.agent import AgentLoop

    agent = AgentLoop(
        llm_gateway=gateway,
        semantic_memory=None,  # type: ignore[arg-type]
        episodic_memory=None,  # type: ignore[arg-type]
    )
    return agent


def _patch_steps(agent, monkeypatch, evidence, confidence):
    """Short-circuit every step except the answer, so only the streaming
    path runs for real."""

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


@pytest.mark.asyncio
async def test_agent_stream_emits_token_events(monkeypatch, temp_dir: Path):
    """The agent forwards each LLM delta as a `token` event."""
    gateway = _StubGateway(["违约", "金为", "万分之三"])
    agent = _agent(gateway, temp_dir)
    _patch_steps(agent, monkeypatch, EVIDENCE, CONFIDENCE)

    events = [e async for e in agent.stream(query="违约金?", session_id="s1")]

    token_events = [e for e in events if e["type"] == "token"]
    assert [e["data"]["text"] for e in token_events] == ["违约", "金为", "万分之三"]

    # The gateway's streaming path was used, not the blocking one.
    assert gateway.stream_calls == 1
    assert gateway.complete_calls == 0


@pytest.mark.asyncio
async def test_agent_stream_answer_event_carries_full_text(monkeypatch, temp_dir: Path):
    """The `answer` event carries the complete text, for clients that
    only listen for that event."""
    gateway = _StubGateway(["ab", "cd"])
    agent = _agent(gateway, temp_dir)
    _patch_steps(agent, monkeypatch, EVIDENCE, CONFIDENCE)

    events = [e async for e in agent.stream(query="q", session_id="s1")]

    answer = [e for e in events if e["type"] == "answer"]
    assert len(answer) == 1
    assert answer[0]["data"]["text"] == "abcd"


@pytest.mark.asyncio
async def test_agent_stream_done_carries_full_response(monkeypatch, temp_dir: Path):
    """The terminal `done` event carries the whole response object."""
    gateway = _StubGateway(["ab", "cd"])
    agent = _agent(gateway, temp_dir)
    _patch_steps(agent, monkeypatch, EVIDENCE, CONFIDENCE)

    events = [e async for e in agent.stream(query="q", session_id="s1")]

    done = [e for e in events if e["type"] == "done"]
    assert len(done) == 1
    response = done[0]["data"]["response"]
    assert response["answer"] == "abcd"
    assert response["confidence"] == 0.9
    assert response["refused"] is False
    assert response["session_id"] == "s1"
    assert len(response["citations"]) == 1
    assert response["citations"][0]["file_name"] == "contract.pdf"


@pytest.mark.asyncio
async def test_agent_stream_emits_citations(monkeypatch, temp_dir: Path):
    """Citations are emitted after the answer."""
    gateway = _StubGateway(["ab"])
    agent = _agent(gateway, temp_dir)
    _patch_steps(agent, monkeypatch, EVIDENCE, CONFIDENCE)

    events = [e async for e in agent.stream(query="q", session_id="s1")]

    types = [e["type"] for e in events]
    assert "citations" in types
    assert types.index("token") < types.index("citations")
    assert types.index("citations") < types.index("done")


@pytest.mark.asyncio
async def test_agent_stream_refusal_emits_no_tokens(monkeypatch, temp_dir: Path):
    """The refusal path produces a refusal event and no token events."""
    gateway = _StubGateway(["should not appear"])
    agent = _agent(gateway, temp_dir)
    _patch_steps(
        agent,
        monkeypatch,
        EVIDENCE,
        {"confidence": 0.1, "should_refuse": True, "reason": "no_results"},
    )

    events = [e async for e in agent.stream(query="q", session_id="s1")]

    assert not [e for e in events if e["type"] == "token"]
    refusals = [e for e in events if e["type"] == "refusal"]
    assert len(refusals) == 1
    assert refusals[0]["data"]["reason"] == "no_results"

    done = [e for e in events if e["type"] == "done"][0]
    assert done["data"]["response"]["refused"] is True
    assert gateway.stream_calls == 0


@pytest.mark.asyncio
async def test_agent_stream_error_keeps_partial_text(monkeypatch, temp_dir: Path):
    """If the model fails mid-answer, the text produced so far is kept."""
    gateway = _StubGateway(["partial text"], fail_with=RuntimeError("boom"))
    agent = _agent(gateway, temp_dir)
    _patch_steps(agent, monkeypatch, EVIDENCE, CONFIDENCE)

    events = [e async for e in agent.stream(query="q", session_id="s1")]

    tokens = [e["data"]["text"] for e in events if e["type"] == "token"]
    assert tokens == ["partial text"]

    done = [e for e in events if e["type"] == "done"][0]
    response = done["data"]["response"]
    assert response["refused"] is True
    assert "partial text" in response["answer"]
    assert "生成回答时出现错误" in response["answer"]


@pytest.mark.asyncio
async def test_agent_run_still_uses_the_blocking_path(monkeypatch, temp_dir: Path):
    """AgentLoop.run() is unchanged - it still uses complete(), not stream()."""
    gateway = _StubGateway(["ab", "cd"])
    agent = _agent(gateway, temp_dir)
    _patch_steps(agent, monkeypatch, EVIDENCE, CONFIDENCE)

    result = await agent.run(query="q", session_id="s1")

    assert result["answer"] == "abcd"
    assert gateway.complete_calls == 1
    assert gateway.stream_calls == 0


# ===== SSE route =====


@pytest.mark.asyncio
async def test_sse_route_emits_token_frames(monkeypatch, temp_dir: Path):
    """The HTTP route really puts token events on the wire."""
    import json

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from yuanwork.api.routes import chat as chat_routes

    gateway = _StubGateway(["第一", "第二"])
    agent = _agent(gateway, temp_dir)
    _patch_steps(agent, monkeypatch, EVIDENCE, CONFIDENCE)

    # The route imports these inside the handler, so the patch has to be
    # on the source module, not on the route module.
    monkeypatch.setattr("yuanwork.orchestrator.agent.AgentLoop", lambda **kw: agent)
    monkeypatch.setattr("yuanwork.api.deps.get_llm_gateway", lambda: gateway)
    monkeypatch.setattr("yuanwork.api.deps.get_semantic_memory", lambda: None)
    monkeypatch.setattr("yuanwork.api.deps.get_episodic_memory", lambda: None)
    monkeypatch.setattr("yuanwork.api.deps.get_consolidation_engine", lambda: None)
    monkeypatch.setattr("yuanwork.api.deps.get_wiki_compile_trigger", lambda: None)

    app = FastAPI()
    app.include_router(chat_routes.router, prefix="/api/v1")

    with TestClient(app) as client:
        resp = client.post("/api/v1/chat/stream", json={"query": "违约金?"})

    assert resp.status_code == 200
    body = resp.text

    # sse-starlette terminates frames with CRLF.
    assert "event: token\r\n" in body
    assert "event: done\r\n" in body
    assert "event: citations\r\n" in body

    # The token payloads carry the deltas.
    token_payloads = [
        json.loads(line[len("data: "):])
        for line in body.split("\r\n")
        if line.startswith("data: ") and '"text"' in line
    ]
    assert any(p.get("text") == "第一" for p in token_payloads)
    assert any(p.get("text") == "第二" for p in token_payloads)

    # The done event carries the whole response.
    done_payload = None
    for line in body.split("\r\n"):
        if line.startswith("data: ") and '"response"' in line:
            done_payload = json.loads(line[len("data: "):])
            break
    assert done_payload is not None
    assert done_payload["response"]["answer"] == "第一第二"


@pytest.mark.asyncio
async def test_sse_route_emits_done_on_error(monkeypatch, temp_dir: Path):
    """A failing stream still closes with a terminal `done` event."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from yuanwork.api.routes import chat as chat_routes

    class _Exploding:
        def __init__(self, **kwargs):
            pass

        async def stream(self, **kwargs):
            raise RuntimeError("agent exploded")
            yield  # pragma: no cover

    monkeypatch.setattr("yuanwork.orchestrator.agent.AgentLoop", _Exploding)
    monkeypatch.setattr("yuanwork.api.deps.get_llm_gateway", lambda: None)
    monkeypatch.setattr("yuanwork.api.deps.get_semantic_memory", lambda: None)
    monkeypatch.setattr("yuanwork.api.deps.get_episodic_memory", lambda: None)
    monkeypatch.setattr("yuanwork.api.deps.get_consolidation_engine", lambda: None)
    monkeypatch.setattr("yuanwork.api.deps.get_wiki_compile_trigger", lambda: None)

    app = FastAPI()
    app.include_router(chat_routes.router, prefix="/api/v1")

    with TestClient(app) as client:
        resp = client.post("/api/v1/chat/stream", json={"query": "q"})

    assert "event: error\r\n" in resp.text
    assert "event: done\r\n" in resp.text
    assert '"response": null' in resp.text


def _settings(temp_dir: Path):
    from yuanwork.config import YuanWorkSettings

    (temp_dir / "models.yaml").write_text(
        """
models:
  fake-mini:
    provider: openai
    api_key: sk-not-a-real-key
    model_name: gpt-4o-mini
routing:
  general: fake-mini
""",
        encoding="utf-8",
    )
    return YuanWorkSettings(config_dir=temp_dir, data_dir=temp_dir / "data")
