"""End-to-end proof that /chat/stream really streams.

This script starts the REAL FastAPI application, patches only
`litellm.completion` (so the real gateway / agent / SSE code paths run),
and measures when each SSE frame actually reaches the client.

The decisive assertion is (c): the first token must arrive well before
the last one. An implementation that buffers the whole answer and flushes
it at the end would pass a naive "a token event exists" check but fail
here.

Usage:
    ./.venv/Scripts/python.exe scripts/e2e_stream_check.py
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# --- Make `yuanwork` importable without installing the package ----------
BACKEND_DIR = Path(__file__).resolve().parent.parent / "src" / "backend"
sys.path.insert(0, str(BACKEND_DIR))

# The console on Windows defaults to cp936, which mangles the Chinese
# answer in the printed timeline. Force UTF-8 so the output is readable.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# --- Configuration of the fake provider ---------------------------------
DELTAS = ["违约金", "为每日", "万分之", "三。", "依据合同第 12 条。"]
TOKEN_DELAY = 0.2  # seconds between tokens
EXPECTED_ANSWER = "".join(DELTAS)

FAKE_MODELS_YAML = """
models:
  fake-mini:
    provider: openai
    api_key: sk-e2e-fake-key
    model_name: gpt-4o-mini
routing:
  understand: fake-mini
  answer: fake-mini
  conflict_check: fake-mini
  general: fake-mini
  extract: fake-mini
  reflection: fake-mini
  wiki_compile: fake-mini
  ner: fake-mini
