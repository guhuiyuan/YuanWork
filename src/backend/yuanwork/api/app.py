"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from yuanwork import __version__
from yuanwork.api.deps import (
    set_consolidation_engine,
    set_episodic_memory,
    set_llm_gateway,
    set_profile_store,
    set_semantic_memory,
    set_sqlite_store,
    set_wiki_compile_trigger,
)
from yuanwork.config import YuanWorkSettings

if TYPE_CHECKING:
    from yuanwork.consolidation.engine import ConsolidationEngine
    from yuanwork.db.sqlite import SQLiteStore
    from yuanwork.llm.gateway import LLMGateway
    from yuanwork.memory.episodic import EpisodicMemory
    from yuanwork.memory.profile import ProfileStore
    from yuanwork.memory.semantic import SemanticMemory
    from yuanwork.wiki.trigger import WikiCompileTrigger


logger = logging.getLogger(__name__)


def _dotenv_candidates() -> list[Path]:
    """Places a .env file may live, most specific first.

    The project layout is ``<root>/src/backend/yuanwork/...`` and the
    .env lives at ``<root>/.env``. The desktop app starts the backend
    with cwd = ``src/backend``, so a cwd-relative lookup alone would miss
    the file and the LLM key would never reach os.environ.
    """
    candidates = [Path.cwd() / ".env"]
    package_root = Path(__file__).resolve().parent.parent
    for parent in package_root.parents[:6]:
        candidates.append(parent / ".env")
    return candidates


def _load_dotenv_into_environ() -> None:
    """Best-effort load of .env into os.environ.

    pydantic-settings only consumes .env for its own field resolution; it
    does not mutate os.environ. The LLM gateway (and any other library
    that reads env vars directly) needs the values in os.environ. This
    helper searches the project root for a .env file and applies the
    key=value pairs without overwriting already-set variables.
    """
    import os

    try:
        from dotenv import dotenv_values
    except ImportError:
        # python-dotenv not installed; nothing to do
        return

    loaded = 0
    for path in _dotenv_candidates():
        if not path.is_file():
            continue
        for key, value in dotenv_values(path).items():
            if value is None:
                continue
            # Don't clobber a value the user already exported.
            if key not in os.environ and key.upper() not in os.environ:
                os.environ[key] = value
                os.environ.setdefault(key.upper(), value)
                loaded += 1
    if loaded:
        logger.info(f"Loaded {loaded} env var(s) from .env into os.environ")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: initialize resources on startup, cleanup on shutdown."""
    settings = app.state.settings

    logger.info("Initializing YuanWork components...")
    logger.info(f"   Data dir: {settings.data_dir}")
    logger.info(f"   Network mode: {settings.network_mode.name}")

    # Load .env into os.environ so downstream libraries (litellm, httpx) can
    # see API keys referenced as api_key_env in models.yaml. pydantic-settings
    # reads .env into the model fields but does NOT export to os.environ.
    _load_dotenv_into_environ()

    # Initialize SQLite store
    from yuanwork.db.sqlite import SQLiteStore

    sqlite_store = SQLiteStore(settings.data_dir / "store.db")
    await sqlite_store.initialize()
    set_sqlite_store(sqlite_store)
    logger.info("✓ SQLite store initialized")

    # Initialize LLM gateway
    from yuanwork.llm.gateway import LLMGateway
    from yuanwork.privacy.audit import AuditLogger

    audit_logger = AuditLogger(settings.data_dir / "logs" / "audit.jsonl")
    logger.info(f"✓ Audit logger at {audit_logger.audit_path}")

    llm_gateway = LLMGateway(settings, audit_logger=audit_logger)
    set_llm_gateway(llm_gateway)
    logger.info(f"✓ LLM gateway initialized ({len(llm_gateway.configured_models())} models)")

    # Initialize semantic memory (LanceDB)
    from yuanwork.memory.semantic import SemanticMemory

    semantic_memory = SemanticMemory(
        data_dir=settings.data_dir / "lancedb",
        embedding_model=settings.embedding_model,
        embedding_device=settings.embedding_device,
    )
    # Open LanceDB connection. The bge-m3 embedding model is intentionally NOT
    # loaded here (lazy, saves boot time and 2.3GB model download on first use).
    await semantic_memory.initialize()
    set_semantic_memory(semantic_memory)
    logger.info("✓ Semantic memory initialized (embedding lazy-loaded)")

    # Optionally prewarm the embedding model. Off by default — the first
    # chat/ingest that needs embeddings will trigger the load, which on CPU
    # takes 1-3 minutes but doesn't block the rest of the server.
    # Set YUANWORK_PREWARM_EMBEDDING=1 to enable.
    if os.environ.get("YUANWORK_PREWARM_EMBEDDING") == "1":
        await semantic_memory.prewarm_embedding_model()
        logger.info("✓ Background embedding prewarm started")
    else:
        logger.info("ℹ Embedding model not prewarming (set YUANWORK_PREWARM_EMBEDDING=1 to enable)")

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

    # Initialize consolidation engine (integration layer)
    from yuanwork.consolidation.engine import ConsolidationEngine

    consolidation_engine = ConsolidationEngine(
        episodic_memory=episodic_memory,
        semantic_memory=semantic_memory,
        profile_store=profile_store,
        sqlite_store=sqlite_store,
        llm_gateway=llm_gateway,
    )
    set_consolidation_engine(consolidation_engine)
    logger.info("✓ Consolidation engine initialized")

    # Initialize wiki page compile trigger (Stage 2 differentiator).
    # Decides when to compile a Wiki page from the chunks surfaced during
    # a chat turn. Uses the LLM gateway for actual compilation; the
    # trigger itself is in-process state.
    from yuanwork.wiki.trigger import WikiCompileTrigger

    wiki_compile_trigger = WikiCompileTrigger(
        sqlite_store=sqlite_store,
        llm_gateway=llm_gateway,
    )
    set_wiki_compile_trigger(wiki_compile_trigger)
    logger.info("✓ Wiki compile trigger initialized")

    # Store references for shutdown
    app.state.sqlite_store = sqlite_store
    app.state.llm_gateway = llm_gateway
    app.state.audit_logger = audit_logger
    app.state.semantic_memory = semantic_memory
    app.state.episodic_memory = episodic_memory
    app.state.profile_store = profile_store
    app.state.consolidation_engine = consolidation_engine
    app.state.wiki_compile_trigger = wiki_compile_trigger

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
    from yuanwork.api.routes import audit, chat, health, ingest, kb, memory, wiki

    app.include_router(health.router, prefix="/api/v1", tags=["health"])
    app.include_router(ingest.router, prefix="/api/v1", tags=["ingest"])
    app.include_router(kb.router, prefix="/api/v1", tags=["kb"])
    app.include_router(chat.router, prefix="/api/v1", tags=["chat"])
    app.include_router(memory.router, prefix="/api/v1", tags=["memory"])
    app.include_router(wiki.router, prefix="/api/v1", tags=["wiki"])
    app.include_router(audit.router, prefix="/api/v1", tags=["audit"])

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
