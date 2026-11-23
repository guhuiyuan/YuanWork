"""预热与第一个问题撞车时，模型只该加载一次（AC-F-004）。

真机上撞见过：后端启动时会在后台预热嵌入模型（`YUANWORK_PREWARM_EMBEDDING=1`），
读者在预热还在跑的时候问了第一个问题——那条路看到「模型还没加载」，于是
**又加载了一次**：显卡上两份 2.3 GB 的模型，编码器与模型字段可能各指向一份，
查询向量的精度就错了，稠密检索直接抛「mat1 and mat2 must have the same
dtype, but got Half and Float」。检索把异常咽下去（只记一条告警），
于是应用对**明明写着答案的文件**说「证据不足」。

所以：加载要**排队**。等着的那个用等来的模型，不自己再加载一份。
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

from yuanwork.memory import semantic as semantic_module
from yuanwork.memory.semantic import SemanticMemory


class _FakeModel:
    """A stand-in for the embedding model.

    The encoder knows a stand-in from the real thing (there is no
    ``model`` attribute to move onto the device), and this test only
    cares about **how many times** the model was loaded.
    """


def _a_semantic_memory() -> SemanticMemory:
    semantic = SemanticMemory(
        data_dir=Path("."), embedding_device="cpu"
    )
    semantic.embedding_quantize = "none"
    return semantic


def test_two_loads_at_the_same_time_load_the_model_once(monkeypatch):
    """预热与第一个问题撞车时，模型只该加载一次。"""
    calls: list[str] = []
    gate = threading.Event()

    def fake_load(loader, name, **kwargs):
        calls.append(name)
        # The real load takes seconds; the second caller must wait for
        # it rather than start its own.
        gate.wait(timeout=5)
        return _FakeModel()

    monkeypatch.setattr(
        semantic_module, "_import_flag_embedding", lambda: object
    )
    monkeypatch.setattr(semantic_module, "load_model_offline_first", fake_load)

    semantic = _a_semantic_memory()

    async def go():
        first = asyncio.create_task(semantic.load_embedding_model_async())
        second = asyncio.create_task(semantic.load_embedding_model_async())
        # Let the second caller reach the lock before the first load ends.
        await asyncio.sleep(0.05)
        gate.set()
        await asyncio.gather(first, second)

    asyncio.run(go())

    assert len(calls) == 1, f"the model was loaded {len(calls)} times"
    assert semantic.is_embedding_loaded()


def test_the_second_caller_waits_for_the_first_load(monkeypatch):
    """等着的那一个不能自己再加载一份 —— 它要的是等来的那个模型。"""
    calls: list[str] = []

    def fake_load(loader, name, **kwargs):
        calls.append(name)
        time.sleep(0.2)
        return _FakeModel()

    monkeypatch.setattr(
        semantic_module, "_import_flag_embedding", lambda: object
    )
    monkeypatch.setattr(semantic_module, "load_model_offline_first", fake_load)

    semantic = _a_semantic_memory()

    async def go():
        await asyncio.gather(
            semantic.load_embedding_model_async(),
            semantic.load_embedding_model_async(),
            semantic.load_embedding_model_async(),
        )

    asyncio.run(go())

    assert len(calls) == 1
    assert semantic.is_embedding_loaded()


def test_the_model_is_loaded_again_after_a_failed_load(monkeypatch):
    """加载失败不能把门锁死 —— 下一次还要能再试。"""
    calls: list[str] = []

    def failing_load(loader, name, **kwargs):
        calls.append(name)
        raise OSError("the model is not in the cache")

    monkeypatch.setattr(
        semantic_module, "_import_flag_embedding", lambda: object
    )
    monkeypatch.setattr(semantic_module, "load_model_offline_first", failing_load)

    semantic = _a_semantic_memory()

    async def go():
        for _ in range(2):
            try:
                await semantic.load_embedding_model_async()
            except OSError:
                pass

    asyncio.run(go())

    assert len(calls) == 2, "a failed load locked the model out"
    assert semantic.is_embedding_loaded() is False
