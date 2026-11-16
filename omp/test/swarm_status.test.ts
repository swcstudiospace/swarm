/**
 * `/swarm-status <correlation-id>` (14.7 Wave 1, Task 4): argument parsing, exactly one orch_status bridge call with the
 * expected argv, refusal without any bridge call, and no write or model-turn seam touched. The bridge is recorded, not
 * run, so there is no python child and no filesystem fixture.
 */
import { expect, spyOn, test } from "bun:test";
import { type Bridge, type BridgeRequest, type BridgeResult, SwarmToolError } from "../src/bridge.ts";
import { createSwarmExtension } from "../src/index.ts";
import { parseSwarmStatusArgs, SWARM_STATUS_USAGE } from "../src/swarm_status.ts";
import { type CallLog, commandCtx, fakePi, tmpDir } from "./helpers.ts";

const CORR = "c0ffee01-2222-4333-8444-555555555555";
const SUMMARY = "no tasks (run orch_plan.py first)";
const OK: BridgeResult = { exitCode: 0, json: { status: "ok", correlation_id: CORR, tasks: [], summary: SUMMARY }, swarmDir: "" };

/** The registered command over a recording bridge; `pi` records runtime actions, so a sendUserMessage would show up. */
function session(outcome: BridgeResult | Error = OK) {
  const log: CallLog = [];
  const bridge: Bridge = async (req) => {
    log.push({ call: "bridge", req });
    if (outcome instanceof Error) throw outcome;
    return outcome;
  };
  const pi = fakePi({ sendUserMessage: (text) => void log.push({ call: "sendUserMessage", text }) });
  pi.load(createSwarmExtension({ bridge }));
  return { log, pi, command: pi.command("swarm-status") };
}

const bridgeCalls = (log: CallLog): BridgeRequest[] => log.filter((entry) => entry.call === "bridge").map((entry) => entry.req as BridgeRequest);
const notices = (log: CallLog) => log.filter((entry) => entry.call === "notify");

test("parses one correlation id and ignores surrounding whitespace", () => {
  expect(parseSwarmStatusArgs(CORR)).toEqual({ correlationId: CORR });
  expect(parseSwarmStatusArgs(`  ${CORR}\t`)).toEqual({ correlationId: CORR });
});

test("refuses empty, extra-token and malformed arguments", () => {
  expect(parseSwarmStatusArgs("")).toEqual({ error: SWARM_STATUS_USAGE });
  expect(parseSwarmStatusArgs("   ")).toEqual({ error: SWARM_STATUS_USAGE });
  expect(parseSwarmStatusArgs(`${CORR} extra`)).toEqual({ error: SWARM_STATUS_USAGE });
  for (const bad of ["--root=/", "-x", "../escape", ".hidden"]) {
    expect("error" in parseSwarmStatusArgs(bad)).toBe(true);
  }
});

test("registers swarm-status beside /swarm, which stays registered", () => {
  const { pi } = session();
  expect([...pi.commands.keys()].sort()).toEqual(["swarm", "swarm-status"]);
});

test("makes exactly one orch_status bridge call with the expected argv", async () => {
  const cwd = tmpDir();
  const { log, command } = session();
  await command.handler(CORR, commandCtx(cwd, [], { log }));
  expect(bridgeCalls(log)).toEqual([expect.objectContaining({ script: "orch_status", args: [`--correlation-id=${CORR}`], cwd })]);
});

test("refuses a missing or malformed correlation id without any bridge call, never falling back to the latest", async () => {
  for (const args of ["", "   ", "--root=/", `${CORR} extra`]) {
    const { log, command } = session();
    await command.handler(args, commandCtx(tmpDir(), [], { log }));
    expect(bridgeCalls(log)).toHaveLength(0);
    expect(notices(log)).toEqual([expect.objectContaining({ level: "warning", message: expect.stringContaining("/swarm-status") })]);
  }
});

test("is read-only: no write flag reaches the bridge and no model turn starts", async () => {
  const { log, pi, command } = session();
  await command.handler(CORR, commandCtx(tmpDir(), [], { log }));
  const calls = bridgeCalls(log);
  expect(calls).toHaveLength(1);
  for (const req of calls) {
    for (const write of ["--transition", "--ingest", "--advisory", "--reason"]) {
      expect(req.args.some((arg) => arg === write || arg.startsWith(`${write}=`))).toBe(false);
    }
  }
  expect(log.map((entry) => entry.call)).not.toContain("sendUserMessage");
  expect(pi.runtimeCalls).not.toContain("sendUserMessage");
});

test("shows the orch_status summary as info text", async () => {
  const { log, command } = session();
  await command.handler(CORR, commandCtx(tmpDir(), [], { log }));
  expect(notices(log)).toEqual([{ call: "notify", message: SUMMARY, level: "info" }]);
});

test("falls back to the JSON text when the result carries no summary", async () => {
  const json = { status: "ok", correlation_id: CORR, tasks: [] };
  const { log, command } = session({ exitCode: 0, json, swarmDir: "" });
  await command.handler(CORR, commandCtx(tmpDir(), [], { log }));
  expect(notices(log)).toEqual([{ call: "notify", message: JSON.stringify(json), level: "info" }]);
});

test("reports a non-zero orch_status exit as an error notice, never a throw", async () => {
  const { log, command } = session({ exitCode: 1, json: { status: "error", error: { message: "E-INPUT: bad correlation" } }, swarmDir: "" });
  await command.handler(CORR, commandCtx(tmpDir(), [], { log }));
  expect(notices(log)).toEqual([
    { call: "notify", message: "/swarm-status: orch_status exit 1: E-INPUT: bad correlation", level: "error" },
  ]);
});

test("reports a bridge error (SwarmToolError) as an error notice", async () => {
  const { log, command } = session(new SwarmToolError("E-DEP", "cannot start python3: missing"));
  await command.handler(CORR, commandCtx(tmpDir(), [], { log }));
  expect(notices(log)).toEqual([{ call: "notify", message: "/swarm-status: E-DEP: cannot start python3: missing", level: "error" }]);
});

test("without a UI the result goes to stderr, never stdout", async () => {
  const { log, command } = session();
  const out: string[] = [];
  const err: string[] = [];
  const outSpy = spyOn(process.stdout, "write").mockImplementation((chunk) => (out.push(String(chunk)), true));
  const errSpy = spyOn(process.stderr, "write").mockImplementation((chunk) => (err.push(String(chunk)), true));
  try {
    await command.handler(CORR, commandCtx(tmpDir(), [], { log, hasUI: false }));
  } finally {
    outSpy.mockRestore();
    errSpy.mockRestore();
  }
  expect(out).toEqual([]);
  expect(err).toEqual([`[/swarm-status] ${SUMMARY}\n`]);
  expect(notices(log)).toEqual([]);
});
