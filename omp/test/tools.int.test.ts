/** Integration: tools → real bridge → real python scripts → tmp Task Store (never the repo's .swarm/). */
import { beforeEach, expect, test } from "bun:test";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readdirSync, readFileSync, unlinkSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { type Bridge, type BridgeRequest, runPy, SwarmToolError } from "../src/bridge.ts";
import { createSwarmExtension } from "../src/index.ts";
import type { ExtensionContext, SessionEntry } from "../src/omp-api.ts";
import { agentCtx, callTool, ephemeralSigningKey, type FakePi, fakeCtx, fakePi, gitRepo, isolateEnv, REPO_ROOT, runPython, tmpDir } from "./helpers.ts";
import { SWARM_SLUGS } from "../src/guard.ts";

isolateEnv(
  "SWARM_DIR", "SWARM_ROOT", "SWARM_TASK_ID", "SWARM_CORRELATION_ID", "SWARM_AGENT_SESSION", "SWARM_AGENT",
  "SWARM_SIGNING_KEY", "SWARM_ALLOW_INSECURE_DEV_KEY", "SWARM_AGENTS_FILE",
  "SWARM_ED25519_KEY", "SWARM_ED25519_KEY_FILE", "SWARM_REQUIRE_KEY", "HOME", "XDG_CONFIG_HOME",
  "SWARM_PURE_PYTHON", "SWARM_GSD_STATE_FILE", "SWARM_CLAUDE_STATE_DIR",
);
beforeEach(() => {
  process.env.SWARM_SIGNING_KEY = ephemeralSigningKey();
  // No host signing references or state mirrors, even if a test clears the ephemeral raw key.
  delete process.env.SWARM_ED25519_KEY;
  delete process.env.SWARM_ED25519_KEY_FILE;
  delete process.env.SWARM_ALLOW_INSECURE_DEV_KEY;
  delete process.env.SWARM_REQUIRE_KEY;
  process.env.HOME = tmpDir("swarm-omp-home-");
  process.env.XDG_CONFIG_HOME = tmpDir("swarm-omp-config-");
  process.env.SWARM_GSD_STATE_FILE = "";
  process.env.SWARM_CLAUDE_STATE_DIR = tmpDir("swarm-omp-claude-");
});

/** All tools over the real bridge, counting bridge calls. */
function swarm() {
  const calls: BridgeRequest[] = [];
  const bridge: Bridge = (req) => {
    calls.push(req);
    return runPy(req);
  };
  const pi = fakePi();
  createSwarmExtension({ bridge })(pi.api);
  return { calls, tool: pi.tool };
}

/** A tmp git repo with its own tmp SWARM_DIR (exported, as the headless runner does). */
function tmpStore(): { repo: string; sdir: string; ctx: ExtensionContext } {
  const repo = gitRepo();
  const sdir = tmpDir("swarm-omp-dir-");
  process.env.SWARM_DIR = sdir;
  return { repo, sdir, ctx: fakeCtx(repo) };
}

type StatusRow = { task_id: string; state: string; ready: boolean };

async function statusRows(tool: FakePi["tool"], ctx: ExtensionContext, corr: string): Promise<StatusRow[]> {
  const res = await callTool(tool("swarm_status"), { correlation_id: corr }, ctx);
  return (res.details as { tasks: StatusRow[] }).tasks;
}

function stateOf(rows: StatusRow[], id: string): string | undefined {
  return rows.find((r) => r.task_id === id)?.state;
}

async function rejection(p: Promise<unknown>): Promise<SwarmToolError> {
  const err = await p.then(
    () => undefined,
    (e: unknown) => e,
  );
  if (!(err instanceof SwarmToolError)) throw new Error(`expected a SwarmToolError, got ${String(err)}`);
  return err;
}

const HOTFIX = { brief: "fix the login bug", pattern: "hotfix", risk_class: "low", prefix: "H", correlation_id: "c-hot" };

/** Lease a task along the legal path through swarm_transition. */
async function lease(tool: FakePi["tool"], ctx: ExtensionContext, id: string): Promise<void> {
  for (const state of ["CLAIMED", "IN_PROGRESS"]) {
    await callTool(tool("swarm_transition"), { task_id: id, state, reason: "lease" }, ctx);
  }
}

const RESULT = {
  task_id: "H-patch",
  state: "IN_REVIEW",
  outputs: [{ kind: "code.patch", uri: "file://patch.diff", version: "1", digest: "" }],
  summary_md: "patched",
};

test("plan dry: every advertised pattern plans (feature 13, hotfix 8, dependency 8, cloud-native 16, cluster 14, web3 15, parallel 5)", async () => {
  const { ctx } = tmpStore();
  const { tool } = swarm();
  const expected: Record<string, number> = { feature: 13, hotfix: 8, dependency: 8, "cloud-native": 16, cluster: 14, web3: 15, parallel: 5 };
  // the schema must not advertise a pattern (e.g. `custom`) that python always rejects through this tool
  const props = tool("swarm_plan").parameters.properties;
  const pattern = props && typeof props === "object" && "pattern" in props ? props.pattern : undefined;
  const advertised = pattern && typeof pattern === "object" && "enum" in pattern && Array.isArray(pattern.enum) ? pattern.enum.map(String) : [];
  expect([...advertised].sort()).toEqual(Object.keys(expected).sort());
  const slices = [
    { id: "api", paths: ["swarm"], agent: "A05" },
    { id: "cli", paths: ["scripts"], agent: "A05" },
  ];
  for (const name of advertised) {
    const params = { brief: "b", pattern: name, risk_class: "medium", dry_run: true, ...(name === "parallel" ? { slices } : {}) };
    const res = await callTool(tool("swarm_plan"), params, ctx);
    const details = res.details;
    if (!details || typeof details !== "object" || !("dry_run" in details) || !("tasks" in details)) {
      throw new Error(`plan ${name} result has no dry_run/tasks`);
    }
    const dryRun = details.dry_run;
    const tasks = details.tasks;
    if (typeof dryRun !== "boolean" || !Array.isArray(tasks)) {
      throw new Error(`plan ${name} result has the wrong dry_run/tasks shape`);
    }
    expect(dryRun).toBe(true);
    expect(tasks).toHaveLength(expected[name]);
  }
});

