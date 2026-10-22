import { useState } from "react";
import type { KbFile } from "../store";

/**
 * One file in the knowledge base, with a way to take it out again.
 *
 * Deleting is confirmed inline rather than in a native dialog: a
 * `confirm()` blocks the webview and reads as a system error, and the
 * question here is small enough to answer in place. The wording says
 * the file is only removed from the index - the user's own file stays
 * on disk.
 */
export function KbFileRow({
  file,
  onDelete,
}: {
  file: KbFile;
  onDelete: () => Promise<void> | void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);

  async function handleDelete() {
    setBusy(true);
    try {
      await onDelete();
      setConfirming(false);
    } finally {
      setBusy(false);
    }
  }

  if (confirming) {
    return (
      <>
        <div className="kb-file kb-file--confirming" title={file.file_path}>
          <span className="kb-file__icon">{iconForType(file.file_type)}</span>
          <span className="kb-file__name">{file.file_name}</span>
          <div className="kb-file__confirm">
            <span className="kb-file__confirm-text">从知识库移除？</span>
            <button
              className="kb-file__btn kb-file__btn--danger"
              onClick={handleDelete}
              disabled={busy}
            >
              {busy ? "移除中…" : "移除"}
            </button>
            <button
              className="kb-file__btn"
              onClick={() => setConfirming(false)}
              disabled={busy}
            >
              取消
            </button>
          </div>
        </div>
        <style>{kbFileStyles}</style>
      </>
    );
  }

  return (
    <>
      <div className="kb-file" title={file.file_path}>
        <span className="kb-file__icon">{iconForType(file.file_type)}</span>
        <span className="kb-file__name">{file.file_name}</span>
        <span className="kb-file__count">{file.chunk_count}</span>
        <button
          className="kb-file__remove"
          title="从知识库移除（文件仍保留在磁盘上）"
          aria-label={`从知识库移除 ${file.file_name}`}
          onClick={() => setConfirming(true)}
        >
          ✕
        </button>
      </div>
      <style>{kbFileStyles}</style>
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

const kbFileStyles = `
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
  .kb-file__remove {
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
  .kb-file:hover .kb-file__remove { opacity: 1; }
  .kb-file__remove:hover {
    background: var(--danger);
    color: #fff;
    opacity: 1;
  }
  .kb-file__remove:focus-visible { opacity: 1; outline: 2px solid var(--accent); }
  .kb-file--confirming { background: var(--bg-tertiary); }
  .kb-file__confirm {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-shrink: 0;
  }
  .kb-file__confirm-text {
    font-size: 11px;
    color: var(--text-secondary);
    white-space: nowrap;
  }
  .kb-file__btn {
    border: 1px solid var(--border);
    background: var(--bg-primary);
    color: var(--text-primary);
    border-radius: 4px;
    font-size: 11px;
    padding: 2px 7px;
    cursor: pointer;
  }
  .kb-file__btn:hover { background: var(--bg-secondary); }
  .kb-file__btn:disabled { opacity: 0.6; cursor: default; }
  .kb-file__btn--danger {
    background: var(--danger);
    border-color: var(--danger);
    color: #fff;
  }
  .kb-file__btn--danger:hover { background: #b91c1c; }
`;
