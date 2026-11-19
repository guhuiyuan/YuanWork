"""The backend must not outlive the application that started it.

The desktop app starts the backend as a child process and kills it when
its window closes.  But an app that is killed - by a crash, by the task
manager, by ``taskkill`` - never gets to close its window, and the
backend keeps running: it still holds the port, still has the database
open, and the *next* app instance cannot bind the port.

That failure is quiet and nasty: the next app instance starts, its own
backend cannot bind, and the readiness probe is answered by the orphan -
so the app happily talks to a stale backend with a stale data
directory.  The user sees an app that works and an index that is empty.

So the backend watches its parent and leaves when the parent is gone.

The parent PID arrives in the environment (``YUANWORK_PARENT_PID``);
the instance token (``YUANWORK_INSTANCE_TOKEN``) lets the app check
that the server answering on the port is *its* server.
"""

from __future__ import annotations

import os
import threading
import time

#: How often the backend checks whether its parent is still alive.
WATCH_INTERVAL_SECONDS = 5.0


def process_alive(pid: int) -> bool:
    """Whether a process with this PID is still running.

    Windows has no ``kill(pid, 0)``, and the obvious check is not
    enough: ``OpenProcess`` *succeeds* for a process that has been
    terminated but whose kernel object is still around (a handle is
    open somewhere), so the PID looks alive long after the process is
    gone. The exit code is the honest answer - a running process
    reports STILL_ACTIVE, a terminated one reports its exit code.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # PROCESS_QUERY_LIMITED_INFORMATION is enough to ask "is it there".
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                # We cannot tell - assume it is alive rather than
                # killing a backend that is still working.
                return True
            # STILL_ACTIVE (259) means the process has not exited yet.
            return exit_code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # It is there - we are simply not allowed to signal it.
        return True
    return True


def parent_pid_from_env() -> int | None:
    """The PID of the app that started us, if it told us."""
    raw = os.environ.get("YUANWORK_PARENT_PID", "").strip()
    if not raw:
        return None
    try:
        pid = int(raw)
    except ValueError:
        return None
    return pid if pid > 0 else None


def instance_token_from_env() -> str:
    """The token the app gave us, if any."""
    return os.environ.get("YUANWORK_INSTANCE_TOKEN", "").strip()


def watch_parent(parent_pid: int, interval: float = WATCH_INTERVAL_SECONDS) -> threading.Thread:
    """Leave the process when the parent is gone.

    The check runs in a daemon thread: it sleeps, asks whether the
    parent is still there, and exits the process when it is not.  The
    exit is abrupt on purpose - there is no one left to talk to, and
    everything that was committed is already durable.
    """
    def loop() -> None:
        while True:
            time.sleep(interval)
            if not process_alive(parent_pid):
                os._exit(0)

    thread = threading.Thread(target=loop, daemon=True, name="parent-watch")
    thread.start()
    return thread