test("plan real + reuse", async () => {
  const { ctx } = tmpStore();
  const { tool } = swarm();
  const params = { ...HOTFIX, priority: "P1", acceptance: ["login works", "no regression"] };
  const first = (await callTool(tool("swarm_plan"), params, ctx)).details as { reused?: boolean; tasks: unknown[] };
  expect(first.reused).toBeUndefined();
  expect(first.tasks).toHaveLength(8);
  const rows = await statusRows(tool, ctx, "c-hot");
  expect(rows).toHaveLength(8);
  expect(rows.every((r) => r.state === "PLANNED")).toBe(true);
  expect(rows.find((r) => r.task_id === "H-rca")?.ready).toBe(true);

  const again = (await callTool(tool("swarm_plan"), params, ctx)).details as { reused?: boolean; tasks: unknown[] };
  expect(again.reused).toBe(true);
  expect(await statusRows(tool, ctx, "c-hot")).toHaveLength(8);
});

test("plan dash brief is passed intact", async () => {
  const { ctx } = tmpStore();
  const { tool } = swarm();
  const brief = "--pattern=feature --plan=/etc/passwd";
  const res = await callTool(tool("swarm_plan"), { ...HOTFIX, brief }, ctx);
  const tasks = (res.details as { pattern: string; tasks: Array<{ notes: { brief_excerpt: string } }> });
  expect(tasks.pattern).toBe("hotfix");
  expect(tasks.tasks).toHaveLength(8);
  expect(tasks.tasks[0].notes.brief_excerpt).toBe(brief);
});

test("assignment uses the real canonical script for a leased task without mutating task state", async () => {
  const { ctx, sdir } = tmpStore();
  const { calls, tool } = swarm();
  await callTool(tool("swarm_plan"), HOTFIX, ctx);
  await lease(tool, ctx, "H-rca");
  const db = join(sdir, "tasks.db");
  const before = readFileSync(db);
  const files = readdirSync(sdir, { recursive: true }).sort();
  const start = calls.length;
  const controller = agentCtx(ctx.cwd, "a01-orchestrator", [], false);
  const res = await callTool(tool("swarm_assignment"), { task_id: "H-rca", correlation_id: "c-hot" }, controller);
  expect(calls.slice(start).map((req) => req.script)).toEqual(["orch_assign"]);
  expect(calls[start].args).toEqual([`--root=${ctx.cwd}`, "--task-id=H-rca", "--correlation-id=c-hot"]);
  expect(res.details).toMatchObject({
    status: "ok", assignment_verified: true, task_id: "H-rca", correlation_id: "c-hot", agent_slug: "a14-maintenance",
    assignment: {
      type: "task.assign", source: "A01@runner", target: "A14", correlation_id: "c-hot",
      payload: { task_id: "H-rca", correlation_id: "c-hot", agent_id: "A14", attempt: 1 },
    },
  });
  const details = res.details as { prompt: string; assignment: { sig: string } };
  expect(res.content).toEqual([{ type: "text", text: details.prompt }]);
  const embedded = /# task\.assign \(signed envelope\)\n```json\n([\s\S]*?)\n```/.exec(details.prompt);
  expect(embedded).not.toBeNull();
  expect(JSON.parse(embedded![1])).toEqual(details.assignment);
  expect(details.assignment.sig).toMatch(/^(hmac|ed25519):.+/);
  expect(JSON.stringify(res)).not.toMatch(/SWARM_SIGNING_KEY|SWARM_ED25519_KEY_FILE|PRIVATE KEY/);
  process.env.SWARM_AGENT = "a01-orchestrator";
  const headless = await callTool(tool("swarm_assignment"), { task_id: "H-rca", correlation_id: "c-hot" }, fakeCtx(ctx.cwd));
  expect(headless.details).toMatchObject({ assignment_verified: true, task_id: "H-rca", agent_slug: "a14-maintenance" });
  expect(readFileSync(db)).toEqual(before);
  expect(readdirSync(sdir, { recursive: true }).sort()).toEqual(files);
});

test("assignment rejects missing, wrong-correlation and unleased tasks through the canonical script", async () => {
  const { ctx, sdir } = tmpStore();
  const { tool } = swarm();
  await callTool(tool("swarm_plan"), HOTFIX, ctx);
  const controller = agentCtx(ctx.cwd, "a01-orchestrator", [], false);
  for (const params of [
    { task_id: "missing", correlation_id: "c-hot" },
    { task_id: "H-rca", correlation_id: "different" },
    { task_id: "H-rca", correlation_id: "c-hot" },
  ]) {
    const before = readFileSync(join(sdir, "tasks.db"));
    const err = await rejection(callTool(tool("swarm_assignment"), params, controller));
    expect(["E-INPUT", "E-CONTRACT", "E-POLICY"]).toContain(err.code);
    expect(err.details).toBeUndefined();
    expect(err.message).not.toContain("task.assign");
    expect(readFileSync(join(sdir, "tasks.db"))).toEqual(before);
  }
  await lease(tool, ctx, "H-rca");
  await callTool(tool("swarm_transition"), { task_id: "H-rca", state: "BLOCKED", reason: "lease ended" }, ctx);
  const before = readFileSync(join(sdir, "tasks.db"));
  const ended = await rejection(callTool(tool("swarm_assignment"), { task_id: "H-rca", correlation_id: "c-hot" }, controller));
  expect(ended.code).toBe("E-POLICY");
  expect(readFileSync(join(sdir, "tasks.db"))).toEqual(before);
});

test("assignment denies all non-controller direct callers and controller plan mode before bridge work", async () => {
  const { ctx } = tmpStore();
  const { calls, tool } = swarm();
  const params = { task_id: "H-rca", correlation_id: "c-hot" };
  const contexts = [
    fakeCtx(ctx.cwd),
    ...SWARM_SLUGS.filter((slug) => slug !== "a01-orchestrator").map((slug) => agentCtx(ctx.cwd, slug, [], false)),
    agentCtx(ctx.cwd, "task", [], false),
    agentCtx(ctx.cwd, "a01-orchestrator", [{ type: "mode_change", mode: "plan" }], false),
  ];
  delete process.env.SWARM_AGENT;
  for (const caller of contexts) {
    expect((await rejection(callTool(tool("swarm_assignment"), params, caller))).code).toBe("E-POLICY");
  }
  process.env.SWARM_AGENT = "a01-orchestrator";
  // A session_init without a controller identity must not inherit the top-level controller environment.
  for (const caller of [
    fakeCtx(ctx.cwd, [{ type: "session_init", restrictToolNames: false }]),
    agentCtx(ctx.cwd, "task", [], false),
    agentCtx(ctx.cwd, "a05-backend", [], false),
  ]) {
    expect((await rejection(callTool(tool("swarm_assignment"), params, caller))).code).toBe("E-POLICY");
  }
  expect(calls).toHaveLength(0);
});

