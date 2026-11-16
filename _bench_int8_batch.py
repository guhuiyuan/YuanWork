"""Does int8 need a big batch to pay off?

The first measurement said int8 was about twice as fast. The second
— with the batches cut into windows — said it was slower. Both were
real; the difference is the batch size. int8's arithmetic is cheaper,
but quantizing the activations costs something per call, and a small
batch does not spread that over many slices.

This measures the two knobs against each other: fp32 and int8, with
a big fixed batch and with the windows.

    .venv/Scripts/python.exe _bench_int8_batch.py --chunks 128
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "src" / "backend"))


async def real_chunks(count: int) -> list[dict]:
    from yuanwork.ingestion.chunker.structural_chunker import StructuralChunker
    from yuanwork.ingestion.parser.dispatcher import ParserDispatcher

    dispatcher = ParserDispatcher()
    chunker = StructuralChunker()
    workspace = Path("D:/workspace")
    wanted = {".md": 0, ".txt": 0, ".pdf": 0, ".docx": 0, ".pptx": 0}
    chunks: list[dict] = []

    async def take(path: Path) -> None:
        parser = dispatcher.get_parser(path)
        if parser is None:
            return
        try:
            blocks = await parser.parse(path, "doc-bench")
        except Exception:
            return
        for chunk in chunker.chunk(blocks, "doc-bench"):
            chunks.append(chunk)
            if len(chunks) >= count:
                return

    for path in sorted(workspace.rglob("*")):
        if len(chunks) >= count:
            break
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix in wanted and wanted[suffix] < 12:
            wanted[suffix] += 1
            await take(path)

    return chunks[:count]


async def measure(memory, batches, rounds=2) -> float:
    best = 1e9
    for _round in range(rounds):
        started = time.perf_counter()
        for batch in batches:
            await memory._embed_one_batch(batch, False, memory.embedding_timeout)
        best = min(best, time.perf_counter() - started)
    return best


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chunks", type=int, default=128)
    args = parser.parse_args()

    from yuanwork.memory.semantic import SemanticMemory, _batches_by_length

    chunks = await real_chunks(args.chunks)
    texts = [c["text"] for c in chunks]
    print(f"{len(texts)} 片真实切片")

    # Two ways of cutting the same slices into batches.
    windowed = [
        [texts[p] for p in positions]
        for positions in _batches_by_length(texts, 32)
    ]
    sorted_all = sorted(texts, key=len)
    big = [sorted_all[i : i + 32] for i in range(0, len(sorted_all), 32)]

    def padding(batches):
        total = padded = 0
        for batch in batches:
            longest = max(len(t) for t in batch)
            total += longest * len(batch)
            padded += sum(len(t) for t in batch)
        return 1 - padded / total if total else 0.0

    print(f"  分窗：{len(windowed)} 批，padding {padding(windowed) * 100:.1f}%")
    print(f"  大批：{len(big)} 批，padding {padding(big) * 100:.1f}%")
    print()

    fp32 = SemanticMemory(Path(tempfile.mkdtemp(prefix="yuanwork_i8a_")) / "lancedb")
    await fp32.load_embedding_model_async()
    int8 = SemanticMemory(Path(tempfile.mkdtemp(prefix="yuanwork_i8b_")) / "lancedb")
    int8.embedding_quantize = "int8"
    await int8.load_embedding_model_async()

    for memory in (fp32, int8):
        await memory._embed_one_batch(windowed[0], False, memory.embedding_timeout)

    print("=" * 60)
    print("int8 与批大小的关系")
    print("=" * 60)
    for label, batches in (("分窗", windowed), ("大批 32", big)):
        f = await measure(fp32, batches)
        q = await measure(int8, batches)
        print(f"  {label:8s} fp32 {f:6.2f} 秒（{len(texts)/f:5.2f} 片/秒）"
              f"　int8 {q:6.2f} 秒（{len(texts)/q:5.2f} 片/秒）"
              f"　int8 快 {f/q:.2f} 倍")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
