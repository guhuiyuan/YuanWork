"""The gateway's streaming mechanism.

The provider's stream is a *blocking* generator, so it is driven on a
worker thread and the deltas are handed back through a queue. That is
what keeps the event loop free, and it is also what makes the
cancellation and the audit trail work.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from yuanwork.config import YuanWorkSettings
from yuanwork.llm.gateway import LLMGateway, LLMMessage, LLMTask


def make_gateway(tmp_path: Path, audit_logger=None) -> LLMGateway:
    gateway = LLMGateway(
        YuanWorkSettings(data_dir=tmp_path / "data"), audit_logger=audit_logger
    )
    gateway._models = {"alpha": {"provider": "anthropic", "model_name": "A", "api_key": "k"}}
    gateway._routing = {"general": "alpha", "answer": "alpha"}
    return gateway


class FakeChunk:
    def __init__(self, text: str | None = None, usage=None):
        self.choices = []
        if text is not None:
            delta = type("D", (), {"content": text})()
            self.choices = [type("C", (), {"delta": delta})()]
        self.usage = usage


class FakeStream:
    """A blocking generator, like the provider's."""

    def __init__(self, chunks, delay: float = 0.0):
        self.chunks = chunks
        self.delay = delay
        self.closed = False
        self.pulled = 0

    def __iter__(self):
        for chunk in self.chunks:
            self.pulled += 1
            if self.delay:
                time.sleep(self.delay)
            yield chunk

    def close(self):
        self.closed = True


def install_stream(monkeypatch, stream):
    def fake_completion(**kwargs):
        assert kwargs["stream"] is True
        return stream

    monkeypatch.setattr("yuanwork.llm.gateway.litellm.completion", fake_completion)


def collect(gateway, count: int | None = None) -> list[str]:
    """Drain the stream."""

    async def run():
        out = []
        async for delta in gateway.stream(LLMTask(
            task_type="answer", messages=[LLMMessage(role="user", content="你好")]
        )):
            out.append(delta)
            if count is not None and len(out) >= count:
                break
        return out

    return asyncio.run(run())


# --------------------------------------------------------------------------
# the deltas
# --------------------------------------------------------------------------


def test_the_deltas_come_through_in_order(tmp_path: Path, monkeypatch):
    stream = FakeStream([FakeChunk("你"), FakeChunk("好")])
    install_stream(monkeypatch, stream)
    gateway = make_gateway(tmp_path)

    assert collect(gateway) == ["你", "好"]


def test_a_chunk_without_text_is_skipped(tmp_path: Path, monkeypatch):
    stream = FakeStream([FakeChunk(None), FakeChunk("好"), FakeChunk("")])
    install_stream(monkeypatch, stream)
    gateway = make_gateway(tmp_path)

    assert collect(gateway) == ["好"]


def test_the_stream_flag_is_set(tmp_path: Path, monkeypatch):
    captured: dict = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return FakeStream([FakeChunk("好")])

    monkeypatch.setattr("yuanwork.llm.gateway.litellm.completion", fake_completion)
    gateway = make_gateway(tmp_path)
    collect(gateway)

    assert captured["stream"] is True
    assert captured["timeout"] == 120.0


# --------------------------------------------------------------------------
# the event loop stays free
# --------------------------------------------------------------------------


def test_the_event_loop_is_free_during_the_stream(tmp_path: Path, monkeypatch):
    """The provider is slow; other work still runs."""
    stream = FakeStream(
        [FakeChunk(f"t{i}") for i in range(5)], delay=0.05
    )
    install_stream(monkeypatch, stream)
    gateway = make_gateway(tmp_path)

    async def run():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.005)

        task = asyncio.create_task(heartbeat())
        deltas = []
        async for delta in gateway.stream(LLMTask(
            task_type="answer", messages=[LLMMessage(role="user", content="你好")]
        )):
            deltas.append(delta)
        task.cancel()
        return deltas, ticks

    deltas, ticks = asyncio.run(run())
    assert len(deltas) == 5
    # the heartbeat ran while the provider was still producing
    assert ticks > 1


# --------------------------------------------------------------------------
# the cancellation
# --------------------------------------------------------------------------


def test_a_consumer_that_walks_away_stops_the_worker(tmp_path: Path, monkeypatch):
    """Leaving the stream early must not keep pulling from the provider."""
    stream = FakeStream([FakeChunk(f"t{i}") for i in range(50)], delay=0.01)
    install_stream(monkeypatch, stream)
    gateway = make_gateway(tmp_path)

    async def run():
        async for delta in gateway.stream(LLMTask(
            task_type="answer", messages=[LLMMessage(role="user", content="你好")]
        )):
            return delta  # walk away after the first delta

    asyncio.run(run())
    time.sleep(0.2)  # give the worker a moment to notice
    assert stream.pulled < 50


def test_the_provider_connection_is_closed(tmp_path: Path, monkeypatch):
    stream = FakeStream([FakeChunk("好")])
    install_stream(monkeypatch, stream)
    gateway = make_gateway(tmp_path)

    collect(gateway)
    assert stream.closed is True


# --------------------------------------------------------------------------
# the failures
# --------------------------------------------------------------------------


def test_a_provider_failure_is_raised_to_the_consumer(tmp_path: Path, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("provider 挂了")

    monkeypatch.setattr("yuanwork.llm.gateway.litellm.completion", boom)
    gateway = make_gateway(tmp_path)

    with pytest.raises(RuntimeError):
        collect(gateway)


def test_a_failure_mid_stream_is_raised_after_the_deltas(tmp_path: Path, monkeypatch):
    class FailingStream:
        def __iter__(self):
            yield FakeChunk("半句")
            raise RuntimeError("断了")

        def close(self):
            pass

    install_stream(monkeypatch, FailingStream())
    gateway = make_gateway(tmp_path)

    async def run():
        out = []
        async for delta in gateway.stream(LLMTask(
            task_type="answer", messages=[LLMMessage(role="user", content="你好")]
        )):
            out.append(delta)
        return out

    with pytest.raises(RuntimeError):
        asyncio.run(run())


# --------------------------------------------------------------------------
# the audit trail
# --------------------------------------------------------------------------


def test_the_audit_entry_is_written_even_when_the_consumer_walks_away(
    tmp_path: Path, monkeypatch
):
    """The audit is written on the worker thread, so an early exit does
    not lose the record."""
    from yuanwork.privacy.audit import AuditLogger

    logger = AuditLogger(tmp_path / "audit.jsonl")
    gateway = make_gateway(tmp_path, audit_logger=logger)
    install_stream(monkeypatch, FakeStream([FakeChunk(f"t{i}") for i in range(20)]))

    async def run():
        async for _delta in gateway.stream(LLMTask(
            task_type="answer", messages=[LLMMessage(role="user", content="你好")]
        )):
            break

    asyncio.run(run())
    time.sleep(0.2)
    assert len(logger.read_all()) == 1


def test_the_stream_runs_on_a_worker_thread(tmp_path: Path, monkeypatch):
    """The provider call happens off the event loop's thread."""
    seen: list[int] = []

    def fake_completion(**kwargs):
        seen.append(threading.get_ident())
        return FakeStream([FakeChunk("好")])

    monkeypatch.setattr("yuanwork.llm.gateway.litellm.completion", fake_completion)
    gateway = make_gateway(tmp_path)

    async def run():
        main_thread = threading.get_ident()
        async for _ in gateway.stream(LLMTask(
            task_type="answer", messages=[LLMMessage(role="user", content="你好")]
        )):
            pass
        return main_thread

    main_thread = asyncio.run(run())
    assert seen and seen[0] != main_thread