test("assignment schema and direct execution admit only the two existing ID-shaped fields", async () => {
  const { ctx } = tmpStore();
  const { calls, tool } = swarm();
  const assignment = tool("swarm_assignment");
  expect(assignment.hidden).toBe(true);
  expect(assignment.loadMode).toBe("essential");
  expect(assignment.parameters.additionalProperties).toBe(false);
  expect(assignment.parameters.required).toEqual(["task_id", "correlation_id"]);
  const props = assignment.parameters.properties as Record<string, { pattern: string }>;
  expect(Object.keys(props).sort()).toEqual(["correlation_id", "task_id"]);
  const controller = agentCtx(ctx.cwd, "a01-orchestrator", [], false);
  for (const key of ["task_id", "correlation_id"]) {
    const pattern = new RegExp(props[key].pattern);
    for (const value of ["", "../escape", "--task-id=other", "/tmp/task", "x".repeat(129)]) {
      expect(pattern.test(value)).toBe(false);
      expect((await rejection(callTool(assignment, { task_id: "T-req", correlation_id: "c1", [key]: value }, controller))).code).toBe("E-INPUT");
    }
  }
  for (const key of ["envelope", "assignment", "code", "agent", "key", "payload", "path", "dry_run"]) {
    expect((await rejection(callTool(assignment, { task_id: "T-req", correlation_id: "c1", [key]: "not-accepted" }, controller))).code).toBe("E-INPUT");
  }
  for (const params of [{}, { task_id: "T-req" }, { correlation_id: "c1" }, { task_id: 1, correlation_id: "c1" }]) {
    expect((await rejection(callTool(assignment, params, controller))).code).toBe("E-INPUT");
  }
  expect(calls).toHaveLength(0);
});

test("assignment rejects and redacts invalid bridge authority, metadata and prompt responses", async () => {
  const { ctx } = tmpStore();
  const setup = swarm();
  await callTool(setup.tool("swarm_plan"), HOTFIX, ctx);
  await lease(setup.tool, ctx, "H-rca");
  const params = { task_id: "H-rca", correlation_id: "c-hot" };
  const controller = agentCtx(ctx.cwd, "a01-orchestrator", [], false);
  const valid = await runPy({
    script: "orch_assign", args: [`--root=${ctx.cwd}`, "--task-id=H-rca", "--correlation-id=c-hot"], cwd: ctx.cwd,
  });
  const variants: Array<{
    exitCode?: 0 | 1;
    result?: Record<string, unknown>;
    envelope?: Record<string, unknown>;
    payload?: Record<string, unknown>;
  }> = [
    { exitCode: 1 },
    { result: { status: "fail" } },
    { result: { assignment_verified: undefined } },
    { result: { assignment_verified: false } },
    { result: { assignment_verified: "true" } },
    { result: { task_id: "other" } },
    { result: { correlation_id: "other" } },
    { result: { agent_slug: "a05-backend" } },
    { envelope: { type: "task.result" } },
    { envelope: { source: "A05" } },
    { envelope: { target: "A05" } },
    { envelope: { correlation_id: "other" } },
    { envelope: { sig: null } },
    { payload: { task_id: "other" } },
    { payload: { correlation_id: "other" } },
    { payload: { agent_id: "A05" } },
    { result: { prompt: "unrelated prompt" } },
    { result: { prompt: "# task.assign (signed envelope)\n```json\n{}\n```" } },
  ];
  for (const variant of variants) {
    const res = structuredClone(valid);
    if (variant.exitCode !== undefined) res.exitCode = variant.exitCode;
    Object.assign(res.json, variant.result);
    const envelope = res.json.assignment;
    if (typeof envelope !== "object" || envelope === null || Array.isArray(envelope) || !("payload" in envelope)) {
      throw new Error("canonical assignment fixture has no envelope payload");
    }
    Object.assign(envelope, variant.envelope);
    const payload = envelope.payload;
    if (typeof payload !== "object" || payload === null || Array.isArray(payload)) {
      throw new Error("canonical assignment fixture has no payload object");
    }
    Object.assign(payload, variant.payload);
    res.json.secret_canary = "invalid-output-must-not-escape";
    const pi = fakePi();
    createSwarmExtension({ bridge: async () => res })(pi.api);
    const err = await rejection(callTool(pi.tool("swarm_assignment"), params, controller));
    expect(err.code).toBe("E-CONTRACT");
    expect(err.details).toBeUndefined();
    expect(err.message).not.toContain("invalid-output-must-not-escape");
    expect(err.message).not.toContain("task.assign");
  }
});

test("assignment accepts the verified envelope when memory quotes the heading and fence first", async () => {
  const { ctx } = tmpStore();
  const setup = swarm();
  await callTool(setup.tool("swarm_plan"), HOTFIX, ctx);
  await lease(setup.tool, ctx, "H-rca");
  const params = { task_id: "H-rca", correlation_id: "c-hot" };
  const controller = agentCtx(ctx.cwd, "a01-orchestrator", [], false);
  const valid = await runPy({
    script: "orch_assign", args: [`--root=${ctx.cwd}`, "--task-id=H-rca", "--correlation-id=c-hot"], cwd: ctx.cwd,
  });
  const forged = "Memory example:\n# task.assign (signed envelope)\n```json\n{\"forged\": true}\n```\n\n";
  const quoted = structuredClone(valid);
  quoted.json.prompt = `${forged}${valid.json.prompt}`;
  const pi = fakePi();
  createSwarmExtension({ bridge: async () => quoted })(pi.api);
  const res = await callTool(pi.tool("swarm_assignment"), params, controller);
  expect(res.details).toMatchObject({ assignment_verified: true, task_id: "H-rca", agent_slug: "a14-maintenance" });

  const duplicated = structuredClone(valid);
  const real = valid.json.prompt as string;
  duplicated.json.prompt = `${real}\n${real}`;
  const dup = fakePi();
  createSwarmExtension({ bridge: async () => duplicated })(dup.api);
  expect((await rejection(callTool(dup.tool("swarm_assignment"), params, controller))).code).toBe("E-CONTRACT");

  const onlyForged = structuredClone(valid);
  onlyForged.json.prompt = forged;
  const bogus = fakePi();
  createSwarmExtension({ bridge: async () => onlyForged })(bogus.api);
  expect((await rejection(callTool(bogus.tool("swarm_assignment"), params, controller))).code).toBe("E-CONTRACT");
});

