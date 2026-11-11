/**
 * `/swarm <brief>` (ORCH-03, D-11): the ordered plan → dispatch → wait → report log through the registered command,
 * with a recording bridge; usage and conflict paths never dispatch.
 */
import { beforeEach, expect, spyOn, test } from "bun:test";
import { type Bridge, type BridgeRequest, type BridgeResult, runPy, SwarmToolError } from "../src/bridge.ts";
import { HOLD_ENV, holdTimeoutMs, parseSwarmArgs, swarmCommand, type SwarmCommandOptions, swarmCorrelationId } from "../src/commands.ts";
import { DISPATCH_MARKER } from "../src/hooks.ts";
import { createSwarmExtension } from "../src/index.ts";
import type { SessionEntry } from "../src/omp-api.ts";
import {
  type CallLog,
  callTool,
  commandCtx,
  ephemeralSigningKey,
  fakeCtx,
  fakePi,
  fakeTurn,
  gitRepo,
  isolateEnv,
  REPO_ROOT,
  runPython,
  type ScriptedTurn,
  tmpDir,
  withOmpArgv,
} from "./helpers.ts";

isolateEnv("SWARM_DIR", "SWARM_ROOT", "SWARM_AGENT", "SWARM_CHILD", "SWARM_AGENT_SESSION", "SWARM_TASK_ID", "SWARM_CORRELATION_ID",
  "SWARM_SIGNING_KEY", "SWARM_ED25519_KEY", "SWARM_ED25519_KEY_FILE", "SWARM_ALLOW_INSECURE_DEV_KEY", "SWARM_REQUIRE_KEY", HOLD_ENV);

const CORR = "c0ffee01-2222-4333-8444-555555555555";
const BRIEF = "add a /health endpoint";
const BRIEF_CORR = swarmCorrelationId({ brief: BRIEF, pattern: "feature", risk_class: "medium" });
const PLAN: BridgeResult = { exitCode: 0, json: {}, swarmDir: "" };

let fixtureSigningKey = "";
// Every dispatch fixture comes from canonical Python signing with test-owned key material, not a fake signature.
beforeEach(() => {
  fixtureSigningKey = ephemeralSigningKey();
  process.env.SWARM_SIGNING_KEY = fixtureSigningKey;
  for (const key of ["SWARM_ROOT", "SWARM_AGENT", "SWARM_CHILD", "SWARM_AGENT_SESSION", "SWARM_TASK_ID", "SWARM_CORRELATION_ID", "SWARM_ED25519_KEY", "SWARM_ED25519_KEY_FILE", "SWARM_ALLOW_INSECURE_DEV_KEY"]) {
    delete process.env[key];
  }
  process.env.SWARM_REQUIRE_KEY = "1";
  process.env.SWARM_DIR = tmpDir("swarm-omp-fixture-");
  const planned = runPython("orch_plan.py", [`--root=${gitRepo()}`, `--brief-text=${BRIEF}`, `--correlation-id=${CORR}`], {});
  if (planned.code !== 0 || planned.json.execution_assignment_verified !== true) throw new Error("canonical signed planning fixture failed");
  PLAN.json = planned.json;
});
const HINT = `prefix Tc0ff already used by correlation ${CORR} with a different brief; pass a new --correlation-id or --prefix for this brief`;
const PLAN_MODE: SessionEntry[] = [{ type: "mode_change", mode: "plan" }];

/** A session through the factory: one ordered log shared by the bridge, sendUserMessage, notify and waitForIdle. */
function session(outcome: BridgeResult | Error = PLAN) {
  const log: CallLog = [];
  const bridge: Bridge = async (req) => {
    log.push({ call: "bridge", req });
    if (outcome instanceof Error) throw outcome;
    return outcome;
  };
  const pi = fakePi({ sendUserMessage: (text) => void log.push({ call: "sendUserMessage", text }) });
  pi.load(createSwarmExtension({ bridge }));
  return { log, pi, swarm: pi.command("swarm") };
}

const calls = (log: CallLog) => log.map((e) => e.call);
/** The five info notifications the startup step reports with a UI before planning (lifecycle, LOADING, INITIALIZING, trigger). */
const STARTUP = ["notify", "notify", "notify", "notify", "notify"];
const bridgeReq = (log: CallLog) => log.find((e) => e.call === "bridge")?.req as BridgeRequest | undefined;
const dispatchText = (log: CallLog) => log.find((e) => e.call === "sendUserMessage")?.text as string | undefined;
/** The task.assign payload line of the dispatch prompt (the one JSON object line). */
function payload(text: string): Record<string, unknown> {
  const line = text.split("\n").find((l) => l.startsWith("{"));
  if (!line) throw new Error(`no payload line in:\n${text}`);
  return JSON.parse(line);
}

