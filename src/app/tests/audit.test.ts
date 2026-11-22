/**
 * The audit log on the right.
 *
 * The backend has been keeping one line per model call all along; the
 * panel was the missing half - it used to say "审计日志（阶段 3 启用）"
 * and promised a "查询摘要" that is not in the log (ADR-056).
 *
 * What this file pins:
 *
 * - **Only the metadata.** The log holds the hash of a prompt, never
 *   the prompt (ADR-018) - so the table cannot show the question or
 *   the answer, and it does not promise one either.
 * - **The totals.** Calls, tokens, failures.
 * - **The clear.** It goes through DELETE, reports how many records
 *   went away, and says so when it failed.
 * - **The bridge.** The desktop app reaches the backend through Rust
 *   commands - three of them have to exist and be registered, or the
 *   panel is empty on the very machine the records are on.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import {
  applyClear,
  auditHint,
  auditRows,
  auditSummary,
  clampAuditLimit,
  clearSucceeded,
  formatCount,
  formatDuration,
  formatOutcome,
  formatTokens,
  formatTimestamp,
  nextAuditLimit,
  reasonOf,
  runClear,
  AUDIT_COLUMNS,
  AUDIT_LIMIT,
  AUDIT_LIMIT_MAX,
  CLEAR_LABEL,
  CONFIRM_CLEAR_LABEL,
  type AuditEntry,
} from "../src/api/audit.ts";

const here = dirname(fileURLToPath(import.meta.url));
const read = (...parts: string[]) =>
  readFileSync(join(here, "..", ...parts), "utf-8");

const apiSource = read("src", "api", "backend.ts");
const auditSource = read("src", "api", "audit.ts");
const tabSource = read("src", "components", "AuditTab.tsx");
const rightPanelSource = read("src", "components", "RightPanel.tsx");
const commandsSource = read("src-tauri", "src", "commands.rs");
const libSource = read("src-tauri", "src", "lib.rs");

/** One call, the way the backend writes it down. */
const CALL: AuditEntry = {
  timestamp: "2026-11-22T09:31:05.123456",
  type: "llm_call",
  model: "MiniMax-M3",
  task_type: "answer",
  prompt_hash: "a1b2c3d4e5f6a7b8",
  prompt_length: 812,
  prompt_tokens: 640,
  completion_tokens: 128,
  duration_ms: 2340.5,
  success: true,
  error_kind: null,
};

// ===== the table has only metadata in it =====

test("the table does not promise a summary that is not in the log", () => {
  // The old panel said it recorded the "查询摘要" - the entries have
  // no summary, only a hash (ADR-018).
  for (const source of [auditSource, tabSource, rightPanelSource]) {
    assert.doesNotMatch(source, /查询摘要/);
  }
});

test("the promise that was never kept is gone from the panel", () => {
  assert.doesNotMatch(rightPanelSource, /阶段 3 启用/);
  assert.match(rightPanelSource, /<AuditTab \/>/);
});

test("the question and the answer never reach the table", () => {
  // Even if the backend ever sent them, the table only picks up the
  // metadata it knows - the text has no way in (ADR-018).
  const leaky = {
    ...CALL,
    prompt: "采购合同的验收期限是多久？",
    response: "验收期限为 30 日。",
    summary: "验收期限为 30 日。",
    answer: "验收期限为 30 日。",
  } as unknown as AuditEntry;

  const row = auditRows([leaky])[0];
  const shown = JSON.stringify(row);
  for (const text of ["验收期限", "采购合同", "30 日"]) {
    assert.doesNotMatch(shown, new RegExp(text));
  }
  // what is left is the metadata
  assert.equal(row.timestamp, "11-22 09:31:05");
  assert.equal(row.model, "MiniMax-M3");
  assert.equal(row.tokens, "640 / 128");
  assert.equal(row.duration, "2.3 秒");
  assert.equal(row.outcome, "成功");
});