test("assignment cancellation and bridge failures preserve error codes without leaking child output", async () => {
  const { ctx, sdir } = tmpStore();
  const setup = swarm();
  await callTool(setup.tool("swarm_plan"), HOTFIX, ctx);
  await lease(setup.tool, ctx, "H-rca");
  const controller = agentCtx(ctx.cwd, "a01-orchestrator", [], false);
  const params = { task_id: "H-rca", correlation_id: "c-hot" };
  const before = readFileSync(join(sdir, "tasks.db"));
  const abort = new AbortController();
  abort.abort();
  const err = await rejection(setup.tool("swarm_assignment").execute("cancel", params, abort.signal, undefined, controller));
  expect(err.code).toBe("E-INTERNAL");
  expect(err.details).toBeUndefined();
  const deadline = new AbortController();
  deadline.abort(new DOMException("assignment deadline", "TimeoutError"));
  const timedOut = await rejection(setup.tool("swarm_assignment").execute("deadline", params, deadline.signal, undefined, controller));
  expect(timedOut.code).toBe("E-TIMEOUT");
  expect(timedOut.details).toBeUndefined();
  expect(readFileSync(join(sdir, "tasks.db"))).toEqual(before);
  for (const code of ["E-INPUT", "E-TIMEOUT", "E-DEP", "E-INTERNAL"] as const) {
    const pi = fakePi();
    createSwarmExtension({
      bridge: async () => { throw new SwarmToolError(code, "invalid-output-must-not-escape", { assignment: "invalid-output-must-not-escape" }); },
    })(pi.api);
    const failure = await rejection(callTool(pi.tool("swarm_assignment"), params, controller));
    expect(failure.code).toBe(code);
    expect(failure.details).toBeUndefined();
    expect(failure.message).not.toContain("invalid-output-must-not-escape");
  }
});

test("ingest a leased task's IN_REVIEW result", async () => {
  const { ctx, sdir } = tmpStore();
  const { tool } = swarm();
  await callTool(tool("swarm_plan"), HOTFIX, ctx);
  await lease(tool, ctx, "H-patch");
  expect(stateOf(await statusRows(tool, ctx, "c-hot"), "H-patch")).toBe("IN_PROGRESS");

  const res = await tool("swarm_ingest").execute("toolu_01/../x y", { task_id: "H-patch", result: RESULT }, undefined, undefined, ctx);
  expect(res.content[0].text).toContain("H-patch → IN_REVIEW");
  expect(stateOf(await statusRows(tool, ctx, "c-hot"), "H-patch")).toBe("IN_REVIEW");
  expect(readdirSync(join(sdir, "results"))).toEqual(["ingest-toolu_01_.._x_y.json"]);
  expect(JSON.parse(readFileSync(join(sdir, "results", "ingest-toolu_01_.._x_y.json"), "utf8"))).toEqual(RESULT);
});

test("ingest malformed result rejects with the python taxonomy code", async () => {
  const { ctx } = tmpStore();
  const { tool } = swarm();
  await callTool(tool("swarm_plan"), HOTFIX, ctx);
  await lease(tool, ctx, "H-patch");
  const err = await rejection(callTool(tool("swarm_ingest"), { task_id: "H-patch", result: { task_id: "H-patch" } }, ctx));
  // flagged assumption (03-02): validate_result's "missing state" is observed as E-CONTRACT
  expect(err.code).toBe("E-CONTRACT");
  expect(err.message).toContain("/state: required");
  expect(stateOf(await statusRows(tool, ctx, "c-hot"), "H-patch")).toBe("IN_PROGRESS");
});

test("transition illegal rejects E-CONTRACT and leaves the state", async () => {
  const { ctx } = tmpStore();
  const { tool } = swarm();
  await callTool(tool("swarm_plan"), HOTFIX, ctx);
  const err = await rejection(callTool(tool("swarm_transition"), { task_id: "H-rca", state: "DONE", reason: "skip" }, ctx));
  expect(err.code).toBe("E-CONTRACT");
  expect(err.message).toContain("illegal transition PLANNED → DONE");
  expect(stateOf(await statusRows(tool, ctx, "c-hot"), "H-rca")).toBe("PLANNED");
});

test("transition dry run writes nothing", async () => {
  const { ctx } = tmpStore();
  const { tool } = swarm();
  await callTool(tool("swarm_plan"), HOTFIX, ctx);
  const res = await callTool(tool("swarm_transition"), { task_id: "H-rca", state: "CLAIMED", reason: "x", dry_run: true }, ctx);
  expect(res.content[0].text).toContain("dry-run: would transition H-rca → CLAIMED");
  expect(stateOf(await statusRows(tool, ctx, "c-hot"), "H-rca")).toBe("PLANNED");
});

const PLAN_MODE: SessionEntry[] = [
  { type: "model_change" },
  { type: "custom_message", customType: "plan-mode-context" },
  { type: "message", message: { role: "user" } },
];

test("plan mode store unchanged", async () => {
  const { repo, sdir } = tmpStore();
  const setup = swarm();
  await callTool(setup.tool("swarm_plan"), HOTFIX, fakeCtx(repo));
  await lease(setup.tool, fakeCtx(repo), "H-patch");
  await lease(setup.tool, fakeCtx(repo), "H-rev");
  const db = join(sdir, "tasks.db");
  const sha = () => createHash("sha256").update(readFileSync(db)).digest("hex");
  const before = sha();

  const { calls, tool } = swarm();
  const ctx = fakeCtx(repo, PLAN_MODE);
  const mutations: Array<[string, Record<string, unknown>]> = [
    ["swarm_plan", { ...HOTFIX, prefix: "P", correlation_id: "c-plan-mode" }],
    ["swarm_ingest", { task_id: "H-patch", result: RESULT }],
    ["swarm_transition", { task_id: "H-patch", state: "IN_REVIEW", reason: "x" }],
    ["swarm_gate", { gate: "review", task_id: "H-rev", correlation_id: "c-hot", per_target_findings: { "H-patch": [] } }],
  ];
  for (const [name, params] of mutations) {
    for (let i = 0; i < 2; i++) {
      const err = await rejection(callTool(tool(name), params, ctx));
      expect(err.code).toBe("E-POLICY");
    }
  }
  expect(calls).toHaveLength(0);
  expect(sha()).toBe(before);
  expect(existsSync(join(sdir, "results"))).toBe(false);

  const rows = await statusRows(tool, ctx, "c-hot");
  expect(calls).toHaveLength(1);
  expect(stateOf(rows, "H-patch")).toBe("IN_PROGRESS");
});