/** Consumer-side verification uses only this test's signer, never a host key or private-reference loader. */
function dispatchEvidence(envelope: Record<string, unknown>, correlationId: string): { verified: boolean; authorized: boolean } {
  const verification = Bun.spawnSync(["python3", "-c", `
import json, sys
from swarm.envelope import verify_envelope

envelope = json.load(sys.stdin)
assignment = envelope.get("payload", {})
inputs = assignment.get("inputs", [])
plan = inputs[0] if len(inputs) == 1 else {}
tasks = plan.get("tasks", [])
complete = {task["task_id"] for task in tasks if task["state"] in ("IN_REVIEW", "APPROVED", "DONE")}
ready = {task["task_id"] for task in tasks
         if task["state"] in ("CREATED", "VALIDATED", "PLANNED", "RETRY")
         and all(dependency in complete for dependency in task["depends_on"])}
assigned_ready = assignment.get("ready_tasks", [])
authorized = (
    envelope.get("schema") == "swarm.v1.task.assign"
    and envelope.get("type") == "task.assign"
    and envelope.get("source") == "A01@controller"
    and envelope.get("target") == assignment.get("agent_id") == "A01"
    and assignment.get("capability") == "plan.execute"
    and envelope.get("correlation_id") == assignment.get("correlation_id") == assignment.get("task_id") == plan.get("correlation_id") == sys.argv[1]
    and assignment.get("risk_class") == envelope.get("risk_class")
    and isinstance(assignment.get("budget"), dict)
    and isinstance(assignment.get("acceptance"), list)
    and all(isinstance(criterion, str) for criterion in assignment["acceptance"])
    and bool(tasks)
    and len(assigned_ready) == len(set(assigned_ready)) == len(ready)
    and set(assigned_ready) == ready
)
print(json.dumps({"verified": verify_envelope(envelope), "authorized": authorized}))
`, correlationId], {
    cwd: REPO_ROOT,
    env: { PATH: process.env.PATH ?? "/usr/bin:/bin", SWARM_SIGNING_KEY: fixtureSigningKey, SWARM_REQUIRE_KEY: "1", SWARM_PURE_PYTHON: "1" },
    stdin: Buffer.from(JSON.stringify(envelope)),
    stdout: "pipe",
    stderr: "pipe",
  });
  expect(verification.exitCode).toBe(0);
  expect(verification.stderr.toString()).toBe("");
  return JSON.parse(verification.stdout.toString());
}
/** The task ids in a swarm_status result. */
function statusTaskIds(details: unknown): string[] {
  if (!details || typeof details !== "object" || !("tasks" in details) || !Array.isArray(details.tasks)) throw new Error("no tasks in status");
  return details.tasks.map((t: unknown) => {
    if (!t || typeof t !== "object" || !("task_id" in t) || typeof t.task_id !== "string") throw new Error("task without task_id");
    return t.task_id;
  });
}

test("kickoff dispatches a cryptographically valid controller assignment for the canonical plan", async () => {
  const repo = gitRepo();
  const { log, swarm } = session();
  await swarm.handler("add a /health endpoint", commandCtx(repo, [], { log }));

  const text = dispatchText(log) ?? "";
  expect(text.startsWith(DISPATCH_MARKER)).toBe(true);
  expect(text).toContain("a01-orchestrator");
  expect(dispatchEvidence(payload(text), CORR)).toEqual({ verified: true, authorized: true });
  expect(calls(log)).toContain("waitForIdle");
  expect(log.at(-1)?.level).toBe("info");
});

test("no UI: the same authorized assignment dispatches and is awaited without notification", async () => {
  const repo = gitRepo();
  const { log, swarm } = session();
  await swarm.handler("add a /health endpoint", commandCtx(repo, [], { log, hasUI: false }));
  expect(dispatchEvidence(payload(dispatchText(log) ?? ""), CORR)).toEqual({ verified: true, authorized: true });
  expect(calls(log)).toContain("waitForIdle");
  expect(calls(log)).not.toContain("notify");
});

/** The dispatch user message as omp records it on the branch once the turn starts. */
const DISPATCH_ENTRY: SessionEntry = {
  type: "message",
  message: { role: "user", content: [{ type: "text", text: `${DISPATCH_MARKER} AgentSwarm plan ${CORR} is ready` }] },
};

/** A session whose fake sendUserMessage starts a scripted turn; `opts` reach swarmCommand's DI options directly. */
function turnSession(script: ScriptedTurn, opts?: SwarmCommandOptions) {
  const log: CallLog = [];
  const entries: SessionEntry[] = [];
  const turn = fakeTurn(script, entries, log);
  const bridge: Bridge = async (req) => {
    log.push({ call: "bridge", req });
    return PLAN;
  };
  const pi = fakePi({
    sendUserMessage: (text) => {
      log.push({ call: "sendUserMessage", text });
      turn.start();
    },
  });
  pi.load(createSwarmExtension({ bridge }));
  const swarm = opts ? swarmCommand(pi.api, bridge, opts) : pi.command("swarm");
  const ctx = commandCtx(gitRepo(), entries, { log, isIdle: turn.isIdle });
  return { log, turn, swarm, ctx };
}

test("async start: waitForIdle resolves at once while the turn is in flight, yet the handler holds until isIdle flips true", async () => {
  const { log, turn, swarm, ctx } = turnSession({ startMs: 30 });
  let settled = false;
  const run = swarm.handler(BRIEF, ctx).then(() => void (settled = true));

  await turn.observed(); // the handler polled with the turn in flight; a handler that did not hold would have settled
  expect(turn.ended()).toBe(false);
  expect(settled).toBe(false);
  expect(calls(log)).toEqual([...STARTUP, "bridge", "sendUserMessage", "turn-start"]); // waitForIdle not yet called

  turn.finish(); // the turn ends on this call, not on a timer racing the assertions above
  await run;
  expect(turn.ended()).toBe(true);
  expect(calls(log)).toEqual([...STARTUP, "bridge", "sendUserMessage", "turn-start", "turn-end", "waitForIdle", "notify"]);
  expect(log.at(-1)?.message).toContain(`plan ${CORR} dispatched`);
});

test("branch entry only: a turn that runs between two polls is seen through the dispatch message on the branch", async () => {
  const { log, swarm, ctx } = turnSession({ entryMs: 20, entry: DISPATCH_ENTRY });
  await swarm.handler(BRIEF, ctx);
  expect(calls(log)).toEqual([...STARTUP, "bridge", "sendUserMessage", "waitForIdle", "notify"]);
  expect(log.at(-1)?.level).toBe("info");
});

