// Browser loader for the orch_status read route (Phase 13, U-WEB-1).
//
// The guest is the jco transpile of the server-built gsd-core component (web/generated/). The loader
// takes a snapshot, the orch_status request fields as UTF-8 JSON bytes, checks it against the wire
// rules of crates/gsd-route (the key set and the depth limit), and calls the guest's invoke. The
// reply bytes are the same bytes the server's orch_status_invoke returns for the same request.
//
// The loader imports nothing from npm and supplies no host functions: the read route's component
// imports none. It implements no write route.

import { invoke as guestInvoke } from "./generated/gsd-core.js";

/** The deepest value the wire's snapshot parser accepts (crates/gsd-route/src/lib.rs MAX_JSON_DEPTH). The root is depth 0 and scalars count, as they do in the parser. */
export const MAX_DEPTH = 128;

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
const ENCODER = new TextEncoder();

/** A failed invoke: the guest's error code and its message, which never carries an input value. */
export class InvokeFailure extends Error {
  constructor(code, message) {
    super(message);
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

/** Checks the snapshot bytes and returns the request object in the wire's key order. */
export function requestFromBytes(snapshotBytes) {
  let text;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(snapshotBytes);
  } catch {
    refuse("request is not valid UTF-8");
  }
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch {
    refuse("request is not valid JSON");
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    refuse("request must be a JSON object");
  }
  if (deepestValue(parsed) > MAX_DEPTH) refuse("request nests deeper than the wire allows");
  const keys = Object.keys(parsed);
  if (keys.some((key) => !REQUEST_KEYS.includes(key))) refuse("request has a key the wire does not accept");
  if (!REQUIRED_KEYS.every((key) => Object.hasOwn(parsed, key))) refuse("request is missing a wire key");
  const ordered = {};
  for (const key of REQUEST_KEYS) {
    if (Object.hasOwn(parsed, key)) ordered[key] = parsed[key];
  }
  return ordered;
}

/** Maps a thrown guest error to an InvokeFailure. A trap carries no guest code and no runtime text. */
function toFailure(error) {
  if (error instanceof InvokeFailure) return error;
  const payload = error && error.payload;
  if (payload && CODES.has(payload.code) && typeof payload.message === "string") {
    return new InvokeFailure(payload.code, payload.message);
  }
  return new InvokeFailure("internal", "the guest did not complete the call");
}

/** The read route: snapshot bytes in, reply bytes out, as the server's orch_status_invoke answers. */
export function invoke(snapshotBytes) {
  const request = requestFromBytes(snapshotBytes);
  const requestBytes = ENCODER.encode(JSON.stringify(request));
  try {
    return guestInvoke(requestBytes);
  } catch (error) {
    throw toFailure(error);
  }
}