test("gate signed rows: one verdict per gate_for target, derived from its findings", async () => {
  const { ctx, sdir } = tmpStore();
  delete process.env.SWARM_AGENT_SESSION; // in-session, swarm_gate is the recorder
  const { tool } = swarm();
  await callTool(tool("swarm_plan"), { brief: "add search", pattern: "feature", risk_class: "low", prefix: "F", correlation_id: "c-feat" }, ctx);
  await lease(tool, ctx, "F-rev");
  const per_target_findings = {
    "F-be": [{ severity: "major", summary: "unparameterised SQL", location: "be/db.py:12" }],
    "F-fe": [{ severity: "minor", summary: "naming nit" }],
    "F-data": [],
  };
  const res = await callTool(tool("swarm_gate"), { gate: "review", task_id: "F-rev", correlation_id: "c-feat", per_target_findings }, agentCtx(ctx.cwd, "a09-reviewer"));
  expect(res.content[0].text.startsWith("FAIL: review gate FAIL")).toBe(true); // exit 1 is returned, not thrown
  expect((res.details as { recorded: string[] }).recorded).toEqual(["F-be", "F-data", "F-fe"]);

  const expected: Record<string, string> = { "F-be": "fail", "F-fe": "pass", "F-data": "pass" };
  for (const [target, verdict] of Object.entries(expected)) {
    const h = runPython("orch_status.py", [`--history=${target}`], { SWARM_DIR: sdir });
    expect(h.code, h.stderr).toBe(0);
    const row = (h.json.verdicts as Record<string, { verdict: string; agent_id: string; envelope_json: string }>).review;
    expect(row.verdict).toBe(verdict);
    const env = JSON.parse(row.envelope_json) as { sig: string; payload: { gate_task: string; task_id: string } };
    expect(env.sig).toMatch(/^(hmac|ed25519):.+/);
    expect(env.payload.task_id).toBe(target);
    expect(env.payload.gate_task).toBe("F-rev");
  }
});

/** A nested JSON field, narrowed step by step instead of asserting a shape; absent anywhere on the path → undefined. */
function jsonAt(value: unknown, ...path: string[]): unknown {
  let cur = value;
  for (const key of path) {
    if (typeof cur !== "object" || cur === null || !Object.hasOwn(cur, key)) return undefined;
    cur = Object.getOwnPropertyDescriptor(cur, key)?.value;
  }
  return cur;
}

test("gate dry run end to end: canned pass reaches the agent and satisfies no real gate", async () => {
  const { ctx, sdir } = tmpStore();
  delete process.env.SWARM_AGENT_SESSION; // in-session, swarm_gate is the recorder
  const { tool } = swarm();
  await callTool(tool("swarm_plan"), { brief: "add search", pattern: "feature", risk_class: "low", prefix: "F", correlation_id: "c-feat" }, ctx);
  await lease(tool, ctx, "F-rev");
  const reviewer = agentCtx(ctx.cwd, "a09-reviewer");
  const targets = ["F-be", "F-data", "F-fe"];
  const reviewOf = (target: string) => {
    const h = runPython("orch_status.py", [`--history=${target}`], { SWARM_DIR: sdir });
    expect(h.code, h.stderr).toBe(0);
    return { verdict: jsonAt(h.json, "verdicts", "review"), missing: jsonAt(h.json, "missing_gate_reasons", "review") };
  };

  // a major finding would fail F-be for real; under dry_run the script's canned pass ignores it
  const per_target_findings = { "F-be": [{ severity: "major", summary: "unparameterised SQL" }], "F-fe": [], "F-data": [] };
  const dry = await callTool(tool("swarm_gate"), { gate: "review", task_id: "F-rev", correlation_id: "c-feat", per_target_findings, dry_run: true }, reviewer);
  expect(dry.content[0].text).toBe("dry-run: canned pass verdict");
  expect(jsonAt(dry.details, "recorded")).toEqual([]);
  for (const t of targets) expect(reviewOf(t)).toEqual({ verdict: undefined, missing: "absent" });

  // the same call without dry_run records a signed row on every target, so the store was writable all along
  const real = await callTool(tool("swarm_gate"), { gate: "review", task_id: "F-rev", correlation_id: "c-feat", per_target_findings }, reviewer);
  expect(jsonAt(real.details, "recorded")).toEqual(targets);
  expect(reviewOf("F-be").missing).toBe("fail");
  expect(reviewOf("F-fe").missing).toBeUndefined();
});

/** Real pytest, canonical qa_gate and a leased temporary Task Store; no synthetic gate replies. */
async function qualityFixture(delayS = 0.05) {
  const store = tmpStore();
  // Match the isolated controller fixtures above: the test key records rows, not a keyless agent session.
  delete process.env.SWARM_AGENT_SESSION;
  process.env.SWARM_PURE_PYTHON = "1";
  process.env.SWARM_ROOT = REPO_ROOT;
  // A real runner deadline needs the child interpreter's clock; fake JS timers cannot drive it.
  writeFileSync(join(store.repo, "pytest.ini"), "[pytest]\n");
  writeFileSync(join(store.repo, "test_deadline.py"), [
    "import time",
    "from pathlib import Path",
    "def test_bounded_work():",
    "    Path('runner-started').write_text('started')",
    `    time.sleep(${delayS})`,
    "    assert 2 + 2 == 4",
    "    Path('runner-finished').write_text('finished')",
    "",
  ].join("\n"));
  const setup = swarm();
  await callTool(setup.tool("swarm_plan"), { ...HOTFIX, risk_class: "medium" }, store.ctx);
  await lease(setup.tool, store.ctx, "H-qa");
  return {
    ...store, ...setup,
    owner: agentCtx(store.repo, "a08-qa"),
    params: { gate: "quality", task_id: "H-qa", correlation_id: "c-hot" },
  };
}

function qualityHistory(sdir: string, target = "H-patch") {
  const h = runPython("orch_status.py", [`--history=${target}`], { SWARM_DIR: sdir });
  expect(h.code, h.stderr).toBe(0);
  return {
    row: jsonAt(h.json, "verdicts", "quality"),
    missing: jsonAt(h.json, "missing_gate_reasons", "quality"),
  };
}

async function waitForRunner(file: string): Promise<void> {
  // Real subprocess readiness: fake timers cannot drive a child interpreter or its filesystem writes.
  const end = Date.now() + 5_000;
  while (!existsSync(file) && Date.now() < end) await Bun.sleep(25);
  expect(existsSync(file)).toBe(true);
}

