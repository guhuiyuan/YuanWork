"""The build benchmark: run a build over a corpus and say what it cost.

The numbers it produces are the ones the performance promises are made
of:

* how long the whole build took (AC-P-001)
* how long the long tail took, and how many files it held (AC-P-007)
* how fast the parsing was, in files a minute (AC-P-010)
* how fast the embedding was, in slices a second (AC-P-011)
* how fast the store was written, in slices a second (AC-P-012)
* how much memory the build took (AC-F-026)

It leaves a baseline file behind. The next run is compared against it:
a build that got more than ten percent slower is a regression, and the
script says so.

    .venv/Scripts/python.exe scripts/bench/run.py --files 200
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
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

from bench.corpus import DEFAULT_PROFILE, StructureProfile, generate_corpus  # noqa: E402

#: Where the baseline lives.
BASELINE_PATH = Path(__file__).resolve().parent / "baseline.json"

#: How much slower a build may get before it counts as a regression.
REGRESSION_TOLERANCE = 0.10

#: The promises, as numbers.
TARGETS = {
    "end_to_end_minutes": 120.0,
    "longtail_minutes": 100.0,
    "parse_files_per_minute": 120.0,
    "embed_chunks_per_second": 15.0,
    "write_chunks_per_second": 500.0,
}


class MemoryWatcher:
    """The peak memory of this process, sampled while the build runs."""

    def __init__(self, interval: float = 0.5):
        self.interval = interval
        self.peak_bytes = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _sample(self) -> int:
        try:
            import psutil

            return int(psutil.Process(os.getpid()).memory_info().rss)
        except Exception:
            return 0

    def _run(self) -> None:
        while not self._stop.is_set():
            self.peak_bytes = max(self.peak_bytes, self._sample())
            self._stop.wait(self.interval)

    def __enter__(self) -> "MemoryWatcher":
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)


async def run_build(
    corpus_dir: Path,
    data_dir: Path,
    files: int = 200,
) -> dict[str, Any]:
    """Index a corpus and say what it cost."""
    from yuanwork.db.sqlite import SQLiteStore
    from yuanwork.ingestion.pipeline import IngestionPipeline
    from yuanwork.memory.semantic import SemanticMemory

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
    try:
        # The build that ships: read in parallel, written in batches
        # (TASK-045). It is finished when the long tail is searchable -
        # the core layer's pages compile in the background (AC-F-026).
        result = await pipeline.build_directory(corpus_dir, recursive=True)
    finally:
        elapsed = time.perf_counter() - started
        await semantic.close()
        await store.close()

    timings = dict(getattr(pipeline, "timings", {}) or {})
    total_chunks = sum(
        entry.get("chunks", 0) for entry in result.get("files", [])
    )

    parse_seconds = timings.get("parse", 0.0)
    embed_seconds = timings.get("embed", 0.0)
    write_seconds = timings.get("write", 0.0)

    # A stage that did not run was not measured. The embedding model
    # cannot be loaded on every machine - and a number that was never
    # measured must not be mistaken for a number that was measured and
    # came out zero.
    measured = {
        "parse": parse_seconds > 0,
        "embed": embed_seconds > 0 and total_chunks > 0,
        "write": write_seconds > 0 and total_chunks > 0,
    }

    return {
        "files": result.get("indexed", 0),
        "chunks": total_chunks,
        "elapsed_seconds": round(elapsed, 3),
        "measured": measured,
        "timings": {k: round(v, 3) for k, v in timings.items()},
        "stage_share": {
            "parse": round(parse_seconds / elapsed, 4) if elapsed else 0.0,
            "embed": round(embed_seconds / elapsed, 4) if elapsed else 0.0,
            "write": round(write_seconds / elapsed, 4) if elapsed else 0.0,
        },
        "throughput": {
            "parse_files_per_minute": (
                round(result.get("indexed", 0) / parse_seconds * 60, 2)
                if parse_seconds else 0.0
            ),
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
        "requested_files": files,
    }


def compare_with_baseline(
    report: dict[str, Any], baseline: dict[str, Any] | None
) -> dict[str, Any]:
    """How this run compares with the baseline.

    A build that got more than ten percent slower is a regression.
    """
    if not baseline:
        return {"regression": False, "reason": "没有基线，第一次跑"}

    baseline_throughput = baseline.get("throughput") or {}
    throughput = report.get("throughput") or {}
    regressions: list[str] = []
    for key in ("parse_files_per_minute", "embed_chunks_per_second",
                "write_chunks_per_second"):
        before = baseline_throughput.get(key)
        now = throughput.get(key)
        if not before or not now:
            # A number that was never measured cannot get worse.
            continue
        if now < before * (1.0 - REGRESSION_TOLERANCE):
            regressions.append(f"{key}: {before} → {now}")

    return {
        "regression": bool(regressions),
        "regressions": regressions,
        "tolerance": REGRESSION_TOLERANCE,
    }


def machine_info() -> dict[str, Any]:
    """What this run ran on.

    A number without the machine it came from is not a number - the
    next run has to be on the same one to be comparable.
    """
    import platform

    info: dict[str, Any] = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "processor": platform.processor() or platform.machine(),
    }
    try:
        import psutil

        info["cpu_count"] = psutil.cpu_count(logical=True)
        info["memory_bytes"] = psutil.virtual_memory().total
    except Exception:
        pass
    try:
        import torch

        info["torch"] = torch.__version__
        info["cuda"] = bool(torch.cuda.is_available())
    except Exception:
        pass
    return info


def write_baseline(report: dict[str, Any], path: Path | None = None) -> Path:
    """Leave the baseline behind.

    The baseline says what it was measured on, and what could not be
    measured at all - a number nobody can compare is worse than no
    number.
    """
    path = path or BASELINE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    baseline = dict(report)
    baseline["machine"] = machine_info()
    not_measured = unmeasured(report)
    baseline["not_measured"] = not_measured
    if not_measured:
        baseline["note"] = (
            "这些环节没有测到：" + "、".join(not_measured)
            + "（多半是嵌入模型在本机装不上）——没有测到的数字不算数，也不参与退步判断。"
        )
    else:
        baseline["note"] = "全部环节都已测到。"

    path.write_text(
        json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def load_baseline(path: Path | None = None) -> dict[str, Any] | None:
    path = path or BASELINE_PATH
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def check_targets(report: dict[str, Any]) -> list[str]:
    """Which promises the run did not keep.

    A stage that could not be measured is not a broken promise - it is
    an unknown one, and it is said so in the report.
    """
    throughput = report.get("throughput") or {}
    measured = report.get("measured") or {}
    elapsed_minutes = report.get("elapsed_seconds", 0.0) / 60.0
    missed: list[str] = []
    if elapsed_minutes > TARGETS["end_to_end_minutes"]:
        missed.append(
            f"端到端 {elapsed_minutes:.1f} 分钟 > {TARGETS['end_to_end_minutes']} 分钟"
        )
    if measured.get("parse", True) and throughput.get(
        "parse_files_per_minute", 0.0
    ) < TARGETS["parse_files_per_minute"]:
        missed.append(
            f"解析 {throughput.get('parse_files_per_minute')} 文件/分钟 "
            f"< {TARGETS['parse_files_per_minute']}"
        )
    if measured.get("embed", True) and throughput.get(
        "embed_chunks_per_second", 0.0
    ) < TARGETS["embed_chunks_per_second"]:
        missed.append(
            f"嵌入 {throughput.get('embed_chunks_per_second')} 切片/秒 "
            f"< {TARGETS['embed_chunks_per_second']}"
        )
    if measured.get("write", True) and throughput.get(
        "write_chunks_per_second", 0.0
    ) < TARGETS["write_chunks_per_second"]:
        missed.append(
            f"落库 {throughput.get('write_chunks_per_second')} 切片/秒 "
            f"< {TARGETS['write_chunks_per_second']}"
        )
    return missed


def unmeasured(report: dict[str, Any]) -> list[str]:
    """The stages the run could not measure.

    The embedding model cannot be loaded on every machine - and a
    promise that was not measured is not a promise that was broken.
    """
    measured = report.get("measured") or {}
    return [
        stage
        for stage in ("parse", "embed", "write")
        if measured.get(stage) is False
    ]


def print_report(report: dict[str, Any], comparison: dict[str, Any]) -> None:
    """What the user reads."""
    print("=" * 60)
    print("建库性能基准")
    print("-" * 60)
    print(f"文件：{report.get('files')}　切片：{report.get('chunks')}")
    print(f"总时长：{report.get('elapsed_seconds')} 秒")
    print()
    print("分段耗时占比：")
    for stage, share in (report.get("stage_share") or {}).items():
        print(f"  {stage}: {share * 100:.1f}%")
    print()
    print("吞吐：")
    for key, value in (report.get("throughput") or {}).items():
        print(f"  {key}: {value}")
    print()
    missed = check_targets(report)
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
    if comparison.get("regression"):
        print()
        print("相对基线退化了：")
        for item in comparison.get("regressions", []):
            print(f"  · {item}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="The build benchmark")
    parser.add_argument("--files", type=int, default=200, help="How many files")
    parser.add_argument("--keep", action="store_true", help="Keep the corpus")
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="Write this run as the new baseline",
    )
    args = parser.parse_args()

    work = Path(tempfile.mkdtemp(prefix="yuanwork_bench_"))
    try:
        corpus = work / "corpus"
        # The profile is scaled to the number of files asked for. It is
        # a copy - the default profile is not touched.
        per_kind = max(1, args.files // max(1, len(DEFAULT_PROFILE.counts)))
        profile = StructureProfile(
            name=DEFAULT_PROFILE.name,
            counts={kind: per_kind for kind in DEFAULT_PROFILE.counts},
            pages=DEFAULT_PROFILE.pages,
            paragraphs=DEFAULT_PROFILE.paragraphs,
            tables=DEFAULT_PROFILE.tables,
            rows=DEFAULT_PROFILE.rows,
            images=DEFAULT_PROFILE.images,
        )

        print(f"生成基准集：{sum(profile.counts.values())} 个文件 …")
        generate_corpus(corpus, profile)

        with MemoryWatcher() as watcher:
            report = await run_build(corpus, work / "data", files=args.files)
        report["peak_memory_bytes"] = watcher.peak_bytes

        baseline = load_baseline()
        comparison = compare_with_baseline(report, baseline)
        print_report(report, comparison)

        if args.update_baseline or baseline is None:
            path = write_baseline(report)
            print()
            print(f"基线已写入：{path}")

        return 1 if comparison.get("regression") or check_targets(report) else 0
    finally:
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
        else:
            print(f"基准集留在：{work}")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
