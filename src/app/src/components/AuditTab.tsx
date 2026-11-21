import { useEffect, useState } from "react";
import {
  auditRows,
  auditSummary,
  runClear,
  AUDIT_COLUMNS,
  CLEAR_LABEL,
  CONFIRM_CLEAR_LABEL,
  formatCount,
  type AuditEntry,
  type AuditStats,
} from "../api/audit.ts";
import {
  auditStats,
  clearAuditLog,
  listAuditEntries,
} from "../api/backend.ts";

/**
 * The audit log on the right.
 *
 * The backend has been keeping one line per model call all along; this
 * is where the reader finally sees it (ADR-056):
 *
 * - **A table of metadata.** When the call happened, what it was for,
 *   which model answered, what it cost, how long it took and whether
 *   it worked. The prompt and the answer are *not* in the log - only
 *   the hash of the prompt is, and that shows in the tooltip of the
 *   row (ADR-018).
 * - **The totals above the table.** How many calls were made, what
 *   they cost and how many failed - the three numbers that say
 *   whether anything is wrong.
 * - **A clear button that asks first.** Clearing takes the reader's
 *   own records off their machine and cannot be undone, so it says
 *   how many went away - and says so *only* when it worked.
 */
export function AuditTab() {
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [stats, setStats] = useState<AuditStats | null>(null);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ text: string; ok: boolean } | null>(
    null
  );
  // The second state of the clear button: it asks before it acts.
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    Promise.all([listAuditEntries(), auditStats()])
      .then(([list, sums]) => {
        if (cancelled) return;
        setEntries(list?.entries ?? []);
        setTotal(list?.total ?? 0);
        setStats(sums);
        setError(null);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(`读不到审计记录：${(e as Error)?.message ?? String(e)}`);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  /**
   * Clear the log and say what happened.
   *
   * The count comes from the backend: it is the number of lines that
   * were on the disk a moment ago. A failure is reported as a failure
   * - the reader is not told the log was cleared when it was not
   * (AC-F-037).
   */
  async function handleClear() {
    setBusy(true);
    const outcome = await runClear(clearAuditLog);
    setBusy(false);
    setConfirming(false);
    setNotice({ text: outcome.message, ok: outcome.ok });
    if (!outcome.ok) return;
    // The table is empty now - read the totals again so the summary
    // is the truth rather than what it was a moment ago.
    setEntries([]);
    setTotal(0);
    try {
      setStats(await auditStats());
    } catch (e) {
      setError(`读不到审计汇总：${(e as Error)?.message ?? String(e)}`);
    }
  }

  const rows = auditRows(entries);
  const summary = auditSummary(stats);

  return (
    <div className="audit">
      <div className="audit__summary">
        <div className="audit__stat">
          <span className="audit__stat-value">{formatCount(summary.calls)}</span>
          <span className="audit__stat-label">调用次数</span>
        </div>
        <div className="audit__stat">
          <span className="audit__stat-value">{formatCount(summary.tokens)}</span>
          <span className="audit__stat-label">令牌数</span>
        </div>
        <div className="audit__stat">
          <span
            className={`audit__stat-value ${
              summary.failures > 0 ? "audit__stat-value--bad" : ""
            }`}
          >
            {formatCount(summary.failures)}
          </span>
          <span className="audit__stat-label">失败次数</span>
        </div>
      </div>

      <div className="audit__toolbar">
        {confirming ? (
          <>
            <button
              className="audit__btn audit__btn--danger"
              onClick={handleClear}
              disabled={busy}
            >
              {busy ? "清空中…" : CONFIRM_CLEAR_LABEL}
            </button>
            <button
              className="audit__btn"
              onClick={() => setConfirming(false)}
              disabled={busy}
            >
              取消
            </button>
          </>
        ) : (
          <button
            className="audit__btn audit__btn--danger"
            onClick={() => {
              setNotice(null);
              setConfirming(true);
            }}
            disabled={busy}
          >
            {CLEAR_LABEL}
          </button>
        )}
        <span className="audit__total">
          共 {formatCount(total)} 条记录
          {total > rows.length
            ? `（显示最近 ${formatCount(rows.length)} 条）`
            : ""}
        </span>
      </div>

      {notice && (
        <div
          className={`audit__notice ${
            notice.ok ? "audit__notice--ok" : "audit__notice--bad"
          }`}
        >
          {notice.text}
        </div>
      )}

      {error && <div className="audit__notice audit__notice--bad">{error}</div>}

      {loading ? (
        <div className="audit__empty">读取中…</div>
      ) : rows.length === 0 ? (
        <div className="audit__empty">
          还没有审计记录
          <div className="audit__empty-hint">
            每调用一次模型就会记下一条 —— 只有元数据，不记提问与回答的原文。
          </div>
        </div>
      ) : (
        <div className="audit__table-wrap">
          <table className="audit__table">
            <thead>
              <tr>
                {AUDIT_COLUMNS.map((column) => (
                  <th key={column.key} style={{ width: column.width }}>
                    {column.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr
                  key={row.id}
                  title={row.hint}
                  className={
                    row.success === false ? "audit__row--failed" : undefined
                  }
                >
                  <td className="audit__cell audit__cell--time">{row.timestamp}</td>
                  <td className="audit__cell">{row.task}</td>
                  <td className="audit__cell audit__cell--model">{row.model}</td>
                  <td className="audit__cell audit__cell--num">{row.tokens}</td>
                  <td className="audit__cell audit__cell--num">{row.duration}</td>
                  <td
                    className={`audit__cell audit__cell--num ${
                      row.success === false ? "audit__cell--bad" : ""
                    }`}
                  >
                    {row.outcome}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <style>{auditStyles}</style>
    </div>
  );
}

const auditStyles = `
  .audit { padding: 10px; font-size: 12px; }
  .audit__summary {
    display: flex;
    gap: 6px;
    margin-bottom: 10px;
  }
  .audit__stat {
    flex: 1;
    background: var(--bg-primary);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 6px 8px;
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }
  .audit__stat-value {
    font-size: 15px;
    font-weight: 600;
    color: var(--text-primary);
  }
  .audit__stat-value--bad { color: var(--danger); }
  .audit__stat-label {
    font-size: 10px;
    color: var(--text-tertiary);
    white-space: nowrap;
  }
  .audit__toolbar {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-bottom: 8px;
  }
  .audit__btn {
    border: 1px solid var(--border);
    background: var(--bg-primary);
    color: var(--text-primary);
    border-radius: 4px;
    font-size: 11px;
    padding: 3px 8px;
    cursor: pointer;
  }
  .audit__btn:hover { background: var(--bg-secondary); }
  .audit__btn:disabled { opacity: 0.6; cursor: default; }
  .audit__btn--danger {
    background: var(--danger);
    border-color: var(--danger);
    color: #fff;
  }
  .audit__btn--danger:hover { background: #b91c1c; }
  .audit__total {
    color: var(--text-tertiary);
    font-size: 11px;
    white-space: nowrap;
  }
  .audit__notice {
    border-radius: 4px;
    font-size: 11px;
    padding: 5px 8px;
    margin-bottom: 8px;
    line-height: 1.5;
  }
  .audit__notice--ok {
    background: var(--bg-tertiary);
    color: var(--text-secondary);
  }
  .audit__notice--bad {
    background: var(--conflict-bg);
    color: var(--conflict-border);
  }
  .audit__empty {
    color: var(--text-tertiary);
    font-size: 12px;
    text-align: center;
    padding: 20px 8px;
    line-height: 1.5;
  }
  .audit__empty-hint {
    font-size: 11px;
    margin-top: 6px;
  }
  .audit__table-wrap {
    overflow-x: auto;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--bg-primary);
  }
  .audit__table {
    border-collapse: collapse;
    width: 100%;
    font-size: 11px;
  }
  .audit__table th {
    text-align: left;
    font-weight: 600;
    color: var(--text-tertiary);
    padding: 5px 6px;
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
  }
  .audit__table td {
    padding: 4px 6px;
    border-bottom: 1px solid var(--border);
    color: var(--text-secondary);
    vertical-align: top;
  }
  .audit__table tr:last-child td { border-bottom: none; }
  .audit__row--failed td { background: var(--conflict-bg); }
  .audit__cell { white-space: nowrap; }
  .audit__cell--model {
    max-width: 92px;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .audit__cell--num { text-align: right; }
  .audit__cell--bad { color: var(--danger); font-weight: 600; }
`;
