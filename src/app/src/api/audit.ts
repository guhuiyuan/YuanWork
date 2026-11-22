/**
 * The audit log, as the reader sees it.
 *
 * The backend keeps one line per model call: when it happened, which
 * model answered, what the task was, the hash of the prompt, the token
 * counts, how long it took and whether it worked (ADR-018).
 *
 * Two rules shape this module:
 *
 * - **Only the metadata.** A line holds the *hash* of a prompt, never
 *   its text - so nothing here can put the question or the answer on
 *   the screen. The reader gets "it cost this much", not "it said
 *   that" (ADR-018).
 * - **The hash only shows on hover.** The panel is one column wide;
 *   the hash is there for the reader who compares two calls, not for
 *   the reader who scans the table (ADR-056).
 *
 * Everything here is a pure function of its arguments, so the wording
 * of the table can be pinned by a test instead of by a screenshot.
 */

/** One line of the log - the metadata of a model call. */
export interface AuditEntry {
  /** When the call happened (ISO, local time). */
  timestamp?: string;
  /** "llm_call" - the log also knows how to record other things. */
  type?: string;
  /** The model that answered, as the gateway routed it. */
  model?: string;
  /** The work the call was for: "answer", "understand", ... */
  task_type?: string;
  /** 16 hex characters - the hash of the prompt, never the prompt. */
  prompt_hash?: string;
  /** How long the prompt was, in characters. */
  prompt_length?: number;
  /** What the prompt cost. */
  prompt_tokens?: number;
  /** What the answer cost. */
  completion_tokens?: number;
  /** How long the call took. */
  duration_ms?: number;
  /** Whether it worked. */
  success?: boolean;
  /** The kind of failure - never the text of the exception. */
  error_kind?: string | null;
}

/** What `GET /api/v1/audit` answers. */
export interface AuditListPayload {
  /** How many entries this answer holds. */
  count: number;
  /** How many the log holds in all. */
  total: number;
  /** The entries, newest first. */
  entries: AuditEntry[];
}

/** What `GET /api/v1/audit/stats` answers. */
export interface AuditStats {
  /** Every line in the log. */
  total_entries: number;
  /** The lines that are model calls. */
  llm_calls: number;
  /** The calls that failed. */
  failures: number;
  /** What all the prompts cost. */
  total_prompt_tokens: number;
  /** What all the answers cost. */
  total_completion_tokens: number;
  /** The calls per model. */
  by_model?: Record<string, number>;
  /** The calls per kind of task. */
  by_task_type?: Record<string, number>;
}

/** What `DELETE /api/v1/audit` answers. */
export interface AuditClearResult {
  /** How many entries were removed. */
  cleared: number;
}

/** How many entries the panel asks for. */
export const AUDIT_LIMIT = 100;

/**
 * The most entries the backend hands out at once.
 *
 * Asking for more comes back as a 422 - and a raw HTTP error is not
 * something the reader can act on (ADR-056).
 */
export const AUDIT_LIMIT_MAX = 1000;

/** The label of the clear button - it clears on the first click. */
export const CLEAR_LABEL = "清空";

/** The label of the clear button when it asks first. */
export const CONFIRM_CLEAR_LABEL = "确认清空？";

/** The empty cell of the table - a dash, not a blank. */
export const EMPTY = "—";

/** The kinds of task the gateway routes, in the reader's words. */
export const AUDIT_TASK_LABELS: Record<string, string> = {
  understand: "理解问题",
  extract: "抽取证据",
  answer: "生成回答",
  conflict_check: "冲突判定",
  evidence_gate: "证据闸",
  reflection: "反思",
  wiki_compile: "编译页面",
  ner: "实体抽取",
  general: "通用",
};

/** The kinds of failure the log can carry, in the reader's words. */
export const AUDIT_ERROR_LABELS: Record<string, string> = {
  timeout: "超时",
  connection: "连接失败",
  rate_limit: "限流",
  auth: "鉴权失败",
  invalid_response: "回答无法解析",
  cancelled: "已取消",
  unknown: "未知错误",
};

