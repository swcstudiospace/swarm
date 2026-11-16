// Drift guard: the request keys the browser loader accepts (component-loader.mjs REQUEST_KEYS) must be
// exactly the properties of the snapshot schema (snapshot-schema.json). A key added to one and not the
// other fails here.
//
// Run: node --test web/snapshot-schema.test.mjs

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

// The loader's guest is fetched from file: URLs, which Node 24's fetch does not support.
const originalFetch = globalThis.fetch;
globalThis.fetch = async (input, init) => {
  const href = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
  if (!href.startsWith("file:")) return originalFetch(input, init);
  const bytes = await readFile(fileURLToPath(href));
  return new Response(bytes, { headers: { "content-type": "application/wasm" } });
};

const { REQUEST_KEYS } = await import("./component-loader.mjs");

const schema = JSON.parse(await readFile(new URL("./snapshot-schema.json", import.meta.url), "utf8"));

test("schema_properties_set_equal_request_keys", () => {
  assert.deepEqual([...Object.keys(schema.properties)].sort(), [...REQUEST_KEYS].sort());
});
