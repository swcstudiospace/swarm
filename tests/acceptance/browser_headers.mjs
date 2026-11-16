// U-WEB-2 acceptance: the browser build is served with COOP and COEP, and the page is cross-origin
// isolated in Chrome. A non-isolated control shows that the headers are what makes the difference.
//
// Run from the repository root: node tests/acceptance/browser_headers.mjs
// Chrome comes from CHROME_PATH, defaulting to /usr/bin/google-chrome.

import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const repo = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
// playwright-core is a dev dependency of web/, so it resolves from there.
const { chromium } = createRequire(join(repo, "web", "package.json"))("playwright-core");
const { startServer } = await import(pathToFileURL(join(repo, "web", "serve.mjs")).href);
const chromePath = process.env.CHROME_PATH ?? "/usr/bin/google-chrome";

let failures = 0;
function check(name, ok, detail = "") {
  console.log(`${ok ? "PASS" : "FAIL"} ${name}${detail ? ` :: ${detail}` : ""}`);
  if (!ok) failures += 1;
}

const browser = await chromium.launch({ executablePath: chromePath, headless: true });
try {
  // Isolated: the page must be cross-origin isolated and the module must carry both headers.
  const { server, url } = await startServer({ isolated: true });
  try {
    const page = await browser.newPage();
    await page.goto(`${url}/`);
    await page.waitForFunction(() => document.title !== "gsd-core read route", null, { timeout: 30000 });
    const title = await page.title();
    const isolated = await page.evaluate(() => window.crossOriginIsolated);
    check("isolated page loads the loader and is cross-origin isolated", title === "isolated" && isolated === true,
      `title=${title} crossOriginIsolated=${isolated}`);

    for (const path of ["/component-loader.mjs", "/generated/gsd-core.js", "/generated/gsd-core.core.wasm"]) {
      const headers = await page.evaluate(async (target) => {
        const response = await fetch(target);
        return {
          status: response.status,
          coop: response.headers.get("cross-origin-opener-policy"),
          coep: response.headers.get("cross-origin-embedder-policy"),
        };
      }, path);
      check(`${path} carries COOP and COEP`,
        headers.status === 200 && headers.coop === "same-origin" && headers.coep === "require-corp",
        `status=${headers.status} coop=${headers.coop} coep=${headers.coep}`);
    }
    await page.close();
  } finally {
    server.close();
  }

  // Non-isolated control: the same files without the headers must not be cross-origin isolated.
  const control = await startServer({ isolated: false });
  try {
    const page = await browser.newPage();
    await page.goto(`${control.url}/`);
    await page.waitForFunction(() => document.title !== "gsd-core read route", null, { timeout: 30000 });
    const title = await page.title();
    const isolated = await page.evaluate(() => window.crossOriginIsolated);
    check("non-isolated control is not cross-origin isolated", title === "non-isolated" && isolated === false,
      `title=${title} crossOriginIsolated=${isolated}`);
    await page.close();
  } finally {
    control.server.close();
  }
} finally {
  await browser.close();
}

console.log(failures === 0 ? "all browser header checks pass" : `${failures} browser header check(s) failed`);
process.exit(failures === 0 ? 0 : 1);
