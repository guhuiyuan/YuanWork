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
  auditHint,
  auditRows,
  auditSummary,
  formatCount,
  formatDuration,
  formatOutcome,
  formatTokens,
  formatTimestamp,
  runClear,
  AUDIT_COLUMNS,
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
  assert.match(tabSource, /runClear\(clearAuditLog\)/);
  assert.match(tabSource, /notice\.text/);
  assert.match(tabSource, /notice\.ok/);
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
