"""Tests for the socket-layer network isolation guard (Stage 3)."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from yuanwork.privacy.network_guard import (
    NetworkGuard,
    NetworkIsolationError,
)


# ---- Pure policy tests (no socket patching) ----


def test_is_allowed_exact_match():
    g = NetworkGuard("LLM_ONLY", ["api.openai.com", "api.anthropic.com"])
    assert g.is_allowed("api.openai.com") is True
    assert g.is_allowed("api.anthropic.com") is True


def test_is_allowed_subdomain():
    g = NetworkGuard("LLM_ONLY", ["api.openai.com"])
    # v1.api.openai.com should match because it ends with .api.openai.com
    assert g.is_allowed("v1.api.openai.com") is True


def test_is_allowed_blocks_lookalike():
    g = NetworkGuard("LLM_ONLY", ["api.openai.com"])
    # Right-anchored endswith means these must NOT match
    assert g.is_allowed("api.openai.com.evil.com") is False
    assert g.is_allowed("evil.com") is False
    assert g.is_allowed("fakeapiopenai.com") is False


def test_is_allowed_localhost_in_llm_only():
    g = NetworkGuard("LLM_ONLY", ["api.openai.com"])
    assert g.is_allowed("localhost") is True
    assert g.is_allowed("127.0.0.1") is True
    assert g.is_allowed("::1") is True
    assert g.is_allowed("0.0.0.0") is True


def test_is_allowed_offline_blocks_localhost_too():
    g = NetworkGuard("OFFLINE", ["api.openai.com"])
    assert g.is_allowed("api.openai.com") is False
    assert g.is_allowed("localhost") is False
    assert g.is_allowed("127.0.0.1") is False


def test_is_allowed_allow_all_permits_everything():
    g = NetworkGuard("ALLOW_ALL", [])
    assert g.is_allowed("anything.com") is True
    assert g.is_allowed("127.0.0.1") is True
    assert g.is_allowed("") is False  # but not None / empty


def test_is_allowed_strips_port():
    g = NetworkGuard("LLM_ONLY", ["api.openai.com"])
    assert g.is_allowed("api.openai.com:443") is True


def test_check_raises_with_actionable_message():
    g = NetworkGuard("LLM_ONLY", ["api.openai.com", "api.anthropic.com"])
    with pytest.raises(NetworkIsolationError) as exc_info:
        g.check("evil.example.com")
    err = exc_info.value
    assert err.hostname == "evil.example.com"
    assert "api.openai.com" in str(err)
    assert "LLM_ONLY" in str(err)
    # The message must include the action to take
    assert "allowed_domains" in str(err) or "allow_all" in str(err)


# ---- Socket-layer hook tests ----


@pytest.fixture
def guard_factory():
    """Yield a factory that creates guards with install/uninstall cleanup.

    Each call to the factory returns a fresh guard; cleanup is automatic.
    """
    guards: list[NetworkGuard] = []

    def _make(mode: str = "LLM_ONLY", allowed: list[str] | None = None):
        g = NetworkGuard(mode, allowed or ["api.openai.com", "localhost"])
        guards.append(g)
        return g

    yield _make

    # Cleanup: uninstall all guards created in this test
    for g in guards:
        g.uninstall()
    # Final safety: ensure no guard is still installed
    assert NetworkGuard._installed_guard is None, "guard leaked across test"


def test_install_blocks_non_whitelisted_socket(guard_factory):
    """The patched create_connection raises for non-whitelisted hosts."""
    g = guard_factory()
    assert g.install() is True
    # Attempting to connect to a non-whitelisted host should raise
    with pytest.raises(NetworkIsolationError) as exc:
        socket.create_connection(("evil.example.com", 443), timeout=0.1)
    assert exc.value.hostname == "evil.example.com"


def test_install_blocks_via_socket_connect_method(guard_factory):
    """Direct socket.socket().connect() is also guarded."""
    g = guard_factory()
    g.install()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setblocking(False)
    try:
        with pytest.raises(NetworkIsolationError):
            s.connect(("evil.example.com", 443))
    finally:
        s.close()


# ---- the resolved address ----
#
# The socket layer sees the **address**, not the name: `create_connection`
# checks the hostname, but the kernel-level connect() only gets the
# resolved IP. Without recognising that address, every allowed domain is
# blocked by its own DNS - the provider API becomes unreachable and every
# question is refused with "the model is unavailable".


def _fake_dns(monkeypatch, domain: str, address: str):
    """Resolve one domain to one fixed address; the rest stays real."""
    original = socket.getaddrinfo

    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host == domain:
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port or 443))
            ]
        return original(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


def test_the_resolved_address_of_an_allowed_domain_is_allowed(
    guard_factory, monkeypatch
):
    """The address an allowed domain resolves to is an allowed target."""
    g = guard_factory(allowed=["api.openai.com"])
    _fake_dns(monkeypatch, "api.openai.com", "203.0.113.7")

    assert g._is_allowed_address("203.0.113.7") is True
    assert g._is_allowed_address("198.51.100.9") is False


def test_a_connection_to_an_allowed_domain_gets_past_the_guard(
    guard_factory, monkeypatch
):
    """The connection to an allowed domain must not be refused by the guard.

    203.0.113.0/24 is reserved for documentation (RFC 5737), so the
    connection fails at the network level - but it must fail there and
    not at the guard.
    """
    g = guard_factory(allowed=["api.openai.com"])
    _fake_dns(monkeypatch, "api.openai.com", "203.0.113.7")
    g.install()

    with pytest.raises(OSError) as exc:
        socket.create_connection(("api.openai.com", 443), timeout=1)
    assert not isinstance(exc.value, NetworkIsolationError)


def test_an_address_outside_the_allowed_ones_is_still_blocked(
    guard_factory, monkeypatch
):
    """An IP that no allowed domain resolves to is still refused."""
    g = guard_factory(allowed=["api.openai.com"])
    _fake_dns(monkeypatch, "api.openai.com", "203.0.113.7")
    g.install()

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setblocking(False)
    try:
        with pytest.raises(NetworkIsolationError):
            s.connect(("198.51.100.9", 443))
    finally:
        s.close()


def test_allow_all_install_is_noop(guard_factory):
    """ALLOW_ALL mode doesn't patch the socket (no need to guard)."""
    g = guard_factory(mode="ALLOW_ALL")
    installed = g.install()
    assert installed is False
    # Original create_connection is unchanged
    assert socket.create_connection == g._original_create_connection or \
        not g._installed


