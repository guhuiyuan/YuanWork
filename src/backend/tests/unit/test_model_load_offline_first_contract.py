"""模型从本地缓存加载，不依赖网络（TASK-069）。

模型下载过一次就在缓存里（ADR-045：缓存归项目管，在根目录下）。再去问
Hub 一次不是白问的：**连接挂住而不是被拒，要 57 秒然后失败**——而且失败
的不是模型，是那个库不带 `local_files_only` 加载的 tokenizer。后果是
**建库的每一批都丢了**。

所以：先离线，只有缓存里没有才联网。
"""

from __future__ import annotations

import os

import pytest

from yuanwork.utils.model_load import load_model_offline_first


class _Recorder:
    """A loader that records whether it was called offline or online."""

    def __init__(self, fail_offline: bool = True) -> None:
        self.fail_offline = fail_offline
        self.calls: list[bool] = []

    def __call__(self, model_name: str, **kwargs):
        import huggingface_hub.constants as hub_constants

        offline = bool(hub_constants.HF_HUB_OFFLINE)
        self.calls.append(offline)
        if offline and self.fail_offline:
            raise OSError("not in the cache")
        return f"model:{model_name}:{'offline' if offline else 'online'}"


def test_a_cached_model_is_loaded_without_the_network():
    loader = _Recorder(fail_offline=False)
    result = load_model_offline_first(loader, "BAAI/bge-m3")
    assert result == "model:BAAI/bge-m3:offline"
    assert loader.calls == [True]


def test_a_model_that_is_not_cached_is_looked_for():
    """The first attempt is offline; only if it fails does the network
    get asked - otherwise a fresh install could never download."""
    loader = _Recorder(fail_offline=True)
    result = load_model_offline_first(loader, "BAAI/bge-m3")
    assert result == "model:BAAI/bge-m3:online"
    assert loader.calls == [True, False]


def test_the_offline_flag_is_restored_afterwards():
    """The setting belongs to the process, not to one load."""
    import huggingface_hub.constants as hub_constants

    before = hub_constants.HF_HUB_OFFLINE
    load_model_offline_first(_Recorder(fail_offline=True), "BAAI/bge-m3")
    assert hub_constants.HF_HUB_OFFLINE is before
    assert os.environ.get("HF_HUB_OFFLINE") in (None, "0", "1")


def test_the_settings_of_the_process_are_not_clobbered():
    """A user who set the flag themselves keeps it."""
    import huggingface_hub.constants as hub_constants

    hub_constants.HF_HUB_OFFLINE = False
    os.environ["HF_HUB_OFFLINE"] = "0"
    try:
        load_model_offline_first(_Recorder(fail_offline=True), "BAAI/bge-m3")
        assert os.environ["HF_HUB_OFFLINE"] == "0"
    finally:
        os.environ.pop("HF_HUB_OFFLINE", None)
