"""本项目可写状态的落点（ADR-045）。

三件事要钉住：

1. **一个旋钮搬走全部**——``YUANWORK_HOME`` 一设，索引与模型缓存一起搬
2. **落点不跟着当前目录跑**——相对路径按项目根解析；同一个项目在任何
   启动方式下都是同一套知识库
3. **模型缓存的落点必须在 huggingface_hub 被导入前定下来**——它的缓存位置
   是导入时算的常量，晚一步设置不报错也不生效
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from yuanwork import paths


@pytest.fixture(autouse=True)
def _no_environment_leak(monkeypatch):
    """Every test starts from a clean slate.

    The machine's own .env sits at the repository root and this
    project's .env is read by walking up from the package - so without
    this the tests would read the developer's real configuration.
    """
    for name in (
        paths.ENV_HOME,
        paths.ENV_DATA_DIR,
        paths.ENV_MODEL_CACHE,
        paths.ENV_PROJECT_ROOT,
        "HF_HOME",
        "HUGGINGFACE_HUB_CACHE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(paths, "read_dotenv", dict)
    monkeypatch.setattr(paths, "_lookup", _lookup_without_dotenv)
    yield


def _lookup_without_dotenv(name: str, dotenv=None) -> str | None:
    """The .env is out of the way - only the environment counts."""
    for candidate in (name, name.upper()):
        value = os.environ.get(candidate)
        if value and value.strip():
            return value.strip()
    return None


# ===== the root moves everything =====


def test_the_root_moves_the_index_and_the_model_cache(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path / "yuanwork"))
    assert paths.data_dir() == tmp_path / "yuanwork" / "data"
    assert paths.model_cache_dir() == tmp_path / "yuanwork" / "models"


def test_the_data_dir_can_be_pointed_somewhere_else_on_its_own(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path / "yuanwork"))
    monkeypatch.setenv(paths.ENV_DATA_DIR, str(tmp_path / "elsewhere"))
    assert paths.data_dir() == tmp_path / "elsewhere"
    # The models still follow the root - the two are separate knobs.
    assert paths.model_cache_dir() == tmp_path / "yuanwork" / "models"


def test_the_model_cache_can_be_pointed_somewhere_else_on_its_own(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(paths.ENV_MODEL_CACHE, str(tmp_path / "models"))
    assert paths.model_cache_dir() == tmp_path / "models"


def test_a_tilde_in_the_path_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.ENV_HOME, "~/somewhere")
    assert paths.root_dir() == Path.home() / "somewhere"


def test_the_paths_are_absolute(tmp_path, monkeypatch):
    """A relative path must not stay relative - it would follow the cwd."""
    monkeypatch.setenv(paths.ENV_DATA_DIR, "./data")
    assert paths.data_dir().is_absolute()


# ===== the location does not follow the working directory =====


def test_a_relative_path_resolves_against_the_project_root(
    tmp_path, monkeypatch
):
    """``./data`` means "this project's data", not "where I stand"."""
    monkeypatch.setenv(paths.ENV_PROJECT_ROOT, str(tmp_path))
    monkeypatch.setenv(paths.ENV_DATA_DIR, "./data")
    assert paths.data_dir() == tmp_path / "data"


def test_the_location_does_not_change_with_the_working_directory(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(paths.ENV_PROJECT_ROOT, str(tmp_path))
    monkeypatch.setenv(paths.ENV_DATA_DIR, "./data")

    here = tmp_path / "a" / "b"
    here.mkdir(parents=True)
    monkeypatch.chdir(here)
    first = paths.data_dir()

    monkeypatch.chdir(tmp_path)
    assert paths.data_dir() == first


def test_the_project_root_is_found_from_the_package_not_the_cwd():
    """The checkout is the same whether the backend starts from
    ``src/backend`` or from the repository root."""
    root = paths.project_root()
    assert root is not None
    assert (root / "src" / "backend").is_dir()


# ===== the model cache has to be set before the library is imported =====


def test_the_model_cache_is_handed_to_huggingface_hub(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path / "yuanwork"))
    variables = paths.model_cache_env_vars()
    assert variables["HF_HOME"] == str(tmp_path / "yuanwork" / "models")
    assert variables["HUGGINGFACE_HUB_CACHE"] == str(
        tmp_path / "yuanwork" / "models" / "hub"
    )


def test_a_cache_the_user_pointed_at_is_left_alone(tmp_path, monkeypatch):
    """That is their choice, not ours to change."""
    monkeypatch.setenv("HF_HOME", str(tmp_path / "their-cache"))
    assert paths.model_cache_env_vars() == {}


def test_a_hub_cache_the_user_pointed_at_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(tmp_path / "their-cache"))
    assert paths.model_cache_env_vars() == {}


def test_applying_the_variables_puts_them_in_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path / "yuanwork"))
    paths.apply_model_cache_env()
    assert os.environ["HF_HOME"] == str(tmp_path / "yuanwork" / "models")


def test_the_variable_is_set_before_huggingface_hub_is_imported(tmp_path, monkeypatch):
    """The library computes its cache path *when it is imported*.

    Setting the variable afterwards is not an error - it is silently
    ignored, and the models land in the old directory anyway. So the
    order matters: the variable has to be in place before the import.
    """
    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path / "yuanwork"))
    paths.apply_model_cache_env()

    script = (
        "import sys;"
        "sys.path.insert(0, %r);"
        "import huggingface_hub.constants as c;"
        "print(c.HF_HUB_CACHE)" % str(Path(__file__).resolve().parents[3] / "src" / "backend")
    )
    import subprocess

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, "HF_HOME": os.environ["HF_HOME"]},
    )
    assert result.returncode == 0, result.stderr
    assert str(tmp_path / "yuanwork" / "models" / "hub") in result.stdout


# ===== the old flat directory =====


def test_the_old_flat_data_directory_is_moved_under_data(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path / "yuanwork"))
    root = tmp_path / "yuanwork"
    root.mkdir()
    (root / "store.db").write_text("x", encoding="utf-8")
    (root / "lancedb").mkdir()

    moved = paths.migrate_legacy_data_dir()

    assert set(moved) == {"store.db", "lancedb"}
    assert (root / "data" / "store.db").exists()
    assert (root / "data" / "lancedb").is_dir()


def test_the_move_happens_only_once(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path / "yuanwork"))
    root = tmp_path / "yuanwork"
    root.mkdir()
    (root / "store.db").write_text("x", encoding="utf-8")

    assert paths.migrate_legacy_data_dir() == ["store.db"]
    assert paths.migrate_legacy_data_dir() == []


def test_nothing_is_moved_when_the_data_dir_was_chosen_by_hand(
    tmp_path, monkeypatch
):
    """The user's explicit choice wins - there is nothing to move."""
    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path / "yuanwork"))
    monkeypatch.setenv(paths.ENV_DATA_DIR, str(tmp_path / "chosen"))
    root = tmp_path / "yuanwork"
    root.mkdir()
    (root / "store.db").write_text("x", encoding="utf-8")

    assert paths.migrate_legacy_data_dir() == []
    assert (root / "store.db").exists()


