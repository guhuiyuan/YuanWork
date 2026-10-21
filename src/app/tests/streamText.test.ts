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
  isGenerationError,
  refusalBanner,
  refusalContent,
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

test("a generation error is told apart from a low-confidence refusal", () => {
  assert.equal(isGenerationError("generation_error: boom"), true);
  assert.equal(isGenerationError("low_confidence"), false);
  assert.equal(isGenerationError(undefined), false);
  assert.equal(isGenerationError(""), false);
});

test("the banner says the model failed, not that confidence was low", () => {
  const banner = refusalBanner("generation_error: boom");
  assert.match(banner.title, /生成回答时出错/);
  assert.doesNotMatch(banner.title, /置信度/);
});

test("the banner says confidence was too low for a plain refusal", () => {
  const banner = refusalBanner("low_confidence");
  assert.match(banner.title, /置信度不足/);
  assert.doesNotMatch(banner.title, /生成回答时出错/);
});

test("a refusal without a reason falls back to the low-confidence wording", () => {
  const banner = refusalBanner(undefined);
  assert.match(banner.title, /置信度不足/);
  assert.ok(banner.detail.length > 0);
});
