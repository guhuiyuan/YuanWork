/**
 * The terms of an answer, and the text they stand for.
 *
 * A term the reader may not know opens the passages it appears in -
 * the original text with the place it came from, never a generated
 * explanation (ADR-041). These tests pin the two halves of that: the
 * text is marked without being changed, and the summary says where
 * the term was found and nothing more.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import { markTerms, termContextSummary } from "../src/api/terms.ts";
import type { TermContext, TermContextItem } from "../src/store.ts";

const here = dirname(fileURLToPath(import.meta.url));
const chatSource = readFileSync(
  join(here, "..", "src", "components", "ChatWindow.tsx"),
  "utf-8"
);

function contextItem(overrides: Partial<TermContextItem> = {}): TermContextItem {
  return {
    text: "违约金按合同总额的三分之一计算。",
    file_name: "采购合同.md",
    doc_id: "doc-1",
    chunk_id: "chunk-1",
    location: "第 12 页",
    page: 12,
    ...overrides,
  };
}

function context(overrides: Partial<TermContext> = {}): TermContext {
  return {
    term: "违约责任",
    found: true,
    contexts: [contextItem()],
    ...overrides,
  };
}

// ===== marking the terms =====

test("a text without terms is one piece", () => {
  assert.deepEqual(markTerms("违约金按合同总额计算。", []), [
    { text: "违约金按合同总额计算。" },
  ]);
});

test("a term is marked where it stands", () => {
  assert.deepEqual(markTerms("违约金按合同总额计算。", ["违约金"]), [
    { text: "违约金", term: "违约金" },
    { text: "按合同总额计算。" },
  ]);
});

test("the longest term wins", () => {
  const segments = markTerms("采购合同已经签署。", ["合同", "采购合同"]);
  assert.deepEqual(segments, [
    { text: "采购合同", term: "采购合同" },
    { text: "已经签署。" },
  ]);
});

test("a term inside a longer term is not marked twice", () => {
  const segments = markTerms("采购合同已经签署。", ["采购合同", "合同"]);
  const marked = segments.filter((s) => s.term);
  assert.equal(marked.length, 1);
  assert.equal(marked[0].term, "采购合同");
});

test("a term that is not in the text is not marked", () => {
  assert.deepEqual(markTerms("这是一段普通的文字。", ["违约金"]), [
    { text: "这是一段普通的文字。" },
  ]);
});

test("the text comes back exactly as it went in", () => {
  const text = "甲方应支付违约金，违约金按合同总额计算。";
  const segments = markTerms(text, ["违约金", "合同总额"]);
  assert.equal(
    segments.map((s) => s.text).join(""),
    text
  );
});

test("the same term is marked every time it stands", () => {
  const segments = markTerms("违约金是违约金。", ["违约金"]);
  assert.equal(segments.filter((s) => s.term).length, 2);
});

test("the plain text around the terms is kept", () => {
  const segments = markTerms("前 违约金 后", ["违约金"]);
  assert.deepEqual(segments, [
    { text: "前 " },
    { text: "违约金", term: "违约金" },
    { text: " 后" },
  ]);
});

test("an empty term is ignored", () => {
  assert.deepEqual(markTerms("一段文字", ["", "  "]), [{ text: "一段文字" }]);
});

test("an empty text is no pieces", () => {
  assert.deepEqual(markTerms("", ["违约金"]), []);
});

test("a term is only marked where it stands, not somewhere else", () => {
  const segments = markTerms("合同签署", ["合同", "合同签署"]);
  assert.deepEqual(segments, [{ text: "合同签署", term: "合同签署" }]);
});

// ===== where the term was found =====

test("the summary says where the term was found", () => {
  assert.equal(termContextSummary(context()), "《采购合同.md》第 12 页");
});

test("a term the knowledge base does not have says so", () => {
  const summary = termContextSummary(
    context({ found: false, contexts: [] })
  );
  assert.ok(summary.includes("违约责任"));
  assert.ok(summary.includes("没有"));
  assert.ok(!summary.includes("《"));
});

test("the summary lists every place the term was found", () => {
  const summary = termContextSummary(
    context({
      contexts: [
        contextItem(),
        contextItem({
          file_name: "补充协议.pdf",
          location: "Slide 3",
          chunk_id: "chunk-2",
        }),
      ],
    })
  );
  assert.ok(summary.includes("《采购合同.md》第 12 页"));
  assert.ok(summary.includes("《补充协议.pdf》Slide 3"));
});

test("the page is used when the backend sent no place", () => {
  const summary = termContextSummary(
    context({ contexts: [contextItem({ location: "", page: 3 })] })
  );
  assert.equal(summary, "《采购合同.md》第 3 页");
});

test("the summary is a place and not an explanation", () => {
  // The summary names where the term was found - it does not say what
  // the term means. An explanation would be text nobody can check
  // (ADR-041), and the passages are the evidence.
  const summary = termContextSummary(context());
  for (const word of ["是指", "意思是", "指的是", "所谓的"]) {
    assert.ok(
      !summary.includes(word),
      `the summary explains the term: ${summary}`
    );
  }
});

// ===== the panel =====

test("the panel shows the passage and the place it came from", () => {
  // The test runner has no component renderer, so the panel is pinned
  // the way this project pins its React parts: by what the component
  // reads out of the answer.
  assert.match(chatSource, /\{item\.text\}/);
  assert.match(chatSource, /\{item\.location\}/);
  assert.match(chatSource, /\{item\.file_name\}/);
});

test("the panel shows where the term was found", () => {
  assert.match(chatSource, /termContextSummary\(termContext\)/);
});
