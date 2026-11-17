"""The gateway's timeout, and the capabilities it does *not* have.

* the 120-second request timeout
* no retry, no rate limit, no budget enforcement
* the JSON mode that the Anthropic protocol does not enforce
"""

from __future__ import annotations

import json

import asyncio
import inspect
from pathlib import Path

import pytest

from yuanwork.config import NetworkMode, YuanWorkSettings
from yuanwork.llm.gateway import (
    DEFAULT_REQUEST_TIMEOUT,
    LLMGateway,
    LLMMessage,
    LLMTask,
)


def make_gateway(tmp_path: Path, **settings_kwargs) -> LLMGateway:
    gateway = LLMGateway(YuanWorkSettings(data_dir=tmp_path / "data", **settings_kwargs))
    gateway._models = {
        "alpha": {"provider": "anthropic", "model_name": "Alpha-1", "api_key": "k"},
    }
    gateway._routing = {"general": "alpha", "answer": "alpha"}
    return gateway


def fake_completion(monkeypatch, response=None):
    captured: dict = {}

    def _call(**kwargs):
        captured.update(kwargs)
        return response

    monkeypatch.setattr("litellm.completion", _call)
    return captured


class FakeResponse:
    def __init__(self):
        self.choices = [
            type("C", (), {
                "message": type("M", (), {"content": "答案"})(),
                "finish_reason": "stop",
            })()
        ]
        self.usage = None


# --------------------------------------------------------------------------
# the timeout
# --------------------------------------------------------------------------


def test_the_default_timeout_is_120_seconds():
    assert DEFAULT_REQUEST_TIMEOUT == 120.0


def test_the_timeout_reaches_the_provider(tmp_path: Path, monkeypatch):
    gateway = make_gateway(tmp_path)
    captured = fake_completion(monkeypatch, FakeResponse())

    asyncio.run(gateway.complete(LLMTask(
        task_type="answer", messages=[LLMMessage(role="user", content="你好")]
    )))
    assert captured["timeout"] == 120.0


def test_the_timeout_can_be_overridden_per_call(tmp_path: Path, monkeypatch):
    gateway = make_gateway(tmp_path)
    captured = fake_completion(monkeypatch, FakeResponse())

    asyncio.run(gateway.complete(LLMTask(
        task_type="answer",
        messages=[LLMMessage(role="user", content="你好")],
        timeout=5.0,
    )))
    assert captured["timeout"] == 5.0


def test_the_timeout_is_not_a_configuration_item():
    """It is a module constant, not a setting."""
    settings = YuanWorkSettings(data_dir=Path("/tmp/x"))
    assert not hasattr(settings, "llm_timeout")
    assert not hasattr(settings, "request_timeout")


# --------------------------------------------------------------------------
# the capabilities that are missing
# --------------------------------------------------------------------------


def test_there_is_no_retry():
    """A provider failure is raised to the caller, not retried."""
    from yuanwork.llm import gateway as gateway_module

    source = inspect.getsource(gateway_module)
    assert "num_retries" not in source
    assert "retry" not in source.lower().replace("retrying", "")


def test_there_is_no_rate_limit():
    from yuanwork.llm import gateway as gateway_module

    source = inspect.getsource(gateway_module)
    assert "Semaphore" not in source
    assert "RateLimit" not in source


def test_the_budget_settings_are_never_read():
    """`per_session_max_tokens`, `per_query_max_tokens` and
    `monthly_cap_usd` exist but nothing enforces them.

    The names only appear inside the built-in configuration template —
    they are never *read* from the settings object.
    """
    from yuanwork.llm import gateway as gateway_module

    source = inspect.getsource(gateway_module)
    for name in ("per_session_max_tokens", "per_query_max_tokens", "monthly_cap_usd"):
        assert f"settings.{name}" not in source
        assert f"self.{name}" not in source


def test_the_budget_settings_are_gone():
    """Nothing enforces a token budget or a cost cap, and the target
    state does not promise one - so the settings are gone rather than
    lying about being applied."""
    settings = YuanWorkSettings(data_dir=Path("/tmp/x"))
    assert not hasattr(settings, "per_session_max_tokens")
    assert not hasattr(settings, "per_query_max_tokens")
    assert not hasattr(settings, "monthly_cap_usd")


def test_the_configured_budget_block_is_never_read():
    """`models.yaml` has a `budget` section that the gateway ignores."""
    from yuanwork.llm import gateway as gateway_module

    source = inspect.getsource(gateway_module)
    assert '"budget"' not in source


