/**
 * Bridge unit/integration (D-06..D-08): real python3 children against fixture scripts in a tmp SWARM_ROOT.
 * Fixtures are written at test time; nothing is added to the repo's scripts/ or .swarm/.
 */
import { afterEach, expect, spyOn, test } from "bun:test";
import { cpSync, existsSync, mkdirSync, readFileSync, realpathSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import type * as BridgeModule from "../src/bridge.ts";
import { runScript, SwarmToolError, swarmRoot } from "../src/bridge.ts";
import { createSwarmExtension } from "../src/index.ts";
import { callTool, fakeCtx, fakePi, gitRepo, isolateEnv, REPO_ROOT, tmpDir } from "./helpers.ts";

isolateEnv("SWARM_DIR", "SWARM_ROOT", "SWARM_TASK_ID", "SWARM_CORRELATION_ID", "SWARM_ED25519_KEY_FILE", "PATH");

const FIXTURES: Record<string, string> = {
  fx_env: `import json, os, sys
print(json.dumps({"status": "ok", "SWARM_DIR": os.environ.get("SWARM_DIR"), "SWARM_TASK_ID": os.environ.get("SWARM_TASK_ID"),
                  "SWARM_CORRELATION_ID": os.environ.get("SWARM_CORRELATION_ID"), "cwd": os.getcwd(), "argv": sys.argv[1:],
                  "SWARM_ED25519_KEY_FILE": os.environ.get("SWARM_ED25519_KEY_FILE")}))
`,
  fx_ok: `print('{"status": "ok", "summary": "fine", "n": 1}')
`,
  fx_fail: `import sys
print('{"status": "fail", "summary": "gate failed", "findings": [1]}')
sys.exit(1)
`,
  fx_err: `import json, sys
print(json.dumps({"status": "error", "error": {"code": "E-CONTRACT", "message": "E-CONTRACT: illegal transition DONE -> CREATED for T-1"}}))
sys.exit(2)
`,
  fx_usage: `import sys
sys.stderr.write("usage: fx_usage.py [-h]\\nfx_usage.py: error: argument --brief-text: expected one argument\\n")
sys.exit(2)
`,
  fx_garbage: `print("definitely not json")
`,
  fx_137: `import os, sys
print('{"status": "ok"}')
sys.stdout.flush()
os._exit(137)
`,
  // sleeps with a sleeping grandchild; both pids go to --pids=<file>
  fx_sleep: `import json, os, subprocess, sys, time
out = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--pids="))
gc = subprocess.Popen(["sleep", "60"])
open(out + ".tmp", "w").write(json.dumps({"child": os.getpid(), "grandchild": gc.pid}))
os.replace(out + ".tmp", out)
time.sleep(60)
`,
  // exits 0 at once while a short-lived grandchild keeps stdout open
  fx_linger: `import json, os, subprocess, sys
out = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--pids="))
gc = subprocess.Popen(["sleep", "5"])
open(out + ".tmp", "w").write(json.dumps({"child": os.getpid(), "grandchild": gc.pid}))
os.replace(out + ".tmp", out)
print('{"status": "ok"}')
sys.stdout.flush()
os._exit(0)
`,
};

interface Pids {
  child: number;
  grandchild: number;
}
const spawned: number[] = [];

afterEach(() => {
  for (const pid of spawned.splice(0)) {
    try {
      process.kill(pid, "SIGKILL");
    } catch {}
  }
});

/** tmp SWARM_ROOT (scripts/orch_plan.py marker + fixtures) and tmp SWARM_DIR; returns a cwd inside a tmp git repo. */
function setup(): { root: string; cwd: string; repo: string } {
  const root = tmpDir("swarm-omp-root-");
  mkdirSync(join(root, "scripts"));
  writeFileSync(join(root, "scripts", "orch_plan.py"), "");
  for (const [name, body] of Object.entries(FIXTURES)) writeFileSync(join(root, "scripts", `${name}.py`), body);
  process.env.SWARM_ROOT = root;
  process.env.SWARM_DIR = tmpDir("swarm-omp-dir-");
  const repo = gitRepo("sub");
  return { root, cwd: join(repo, "sub"), repo };
}

function alive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

/** Polls real OS process state (pid files, kill(pid, 0)); fake timers cannot drive other processes. */
async function waitFor(cond: () => boolean, ms = 5_000): Promise<boolean> {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    if (cond()) return true;
    await Bun.sleep(25);
  }
  return cond();
}

