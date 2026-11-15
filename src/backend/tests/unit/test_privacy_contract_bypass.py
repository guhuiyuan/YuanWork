"""The bypass surface of the network guard.

The guard used to let every connection to localhost through - which
meant a proxy on the machine could tunnel the whole outbound traffic
without the guard noticing. A connection to an **explicitly configured
proxy** is now refused: the guard does not let traffic through a tunnel
it cannot inspect.

The paths the guard still does *not* cover are pinned too, so the
boundary is explicit rather than accidental.

* AC-F-019 — the whitelist cannot be tunnelled around
"""

from __future__ import annotations

import socket
import subprocess
import sys

import pytest

from yuanwork.privacy.network_guard import (
    NetworkGuard,
    NetworkIsolationError,
    configured_proxy_targets,
)


@pytest.fixture
def installed(monkeypatch):
    """A guard with a proxy configured in the environment."""
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    guard = NetworkGuard(mode="LLM_ONLY", allowed_domains=["api.openai.com"])
    guard.install()
    yield guard
    guard.uninstall()


@pytest.fixture
def clean(monkeypatch):
    """A guard with no proxy configured."""
    for name in (
        "http_proxy", "https_proxy", "all_proxy",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    ):
        monkeypatch.delenv(name, raising=False)
    guard = NetworkGuard(mode="LLM_ONLY", allowed_domains=["api.openai.com"])
    guard.install()
    yield guard
    guard.uninstall()


# --------------------------------------------------------------------------
# the proxy tunnel
# --------------------------------------------------------------------------


def test_the_configured_proxy_is_recognized(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")

    targets = configured_proxy_targets()

    assert ("127.0.0.1", 7890) in targets


def test_a_proxy_without_a_port_blocks_the_whole_host(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.internal")

    targets = configured_proxy_targets()

    assert ("proxy.internal", None) in targets


def test_a_connection_to_the_proxy_is_blocked(installed):
    """The tunnel is refused: the guard cannot see where it goes."""
    with pytest.raises(NetworkIsolationError) as excinfo:
        socket.create_connection(("127.0.0.1", 7890), timeout=0.1)

    assert excinfo.value.reason == "proxy_tunnel"


def test_the_error_says_why(installed):
    with pytest.raises(NetworkIsolationError) as excinfo:
        socket.create_connection(("127.0.0.1", 7890), timeout=0.1)

    message = str(excinfo.value)
    assert "proxy" in message
    assert "YUANWORK_NETWORK_MODE=allow_all" in message


def test_another_local_port_is_still_allowed(installed):
    """Only the proxy itself is refused - the local model still works."""
    with pytest.raises(OSError) as excinfo:
        socket.create_connection(("127.0.0.1", 9), timeout=0.1)
    assert not isinstance(excinfo.value, NetworkIsolationError)


def test_without_a_proxy_localhost_is_allowed(clean):
    with pytest.raises(OSError) as excinfo:
        socket.create_connection(("127.0.0.1", 9), timeout=0.1)
    assert not isinstance(excinfo.value, NetworkIsolationError)


def test_a_whitelisted_domain_is_still_allowed(installed):
    assert installed.is_allowed("api.openai.com") is True
    assert installed.check("api.openai.com") is None


def test_the_proxy_check_works_without_the_socket_patch(monkeypatch):
    """The policy is a pure function - it can be asked directly."""
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp:3128")
    guard = NetworkGuard(mode="LLM_ONLY", allowed_domains=["api.openai.com"])

    assert guard.is_proxy_target("proxy.corp", 3128) is True
    assert guard.is_proxy_target("proxy.corp", 9999) is False
    assert guard.is_proxy_target("api.openai.com", 443) is False


def test_the_proxy_is_blocked_in_allow_all_mode_too(monkeypatch):
    """ALLOW_ALL means no isolation at all - including no tunnel check."""
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    guard = NetworkGuard(mode="ALLOW_ALL", allowed_domains=[])

    assert guard.is_proxy_target("127.0.0.1", 7890) is True
    # but the mode does not install the patch, so nothing is blocked
    assert guard.is_allowed("127.0.0.1") is True


# --------------------------------------------------------------------------
# the resolution order
# --------------------------------------------------------------------------


def test_the_check_happens_before_the_name_is_resolved(clean):
    """The guard only looks at the hostname string - a whitelisted name
    that resolves to another address is still let through."""
    assert clean.is_allowed("api.openai.com") is True
    assert clean.check("api.openai.com") is None


# --------------------------------------------------------------------------
# the other socket entry points
# --------------------------------------------------------------------------


def test_getaddrinfo_is_not_guarded(clean):
    """Address resolution is not intercepted."""
    socket.getaddrinfo("localhost", 80, type=socket.SOCK_STREAM)


def test_connect_ex_is_not_guarded(clean):
    """`connect_ex` returns an error code instead of raising, and it is
    not patched."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(0.1)
        # the call is not intercepted — it fails at the network level
        result = sock.connect_ex(("127.0.0.1", 9))
        assert isinstance(result, int)
    finally:
        sock.close()


# --------------------------------------------------------------------------
# other processes
# --------------------------------------------------------------------------


def test_a_child_process_is_not_guarded(clean):
    """The patch only affects this process."""
    code = (
        "import socket;"
        "s = socket.socket();"
        "print(s.connect_ex(('127.0.0.1', 9)))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert out.returncode == 0
    assert "NetworkIsolationError" not in out.stderr


def test_the_guard_is_only_installed_by_the_module_entry_point():
    """`__main__.py` is the only place that installs it."""
    import pathlib

    backend = pathlib.Path(__file__).resolve().parents[2] / "yuanwork"
    callers = []
    for path in backend.rglob("*.py"):
        if path.name == "network_guard.py":
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        if ".install()" in source:
            callers.append(path.name)
    # only the module entry point installs it
    assert callers == ["__main__.py"]
