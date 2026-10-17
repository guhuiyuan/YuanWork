"""Pytest fixtures and configuration."""

from __future__ import annotations

import asyncio
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