test("branch entry: a dispatch message that was already on the branch does not count as this turn's start", async () => {
  const { log, swarm, ctx } = turnSession({}, { startTimeoutMs: 100 });
  ctx.sessionManager.getBranch().push(DISPATCH_ENTRY); // an earlier /swarm of the same brief
  await swarm.handler(BRIEF, ctx);
  expect(calls(log)).toEqual([...STARTUP, "bridge", "sendUserMessage", "notify"]);
  expect(log.at(-1)?.level).toBe("error");
});

test("never starts: after the start cap the handler reports the failure at level error and never reports success", async () => {
  const { log, swarm, ctx } = turnSession({}, { startTimeoutMs: 100 });
  const t0 = Date.now();
  await swarm.handler(BRIEF, ctx);
  expect(Date.now() - t0).toBeLessThan(1000);
  expect(calls(log)).toEqual([...STARTUP, "bridge", "sendUserMessage", "notify"]);
  const last = log.at(-1);
  expect(last?.level).toBe("error");
  expect(String(last?.message)).toContain("dispatch did not start");
  expect(String(last?.message)).toContain(CORR);
  expect(log.some((e) => String(e.message ?? "").includes("dispatched"))).toBe(false);
});

test("hold cap: a turn still running at the cap is reported at level warning as dispatched, never at level info", async () => {
  const { log, turn, swarm, ctx } = turnSession({ startMs: 0 }, { holdTimeoutMs: 100 });
  const t0 = Date.now();
  await swarm.handler(BRIEF, ctx);
  expect(Date.now() - t0).toBeLessThan(1000);
  expect(calls(log)).toEqual([...STARTUP, "bridge", "sendUserMessage", "turn-start", "notify"]);
  const last = log.at(-1);
  expect(last?.level).toBe("warning");
  expect(String(last?.message)).toContain(`plan ${CORR} dispatched`);
  expect(String(last?.message)).toContain("still running after 0.1 s");
  expect(String(last?.message)).toContain(HOLD_ENV);
  expect(log.some((e) => e.call === "notify" && e.level === "info" && String(e.message).includes("dispatched"))).toBe(false);
  turn.finish();
});

test("no UI, no cap: the handler holds for as long as the turn runs and reports nothing when it ends", async () => {
  const { log, turn, swarm, ctx } = turnSession({ startMs: 0 }, {});
  ctx.hasUI = false;
  let settled = false;
  const run = swarm.handler(BRIEF, ctx).then(() => void (settled = true));
  await turn.observed();
  // a real wait on purpose: "unbounded" can only be shown by outliving the cap the UI rows use (the helper's
  // poll loops are async, which bun's fake timers cannot advance); the turn then ends on finish(), not the clock
  await Bun.sleep(120);
  expect(settled).toBe(false);
  expect(calls(log)).toEqual(["bridge", "sendUserMessage", "turn-start"]);
  turn.finish();
  await run;
  expect(calls(log)).toEqual(["bridge", "sendUserMessage", "turn-start", "turn-end", "waitForIdle"]);
});

test("SWARM_DISPATCH_HOLD_MS: a positive integer caps the hold without a UI; anything else is ignored", async () => {
  process.env[HOLD_ENV] = "80";
  const capped = turnSession({ startMs: 0 });
  capped.ctx.hasUI = false;
  const t0 = Date.now();
  const { err } = await capturedStreams(() => capped.swarm.handler(BRIEF, capped.ctx));
  expect(Date.now() - t0).toBeLessThan(1000);
  const hold = err.find((line) => line.includes("still running after 0.08 s"));
  expect(hold?.startsWith("[/swarm] ")).toBe(true);
  expect(err.join("")).not.toContain(["E", "POLICY"].join("-"));
  capped.turn.finish();

  for (const bad of ["", "  ", "0", "-5", "soon", "1.5"]) {
    process.env[HOLD_ENV] = bad;
    expect(holdTimeoutMs({ hasUI: false }, {}, process.env)).toBe(Number.POSITIVE_INFINITY);
    expect(holdTimeoutMs({ hasUI: true }, {}, process.env)).toBe(30 * 60_000);
  }
  process.env[HOLD_ENV] = "5000";
  expect(holdTimeoutMs({ hasUI: true }, {}, process.env)).toBe(5000);
  expect(holdTimeoutMs({ hasUI: true }, { holdTimeoutMs: 7 }, process.env)).toBe(7);
});

test("sync start: isIdle already false on the first poll counts as started; the hold ends on the next idle poll", async () => {
  const repo = gitRepo();
  const { log, swarm } = session();
  const reads: boolean[] = [];
  const ctx = commandCtx(repo, [], {
    log,
    // in flight on the first poll, over on the second
    isIdle: () => {
      const idle = reads.length > 0;
      reads.push(idle);
      return idle;
    },
  });
  await swarm.handler(BRIEF, ctx);
  expect(calls(log)).toEqual([...STARTUP, "bridge", "sendUserMessage", "waitForIdle", "notify"]);
  expect(reads).toEqual([false, true]);
});