async function pidsFrom(file: string): Promise<Pids> {
  expect(await waitFor(() => existsSync(file))).toBe(true);
  const pids = JSON.parse(readFileSync(file, "utf8")) as Pids;
  spawned.push(pids.child, pids.grandchild);
  return pids;
}

async function expectGroupDead({ child, grandchild }: Pids): Promise<void> {
  expect(await waitFor(() => !alive(child) && !alive(grandchild))).toBe(true);
}

async function rejection(p: Promise<unknown>): Promise<SwarmToolError> {
  const err = await p.then(
    (v) => {
      throw new Error(`expected a rejection, got ${JSON.stringify(v)}`);
    },
    (e: unknown) => e,
  );
  expect(err).toBeInstanceOf(SwarmToolError);
  return err as SwarmToolError;
}

// ── env hygiene (D-06, D-08) ────────────────────────────────────────────────

test("env: absolute SWARM_DIR, stale ids stripped, --flag=value argv then --json", async () => {
  const { cwd } = setup();
  process.env.SWARM_DIR = "rel-swarm-dir";
  process.env.SWARM_TASK_ID = "T-stale";
  process.env.SWARM_CORRELATION_ID = "C-stale";
  const res = await runScript({ script: "fx_env", args: ["--root=/x", "--brief-text=--pattern"], cwd });
  expect(res.exitCode).toBe(0);
  expect(res.json.SWARM_DIR).toBe(resolve("rel-swarm-dir"));
  expect(res.swarmDir).toBe(resolve("rel-swarm-dir"));
  expect(res.json.SWARM_TASK_ID).toBeNull();
  expect(res.json.SWARM_CORRELATION_ID).toBeNull();
  expect(res.json.argv).toEqual(["--root=/x", "--brief-text=--pattern", "--json"]);
});

test("env: a relative SWARM_ED25519_KEY_FILE resolves against the session cwd, never the runtime root", async () => {
  const { cwd } = setup();
  process.env.SWARM_ED25519_KEY_FILE = "secrets/key.hex";
  const relative = await runScript({ script: "fx_env", args: [], cwd });
  expect(relative.json.SWARM_ED25519_KEY_FILE).toBe(resolve(cwd, "secrets/key.hex"));
  process.env.SWARM_ED25519_KEY_FILE = "/abs/key.hex";
  const absolute = await runScript({ script: "fx_env", args: [], cwd });
  expect(absolute.json.SWARM_ED25519_KEY_FILE).toBe("/abs/key.hex");
});

test("env: unset SWARM_DIR falls back to the git toplevel of cwd", async () => {
  const { cwd, repo } = setup();
  delete process.env.SWARM_DIR;
  const res = await runScript({ script: "fx_env", args: [], cwd });
  expect(res.json.SWARM_DIR).toBe(`${repo}/.swarm`);
});

// ── SWARM_ROOT resolution ──────────────────────────────────────────────────

test("root: env SWARM_ROOT with scripts/orch_plan.py is used as script root and child cwd", async () => {
  const { root, cwd } = setup();
  const res = await runScript({ script: "fx_env", args: [], cwd });
  expect(swarmRoot()).toBe(root);
  expect(res.json.cwd).toBe(root);
});

test("root: whitespace-only SWARM_ROOT falls back to the package root", () => {
  process.env.SWARM_ROOT = "   ";
  expect(swarmRoot()).toBe(realpathSync(REPO_ROOT));
});

test("root: SWARM_ROOT without scripts/orch_plan.py falls back to the package root", () => {
  process.env.SWARM_ROOT = tmpDir("swarm-omp-empty-");
  expect(swarmRoot()).toBe(realpathSync(REPO_ROOT));
});

/** A relocated copy of the whole omp/ package (D-09) under a tmp dir whose ../.. holds no scripts/orch_plan.py. */
function relocatedOmp(): string {
  const pkg = join(tmpDir("swarm-omp-pkg-"), "omp");
  cpSync(join(REPO_ROOT, "omp", "src"), join(pkg, "src"), { recursive: true });
  cpSync(join(REPO_ROOT, "omp", "package.json"), join(pkg, "package.json"));
  return pkg;
}

