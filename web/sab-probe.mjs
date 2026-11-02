// SharedArrayBuffer probe (Phase 13, U-WEB-3).
//
// The accelerator needs cross-origin isolation (the COOP and COEP headers from serve.mjs) and a
// SharedArrayBuffer constructor. Both are checked here, and nothing else is assumed.

/**
 * Reports whether the accelerator can be used in this scope.
 * @param {object} scope the global object of the page or worker (globalThis by default)
 */
export function probe(scope = globalThis) {
  const isolated = scope.crossOriginIsolated === true;
  const sharedArrayBuffer = typeof scope.SharedArrayBuffer === "function";
  return {
    isolated,
    sharedArrayBuffer,
    accelerator: isolated && sharedArrayBuffer ? "sharedarraybuffer" : null,
  };
}
