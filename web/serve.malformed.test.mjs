// A malformed percent-escape in the request path must get a 400, must not leave the request
// unanswered, and must not escape the request handler as an unhandled rejection. Today
// decodeURIComponent throws URIError inside the async handler, before its try block, so the
// response is never written and the rejection goes unhandled.
//
// Run: node --test web/serve.malformed.test.mjs

import assert from "node:assert/strict";
import { test } from "node:test";
import { startServer } from "./serve.mjs";

const REQUEST_TIMEOUT_MS = 3000;

// Resolves with the response status, or null when the server did not answer within the timeout.
async function getStatus(base, path) {
  try {
    const response = await fetch(`${base}${path}`, { signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS) });
    await response.arrayBuffer();
    return response.status;
  } catch {
    return null;
  }
}

test("malformed_percent_escape_gets_400_and_server_survives", async () => {
  const rejections = [];
  const onRejection = (reason) => rejections.push(reason);
  process.on("unhandledRejection", onRejection);
  const { server, url } = await startServer({ port: 0 });
  try {
    const malformedLone = await getStatus(url, "/%");
    const malformedTruncated = await getStatus(url, "/%E0%A4%A");
    const root = await getStatus(url, "/");

    // Let any rejection raised by the handler reach the process listener before checking.
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(malformedLone, 400, "GET /% must answer 400");
    assert.equal(malformedTruncated, 400, "GET /%E0%A4%A must answer 400");
    assert.equal(root, 200, "GET / must still answer 200 after the malformed requests");
    assert.deepEqual(rejections.map(String), [], "no unhandled rejection may escape the request handler");
  } finally {
    process.off("unhandledRejection", onRejection);
    await new Promise((resolve) => {
      server.close(resolve);
      server.closeAllConnections?.();
    });
  }
});
