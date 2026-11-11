// Read-route mode selection (Phase 13, U-WEB-3).
//
// The accelerated path is used only when the page is cross-origin isolated and SharedArrayBuffer
// exists. It stages the request and the reply in shared memory and then returns a private copy. The
// fallback calls the loader directly. Both return the same reply bytes, and `status()` names the mode,
// so the fallback never reports that it accelerated.

import { invoke } from "./component-loader.mjs";
import { probe } from "./sab-probe.mjs";

export const MODES = Object.freeze({
  accelerated: "isolated-sab",
  fallback: "non-isolated-fallback",
});

/** Accepts the snapshot as a byte array or an ArrayBuffer; any other value is a caller error. */
function binaryInput(value) {
  if (value instanceof Uint8Array) return value;
  if (value instanceof ArrayBuffer) return new Uint8Array(value);
  throw new TypeError("snapshot bytes must be a Uint8Array or an ArrayBuffer");
}

/**
 * Returns the read route for this scope: its mode, its status, and its invoke.
 * @param {object} scope the global object of the page or worker (globalThis by default)
 */
export function readRoute(scope = globalThis) {
  const { accelerator, isolated, sharedArrayBuffer } = probe(scope);
  const mode = accelerator ? MODES.accelerated : MODES.fallback;

  function stage(bytes) {
    const shared = new Uint8Array(new SharedArrayBuffer(bytes.byteLength));
    shared.set(bytes);
    return shared;
  }

  return {
    status() {
      return { mode, accelerated: mode === MODES.accelerated, isolated, sharedArrayBuffer };
    },
    invoke(snapshotBytes) {
      const bytes = binaryInput(snapshotBytes);
      if (mode !== MODES.accelerated) return invoke(bytes);
      // The loader decodes its input as a normal buffer, so the staged copy is read through one.
      const request = Uint8Array.from(stage(bytes));
      const reply = invoke(request);
      return Uint8Array.from(stage(reply));
    },
  };
}
