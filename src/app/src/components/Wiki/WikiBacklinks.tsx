import { useEffect } from "react";
import { useAppStore } from "../../store";
import { getWikiBacklinks } from "../../api/backend";

export function WikiBacklinks() {
  const selectedId = useAppStore((s) => s.selectedWikiPage);
  const backlinks = useAppStore((s) => s.wikiBacklinks);
  const setBacklinks = useAppStore((s) => s.setWikiBacklinks);
  const selectPage = useAppStore((s) => s.selectWikiPage);

  useEffect(() => {
    if (!selectedId) {
      setBacklinks(null);
      return;
    }
    let cancelled = false;
    getWikiBacklinks(selectedId)
      .then((b) => {
        if (!cancelled) setBacklinks(b);
      })
      .catch((e) => {
        console.error("backlinks fetch failed", e);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId, setBacklinks]);

  if (!selectedId) {
    return (
      <div className="empty-state">
        选择一个页面查看反向链接
      </div>
    );
  }
  if (!backlinks) {
    return <div className="empty-state">加载中…</div>;
  }
  if (backlinks.backlinks.length === 0) {
    return (
      <div className="empty-state">
        暂无反向链接
        <div style={{ fontSize: 12 }}>
          其他页面引用了此页面时会显示在这里
        </div>
      </div>
    );
  }

  // Group by source_type for clarity
  const byType: Record<string, typeof backlinks.backlinks> = {};
  for (const link of backlinks.backlinks) {
    if (!byType[link.source_type]) byType[link.source_type] = [];
    byType[link.source_type].push(link);
  }

  return (
    <div className="backlinks">
      <div className="backlinks__header">
        反向链接
        <span className="backlinks__count">{backlinks.backlinks.length}</span>
      </div>
      {Object.entries(byType).map(([type, links]) => (
        <div key={type} className="backlinks__group">
          <div className="backlinks__group-title">
            {labelForType(type)} <span>({links.length})</span>
          </div>
          {links.map((l) => (
            <div
              key={`${l.source_id}-${l.target_id}-${l.link_type}`}
              className={`backlinks__item ${
                l.source_type === "page" ? "backlinks__item--clickable" : ""
              }`}
              onClick={() => {
                if (l.source_type === "page") selectPage(l.source_id);
              }}
              title={`${l.link_type} → ${l.target_id}`}
            >
              <span className="backlinks__source-id">{l.source_id}</span>
              <span className="backlinks__type">· {l.link_type}</span>
            </div>
          ))}
        </div>
      ))}
      <style>{backlinksStyles}</style>
    </div>
  );
}

function labelForType(t: string): string {
  return {
    page: "其他页面",
    chunk: "文档片段",
    doc: "文档",
    episode: "对话",
  }[t] ?? t;
}

const backlinksStyles = `
  .backlinks { padding: 12px; font-size: 12px; }
  .backlinks__header {
    font-weight: 600;
    color: var(--text-secondary);
    margin-bottom: 12px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    font-size: 11px;
  }
  .backlinks__count {
    background: var(--bg-tertiary);
    padding: 1px 6px;
    border-radius: 8px;
    font-size: 10px;
    color: var(--text-primary);
    margin-left: 4px;
  }
  .backlinks__group { margin-bottom: 12px; }
  .backlinks__group-title {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-tertiary);
    margin-bottom: 4px;
  }
  .backlinks__group-title span { color: var(--text-tertiary); font-weight: 400; }
  .backlinks__item {
    padding: 4px 8px;
    border-radius: 4px;
    color: var(--text-primary);
    word-break: break-all;
  }
  .backlinks__item--clickable { color: var(--accent); cursor: pointer; }
  .backlinks__item--clickable:hover { background: var(--bg-tertiary); }
  .backlinks__source-id { font-family: var(--font-mono); font-size: 11px; }
  .backlinks__type { color: var(--text-tertiary); font-size: 10px; margin-left: 4px; }
`;
