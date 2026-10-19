"""Network guard — enforce network access policy at the socket layer.

Why this exists (Stage 3):
  The user runs a personal KB assistant. Documents and embeddings must stay
  on disk; only the LLM provider API should ever be called out to. A
  malicious or buggy library, or a third-party tool re-purposed by an LLM,
  could exfiltrate data over the network. We block that at the lowest
  possible layer: `socket.create_connection` and `socket.socket.connect`.

Modes (from `YuanWorkSettings.network_mode`):
  - `ALLOW_ALL` (0)  — no patch, all connections permitted (escape hatch)
  - `LLM_ONLY` (1)   — only allow whitelisted provider domains + localhost.
                       Default. Recommended for personal KB.
  - `OFFLINE`    (2)  — block everything, including localhost. For
                       air-gapped use with local models only.

Localhost is allowed in `LLM_ONLY` because Ollama / llama.cpp / vLLM
typically bind to 127.0.0.1. In `OFFLINE` we block even that, because the
user has explicitly chosen to run with no network of any kind.

The guard is **opt-in at the install level**: it patches the socket module
only after `install()` is called. Tests must `uninstall()` in a finally
block (use the `installed_guard` fixture) to avoid contaminating other
tests. The `ALLOW_ALL` mode is a no-op — we don't even install.

The error UX (per user policy) is loud and actionable:
    NetworkIsolationError: outbound to 'evil.example.com' is blocked.
    Network mode is LLM_ONLY. Allowed domains:
    ['api.openai.com', 'api.anthropic.com', 'api.deepseek.com',
     'api.minimaxi.com', 'localhost']
    To allow this domain, add it to settings.allowed_domains or to
    configs/privacy.yaml. To disable network isolation, set
    YUANWORK_NETWORK_MODE=allow_all.

The check is `endswith("." + d)` for subdomain match plus exact match.
`evil.com/api.openai.com` does NOT match because `endswith` is
right-anchored; `v1.api.openai.com` matches `api.openai.com`.
"""

from __future__ import annotations

import logging
import socket
import threading
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# Hostnames that are always treated as local, regardless of mode
# (only when mode != OFFLINE).
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


class NetworkIsolationError(PermissionError):
    """Raised when a network connection is blocked by NetworkGuard.

    Subclass of `PermissionError` so generic permission-handling code
    catches it, but with extra fields for the UX.
    """

    def __init__(self, hostname: str, allowed_domains: list[str], mode: str):
        self.hostname = hostname
        self.allowed_domains = list(allowed_domains)
        self.mode = mode
        msg = (
            f"Network isolation: outbound to {hostname!r} is blocked.\n"
            f"  Network mode: {mode}\n"
            f"  Allowed domains: {sorted(self.allowed_domains)}\n"
            f"  To allow this domain, add it to settings.allowed_domains "
            f"(or configs/privacy.yaml), or set\n"
            f"  YUANWORK_NETWORK_MODE=allow_all to disable isolation."
        )
        super().__init__(msg)


