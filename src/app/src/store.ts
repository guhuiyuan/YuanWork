import { create } from "zustand";

export type View = "chat" | "wiki" | "graph";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations?: Citation[];
  confidence?: number;
  refused?: boolean;
  conflict?: string;
  timestamp: number;
}

export interface Citation {
  index: number;
  file_name: string;
  page?: number;
  slide_number?: number;
  text: string;
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
}

interface AppState {
  view: View;
  setView: (v: View) => void;

  messages: ChatMessage[];
  isStreaming: boolean;
  addMessage: (m: ChatMessage) => void;
  appendToLast: (chunk: string) => void;
  setStreaming: (s: boolean) => void;
  clearMessages: () => void;

  kbFiles: KbFile[];
  setKbFiles: (f: KbFile[]) => void;
  currentFolder: string | null;
  setCurrentFolder: (f: string | null) => void;

  wikiPages: WikiPage[];
  setWikiPages: (p: WikiPage[]) => void;
  selectedWikiPage: string | null;
  selectWikiPage: (id: string | null) => void;
}

export const useAppStore = create<AppState>((set) => ({
  view: "chat",
  setView: (v) => set({ view: v }),

  messages: [],
  isStreaming: false,
  addMessage: (m) => set((s) => ({ messages: [...s.messages, m] })),
  appendToLast: (chunk) =>
    set((s) => {
      const msgs = [...s.messages];
      if (msgs.length > 0) {
        const last = msgs[msgs.length - 1];
        if (last.role === "assistant") {
          msgs[msgs.length - 1] = { ...last, content: last.content + chunk };
        }
      }
      return { messages: msgs };
    }),
  setStreaming: (s) => set({ isStreaming: s }),
  clearMessages: () => set({ messages: [] }),

  kbFiles: [],
  setKbFiles: (f) => set({ kbFiles: f }),
  currentFolder: null,
  setCurrentFolder: (f) => set({ currentFolder: f }),

  wikiPages: [],
  setWikiPages: (p) => set({ wikiPages: p }),
  selectedWikiPage: null,
  selectWikiPage: (id) => set({ selectedWikiPage: id }),
}));