test("the prompt hash only shows in the tooltip", () => {
  // ADR-056: the hash is there for the reader who compares two calls,
  // not for the reader who scans the table.
  //
  // AC-F-036 lists the hash among the things the table shows, and
  // ADR-056 keeps it out of the narrow columns. The two cannot both be
  // true of a column - the panel follows ADR-056 and the row still
  // carries the hash, in the tooltip. The wording of AC-F-036 has to
  // be reconciled with it (CLAUDE.md rule 4).
  const row = auditRows([CALL])[0];
  assert.match(row.hint, /a1b2c3d4e5f6a7b8/);
  assert.match(row.hint, /提示词哈希/);
  // it is not a column of its own
  const keys = AUDIT_COLUMNS.map((c) => c.key);
  assert.ok(!keys.includes("prompt_hash" as never));
  assert.deepEqual(keys, [
    "timestamp",
    "task",
    "model",
    "tokens",
    "duration",
    "outcome",
  ]);
});

test("the two token numbers say which is which", () => {
  // AC-F-036 asks for "the prompt and the answer token counts" - two
  // bare numbers do not say which is which.
  const column = AUDIT_COLUMNS.find((c) => c.key === "tokens");
  assert.match(column?.label ?? "", /提示/);
  assert.match(column?.label ?? "", /回答/);
  // and the tooltip of the row says it too
  assert.match(auditHint(CALL), /令牌：提示 640 \/ 回答 128/);
});

test("the tooltip says what the table cannot show", () => {
  const hint = auditHint({
    ...CALL,
    success: false,
    error_kind: "timeout",
  });
  assert.match(hint, /2026-11-22 09:31:05/);
  assert.match(hint, /a1b2c3d4e5f6a7b8/);
  assert.match(hint, /超时/);
});

test("a line that says nothing is shown as a dash, not as a blank", () => {
  const row = auditRows([{} as AuditEntry])[0];
  assert.equal(row.timestamp, "—");
  assert.equal(row.task, "—");
  assert.equal(row.model, "—");
  assert.equal(row.tokens, "—");
  assert.equal(row.duration, "—");
  assert.equal(row.outcome, "—");
  assert.equal(row.success, null);
});

test("the words of the table are the reader's words", () => {
  assert.equal(formatTimestamp("2026-11-22T09:31:05.123456"), "11-22 09:31:05");
  assert.equal(formatTimestamp("2026-11-22T09:31"), "11-22 09:31");
  assert.equal(formatTokens(640, 128), "640 / 128");
  assert.equal(formatTokens(1234, 0), "1,234 / 0");
  assert.equal(formatDuration(820), "820 毫秒");
  assert.equal(formatDuration(2340.5), "2.3 秒");
  assert.equal(formatOutcome(true), "成功");
  assert.equal(formatOutcome(false), "失败");
  assert.equal(formatCount(12345), "12,345");
});

// ===== the totals =====

test("the summary says how many calls, tokens and failures", () => {
  const summary = auditSummary({
    total_entries: 30,
    llm_calls: 12,
    failures: 2,
    total_prompt_tokens: 1000,
    total_completion_tokens: 500,
  });
  assert.equal(summary.calls, 12);
  assert.equal(summary.tokens, 1500);
  assert.equal(summary.failures, 2);
});

test("a summary that was never read shows zeros, not blanks", () => {
  const summary = auditSummary(null);
  assert.equal(summary.calls, 0);
  assert.equal(summary.tokens, 0);
  assert.equal(summary.failures, 0);
});

test("the panel shows the three totals", () => {
  assert.match(tabSource, /调用次数/);
  assert.match(tabSource, /令牌数/);
  assert.match(tabSource, /失败次数/);
  assert.match(tabSource, /summary\.calls/);
  assert.match(tabSource, /summary\.tokens/);
  assert.match(tabSource, /summary\.failures/);
});

// ===== the clear =====

