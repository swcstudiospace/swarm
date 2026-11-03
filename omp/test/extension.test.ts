/**
 * SC5 / TOOL-01 / TOOL-06: the extension entry does no I/O at load, registers hidden essential
 * swarm operations, the `/swarm` command and session handlers, routes operations through the injected
 * bridge, and guards typed assignment authority without opening generic execution.
 *
 * mock.module("node:fs") is process-wide and mock.restore cannot undo it, so this file runs in its own `bun test`
 * process (omp/package.json scripts.test runs it first, then the rest with it ignored).
 */
import { afterAll, expect, mock, spyOn, test } from "bun:test";
import * as nodeFs from "node:fs";
import type { BridgeRequest, BridgeResult } from "../src/bridge.ts";
import type * as EntryModule from "../src/index.ts";
import { SWARM_SLUGS } from "../src/guard.ts";
import { agentCtx, type CallLog, callTool, commandCtx, fakeCtx, fakePi, gitRepo, isolateEnv, tmpDir } from "./helpers.ts";

isolateEnv("SWARM_DIR", "SWARM_ROOT", "SWARM_TASK_ID", "SWARM_CORRELATION_ID", "SWARM_AGENT", "SWARM_ED25519_KEY_FILE", "SWARM_SIGNER_DENY_PATHS");

// Capture the real functions before mocking, so the wrappers never call themselves.
const realFs: Record<string, unknown> = { ...nodeFs };
let fsCalls = 0;
mock.module("node:fs", () =>
  Object.fromEntries(
    Object.entries(realFs).map(([name, value]) => [
      name,
      typeof value === "function"
        ? (...args: unknown[]) => {
            fsCalls++;
            return Reflect.apply(value, nodeFs, args);
          }
        : value,
    ]),
  ),
);
const spawn = spyOn(Bun, "spawn");
const spawnSync = spyOn(Bun, "spawnSync");
afterAll(() => {
  spawn.mockRestore();
  spawnSync.mockRestore();
});

/** The extension registers exactly these event handlers; the swarm bar adds the session/agent/turn/tool-execution ones. */
const EXPECTED_HANDLERS = [
  "agent_end",
  "agent_start",
  "before_agent_start",
  "session_shutdown",
  "session_start",
  "session_switch",
  "tool_call",
  "tool_execution_end",
  "tool_execution_start",
  "tool_result",
  "turn_end",
  "turn_start",
];

type Entry = typeof EntryModule;
let entry: Promise<Entry> | undefined;
/** The entry, imported once under the mocks; the query busts any cached src/index.ts so evaluation is observed. */
function loadEntry(): Promise<Entry> {
  entry ??= import(`../src/index.ts?noio=${Date.now()}`);
  return entry;
}

/** Every object (and function) reachable from `value`, by identity. */
function reachable(value: unknown, out = new Set<object>()): Set<object> {
  if (value === null || (typeof value !== "object" && typeof value !== "function")) return out;
  if (out.has(value)) return out;
  out.add(value);
  for (const child of Object.values(value)) reachable(child, out);
  return out;
}

test("positive control: after mock.module, a real node:fs call and a real Bun.spawn/spawnSync each move their counter", async () => {
  fsCalls = 0;
  spawn.mockClear();
  spawnSync.mockClear();
  // Intentional dynamic import (tests the module-loading boundary): a static import is hoisted and resolves
  // before mock.module runs, so only a later import observes the mocked node:fs.
  const mockedFs = await import("node:fs");
  expect(mockedFs.existsSync(import.meta.dir)).toBe(true);
  expect(fsCalls).toBe(1);
  expect(Bun.spawnSync(["true"], { stdout: "ignore", stderr: "ignore" }).exitCode).toBe(0);
  expect(spawnSync).toHaveBeenCalledTimes(1);
  expect(spawn).toHaveBeenCalledTimes(0);
  const proc = Bun.spawn(["true"], { stdout: "ignore", stderr: "ignore" });
  expect(await proc.exited).toBe(0);
  expect(spawn).toHaveBeenCalledTimes(1);
  expect(spawnSync).toHaveBeenCalledTimes(1);
});

