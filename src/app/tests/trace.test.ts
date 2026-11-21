/**
 * The reasoning trace on the right.
 *
 * The backend sends one event per step of a run; the panel shows those
 * steps, not the last three chat messages (ADR-053). This file pins
 * the two halves of that: the folding of the events into steps, and
 * the fact that the panel is fed by the events rather than by the
 * conversation.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import {
  applyRefusalReason,
  applyTraceEvent,
  reasonLabel,
  traceStepLabel,
  type TraceStep,
} from "../src/api/trace.ts";

const here = dirname(fileURLToPath(import.meta.url));
const read = (...parts: string[]) =>
  readFileSync(join(here, "..", ...parts), "utf-8");

const chatSource = read("src", "components", "ChatWindow.tsx");
const rightPanelSource = read("src", "components", "RightPanel.tsx");
const storeSource = read("src", "store.ts");

/** The events of a whole run, in the order the backend sends them. */
const RUN: unknown[] = [
  { step: "understand", status: "started" },
  { step: "understand", status: "completed", understanding: { intent: "summarize" } },
  {
    step: "localize",
    status: "completed",
    candidate_count: 2,
    candidates: [{ file_name: "a.md", score: 0.9 }],
  },
  { step: "extract", status: "completed", evidence_count: 12 },
  { step: "gate", status: "completed", gate: { passed: true, sufficient_count: 12 } },
  {
    step: "answer",
    status: "completed",
    refused: false,
    confidence: { confidence: 0.72 },
  },
  { step: "check_conflicts", status: "completed", conflict_count: 0 },
];

test("the steps come out in the order the backend ran them", () => {
  let steps: TraceStep[] = [];
  for (const event of RUN) steps = applyTraceEvent(steps, event, 1000);
  assert.deepEqual(
    steps.map((s) => s.step),
    ["understand", "localize", "extract", "gate", "answer", "check_conflicts"]
  );
});

test("a step that is started shows as running, and its file names are named", () => {
  let steps: TraceStep[] = [];
  steps = applyTraceEvent(steps, { step: "understand", status: "started" }, 1000);
  assert.equal(steps[0].status, "started");
  assert.equal(steps[0].detail, "进行中");

  steps = applyTraceEvent(
    steps,
    {
      step: "localize",
      status: "completed",
      candidate_count: 2,
      candidates: [{ file_name: "a.md", score: 0.9 }, { file_name: "b.md", score: 0.5 }],
    },
    1000
  );
  assert.match(steps[1].detail, /2/);
  assert.match(steps[1].detail, /a\.md/);
});

test("each step says what it found", () => {
  let steps: TraceStep[] = [];
  for (const event of RUN) steps = applyTraceEvent(steps, event, 1000);
  const byStep = Object.fromEntries(steps.map((s) => [s.step, s.detail]));
  assert.match(byStep.understand, /总结/);
  assert.match(byStep.extract, /12 条证据/);
  assert.match(byStep.gate, /通过/);
  assert.match(byStep.answer, /已作答/);
  assert.match(byStep.check_conflicts, /没有发现分歧/);
});

test("the model's own reading of the question is not shown", () => {
  // The understanding is the model's words. The panel shows the intent
  // as a word from a fixed list, never the text.
  let steps: TraceStep[] = [];
  steps = applyTraceEvent(
    steps,
    {
      step: "understand",
      status: "completed",
      understanding: { intent: "summarize", topic: "采购合同的验收期限" },
    },
    1000
  );
  assert.doesNotMatch(steps[0].detail, /采购合同/);
});

test("a gate that says no says why", () => {
  let steps: TraceStep[] = [];
  steps = applyTraceEvent(
    steps,
    { step: "gate", status: "completed", gate: { passed: false, reason: "insufficient_evidence" } },
    1000
  );
  assert.equal(steps[0].outcome, "refused");
  assert.match(steps[0].detail, /证据条数不够/);
});

test("a refusal reason lands on the answering step", () => {
  let steps: TraceStep[] = [];
  for (const event of RUN) steps = applyTraceEvent(steps, event, 1000);
  steps = applyRefusalReason(steps, "index_missing");
  const answer = steps.find((s) => s.step === "answer");
  assert.ok(answer);
  assert.equal(answer?.outcome, "refused");
  assert.match(answer?.detail ?? "", /没进索引/);
});

test("a reason that is not known is still shown", () => {
  assert.equal(reasonLabel("something_new"), "something_new");
  assert.equal(reasonLabel("model_insufficient"), "模型认为证据不足以回答");
  assert.equal(traceStepLabel("gate"), "证据闸");
});

test("an event that is not a step changes nothing", () => {
  const steps: TraceStep[] = [];
  assert.equal(applyTraceEvent(steps, null), steps);
  assert.equal(applyTraceEvent(steps, { hello: "world" }), steps);
  assert.equal(applyTraceEvent(steps, { step: "gate", status: "weird" }), steps);
});

test("the panel is fed by the trace events", () => {
  // The event the backend sends has to be handled - it used to be
  // dropped on the floor.
  assert.match(chatSource, /case "trace"/);
  assert.match(chatSource, /applyTrace\(ev\.data\)/);
});

test("a new question starts a new trace", () => {
  assert.match(chatSource, /clearTrace\(\)/);
  assert.match(storeSource, /clearMessages: \(\) => set\(\{ messages: \[\], trace: \[\] \}\)/);
});

test("the panel shows the steps, not the last three messages", () => {
  // The old panel showed the last three chat messages under a promise
  // of something else. That is what the reader complained about.
  assert.doesNotMatch(rightPanelSource, /阶段 2 将可视化/);
  assert.doesNotMatch(rightPanelSource, /messages\.slice\(-3\)/);
  assert.match(rightPanelSource, /trace\.map\(\(step\)/);
  assert.match(rightPanelSource, /traceStepLabel\(step\.step\)/);
});
