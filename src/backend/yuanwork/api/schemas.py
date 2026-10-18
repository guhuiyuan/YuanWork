"""Pydantic schemas for API request/response models."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


# ===== Health & Meta =====


class HealthResponse(BaseModel):
    """Health check response."""

    status: Literal["ok", "degraded", "down"] = "ok"
    version: str
    data_dir: str
    network_mode: str
    components: dict[str, dict[str, Any]] = Field(default_factory=dict)


class VersionResponse(BaseModel):
    """Version information."""

    version: str
    python_version: str
    platform: str


# ===== Knowledge Base =====


class FileType(str, Enum):
    """Supported file types."""

    PDF = "pdf"
    DOCX = "docx"
    PPTX = "pptx"
    XLSX = "xlsx"
    MD = "md"
    TXT = "txt"
    IMAGE = "image"
    UNKNOWN = "unknown"


class IngestDirRequest(BaseModel):
    """Request to ingest a directory."""

    path: str = Field(..., description="Absolute path to the directory")
    recursive: bool = Field(default=True)
    force: bool = Field(default=False, description="Re-ingest even if unchanged")


class IngestFileRequest(BaseModel):
    """Request to ingest a single file."""

    path: str = Field(..., description="Absolute path to the file")


class IngestJobResponse(BaseModel):
    """Response for an ingestion job."""

    job_id: str
    files_queued: int
    files_skipped: int = 0
    estimated_seconds: float | None = None


class IngestStatusResponse(BaseModel):
    """Status of an ingestion job."""

    job_id: str
    status: Literal["pending", "running", "completed", "failed"]
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    current_file: str | None = None
    files_done: int = 0
    files_total: int = 0
    error: str | None = None


class KBFileInfo(BaseModel):
    """Information about an ingested file."""

    doc_id: str
    file_name: str
    file_path: str
    file_type: FileType
    file_hash: str
    chunk_count: int
    size_bytes: int
    indexed_at: datetime
    last_modified: datetime


class KBFilesResponse(BaseModel):
    """Response listing knowledge base files."""

    files: list[KBFileInfo]
    total: int


# ===== Chat =====


class ChatMessage(BaseModel):
    """A single chat message."""

    role: Literal["user", "assistant", "system"]
    content: str


class ChatRequest(BaseModel):
    """Chat request."""

    query: str = Field(..., min_length=1, max_length=8000)
    session_id: str | None = Field(default=None)
    history: list[ChatMessage] = Field(default_factory=list)
    opts: dict[str, Any] = Field(default_factory=dict)


class Citation(BaseModel):
    """A citation to source material."""

    index: int = Field(..., description="Citation number [n]")
    file_name: str
    file_path: str
    page: int | None = None
    slide_number: int | None = None
    section: str | None = None
    text: str = Field(..., description="Quoted original text")
    chunk_id: str


class ConflictInfo(BaseModel):
    """Information about an LLM-vs-KB conflict."""

    type: Literal["llm_vs_kb", "episodic_vs_semantic"]
    topic: str | None = None
    llm_answer: str = ""
    kb_evidence: str = ""
    citation: Citation | None = None
    message: str = ""
    explanation: str | None = None


class ChatResponse(BaseModel):
    """Chat response (non-streaming fallback)."""

    session_id: str
    answer: str
    citations: list[Citation] = Field(default_factory=list)
    confidence: float = Field(..., ge=0.0, le=1.0)
    refused: bool = False
    refusal_reason: str | None = None
    conflicts: list[ConflictInfo] = Field(default_factory=list)
    wiki_compile: dict[str, Any] | None = None
    trace: list[dict[str, Any]] = Field(default_factory=list)


# ===== Memory =====


class EpisodicMemoryEntry(BaseModel):
    """A single episodic memory entry (past conversation)."""

    id: int
    session_id: str
    role: str
    content: str
    topic_tags: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    created_at: datetime


class EpisodicMemoryResponse(BaseModel):
    """Episodic memory search response."""

    entries: list[EpisodicMemoryEntry]
    query_topic: str | None = None


class ProfileEntry(BaseModel):
    """A single profile entry (user fact/preference)."""

    key: str
    value: str
    source_episode_id: int | None = None
    confidence: float
    updated_at: datetime
    expires_at: datetime | None = None


class ProfileResponse(BaseModel):
    """Profile response."""

    entries: list[ProfileEntry]
    total: int


class ConflictLog(BaseModel):
    """A logged conflict."""

    id: int
    conflict_type: str
    llm_answer: str
    kb_evidence: str
    resolution: str | None = None
    user_acknowledged: bool = False
    created_at: datetime


class ConflictLogResponse(BaseModel):
    """Conflict log response."""

    conflicts: list[ConflictLog]
    total: int


class ConsolidateRequest(BaseModel):
    """Request to trigger memory consolidation."""

    reason: str = Field(default="manual")
    force: bool = Field(default=False)


class ConsolidateResponse(BaseModel):
    """Response from consolidation."""

    episodes_processed: int
    duplicates_merged: int
    profile_updates: int
    conflicts_archived: int
    duration_ms: float


# ===== Wiki =====


class WikiPageInfo(BaseModel):
    """Information about a wiki page."""

    id: str
    title: str
    summary: str
    category: str
    tags: list[str] = Field(default_factory=list)
    source_doc_ids: list[str] = Field(default_factory=list)
    chunk_count: int = 0
    version: int
    created_at: datetime
    updated_at: datetime
    last_compiled_at: datetime | None = None


class WikiPageDetail(WikiPageInfo):
    """Full wiki page with content."""

    markdown: str


class WikiLink(BaseModel):
    """A bidirectional link between wiki entities."""

    source_id: str
    source_type: Literal["page", "chunk", "doc", "episode"]
    target_id: str
    target_type: Literal["page", "chunk", "doc", "episode"]
    link_type: str
    weight: float


class WikiBacklinks(BaseModel):
    """Backlinks for a wiki page."""

    page_id: str
    backlinks: list[WikiLink] = Field(default_factory=list)


class WikiPagesResponse(BaseModel):
    """Wiki pages list response."""

    pages: list[WikiPageInfo]
    total: int


# ===== Generic =====


class ErrorResponse(BaseModel):
    """Error response."""

    error: str
    detail: str | None = None
    code: str | None = None


class SuccessResponse(BaseModel):
    """Generic success response."""

    success: bool = True
    message: str | None = None
