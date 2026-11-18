"""TASK-077 / TASK-078 — 真机验收：工作域里的文件看得见了吗。

走应用自己的那条路：把真实工作域加进工作域 → 重建 → 报告

* 知识库里的文件数（建库前 / 建库后）
* 读不进来的文件（路径 + 原因）——侧栏那一节就是它
* 那几份读不出来的扫描件有没有进库（靠 OCR）

用法::

    .venv/Scripts/python.exe scripts/bench/real_workspace_check.py

环境变量：

* ``YUANWORK_CHECK_FOLDER`` —— 要检查的工作域（默认 ``D:/workspace``）
* ``YUANWORK_CHECK_PORT`` —— 后端端口（默认 8768）
* ``YUANWORK_CHECK_DATA_DIR`` —— 索引落点（默认 ``D:/yuanwork/latency-index``）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PORT = int(os.environ.get("YUANWORK_CHECK_PORT", "8768"))
BASE = f"http://127.0.0.1:{PORT}/api/v1"
WORKSPACE = os.environ.get("YUANWORK_CHECK_FOLDER", "D:/workspace")
DATA_DIR = os.environ.get("YUANWORK_CHECK_DATA_DIR", "D:/yuanwork/latency-index")

#: 那几份以前读不出来的文件（正文在文本框里 / 正文是图片）。
NEEDLES = (
    "课程作业",
    "用混合量热法测定冰的熔化热",
    "一阶电路的响应",
)


def call(
    path: str,
    method: str = "GET",
    payload: dict | None = None,
    timeout: float = 60.0,
) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def backend_is_up(timeout: float = 3.0) -> bool:
    try:
        call("/health", timeout=timeout)
        return True
    except (urllib.error.URLError, OSError):
        return False


def start_backend() -> subprocess.Popen:
    """拉起一个后端，指向同一个索引目录。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src" / "backend")
    env["YUANWORK_DATA_DIR"] = DATA_DIR
    env["YUANWORK_PORT"] = str(PORT)
    env["YUANWORK_PREWARM_EMBEDDING"] = "1"
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    log = (logs / f"real-{PORT}.log").open("w", encoding="utf-8")
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008  # DETACHED
    return subprocess.Popen(
        [
            str(ROOT / ".venv" / "Scripts" / "python.exe"),
            "-X", "utf8", "-m", "yuanwork", "--port", str(PORT),
        ],
        cwd=str(ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=flags,
    )


def wait_for_backend(timeout: float = 300.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if backend_is_up():
            return True
        time.sleep(1.0)
    return False


def main() -> int:
    if backend_is_up():
        print(f"后端已在 {BASE} 上，直接用它（索引 {DATA_DIR}）")
    else:
        process = start_backend()
        print(f"后端已拉起（pid {process.pid}），索引 {DATA_DIR}")
        if not wait_for_backend():
            print("后端没能在 300 秒内就绪")
            return 1

    entries = call("/workspace/entries").get("entries") or []
    print(f"工作域条目：{len(entries)} 个")
    if not any(
        (e.get("path") or "").replace("\\", "/").rstrip("/")
        == WORKSPACE.replace("\\", "/").rstrip("/")
        for e in entries
    ):
        added = call(
            "/workspace/folders", method="POST",
            payload={"path": WORKSPACE, "included": True},
        )
        print(f"已把 {added.get('path')} 加入工作域")

    before = call("/kb/stats")
    print(f"建库前：{before['total_files']} 个文件 / {before['total_chunks']} 个切片")

    # A build that is already running is watched instead of starting a
    # second one - two builds on one knowledge base fight each other.
    running = None
    try:
        latest = call("/ingest/status/latest", timeout=180.0)
        if latest.get("status") in ("pending", "running"):
            running = latest["job_id"]
    except Exception:  # noqa: BLE001 - no history is the same as no build
        running = None

    if running:
        job_id = running
        print(f"有一轮建库正在跑，等它：{job_id}")
    else:
        # 路由要先数一遍工作域覆盖的文件才回答——15 万个文件要一分多钟。
        job = call("/ingest/rebuild", method="POST", timeout=1800.0)
        job_id = job["job_id"]
        print(f"重建已启动：{job_id}（{job['files_queued']} 个文件）")

    started = time.time()
    status: dict = {}
    while True:
        # 建库时后端在数文件与算哈希，这段��间事件循环被占着，
        # 状态端点会晚一点才答——所以这里的超时给得宽。
        status = call(f"/ingest/status/{job_id}", timeout=180.0)
        if status["status"] in ("completed", "failed"):
            break
        if time.time() - started > 7200:
            print("等了两小时还没结束")
            return 1
        time.sleep(5.0)

    print(f"建库结束：{status['status']}，用时 {time.time() - started:.0f} 秒")
    after = call("/kb/stats")
    print(f"建库后：{after['total_files']} 个文件 / {after['total_chunks']} 个切片")

    failed = status.get("failed_files") or []
    print(f"读不进来的文件：{status.get('failed_total')} 个（清单里 {len(failed)} 条）")
    for item in failed[:20]:
        print(f"  - {item['path']}  →  {item['error']}")
    if len(failed) > 20:
        print(f"  …… 还有 {len(failed) - 20} 条")

    for needle in NEEDLES:
        rows = call(f"/kb/files?search={urllib.parse.quote(needle)}&limit=20")
        print(f"「{needle}」→ 库里 {rows['total']} 份")
        for row in rows["files"]:
            print(f"  · {row['file_name']}  {row['chunk_count']} 个切片")

    return 0


if __name__ == "__main__":
    sys.exit(main())
