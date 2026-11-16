// U-WEB-3 acceptance: the read route runs in both modes in Chrome, the status names the mode, and
// every parity vector gives the same reply bytes as the Python extension (G-python-truth).
//
// The isolated context must use the accelerated path (isolated-sab). The non-isolated context must
// take the fallback and say so (non-isolated-fallback), and must not claim acceleration.
//
// Run from the repository root: node tests/acceptance/browser_sab.mjs

import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const repo = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const { chromium } = createRequire(join(repo, "web", "package.json"))("playwright-core");
const { startServer } = await import(pathToFileURL(join(repo, "web", "serve.mjs")).href);
const vectors = JSON.parse(readFileSync(join(repo, "tests", "acceptance", "browser_vectors.json"), "utf8")).vectors;
const chromePath = process.env.CHROME_PATH ?? "/usr/bin/google-chrome";

let failures = 0;
function check(name, ok, detail = "") {
  console.log(`${ok ? "PASS" : "FAIL"} ${name}${detail ? ` :: ${detail}` : ""}`);
  if (!ok) failures += 1;
}

// Runs every vector through the page's read route and returns the observed answers.
async function runInPage(page) {
  return page.evaluate(async (cases) => {
    const { readRoute } = await import("/fallback-mode.mjs");
    const route = readRoute();
    const encoder = new TextEncoder();
    const answers = [];
    for (const vector of cases) {
      try {
        const reply = route.invoke(encoder.encode(JSON.stringify(vector.request)));
        let binary = "";
        for (const byte of reply) binary += String.fromCharCode(byte);
        answers.push({ name: vector.name, ok: true, reply_b64: btoa(binary) });
      } catch (error) {
        answers.push({ name: vector.name, ok: false, code: error.code ?? "internal" });
      }
    }
    return { status: route.status(), answers };
  }, vectors);
}

function comparable(vector, answer) {
  if (vector.expect === "reply") {
    return answer.ok && answer.reply_b64 === vector.reply_b64;
  }
  // The server's refusal code is E-INPUT; the browser's is the same code in lower case.
  return !answer.ok && answer.code === vector.code.replace("E-", "").toLowerCase();
}

const browser = await chromium.launch({ executablePath: chromePath, headless: true });
try {
  for (const [label, isolated, expectedMode, expectedAccelerated] of [
    ["isolated context", true, "isolated-sab", true],
    ["non-isolated context", false, "non-isolated-fallback", false],
  ]) {
    const { server, url } = await startServer({ isolated });
    try {
      const page = await browser.newPage();
      await page.goto(`${url}/`);
      await page.waitForFunction(() => document.title !== "gsd-core read route", null, { timeout: 30000 });
      const { status, answers } = await runInPage(page);

      check(`${label} reports mode ${expectedMode}`,
        status.mode === expectedMode && status.accelerated === expectedAccelerated,
        `mode=${status.mode} accelerated=${status.accelerated} isolated=${status.isolated} sab=${status.sharedArrayBuffer}`);

      for (const vector of vectors) {
        const answer = answers.find((candidate) => candidate.name === vector.name);
        check(`${label}: ${vector.name} matches the Python extension`, answer !== undefined && comparable(vector, answer),
          answer ? (answer.ok ? "reply" : `refusal ${answer.code}`) : "no answer");
      }
      await page.close();
    } finally {
      server.close();
    }
  }
} finally {
  await browser.close();
}

console.log(failures === 0 ? "all browser mode and parity checks pass" : `${failures} browser check(s) failed`);
process.exit(failures === 0 ? 0 : 1);
