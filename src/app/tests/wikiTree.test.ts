/**
 * The wiki page tree: every page has to be reachable.
 *
 * The tree draws the pages in groups by category. It used to draw only
 * the categories on a hardcoded list — and the comparison tables (a
 * third kind of page, AC-F-012) were on nobody's list, so nine pages
 * of a real workspace were simply not there: the sidebar said
 * 「Wiki 28」 and the tree showed 19.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const read = (...parts: string[]) =>
  readFileSync(join(here, "..", ...parts), "utf-8");

const treeSource = read("src", "components", "Wiki", "WikiTreePanel.tsx");
const pageSource = read("src", "components", "Wiki", "WikiPageView.tsx");

test("a category nobody listed is still drawn", () => {
  // The order is the known categories first; the rest follow.
  assert.match(treeSource, /CATEGORY_ORDER\.filter\(\(c\) => grouped\[c\]\?\.length\)/);
  assert.match(treeSource, /!CATEGORY_ORDER\.includes\(c\)/);
  assert.match(treeSource, /categories\.map\(\(cat\) =>/);
});

test("the comparison tables have a name and an icon", () => {
  assert.match(treeSource, /comparison: "🔀"/);
  assert.match(treeSource, /comparison: "对比表"/);
});

test("the page itself says the category in words", () => {
  // The backend speaks in keys; the reader thinks in 对比表.
  assert.match(pageSource, /labelForCategory\(detail\.category\)/);
  assert.match(pageSource, /import \{ labelForCategory \} from "\.\/WikiTreePanel"/);
});

test("the label function falls back to the key", () => {
  // A category the app has no name for keeps its key — it is shown.
  assert.match(treeSource, /\?\? c;/);
});

test("the tree still filters by title, category and tag", () => {
  assert.match(treeSource, /p\.title\.toLowerCase\(\)\.includes\(q\)/);
  assert.match(treeSource, /p\.category\.toLowerCase\(\)\.includes\(q\)/);
  assert.match(treeSource, /\(p\.tags \?\? \[\]\)\.some/);
});
