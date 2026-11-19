"""A backend that writes its stack traces to a file.

When the backend stops answering, the question is *where* it is stuck.
faulthandler can dump every thread's stack to a file every minute —
that is the answer.

**The guard matters**: this file is the main module of the process, and
a worker process started by the pool re-imports it. Without the guard
every worker would start a whole backend of its own — eight backends,
eight embedding models, and the machine runs out of memory.

Usage::

    .venv/Scripts/python.exe scripts/bench/diag_backend.py --port 8765
"""

from __future__ import annotations

import faulthandler
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src" / "backend"))


def main() -> None:
    trace = (ROOT / "logs" / "diag-trace.txt").open("w", encoding="utf-8")
    faulthandler.enable(trace)
    faulthandler.dump_traceback_later(60, repeat=True, file=trace)

    from yuanwork.__main__ import main as run

    run()


if __name__ == "__main__":
    main()
