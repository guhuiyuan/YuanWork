"""Trace logger - records agent execution traces for debugging."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any


class TraceLogger:
    """Logs agent execution traces to local files (for debugging only)."""

    def __init__(self, trace_dir: Path):
        self.trace_dir = Path(trace_dir)
        self.trace_dir.mkdir(parents=True, exist_ok=True)

    def log(self, session_id: str, trace: list[dict[str, Any]]) -> None:
        """Save a trace to disk."""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.trace_dir / f"trace_{session_id}_{ts}.json"

        try:
            with path.open("w", encoding="utf-8") as f:
                json.dump(
                    {
                        "session_id": session_id,
                        "timestamp": ts,
                        "trace": trace,
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception as e:
            logging.warning(f"Failed to save trace: {e}")
