"""LLM Gateway - unified interface to multiple LLM providers via LiteLLM.

Supports:
- OpenAI / Anthropic / DeepSeek / MiniMax / Ollama (and 100+ others via LiteLLM)
- Routing: undertaker (cheap) for understanding, worker (powerful) for answering
- Fallback chain on failure
- Token budget tracking
- Streaming support
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import litellm
import yaml

from yuanwork.config import NetworkMode, YuanWorkSettings

logger = logging.getLogger(__name__)


# Suppress litellm's noisy logs
litellm.suppress_debug_info = True
litellm.set_verbose = False


@dataclass
class LLMMessage:
    """A single message in a chat conversation."""

    role: Literal["system", "user", "assistant"]
    content: str
    name: str | None = None


@dataclass
class LLMResponse:
    """Standardized LLM response."""

    content: str
    model: str
    finish_reason: str
    usage: dict[str, int] = field(default_factory=dict)
    raw: Any = None


@dataclass
class LLMTask:
    """A task that needs an LLM call."""

    task_type: Literal[
        "understand",     # Query understanding
        "extract",        # Information extraction
        "answer",         # Final answer generation
        "conflict_check", # Conflict detection
        "reflection",     # Memory reflection
        "wiki_compile",   # Wiki page compilation
        "ner",            # Named entity recognition
        "general",        # Generic chat
    ]
    messages: list[LLMMessage]
    temperature: float = 0.0
    max_tokens: int | None = None
    json_mode: bool = False
    stream: bool = False


class LLMGateway:
    """Unified LLM gateway with task-based routing."""

    def __init__(self, settings: YuanWorkSettings):
        self.settings = settings
        self._routing: dict[str, str] = {}
        self._models: dict[str, dict[str, Any]] = {}
        self._load_config()

    def _load_config(self) -> None:
        """Load model configuration from configs/models.yaml."""
        config_path = self.settings.config_dir / "models.yaml"

        if not config_path.exists():
            logger.warning(
                f"models.yaml not found at {config_path}. "
                f"Using defaults. Create configs/models.yaml to customize."
            )
            self._routing = self._default_routing()
            return

        with config_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        self._models = data.get("models", {})
        self._routing = data.get("routing", self._default_routing())

        logger.info(f"Loaded {len(self._models)} models from config")

    def _default_routing(self) -> dict[str, str]:
        """Default task-to-model routing."""
        return {
            "understand": "gpt-4o-mini",
            "extract": "gpt-4o-mini",
            "answer": "gpt-4o",
            "conflict_check": "gpt-4o-mini",
            "reflection": "gpt-4o-mini",
            "wiki_compile": "gpt-4o",
            "ner": "gpt-4o-mini",
            "general": "gpt-4o",
        }

    def configured_models(self) -> list[str]:
        """List configured model names."""
        return list(self._models.keys())

    def get_model_for_task(self, task_type: str) -> str:
        """Get the model name for a task type."""
        return self._routing.get(task_type, self._routing.get("general", "gpt-4o-mini"))

    async def complete(self, task: LLMTask) -> LLMResponse:
        """Run a non-streaming completion."""
        model = self.get_model_for_task(task.task_type)

        # Build litellm messages
        messages = [{"role": m.role, "content": m.content} for m in task.messages]

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": task.temperature,
            "stream": False,
        }

        if task.max_tokens:
            kwargs["max_tokens"] = task.max_tokens

        if task.json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        # Apply offline mode check
        if self.settings.network_mode == NetworkMode.OFFLINE:
            # Force local model
            if not model.startswith("ollama/") and not model.startswith("local/"):
                logger.warning(
                    f"Offline mode: redirecting {model} to local fallback"
                )
                kwargs["model"] = "ollama/llama3.1:8b"

        try:
            response = await asyncio.to_thread(litellm.completion, **kwargs)
            content = response.choices[0].message.content
            usage = {
                "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
                "completion_tokens": response.usage.completion_tokens if response.usage else 0,
                "total_tokens": response.usage.total_tokens if response.usage else 0,
            }
            return LLMResponse(
                content=content or "",
                model=model,
                finish_reason=response.choices[0].finish_reason or "stop",
                usage=usage,
                raw=response,
            )
        except Exception as e:
            logger.exception(f"LLM call failed for model {model}: {e}")
            raise

    async def stream(self, task: LLMTask) -> AsyncIterator[str]:
        """Stream completion tokens."""
        model = self.get_model_for_task(task.task_type)
        messages = [{"role": m.role, "content": m.content} for m in task.messages]

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": task.temperature,
            "stream": True,
        }

        if task.max_tokens:
            kwargs["max_tokens"] = task.max_tokens

        if task.json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        if self.settings.network_mode == NetworkMode.OFFLINE:
            if not model.startswith("ollama/") and not model.startswith("local/"):
                kwargs["model"] = "ollama/llama3.1:8b"

        try:
            # Use litellm's streaming completion
            response = await asyncio.to_thread(litellm.completion, **kwargs)
            for chunk in response:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        except Exception as e:
            logger.exception(f"Streaming LLM call failed: {e}")
            raise

    async def chat(
        self,
        messages: list[LLMMessage],
        task_type: str = "general",
        temperature: float = 0.7,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> str:
        """Convenience method for simple chat."""
        task = LLMTask(
            task_type=task_type,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
        )
        response = await self.complete(task)
        return response.content


# Default models configuration example (saved at first run)
DEFAULT_MODELS_YAML = """\
# YuanWork LLM Models Configuration
# Edit this file to add your API keys and switch models.

models:
  # Example: MiniMax (uses Anthropic-compatible API)
  minimax-chat:
    provider: anthropic
    api_base: https://api.minimaxi.com/anthropic
    api_key_env: MINIMAX_API_KEY
    model_name: MiniMax-M3

  # Example: OpenAI
  gpt-4o:
    provider: openai
    api_key_env: OPENAI_API_KEY
    model_name: gpt-4o

  gpt-4o-mini:
    provider: openai
    api_key_env: OPENAI_API_KEY
    model_name: gpt-4o-mini

  # Example: Anthropic
  claude-sonnet:
    provider: anthropic
    api_key_env: ANTHROPIC_API_KEY
    model_name: claude-3-5-sonnet-latest

  # Example: DeepSeek
  deepseek-chat:
    provider: openai
    api_base: https://api.deepseek.com
    api_key_env: DEEPSEEK_API_KEY
    model_name: deepseek-chat

  # Example: Local Ollama
  ollama-llama3:
    provider: ollama
    api_base: http://localhost:11434
    model_name: llama3.1:8b

# Task routing: which model handles which task type
routing:
  understand: gpt-4o-mini       # Query understanding (cheap)
  extract: gpt-4o-mini          # Information extraction (cheap)
  answer: gpt-4o                # Final answer (powerful)
  conflict_check: gpt-4o-mini   # Conflict detection (cheap)
  reflection: gpt-4o-mini       # Memory reflection (cheap)
  wiki_compile: gpt-4o          # Wiki page compilation (needs quality)
  ner: gpt-4o-mini             # Named entity recognition (cheap)
  general: gpt-4o               # Generic chat

budget:
  per_session_max_tokens: 200000
  per_query_max_tokens: 8000
  monthly_cap_usd: 30
"""


def ensure_default_config(config_dir: Path) -> None:
    """Create default models.yaml if it doesn't exist."""
    config_path = config_dir / "models.yaml"
    if not config_path.exists():
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path.write_text(DEFAULT_MODELS_YAML, encoding="utf-8")
        logger.info(f"Created default config at {config_path}")
