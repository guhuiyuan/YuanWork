"""Tests for where the backend looks for its .env file.

The desktop app starts the backend with cwd = ``src/backend``, while the
.env lives at the project root. A cwd-relative lookup alone therefore
misses the file, and the LLM API key never reaches ``os.environ`` - the
app starts, but every chat fails with "missing credentials".
"""

from __future__ import annotations

import os
from pathlib import Path


def test_the_project_root_dotenv_is_a_candidate():
    """The repo's own .env must be on the search path."""
    from yuanwork.api.app import _dotenv_candidates

    candidates = _dotenv_candidates()

    package_root = Path(__file__).resolve().parents[3]
    expected = package_root / ".env"
    assert expected in candidates, (
        f"{expected} is not among the candidates: {candidates}"
    )


def test_the_cwd_dotenv_is_the_first_candidate():
    from yuanwork.api.app import _dotenv_candidates

    candidates = _dotenv_candidates()

    assert candidates[0] == Path.cwd() / ".env"


def test_the_project_root_is_found_even_from_the_sidecar_cwd(monkeypatch):
    """The decisive case: the desktop app starts the backend with
    cwd = src/backend, where there is no .env."""
    from yuanwork.api.app import _dotenv_candidates

    backend_dir = Path(__file__).resolve().parents[2]  # src/backend
    monkeypatch.setattr(os, "getcwd", lambda: str(backend_dir))

    candidates = _dotenv_candidates()

    package_root = Path(__file__).resolve().parents[3]
    assert candidates[0] == backend_dir / ".env"
    assert not (backend_dir / ".env").is_file(), "test premise: no .env there"
    assert package_root / ".env" in candidates


def test_the_search_reaches_at_least_three_levels_up():
    """<root>/src/backend/yuanwork -> <root> is three levels up."""
    from yuanwork.api.app import _dotenv_candidates

    candidates = _dotenv_candidates()

    package_root = Path(__file__).resolve().parents[3]
    assert package_root in [c.parent for c in candidates]


def test_loading_does_not_clobber_an_exported_variable(monkeypatch):
    """A value the user exported wins over the .env file."""
    from yuanwork.api.app import _load_dotenv_into_environ

    monkeypatch.setenv("YUANWORK_TEST_KEEP_ME", "exported")

    _load_dotenv_into_environ()

    assert os.environ["YUANWORK_TEST_KEEP_ME"] == "exported"
