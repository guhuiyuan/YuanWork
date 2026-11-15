"""The acceptance replay.

Replays the three promises against a real store and prints what the
user would see. It is the milestone check: run it and read the output
- if a promise does not hold, it says so and the script fails.

    .venv/Scripts/python.exe src/backend/tests/e2e/replay.py
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

from e2e.harness import ScriptedLLM, evidence, make_agent  # noqa: E402
from yuanwork.db.sqlite import SQLiteStore  # noqa: E402


async def replay_answer(store: SQLiteStore) -> bool:
    """Promise 1: the answer can be located in the source."""
    llm = ScriptedLLM()
    agent = make_agent(store, llm, evidence(3))

    result = await agent.run(query="违约金怎么约定？", session_id="replay-1")

    print("=" * 60)
    print("承诺一：答案能定位到原文")
    print("-" * 60)
    print(result["answer"])
    for citation in result["citations"]:
        print(
            f"  引用 [{citation['index']}] {citation['file_name']}"
            f" · {citation.get('location') or '（无位置）'}"
        )

    located = bool(result["citations"]) and all(
        citation.get("file_name") and citation.get("anchor")
        for citation in result["citations"]
    )
    print()
    print(f"结论：{'承诺成立' if located else '承诺不成立'}")
    return located


async def replay_conflict(store: SQLiteStore) -> bool:
    """Promise 2: the knowledge base wins, and the divergence is said."""
    llm = ScriptedLLM(
        answer="违约金是合同总额的十分之一 [1]。",
        conflict={
            "verdict": "conflict",
            "conflict_claims": [
                {
                    "topic": "违约金比例",
                    "llm_says": "合同总额的十分之一",
                    "kb_says": "合同总额的三分之一",
                }
            ],
            "explanation": "两者不一致",
        },
    )
    agent = make_agent(store, llm, evidence(3))

    result = await agent.run(query="违约金怎么约定？", session_id="replay-2")

    print("=" * 60)
    print("承诺二：知识库优先并显式提醒")
    print("-" * 60)
    print(result["answer"])

    marked = result["answer"].lstrip().startswith("⚠")
    kb_wins = "以知识库为准" in result["answer"]
    print()
    print(
        f"结论：{'承诺成立' if marked and kb_wins else '承诺不成立'}"
        f"（显式提醒={'是' if marked else '否'}，知识库为准={'是' if kb_wins else '否'}）"
    )
    return marked and kb_wins


async def replay_refusal(store: SQLiteStore) -> bool:
    """Promise 3: not enough evidence is a refusal with places and advice."""
    llm = ScriptedLLM(sufficient=False)
    agent = make_agent(store, llm, evidence(3))

    result = await agent.run(query="违约金怎么约定？", session_id="replay-3")

    print("=" * 60)
    print("承诺三：证据不足直接拒答并给出位置与建议")
    print("-" * 60)
    print(result["answer"])

    refused = result["refused"] is True
    has_place = "合同.pdf" in result["answer"]
    has_advice = any(
        word in result["answer"] for word in ("建议", "可以", "试试", "关键词")
    )
    print()
    print(
        f"结论：{'承诺成立' if refused and has_place and has_advice else '承诺不成立'}"
        f"（拒答={'是' if refused else '否'}，位置={'有' if has_place else '无'}，"
        f"建议={'有' if has_advice else '无'}）"
    )
    return refused and has_place and has_advice


async def main() -> int:
    with tempfile.TemporaryDirectory(prefix="yuanwork_replay_") as directory:
        store = SQLiteStore(Path(directory) / "replay.db")
        await store.initialize()
        try:
            results = [
                await replay_answer(store),
                await replay_conflict(store),
                await replay_refusal(store),
            ]
        finally:
            await store.close()

    print("=" * 60)
    held = sum(1 for ok in results if ok)
    print(f"三条承诺：{held}/{len(results)} 成立")
    return 0 if held == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
