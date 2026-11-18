/**
 * What the knowledge base holds, and what it could not read.
 *
 * The file list is paged - a knowledge base can hold thousands of
 * files - so the count comes from the stats, not from the length of a
 * page. The files the build could not read are kept with the reason,
 * and they are still there after a restart (AC-E-004).
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import { useAppStore } from "../src/store.ts";

const here = dirname(fileURLToPath(import.meta.url));
const read = (...parts: string[]) =>
  readFileSync(join(here, "..", ...parts), "utf-8");

const apiSource = read("src", "api", "backend.ts");
const hookSource = read("src", "hooks", "useIngest.ts");
const commandsSource = read("src-tauri", "src", "commands.rs");

// ===== the file list =====

test("the file list is paged", () => {
  assert.match(apiSource, /export const KB_PAGE_SIZE = 100;/);
  assert.match(
    apiSource,
    /\/kb\/files\?limit=\$\{limit\}&offset=\$\{offset\}/
  );
});

test("the count of files comes from the stats", () => {
  // The length of a page is not the number of files.
  assert.match(apiSource, /export async function kbStats/);
  assert.match(apiSource, /\/kb\/stats/);
  assert.match(hookSource, /const stats = await kbStats\(\)/);
});

test("the desktop shell asks for the same pages", () => {
  assert.match(
    commandsSource,
    /api\/v1\/kb\/files\?limit=\{\}&offset=\{\}/
  );
  assert.match(commandsSource, /pub async fn kb_stats/);
});

// ===== the files that could not be read =====

test("the last build can be read", () => {
  // The app reads it when it starts: the files the last build could
  // not read are still there after a restart.
  assert.match(apiSource, /export async function latestIngestStatus/);
  assert.match(apiSource, /ingest\/status\/latest/);
});

test("the failed files are kept in the store", () => {
  const store = useAppStore.getState();
  store.setFailedFiles([{ path: "D:\\kb\\a.docx", error: "解析失败" }], 3);

  const state = useAppStore.getState();
  assert.equal(state.failedFiles[0].path, "D:\\kb\\a.docx");
  assert.equal(state.failedFiles[0].error, "解析失败");
  // the list is capped - the count is the real one
  assert.equal(state.failedTotal, 3);

  store.setFailedFiles([]);
  assert.equal(useAppStore.getState().failedTotal, 0);
});

test("the build hands the failures to the store", () => {
  assert.match(hookSource, /setFailedFiles\(\s*failed,\s*status\.failed_total/);
});

test("a format nobody supports is counted apart", () => {
  // A workspace with a code project in it has a hundred thousand of
  // them - that is not the same problem as "could not be read".
  assert.match(hookSource, /status\.unsupported_total/);
});
