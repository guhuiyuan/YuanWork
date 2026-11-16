"""扫嵌入的批参数：在真实切片上量出这台机器该切多大的批。

三个问题，各扫一遍：

* **批多大** —— 批要补到它里面最长的那条，批越大跨得越远，每一条都要替最长的那条买单
* **读多长** —— `max_length` 换的是正确性（读不到的部分检索不到），也换速度
* **怎么分窗** —— 批内最长与最短的比值，多大合适

语料从真实工作域现解析出来，缓存在 ``data/bench_corpus.json``；模型走本地缓存，不联网。

    .venv/Scripts/python.exe -X utf8 scripts/bench/embedding_batches.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

ROOT = Path(__file__).resolve().parents[2]
for _path in (str(ROOT / "src" / "backend"), str(ROOT / "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from yuanwork.ingestion.chunker.structural_chunker import StructuralChunker  # noqa: E402
from yuanwork.ingestion.parser.dispatcher import ParserDispatcher  # noqa: E402
from yuanwork.memory.semantic import _batches_by_length  # noqa: E402

WORKSPACE = Path(os.environ.get("YUANWORK_BENCH_WORKSPACE", "D:/workspace"))
CORPUS = ROOT / "data" / "bench_corpus.json"
RESULT = ROOT / "data" / "bench_batches.txt"

SUPPORTED = {".md", ".txt", ".docx", ".pdf"}


def quiet_tqdm() -> None:
    """进度条会把输出搅乱。"""

    class _Null:
        """A progress bar that shows nothing but still yields its items."""

        def __call__(self, iterable=None, *args, **kwargs):
            if iterable is None:
                return self
            return iterable

        def __iter__(self):
            return iter(())

        def __getattr__(self, name):
            return lambda *a, **k: None

    for module in list(sys.modules.values()):
        if module is None:
            continue
        name = getattr(module, "__name__", "")
        if name.startswith("FlagEmbedding") and hasattr(module, "tqdm"):
            module.tqdm = _Null()


def collect_chunks(limit: int = 400) -> list[str]:
    """真实工作域里的切片——不是合成的，密度与长度分布是真的。"""
    if CORPUS.exists():
        return json.loads(CORPUS.read_text(encoding="utf-8"))

    dispatcher = ParserDispatcher()
    chunker = StructuralChunker()
    files = sorted(
        p for p in WORKSPACE.rglob("*")
        if p.is_file() and p.suffix.lower() in SUPPORTED
    )
    random.Random(20261116).shuffle(files)

    texts: list[str] = []
    for path in files:
        if len(texts) >= limit * 12:
            break
        parser = dispatcher.get_parser(path)
        if parser is None:
            continue
        try:
            blocks = asyncio.run(parser.parse(path, "bench"))
        except Exception:
            continue
        if not blocks:
            continue
        texts.extend(c["text"] for c in chunker.chunk(blocks, "bench"))

    CORPUS.parent.mkdir(parents=True, exist_ok=True)
    CORPUS.write_text(json.dumps(texts, ensure_ascii=False), encoding="utf-8")
    return texts


def windows(texts: list[str], size: int, ratio: float = 2.0) -> list[list[str]]:
    """分窗（跟语义记忆里的一样）。"""
    return [
        [texts[i] for i in positions]
        for positions in _batches_by_length(texts, size, ratio=ratio)
    ]


def measure(model, batches, max_length: int, label: str, rows: list[str]) -> None:
    chunks = sum(len(b) for b in batches)
    import torch

    torch.cuda.reset_peak_memory_stats()
    torch.cuda.synchronize()

    started = time.perf_counter()
    for batch in batches:
        model.encode(
            batch,
            batch_size=len(batch),
            max_length=max_length,
            return_dense=True,
            return_sparse=False,
            return_colbert_vecs=False,
        )
    torch.cuda.synchronize()
    seconds = time.perf_counter() - started
    peak = torch.cuda.max_memory_allocated() / 1024 ** 2
    row = (
        f"{label:44s} {chunks / seconds:7.1f} 切片/秒　{seconds:5.1f} 秒"
        f"　批数 {len(batches):4d}　均批 {chunks / max(len(batches), 1):6.1f}"
        f"　显存峰值 {peak:6.0f} MB"
    )
    rows.append(row)
    print(row, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=1200)
    args = parser.parse_args()

    texts = collect_chunks()
    lengths = sorted(len(t) for t in texts)
    total = len(lengths)
    over = sum(1 for n in lengths if n > 512)
    rows: list[str] = [
        f"语料：{total} 个切片",
        f"  长度 中位 {lengths[total // 2]}　p90 {lengths[int(total * 0.9)]}"
        f"　最大 {lengths[-1]}　平均 {sum(lengths) // total}",
        f"  超过 512 字符（会被 max_length=512 截断）：{over}"
        f"（{over / total * 100:.1f}%）",
    ]
    print("\n".join(rows), flush=True)
    sample = texts[: args.samples]

    quiet_tqdm()
    from FlagEmbedding import BGEM3FlagModel

    model = BGEM3FlagModel("BAAI/bge-m3", devices="cuda", use_fp16=True, max_length=2048)

    import torch

    rows.append(f"CUDA {torch.cuda.get_device_name(0)}　"
                f"显存 {torch.cuda.get_device_properties(0).total_memory / 1024 ** 3:.1f} GB")

    # 预热：第一次的前向要付 cuBLAS 初始化的钱
    model.encode(
        sample[:32], batch_size=32, max_length=512,
        return_dense=True, return_sparse=False, return_colbert_vecs=False,
    )
    torch.cuda.synchronize()
    rows.append("预热完毕")

    rows.append("")
    rows.append("=== A：批大小（分窗 ratio=2.0，max_length=512）===")
    for size in (32, 64, 128, 256, 512):
        measure(model, windows(sample, size), 512, f"分窗 batch={size}", rows)

    rows.append("")
    rows.append("=== B：max_length（分窗 batch=256）===")
    batches256 = windows(sample, 256)
    for max_length in (256, 512, 1024, 2048):
        measure(model, batches256, max_length, f"max_length={max_length}", rows)

    rows.append("")
    rows.append("=== C：分窗比（batch=256，max_length=512）===")
    for ratio in (1.25, 1.5, 2.0, 3.0, 10.0):
        measure(model, windows(sample, 256, ratio=ratio), 512, f"ratio={ratio}", rows)

    rows.append("")
    rows.append("=== D：排序+定长切批（对照，不分窗）===")
    order = sorted(range(len(sample)), key=lambda i: len(sample[i]))
    sorted_texts = [sample[i] for i in order]
    for size in (32, 64, 128, 256):
        batches = [sorted_texts[i : i + size] for i in range(0, len(sorted_texts), size)]
        measure(model, batches, 512, f"排序+定长 batch={size}", rows)

    RESULT.parent.mkdir(parents=True, exist_ok=True)
    RESULT.write_text("\n".join(rows), encoding="utf-8")
    print(f"\n结果写到 {RESULT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