test("registers hidden essential swarm operations without fs, spawn or spawnSync", async () => {
  fsCalls = 0;
  spawn.mockClear();
  spawnSync.mockClear();
  const { default: factory } = await loadEntry();
  expect(fsCalls).toBe(0); // module evaluation
  expect(spawn).toHaveBeenCalledTimes(0);
  expect(spawnSync).toHaveBeenCalledTimes(0);

  const pi = fakePi();
  factory(pi.api);
  expect(fsCalls).toBe(0); // factory call
  expect(spawn).toHaveBeenCalledTimes(0);
  expect(spawnSync).toHaveBeenCalledTimes(0);
  // registration order is not part of the contract
  expect([...pi.handlers.keys()].sort()).toEqual(EXPECTED_HANDLERS);
  expect([...pi.commands.keys()]).toEqual(["swarm"]);
  for (const tool of pi.tools) {
    expect(tool.hidden).toBe(true);
    expect(tool.loadMode).toBe("essential");
    expect(tool.parameters.type).toBe("object");
  }
  expect(pi.tool("swarm_assignment").name).toBe("swarm_assignment");
});

test("no action at load: the factory never calls a runtime pi action", async () => {
  const { default: factory } = await loadEntry();
  const pi = fakePi();
  factory(pi.api);
  expect(pi.actionCalls).toEqual([]);
  expect([...pi.handlers.keys()].sort()).toEqual(EXPECTED_HANDLERS);
  expect([...pi.commands.keys()]).toEqual(["swarm"]);
});

test("guard with no io: the tool_call handler decides without fs, spawn, spawnSync or the bridge", async () => {
  const { createSwarmExtension } = await loadEntry();
  const bridged: BridgeRequest[] = [];
  const pi = fakePi({ activeTools: ["read", "yield"] });
  pi.load(
    createSwarmExtension({
      bridge: async (req) => {
        bridged.push(req);
        return { exitCode: 0, json: {}, swarmDir: "" };
      },
    }),
  );
  const guard = pi.handler("tool_call");
  const a01 = agentCtx("/nonexistent-guard-cwd", "a01-orchestrator", [], false);
  fsCalls = 0;
  spawn.mockClear();
  spawnSync.mockClear();

  const blocked = guard({ toolName: "bash", toolCallId: "tc-1", input: { command: "ls" } }, a01) as { reason?: string };
  expect(blocked.reason?.startsWith("BLOCKED needs: depth")).toBe(true);
  const depthYield = { toolName: "yield", toolCallId: "tc-2", input: { data: { state: "BLOCKED", needs: "depth" } } };
  expect(guard(depthYield, a01)).toBeUndefined();
  expect(guard({ toolName: "bash", toolCallId: "tc-3", input: { command: "ls" } }, fakeCtx("/nonexistent-guard-cwd"))).toBeUndefined();

  expect(fsCalls).toBe(0);
  expect(spawn).toHaveBeenCalledTimes(0);
  expect(spawnSync).toHaveBeenCalledTimes(0);
  expect(bridged).toEqual([]);
  expect(pi.actionCalls).toEqual([]);
  expect(pi.runtimeCalls).toEqual(["getActiveTools", "getActiveTools"]);
});

test("registered headless guard consumes custom deny metadata without signer loading or io", async () => {
  const { createSwarmExtension } = await loadEntry();
  const source = "/nonexistent-controller-private/custom-signer.source";
  process.env.SWARM_AGENT = "a05-backend";
  process.env.SWARM_SIGNER_DENY_PATHS = JSON.stringify([source]);
  delete process.env.SWARM_ED25519_KEY_FILE;
  const bridged: BridgeRequest[] = [];
  const pi = fakePi();
  pi.load(createSwarmExtension({ bridge: async (req) => {
    bridged.push(req);
    return { exitCode: 0, json: {}, swarmDir: "" };
  } }));
  const guard = pi.handler("tool_call");
  const ctx = fakeCtx("/nonexistent-guard-cwd");
  fsCalls = 0;
  spawn.mockClear();
  spawnSync.mockClear();
  const events = [
    { toolName: "read", input: { path: `${source}:raw` } },
    { toolName: "grep", input: { path: "/nonexistent-controller-private", pattern: "." } },
    { toolName: "bash", input: { command: `cat ${source}` } },
    { toolName: "bash", input: { command: `cat < ${source}` } },
    { toolName: "bash", input: { command: `cp ${source} /tmp/public-copy`, cwd: "/unrelated",
      env: { HOME: "/unrelated", SWARM_ED25519_KEY_FILE: "/unrelated/public", SWARM_SIGNER_DENY_PATHS: "[]" } } },
  ];
  for (const [index, event] of events.entries()) {
    expect(guard({ ...event, toolCallId: `custom-${index}` }, ctx)).toEqual({
      block: true, reason: `BLOCKED needs: human-approval (signer_credential: signer-file-${event.toolName === "bash" ? "shell" : "read"})`,
    });
  }
  expect(guard({ toolName: "bash", toolCallId: "custom-stat", input: { command: `stat ${source}` } }, ctx)).toBeUndefined();
  expect(guard({ toolName: "read", toolCallId: "custom-public", input: { path: "src/guard.ts" } }, ctx)).toBeUndefined();
  expect(process.env.SWARM_ED25519_KEY_FILE).toBeUndefined();
  expect(fsCalls).toBe(0);
  expect(spawn).toHaveBeenCalledTimes(0);
  expect(spawnSync).toHaveBeenCalledTimes(0);
  expect(bridged).toEqual([]);
  expect(pi.actionCalls).toEqual([]);
  expect(pi.runtimeCalls).toEqual([]);
});

