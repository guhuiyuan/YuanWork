/**
 * The workspace: the folders and the single files the app works on.
 *
 * The workspace is the scope of the whole app - the build runs over
 * its entries, and the sidebar lists them. Beside them it lists what
 * the build made of them: the files that are in the knowledge base,
 * and the files it could not read (AC-E-004). These tests pin the
 * three things that have to hold: the API calls go to the right
 * endpoints, the store keeps the entries, and the screens say the
 * truth about what removing an entry does.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import { useAppStore, type WorkspaceEntry } from "../src/store.ts";
import {
  addWorkspaceFile,
  addWorkspaceFolder,
  listWorkspaceEntries,
  rebuildWorkspace,
  removeWorkspaceEntry,
} from "../src/api/backend.ts";
import { entryName } from "../src/hooks/useIngest.ts";

const here = dirname(fileURLToPath(import.meta.url));
const read = (...parts: string[]) =>
  readFileSync(join(here, "..", ...parts), "utf-8");

const sidebarSource = read("src", "components", "Sidebar.tsx");
const onboardingSource = read("src", "components", "Onboarding.tsx");
const hookSource = read("src", "hooks", "useIngest.ts");
const appSource = read("src", "App.tsx");

// ===== the store =====

test("the workspace starts out empty", () => {
  assert.deepEqual(useAppStore.getState().workspaceEntries, []);
  assert.equal(useAppStore.getState().workspaceLoaded, false);
});

test("the workspace entries can be set", () => {
  const entry: WorkspaceEntry = {
    id: "e1",
    path: "D:\\work\\notes",
    kind: "folder",
    included: true,
    added_at: "2026-11-15T10:00:00",
    doc_id: null,
  };
  useAppStore.getState().setWorkspaceEntries([entry]);
  assert.deepEqual(useAppStore.getState().workspaceEntries, [entry]);

  useAppStore.getState().setWorkspaceEntries([]);
  assert.deepEqual(useAppStore.getState().workspaceEntries, []);
});

// ===== the API =====

interface FetchCall {
  url: string;
  method: string;
  body: unknown;
}

/**
 * A fetch that records the calls and answers with `body`.
 *
 * The API layer picks Tauri or plain HTTP - in the test runner there
 * is no Tauri, so the HTTP path is the one that runs.
 */
function withFetch(
  body: unknown,
  status = 200
): { calls: FetchCall[]; restore: () => void } {
  const calls: FetchCall[] = [];
  const previous = globalThis.fetch;
  globalThis.fetch = (async (input: unknown, init?: RequestInit) => {
    calls.push({
      url: String(input),
      method: init?.method ?? "GET",
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    });
    return {
      ok: status >= 200 && status < 300,
      status,
      text: async () => JSON.stringify(body),
      json: async () => body,
    };
  }) as typeof fetch;
  return {
    calls,
    restore: () => {
      globalThis.fetch = previous;
    },
  };
}

test("the workspace is read from /workspace/entries", async () => {
  const entries = [
    {
      id: "e1",
      path: "D:\\work",
      kind: "folder",
      included: true,
      added_at: "2026-11-15T10:00:00",
    },
  ];
  const stub = withFetch({ entries, total: 1 });
  try {
    const fetched = await listWorkspaceEntries();
    assert.deepEqual(fetched, entries);
    assert.equal(stub.calls.length, 1);
    assert.match(stub.calls[0].url, /\/api\/v1\/workspace\/entries$/);
    assert.equal(stub.calls[0].method, "GET");
  } finally {
    stub.restore();
  }
});

test("a folder is added to the workspace", async () => {
  const entry = {
    id: "e1",
    path: "D:\\work",
    kind: "folder",
    included: true,
    added_at: "2026-11-15T10:00:00",
  };
  const stub = withFetch(entry);
  try {
    const added = await addWorkspaceFolder("D:\\work");
    assert.equal(added.id, "e1");
    assert.match(stub.calls[0].url, /\/api\/v1\/workspace\/folders$/);
    assert.equal(stub.calls[0].method, "POST");
    assert.deepEqual(stub.calls[0].body, { path: "D:\\work", included: true });
  } finally {
    stub.restore();
  }
});

test("a single file is added to the workspace", async () => {
  const entry = {
    id: "e2",
    path: "D:\\work\\a.md",
    kind: "file",
    included: true,
    added_at: "2026-11-15T10:00:00",
  };
  const stub = withFetch(entry);
  try {
    const added = await addWorkspaceFile("D:\\work\\a.md");
    assert.equal(added.kind, "file");
    assert.match(stub.calls[0].url, /\/api\/v1\/workspace\/files$/);
    assert.equal(stub.calls[0].method, "POST");
    assert.deepEqual(stub.calls[0].body, {
      path: "D:\\work\\a.md",
      included: true,
    });
  } finally {
    stub.restore();
  }
});

test("an entry is taken out of the workspace", async () => {
  const stub = withFetch({ removed: "e1" });
  try {
    await removeWorkspaceEntry("e1");
    assert.match(stub.calls[0].url, /\/api\/v1\/workspace\/entries\/e1$/);
    assert.equal(stub.calls[0].method, "DELETE");
  } finally {
    stub.restore();
  }
});

test("the build runs over the workspace", async () => {
  const job = {
    job_id: "job-1",
    files_queued: 12,
    estimated_seconds: 3.5,
  };
  const stub = withFetch(job);
  try {
    const started = await rebuildWorkspace();
    assert.equal(started.job_id, "job-1");
    assert.equal(started.files_queued, 12);
    assert.match(stub.calls[0].url, /\/api\/v1\/ingest\/rebuild$/);
    assert.equal(stub.calls[0].method, "POST");
  } finally {
    stub.restore();
  }
});