"""


# ===== Fake litellm objects =====


class _FakeUsage:
    def __init__(self, prompt_tokens: int = 12, completion_tokens: int = 8):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = prompt_tokens + completion_tokens


class _FakeDelta:
    def __init__(self, content: str | None):
        self.content = content


class _FakeChoice:
    def __init__(self, content: str):
        self.delta = _FakeDelta(content)
        self.message = type("_Msg", (), {"content": content})()
        self.finish_reason = "stop"


class _FakeChunk:
    def __init__(self, content: str, usage: _FakeUsage | None = None):
        self.choices = [_FakeChoice(content)]
        self.usage = usage


class _FakeResponse:
    def __init__(self, content: str):
        self.choices = [_FakeChoice(content)]
        self.usage = _FakeUsage()


def _install_fake_provider() -> Any:
    """Replace litellm.completion with a fake that streams slowly."""
    import litellm

    original = litellm.completion

    def _fake_completion(**kwargs):
        if kwargs.get("stream"):
            def _gen():
                for i, text in enumerate(DELTAS):
                    time.sleep(TOKEN_DELAY)
                    usage = _FakeUsage() if i == len(DELTAS) - 1 else None
                    yield _FakeChunk(text, usage)
            return _gen()

        # Non-streaming calls (understand / conflict check / wiki compile).
        return _FakeResponse(
            json.dumps(
                {
                    "intent": "question",
                    "topic": "违约金",
                    "file_hint": None,
                    "time_hint": None,
                    "entity_hints": [],
                    "requires_evidence": True,
                    "needs_refusal_if_no_match": True,
                    "verdict": "consistent",
                    "conflict_claims": [],
                    "explanation": "no conflict",
                    "title": "违约金",
                    "content": "违约金为每日万分之三。",
                },
                ensure_ascii=False,
            )
        )

    litellm.completion = _fake_completion
    return original


def _patch_extract_step() -> None:
    """Give the agent evidence so it does not legitimately refuse.

    Only this step is patched: the answer step - the code under test -
    runs for real, as do the SSE route and the gateway.
    """
    from yuanwork.orchestrator.agent import AgentLoop

    evidence = [
        {
            "id": "chunk-1",
            "file_name": "contract.pdf",
            "file_path": "/kb/contract.pdf",
            "text": "第十二条 违约金为每日万分之三。",
            "meta": {"page": 12},
        }
    ]
    confidence = {
        "confidence": 0.92,
        "level": "high",
        "top_score": 0.88,
        "score_gap": 0.3,
        "evidence_count": 1,
        "should_refuse": False,
        "reason": None,
    }

    async def _extract(self, state):
        state.evidence = evidence
        state.confidence = confidence
        state.trace.append({"step": "extract", "evidence_count": 1})

    AgentLoop._step_extract = _extract  # type: ignore[method-assign]


# ===== Helpers =====


def _free_port(preferred: int = 8799) -> int:
    for port in (preferred, preferred + 1, preferred + 2):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("no free port")


def _parse_frames(raw: bytes) -> list[tuple[str, str]]:
    """Split raw SSE bytes into (event, data) pairs."""
    frames: list[tuple[str, str]] = []
    for block in raw.split(b"\r\n\r\n"):
        text = block.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        event = "message"
        data_lines: list[str] = []
        for line in text.split("\r\n"):
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
        frames.append((event, "\n".join(data_lines)))
    return frames


class Results:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        if ok:
            self.passed += 1
            print(f"  PASS  {name}" + (f"  ({detail})" if detail else ""))
        else:
            self.failed += 1
            print(f"  FAIL  {name}" + (f"  ({detail})" if detail else ""))
        return ok


# ===== The test =====


async def run() -> int:
    import httpx
    import uvicorn

    from yuanwork.api.app import create_app
    from yuanwork.config import YuanWorkSettings

    results = Results()
    tmp = Path(tempfile.mkdtemp(prefix="yuanwork_e2e_"))
    config_dir = tmp / "configs"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "models.yaml").write_text(FAKE_MODELS_YAML, encoding="utf-8")

    os.environ["YUANWORK_CONFIG_DIR"] = str(config_dir)
    os.environ["YUANWORK_DATA_DIR"] = str(tmp / "data")

    original_completion = _install_fake_provider()
    _patch_extract_step()

    port = _free_port()
    settings = YuanWorkSettings(
        config_dir=config_dir, data_dir=tmp / "data", port=port
    )
    app = create_app(settings)

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    base = f"http://127.0.0.1:{port}"
    try:
        # Wait for the server to come up.
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.05)
        else:
            print("server failed to start", file=sys.stderr)
            return 2

        raw_chunks: list[bytes] = []
        arrivals: list[float] = []
        health: dict[str, Any] = {}

        async with httpx.AsyncClient(timeout=30.0) as client:
            async def health_probe() -> None:
                await asyncio.sleep(0.15)
                t0 = time.monotonic() - started
                try:
                    resp = await client.get(f"{base}/api/v1/health")
                    health["requested_at"] = t0
                    health["status"] = resp.status_code
                except Exception as exc:  # pragma: no cover
                    health["error"] = str(exc)
                health["completed_at"] = time.monotonic() - started

            started = time.monotonic()
            probe = asyncio.create_task(health_probe())

            async with client.stream(
                "POST", f"{base}/api/v1/chat/stream", json={"query": "违约金是多少？"}
            ) as resp:
                results.check("HTTP 200", resp.status_code == 200, str(resp.status_code))
                buffer = b""
                async for chunk in resp.aiter_bytes():
                    raw_chunks.append(chunk)
                    buffer += chunk
                    # Timestamp each complete frame as it lands.
                    while b"\r\n\r\n" in buffer:
                        frame, buffer = buffer.split(b"\r\n\r\n", 1)
                        arrivals.append(time.monotonic() - started)
                if buffer.strip():
                    arrivals.append(time.monotonic() - started)

            await probe
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(server_task, timeout=15)
        except asyncio.TimeoutError:  # pragma: no cover
            server_task.cancel()
        import litellm

        litellm.completion = original_completion

    raw = b"".join(raw_chunks)
    frames = _parse_frames(raw)
    token_frames = [(t, d) for (t, d) in frames if t == "token"]
    token_texts = [json.loads(d).get("text") for (t, d) in token_frames]
    done_frames = [(t, d) for (t, d) in frames if t == "done"]

    print("\nTimeline (seconds since the request started):")
    for i, (t, d) in enumerate(frames):
        stamp = arrivals[i] if i < len(arrivals) else -1
        preview = d[:60].replace("\n", " ")
        print(f"  {stamp:7.3f}s  {t:<9} {preview}")

    print("\nAssertions:")

    # a. enough token events
    results.check(
        "at least 5 token events",
        len(token_frames) >= 5,
        f"got {len(token_frames)}",
    )

    # b. order and content
    results.check(
        "tokens arrive in order and concatenate to the full answer",
        token_texts == DELTAS and "".join(t or "" for t in token_texts) == EXPECTED_ANSWER,
        f"{token_texts}",
    )

    # c. THE decisive one: first token well before the last
    if len(arrivals) >= 2:
        gap = arrivals[-1] - arrivals[0]
    else:
        gap = 0.0
    results.check(
        "first token arrives >=300ms before the last",
        gap >= 0.3,
        f"gap={gap:.3f}s",
    )

    # d. the whole stream takes at least as long as the provider took
    results.check(
        "stream spans >=0.5s (5 tokens x 200ms)",
        gap >= 0.5,
        f"gap={gap:.3f}s",
    )

    # e. CRLF frame terminators
    results.check(
        "frames use CRLF terminators",
        b"event: token\r\n" in raw and b"event: done\r\n" in raw,
    )

    # f. the done event carries the response
    done_ok = False
    done_answer = None
    if done_frames:
        payload = json.loads(done_frames[0][1])
        response = payload.get("response")
        if isinstance(response, dict):
            done_answer = response.get("answer")
            done_ok = done_answer == EXPECTED_ANSWER
    results.check(
        "done event carries the full response",
        done_ok,
        f"answer={done_answer!r}",
    )

    # g. the event loop stayed responsive during the stream
    health_ok = (
        health.get("status") == 200
        and health.get("completed_at", 99) < (arrivals[-1] if arrivals else 0)
    )
    results.check(
        "a concurrent /health was served before the stream ended",
        health_ok,
        f"health={health.get('status')} at {health.get('completed_at', -1):.3f}s, "
        f"stream ended {arrivals[-1] if arrivals else -1:.3f}s",
    )

    print(f"\n{results.passed} passed, {results.failed} failed")
    return 0 if results.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