test.each([
  ["--pattern=hotfix --risk=high fix login", "fix login", "hotfix", "high"],
  ["fix login --risk=low", "fix login", "feature", "low"],
  ["migrate  the   users table --pattern=dependency", "migrate  the   users table", "dependency", "medium"],
  ["--pattern=cloud-native add an event-driven ingest", "add an event-driven ingest", "cloud-native", "medium"],
  ["roll out the payments overlay --pattern=cluster --risk=high", "roll out the payments overlay", "cluster", "high"],
  ["--pattern=web3 ship the staking vault", "ship the staking vault", "web3", "medium"],
])("flags: %s → brief %j, pattern %s, risk %s", async (args, brief, pattern, risk) => {
  const repo = gitRepo();
  const { log, swarm } = session();
  await swarm.handler(args, commandCtx(repo, [], { log }));
  const corr = swarmCorrelationId({ brief, pattern: pattern as "feature", risk_class: risk as "low" });
  expect(bridgeReq(log)?.args).toEqual([`--root=${repo}`, `--brief-text=${brief}`, `--pattern=${pattern}`, `--risk-class=${risk}`, `--correlation-id=${corr}`]);
  expect(calls(log)).toContain("sendUserMessage");
});

test("correlation id: a uuid5 fixed by (pattern, risk, brief); any input change re-keys it", () => {
  const base = { brief: BRIEF, pattern: "feature", risk_class: "medium" } as const;
  expect(BRIEF_CORR).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  expect(swarmCorrelationId({ ...base })).toBe(BRIEF_CORR);
  const variants = [
    swarmCorrelationId({ ...base, brief: `${BRIEF}!` }),
    swarmCorrelationId({ ...base, pattern: "hotfix" }),
    swarmCorrelationId({ ...base, risk_class: "high" }),
  ];
  expect(new Set([BRIEF_CORR, ...variants]).size).toBe(4);
  // flags never change the normalized brief, so their placement never changes the id
  const a = parseSwarmArgs(`--risk=high ${BRIEF}`);
  const b = parseSwarmArgs(`${BRIEF} --risk=high`);
  if ("error" in a || "error" in b) throw new Error("usage error");
  expect(swarmCorrelationId(a)).toBe(swarmCorrelationId(b));
});

test.each(["--pattern=bogus x", "--risk=urgent x", "--pattern= x", "", "   ", "--risk=low", "--pattern=hotfix --risk=high"])(
  "usage: %j notifies usage with no bridge call and no dispatch",
  async (args) => {
    const repo = gitRepo();
    const { log, swarm } = session();
    await swarm.handler(args, commandCtx(repo, [], { log }));
    expect(calls(log)).toEqual(["notify"]);
    expect(String(log[0].message).toLowerCase()).toContain("usage");
    expect(String(log[0].message)).toContain("/swarm <brief>");
  },
);

test("parseSwarmArgs: flags anywhere are removed from the brief; the last repeated flag wins", () => {
  expect(parseSwarmArgs("--risk=low ship it --pattern=hotfix")).toEqual({ brief: "ship it", pattern: "hotfix", risk_class: "low" });
  expect(parseSwarmArgs("--risk=low --risk=high ship it")).toEqual({ brief: "ship it", pattern: "feature", risk_class: "high" });
  expect(parseSwarmArgs("keep --pattern-ish text")).toEqual({ brief: "keep --pattern-ish text", pattern: "feature", risk_class: "medium" });
  expect(parseSwarmArgs("--pattern=custom x")).toHaveProperty("error");
});

test.each<[string, BridgeResult | Error]>([
  ["thrown E-CONTRACT", new SwarmToolError("E-CONTRACT", HINT)],
  ["exit 2 result", { exitCode: 2 as unknown as 0, json: { error: { code: "E-CONTRACT", message: HINT } }, swarmDir: "" }],
])("conflict (%s): the hint is notified and nothing is dispatched or awaited", async (_name, outcome) => {
  const repo = gitRepo();
  const { log, swarm } = session(outcome);
  await swarm.handler("add a /health endpoint", commandCtx(repo, [], { log }));
  expect(calls(log)).toEqual([...STARTUP, "bridge", "notify"]);
  expect(String(log.at(-1)?.message)).toContain("pass a new --correlation-id or --prefix");
});

test.each<[string, Error | BridgeResult]>([
  ["E-INPUT", new SwarmToolError("E-INPUT", "orch_plan usage error")],
  ["E-DEP", new SwarmToolError("E-DEP", "python3 not found")],
  ["plain error", new Error("spawn failed")],
  ["exit 1 fail", { exitCode: 1, json: { status: "fail", summary: "store locked" }, swarmDir: "" }],
])("failure (%s): notified, never dispatched", async (_name, outcome) => {
  const repo = gitRepo();
  const { log, swarm } = session(outcome);
  await swarm.handler("add a /health endpoint", commandCtx(repo, [], { log }));
  expect(calls(log)).toEqual([...STARTUP, "bridge", "notify"]);
});

test("reused plan: an identical brief dispatches normally", async () => {
  const repo = gitRepo();
  const { log, swarm } = session({ ...PLAN, json: { ...PLAN.json, reused: true } });
  await swarm.handler("add a /health endpoint", commandCtx(repo, [], { log }));
  expect(dispatchEvidence(payload(dispatchText(log) ?? ""), CORR)).toEqual({ verified: true, authorized: true });
  expect(calls(log)).toContain("waitForIdle");
  expect(log.at(-1)?.level).toBe("info");
});

