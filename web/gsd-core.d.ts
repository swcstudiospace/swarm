// Types for web/component-loader.mjs: the orch_status read route in the browser.

/** The wire's request modes. */
export type Mode = "list" | "history";

/** The taxonomy error codes the guest reports (the WIT error-code enum). */
export type ErrorCode = "input" | "timeout" | "dep" | "capacity" | "contract" | "policy" | "internal";

/** The orch_status request fields (see web/snapshot-schema.json). */
export interface Snapshot {
  mode: Mode;
  agent: string;
  script: string;
  correlation: string | null;
  db_path: string;
  swarm_dir: string;
  events: number;
  tasks: unknown[];
  verdicts: unknown[];
  history?: { task_id: string; task: unknown; transitions: unknown[] };
}

/** The deepest value the loader accepts: the approved read-route depth limit, tighter than the wire's snapshot parser limit (128). The root is at depth 0 and scalars count. */
export const MAX_DEPTH: 32;

/** The request keys the wire accepts, in the order the loader writes them. */
export const REQUEST_KEYS: readonly string[];

/** A failed invoke: the guest's code and its message, which never carries an input value. */
export class InvokeFailure extends Error {
  readonly code: ErrorCode;
}

/** Checks snapshot bytes and returns the request object in the wire's key order. Throws InvokeFailure("input"). */
export function requestFromBytes(snapshotBytes: Uint8Array): Record<string, unknown>;

/** Runs the read route: snapshot JSON bytes in, the server's reply bytes out. Throws InvokeFailure. */
export function invoke(snapshotBytes: Uint8Array): Uint8Array;
