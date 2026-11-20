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
  /** The terms in the answer - each one opens the text it stands for. */
  terms?: string[];
  /** Why the answer was refused - "insufficient_evidence", "unlocatable_evidence"
   *  or "generation_error: ..." */
  refusalReason?: string;
  conflict?: string;
  timestamp: number;
}

export interface CitationAnchor {
  /** "page" | "slide" | "section" | "sheet_row" | "heading_path" */
  kind: string;
  page?: number;
  slide?: number;
  section?: string;
  sheet?: string;
  row?: number;
  heading_path?: string[];
  /** Which text box (a Word report can keep its body in text boxes). */
  textbox_index?: number;
  /** Which picture (a scanned document is pictures). */
  image_index?: number;
}

export interface Citation {
  index: number;
  file_name: string;
  file_path?: string;
  page?: number;
  slide_number?: number;
  section?: string | null;
  /** The worksheet of a spreadsheet, and the row inside it. */
  sheet?: string | null;
  row?: number | null;
  /** The position in the document, in the shape the shell takes. */
  anchor?: CitationAnchor | null;
  /** The position in words, e.g. "第 12 页". */
  location?: string | null;
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

/**
 * A file the build could not read, and why.
 *
 * A count of failures says that something went wrong; the user needs to
 * know which file and why.
 */
export interface FailedFile {
  path: string;
  error: string;
}

/**
 * One entry of the workspace - a folder or a single file the user set up.
 *
 * The workspace is the scope of the whole app: the build runs over its
 * entries, and the sidebar lists them instead of the indexed files.
 */
export interface WorkspaceEntry {
  id: string;
  path: string;
  kind: "folder" | "file";
  /** Whether the entry takes part in the build. */
  included: boolean;
  added_at: string;
  /** The document this entry was indexed as, once it was built. */
  doc_id?: string | null;
}

/** One passage around a term, and where it came from. */
export interface TermContextItem {
  text: string;
  file_name: string;
  doc_id: string;
  chunk_id: string;
  /** Where in the document - "第 12 页", "Slide 3", ... */
  location: string;
  page: number | null;
}

/** Where a term appears in the knowledge base. */
export interface TermContext {
  term: string;
  /** Whether the knowledge base holds the term at all. */
  found: boolean;
  contexts: TermContextItem[];
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
  /** The file the page was read from has changed since it was compiled. */
  stale?: boolean;
  updated_at: string;
  last_compiled_at: string | null;
}

export interface WikiPageDetail extends WikiPage {
  markdown: string;
  source_doc_ids: string[];
}

/**
 * One paragraph of a page, and who wrote it.
 *
 * A paragraph the user wrote stays theirs - a later automatic
 * recompile replaces the model's paragraphs and leaves these alone.
 */
export interface WikiPageBlock {
  index: number;
  text: string;
  owner: "model" | "user";
}

/**
 * A conclusion on a page, and the place it was read from.
 *
 * The anchor is what makes the conclusion checkable - the reader can
 * open the original at that position.
 */
export interface WikiConclusion {
  id: string;
  text: string;
  anchor?: CitationAnchor | null;
  location: string;
  file_name: string;
  file_path: string;
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
  /**
   * How many files the knowledge base holds.
   *
   * The file list is paged, so its length is not the number - this is
   * the number the sidebar shows.
   */
  kbTotal: number;
  setKbTotal: (n: number) => void;
  /** How many slices the knowledge base holds. */
  kbChunkTotal: number;
  setKbChunkTotal: (n: number) => void;
  /**
   * The files the last build could not read, and how many there were.
   *
   * The list is capped by the backend - the count is the real one.
   */
  failedFiles: FailedFile[];
  setFailedFiles: (f: FailedFile[], total?: number, unsupported?: number) => void;
  /** How many files the last build could not read - the list is capped. */
  failedTotal: number;
  /**
   * How many of them are a format nobody supports.
   *
   * A workspace with a code project in it has a hundred thousand of
   * them - a different problem from "could not be read", and the user
   * should not have to tell them apart.
   */
  unsupportedTotal: number;
  /** True once the file list has been fetched - avoids a "no files yet" flash. */
  kbLoaded: boolean;
  setKbLoaded: (v: boolean) => void;
  /** Set when the file list could not be fetched at all (backend down). */
  kbError: string | null;
  setKbError: (e: string | null) => void;
  currentFolder: string | null;
  setCurrentFolder: (f: string | null) => void;

  /**
   * The workspace: the folders and the single files the user set up.
   *
   * The build runs over these entries, and the sidebar lists them -
   * the indexed files are what the build made of them.
   */
  workspaceEntries: WorkspaceEntry[];
  setWorkspaceEntries: (e: WorkspaceEntry[]) => void;
  /** True once the workspace has been fetched - avoids an empty flash. */
  workspaceLoaded: boolean;
  setWorkspaceLoaded: (v: boolean) => void;
  /** Set when the workspace could not be read at all (backend down). */
  workspaceError: string | null;
  setWorkspaceError: (e: string | null) => void;

  /** The folder currently being indexed, and how it is going. */
  ingesting: boolean;
  ingestMessage: string | null;
  ingestFolder: string | null;
  setIngestState: (s: {
    ingesting: boolean;
    message?: string | null;
    folder?: string | null;
  }) => void;
  /**
   * Bumped by `cancelIngest`. The poll loop compares it with the value
   * it started with, so a cancel reaches whichever component started
   * the job - the state is shared, so the cancel signal has to be too.
   */
  ingestToken: number;
  cancelIngest: () => void;

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
  kbTotal: 0,
  setKbTotal: (n) => set({ kbTotal: n }),
  kbChunkTotal: 0,
  setKbChunkTotal: (n) => set({ kbChunkTotal: n }),
  failedFiles: [],
  failedTotal: 0,
  unsupportedTotal: 0,
  setFailedFiles: (f, total, unsupported) =>
    set({
      failedFiles: f,
      failedTotal: total ?? f.length,
      unsupportedTotal: unsupported ?? 0,
    }),
  kbLoaded: false,
  setKbLoaded: (v) => set({ kbLoaded: v }),
  kbError: null,
  setKbError: (e) => set({ kbError: e }),
  currentFolder: null,
  setCurrentFolder: (f) => set({ currentFolder: f }),

  workspaceEntries: [],
  setWorkspaceEntries: (e) => set({ workspaceEntries: e }),
  workspaceLoaded: false,
  setWorkspaceLoaded: (v) => set({ workspaceLoaded: v }),
  workspaceError: null,
  setWorkspaceError: (e) => set({ workspaceError: e }),

  ingesting: false,
  ingestMessage: null,
  ingestFolder: null,
  setIngestState: ({ ingesting, message, folder }) =>
    set((s) => ({
      ingesting,
      ingestMessage: message === undefined ? s.ingestMessage : message,
      ingestFolder: folder === undefined ? s.ingestFolder : folder,
    })),
  ingestToken: 0,
  cancelIngest: () => set((s) => ({ ingestToken: s.ingestToken + 1 })),

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
