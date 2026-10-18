"""Chat endpoints (3-step reasoning)."""

from __future__ import annotations

import json
import logging
import uuid
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from yuanwork.api.schemas import ChatRequest, ChatResponse, ErrorResponse

logger = logging.getLogger(__name__)

router = APIRouter()


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
                get_consolidation_engine,
                get_episodic_memory,
                get_llm_gateway,
                get_semantic_memory,
                get_wiki_compile_trigger,
            )

            semantic = get_semantic_memory()
            episodic = get_episodic_memory()
            llm = get_llm_gateway()
            consolidation = get_consolidation_engine()
            wiki_trigger = get_wiki_compile_trigger()

            agent = AgentLoop(
                llm_gateway=llm,
                semantic_memory=semantic,
                episodic_memory=episodic,
                consolidation_engine=consolidation,
                wiki_compile_trigger=wiki_trigger,
            )

            async for event in agent.stream(
                query=body.query,
                session_id=session_id,
                history=body.history,
                opts=body.opts,
            ):
                yield {
                    "event": event["type"],
                    "data": json.dumps(event["data"], default=str, ensure_ascii=False),
                }

        except Exception as e:
            logger.exception(f"Chat stream failed: {e}")
            yield {
                "event": "error",
                "data": json.dumps(
                    ErrorResponse(error="Stream failed", detail=str(e)).model_dump(),
                ),
            }

    return EventSourceResponse(event_generator())


@router.post("/chat", response_model=ChatResponse)
async def chat(request: Request, body: ChatRequest) -> ChatResponse:
    """Non-streaming chat endpoint (collects full response)."""
    session_id = body.session_id or str(uuid.uuid4())

    try:
        from yuanwork.orchestrator.agent import AgentLoop
        from yuanwork.api.deps import (
            get_consolidation_engine,
            get_episodic_memory,
            get_llm_gateway,
            get_semantic_memory,
            get_wiki_compile_trigger,
        )

        semantic = get_semantic_memory()
        episodic = get_episodic_memory()
        llm = get_llm_gateway()
        consolidation = get_consolidation_engine()
        wiki_trigger = get_wiki_compile_trigger()

        agent = AgentLoop(
            llm_gateway=llm,
            semantic_memory=semantic,
            episodic_memory=episodic,
            consolidation_engine=consolidation,
            wiki_compile_trigger=wiki_trigger,
        )

        result = await agent.run(
            query=body.query,
            session_id=session_id,
            history=body.history,
            opts=body.opts,
        )

        return ChatResponse(
            session_id=session_id,
            answer=result["answer"],
            citations=result["citations"],
            confidence=result["confidence"],
            refused=result.get("refused", False),
            refusal_reason=result.get("refusal_reason"),
            conflicts=result.get("conflicts", []),
            wiki_compile=result.get("wiki_compile"),
            trace=result.get("trace", []),
        )

    except Exception as e:
        logger.exception(f"Chat failed: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/chat/cancel/{session_id}")
async def cancel_chat(session_id: str, request: Request) -> dict:
    """Cancel an ongoing chat generation."""
    # TODO: implement cancellation registry
    return {"cancelled": session_id, "note": "Cancellation not yet implemented"}