test("quality timeout validation rejects before findings files, bridge calls or Task Store effects", async () => {
  const { ctx, sdir } = tmpStore();
  const { calls, tool } = swarm();
  await callTool(tool("swarm_plan"), { ...HOTFIX, risk_class: "medium" }, ctx);
  const db = readFileSync(join(sdir, "tasks.db"));
  const files = readdirSync(sdir, { recursive: true }).sort();
  const start = calls.length;
  const base = { task_id: "H-qa", correlation_id: "c-hot", per_target_findings: { "H-patch": [] } };
  const invalid: unknown[] = [
    undefined, null, false, true, "", "1", "7200", NaN, Infinity, -Infinity,
    0, -1, 0.5, 1.5, 7200.5, 7201, {}, [],
  ];
  for (const timeout_s of invalid) {
    const err = await rejection(callTool(tool("swarm_gate"), { ...base, gate: "quality", timeout_s }, agentCtx(ctx.cwd, "a08-qa")));
    expect(err.code).toBe("E-INPUT");
  }
  for (const [gate, slug] of [["review", "a09-reviewer"], ["security", "a10-security"], ["release", "a12-release"]]) {
    for (const timeout_s of [1, 7200, null, undefined]) {
      const err = await rejection(callTool(tool("swarm_gate"), { ...base, gate, timeout_s }, agentCtx(ctx.cwd, slug)));
      expect(err.code).toBe("E-INPUT");
    }
  }
  expect(calls.length).toBe(start);
  expect(readFileSync(join(sdir, "tasks.db"))).toEqual(db);
  expect(readdirSync(sdir, { recursive: true }).sort()).toEqual(files);
  expect(existsSync(join(sdir, "results"))).toBe(false);
  expect(existsSync(join(sdir, "verdicts"))).toBe(false);
  expect(qualityHistory(sdir)).toEqual({ row: undefined, missing: "absent" });
});

test("quality timeout does not weaken owner-only or plan-mode protection", async () => {
  const { ctx, sdir } = tmpStore();
  const { calls, tool } = swarm();
  await callTool(tool("swarm_plan"), HOTFIX, ctx);
  const db = readFileSync(join(sdir, "tasks.db"));
  const files = readdirSync(sdir, { recursive: true }).sort();
  const start = calls.length;
  for (const owner of [
    ...SWARM_SLUGS.filter((slug) => slug !== "a08-qa").map((slug) => agentCtx(ctx.cwd, slug)),
    agentCtx(ctx.cwd, "task"),
    agentCtx(ctx.cwd, "a08-qa", PLAN_MODE),
  ]) {
    for (const timeout_s of [1, 7200]) {
      const err = await rejection(callTool(tool("swarm_gate"), {
        gate: "quality", task_id: "H-qa", correlation_id: "c-hot", timeout_s,
        per_target_findings: { "H-patch": [] },
      }, owner));
      expect(err.code).toBe("E-POLICY");
    }
  }
  expect(calls.length).toBe(start);
  expect(readFileSync(join(sdir, "tasks.db"))).toEqual(db);
  expect(readdirSync(sdir, { recursive: true }).sort()).toEqual(files);
});

test("quality accepts both deadline boundaries with real signed canonical results and unchanged risk tiers", async () => {
  const { tool, owner, params, sdir, ctx, repo } = await qualityFixture();
  for (const timeout_s of [1, 7200]) {
    const res = await callTool(tool("swarm_gate"), { ...params, timeout_s }, owner);
    expect(res.details).toMatchObject({
      status: "ok", verdict: "pass", risk_class: "medium", recorded: ["H-patch"],
      runs: { unit: "pass", integration: "pass", e2e: "skipped:not-selected", perf: "skipped:not-selected" },
      executed: [{ runner: "pytest", returncode: 0, passed: 1, failed: 0 }],
    });
    expect(existsSync(join(repo, "runner-finished"))).toBe(true);
    const row = qualityHistory(sdir).row;
    expect(jsonAt(row, "verdict")).toBe("pass");
    const envelope = JSON.parse(String(jsonAt(row, "envelope_json")));
    expect(envelope.sig).toMatch(/^(hmac|ed25519):.+/);
    expect(envelope.payload).toMatchObject({ task_id: "H-patch", gate_task: "H-qa", gate: "quality" });
    expect(stateOf(await statusRows(tool, ctx, "c-hot"), "H-qa")).toBe("IN_PROGRESS");
  }
}, 15_000);

test("quality short runner timeout is fail-closed; a later omitted deadline runs normally", async () => {
  const { tool, owner, params, sdir, ctx, repo } = await qualityFixture(1.5);
  const err = await rejection(callTool(tool("swarm_gate"), { ...params, timeout_s: 1 }, owner));
  // subprocess.TimeoutExpired is the existing canonical Python E-INTERNAL contract, not a signed failure.
  expect(err.code).toBe("E-INTERNAL");
  expect(err.message).toContain("TimeoutExpired");
  expect(existsSync(join(repo, "runner-started"))).toBe(true);
  expect(existsSync(join(repo, "runner-finished"))).toBe(false);
  expect(qualityHistory(sdir)).toEqual({ row: undefined, missing: "absent" });
  expect(existsSync(join(sdir, "verdicts", "H-qa.quality.json"))).toBe(false);
  expect(stateOf(await statusRows(tool, ctx, "c-hot"), "H-qa")).toBe("IN_PROGRESS");
  const normal = await callTool(tool("swarm_gate"), params, owner);
  expect(normal.details).toMatchObject({
    status: "ok", verdict: "pass", recorded: ["H-patch"],
    executed: [{ runner: "pytest", returncode: 0, passed: 1, failed: 0 }],
  });
  expect(existsSync(join(repo, "runner-finished"))).toBe(true);
  expect(jsonAt(qualityHistory(sdir).row, "verdict")).toBe("pass");
}, 15_000);