test("a failed read is an error, not an empty workspace", async () => {
  const stub = withFetch({ detail: "boom" }, 500);
  try {
    await assert.rejects(() => listWorkspaceEntries(), /HTTP 500/);
  } finally {
    stub.restore();
  }
});

// ===== the entries =====

test("an entry is named after the last piece of its path", () => {
  assert.equal(
    entryName({
      id: "e1",
      path: "D:\\work\\notes",
      kind: "folder",
      included: true,
      added_at: "2026-11-15T10:00:00",
    }),
    "notes"
  );
  assert.equal(
    entryName({
      id: "e2",
      path: "D:/work/a.md",
      kind: "file",
      included: true,
      added_at: "2026-11-15T10:00:00",
    }),
    "a.md"
  );
});

// ===== the sidebar =====

test("the sidebar lists the workspace entries", () => {
  assert.match(sidebarSource, /workspaceEntries\.map\(/);
  assert.match(sidebarSource, /useAppStore\(\(s\) => s\.workspaceEntries\)/);
});

test("the sidebar lists the indexed files as well as the workspace", () => {
  // The user asked for a folder and saw nothing - "which files are in
  // the knowledge base" is a question the app has to answer. The
  // workspace says what the user set up; the file list says what the
  // build made of it.
  assert.match(sidebarSource, /workspaceEntries\.map\(/);
  assert.match(sidebarSource, /kbFiles\.map\(/);
  assert.match(sidebarSource, /KbFileRow/);
});

test("the file count is the real one, not the length of a page", () => {
  // The list is paged - the array length is the page, not the count.
  assert.match(sidebarSource, /已索引 \{kbTotal\} 个文件/);
  assert.doesNotMatch(sidebarSource, /已索引 \{kbFiles\.length\} 个文件/);
});

test("the files that could not be read are in the sidebar", () => {
  // AC-E-004: the file is listed and the reason is given. A count
  // alone says that something went wrong, not what.
  assert.match(sidebarSource, /读不进来的文件/);
  assert.match(sidebarSource, /failedTotal/);
  assert.match(sidebarSource, /file\.error/);
});

test("a row says what the entry is", () => {
  assert.match(sidebarSource, /entry\.kind === "folder" \? "📁" : "📄"/);
  assert.match(sidebarSource, /entry\.kind === "folder" \? "文件夹" : "文件"/);
});

test("removing an entry says what it does and does not do", () => {
  // It clears what the entry brought into the index, and it does not
  // touch the file on disk.
  assert.match(sidebarSource, /它带入索引的内容会被清除，文件仍在磁盘上/);
  assert.doesNotMatch(sidebarSource, /文件已删除|已从磁盘删除/);
});

test("the sidebar offers a folder, a single file and a rebuild", () => {
  assert.match(sidebarSource, /onClick=\{pickAndIngest\}/);
  assert.match(sidebarSource, /onClick=\{addFile\}/);
  assert.match(sidebarSource, /onClick=\{rebuild\}/);
});

test("the sidebar can cancel a build", () => {
  // A build can run for half an hour, and by then the welcome screen is
  // gone (the workspace already has an entry) - so the sidebar is the
  // only place left with a way out.
  assert.match(sidebarSource, /onClick=\{cancel\}/);
  assert.match(sidebarSource, /ingest-cancel/);
});

// ===== the first-run screen =====

test("the first-run screen needs an empty workspace", () => {
  assert.match(onboardingSource, /workspaceEntries\.length > 0/);
  assert.match(onboardingSource, /workspaceLoaded/);
  assert.match(onboardingSource, /workspaceError/);
});

test("the first-run screen needs a user who never onboarded", () => {
  assert.match(onboardingSource, /hasOnboarded\(\)/);
});

test("the first-run screen still offers the working directory", () => {
  assert.match(onboardingSource, /选择工作目录/);
  assert.match(onboardingSource, /pickAndIngest\(\)/);
});

test("the app decides the welcome screen on the workspace", () => {
  // AC-F-001 is about the workspace: "the user has not set up a
  // workspace". The indexed files are what the build made of it - an
  // empty knowledge base does not mean the setup is missing.
  assert.match(appSource, /shouldShowOnboarding\(\s*workspaceLoaded/);
  assert.match(appSource, /workspaceEntries\.length/);
  assert.doesNotMatch(appSource, /shouldShowOnboarding\(\s*kbLoaded/);
  assert.doesNotMatch(appSource, /kbFiles\.length/);
});

// ===== the flow =====

test("a picked folder joins the workspace and the build follows", () => {
  // The folder is added to the workspace - the build then runs over
  // the workspace, not over the folder alone.
  const added = hookSource.indexOf("addWorkspaceFolder(picked)");
  const rebuilt = hookSource.indexOf("rebuildWorkspace()");
  assert.ok(added > 0, "the folder is added to the workspace");
  assert.ok(rebuilt > added, "the build follows the addition");
});

test("a single file can be added by hand", () => {
  assert.match(hookSource, /addWorkspaceFile\(picked\)/);
});

test("the build is polled with the existing status endpoint", () => {
  assert.match(hookSource, /getIngestStatus\(job\.job_id\)/);
});

test("the workspace is read again when the job is done", () => {
  const completed = hookSource.indexOf('status.status === "completed"');
  const reread = hookSource.indexOf("refreshWorkspaceQuietly()", completed);
  assert.ok(completed > 0);
  assert.ok(reread > completed, "the entries are read again after the build");
});

test("taking an entry out says what it does", () => {
  assert.match(hookSource, /它带入索引的内容已清除，文件仍在磁盘上/);
  assert.doesNotMatch(hookSource, /文件已删除|已从磁盘删除/);
});
