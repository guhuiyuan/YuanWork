import { useEffect } from "react";
import ReactMarkdown from "react-markdown";
import { useAppStore } from "../../store";
import { getWikiPage } from "../../api/backend";

export function WikiPageView() {
  const selectedId = useAppStore((s) => s.selectedWikiPage);
  const detail = useAppStore((s) => s.wikiPageDetail);
  const setDetail = useAppStore((s) => s.setWikiPageDetail);
  const selectPage = useAppStore((s) => s.selectWikiPage);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    setDetail(null);
    getWikiPage(selectedId)
      .then((d) => {
        if (!cancelled) setDetail(d);
      })
      .catch((e) => {
        console.error("page fetch failed", e);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId, setDetail]);

  if (!selectedId) {
    return (
      <div className="empty-state">
        <div>← 选择一个 Wiki 页面</div>
        <div style={{ fontSize: 12 }}>
          回答多文档问题时系统会自动编译
        </div>
      </div>
    );
  }

  if (!detail) {
    return <div className="empty-state">加载中…</div>;
  }

  return (
    <div className="wiki-page">
      <header className="wiki-page__header">
        <h1>{detail.title}</h1>
        <div className="wiki-page__meta">
          <span className="wiki-page__category">{detail.category}</span>
          {detail.tags.map((t) => (
            <span key={t} className="wiki-page__tag">#{t}</span>
          ))}
          <span className="wiki-page__version">v{detail.version}</span>
          {detail.last_compiled_at && (
            <span className="wiki-page__compiled">
              · {formatDate(detail.last_compiled_at)}
            </span>
          )}
          <span className="wiki-page__chunks">
            · {detail.chunk_count} 个来源片段
          </span>
        </div>
        {detail.summary && (
          <p className="wiki-page__summary">{detail.summary}</p>
        )}
      </header>
      <div className="wiki-page__content">
        <ReactMarkdown
          components={{
            a: ({ href, children, ...rest }) => {
              if (href?.startsWith("#wiki:")) {
                const target = decodeURIComponent(href.slice("#wiki:".length));
                return (
                  <a
                    href={href}
                    className="wiki-link"
                    onClick={(e) => {
                      e.preventDefault();
                      selectPage(target);
                    }}
                  >
                    {children}
                  </a>
                );
              }
              return (
                <a href={href} target="_blank" rel="noopener noreferrer" {...rest}>
                  {children}
                </a>
              );
            },
          }}
        >
          {preprocessWikiLinks(detail.markdown)}
        </ReactMarkdown>
      </div>
      <style>{pageStyles}</style>
    </div>
  );
}

/**
 * Convert [[Page Title]] into markdown link syntax that react-markdown
 * understands, with a custom #wiki: scheme that the link handler above
 * intercepts to navigate within the app.
 */
function preprocessWikiLinks(md: string): string {
  return md.replace(/\[\[([^\]]+)\]\]/g, (_full, text: string) => {
    const target = slugify(text);
    return `[${text}](#wiki:${encodeURIComponent(target)})`;
  });
}

/**
 * Mirror the Python slugify used in yuanwork/wiki/trigger.py:
 *   re.sub(r"[^\w\s-]", "", text.lower())
 *   re.sub(r"[-\s]+", "-", slug).strip("-")[:64]
 * In JavaScript, \w in a literal regex includes Unicode word characters
 * (letters, digits, underscore) so CJK characters pass through unchanged.
 */
function slugify(text: string): string {
  let slug = text.toLowerCase();
  slug = slug.replace(/[^\w\s-]/g, "");
  slug = slug.replace(/[-\s]+/g, "-").replace(/^-+|-+$/g, "");
  return slug.slice(0, 64) || "untitled";
}

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString("zh-CN", { dateStyle: "short" });
  } catch {
    return iso;
  }
}

const pageStyles = `
  .wiki-page { display: flex; flex-direction: column; height: 100%; }
  .wiki-page__header {
    padding: 20px 32px 16px 32px;
    border-bottom: 1px solid var(--border);
    background: var(--bg-secondary);
  }
  .wiki-page__header h1 { font-size: 24px; margin-bottom: 8px; }
  .wiki-page__meta {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    align-items: center;
    font-size: 12px;
    color: var(--text-secondary);
  }
  .wiki-page__category {
    background: var(--bg-tertiary);
    padding: 1px 8px;
    border-radius: 8px;
    font-size: 11px;
    text-transform: lowercase;
  }
  .wiki-page__tag { color: var(--accent); font-size: 11px; }
  .wiki-page__version { color: var(--text-tertiary); font-size: 11px; }
  .wiki-page__compiled, .wiki-page__chunks { color: var(--text-tertiary); font-size: 11px; }
  .wiki-page__summary {
    margin-top: 8px;
    color: var(--text-secondary);
    font-size: 13px;
    line-height: 1.6;
  }
  .wiki-page__content {
    padding: 16px 32px 32px 32px;
    font-size: 14px;
    line-height: 1.7;
    flex: 1;
    overflow-y: auto;
  }
  .wiki-page__content h1 { font-size: 22px; margin: 16px 0 8px 0; }
  .wiki-page__content h2 {
    font-size: 18px;
    margin: 16px 0 8px 0;
    border-bottom: 1px solid var(--border);
    padding-bottom: 4px;
  }
  .wiki-page__content h3 { font-size: 15px; margin: 12px 0 6px 0; }
  .wiki-page__content p { margin: 8px 0; }
  .wiki-page__content ul, .wiki-page__content ol { padding-left: 24px; }
  .wiki-page__content li { margin: 4px 0; }
  .wiki-page__content code {
    background: var(--bg-tertiary);
    padding: 1px 4px;
    border-radius: 3px;
    font-family: var(--font-mono);
    font-size: 12px;
  }
  .wiki-page__content pre {
    background: var(--bg-tertiary);
    padding: 12px;
    border-radius: 6px;
    overflow-x: auto;
  }
  .wiki-page__content pre code { background: none; padding: 0; }
  .wiki-page__content blockquote {
    border-left: 3px solid var(--border);
    padding-left: 12px;
    color: var(--text-secondary);
    margin: 8px 0;
  }
  .wiki-page__content a { color: var(--accent); }
  .wiki-link { color: var(--accent); text-decoration: none; border-bottom: 1px dotted var(--accent); }
  .wiki-link:hover { text-decoration: none; border-bottom-style: solid; }
`;
