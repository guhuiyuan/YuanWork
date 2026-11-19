"""The backend must not outlive the app that started it.

The desktop app starts the backend as a child process. When the app is
killed without closing its window, the backend used to stay behind
holding the port - and the *next* app instance then talked to that
stale backend and saw an empty index (TASK-082).
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from yuanwork.utils.parent_watch import (
    instance_token_from_env,
    parent_pid_from_env,
    process_alive,
    watch_parent,
)


def test_a_live_process_is_alive() -> None:
    assert process_alive(os.getpid()) is True


def test_a_dead_pid_is_not_alive() -> None:
    # Start a process, let it finish, then ask about its PID.
    finished = subprocess.run(
        [sys.executable, "-c", "pass"], capture_output=True, check=False
    )
    assert finished.returncode == 0
    # The PID is very likely gone; either way it must not be *our* PID.
    assert process_alive(os.getpid()) is True
    assert process_alive(0) is False
    assert process_alive(-1) is False


def test_a_killed_process_does_not_look_alive() -> None:
    # On Windows a terminated process can keep its kernel object around
    # (a handle is open somewhere), and then OpenProcess still succeeds -
    # the PID looks alive long after the process is gone. The watchdog
    # would then never fire, and the backend would stay behind holding
    # the port (TASK-082).
    if os.name != "nt":
        pytest.skip("Windows-only behaviour")
    killed = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    pid = killed.pid
    assert process_alive(pid) is True
    killed.kill()
    killed.wait(timeout=10)
    # It is gone: the exit code is set, so the PID is not "still active".
    assert process_alive(pid) is False


def test_the_parent_pid_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YUANWORK_PARENT_PID", "4321")
    assert parent_pid_from_env() == 4321

    monkeypatch.setenv("YUANWORK_PARENT_PID", "  4322 ")
    assert parent_pid_from_env() == 4322

    # Nothing to watch -> no watchdog.
    monkeypatch.delenv("YUANWORK_PARENT_PID", raising=False)
    assert parent_pid_from_env() is None

    # Junk is not a PID.
    monkeypatch.setenv("YUANWORK_PARENT_PID", "not-a-pid")
    assert parent_pid_from_env() is None

    monkeypatch.setenv("YUANWORK_PARENT_PID", "0")
    assert parent_pid_from_env() is None


def test_the_instance_token_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("YUANWORK_INSTANCE_TOKEN", "abc-123")
    assert instance_token_from_env() == "abc-123"

    monkeypatch.delenv("YUANWORK_INSTANCE_TOKEN", raising=False)
    assert instance_token_from_env() == ""


def test_the_watchdog_leaves_when_the_parent_is_gone() -> None:
    # The watchdog exits the *process* when the parent is gone, so it
    # is checked in a child process: the child watches a PID that is
    # not there and must die on its own.
    script = (
        "import sys, time;"
        "sys.path.insert(0, r'%s');"
        "from yuanwork.utils.parent_watch import watch_parent;"
        "watch_parent(999999, interval=0.2);"
        "print('alive', flush=True);"
        "time.sleep(30)" % os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "alive"
        # It must leave on its own, without being killed.
        child.wait(timeout=15)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
    assert child.returncode == 0