test("root: no SWARM_ROOT and no package root → E-DEP", async () => {
  const pkg = relocatedOmp();
  delete process.env.SWARM_ROOT;
  // runtime-selected path: a copy of the module whose ../.. has no scripts/orch_plan.py
  const mod = (await import(join(pkg, "src", "bridge.ts"))) as typeof BridgeModule;
  const err = await mod.runPy({ script: "orch_status", args: [], cwd: pkg }).catch((e: { code: string }) => e);
  expect(err.code).toBe("E-DEP");
});

test("root: a relocated omp/ package loads on its own and runs the runtime named by SWARM_ROOT (D-09)", async () => {
  const pkg = relocatedOmp();
  const { root, cwd } = setup();
  // runtime-selected path (the tmp copy); the whole import graph resolves inside it: no omp/src file reaches outside omp/
  const entry = (await import(join(pkg, "src", "index.ts"))) as { default: unknown };
  expect(typeof entry.default).toBe("function");
  const mod = (await import(join(pkg, "src", "bridge.ts"))) as typeof BridgeModule;
  expect(mod.swarmRoot()).toBe(root);
  const res = await mod.runScript({ script: "fx_env", args: [], cwd });
  expect(res.json.cwd).toBe(root);
  expect(res.swarmDir).toBe(process.env.SWARM_DIR);
});

// ── exit/stdout → outcome map ──────────────────────────────────────────────

test("map: exit 0 → result", async () => {
  const { cwd } = setup();
  const res = await runScript({ script: "fx_ok", args: [], cwd });
  expect(res).toMatchObject({ exitCode: 0, json: { status: "ok", n: 1 } });
});

test("map: exit 1 with status fail → returned with exitCode 1, not thrown", async () => {
  const { cwd } = setup();
  const res = await runScript({ script: "fx_fail", args: [], cwd });
  expect(res).toMatchObject({ exitCode: 1, json: { status: "fail", findings: [1] } });
});

test("map: exit 2 with JSON error → that code, message not double-prefixed", async () => {
  const { cwd } = setup();
  const err = await rejection(runScript({ script: "fx_err", args: [], cwd }));
  expect(err.code).toBe("E-CONTRACT");
  expect(err.message).toBe("E-CONTRACT: illegal transition DONE -> CREATED for T-1");
});

test("map: exit 2 with empty stdout → E-INPUT with the stderr tail", async () => {
  const { cwd } = setup();
  const err = await rejection(runScript({ script: "fx_usage", args: [], cwd }));
  expect(err.code).toBe("E-INPUT");
  expect(err.message).toContain("argument --brief-text: expected one argument");
});

test("map: non-JSON stdout → E-INTERNAL", async () => {
  const { cwd } = setup();
  const err = await rejection(runScript({ script: "fx_garbage", args: [], cwd }));
  expect(err.code).toBe("E-INTERNAL");
  expect(err.message).toContain("definitely not json");
});

test("map: unexpected exit code 137 → E-INTERNAL even with JSON stdout", async () => {
  const { cwd } = setup();
  const err = await rejection(runScript({ script: "fx_137", args: [], cwd }));
  expect(err.code).toBe("E-INTERNAL");
  expect(err.message).toContain("137");
});

test("map: spawn failure (python3 not on PATH) → E-DEP", async () => {
  const { cwd } = setup();
  process.env.PATH = tmpDir("swarm-omp-nopath-");
  const err = await rejection(runScript({ script: "fx_ok", args: [], cwd }));
  expect(err.code).toBe("E-DEP");
});

// ── cancellation (D-07) ────────────────────────────────────────────────────

test("cancel: abort kills child and grandchild, rejects E-INTERNAL cancelled", async () => {
  const { cwd, root } = setup();
  const file = join(root, "pids.json");
  const ac = new AbortController();
  const p = runScript({ script: "fx_sleep", args: [`--pids=${file}`], cwd, signal: ac.signal });
  const pids = await pidsFrom(file);
  ac.abort();
  const err = await rejection(p);
  expect(err.code).toBe("E-INTERNAL");
  expect(err.message).toContain("cancelled");
  expect(err.message).toContain("swarm_status");
  await expectGroupDead(pids);
});

