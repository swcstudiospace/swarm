// The loader hands the guest the bytes it was given, so for raw request bytes the loader's reply is
// the reply the Rust engine gives for the same bytes. Today invoke re-serialises the parsed request
// (component-loader.mjs invoke: ENCODER.encode(JSON.stringify(request))), which changes four vectors:
//   - events 9007199254740993 becomes 9007199254740992, so the echoed events differ;
//   - events 2.0 becomes 2, which the Rust parser refuses (int_of takes only integers) but the loader accepts;
//   - events 9223372036854775807 becomes 9223372036854776000, which overflows i64, so the loader refuses;
//   - a leading UTF-8 BOM is dropped by TextDecoder, so the loader accepts what the Rust parser refuses.
// The control request has no vector and must agree with the engine today.
//
// The Rust reply for raw bytes is the guest's own invoke on those bytes: the guest is built from
// gsd_route::orch_status_invoke (crates/gsd-guest/src/lib.rs), the function the server answers with.
// target/debug/gsd_engine cannot be given snapshot bytes: its stdin object is {agent, script, argv, root,
// env} (crates/gsd-adapter/src/bin/gsd_engine.rs invocation_from), and tests/parity/harness.py sends only
// that object, so the binary has no raw-bytes call to match.
//
// Run: node --test web/component-loader.reply.test.mjs

import assert from "node:assert/strict";
import { test } from "node:test";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

// The loader's guest is fetched from file: URLs, which Node 24's fetch does not support.
const originalFetch = globalThis.fetch;
globalThis.fetch = async (input, init) => {
  const href = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
  if (!href.startsWith("file:")) return originalFetch(input, init);
  const bytes = await readFile(fileURLToPath(href));
  return new Response(bytes, { headers: { "content-type": "application/wasm" } });
};

const { InvokeFailure, invoke } = await import("./component-loader.mjs");
const { invoke: guestInvoke } = await import("./generated/gsd-core.js");

const ENCODER = new TextEncoder();
const BOM = [0xef, 0xbb, 0xbf];

// A list-mode request as raw text, with the given literal as the value of `events`.
function listRequest(eventsLiteral) {
  return ENCODER.encode(
    `{"mode":"list","agent":"A01","script":"orch_status","correlation":null,"db_path":"/x/tasks.db",` +
      `"swarm_dir":"/x","events":${eventsLiteral},"tasks":[],"verdicts":[],"history":null}`,
  );
}

function withBom(bytes) {
  return Uint8Array.from([...BOM, ...bytes]);
}

// The error of a failed call as {code, message}: the loader's InvokeFailure or the guest's thrown payload.
function failureOf(error) {
  if (error instanceof InvokeFailure) return { code: error.code, message: error.message };
  const payload = error && error.payload;
  if (payload && typeof payload.code === "string") return { code: payload.code, message: payload.message };
  return { code: "trap" };
}

// What one call answers for these bytes: the reply as base64, or the failure.
function outcome(call, bytes) {
  try {
    return { reply: Buffer.from(call(bytes)).toString("base64") };
  } catch (error) {
    return { failure: failureOf(error) };
  }
}

// The loader's answer to raw bytes equals the Rust engine's answer to the same bytes.
function assertSameAnswerAsEngine(bytes, label) {
  assert.deepEqual(outcome(invoke, bytes), outcome(guestInvoke, bytes), label);
}

test("raw_request_with_events_9007199254740993_gets_the_engine_reply", () => {
  assertSameAnswerAsEngine(listRequest("9007199254740993"), "events 9007199254740993");
});

test("raw_request_with_events_2_0_gets_the_engine_reply", () => {
  assertSameAnswerAsEngine(listRequest("2.0"), "events 2.0");
});

test("raw_request_with_events_i64_max_gets_the_engine_reply", () => {
  assertSameAnswerAsEngine(listRequest("9223372036854775807"), "events 9223372036854775807");
});

test("raw_request_with_leading_bom_gets_the_engine_reply", () => {
  assertSameAnswerAsEngine(withBom(listRequest("3")), "leading UTF-8 BOM");
});

// Control: no vector in the bytes, so the loader and the engine agree today and must keep agreeing.
test("control_plain_list_request_gets_the_engine_reply", () => {
  assertSameAnswerAsEngine(listRequest("3"), "control: plain events 3");
});
