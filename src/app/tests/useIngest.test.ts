/**
 * The ingest state machine and the polling budget.
 *
 * The ingest state lives in the store rather than in the hook, so the
 * sidebar and the welcome screen share one view of it — two concurrent
 * jobs on the same knowledge base would fight each other.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import { useAppStore } from "../src/store.ts";

const here = dirname(fileURLToPath(import.meta.url));
const hookSource = readFileSync(join(here, "..", "src", "hooks", "useIngest.ts"), "utf-8");

test("the polling budget is the documented one", () => {
  assert.match(hookSource, /const POLL_INTERVAL_MS = 700;/);
  assert.match(hookSource, /const MAX_WAIT_MS = 30 \* 60 \* 1000;/);
  assert.match(hookSource, /const STATUS_TIMEOUT_MS = 20_000;/);
  assert.match(hookSource, /const MAX_STUCK_POLLS = 15;/);
});

test("the ingest state starts out idle", () => {
  const state = useAppStore.getState();
  assert.equal(state.ingesting, false);
  assert.equal(state.ingestMessage, null);
  assert.equal(state.ingestFolder, null);
});

test("the ingest state can be set and cleared", () => {
  const store = useAppStore.getState();
  store.setIngestState({ ingesting: true, message: "正在索引…", folder: "/kb" });
  let state = useAppStore.getState();
  assert.equal(state.ingesting, true);
  assert.equal(state.ingestMessage, "正在索引…");
  assert.equal(state.ingestFolder, "/kb");

  state.setIngestState({ ingesting: false, message: null });
  state = useAppStore.getState();
  assert.equal(state.ingesting, false);
  assert.equal(state.ingestMessage, null);
  // the folder is kept — it is not cleared by an update that omits it
  assert.equal(state.ingestFolder, "/kb");
});

test("cancelling bumps the token", () => {
  const store = useAppStore.getState();
  const before = store.ingestToken;
  store.cancelIngest();
  assert.equal(useAppStore.getState().ingestToken, before + 1);
});

test("the token is what the poll loop watches", () => {
  // The loop compares the token it started with against the current one,
  // so a cancel from another component stops it.
  assert.match(hookSource, /ingestToken !== token/);
});

test("the ingest state is shared through the store", () => {
  // The hook reads the state from the store rather than owning it.
  assert.match(hookSource, /useAppStore\.getState\(\)\.ingestToken/);
  assert.match(hookSource, /useAppStore\(\(s\) => s\.ingesting\)/);
});

test("a completed index marks the setup as done", () => {
  assert.match(hookSource, /status\.status === "completed"/);
  assert.match(hookSource, /markOnboarded\(\)/);
});

test("a failed index does not mark the setup as done", () => {
  // The `markOnboarded()` call must be inside the completed branch.
  const completedAt = hookSource.indexOf('status.status === "completed"');
  const markedAt = hookSource.indexOf("markOnboarded()");
  const failedAt = hookSource.indexOf('status.status === "failed"');
  assert.ok(completedAt > 0 && markedAt > completedAt);
  assert.ok(failedAt > markedAt, "the failed branch must come after the marking");
});

test("a failed index stops the polling", () => {
  assert.match(hookSource, /status\.status === "failed"[\s\S]{0,200}return false;/);
});

test("a stuck request is only fatal after fifteen of them", () => {
  assert.match(hookSource, /stuck \+= 1;/);
  assert.match(hookSource, /if \(stuck >= MAX_STUCK_POLLS\)/);
});

test("the progress message shows the two counters", () => {
  assert.match(hookSource, /正在索引…（\$\{done\}\/\$\{count\}）/);
});

test("the folder picker failure is caught", () => {
  // A throwing native dialog must not escape as an unhandled rejection.
  assert.match(hookSource, /catch \(e\) \{[\s\S]{0,300}showToast\(`索引失败/);
});

// ===== the workspace flow =====

test("a picked folder is added to the workspace, not indexed on its own", () => {
  // The workspace is the scope of the app: the folder joins it, and
  // the build that follows runs over the whole workspace.
  assert.match(hookSource, /await addWorkspaceFolder\(picked\)/);
  assert.match(hookSource, /await rebuildWorkspace\(\)/);
});

test("a single file can be added by hand", () => {
  assert.match(hookSource, /await addWorkspaceFile\(picked\)/);
});

test("the build is polled with the job the rebuild returned", () => {
  // The same status endpoint as the folder build - the frontend needs
  // no second kind of polling.
  assert.match(hookSource, /getIngestStatus\(job\.job_id\)/);
});

test("the entries are read again once the build is done", () => {
  const completed = hookSource.indexOf('status.status === "completed"');
  const reread = hookSource.indexOf("refreshWorkspaceQuietly()", completed);
  assert.ok(completed > 0, "the completed branch exists");
  assert.ok(reread > completed, "the workspace is read again after the build");
});

test("the cancel still reaches the workspace build", () => {
  // The poll loop watches the shared token, so a cancel from another
  // component stops the workspace build too.
  assert.match(hookSource, /ingestToken !== token/);
});