test.each(["unsigned", "tampered", "mismatched"])(
  "consumer verification rejects %s assignments and its failed attestation prevents dispatch",
  async (fault) => {
    const original = session();
    await original.swarm.handler(BRIEF, commandCtx(gitRepo(), [], { log: original.log }));
    const envelope = payload(dispatchText(original.log) ?? "");
    expect(dispatchEvidence(envelope, CORR)).toEqual({ verified: true, authorized: true });
    if (fault === "unsigned") delete envelope.sig;
    if (fault === "tampered") envelope.ttl_s = Number(envelope.ttl_s) + 1;
    if (fault === "mismatched") envelope.target = "A05";
    const evidence = dispatchEvidence(envelope, CORR);
    expect(evidence.verified).toBe(false);
    expect(evidence.authorized).toBe(fault !== "mismatched");
    const { log, swarm } = session({
      ...PLAN,
      json: { ...PLAN.json, execution_assignment: envelope, execution_assignment_verified: evidence.verified },
    });
    await swarm.handler(BRIEF, commandCtx(gitRepo(), [], { log }));
    expect(calls(log)).not.toContain("sendUserMessage");
    expect(calls(log)).not.toContain("waitForIdle");
    expect(log.at(-1)?.level).toBe("error");
    expect(log.at(-1)?.message).toContain("/swarm dispatch refused");
    expect(String(log.at(-1)?.message)).not.toContain(["E", "POLICY"].join("-"));
  },
);

test.each([
  "unsuccessful plan",
  "absent", "unverified", "missing verification", "model assertion", "missing signature", "advisory", "schema", "type", "source", "target", "outer correlation",
  "payload correlation", "controller identity", "agent", "capability", "ready tasks", "duplicate ready tasks",
  "missing plan", "plan correlation", "plan task mismatch", "malformed tasks", "missing budget", "risk mismatch",
])("assignment denial (%s): no A01 turn, no wait, no envelope leakage", async (fault) => {
  const result = structuredClone(PLAN);
  const json = result.json;
  const envelope = json.execution_assignment as Record<string, unknown>;
  const assign = envelope.payload as Record<string, unknown>;
  const plan = (assign.inputs as Array<Record<string, unknown>>)[0];
  switch (fault) {
    case "unsuccessful plan": json.status = "error"; break;
    case "absent": delete json.execution_assignment; break;
    case "unverified": json.execution_assignment_verified = false; break;
    case "advisory": envelope.sig = "advisory:UNTRUSTED-SENSITIVE"; break;
    case "missing verification": delete json.execution_assignment_verified; break;
    case "model assertion": json.execution_assignment_verified = "true"; break;
    case "missing signature": delete envelope.sig; break;
    case "schema": envelope.schema = "swarm.v1.task.result"; break;
    case "type": envelope.type = "task.result"; break;
    case "source": envelope.source = "A01"; break;
    case "target": envelope.target = "A05"; break;
    case "outer correlation": envelope.correlation_id = "other"; break;
    case "payload correlation": assign.correlation_id = "other"; break;
    case "controller identity": assign.task_id = "other"; break;
    case "agent": assign.agent_id = "A05"; break;
    case "capability": assign.capability = "code.backend"; break;
    case "ready tasks": assign.ready_tasks = ["not-in-plan"]; break;
    case "duplicate ready tasks": assign.ready_tasks = [...assign.ready_tasks as string[], ...assign.ready_tasks as string[]]; break;
    case "missing plan": assign.inputs = []; break;
    case "plan correlation": plan.correlation_id = "other"; break;
    case "plan task mismatch": (plan.tasks as Array<Record<string, unknown>>)[0].task_id = "other"; break;
    case "malformed tasks": json.tasks = [{ task_id: "missing-state", depends_on: [] }]; break;
    case "missing budget": delete assign.budget; break;
    case "risk mismatch": assign.risk_class = "other"; break;
  }
  json.summary = "UNTRUSTED-SENSITIVE";
  const { log, swarm } = session(result);
  const entries: SessionEntry[] = [];
  let idleReads = 0;
  await swarm.handler(BRIEF, commandCtx(gitRepo(), entries, { log, isIdle: () => (idleReads++, false) }));
  expect(calls(log)).toEqual([...STARTUP, "bridge", "notify"]);
  expect(idleReads).toBe(0);
  expect(entries).toEqual([]);
  expect(log.at(-1)?.level).toBe("error");
  expect(log.at(-1)?.message).toContain("/swarm dispatch refused");
  expect(String(log.at(-1)?.message)).not.toContain(["E", "POLICY"].join("-"));
  expect(log.at(-1)?.message).not.toContain("UNTRUSTED-SENSITIVE");
});

test("unsigned legacy planning remains available but cannot dispatch even without a UI", async () => {
  delete process.env.SWARM_SIGNING_KEY;
  delete process.env.SWARM_REQUIRE_KEY;
  delete process.env.SWARM_ED25519_KEY;
  delete process.env.SWARM_ED25519_KEY_FILE;
  const home = process.env.HOME;
  const xdg = process.env.XDG_CONFIG_HOME;
  const isolated = tmpDir("swarm-no-key-");
  process.env.HOME = isolated;
  process.env.XDG_CONFIG_HOME = isolated;
  const log: CallLog = [];
  const pi = fakePi({ sendUserMessage: (text) => void log.push({ call: "sendUserMessage", text }) });
  pi.load(createSwarmExtension({ bridge: runPy }));
  try {
    const { out, err } = await capturedStreams(() => pi.command("swarm").handler(BRIEF, commandCtx(gitRepo(), [], { log, hasUI: false })));
    expect(calls(log)).toEqual([]);
    expect(out).toEqual([]);
    const hit = err.find((line) => line.includes("dispatch refused"));
    expect(hit?.startsWith("[/swarm] ")).toBe(true);
    expect(err.join("")).not.toContain(["E", "POLICY"].join("-"));
    expect(err.join("")).not.toContain('"payload"');
  } finally {
    if (home === undefined) delete process.env.HOME; else process.env.HOME = home;
    if (xdg === undefined) delete process.env.XDG_CONFIG_HOME; else process.env.XDG_CONFIG_HOME = xdg;
  }
});

