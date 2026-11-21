/**
 * The reasoning trace, as the backend sends it.
 *
 * The backend sends one `trace` event per step of a run - understand,
 * localize, extract, wiki_compile, gate, answer, check_conflicts - and
 * each event says what that step did (ADR-053). The panel on the right
 * shows those steps as they happen, so the reader can see *why* an
 * answer was given - or refused.
 *
 * Two rules shape this module:
 *
 * - **Only the step and its result.** The `understand` step carries the
 *   model's reading of the question. That is the model's words, not a
 *   fact, and the reader is not in a position to judge it - so it is
 *   not shown. What is shown is what the step *did*: how many
 *   candidates it found, how many pieces of evidence it took, what the
 *   gate decided.
 * - **A dirty event changes nothing.** An event that is not a step
 *   leaves the list exactly as it was - the same array, so a reader
 *   that watches the list does not re-render for nothing.
 */

export type TraceStatus = "started" | "completed" | "cancelled";
export type TraceOutcome = "ok" | "refused" | "conflict" | "skipped";

export interface TraceStep {
  /** The step's name, as the backend spells it. */
  step: string;
  status: TraceStatus;
  /** One line: what this step found or decided. */
  detail: string;
  outcome?: TraceOutcome;
  /** The reason code behind a refusal or a skipped step. */
  reason?: string;
  /** How long the step took, in milliseconds. */
  durationMs?: number;
  /** When the step started - the panel times the running step itself. */
  startedAt?: number;
}

/** The steps, in the order the backend runs them. */
export const TRACE_STEP_LABELS: Record<string, string> = {
  understand: "理解问题",
  localize: "定位文件",
  extract: "抽取证据",
  wiki_compile: "编译页面",
  gate: "证据闸",
  answer: "生成回答",
  check_conflicts: "冲突判定",
};

/**
 * The reasons a run can end in a refusal.
 *
 * The codes come from the evidence gate and from the agent; unknown
 * codes fall back to the code itself rather than being swallowed.
 */
export const TRACE_REASON_LABELS: Record<string, string> = {
  insufficient_evidence: "证据条数不够",
  unlocatable_evidence: "证据定位不到原文",
  model_insufficient: "模型认为证据不足以回答",
  model_unavailable: "模型没连上",
  citation_invalid: "引用与资料对不上",
  index_missing: "点名的文件没进索引",
  gate_error: "判定过程出错",
  no_evidence: "没有找到证据",
};

/** What the model thought the question was - a fixed vocabulary. */
const INTENT_LABELS: Record<string, string> = {
  find_clause: "找条款",
  summarize: "总结",
  compare: "对比",
  explain: "解释",
  locate: "定位",
  general: "一般提问",
};

/** The name of a step, in the reader's words. */
export function traceStepLabel(step: string): string {
  return TRACE_STEP_LABELS[step] ?? step;
}

