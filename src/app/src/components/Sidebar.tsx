import { useEffect, useState } from "react";
import { useAppStore } from "../store";
import { listKbFiles, listWikiPages, deleteKbFile } from "../api/backend";
import { useIngest } from "../hooks/useIngest";
import { KbFileRow } from "./KbFileRow";

export function Sidebar() {
  const view = useAppStore((s) => s.view);
  const setView = useAppStore((s) => s.setView);
  const kbFiles = useAppStore((s) => s.kbFiles);
  const setKbFiles = useAppStore((s) => s.setKbFiles);
  const setKbLoaded = useAppStore((s) => s.setKbLoaded);
  const wikiPages = useAppStore((s) => s.wikiPages);
  const setWikiPages = useAppStore((s) => s.setWikiPages);
  const currentFolder = useAppStore((s) => s.currentFolder);
  const setCurrentFolder = useAppStore((s) => s.setCurrentFolder);
  const showToast = useAppStore((s) => s.showToast);
  const { ingesting, message, folder, pickAndIngest, refreshFiles } = useIngest();

  // Load files on mount
  useEffect(() => {
    listKbFiles()
      .then(setKbFiles)
      .catch(() => {})
      .finally(() => setKbLoaded(true));
    listWikiPages().then(setWikiPages).catch(() => {});
  }, [setKbFiles, setKbLoaded, setWikiPages]);

  async function handleDelete(docId: string, fileName: string) {
    try {
      await deleteKbFile(docId);
      const files = await refreshFiles();
      setKbFiles(files);
      showToast(`已从知识库移除「${fileName}」（文件仍在磁盘上）`, "success");
    } catch (e) {
      showToast(`移除失败：${(e as Error).message}`, "error");
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
        <button className="btn btn--primary" onClick={pickAndIngest} disabled={ingesting}>
          {ingesting ? "索引中…" : "+ 添加"}
        </button>
      </div>

      <div className="panel-body">
        {(currentFolder || folder) && (
          <div className="kb-folder" title={folder ?? currentFolder ?? ""}>
            📁 {(folder ?? currentFolder ?? "").split(/[\\/]/).pop()}
          </div>
        )}

        {message && <div className="ingest-msg">{message}</div>}

        {kbFiles.length === 0 && !currentFolder && !ingesting && (
          <div className="empty-state">
            <div>暂无文件</div>
            <div style={{ fontSize: 12 }}>点击「+ 添加」选择本地文件夹</div>
          </div>
        )}

        {kbFiles.map((f) => (
          <KbFileRow
            key={f.doc_id}
            file={f}
            onDelete={() => handleDelete(f.doc_id, f.file_name)}
          />
        ))}
      </div>

      <style>{sidebarStyles}</style>
    </>
  );
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
`;
