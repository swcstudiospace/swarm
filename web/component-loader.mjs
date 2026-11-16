// Browser loader for the orch_status read route (Phase 13, U-WEB-1).
//
// The guest is the jco transpile of the server-built gsd-core component (web/generated/). The loader
// takes a snapshot, the orch_status request fields as UTF-8 JSON bytes. Where those bytes read as a
// JSON request, the loader checks the wire rules of crates/gsd-route (the key set and the depth limit)
// and the tool-input byte cap, then hands the same bytes, unchanged, to the guest's invoke. A reply
// whose JSON nests deeper than the same depth limit is refused before those bytes are returned.
//
// The loader imports nothing from npm and supplies no host functions: the read route's component
// imports none. It implements no write route.

import { invoke as guestInvoke } from "./generated/gsd-core.js";

/** The deepest value the browser loader accepts on a snapshot and on a reply: the approved read-route depth limit (32). It is tighter than the server's snapshot parser (crates/gsd-route/src/lib.rs MAX_JSON_DEPTH, 128). The root is depth 0 and scalars count, as they do in the parser. */
export const MAX_DEPTH = 32;

/** Largest tool-input value inside a snapshot, in UTF-8 bytes. Snapshot transport bytes are exempt. A value of this size is accepted; one byte over is refused before the guest is called. */
export const MAX_TOOL_INPUT_BYTES = 100000;

/** The request keys the wire accepts. `history` is present only in history mode. */
export const REQUEST_KEYS = Object.freeze([
  "mode",
  "agent",
  "script",
  "correlation",
  "db_path",
  "swarm_dir",
  "events",
  "tasks",
  "verdicts",
  "history",
]);

const REQUIRED_KEYS = REQUEST_KEYS.filter((key) => key !== "history");
const CODES = new Set(["input", "timeout", "dep", "capacity", "contract", "policy", "internal"]);

/** A failed invoke: the guest's error code and its message, which never carries an input value. */
export class InvokeFailure extends Error {
  constructor(code, message, options) {
    super(message, options);
    this.name = "InvokeFailure";
    this.code = code;
  }
}

function refuse(message) {
  throw new InvokeFailure("input", message);
}

/** The deepest value in the tree, measured without recursion. The root is depth 0, each child is one deeper, and scalars count, as in the wire parser. */
function deepestValue(root) {
  let deepest = 0;
  const stack = [[root, 0]];
  while (stack.length > 0) {
    const [value, depth] = stack.pop();
    deepest = Math.max(deepest, depth);
    if (value === null || typeof value !== "object") continue;
    for (const child of Object.values(value)) stack.push([child, depth + 1]);
  }
  return deepest;
}
const textEncoder = new TextEncoder();

/** UTF-8 byte length of a tool-input value. A string is the payload itself; any other JSON value is its compact text. */
function toolInputBytes(value) {
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return textEncoder.encode(text).byteLength;
}

/** Refuses a tool-input value (`input`) above MAX_TOOL_INPUT_BYTES. Other values, including a large snapshot envelope, are not size-capped. */
function checkToolInputs(root) {
  const stack = [root];
  while (stack.length > 0) {
    const value = stack.pop();
    if (value === null || typeof value !== "object") continue;
    if (!Array.isArray(value) && Object.hasOwn(value, "input") && toolInputBytes(value.input) > MAX_TOOL_INPUT_BYTES) {
      refuse(`tool input exceeds ${MAX_TOOL_INPUT_BYTES} bytes`);
    }
    for (const child of Object.values(value)) stack.push(child);
  }
}

/** Refuses a reply, or the JSON document inside its stdout, nested deeper than MAX_DEPTH. Non-JSON replies are returned as the guest wrote them. */
function checkReply(replyBytes) {
  const parsed = readJson(replyBytes);
  if (parsed === undefined) return;
  if (deepestValue(parsed) > MAX_DEPTH) refuseContract("reply nests deeper than the wire allows");
  if (parsed !== null && typeof parsed === "object" && typeof parsed.stdout === "string") {
    try {
      const inner = JSON.parse(parsed.stdout);
      if (deepestValue(inner) > MAX_DEPTH) refuseContract("reply nests deeper than the wire allows");
    } catch (error) {
      if (error instanceof InvokeFailure) throw error;
    }
  }
}

function refuseContract(message) {
  throw new InvokeFailure("contract", message);
}

/** Reads the snapshot bytes as JSON text, keeping any leading BOM. Returns undefined when they are not JSON text; the guest decides those bytes. */
function readJson(snapshotBytes) {
  let text;
  try {
    text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(snapshotBytes);
  } catch {
    return undefined;
  }
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

/** The wire's own checks on a parsed request: the object shape, the depth limit and the key set. Throws InvokeFailure("input"). */
function checkRequest(parsed) {
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    refuse("request must be a JSON object");
  }
  if (deepestValue(parsed) > MAX_DEPTH) refuse("request nests deeper than the wire allows");
  checkToolInputs(parsed);
  const keys = Object.keys(parsed);
  if (keys.some((key) => !REQUEST_KEYS.includes(key))) refuse("request has a key the wire does not accept");
  if (!REQUIRED_KEYS.every((key) => Object.hasOwn(parsed, key))) refuse("request is missing a wire key");
}

/** Returns the request object in the wire's key order. */
function orderRequest(parsed) {
  const ordered = {};
  for (const key of REQUEST_KEYS) {
    if (Object.hasOwn(parsed, key)) ordered[key] = parsed[key];
  }
  return ordered;
}

/** Checks the snapshot bytes and returns the request object in the wire's key order. Throws InvokeFailure("input") for bytes that are not a JSON request. */
export function requestFromBytes(snapshotBytes) {
  const parsed = readJson(snapshotBytes);
  if (parsed === undefined) refuse("request is not valid JSON");
  checkRequest(parsed);
  return orderRequest(parsed);
}

/** Maps a thrown guest error to an InvokeFailure. A trap carries no guest code and no runtime text. */
function toFailure(error) {
  if (error instanceof InvokeFailure) return error;
  const payload = error && error.payload;
  if (payload && CODES.has(payload.code) && typeof payload.message === "string") {
    return new InvokeFailure(payload.code, payload.message, { cause: error });
  }
  return new InvokeFailure("internal", "the guest did not complete the call", { cause: error });
}

/** The read route: snapshot bytes in, reply bytes out, as the server's orch_status_invoke answers. The loader refuses a request that reads as JSON and breaks the wire's object shape, depth limit, tool-input cap or key set, before the guest is called. A reply nested deeper than the depth limit is refused before it is returned. Every other byte sequence reaches the guest unchanged, which decides it. */
export function invoke(snapshotBytes) {
  const parsed = readJson(snapshotBytes);
  if (parsed !== undefined) checkRequest(parsed);
  let reply;
  try {
    reply = guestInvoke(snapshotBytes);
  } catch (error) {
    throw toFailure(error);
  }
  checkReply(reply);
  return reply;
}
