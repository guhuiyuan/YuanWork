"""延迟实测：把 7 条延迟承诺量出数来（Phase 5 验收用）。

用法:
    .venv/Scripts/python.exe -X utf8 scripts/bench/latency.py \
        --index-dir "D:/yuanwork/latency-index" --workspace "D:/workspace"

量的七项:
    AC-P-002  首批证据返回（不含模型生成）
    AC-P-003  首个 token
    AC-P-004  拒答判定与冲突判定合计
    AC-P-005  冷启动到可以提问
    AC-P-006  单文件重建到可检索
    AC-P-008  核心层文档编译
    AC-P-009  核心层证据返回（且不触发长尾检索）

索引不存在就用工作域现建一个——测延迟的索引必须是真实的索引。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "src" / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# The console on Windows defaults to cp936, which mangles the Chinese
# report. Force UTF-8 so what is printed can be read.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

#: The questions the measurements ask. They are about the real workspace
#: so that the retrieval has something to find. The first one that the
#: evidence gate lets through is the one that gets measured - a refusal
#: has no answer, and an answer is what the first-token promise is about.
DEFAULT_QUESTIONS = [
    "库仑定律的适用条件有哪些",
    "静电场里的库仑定律是什么",
    "电磁学课程讲了什么",
]
CORE_QUESTION = "库仑定律的内容与适用条件"

results: dict[str, Any] = {}


def record(name: str, value: Any, note: str = "") -> None:
    """One measured number, and what it means."""
    results[name] = value
    mark = "✓" if value is not None else "—"
    print(f"{mark} {name}: {value}{('　' + note) if note else ''}")


def fail(name: str, error: BaseException) -> None:
    """A measurement that could not be made is recorded as such."""
    results[name] = None
    results[f"{name}_error"] = f"{type(error).__name__}: {error}"
    print(f"✗ {name}: 测不了 —— {type(error).__name__}: {error}")


# --------------------------------------------------------------------------
# the index
# --------------------------------------------------------------------------


async def ensure_index(index_dir: Path, workspace: Path | None) -> None:
    """The measurements need a real index. Build one if there is none.

    "There is an index" means there are files in it - an empty database
    file is what a crashed run leaves behind, and measuring against it
    would say nothing.
    """
    if (index_dir / "store.db").exists():
        from yuanwork.db.sqlite import SQLiteStore

        store = SQLiteStore(index_dir / "store.db")
        await store.initialize()
        try:
            files = await store.list_files(limit=1)
        finally:
            await store.close()
        if files:
            print(f"用现成的索引：{index_dir}")
            return
        print("那个索引是空的（上次跑崩了留下的），重新建")
    if workspace is None:
        raise RuntimeError(f"{index_dir} 里没有索引，而没给工作域")

    from yuanwork.db.sqlite import SQLiteStore
    from yuanwork.ingestion.batch import BatchIngestor
    from yuanwork.ingestion.pipeline import IngestionPipeline
    from yuanwork.memory.semantic import SemanticMemory

    print(f"没有索引，用工作域现建：{workspace}")
    index_dir.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(index_dir / "store.db")
    await store.initialize()
    semantic = SemanticMemory(index_dir / "lancedb")
    await semantic.initialize()
    pipeline = IngestionPipeline(
        sqlite_store=store,
        semantic_memory=semantic,
        llm_gateway=None,
    )
    started = time.perf_counter()
    result = await BatchIngestor(pipeline).ingest(workspace, recursive=True)
    elapsed = time.perf_counter() - started
    print(
        f"建库完成：{result.get('indexed')} 个文件、{elapsed:.1f} 秒"
        f"（{result.get('indexed') / elapsed * 60:.1f} 文件/分钟）"
    )
    await semantic.close()
    await store.close()


# --------------------------------------------------------------------------
# AC-P-005 — cold start
# --------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def measure_cold_start(index_dir: Path, timeout: float = 90.0) -> float | None:
    """How long from process start until the app can take a question.

    The desktop app's own rule: the sidecar probes `/api/v1/health`
    until it answers, and that is "ready". So this measures exactly
    what the app waits for.
    """
    port = _free_port()
    env = {
        **os.environ,
        "YUANWORK_DATA_DIR": str(index_dir),
        "YUANWORK_HOST": "127.0.0.1",
        "YUANWORK_PORT": str(port),
        # The package is not installed into the venv - the backend is
        # started from the source tree (the desktop sidecar does the same).
        "PYTHONPATH": os.pathsep.join(
            [str(BACKEND), os.environ.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep),
    }
    log_path = index_dir / "cold_start.log"
    started = time.perf_counter()
    with open(log_path, "w", encoding="utf-8") as log:
        process = subprocess.Popen(  # noqa: S603 - the command is fixed
            [sys.executable, "-m", "yuanwork"],
            cwd=str(ROOT),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        url = f"http://127.0.0.1:{port}/api/v1/health"
        try:
            while time.perf_counter() - started < timeout:
                if process.poll() is not None:
                    raise RuntimeError(
                        "后端进程退出了：" + log_path.read_text(
                            encoding="utf-8", errors="replace"
                        )[-400:]
                    )
                try:
                    with urllib.request.urlopen(url, timeout=1) as response:
                        if response.status == 200:
                            return round(time.perf_counter() - started, 2)
                except (urllib.error.URLError, OSError):
                    time.sleep(0.2)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
    raise TimeoutError(f"{timeout} 秒内没有就绪")


# --------------------------------------------------------------------------
# the chat — the numbers that need the model
# --------------------------------------------------------------------------


async def _sse_frames(lines: Any) -> Any:
    """The SSE frames in a response: the event name and its JSON data.

    The name is on the `event:` line, the payload on the `data:` line -
    they are two lines of one frame, not two frames.
    """
    name = ""
    async for raw in lines:
        line = raw.rstrip("\r")
        if line.startswith("event:"):
            name = line[len("event:"):].strip()
        elif line.startswith("data:"):
            payload = line[len("data:"):].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                continue
            yield name, data if isinstance(data, dict) else {"data": data}


async def measure_chat(client: Any, question: str) -> dict[str, Any]:
    """One question, timed at every step.

    The stream says when each step started and finished, so the numbers
    come from the same run - not from two runs compared afterwards.
    """
    started = time.perf_counter()
    marks: dict[str, float] = {}
    steps: dict[str, dict[str, float]] = {}
    first_token: float | None = None
    gate_ms: float | None = None
    gate_reason: str | None = None
    sufficient: int | None = None
    longtail_searched: bool | None = None
    core_found: bool | None = None
    events: list[str] = []

    async with client.stream(
        "POST",
        "/chat/stream",
        json={"query": question, "session_id": "latency-check"},
    ) as response:
        response.raise_for_status()
        async for name, data in _sse_frames(response.aiter_lines()):
            now = round(time.perf_counter() - started, 3)
            events.append(name)
            if name == "trace":
                step = str(data.get("step", ""))
                status = str(data.get("status", ""))
                slot = steps.setdefault(step, {})
                slot[f"{status}_at"] = now
                if step == "extract" and status == "completed":
                    marks["first_evidence_at"] = now
                    longtail_searched = data.get("longtail_searched")
                    core_found = data.get("core_found")
                if step == "gate" and status == "completed":
                    gate = data.get("gate") or {}
                    gate_reason = gate.get("reason")
                    sufficient = gate.get("sufficient_count")
            elif name == "token" and first_token is None:
                first_token = now
            elif name == "done":
                payload = data.get("response") or data
                trace = payload.get("trace") or []
                for entry in trace:
                    if entry.get("step") == "gate" and "duration_ms" in entry:
                        gate_ms = float(entry["duration_ms"])

    out: dict[str, Any] = {
        "first_evidence_at": marks.get("first_evidence_at"),
        "first_token_at": first_token,
        "steps": steps,
        "gate_ms": gate_ms,
        "gate_reason": gate_reason,
        "sufficient_count": sufficient,
        "core_found": core_found,
        "longtail_searched": longtail_searched,
        "events": events[:12],
    }
    # The retrieval path: from the moment the files are being looked for
    # to the moment the evidence is there. The understanding step is a
    # model call, and the promise is about the retrieval.
    localize = steps.get("localize", {})
    extract = steps.get("extract", {})
    if "started_at" in localize and "completed_at" in extract:
        out["retrieval_seconds"] = round(
            float(extract["completed_at"]) - float(localize["started_at"]), 3
        )
    # The gate's own time: the trace carries it, and it is the number
    # the budget is about. What happens *between* the extract and the
    # gate (the opportunistic wiki compile) is not a decision.
    if gate_ms is not None:
        out["judgment_seconds"] = round(gate_ms / 1000.0, 3)
    # The conflict check comes after the answer; its own trace entry says
    # when it started and finished.
    conflict = steps.get("check_conflicts", {})
    if "started_at" in conflict and "completed_at" in conflict:
        out["conflict_seconds"] = round(
            float(conflict["completed_at"]) - float(conflict["started_at"]), 3
        )
    # AC-P-004 is the **total** of the two decisions: the gate and the
    # conflict check. They sit on either side of the answer, so they are
    # two separate pieces of time that add up to one budget.
    if out.get("judgment_seconds") is not None and out.get("conflict_seconds") is not None:
        out["judgment_total_seconds"] = round(
            float(out["judgment_seconds"]) + float(out["conflict_seconds"]), 3
        )
    return out


# --------------------------------------------------------------------------
# AC-P-006 — rebuild one file
# --------------------------------------------------------------------------


async def measure_rebuild(client: Any, index_dir: Path) -> float | None:
    """From "rebuild this file" to "the new text can be found".

    The file is a **copy** in a scratch directory: the promise is about
    a file whose content changed, and the user's own files are not
    touched to measure it. The copy is indexed, then a sentence is
    added, then it is rebuilt - and the new sentence has to be findable.
    """
    from yuanwork.api.deps import get_semantic_memory

    scratch = index_dir / "rebuild_probe"
    scratch.mkdir(parents=True, exist_ok=True)
    target = scratch / "重建探针.md"
    first = "第一段：电容与电感的储能公式。"
    marker = "第二段新增：介电常数与损耗角正切的定义。"
    target.write_text(f"# 重建探针\n\n{first}\n", encoding="utf-8")

    semantic = get_semantic_memory()

    # First: the file goes into the index.
    response = await client.post("/ingest/file", json={"path": str(target)})
    response.raise_for_status()
    first_job = response.json()["job_id"]
    await _wait_for_job(client, first_job)
    if not await semantic.search(query=first[:8], top_k=5):
        raise RuntimeError("第一次入库之后搜不到它")

    # Then: the content changes, and the file is rebuilt.
    target.write_text(f"# 重建探针\n\n{first}\n\n{marker}\n", encoding="utf-8")
    response = await client.post("/ingest/file", json={"path": str(target)})
    response.raise_for_status()
    job_id = response.json()["job_id"]

    started = time.perf_counter()
    await _wait_for_job(client, job_id, started)

    # The new text has to be findable - not just the old one.
    found = await semantic.search(query=marker[:10], top_k=5)
    elapsed = time.perf_counter() - started
    results["rebuild_marker"] = marker[:10]
    if not found:
        raise RuntimeError("重建完了，但新内容搜不到")
    return round(elapsed, 2)


async def _wait_for_job(client: Any, job_id: str, started: float | None = None) -> None:
    """Wait for an ingest job, and say so when it does not finish."""
    if started is None:
        started = time.perf_counter()
    while time.perf_counter() - started < 180:
        status = (await client.get(f"/ingest/status/{job_id}")).json()
        if status.get("status") in ("completed", "failed", "error"):
            if status.get("status") != "completed":
                raise RuntimeError(f"重建没有成功：{status}")
            return
        await asyncio.sleep(0.2)
    raise TimeoutError("重建没在 180 秒内结束")


# --------------------------------------------------------------------------
# AC-P-008 — compile a core document
# --------------------------------------------------------------------------


async def measure_core_compile() -> float | None:
    """How long one core document takes to compile into pages."""
    from yuanwork.api.deps import get_llm_gateway, get_sqlite_store
    from yuanwork.ingestion.parser.dispatcher import ParserDispatcher
    from yuanwork.wiki.core_compiler import CoreCompiler

    sqlite = get_sqlite_store()

    # A core document: one that was classified as core when it came in.
    rows = await sqlite.list_doc_tiers(tier="core", limit=50)
    if not rows:
        return None

    # The tier table knows the document, the files table knows where it
    # is on disk.
    known = {
        str(entry.get("doc_id") or ""): str(entry.get("file_path") or "")
        for entry in await sqlite.list_files(limit=2000)
    }

    dispatcher = ParserDispatcher()
    compiler = CoreCompiler(sqlite, llm_gateway=get_llm_gateway())

    for row in rows:
        doc_id = str(row.get("doc_id") or "")
        source = Path(known.get(doc_id, ""))
        if not doc_id or not source.exists():
            continue
        parser = dispatcher.get_parser(source)
        if parser is None:
            continue
        blocks = await parser.parse(source, doc_id)
        if not blocks:
            continue
        text = "\n\n".join(str(getattr(b, "content", "") or "") for b in blocks).strip()
        segments = [
            {
                "text": str(getattr(b, "content", "") or ""),
                "anchor": (getattr(b, "meta", None) or {}).get("anchor"),
            }
            for b in blocks
        ]
        started = time.perf_counter()
        result = await compiler.compile(
            {
                "doc_id": doc_id,
                "file_name": source.name,
                "file_path": str(source),
                "text": text,
                "segments": segments,
            }
        )
        elapsed = round(time.perf_counter() - started, 2)
        results["core_compile_pages"] = len(result.get("pages") or [])
        results["core_compile_error"] = result.get("error")
        results["core_compile_doc"] = source.name

        # The question for the core layer has to be about **that**
        # document - otherwise the core layer has nothing to answer and
        # the measurement is of the long tail instead. It has to reach
        # two conclusions, because two are what makes the core layer
        # "enough" (the short circuit's condition).
        conclusions = await sqlite.list_active_conclusions(limit=20)
        if conclusions:
            texts = [
                str(item.get("text") or item.get("topic") or "").strip()
                for item in conclusions
            ]
            texts = [text for text in texts if text]
            if texts:
                results["core_question"] = " ".join(texts[:3])[:80]
        return elapsed
    return None


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------


async def main() -> int:
    parser = argparse.ArgumentParser(description="The latency acceptance run")
    parser.add_argument("--index-dir", required=True, help="Where the index lives")
    parser.add_argument("--workspace", help="The corpus to index when there is no index")
    parser.add_argument("--json", help="Write the measurements to this file as JSON")
    parser.add_argument("--skip-model", action="store_true", help="Skip the model calls")
    args = parser.parse_args()

    index_dir = Path(args.index_dir).resolve()
    workspace = Path(args.workspace).resolve() if args.workspace else None

    # The data directory has to be decided before `yuanwork` is imported.
    os.environ["YUANWORK_DATA_DIR"] = str(index_dir)

    await ensure_index(index_dir, workspace)

    # --- AC-P-005: cold start (a real process) ----------------------------
    try:
        record("desktop_ready_seconds", measure_cold_start(index_dir))
    except Exception as e:  # noqa: BLE001 - one measurement must not stop the rest
        fail("desktop_ready_seconds", e)

    import httpx
    import uvicorn

    from yuanwork.api.app import create_app

    app = create_app()

    # A real server, not a test client: the numbers are about what the
    # user waits for, and a test client would measure the test client.
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    deadline = time.perf_counter() + 60
    while not server.started:
        if time.perf_counter() > deadline:
            raise TimeoutError("后端 60 秒内没起来")
        await asyncio.sleep(0.05)

    base = f"http://127.0.0.1:{port}/api/v1"
    print(f"后端已就绪：{base}")
    try:
        # The embedding model has to be loaded before the numbers mean
        # anything: otherwise the first question pays for the load, and
        # that is a boot cost, not a latency promise.
        if not args.skip_model:
            from yuanwork.api.deps import get_semantic_memory

            warm_started = time.perf_counter()
            await get_semantic_memory().load_embedding_model_async()
            results["embedding_warmup_seconds"] = round(
                time.perf_counter() - warm_started, 2
            )
            print(f"嵌入模型已预热：{results['embedding_warmup_seconds']} 秒")

        async with httpx.AsyncClient(base_url=base, timeout=300.0) as client:
            # --- AC-P-002 / AC-P-003 / AC-P-004: one question, timed -------
            if args.skip_model:
                print("（跳过要模型的测量）")
            else:
                try:
                    # Twice. The first question pays for the lazy imports
                    # and the first inference on the device; the second
                    # one is what the app does from then on. Both are
                    # reported - only the second is the steady state.
                    cold = await measure_chat(client, DEFAULT_QUESTIONS[0])
                    results["chat_cold"] = cold
                    print(
                        f"（第一遍：冷路径——首个事件 {cold.get('first_evidence_at')} 秒，"
                        f"闸门 {cold.get('gate_reason')}）"
                    )

                    # The gate may refuse: a refusal has no answer, and
                    # the first-token promise is about an answer. The
                    # questions are tried in order until one is let
                    # through, and the one that was measured is recorded.
                    chat: dict[str, Any] = {}
                    for question in DEFAULT_QUESTIONS:
                        chat = await measure_chat(client, question)
                        results["chat_detail"] = chat
                        if chat.get("first_token_at") is not None:
                            results["question_used"] = question
                            break
                        print(
                            f"（{question}：闸门判 {chat.get('gate_reason')}，"
                            f"换个问句）"
                        )
                    record("retrieval_seconds", chat.get("retrieval_seconds"))
                    record("first_evidence_seconds", chat.get("first_evidence_at"))
                    record("first_token_seconds", chat.get("first_token_at"))
                    record("judgment_seconds", chat.get("judgment_seconds"))
                    record("gate_ms", chat.get("gate_ms"))
                    record(
                        "conflict_seconds",
                        chat.get("conflict_seconds"),
                        "冲突判定（判定合计 = 闸门 + 冲突判定）",
                    )
                    results["question_used"] = results.get("question_used", "")
                except Exception as e:  # noqa: BLE001
                    fail("retrieval_seconds", e)

            # --- AC-P-008: compile a core document -------------------------
            if not args.skip_model:
                try:
                    record("core_compile_seconds", await measure_core_compile())
                except Exception as e:  # noqa: BLE001
                    fail("core_compile_seconds", e)

            # --- AC-P-009: the core layer answers, the long tail is not searched
            try:
                from yuanwork.api.deps import get_semantic_memory, get_sqlite_store
                from yuanwork.retrieval.hybrid import HybridRetriever
                from yuanwork.retrieval.tiered import TieredRetriever

                sqlite = get_sqlite_store()
                semantic = get_semantic_memory()
                tiered = TieredRetriever(
                    sqlite_store=sqlite,
                    retriever=HybridRetriever(semantic),
                )
                # Warm up first: the first query pays for the GPU context
                # and the first inference, and that is a boot cost, not
                # the retrieval's speed.
                question = str(results.get("core_question") or CORE_QUESTION)
                results["core_question_used"] = question
                await tiered.search(query=question, top_k=10)
                started = time.perf_counter()
                found = await tiered.search(query=question, top_k=10)
                elapsed = round(time.perf_counter() - started, 3)
                record("core_retrieval_seconds", elapsed)
                results["core_found"] = found.get("core_found")
                results["longtail_searched"] = found.get("longtail_searched")
                results["core_ms"] = found.get("core_ms")
            except Exception as e:  # noqa: BLE001
                fail("core_retrieval_seconds", e)

            # --- AC-P-006: rebuild one file and find it again --------------
            try:
                record(
                    "rebuild_seconds",
                    await measure_rebuild(client, index_dir),
                    "重建一个改过内容的文件",
                )
            except Exception as e:  # noqa: BLE001
                fail("rebuild_seconds", e)
    finally:
        server_task.cancel()
        try:
            await server_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    out = Path(args.json) if args.json else ROOT / "data" / "latency.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n实测已写入：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
