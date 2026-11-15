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
    ApproveRuleResponse,
    ConflictLog,
    ConflictLogResponse,
    ConsolidateRequest,
    ConsolidateResponse,
    EpisodicMemoryEntry,
    EpisodicMemoryResponse,
    PendingRule,
    PendingRulesResponse,
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


@router.get("/memory/rules/pending", response_model=PendingRulesResponse)
async def list_pending_rules(
    limit: int = Query(default=50, ge=1, le=200),
) -> PendingRulesResponse:
    """The rules that are waiting for the user.

    A rule is a conclusion the integration layer drew across several
    interactions. It does not count as evidence until the user confirms
    it (ADR-039) - these are the ones to look at.
    """
    sqlite = get_sqlite_store()
    entities = await sqlite.list_entities(status="pending", limit=limit)
    rules = [
        entity
        for entity in entities
        if (entity.get("properties") or {}).get("kind") == "rule"
    ]

    return PendingRulesResponse(
        rules=[
            PendingRule(
                id=rule["id"],
                statement=rule["name"],
                source_event_ids=list(rule.get("source_event_ids") or []),
                first_seen=rule.get("first_seen"),
                last_seen=rule.get("last_seen"),
            )
            for rule in rules
        ],
        total=len(rules),
    )


@router.post("/memory/rules/{entity_id}/approve", response_model=ApproveRuleResponse)
async def approve_rule(entity_id: str) -> ApproveRuleResponse:
    """The user confirms a rule - it takes effect from now on.

    The events it was induced from stay with it, so the conclusion can
    still be read together with what it rests on.
    """
    from yuanwork.consolidation.engine import (
        RuleNotPending,
        UnknownRule,
        approve_rule as approve,
    )

    sqlite = get_sqlite_store()
    try:
        rule = await approve(sqlite, entity_id)
    except UnknownRule:
        raise HTTPException(status_code=404, detail=f"规律不存在：{entity_id}")
    except RuleNotPending:
        raise HTTPException(
            status_code=400, detail=f"这条规律不在待审状态：{entity_id}"
        )

    return ApproveRuleResponse(
        id=rule["id"],
        statement=rule["name"],
        status=rule.get("status", "active"),
        source_event_ids=list(rule.get("source_event_ids") or []),
        approved=True,
    )


@router.post("/memory/consolidate", response_model=ConsolidateResponse)
async def trigger_consolidation(body: ConsolidateRequest) -> ConsolidateResponse:
    """Trigger a consolidation round by hand.

    This is the third of the three ways a round starts (ADR-015): the
    turns pile up, nobody asks anything for a while, or the user asks
    for it here.
    """
    from yuanwork.api.deps import optional_consolidation_engine

    engine = optional_consolidation_engine()
    if engine is None:
        # The memory engineering layer is switched off - the round has
        # nowhere to run.
        return ConsolidateResponse(
            episodes_processed=0,
            duplicates_merged=0,
            profile_updates=0,
            conflicts_archived=0,
            duration_ms=0.0,
            triggered=False,
            reason="episodic memory disabled",
        )

    result = await engine.consolidate(reason=body.reason)

    return ConsolidateResponse(
        episodes_processed=result["episodes_processed"],
        duplicates_merged=result["duplicates_merged"],
        profile_updates=result["profile_updates"],
        conflicts_archived=result["conflicts_archived"],
        duration_ms=result["duration_ms"],
        events_read=result["events_read"],
        entities_induced=result["entities_induced"],
        rules_induced=result["rules_induced"],
        page_points_induced=result["page_points_induced"],
        induction_failures=result["induction_failures"],
        triggered=True,
        reason=body.reason,
    )
