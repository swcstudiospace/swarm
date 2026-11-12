// Build-time transpile of the server-built gsd-core component into web/generated/ (U-WEB-0).
//
// Dev-only: jco runs here, never in the browser. The script refuses a component whose SHA-256 is
// not the pin in jco.pin.json (the pin is crates/gsd-sandbox/guest-module.sha256), refuses any jco
// that is not the pinned version, and refuses generated output that imports anything but a relative
// module, so the browser artifact carries no npm runtime dependency.
//
// Usage: node transpile.mjs <component.wasm>   (or set GSD_GUEST_WASM)

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, readdirSync, renameSync, rmSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "..");
const pin = JSON.parse(readFileSync(join(here, "jco.pin.json"), "utf8"));

function fail(message) {
  console.error(`transpile: ${message}`);
  process.exit(1);
}

const given = process.argv[2] ?? process.env.GSD_GUEST_WASM;
if (!given) fail("pass the component path, or set GSD_GUEST_WASM");
const component = resolve(given);
if (!existsSync(component)) fail(`component not found: ${component}`);

const digest = createHash("sha256").update(readFileSync(component)).digest("hex");
if (digest !== pin.component_sha256) {
  fail(`component sha256 ${digest} is not the pin ${pin.component_sha256}`);
}
const pinnedDigest = readFileSync(join(root, pin.component_pin), "utf8").trim().split(/\s+/)[0];
if (digest !== pinnedDigest) {
  fail(`component sha256 ${digest} is not the pin ${pinnedDigest} in ${pin.component_pin}`);
}

const jcoPackage = join(here, "node_modules", "@bytecodealliance", "jco", "package.json");
if (!existsSync(jcoPackage)) fail("jco is not installed: run npm install in web/");
const jcoVersion = JSON.parse(readFileSync(jcoPackage, "utf8")).version;
if (jcoVersion !== pin.version) fail(`jco ${jcoVersion} is not the pinned ${pin.version}`);

const out = join(here, "generated");
const jcoBin = join(here, "node_modules", ".bin", "jco");
// Transpile into a staging directory and move it into generated/ only after every check passes, so a
// refused build never leaves generated/ wiped or half-written.
let staging;
let failure = null;
try {
  staging = mkdtempSync(join(here, ".generated-staging-"));
  execFileSync(
    jcoBin,
    [
      "transpile",
      component,
      "-o",
      staging,
      "--name",
      pin.component,
      "--no-wasi-shim",
      "--no-nodejs-compat",
      "--quiet",
    ],
    { stdio: "inherit" },
  );

  // The browser artifact may import only its own relative files, never an npm package or a Node
  // built-in. Matches `from "x"`, `import "x"` at the start of a line, and `import("x")`.
  const specifier = /(?:\bfrom\s+|\bimport\s*\(\s*|^\s*import\s*)["']([^"']+)["']/gm;
  for (const name of readdirSync(staging)) {
    if (!name.endsWith(".js")) continue;
    const source = readFileSync(join(staging, name), "utf8");
    for (const match of source.matchAll(specifier)) {
      const target = match[1];
      if (!target.startsWith("./") && !target.startsWith("../")) {
        throw new Error(`${name} imports a non-relative module: ${target}`);
      }
    }
  }

  rmSync(out, { recursive: true, force: true });
  renameSync(staging, out);
  staging = undefined;
} catch (error) {
  failure = error.message;
} finally {
  if (staging) rmSync(staging, { recursive: true, force: true });
}
if (failure !== null) fail(failure);

for (const name of readdirSync(out).sort()) {
  const bytes = readFileSync(join(out, name));
  const hash = createHash("sha256").update(bytes).digest("hex");
  console.log(`${hash}  generated/${name}`);
}