test("factory twice: sessions get unique tools and share no mutable object", async () => {
  const { default: factory } = await loadEntry();
  const [pi1, pi2] = [fakePi(), fakePi()];
  factory(pi1.api);
  factory(pi2.api);
  for (const pi of [pi1, pi2]) {
    const names = pi.tools.map((t) => t.name);
    expect(new Set(names).size).toBe(names.length);
  }
  expect(new Set(pi1.tools.map((t) => t.name))).toEqual(new Set(pi2.tools.map((t) => t.name)));

  const other = reachable(pi2.tools);
  const shared = [...reachable(pi1.tools)].filter((o) => other.has(o)).map((o) => JSON.stringify(o)?.slice(0, 80));
  expect(shared).toEqual([]);

  const before = JSON.stringify(pi2.tool("swarm_plan").parameters);
  const props = pi1.tool("swarm_plan").parameters.properties;
  if (!props || typeof props !== "object") throw new Error("swarm_plan has no properties");
  Reflect.set(props, "injected", { type: "string" });
  Reflect.deleteProperty(props, "brief");
  expect(JSON.stringify(pi2.tool("swarm_plan").parameters)).toBe(before);
});

test("bridge once per tool: one recorded bridge call each with the expected script and argv, no Bun.spawn", async () => {
  const { createSwarmExtension } = await loadEntry();
  // A real tmp git repo as ctx.cwd: `--root=` comes from gitToplevel, a read-only Bun.spawnSync of git at execute
  // time (legitimate), so only Bun.spawn (the python bridge) must stay at 0 here.
  const repo = gitRepo();
  const sdir = tmpDir("swarm-omp-dir-");
  process.env.SWARM_DIR = sdir;
  const calls: BridgeRequest[] = [];
  const bridge = async (req: BridgeRequest): Promise<BridgeResult> => {
    calls.push(req);
    return { exitCode: 0, json: { summary: `${req.script} ok` }, swarmDir: sdir };
  };
  const pi = fakePi();
  createSwarmExtension({ bridge })(pi.api);
  const ctx = fakeCtx(repo);
  const root = `--root=${repo}`;
  const cases: Array<[string, Record<string, unknown>, string, string[]]> = [
    ["swarm_plan", { brief: "fix login", pattern: "hotfix", risk_class: "low" }, "orch_plan",
      [root, "--brief-text=fix login", "--pattern=hotfix", "--risk-class=low"]],
    ["swarm_status", {}, "orch_status", [root]],
    ["swarm_ingest", { task_id: "H-patch", result: { task_id: "H-patch", state: "IN_REVIEW" } }, "orch_status",
      [root, "--task-id=H-patch", `--ingest=${sdir}/results/ingest-call-1.json`]],
    ["swarm_transition", { task_id: "H-rca", state: "CLAIMED", reason: "lease" }, "orch_status",
      [root, "--transition", "H-rca", "CLAIMED", "--reason=lease"]],
    ["swarm_gate", { gate: "review", task_id: "H-rev", correlation_id: "c-hot" }, "rev_gate",
      [root, "--task-id=H-rev", "--correlation-id=c-hot"]],
  ];

  spawn.mockClear();
  for (const [name, params, script, args] of cases) {
    calls.length = 0;
    const res = await callTool(pi.tool(name), params, name === "swarm_gate" ? agentCtx(repo, "a09-reviewer") : ctx);
    expect(calls).toHaveLength(1);
    expect(calls[0].script).toBe(script);
    expect(calls[0].args).toEqual(args);
    expect(calls[0].cwd).toBe(repo);
    expect(res.content[0].text).toBe(`${script} ok`);
  }
  expect(spawn).toHaveBeenCalledTimes(0);
});

