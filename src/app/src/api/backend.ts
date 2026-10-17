/**
 * Tauri command bridge - typed wrappers around the Rust commands.
 * In a browser-only environment, falls back to direct HTTP for dev.
 */

import { invoke } from "@tauri-apps/api/core";
import type { ChatMessage, Citation, KbFile, WikiPage } from "../store";

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
  answer: string;
  citations: Citation[];
  confidence: number;
  refused: boolean;
  conflict?: string | null;
}

export async function chat(req: ChatRequest): Promise<ChatResponse> {
  if (isTauri) {
    const json = await invoke<string>("chat_stream", { request: req });
    return JSON.parse(json);
  }
  return callBackend<ChatResponse>("/chat", {
    method: "POST",
    body: JSON.stringify(req),
  });
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
  const body = await callBackend<{ pages: WikiPage[] }>("/wiki/pages");
  return body.pages ?? [];
}

export async function getWikiPage(pageId: string) {
  if (isTauri) {
    return invoke<unknown>("get_wiki_page", { pageId });
  }
  return callBackend(`/wiki/pages/${pageId}`);
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
