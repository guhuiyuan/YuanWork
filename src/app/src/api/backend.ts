/**
 * Tauri command bridge - typed wrappers around the Rust commands.
 * In a browser-only environment, falls back to direct HTTP for dev.
 */

import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { anchorOf } from "./citations.ts";
import { parseSseFrame, parseSseFrames, type RawSseFrame } from "./sse.ts";
import type {
  Citation,
  KbFile,
  RefusalPosition,
  TermContext,
  WikiBacklinksPayload,
  WikiConclusion,
  WikiGraph,
  WikiPage,
  WikiPageBlock,
  WikiPageDetail,
  WorkspaceEntry,
} from "../store.ts";

const isTauri = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

/**
 * The port the Python backend listens on.
 *
 * The desktop app never uses this: it talks to the backend through the
 * Rust commands, which know the port the sidecar was started on
 * (`YUANWORK_PORT`, default 8765). This only matters for browser dev
 * mode, where you can point it elsewhere with `?port=8770` in the URL
 * - handy when another project already holds 8765.
 */
const BACKEND_PORT = (() => {
  if (typeof window === "undefined") return 8765;
  const fromUrl = new URLSearchParams(window.location.search).get("port");
  const port = Number(fromUrl);
  return Number.isInteger(port) && port > 0 && port < 65536 ? port : 8765;
})();

const BACKEND_BASE = `http://127.0.0.1:${BACKEND_PORT}`;

