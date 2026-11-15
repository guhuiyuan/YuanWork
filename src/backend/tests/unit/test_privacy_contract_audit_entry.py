"""The audit log: what it records and how it is read.

The audit log answers two questions - *did I call the model* and *how
much did it cost*. It used to answer a third one: *what did I ask*, by
keeping the first 200 characters of every prompt and answer. That made
the log a second copy of the user's data, which is exactly what the
privacy promise says it is not.

An entry now holds the *hash* of the prompt, its length, the token
counts and the duration - never the text. The logger's API does not
accept the text either, so it cannot be stored by accident.

* the entry's fields
* the prompt hash and its length
* the failure is recorded as a kind, not as its text
* the promise the privacy document makes, and what the code does
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yuanwork.privacy.audit import AuditLogger, classify_error


@pytest.fixture
def logger(tmp_path: Path) -> AuditLogger:
    return AuditLogger(tmp_path / "logs" / "audit.jsonl")


def log(logger: AuditLogger, **overrides):
    entry = {
        "model": "minimax-chat",
        "task_type": "answer",
        "prompt_hash": "abc1234567890123",
        "prompt_length": 12,
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "duration_ms": 42.0,
        "success": True,
        "error_kind": None,
    }
    entry.update(overrides)
    logger.log_llm_call(**entry)


# --------------------------------------------------------------------------
# the entry
# --------------------------------------------------------------------------


def test_an_entry_round_trips(logger):
    log(logger)
    entries = logger.read_all()
    assert len(entries) == 1
    entry = entries[0]
    assert entry["model"] == "minimax-chat"
    assert entry["task_type"] == "answer"
    assert entry["prompt_tokens"] == 10
    assert entry["completion_tokens"] == 5
    assert entry["duration_ms"] == 42.0
    assert entry["success"] is True
    assert entry["error_kind"] is None


def test_the_entry_has_a_timestamp(logger):
    log(logger)
    entry = logger.read_all()[0]
    assert entry["timestamp"]
    assert "T" in entry["timestamp"]


def test_the_entry_type_is_the_llm_call(logger):
    log(logger)
    assert logger.read_all()[0]["type"] == "llm_call"


def test_the_entry_has_the_prompt_hash_and_its_length(logger):
    log(logger, prompt_hash="0123456789abcdef", prompt_length=1234)
    entry = logger.read_all()[0]
    assert entry["prompt_hash"] == "0123456789abcdef"
    assert entry["prompt_length"] == 1234


def test_a_failure_is_recorded_too(logger):
    log(logger, success=False, error_kind="timeout")
    entry = logger.read_all()[0]
    assert entry["success"] is False
    assert entry["error_kind"] == "timeout"


# --------------------------------------------------------------------------
# no text, anywhere
# --------------------------------------------------------------------------


def test_the_entry_does_not_keep_the_prompt_or_the_answer(logger):
    """The promise in the privacy document, and the code behind it."""
    log(logger, prompt_hash="abc1234567890123", prompt_length=12)

    entry = logger.read_all()[0]

    assert "prompt_summary" not in entry
    assert "response_summary" not in entry
    assert "prompt" not in entry
    assert "response" not in entry


def test_the_logger_cannot_be_asked_to_store_text(logger):
    """There is no parameter that would take the prompt or the answer."""
    import inspect

    params = set(inspect.signature(AuditLogger.log_llm_call).parameters)

    assert "prompt_summary" not in params
    assert "response_summary" not in params
    assert "prompt" not in params
    assert "response" not in params


def test_a_failure_is_a_kind_not_a_text(logger):
    """An exception can carry the prompt, a local path or an endpoint."""
    log(logger, success=False, error_kind="connection")

    entry = logger.read_all()[0]

    assert entry["error_kind"] == "connection"
    assert "error" not in entry


def test_the_error_classifier_never_returns_the_text():
    class Boom(Exception):
        pass

    exc = Boom("用户问的是：发票金额是多少")

    kind = classify_error(exc)

    assert kind == "unknown"
    assert "发票" not in kind


def test_the_error_classifier_knows_the_common_kinds():
    class APITimeoutError(Exception):
        pass

    class RateLimitError(Exception):
        pass

    assert classify_error(APITimeoutError("x")) == "timeout"
    assert classify_error(RateLimitError("x")) == "rate_limit"
    assert classify_error(None) is None


def test_the_prompt_hash_is_stable():
    """The same prompt hashes to the same value."""
    from yuanwork.llm.gateway import LLMMessage, _query_hash

    messages = [LLMMessage(role="user", content="同一个问题")]
    assert _query_hash(messages) == _query_hash(messages)
    assert len(_query_hash(messages)) == 16


def test_different_prompts_hash_differently():
    from yuanwork.llm.gateway import LLMMessage, _query_hash

    a = _query_hash([LLMMessage(role="user", content="问题一")])
    b = _query_hash([LLMMessage(role="user", content="问题二")])
    assert a != b


# --------------------------------------------------------------------------
# the data access event
# --------------------------------------------------------------------------


def test_the_data_access_event_is_still_there(logger):
    logger.log_data_access(action="read", resource="chunks", details={})
    entries = logger.read_all()
    assert len(entries) == 1
    assert entries[0]["type"] == "data_access"


def test_the_data_access_logger_has_no_call_site():
    import pathlib

    backend = pathlib.Path(__file__).resolve().parents[2] / "yuanwork"
    callers = []
    for path in backend.rglob("*.py"):
        if path.name == "audit.py":
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        if "log_data_access" in source:
            callers.append(str(path))
    assert callers == []


# --------------------------------------------------------------------------
# what the gateway writes
# --------------------------------------------------------------------------


def test_the_gateway_writes_a_hash_and_a_length(tmp_path: Path, monkeypatch):
    """A real call, with the model replaced by a stub."""
    import asyncio

    from yuanwork.config import YuanWorkSettings
    from yuanwork.llm.gateway import LLMGateway, LLMMessage, LLMTask

    audit = AuditLogger(tmp_path / "audit.jsonl")
    settings = YuanWorkSettings(data_dir=tmp_path / "data")
    gw = LLMGateway(settings, audit_logger=audit)

    def _fake(**kwargs):
        class _Usage:
            prompt_tokens = 7
            completion_tokens = 3
            total_tokens = 10

        class _Msg:
            content = "回答内容"

        class _Choice:
            message = _Msg()
            finish_reason = "stop"

        class _Resp:
            choices = [_Choice()]
            usage = _Usage()

        return _Resp()

    monkeypatch.setattr("yuanwork.llm.gateway.litellm.completion", _fake)

    task = LLMTask(
        task_type="answer",
        messages=[LLMMessage(role="user", content="我的问题是什么")],
    )
    asyncio.run(gw.complete(task))

    entry = audit.read_all()[0]
    assert len(entry["prompt_hash"]) == 16
    assert entry["prompt_length"] == len("我的问题是什么")
    # neither the question nor the answer is in the log
    assert "我的问题是什么" not in json.dumps(entry, ensure_ascii=False)
    assert "回答内容" not in json.dumps(entry, ensure_ascii=False)
