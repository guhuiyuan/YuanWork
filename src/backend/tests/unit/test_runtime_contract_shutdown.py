"""The shutdown hook and the helper functions that are never used."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from yuanwork.api.app import create_app
from yuanwork.config import YuanWorkSettings


@pytest.fixture
def app(tmp_path: Path):
    return create_app(YuanWorkSettings(data_dir=tmp_path / "data"))


# --------------------------------------------------------------------------
# the shutdown hook
# --------------------------------------------------------------------------


async def test_the_shutdown_releases_the_components(app):
    from fastapi.testclient import TestClient

    with TestClient(app):
        assert app.state.semantic_memory is not None
        assert app.state.sqlite_store is not None
    # after the context manager the stores are closed
    assert app.state.sqlite_store._conn is None


def test_the_shutdown_hook_closes_both_stores():
    """The lifespan's finally block releases the vector store and the
    relational one."""
    from yuanwork.api import app as app_module

    source = inspect.getsource(app_module)
    assert "semantic_memory.close()" in source
    assert "sqlite_store.close()" in source


# --------------------------------------------------------------------------
# the helper functions that are never called
# --------------------------------------------------------------------------


def test_the_offline_mode_helper_is_never_called():
    import pathlib

    backend = pathlib.Path(__file__).resolve().parents[2] / "yuanwork"
    callers = []
    for path in backend.rglob("*.py"):
        if path.name == "config.py":
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        if "is_offline_mode" in source:
            callers.append(path.name)
    assert callers == []


def test_the_yaml_config_helper_is_never_called():
    import pathlib

    backend = pathlib.Path(__file__).resolve().parents[2] / "yuanwork"
    callers = []
    for path in backend.rglob("*.py"):
        if path.name == "config.py":
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        if "load_yaml_config" in source:
            callers.append(path.name)
    assert callers == []


def test_the_yaml_helper_returns_an_empty_dict_for_a_missing_file(tmp_path: Path):
    from yuanwork.config import load_yaml_config

    assert load_yaml_config("does-not-exist") == {}


def test_the_privacy_config_file_does_not_exist():
    """The guard's error message points at `configs/privacy.yaml`, but
    that file is not there."""
    import pathlib

    backend = pathlib.Path(__file__).resolve().parents[2] / "yuanwork"
    configs = backend.parent / "configs"
    assert not (configs / "privacy.yaml").exists()
    assert (configs / "models.yaml").exists()


# --------------------------------------------------------------------------
# the log configuration
# --------------------------------------------------------------------------


def test_the_log_configuration_is_never_called():
    import pathlib

    backend = pathlib.Path(__file__).resolve().parents[2] / "yuanwork"
    callers = []
    for path in backend.rglob("*.py"):
        if path.name == "logging.py":
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        if "setup_logging" in source:
            callers.append(path.name)
    assert callers == []


def test_the_structlog_dependency_is_never_imported():
    import pathlib

    backend = pathlib.Path(__file__).resolve().parents[2] / "yuanwork"
    for path in backend.rglob("*.py"):
        source = path.read_text(encoding="utf-8", errors="replace")
        assert "import structlog" not in source
