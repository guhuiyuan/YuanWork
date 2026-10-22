/**
 * Unit tests for the first-run decision.
 *
 * The welcome screen has to appear on a fresh install and stay away
 * afterwards - including when the user deliberately emptied the
 * knowledge base.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { shouldShowOnboarding } from "../src/api/firstRun.ts";

test("a fresh install with an empty knowledge base sees the welcome screen", () => {
  assert.equal(shouldShowOnboarding(true, 0, false), true);
});

test("the welcome screen does not flash before the file list arrives", () => {
  // kbLoaded false means "we have not asked the backend yet" - showing
  // the welcome screen then would make it flash on every launch.
  assert.equal(shouldShowOnboarding(false, 0, false), false);
});

test("a knowledge base with files means no welcome screen", () => {
  assert.equal(shouldShowOnboarding(true, 3, false), false);
});

test("an empty knowledge base after setup means no welcome screen", () => {
  // The user removed every file on purpose - do not drag them back.
  assert.equal(shouldShowOnboarding(true, 0, true), false);
});
