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
    # Default config_dir: walk up from the package to find the bundled
    # configs/models.yaml.  Layout is src/backend/yuanwork/*.py and
    # src/backend/configs/*.yaml, so configs/ is the package's parent.
    config_dir: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parent.parent / "configs"
    )
    log_level: str = Field(default="INFO")

    # ===== Server =====
    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8765)

    # ===== Privacy =====
    network_mode: NetworkMode = Field(default=NetworkMode.LLM_ONLY)
    enable_redaction: bool = Field(
        default=True,
        description="Mask phone numbers, id cards, emails and card numbers "
        "before any evidence is sent to the model",
    )
    allowed_domains: list[str] = Field(
        default_factory=lambda: [
            "api.openai.com",
            "api.anthropic.com",
            "api.deepseek.com",
            "api.minimaxi.com",
        ]
    )

    # ===== Embedding =====
    embedding_model: str = Field(default="BAAI/bge-m3")
    # auto = the GPU when there is one, the CPU otherwise. The reason
    # for the fallback is written down when the model is loaded.
    embedding_device: str = Field(default="auto")  # auto / cpu / cuda
    # How many slices the model reads at once. A bigger batch is
    # markedly faster on the CPU - the work per batch is the same, but
    # the call overhead and the padding are spread over more slices
    # (measured on real slices: 8 at a time gives 4 a second, 32
    # gives 13, 64 gives 16).
    embedding_batch_size: int = Field(default=32, gt=0)
    # How many tokens one piece of text may have. bge-m3 reads 8192,
    # but the long documents are cut into pieces anyway - and a shorter
    # read is a faster one (ADR-035).
    embedding_max_tokens: int = Field(default=512, gt=0)
    # Half precision on the GPU. It is worth nothing on the CPU - it is
    # slower there - so it is only used when there is a GPU.
    embedding_use_fp16: bool = Field(default=True)
    # bge-m3's own sparse (lexical) channel. The retrieval side does
    # not use it - the sparse channel is the full-text index (BM25) -
    # but computing it is a real cost on every slice, and on the CPU
    # it is a big one. Off by default; turn it on to fill the column
    # again (or to try the sparse channel out again some day).
    embedding_sparse_channel: bool = Field(default=False)
    # int8: the model's linear layers run in int8 (the weights become
    # int8, the activations are quantized on the fly). On the CPU this
    # is about twice as fast, and the vectors move a little - so it is
    # a setting and not a default. "none" keeps the model's own
    # precision.
    embedding_quantize: str = Field(default="none")

    # ===== Reranker =====
    reranker_model: str = Field(default="BAAI/bge-reranker-v2-m3")
    # Off by default: the cross-encoder costs 568 MB of disk, ~1.2 GB of
    # RAM and 5-30 s on the first use - on a CPU-only machine that eats
    # the whole latency budget of the first question.
    use_reranker: bool = Field(default=False)

    # ===== Retrieval =====
    top_k_initial: int = Field(default=50)  # Initial vector recall
    top_k_rerank: int = Field(default=10)  # After reranking
    confidence_threshold_high: float = Field(default=0.7)
    confidence_threshold_low: float = Field(default=0.4)
    fusion_alpha: float = Field(
        default=0.65,
        description="Weight of the dense channel in the fused score",
        ge=0.0,
        le=1.0,
    )
    fusion_beta: float = Field(
        default=0.35,
        description="Weight of the full-text channel in the fused score",
        ge=0.0,
        le=1.0,
    )
    # How long each step of the retrieval chain may take. A step that
    # runs over its budget is degraded (the full-text channel) or fails
    # the query (the dense channel) - what it must not do is hang.
    dense_timeout_seconds: float = Field(default=10.0, gt=0)
    sparse_timeout_seconds: float = Field(default=5.0, gt=0)
    embedding_timeout_seconds: float = Field(default=60.0, gt=0)

    # ===== Memory / Wiki =====
    enable_episodic_memory: bool = Field(default=True)
    enable_wiki_auto_compile: bool = Field(default=True)
    reflection_trigger_turns: int = Field(default=10)
    reflection_trigger_idle_minutes: int = Field(default=30)

    # ===== Ingestion =====
    max_file_size_mb: int = Field(
        default=100,
        description="Files above this size are skipped instead of being read into memory",
    )
    # How long a document has to be before the rules put it in the core
    # layer on its own (ADR-031).
    core_min_chars: int = Field(default=20000, gt=0)
    # Below this size the model is not asked about the layer - a note is
    # a note, and every question costs a call.
    tier_model_min_chars: int = Field(default=8000, gt=0)
    # How long one core document may take to compile (AC-P-008: five
    # minutes, without the queue in front of the model). The compilation
    # runs while the document is being indexed - never on the way a
    # question is answered. The ceiling is the promise itself: it cannot
    # be configured above five minutes.
    core_compile_timeout_seconds: int = Field(default=240, gt=0, le=300)
    # How much of a core document the model reads in one go. A longer
    # document is read in several parts, and every conclusion says
    # which part it came from (AC-E-013).
    core_read_chars: int = Field(default=60000, gt=0)
    # How many slices are written to the store at once. A build of
    # thousands of files is written in batches - one embedding call per
    # batch instead of one per file (AC-P-007).
    index_batch_size: int = Field(default=256, gt=0)
    # How many slices the vector store takes in one write. The build
    # hands the store a batch; the store writes it in pieces, so the
    # peak memory and the write latency stay bounded.
    index_write_batch_size: int = Field(default=256, gt=0)
    # How many slices a second may be written at most. Zero means no
    # limit. The cap keeps the build from starving the rest of the
    # machine (AC-P-007).
    index_rate_limit: float = Field(default=0.0, ge=0)
    # How many documents may sit in the core layer. A core layer with
    # thousands of documents is not a summary of anything any more
    # (AC-P-007).
    core_max_docs: int = Field(default=200, gt=0)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # The two fusion weights are a split of one whole: the fused
        # score is alpha * dense + beta * full-text, and a score above
        # 1.0 would mean nothing to the reader.
        total = self.fusion_alpha + self.fusion_beta
        if abs(total - 1.0) > 1e-6:
            raise ValueError(
                "YUANWORK_FUSION_ALPHA and YUANWORK_FUSION_BETA must add up "
                f"to 1.0 (got {self.fusion_alpha} + {self.fusion_beta})"
            )
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
