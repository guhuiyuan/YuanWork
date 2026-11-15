/**
 * Unit tests for the first-run decision.
 *
 * The welcome screen has to appear on a fresh install and stay away
 * afterwards - including when the user deliberately emptied the
 * workspace.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { hasOnboarded, markOnboarded, shouldShowOnboarding } from "../src/api/firstRun.ts";

test("a fresh install with an empty workspace sees the welcome screen", () => {
  assert.equal(shouldShowOnboarding(true, 0, false), true);
});

test("the welcome screen does not flash before the workspace arrives", () => {
  // loaded false means "we have not asked the backend yet" - showing
  // the welcome screen then would make it flash on every launch.
  assert.equal(shouldShowOnboarding(false, 0, false), false);
});

test("a workspace with entries means no welcome screen", () => {
  assert.equal(shouldShowOnboarding(true, 3, false), false);
});

test("an empty workspace after setup means no welcome screen", () => {
  // The user removed every entry on purpose - do not drag them back.
  assert.equal(shouldShowOnboarding(true, 0, true), false);
});

test("a failed read is not an empty workspace", () => {
  // The backend is down. Showing the welcome screen would claim the
  // workspace is empty when it is merely unreachable.
  assert.equal(shouldShowOnboarding(true, 0, false, "HTTP 500"), false);
});

// ===== the persistence =====

/** A localStorage stand-in that can be made to fail. */
function fakeStorage(opts: { fail?: boolean } = {}) {
  const items = new Map<string, string>();
  return {
    getItem: (k: string) => (opts.fail ? null : items.get(k) ?? null),
    setItem: (k: string, v: string) => {
      if (opts.fail) throw new Error("storage disabled");
      items.set(k, v);
    },
    items,
  };
}

function withWindow(storage: unknown, run: () => void) {
  const previous = (globalThis as Record<string, unknown>).window;
  (globalThis as Record<string, unknown>).window = { localStorage: storage };
  try {
    run();
  } finally {
    (globalThis as Record<string, unknown>).window = previous;
  }
}

test("the onboarded flag is stored under a fixed key", () => {
  const storage = fakeStorage();
  withWindow(storage, () => {
    markOnboarded();
    assert.equal(storage.items.get("yuanwork.onboarded"), "1");
  });
});

test("the flag survives a restart", () => {
  const storage = fakeStorage();
  withWindow(storage, () => {
    markOnboarded();
  });
  withWindow(storage, () => {
    assert.equal(hasOnboarded(), true);
  });
});

test("a fresh install has no flag", () => {
  const storage = fakeStorage();
  withWindow(storage, () => {
    assert.equal(hasOnboarded(), false);
  });
});

test("a storage that throws is treated as not onboarded", () => {
  const storage = fakeStorage({ fail: true });
  withWindow(storage, () => {
    assert.equal(hasOnboarded(), false);
    // and marking it does not throw either
    markOnboarded();
  });
});

test("a missing window is treated as not onboarded", () => {
  const previous = (globalThis as Record<string, unknown>).window;
  delete (globalThis as Record<string, unknown>).window;
  try {
    assert.equal(hasOnboarded(), false);
    markOnboarded();
  } finally {
    (globalThis as Record<string, unknown>).window = previous;
  }
});

test("a load error suppresses the welcome screen", () => {
  // A failed read is not an empty workspace — the welcome screen
  // would be a lie.
  assert.equal(shouldShowOnboarding(true, 0, false, "读取失败"), false);
});
