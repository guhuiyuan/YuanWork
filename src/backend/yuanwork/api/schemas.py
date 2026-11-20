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
    #: Which instance answers here. The desktop app hands its backend a
    #: token at startup and checks it here, so a leftover server from an
    #: earlier run is never mistaken for the one just started.
    instance_token: str = ""


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


class FailedFile(BaseModel):
    """A file the build could not read, and why.

    A count of failures says that something went wrong; the user needs
    to know which file and why (AC-E-004).
    """

    path: str
    error: str


class IngestStatusResponse(BaseModel):
    """Status of an ingestion job."""

    job_id: str
    status: Literal["pending", "running", "completed", "failed"]
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    current_file: str | None = None
    files_done: int = 0
    files_total: int = 0
    error: str | None = None
    #: The files the build could not read.
    failed_files: list[FailedFile] = Field(default_factory=list)
    #: How many files there were in all - the list above is capped, so
    #: this is the number that says whether anything is missing.
    failed_total: int = 0
    #: How many of them are simply a format nobody supports. That is a
    #: different problem from "could not be read", and the user should
    #: not have to tell them apart.
    unsupported_total: int = 0


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
    sheet: str | None = Field(default=None, description="Excel sheet name")
    row: int | None = Field(default=None, description="Excel row number")
    anchor: dict[str, Any] | None = Field(
        default=None, description="The position of the quote in its document"
    )
    location: str | None = Field(
        default=None, description="Human-readable position, e.g. 第 12 页"
    )
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


class InteractionRequest(BaseModel):
    """What the client reports about an interaction.

    The user opening a citation, accepting an answer or rejecting it
    happens on the client; the server records what it is told.
    """

    session_id: str = ""
    subject: str = ""
    detail: str = ""
    topic: str = ""
    importance: float = Field(default=0.5, ge=0.0, le=1.0)


class RefusalPosition(BaseModel):
    """One place the answer most likely is.

    It carries the path, so the reader can click it and look at the
    original - a refusal that names a place it will not let you open
    is a dead end (TASK-085).
    """

    file_name: str = ""
    file_path: str = ""
    location: str = ""


class ChatResponse(BaseModel):
    """Chat response (non-streaming fallback)."""

    session_id: str
    answer: str
    citations: list[Citation] = Field(default_factory=list)
    confidence: float = Field(..., ge=0.0, le=1.0)
    refused: bool = False
    refusal_reason: str | None = None
    #: The places a refusal points at - clickable, with the path.
    refusal_positions: list[RefusalPosition] = Field(default_factory=list)
    conflicts: list[ConflictInfo] = Field(default_factory=list)
    wiki_compile: dict[str, Any] | None = None
    # The terms the reader may not know - each one opens the text it
    # stands for (ADR-041).
    terms: list[str] = Field(default_factory=list)
    trace: list[dict[str, Any]] = Field(default_factory=list)


# ===== The context of a term =====


class TermContextItem(BaseModel):
    """One passage around a term, and where it came from."""

    text: str
    file_name: str = ""
    doc_id: str = ""
    chunk_id: str = ""
    #: Where in the document - "第 12 页", "Slide 3", ...
    location: str = ""
    page: int | None = None


class TermContextResponse(BaseModel):
    """Where a term appears in the knowledge base."""

    term: str
    #: Whether the knowledge base holds the term at all.
    found: bool = False
    contexts: list[TermContextItem] = Field(default_factory=list)


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
    # Accepted and ignored: the round does the same work either way -
    # the trigger decides *when* it runs, not *what* it does.
    force: bool = Field(default=False)


class ConsolidateResponse(BaseModel):
    """Response from consolidation."""

    episodes_processed: int
    duplicates_merged: int
    profile_updates: int
    conflicts_archived: int
    duration_ms: float
    # What the round induced out of the recent interactions.
    events_read: int = 0
    entities_induced: int = 0
    rules_induced: int = 0
    page_points_induced: int = 0
    induction_failures: int = 0
    # Whether a round ran at all, and why.
    triggered: bool = True
    reason: str = "manual"


# ===== The rules waiting for the user =====


class PendingRule(BaseModel):
    """A rule the integration layer found, waiting for the user."""

    id: str
    statement: str
    source_event_ids: list[int] = Field(default_factory=list)
    first_seen: str | None = None
    last_seen: str | None = None


class PendingRulesResponse(BaseModel):
    """The rules waiting for the user."""

    rules: list[PendingRule] = Field(default_factory=list)
    total: int = 0


class ApproveRuleResponse(BaseModel):
    """A rule the user confirmed."""

    id: str
    statement: str
    status: str
    source_event_ids: list[int] = Field(default_factory=list)
    approved: bool = True


