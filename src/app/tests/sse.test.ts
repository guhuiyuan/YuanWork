/**
 * Unit tests for the browser-side SSE parser.
 *
 * Run with `npm test` (Node's built-in test runner, no dependencies).
 *
 * The backend uses sse-starlette, which terminates every frame with CRLF
 * (`\r\n\r\n`), so the CRLF case is the one that matters in production;
 * bare LF is accepted as well.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { parseSseFrame, parseSseFrames } from "../src/api/sse.ts";

test("parses a CRLF frame (sse-starlette format)", () => {
  const { frames, rest } = parseSseFrames(
    "event: token\r\ndata: {\"text\":\"hi\"}\r\n\r\n"
  );
  assert.equal(frames.length, 1);
  assert.equal(frames[0].event, "token");
  assert.equal(frames[0].data, '{"text":"hi"}');
  assert.equal(rest, "");
});

test("parses a bare LF frame", () => {
  const { frames, rest } = parseSseFrames('event: ping\ndata: {}\n\n');
  assert.equal(frames.length, 1);
  assert.equal(frames[0].event, "ping");
  assert.equal(frames[0].data, "{}");
  assert.equal(rest, "");
});

test("splits several frames in one buffer", () => {
  const { frames, rest } = parseSseFrames(
    "event: token\r\ndata: a\r\n\r\nevent: token\r\ndata: b\r\n\r\nevent: done\r\ndata: c\r\n\r\n"
  );
  assert.deepEqual(
    frames.map((f) => f.data),
    ["a", "b", "c"]
  );
  assert.equal(rest, "");
});

test("keeps a partial frame as rest", () => {
  const { frames, rest } = parseSseFrames("event: token\r\ndata: hel");
  assert.equal(frames.length, 0);
  assert.equal(rest, "event: token\r\ndata: hel");
});

test("joins multiple data lines with a newline", () => {
  const frame = parseSseFrame("event: token\r\ndata: line1\r\ndata: line2\r\n\r\n");
  assert.ok(frame);
  assert.equal(frame.data, "line1\nline2");
});

test("strips exactly one space after the colon", () => {
  const frame = parseSseFrame("event: token\r\ndata:  two spaces\r\n\r\n");
  assert.ok(frame);
  assert.equal(frame.data, " two spaces");
});

test("ignores comment keepalive frames", () => {
  const { frames } = parseSseFrames(": keep-alive\r\n\r\nevent: ping\r\ndata: {}\r\n\r\n");
  assert.equal(frames.length, 1);
  assert.equal(frames[0].event, "ping");
});

test("defaults the event name to message", () => {
  const frame = parseSseFrame("data: payload\r\n\r\n");
  assert.ok(frame);
  assert.equal(frame.event, "message");
  assert.equal(frame.data, "payload");
});

test("returns null for an empty frame", () => {
  assert.equal(parseSseFrame("\r\n\r\n"), null);
  assert.equal(parseSseFrame(""), null);
});

test("a frame split across two chunks is reassembled", () => {
  const whole = "event: token\r\ndata: {\"text\":\"你好\"}\r\n\r\n";
  const cut = 20;
  const first = parseSseFrames(whole.slice(0, cut));
  assert.equal(first.frames.length, 0);
  const second = parseSseFrames(first.rest + whole.slice(cut));
  assert.equal(second.frames.length, 1);
  assert.equal(second.frames[0].data, '{"text":"你好"}');
});

test("a buffer that mixes CRLF and LF frames parses both", () => {
  const { frames } = parseSseFrames(
    "event: a\r\ndata: 1\r\n\r\nevent: b\ndata: 2\n\n"
  );
  assert.equal(frames.length, 2);
  assert.equal(frames[0].event, "a");
  assert.equal(frames[1].event, "b");
});

test("a field without a colon is read as an empty value", () => {
  // The SSE spec says a line without a colon is a field with an empty
  // value. It must not be mistaken for a frame boundary.
  const frame = parseSseFrame("event: token\r\ndata\r\n\r\n");
  assert.ok(frame);
  assert.equal(frame.event, "token");
  assert.equal(frame.data, "");
});

test("an unknown field is ignored", () => {
  const frame = parseSseFrame("event: token\r\nid: 42\r\ndata: x\r\n\r\n");
  assert.ok(frame);
  assert.equal(frame.event, "token");
  assert.equal(frame.data, "x");
});

test("a frame with no event name defaults to message", () => {
  const { frames } = parseSseFrames("data: only data\r\n\r\n");
  assert.equal(frames.length, 1);
  assert.equal(frames[0].event, "message");
  assert.equal(frames[0].data, "only data");
});
