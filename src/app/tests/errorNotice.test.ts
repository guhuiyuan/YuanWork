/**
 * What the user reads when a panel crashes.
 *
 * A render-time exception used to take the whole window with it. The
 * boundary turns it into a notice - and the notice has to say
 * something useful without dumping internals on the screen.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { errorNotice } from "../src/api/errorNotice.ts";

test("the notice says which part crashed", () => {
  const notice = errorNotice(new Error("boom"), "Wiki 页面");
  assert.ok(notice.title.includes("Wiki 页面"));
  assert.ok(notice.title.includes("出错"));
});

test("the notice says the rest of the app still works", () => {
  const notice = errorNotice(new Error("boom"));
  assert.ok(notice.detail.includes("其余部分还能用"));
});

test("the message of the error is kept", () => {
  const notice = errorNotice(new Error("tags is not a function"));
  assert.ok(notice.detail.includes("tags is not a function"));
});

test("a long message is cut short", () => {
  const notice = errorNotice(new Error("x".repeat(500)));
  assert.ok(notice.detail.length < 200, `too long: ${notice.detail.length}`);
});

test("something that is not an error still gets a notice", () => {
  const notice = errorNotice("出错了");
  assert.ok(notice.title.includes("出错"));
  assert.ok(notice.detail.includes("其余部分还能用"));
});

test("nothing at all still gets a notice", () => {
  const notice = errorNotice(undefined);
  assert.ok(notice.detail.includes("其余部分还能用"));
});