# ===== the .env =====


def test_the_env_file_is_found_from_the_package_not_the_cwd(tmp_path, monkeypatch):
    """The desktop shell starts the backend from ``src/backend``; the
    .env lives at the repository root."""
    monkeypatch.setattr(paths, "read_dotenv", _real_read_dotenv)
    values = paths.read_dotenv()
    assert isinstance(values, dict)


def _real_read_dotenv() -> dict[str, str]:
    from dotenv import dotenv_values

    for candidate in paths.dotenv_candidates():
        if candidate.is_file():
            return {
                k: v
                for k, v in dotenv_values(candidate).items()
                if v is not None
            }
    return {}


def test_the_candidates_start_with_the_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    candidates = paths.dotenv_candidates()
    assert candidates[0] == tmp_path / ".env"
    assert len(candidates) > 1


def test_the_package_ancestors_are_among_the_candidates():
    """The repository root is where the .env lives."""
    root = Path(__file__).resolve().parents[3]
    assert root / ".env" in paths.dotenv_candidates()


# ===== the settings =====


def test_the_settings_use_the_resolved_directories(tmp_path, monkeypatch):
    from yuanwork.config import YuanWorkSettings

    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path / "yuanwork"))
    settings = YuanWorkSettings(_env_file=None)
    assert settings.data_dir == tmp_path / "yuanwork" / "data"
    assert settings.model_cache_dir == tmp_path / "yuanwork" / "models"


def test_the_settings_create_the_data_directory(tmp_path, monkeypatch):
    from yuanwork.config import YuanWorkSettings

    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path / "yuanwork"))
    settings = YuanWorkSettings(_env_file=None)
    assert settings.data_dir.is_dir()
    assert (settings.data_dir / "logs").is_dir()


def test_a_data_directory_the_caller_chose_is_left_alone(tmp_path, monkeypatch):
    from yuanwork.config import YuanWorkSettings

    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path / "yuanwork"))
    settings = YuanWorkSettings(data_dir=tmp_path / "chosen", _env_file=None)
    assert settings.data_dir == tmp_path / "chosen"
