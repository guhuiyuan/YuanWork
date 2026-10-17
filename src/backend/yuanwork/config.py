"""Configuration management for YuanWork.

Loads configuration from:
1. Environment variables (.env file)
2. YAML configuration files in configs/
3. Command-line arguments (highest priority)

Follows 12-factor app methodology.
"""

from __future__ import annotations

import os
from enum import IntEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class NetworkMode(IntEnum):
    """Network isolation mode for privacy."""

    ALLOW_ALL = 0  # Allow all network access
    LLM_ONLY = 1  # Only allow LLM provider APIs
    OFFLINE = 2  # Offline only (local models)


class YuanWorkSettings(BaseSettings):
    """Main application settings."""

    model_config = SettingsConfigDict(
        env_prefix="YUANWORK_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ===== Paths =====
    data_dir: Path = Field(default=Path("~/.yuanwork").expanduser())
    config_dir: Path = Field(default=Path("configs"))
    log_level: str = Field(default="INFO")

    # ===== Server =====
    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8765)
    workers: int = Field(default=1)  # Single worker for now (bge-m3 in-process)

    # ===== Privacy =====
    network_mode: NetworkMode = Field(default=NetworkMode.LLM_ONLY)
    allowed_domains: list[str] = Field(
        default_factory=lambda: [
            "api.openai.com",
            "api.anthropic.com",
            "api.deepseek.com",
            "api.minimaxi.com",
        ]
    )

    # ===== LLM Budget =====
    per_session_max_tokens: int = Field(default=200_000)
    per_query_max_tokens: int = Field(default=8_000)
    monthly_cap_usd: float = Field(default=30.0)

    # ===== Embedding =====
    embedding_model: str = Field(default="BAAI/bge-m3")
    embedding_device: str = Field(default="cpu")  # cpu / cuda
    embedding_batch_size: int = Field(default=8)

    # ===== Reranker =====
    reranker_model: str = Field(default="BAAI/bge-reranker-v2-m3")
    use_reranker: bool = Field(default=True)

    # ===== Retrieval =====
    top_k_initial: int = Field(default=50)  # Initial vector recall
    top_k_rerank: int = Field(default=10)  # After reranking
    confidence_threshold_high: float = Field(default=0.7)
    confidence_threshold_low: float = Field(default=0.4)

    # ===== Memory / Wiki =====
    enable_episodic_memory: bool = Field(default=True)
    enable_wiki_auto_compile: bool = Field(default=True)
    reflection_trigger_turns: int = Field(default=10)
    reflection_trigger_idle_minutes: int = Field(default=30)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # Ensure data dir exists
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "logs").mkdir(exist_ok=True)


# Singleton instance (lazy-loaded)
_settings: YuanWorkSettings | None = None


def get_settings() -> YuanWorkSettings:
    """Get or create the singleton settings instance."""
    global _settings
    if _settings is None:
        _settings = YuanWorkSettings()
    return _settings


def load_yaml_config(name: str) -> dict[str, Any]:
    """Load a YAML configuration file from configs/ directory.

    Args:
        name: Config file name without extension (e.g. "models", "retrieval")

    Returns:
        Parsed YAML content as dict. Empty dict if file not found.
    """
    settings = get_settings()
    config_path = settings.config_dir / f"{name}.yaml"

    if not config_path.exists():
        return {}

    with config_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def reset_settings() -> None:
    """Reset singleton (mainly for tests)."""
    global _settings
    _settings = None


# Convenience accessors
def is_offline_mode() -> bool:
    """Check if running in offline-only mode."""
    return get_settings().network_mode == NetworkMode.OFFLINE


def is_domain_allowed(url: str) -> bool:
    """Check if a URL's domain is in the allow-list."""
    from urllib.parse import urlparse

    settings = get_settings()

    if settings.network_mode == NetworkMode.ALLOW_ALL:
        return True

    domain = urlparse(url).netloc.lower()
    # Strip port if present
    domain = domain.split(":")[0]

    allowed = {d.lower() for d in settings.allowed_domains}
    return domain in allowed or any(domain.endswith("." + d) for d in allowed)
