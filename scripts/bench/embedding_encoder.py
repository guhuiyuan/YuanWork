"""自有编码器 vs 嵌入库自己的路：同一批真实切片，同一次运行里对比。

嵌入库的 ``encode()`` 先跑一遍模型去探显存（结果丢掉），再把同一批跑一遍——
所以这条对比量的是「那一次重复计算」到底值多少。

    .venv/Scripts/python.exe -X utf8 scripts/bench/embedding_encoder.py
"""

from __future__ import annotations

import json
import os
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

from yuanwork.memory.encoder import SlicedEncoder  # noqa: E402
from yuanwork.memory.semantic import _batches_by_length  # noqa: E402

CORPUS = ROOT / "data" / "bench_corpus.json"
RESULT = ROOT / "data" / "bench_encoder.txt"


def quiet_tqdm() -> None:
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


def main() -> int:
    if not CORPUS.exists():
        print(f"没有语料缓存：{CORPUS}\n先跑 scripts/bench/embedding_batches.py")
        return 2

    texts = json.loads(CORPUS.read_text(encoding="utf-8"))
    sample = texts[:1200]
    rows = [f"语料：{len(sample)} 个切片"]

    quiet_tqdm()
    from FlagEmbedding import BGEM3FlagModel

    model = BGEM3FlagModel("BAAI/bge-m3", devices="cuda", use_fp16=True, max_length=512)

    import torch

    rows.append(f"CUDA {torch.cuda.get_device_name(0)}")

    encoder = SlicedEncoder(model, "cuda", 512)
    encoder.encode(sample[:32])  # 预热
    torch.cuda.synchronize()
    rows.append("预热完毕")

    def timed(fn, label):
        torch.cuda.reset_peak_memory_stats()
        torch.cuda.synchronize()
        started = time.perf_counter()
        fn()
        torch.cuda.synchronize()
        seconds = time.perf_counter() - started
        peak = torch.cuda.max_memory_allocated() / 1024 ** 2
        row = (
            f"{label:44s} {len(sample) / seconds:7.1f} 切片/秒　{seconds:5.1f} 秒"
            f"　显存峰值 {peak:6.0f} MB"
        )
        rows.append(row)
        print(row, flush=True)
        return len(sample) / seconds

    windows = [
        [sample[i] for i in positions]
        for positions in _batches_by_length(sample, 32)
    ]

    def library():
        for batch in windows:
            model.encode(
                batch, batch_size=len(batch), max_length=512,
                return_dense=True, return_sparse=False, return_colbert_vecs=False,
            )

    rows.append("")
    rows.append("=== 库自己的路（含被丢弃的探显存前向）===")
    old = timed(library, "model.encode，分窗 32")

    rows.append("")
    rows.append("=== 自有编码器（自己前向，无重复计算）===")
    new = timed(lambda: encoder.encode(sample), "自有编码器，默认")

    rows.append("")
    rows.append("=== 自有编码器：每批多少条 ===")
    for cap in (16, 32, 64, 128, 256):
        timed(lambda c=cap: encoder.encode(sample, c), f"每批至多 {cap} 条")

    rows.append("")
    rows.append(f"快 {new / old:.2f} 倍（{old:.1f} → {new:.1f} 切片/秒）")
    RESULT.parent.mkdir(parents=True, exist_ok=True)
    RESULT.write_text("\n".join(rows), encoding="utf-8")
    print(f"\n结果写到 {RESULT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
