"""图谱里到底有没有关系 —— 真机核对（TASK-093）。

TASK-093 的验收标准里有一条是「真机核对：``GET /api/v1/wiki/graph`` 的
``edges`` 长度大于 0」。这一支脚本走的是同一条路：不经过 HTTP，直接拿
真实索引目录里的那本库建一次图，把它数出来。

* 节点多少个、都是什么类型
* 边多少条、都是什么类型
* 有没有指向不存在节点的边（画图库遇到这种节点会抛）
* 有多少节点一个边也没有（孤立的散点）

**读者自己那本库不会被打开。** ``SQLiteStore.initialize`` 会在库里写下建表
语句与迁移 —— 那是往读者正在用的东西上写 DDL。这一支脚本先把库**复制**一份
（用 SQLite 自己的备份接口，WAL 里还没落盘的页也会一起过来），再在副本上
建图。副本读不到时直接说读不到，不去动原库。

用法::

    .venv/Scripts/python.exe scripts/bench/probe_graph_edges.py

环境变量：

* ``YUANWORK_GRAPH_DB`` —— 要看的库（默认 ``D:/yuanwork/data/store.db``）
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
from yuanwork.wiki.graph import WikiGraph  # noqa: E402

DEFAULT_DB = Path("D:/yuanwork/data/store.db")


def db_path() -> Path:
    """要看的库在哪。"""
    raw = os.environ.get("YUANWORK_GRAPH_DB", "").strip()
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


async def probe(path: Path) -> dict[str, object]:
    """建一次图，把它数出来（在副本上建，读者那本库不动）。"""
    with tempfile.TemporaryDirectory(prefix="yuanwork-graph-probe-") as tmp:
        copy = Path(tmp) / "store.db"
        copy_library(path, copy)
        store = SQLiteStore(copy)
        await store.initialize()
        try:
            graph = await WikiGraph(store).build_graph()
        finally:
            await store.close()

    nodes = graph["nodes"]
    edges = graph["edges"]
    known = {str(node["id"]) for node in nodes}
    dangling = [
        edge
        for edge in edges
        if str(edge["source"]) not in known or str(edge["target"]) not in known
    ]
    connected = {
        str(edge["source"]) for edge in edges
    } | {str(edge["target"]) for edge in edges}
    isolated = [node for node in nodes if str(node["id"]) not in connected]
    return {
        "nodes": nodes,
        "edges": edges,
        "node_types": dict(Counter(node["type"] for node in nodes)),
        "edge_types": dict(Counter(edge["type"] for edge in edges)),
        "dangling": dangling,
        "isolated": isolated,
    }


def report(result: dict[str, object]) -> None:
    """把数出来的说清楚。"""
    nodes = result["nodes"]
    edges = result["edges"]
    print(f"库：{db_path()}")
    print(f"节点 {len(nodes)} 个，边 {len(edges)} 条")
    print("节点类型：", result["node_types"])
    print("边类型：", result["edge_types"])
    print("指向不存在节点的边：", len(result["dangling"]))
    isolated = result["isolated"]
    print(f"孤立的节点：{len(isolated)}")
    for node in isolated[:8]:
        print(f"  · {node['label']}（{node['type']}）")
    if edges:
        print("前 10 条边：")
        labels = {str(node["id"]): node["label"] for node in nodes}
        for edge in edges[:10]:
            print(
                f"  {labels.get(str(edge['source']), edge['source'])}"
                f" --{edge['type']}-->"
                f" {labels.get(str(edge['target']), edge['target'])}"
            )


async def main() -> int:
    path = db_path()
    if not path.exists():
        print(f"库不在那儿：{path}")
        return 1
    report(await probe(path))
    return 0


def test_the_real_graph_has_relations() -> None:
    """真机上的那张图要有边——0 条边就是「图谱没有作用」。

    这条检查的正式位置在 ``src/backend/tests/unit/test_wiki_graph_edges.py``
    （那里用的是 ``YUANWORK_GRAPH_DB`` 指名的库，没有就跳过）；这一支只是
    手工跑时顺手看一眼，所以不重复断言。
    """
    path = db_path()
    if not path.exists():
        print(f"跳过：{path} 不在")
        return
    report(asyncio.run(probe(path)))


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
