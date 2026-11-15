"""The configuration's prefix and precedence rules.

* only `YUANWORK_`-prefixed variables are read
* the order is: constructor argument, environment, .env file, default
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yuanwork.config import YuanWorkSettings, get_settings, reset_settings


@pytest.fixture(autouse=True)
def _clean_singleton():
    reset_settings()
    yield
    reset_settings()


def make(tmp_path: Path, **kwargs) -> YuanWorkSettings:
    return YuanWorkSettings(data_dir=tmp_path / "data", **kwargs)


# --------------------------------------------------------------------------
# the prefix
# --------------------------------------------------------------------------


def test_the_prefix_is_yuanwork():
    assert YuanWorkSettings.model_config["env_prefix"] == "YUANWORK_"


def test_a_prefixed_variable_is_read(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("YUANWORK_PORT", "9123")
    assert make(tmp_path).port == 9123


def test_an_unprefixed_variable_is_ignored(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("PORT", "9123")
    assert make(tmp_path).port == 8765


def test_the_other_unprefixed_names_are_ignored_too(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("NETWORK_MODE", "2")
    monkeypatch.setenv("EMBEDDING_MODEL", "some/other")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    settings = make(tmp_path)
    assert settings.network_mode == 1
    assert settings.embedding_model == "BAAI/bge-m3"
    assert settings.log_level == "INFO"


def test_the_variable_names_are_case_insensitive(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("yuanwork_port", "9124")
    assert make(tmp_path).port == 9124


# --------------------------------------------------------------------------
# the precedence
# --------------------------------------------------------------------------


def test_the_constructor_argument_beats_the_environment(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("YUANWORK_PORT", "9123")
    assert make(tmp_path, port=7000).port == 7000


def test_the_environment_beats_the_default(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("YUANWORK_LOG_LEVEL", "DEBUG")
    assert make(tmp_path).log_level == "DEBUG"


def test_the_default_is_used_when_nothing_is_set(tmp_path: Path):
    assert make(tmp_path).port == 8765
    assert make(tmp_path).host == "127.0.0.1"
    assert make(tmp_path).log_level == "INFO"


def test_a_variable_in_the_environment_file_is_read(tmp_path: Path, monkeypatch):
    """The `.env` file in the working directory is the third source."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("YUANWORK_PORT=9199\n", encoding="utf-8")
    assert YuanWorkSettings(data_dir=tmp_path / "data").port == 9199


def test_the_constructor_argument_beats_the_file(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("YUANWORK_PORT=9199\n", encoding="utf-8")
    assert YuanWorkSettings(data_dir=tmp_path / "data", port=7000).port == 7000


def test_the_environment_beats_the_file(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("YUANWORK_PORT=9199\n", encoding="utf-8")
    monkeypatch.setenv("YUANWORK_PORT", "9123")
    assert YuanWorkSettings(data_dir=tmp_path / "data").port == 9123
