"""The configuration's defaults and its validation.

* every setting has a default
* the network mode accepts only the integer literals
* the domain list has to be a JSON array
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from yuanwork.config import NetworkMode, YuanWorkSettings, reset_settings


@pytest.fixture(autouse=True)
def _clean_singleton():
    reset_settings()
    yield
    reset_settings()


def make(tmp_path: Path, **kwargs) -> YuanWorkSettings:
    return YuanWorkSettings(data_dir=tmp_path / "data", **kwargs)


# --------------------------------------------------------------------------
# the defaults
# --------------------------------------------------------------------------


def test_the_paths_and_server_defaults(tmp_path: Path):
    settings = make(tmp_path)
    assert settings.host == "127.0.0.1"
    assert settings.port == 8765
    assert settings.log_level == "INFO"


def test_the_privacy_defaults(tmp_path: Path):
    settings = make(tmp_path)
    assert settings.network_mode == NetworkMode.LLM_ONLY
    assert len(settings.allowed_domains) == 4
    assert "api.openai.com" in settings.allowed_domains


def test_the_budget_settings_are_gone(tmp_path: Path):
    """Nothing enforces a token budget or a cost cap, and the target
    state does not promise one - so the settings are gone rather than
    lying about being applied."""
    settings = make(tmp_path)

    assert not hasattr(settings, "per_session_max_tokens")
    assert not hasattr(settings, "per_query_max_tokens")
    assert not hasattr(settings, "monthly_cap_usd")


def test_the_worker_count_setting_is_gone(tmp_path: Path):
    """The application is one process: the state lives in the process."""
    settings = make(tmp_path)

    assert not hasattr(settings, "workers")


def test_the_model_defaults(tmp_path: Path):
    settings = make(tmp_path)
    assert settings.embedding_model == "BAAI/bge-m3"
    # "auto" means the GPU when there is one, the CPU otherwise - and
    # the reason for the fallback is written down (ADR-035).
    assert settings.embedding_device == "auto"
    # The batch size is the biggest lever on how long a build takes,
    # and the default is the measured one (TASK-056).
    assert settings.embedding_batch_size == 32
    assert settings.embedding_max_tokens == 512
    assert settings.embedding_use_fp16 is True
    # The bge-m3 sparse channel is not read by the retrieval side (the
    # sparse channel is the full-text index), so it is off unless the
    # user asks for it (TASK-055).
    assert settings.embedding_sparse_channel is False
    # int8 is about twice as fast on the CPU, but it moves the vectors
    # a little - so it is a setting and not a default (TASK-060).
    assert settings.embedding_quantize == "none"
    assert settings.reranker_model == "BAAI/bge-reranker-v2-m3"
    # The cross-encoder costs 568 MB and 5-30 s on the first use, so it
    # is off unless the user asks for it.
    assert settings.use_reranker is False


def test_the_retrieval_defaults(tmp_path: Path):
    settings = make(tmp_path)
    assert settings.top_k_initial == 50
    assert settings.top_k_rerank == 10
    assert settings.confidence_threshold_high == 0.7
    assert settings.confidence_threshold_low == 0.4
    assert settings.fusion_alpha == 0.65
    assert settings.fusion_beta == 0.35
    assert settings.dense_timeout_seconds == 10.0
    assert settings.sparse_timeout_seconds == 5.0
    assert settings.embedding_timeout_seconds == 60.0


def test_the_fusion_weights_have_to_add_up_to_one(tmp_path: Path):
    """The fused score is alpha * dense + beta * full-text."""
    with pytest.raises(ValueError):
        make(tmp_path, fusion_alpha=0.9, fusion_beta=0.9)

    settings = make(tmp_path, fusion_alpha=0.8, fusion_beta=0.2)
    assert settings.fusion_alpha == 0.8


def test_the_memory_defaults(tmp_path: Path):
    settings = make(tmp_path)
    assert settings.enable_episodic_memory is True
    assert settings.enable_wiki_auto_compile is True
    assert settings.reflection_trigger_turns == 10
    assert settings.reflection_trigger_idle_minutes == 30


def test_no_setting_is_mandatory(tmp_path: Path):
    """Every field has a default, so a bare configuration is valid."""
    settings = YuanWorkSettings(data_dir=tmp_path / "data")
    assert settings.port == 8765


# --------------------------------------------------------------------------
# the validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["0", "1", "2"])
def test_the_network_mode_accepts_the_integers(monkeypatch, tmp_path: Path, value):
    monkeypatch.setenv("YUANWORK_NETWORK_MODE", value)
    assert make(tmp_path).network_mode == int(value)


@pytest.mark.parametrize("value", ["allow_all", "llm_only", "offline", "Llm_Only"])
def test_the_network_mode_rejects_the_names(monkeypatch, tmp_path: Path, value):
    """The documented names do not work — only the integer literals do."""
    monkeypatch.setenv("YUANWORK_NETWORK_MODE", value)
    with pytest.raises(ValidationError):
        make(tmp_path)


def test_the_network_mode_rejects_a_number_out_of_range(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("YUANWORK_NETWORK_MODE", "9")
    with pytest.raises(ValidationError):
        make(tmp_path)


def test_the_domain_list_accepts_a_json_array(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(
        "YUANWORK_ALLOWED_DOMAINS", '["a.example.com", "b.example.com"]'
    )
    settings = make(tmp_path)
    assert settings.allowed_domains == ["a.example.com", "b.example.com"]


def test_the_domain_list_rejects_a_bare_string(monkeypatch, tmp_path: Path):
    """A value that cannot be parsed at all is a settings error, not a
    field validation error."""
    from pydantic_settings import SettingsError

    monkeypatch.setenv("YUANWORK_ALLOWED_DOMAINS", "a.example.com")
    with pytest.raises((ValidationError, SettingsError)):
        make(tmp_path)


def test_the_port_must_be_a_number(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("YUANWORK_PORT", "not-a-port")
    with pytest.raises(ValidationError):
        make(tmp_path)


def test_an_illegal_value_crashes_the_process(monkeypatch, tmp_path: Path):
    """The validation error is not caught anywhere — it propagates out of
    the settings construction."""
    monkeypatch.setenv("YUANWORK_PORT", "not-a-port")
    with pytest.raises(ValidationError):
        make(tmp_path)


# --------------------------------------------------------------------------
# every declared setting is read
# --------------------------------------------------------------------------


def _settings_the_code_reads() -> set[str]:
    """The settings something in the codebase actually reads."""
    import re

    root = Path(__file__).resolve().parents[2] / "yuanwork"
    # `settings.foo`, `get_settings().foo` and `getattr(settings, "foo")`
    pattern = re.compile(
        r"(?:getattr\(\s*settings\s*,\s*[\"']|settings\s*\.\s*)([a-z_][a-z0-9_]*)"
    )
    read: set[str] = set()
    for path in root.rglob("*.py"):
        if path.name == "config.py":
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        read.update(pattern.findall(source))
    return read


def test_every_declared_setting_is_read(tmp_path: Path):
    """A setting nobody reads is a promise the code does not keep.

    The ones that could not be wired were deleted instead - see
    `test_the_budget_settings_are_gone` and
    `test_the_worker_count_setting_is_gone`.
    """
    read = _settings_the_code_reads()
    dead = [
        name for name in YuanWorkSettings.model_fields if name not in read
    ]

    assert dead == [], f"settings nobody reads: {dead}"
