"""FastAPI dependency injection utilities."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends

from yuanwork.config import YuanWorkSettings, get_settings

if TYPE_CHECKING:
    from yuanwork.db.sqlite import SQLiteStore
    from yuanwork.llm.gateway import LLMGateway
    from yuanwork.memory.semantic import SemanticMemory
    from yuanwork.memory.episodic import EpisodicMemory
    from yuanwork.memory.profile import ProfileStore


# Type aliases for cleaner endpoint signatures
SettingsDep = Annotated[YuanWorkSettings, Depends(get_settings)]


# Lazy-loaded singleton dependencies
# These are populated by the app factory at startup

_llm_gateway: "LLMGateway | None" = None
_semantic_memory: "SemanticMemory | None" = None
_episodic_memory: "EpisodicMemory | None" = None
_profile_store: "ProfileStore | None" = None
_sqlite_store: "SQLiteStore | None" = None


def set_llm_gateway(gateway: "LLMGateway") -> None:
    """Set the LLM gateway singleton."""
    global _llm_gateway
    _llm_gateway = gateway


def get_llm_gateway() -> "LLMGateway":
    """Get the LLM gateway instance."""
    if _llm_gateway is None:
        raise RuntimeError("LLM gateway not initialized")
    return _llm_gateway


def set_semantic_memory(memory: "SemanticMemory") -> None:
    """Set the semantic memory singleton."""
    global _semantic_memory
    _semantic_memory = memory


def get_semantic_memory() -> "SemanticMemory":
    """Get the semantic memory instance."""
    if _semantic_memory is None:
        raise RuntimeError("Semantic memory not initialized")
    return _semantic_memory


def set_episodic_memory(memory: "EpisodicMemory") -> None:
    """Set the episodic memory singleton."""
    global _episodic_memory
    _episodic_memory = memory


def get_episodic_memory() -> "EpisodicMemory":
    """Get the episodic memory instance."""
    if _episodic_memory is None:
        raise RuntimeError("Episodic memory not initialized")
    return _episodic_memory


def set_profile_store(store: "ProfileStore") -> None:
    """Set the profile store singleton."""
    global _profile_store
    _profile_store = store


def get_profile_store() -> "ProfileStore":
    """Get the profile store instance."""
    if _profile_store is None:
        raise RuntimeError("Profile store not initialized")
    return _profile_store


def set_sqlite_store(store: "SQLiteStore") -> None:
    """Set the SQLite store singleton."""
    global _sqlite_store
    _sqlite_store = store


def get_sqlite_store() -> "SQLiteStore":
    """Get the SQLite store instance."""
    if _sqlite_store is None:
        raise RuntimeError("SQLite store not initialized")
    return _sqlite_store


def reset_dependencies() -> None:
    """Reset all dependencies (for testing)."""
    global _llm_gateway, _semantic_memory, _episodic_memory, _profile_store, _sqlite_store
    _llm_gateway = None
    _semantic_memory = None
    _episodic_memory = None
    _profile_store = None
    _sqlite_store = None
