// Proving test for WB-02 row W-04 (web/fallback-mode.mjs readRoute().invoke, both modes).
// The real generated guest fetches its wasm by file:// URL at import, which Node cannot do, so the
// loader files are copied from web/ into a temp dir with a stub guest. The copy is taken per run, so
// the test always exercises the current fallback-mode.mjs and component-loader.mjs.
// Run: node --test web/fallback.proofs.test.mjs

import assert from "node:assert/strict";
import { test } from "node:test";
import { copyFileSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const WEB = dirname(fileURLToPath(import.meta.url));
const dir = mkdtempSync(join(tmpdir(), "wb02-w04-"));
process.on("exit", () => rmSync(dir, { recursive: true, force: true }));
for (const name of ["fallback-mode.mjs", "component-loader.mjs", "sab-probe.mjs"]) {
  copyFileSync(join(WEB, name), join(dir, name));
}
mkdirSync(join(dir, "generated"));
writeFileSync(join(dir, "generated", "gsd-core.js"), "export function invoke(bytes) { return new Uint8Array(0); }\n");
const { readRoute } = await import(join(dir, "fallback-mode.mjs"));

const scopes = {
  accelerated: { crossOriginIsolated: true, SharedArrayBuffer },
  fallback: { crossOriginIsolated: false },
};

for (const [mode, scope] of Object.entries(scopes)) {
  test(`W-04 ${mode}: non-binary input is rejected with TypeError`, () => {
    assert.throws(() => readRoute(scope).invoke("{}"), TypeError);
    assert.throws(() => readRoute(scope).invoke(42), TypeError);
    assert.throws(() => readRoute(scope).invoke(null), TypeError);
  });
}

test("W-04 both modes report the same error for the same non-binary input", () => {
  const errorOf = (scope) => {
    try {
      readRoute(scope).invoke("{}");
      return "no error";
    } catch (error) {
      return `${error.name}`;
    }
  };
  assert.equal(errorOf(scopes.accelerated), errorOf(scopes.fallback));
});

test("W-04 control: a Uint8Array is not rejected as a type error in either mode", () => {
  for (const scope of Object.values(scopes)) {
    assert.doesNotThrow(() => readRoute(scope).invoke(new Uint8Array(0)));
  }
});
