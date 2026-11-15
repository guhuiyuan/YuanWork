"""The configuration singleton and its directory side effect."""

from __future__ import annotations

from pathlib import Path

import pytest

from yuanwork.config import YuanWorkSettings, get_settings, reset_settings


@pytest.fixture(autouse=True)
def _clean_singleton():
    reset_settings()
    yield
    reset_settings()


def test_the_settings_are_a_singleton(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("YUANWORK_DATA_DIR", str(tmp_path / "data"))
    first = get_settings()
    second = get_settings()
    assert first is second


def test_the_singleton_can_be_reset(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("YUANWORK_DATA_DIR", str(tmp_path / "data"))
    first = get_settings()
    reset_settings()
    assert get_settings() is not first


def test_constructing_the_settings_creates_the_data_directory(tmp_path: Path):
    data_dir = tmp_path / "fresh"
    assert not data_dir.exists()
    YuanWorkSettings(data_dir=data_dir)
    assert data_dir.is_dir()


def test_constructing_the_settings_creates_the_logs_directory(tmp_path: Path):
    data_dir = tmp_path / "fresh"
    YuanWorkSettings(data_dir=data_dir)
    assert (data_dir / "logs").is_dir()


def test_the_directory_creation_happens_on_every_construction(tmp_path: Path):
    """The side effect is in `__init__`, so it happens every time."""
    data_dir = tmp_path / "again"
    YuanWorkSettings(data_dir=data_dir)
    (data_dir / "logs" / "audit.jsonl").write_text("x", encoding="utf-8")
    YuanWorkSettings(data_dir=data_dir)
    assert (data_dir / "logs" / "audit.jsonl").exists()


def test_the_data_directory_is_created_even_when_it_is_missing(tmp_path: Path):
    nested = tmp_path / "a" / "b" / "c"
    YuanWorkSettings(data_dir=nested)
    assert nested.is_dir()


def test_the_default_data_directory_is_under_the_home(tmp_path: Path, monkeypatch):
    """The default is `~/.yuanwork`."""
    monkeypatch.delenv("YUANWORK_DATA_DIR", raising=False)
    # the working directory's .env would otherwise supply a value
    monkeypatch.chdir(tmp_path)
    settings = YuanWorkSettings()
    assert settings.data_dir.name == ".yuanwork"