test("swarm_catalog and swarm_tool_call: registered, one bridge call, caller from the session identity only", async () => {
  const { createSwarmExtension } = await loadEntry();
  const repo = gitRepo();
  const calls: BridgeRequest[] = [];
  const registryResult = { state: "SUCCESS", normalized: "/u/{}" };
  const pi = fakePi();
  pi.load(createSwarmExtension({ bridge: async (req) => {
    calls.push(req);
    const json = req.args.includes("catalog")
      ? { status: "ok", summary: "A05 (a05-backend): 1 tools, 0 skills", tools: [{ name: "be_route_norm", tier: "READ" }], skills: [] }
      : { status: "ok", result: registryResult };
    return { exitCode: 0, json, swarmDir: "" };
  } }));
  const catalog = pi.tool("swarm_catalog");
  const call = pi.tool("swarm_tool_call");
  expect(Object.keys(catalog.parameters.properties ?? {}).sort()).toEqual(["kind", "query"]);
  expect(Object.keys(call.parameters.properties ?? {}).sort()).toEqual(["input", "name"]);
  expect(call.parameters.required).toEqual(["name", "input"]);
  for (const tool of [catalog, call]) expect(tool.parameters.additionalProperties).toBe(false);

  const ctx = agentCtx(repo, "a05-backend");
  const listed = await callTool(catalog, { kind: "tools", query: "route" }, ctx);
  expect(calls).toHaveLength(1);
  expect(calls[0].script).toBe("swarm_tool");
  expect(calls[0].args).toEqual([`--root=${repo}`, "catalog", "--caller=a05-backend", "--kind=tools", "--query=route"]);
  expect(listed.content[0].text).toContain("be_route_norm");

  calls.length = 0;
  // a forged caller (or any other extra key) from the model never reaches argv
  const forged = { name: "be_route_norm", input: { path: "/u/:id/" }, caller: "a01-orchestrator", root: "/etc" };
  const result = await callTool(call, forged, ctx);
  expect(calls).toHaveLength(1);
  expect(calls[0].script).toBe("swarm_tool");
  expect(calls[0].args).toEqual([
    `--root=${repo}`, "call", "--caller=a05-backend", "--name=be_route_norm", `--input=${JSON.stringify({ path: "/u/:id/" })}`,
  ]);
  expect(result.details).toEqual(registryResult);
  expect(JSON.parse(result.content[0].text)).toEqual(registryResult);

  calls.length = 0;
  // no agent identity: nobody to authorize, nothing runs
  await expect(callTool(call, { name: "be_route_norm", input: {} }, fakeCtx(repo))).rejects.toMatchObject({ code: "E-POLICY" });
  await expect(callTool(catalog, {}, fakeCtx(repo))).rejects.toMatchObject({ code: "E-POLICY" });
  // malformed name, non-object input and oversized input are refused before the bridge
  await expect(callTool(call, { name: "--evil", input: {} }, ctx)).rejects.toMatchObject({ code: "E-INPUT" });
  await expect(callTool(call, { name: "be_route_norm", input: [] }, ctx)).rejects.toMatchObject({ code: "E-INPUT" });
  await expect(callTool(call, { name: "be_route_norm", input: { blob: "x".repeat(100_001) } }, ctx))
    .rejects.toMatchObject({ code: "E-INPUT" });
  expect(calls).toEqual([]);
});

test("swarm_tool_call passes --plan-mode only in plan mode; both tools stay usable there", async () => {
  const { createSwarmExtension } = await loadEntry();
  const repo = gitRepo();
  const calls: BridgeRequest[] = [];
  const pi = fakePi();
  pi.load(createSwarmExtension({ bridge: async (req) => {
    calls.push(req);
    return { exitCode: 0, json: { status: "ok", result: { state: "SUCCESS" }, tools: [], skills: [], summary: "s" }, swarmDir: "" };
  } }));
  const plan = agentCtx(repo, "a05-backend", [{ type: "mode_change", mode: "plan" }]);
  await callTool(pi.tool("swarm_catalog"), {}, plan);
  await callTool(pi.tool("swarm_tool_call"), { name: "be_route_norm", input: {} }, plan);
  expect(calls[0].args).not.toContain("--plan-mode");
  expect(calls[1].args).toContain("--plan-mode");
  await callTool(pi.tool("swarm_tool_call"), { name: "be_route_norm", input: {} }, agentCtx(repo, "a05-backend"));
  expect(calls[2].args).not.toContain("--plan-mode");
});

