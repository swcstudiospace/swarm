// Static server for the browser build (Phase 13, U-WEB-2).
//
// Serves this directory with the isolation headers from headers.config.json, so the page is
// cross-origin isolated and SharedArrayBuffer is available. `startServer({ isolated: false })`
// serves the same files without the headers: the non-isolated control for the fallback check.
//
// Usage: node serve.mjs [port]

import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { dirname, extname, join, normalize, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const config = JSON.parse(await readFile(join(here, "headers.config.json"), "utf8"));

const TYPES = {
  ".mjs": "text/javascript; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".wasm": "application/wasm",
  ".json": "application/json; charset=utf-8",
  ".ts": "text/plain; charset=utf-8",
  ".html": "text/html; charset=utf-8",
};

// The page loads the loader, then reports whether the document is cross-origin isolated.
const PAGE = `<!doctype html>
<meta charset="utf-8">
<title>gsd-core read route</title>
<script type="module">
  import "./component-loader.mjs";
  document.title = window.crossOriginIsolated ? "isolated" : "non-isolated";
</script>
`;

/** Starts the server on 127.0.0.1 and resolves with the server and its base URL. */
export function startServer({ isolated = true, port = 0 } = {}) {
  const extra = isolated ? config.headers : {};
  const server = createServer(async (request, response) => {
    try {
      const url = new URL(request.url ?? "/", "http://localhost");
      if (url.pathname === "/") {
        response.writeHead(200, { ...extra, "content-type": TYPES[".html"] });
        response.end(PAGE);
        return;
      }
      let path;
      try {
        path = decodeURIComponent(url.pathname);
      } catch (error) {
        if (!(error instanceof URIError)) throw error;
        response.writeHead(400, { ...extra, "content-type": "text/plain; charset=utf-8" });
        response.end("malformed percent-escape in request path\n");
        return;
      }
      const target = resolve(here, `.${normalize(path)}`);
      if (!target.startsWith(here + sep)) {
        response.writeHead(403);
        response.end();
        return;
      }
      try {
        const body = await readFile(target);
        response.writeHead(200, { ...extra, "content-type": TYPES[extname(target)] ?? "application/octet-stream" });
        response.end(body);
      } catch {
        response.writeHead(404, extra);
        response.end();
      }
    } catch (error) {
      console.error(`request ${request.method} ${request.url} failed:`, error);
      if (response.headersSent) {
        response.destroy();
      } else {
        response.writeHead(500, { "content-type": "text/plain; charset=utf-8" });
        response.end("internal server error\n");
      }
    }
  });
  return new Promise((resolvePromise) => {
    server.listen(port, "127.0.0.1", () => {
      resolvePromise({ server, url: `http://127.0.0.1:${server.address().port}` });
    });
  });
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const { url } = await startServer({ port: Number(process.argv[2] ?? 8080) });
  console.log(`serving ${here} with isolation headers at ${url}/`);
}