/** The columns of the table, in the order they are shown. */
export type AuditColumnKey =
  | "timestamp"
  | "task"
  | "model"
  | "tokens"
  | "duration"
  | "outcome";

export interface AuditColumn {
  key: AuditColumnKey;
  label: string;
  /** How wide the column is - the panel is narrow. */
  width: string;
}

/**
 * The visible columns.
 *
 * The prompt hash is not one of them: it is in the tooltip of the row
 * and nowhere else (ADR-056).
 */
export const AUDIT_COLUMNS: AuditColumn[] = [
  { key: "timestamp", label: "时刻", width: "92px" },
  { key: "task", label: "任务", width: "68px" },
  { key: "model", label: "模型", width: "92px" },
  { key: "tokens", label: "令牌（提示/回答）", width: "108px" },
  { key: "duration", label: "耗时", width: "58px" },
  { key: "outcome", label: "结果", width: "44px" },
];

/** One line of the table, with everything already said in words. */
export interface AuditRow {
  /** The key the row is drawn under. */
  id: string;
  /** When the call happened. */
  timestamp: string;
  /** The kind of work the call was for. */
  task: string;
  /** The model that answered. */
  model: string;
  /** The prompt and the answer, in tokens. */
  tokens: string;
  /** How long the call took. */
  duration: string;
  /** Whether it worked. */
  outcome: string;
  /** Whether it worked - null when the log does not say. */
  success: boolean | null;
  /**
   * The tooltip of the row: the prompt hash shows here and nowhere
   * else.
   */
  hint: string;
}

/** The three numbers the summary shows. */
export interface AuditSummary {
  /** How many model calls were made. */
  calls: number;
  /** What all of them cost, in tokens. */
  tokens: number;
  /** How many of them failed. */
  failures: number;
}

/** What clearing the log did - either it happened, or it did not. */
export interface AuditClearOutcome {
  /** Whether the log was cleared. */
  ok: boolean;
  /** How many entries were removed - zero when nothing was. */
  cleared: number;
  /** What to say about it. */
  message: string;
}

/** What the table shows after the reader tried to clear the log. */
export interface AuditPanelState {
  /** The lines of the table. */
  entries: AuditEntry[];
  /** How many lines the log holds. */
  total: number;
}

