"""Tests for config module."""

from __future__ import annotations

from pathlib import Path

from yuanwork.config import (
    NetworkMode,
    YuanWorkSettings,
    is_domain_allowed,
)


def test_default_settings():
    """Default settings load correctly."""
    settings = YuanWorkSettings(data_dir=Path("/tmp/test_yw"))
    assert settings.port == 8765
    assert settings.host == "127.0.0.1"
    assert settings.network_mode == NetworkMode.LLM_ONLY
    assert settings.embedding_model == "BAAI/bge-m3"
    assert settings.confidence_threshold_high == 0.7
    assert settings.confidence_threshold_low == 0.4


def test_network_modes():
    """Network modes are properly defined."""
    assert NetworkMode.ALLOW_ALL == 0
    assert NetworkMode.LLM_ONLY == 1
    assert NetworkMode.OFFLINE == 2


def test_domain_allowed_in_llm_only_mode():
    """Default allow-list contains expected domains."""
    assert is_domain_allowed("https://api.openai.com/v1/chat")
    assert is_domain_allowed("https://api.anthropic.com/v1/messages")
    assert is_domain_allowed("https://api.deepseek.com/v1/chat")
    assert not is_domain_allowed("https://evil.example.com/steal-data")


def test_data_dir_creation():
    """Data dir is created on instantiation."""
    import shutil
    test_path = Path("/tmp/test_yw_data")
    if test_path.exists():
        shutil.rmtree(test_path)
    settings = YuanWorkSettings(data_dir=test_path)
    assert test_path.exists()
    shutil.rmtree(test_path, ignore_errors=True)
