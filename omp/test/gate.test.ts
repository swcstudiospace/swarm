/** swarm_gate unit tests (D-09, T-03-06): verdict-free schema, whitelisted argv, findings forwarding. */
import { expect, test } from "bun:test";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { type Bridge, type BridgeRequest, type BridgeResult, SwarmToolError } from "../src/bridge.ts";
import { GATE_AGENTS } from "../src/context.ts";
import { buildTools } from "../src/tools.ts";
import { type AnyTool, agentCtx, callTool, fakeCtx, gitRepo, isolateEnv, REPO_ROOT, tmpDir } from "./helpers.ts";

isolateEnv("SWARM_DIR", "SWARM_AGENT");

function gateWith(result: BridgeResult = { exitCode: 0, json: { status: "ok", summary: "gate PASS" }, swarmDir: "" }) {
  const calls: BridgeRequest[] = [];
  const bridge: Bridge = async (req) => {
    calls.push(req);
    return result;
  };
  const gate = buildTools(bridge).find((t) => t.name === "swarm_gate");
  if (!gate) throw new Error("swarm_gate not built");
  return { calls, gate: gate as AnyTool };
}

/** Schema violations: a `verdict`/`verdicts` key at any depth, or an object node that is not closed. */
function violations(node: unknown, path = "$"): string[] {
  if (Array.isArray(node)) return node.flatMap((v, i) => violations(v, `${path}[${i}]`));
  if (typeof node !== "object" || node === null) return [];
  const obj = node as Record<string, unknown>;
  const out = Object.keys(obj)
    .filter((k) => /^verdicts?$/i.test(k))
    .map((k) => `${path}.${k}: verdict key`);
  if (obj.type === "object") {
    const ap = obj.additionalProperties as Record<string, unknown> | false | undefined;
    const items = typeof ap === "object" && ap !== null ? (ap.items as Record<string, unknown> | undefined) : undefined;
    const closedMap = ap !== false && items?.type === "object" && items.additionalProperties === false;
    if (ap !== false && !closedMap) out.push(`${path}: object is not closed`);
  }
  return out.concat(Object.entries(obj).flatMap(([k, v]) => violations(v, `${path}.${k}`)));
}

test("gate schema no verdict at any depth, every object closed", () => {
  const { parameters } = gateWith().gate;
  expect(violations(parameters)).toEqual([]);
  const props = parameters.properties as Record<string, Record<string, unknown>>;
  expect(props.gate.enum).toEqual(["quality", "review", "security", "release"]);
  const finding = (props.per_target_findings.additionalProperties as { items: { properties: Record<string, { enum?: string[] }> } }).items;
  expect(finding.properties.severity.enum).toEqual(["info", "minor", "major", "critical", "blocker"]);

  // the walk is sensitive: a nested verdict or an open finding in a copy (never the source) is caught
  type Copy = { properties: { per_target_findings: { additionalProperties: { items: { properties: Record<string, unknown>; additionalProperties?: unknown } } } } };
  const withVerdict = structuredClone(parameters) as unknown as Copy;
  withVerdict.properties.per_target_findings.additionalProperties.items.properties.verdict = { type: "string" };
  expect(violations(withVerdict)).toEqual(["$.properties.per_target_findings.additionalProperties.items.properties.verdict: verdict key"]);
  const open = structuredClone(parameters) as unknown as Copy;
  delete open.properties.per_target_findings.additionalProperties.items.additionalProperties;
  expect(violations(open)).toEqual([
    "$.properties.per_target_findings: object is not closed",
    "$.properties.per_target_findings.additionalProperties.items: object is not closed",
  ]);
});

test("gate release findings refuse E-INPUT before the bridge", async () => {
  const sdir = tmpDir("swarm-omp-dir-");
  process.env.SWARM_DIR = sdir;
  const { calls, gate } = gateWith();
  const params = { gate: "release", task_id: "F-rel", correlation_id: "c1", per_target_findings: { "F-be": [] } };
  const err = await callTool(gate, params, agentCtx(gitRepo(), GATE_AGENTS.release)).catch((e: unknown) => e);
  expect(err).toBeInstanceOf(SwarmToolError);
  expect((err as SwarmToolError).code).toBe("E-INPUT");
  expect(calls).toHaveLength(0);
  expect(existsSync(join(sdir, "results"))).toBe(false);
});