# ===== Wiki =====


class WikiPageInfo(BaseModel):
    """Information about a wiki page."""

    id: str
    title: str
    summary: str
    key_points: list[str] = Field(default_factory=list)
    category: str
    tags: list[str] = Field(default_factory=list)
    source_doc_ids: list[str] = Field(default_factory=list)
    chunk_count: int = 0
    version: int
    #: The file the page was read from has changed since it was
    #: compiled (AC-E-015). The page is kept - it is what the document
    #: said - but the reader has to know.
    stale: bool = False
    created_at: datetime
    updated_at: datetime
    last_compiled_at: datetime | None = None


class WikiPageDetail(WikiPageInfo):
    """Full wiki page with content."""

    markdown: str


class WikiPageBlock(BaseModel):
    """A paragraph of a page, and who wrote it."""

    #: Where the paragraph sits in the page.
    index: int
    #: The text of the paragraph.
    text: str
    #: "model" - the model wrote it ; "user" - the user wrote it, and a
    #: later automatic recompile will not replace it (AC-F-027).
    owner: Literal["model", "user"] = "model"


class WikiPageBlocksResponse(BaseModel):
    """The blocks of a page - its paragraphs, one by one."""

    page_id: str
    blocks: list[WikiPageBlock] = Field(default_factory=list)
    total: int = 0


class WikiConclusion(BaseModel):
    """A conclusion on a page, and the place it was read from."""

    id: str
    page_id: str
    text: str
    #: The position in the original document (ADR-032).
    anchor: dict[str, Any] = Field(default_factory=dict)
    #: The position in words, e.g. "第 12 页".
    location: str = ""
    #: The file the conclusion was read from.
    file_name: str = ""
    file_path: str = ""
    #: Which segment of the document it came from.
    segment_index: int = 0
    active: bool = True


class WikiConclusionsResponse(BaseModel):
    """The conclusions of a page."""

    page_id: str
    conclusions: list[WikiConclusion] = Field(default_factory=list)


class DocumentTierRequest(BaseModel):
    """The user's own choice of layer for a document."""

    #: "core" = the documents with conclusions of their own ;
    #: "longtail" = the scattered, everyday material.
    tier: Literal["core", "longtail"]


class WikiPageEdit(BaseModel):
    """The user's edit of a page body."""

    markdown: str


class WikiCompileResponse(BaseModel):
    """The outcome of a manual page compilation."""

    #: Whether a page was written.
    compiled: bool
    page_id: str = ""
    title: str = ""
    #: Why nothing was compiled - "no_evidence", "search_failed",
    #: "compile_failed". The detail stays in the log.
    reason: str = ""
    chunk_count: int = 0


class WikiLink(BaseModel):
    """A bidirectional link between wiki entities."""

    source_id: str
    source_type: Literal["page", "chunk", "doc", "episode"]
    target_id: str
    target_type: Literal["page", "chunk", "doc", "episode"]
    link_type: str
    weight: float
    #: The name of the other end - a link to a page id is not something
    #: a reader can use.
    title: str = ""


class WikiBacklinks(BaseModel):
    """Backlinks for a wiki page."""

    page_id: str
    backlinks: list[WikiLink] = Field(default_factory=list)


class WikiPagesResponse(BaseModel):
    """Wiki pages list response."""

    pages: list[WikiPageInfo]
    total: int


class WikiGraphNode(BaseModel):
    """A node in the wiki graph (page or entity)."""

    id: str
    label: str
    type: str
    category: str | None = None
    weight: int = 1
    #: The key points of a page - what the page says, in short.
    key_points: list[str] = Field(default_factory=list)


class WikiGraphEdge(BaseModel):
    """An edge in the wiki graph (link between two nodes)."""

    source: str
    target: str
    type: str
    weight: float = 1.0


class WikiGraphResponse(BaseModel):
    """Wiki graph payload (nodes + edges) for visualization."""

    nodes: list[WikiGraphNode]
    edges: list[WikiGraphEdge]


# ===== Workspace =====


class WorkspacePathRequest(BaseModel):
    """Request to add a folder or a single file to the workspace."""
    path: str = Field(..., description="Absolute path to the folder or the file")
    included: bool = Field(default=True, description="Whether to index it")


class WorkspaceEntryInfo(BaseModel):
    """One entry of the workspace."""
    id: str
    path: str
    kind: Literal["folder", "file"]
    included: bool
    added_at: str
    doc_id: str | None = None


class WorkspaceEntriesResponse(BaseModel):
    """The workspace entries."""
    entries: list[WorkspaceEntryInfo]
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
