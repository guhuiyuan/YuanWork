/**
 * The context of the previous turn - **positions and the reader's own
 * question, never the previous answer** (ADR-057).
 *
 * The previous answer is model-generated text with no source. Carrying
 * it into the prompt would open a way to answer without evidence, so
 * this file also checks that it is nowhere in what goes out.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import {
  buildFollowUp,
  MAX_FOLLOW_UP_POSITIONS,
  MAX_FOLLOW_UP_QUESTION,
} from "../src/api/followUp.ts";
import type { ChatMessage } from "../src/store.ts";

function user(content: string): ChatMessage {
  return {
    id: `u-${content}`,
    role: "user",
    content,
    timestamp: 1,
  };
}

function assistant(content: string, citations: ChatMessage["citations"] = []): ChatMessage {
  return {
    id: `a-${content}`,
    role: "assistant",
    content,
    citations,
    timestamp: 2,
  };
}

function citation(fileName: string, page?: number) {
  return {
    index: 1,
    file_name: fileName,
    page,
    text: "被引用的那一段",
  };
}

test("there is no context when nothing has been answered yet", () => {
  assert.equal(buildFollowUp([]), null);
  assert.equal(buildFollowUp([user("第一个问题")]), null);
  assert.equal(buildFollowUp([assistant("答了，但没有引用")]), null);
});

test("the context is the previous positions and the reader's own question", () => {
  const context = buildFollowUp([
    user("把这份合同的违约金讲清楚"),
    assistant("违约金按合同总额的三分之一计算 [1]。", [
      citation("采购合同.pdf", 3),
    ]),
  ]);

  assert.ok(context, "没有带出上一轮的上下文");
  assert.equal(context!.question, "把这份合同的违约金讲清楚");
  assert.equal(context!.positions.length, 1);
  assert.equal(context!.positions[0].file_name, "采购合同.pdf");
  assert.equal(context!.positions[0].page, 3);
});

test("the previous answer is not in the context", () => {
  const context = buildFollowUp([
    user("把这份合同的违约金讲清楚"),
    assistant("上一轮模型生成的回答原文 [1]。", [citation("采购合同.pdf", 3)]),
  ]);

  assert.ok(context);
  // Only two things go out: the positions and the reader's question.
  assert.deepEqual(Object.keys(context!).sort(), ["positions", "question"]);
  assert.ok(
    !JSON.stringify(context).includes("上一轮模型生成的回答原文"),
    "上一轮的回答跟着去了"
  );
});

test("the last answered turn is the one that counts", () => {
  const context = buildFollowUp([
    user("第一个问题"),
    assistant("第一个回答 [1]。", [citation("旧文件.pdf", 1)]),
    user("第二个问题"),
    assistant("第二个回答 [1]。", [citation("新文件.pdf", 2)]),
  ]);

  assert.ok(context);
  assert.equal(context!.question, "第二个问题");
  assert.equal(context!.positions[0].file_name, "新文件.pdf");
});

test("the positions are capped", () => {
  const citations = Array.from({ length: 30 }, (_, i) => citation(`文件${i}.pdf`, i + 1));
  const context = buildFollowUp([
    user("问题"),
    assistant("回答 [1]。", citations),
  ]);

  assert.ok(context);
  assert.equal(context!.positions.length, MAX_FOLLOW_UP_POSITIONS);
});

test("the question is capped", () => {
  const context = buildFollowUp([
    user("问".repeat(900)),
    assistant("回答 [1]。", [citation("合同.pdf", 1)]),
  ]);

  assert.ok(context);
  assert.equal(context!.question.length, MAX_FOLLOW_UP_QUESTION);
});

test("a citation without a file name is dropped", () => {
  const context = buildFollowUp([
    user("问题"),
    assistant("回答 [1]。", [{ index: 1, file_name: "  ", text: "x" }]),
  ]);

  assert.equal(context, null);
});