test("registered assignment transport returns the canonical prompt and envelope only for A01", async () => {
  const { createSwarmExtension } = await loadEntry();
  const repo = gitRepo();
  const assignment = {
    source: "A01@runner", target: "A02", type: "task.assign", schema: "swarm.v1.task.assign", correlation_id: "c-hot",
    sig: "hmac:synthetic-controller-signature",
    payload: { task_id: "H-req", correlation_id: "c-hot", agent_id: "A02" },
  };
  const prompt = `# task.assign (signed envelope)\n\`\`\`json\n${JSON.stringify(assignment)}\n\`\`\`\n\nCanonical specialist instructions.`;
  const canonical = {
    status: "ok", task_id: "H-req", correlation_id: "c-hot", agent_slug: "a02-requirements",
    assignment_verified: true, assignment, prompt,
  };
  const requests: BridgeRequest[] = [];
  const pi = fakePi({ activeTools: ["task", "swarm_assignment"] });
  pi.load(createSwarmExtension({ bridge: async (req) => {
    requests.push(req);
    return { exitCode: 0, json: canonical, swarmDir: "" };
  } }));
  const tool = pi.tool("swarm_assignment");
  expect(tool.parameters.required).toEqual(expect.arrayContaining(["task_id", "correlation_id"]));
  expect(Object.keys(tool.parameters.properties ?? {}).sort()).toEqual(["correlation_id", "task_id"]);
  expect(tool.parameters.additionalProperties).toBe(false);
  const params = { task_id: "H-req", correlation_id: "c-hot" };
  const ctx = agentCtx(repo, "a01-orchestrator", [], false);
  expect(pi.handler("tool_call")({ toolName: tool.name, toolCallId: "assign-1", input: params }, ctx)).toBeUndefined();
  const result = await callTool(tool, params, ctx);
  expect(requests).toHaveLength(1);
  expect(requests[0].script).toBe("orch_assign");
  expect(requests[0].args).toEqual([`--root=${repo}`, "--task-id=H-req", "--correlation-id=c-hot"]);
  expect(requests[0].cwd).toBe(repo);
  expect(result.content).toEqual([{ type: "text", text: prompt }]);
  expect(result.details).toEqual(canonical);
});

test("registered assignment transport accepts identities only, not caller-supplied authority", async () => {
  const { createSwarmExtension } = await loadEntry();
  const requests: BridgeRequest[] = [];
  const pi = fakePi();
  pi.load(createSwarmExtension({ bridge: async (req) => {
    requests.push(req);
    return { exitCode: 0, json: {}, swarmDir: "" };
  } }));
  const tool = pi.tool("swarm_assignment");
  const ctx = agentCtx("/nonexistent-assignment-cwd", "a01-orchestrator");
  fsCalls = 0;
  spawn.mockClear();
  spawnSync.mockClear();
  for (const extra of [
    { assignment: { type: "task.assign" } },
    { payload: { capability: "code.backend" } },
    { script: "arbitrary_script" },
    { root: "/caller-selected-workspace" },
    { dry_run: true },
    { advisory: true },
  ]) {
    await expect(callTool(tool, { task_id: "H-req", correlation_id: "c-hot", ...extra }, ctx))
      .rejects.toMatchObject({ code: "E-INPUT" });
  }
  expect(requests).toEqual([]);
  expect(fsCalls).toBe(0);
  expect(spawn).toHaveBeenCalledTimes(0);
  expect(spawnSync).toHaveBeenCalledTimes(0);
});

