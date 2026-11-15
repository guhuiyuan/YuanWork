"""Audit logger - record LLM calls and data access for transparency.

The audit log answers two questions: *did I call the model* and *how
much did it cost*. It deliberately does not answer *what did I ask* -
the log used to keep the first 200 characters of every prompt and
answer, which made the log itself a second copy of the user's data.

An entry therefore holds the *hash* of the prompt, its length, the token
counts and the duration - never the text. The API does not accept the
text either: there is no way to store it by accident.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


#: The kinds of failure an entry can carry. The text of an exception is
#: not stored - it can contain the prompt, a local path or an upstream
#: endpoint.
ERROR_KINDS = (
    "timeout",
    "connection",
    "rate_limit",
    "auth",
    "invalid_response",
    "cancelled",
    "unknown",
)


def classify_error(exc: BaseException | None) -> str | None:
    """The kind of a failure, without its text."""
    if exc is None:
        return None

    name = type(exc).__name__.lower()
    if "timeout" in name:
        return "timeout"
    if "ratelimit" in name or "toomanyrequests" in name:
        return "rate_limit"
    if "auth" in name or "permission" in name:
        return "auth"
    if "connect" in name:
        return "connection"
    if "cancelled" in name:
        return "cancelled"
    if "json" in name or "decode" in name:
        return "invalid_response"
    return "unknown"


class AuditLogger:
    """Log LLM calls and data access for user transparency.

    Stores logs in a local SQLite table for easy inspection.
    """

    def __init__(self, audit_path: Path):
        self.audit_path = Path(audit_path)
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)

    def log_llm_call(
        self,
        model: str,
        task_type: str,
        prompt_hash: str,
        prompt_length: int = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        duration_ms: float = 0.0,
        success: bool = True,
        error_kind: str | None = None,
    ) -> None:
        """Log an LLM call - without the prompt or the answer.

        The prompt's hash lets the user see that the same question was
        asked twice; its length and the token counts say what it cost.
        """
        entry = {
            "timestamp": datetime.now().isoformat(),
            "type": "llm_call",
            "model": model,
            "task_type": task_type,
            "prompt_hash": prompt_hash,
            "prompt_length": prompt_length,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "duration_ms": duration_ms,
            "success": success,
            "error_kind": error_kind,
        }
        self._append(entry)

    def log_data_access(
        self, action: str, resource: str, details: dict[str, Any] | None = None
    ) -> None:
        """Log a data access event."""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "type": "data_access",
            "action": action,
            "resource": resource,
            "details": details or {},
        }
        self._append(entry)

    def _append(self, entry: dict[str, Any]) -> None:
        """Append an entry to the audit log (JSONL format)."""
        try:
            with self.audit_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"Failed to write audit log: {e}")

    def clear(self) -> None:
        """Clear the audit log."""
        try:
            self.audit_path.unlink(missing_ok=True)
        except Exception:
            pass

    def read_all(self) -> list[dict[str, Any]]:
        """Read all audit entries."""
        if not self.audit_path.exists():
            return []
        entries = []
        with self.audit_path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return entries