async function callBackend<T>(path: string, init?: RequestInit): Promise<T> {
  if (isTauri) {
    // Use Tauri HTTP plugin in production; for now we proxy through Rust commands
    throw new Error("Direct HTTP from webview is not configured. Use invoke().");
  }
  const resp = await fetch(`${BACKEND_BASE}/api/v1${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!resp.ok) {
    throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);
  }
  return resp.json();
}

// ===== Chat =====

export interface ChatRequest {
  query: string;
  session_id?: string;
}

export interface ChatResponse {
  session_id?: string;
  answer: string;
  citations: Citation[];
  confidence: number;
  refused: boolean;
  refusal_reason?: string | null;
  /** The places a refusal points at - clickable, with the path. */
  refusal_positions?: RefusalPosition[];
  conflicts?: ConflictInfo[];
  /** The terms in the answer - each one opens the text it stands for. */
  terms?: string[];
  wiki_compile?: {
    triggered: boolean;
    page_id?: string;
    title?: string;
    reason?: string;
    error?: string;
  } | null;
  trace?: Record<string, unknown>[];
}

export interface ConflictInfo {
  type?: string;
  topic?: string;
  llm_says?: string;
  kb_says?: string;
  message?: string;
  explanation?: string;
}

/** Event types the backend's SSE stream can emit. */
export type StreamEventType =
  | "trace"
  | "token"
  | "answer"
  | "citations"
  | "refusal"
  | "conflict"
  | "done"
  | "error"
  | "ping";

export interface StreamEvent {
  type: StreamEventType;
  data: Record<string, unknown>;
}

/** Tauri channel the Rust bridge re-emits the backend's SSE events on. */
const CHAT_EVENT_CHANNEL = "chat:event";

/**
 * How long the backend may go without sending any bytes before the stream
 * is considered stalled.
 *
 * This is an *idle* timeout, not a total one: a long answer that keeps
 * trickling tokens is fine, only a silent connection is aborted.
 */
export const STREAM_IDLE_TIMEOUT_MS = 120_000;

// ===== SSE parsing (browser mode) =====

function toStreamEvent(frame: RawSseFrame): StreamEvent {
  let data: Record<string, unknown> = {};
  try {
    const parsed = JSON.parse(frame.data);
    if (parsed && typeof parsed === "object") {
      data = parsed as Record<string, unknown>;
    }
  } catch {
    data = { raw: frame.data };
  }
  return { type: frame.event as StreamEventType, data };
}

// ===== Chat streaming =====

/**
 * The requests that are running, by session.
 *
 * The browser path can abort its own request; the desktop path asks
 * the shell to stop reading the stream.
 */
const runningStreams = new Map<string, AbortController>();

/**
 * Stop a generation that is running.
 *
 * The backend is told as well - it stops pulling from the model even
 * if the connection is still open.
 */
export async function cancelChat(sessionId: string): Promise<void> {
  const controller = runningStreams.get(sessionId);
  runningStreams.delete(sessionId);
  controller?.abort(new Error("cancelled by the user"));

  if (isTauri) {
    await invoke<unknown>("cancel_chat", { sessionId });
    return;
  }
  await callBackend(`/chat/cancel/${encodeURIComponent(sessionId)}`, {
    method: "POST",
  });
}

/**
 * Stream a chat completion, delivering every backend event to `onEvent`
 * as it arrives.
 *
 * The answer text arrives as a series of `token` events; the resolved
 * promise carries the final response (answer, citations, confidence).
 */
export async function chatStream(
  req: ChatRequest,
  onEvent: (event: StreamEvent) => void
): Promise<ChatResponse> {
  if (isTauri) {
    // The Rust command reads the backend's SSE stream and re-emits each
    // event on this channel while it runs.
    const unlisten = await listen<StreamEvent>(CHAT_EVENT_CHANNEL, (e) => {
      onEvent(e.payload);
    });
    try {
      const json = await invoke<string>("chat_stream", { request: req });
      return JSON.parse(json);
    } finally {
      unlisten();
    }
  }

  // The idle timeout aborts the request if the backend goes silent. The
  // timer is reset every time bytes arrive, so a long but progressing
  // answer is never cut short.
  const controller = new AbortController();
  if (req.session_id) {
    runningStreams.set(req.session_id, controller);
  }
  let idleTimer: ReturnType<typeof setTimeout> | undefined;
  const armIdleTimer = () => {
    if (idleTimer !== undefined) clearTimeout(idleTimer);
    idleTimer = setTimeout(() => {
      controller.abort(
        new Error(`stream stalled for ${STREAM_IDLE_TIMEOUT_MS}ms`)
      );
    }, STREAM_IDLE_TIMEOUT_MS);
  };

  let resp: Response;
  try {
    armIdleTimer();
    resp = await fetch(`${BACKEND_BASE}/api/v1/chat/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(req),
      signal: controller.signal,
    });
  } finally {
    if (idleTimer !== undefined) clearTimeout(idleTimer);
  }
  if (!resp.ok) {
    throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);
  }
  if (!resp.body) {
    throw new Error("streaming response has no body");
  }

  const reader = resp.body.getReader();
  // `stream: true` keeps multi-byte characters intact when a chunk
  // boundary falls inside one.
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let finalResponse: ChatResponse | null = null;

  const handle = (frames: RawSseFrame[]) => {
    for (const frame of frames) {
      const event = toStreamEvent(frame);
      if (event.type === "done" && event.data.response) {
        finalResponse = event.data.response as ChatResponse;
      }
      onEvent(event);
    }
  };

  try {
    for (;;) {
      armIdleTimer();
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const { frames, rest } = parseSseFrames(buffer);
      buffer = rest;
      handle(frames);
    }
  } finally {
    if (idleTimer !== undefined) clearTimeout(idleTimer);
    if (req.session_id) runningStreams.delete(req.session_id);
  }

  // Flush whatever is left, including a frame with no trailing blank line.
  buffer += decoder.decode();
  const tail = parseSseFrames(buffer);
  handle(tail.frames);
  if (tail.rest.trim()) {
    const last = parseSseFrame(tail.rest);
    if (last) handle([last]);
  }

  if (!finalResponse) {
    throw new Error("stream ended without a final response");
  }
  return finalResponse;
}

// ===== Knowledge Base =====