test("quality concurrent deadlines isolate the timed-out target from the successful invocation", async () => {
  const { tool, owner, params, ctx, sdir } = await qualityFixture(1.5);
  await callTool(tool("swarm_plan"), { ...HOTFIX, risk_class: "medium", prefix: "J", correlation_id: "c-other" }, ctx);
  await lease(tool, ctx, "J-qa");
  const [short, longer] = await Promise.all([
    rejection(callTool(tool("swarm_gate"), { ...params, timeout_s: 1 }, owner)),
    callTool(tool("swarm_gate"), {
      gate: "quality", task_id: "J-qa", correlation_id: "c-other", timeout_s: 5,
    }, owner),
  ]);
  expect(short.code).toBe("E-INTERNAL");
  expect(short.message).toContain("TimeoutExpired");
  expect(longer.details).toMatchObject({ status: "ok", verdict: "pass", recorded: ["J-patch"] });
  expect(qualityHistory(sdir)).toEqual({ row: undefined, missing: "absent" });
  expect(jsonAt(qualityHistory(sdir, "J-patch").row, "verdict")).toBe("pass");
  expect(existsSync(join(sdir, "verdicts", "H-qa.quality.json"))).toBe(false);
  expect(stateOf(await statusRows(tool, ctx, "c-hot"), "H-qa")).toBe("IN_PROGRESS");
  expect(stateOf(await statusRows(tool, ctx, "c-other"), "J-qa")).toBe("IN_PROGRESS");
}, 15_000);

test("quality explicit deadlines preserve canonical correlation and ended-lease refusal without rows", async () => {
  const { tool, owner, params, ctx, sdir } = await qualityFixture();
  const wrongCorr = await rejection(callTool(tool("swarm_gate"), { ...params, correlation_id: "wrong", timeout_s: 7200 }, owner));
  expect(wrongCorr.code).toBe("E-POLICY");
  expect(qualityHistory(sdir)).toEqual({ row: undefined, missing: "absent" });
  expect(existsSync(join(sdir, "verdicts", "H-qa.quality.json"))).toBe(false);
  await callTool(tool("swarm_transition"), { task_id: "H-qa", state: "BLOCKED", reason: "lease ended" }, ctx);
  const ended = await rejection(callTool(tool("swarm_gate"), { ...params, timeout_s: 7200 }, owner));
  expect(ended.code).toBe("E-POLICY");
  expect(qualityHistory(sdir)).toEqual({ row: undefined, missing: "absent" });
  expect(existsSync(join(sdir, "verdicts", "H-qa.quality.json"))).toBe(false);
  expect(stateOf(await statusRows(tool, ctx, "c-hot"), "H-qa")).toBe("BLOCKED");
}, 15_000);

test("quality cancellation and an earlier session deadline kill real runner work without verdict rows", async () => {
  const { tool, owner, params, ctx, sdir, repo } = await qualityFixture(30);
  for (const reason of [undefined, new DOMException("shorter session deadline", "TimeoutError")]) {
    if (existsSync(join(repo, "runner-started"))) unlinkSync(join(repo, "runner-started"));
    const controller = new AbortController();
    const work = rejection(callTool(tool("swarm_gate"), { ...params, timeout_s: 7200 }, owner, controller.signal));
    try {
      await waitForRunner(join(repo, "runner-started"));
      controller.abort(reason);
      const err = await work;
      expect(err.code).toBe(reason === undefined ? "E-INTERNAL" : "E-TIMEOUT");
      expect(qualityHistory(sdir)).toEqual({ row: undefined, missing: "absent" });
      expect(existsSync(join(sdir, "verdicts", "H-qa.quality.json"))).toBe(false);
      expect(existsSync(join(repo, "runner-finished"))).toBe(false);
      expect(stateOf(await statusRows(tool, ctx, "c-hot"), "H-qa")).toBe("IN_PROGRESS");
    } finally {
      controller.abort();
      await work;
    }
  }
}, 15_000);

test("quality rechecks a lease ended while the bounded real runner was in flight", async () => {
  const { tool, owner, params, ctx, sdir, repo } = await qualityFixture(1.5);
  const controller = new AbortController();
  const work = rejection(callTool(tool("swarm_gate"), { ...params, timeout_s: 5 }, owner, controller.signal));
  try {
    await waitForRunner(join(repo, "runner-started"));
    await callTool(tool("swarm_transition"), { task_id: "H-qa", state: "BLOCKED", reason: "lease ended during QA" }, ctx);
    const err = await work;
    expect(err.code).toBe("E-POLICY");
    expect(existsSync(join(repo, "runner-finished"))).toBe(true);
    expect(qualityHistory(sdir)).toEqual({ row: undefined, missing: "absent" });
    expect(existsSync(join(sdir, "verdicts", "H-qa.quality.json"))).toBe(false);
    expect(stateOf(await statusRows(tool, ctx, "c-hot"), "H-qa")).toBe("BLOCKED");
  } finally {
    controller.abort();
    await work;
  }
}, 15_000);

test("quality outer supervision bounds a real delayed child group including its cleanup margin", async () => {
  const { tool, owner, params, ctx, sdir, repo } = await qualityFixture();
  // Same isolated runtime-fixture pattern as bridge.test.ts, but no canned JSON or argv echo:
  // if supervision fails, this finite child finishes and delegates to the REAL canonical gate.
  // Real process-group timers cannot be advanced with fake JS timers; only this test waits out the 60s margin.
  const runtime = tmpDir("swarm-omp-delayed-runtime-");
  mkdirSync(join(runtime, "scripts"));
  writeFileSync(join(runtime, "scripts", "orch_plan.py"), "");
  const pids = join(repo, "delayed-pids.json");
  writeFileSync(join(runtime, "scripts", "qa_gate.py"), [
    "import json, os, runpy, signal, subprocess, sys",
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(65)'])",
    "def stop(signum, frame):",
    "    child.terminate()",
    "    child.wait()",
    "    raise SystemExit(143)",
    "signal.signal(signal.SIGTERM, stop)",
    `with open(${JSON.stringify(pids)}, 'w') as f:`,
    "    json.dump({'parent': os.getpid(), 'child': child.pid}, f)",
    "child.wait()",
    `sys.path.insert(0, ${JSON.stringify(REPO_ROOT)})`,
    `runpy.run_path(${JSON.stringify(join(REPO_ROOT, "scripts", "qa_gate.py"))}, run_name='__main__')`,
    "",
  ].join("\n"));
  process.env.SWARM_ROOT = runtime;
  const controller = new AbortController();
  const start = Date.now();
  const work = rejection(callTool(tool("swarm_gate"), { ...params, timeout_s: 1 }, owner, controller.signal));
  try {
    await waitForRunner(pids);
    const err = await work;
    expect(err.code).toBe("E-TIMEOUT");
    // Not merely argv forwarding: a real process outlives the one-second runner budget,
    // survives for its cleanup margin, and is then killed rather than recording success.
    expect(Date.now() - start).toBeGreaterThanOrEqual(60_000);
    const ids = JSON.parse(readFileSync(pids, "utf8")) as { parent: number; child: number };
    for (const pid of [ids.parent, ids.child]) expect(() => process.kill(pid, 0)).toThrow();
    expect(existsSync(join(repo, "runner-started"))).toBe(false);
    expect(qualityHistory(sdir)).toEqual({ row: undefined, missing: "absent" });
    expect(existsSync(join(sdir, "verdicts", "H-qa.quality.json"))).toBe(false);
    process.env.SWARM_ROOT = REPO_ROOT;
    expect(stateOf(await statusRows(tool, ctx, "c-hot"), "H-qa")).toBe("IN_PROGRESS");
  } finally {
    controller.abort();
    await work;
  }
}, 75_000);

