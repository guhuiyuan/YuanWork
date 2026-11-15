/**
 * The position a citation points at.
 *
 * A citation has to say two things: which file it came from, and
 * where in that file to look. The second one is what makes the
 * citation clickable.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  anchorOf,
  citationLabel,
  describeAnchor,
} from "../src/api/citations.ts";
import type { Citation } from "../src/store.ts";

function citation(overrides: Partial<Citation> = {}): Citation {
  return {
    index: 1,
    file_name: "合同.pdf",
    file_path: "C:\\kb\\合同.pdf",
    text: "违约金按合同总额的三分之一计算。",
    ...overrides,
  };
}

// ===== the anchor =====

test("the anchor of a citation is the one the backend sent", () => {
  const found = anchorOf(
    citation({ anchor: { kind: "page", page: 12 } })
  );
  assert.deepEqual(found, { kind: "page", page: 12 });
});

test("a citation without an anchor falls back to the flat fields", () => {
  assert.deepEqual(anchorOf(citation({ page: 12 })), {
    kind: "page",
    page: 12,
  });
  assert.deepEqual(anchorOf(citation({ slide_number: 3 })), {
    kind: "slide",
    slide: 3,
  });
  assert.deepEqual(anchorOf(citation({ sheet: "进度表", row: 7 })), {
    kind: "sheet_row",
    sheet: "进度表",
    row: 7,
  });
  assert.deepEqual(anchorOf(citation({ section: "第三章 费用" })), {
    kind: "section",
    section: "第三章 费用",
  });
});

test("a citation without a position has no anchor", () => {
  assert.equal(anchorOf(citation()), null);
});

test("a citation without a file path has no anchor", () => {
  assert.equal(anchorOf(citation({ file_path: "" })), null);
});

// ===== the position in words =====

test("the position is said in words", () => {
  assert.equal(describeAnchor({ kind: "page", page: 12 }), "第 12 页");
  assert.equal(describeAnchor({ kind: "slide", slide: 3 }), "Slide 3");
  assert.equal(
    describeAnchor({ kind: "sheet_row", sheet: "进度表", row: 7 }),
    "进度表 第 7 行"
  );
  assert.equal(
    describeAnchor({ kind: "heading_path", heading_path: ["第一章", "1.1 概述"] }),
    "第一章 › 1.1 概述"
  );
});

test("an anchor without its coordinates says nothing", () => {
  assert.equal(describeAnchor({ kind: "page" }), "");
  assert.equal(describeAnchor(null), "");
});

// ===== the label =====

test("the label says the file and where in it", () => {
  assert.equal(
    citationLabel(citation({ page: 12 })),
    "合同.pdf · 第 12 页"
  );
});

test("the label of a citation without a position is just the file", () => {
  assert.equal(citationLabel(citation()), "合同.pdf");
});

test("the label prefers the position the backend worded", () => {
  assert.equal(
    citationLabel(citation({ location: "第 12 页（扫描件）", page: 12 })),
    "合同.pdf · 第 12 页（扫描件）"
  );
});
