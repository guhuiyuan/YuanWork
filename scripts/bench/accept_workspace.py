"""The acceptance run over the real workspace.

The synthetic corpus says what a build costs on a corpus with a known
shape. This one says what it costs on **the user's own documents** -
the only number that counts for the promise.

It takes the workspace, profiles its structure (without reading a word
of it), indexes it, and checks the promises:

* the whole build within two hours (AC-P-001)
* the long tail within a hundred minutes (AC-P-007)
* the build is complete when the long tail is searchable - the core
  layer's pages compile in the background (AC-F-026)
* the parsing, the embedding and the writing throughputs (AC-P-010,
  AC-P-011, AC-P-012)

    .venv/Scripts/python.exe scripts/bench/accept_workspace.py <工作域>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# The console on a Chinese Windows is GBK - the report carries Chinese
# and the ⚠. The script writes UTF-8 whatever the console says.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# The script is run from the repository root - the package lives under
# src/backend, and the benchmark modules next to this file.
_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_SCRIPTS = Path(__file__).resolve().parents[1]
for _path in (str(_BACKEND), str(_SCRIPTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from bench.workspace_profile import profile_workspace  # noqa: E402
from bench.run import TARGETS, check_targets, parse_rates, unmeasured  # noqa: E402


async def accept_workspace(workspace: Path, data_dir: Path) -> dict[str, Any]:
    """Index the workspace and say whether the promises held."""
    from yuanwork.db.sqlite import SQLiteStore
    from yuanwork.ingestion.batch import BatchIngestor
    from yuanwork.ingestion.pipeline import IngestionPipeline
    from yuanwork.memory.semantic import SemanticMemory

    profile = profile_workspace(workspace)
    print("=" * 60)
    print("真实工作域的结构画像")
    print("-" * 60)
    print(
        f"文件：{profile['files']}　页数：{profile['pages']}　"
        f"段落：{profile['paragraphs']}　表格：{profile['tables']}"
    )
    print(
        f"体量：{profile['total_bytes'] / 1024 / 1024:.1f} MB　"
        f"图片占比：{profile['image_ratio'] * 100:.1f}%"
    )
    print(f"大小分布：{profile['size_bytes']}")

    store = SQLiteStore(data_dir / "store.db")
    await store.initialize()
    semantic = SemanticMemory(data_dir / "lancedb")
    await semantic.initialize()

    pipeline = IngestionPipeline(
        sqlite_store=store,
        semantic_memory=semantic,
        llm_gateway=None,
    )

    started = time.perf_counter()
    ingestor = BatchIngestor(pipeline)
    try:
        # The build is finished when the long tail is searchable. The
        # core layer's pages are compiled in the background - they do
        # not hold the build up (AC-F-026).
        result = await ingestor.ingest(workspace, recursive=True)
    finally:
        elapsed = time.perf_counter() - started

    # The pages arrive afterwards. How long that takes is worth knowing,
    # but it is not part of the build's promise.
    pages_started = time.perf_counter()
    await ingestor.wait_for_background()
    pages_elapsed = time.perf_counter() - pages_started

    try:
        await semantic.close()
        await store.close()
    except Exception:
        pass

    timings = dict(getattr(pipeline, "timings", {}) or {})
    total_chunks = sum(
        entry.get("chunks", 0) for entry in result.get("files", [])
    )
    parse_seconds = timings.get("parse", 0.0)
    embed_seconds = timings.get("embed", 0.0)
    write_seconds = timings.get("write", 0.0)

    # The parsing time is the sum over the workers; the rate the build
    # delivers divides it by the number of workers that read at the
    # same time. A rate per core would say the build is four times
    # slower than it is.
    workers = int(getattr(pipeline, "build_workers", 1) or 1)

    report: dict[str, Any] = {
        "profile": profile,
        "measured": {
            "parse": parse_seconds > 0,
            "embed": embed_seconds > 0 and total_chunks > 0,
            "write": write_seconds > 0 and total_chunks > 0,
        },
        "indexed": result.get("indexed", 0),
        "skipped": result.get("skipped", 0),
        "errors": result.get("errors", 0),
        "chunks": total_chunks,
        "elapsed_seconds": round(elapsed, 3),
        "workers": workers,
        "timings": {k: round(v, 3) for k, v in timings.items()},
        "throughput": {
            **parse_rates(result.get("indexed", 0), parse_seconds, workers),
            "embed_chunks_per_second": (
                round(total_chunks / embed_seconds, 2) if embed_seconds else 0.0
            ),
            "write_chunks_per_second": (
                round(total_chunks / write_seconds, 2) if write_seconds else 0.0
            ),
            "files_per_minute": (
                round(result.get("indexed", 0) / elapsed * 60, 2) if elapsed else 0.0
            ),
        },
    }

    # AC-F-026: the build is done when the long tail is searchable. The
    # core layer's pages compile in the background - they do not hold
    # the build up.
    report["longtail_searchable"] = total_chunks > 0
    report["core_pages_seconds"] = round(pages_elapsed, 3)
    report["core_pages"] = 0
    try:
        pages = await store.list_wiki_pages(limit=1000)
        report["core_pages"] = len([p for p in pages if p.get("tier") == "core"])
    except Exception:
        pass

    return report


def print_report(report: dict[str, Any]) -> None:
    """What the user reads."""
    print()
    print("=" * 60)
    print("建库验收")
    print("-" * 60)
    print(
        f"入索引：{report['indexed']}　跳过：{report['skipped']}　"
        f"失败：{report['errors']}"
    )
    print(f"切片：{report['chunks']}　总时长：{report['elapsed_seconds']} 秒")
    print(f"长尾层可检索：{'是' if report['longtail_searchable'] else '否'}")
    print(
        f"核心层页面：{report['core_pages']} 个"
        f"（建库之后又用了 {report.get('core_pages_seconds')} 秒）"
    )
    print()
    print("吞吐：")
    for key, value in (report.get("throughput") or {}).items():
        print(f"  {key}: {value}")

    missed = check_targets(report)
    if not report["longtail_searchable"]:
        missed.append("长尾层没有可检索的内容")
    if report["elapsed_seconds"] / 60.0 > TARGETS["end_to_end_minutes"]:
        missed.append("端到端超过两小时")

    print()
    if missed:
        print("未达成的承诺：")
        for item in missed:
            print(f"  · {item}")
    else:
        print("所有承诺都达成了")

    not_measured = unmeasured(report)
    if not_measured:
        print()
        print("没有测到的环节：" + "、".join(not_measured))


async def main() -> int:
    parser = argparse.ArgumentParser(description="The workspace acceptance run")
    parser.add_argument("workspace", help="The workspace to index")
    parser.add_argument("--json", help="Write the report to this file as JSON")
    args = parser.parse_args()

    workspace = Path(args.workspace)
    if not workspace.exists():
        print(f"工作域不存在：{workspace}")
        return 2

    work = Path(tempfile.mkdtemp(prefix="yuanwork_accept_"))
    try:
        report = await accept_workspace(workspace, work / "data")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print_report(report)
    if args.json:
        Path(args.json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"报告已写入：{args.json}")

    return 0 if not check_targets(report) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