class NetworkGuard:
    """Enforce network access policy via socket-layer patching.

    Lifecycle:
        guard = NetworkGuard.from_settings()
        guard.install()       # patch socket
        ...                   # do network work
        guard.uninstall()     # restore socket

    Thread-safe: install/uninstall use a class-level lock so concurrent
    code paths can't see a half-installed state.
    """

    _install_lock = threading.Lock()
    _installed_guard: "NetworkGuard | None" = None

    def __init__(
        self,
        mode: str,
        allowed_domains: list[str],
    ):
        self.mode = mode.upper() if isinstance(mode, str) else str(mode)
        # Normalize domain list: lowercase, no port, no scheme
        self.allowed_domains = [d.lower().strip() for d in allowed_domains if d.strip()]
        # Storage for original socket functions (for uninstall)
        self._original_create_connection: Any = None
        self._original_socket_connect: Any = None
        self._installed = False

    # ----- Factories -----

    @classmethod
    def from_settings(cls) -> "NetworkGuard":
        """Build a guard from YuanWorkSettings."""
        from yuanwork.config import NetworkMode, get_settings

        settings = get_settings()
        mode_name = (
            settings.network_mode.name
            if hasattr(settings.network_mode, "name")
            else str(settings.network_mode)
        )
        return cls(
            mode=mode_name,
            allowed_domains=list(settings.allowed_domains),
        )

    # ----- Policy logic -----

    def is_allowed(self, hostname: str) -> bool:
        """Return True if the given hostname is allowed by current policy.

        Pure function; no I/O. Use this to make policy decisions in code
        that has its own connection layer (e.g. an HTTP client wrapper).
        """
        if not hostname:
            return False
        h = hostname.lower().strip()
        # Strip IPv6 brackets if present
        if h.startswith("[") and "]" in h:
            h = h[1 : h.index("]")]
        # Strip port — but only if this isn't a bare IPv6 literal. An IPv6
        # address has multiple colons (e.g. "::1" has 2), while a host:port
        # pair has exactly one.
        if h.count(":") <= 1:
            h = h.split(":")[0]

        if self.mode == "ALLOW_ALL":
            return True
        if self.mode == "OFFLINE":
            # OFFLINE blocks everything, even localhost
            return False
        # LLM_ONLY — allow local + whitelisted (incl. subdomain match)
        if h in _LOCAL_HOSTS:
            return True
        for d in self.allowed_domains:
            if h == d or h.endswith("." + d):
                return True
        return False

    def check(self, hostname: str) -> None:
        """Raise NetworkIsolationError if the hostname is not allowed."""
        if not self.is_allowed(hostname):
            raise NetworkIsolationError(
                hostname=hostname,
                allowed_domains=self.allowed_domains,
                mode=self.mode,
            )

    @staticmethod
    def extract_hostname(address: Any) -> str | None:
        """Extract hostname from any of the address shapes socket APIs use.

        `socket.create_connection((host, port), ...)`
        `socket.create_connection(host, port, ...)`
        `socket.socket().connect((host, port))`
        `socket.socket().connect(host)`  (some libs)
        """
        if not address:
            return None
        # The address is most often a (host, port) tuple
        if isinstance(address, (tuple, list)):
            if not address:
                return None
            host = address[0]
        else:
            host = address
        if isinstance(host, bytes):
            try:
                host = host.decode("ascii", errors="replace")
            except Exception:
                return None
        if not host:
            return None
        return str(host)

    # ----- Install / uninstall -----

    @property
    def is_installed(self) -> bool:
        return self._installed

    def install(self) -> bool:
        """Patch `socket.create_connection` and `socket.socket.connect`.

        Returns True if a patch was actually applied, False if the guard
        is in ALLOW_ALL mode (no-op) or already installed.

        Idempotent — calling install() twice does not stack patches.
        """
        with NetworkGuard._install_lock:
            if self.mode == "ALLOW_ALL":
                logger.info(
                    "NetworkGuard: ALLOW_ALL mode — socket patch skipped"
                )
                return False
            if self._installed:
                return False
            if NetworkGuard._installed_guard is not None:
                # Another guard is already installed. Refuse to stack.
                logger.warning(
                    "NetworkGuard: another guard is already installed; "
                    "skipping install of this one"
                )
                return False

            # Patch socket.create_connection (used by urllib, httpx, etc.)
            self._original_create_connection = socket.create_connection
            guard = self  # capture for closure

            def _guarded_create_connection(address, *args, **kwargs):  # type: ignore[no-untyped-def]
                host = NetworkGuard.extract_hostname(address)
                if host is not None:
                    guard.check(host)
                return guard._original_create_connection(address, *args, **kwargs)

            socket.create_connection = _guarded_create_connection  # type: ignore[assignment]

            # Patch socket.socket.connect (used by lower-level code)
            self._original_socket_connect = socket.socket.connect
            _orig_connect = self._original_socket_connect

            def _guarded_connect(self_sock, address, *args, **kwargs):  # type: ignore[no-untyped-def]
                host = NetworkGuard.extract_hostname(address)
                if host is not None:
                    guard.check(host)
                return _orig_connect(self_sock, address, *args, **kwargs)

            socket.socket.connect = _guarded_connect  # type: ignore[assignment]

            self._installed = True
            NetworkGuard._installed_guard = self
            logger.info(
                f"NetworkGuard installed: mode={self.mode}, "
                f"allowed={self.allowed_domains}"
            )
            return True

    def uninstall(self) -> bool:
        """Restore the original socket functions. Returns True if a patch was removed.

        Safe to call when not installed.
        """
        with NetworkGuard._install_lock:
            if not self._installed:
                return False
            if self._original_create_connection is not None:
                socket.create_connection = self._original_create_connection  # type: ignore[assignment]
                self._original_create_connection = None
            if self._original_socket_connect is not None:
                socket.socket.connect = self._original_socket_connect  # type: ignore[assignment]
                self._original_socket_connect = None
            self._installed = False
            if NetworkGuard._installed_guard is self:
                NetworkGuard._installed_guard = None
            logger.info("NetworkGuard uninstalled")
            return True

    def __enter__(self) -> "NetworkGuard":
        self.install()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.uninstall()


# Re-export the URL-domain helper from config, for symmetry. Tests and
# external code can use either is_domain_allowed(url) or
# NetworkGuard.from_settings().is_allowed(hostname).
def is_domain_allowed(url: str) -> bool:
    """Check if a URL's domain is allowed by the current policy."""
    from yuanwork.config import get_settings

    settings = get_settings()
    if settings.network_mode.value == 0:  # ALLOW_ALL
        return True
    domain = urlparse(url).netloc.lower()
    domain = domain.split(":")[0]
    guard = NetworkGuard(
        mode=settings.network_mode.name,
        allowed_domains=list(settings.allowed_domains),
    )
    return guard.is_allowed(domain)