export async function pickFolder(): Promise<string | null> {
  if (isTauri) {
    return invoke<string | null>("pick_folder");
  }
  // Browser dev mode has no native folder picker. Returning null here
  // would make "选择工作目录" a dead end with no feedback at all, so
  // ask for the path instead.
  const path = window.prompt(
    "浏览器开发模式没有原生目录选择器。\n请输入要索引的文件夹完整路径：",
    "D:\\"
  );
  return path && path.trim() ? path.trim() : null;
}

/**
 * Pick a single file.
 *
 * The desktop app has a native file dialog; the browser build has
 * none, so it asks for the path instead - same as the folder picker.
 */
export async function pickFile(): Promise<string | null> {
  if (isTauri) {
    try {
      const { open } = await import("@tauri-apps/plugin-dialog");
      // The dialog answers with one path, but a list when several
      // were allowed - both shapes are handled here.
      const picked = (await open({
        multiple: false,
        directory: false,
        title: "选择要加入工作域的文件",
      })) as string | string[] | null;
      if (typeof picked === "string") return picked;
      if (Array.isArray(picked) && picked.length > 0) return picked[0];
      return null;
    } catch (e) {
      // The dialog is not available (an older shell, a locked down
      // build). The webview has no prompt dialog to fall back on, so
      // the failure is passed on - the caller can say so.
      throw new Error(`无法打开文件选择器（${(e as Error).message}）`);
    }
  }
  const path = window.prompt(
    "浏览器开发模式没有原生文件选择器。\n请输入要加入工作域的文件完整路径：",
    "D:\\"
  );
  return path && path.trim() ? path.trim() : null;
}

export interface IngestJob {
  job_id: string;
  /**
   * How many files the job will work on - null when the job counts the
   * work itself (the workspace build walks the workspace in the
   * background). Null means "not counted yet", not "nothing to do".
   */
  files_queued: number | null;
  estimated_seconds: number | null;
}

export interface IngestStatus {
  job_id: string;
  status: "pending" | "running" | "completed" | "failed";
  progress: number;
  current_file: string | null;
  files_done: number;
  files_total: number;
  error: string | null;
  /** The files the build could not read, with the reason. */
  failed_files?: FailedFile[];
  /** How many there were in all - the list above is capped. */
  failed_total?: number;
  /**
   * How many of them are simply a format nobody supports.
   *
   * That is a different problem from "could not be read" - a workspace
   * with a code project in it has a hundred thousand of them.
   */
  unsupported_total?: number;
}

/**
 * A file the build could not read.
 *
 * A count of failures says that something went wrong; the user needs
 * to know *which* file and *why*.
 */
export interface FailedFile {
  path: string;
  error: string;
}

/**
 * Start indexing a directory.
 *
 * Indexing runs in the background - the returned job has to be polled
 * with `getIngestStatus` before the file list is complete.
 */
export async function ingestDirectory(
  path: string,
  recursive = true
): Promise<IngestJob> {
  if (isTauri) {
    const json = await invoke<string>("ingest_directory", { path, recursive });
    return JSON.parse(json) as IngestJob;
  }
  return callBackend<IngestJob>("/ingest/dir", {
    method: "POST",
    body: JSON.stringify({ path, recursive, force: false }),
  });
}

/** Poll an ingestion job. */
export async function getIngestStatus(jobId: string): Promise<IngestStatus> {
  if (isTauri) {
    return invoke<IngestStatus>("get_ingest_status", { jobId });
  }
  // Browser mode: the poll runs in a loop, so a hung request must not
  // be able to block it forever.
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 20_000);
  try {
    const resp = await fetch(
      `${BACKEND_BASE}/api/v1/ingest/status/${encodeURIComponent(jobId)}`,
      { signal: controller.signal }
    );
    if (!resp.ok) {
      throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);
    }
    return (await resp.json()) as IngestStatus;
  } finally {
    clearTimeout(timer);
  }
}

