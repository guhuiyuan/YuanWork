import { create } from "zustand";

export type View = "chat" | "wiki" | "graph";

export type ToastKind = "info" | "error" | "success";

export interface Toast {
  id: number;
  text: string;
  kind: ToastKind;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations?: Citation[];
  confidence?: number;
  refused?: boolean;
  /** Why the answer was refused - "low_confidence" or "generation_error: ..." */
  refusalReason?: string;
  conflict?: string;
  timestamp: number;
}

export interface Citation {
  index: number;
  file_name: string;
  file_path?: string;
  page?: number;
  slide_number?: number;
  section?: string | null;
  text: string;
  chunk_id?: string;
}

export interface KbFile {
  doc_id: string;
  file_name: string;
  file_path: string;
  file_type: string;
  chunk_count: number;
}

export interface WikiPage {
  id: string;
  title: string;
  summary: string;
  category: string;
  tags: string[];
  source_doc_ids: string[];
  chunk_count: number;
  version: number;
  updated_at: string;
  last_compiled_at: string | null;
}

export interface WikiPageDetail extends WikiPage {
  markdown: string;
  source_doc_ids: string[];
}

export interface WikiLink {
  source_id: string;
  source_type: "page" | "chunk" | "doc" | "episode";
  target_id: string;
  target_type: "page" | "chunk" | "doc" | "episode";
  link_type: string;
  weight: number;
}

export interface WikiBacklinksPayload {
  page_id: string;
  backlinks: WikiLink[];
}

export interface WikiGraphNode {
  id: string;
  label: string;
  type: string; // "page" | "entity_person" | "entity_concept" | ...
  category?: string;
  weight: number;
}

export interface WikiGraphEdge {
  source: string;
  target: string;
  type: string;
  weight: number;
}

export interface WikiGraph {
  nodes: WikiGraphNode[];
  edges: WikiGraphEdge[];
}

interface AppState {
  view: View;
  setView: (v: View) => void;

  messages: ChatMessage[];
  isStreaming: boolean;
  addMessage: (m: ChatMessage) => void;
  appendToMessage: (id: string, chunk: string) => void;
  updateMessage: (id: string, patch: Partial<ChatMessage>) => void;
  setStreaming: (s: boolean) => void;
  clearMessages: () => void;

  kbFiles: KbFile[];
  setKbFiles: (f: KbFile[]) => void;
  /** True once the file list has been fetched - avoids a "no files yet" flash. */
  kbLoaded: boolean;
  setKbLoaded: (v: boolean) => void;
  currentFolder: string | null;
  setCurrentFolder: (f: string | null) => void;

  /** Transient message shown to the user (e.g. "已从知识库移除 X"). */
  toast: Toast | null;
  showToast: (text: string, kind?: ToastKind) => void;
  dismissToast: () => void;

  wikiPages: WikiPage[];
  setWikiPages: (p: WikiPage[]) => void;
  selectedWikiPage: string | null;
  selectWikiPage: (id: string | null) => void;

  wikiPageDetail: WikiPageDetail | null;
  setWikiPageDetail: (d: WikiPageDetail | null) => void;

  wikiBacklinks: WikiBacklinksPayload | null;
  setWikiBacklinks: (b: WikiBacklinksPayload | null) => void;

  wikiGraph: WikiGraph | null;
  setWikiGraph: (g: WikiGraph | null) => void;
}

export const useAppStore = create<AppState>((set) => ({
  view: "chat",
  setView: (v) => set({ view: v }),

  messages: [],
  isStreaming: false,
  addMessage: (m) => set((s) => ({ messages: [...s.messages, m] })),
  appendToMessage: (id, chunk) =>
    set((s) => ({
      messages: s.messages.map((m) =>
        m.id === id ? { ...m, content: m.content + chunk } : m
      ),
    })),
  updateMessage: (id, patch) =>
    set((s) => ({
      messages: s.messages.map((m) => (m.id === id ? { ...m, ...patch } : m)),
    })),
  setStreaming: (s) => set({ isStreaming: s }),
  clearMessages: () => set({ messages: [] }),

  kbFiles: [],
  setKbFiles: (f) => set({ kbFiles: f }),
  kbLoaded: false,
  setKbLoaded: (v) => set({ kbLoaded: v }),
  currentFolder: null,
  setCurrentFolder: (f) => set({ currentFolder: f }),

  toast: null,
  showToast: (text, kind = "info") =>
    set({ toast: { id: Date.now(), text, kind } }),
  dismissToast: () => set({ toast: null }),

  wikiPages: [],
  setWikiPages: (p) => set({ wikiPages: p }),
  selectedWikiPage: null,
  selectWikiPage: (id) => set({ selectedWikiPage: id }),

  wikiPageDetail: null,
  setWikiPageDetail: (d) => set({ wikiPageDetail: d }),

  wikiBacklinks: null,
  setWikiBacklinks: (b) => set({ wikiBacklinks: b }),

  wikiGraph: null,
  setWikiGraph: (g) => set({ wikiGraph: g }),
}));