/** The reason behind a refusal, in the reader's words. */
export function reasonLabel(reason: string | null | undefined): string {
  if (!reason) return "（没有说原因）";
  return TRACE_REASON_LABELS[reason] ?? reason;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/** How many candidates to name before the list gets long. */
const MAX_NAMED_CANDIDATES = 3;

function detailFor(
  step: string,
  data: Record<string, unknown>,
  status: TraceStatus
): { detail: string; outcome?: TraceOutcome; reason?: string } {
  switch (step) {
    case "understand": {
      const understanding = isRecord(data.understanding) ? data.understanding : {};
      const intent = typeof understanding.intent === "string" ? understanding.intent : "";
      const label = INTENT_LABELS[intent];
      return {
        detail: label ? `这是一句「${label}」的问题` : "读完了问题",
        outcome: "ok",
      };
    }
    case "localize": {
      const count = typeof data.candidate_count === "number" ? data.candidate_count : 0;
      const names = Array.isArray(data.candidates)
        ? data.candidates
            .map((c) => (isRecord(c) && typeof c.file_name === "string" ? c.file_name : null))
            .filter((n): n is string => !!n)
            .slice(0, MAX_NAMED_CANDIDATES)
        : [];
      const named = names.length ? `：${names.join("、")}` : "";
      return { detail: `找到 ${count} 个候选文件${named}`, outcome: "ok" };
    }
    case "extract": {
      const count = typeof data.evidence_count === "number" ? data.evidence_count : 0;
      return { detail: `取到 ${count} 条证据`, outcome: "ok" };
    }
    case "wiki_compile": {
      const compile = isRecord(data.wiki_compile) ? data.wiki_compile : {};
      if (compile.triggered) {
        const title = typeof compile.title === "string" ? compile.title : "";
        return {
          detail: title ? `编译了《${title}》` : "编译了一个页面",
          outcome: "ok",
        };
      }
      return { detail: "没有触发编译", outcome: "skipped", reason: "not_triggered" };
    }
    case "gate": {
      const gate = isRecord(data.gate) ? data.gate : {};
      const passed = gate.passed === true;
      if (passed) {
        const count = typeof gate.sufficient_count === "number" ? gate.sufficient_count : 0;
        const modelRan = gate.model_evaluated === true;
        return {
          detail: `通过（${modelRan ? "硬规则与模型自评都通过" : "硬规则通过，模型自评未跑"}），可用证据 ${count} 条`,
          outcome: "ok",
        };
      }
      const reason = typeof gate.reason === "string" ? gate.reason : "";
      return {
        detail: `未通过：${reasonLabel(reason)}`,
        outcome: "refused",
        reason,
      };
    }
    case "answer": {
      const refused = data.refused === true;
      if (refused) {
        return { detail: "没有作答", outcome: "refused" };
      }
      const confidence = isRecord(data.confidence) ? data.confidence : {};
      const score = typeof confidence.confidence === "number" ? confidence.confidence : null;
      const suffix = score === null ? "" : `，证据置信度 ${Math.round(score * 100)}%`;
      return { detail: `已作答${suffix}`, outcome: "ok" };
    }
    case "check_conflicts": {
      const count = typeof data.conflict_count === "number" ? data.conflict_count : 0;
      return {
        detail: count ? `发现 ${count} 处分歧` : "没有发现分歧",
        outcome: count ? "conflict" : "ok",
      };
    }
    default:
      return { detail: status === "started" ? "进行中" : "已完成", outcome: "ok" };
  }
}

/**
 * Fold one trace event into the steps.
 *
 * The backend sends `started` and then `completed` for each step, so a
 * step that is already there is updated in place; a step that is new is
 * appended - which keeps the order the backend ran them in.
 */
export function applyTraceEvent(
  steps: TraceStep[],
  data: unknown,
  now: number = Date.now()
): TraceStep[] {
  if (!isRecord(data)) return steps;
  const step = data.step;
  const status = data.status;
  if (typeof step !== "string" || !step) return steps;
  if (status !== "started" && status !== "completed" && status !== "cancelled") {
    return steps;
  }

  const index = steps.findIndex((s) => s.step === step);
  const existing = index >= 0 ? steps[index] : undefined;

  if (status === "started") {
    const next: TraceStep = {
      step,
      status: "started",
      detail: "进行中",
      startedAt: now,
    };
    if (!existing) return [...steps, next];
    const copy = steps.slice();
    copy[index] = { ...existing, status: "started", startedAt: now, detail: "进行中" };
    return copy;
  }

  const { detail, outcome, reason } = detailFor(step, data, status);
  const durationMs =
    typeof data.duration_ms === "number"
      ? data.duration_ms
      : existing?.startedAt !== undefined
        ? now - existing.startedAt
        : undefined;

  const next: TraceStep = { step, status, detail, outcome, reason, durationMs };
  if (!existing) return [...steps, next];
  const copy = steps.slice();
  copy[index] = { ...existing, ...next, startedAt: existing.startedAt };
  return copy;
}

/**
 * The refusal reason, which arrives with the refusal event rather than
 * with a step - so it is put on the answering step.
 */
export function applyRefusalReason(
  steps: TraceStep[],
  reason: string | undefined
): TraceStep[] {
  if (!reason) return steps;
  const index = steps.findIndex((s) => s.step === "answer");
  if (index < 0) return steps;
  const copy = steps.slice();
  const answer = copy[index];
  copy[index] = {
    ...answer,
    outcome: "refused",
    reason,
    detail: `没有作答：${reasonLabel(reason)}`,
  };
  return copy;
}