/**
 * The last build's status - null when there was none.
 *
 * The files the last build could not read are kept in the database, so
 * the app can still show them after a restart.
 */
export async function latestIngestStatus(): Promise<IngestStatus | null> {
  try {
    if (isTauri) {
      return await invoke<IngestStatus>("get_ingest_status", {
        jobId: "latest",
      });
    }
    const resp = await fetch(`${BACKEND_BASE}/api/v1/ingest/status/latest`);
    if (!resp.ok) return null;
    return (await resp.json()) as IngestStatus;
  } catch {
    // No build yet, or the backend is not reachable - the sidebar has
    // nothing to show, which is not an error worth a toast.
    return null;
  }
}

/** How many files a page of the knowledge base's file list holds. */
export const KB_PAGE_SIZE = 100;

export async function listKbFiles(): Promise<KbFile[]> {
  return listKbFilesPage(0, KB_PAGE_SIZE);
}

/**
 * One page of the knowledge base's files.
 *
 * The list is paged: a knowledge base can hold thousands of files, and
 * the sidebar shows them a page at a time. The number of files is not
 * the length of a page - `kbStats` is where the real count comes from.
 */
export async function listKbFilesPage(
  offset = 0,
  limit = KB_PAGE_SIZE
): Promise<KbFile[]> {
  if (isTauri) {
    return (
      (await invoke<KbFile[]>("list_kb_files", { limit, offset })) ?? []
    );
  }
  const body = await callBackend<{ files: KbFile[] }>(
    `/kb/files?limit=${limit}&offset=${offset}`
  );
  return body.files ?? [];
}

/** How much is in the knowledge base. */
export interface KbStats {
  total_files: number;
  total_chunks: number;
  by_type?: Record<string, number>;
  /** What the index really holds - the ledger may claim more. */
  index_chunks?: number;
  /** The slices the ledger claims and the index does not have. */
  missing_chunks?: number;
}

export async function kbStats(): Promise<KbStats> {
  if (isTauri) {
    const raw = await invoke<unknown>("kb_stats");
    return toJson<KbStats>(raw);
  }
  return callBackend<KbStats>("/kb/stats");
}

/**
 * Remove a document from the knowledge base.
 *
 * Only the index is touched - the file itself stays where it is on
 * disk.
 */
export async function deleteKbFile(docId: string): Promise<void> {
  if (isTauri) {
    return invoke<void>("delete_kb_file", { docId });
  }
  await callBackend(`/kb/files/${encodeURIComponent(docId)}`, { method: "DELETE" });
}

/**
 * Where a term appears in the knowledge base.
 *
 * The passages come from the original documents, each with the place
 * it was read from - the backend does not explain the term, it shows
 * the text it stands for (ADR-041).
 */
export async function getTermContext(term: string): Promise<TermContext> {
  if (isTauri) {
    const raw = await invoke<unknown>("get_term_context", { term });
    return toJson<TermContext>(raw);
  }
  return callBackend<TermContext>(
    `/kb/terms/${encodeURIComponent(term)}/context`
  );
}

// ===== Workspace =====

/**
 * The workspace: the folders and the single files the user set up.
 *
 * This is the scope of the app - the build runs over these entries,
 * and taking an entry out takes what it brought into the index with
 * it.
 */
export async function listWorkspaceEntries(): Promise<WorkspaceEntry[]> {
  if (isTauri) {
    const raw = await invoke<unknown>("list_workspace_entries");
    return toEntries(raw);
  }
  const body = await callBackend<{ entries?: WorkspaceEntry[] }>(
    "/workspace/entries"
  );
  return body.entries ?? [];
}

/**
 * Add a folder to the workspace.
 *
 * Adding it does not index it - the build runs over the whole
 * workspace, so the folder joins the next build.
 */
