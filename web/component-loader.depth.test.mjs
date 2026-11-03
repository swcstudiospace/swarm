// The loader's wire depth limit: a request nested to depth 32 is accepted and depth 33 is refused
// with code "input" before the guest is called. Today MAX_DEPTH is 128, so depth 33 is accepted
// and the depth 33 refusal tests fail; the depth 1 and depth 32 tests pass today and must stay green.
//
// Each request is valid for the wire schema at its depth (crates/gsd-route/src/orch_status_wire.rs):
// list mode with no tasks, and the nesting under `history`, which list mode ignores. A refusal can
// therefore come only from the depth check, never from a field.
//
// Depth follows the loader's own measure (component-loader.mjs deepestValue): the root object is
// depth 0, each child value is one deeper, and scalars count. The root's scalar fields are depth 1,
// so a request whose deepest value is at depth N has N - 1 nested arrays under `history`.
//
// The loader's module top level instantiates the jco guest (web/generated/gsd-core.js), so these
// tests import the real module; the depth gate in requestFromBytes runs before the guest is called.
//
// Run: node --test web/component-loader.depth.test.mjs

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

const { InvokeFailure, invoke, requestFromBytes } = await import("./component-loader.mjs");

const ENCODER = new TextEncoder();

// A list-mode request whose deepest value is at `depth`. Every key but `history` is a scalar or an
// empty list, which the wire accepts. The wire ignores `history` in list mode, so any value there is
// valid; it holds `depth - 1` nested arrays around a scalar leaf.
function requestWithDepth(depth) {
  let history = "leaf";
  for (let level = 1; level < depth; level += 1) history = [history];
  return {
    mode: "list",
    agent: "agent",
    script: "script",
    correlation: "correlation",
    db_path: "db",
    swarm_dir: "swarm",
    events: 0,
    tasks: [],
    verdicts: [],
    history,
  };
}

function bytesOf(request) {
  return ENCODER.encode(JSON.stringify(request));
}

function isInputRefusal(error) {
  return error instanceof InvokeFailure && error.code === "input";
}

test("request_nested_to_depth_1_is_accepted", () => {
  const request = requestWithDepth(1);
  assert.doesNotThrow(() => requestFromBytes(bytesOf(request)));
});

test("request_nested_to_depth_32_is_accepted", () => {
  const request = requestWithDepth(32);
  const accepted = requestFromBytes(bytesOf(request));
  assert.deepEqual(accepted.tasks, request.tasks);
});

test("request_nested_to_depth_32_is_not_refused_by_invoke", () => {
  let thrown = null;
  try {
    invoke(bytesOf(requestWithDepth(32)));
  } catch (error) {
    thrown = error;
  }
  assert.ok(!isInputRefusal(thrown), `a depth-32 request was refused as input: ${thrown?.message}`);
});

test("request_nested_to_depth_33_is_refused_with_input_code", () => {
  assert.throws(() => requestFromBytes(bytesOf(requestWithDepth(33))), isInputRefusal);
});

test("request_nested_to_depth_33_is_refused_by_invoke_before_the_guest", () => {
  assert.throws(() => invoke(bytesOf(requestWithDepth(33))), isInputRefusal);
});