def test_install_idempotent(guard_factory):
    """Calling install() twice doesn't double-patch."""
    g = guard_factory()
    first = g.install()
    second = g.install()
    assert first is True
    assert second is False  # already installed


def test_uninstall_restores_socket(guard_factory):
    """After uninstall, the original create_connection is back."""
    g = guard_factory()
    g.install()
    # Block check via a throwaway call that will get DNS error
    # (we just want to confirm the patched function is in place)
    with pytest.raises(NetworkIsolationError):
        socket.create_connection(("evil.example.com", 443), timeout=0.1)
    # Now uninstall
    assert g.uninstall() is True
    # Original is back — call to a bad host now gets a real socket
    # error (gaierror) rather than NetworkIsolationError
    assert g.is_installed is False


def test_context_manager(guard_factory):
    """`with NetworkGuard(...) as g:` installs on enter, uninstalls on exit."""
    g = guard_factory()
    with g as installed_guard:
        assert installed_guard.is_installed is True
        with pytest.raises(NetworkIsolationError):
            socket.create_connection(("evil.example.com", 443), timeout=0.1)
    assert g.is_installed is False


def test_extract_hostname_various_shapes():
    """Extract hostname from the various address shapes libs use."""
    # (host, port) tuple
    assert NetworkGuard.extract_hostname(("api.openai.com", 443)) == "api.openai.com"
    # Just host
    assert NetworkGuard.extract_hostname("api.openai.com") == "api.openai.com"
    # IP
    assert NetworkGuard.extract_hostname(("127.0.0.1", 8000)) == "127.0.0.1"
    # IPv6
    assert NetworkGuard.extract_hostname(("::1", 8000, 0, 0)) == "::1"
    # bytes
    assert NetworkGuard.extract_hostname((b"api.openai.com", 443)) == "api.openai.com"
    # Empty / None
    assert NetworkGuard.extract_hostname(None) is None
    assert NetworkGuard.extract_hostname(()) is None
    assert NetworkGuard.extract_hostname("") is None


# ---- Real httpx integration ----


def test_httpx_blocked_when_guard_active(guard_factory):
    """A real httpx call to a non-whitelisted host surfaces the guard block.

    httpcore wraps socket-level errors in `httpx.ConnectError`, so the
    caller sees a ConnectError rather than the raw
    NetworkIsolationError. The original message ("Network isolation: ...")
    is preserved in the wrapper, which is what we assert.

    `trust_env=False` keeps httpx from picking up the system HTTP proxy
    (the dev environment has a local proxy on 127.0.0.1:7897), which
    would tunnel the real connection through localhost and bypass our
    guard. In production, a user-supplied proxy in `allowed_domains` is
    expected.
    """
    g = guard_factory()
    g.install()
    import httpx
    with pytest.raises(httpx.ConnectError) as exc:
        httpx.get(
            "https://evil.example.com/test",
            timeout=0.5,
            trust_env=False,
        )
    # The error message must surface the blocked hostname and policy
    assert "Network isolation" in str(exc.value)
    assert "evil.example.com" in str(exc.value)
    assert "LLM_ONLY" in str(exc.value)


def test_from_settings_reads_singleton(temp_dir: Path, monkeypatch):
    """from_settings() pulls from the global YuanWorkSettings."""
    from yuanwork.config import (
        NetworkMode,
        YuanWorkSettings,
        get_settings,
        reset_settings,
    )
    # Build a fresh settings with OFFLINE mode
    reset_settings()
    s = YuanWorkSettings(
        data_dir=temp_dir,
        network_mode=NetworkMode.OFFLINE,
        allowed_domains=[],
    )
    monkeypatch.setattr(
        "yuanwork.config._settings", s, raising=False
    )
    g = NetworkGuard.from_settings()
    assert g.mode == "OFFLINE"
    # In OFFLINE, even localhost is blocked
    assert g.is_allowed("api.openai.com") is False
    assert g.is_allowed("127.0.0.1") is False
