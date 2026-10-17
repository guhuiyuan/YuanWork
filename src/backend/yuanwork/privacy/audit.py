"""Audit logger - record LLM calls and data access for transparency."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


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
        prompt_summary: str,
        response_summary: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        duration_ms: float = 0.0,
        success: bool = True,
        error: str | None = None,
    ) -> None:
        """Log an LLM call."""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "type": "llm_call",
            "model": model,
            "task_type": task_type,
            "prompt_summary": prompt_summary[:200],
            "response_summary": response_summary[:200],
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "duration_ms": duration_ms,
            "success": success,
            "error": error,
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
