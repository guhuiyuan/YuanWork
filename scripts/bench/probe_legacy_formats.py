"""工作域里的旧版二进制格式，现在归到哪一档 —— 真机核对（TASK-091）。

真机工作域里有几十个 ``.doc`` / ``.ppt`` / ``.xls``。在 TASK-091 之前，
每次重建都去试它们、每次都失败，用户在侧栏看到的是「读不进来的文件」；
之后它们**不再被尝试**，归进「格式不支持」那一档 —— 理由说得清是哪种格式、
该另存成什么。

这一支脚本走的是建库前的那一步（把工作域分成三堆），不建库、不算哈希，
所以它不动索引。

**读者自己那本库不会被打开。** ``SQLiteStore.initialize`` 会在库里写下建表
语句与迁移 —— 那是往读者正在用的东西上写 DDL。这一支脚本先把库**复制**一份
（用 SQLite 自己的备份接口），再在副本上看。

用法::

    .venv/Scripts/python.exe scripts/bench/probe_legacy_formats.py

环境变量：

* ``YUANWORK_WORKSPACE_DB`` —— 索引落点里的那本库（默认 ``D:/yuanwork/data/store.db``）
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src" / "backend"))

from yuanwork.db.sqlite import SQLiteStore  # noqa: E402
from yuanwork.ingestion.parser.dispatcher import (  # noqa: E402
    LEGACY_BINARY_EXTENSIONS,
    unsupported_reason,
)
from yuanwork.ingestion.pipeline import IngestionPipeline  # noqa: E402
from yuanwork.workspace import Workspace  # noqa: E402

DEFAULT_DB = Path("D:/yuanwork/data/store.db")


def db_path() -> Path:
    raw = os.environ.get("YUANWORK_WORKSPACE_DB", "").strip()
    return Path(raw) if raw else DEFAULT_DB


def copy_library(source: Path, target: Path) -> None:
    """把库复制一份 —— 用 SQLite 自己的备份接口。

    手工复制文件会漏掉还在 WAL 里的页；直接开原库则会把建表语句与迁移
    写进读者正在用的那本库。
    """
    src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


async def partition(path: Path) -> dict[str, object]:
    """把真机工作域分成三堆：能读的、超限的、格式不支持的（在副本上看）。"""
    with tempfile.TemporaryDirectory(prefix="yuanwork-workspace-probe-") as tmp:
        copy = Path(tmp) / "store.db"
        copy_library(path, copy)
        store = SQLiteStore(copy)
        await store.initialize()
        try:
            files = await Workspace(store).list_files()
            pipeline = IngestionPipeline(sqlite_store=store, semantic_memory=None)
            ready, unsupported, oversized = pipeline.partition_workspace_files(files)
        finally:
            await store.close()
    return {
        "total": len(files),
        "ready": len(ready),
        "oversized": len(oversized),
        "unsupported": len(unsupported),
        "legacy": [
            item for item in unsupported
            if Path(item["path"]).suffix.lower() in LEGACY_BINARY_EXTENSIONS
        ],
        "reasons": Counter(item["reason"] for item in unsupported),
    }


def report(result: dict[str, object]) -> None:
    """把数出来的说清楚。"""
    print(f"库：{db_path()}")
    print(
        f"工作域 {result['total']} 个文件：能读 {result['ready']}、"
        f"超限 {result['oversized']}、格式不支持 {result['unsupported']}"
    )
    legacy = result["legacy"]
    print(f"旧版二进制格式（.doc/.ppt/.xls）：{len(legacy)} 个 —— **不再被尝试**")
    for item in legacy[:5]:
        print(f"  · {Path(item['path']).name}  →  {item['reason']}")
    if len(legacy) > 5:
        print(f"  …… 还有 {len(legacy) - 5} 个")
    print("不支持的理由：")
    for reason, count in result["reasons"].most_common():
        print(f"  {count:>6} 个  {reason}")


async def main() -> int:
    path = db_path()
    if not path.exists():
        print(f"库不在那儿：{path}")
        return 1
    report(await partition(path))
    return 0


def test_the_real_workspace_keeps_the_old_formats_apart() -> None:
    """真机工作域里，旧版二进制格式要落在「格式不支持」那一档。

    库不在那儿就跳过：这是真机核对，不是每个开发机上都要过的关卡。
    """
    path = db_path()
    if not path.exists():
        print(f"跳过：{path} 不在")
        return
    result = asyncio.run(partition(path))
    report(result)
    assert result["unsupported"], "工作域里一个不支持的格式都没有——分堆没在跑"
    for item in result["legacy"]:
        assert "旧版二进制格式" in item["reason"], item


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
