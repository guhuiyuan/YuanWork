"""The model gateway's routing and parameters.

* which model a task type gets
* how an alias becomes a litellm model string
* the parameters that reach the provider
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from yuanwork.config import YuanWorkSettings
from yuanwork.llm.gateway import LLMGateway, LLMMessage, LLMTask


def make_gateway(tmp_path: Path, models: dict, routing: dict) -> LLMGateway:
    """A gateway with a hand-written model table.

    The real configuration file is not used, so the tests do not depend
    on which providers the user happens to have configured.
    """
    gateway = LLMGateway(YuanWorkSettings(data_dir=tmp_path / "data"))
    gateway._models = models
    gateway._routing = routing
    return gateway


MODEL = {
    "alpha": {
        "provider": "anthropic",
        "model_name": "Alpha-1",
        "api_key": "key-a",
        "api_base": "https://alpha.example/anthropic",
    },
    "beta": {
        "provider": "openai",
        "model_name": "Beta-2",
        "api_key": "key-b",
    },
    "nokey": {
        "provider": "anthropic",
        "model_name": "NoKey-3",
    },
}


@pytest.fixture
def gateway(tmp_path: Path) -> LLMGateway:
    return make_gateway(
        tmp_path,
        dict(MODEL),
        {
            "understand": "alpha",
            "answer": "alpha",
            "general": "alpha",
        },
    )


# --------------------------------------------------------------------------
# the routing
# --------------------------------------------------------------------------


def test_the_routed_model_is_used(gateway):
    assert gateway.get_model_for_task("understand") == "alpha"


def test_an_unrouted_task_falls_back_to_general(gateway):
    assert gateway.get_model_for_task("ner") == "alpha"


def test_a_task_without_a_key_falls_back_to_a_model_that_has_one(gateway):
    gateway._routing["ner"] = "nokey"
    assert gateway.get_model_for_task("ner") == "alpha"


def test_the_fallback_picks_the_first_model_with_a_key(gateway):
    gateway._routing["ner"] = "nokey"
    gateway._models = {
        "nokey": MODEL["nokey"],
        "beta": MODEL["beta"],
        "alpha": MODEL["alpha"],
    }
    assert gateway.get_model_for_task("ner") == "beta"


def test_without_any_key_the_routing_target_is_returned(tmp_path: Path):
    gateway = make_gateway(tmp_path, {"nokey": MODEL["nokey"]}, {"general": "nokey"})
    assert gateway.get_model_for_task("general") == "nokey"


def test_without_any_model_at_all_a_default_name_is_returned(tmp_path: Path):
    gateway = make_gateway(tmp_path, {}, {})
    assert gateway.get_model_for_task("general") == "gpt-4o-mini"


def test_the_configured_models_are_listed(gateway):
    assert set(gateway.configured_models()) == {"alpha", "beta", "nokey"}


# --------------------------------------------------------------------------
# the alias -> litellm model string
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "provider,expected",
    [
        ("anthropic", "anthropic/Alpha-1"),
        ("openai", "openai/Alpha-1"),
        ("ollama", "ollama/Alpha-1"),
        ("deepseek", "Alpha-1"),
        ("minimax", "Alpha-1"),
        ("custom", "Alpha-1"),
    ],
)
def test_the_provider_decides_the_model_string(tmp_path: Path, provider, expected):
    gateway = make_gateway(
        tmp_path,
        {"m": {"provider": provider, "model_name": "Alpha-1", "api_key": "k"}},
        {"general": "m"},
    )
    kwargs = gateway._resolve_litellm_kwargs("m")
    assert kwargs["model"] == expected


def test_the_api_key_and_base_are_passed_through(gateway):
    kwargs = gateway._resolve_litellm_kwargs("alpha")
    assert kwargs["api_key"] == "key-a"
    assert kwargs["api_base"] == "https://alpha.example/anthropic"


def test_an_unknown_alias_is_used_as_the_model_name(tmp_path: Path):
    gateway = make_gateway(tmp_path, {}, {"general": "mystery"})
    assert gateway._resolve_litellm_kwargs("mystery") == {"model": "mystery"}


def test_the_model_name_falls_back_to_the_alias(tmp_path: Path):
    gateway = make_gateway(
        tmp_path, {"m": {"provider": "anthropic", "api_key": "k"}}, {"general": "m"}
    )
    assert gateway._resolve_litellm_kwargs("m")["model"] == "anthropic/m"


# --------------------------------------------------------------------------
# the parameters that reach the provider
# --------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, content: str = "回答"):
        self.choices = [
            type("C", (), {
                "message": type("M", (), {"content": content})(),
                "finish_reason": "stop",
            })()
        ]
        self.usage = type("U", (), {
            "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
        })()


def capture(monkeypatch, gateway: LLMGateway, response=None) -> dict:
    """Run a completion and capture the kwargs handed to litellm."""
    captured: dict = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return response or FakeResponse()

    monkeypatch.setattr("yuanwork.llm.gateway.litellm.completion", fake_completion)
    asyncio.run(gateway.complete(
        LLMTask(task_type="answer", messages=[LLMMessage(role="user", content="你好")])
    ))
    return captured


def test_the_default_temperature_is_zero():
    task = LLMTask(task_type="answer", messages=[])
    assert task.temperature == 0.0


def test_the_default_max_tokens_is_unset():
    assert LLMTask(task_type="answer", messages=[]).max_tokens is None


def test_the_default_json_mode_is_off():
    assert LLMTask(task_type="answer", messages=[]).json_mode is False


def test_the_temperature_reaches_the_provider(gateway, monkeypatch):
    kwargs = capture(monkeypatch, gateway)
    assert kwargs["temperature"] == 0.0


def test_the_max_tokens_reaches_the_provider(gateway, monkeypatch):
    captured: dict = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr("yuanwork.llm.gateway.litellm.completion", fake_completion)
    asyncio.run(gateway.complete(LLMTask(
        task_type="answer",
        messages=[LLMMessage(role="user", content="你好")],
        max_tokens=2000,
    )))
    assert captured["max_tokens"] == 2000


def test_the_max_tokens_are_omitted_when_unset(gateway, monkeypatch):
    kwargs = capture(monkeypatch, gateway)
    assert "max_tokens" not in kwargs


def test_the_json_mode_becomes_a_response_format(gateway, monkeypatch):
    captured: dict = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr("yuanwork.llm.gateway.litellm.completion", fake_completion)
    asyncio.run(gateway.complete(LLMTask(
        task_type="understand",
        messages=[LLMMessage(role="user", content="你好")],
        json_mode=True,
    )))
    assert captured["response_format"] == {"type": "json_object"}


def test_the_messages_are_passed_as_plain_dicts(gateway, monkeypatch):
    kwargs = capture(monkeypatch, gateway)
    assert kwargs["messages"] == [{"role": "user", "content": "你好"}]


def test_the_response_is_wrapped(gateway, monkeypatch):
    captured: dict = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return FakeResponse(content="答案")

    monkeypatch.setattr("yuanwork.llm.gateway.litellm.completion", fake_completion)
    response = asyncio.run(gateway.complete(LLMTask(
        task_type="answer", messages=[LLMMessage(role="user", content="你好")]
    )))
    assert response.content == "答案"
    assert response.model == "alpha"
    assert response.finish_reason == "stop"
    assert response.usage["total_tokens"] == 15