test("deadline: abort with a TimeoutError reason → E-TIMEOUT", async () => {
  const { cwd, root } = setup();
  const file = join(root, "pids.json");
  const ac = new AbortController();
  const p = runScript({ script: "fx_sleep", args: [`--pids=${file}`], cwd, signal: ac.signal });
  const pids = await pidsFrom(file);
  ac.abort(Object.assign(new Error("Deadline exceeded"), { name: "TimeoutError" }));
  const err = await rejection(p);
  expect(err.code).toBe("E-TIMEOUT");
  await expectGroupDead(pids);
});

test("deadline: bridge timeoutMs → E-TIMEOUT and the group is dead", async () => {
  const { cwd, root } = setup();
  const file = join(root, "pids.json");
  const p = runScript({ script: "fx_sleep", args: [`--pids=${file}`], cwd, timeoutMs: 300 });
  const pids = await pidsFrom(file);
  const err = await rejection(p);
  expect(err.code).toBe("E-TIMEOUT");
  await expectGroupDead(pids);
});

test("abort after exit: a child that already exited 0 still rejects", async () => {
  const { cwd, root } = setup();
  const file = join(root, "pids.json");
  const ac = new AbortController();
  let settled = false;
  const p = runScript({ script: "fx_linger", args: [`--pids=${file}`], cwd, signal: ac.signal });
  void p.then(() => (settled = true), () => (settled = true));
  const pids = await pidsFrom(file);
  expect(await waitFor(() => !alive(pids.child))).toBe(true); // exited 0; stdout still held by the grandchild
  expect(settled).toBe(false);
  ac.abort();
  const err = await rejection(p);
  expect(err.code).toBe("E-INTERNAL");
  await expectGroupDead(pids);
});

test("escalation: no SIGKILL reaches the group after a SIGTERM'd child has exited (WR-02)", async () => {
  const { cwd, root } = setup();
  const file = join(root, "pids.json");
  const ac = new AbortController();
  const kill = spyOn(process, "kill");
  try {
    const p = runScript({ script: "fx_sleep", args: [`--pids=${file}`], cwd, signal: ac.signal });
    const pids = await pidsFrom(file);
    ac.abort();
    await rejection(p);
    await expectGroupDead(pids);
    await Bun.sleep(3_300); // past the 3 s SIGTERM → SIGKILL grace
    const toGroup = kill.mock.calls.filter(([target]) => target === -pids.child).map(([, sig]) => sig);
    expect(toGroup).toEqual(["SIGTERM"]);
  } finally {
    kill.mockRestore();
  }
}, 10_000);

test("abort before start: rejects without spawning", async () => {
  const { cwd } = setup();
  const spawn = spyOn(Bun, "spawn");
  try {
    const err = await rejection(runScript({ script: "fx_ok", args: [], cwd, signal: AbortSignal.abort() }));
    expect(err.code).toBe("E-INTERNAL");
    expect(spawn).not.toHaveBeenCalled();
  } finally {
    spawn.mockRestore();
  }
});

test("sweep: a session's shutdown group-kills only its own in-flight children (WR-01)", async () => {
  const { cwd, root } = setup();
  // two sessions (two factory calls); each tool call runs fx_sleep through the real bridge
  const sessions = ["a", "b"].map((name) => {
    const file = join(root, `pids-${name}.json`);
    const pi = fakePi();
    createSwarmExtension({ bridge: (req) => runScript({ ...req, script: "fx_sleep", args: [`--pids=${file}`] }) })(pi.api);
    const handlers = pi.handlers.get("session_shutdown") ?? [];
    expect(handlers.length).toBe(1);
    return { file, handlers, call: callTool(pi.tool("swarm_status"), {}, fakeCtx(cwd)) };
  });
  const [a, b] = sessions;
  const pidsA = await pidsFrom(a.file);
  const pidsB = await pidsFrom(b.file);
  a.handlers[0]({ type: "session_shutdown" }, fakeCtx(cwd));
  expect((await rejection(a.call)).code).toBe("E-INTERNAL");
  await expectGroupDead(pidsA);
  await Bun.sleep(200);
  expect(alive(pidsB.child) && alive(pidsB.grandchild)).toBe(true); // the sibling session's child is untouched
  b.handlers[0]({ type: "session_shutdown" }, fakeCtx(cwd));
  expect((await rejection(b.call)).code).toBe("E-INTERNAL");
  await expectGroupDead(pidsB);
});
