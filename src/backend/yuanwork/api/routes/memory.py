"""Memory endpoints (episodic, profile, conflicts)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

from yuanwork.api.deps import (
    get_episodic_memory,
    get_profile_store,
    get_sqlite_store,
)
from yuanwork.api.schemas import (
    ConflictLog,
    ConflictLogResponse,
    ConsolidateRequest,
    ConsolidateResponse,
    EpisodicMemoryEntry,
    EpisodicMemoryResponse,
    ProfileEntry,
    ProfileResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/memory/episodic", response_model=EpisodicMemoryResponse)
async def search_episodic_memory(
    topic: str | None = Query(default=None, description="Topic filter"),
    limit: int = Query(default=20, ge=1, le=100),
) -> EpisodicMemoryResponse:
    """Search episodic memory (past conversations)."""
    episodic = get_episodic_memory()

    if topic:
        entries = await episodic.search_by_topic(topic, limit=limit)
    else:
        entries = await episodic.list_recent(limit=limit)

    return EpisodicMemoryResponse(
        entries=[
            EpisodicMemoryEntry(
                id=e["id"],
                session_id=e["session_id"],
                role=e["role"],
                content=e["content"],
                topic_tags=e.get("topic_tags", []),
                entities=e.get("entities", []),
                created_at=e["created_at"],
            )
            for e in entries
        ],
        query_topic=topic,
    )


@router.get("/memory/profile", response_model=ProfileResponse)
async def get_profile(key_prefix: str | None = Query(default=None)) -> ProfileResponse:
    """Get user profile entries (facts/preferences)."""
    profile = get_profile_store()
    entries = await profile.list_all(prefix=key_prefix)

    return ProfileResponse(
        entries=[
            ProfileEntry(
                key=e["key"],
                value=e["value"],
                source_episode_id=e.get("source_episode_id"),
                confidence=e.get("confidence", 1.0),
                updated_at=e["updated_at"],
                expires_at=e.get("expires_at"),
            )
            for e in entries
        ],
        total=len(entries),
    )


@router.delete("/memory/profile/{key}")
async def delete_profile_entry(key: str) -> dict:
    """Delete a profile entry."""
    profile = get_profile_store()
    deleted = await profile.delete(key)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Profile key not found: {key}")
    return {"deleted": key}


@router.get("/memory/conflicts", response_model=ConflictLogResponse)
async def list_conflicts(
    limit: int = Query(default=50, ge=1, le=500),
    include_acknowledged: bool = Query(default=False),
) -> ConflictLogResponse:
    """List logged LLM-vs-KB conflicts."""
    sqlite = get_sqlite_store()
    conflicts = await sqlite.list_conflicts(
        limit=limit,
        include_acknowledged=include_acknowledged,
    )

    return ConflictLogResponse(
        conflicts=[
            ConflictLog(
                id=c["id"],
                conflict_type=c["conflict_type"],
                llm_answer=c["llm_answer"],
                kb_evidence=c["kb_evidence"],
                resolution=c.get("resolution"),
                user_acknowledged=c.get("user_acknowledged", False),
                created_at=c["created_at"],
            )
            for c in conflicts
        ],
        total=len(conflicts),
    )


@router.post("/memory/consolidate", response_model=ConsolidateResponse)
async def trigger_consolidation(body: ConsolidateRequest) -> ConsolidateResponse:
    """Trigger memory consolidation (reflection)."""
    from yuanwork.consolidation.engine import ConsolidationEngine
    from yuanwork.api.deps import (
        get_episodic_memory,
        get_profile_store,
        get_semantic_memory,
        get_sqlite_store,
    )

    engine = ConsolidationEngine(
        episodic_memory=get_episodic_memory(),
        semantic_memory=get_semantic_memory(),
        profile_store=get_profile_store(),
        sqlite_store=get_sqlite_store(),
    )

    result = await engine.run(reason=body.reason, force=body.force)

    return ConsolidateResponse(
        episodes_processed=result["episodes_processed"],
        duplicates_merged=result["duplicates_merged"],
        profile_updates=result["profile_updates"],
        conflicts_archived=result["conflicts_archived"],
        duration_ms=result["duration_ms"],
    )