test("the clear goes through DELETE and reports the count", async () => {
  const outcome = await runClear(async () => ({ cleared: 7 }));
  assert.equal(outcome.ok, true);
  assert.equal(outcome.cleared, 7);
  assert.match(outcome.message, /7/);
  assert.match(outcome.message, /已清空/);

  // the backend is asked with a DELETE
  assert.match(apiSource, /export async function clearAuditLog/);
  assert.match(apiSource, /method: "DELETE"/);
  assert.match(apiSource, /"\/audit", \{ method: "DELETE" \}/);
});

test("a clear that failed says so and does not claim the log is empty", async () => {
  const outcome = await runClear(async () => {
    throw new Error("后端不在");
  });
  assert.equal(outcome.ok, false);
  assert.equal(outcome.cleared, 0);
  assert.match(outcome.message, /清空失败/);
  assert.match(outcome.message, /后端不在/);
  assert.doesNotMatch(outcome.message, /已清空/);
});

test("the panel reports the clear, and reports a failure as a failure", () => {
  assert.match(tabSource, /runClear\(clearAuditLog/);
  assert.match(tabSource, /notice\.text/);
  assert.match(tabSource, /notice\.ok/);
});

// ===== the clear tells the truth =====

test("a clear the log does not confirm is a failure, not a success", async () => {
  // The backend answers with the number of lines it *was* holding; a
  // delete that failed on the disk is swallowed there, so the answer
  // would still say "cleared". The panel reads the log back and
  // believes the log (AC-F-037).
  const outcome = await runClear(
    async () => ({ cleared: 12 }),
    async () => ({ count: 12, total: 12, entries: [] })
  );
  assert.equal(outcome.ok, false);
  assert.equal(outcome.cleared, 0);
  assert.match(outcome.message, /清空失败/);
  assert.doesNotMatch(outcome.message, /已清空/);
});

test("a clear the log confirms is a success", async () => {
  const outcome = await runClear(
    async () => ({ cleared: 12 }),
    async () => ({ count: 0, total: 0, entries: [] })
  );
  assert.equal(outcome.ok, true);
  assert.equal(outcome.cleared, 12);
  assert.match(outcome.message, /已清空 12 条记录/);
});

test("a clear that cannot be checked is not reported as done", async () => {
  // If the log cannot be read back, it cannot be said to be empty.
  const outcome = await runClear(
    async () => ({ cleared: 12 }),
    async () => {
      throw new Error("后端不在");
    }
  );
  assert.equal(outcome.ok, false);
  assert.doesNotMatch(outcome.message, /已清空/);
});

test("the log decides whether the clear worked", () => {
  assert.equal(clearSucceeded(12, 0), true); // the log is gone
  assert.equal(clearSucceeded(12, 3), true); // only new calls came in
  assert.equal(clearSucceeded(12, 12), false); // the old lines are still there
  assert.equal(clearSucceeded(12, 20), false);
  assert.equal(clearSucceeded(0, 0), true); // there was nothing to clear
});

test("a failed clear leaves the table as it was", () => {
  // AC-F-037: the table must not be emptied when nothing was cleared.
  const failed = applyClear([CALL], 1, {
    ok: false,
    cleared: 0,
    message: "清空失败：后端不在",
  });
  assert.equal(failed.entries.length, 1);
  assert.equal(failed.total, 1);
});

test("a clear that worked empties the table and the count", () => {
  const done = applyClear([CALL], 1, {
    ok: true,
    cleared: 1,
    message: "已清空 1 条记录",
  });
  assert.deepEqual(done.entries, []);
  assert.equal(done.total, 0);
});

test("the panel asks the log whether the clear worked", () => {
  // The decision lives in a function a test can see, not in a branch
  // of the component.
  assert.match(tabSource, /applyClear\(entries, total, outcome\)/);
  assert.match(tabSource, /runClear\(clearAuditLog, \(\) =>/);
});

test("the totals of a log that is gone are zero, not the old numbers", () => {
  // The summary must not keep the numbers of a log that no longer
  // exists.
  assert.match(tabSource, /setStats\(null\)/);
  assert.equal(auditSummary(null).calls, 0);
  assert.equal(auditSummary(null).tokens, 0);
  assert.equal(auditSummary(null).failures, 0);
});

// ===== the confirmation =====

test("the clear asks first, in place", () => {
  assert.equal(CLEAR_LABEL, "清空");
  assert.equal(CONFIRM_CLEAR_LABEL, "确认清空？");
  assert.match(tabSource, /CONFIRM_CLEAR_LABEL/);
  assert.match(tabSource, /setConfirming\(true\)/);
  // a native dialog blocks the webview - the question is answered
  // where the button is
  assert.doesNotMatch(tabSource, /window\.confirm/);
  assert.doesNotMatch(tabSource, /[^.\w]confirm\(/);
});

test("the confirmation is a second state of the button, and it can be taken back", () => {
  assert.match(tabSource, /setConfirming\(false\)/);
  assert.match(tabSource, /取消/);
});

// ===== how much of the log the panel can see =====

test("the limit stays inside what the backend accepts", () => {
  // The backend refuses anything above a thousand with a 422 - the
  // reader would get a raw HTTP error instead of the log.
  assert.equal(clampAuditLimit(100), 100);
  assert.equal(clampAuditLimit(0), 1);
  assert.equal(clampAuditLimit(-5), 1);
  assert.equal(clampAuditLimit(5000), AUDIT_LIMIT_MAX);
  assert.equal(clampAuditLimit(undefined), AUDIT_LIMIT);
});

test("more is asked for in steps, up to the ceiling", () => {
  assert.equal(nextAuditLimit(100), 500);
  assert.equal(nextAuditLimit(500), AUDIT_LIMIT_MAX);
  assert.equal(nextAuditLimit(AUDIT_LIMIT_MAX), AUDIT_LIMIT_MAX);
});

test("nothing asks the backend for more than it will give", () => {
  assert.match(apiSource, /clampAuditLimit/);
  // and the shell clamps it again, as a second hand
  assert.match(commandsSource, /clamp\(1, 1000\)/);
});

test("the reader can look further back than the last hundred calls", () => {
  // The audit log is worth having because the one call that went wrong
  // can be found (ADR-056) - the last hundred are not enough.
  assert.match(tabSource, /nextAuditLimit/);
  assert.match(tabSource, /显示更多/);
});

// ===== the table and the totals are two requirements =====

test("the table shows even when the totals cannot be read", () => {
  assert.match(tabSource, /allSettled/);
  assert.match(tabSource, /读不到审计记录/);
  assert.match(tabSource, /读不到审计汇总/);
});

test("a number that could not be read is a dash, not a zero", () => {
  // The log holds calls - a zero would say it is empty.
  assert.match(tabSource, /summaryKnown \? formatCount\(value\) : EMPTY/);
});

// ===== the bridge =====

test("the desktop shell asks for the audit log over the bridge", () => {
  // Without the bridge the panel is empty on the very machine the
  // records are on.
  assert.match(apiSource, /invoke<unknown>\("list_audit_entries"/);
  assert.match(apiSource, /invoke<unknown>\("audit_stats"/);
  assert.match(apiSource, /invoke<unknown>\("clear_audit_log"/);
  assert.match(apiSource, /\/audit\?limit=\$\{limit\}/);
  assert.match(apiSource, /"\/audit\/stats"/);
});

test("the three commands exist in the shell", () => {
  for (const name of ["list_audit_entries", "audit_stats", "clear_audit_log"]) {
    assert.match(
      commandsSource,
      new RegExp(`pub async fn ${name}`),
      `the command \`${name}\` is missing from commands.rs`
    );
  }
});

test("the three commands are registered", () => {
  for (const name of ["list_audit_entries", "audit_stats", "clear_audit_log"]) {
    assert.match(
      libSource,
      new RegExp(`commands::${name}`),
      `the command \`${name}\` is not registered in lib.rs`
    );
  }
});

test("the shell reads the endpoints the backend actually has", () => {
  assert.match(commandsSource, /\/api\/v1\/audit\?limit=/);
  assert.match(commandsSource, /\/api\/v1\/audit\/stats/);
  assert.match(commandsSource, /\/api\/v1\/audit"/);
});
