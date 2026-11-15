import { useEffect, useState } from "react";
import { useAppStore, type WorkspaceEntry } from "../store";
import { listKbFiles, listWikiPages } from "../api/backend";
import { useIngest, entryName } from "../hooks/useIngest";
import { markOnboarded } from "../api/firstRun";

export function Sidebar() {
  const view = useAppStore((s) => s.view);
  const setView = useAppStore((s) => s.setView);
  const kbFiles = useAppStore((s) => s.kbFiles);
  const setKbFiles = useAppStore((s) => s.setKbFiles);
  const setKbLoaded = useAppStore((s) => s.setKbLoaded);
  const setKbError = useAppStore((s) => s.setKbError);
  const wikiPages = useAppStore((s) => s.wikiPages);
  const setWikiPages = useAppStore((s) => s.setWikiPages);
  const workspaceEntries = useAppStore((s) => s.workspaceEntries);
  const workspaceLoaded = useAppStore((s) => s.workspaceLoaded);
  const workspaceError = useAppStore((s) => s.workspaceError);
  const showToast = useAppStore((s) => s.showToast);
  const {
    ingesting,
    message,
    pickAndIngest,
    addFile,
    rebuild,
    removeEntry,
    refreshWorkspace,
    cancel,
  } = useIngest();

  // Load the workspace and the knowledge base on mount.
  //
  // The workspace comes first: it says whether the setup already
  // happened, and the app has to know that before it decides to draw
  // the first-run screen.
  useEffect(() => {
    let gone = false;

    async function load() {
      try {
        const entries = await refreshWorkspace();
        // A workspace that already has entries means the setup
        // happened - maybe in an earlier session. Without this the
        // welcome screen would come back over the live chat.
        if (!gone && entries.length > 0) markOnboarded();
      } catch (e) {
        // The backend is unreachable. This is NOT an empty workspace,
        // and saying so would be a lie.
        if (!gone) {
          showToast(`无法读取工作域：${(e as Error).message}`, "error");
        }
      }

      try {
        const files = await listKbFiles();
        if (gone) return;
        setKbFiles(files);
        // A knowledge base that already has files means the setup
        // happened - maybe in an earlier version. Without this, deleting
        // the last file would pop the welcome screen over the live chat.
        if (files.length > 0) markOnboarded();
      } catch (e) {
        if (gone) return;
        setKbError((e as Error).message);
        showToast(`无法读取知识库：${(e as Error).message}`, "error");
      } finally {
        if (!gone) setKbLoaded(true);
      }

      listWikiPages().then(setWikiPages).catch(() => {});
    }

    load();
    return () => {
      gone = true;
    };
  }, [refreshWorkspace, setKbError, setKbFiles, setKbLoaded, setWikiPages, showToast]);

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
        <span>工作域</span>
        <div className="ws-actions">
          <button
            className="btn btn--primary"
            onClick={pickAndIngest}
            disabled={ingesting}
            title="选择一个本地文件夹，加入工作域"
          >
            + 文件夹
          </button>
          <button
            className="btn"
            onClick={addFile}
            disabled={ingesting}
            title="把一个文件单独加入工作域"
          >
            + 文件
          </button>
          <button
            className="btn"
            onClick={rebuild}
            disabled={ingesting}
            title="按工作域重新建库（已索引的文件会跳过）"
          >
            重建
          </button>
        </div>
      </div>

      <div className="panel-body">
        {message && (
          <div className="ingest-msg">
            <span>{message}</span>
            {ingesting && (
              <button
                className="ingest-cancel"
                onClick={cancel}
                title="停止等待这次建库"
              >
                取消
              </button>
            )}
          </div>
        )}

        {workspaceError && (
          <div className="ingest-msg">无法读取工作域：{workspaceError}</div>
        )}

        {workspaceLoaded &&
          !workspaceError &&
          workspaceEntries.length === 0 &&
          !ingesting && (
            <div className="empty-state">
              <div>工作域是空的</div>
              <div style={{ fontSize: 12 }}>
                「+ 文件夹」选择工作目录，或「+ 文件」单独加入一个文件
              </div>
            </div>
          )}

        {workspaceEntries.map((entry) => (
          <WorkspaceRow
            key={entry.id}
            entry={entry}
            onRemove={() => removeEntry(entry)}
          />
        ))}

        {kbFiles.length > 0 && (
          <div className="ws-count">已索引 {kbFiles.length} 个文件</div>
        )}
      </div>

      <style>{sidebarStyles}</style>
    </>
  );
}

