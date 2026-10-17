import { useEffect, useState } from "react";
import { useAppStore } from "../store";
import { listKbFiles, listWikiPages, pickFolder, ingestDirectory } from "../api/backend";

export function Sidebar() {
  const view = useAppStore((s) => s.view);
  const setView = useAppStore((s) => s.setView);
  const kbFiles = useAppStore((s) => s.kbFiles);
  const setKbFiles = useAppStore((s) => s.setKbFiles);
  const wikiPages = useAppStore((s) => s.wikiPages);
  const setWikiPages = useAppStore((s) => s.setWikiPages);
  const currentFolder = useAppStore((s) => s.currentFolder);
  const setCurrentFolder = useAppStore((s) => s.setCurrentFolder);
  const [ingesting, setIngesting] = useState(false);
  const [ingestMsg, setIngestMsg] = useState<string | null>(null);

  // Load files on mount
  useEffect(() => {
    listKbFiles().then(setKbFiles).catch(() => {});
    listWikiPages().then(setWikiPages).catch(() => {});
  }, [setKbFiles, setWikiPages]);

  async function handlePickAndIngest() {
    const folder = await pickFolder();
    if (!folder) return;
    setCurrentFolder(folder);
    setIngesting(true);
    setIngestMsg("正在索引…");
    try {
      const result = await ingestDirectory(folder, true);
      setIngestMsg(result);
      const files = await listKbFiles();
      setKbFiles(files);
    } catch (e) {
      setIngestMsg(`失败：${(e as Error).message}`);
    } finally {
      setIngesting(false);
    }
  }

  return (
    <>
      <div className="panel-header">
        <span>导航</span>
      </div>

      <div style={{ padding: "8px 12px", display: "flex", flexDirection: "column", gap: 4 }}>
        <button
          className={`nav-item ${view === "chat" ? "nav-item--active" : ""}`}
          onClick={() => setView("chat")}
        >
          💬 对话
        </button>
        <button
          className={`nav-item ${view === "wiki" ? "nav-item--active" : ""}`}
          onClick={() => setView("wiki")}
        >
          📖 Wiki
          <span className="nav-item__count">{wikiPages.length}</span>
        </button>
        <button
          className={`nav-item ${view === "graph" ? "nav-item--active" : ""}`}
          onClick={() => setView("graph")}
        >
          🕸 图谱
        </button>
      </div>

      <div className="panel-header" style={{ borderTop: "1px solid var(--border)" }}>
        <span>知识库</span>
        <button className="btn btn--primary" onClick={handlePickAndIngest} disabled={ingesting}>
          {ingesting ? "索引中…" : "+ 添加"}
        </button>
      </div>

      <div className="panel-body">
        {currentFolder && (
          <div className="kb-folder" title={currentFolder}>
            📁 {currentFolder.split(/[\\/]/).pop()}
          </div>
        )}

        {ingestMsg && <div className="ingest-msg">{ingestMsg}</div>}

        {kbFiles.length === 0 && !currentFolder && (
          <div className="empty-state">
            <div>暂无文件</div>
            <div style={{ fontSize: 12 }}>点击「+ 添加」选择本地文件夹</div>
          </div>
        )}

        {kbFiles.map((f) => (
          <div key={f.doc_id} className="kb-file" title={f.file_path}>
            <span className="kb-file__icon">{iconForType(f.file_type)}</span>
            <span className="kb-file__name">{f.file_name}</span>
            <span className="kb-file__count">{f.chunk_count}</span>
          </div>
        ))}
      </div>

      <style>{sidebarStyles}</style>
    </>
  );
}

function iconForType(t: string): string {
  const m: Record<string, string> = {
    pdf: "📕",
    docx: "📘",
    pptx: "📙",
    xlsx: "📗",
    md: "📝",
    txt: "📄",
  };
  return m[t] ?? "📄";
}

const sidebarStyles = `
  .nav-item {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 10px;
    border-radius: 6px;
    font-size: 13px;
    color: var(--text-primary);
    text-align: left;
  }
  .nav-item:hover { background: var(--bg-tertiary); }
  .nav-item--active { background: var(--bg-tertiary); font-weight: 500; }
  .nav-item__count {
    margin-left: auto;
    background: var(--bg-tertiary);
    padding: 1px 6px;
    border-radius: 8px;
    font-size: 11px;
    color: var(--text-secondary);
  }
  .kb-folder {
    font-size: 12px;
    color: var(--text-secondary);
    padding: 4px 0 8px 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .ingest-msg {
    font-size: 12px;
    color: var(--text-secondary);
    padding: 6px 0;
  }
  .kb-file {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 8px;
    border-radius: 4px;
    font-size: 12px;
  }
  .kb-file:hover { background: var(--bg-tertiary); }
  .kb-file__icon { flex-shrink: 0; }
  .kb-file__name {
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .kb-file__count {
    color: var(--text-tertiary);
    font-size: 11px;
  }
`;
