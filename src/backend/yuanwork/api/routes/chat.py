"""Chat endpoints (3-step reasoning)."""

from __future__ import annotations

import json
import logging
import uuid
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from yuanwork.api.errors import classify_exception, error_payload
from yuanwork.api.schemas import ChatRequest, ChatResponse, ErrorResponse
from yuanwork.privacy.redactor import Redactor

logger = logging.getLogger(__name__)

router = APIRouter()


def _follow_up(body: ChatRequest) -> dict | None:
    """这次请求带的追问上下文（没有就是没有）。

    **只有位置与读者上一句问话**——上一轮模型生成的回答不在请求里，
    也不该在（ADR-057）。
    """
    if body.follow_up is None:
        return None
    return body.follow_up.model_dump()


def _build_redactor(request: Request) -> Redactor:
    """The redactor for this request, built from the settings.

    Masking happens on the way *to* the model: the user still reads their
    own phone number in the citation, but the model never sees it.
    """
    settings = getattr(request.app.state, "settings", None)
    enabled = bool(getattr(settings, "enable_redaction", True))
    return Redactor(enabled=enabled)


@router.post("/chat/stream")
async def chat_stream(request: Request, body: ChatRequest) -> EventSourceResponse:
    """Stream chat response via Server-Sent Events.

    Events emitted:
    - trace: Agent step trace (for debugging)
    - token: Incremental answer tokens
    - citations: Final citations list
    - conflict: Conflict detected
    - done: Stream complete
    - error: Error occurred
    """
    session_id = body.session_id or str(uuid.uuid4())

    async def event_generator() -> AsyncIterator[dict]:
        """Generate SSE events."""
        try:
            from yuanwork.orchestrator.agent import AgentLoop
            from yuanwork.api.deps import (
                get_llm_gateway,
                get_semantic_memory,
                optional_sqlite_store,
                optional_consolidation_engine,
                optional_episodic_memory,
                optional_episodic_recorder,
                optional_wiki_compile_trigger,
            )

            semantic = get_semantic_memory()
            # The memory engineering layer can be switched off - a chat
            # that does not use it still works.
            episodic = optional_episodic_memory()
            llm = get_llm_gateway()
            consolidation = optional_consolidation_engine()
            wiki_trigger = optional_wiki_compile_trigger()
            recorder = optional_episodic_recorder()

            agent = AgentLoop(
                llm_gateway=llm,
                semantic_memory=semantic,
                episodic_memory=episodic,
                consolidation_engine=consolidation,
                wiki_compile_trigger=wiki_trigger,
                redactor=_build_redactor(request),
                episodic_recorder=recorder,
                # The core layer is searched first (ADR-034).
                sqlite_store=optional_sqlite_store(),
            )

            async for event in agent.stream(
                query=body.query,
                session_id=session_id,
                follow_up=_follow_up(body),
                opts=body.opts,
            ):
                yield {
                    "event": event["type"],
                    "data": json.dumps(event["data"], default=str, ensure_ascii=False),
                }

        except Exception as e:
            logger.exception(f"Chat stream failed: {e}")
            # The user gets a readable message; the exception text stays
            # in the log.
            payload = error_payload(classify_exception(e))
            yield {
                "event": "error",
                "data": json.dumps(
                    ErrorResponse(
                        error=payload["error"],
                        detail=payload["error"],
                        code=payload["code"],
                    ).model_dump(),
                ),
            }
            # Always close the stream with a terminal event, so a client can
            # tell "the stream ended" from "the stream was cut".
            yield {
                "event": "done",
                "data": json.dumps({"session_id": session_id, "response": None}),
            }

    return EventSourceResponse(
        event_generator(),
        headers={
            # Proxies must not buffer, otherwise the tokens arrive in one
            # lump at the end and the streaming is pointless.
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/chat", response_model=ChatResponse)
async def chat(request: Request, body: ChatRequest) -> ChatResponse:
    """Non-streaming chat endpoint (collects full response)."""
    session_id = body.session_id or str(uuid.uuid4())

    try:
        from yuanwork.orchestrator.agent import AgentLoop
        from yuanwork.api.deps import (
            get_llm_gateway,
            get_semantic_memory,
            optional_sqlite_store,
            optional_consolidation_engine,
            optional_episodic_memory,
            optional_episodic_recorder,
            optional_wiki_compile_trigger,
        )

        semantic = get_semantic_memory()
        # The memory engineering layer can be switched off - a chat that
        # does not use it still works.
        episodic = optional_episodic_memory()
        llm = get_llm_gateway()
        consolidation = optional_consolidation_engine()
        wiki_trigger = optional_wiki_compile_trigger()
        recorder = optional_episodic_recorder()

        agent = AgentLoop(
            llm_gateway=llm,
            semantic_memory=semantic,
            episodic_memory=episodic,
            consolidation_engine=consolidation,
            wiki_compile_trigger=wiki_trigger,
            redactor=_build_redactor(request),
            episodic_recorder=recorder,
            # The core layer is searched first (ADR-034).
            sqlite_store=optional_sqlite_store(),
        )

        result = await agent.run(
            query=body.query,
            session_id=session_id,
            follow_up=_follow_up(body),
            opts=body.opts,
        )

        return ChatResponse(
            session_id=session_id,
            answer=result["answer"],
            citations=result["citations"],
            confidence=result["confidence"],
            refused=result.get("refused", False),
            refusal_reason=result.get("refusal_reason"),
            refusal_positions=result.get("refusal_positions", []),
            conflicts=result.get("conflicts", []),
            wiki_compile=result.get("wiki_compile"),
            terms=result.get("terms", []),
            trace=result.get("trace", []),
        )

    except Exception as e:
        logger.exception(f"Chat failed: {e}")
        # The exception text stays in the log; the client gets a code and
        # a message it can show.
        raise HTTPException(
            status_code=500,
            detail=error_payload(classify_exception(e)),
        ) from e


@router.post("/chat/cancel/{session_id}")
async def cancel_chat(session_id: str, request: Request) -> dict:
    """Cancel an ongoing chat generation.

    The run stops between the steps and between the tokens, so the
    backend stops pulling from the model. What was already generated
    stays - the user asked to stop, not to throw the answer away.
    """
    from yuanwork.orchestrator import cancel

    stopped = cancel.cancel(session_id)
    return {
        "cancelled": stopped,
        "session_id": session_id,
        "note": (
            "已停止生成" if stopped else "这个会话没有正在进行的生成"
        ),
    }
