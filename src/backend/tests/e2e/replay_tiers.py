"""The acceptance replay for the two layers.

Replays the tier promises against a real store and prints what the
user would see: the pages a core document was compiled into, the
conclusions with the place they were read from, the answer that leans
on them - and the fact that the long tail was never searched.

It is the milestone check: run it and read the output - if a promise
does not hold, it says so and the script fails.

    .venv/Scripts/python.exe src/backend/tests/e2e/replay_tiers.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from typing import Any

# The console on a Chinese Windows is GBK, and the answer carries a ⚠ -
# printing it would die. The script writes UTF-8 whatever the console
# says.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# The script is run from the repository root - the package lives under
# src/backend, and the harness next to this file.
_BACKEND = Path(__file__).resolve().parents[2]
for _path in (str(_BACKEND), str(_BACKEND / "tests")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from e2e.harness import (  # noqa: E402
    RecordingLongTail,
    RecordingSemantic,
    TieredLLM,
    make_tiered_agent,
)
from yuanwork.db.sqlite import SQLiteStore  # noqa: E402
from yuanwork.ingestion.pipeline import IngestionPipeline  # noqa: E402
from unit.ingestion_fixtures import make_pdf_file  # noqa: E402


async def build_core_document(store: SQLiteStore, directory: Path) -> dict[str, Any]:
    """A contract comes in - the pages are compiled right away."""
    llm = TieredLLM()
    pipeline = IngestionPipeline(
        sqlite_store=store,
        semantic_memory=RecordingSemantic(),
        llm_gateway=llm,
    )

    contract = make_pdf_file(
        directory,
        name="采购合同.pdf",
        pages=[
            "甲方为华润置地有限公司。验收期限为 30 日。",
            "验收期限自交付之日起计算。违约金为合同金额的百分之五。",
        ],
    )

    result = await pipeline.ingest_file(contract)
    return {"llm": llm, "pipeline": pipeline, "result": result}


async def replay_pages(store: SQLiteStore, directory: Path) -> bool:
    """The core document becomes pages, and the conclusions say where
    they were read from."""
    built = await build_core_document(store, directory)
    result = built["result"]

    print("=" * 60)
    print("分层承诺一：核心层文档在摄入时编译成页面")
    print("-" * 60)
    print(f"文件：采购合同.pdf")
    print(f"分层：{result.get('tier')}（判定来源：{result.get('decided_by')}）")
    print(f"编译：{'成功' if result.get('compiled') else '未编译'}"
          f"，页面 {result.get('pages')} 个")

    pages = await store.list_wiki_pages()
    for page in pages:
        print()
        print(f"  页面《{page['title']}》（{page['page_type']} / {page['tier']}）")
        conclusions = await store.list_page_conclusions(page["id"])
        for conclusion in conclusions:
            anchor = conclusion.get("anchor") or {}
            where = anchor.get("page")
            print(f"    · {conclusion['text']}  —— 第 {where} 页")

    compiled = result.get("compiled") is True
    located = all(
        (c.get("anchor") or {}).get("page")
        for c in await store.list_page_conclusions("core/验收期限")
    )
    print()
    print(f"结论：{'承诺成立' if compiled and located else '承诺不成立'}")
    return compiled and located


async def replay_short_circuit(store: SQLiteStore, directory: Path) -> bool:
    """The core layer answers - the long tail is not searched."""
    built = await build_core_document(store, directory)

    longtail = RecordingLongTail(
        [
            {
                "id": "c1",
                "text": "验收期限在附件的第三页",
                "file_name": "附件.pdf",
                "file_path": "/kb/附件.pdf",
                "meta": {"page": 3},
                "_score": 0.8,
            },
            {
                "id": "c2",
                "text": "验收期限的说明也在附件里",
                "file_name": "附件.pdf",
                "file_path": "/kb/附件.pdf",
                "meta": {"page": 4},
                "_score": 0.7,
            },
        ]
    )
    agent = make_tiered_agent(store, built["llm"], longtail)

    result = await agent.run(query="验收期限是多久？", session_id="replay-tier-1")

    print("=" * 60)
    print("分层承诺二：核心层结论足够时不检索长尾层")
    print("-" * 60)
    print(result["answer"])
    for citation in result["citations"]:
        print(
            f"  引用 [{citation['index']}] {citation['file_name']}"
            f" · {citation.get('location') or '（无位置）'}"
        )
    print()
    print(f"长尾层检索次数：{len(longtail.calls)}")

    short_circuited = not longtail.calls
    answered = result["refused"] is False and bool(result["citations"])
    print()
    print(
        f"结论：{'承诺成立' if short_circuited and answered else '承诺不成立'}"
        f"（长尾层{'未被检索' if short_circuited else '被检索了'}）"
    )
    return short_circuited and answered


async def replay_two_versions(store: SQLiteStore, directory: Path) -> bool:
    """A revision keeps both versions and puts them side by side."""
    built = await build_core_document(store, directory)

    # The contract is revised.
    built["llm"].conclusions = [{"text": "验收期限为 15 日", "segment": 0}]
    built["llm"].contradictions = {
        "contradictions": [
            {
                "old_index": 0,
                "new_index": 0,
                "topic": "验收期限",
                "reason": "旧版 30 日，新版 15 日",
            }
        ]
    }
    revised = make_pdf_file(
        directory,
        name="采购合同.pdf",
        pages=["甲方为华润置地有限公司。验收期限为 15 日。"],
    )
    await built["pipeline"].ingest_file(revised, force=True)

    print("=" * 60)
    print("分层承诺三：修订保留两个版本并生成对比表")
    print("-" * 60)

    conclusions = await store.list_page_conclusions(
        "core/验收期限", active_only=False
    )
    for conclusion in conclusions:
        state = "生效" if conclusion["active"] else "保留（不生效）"
        group = conclusion["conflict_group"] or "无"
        print(f"  · {conclusion['text']}  [{state}]  矛盾组：{group}")

    tables = [
        p for p in await store.list_wiki_pages() if p["page_type"] == "comparison"
    ]
    for table in tables:
        print()
        print(f"  对比表《{table['title']}》")
        for line in table["markdown"].splitlines():
            if line.startswith("|"):
                print(f"    {line}")

    kept = len(conclusions) >= 2
    has_table = bool(tables)
    print()
    print(
        f"结论：{'承诺成立' if kept and has_table else '承诺不成立'}"
        f"（旧版本={'保留' if kept else '被覆盖了'}，对比表={'有' if has_table else '无'}）"
    )
    return kept and has_table


async def main() -> int:
    with tempfile.TemporaryDirectory(prefix="yuanwork_replay_tiers_") as directory:
        store = SQLiteStore(Path(directory) / "replay.db")
        await store.initialize()
        work = Path(directory)
        try:
            results = [
                await replay_pages(store, work),
                await replay_short_circuit(store, work),
                await replay_two_versions(store, work),
            ]
        finally:
            await store.close()

    print("=" * 60)
    held = sum(1 for ok in results if ok)
    print(f"三条分层承诺：{held}/{len(results)} 成立")
    return 0 if held == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
