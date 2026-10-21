"""LLM Gateway - unified interface to multiple LLM providers via LiteLLM.

Supports:
- OpenAI / Anthropic / DeepSeek / MiniMax / Ollama (and 100+ others via LiteLLM)
- Routing: undertaker (cheap) for understanding, worker (powerful) for answering
- Fallback chain on failure
- Token budget tracking
- Streaming support
- Audit logging (Stage 3): every LLM call is recorded with timing,
  tokens, model, and a query hash (no full text). See AuditLogger.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import litellm
import yaml

from yuanwork.config import NetworkMode, YuanWorkSettings

logger = logging.getLogger(__name__)


# Default per-request timeout in seconds.
#
# litellm's own default is 6000 seconds (~100 minutes), which means a
# provider that stops responding leaves the request hanging for a very
# long time. 120s is generous for a 2000-token answer and still bounds a
# stuck call. For streaming calls this is an idle timeout: the provider
# has to send *something* within the window, not finish within it.
DEFAULT_REQUEST_TIMEOUT = 120.0


# Suppress litellm's noisy logs
litellm.suppress_debug_info = True
litellm.set_verbose = False


def _query_hash(messages: list["LLMMessage"]) -> str:
    """Stable 16-char hash of the prompt, for audit deduplication.

    The hash lets a user verify 'did I send this prompt before' without
    exposing the prompt text in the audit log. We serialize messages
    canonically (role + content) and SHA-256 the result.
    """
    canon = json.dumps(
        [{"role": m.role, "content": m.content} for m in messages],
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


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
class _StreamEnd:
    """Terminal marker pushed onto the streaming queue.

    Carries the token usage reported by the provider and any exception the
    worker thread hit, so the async side can finish the stream and log it.
    """

    usage: dict[str, int]
    error: BaseException | None = None


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
    # Per-request timeout in seconds. `None` uses DEFAULT_REQUEST_TIMEOUT.
    # For a streaming call this is an *idle* timeout: the provider has to
    # send something within this window or the request is aborted.
    timeout: float | None = None


class LLMGateway:
    """Unified LLM gateway with task-based routing."""

    def __init__(
        self,
        settings: YuanWorkSettings,
        audit_logger: Any | None = None,
    ):
        """
        Args:
            settings: YuanWorkSettings instance.
            audit_logger: Optional AuditLogger (from yuanwork.privacy.audit).
                When provided, every complete() and stream() call is
                recorded with model, task_type, token counts, duration,
                success/failure, and a hash of the prompt (no full text).
                Pass `None` to disable audit logging (e.g. in tests).
        """
        self.settings = settings
        self.audit_logger = audit_logger
        self._routing: dict[str, str] = {}
        # alias -> {provider, model_name, api_base, api_key, ...}
        self._models: dict[str, dict[str, Any]] = {}
        self._load_config()

    def _load_config(self) -> None:
        """Load model configuration from configs/models.yaml."""
        import os

        config_path = self.settings.config_dir / "models.yaml"

        if not config_path.exists():
            # No config file: fall back to the built-in template so a fresh
            # install still works with the one model whose key is set.
            logger.warning(
                f"models.yaml not found at {config_path}. "
                f"Using the built-in default. Create configs/models.yaml to customize."
            )
            data = yaml.safe_load(DEFAULT_MODELS_YAML) or {}
        else:
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
        """Default task-to-model routing.

        Mirrors the built-in models.yaml: one model for every task.
        """
        return {
            "understand": "minimax-chat",
            "extract": "minimax-chat",
            "answer": "minimax-chat",
            "conflict_check": "minimax-chat",
            "reflection": "minimax-chat",
            "wiki_compile": "minimax-chat",
            "ner": "minimax-chat",
            "general": "minimax-chat",
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
            # Without this litellm waits 6000 seconds by default.
            "timeout": task.timeout or DEFAULT_REQUEST_TIMEOUT,
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

        # Audit-logging state
        start = time.monotonic()
        qhash = _query_hash(task.messages)
        prompt_summary = "\n".join(
            f"[{m.role}] {m.content[:200]}" for m in task.messages
        )[:200]
        response_content: str | None = None
        usage: dict[str, int] = {}
        error: str | None = None
        try:
            response = await asyncio.to_thread(litellm.completion, **kwargs)
            content = response.choices[0].message.content
            usage = {
                "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
                "completion_tokens": response.usage.completion_tokens if response.usage else 0,
                "total_tokens": response.usage.total_tokens if response.usage else 0,
            }
            response_content = content or ""
            return LLMResponse(
                content=response_content,
                model=alias,
                finish_reason=response.choices[0].finish_reason or "stop",
                usage=usage,
                raw=response,
            )
        except Exception as e:
            logger.exception(f"LLM call failed for model {alias}: {e}")
            error = repr(e)
            raise
        finally:
            if self.audit_logger is not None:
                try:
                    duration_ms = (time.monotonic() - start) * 1000.0
                    self.audit_logger.log_llm_call(
                        model=alias,
                        task_type=task.task_type,
                        prompt_summary=f"#{qhash} {prompt_summary}",
                        response_summary=(response_content or "")[:200],
                        prompt_tokens=usage.get("prompt_tokens", 0),
                        completion_tokens=usage.get("completion_tokens", 0),
                        duration_ms=duration_ms,
                        success=error is None,
                        error=error,
                    )
                except Exception:
                    logger.exception("Audit log write failed (non-fatal)")

    async def stream(self, task: LLMTask) -> AsyncIterator[str]:
        """Stream completion tokens.

        litellm's streaming response is a *blocking* generator. Pulling it
        directly would hold the event loop for the whole generation, so
        every other request (health checks, other chats) would stall until
        the answer was finished.

        Instead the generator is driven on a worker thread and its deltas
        are handed to the event loop through an asyncio.Queue. The loop
        stays free for the whole stream, and the consumer receives each
        delta as soon as the provider produces it.
        """
        alias = self.get_model_for_task(task.task_type)
        messages = [{"role": m.role, "content": m.content} for m in task.messages]

        kwargs = self._resolve_litellm_kwargs(alias)
        kwargs.update({
            "messages": messages,
            "temperature": task.temperature,
            "stream": True,
            # Without this litellm waits 6000 seconds by default. For a
            # streaming call it is an idle timeout: the provider has to
            # send something within the window.
            "timeout": task.timeout or DEFAULT_REQUEST_TIMEOUT,
        })

        if task.max_tokens:
            kwargs["max_tokens"] = task.max_tokens

        if task.json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        if self.settings.network_mode == NetworkMode.OFFLINE:
            model_str = kwargs.get("model", "")
            if not model_str.startswith("ollama/") and not model_str.startswith("local/"):
                kwargs["model"] = "ollama/llama3.1:8b"

        # Audit-logging state
        start = time.monotonic()
        qhash = _query_hash(task.messages)
        prompt_summary = "\n".join(
            f"[{m.role}] {m.content[:200]}" for m in task.messages
        )[:200]
        accumulated: list[str] = []
        prompt_tokens = 0
        completion_tokens = 0
        error: str | None = None
        success = False

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue()
        # Set when the consumer goes away so the worker stops pulling.
        stop = threading.Event()

        def _put(item: Any) -> None:
            """Hand an item to the event loop from the worker thread.

            The loop may already be closed if the process is tearing down;
            in that case the item is simply dropped instead of raising
            RuntimeError inside the worker thread.
            """
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:
                pass

        def _produce() -> None:
            """Drive the blocking litellm generator on a worker thread."""
            usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            failure: BaseException | None = None
            response: Any = None
            try:
                response = litellm.completion(**kwargs)
                # An explicit iterator, so `stop` is checked BEFORE the
                # next chunk is pulled. A plain `for` pulls first and then
                # runs the body, which would fetch one extra chunk from the
                # provider after the client disconnected.
                chunks = iter(response)
                while not stop.is_set():
                    try:
                        chunk = next(chunks)
                    except StopIteration:
                        break
                    choices = getattr(chunk, "choices", None)
                    if choices:
                        text = getattr(choices[0].delta, "content", None)
                        if text:
                            _put(text)
                    # The final chunk carries usage info for some providers
                    chunk_usage = getattr(chunk, "usage", None)
                    if chunk_usage:
                        usage["prompt_tokens"] = (
                            chunk_usage.prompt_tokens or usage["prompt_tokens"]
                        )
                        usage["completion_tokens"] = (
                            chunk_usage.completion_tokens or usage["completion_tokens"]
                        )
                        usage["total_tokens"] = (
                            getattr(chunk_usage, "total_tokens", 0)
                            or usage["total_tokens"]
                        )
            except BaseException as exc:  # noqa: BLE001 - forwarded to the consumer
                failure = exc
            finally:
                # Return the connection to the provider's pool instead of
                # leaving it dangling until the garbage collector runs.
                close = getattr(response, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:  # noqa: BLE001 - best effort cleanup
                        pass
                _put(_StreamEnd(usage, failure))

        worker = threading.Thread(
            target=_produce, name="litellm-stream", daemon=True
        )
        worker.start()

        try:
            while True:
                item = await queue.get()
                if isinstance(item, _StreamEnd):
                    prompt_tokens = item.usage["prompt_tokens"]
                    completion_tokens = item.usage["completion_tokens"]
                    if item.error is not None:
                        raise item.error
                    success = True
                    break
                accumulated.append(item)
                yield item
        except Exception as e:
            logger.exception(f"Streaming LLM call failed: {e}")
            error = repr(e)
            raise
        finally:
            # Runs on normal completion, on error, and on early exit
            # (client disconnect) - in the last case it also tells the
            # worker thread to stop pulling from the provider.
            stop.set()
            if self.audit_logger is not None:
                try:
                    duration_ms = (time.monotonic() - start) * 1000.0
                    self.audit_logger.log_llm_call(
                        model=alias,
                        task_type=task.task_type,
                        prompt_summary=f"#{qhash} {prompt_summary}",
                        response_summary="".join(accumulated)[:200],
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        duration_ms=duration_ms,
                        success=success,
                        error=error,
                    )
                except Exception:
                    logger.exception("Audit log write failed (non-fatal)")

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
#
# Only MiniMax is enabled out of the box - it is the one model whose key
# is actually set. Uncomment another provider and set its env var to
# route tasks to it.

models:
  # MiniMax (uses Anthropic-compatible API)
  minimax-chat:
    provider: anthropic
    api_base: https://api.minimaxi.com/anthropic
    api_key_env: MINIMAX_API_KEY
    model_name: MiniMax-M3

  # Example: OpenAI
  # gpt-4o:
  #   provider: openai
  #   api_key_env: OPENAI_API_KEY
  #   model_name: gpt-4o
  #
  # gpt-4o-mini:
  #   provider: openai
  #   api_key_env: OPENAI_API_KEY
  #   model_name: gpt-4o-mini

  # Example: Anthropic
  # claude-sonnet:
  #   provider: anthropic
  #   api_key_env: ANTHROPIC_API_KEY
  #   model_name: claude-3-5-sonnet-latest

  # Example: DeepSeek
  # deepseek-chat:
  #   provider: openai
  #   api_base: https://api.deepseek.com
  #   api_key_env: DEEPSEEK_API_KEY
  #   model_name: deepseek-chat

  # Example: Local Ollama
  # ollama-llama3:
  #   provider: ollama
  #   api_base: http://localhost:11434
  #   model_name: llama3.1:8b

# Task routing: which model handles which task type
routing:
  understand: minimax-chat       # Query understanding
  extract: minimax-chat          # Information extraction
  answer: minimax-chat           # Final answer generation
  conflict_check: minimax-chat   # Conflict detection
  reflection: minimax-chat       # Memory reflection
  wiki_compile: minimax-chat     # Wiki page compilation
  ner: minimax-chat              # Named entity recognition
  general: minimax-chat          # Generic chat fallback

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