test("plan mode: /swarm refuses before any write or bridge call", async () => {
  const repo = gitRepo();
  const { log, swarm } = session();
  const stderr: string[] = [];
  const errSpy = spyOn(process.stderr, "write").mockImplementation((chunk) => (stderr.push(String(chunk)), true));
  try {
    await swarm.handler(BRIEF, commandCtx(repo, PLAN_MODE, { log }));
  } finally {
    errSpy.mockRestore();
  }
  expect(calls(log)).not.toContain("bridge");
  expect(calls(log)).not.toContain("sendUserMessage");
  expect(calls(log)).not.toContain("waitForIdle");
  expect(stderr.join("")).not.toContain("[/swarm]");
  const notify = log.filter((e) => e.call === "notify");
  expect(notify).toHaveLength(1);
  expect(String(notify[0]?.message)).toBe("/swarm refused: omp is in plan mode (read-only), so nothing was planned; leave plan mode first");
  expect(notify[0]?.level).toBe("warning");
});

test("non-top-level session: /swarm is refused before any write or bridge call", async () => {
  const repo = gitRepo();
  const { log, swarm } = session();
  await swarm.handler(BRIEF, commandCtx(repo, [{ type: "session_init", agent: "a08-qa" }], { log }));
  expect(calls(log)).toEqual(["notify"]);
  expect(String(log[0].message)).toBe("/swarm refused: only the top-level controller session may dispatch A01");
});

/** stdout/stderr writes during `run`, with the streams restored afterwards. */
async function capturedStreams(run: () => Promise<void>): Promise<{ out: string[]; err: string[] }> {
  const out: string[] = [];
  const err: string[] = [];
  const outSpy = spyOn(process.stdout, "write").mockImplementation((chunk) => (out.push(String(chunk)), true));
  const errSpy = spyOn(process.stderr, "write").mockImplementation((chunk) => (err.push(String(chunk)), true));
  try {
    await run();
  } finally {
    outSpy.mockRestore();
    errSpy.mockRestore();
  }
  return { out, err };
}

/** WR-11: without a UI every failing path still reports on stderr (never stdout); the success path stays silent. */
test.each<[string, string, BridgeResult | Error, SessionEntry[], string]>([
  ["usage", "--pattern=bogus x", PLAN, [], "usage:"],
  ["conflict", BRIEF, new SwarmToolError("E-CONTRACT", HINT), [], "pass a new --correlation-id"],
  ["E-DEP", BRIEF, new SwarmToolError("E-DEP", "python3 not found"), [], "python3 not found"],
  ["exit 1", BRIEF, { exitCode: 1, json: { status: "fail", summary: "store locked" }, swarmDir: "" }, [], "orch_plan exit 1: store locked"],
])("no UI (%s): the message goes to stderr as [/swarm] …, nothing to stdout", async (_name, args, outcome, entries, needle) => {
  const { log, swarm } = session(outcome);
  const { out, err } = await capturedStreams(() => swarm.handler(args, commandCtx(gitRepo(), entries, { log, hasUI: false })));
  expect(calls(log)).not.toContain("notify");
  expect(calls(log)).not.toContain("sendUserMessage");
  expect(out).toEqual([]);
  const hit = err.find((line) => line.includes(needle));
  expect(hit?.startsWith("[/swarm] ")).toBe(true);
  expect(hit?.endsWith("\n")).toBe(true);
  expect(err.join("")).not.toContain(["E", "POLICY"].join("-"));
});

test("no UI: a dispatch that never starts reports on stderr; a dispatched plan writes nothing", async () => {
  const never = turnSession({}, { startTimeoutMs: 50 });
  never.ctx.hasUI = false;
  const failed = await capturedStreams(() => never.swarm.handler(BRIEF, never.ctx));
  expect(failed.out).toEqual([]);
  const hit = failed.err.find((line) => line.includes("dispatch did not start"));
  expect(hit).toContain(CORR);
  expect(failed.err.join("")).not.toContain(["E", "POLICY"].join("-"));

  const { log, swarm } = session();
  const ok = await capturedStreams(() => swarm.handler(BRIEF, commandCtx(gitRepo(), [], { log, hasUI: false })));
  expect(calls(log)).toEqual(["bridge", "sendUserMessage", "waitForIdle"]);
  expect(ok.out).toEqual([]);
  expect(ok.err.join("")).not.toContain(["E", "POLICY"].join("-"));
  expect(ok.err.join("")).not.toContain("dispatch refused");
});


test("scoped bridge: the command and swarm_plan share one session's inflight set; another session has its own", async () => {
  const repo = gitRepo();
  const reqs: BridgeRequest[] = [];
  const bridge: Bridge = async (req) => {
    reqs.push(req);
    return PLAN;
  };
  const one = fakePi({ sendUserMessage: () => {} });
  const two = fakePi({ sendUserMessage: () => {} });
  one.load(createSwarmExtension({ bridge }));
  two.load(createSwarmExtension({ bridge }));

  await one.command("swarm").handler("add a /health endpoint", commandCtx(repo));
  await callTool(one.tool("swarm_plan"), { brief: BRIEF, pattern: "feature", risk_class: "medium", correlation_id: BRIEF_CORR }, fakeCtx(repo));
  await two.command("swarm").handler("add a /health endpoint", commandCtx(repo));

  expect(reqs).toHaveLength(3);
  expect(reqs[0].inflight).toBeInstanceOf(Set);
  expect(reqs[0].inflight).toBe(reqs[1].inflight);
  expect(reqs[2].inflight).toBeInstanceOf(Set);
  expect(reqs[2].inflight).not.toBe(reqs[0].inflight);
  expect(reqs[0].args).toEqual(reqs[1].args);
});

