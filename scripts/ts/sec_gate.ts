#!/usr/bin/env bun
/** A10 — secrets regex scan. Native subset of sec_gate.py (full audits stay in Python).
 * Optional repeatable --path limits the scan to those paths under --root. With no --path the whole tree is scanned.
 * A --path outside --root or that does not exist is E-INPUT and records nothing.
 */
import { existsSync, readdirSync, readFileSync, realpathSync, statSync } from "node:fs";
import { isAbsolute, join, relative, resolve, sep } from "node:path";
import { EXIT, parseAgentArgs, runAgentScript, type AgentArgs } from "./script_base.ts";

const RULES: [string, RegExp][] = [
  ["aws-access-key", /\bAKIA[0-9A-Z]{16}\b/],
  ["github-token", /\b(gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})/],
  ["private-key", /-----BEGIN (RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY( BLOCK)?-----/],
  ["stripe-live-key", /\bsk_live_[0-9a-zA-Z]{24,}/],
];

const SKIP: Record<string, true> = {
  ".git": true,
  node_modules: true,
  ".venv": true,
  dist: true,
  build: true,
  __pycache__: true,
  ".swarm": true,
};

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    if (SKIP[name]) continue;
    const p = join(dir, name);
    const st = statSync(p);
    if (st.isDirectory()) walk(p, out);
    else if (st.size < 2_000_000) out.push(p);
  }
  return out;
}

function subjectPaths(root: string, rest: string[]): { paths: string[] | null; error?: string } {
  const rootReal = existsSync(root) ? realpathSync(root) : resolve(root);
  const paths: string[] = [];
  const seen = new Set<string>();
  for (let i = 0; i < rest.length; i++) {
    const tok = rest[i];
    if (tok !== "--path" && !tok.startsWith("--path=")) continue;
    const raw = tok.startsWith("--path=") ? tok.slice("--path=".length) : rest[++i];
    if (!raw) return { paths: null, error: "--path requires a non-empty path relative to --root" };
    const joined = resolve(rootReal, raw);
    if (!existsSync(joined)) {
      return { paths: null, error: `--path ${JSON.stringify(raw)} does not exist under ${rootReal}` };
    }
    let target: string;
    try {
      target = realpathSync(joined);
    } catch {
      return { paths: null, error: `--path ${JSON.stringify(raw)} does not exist under ${rootReal}` };
    }
    const rel = relative(rootReal, target);
    if (rel.startsWith("..") || isAbsolute(rel)) {
      return { paths: null, error: `--path ${JSON.stringify(raw)} leaves the repository root` };
    }
    if (!seen.has(target)) {
      seen.add(target);
      paths.push(target);
    }
  }
  return { paths: paths.length ? paths : null };
}

function filesFor(root: string, scopes: string[] | null): string[] {
  if (!scopes) return walk(root);
  const out: string[] = [];
  const seen = new Set<string>();
  for (const scope of scopes) {
    const st = statSync(scope);
    if (st.isDirectory()) {
      for (const file of walk(scope)) {
        const rel = relative(root, file);
        if (rel.split(sep).some((part) => SKIP[part])) continue;
        if (!seen.has(file)) {
          seen.add(file);
          out.push(file);
        }
      }
    } else if (st.isFile() && st.size < 2_000_000 && !seen.has(scope)) {
      seen.add(scope);
      out.push(scope);
    }
  }
  return out;
}

async function run(args: AgentArgs) {
  const scoped = subjectPaths(args.root, args.rest);
  if (scoped.error) throw new Error(scoped.error);
  if (args.dryRun) {
    return { status: "ok", summary: "dry-run: security scan skipped", dry_run: true, findings: [], verdict: "pass" };
  }
  const root = resolve(args.root);
  const findings: object[] = [];
  let n = 0;
  for (const f of filesFor(root, scoped.paths)) {
    let text = "";
    try { text = readFileSync(f, "utf8"); } catch { continue; }
    for (const [rule, re] of RULES) {
      if (re.test(text)) findings.push({ id: `SEC-${++n}`, severity: "major", kind: "secret", summary: rule, location: relative(root, f) });
    }
  }
  const fail = findings.length > 0;
  return { status: fail ? "fail" : "ok", verdict: fail ? "fail" : "pass", findings, summary: `sec_gate ${fail ? "FAIL" : "PASS"}` };
}

if (import.meta.main) {
  const parsed = parseAgentArgs(process.argv.slice(2));
  const scoped = subjectPaths(parsed.root, parsed.rest);
  if (scoped.error) {
    const error = {
      agent: "A10",
      script: "sec_gate",
      status: "error",
      error: { code: "E-INPUT", message: scoped.error },
    };
    if (parsed.json) console.log(JSON.stringify(error, null, 2));
    else console.log(`[A10/sec_gate] ERROR ${scoped.error}`);
    process.exit(EXIT.ERROR);
  }
  process.exit(await runAgentScript({ agentId: "A10", name: "sec_gate", run }));
}