export async function addWorkspaceFolder(path: string): Promise<WorkspaceEntry> {
  if (isTauri) {
    const raw = await invoke<unknown>("add_workspace_folder", { path });
    return toEntry(raw);
  }
  return callBackend<WorkspaceEntry>("/workspace/folders", {
    method: "POST",
    body: JSON.stringify({ path, included: true }),
  });
}

/** Add a single file to the workspace, by hand. */
export async function addWorkspaceFile(path: string): Promise<WorkspaceEntry> {
  if (isTauri) {
    const raw = await invoke<unknown>("add_workspace_file", { path });
    return toEntry(raw);
  }
  return callBackend<WorkspaceEntry>("/workspace/files", {
    method: "POST",
    body: JSON.stringify({ path, included: true }),
  });
}

/**
 * Take an entry out of the workspace.
 *
 * The backend also clears what that entry brought into the index - the
 * files on disk are left alone.
 */
export async function removeWorkspaceEntry(entryId: string): Promise<void> {
  if (isTauri) {
    await invoke<void>("remove_workspace_entry", { entryId });
    return;
  }
  await callBackend(`/workspace/entries/${encodeURIComponent(entryId)}`, {
    method: "DELETE",
  });
}

/**
 * Build the index over everything the workspace covers.
 *
 * The build runs in the background - the returned job is polled with
 * `getIngestStatus`, exactly like the folder build.
 */
export async function rebuildWorkspace(): Promise<IngestJob> {
  if (isTauri) {
    const raw = await invoke<unknown>("rebuild_workspace");
    return toJson<IngestJob>(raw);
  }
  return callBackend<IngestJob>("/ingest/rebuild", { method: "POST" });
}

/** The entries - a list, or the list inside an envelope. */
function toEntries(raw: unknown): WorkspaceEntry[] {
  const value = toJson<unknown>(raw);
  if (Array.isArray(value)) return value as WorkspaceEntry[];
  const entries = (value as { entries?: WorkspaceEntry[] } | null)?.entries;
  return Array.isArray(entries) ? entries : [];
}

/** One entry - the object itself, or the text it was sent as. */
function toEntry(raw: unknown): WorkspaceEntry {
  return toJson<WorkspaceEntry>(raw);
}

// ===== Wiki =====

export async function listWikiPages(): Promise<WikiPage[]> {
  if (isTauri) {
    return invoke<WikiPage[]>("list_wiki_pages");
  }
  const body = await callBackend<{ pages: WikiPage[]; total: number }>(
    "/wiki/pages"
  );
  return body.pages ?? [];
}

export async function getWikiPage(pageId: string): Promise<WikiPageDetail> {
  if (isTauri) {
    return invoke<WikiPageDetail>("get_wiki_page", { pageId });
  }
  return callBackend<WikiPageDetail>(
    `/wiki/pages/${encodeURIComponent(pageId)}`
  );
}

export async function getWikiBacklinks(
  pageId: string
): Promise<WikiBacklinksPayload> {
  if (isTauri) {
    return invoke<WikiBacklinksPayload>("get_wiki_backlinks", { pageId });
  }
  return callBackend<WikiBacklinksPayload>(
    `/wiki/pages/${encodeURIComponent(pageId)}/backlinks`
  );
}

/**
 * The conclusions of a page, each with the place it was read from.
 *
 * The anchor is what lets the reader jump back to the original.
 */
export async function listPageConclusions(
  pageId: string
): Promise<WikiConclusion[]> {
  if (isTauri) {
    return invoke<WikiConclusion[]>("list_page_conclusions", { pageId });
  }
  const body = await callBackend<{ conclusions: WikiConclusion[] }>(
    `/wiki/pages/${encodeURIComponent(pageId)}/conclusions`
  );
  return body.conclusions ?? [];
}

export async function getWikiGraph(): Promise<WikiGraph> {
  if (isTauri) {
    return invoke<WikiGraph>("get_wiki_graph");
  }
  return callBackend<WikiGraph>("/wiki/graph");
}

