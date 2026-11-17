"""加载模型：从本地缓存来。

模型下载过一次就在缓存里（ADR-045：缓存归项目管，在它的根目录下）。
再去问 Hub 一次不是白问的：**连接挂住而不是被拒**的时候，要 **57 秒**
然后抛超时错误——而且失败的不是模型，是那个库不带 `local_files_only`
加载的 tokenizer。

后果不是「慢一点」：建库的**每一批**都失败（实测一批 267 个切片地丢），
索引几乎是空的，而建库照样报「完成」。

所以：第一次尝试完全离线，只有缓存里没有才走网络。
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def load_model_offline_first(loader: Any, model_name: str, **kwargs: Any) -> Any:
    """Load a model from the local cache, and only then from the network.

    Measured on the benchmark machine: offline **2.3 seconds** and it
    works; online **57.2 seconds** and then an error - and with the
    error, every batch of the build was lost.

    The offline flag belongs to the process, not to one load, so it is
    put back the way it was.
    """
    import huggingface_hub.constants as hub_constants

    was_offline = hub_constants.HF_HUB_OFFLINE
    was_env = os.environ.get("HF_HUB_OFFLINE")

    def restore() -> None:
        hub_constants.HF_HUB_OFFLINE = was_offline
        if was_env is None:
            os.environ.pop("HF_HUB_OFFLINE", None)
        else:
            os.environ["HF_HUB_OFFLINE"] = was_env

    hub_constants.HF_HUB_OFFLINE = True
    os.environ["HF_HUB_OFFLINE"] = "1"
    try:
        try:
            return loader(model_name, **kwargs)
        except Exception as first_error:  # noqa: BLE001 - it may just not be cached
            logger.info(
                "模型不在本地缓存里（%s），改用网络：%r", model_name, first_error
            )
            restore()
            return loader(model_name, **kwargs)
    finally:
        restore()
