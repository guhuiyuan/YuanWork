import { useCallback, useEffect, useMemo, useState } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import { useAppStore } from "../../store";
import type { WikiConclusion, WikiPageBlock } from "../../store";
import {
  getWikiPage,
  listPageBlocks,
  listPageConclusions,
  openConclusion,
  updateWikiPage,
} from "../../api/backend";

export function WikiPageView() {
  const selectedId = useAppStore((s) => s.selectedWikiPage);
  const detail = useAppStore((s) => s.wikiPageDetail);
  const setDetail = useAppStore((s) => s.setWikiPageDetail);
  const selectPage = useAppStore((s) => s.selectWikiPage);
  const showToast = useAppStore((s) => s.showToast);
  const [conclusions, setConclusions] = useState<WikiConclusion[]>([]);
  /** The paragraphs of the page, and who wrote each one. */
  const [blocks, setBlocks] = useState<WikiPageBlock[]>([]);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    setDetail(null);
    // Another page is not edited with the text of this one.
    setEditing(false);
    setDraft("");
    setEditError(null);
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

  // The conclusions of the page, each with the place it was read from.
  useEffect(() => {
    if (!selectedId) {
      setConclusions([]);
      return;
    }
    let cancelled = false;
    listPageConclusions(selectedId)
      .then((list) => {
        if (!cancelled) setConclusions(list);
      })
      .catch((e) => {
        // A page without conclusions is normal - the section is simply
        // not shown.
        console.error("conclusion fetch failed", e);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  // The paragraphs of the page. A backend that does not know about
  // them is not an error - the body is then shown as one text.
  useEffect(() => {
    // The paragraphs on screen belong to the page on screen - not to
    // the page that was on screen a moment ago.
    setBlocks([]);
    if (!selectedId) {
      return;
    }
    let cancelled = false;
    listPageBlocks(selectedId)
      .then((list) => {
        if (!cancelled) setBlocks(list);
      })
      .catch((e) => {
        console.error("block fetch failed", e);
        if (!cancelled) setBlocks([]);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  const startEdit = useCallback(() => {
    // The draft is the page's paragraphs as they are *recorded* - the
    // blocks are the ownership record. The body and the blocks can
    // disagree (a recompile keeps the user's paragraph in the blocks),
    // and a draft taken from the body would drop the user's own words
    // on the next save (AC-F-027).
    const recorded = blocks.map((b) => b.text).join("\n\n");
    setDraft(recorded || detail?.markdown || "");
    setEditError(null);
    setEditing(true);
  }, [blocks, detail?.markdown]);

  const cancelEdit = useCallback(() => {
    setEditing(false);
    setDraft("");
    setEditError(null);
  }, []);

  /**
   * Save the edit and read the page again.
   *
   * The backend decides which paragraphs became the user's - the ones
   * that changed - so the page and its paragraphs are both read again
   * afterwards: the ownership shown has to be the recorded one.
   */
  const saveEdit = useCallback(async () => {
    if (!selectedId || !draft.trim()) return;
    setSaving(true);
    setEditError(null);
    try {
      const saved = await updateWikiPage(selectedId, draft);
      // The edit is stored. Reading the page back is a nicety - a
      // failure there must not be reported as a failed save, or the
      // user is told their words are lost when they are not.
      setEditing(false);
      setDraft("");

      // The reader may have moved on to another page while the save
      // was in flight - the page on screen is not this one.
      const stillHere = () =>
        useAppStore.getState().selectedWikiPage === selectedId;

      if (stillHere()) {
        showToast("已保存。改过的段落记在你名下。", "success");
      }

      try {
        const fresh =
          saved && typeof saved.markdown === "string"
            ? saved
            : await getWikiPage(selectedId);
        // The body is shown even when the paragraphs cannot be read -
        // the edit is in, and the page must show it.
        if (stillHere()) setDetail(fresh);
      } catch (e) {
        console.error("could not read the page back", e);
      }

      try {
        const list = await listPageBlocks(selectedId);
        if (stillHere()) setBlocks(list);
      } catch (e) {
        console.error("could not read the paragraphs back", e);
      }
    } catch (e) {
      setEditError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }, [selectedId, draft, setDetail, showToast]);

  const markdownComponents = useMemo<Components>(
    () => ({
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
    }),
    [selectPage]
  );

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
        <div className="wiki-page__title-row">
          <h1>{detail.title}</h1>
          {!editing && (
            <button type="button" className="btn" onClick={startEdit}>
              编辑
            </button>
          )}
        </div>
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

      {editing ? (
        <div className="wiki-page__editor">
          <div className="wiki-page__editor-hint">
            直接改正文。你改过的段落会记在你名下，之后自动重编译不会覆盖它们。
          </div>
          <textarea
            className="wiki-page__editor-text"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={20}
            disabled={saving}
            spellCheck={false}
          />
          <div className="wiki-page__editor-actions">
            <button
              type="button"
              className="btn btn--primary"
              onClick={() => void saveEdit()}
              disabled={saving || !draft.trim()}
            >
              {saving ? "保存中…" : "保存"}
            </button>
            <button type="button" className="btn" onClick={cancelEdit} disabled={saving}>
              取消
            </button>
            {editError && (
              <span className="wiki-page__editor-error">保存失败：{editError}</span>
            )}
          </div>
        </div>
      ) : (
        <>
          {blocks.length > 0 && (
            <div className="wiki-page__ownership-note">
              标注「你写的」段落由你编辑，后续自动重编译只替换模型生成的段落。
            </div>
          )}
          <div className="wiki-page__content">
            {blocks.length > 0 ? (
              blocks.map((block, index) => (
                <section
                  key={block.index ?? index}
                  className={`wiki-page__block wiki-page__block--${block.owner}`}
                >
                  <span className="wiki-page__block-owner">
                    {block.owner === "user" ? "你写的" : "模型生成"}
                  </span>
                  <ReactMarkdown components={markdownComponents}>
                    {preprocessWikiLinks(block.text)}
                  </ReactMarkdown>
                </section>
              ))
            ) : (
              <ReactMarkdown components={markdownComponents}>
                {preprocessWikiLinks(detail.markdown)}
              </ReactMarkdown>
            )}
          </div>
        </>
      )}

      {conclusions.length > 0 && (
        <div className="wiki-page__conclusions">
          <h2>结论与出处</h2>
          <ul>
            {conclusions.map((c) => (
              <li key={c.id}>
                <span className="wiki-page__conclusion-text">{c.text}</span>
                {c.file_path && (
                  <button
                    type="button"
                    className="wiki-page__conclusion-link"
                    title={`${c.file_name} ${c.location}`}
                    onClick={() => {
                      openConclusion(c).catch((e) =>
                        console.error("cannot open the source", e)
                      );
                    }}
                  >
                    {c.location || c.file_name}
                  </button>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
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
  .wiki-page__title-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
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
  .wiki-page__ownership-note {
    padding: 8px 32px 0 32px;
    font-size: 12px;
    color: var(--text-tertiary);
  }
  .wiki-page__block { margin-bottom: 12px; }
  .wiki-page__block-owner {
    display: inline-block;
    font-size: 10px;
    color: var(--text-tertiary);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 0 5px;
    margin-bottom: 2px;
  }
  .wiki-page__block--user .wiki-page__block-owner {
    color: var(--accent);
    border-color: var(--accent);
  }
  .wiki-page__editor {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 16px 32px 32px 32px;
    flex: 1;
    min-height: 0;
  }
  .wiki-page__editor-hint {
    font-size: 12px;
    color: var(--text-secondary);
  }
  .wiki-page__editor-text {
    flex: 1;
    min-height: 240px;
    font-family: var(--font-mono);
    font-size: 13px;
    line-height: 1.6;
    resize: vertical;
  }
  .wiki-page__editor-actions {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .wiki-page__editor-error {
    font-size: 12px;
    color: var(--conflict-border, #991b1b);
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
  .wiki-page__conclusions {
    padding: 12px 32px 24px 32px;
    border-top: 1px solid var(--border);
    background: var(--bg-secondary);
  }
  .wiki-page__conclusions h2 { font-size: 14px; margin: 0 0 8px 0; }
  .wiki-page__conclusions ul { margin: 0; padding-left: 20px; }
  .wiki-page__conclusions li { margin: 4px 0; font-size: 13px; }
  .wiki-page__conclusion-text { margin-right: 8px; }
  .wiki-page__conclusion-link {
    background: none;
    border: 1px solid var(--border);
    border-radius: 4px;
    color: var(--accent);
    cursor: pointer;
    font-size: 11px;
    padding: 1px 6px;
  }
  .wiki-page__conclusion-link:hover { border-color: var(--accent); }
`;
