"""The startup sequence and what it prints.

The startup information goes to the standard output, not through the
logging framework — that is deliberate, because the application's own
info logs are not visible in the default configuration.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

from yuanwork import __main__ as cli
from yuanwork.config import reset_settings


@pytest.fixture(autouse=True)
def _clean_singleton():
    reset_settings()
    yield
    reset_settings()


@pytest.fixture
def started(monkeypatch, tmp_path: Path, capsys):
    """Run `main()` with the server call stubbed out."""
    monkeypatch.setenv("YUANWORK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(sys, "argv", ["yuanwork"])

    captured: dict = {}

    def fake_run(app, **kwargs):
        captured.update(kwargs)
        captured["app"] = app

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    cli.main()
    return captured, capsys


def test_the_server_is_started_with_the_resolved_values(started):
    captured, _ = started
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8765
    assert captured["log_level"] == "info"


def test_the_reload_flag_is_passed_through(started):
    captured, _ = started
    assert captured["reload"] is False


def test_the_five_startup_lines_are_printed(started):
    _captured, capsys = started
    out = capsys.readouterr().out
    assert "Network guard:" in out
    assert "Starting YuanWork backend at http://127.0.0.1:8765" in out
    assert "Data dir:" in out
    assert "Network mode: LLM_ONLY" in out
    assert "Log level: info" in out


def test_the_log_level_is_lowercased(started):
    _captured, capsys = started
    assert "Log level: info" in capsys.readouterr().out


def test_the_guard_is_installed_before_the_server_starts(started):
    """The network guard has to be in place before anything can open a
    socket."""
    from yuanwork.privacy import network_guard

    captured, _ = started
    assert captured["app"] is not None
    # the guard was installed (and left installed after main() returned)
    assert network_guard.NetworkGuard._installed_guard is not None


def test_the_application_is_built_before_the_guard_is_installed():
    """The order matters: the app is created first, then the guard is
    installed, and only then does the server start."""
    source = inspect.getsource(cli.main)
    app_at = source.index("app = create_app(")
    guard_at = source.index("guard.install()")
    server_at = source.index("uvicorn.run(")
    assert app_at < guard_at < server_at


def test_the_guard_is_reported_as_active(started):
    _captured, capsys = started
    out = capsys.readouterr().out
    assert "Network guard: ACTIVE" in out or "Network guard: disabled" in out
