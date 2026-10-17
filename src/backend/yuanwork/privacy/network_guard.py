"""Network guard - enforces network access policies based on privacy settings."""

from __future__ import annotations

import logging

from yuanwork.config import NetworkMode, YuanWorkSettings, is_domain_allowed

logger = logging.getLogger(__name__)


class NetworkGuard:
    """Enforce network access policies."""

    def __init__(self, settings: YuanWorkSettings):
        self.settings = settings

    def check_url(self, url: str) -> bool:
        """Check if a URL is allowed by current policy."""
        if self.settings.network_mode == NetworkMode.ALLOW_ALL:
            return True

        if self.settings.network_mode == NetworkMode.OFFLINE:
            logger.warning(f"Offline mode: blocking {url}")
            return False

        # LLM_ONLY mode
        allowed = is_domain_allowed(url)
        if not allowed:
            logger.warning(f"Domain not in allow-list: {url}")
        return allowed

    def require_network(self) -> None:
        """Raise an error if network is not allowed."""
        if self.settings.network_mode == NetworkMode.OFFLINE:
            raise RuntimeError(
                "Network access disabled (offline mode). "
                "Switch to a local model or change network mode."
            )
