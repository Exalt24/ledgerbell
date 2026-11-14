// node --test netlify/test
import assert from "node:assert/strict";
import test from "node:test";
import handler from "../functions/status.mjs";
import { evaluate, weekdayMs } from "../lib/evaluate.mjs";

const HOUR = 3600 * 1000;
const at = (iso) => Date.parse(iso);
const status = (over = {}) => ({ generated_at: "2026-11-11T20:00:00Z", last_event_at: "2026-11-11T20:15:00Z", last_routine: "eod", kill_engaged: false, mode: "paper", ...over });

test("a routine that ran yesterday is ok", () => {
  const r = evaluate(status(), at("2026-11-12T10:00:00Z"));
  assert.equal(r.code, 200);
  assert.equal(r.body.state, "ok");
  assert.equal(r.body.last_routine, "eod");
});

test("a bot silent for more than 36 weekday hours is stale and answers 503", () => {
  const r = evaluate(status(), at("2026-11-13T10:00:00Z")); // Wednesday evening to Friday morning, 38 hours
  assert.equal(r.code, 503);
  assert.equal(r.body.state, "stale");
});

test("the weekend does not count: Friday evening to Monday morning is ok", () => {
  const r = evaluate(status({ generated_at: "2026-11-13T22:00:00Z", last_event_at: "2026-11-13T22:15:00Z", last_routine: "weekly" }), at("2026-11-16T09:00:00Z"));
  assert.equal(r.code, 200);
});

test("but a bot that missed Monday and Tuesday is stale", () => {
  const r = evaluate(status({ generated_at: "2026-11-13T22:00:00Z", last_event_at: "2026-11-13T22:15:00Z" }), at("2026-11-17T12:00:00Z"));
  assert.equal(r.code, 503);
});

test("an engaged kill switch is reported as killed, with 200, because the bot did its job", () => {
  const r = evaluate(status({ kill_engaged: true }), at("2026-11-12T10:00:00Z"));
  assert.deepEqual([r.code, r.body.state], [200, "killed"]);
});

test("a status file that was not refreshed is stale even when the last event is recent", () => {
  const r = evaluate(status({ generated_at: "2026-11-09T09:00:00Z", last_event_at: "2026-11-12T09:00:00Z" }), at("2026-11-12T10:00:00Z"));
  assert.deepEqual([r.code, r.body.state, r.body.why], [503, "stale", "the status file was not refreshed"]);
});

test("a missing file, junk and missing times are unknown, never ok", () => {
  for (const bad of [null, undefined, "x", 5, {}, { last_event_at: "nope", generated_at: "nope" }, { last_event_at: "2026-11-12T00:00:00Z" }]) {
    const r = evaluate(bad, at("2026-11-12T10:00:00Z"));
    assert.deepEqual([r.code, r.body.state], [503, "unknown"]);
  }
});

test("a time in the future is invalid", () => {
  const r = evaluate(status({ last_event_at: "2026-12-01T00:00:00Z" }), at("2026-11-12T10:00:00Z"));
  assert.deepEqual([r.code, r.body.state], [503, "invalid"]);
});

test("kill_engaged only counts when it is exactly true", () => {
  const r = evaluate(status({ kill_engaged: "false" }), at("2026-11-12T10:00:00Z"));
  assert.equal(r.body.state, "ok");
});

test("weekdayMs skips Saturday and Sunday and handles a reversed range", () => {
  assert.equal(weekdayMs(at("2026-11-14T00:00:00Z"), at("2026-11-16T00:00:00Z")), 0); // Saturday and Sunday
  assert.equal(weekdayMs(at("2026-11-13T12:00:00Z"), at("2026-11-16T12:00:00Z")), 24 * HOUR); // Friday noon to midnight, Monday midnight to noon
  assert.equal(weekdayMs(at("2026-11-12T00:00:00Z"), at("2026-11-11T00:00:00Z")), 0);
});

test("the function answers over HTTP shapes: 200 with the static file, 503 when the file is missing", async () => {
  const realFetch = globalThis.fetch;
  const fresh = status({ generated_at: new Date().toISOString(), last_event_at: new Date().toISOString() });
  globalThis.fetch = async () => new Response(JSON.stringify(fresh), { status: 200 });
  let res = await handler(new Request("https://example.netlify.app/status"));
  assert.equal(res.status, 200);
  assert.equal((await res.json()).state, "ok");
  assert.equal(res.headers.get("cache-control"), "no-store");
  globalThis.fetch = async () => new Response("not found", { status: 404 });
  res = await handler(new Request("https://example.netlify.app/status"));
  assert.equal(res.status, 503);
  globalThis.fetch = async () => {
    throw new Error("network down");
  };
  res = await handler(new Request("https://example.netlify.app/status"));
  assert.equal(res.status, 503);
  globalThis.fetch = realFetch;
});