/**
 * The paragraphs of a page, and who wrote each one.
 *
 * The ownership is what the backend recorded: a paragraph the user
 * wrote stays theirs when the page is compiled again.
 */
export async function listPageBlocks(pageId: string): Promise<WikiPageBlock[]> {
  if (isTauri) {
    const raw = await invoke<unknown>("list_page_blocks", { pageId });
    return toBlocks(raw);
  }
  const body = await callBackend<{ blocks: WikiPageBlock[] }>(
    `/wiki/pages/${encodeURIComponent(pageId)}/blocks`
  );
  return body.blocks ?? [];
}

/**
 * Save the user's edit of a page body.
 *
 * The backend decides which paragraphs became the user's - the ones
 * that changed - and leaves the rest as they were.
 */
export async function updateWikiPage(
  pageId: string,
  markdown: string
): Promise<WikiPageDetail> {
  if (isTauri) {
    const raw = await invoke<unknown>("update_wiki_page", { pageId, markdown });
    return toJson<WikiPageDetail>(raw);
  }
  return callBackend<WikiPageDetail>(`/wiki/pages/${encodeURIComponent(pageId)}`, {
    method: "PUT",
    body: JSON.stringify({ markdown }),
  });
}

/**
 * The JSON a command returned.
 *
 * Some commands hand back the parsed object, others the raw text -
 * both are read here so the caller does not have to care.
 */
function toJson<T>(raw: unknown): T {
  if (typeof raw !== "string") return raw as T;
  try {
    return JSON.parse(raw) as T;
  } catch {
    throw new Error("the backend sent something that is not JSON");
  }
}

/** The blocks of a page - a list, or the list inside an envelope. */
function toBlocks(raw: unknown): WikiPageBlock[] {
  if (Array.isArray(raw)) return raw as WikiPageBlock[];
  const blocks = (raw as { blocks?: WikiPageBlock[] } | null)?.blocks;
  return Array.isArray(blocks) ? blocks : [];
}

// ===== Opening the source =====

/** What was opened, and where. */
export interface OpenedLocation {
  path: string;
  location: string;
  method: string;
}

/**
 * Open the file a citation points at, at the position it points to.
 *
 * The way to get there is different per format - the shell knows how:
 * a PDF viewer understands a `#page=` fragment, Office is told through
 * its own automation.
 */
export async function openCitation(
  citation: Citation
): Promise<OpenedLocation> {
  const anchor = anchorOf(citation);
  const path = citation.file_path ?? "";
  if (isTauri) {
    return invoke<OpenedLocation>("open_at_anchor", { path, anchor });
  }
  throw new Error("浏览器里打不开本地文件，请在桌面端使用。");
}

/**
 * Open the file a conclusion was read from, at the place it was read.
 *
 * A conclusion the reader cannot check is a conclusion the reader has
 * to take on faith - this is the way back to the original.
 */
export async function openConclusion(
  conclusion: WikiConclusion
): Promise<OpenedLocation> {
  const path = conclusion.file_path ?? "";
  const anchor = conclusion.anchor ?? null;
  if (isTauri) {
    return invoke<OpenedLocation>("open_at_anchor", { path, anchor });
  }
  throw new Error("浏览器里打不开本地文件，请在桌面端使用。");
}

// ===== Memory =====

export async function searchEpisodic(topic?: string) {
  if (isTauri) {
    return invoke<unknown>("search_episodic", { topic: topic ?? null });
  }
  const qs = topic ? `?topic=${encodeURIComponent(topic)}` : "";
  return callBackend(`/memory/episodic${qs}`);
}

export async function triggerConsolidation() {
  if (isTauri) {
    return invoke<unknown>("trigger_consolidation");
  }
  return callBackend("/memory/consolidate", {
    method: "POST",
    body: JSON.stringify({ reason: "manual", force: false }),
  });
}