test("real orch_plan: the same brief reuses one plan", async () => {
  const repo = gitRepo();
  process.env.SWARM_DIR = tmpDir("swarm-omp-dir-");
  const log: CallLog = [];
  const pi = fakePi({ sendUserMessage: (text) => void log.push({ call: "sendUserMessage", text }) });
  const planned: BridgeResult[] = [];
  pi.load(createSwarmExtension({ bridge: async (req) => {
    const result = await runPy(req);
    if (req.script === "orch_plan") planned.push(result);
    return result;
  } }));
  const swarm = pi.command("swarm");
  const ctx = commandCtx(repo, [], { log, hasUI: false });

  await swarm.handler(BRIEF, ctx);
  await swarm.handler(`  ${BRIEF} --risk=medium `, ctx);
  const texts = log.filter((e) => e.call === "sendUserMessage").map((e) => String(e.text));
  expect(texts).toHaveLength(2);
  expect(planned).toHaveLength(2);
  for (let i = 0; i < planned.length; i++) {
    expect(planned[i].json.execution_assignment_verified).toBe(true);
    expect(dispatchEvidence(payload(texts[i]), BRIEF_CORR)).toEqual({ verified: true, authorized: true });
  }
  expect(planned[1].json.reused).toBe(true);

  const [first, again] = texts.map(payload) as Array<{ correlation_id: string; payload: { ready_tasks: string[]; task_id: string } }>;
  expect(first.correlation_id).toBe(BRIEF_CORR);
  expect(again.payload.ready_tasks).toEqual(first.payload.ready_tasks);
  expect(again.payload.task_id).toBe(BRIEF_CORR);

  // one plan for the brief in the store: the second run created nothing
  const mine = statusTaskIds((await callTool(pi.tool("swarm_status"), { correlation_id: BRIEF_CORR }, fakeCtx(repo))).details);
  expect(mine).toHaveLength(13);
  expect(mine).toContain(first.payload.ready_tasks[0]);
});

// beforeEach has already planned BRIEF in this store (under CORR): this test plans only the other brief, so its body runs one cold plan
test("real orch_plan: a different brief gets its own correlation beside another brief's plan", async () => {
  const repo = gitRepo();
  const log: CallLog = [];
  const pi = fakePi({ sendUserMessage: (text) => void log.push({ call: "sendUserMessage", text }) });
  const planned: BridgeResult[] = [];
  pi.load(createSwarmExtension({ bridge: async (req) => {
    const result = await runPy(req);
    if (req.script === "orch_plan") planned.push(result);
    return result;
  } }));
  await pi.command("swarm").handler("fix the login bug --pattern=hotfix", commandCtx(repo, [], { log, hasUI: false }));
  const texts = log.filter((e) => e.call === "sendUserMessage").map((e) => String(e.text));
  expect(texts).toHaveLength(1);
  expect(planned).toHaveLength(1);
  expect(planned[0].json.execution_assignment_verified).toBe(true);
  const otherCorr = swarmCorrelationId({ brief: "fix the login bug", pattern: "hotfix", risk_class: "medium" });
  expect(dispatchEvidence(payload(texts[0]), otherCorr)).toEqual({ verified: true, authorized: true });

  const other = payload(texts[0]) as { correlation_id: string; payload: { ready_tasks: string[] } };
  expect(other.correlation_id).toBe(otherCorr);
  expect(other.correlation_id).not.toBe(BRIEF_CORR);
  expect(other.payload.ready_tasks).toHaveLength(1);

  const others = statusTaskIds((await callTool(pi.tool("swarm_status"), { correlation_id: otherCorr }, fakeCtx(repo))).details);
  expect(others).toHaveLength(8);
  expect(others).toContain(other.payload.ready_tasks[0]);
});

test("reused plan with a whole-number stored metric still dispatches a verifiable assignment", async () => {
  // JS has one number type: the bridge would read 100.0 back as 100. The controller must sign the form that survives.
  const repo = gitRepo();
  process.env.SWARM_DIR = tmpDir("swarm-omp-metric-");
  const args = [`--root=${repo}`, `--brief-text=${BRIEF}`, `--correlation-id=${BRIEF_CORR}`];
  const initial = await runPy({ script: "orch_plan", args, cwd: repo });
  expect(initial.json.execution_assignment_verified).toBe(true);
  const taskId = (initial.json.tasks as Array<{ task_id: string }>)[0].task_id;
  const recorded = Bun.spawnSync(["python3", "-c", `
import json, sys
from swarm.taskstore import TaskStore
store = TaskStore(root=sys.argv[1])
store.update(sys.argv[2], notes=json.dumps(dict(store.get(sys.argv[2])["notes_json"], result={"metric": 100.0, "ratio": 0.5})))
`, repo, taskId], { cwd: REPO_ROOT, env: { ...process.env }, stdout: "pipe", stderr: "pipe" });
  expect(recorded.stderr.toString()).toBe("");
  expect(recorded.exitCode).toBe(0);
  const log: CallLog = [];
  const pi = fakePi({ sendUserMessage: (text) => void log.push({ call: "sendUserMessage", text }) });
  pi.load(createSwarmExtension({ bridge: runPy }));
  await pi.command("swarm").handler(BRIEF, commandCtx(repo, [], { log }));
  const text = dispatchText(log) ?? "";
  expect(text).not.toContain("100.0");
  const envelope = payload(text);
  const signed = envelope.payload as { inputs: Array<{ tasks: Array<{ task_id: string; notes: { result?: unknown } }> }> };
  expect(signed.inputs[0].tasks.find((task) => task.task_id === taskId)?.notes.result).toEqual({ metric: 100, ratio: 0.5 });
  expect(dispatchEvidence(envelope, BRIEF_CORR)).toEqual({ verified: true, authorized: true });
});

