/**
 * Tauri command bridge - typed wrappers around the Rust commands.
 * In a browser-only environment, falls back to direct HTTP for dev.
 */

import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { parseSseFrame, parseSseFrames, type RawSseFrame } from "./sse";
import type {
  Citation,
  KbFile,
  WikiBacklinksPayload,
  WikiGraph,
  WikiPage,
  WikiPageDetail,
} from "../store";

const isTauri = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

async function callBackend<T>(path: string, init?: RequestInit): Promise<T> {
  if (isTauri) {
    // Use Tauri HTTP plugin in production; for now we proxy through Rust commands
    throw new Error("Direct HTTP from webview is not configured. Use invoke().");
  }
  const resp = await fetch(`http://127.0.0.1:8765/api/v1${path}`, {
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
  conflicts?: ConflictInfo[];
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
    resp = await fetch("http://127.0.0.1:8765/api/v1/chat/stream", {
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
  return null; // Browser fallback - user must enter manually
}

export async function ingestDirectory(
  path: string,
  recursive = true
): Promise<string> {
  if (isTauri) {
    return invoke<string>("ingest_directory", { path, recursive });
  }
  return callBackend<string>("/ingest/dir", {
    method: "POST",
    body: JSON.stringify({ path, recursive, force: false }),
  });
}

export async function listKbFiles(): Promise<KbFile[]> {
  if (isTauri) {
    return invoke<KbFile[]>("list_kb_files");
  }
  const body = await callBackend<{ files: KbFile[] }>("/kb/files");
  return body.files ?? [];
}

export async function deleteKbFile(docId: string): Promise<void> {
  if (isTauri) {
    return invoke<void>("delete_kb_file", { docId });
  }
  await callBackend(`/kb/files/${docId}`, { method: "DELETE" });
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

export async function getWikiGraph(): Promise<WikiGraph> {
  if (isTauri) {
    return invoke<WikiGraph>("get_wiki_graph");
  }
  return callBackend<WikiGraph>("/wiki/graph");
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
