"""Project-level pytest configuration.

Adds src/backend to sys.path so `from yuanwork.xxx import ...` works in tests
without requiring the user to set PYTHONPATH manually.

This file is auto-discovered by pytest (conftest.py magic).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Path to this conftest.py: src/backend/conftest.py
_BACKEND_DIR = Path(__file__).resolve().parent

# Prepend to sys.path so `import yuanwork` resolves to src/backend/yuanwork/
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))
