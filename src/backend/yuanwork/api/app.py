"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from yuanwork import __version__
from yuanwork.api.deps import (
    set_episodic_memory,
    set_llm_gateway,
    set_profile_store,
    set_semantic_memory,
    set_sqlite_store,
)
from yuanwork.config import YuanWorkSettings

if TYPE_CHECKING:
    from yuanwork.db.sqlite import SQLiteStore
    from yuanwork.llm.gateway import LLMGateway
    from yuanwork.memory.episodic import EpisodicMemory
    from yuanwork.memory.profile import ProfileStore
    from yuanwork.memory.semantic import SemanticMemory


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: initialize resources on startup, cleanup on shutdown."""
    settings = app.state.settings

    logger.info("Initializing YuanWork components...")
    logger.info(f"   Data dir: {settings.data_dir}")
    logger.info(f"   Network mode: {settings.network_mode.name}")

    # Initialize SQLite store
    from yuanwork.db.sqlite import SQLiteStore

    sqlite_store = SQLiteStore(settings.data_dir / "store.db")
    await sqlite_store.initialize()
    set_sqlite_store(sqlite_store)
    logger.info("✓ SQLite store initialized")

    # Initialize LLM gateway
    from yuanwork.llm.gateway import LLMGateway

    llm_gateway = LLMGateway(settings)
    set_llm_gateway(llm_gateway)
    logger.info(f"✓ LLM gateway initialized ({len(llm_gateway.configured_models())} models)")

    # Initialize semantic memory (LanceDB)
    from yuanwork.memory.semantic import SemanticMemory

    semantic_memory = SemanticMemory(
        data_dir=settings.data_dir / "lancedb",
        embedding_model=settings.embedding_model,
        embedding_device=settings.embedding_device,
    )
    # Don't initialize embedding model at startup (lazy, saves boot time)
    set_semantic_memory(semantic_memory)
    logger.info("✓ Semantic memory initialized (embedding lazy-loaded)")

    # Initialize episodic memory
    from yuanwork.memory.episodic import EpisodicMemory

    episodic_memory = EpisodicMemory(sqlite_store)
    set_episodic_memory(episodic_memory)
    logger.info("✓ Episodic memory initialized")

    # Initialize profile store
    from yuanwork.memory.profile import ProfileStore

    profile_store = ProfileStore(sqlite_store)
    set_profile_store(profile_store)
    logger.info("✓ Profile store initialized")

    # Store references for shutdown
    app.state.sqlite_store = sqlite_store
    app.state.llm_gateway = llm_gateway
    app.state.semantic_memory = semantic_memory
    app.state.episodic_memory = episodic_memory
    app.state.profile_store = profile_store

    logger.info("🚀 YuanWork backend ready")

    try:
        yield
    finally:
        logger.info("Shutting down YuanWork components...")
        # Cleanup
        if hasattr(semantic_memory, "close"):
            await semantic_memory.close()
        if hasattr(sqlite_store, "close"):
            await sqlite_store.close()
        logger.info("✓ Shutdown complete")


def create_app(settings: YuanWorkSettings | None = None) -> FastAPI:
    """Create the FastAPI application.

    Args:
        settings: Optional settings instance. If None, loads from environment.

    Returns:
        Configured FastAPI app.
    """
    if settings is None:
        from yuanwork.config import get_settings

        settings = get_settings()

    app = FastAPI(
        title="YuanWork API",
        description="Local personal work assistant with wiki + memory engineering",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # Store settings on app state for lifespan access
    app.state.settings = settings

    # CORS middleware (Tauri webview is local, but allow localhost dev)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",  # Vite dev server
            "http://localhost:1420",  # Tauri default
            "tauri://localhost",
            "http://127.0.0.1:5173",
            "http://127.0.0.1:1420",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register routers
    from yuanwork.api.routes import chat, health, ingest, kb, memory, wiki

    app.include_router(health.router, prefix="/api/v1", tags=["health"])
    app.include_router(ingest.router, prefix="/api/v1", tags=["ingest"])
    app.include_router(kb.router, prefix="/api/v1", tags=["kb"])
    app.include_router(chat.router, prefix="/api/v1", tags=["chat"])
    app.include_router(memory.router, prefix="/api/v1", tags=["memory"])
    app.include_router(wiki.router, prefix="/api/v1", tags=["wiki"])

    # Global exception handler
    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request, exc):
        logger.exception(f"Unhandled exception in {request.url.path}: {exc}")
        return JSONResponse(
            status_code=500,
            content={
                "error": "Internal server error",
                "detail": str(exc),
                "code": "INTERNAL_ERROR",
            },
        )

    return app
