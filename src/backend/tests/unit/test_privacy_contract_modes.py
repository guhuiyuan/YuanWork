"""The network guard: the three modes and the matching rules.

* what each mode allows
* the exact-match / right-anchored suffix rule
* the IPv6 and port handling
"""

from __future__ import annotations

import pytest

from yuanwork.config import NetworkMode
from yuanwork.privacy.network_guard import (
    _LOCAL_HOSTS,
    NetworkGuard,
    NetworkIsolationError,
)


def make_guard(mode: str = "LLM_ONLY", domains=None) -> NetworkGuard:
    """The guard takes the mode *name* — that is what `from_settings`
    passes (the enum's `.name`)."""
    return NetworkGuard(
        mode=mode,
        allowed_domains=domains if domains is not None else [
            "api.openai.com", "api.anthropic.com",
            "api.deepseek.com", "api.minimaxi.com",
        ],
    )


# --------------------------------------------------------------------------
# the modes
# --------------------------------------------------------------------------


def test_the_default_mode_is_llm_only():
    assert NetworkMode.LLM_ONLY == 1


def test_llm_only_allows_the_whitelisted_domains():
    guard = make_guard()
    assert guard.is_allowed("api.openai.com") is True
    assert guard.is_allowed("api.minimaxi.com") is True


def test_llm_only_allows_the_subdomains():
    guard = make_guard()
    assert guard.is_allowed("v1.api.openai.com") is True


def test_llm_only_blocks_everything_else():
    guard = make_guard()
    assert guard.is_allowed("evil.com") is False
    assert guard.is_allowed("api.openai.com.evil.com") is False


def test_llm_only_allows_the_local_addresses():
    guard = make_guard()
    for host in _LOCAL_HOSTS:
        assert guard.is_allowed(host) is True, host


def test_the_local_addresses_are_the_four_expected_ones():
    assert set(_LOCAL_HOSTS) == {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def test_offline_blocks_everything_including_localhost():
    guard = make_guard("OFFLINE")
    assert guard.is_allowed("api.openai.com") is False
    assert guard.is_allowed("localhost") is False
    assert guard.is_allowed("127.0.0.1") is False


def test_allow_all_permits_everything():
    guard = make_guard("ALLOW_ALL")
    assert guard.is_allowed("evil.com") is True
    assert guard.is_allowed("api.openai.com") is True


def test_an_empty_hostname_is_blocked():
    guard = make_guard()
    assert guard.is_allowed("") is False


# --------------------------------------------------------------------------
# the matching rule
# --------------------------------------------------------------------------


def test_the_match_is_exact_or_a_right_anchored_suffix():
    guard = make_guard(domains=["example.com"])
    assert guard.is_allowed("example.com") is True
    assert guard.is_allowed("api.example.com") is True
    assert guard.is_allowed("notexample.com") is False
    assert guard.is_allowed("example.com.evil.com") is False


def test_the_match_is_case_insensitive():
    guard = make_guard(domains=["example.com"])
    assert guard.is_allowed("API.EXAMPLE.COM") is True


def test_a_port_is_stripped():
    guard = make_guard(domains=["example.com"])
    assert guard.is_allowed("example.com:443") is True


def test_an_ipv6_literal_is_kept():
    """A bare IPv6 address has more than one colon, so the port is not
    stripped."""
    guard = make_guard(domains=["example.com"])
    assert guard.is_allowed("::1") is True  # a local address
    assert guard.is_allowed("2001:db8::1") is False


def test_an_ipv6_literal_in_brackets():
    guard = make_guard(domains=["example.com"])
    assert guard.is_allowed("[2001:db8::1]") is False


# --------------------------------------------------------------------------
# the exception
# --------------------------------------------------------------------------


def test_the_error_is_a_permission_error():
    assert issubclass(NetworkIsolationError, PermissionError)


def test_the_check_raises_for_a_blocked_host():
    guard = make_guard()
    with pytest.raises(NetworkIsolationError) as excinfo:
        guard.check("evil.com")
    assert excinfo.value.hostname == "evil.com"


def test_the_error_message_carries_the_context():
    guard = make_guard()
    with pytest.raises(NetworkIsolationError) as excinfo:
        guard.check("evil.com")
    message = str(excinfo.value)
    assert "evil.com" in message
    assert "LLM_ONLY" in message
    assert "api.openai.com" in message


def test_the_error_message_points_at_the_three_ways_out():
    guard = make_guard()
    with pytest.raises(NetworkIsolationError) as excinfo:
        guard.check("evil.com")
    message = str(excinfo.value)
    assert "allowed_domains" in message
    assert "privacy.yaml" in message
    assert "allow_all" in message


def test_the_check_passes_for_an_allowed_host():
    guard = make_guard()
    assert guard.check("api.openai.com") is None


# --------------------------------------------------------------------------
# the hostname extraction
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address,expected",
    [
        (("api.openai.com", 443), "api.openai.com"),
        (("api.openai.com", 443, 0, 0), "api.openai.com"),
        (("127.0.0.1", 80), "127.0.0.1"),
    ],
)
def test_the_hostname_is_extracted(address, expected):
    assert NetworkGuard.extract_hostname(address) == expected


def test_a_bare_ipv6_address_is_extracted():
    assert NetworkGuard.extract_hostname(("::1", 80, 0, 0)) == "::1"


def test_an_empty_address_yields_none():
    """A value with no host skips the check entirely."""
    assert NetworkGuard.extract_hostname(None) is None
    assert NetworkGuard.extract_hostname(()) is None


def test_undecodable_bytes_are_replaced_not_rejected():
    """`bytes.decode(errors="replace")` never fails, so a garbage host
    comes through as replacement characters rather than as None."""
    assert NetworkGuard.extract_hostname((b"\xff\xfe", 80)) is not None
