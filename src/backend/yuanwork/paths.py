"""本项目可写状态的落点。

两件事必须在「进程最早期」定下来，晚一步就来不及：

1. **数据目录**（索引、日志、审计）与**模型缓存目录**——后者是本项目
   下载的（bge-m3 加重排模型，4.7 GB），本来落在 HuggingFace 自己的
   默认目录里，项目管不着它。
2. **模型缓存目录要写进 `HF_HOME`**——huggingface_hub 的缓存位置是
   **导入时**算出来的常量，导入之后再设环境变量**不报错也不生效**。
   所以这件事在 ``yuanwork/__init__.py`` 里做，那时还没有人 import 过它。

落点的规则只有一条：

    根目录 = ``YUANWORK_HOME`` 或 项目根 或 ``~/.yuanwork``
    数据目录 = ``YUANWORK_DATA_DIR`` 或 根目录下的 ``data/``
    模型缓存 = ``YUANWORK_MODEL_CACHE_DIR`` 或 根目录下的 ``models/``

「项目根」是含有 ``src/backend`` 的那一层——**从包的位置往上找，不靠
当前工作目录**。这一条是刻意的：数据目录此前是相对路径（``.env`` 里的
``./data``），而后端在三种启动方式下的当前目录各不相同（桌面端是
``src/backend``，命令行可能是仓库根，也可能是 ``src/backend``），于是
**同一个项目落到了三个不同的目录**——实测 ``src/backend/data`` 与仓库
根的 ``data/`` 各躺着一份 store.db。

相对路径也按项目根解析，不按当前目录：``.env`` 里的 ``./data`` 说的是
「这个项目的数据」，不是「我现在站在哪」。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

# 包的目录：src/backend/yuanwork/
_PACKAGE_ROOT = Path(__file__).resolve().parent

ENV_HOME = "YUANWORK_HOME"
ENV_DATA_DIR = "YUANWORK_DATA_DIR"
ENV_MODEL_CACHE = "YUANWORK_MODEL_CACHE_DIR"
ENV_PROJECT_ROOT = "YUANWORK_PROJECT_ROOT"

#: 老版本把索引直接放在根目录下（store.db 等），现在搬到 data/ 下面。
LEGACY_DATA_NAMES = (
    "store.db",
    "store.db-wal",
    "store.db-shm",
    "lancedb",
    "fts",
    "logs",
)


def dotenv_candidates() -> list[Path]:
    """Places a .env file may live, most specific first.

    The project layout is ``<root>/src/backend/yuanwork/...`` and the
    .env lives at ``<root>/.env``. The desktop app starts the backend
    with cwd = ``src/backend``, so a cwd-relative lookup alone would miss
    the file.
    """
    candidates = [Path.cwd() / ".env"]
    for parent in _PACKAGE_ROOT.parents[:6]:
        candidates.append(parent / ".env")
    return candidates


def read_dotenv() -> dict[str, str]:
    """The first .env that exists, as a dict (empty when there is none).

    The walk-up from the package matters more than the one from the
    working directory: the desktop shell runs the backend from
    ``src/backend``, and the project's .env is at the repository root.
    """
    try:
        from dotenv import dotenv_values
    except ImportError:
        return {}
    for path in dotenv_candidates():
        if not path.is_file():
            continue
        try:
            values = dotenv_values(path)
        except OSError:
            continue
        return {k: v for k, v in values.items() if v is not None}
    return {}


def load_dotenv_into_environ() -> int:
    """Copy .env into os.environ without overwriting what the user exported.

    pydantic-settings only consumes .env for its own field resolution; it
    does not mutate os.environ. The LLM gateway (and any other library
    that reads env vars directly) needs the values in os.environ.
    """
    loaded = 0
    for path in dotenv_candidates():
        if not path.is_file():
            continue
        try:
            from dotenv import dotenv_values
        except ImportError:
            return loaded
        try:
            values = dotenv_values(path)
        except OSError:
            continue
        for key, value in values.items():
            if value is None:
                continue
            if key not in os.environ and key.upper() not in os.environ:
                os.environ[key] = value
                os.environ.setdefault(key.upper(), value)
                loaded += 1
    return loaded


def _lookup(name: str, dotenv: dict[str, str] | None = None) -> str | None:
    """环境变量优先，其次 .env（大小写不敏感，与配置类一致）。"""
    for candidate in (name, name.upper()):
        value = os.environ.get(candidate)
        if value and value.strip():
            return value.strip()
    if dotenv is None:
        dotenv = read_dotenv()
    for candidate in (name, name.upper()):
        value = dotenv.get(candidate)
        if value and value.strip():
            return value.strip()
    return None


def project_root() -> Path | None:
    """The directory that holds ``src/backend`` — found from the package.

    Not from the working directory: that is the whole point. The desktop
    shell starts the backend from ``src/backend``; the CLI is often run
    from the repository root. The checkout is the same either way.
    """
    explicit = _lookup(ENV_PROJECT_ROOT)
    if explicit:
        return Path(explicit).expanduser().resolve()
    for parent in _PACKAGE_ROOT.parents[:6]:
        if (parent / "src" / "backend").is_dir():
            return parent
    return None


def _resolve(raw: str) -> Path:
    """Turn a configured path into an absolute one.

    Relative paths are resolved against the project root, not against the
    working directory: ``./data`` in the .env means "this project's data",
    not "wherever the process happens to be standing".
    """
    return resolve_path(Path(raw))


def resolve_path(path: Path | str) -> Path:
    """An absolute path, with a relative one resolved against the project root."""
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    base = project_root() or Path.cwd()
    return (base / candidate).resolve()


def root_dir() -> Path:
    """The root every piece of writable state hangs under."""
    explicit = _lookup(ENV_HOME)
    if explicit:
        return _resolve(explicit)
    found = project_root()
    if found is not None:
        return found
    return (Path.home() / ".yuanwork").resolve()


def data_dir() -> Path:
    """Where the index, the logs and the audit log live."""
    explicit = _lookup(ENV_DATA_DIR)
    if explicit:
        return _resolve(explicit)
    return root_dir() / "data"


def model_cache_dir() -> Path:
    """Where the models this project downloads are kept."""
    explicit = _lookup(ENV_MODEL_CACHE)
    if explicit:
        return _resolve(explicit)
    return root_dir() / "models"


def model_cache_env_vars() -> dict[str, str]:
    """The environment variables that point huggingface_hub at our cache.

    Empty when the user has already pointed it somewhere themselves —
    that is their choice, not ours to change. Empty also when the cache
    is already where it should be.
    """
    if os.environ.get("HF_HOME") or os.environ.get("HUGGINGFACE_HUB_CACHE"):
        return {}
    target = model_cache_dir()
    return {
        "HF_HOME": str(target),
        "HUGGINGFACE_HUB_CACHE": str(target / "hub"),
    }


def apply_model_cache_env() -> dict[str, str]:
    """Put the model cache where we want it — before huggingface_hub is imported.

    Returns the variables that were set. Calling this twice is harmless.
    """
    applied: dict[str, str] = {}
    for key, value in model_cache_env_vars().items():
        os.environ.setdefault(key, value)
        applied[key] = value
    return applied


def migrate_legacy_data_dir() -> list[str]:
    """Move an old flat data directory under ``data/``.

    Earlier versions kept the index directly in the root directory
    (``store.db`` and friends). The root now holds ``data/`` and
    ``models/``. The move happens once, at startup, and only when the
    new location does not exist yet — nothing is overwritten.
    """
    root = root_dir()
    target = root / "data"
    # Only when the data directory is the default one. If the user
    # pointed YUANWORK_DATA_DIR somewhere else, that is their choice
    # and there is nothing to move.
    if data_dir() != target:
        return []
    if target.exists():
        return []
    legacy = [name for name in LEGACY_DATA_NAMES if (root / name).exists()]
    if not legacy:
        return []
    target.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    for name in legacy:
        try:
            shutil.move(str(root / name), str(target / name))
        except OSError:
            continue
        moved.append(name)
    return moved
