/**
 * The answer, rendered.
 *
 * The answer is markdown, and the reader has to see it as such: the
 * marks are not part of what was written. Two things must survive the
 * rendering - the text itself, and the terms that open the original.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { createElement, isValidElement, type ReactNode } from "react";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import {
  markTermsInNodes,
  plainPreview,
  textOf,
  TERM_CLASS,
} from "../src/api/markdown.ts";

const here = dirname(fileURLToPath(import.meta.url));
const read = (...parts: string[]) =>
  readFileSync(join(here, "..", ...parts), "utf-8");

// ===== the terms stay clickable =====

test("a term in the text becomes a button", () => {
  const nodes = markTermsInNodes("违约金按合同计算", ["违约金"], () => {});

  assert.ok(Array.isArray(nodes));
  const buttons = (nodes as ReactNode[]).filter(
    (n) => isValidElement(n) && (n.props as { className?: string }).className === TERM_CLASS
  );
  assert.equal(buttons.length, 1);
  assert.equal(
    textOf((buttons[0] as { props: { children?: ReactNode } }).props.children),
    "违约金"
  );
});

test("the text that goes in is the text that comes out", () => {
  const source = "违约金按合同总额的三分之一计算，方另有约定的除外";
  const nodes = markTermsInNodes(source, ["违约金", "合同"], () => {});

  assert.equal(textOf(nodes as ReactNode), source);
});

test("a term inside a bold run is marked too", () => {
  // The markdown is parsed first, so the text sits inside an element.
  const tree = createElement("strong", null, "违约金按合同计算");

  const nodes = markTermsInNodes(tree, ["违约金"], () => {}) as ReactNode[];

  const inner = (nodes[0] as { props: { children?: ReactNode } }).props.children;
  const buttons = (inner as ReactNode[]).filter(
    (n) => isValidElement(n) && (n.props as { className?: string }).className === TERM_CLASS
  );
  assert.equal(buttons.length, 1);
});

test("clicking a term hands the term over", () => {
  const clicked: string[] = [];
  const nodes = markTermsInNodes("违约金", ["违约金"], (t) => clicked.push(t)) as ReactNode[];

  const button = nodes.find(
    (n) => isValidElement(n) && (n.props as { className?: string }).className === TERM_CLASS
  ) as { props: { onClick: () => void } };
  button.props.onClick();

  assert.deepEqual(clicked, ["违约金"]);
});

test("an answer without terms is left alone", () => {
  const nodes = markTermsInNodes("没有术语的回答", [], () => {});
  assert.equal(textOf(nodes as ReactNode), "没有术语的回答");
});

// ===== the text of a rendered piece =====

test("the text of a tree is its words", () => {
  const tree = createElement("p", null, "合同", createElement("strong", null, "违约金"));
  assert.equal(textOf(tree), "合同违约金");
});

// ===== the preview in the trace panel =====

test("the preview has no marks in it", () => {
  // The trace panel shows the exchange as the reader sees it - and the
  // reader does not see "##" or "**".
  assert.equal(
    plainPreview("## 你可以：\n\n1. **补上另一份文档** —— 缺哪份就补哪份"),
    "你可以：\n1. 补上另一份文档 —— 缺哪份就补哪份"
  );
});

test("a single star is a name, not a mark", () => {
  // "A*算法" is a name. Taking the star off would change the word.
  assert.match(plainPreview("A*算法与A**的差别"), /A\*算法/);
});

test("the preview is short", () => {
  assert.equal(plainPreview("x".repeat(500)).length, 100);
});

// ===== the component and the window =====

const chatSource = read("src", "components", "ChatWindow.tsx");
const answerSource = read("src", "components", "AnswerMarkdown.tsx");

test("the answer is rendered as markdown", () => {
  assert.match(answerSource, /ReactMarkdown/);
  assert.match(chatSource, /<AnswerMarkdown/);
  assert.match(chatSource, /content=\{shownContent\}/);
});

test("the marks themselves are not shown as text", () => {
  // The plain text rendering showed "## 你可以：" and "**补上另一份文档**".
  // The markdown rendering has to take over the whole content.
  assert.doesNotMatch(chatSource, /segments\.map/);
  assert.doesNotMatch(chatSource, /markTerms\(/);
});

test("a link never takes the application away", () => {
  // An answer can point at a web page. Clicking it must not navigate
  // the window the work is in - so the address is shown, not followed.
  assert.match(answerSource, /a: \(\{ href, children \}\)/);
  assert.doesNotMatch(answerSource, /<a\s/);
  assert.match(answerSource, /md__link/);
});

test("an image is not fetched", () => {
  assert.match(answerSource, /img: \(\{ alt \}\)/);
  assert.doesNotMatch(answerSource, /<img\s/);
});

test("the tables of an answer are drawn as tables", () => {
  // The wiki's comparison tables are markdown tables too.
  assert.match(answerSource, /remarkGfm/);
});