function statusTool() {
  const pi = fakePi();
  createSwarmExtension({ bridge: runPy })(pi.api);
  return pi.tool("swarm_status");
}

function plan(repo: string, swarmDir: string | undefined) {
  const r = runPython(
    "orch_plan.py",
    [`--root=${repo}`, "--brief-text=fix the login bug", "--pattern=hotfix", "--risk-class=low", "--prefix=H"],
    { SWARM_DIR: swarmDir, SWARM_TASK_ID: undefined, SWARM_CORRELATION_ID: undefined },
  );
  expect(r.code, r.stderr).toBe(0);
  return r.json;
}

test("status tracer", async () => {
  const repo = gitRepo();
  const sdir = tmpDir("swarm-omp-dir-");
  plan(repo, sdir);
  process.env.SWARM_DIR = sdir;
  const res = await callTool(statusTool(), {}, fakeCtx(repo));
  const details = res.details as { status: string; tasks: Array<{ task_id: string }> };
  expect(details.status).toBe("ok");
  expect(details.tasks.length).toBeGreaterThan(0);
  expect(details.tasks[0].task_id.startsWith("H")).toBe(true);
  expect(res.content[0].text).toContain("TASK");
});

test("status db path", async () => {
  const repo = gitRepo("sub/deeper");
  delete process.env.SWARM_DIR;
  plan(repo, undefined);
  const tool = statusTool();
  const fromRoot = (await callTool(tool, {}, fakeCtx(repo))).details as { db: string; swarm_dir: string };
  const fromSub = (await callTool(tool, {}, fakeCtx(`${repo}/sub/deeper`))).details as { db: string };
  expect(fromRoot.db).toBe(`${repo}/.swarm/tasks.db`);
  expect(fromRoot.db.endsWith("/.swarm/tasks.db")).toBe(true);
  expect(fromSub.db).toBe(fromRoot.db);
  expect(fromRoot.swarm_dir).toBe(`${repo}/.swarm`);
});

test("status env wins", async () => {
  const repo = gitRepo();
  const other = tmpDir("swarm-omp-other-");
  process.env.SWARM_DIR = other;
  const res = await callTool(statusTool(), {}, fakeCtx(repo));
  const details = res.details as { db: string; swarm_dir: string; tasks: unknown[] };
  expect(details.swarm_dir).toBe(other);
  expect(details.db).toBe(`${other}/tasks.db`);
  expect(details.tasks).toEqual([]);
  expect(res.content[0].text).toContain(`db: ${other}/tasks.db`);
});

/** A copy of the live registry in which `be_route_norm` is a WRITE tool, so plan mode has something to refuse. */
function registryWithWriteTool(): string {
  const registry = JSON.parse(readFileSync(join(import.meta.dir, "..", "..", "agents.json"), "utf8"));
  for (const agent of registry.agents) {
    for (const tool of agent.registered_tools) if (tool.name === "be_route_norm") tool.side_effect_tier = "WRITE";
  }
  const file = join(tmpDir("swarm-omp-registry-"), "agents.json");
  writeFileSync(file, JSON.stringify(registry));
  return file;
}

test("swarm_catalog lists the session agent's tools with tier and timeout; swarm_tool_call returns the registry object", async () => {
  const { repo } = tmpStore();
  const { calls, tool } = swarm();
  const ctx = agentCtx(repo, "a05-backend");

  const listed = await callTool(tool("swarm_catalog"), { kind: "tools", query: "route" }, ctx);
  const rows = (listed.details as { tools: Array<{ name: string; tier: string; timeout_s: number }> }).tools;
  expect(rows.map((r) => r.name)).toContain("be_route_norm");
  expect(rows.every((r) => r.tier === "READ" && r.timeout_s > 0)).toBe(true);
  expect((listed.details as { caller: string }).caller).toBe("A05");

  const ok = await callTool(tool("swarm_tool_call"), { name: "be_route_norm", input: { path: "/users/:id/" } }, ctx);
  expect(ok.details).toEqual({ state: "SUCCESS", normalized: "/users/{}" });

  // a tool of another lane: the registry answers UNAUTHORIZED (exit 1 is data, not a thrown error)
  const denied = await callTool(tool("swarm_tool_call"), { name: "req_gwt_check", input: { text: "x" } }, ctx);
  expect(denied.details).toMatchObject({ state: "UNAUTHORIZED", caller: "A05", target: "req_gwt_check" });
  const bad = await callTool(tool("swarm_tool_call"), { name: "be_route_norm", input: {} }, ctx);
  expect(bad.details).toMatchObject({ state: "INVALID_INPUT", field: "path" });
  expect(calls).toHaveLength(4);
});

test("plan mode: swarm_tool_call refuses a WRITE tool before it runs, a READ tool and swarm_catalog still work", async () => {
  const { repo } = tmpStore();
  process.env.SWARM_AGENTS_FILE = registryWithWriteTool();
  const { tool } = swarm();
  const planned = agentCtx(repo, "a05-backend", PLAN_MODE);
  const err = await rejection(callTool(tool("swarm_tool_call"), { name: "be_route_norm", input: { path: "/a/" } }, planned));
  expect(err.code).toBe("E-POLICY");
  const read = await callTool(tool("swarm_tool_call"), { name: "be_route_collision", input: { routes: [{ method: "GET", path: "/a" }] } }, planned);
  expect((read.details as { state: string }).state).toBe("SUCCESS");
  const listed = await callTool(tool("swarm_catalog"), { query: "route_norm" }, planned);
  expect((listed.details as { tools: Array<{ tier: string }> }).tools[0].tier).toBe("WRITE");
  // outside plan mode the same WRITE tool runs
  const run = await callTool(tool("swarm_tool_call"), { name: "be_route_norm", input: { path: "/a/" } }, agentCtx(repo, "a05-backend"));
  expect((run.details as { state: string }).state).toBe("SUCCESS");
});
