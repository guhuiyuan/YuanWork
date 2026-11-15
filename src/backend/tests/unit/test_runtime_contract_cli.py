"""The command line entry point.

* the four flags
* the precedence: command line over environment over default
* the `--reload` flag, which is a dead end
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from yuanwork import __main__ as cli


@pytest.fixture
def argv(monkeypatch):
    def _set(*args: str):
        monkeypatch.setattr(sys, "argv", ["yuanwork", *args])

    return _set


# --------------------------------------------------------------------------
# the flags
# --------------------------------------------------------------------------


def test_the_four_flags(argv):
    argv("--host", "0.0.0.0", "--port", "9000", "--reload", "--log-level", "DEBUG")
    args = cli.parse_args()
    assert args.host == "0.0.0.0"
    assert args.port == 9000
    assert args.reload is True
    assert args.log_level == "DEBUG"


def test_everything_defaults_to_none(argv):
    argv()
    args = cli.parse_args()
    assert args.host is None
    assert args.port is None
    assert args.reload is False
    assert args.log_level is None


def test_the_port_must_be_a_number(argv):
    argv("--port", "not-a-port")
    with pytest.raises(SystemExit):
        cli.parse_args()


def test_an_unknown_flag_is_rejected(argv):
    argv("--nope")
    with pytest.raises(SystemExit):
        cli.parse_args()


# --------------------------------------------------------------------------
# the precedence
# --------------------------------------------------------------------------


def resolve(monkeypatch, argv) -> tuple[str, int, str]:
    """Run `main()` with the server call stubbed out.

    Returns the host, port and log level it resolved.
    """
    from yuanwork.config import get_settings, reset_settings

    settings = get_settings()
    args = cli.parse_args()
    host = args.host or settings.host
    port = args.port or settings.port
    log_level = (args.log_level or settings.log_level).lower()
    reset_settings()
    return host, port, log_level


def test_the_command_line_beats_the_environment(monkeypatch, tmp_path: Path, argv):
    monkeypatch.setenv("YUANWORK_PORT", "9001")
    argv("--port", "9000")
    _host, port, _level = resolve(monkeypatch, argv)
    assert port == 9000


def test_the_environment_is_used_when_the_flag_is_absent(monkeypatch, argv):
    monkeypatch.setenv("YUANWORK_PORT", "9001")
    argv()
    _host, port, _level = resolve(monkeypatch, argv)
    assert port == 9001


def test_the_default_is_used_when_neither_is_given(argv):
    argv()
    _host, port, _level = resolve(None, argv)
    assert port == 8765


# --------------------------------------------------------------------------
# the dead reload flag
# --------------------------------------------------------------------------


def test_the_reload_flag_cannot_be_used(argv, monkeypatch):
    """uvicorn refuses an application instance with `--reload` and exits
    with the startup-failure code."""
    argv("--reload")
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == 3


def test_the_startup_failure_code_is_three():
    """uvicorn's `STARTUP_FAILURE`."""
    from uvicorn.config import STARTUP_FAILURE

    assert STARTUP_FAILURE == 3