/** A number the backend sent, or zero when it sent nothing. */
function numberOf(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

/** A number the backend sent, or null when it sent nothing. */
function countOf(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * The limit inside what the backend accepts.
 *
 * The backend refuses anything below 1 or above a thousand with a 422;
 * the panel would show the reader a raw HTTP error instead of the log.
 */
export function clampAuditLimit(limit: number | null | undefined): number {
  const n = countOf(limit);
  if (n === null) return AUDIT_LIMIT;
  return Math.min(AUDIT_LIMIT_MAX, Math.max(1, Math.round(n)));
}

/**
 * The next step of "show me more": 100, then 500, then the ceiling.
 *
 * The log holds thousands of lines and the panel is narrow - but the
 * point of the audit log is to look up the one call that went wrong,
 * which the last hundred calls alone cannot answer (ADR-056).
 */
export function nextAuditLimit(current: number): number {
  const n = countOf(current);
  const base = n === null || n < 1 ? AUDIT_LIMIT : n;
  return clampAuditLimit(base * 5);
}

/** What to say about a failure - the message of the error, in words. */
export function reasonOf(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  if (typeof error === "string" && error.trim()) return error;
  if (error === null || error === undefined) return "未知错误";
  return String(error);
}

/**
 * A number in the reader's eyes: 1234 is "1,234".
 *
 * The grouping is done here rather than by the locale, so the number
 * looks the same wherever the app runs.
 */
export function formatCount(value: number): string {
  const n = Math.round(numberOf(value));
  const sign = n < 0 ? "-" : "";
  const digits = String(Math.abs(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return sign + digits;
}

/**
 * When the call happened - "11-22 10:11:12".
 *
 * The year is in the tooltip of the row, not in the cell: the panel
 * is narrow and the reader scans the time, not the date.
 */
export function formatTimestamp(raw: string | undefined | null): string {
  if (!raw) return EMPTY;
  const withSeconds = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})/.exec(
    raw
  );
  if (withSeconds) {
    return `${withSeconds[2]}-${withSeconds[3]} ${withSeconds[4]}:${withSeconds[5]}:${withSeconds[6]}`;
  }
  const withoutSeconds = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/.exec(raw);
  if (withoutSeconds) {
    return `${withoutSeconds[2]}-${withoutSeconds[3]} ${withoutSeconds[4]}:${withoutSeconds[5]}`;
  }
  return raw;
}

/** The whole time, with the year - "2026-11-22 10:11:12". */
export function formatFullTimestamp(raw: string | undefined | null): string {
  if (!raw) return EMPTY;
  return raw.replace("T", " ").slice(0, 19);
}

/**
 * The two token counts, told apart.
 *
 * A pair of bare numbers ("640 / 128") leaves the reader guessing which
 * is the prompt and which is the answer - the header says it, and so
 * does the tooltip of the row (AC-F-036).
 */
function tokenParts(
  prompt?: number | null,
  completion?: number | null
): { prompt: string; completion: string } | null {
  const p = countOf(prompt);
  const c = countOf(completion);
  if (p === null && c === null) return null;
  return {
    prompt: p === null ? EMPTY : formatCount(p),
    completion: c === null ? EMPTY : formatCount(c),
  };
}

/** What the prompt and the answer cost - "128 / 64". */
export function formatTokens(
  prompt?: number | null,
  completion?: number | null
): string {
  const parts = tokenParts(prompt, completion);
  if (!parts) return EMPTY;
  return `${parts.prompt} / ${parts.completion}`;
}

/** How long the call took - "820 毫秒" or "1.2 秒". */
export function formatDuration(ms?: number | null): string {
  const value = countOf(ms);
  if (value === null) return EMPTY;
  if (value < 1000) return `${Math.round(value)} 毫秒`;
  return `${(value / 1000).toFixed(1)} 秒`;
}

/** Whether the call worked. */
export function formatOutcome(success: boolean | null): string {
  if (success === true) return "成功";
  if (success === false) return "失败";
  return EMPTY;
}

/** The kind of work the call was for, in the reader's words. */
export function formatTaskType(taskType?: string | null): string {
  if (!taskType) return EMPTY;
  return AUDIT_TASK_LABELS[taskType] ?? taskType;
}

/** The kind of failure, in the reader's words. */
export function formatErrorKind(errorKind?: string | null): string {
  if (!errorKind) return EMPTY;
  return AUDIT_ERROR_LABELS[errorKind] ?? errorKind;
}

/**
 * The tooltip of a row.
 *
 * This is where the prompt hash lives: the table itself stays narrow,
 * and the hash is there for the reader who needs to tell two calls
 * apart (ADR-056). The tooltip also says which of the two token
 * numbers is the prompt and which is the answer.
 */
export function auditHint(entry: AuditEntry): string {
  const parts: string[] = [];
  const when = formatFullTimestamp(entry?.timestamp);
  if (when !== EMPTY) parts.push(when);
  if (entry?.model) parts.push(`模型：${entry.model}`);
  if (entry?.prompt_hash) parts.push(`提示词哈希：${entry.prompt_hash}`);
  const tokens = tokenParts(entry?.prompt_tokens, entry?.completion_tokens);
  if (tokens) parts.push(`令牌：提示 ${tokens.prompt} / 回答 ${tokens.completion}`);
  if (entry?.success === false) {
    const kind = formatErrorKind(entry.error_kind);
    parts.push(kind === EMPTY ? "失败" : `失败原因：${kind}`);
  }
  return parts.join(" · ");
}

/**
 * The rows of the table.
 *
 * Only the metadata is read - a field the backend may add later (a
 * prompt, an answer) is never picked up, so it cannot reach the
 * screen even if the backend starts sending it (ADR-018).
 */
export function auditRows(
  entries: AuditEntry[] | null | undefined
): AuditRow[] {
  if (!Array.isArray(entries)) return [];
  return entries.map((entry, index) => {
    const e = entry ?? {};
    const success = typeof e.success === "boolean" ? e.success : null;
    const model = e.model && e.model.trim() ? e.model : EMPTY;
    return {
      id: `${index}-${e.timestamp ?? ""}-${e.prompt_hash ?? ""}`,
      timestamp: formatTimestamp(e.timestamp),
      task: formatTaskType(e.task_type),
      model,
      tokens: formatTokens(e.prompt_tokens, e.completion_tokens),
      duration: formatDuration(e.duration_ms),
      outcome: formatOutcome(success),
      success,
      hint: auditHint(e),
    };
  });
}

/** The three numbers the summary shows. */
export function auditSummary(
  stats: AuditStats | null | undefined
): AuditSummary {
  return {
    calls: numberOf(stats?.llm_calls),
    tokens:
      numberOf(stats?.total_prompt_tokens) +
      numberOf(stats?.total_completion_tokens),
    failures: numberOf(stats?.failures),
  };
}

/**
 * Whether the log really is empty after the clear.
 *
 * The backend answers with the number of lines it was holding - but a
 * delete that fails on disk is swallowed there, and the answer would
 * still say "cleared". So the panel asks the log itself: if the old
 * lines are still there, the clear did not happen (AC-F-037).
 */
export function clearSucceeded(cleared: number, totalAfter: number): boolean {
  // There was nothing to clear, so nothing could have survived.
  if (cleared <= 0) return true;
  // The log is empty - it is gone.
  if (totalAfter <= 0) return true;
  // Fewer lines than before: what is left are calls made in between.
  return totalAfter < cleared;
}

/**
 * Clear the log - and say what happened.
 *
 * Clearing takes the reader's records off their own machine, so it has
 * to report how many went away. A failure says so: a log that says
 * "cleared" when nothing was cleared is a lie the reader cannot check
 * (AC-F-037).
 *
 * The `verify` step reads the log back after the delete. Without it
 * the panel would repeat the number the backend handed it - and that
 * number is the number the backend *was* holding, not the number that
 * went away.
 */
export async function runClear(
  clear: () => Promise<{ cleared?: number | null } | undefined>,
  verify?: () => Promise<AuditListPayload | undefined>
): Promise<AuditClearOutcome> {
  let cleared: number;
  try {
    const result = await clear();
    cleared = numberOf(result?.cleared);
  } catch (e) {
    return { ok: false, cleared: 0, message: `清空失败：${reasonOf(e)}` };
  }

  if (verify) {
    let after: AuditListPayload | undefined;
    try {
      after = await verify();
    } catch (e) {
      // The log cannot be read, so it cannot be said to be empty.
      return {
        ok: false,
        cleared: 0,
        message: `清空失败：清空后读不到审计记录，没法确认是否清空（${reasonOf(e)}）`,
      };
    }
    const left = numberOf(after?.total);
    if (!clearSucceeded(cleared, left)) {
      return {
        ok: false,
        cleared: 0,
        message: `清空失败：还有 ${formatCount(left)} 条记录没被删除`,
      };
    }
  }

  return {
    ok: true,
    cleared,
    message: `已清空 ${formatCount(cleared)} 条记录`,
  };
}

/**
 * What the table shows after the reader tried to clear the log.
 *
 * The table is emptied only when the clear really happened: a failed
 * clear leaves the records on the screen, so the reader can see what
 * did not go away (AC-F-037).
 */
export function applyClear(
  entries: AuditEntry[],
  total: number,
  outcome: AuditClearOutcome
): AuditPanelState {
  if (!outcome.ok) return { entries, total };
  return { entries: [], total: 0 };
}
