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
        # alias -> {provider, model_name, api_base, api_key, ...}
        self._models: dict[str, dict[str, Any]] = {}
        self._load_config()

    def _load_config(self) -> None:
        """Load model configuration from configs/models.yaml."""
        import os

        config_path = self.settings.config_dir / "models.yaml"

        if not config_path.exists():
            logger.warning(
                f"models.yaml not found at {config_path}. "
                f"Using defaults. Create configs/models.yaml to customize."
            )
            self._routing = self._default_routing()
            self._models = {}
            return

        with config_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        raw_models = data.get("models", {})
        self._routing = data.get("routing", self._default_routing())

        # Resolve api_key_env -> api_key and keep provider/base/model_name
        # for later assembly into the litellm call.
        for alias, spec in raw_models.items():
            spec = dict(spec)  # copy so we don't mutate YAML in memory
            api_key_env = spec.pop("api_key_env", None)
            if api_key_env:
                resolved = os.environ.get(api_key_env) or os.environ.get(
                    api_key_env.upper()
                )
                if resolved:
                    spec["api_key"] = resolved
                else:
                    logger.warning(
                        f"Model '{alias}' references api_key_env={api_key_env} "
                        f"but that env var is not set"
                    )
            self._models[alias] = spec

        logger.info(
            f"Loaded {len(self._models)} models from config "
            f"({sum(1 for s in self._models.values() if s.get('api_key'))} with keys)"
        )

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
        """Get the model name for a task type.

        If the routed model has no api_key configured, fall back to the
        first configured model that does have one. This lets a fresh
        install with only one API key still work end-to-end instead of
        returning 'Missing credentials' on every call.
        """
        target = self._routing.get(task_type) or self._routing.get("general")
        if target and self._models.get(target, {}).get("api_key"):
            return target
        # Fallback: any configured model with a key
        for alias, spec in self._models.items():
            if spec.get("api_key"):
                if target and not self._models.get(target, {}).get("api_key"):
                    logger.warning(
                        f"Routing target '{target}' has no API key; "
                        f"falling back to '{alias}'"
                    )
                return alias
        return target or "gpt-4o-mini"

    def _resolve_litellm_kwargs(self, alias: str) -> dict[str, Any]:
        """Translate a configured alias into litellm-ready kwargs.

        The alias from `routing` (e.g. "gpt-4o-mini") is looked up in the
        `models` section to get provider/model_name/api_base/api_key. The
        resulting litellm model string is `provider/model_name` (or the raw
        model_name if the provider is already embedded, or just model_name
        for ollama where provider is implicit).
        """
        spec = self._models.get(alias)
        if not spec:
            # Fall back: treat the alias itself as the litellm model.
            # (Lets routing work even if user only configures routing.)
            return {"model": alias}

        provider = spec.get("provider", "").lower()
        model_name = spec.get("model_name") or alias

        # ollama uses host:port prefix; litellm detects the provider from
        # the "ollama/" prefix or api_base.
        if provider == "ollama":
            litellm_model = f"ollama/{model_name}"
        elif provider == "anthropic":
            litellm_model = f"anthropic/{model_name}"
        elif provider == "openai":
            litellm_model = f"openai/{model_name}"
        elif provider in ("deepseek", "minimax", "custom"):
            # deepseek uses OpenAI-compatible API; minimax uses anthropic-
            # compatible. We model both by setting api_base so litellm routes
            # the call to the right endpoint with the right protocol.
            litellm_model = model_name
        else:
            litellm_model = model_name

        kwargs: dict[str, Any] = {"model": litellm_model}
        if spec.get("api_key"):
            kwargs["api_key"] = spec["api_key"]
        if spec.get("api_base"):
            kwargs["api_base"] = spec["api_base"]
        return kwargs

    async def complete(self, task: LLMTask) -> LLMResponse:
        """Run a non-streaming completion."""
        alias = self.get_model_for_task(task.task_type)

        # Build litellm messages
        messages = [{"role": m.role, "content": m.content} for m in task.messages]

        kwargs = self._resolve_litellm_kwargs(alias)
        kwargs.update({
            "messages": messages,
            "temperature": task.temperature,
            "stream": False,
        })

        if task.max_tokens:
            kwargs["max_tokens"] = task.max_tokens

        if task.json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        # Apply offline mode check
        if self.settings.network_mode == NetworkMode.OFFLINE:
            model_str = kwargs.get("model", "")
            if not model_str.startswith("ollama/") and not model_str.startswith("local/"):
                logger.warning(
                    f"Offline mode: redirecting {alias} to local fallback"
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
                model=alias,
                finish_reason=response.choices[0].finish_reason or "stop",
                usage=usage,
                raw=response,
            )
        except Exception as e:
            logger.exception(f"LLM call failed for model {alias}: {e}")
            raise

    async def stream(self, task: LLMTask) -> AsyncIterator[str]:
        """Stream completion tokens."""
        alias = self.get_model_for_task(task.task_type)
        messages = [{"role": m.role, "content": m.content} for m in task.messages]

        kwargs = self._resolve_litellm_kwargs(alias)
        kwargs.update({
            "messages": messages,
            "temperature": task.temperature,
            "stream": True,
        })

        if task.max_tokens:
            kwargs["max_tokens"] = task.max_tokens

        if task.json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        if self.settings.network_mode == NetworkMode.OFFLINE:
            model_str = kwargs.get("model", "")
            if not model_str.startswith("ollama/") and not model_str.startswith("local/"):
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
