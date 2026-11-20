/**
 * Unit tests for the chat text composition helpers.
 *
 * Run with `npm test` (Node's built-in test runner, no dependencies).
 *
 * These rules are invisible in the code but very visible to the user:
 * getting the order wrong shows a duplicated or reordered answer.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  appendFailureNotice,
  failureNotice,
  isCancellation,
  isGenerationError,
  refusalBanner,
  refusalContent,
  refusalTextWithoutPositions,
} from "../src/api/streamText.ts";

test("the failure notice goes after the streamed text", () => {
  const out = appendFailureNotice("已经生成的内容", "（请求失败：boom）");
  assert.equal(out, "已经生成的内容\n\n（请求失败：boom）");
  assert.ok(out.indexOf("已经生成的内容") < out.indexOf("（请求失败"));
});

test("the failure notice stands alone when nothing was streamed", () => {
  assert.equal(appendFailureNotice("", "（请求失败：boom）"), "（请求失败：boom）");
});

test("the refusal content replaces the streamed text", () => {
  // The backend's refusal message already contains the partial answer
  // plus the error notice, so it must not be appended a second time.
  const message = "半句答案\n\n抱歉，生成回答时出现错误。";
  assert.equal(refusalContent(message), message);
});

// ===== the cancellation =====

test("a cancellation is told apart from a failure", () => {
  // The user asked to stop - that is not a failure and must not be
  // dressed up as one.
  assert.equal(isCancellation("cancelled by the user"), true);
  assert.equal(isCancellation("AbortError: The user aborted a request."), true);
  assert.equal(isCancellation("已停止生成"), true);
});

test("a real failure is not a cancellation", () => {
  assert.equal(isCancellation("HTTP 500: boom"), false);
  assert.equal(isCancellation("stream ended without a final response"), false);
  assert.equal(isCancellation(""), false);
});

test("a generation error is told apart from an evidence refusal", () => {
  assert.equal(isGenerationError("generation_error: boom"), true);
  assert.equal(isGenerationError("model_unavailable"), true);
  assert.equal(isGenerationError("insufficient_evidence"), false);
  assert.equal(isGenerationError("unlocatable_evidence"), false);
  assert.equal(isGenerationError("model_insufficient"), false);
  assert.equal(isGenerationError(undefined), false);
  assert.equal(isGenerationError(""), false);
});

test("the banner says the model failed, not that the evidence was thin", () => {
  const banner = refusalBanner("generation_error: boom");
  assert.match(banner.title, /生成回答时出错/);
  assert.doesNotMatch(banner.title, /证据不足/);
});

test("the banner says the evidence was thin for a plain refusal", () => {
  const banner = refusalBanner("insufficient_evidence");
  assert.match(banner.title, /证据不足/);
  assert.doesNotMatch(banner.title, /生成回答时出错/);
});

test("a refusal without a reason falls back to the plain wording", () => {
  const banner = refusalBanner(undefined);
  assert.match(banner.title, /证据不足/);
  assert.ok(banner.detail.length > 0);
});

test("a citation that does not check out gets its own wording", () => {
  const banner = refusalBanner("citation_invalid");
  assert.match(banner.title, /引用校验/);
  assert.doesNotMatch(banner.title, /生成回答时出错/);
});

test("a known internal error is shown in Chinese", () => {
  assert.match(
    failureNotice("stream ended without a final response"),
    /回答流意外中断/
  );
  assert.match(failureNotice("Failed to fetch"), /无法连接后端服务/);
  assert.match(
    failureNotice(
      "error sending request for url (http://127.0.0.1:8765/api/v1/chat/stream): connection refused"
    ),
    /无法连接后端服务/
  );
});

test("an HTTP status is kept as-is", () => {
  assert.match(failureNotice("HTTP 500: boom"), /HTTP 500/);
});

test("an unknown reason is kept but truncated", () => {
  const long = "x".repeat(400);
  const out = failureNotice(long);
  assert.ok(out.length < 200, `notice is too long: ${out.length} chars`);
  assert.match(out, /…/);
});

test("an empty reason still produces a notice", () => {
  assert.match(failureNotice(""), /请求失败/);
});

// ===== the list of positions in a refusal =====

const REFUSAL = `我在你的知识库里没有找到足够明确的证据来回答这个问题。

## 最可能相关的位置

- 《合同.pdf》（第 12 页）
- 《说明.docx》

## 你可以：

1. **换用更具体的关键词** —— 例如合同编号、金额、日期、条款名`;

test("the list of positions is taken out of the refusal text", () => {
  // The desktop draws those positions as buttons; the plain list would
  // say the same thing twice.
  const out = refusalTextWithoutPositions(REFUSAL);
  assert.doesNotMatch(out, /最可能相关的位置/);
  assert.doesNotMatch(out, /合同\.pdf/);
  // but the rest of the refusal is still there
  assert.match(out, /没有找到足够明确的证据/);
  assert.match(out, /你可以/);
  assert.match(out, /换用更具体的关键词/);
});

test("a refusal without the section is left alone", () => {
  // A text that does not look the way it is expected to look keeps
  // all of itself.
  const text = "我在你的知识库里没有找到足够明确的证据来回答这个问题。";
  assert.equal(refusalTextWithoutPositions(text), text);
});

test("a refusal whose advice is missing keeps everything", () => {
  // Losing the advice at the end of a refusal is worse than showing a
  // list twice.
  const text = "开头\n\n## 最可能相关的位置\n\n- 《合同.pdf》";
  assert.equal(refusalTextWithoutPositions(text), text);
});

// ===== the streaming contract in the component and the store =====

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const chatSource = readFileSync(join(here, "..", "src", "components", "ChatWindow.tsx"), "utf-8");
const storeSource = readFileSync(join(here, "..", "src", "store.ts"), "utf-8");

test("the token event is the only source of incremental text", () => {
  // The `answer` event carries the whole answer, so using it as text
  // would duplicate what the tokens already rendered.
  assert.match(chatSource, /case "token"/);
  assert.match(chatSource, /case "answer"/);
  // the answer event is only kept as a fallback
  assert.match(chatSource, /answerTextRef/);
});

test("the text is appended to the message it belongs to", () => {
  // The old `appendToLast` wrote to whatever the last message happened
  // to be, which corrupts the transcript when a message is inserted.
  assert.match(storeSource, /appendToMessage/);
  assert.doesNotMatch(storeSource, /appendToLast/);
});

test("the flush is throttled by a frame, with a timer as the backstop", () => {
  // requestAnimationFrame does not fire in a hidden tab, so the whole
  // answer would sit in the buffer.
  assert.match(chatSource, /requestAnimationFrame/);
  assert.match(chatSource, /setTimeout/);
});

test("a refusal replaces the streamed text", () => {
  // The backend's refusal message already contains what was streamed;
  // appending would duplicate the tail.
  assert.match(chatSource, /refusalContent/);
});

test("a failure notice is appended after the text", () => {
  assert.match(chatSource, /appendFailureNotice/);
});

test("a failure notice is not added twice", () => {
  assert.match(chatSource, /errorNoticedRef/);
});
