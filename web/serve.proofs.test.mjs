// Proving tests for WB-02 rows W-06 (control only), W-07, W-08, W-09 (web/serve.mjs).
// Run: node --test web/serve.proofs.test.mjs

import assert from "node:assert/strict";
import { test } from "node:test";
import { copyFile, mkdtemp, readFile, rm, symlink } from "node:fs/promises";
import { createServer as createNetServer } from "node:net";
import { request as httpRequest } from "node:http";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const WEB = dirname(fileURLToPath(import.meta.url));
const { startServer } = await import(`${WEB}/serve.mjs`);

// Raw request: the path is sent exactly as given, without client-side URL normalisation.
function rawRequest(base, path, method = "GET") {
  const { hostname, port } = new URL(base);
  return new Promise((resolve, reject) => {
    const req = httpRequest({ host: hostname, port, path, method }, (res) => {
      res.resume();
      res.on("end", () => resolve({ status: res.statusCode, headers: res.headers }));
    });
    req.on("error", reject);
    req.end();
  });
}

async function closeServer(server) {
  await new Promise((resolve) => {
    server.close(resolve);
    server.closeAllConnections?.();
  });
}

// W-06 (NOT-REPRODUCED control): normalize() collapses a decoded "/../" before the lexical check, so
// no request path reaches the 403 branch. A decoded traversal must stay inside web/ and be answered
// from there, with the isolation headers.
test("W-06 control: decoded traversal stays inside web/ and keeps isolation headers", async () => {
  const { server, url } = await startServer({ isolated: true, port: 0 });
  try {
    const { status, headers } = await rawRequest(url, "/..%2fpackage.json");
    assert.equal(status, 200);
    assert.equal(headers["cross-origin-opener-policy"], "same-origin");
  } finally {
    await closeServer(server);
  }
});

// W-07: only GET and HEAD are served; other methods get 405 with Allow.
test("W-07 POST gets 405 with Allow: GET, HEAD", async () => {
  const { server, url } = await startServer({ isolated: true, port: 0 });
  try {
    const { status, headers } = await rawRequest(url, "/serve.mjs", "POST");
    assert.equal(status, 405);
    assert.equal(headers.allow, "GET, HEAD");
  } finally {
    await closeServer(server);
  }
});

test("W-07 GET and HEAD still answer 200", async () => {
  const { server, url } = await startServer({ isolated: true, port: 0 });
  try {
    assert.equal((await rawRequest(url, "/serve.mjs", "GET")).status, 200);
    assert.equal((await rawRequest(url, "/serve.mjs", "HEAD")).status, 200);
  } finally {
    await closeServer(server);
  }
});

// W-08: a symlink inside web/ that points outside must not be served.
test("W-08 symlink escaping web/ is refused", async () => {
  const dir = await mkdtemp(join(tmpdir(), "wb02-w08-"));
  try {
    await copyFile(join(WEB, "serve.mjs"), join(dir, "serve.mjs"));
    await copyFile(join(WEB, "headers.config.json"), join(dir, "headers.config.json"));
    await symlink("/etc/hostname", join(dir, "leak"));
    const { startServer: start } = await import(`${dir}/serve.mjs`);
    const { server, url } = await start({ isolated: true, port: 0 });
    try {
      const { status } = await rawRequest(url, "/leak");
      assert.ok(status === 403 || status === 404, `symlink leak answered ${status}`);
    } finally {
      await closeServer(server);
    }
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

// W-09: listen errors reject the start promise instead of crashing the process.
test("W-09 occupied port rejects startServer with EADDRINUSE", async () => {
  const blocker = createNetServer();
  await new Promise((resolve) => blocker.listen(0, "127.0.0.1", resolve));
  const port = blocker.address().port;
  try {
    await assert.rejects(startServer({ port }), (error) => error.code === "EADDRINUSE");
  } finally {
    await new Promise((resolve) => blocker.close(resolve));
  }
});

test("W-09 out-of-range port rejects with RangeError (control)", async () => {
  await assert.rejects(startServer({ port: 70000 }), RangeError);
  await assert.rejects(startServer({ port: -1 }), RangeError);
});