def test_the_extraction_routing_key_is_dead():
    """No task ever asks for the `extract` model."""
    from yuanwork.llm import gateway as gateway_module
    from yuanwork.orchestrator import agent

    gateway_source = inspect.getsource(gateway_module)
    assert '"extract"' in gateway_source  # the type allows it

    agent_source = inspect.getsource(agent)
    assert 'task_type="extract"' not in agent_source
    assert "task_type='extract'" not in agent_source


# --------------------------------------------------------------------------
# the JSON mode
# --------------------------------------------------------------------------


def test_the_json_mode_only_asks_for_a_response_format(tmp_path: Path, monkeypatch):
    """It does not attach a schema, so the Anthropic protocol has
    nothing to enforce."""
    gateway = make_gateway(tmp_path)
    captured = fake_completion(monkeypatch, FakeResponse())

    asyncio.run(gateway.complete(LLMTask(
        task_type="understand",
        messages=[LLMMessage(role="user", content="你好")],
        json_mode=True,
    )))
    assert captured["response_format"] == {"type": "json_object"}
    assert "json_schema" not in captured["response_format"]


# --------------------------------------------------------------------------
# the offline mode
# --------------------------------------------------------------------------


def test_the_offline_mode_redirects_to_a_local_model(tmp_path: Path, monkeypatch):
    gateway = make_gateway(tmp_path, network_mode=NetworkMode.OFFLINE)
    captured = fake_completion(monkeypatch, FakeResponse())

    asyncio.run(gateway.complete(LLMTask(
        task_type="answer", messages=[LLMMessage(role="user", content="你好")]
    )))
    assert captured["model"] == "ollama/llama3.1:8b"


def test_the_offline_mode_keeps_an_ollama_model(tmp_path: Path, monkeypatch):
    gateway = make_gateway(tmp_path, network_mode=NetworkMode.OFFLINE)
    gateway._models = {
        "alpha": {"provider": "ollama", "model_name": "llama3", "api_key": "k"},
    }
    captured = fake_completion(monkeypatch, FakeResponse())

    asyncio.run(gateway.complete(LLMTask(
        task_type="answer", messages=[LLMMessage(role="user", content="你好")]
    )))
    assert captured["model"] == "ollama/llama3"


# --------------------------------------------------------------------------
# the audit trail
# --------------------------------------------------------------------------


def test_a_failure_is_raised_to_the_caller(tmp_path: Path, monkeypatch):
    gateway = make_gateway(tmp_path)

    def boom(**kwargs):
        raise RuntimeError("provider 挂了")

    monkeypatch.setattr("litellm.completion", boom)

    with pytest.raises(RuntimeError):
        asyncio.run(gateway.complete(LLMTask(
            task_type="answer", messages=[LLMMessage(role="user", content="你好")]
        )))


def test_both_success_and_failure_are_audited(tmp_path: Path, monkeypatch):
    from yuanwork.privacy.audit import AuditLogger

    logger = AuditLogger(tmp_path / "audit.jsonl")
    gateway = LLMGateway(YuanWorkSettings(data_dir=tmp_path / "data"), audit_logger=logger)
    gateway._models = {"alpha": {"provider": "anthropic", "model_name": "A", "api_key": "k"}}
    gateway._routing = {"general": "alpha", "answer": "alpha"}

    fake_completion(monkeypatch, FakeResponse())
    asyncio.run(gateway.complete(LLMTask(
        task_type="answer", messages=[LLMMessage(role="user", content="你好")]
    )))

    def boom(**kwargs):
        raise RuntimeError("挂了")

    monkeypatch.setattr("litellm.completion", boom)
    with pytest.raises(RuntimeError):
        asyncio.run(gateway.complete(LLMTask(
            task_type="answer", messages=[LLMMessage(role="user", content="你好")]
        )))

    entries = logger.read_all()
    assert len(entries) == 2
    assert entries[0]["success"] is True
    assert entries[1]["success"] is False
    assert entries[1]["error_kind"] == "unknown"


def test_the_audit_entry_holds_the_prompt_hash_not_the_prompt(tmp_path, monkeypatch):
    from yuanwork.privacy.audit import AuditLogger

    logger = AuditLogger(tmp_path / "audit.jsonl")
    gateway = LLMGateway(YuanWorkSettings(data_dir=tmp_path / "data"), audit_logger=logger)
    gateway._models = {"alpha": {"provider": "anthropic", "model_name": "A", "api_key": "k"}}
    gateway._routing = {"general": "alpha", "answer": "alpha"}

    fake_completion(monkeypatch, FakeResponse())
    asyncio.run(gateway.complete(LLMTask(
        task_type="answer", messages=[LLMMessage(role="user", content="秘密问题")]
    )))

    entry = logger.read_all()[0]
    assert len(entry["prompt_hash"]) == 16
    assert entry["prompt_length"] == len("秘密问题")
    assert "秘密问题" not in json.dumps(entry, ensure_ascii=False)