test("reused live plan dispatches newly ready dependencies, not its stale initial snapshot", async () => {
  const repo = gitRepo();
  process.env.SWARM_DIR = tmpDir("swarm-omp-dir-");
  const args = [`--root=${repo}`, `--brief-text=${BRIEF}`, `--correlation-id=${BRIEF_CORR}`];
  const initial = await runPy({ script: "orch_plan", args, cwd: repo });
  const tasks = initial.json.tasks as Array<{ task_id: string; depends_on: string[] }>;
  const root = tasks.find((task) => task.depends_on.length === 0)!;
  for (const state of ["CLAIMED", "IN_PROGRESS", "IN_REVIEW"]) {
    const result = await runPy({ script: "orch_status", args: [`--root=${repo}`, "--transition", root.task_id, state, "--reason=test-owned progress"], cwd: repo });
    expect(result.exitCode).toBe(0);
  }
  const log: CallLog = [];
  const pi = fakePi({ sendUserMessage: (text) => void log.push({ call: "sendUserMessage", text }) });
  pi.load(createSwarmExtension({ bridge: runPy }));
  await pi.command("swarm").handler(BRIEF, commandCtx(repo, [], { log }));
  const envelope = payload(dispatchText(log) ?? "");
  expect(dispatchEvidence(envelope, BRIEF_CORR)).toEqual({ verified: true, authorized: true });
  const assignment = envelope.payload as { ready_tasks: string[] };
  expect(assignment.ready_tasks).not.toContain(root.task_id);
  expect(new Set(assignment.ready_tasks)).toEqual(new Set(tasks.filter((task) =>
    task.depends_on.length > 0 && task.depends_on.every((id) => id === root.task_id)).map((task) => task.task_id)));
  expect(calls(log)).toEqual([...STARTUP, "sendUserMessage", "waitForIdle", "notify"]);
});

/**
 * A /swarm session whose dispatch turn is scripted as `script`; `afterSend` runs once the dispatch is sent, which is
 * after the lifecycle step and the deadline of the start wait, so a clock change it arms lands mid-wait.
 */
function dispatchedSession(script: ScriptedTurn, opts: SwarmCommandOptions, afterSend: () => void) {
  const log: CallLog = [];
  const entries: SessionEntry[] = [];
  const turn = fakeTurn(script, entries, log);
  const bridge: Bridge = async (req) => {
    log.push({ call: "bridge", req });
    return PLAN;
  };
  const pi = fakePi({
    sendUserMessage: (text) => {
      log.push({ call: "sendUserMessage", text });
      turn.start();
      afterSend();
    },
  });
  pi.load(createSwarmExtension({ bridge }));
  const swarm = swarmCommand(pi.api, bridge, opts);
  const ctx = commandCtx(gitRepo(), entries, { log, isIdle: turn.isIdle });
  return { log, swarm, ctx };
}

test("clock: a wall-clock jump during the dispatch-start wait does not fail a turn that starts on time", async () => {
  // the poll deadlines run on performance.now(); a Date.now() jump mid-wait must not read as a start timeout
  const realNow = Date.now;
  let skew = 0;
  const jumped = spyOn(Date, "now").mockImplementation(() => realNow() + skew);
  try {
    const { log, swarm, ctx } = dispatchedSession({ entryMs: 30, entry: DISPATCH_ENTRY }, { startTimeoutMs: 5_000 }, () => {
      setTimeout(() => (skew = 3_600_000), 1); // the jump lands between two polls
    });
    await swarm.handler(BRIEF, ctx);
    expect(log.at(-1)?.level).toBe("info");
    expect(String(log.at(-1)?.message)).toContain(`plan ${CORR} dispatched`);
  } finally {
    jumped.mockRestore();
  }
});

test("cancel: an aborted signal stops both waits at once and reports the cancel at level warning", async () => {
  const ac = new AbortController();
  const { log, swarm, ctx } = dispatchedSession({}, { startTimeoutMs: 2_000, signal: ac.signal }, () => {
    setTimeout(() => ac.abort(), 20); // the start wait is already polling when the abort lands
  });
  const t0 = performance.now();
  await swarm.handler(BRIEF, ctx);
  expect(performance.now() - t0).toBeLessThan(1000);
  expect(log.at(-1)?.level).toBe("warning");
  expect(String(log.at(-1)?.message)).toContain("cancelled");
  expect(log.some((e) => String(e.message ?? "").includes("dispatched"))).toBe(false);
});

test("startup progress goes through notify at info level, so a UI shows the init states", async () => {
  const { log, swarm } = session();
  await swarm.handler(BRIEF, commandCtx(gitRepo(), [], { log }));
  const progress = log.filter((e) => e.call === "notify" && e.level === "info").map((e) => String(e.message));
  expect(progress.some((m) => m.includes("state=LOADING"))).toBe(true);
  expect(progress.some((m) => m.includes("state=INITIALIZING"))).toBe(true);
});

test("orch_plan result without a correlation id is refused with E-CONTRACT and never dispatched", async () => {
  const { log, swarm } = session({ exitCode: 0, json: {}, swarmDir: "" });
  await swarm.handler(BRIEF, commandCtx(gitRepo(), [], { log }));
  expect(calls(log)).not.toContain("sendUserMessage");
  expect(calls(log)).not.toContain("waitForIdle");
  expect(log.at(-1)?.level).toBe("error");
  expect(String(log.at(-1)?.message)).toContain("E-CONTRACT");
  expect(String(log.at(-1)?.message)).toContain("correlation_id");
});
