/**
 * The places a refusal points at.
 *
 * A refusal says "click the position below and look at the original" -
 * so those positions have to be clickable, and they have to carry the
 * path of the file they point at (TASK-085).
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const read = (...parts: string[]) =>
  readFileSync(join(here, "..", ...parts), "utf-8");

const chatSource = read("src", "components", "ChatWindow.tsx");
const storeSource = read("src", "store.ts");
const backendSource = read("src", "api", "backend.ts");

test("a refusal carries the places it points at", () => {
  assert.match(storeSource, /refusalPositions\?: RefusalPosition\[\]/);
  assert.match(backendSource, /refusal_positions\?: RefusalPosition\[\]/);
  assert.match(
    storeSource,
    /export interface RefusalPosition \{[\s\S]{0,200}file_path: string;/
  );
});

test("the stream keeps the positions of a refusal", () => {
  // The refusal event is where they arrive.
  assert.match(chatSource, /ev\.data\.positions/);
  assert.match(chatSource, /refusalPositions: positions/);
});

test("the positions are drawn as buttons that open the file", () => {
  assert.match(chatSource, /msg\.refusalPositions && msg\.refusalPositions\.length > 0/);
  assert.match(chatSource, /最可能相关的位置（点开看原文）/);
  assert.match(chatSource, /onClick=\{\(\) => void openPositionAt\(p\)\}/);
  // and it opens the file the backend named
  assert.match(chatSource, /file_path: position\.file_path/);
});

test("the plain list is not shown next to the buttons", () => {
  // The refusal text names the positions for every reader; the desktop
  // draws them as buttons, so the list would say it twice.
  assert.match(chatSource, /refusalTextWithoutPositions/);
  assert.match(
    chatSource,
    /hasPositions \? refusalTextWithoutPositions\(msg\.content\) : msg\.content/
  );
});
