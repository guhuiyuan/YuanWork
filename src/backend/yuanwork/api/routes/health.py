"""Health and version endpoints."""

from __future__ import annotations

import platform
import sys
from datetime import datetime

from fastapi import APIRouter, Request

from yuanwork import __version__
from yuanwork.api.schemas import HealthResponse, VersionResponse

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health_check(request: Request) -> HealthResponse:
    """Health check with component status."""
    settings = request.app.state.settings

    components: dict[str, dict] = {}

    # SQLite
    try:
        sqlite_store = getattr(request.app.state, "sqlite_store", None)
        if sqlite_store is not None:
            components["sqlite"] = {"status": "ok", "path": str(sqlite_store.db_path)}
        else:
            components["sqlite"] = {"status": "not_initialized"}
    except Exception as e:
        components["sqlite"] = {"status": "error", "error": str(e)}

    # LLM Gateway
    try:
        llm_gateway = getattr(request.app.state, "llm_gateway", None)
        if llm_gateway is not None:
            components["llm"] = {
                "status": "ok",
                "configured_models": llm_gateway.configured_models(),
            }
        else:
            components["llm"] = {"status": "not_initialized"}
    except Exception as e:
        components["llm"] = {"status": "error", "error": str(e)}

    # Semantic Memory (LanceDB)
    try:
        semantic = getattr(request.app.state, "semantic_memory", None)
        if semantic is not None:
            embedding_loaded = semantic.is_embedding_loaded()
            prewarm_in_flight = getattr(semantic, "_prewarm_in_flight", False)
            status = "ok" if embedding_loaded else ("loading" if prewarm_in_flight else "deferred")
            components["semantic_memory"] = {
                "status": status,
                "table_count": len(semantic.list_tables()),
                "embedding_loaded": embedding_loaded,
                "embedding_model": semantic.embedding_model_name,
            }
        else:
            components["semantic_memory"] = {"status": "not_initialized"}
    except Exception as e:
        components["semantic_memory"] = {"status": "error", "error": str(e)}

    # Episodic Memory
    try:
        episodic = getattr(request.app.state, "episodic_memory", None)
        if episodic is not None:
            count = await episodic.count()
            components["episodic_memory"] = {"status": "ok", "entry_count": count}
        else:
            components["episodic_memory"] = {"status": "not_initialized"}
    except Exception as e:
        components["episodic_memory"] = {"status": "error", "error": str(e)}

    # Overall status
    has_errors = any(c.get("status") == "error" for c in components.values())
    status = "down" if has_errors else "ok"

    return HealthResponse(
        status=status,
        version=__version__,
        data_dir=str(settings.data_dir),
        network_mode=settings.network_mode.name,
        components=components,
    )


@router.get("/version", response_model=VersionResponse)
async def version() -> VersionResponse:
    """Get version information."""
    return VersionResponse(
        version=__version__,
        python_version=sys.version.split()[0],
        platform=platform.platform(),
    )
