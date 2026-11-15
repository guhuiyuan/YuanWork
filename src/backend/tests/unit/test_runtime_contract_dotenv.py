"""The two `.env` lookup mechanisms.

One reads the file into the settings (working directory only); the
other walks up from the *package* location but only writes into the
process environment — and it runs after the settings were built.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yuanwork.config import YuanWorkSettings, reset_settings


@pytest.fixture(autouse=True)
def _clean_singleton():
    reset_settings()
    yield
    reset_settings()


def test_the_settings_only_look_in_the_working_directory(
    tmp_path: Path, monkeypatch
):
    """`env_file` is `.env` with the default depth of 0 — so a file in a
    parent directory is not found."""
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    (tmp_path / ".env").write_text("YUANWORK_PORT=9199\n", encoding="utf-8")

    monkeypatch.chdir(nested)
    assert YuanWorkSettings(data_dir=tmp_path / "data").port == 8765


def test_the_settings_find_the_file_in_the_working_directory(
    tmp_path: Path, monkeypatch
):
    (tmp_path / ".env").write_text("YUANWORK_PORT=9199\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert YuanWorkSettings(data_dir=tmp_path / "data").port == 9199


def test_the_application_loader_reads_the_file_in_the_working_directory(
    tmp_path: Path, monkeypatch
):
    """The first candidate is always the working directory."""
    from yuanwork.api.app import _load_dotenv_into_environ

    (tmp_path / ".env").write_text("YUANWORK_FROM_FILE=hello\n", encoding="utf-8")
    monkeypatch.delenv("YUANWORK_FROM_FILE", raising=False)
    monkeypatch.chdir(tmp_path)

    _load_dotenv_into_environ()
    import os

    assert os.environ.get("YUANWORK_FROM_FILE") == "hello"


def test_the_other_candidates_come_from_the_package_root():
    """The remaining candidates are the *package's* ancestors, not the
    working directory's — that is what lets the desktop shell find the
    `.env` at the project root."""
    import inspect

    from yuanwork.api import app as app_module

    source = inspect.getsource(app_module._dotenv_candidates)
    assert "package_root" in source
    assert "package_root.parents[:6]" in source


def test_the_candidates_start_with_the_working_directory(tmp_path, monkeypatch):
    from yuanwork.api.app import _dotenv_candidates

    monkeypatch.chdir(tmp_path)
    candidates = _dotenv_candidates()
    assert candidates[0] == tmp_path / ".env"
    assert len(candidates) > 1


def test_the_application_loader_does_not_overwrite_the_environment(
    tmp_path: Path, monkeypatch
):
    from yuanwork.api.app import _load_dotenv_into_environ

    (tmp_path / ".env").write_text("YUANWORK_KEEP=from_file\n", encoding="utf-8")
    monkeypatch.setenv("YUANWORK_KEEP", "from_env")
    monkeypatch.chdir(tmp_path)

    _load_dotenv_into_environ()
    import os

    assert os.environ.get("YUANWORK_KEEP") == "from_env"


def test_the_loader_cannot_change_an_already_built_settings(
    tmp_path: Path, monkeypatch
):
    """The loader runs *after* the settings were built, so a `YUANWORK_`
    variable it puts into the environment is too late for the settings.
    It only helps the vendors' own keys, which are read later."""
    from yuanwork.api.app import _load_dotenv_into_environ

    monkeypatch.delenv("YUANWORK_FROM_FILE", raising=False)
    (tmp_path / ".env").write_text("YUANWORK_FROM_FILE=hello\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    settings = YuanWorkSettings(data_dir=tmp_path / "data")
    _load_dotenv_into_environ()
    import os

    assert os.environ.get("YUANWORK_FROM_FILE") == "hello"
    # ... but the settings never saw it
    assert not hasattr(settings, "from_file")


def test_the_loader_is_silent_when_the_file_is_missing(tmp_path: Path, monkeypatch):
    from yuanwork.api.app import _load_dotenv_into_environ

    monkeypatch.chdir(tmp_path)
    _load_dotenv_into_environ()  # must not raise