/**
 * One entry of the workspace, with a way to take it out again.
 *
 * Taking an entry out also clears what it brought into the index - and
 * the wording says so, because the user's own file stays on disk.
 * Removing is confirmed inline rather than in a native dialog: a
 * `confirm()` blocks the webview and reads as a system error.
 */
function WorkspaceRow({
  entry,
  onRemove,
}: {
  entry: WorkspaceEntry;
  onRemove: () => Promise<void> | void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const name = entryName(entry);

  async function handleRemove() {
    setBusy(true);
    try {
      await onRemove();
      setConfirming(false);
    } finally {
      setBusy(false);
    }
  }

  if (confirming) {
    return (
      <>
        <div className="ws-row ws-row--confirming" title={entry.path}>
          <span className="ws-row__icon">{entry.kind === "folder" ? "📁" : "📄"}</span>
          <span className="ws-row__name">{name}</span>
          <div className="ws-row__confirm">
            <span className="ws-row__confirm-text">从工作域移除？</span>
            <button
              className="ws-row__btn ws-row__btn--danger"
              onClick={handleRemove}
              disabled={busy}
            >
              {busy ? "移除中…" : "移除"}
            </button>
            <button
              className="ws-row__btn"
              onClick={() => setConfirming(false)}
              disabled={busy}
            >
              取消
            </button>
          </div>
        </div>
        <style>{sidebarStyles}</style>
      </>
    );
  }

  return (
    <>
      <div className="ws-row" title={entry.path}>
        <span className="ws-row__icon">{entry.kind === "folder" ? "📁" : "📄"}</span>
        <span className="ws-row__name">{name}</span>
        <span className="ws-row__kind">{entry.kind === "folder" ? "文件夹" : "文件"}</span>
        <button
          className="ws-row__remove"
          title="从工作域移除（它带入索引的内容会被清除，文件仍在磁盘上）"
          aria-label={`从工作域移除 ${name}`}
          onClick={() => setConfirming(true)}
        >
          ✕
        </button>
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
  .ws-actions {
    display: flex;
    gap: 4px;
    text-transform: none;
    letter-spacing: 0;
  }
  .ws-actions .btn { padding: 4px 8px; font-size: 12px; }
  .ws-actions .btn:disabled { opacity: 0.6; cursor: default; }
  .ws-row {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 8px;
    border-radius: 4px;
    font-size: 12px;
  }
  .ws-row:hover { background: var(--bg-tertiary); }
  .ws-row__icon { flex-shrink: 0; }
  .ws-row__name {
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .ws-row__kind {
    flex-shrink: 0;
    color: var(--text-tertiary);
    font-size: 11px;
  }
  .ws-row__remove {
    flex-shrink: 0;
    width: 18px;
    height: 18px;
    border: none;
    border-radius: 4px;
    background: transparent;
    color: var(--text-tertiary);
    font-size: 11px;
    line-height: 1;
    cursor: pointer;
    opacity: 0;
    transition: opacity 0.12s, background 0.12s, color 0.12s;
  }
  .ws-row:hover .ws-row__remove { opacity: 1; }
  .ws-row__remove:hover {
    background: var(--danger);
    color: #fff;
    opacity: 1;
  }
  .ws-row__remove:focus-visible { opacity: 1; outline: 2px solid var(--accent); }
  .ws-row--confirming { background: var(--bg-tertiary); }
  .ws-row__confirm {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-shrink: 0;
  }
  .ws-row__confirm-text {
    font-size: 11px;
    color: var(--text-secondary);
    white-space: nowrap;
  }
  .ws-row__btn {
    border: 1px solid var(--border);
    background: var(--bg-primary);
    color: var(--text-primary);
    border-radius: 4px;
    font-size: 11px;
    padding: 2px 7px;
    cursor: pointer;
  }
  .ws-row__btn:hover { background: var(--bg-secondary); }
  .ws-row__btn:disabled { opacity: 0.6; cursor: default; }
  .ws-row__btn--danger {
    background: var(--danger);
    border-color: var(--danger);
    color: #fff;
  }
  .ws-row__btn--danger:hover { background: #b91c1c; }
  .ws-count {
    margin-top: 10px;
    padding: 0 8px;
    font-size: 11px;
    color: var(--text-tertiary);
  }
  .ingest-msg {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12px;
    color: var(--text-secondary);
    padding: 6px 0;
  }
  .ingest-msg > span { flex: 1; }
  .ingest-cancel {
    border: 1px solid var(--border);
    background: var(--bg-primary);
    color: var(--text-secondary);
    border-radius: 4px;
    font-size: 11px;
    padding: 2px 8px;
    cursor: pointer;
  }
  .ingest-cancel:hover { background: var(--bg-tertiary); }
`;
