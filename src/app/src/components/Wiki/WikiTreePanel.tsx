import { useEffect, useMemo, useState } from "react";
import { useAppStore } from "../../store";
import { listWikiPages } from "../../api/backend";

const CATEGORY_ICONS: Record<string, string> = {
  contract: "📜",
  law: "⚖️",
  project: "📁",
  concept: "💡",
  person: "👤",
  general: "📄",
};

const CATEGORY_ORDER = [
  "contract",
  "law",
  "project",
  "concept",
  "person",
  "general",
];

export function WikiTreePanel() {
  const pages = useAppStore((s) => s.wikiPages);
  const setPages = useAppStore((s) => s.setWikiPages);
  const selectedId = useAppStore((s) => s.selectedWikiPage);
  const selectPage = useAppStore((s) => s.selectWikiPage);
  const [filter, setFilter] = useState("");
  const [refreshing, setRefreshing] = useState(false);

  const refresh = async () => {
    setRefreshing(true);
    try {
      const list = await listWikiPages();
      setPages(list);
    } catch (e) {
      console.error("list wiki pages failed", e);
    } finally {
      setRefreshing(false);
    }
  };

  useEffect(() => {
    if (pages.length === 0) {
      refresh();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const grouped = useMemo(() => {
    const q = filter.trim().toLowerCase();
    const filtered = pages.filter((p) => {
      if (!q) return true;
      return (
        p.title.toLowerCase().includes(q) ||
        p.category.toLowerCase().includes(q) ||
        (p.tags ?? []).some((t) => t.toLowerCase().includes(q))
      );
    });
    const groups: Record<string, typeof pages> = {};
    for (const p of filtered) {
      if (!groups[p.category]) groups[p.category] = [];
      groups[p.category].push(p);
    }
    for (const c in groups) {
      groups[c].sort((a, b) => a.title.localeCompare(b.title, "zh-Hans"));
    }
    return groups;
  }, [pages, filter]);

  return (
    <div className="wiki-tree">
      <div className="wiki-tree__header">
        <input
          className="wiki-tree__filter"
          placeholder="🔍 筛选页面…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
        <button
          className="btn"
          onClick={refresh}
          disabled={refreshing}
          title="刷新"
        >
          {refreshing ? "…" : "↻"}
        </button>
      </div>

      {pages.length === 0 ? (
        <div className="empty-state">
          暂无 Wiki 页面
          <div style={{ fontSize: 12 }}>
            回答多文档问题时自动生成
          </div>
        </div>
      ) : (
        <div className="wiki-tree__groups">
          {CATEGORY_ORDER.filter((c) => grouped[c]?.length).map((cat) => (
            <div key={cat} className="wiki-tree__group">
              <div className="wiki-tree__group-title">
                <span>{CATEGORY_ICONS[cat] ?? "📄"}</span>
                {labelForCategory(cat)}
                <span className="wiki-tree__count">{grouped[cat].length}</span>
              </div>
              {grouped[cat].map((p) => (
                <div
                  key={p.id}
                  className={`wiki-tree__item ${
                    selectedId === p.id ? "wiki-tree__item--active" : ""
                  }`}
                  onClick={() => selectPage(p.id)}
                  title={p.summary || p.title}
                >
                  {p.title}
                  {p.stale && (
                    <span
                      className="wiki-tree__stale"
                      title="源文件已变更，页面仍是旧版本——重新编译前以旧页面为准"
                    >
                      源文件已变更
                    </span>
                  )}
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
      <style>{treeStyles}</style>
    </div>
  );
}

function labelForCategory(c: string): string {
  return {
    contract: "合同",
    law: "法规",
    project: "项目",
    concept: "概念",
    person: "人物",
    general: "通用",
  }[c] ?? c;
}

const treeStyles = `
  .wiki-tree { display: flex; flex-direction: column; height: 100%; }
  .wiki-tree__header {
    display: flex;
    gap: 6px;
    padding: 8px 12px;
    border-bottom: 1px solid var(--border);
  }
  .wiki-tree__filter { flex: 1; font-size: 12px; padding: 4px 8px; }
  .wiki-tree__groups { flex: 1; overflow-y: auto; padding: 8px 0; }
  .wiki-tree__group { margin-bottom: 12px; }
  .wiki-tree__group-title {
    padding: 4px 12px;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-tertiary);
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .wiki-tree__count {
    margin-left: auto;
    background: var(--bg-tertiary);
    padding: 1px 6px;
    border-radius: 8px;
    font-size: 10px;
    color: var(--text-primary);
  }
  .wiki-tree__stale {
    margin-left: 6px;
    color: var(--warning, #c47f00);
    font-size: 10px;
  }
  .wiki-tree__item {
    padding: 4px 12px 4px 24px;
    font-size: 13px;
    color: var(--text-primary);
    cursor: pointer;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .wiki-tree__item:hover { background: var(--bg-tertiary); }
  .wiki-tree__item--active {
    background: var(--bg-tertiary);
    font-weight: 500;
    color: var(--accent);
  }
`;