test("registered assignment transport refuses unsigned or unverified bridge success", async () => {
  const { createSwarmExtension } = await loadEntry();
  const repo = gitRepo();
  let response: Record<string, unknown> = {};
  const pi = fakePi();
  pi.load(createSwarmExtension({ bridge: async () => ({ exitCode: 0, json: response, swarmDir: "" }) }));
  const tool = pi.tool("swarm_assignment");
  const params = { task_id: "H-req", correlation_id: "c-hot" };
  for (const assignment_verified of [undefined, false, true]) {
    response = {
      status: "ok", ...params, assignment_verified, agent_slug: "a02-requirements",
      assignment: { sig: null, payload: params }, prompt: "unsigned advisory instructions",
    };
    await expect(callTool(tool, params, agentCtx(repo, "a01-orchestrator")))
      .rejects.toMatchObject({ code: "E-CONTRACT" });
  }
});

test("registered assignment transport refuses every non-controller before io or bridging", async () => {
  const { createSwarmExtension } = await loadEntry();
  delete process.env.SWARM_AGENT;
  const requests: BridgeRequest[] = [];
  const pi = fakePi({ activeTools: ["task", "swarm_assignment"] });
  pi.load(createSwarmExtension({ bridge: async (req) => {
    requests.push(req);
    return { exitCode: 0, json: {}, swarmDir: "" };
  } }));
  const tool = pi.tool("swarm_assignment");
  const guard = pi.handler("tool_call");
  const params = { task_id: "H-req", correlation_id: "c-hot" };
  const contexts = [
    ...SWARM_SLUGS.filter((slug) => slug !== "a01-orchestrator").map((slug) => agentCtx("/nonexistent-assignment-cwd", slug)),
    agentCtx("/nonexistent-assignment-cwd", "task"),
    fakeCtx("/nonexistent-assignment-cwd"),
    agentCtx("/nonexistent-assignment-cwd", "a01-orchestrator", [
      { type: "custom_message", customType: "plan-mode-context", content: "plan mode" },
    ]),
  ];
  fsCalls = 0;
  spawn.mockClear();
  spawnSync.mockClear();
  for (const ctx of contexts) {
    expect(guard({ toolName: tool.name, toolCallId: "assign-refused", input: params }, ctx)).toMatchObject({ block: true });
    await expect(callTool(tool, params, ctx)).rejects.toMatchObject({ code: "E-POLICY" });
  }
  expect(requests).toEqual([]);
  expect(fsCalls).toBe(0);
  expect(spawn).toHaveBeenCalledTimes(0);
  expect(spawnSync).toHaveBeenCalledTimes(0);
});

test("registered A01 guard keeps generic eval and raw-code devices closed", async () => {
  const { createSwarmExtension } = await loadEntry();
  const requests: BridgeRequest[] = [];
  const pi = fakePi({ activeTools: ["task", "swarm_assignment"] });
  pi.load(createSwarmExtension({ bridge: async (req) => {
    requests.push(req);
    return { exitCode: 0, json: {}, swarmDir: "" };
  } }));
  const guard = pi.handler("tool_call");
  const ctx = agentCtx("/nonexistent-assignment-cwd", "a01-orchestrator", [], false);
  fsCalls = 0;
  for (const event of [
    { toolName: "eval", input: { code: "1 + 1" } },
    { toolName: "write", input: { path: "xd://run_code", content: "{}" } },
    { toolName: "write", input: { path: ".swarm/assignment.json", content: "{}" } },
  ]) {
    expect(guard({ ...event, toolCallId: "raw-refused" }, ctx)).toMatchObject({ block: true });
  }
  expect(requests).toEqual([]);
  expect(fsCalls).toBe(0);
});

test("registered command refuses a successful unsigned plan without starting or waiting for A01", async () => {
  const { createSwarmExtension } = await loadEntry();
  const requests: BridgeRequest[] = [];
  const log: CallLog = [];
  const pi = fakePi({ sendUserMessage: (text) => void log.push({ call: "sendUserMessage", text }) });
  pi.load(createSwarmExtension({ bridge: async (req) => {
    requests.push(req);
    return { exitCode: 0, json: { status: "ok", correlation_id: "unsigned-plan", tasks: [] }, swarmDir: "" };
  } }));
  await pi.command("swarm").handler("plan this brief", commandCtx(gitRepo(), [], { log }));
  expect(requests).toHaveLength(1);
  expect(requests[0].script).toBe("orch_plan");
  expect(requests[0].inflight).toBeInstanceOf(Set);
  expect(log.map((entry) => entry.call)).toEqual(["notify"]);
  expect(log[0].message).toContain("dispatch refused");
  expect(log[0].level).toBe("error");
});