test("gate argv: whitelisted flags, GATE_SCRIPTS mapping, bridge-owned findings file", async () => {
  const sdir = tmpDir("swarm-omp-dir-");
  process.env.SWARM_DIR = sdir;
  const repo = gitRepo();
  const { calls, gate } = gateWith();
  const findings = { "F-be": [{ severity: "major", summary: "sql injection", location: "be/db.py:3" }], "F-fe": [] };
  await gate.execute("toolu_9", { gate: "review", task_id: "F-rev", correlation_id: "c1", per_target_findings: findings }, undefined, undefined, agentCtx(repo, GATE_AGENTS.review));
  const file = join(sdir, "results", "findings-toolu_9.json");
  expect(calls[0].script).toBe("rev_gate");
  expect(calls[0].args).toEqual([`--root=${repo}`, "--task-id=F-rev", "--correlation-id=c1", `--per-target-findings=${file}`]);
  expect(JSON.parse(readFileSync(file, "utf8"))).toEqual(findings);

  // quality and security take per-target findings too, so an agent-reported failure reaches the recorded verdict
  for (const [g, script] of [["quality", "qa_gate"], ["security", "sec_gate"]]) {
    const id = `toolu_${g}`;
    await gate.execute(id, { gate: g, task_id: "F-x", correlation_id: "c1", per_target_findings: findings }, undefined, undefined, agentCtx(repo, GATE_AGENTS[g as keyof typeof GATE_AGENTS]));
    const call = calls[calls.length - 1];
    expect(call.script).toBe(script);
    expect(call.args).toEqual([`--root=${repo}`, "--task-id=F-x", "--correlation-id=c1", `--per-target-findings=${join(sdir, "results", `findings-${id}.json`)}`]);
  }

  for (const [g, script] of [["quality", "qa_gate"], ["security", "sec_gate"], ["release", "rel_plan"]]) {
    await callTool(gate, { gate: g, task_id: "F-x", correlation_id: "c1", dry_run: true }, agentCtx(repo, GATE_AGENTS[g as keyof typeof GATE_AGENTS]));
    const call = calls[calls.length - 1];
    expect(call.script).toBe(script);
    expect(call.args).toEqual([`--root=${repo}`, "--task-id=F-x", "--correlation-id=c1", "--dry-run"]);
  }
});

test("gate scripts match swarm/verdicts.py GATE_SCRIPTS (parity)", async () => {
  const py = Bun.spawnSync(["python3", "-c",
    "import json\nfrom swarm.verdicts import GATE_SCRIPTS\nprint(json.dumps(GATE_SCRIPTS))"],
  { cwd: REPO_ROOT, stdout: "pipe", stderr: "pipe" });
  expect(py.exitCode).toBe(0);
  const expected = JSON.parse(py.stdout.toString()) as Record<string, string>;
  // The schema enum pins the key set: a gate added on either side without the other fails here.
  const tsGates = (gateWith().gate.parameters.properties as Record<string, { enum: string[] }>).gate.enum;
  expect([...tsGates].sort()).toEqual(Object.keys(expected).sort());
  // The bridged script per gate pins the values: drift routes a gate to the wrong script in one runtime.
  process.env.SWARM_DIR = tmpDir("swarm-omp-dir-");
  const repo = gitRepo();
  const { calls, gate } = gateWith();
  for (const [g, script] of Object.entries(expected)) {
    await callTool(gate, { gate: g, task_id: "F-x", correlation_id: "c1" }, agentCtx(repo, GATE_AGENTS[g as keyof typeof GATE_AGENTS]));
    expect(calls[calls.length - 1].script).toBe(script);
  }
});

test("gate extra keys never reach python", async () => {
  const repo = gitRepo();
  const { calls, gate } = gateWith();
  const forged = { gate: "security", task_id: "F-sec", correlation_id: "c1", verdict: "pass", verdicts: { "F-be": "pass" }, args: ["--dry-run"] };
  await callTool(gate, forged, agentCtx(repo, GATE_AGENTS.security));
  expect(calls[0].args).toEqual([`--root=${repo}`, "--task-id=F-sec", "--correlation-id=c1"]);
});

test("gate failing verdict is returned with FAIL: and not thrown", async () => {
  const { gate } = gateWith({ exitCode: 1, json: { status: "fail", verdict: "fail", summary: "review gate FAIL" }, swarmDir: "" });
  const res = await callTool(gate, { gate: "review", task_id: "F-rev", correlation_id: "c1" }, agentCtx(gitRepo(), GATE_AGENTS.review));
  expect(res.content[0].text).toBe("FAIL: review gate FAIL");
  expect((res.details as { verdict: string }).verdict).toBe("fail");
});

