// Proving tests for WB-02 rows W-10, W-12, W-13 (web/transpile.mjs).
// Each test builds a throwaway repo root with web/transpile.mjs, a stub jco and a fake component,
// so the real web/generated/ and jco.pin.json are never touched.
// Run: node --test web/transpile.proofs.test.mjs

import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import { chmodSync, copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const WEB = dirname(fileURLToPath(import.meta.url));
const TRANSPILE = join(WEB, "transpile.mjs");
const COMPONENT_BYTES = Buffer.from("fake-component-bytes");
const COMPONENT_SHA = createHash("sha256").update(COMPONENT_BYTES).digest("hex");
const STUB_JCO = `#!/usr/bin/env node
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
if (process.env.JCO_STUB_FAIL) { console.error("stub jco failed"); process.exit(3); }
const args = process.argv.slice(2);
const out = args[args.indexOf("-o") + 1];
mkdirSync(out, { recursive: true });
writeFileSync(join(out, "gsd-core.js"), process.env.JCO_STUB_SOURCE ?? "export function invoke(b) { return b; }\\n");
`;

// Builds <root>/{crates/gsd-sandbox/guest-module.sha256, web/...} and returns the root.
function fixture({ pinFileDigest = COMPONENT_SHA } = {}) {
  const root = mkdtempSync(join(tmpdir(), "wb02-transpile-"));
  mkdirSync(join(root, "crates", "gsd-sandbox"), { recursive: true });
  writeFileSync(join(root, "crates", "gsd-sandbox", "guest-module.sha256"), `${pinFileDigest}\n`);
  const web = join(root, "web");
  mkdirSync(join(web, "node_modules", "@bytecodealliance", "jco"), { recursive: true });
  mkdirSync(join(web, "node_modules", ".bin"), { recursive: true });
  copyFileSync(TRANSPILE, join(web, "transpile.mjs"));
  writeFileSync(
    join(web, "jco.pin.json"),
    JSON.stringify({
      package: "@bytecodealliance/jco",
      version: "1.39.0",
      component: "gsd-core",
      component_pin: "crates/gsd-sandbox/guest-module.sha256",
      component_sha256: COMPONENT_SHA,
    }),
  );
  writeFileSync(join(web, "node_modules", "@bytecodealliance", "jco", "package.json"), JSON.stringify({ version: "1.39.0" }));
  const jco = join(web, "node_modules", ".bin", "jco");
  writeFileSync(jco, STUB_JCO);
  chmodSync(jco, 0o755);
  writeFileSync(join(root, "component.wasm"), COMPONENT_BYTES);
  return root;
}

function run(root, env = {}) {
  return spawnSync(process.execPath, [join(root, "web", "transpile.mjs"), join(root, "component.wasm")], {
    cwd: root,
    encoding: "utf8",
    env: { ...process.env, ...env },
  });
}

function withRoot(root, body) {
  try {
    body();
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}

test("control: matching pins and relative imports transpile with exit 0", () => {
  const root = fixture();
  withRoot(root, () => {
    const result = run(root);
    assert.equal(result.status, 0, result.stderr);
    assert.ok(existsSync(join(root, "web", "generated", "gsd-core.js")));
  });
});

// W-10: a failing guard must leave the previous generated/ untouched.
test("W-10 guard refusal leaves generated/ unchanged", () => {
  const root = fixture();
  withRoot(root, () => {
    const generated = join(root, "web", "generated");
    mkdirSync(generated, { recursive: true });
    writeFileSync(join(generated, "sentinel.txt"), "keep");
    const result = run(root, { JCO_STUB_SOURCE: 'import fs from "node:fs";\nexport function invoke(b) { return b; }\n' });
    assert.equal(result.status, 1, "guard must refuse the built-in import");
    assert.equal(readFileSync(join(generated, "sentinel.txt"), "utf8"), "keep", "generated/ must not be wiped");
  });
});

test("W-10 failing jco leaves generated/ unchanged", () => {
  const root = fixture();
  withRoot(root, () => {
    const generated = join(root, "web", "generated");
    mkdirSync(generated, { recursive: true });
    writeFileSync(join(generated, "sentinel.txt"), "keep");
    const result = run(root, { JCO_STUB_FAIL: "1" });
    assert.notEqual(result.status, 0);
    assert.equal(readFileSync(join(generated, "sentinel.txt"), "utf8"), "keep", "generated/ must not be wiped");
  });
});

// W-12: a bare side-effect import is a module import too.
test("W-12 bare side-effect import of a built-in is refused", () => {
  const root = fixture();
  withRoot(root, () => {
    const result = run(root, { JCO_STUB_SOURCE: 'import "node:fs";\nexport function invoke(b) { return b; }\n' });
    assert.equal(result.status, 1, `expected refusal, got ${result.status}: ${result.stdout}`);
    assert.match(result.stderr, /non-relative/);
  });
});

// W-13: the component is checked against the digest in the pin file, not only jco.pin.json.
test("W-13 component is refused when the pin file digest differs", () => {
  const root = fixture({ pinFileDigest: "f".repeat(64) });
  withRoot(root, () => {
    const result = run(root);
    assert.equal(result.status, 1, `expected refusal, got ${result.status}: ${result.stderr}`);
  });
});

test("W-13 control: pin file digest equal to the component passes", () => {
  const root = fixture({ pinFileDigest: COMPONENT_SHA });
  withRoot(root, () => {
    assert.equal(run(root).status, 0);
  });
});
