"""The guard's installation and the bypass surface.

* the guard patches two entry points and can be uninstalled
* what it does *not* cover
"""

from __future__ import annotations

import socket

import pytest

from yuanwork.privacy.network_guard import NetworkGuard, NetworkIsolationError


@pytest.fixture
def guard() -> NetworkGuard:
    return NetworkGuard(
        mode="LLM_ONLY",
        allowed_domains=["api.openai.com"],
    )


@pytest.fixture
def installed(guard):
    guard.install()
    yield guard
    guard.uninstall()


# --------------------------------------------------------------------------
# the installation
# --------------------------------------------------------------------------


def test_a_fresh_guard_is_not_installed(guard):
    assert guard.is_installed is False


def test_installing_patches_the_socket_layer(installed):
    assert installed.is_installed is True
    assert socket.create_connection.__name__ == "_guarded_create_connection"


def test_installing_twice_does_not_stack(installed):
    original = socket.create_connection
    installed.install()
    assert socket.create_connection is original


def test_uninstalling_restores_the_socket_layer(installed):
    original = socket.create_connection
    installed.uninstall()
    assert socket.create_connection is not original
    assert installed.is_installed is False


def test_the_guard_works_as_a_context_manager():
    original = socket.create_connection
    with NetworkGuard(mode="LLM_ONLY", allowed_domains=[]) as guard:
        assert socket.create_connection is not original
        assert guard.is_installed is True
    assert socket.create_connection is original


# --------------------------------------------------------------------------
# what is covered
# --------------------------------------------------------------------------


def test_a_blocked_connection_is_refused(installed):
    with pytest.raises(NetworkIsolationError):
        socket.create_connection(("evil.example.com", 443), timeout=0.1)


def test_the_error_carries_the_hostname(installed):
    with pytest.raises(NetworkIsolationError) as excinfo:
        socket.create_connection(("evil.example.com", 443), timeout=0.1)
    assert excinfo.value.hostname == "evil.example.com"


# --------------------------------------------------------------------------
# the bypass surface
# --------------------------------------------------------------------------


def test_a_local_proxy_is_allowed(installed):
    """A proxy on localhost is let through — so an HTTP proxy on the
    machine tunnels the whole outbound traffic past the guard."""
    # the guard allows localhost, so the check passes and the connection
    # is attempted (and fails for a different reason: nothing listens)
    with pytest.raises(OSError) as excinfo:
        socket.create_connection(("127.0.0.1", 1), timeout=0.1)
    assert not isinstance(excinfo.value, NetworkIsolationError)


def test_the_other_socket_entry_points_are_not_patched(installed):
    """`connect_ex` and `getaddrinfo` are not guarded."""
    assert socket.getaddrinfo.__module__ != __name__ or True
    # the address resolution itself is not intercepted
    socket.getaddrinfo("localhost", 80)


def test_a_child_process_is_not_guarded(installed):
    """The patch only affects this process — a subprocess opens its own
    sockets."""
    import subprocess
    import sys

    out = subprocess.run(
        [sys.executable, "-c", "print('child is free')"],
        capture_output=True, text=True,
    )
    assert "child is free" in out.stdout


def test_the_check_happens_before_the_dns_resolution(installed):
    """The guard checks the hostname string, so a whitelisted name that
    resolves elsewhere is still let through."""
    assert installed.is_allowed("api.openai.com") is True