test("gate task_id schema pattern accepts exactly the ids python accepts (CR-01)", () => {
  const { parameters } = gateWith().gate;
  const pattern = new RegExp((parameters.properties as Record<string, { pattern: string }>).task_id.pattern);
  const ids = ["T7f3a-be", "F-rev", "c-hot", "0f8e2c1a-9b7d-4e3f-8a21-1b2c3d4e5f60", "a.b_c", "../../esc", "/tmp/evil",
    "a/b", "a\\b", "-x", ".x", "a..b", "a\u0000", "a b", "", "x".repeat(128), "x".repeat(129)];
  const py = Bun.spawnSync(["python3", "-c",
    "import json,sys\nfrom swarm.script_base import check_task_id\nfrom swarm.errors import SwarmError\n" +
    "def ok(v):\n try:\n  check_task_id(v); return True\n except SwarmError:\n  return False\n" +
    "print(json.dumps([ok(v) for v in json.load(sys.stdin)]))"],
  { cwd: REPO_ROOT, stdin: Buffer.from(JSON.stringify(ids)), stdout: "pipe", stderr: "pipe" });
  expect(py.exitCode).toBe(0);
  const accepted = JSON.parse(py.stdout.toString()) as boolean[];
  expect(ids.map((id) => pattern.test(id))).toEqual(accepted);
  expect(accepted.slice(0, 5)).toEqual([true, true, true, true, true]);
  expect(accepted.slice(5, 15).some(Boolean)).toBe(false);
});

test("gate identity: only the gate's own agent runs it; mismatch or unknown identity is E-POLICY before the bridge (WR-03)", async () => {
  const sdir = tmpDir("swarm-omp-dir-");
  process.env.SWARM_DIR = sdir;
  const repo = gitRepo();
  const { calls, gate } = gateWith();
  const gates = Object.keys(GATE_AGENTS) as Array<keyof typeof GATE_AGENTS>;
  const refused = async (g: string, ctx: ReturnType<typeof fakeCtx>) => {
    const params = { gate: g, task_id: "F-x", correlation_id: "c1", ...(g === "review" ? { per_target_findings: { "F-be": [] } } : {}) };
    const err = await callTool(gate, params, ctx).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(SwarmToolError);
    expect((err as SwarmToolError).code).toBe("E-POLICY");
  };
  delete process.env.SWARM_AGENT;
  for (const g of gates) {
    for (const other of gates.filter((o) => o !== g)) await refused(g, agentCtx(repo, GATE_AGENTS[other])); // another gate agent
    await refused(g, agentCtx(repo, "a01-orchestrator")); // a non-gate agent
    await refused(g, fakeCtx(repo)); // no session_init, no SWARM_AGENT: unknown identity
  }
  process.env.SWARM_AGENT = GATE_AGENTS.security;
  await refused("review", fakeCtx(repo)); // headless identity mismatch
  expect(calls).toHaveLength(0);
  expect(existsSync(join(sdir, "results"))).toBe(false);

  for (const g of gates) {
    await callTool(gate, { gate: g, task_id: "F-x", correlation_id: "c1" }, agentCtx(repo, GATE_AGENTS[g]));
    expect(calls[calls.length - 1].script).toBe(g === "quality" ? "qa_gate" : g === "review" ? "rev_gate" : g === "security" ? "sec_gate" : "rel_plan");
  }
  process.env.SWARM_AGENT = ` ${GATE_AGENTS.release} `; // headless `-p`: identity from the runner's env
  await callTool(gate, { gate: "release", task_id: "F-x", correlation_id: "c1" }, fakeCtx(repo));
  expect(calls).toHaveLength(gates.length + 1);
});

test("gate agents are agents.json gate agents granted swarm_gate", () => {
  const manifest = JSON.parse(readFileSync(join(REPO_ROOT, "agents.json"), "utf8")) as { agents: Array<{ slug: string }> };
  const slugs = new Set(manifest.agents.map((a) => a.slug));
  for (const slug of Object.values(GATE_AGENTS)) {
    expect(slugs.has(slug)).toBe(true);
    expect(readFileSync(join(REPO_ROOT, "omp", "agents", `${slug}.md`), "utf8")).toMatch(/^tools: .*\bswarm_gate\b/m);
  }
});
