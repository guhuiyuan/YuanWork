"""Pytest fixtures and configuration."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def temp_dir():
    """Provide a temporary directory that's cleaned up after the test."""
    tmp = Path(tempfile.mkdtemp(prefix="yuanwork_test_"))
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def event_loop():
    """Provide an event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(autouse=True)
def _clean_yuanwork_env(monkeypatch):
    """Clear the ``YUANWORK_*`` variables before every test.

    The application's lifespan loads the repository's own ``.env`` into
    the process environment. Without this, one test that starts the app
    leaks its configuration into every test that runs after it — and the
    environment has a higher priority than the ``.env`` file, so the
    file-reading tests would silently read the wrong values.
    """
    for key in list(os.environ):
        if key.startswith("YUANWORK_"):
            monkeypatch.delenv(key, raising=False)
