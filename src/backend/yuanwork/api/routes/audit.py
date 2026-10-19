"""Audit log routes — view and clear the LLM-call audit log.

The audit log is a JSONL file at `<data_dir>/logs/audit.jsonl`. Each line
is one LLM call. The log includes model, task type, prompt hash (NOT the
prompt text), response summary, token counts, duration, and success/failure.

Endpoints:
  GET    /api/v1/audit           — list entries (optional ?limit=)
  GET    /api/v1/audit/stats     — aggregate counts (calls / tokens / failures)
  DELETE /api/v1/audit           — clear all entries
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter(prefix="/audit", tags=["audit"])


def _get_audit_logger(request: Request) -> Any:
    """Pull the AuditLogger from app state, or raise 503 if not initialized."""
    audit_logger = getattr(request.app.state, "audit_logger", None)
    if audit_logger is None:
        raise HTTPException(
            status_code=503,
            detail="Audit log not initialized (server may have started without one)",
        )
    return audit_logger


@router.get("", summary="List audit log entries")
async def list_audit_entries(
    request: Request,
    limit: int = Query(
        default=100,
        ge=1,
        le=1000,
        description="Maximum number of recent entries to return (newest first)",
    ),
) -> dict[str, Any]:
    """Return the most recent audit entries (newest first).

    Each entry has: timestamp, type, model, task_type, prompt_summary
    (starts with '#<16hexhash>'), response_summary, token counts, duration,
    success flag, and optional error.
    """
    audit_logger = _get_audit_logger(request)
    all_entries = audit_logger.read_all()
    # Newest first
    entries = list(reversed(all_entries[-limit:]))
    return {
        "count": len(entries),
        "total": len(all_entries),
        "entries": entries,
    }


@router.get("/stats", summary="Aggregate audit statistics")
async def audit_stats(request: Request) -> dict[str, Any]:
    """Aggregate counts across the whole audit log.

    Useful for the UI dashboard: 'so far you've called the LLM N times
    across M models, totalling K tokens, with F failures'.
    """
    audit_logger = _get_audit_logger(request)
    entries = audit_logger.read_all()
    llm_calls = [e for e in entries if e.get("type") == "llm_call"]
    by_model: dict[str, int] = {}
    by_task: dict[str, int] = {}
    total_prompt_tokens = 0
    total_completion_tokens = 0
    failures = 0
    for e in llm_calls:
        m = e.get("model", "unknown")
        by_model[m] = by_model.get(m, 0) + 1
        t = e.get("task_type", "unknown")
        by_task[t] = by_task.get(t, 0) + 1
        total_prompt_tokens += e.get("prompt_tokens", 0) or 0
        total_completion_tokens += e.get("completion_tokens", 0) or 0
        if not e.get("success", True):
            failures += 1
    return {
        "total_entries": len(entries),
        "llm_calls": len(llm_calls),
        "failures": failures,
        "total_prompt_tokens": total_prompt_tokens,
        "total_completion_tokens": total_completion_tokens,
        "by_model": by_model,
        "by_task_type": by_task,
    }


@router.delete("", summary="Clear the audit log")
async def clear_audit_log(request: Request) -> dict[str, Any]:
    """Delete all audit entries. Returns the count that was removed."""
    audit_logger = _get_audit_logger(request)
    before = len(audit_logger.read_all())
    audit_logger.clear()
    return {"cleared": before}
