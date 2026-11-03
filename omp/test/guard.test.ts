/**
 * The tool_call guard (Phase 4): HOOK-04 A01 depth cap (D-06), HOOK-03 swarm-state tools (D-05), identity (D-01)
 * and fail modes (D-02), over the pure guardToolCall and through the handler the factory registers.
 */
import { beforeEach, describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { Bridge } from "../src/bridge.ts";
import { callingAgent } from "../src/context.ts";
import { GUARD_ERROR_PREFIX, type GuardFacts, guardToolCall, normalize, parseSignerDenyPaths, RULES, ruleReason, SWARM_SLUGS } from "../src/guard.ts";
import { createSwarmExtension } from "../src/index.ts";
import type { ExtensionContext, SessionEntry, ToolCallEvent } from "../src/omp-api.ts";
import { agentCtx, fakeCtx, fakePi, type FakePiOptions, isolateEnv, REPO_ROOT } from "./helpers.ts";

isolateEnv("SWARM_AGENT", "SWARM_TASK_ID", "SWARM_SUBSTRATE_AGENT", "SUBSTRATE_TOKEN", "SWARM_ED25519_KEY_FILE", "SWARM_SIGNER_DENY_PATHS", "XDG_CONFIG_HOME");
beforeEach(() => {
  delete process.env.SWARM_AGENT;
  delete process.env.SWARM_TASK_ID;
  delete process.env.SWARM_SUBSTRATE_AGENT;
  delete process.env.SUBSTRATE_TOKEN;
  delete process.env.SWARM_ED25519_KEY_FILE;
  delete process.env.SWARM_SIGNER_DENY_PATHS;
  delete process.env.XDG_CONFIG_HOME;
});

const A01 = "a01-orchestrator";
const CWD = "/nonexistent-guard-cwd";
const HOME = "/nonexistent-guard-home";
const TMP = "/nonexistent-guard-tmp";
const DEPTH_PREFIX = "BLOCKED needs: depth";

const call = (toolName: string, input: unknown = {}): ToolCallEvent => ({ toolName, toolCallId: "tc-1", input });
const yieldData = (data: unknown) => call("yield", { data });

/** A01 at the depth cap: no `task`, not a restricted child, not in plan mode. */
const capped: GuardFacts = { agent: A01, restricted: false, planMode: false, hasTask: false, topLevel: false, env: {}, cwd: CWD, home: HOME, tmp: TMP, runtimeRoots: [] };

/** The tool_call handler the real factory registers, with a runtime getActiveTools (a bridge call would throw). */
function guardHandler(opts: FakePiOptions = {}) {
  const bridge: Bridge = () => {
    throw new Error("the guard must never reach the bridge");
  };
  const pi = fakePi(opts);
  pi.load(createSwarmExtension({ bridge }));
  const handler = pi.handler("tool_call");
  return { pi, run: (event: ToolCallEvent, ctx: ExtensionContext) => handler(event, ctx) };
}

/** A01's own session: session_init names it, tools not restricted (a real subagent, not a plan-mode child). */
const a01Ctx = (entries: SessionEntry[] = []) => agentCtx(CWD, A01, entries, false);

describe("HOOK-04", () => {
  test.each(["bash", "read", "write", "edit", "swarm_plan", "swarm_status", "swarm_assignment", "eval", "grep"])(
    "at the cap, %s is blocked with the depth reason naming the one permitted yield",
    (toolName) => {
      const res = guardToolCall(call(toolName, { command: "ls" }), capped);
      expect(res?.block).toBe(true);
      expect(res?.reason?.startsWith(DEPTH_PREFIX)).toBe(true);
      expect(res?.reason).toContain("yield");
      expect(res?.reason).toContain('state: "BLOCKED"');
      expect(res?.reason).toContain('needs: "depth"');
    },
  );

  test.each([
    ["IN_REVIEW", { data: { task_id: "T-1", state: "IN_REVIEW" } }],
    ["DONE", { data: { task_id: "T-1", state: "DONE", needs: "depth" } }],
    ["error", { error: "x" }],
    ["empty input", {}],
    ["empty data", { data: {} }],
    ["BLOCKED without needs", { data: { state: "BLOCKED" } }],
    ['needs ""', { data: { state: "BLOCKED", needs: "" } }],
    ["needs []", { data: { state: "BLOCKED", needs: [] } }],
    ["needs {}", { data: { state: "BLOCKED", needs: {} } }],
    ['needs "depths"', { data: { state: "BLOCKED", needs: "depths" } }],
    ['needs "no-depth"', { data: { state: "BLOCKED", needs: "no-depth" } }],
    ['needs ["depths"]', { data: { state: "BLOCKED", needs: ["depths"] } }],
    ["needs {Depth} (object keys are exact)", { data: { state: "BLOCKED", needs: { Depth: 1 } } }],
    ["inherited depth key", { data: { state: "BLOCKED", needs: Object.create({ depth: 1 }) } }],
    ['state "blocked" (case-sensitive)', { data: { state: "blocked", needs: "depth" } }],
    ["needs without state", { data: { needs: "depth" } }],
    ["null input", null],
    ["string input", "BLOCKED depth"],
    ["array data", { data: ["BLOCKED", "depth"] }],
  ])("at the cap, yield %s is blocked", (_name, input) => {
    const res = guardToolCall(call("yield", input), capped);
    expect(res?.block).toBe(true);
    expect(res?.reason?.startsWith(DEPTH_PREFIX)).toBe(true);
  });

  test.each([
    ['"depth"', "depth"],
    ['["depth"]', ["depth"]],
    ["{depth: …}", { depth: "cap" }],
    ['" Depth "', " Depth "],
    ['["other", "DEPTH"]', ["other", "DEPTH"]],
  ])("at the cap, yield BLOCKED with needs %s is allowed without task_id", (_name, needs) => {
    expect(guardToolCall(yieldData({ state: "BLOCKED", needs }), capped)).toBeUndefined();
    expect(guardToolCall(yieldData({ task_id: "T-1", correlation_id: "C-1", state: "BLOCKED", needs }), capped)).toBeUndefined();
  });

  test.each([
    ["task active", { hasTask: true }],
    ["restricted child", { restricted: true }],
    ["plan mode", { planMode: true }],
  ])("one step off the cap (%s): no HOOK-04 block", (_name, override) => {
    const facts = { ...capped, ...override };
    expect(guardToolCall(call("read", { path: "x" }), facts)).toBeUndefined();
    expect(guardToolCall(yieldData({ state: "IN_REVIEW" }), facts)).toBeUndefined();
  });

  test.each([["a05-backend"], ["task"], [undefined]])("agent %p without task is not capped", (agent) => {
    expect(guardToolCall(call("read", { path: "x" }), { ...capped, agent, topLevel: agent === undefined })).toBeUndefined();
    expect(guardToolCall(yieldData({ state: "DONE" }), { ...capped, agent, topLevel: agent === undefined })).toBeUndefined();
  });

  test("adjacency: A01 at the cap calling swarm_transition gets the depth reason", () => {
    const res = guardToolCall(call("swarm_transition", { task_id: "T-1", to: "DONE" }), capped);
    expect(res?.reason?.startsWith(DEPTH_PREFIX)).toBe(true);
  });

  test("pure: repeated calls give equal results and leave event and facts unchanged", () => {
    const events = [call("bash", { command: "ls" }), yieldData({ state: "BLOCKED", needs: ["depth"] }), call("swarm_ingest", {})];
    const facts: GuardFacts = { ...capped, env: { SWARM_AGENT: A01 } };
    const before = JSON.stringify({ events, facts });
    for (const event of events) expect(guardToolCall(event, facts)).toEqual(guardToolCall(event, facts));
    expect(JSON.stringify({ events, facts })).toBe(before);
  });

  test("registered handler: A01 without task reproduces the pure decision", () => {
    const { pi, run } = guardHandler({ activeTools: ["read", "bash", "yield"] });
    const events = [
      call("bash", { command: "ls" }),
      call("swarm_transition", {}),
      yieldData({ state: "IN_REVIEW" }),
      yieldData({ state: "BLOCKED", needs: "depth" }),
    ];
    for (const event of events) expect(run(event, a01Ctx())).toEqual(guardToolCall(event, capped));
    expect(run(call("bash", {}), a01Ctx())).toEqual({ block: true, reason: expect.stringMatching(/^BLOCKED needs: depth/) });
    expect(pi.runtimeCalls.length).toBe(events.length + 1);
  });

  test("registered handler: A01 with task, as a restricted child or in plan mode is not capped", () => {
    const withTask = guardHandler({ activeTools: ["read", "task", "yield"] });
    expect(withTask.run(call("bash", {}), a01Ctx())).toBeUndefined();

    const noTask = guardHandler({ activeTools: ["read", "yield"] });
    expect(noTask.run(call("bash", {}), agentCtx(CWD, A01))).toBeUndefined(); // restrictToolNames: true
    expect(noTask.run(call("bash", {}), a01Ctx([{ type: "mode_change", mode: "plan" }]))).toBeUndefined();
  });

  test("registered handler: headless A01 (no session_init, SWARM_AGENT) is capped from its env identity", () => {
    process.env.SWARM_AGENT = A01;
    const { run } = guardHandler({ activeTools: ["read"] });
    expect((run(call("bash", {}), fakeCtx(CWD)) as { reason?: string } | undefined)?.reason?.startsWith(DEPTH_PREFIX)).toBe(true);
  });
});

describe("fail", () => {
  const throwing = () => {
    throw new Error("active tools unavailable");
  };
  /** A ctx whose session log reads session_init fine but whose branch read (plan mode) throws. */
  const brokenBranch = (entries: SessionEntry[]): ExtensionContext => ({
    cwd: CWD,
    sessionManager: {
      getEntries: () => entries,
      getBranch: () => {
        throw new Error("branch unreadable");
      },
    },
  });

  test("getActiveTools is called only when the resolved agent is a01-orchestrator", () => {
    const { pi, run } = guardHandler({ activeTools: ["task"] });
    run(call("bash", {}), agentCtx(CWD, "a05-backend", [], false));
    run(call("bash", {}), agentCtx(CWD, "task", [], false));
    run(call("bash", {}), fakeCtx(CWD));
    expect(pi.runtimeCalls).toEqual([]);
    run(call("bash", {}), a01Ctx());
    expect(pi.runtimeCalls).toEqual(["getActiveTools"]);
  });

  test("a throwing getActiveTools for A01 blocks with the guard-error reason", () => {
    const { run } = guardHandler({ activeTools: throwing });
    const res = run(yieldData({ state: "BLOCKED", needs: "depth" }), a01Ctx()) as { block?: boolean; reason?: string };
    expect(res.block).toBe(true);
    expect(res.reason?.startsWith(GUARD_ERROR_PREFIX)).toBe(true);
    expect(res.reason).toContain("active tools unavailable");
  });

  test("a throwing getActiveTools never reaches the main session: its call passes", () => {
    const { pi, run } = guardHandler({ activeTools: throwing });
    expect(run(call("bash", { command: "ls" }), fakeCtx(CWD))).toBeUndefined();
    expect(pi.runtimeCalls).toEqual([]);
  });

  test.each([
    ["the main session", [] as SessionEntry[]],
    ["a generic task child", [{ type: "session_init", agent: "task", restrictToolNames: false }]],
  ])("a guard throw in %s returns undefined", (_name, entries) => {
    const { run } = guardHandler({ activeTools: ["task"] });
    expect(run(call("bash", { command: "ls" }), brokenBranch(entries))).toBeUndefined();
  });

  const unreadableSession: ExtensionContext = {
    cwd: CWD,
    sessionManager: {
      getEntries: () => {
        throw new Error("session log unreadable");
      },
      getBranch: () => {
        throw new Error("branch must not be read");
      },
    },
  };

  test("an unreadable session log without a swarm marker leaves main-session tools unaffected", () => {
    const { pi, run } = guardHandler({ activeTools: ["task"] });
    expect(run(call("bash", {}), unreadableSession)).toBeUndefined();
    expect(pi.runtimeCalls).toEqual([]);
  });

  test.each(["SWARM_TASK_ID", "SWARM_AGENT"])("an unreadable session log with %s fails closed before reading active tools", (marker) => {
    process.env[marker] = marker === "SWARM_TASK_ID" ? "T-1" : "a05-backend";
    const { pi, run } = guardHandler({ activeTools: ["task"] });
    expect(run(call("bash", {}), unreadableSession)).toEqual({
      block: true,
      reason: `${GUARD_ERROR_PREFIX}session log unreadable)`,
    });
    expect(pi.runtimeCalls).toEqual([]);
  });

  test.each([
    ["a swarm specialist", [{ type: "session_init", agent: "a05-backend", restrictToolNames: false }], undefined],
    ["a headless swarm session (SWARM_TASK_ID)", [] as SessionEntry[], "T-1"],
  ])("a guard throw in %s blocks with the guard-error reason", (_name, entries, taskId) => {
    if (taskId !== undefined) process.env.SWARM_TASK_ID = taskId;
    const { run } = guardHandler({ activeTools: ["task"] });
    const res = run(call("read", { path: "x" }), brokenBranch(entries)) as { block?: boolean; reason?: string };
    expect(res.block).toBe(true);
    expect(res.reason?.startsWith(GUARD_ERROR_PREFIX)).toBe(true);
    expect(res.reason).toContain("branch unreadable");
  });
});

describe("HOOK-03", () => {
  const SWARM_STATE = "BLOCKED needs: human-approval (swarm-state)";
  const STATE_TOOLS = ["swarm_transition", "swarm_ingest"];
  const specialist = agentCtx(CWD, "a05-backend", [], false);

  test("SWARM_SLUGS is exactly the agents.json slug list", () => {
    const manifest = JSON.parse(readFileSync(join(REPO_ROOT, "agents.json"), "utf8")) as { agents: { slug: string }[] };
    expect([...SWARM_SLUGS].sort()).toEqual(manifest.agents.map((a) => a.slug).sort());
  });

  test.each([
    ["a05-backend (restricted)", agentCtx(CWD, "a05-backend")],
    ["a05-backend", specialist],
    ["a12-release", agentCtx(CWD, "a12-release", [], false)],
    ["a generic task child", agentCtx(CWD, "task", [], false)],
    ["the unidentified main session", fakeCtx(CWD)],
  ])("from %s, swarm_transition and swarm_ingest are blocked", (_name, ctx) => {
    const { run } = guardHandler({ activeTools: ["task"] });
    for (const toolName of STATE_TOOLS) {
      expect(run(call(toolName, { task_id: "T-1", to: "DONE" }), ctx)).toEqual({ block: true, reason: SWARM_STATE });
    }
  });

  test.each([["a05-backend"], ["task"], [undefined]])("pure: agent %p is blocked on both state tools", (agent) => {
    const facts: GuardFacts = { agent, restricted: true, planMode: false, hasTask: true, topLevel: agent === undefined, env: {}, cwd: CWD, home: HOME, tmp: TMP, runtimeRoots: [] };
    for (const toolName of STATE_TOOLS) expect(guardToolCall(call(toolName), facts)).toEqual({ block: true, reason: SWARM_STATE });
  });

  test("from a01-orchestrator with task active, both state tools pass", () => {
    const { run } = guardHandler({ activeTools: ["read", "task", "swarm_transition", "swarm_ingest"] });
    for (const toolName of STATE_TOOLS) {
      expect(run(call(toolName, { task_id: "T-1" }), a01Ctx())).toBeUndefined();
      expect(guardToolCall(call(toolName), { ...capped, hasTask: true })).toBeUndefined();
    }
  });

  test("env bleed: session_init a05-backend with SWARM_AGENT=a01-orchestrator is still blocked", () => {
    process.env.SWARM_AGENT = A01;
    expect(callingAgent(specialist)).toBe("a05-backend");
    const { pi, run } = guardHandler({ activeTools: ["task"] });
    for (const toolName of STATE_TOOLS) expect(run(call(toolName), specialist)).toEqual({ block: true, reason: SWARM_STATE });
    expect(pi.runtimeCalls).toEqual([]); // not resolved as A01, so the A01-only read never happens
  });

  test("top-level session with SWARM_AGENT=a01-orchestrator and no session_init is allowed", () => {
    process.env.SWARM_AGENT = ` ${A01} `;
    const { run } = guardHandler({ activeTools: ["task"] });
    for (const toolName of STATE_TOOLS) expect(run(call(toolName), fakeCtx(CWD))).toBeUndefined();
  });

  test("top-level session with a blank SWARM_AGENT stays unidentified and blocked", () => {
    process.env.SWARM_AGENT = "  ";
    const { run } = guardHandler({ activeTools: ["task"] });
    for (const toolName of STATE_TOOLS) expect(run(call(toolName), fakeCtx(CWD))).toEqual({ block: true, reason: SWARM_STATE });
  });

  test.each([["swarm_plan"], ["swarm_status"]])("%s from a05-backend is not a swarm-state call", (toolName) => {
    const { run } = guardHandler({ activeTools: ["task"] });
    expect(run(call(toolName, { brief: "x" }), specialist)).toBeUndefined();
    expect(run(call(toolName, { brief: "x" }), fakeCtx(CWD))).toBeUndefined();
  });
});

describe("canonical assignment authority", () => {
  const denied = { block: true, reason: "BLOCKED needs: human-approval (swarm-assignment)" };
  const assignment = call("swarm_assignment", { task_id: "T-req", correlation_id: "c1" });

  test.each([...SWARM_SLUGS.filter((slug) => slug !== A01), "task", undefined])(
    "pure guard denies assignment to %p before the outside-swarm return",
    (agent) => {
      expect(guardToolCall(assignment, { ...capped, agent, hasTask: true, topLevel: agent === undefined })).toEqual(denied);
    },
  );

  test("authenticated A01 outside plan mode is allowed, but plan mode and the depth cap remain denied", () => {
    expect(guardToolCall(assignment, { ...capped, hasTask: true })).toBeUndefined();
    expect(guardToolCall(assignment, { ...capped, hasTask: true, planMode: true })).toEqual(denied);
    expect(guardToolCall(assignment, capped)?.reason?.startsWith(DEPTH_PREFIX)).toBe(true);
  });

  test("registered handler respects controller identity, plan mode and env isolation", () => {
    const { run } = guardHandler({ activeTools: ["task"] });
    expect(run(assignment, fakeCtx(CWD))).toEqual(denied);
    expect(run(assignment, a01Ctx())).toBeUndefined();
    expect(run(assignment, a01Ctx([{ type: "mode_change", mode: "plan" }]))).toEqual(denied);
    process.env.SWARM_AGENT = A01;
    expect(run(assignment, fakeCtx(CWD))).toBeUndefined();
    expect(run(assignment, agentCtx(CWD, "task", [], false))).toEqual(denied);
    expect(run(assignment, agentCtx(CWD, "a05-backend", [], false))).toEqual(denied);
  });

  test("A01 and specialists cannot obtain assignment authority through shell script execution or relocation", () => {
    for (const agent of SWARM_SLUGS) {
      for (const command of [
        "python3 scripts/orch_assign.py --task-id T-req --correlation-id c1",
        "python3 -m scripts.orch_assign --task-id T-req --correlation-id c1",
        "python3 - --task-id T-req < scripts/orch_assign.py",
        "cp scripts/orch_assign.py /tmp/assign.py && python3 /tmp/assign.py",
      ]) {
        expect(guardToolCall(call("bash", { command }), { ...capped, agent, hasTask: true })).toEqual(denied);
      }
    }
  });

  test("typed assignment does not reopen eval, raw code, signer reads or protected state writes", () => {
    const facts: GuardFacts = { ...capped, hasTask: true };
    for (const event of [
      call("eval", { code: "arbitrary" }),
      call("write", { path: "xd://run_code", content: "{}" }),
      call("write", { path: `${CWD}/.swarm/tasks.db`, content: "forged" }),
      call("read", { path: `${HOME}/.config/agent-swarm/signing-key.hex` }),
    ]) {
      expect(guardToolCall(event, facts)?.block).toBe(true);
    }
  });
});

describe("HOOK-02", () => {
  const B05 = "a05-backend";
  const inside = (agent = B05): GuardFacts => ({ agent, restricted: false, planMode: false, hasTask: true, topLevel: false, env: {}, cwd: CWD, home: HOME, tmp: TMP, runtimeRoots: [] });
  const main: GuardFacts = { ...inside(), agent: undefined, topLevel: true };
  const bash = (command: string) => call("bash", { command });
  const blockedWith = (res: ReturnType<typeof guardToolCall>, capability: string) => {
    expect(res?.block).toBe(true);
    expect(res?.reason?.startsWith(`BLOCKED needs: human-approval (${capability}: `)).toBe(true);
  };

  test("every RULES row carries at least one positive sample", () => {
    for (const rule of RULES) expect(rule.samples.length).toBeGreaterThan(0);
  });

  const rows = RULES.flatMap((rule) => rule.samples.map((sample) => [rule.id, sample, rule] as const));
  test.each(rows)("row %s: %s blocks in a swarm session (handler) and passes in main", (id, sample, rule) => {
    const { run } = guardHandler({ activeTools: ["task"] });
    const agent = rule.agents?.[0] ?? B05;
    const res = run(bash(sample), agentCtx(CWD, agent, [], false)) as { block?: boolean; reason?: string };
    expect(res).toEqual({ block: true, reason: ruleReason(rule) });
    if (rule.reason === undefined) expect(ruleReason(rule)).toBe(`BLOCKED needs: human-approval (${rule.capability}: ${id})`);
    expect(run(bash(sample), fakeCtx(CWD))).toBeUndefined();
  });

  /** Independent of RULES: one command per D-03/D-04 item and the capability it must name. */
  const D03_CASES: [string, string][] = [
    ["git push --force", "destructive"],
    ["git push -f", "destructive"],
    ["git push --force-with-lease", "destructive"],
    ["git reset --hard", "destructive"],
    ["git clean -fd", "destructive"],
    ["git clean -fx", "destructive"],
    ["git branch -D x", "destructive"],
    ["git checkout -- .", "destructive"],
    ["git restore .", "destructive"],
    ["rm -rf /", "destructive"],
    ["rm -rf ~", "destructive"],
    ["rm -rf ..", "destructive"],
    ["rm -rf .git", "destructive"],
    ["rm -rf .swarm", "destructive"],
    ["rm -rf /etc/x", "destructive"],
    ["chmod -R 777 .", "destructive"],
    ['psql -c "DROP TABLE t"', "destructive_ddl"],
    ['mysql -e "ALTER TABLE t DROP COLUMN c"', "destructive_ddl"],
    ['sqlite3 db "DROP DATABASE d"', "destructive_ddl"],
    ['psql -c "DROP SCHEMA s"', "destructive_ddl"],
    ['psql -c "TRUNCATE t"', "destructive_ddl"],
    ['prisma db execute --stdin <<< "DROP TABLE t"', "destructive_ddl"],
    ["prisma migrate reset", "destructive_ddl"],
    ["prisma db push --accept-data-loss", "destructive_ddl"],
    ["terraform apply", "prod_infra"],
    ["terraform destroy", "prod_infra"],
    ["pulumi up", "prod_infra"],
    ["pulumi destroy", "prod_infra"],
    ["helm install app ./c", "prod_infra"],
    ["helm upgrade app ./c", "prod_infra"],
    ["helm uninstall app", "prod_infra"],
    ["aws ec2 terminate-instances --instance-ids i-1", "prod_infra"],
    ["gcloud app deploy", "prod_infra"],
    ["kubectl apply -n production -f k.yaml", "prod_infra"],
    ["kubectl delete pod x -n production", "prod_infra"],
    ["kubectl rollout restart deploy/api -n production", "prod_infra"],
    ["kubectl scale deploy/api --replicas=3 -n production", "prod_infra"],
    ["vercel --prod", "prod_high_risk"],
    ["fly deploy", "prod_high_risk"],
    ["gh release create v1", "prod_high_risk"],
    ["npm publish", "prod_high_risk"],
    ["pnpm publish", "prod_high_risk"],
    ["bun publish", "prod_high_risk"],
    ["docker push img", "prod_high_risk"],
    ["git push --tags", "prod_high_risk"],
    ["git push origin main", "prod_high_risk"],
    ["git push origin master", "prod_high_risk"],
  ];
  test.each(D03_CASES)("D-03: %s blocks naming %s inside, nothing in main", (command, capability) => {
    blockedWith(guardToolCall(bash(command), inside()), capability);
    expect(guardToolCall(bash(command), main)).toBeUndefined();
  });

  test.each([
    ["sudo git reset --hard", "git-reset-hard"],
    ["env A=1 git clean -fdx", "git-clean-force"],
    ["command rm -rf /", "rm-rf-protected"],
    ["git status && git push -f", "git-force-push"],
    ["cd x && sudo git push -f", "git-force-push"],
    ["ls | xargs echo ; git branch -D x", "git-branch-force-delete"],
    ["echo $(git reset --hard)", "git-reset-hard"],
    ["A=1 B=2 env C=3 sudo -E command git -C sub reset --hard", "git-reset-hard"],
    ["/usr/bin/git push --force", "git-force-push"],
    // WR-01: a quoted assignment value with spaces, and sudo/env flags that take a value
    ['GIT_SSH_COMMAND="ssh -i k" git push --force', "git-force-push"],
    ["DATABASE_URL='postgres://u p@h/db' prisma migrate reset", "prisma-migrate-reset"],
    ['X="a b" Y=\'c d\' git push -f', "git-force-push"],
    ["X=a\\ b git push -f", "git-force-push"],
    ["sudo -u root git reset --hard", "git-reset-hard"],
    ["sudo --user root -E git push -f", "git-force-push"],
    ["env -u HOME -i git clean -fdx", "git-clean-force"],
    ["doas -u root git reset --hard", "git-reset-hard"],
    // WR-02: subshell and brace grouping, backslash-newline continuation
    ["(git push -f)", "git-force-push"],
    ["{ git push -f; }", "git-force-push"],
    ["(cd sub && git reset --hard)", "git-reset-hard"],
    ["f() { git clean -fdx; }", "git-clean-force"],
    ["git \\\npush --force", "git-force-push"],
    ["git push \\\r\n  --force origin x", "git-force-push"],
    // WR-03: an apostrophe in a heredoc body or a comment must not swallow the commands after it
    ["cat <<EOF > notes.md\nDon't panic\nEOF\ngit push --force", "git-force-push"],
    ["cat <<-'EOF' > n.md\n\tDon't\n\tEOF\ngit reset --hard", "git-reset-hard"],
    ["# don't\ngit push -f", "git-force-push"],
    ["echo a #don't\ngit reset --hard", "git-reset-hard"],
    ["echo don't ; git push -f", "git-force-push"],
    // WR-04: wrappers and a literal sh -c / eval argument
    ['bash -c "git push --force"', "git-force-push"],
    ["sh -c 'git reset --hard'", "git-reset-hard"],
    ["/bin/bash -x -ec \"cd x && sh -c 'git clean -fdx'\" arg0", "git-clean-force"],
    ['bash -c "echo don\\"t; git push -f"', "git-force-push"],
    ["timeout 60 git push --force", "git-force-push"],
    ["timeout -s KILL 5m nice -n 5 git push -f", "git-force-push"],
    ["time git push -f", "git-force-push"],
    ["nohup git push -f &", "git-force-push"],
    ["echo origin | xargs git push -f", "git-force-push"],
    ["xargs -n 1 -I{} git push -f {}", "git-force-push"],
    ['eval "git push -f"', "git-force-push"],
    ["eval git push -f", "git-force-push"],
    ["stdbuf -oL exec git reset --hard", "git-reset-hard"],
    // WR-05: +refspec force pushes, --delete --force, value-taking git global options
    ["git push origin +feat/x", "git-force-push"],
    ["git push origin feat/x +HEAD:refs/heads/feat", "git-force-push"],
    ["git branch --delete --force feat", "git-branch-force-delete"],
    ["git branch -d -f feat", "git-branch-force-delete"],
    ["git branch -df feat", "git-branch-force-delete"],
    ["git --work-tree /x push --force", "git-force-push"],
    ["git --git-dir /x/.git --namespace n reset --hard", "git-reset-hard"],
    ["git -c core.x=1 --no-pager push origin +feat", "git-force-push"],
    // T-05-24: a wrapper word spelled as a path strips exactly like the bare word
    ["/usr/bin/env git push --force origin main", "git-force-push"],
    ["/bin/env git push -f", "git-force-push"],
    ["../tools/env A=1 git clean -fdx", "git-clean-force"],
    ["/usr/bin/sudo -u x git reset --hard", "git-reset-hard"],
    ["/usr/bin/doas git reset --hard", "git-reset-hard"],
    ["/usr/bin/command -p git push -f", "git-force-push"],
    ["/usr/bin/time -p git reset --hard", "git-reset-hard"],
    ["/usr/bin/nohup git push -f &", "git-force-push"],
    ["/usr/bin/timeout -s KILL 5m /usr/bin/nice -n 5 git push -f", "git-force-push"],
    ["/usr/bin/stdbuf -oL git reset --hard", "git-reset-hard"],
    ["echo origin | /usr/bin/xargs -n 1 git push -f", "git-force-push"],
    ["/usr/bin/eval 'git push -f'", "git-force-push"],
  ])("normalization: %s → %s", (command, id) => {
    expect(guardToolCall(bash(command), inside())?.reason).toEndWith(`: ${id})`);
  });

  test("normalize splits segments and strips prefixes", () => {
    // a substitution body follows the segment that holds it (it runs as that segment's subshell)
    expect(normalize("env A=1 sudo git status && echo $(git reset --hard) || x | y; z")).toEqual([
      "git status", "echo", "git reset --hard", "x", "y", "z",
    ]);
    expect(normalize('psql -c "SELECT 1; DROP TABLE t" | cat')).toEqual(['psql -c "SELECT 1; DROP TABLE t"', "cat"]);
    expect(normalize('A="b c" B=1 env -u X sudo -u root command -p git status')).toEqual(["git status"]);
    expect(normalize("/usr/bin/env -i /usr/bin/sudo -u root /bin/nice -n 5 git status")).toEqual(["git status"]);
    expect(normalize("(cd x && git status) | { read a; echo ${a} {1,2}; }")).toEqual(["cd x", "git status", "read a", "echo ${a} {1,2}"]);
    expect(normalize('echo "(x)" && find . \\( -name x \\)')).toEqual(['echo "(x)"', "find . \\( -name x \\)"]);
    expect(normalize("cat <<EOF > notes.md\nDon't panic; git push -f\nEOF\necho done")).toEqual(["cat <<EOF > notes.md", "echo done"]);
    expect(normalize("echo a#b 'c;d' \"e|f\" x && psql <<< 'SELECT 1; DROP TABLE t'")).toEqual(["echo a#b 'c;d' \"e|f\" x", "psql <<< 'SELECT 1; DROP TABLE t'"]);
    expect(normalize("cat <<EOF\nno delimiter\ngit push -f")).toEqual(["cat <<EOF"]);
    expect(normalize("/bin/bash -x -ec \"cd x && sh -c 'git clean -fdx'\" arg0")).toEqual([
      "/bin/bash -x -ec \"cd x && sh -c 'git clean -fdx'\" arg0", "cd x", "sh -c 'git clean -fdx'", "git clean -fdx",
    ]);
    expect(normalize('bash -c "$VAR" && bash script.sh')).toEqual(['bash -c "$VAR"', "$VAR", "bash script.sh"]);
  });

  // Budget covers the differential leg and the fresh-process scaling probe, which take about 1-2 minutes under host load.
  test("normalize is linear: 1 MB of stacked prefixes, quotes or groups scales linearly, never superlinearly (WR-09)", { timeout: 600_000 }, () => {
    // Nested `$(…)` chain: each level's body holds the next level's substitution, innermost body `git status`.
    // Each level is 14 bytes; the body text is padded so every cut body carries real words.
    const nestedSubstitution = (levels: number): string => `${"$(echo x y z ".repeat(levels)}git status${")".repeat(levels)}`;
    // Same-size differential per shape: [name, adversarial input, expected for adversarial].
    // Adversarial keeps the original 1 MB inputs, so full-scale correctness coverage is unchanged.
    const shapes: [string, string, string[] | undefined][] = [
      ["assignment prefixes", `${"A=1 ".repeat(250_000)}git status`, ["git status"]],
      ["sudo stacking", `${"sudo ".repeat(200_000)}git status`, ["git status"]],
      ["timeout stacking", `${"timeout 1 ".repeat(100_000)}git status`, ["git status"]],
      ["sudo -u flags", `sudo ${"-u ".repeat(300_000)}git status`, ["git status"]],
      ["env stacking", `${"/usr/bin/env ".repeat(100_000)}git status`, ["git status"]],
      ["long path word", `${"/".repeat(1_000_000)} x`, [`${"/".repeat(1_000_000)} x`]],
      ["long value", `A=${"b".repeat(1_000_000)}`, [`A=${"b".repeat(1_000_000)}`]],
      ["quoted pairs", "'a b' ".repeat(150_000), undefined],
      ["brace groups", "x { ".repeat(200_000), undefined],
      ["heredoc lines", `${"cat <<EOF\n".repeat(1000)}${"x\n".repeat(100_000)}`, undefined],
      ["nested substitution", nestedSubstitution(71_000), undefined],
    ];
    // Same-shape builder for the scaling leg: rebuilds each adversarial shape
    // at an arbitrary byte size with the same repeat unit, so the small and
    // large inputs exercise the identical code path at two sizes. Prefix
    // shapes keep their trailing `git status` (never sliced); repeated-token
    // shapes are truncated to the exact size (a cut token is still the same
    // class); heredoc keeps its 1000-line header and scales the body.
    const scalingFor = (name: string, bytes: number): string => {
      switch (name) {
        case "assignment prefixes": return `${"A=1 ".repeat(Math.max(1, Math.ceil((bytes - 10) / 4)))}git status`;
        case "sudo stacking": return `${"sudo ".repeat(Math.max(1, Math.ceil((bytes - 10) / 5)))}git status`;
        case "timeout stacking": return `${"timeout 1 ".repeat(Math.max(1, Math.ceil((bytes - 10) / 10)))}git status`;
        case "sudo -u flags": return `sudo ${"-u ".repeat(Math.max(1, Math.ceil((bytes - 15) / 3)))}git status`;
        case "env stacking": return `${"/usr/bin/env ".repeat(Math.max(1, Math.ceil((bytes - 10) / 13)))}git status`;
        case "long path word": return `${"/".repeat(Math.max(1, bytes - 2))} x`;
        case "long value": return `A=${"b".repeat(Math.max(1, bytes - 2))}`;
        case "quoted pairs": return "'a b' ".repeat(Math.max(1, Math.ceil(bytes / 6))).slice(0, bytes);
        case "brace groups": return "x { ".repeat(Math.max(1, Math.ceil(bytes / 4))).slice(0, bytes);
        case "heredoc lines": {
          const header = "cat <<EOF\n".repeat(1000);
          return header + "x\n".repeat(Math.max(1, Math.ceil((bytes - header.length) / 2)));
        }
        case "nested substitution": return nestedSubstitution(Math.max(1, Math.floor((bytes - 10) / 14)));
        default: throw new Error(`scalingFor: unknown shape ${name}`);
      }
    };
    // Scaling, not wall-clock: each adversarial input is compared against a
    // benign reference of EQUAL byte length (repeated `git status ;` units,
    // truncated to the exact length), so fixed overhead, JIT/GC regime, and
    // heap state cancel out instead of inflating the ratio. The old cross-size
    // ratio compared a small input below normalize's ~200 KB regime knee
    // against a large input above it, which made the ratio inherently
    // environment-sensitive (isolation passed, full suite failed: 43.0x vs the
    // 40x cap on `sudo -u flags`). Equal-size inputs have no knee to straddle.
    // Reps interleave back-to-back (A,B,A,B...) in this process, so CI load
    // slows both sides together and the ratio cannot flake; best-of-N takes
    // the min each side. Linear work costs ~1x against its equal-size
    // reference; the WR-09 backtracking class costs ~1000x or more, so a 40x
    // cap catches a genuine regression decisively with wide headroom. No
    // absolute wall-clock bound is asserted. The { timeout } above is a
    // runaway budget, not a correctness assertion: a genuine superlinear hang
    // still fails fast (via the ratio or the timeout) instead of hanging CI,
    // and normal runs never approach it. Best-of-3 keeps the full-suite cost
    // at ~2-4s even under contention from the ~1400 preceding tests.
    const LINEAR_CAP = 40;
    const REPS = 3;
    const BENIGN_UNIT = "git status ; ";
    const referenceFor = (bytes: number): string =>
      BENIGN_UNIT.repeat(Math.ceil(bytes / BENIGN_UNIT.length)).slice(0, bytes);
    normalize("git status"); // warm up (JIT) before measuring
    const over: string[] = [];
    for (const [name, adversarial, expected] of shapes) {
      // Correctness at the original 1 MB scale is unchanged.
      if (expected !== undefined) expect(normalize(adversarial)).toEqual(expected);
      // The reference must exercise normalize() genuinely, not be skipped.
      const reference = referenceFor(adversarial.length);
      expect(reference.length).toBe(adversarial.length);
      expect(normalize(reference).length).toBeGreaterThan(0);
      let adversarialMs = Infinity;
      let referenceMs = Infinity;
      for (let i = 0; i < REPS; i++) {
        let t = performance.now();
        normalize(adversarial);
        const advMs = performance.now() - t;
        if (advMs < adversarialMs) adversarialMs = advMs;
        t = performance.now();
        normalize(reference);
        const refMs = performance.now() - t;
        if (refMs < referenceMs) referenceMs = refMs;
      }
      const ratio = adversarialMs / referenceMs;
      if (ratio >= LINEAR_CAP) over.push(`${name}: ${ratio.toFixed(1)}x (adversarial ${adversarialMs.toFixed(1)}ms / reference ${referenceMs.toFixed(1)}ms, best of ${REPS})`);
    }
    expect(over).toEqual([]);
    // Same-shape scaling leg: the differential above cannot catch shared-path
    // quadratic work, because both sides of an equal-size comparison grow
    // together. So each adversarial shape is timed against ITSELF at four sizes
    // (200 KB, 400 KB, 800 KB, 1.6 MB) in a fresh bun process: earlier tests in
    // this file leave a large heap and warm JIT state, which swung single samples
    // several-fold on a loaded host. CPU time, best of 5 per size. Linear work
    // doubles per doubling of input; quadratic work quadruples. The check is the
    // MEDIAN of the three per-doubling growth factors, so one noisy size cannot
    // fail it, and it must stay below SCALING_CAP (3x: linear reads ~2x, quadratic ~4x).
    const SIZES = [200_000, 400_000, 800_000, 1_600_000];
    const SCALING_CAP = 3;
    const shapeNames = shapes.map(([name]) => name);
    const guardPath = new URL("../src/guard.ts", import.meta.url).pathname;
    const probe = `
      import { readFileSync } from "node:fs";
      import { normalize } from ${JSON.stringify(guardPath)};
      const inputs = JSON.parse(readFileSync(0, "utf8"));
      const cpuMs = (run) => {
        const before = process.cpuUsage();
        run();
        const used = process.cpuUsage(before);
        return (used.user + used.system) / 1000;
      };
      const out = {};
      for (const [name, inputsAtSizes] of Object.entries(inputs)) {
        out[name] = inputsAtSizes.map((input) => {
          let best = Infinity;
          for (let i = 0; i < 5; i++) {
            Bun.gc(true);
            best = Math.min(best, cpuMs(() => normalize(input)));
          }
          return best;
        });
      }
      console.log(JSON.stringify(out));
    `;
    const sizes = Object.fromEntries(shapeNames.map((name) => [name, SIZES.map((bytes) => scalingFor(name, bytes))]));
    const probeRun = Bun.spawnSync([process.execPath, "-e", probe], { stdin: new TextEncoder().encode(JSON.stringify(sizes)), stdout: "pipe", stderr: "pipe" });
    if (probeRun.exitCode !== 0) throw new Error(`scaling probe exited ${probeRun.exitCode}: ${probeRun.stderr.toString()}`);
    const measuredLine = probeRun.stdout.toString().trim().split("\n").at(-1) ?? "{}";
    const measured = JSON.parse(measuredLine) as Record<string, number[]>;
    const scalingOver: string[] = [];
    for (const name of shapeNames) {
      const times = measured[name];
      const growth = times.slice(1).map((t, i) => t / times[i]).sort((a, b) => a - b);
      const medianGrowth = growth[Math.floor(growth.length / 2)];
      if (medianGrowth >= SCALING_CAP) scalingOver.push(`${name}: median ${medianGrowth.toFixed(2)}x per size doubling (CPU ms at ${SIZES.join(" / ")} B: ${times.map((t) => t.toFixed(1)).join(" / ")}; cap ${SCALING_CAP}x)`);
    }
    expect(scalingOver).toEqual([]);
  });

  test.each([
    "git status", "git commit -m x", "git push origin feat/x", "git push -u origin feat/main-menu", "rm -rf ./build",
    "rm -rf node_modules dist", `rm -rf ${CWD}/tmp`, "bun test", "kubectl get pods", "kubectl apply -f k.yaml",
    "kubectl apply -n staging -f k.yaml", "git checkout -b feat", "git restore src/a.ts", "git branch -d merged",
    "git clean -n", "chmod 755 x", "npm test", "docker build .", "psql -c 'SELECT 1'", "terraform plan", "helm template x",
    "gh release view", "echo main", "aws s3 ls",
    // WR-05 negatives: plain refspecs, --delete without force, a value-taking global option before a safe subcommand
    "git push origin feat/x:feat/x", "git branch --delete merged", "git --work-tree /x status", "git -c core.x=1 push origin feat",
    // IN-01: the tmp dir subtree is scratch space
    `rm -rf ${TMP}/build-cache`, `rm -rf ${TMP}/swarm-omp-abc/x ${TMP}/y`,
    // T-05-24: absolute-path wrappers around harmless commands, and words that only start like a wrapper
    "/usr/bin/env git status", "/usr/bin/env python3 -m pytest", "/usr/bin/time ls", "/usr/bin/timeout 5 bun test", "/bin/ls -la",
    "/usr/bin/envsubst -V", "/opt/bin/sudoers-lint x", "/usr/bin/sudo -u x git log",
  ])("negative inside swarm: %s → undefined", (command) => {
    expect(guardToolCall(bash(command), inside())).toBeUndefined();
  });

  test.each([`rm -rf ${TMP}`, `rm -rf ${TMP}/`, `rm -rf ${TMP}/../etc`, `rm -rf ${TMP}/.swarm`])("IN-01: %s still blocks", (command) => {
    blockedWith(guardToolCall(bash(command), inside()), "destructive");
  });

  test("IN-01: through the handler the real os.tmpdir() subtree is inside", () => {
    const { run } = guardHandler({ activeTools: ["task"] });
    const a05 = agentCtx(CWD, B05, [], false);
    expect(run(bash(`rm -rf ${tmpdir()}/build-cache`), a05)).toBeUndefined();
    expect((run(bash(`rm -rf ${tmpdir()}`), a05) as { reason?: string }).reason).toEndWith(": rm-rf-protected)");
  });

  test("OSA-01: a substitution past the 256-per-level cut cap is still judged, never left in the command text", () => {
    const { run } = guardHandler({ activeTools: ["task"] });
    const a05 = agentCtx(CWD, B05, [], false);
    const res = run(bash("$(:) ".repeat(257) + "`rm -rf /etc -f`"), a05) as { block?: boolean };
    expect(res?.block).toBe(true);
  });

  /** WR-08: read-only kubectl/helm forms and markers that only occur in file or resource names pass for A11. */
  test.each([
    "kubectl rollout status deploy/api -n production",
    "kubectl rollout history deploy/api -n prod",
    "kubectl apply --dry-run=server -f x.yaml -n production",
    "kubectl apply --dry-run -f x.yaml --context=prod",
    "kubectl apply -f deploy/main-app.yaml",
    "kubectl delete -f main.yaml",
    "kubectl apply -k overlays/production",
    "kubectl apply -f x -l app=main",
    "kubectl rollout restart deploy/main-app -n staging",
    "kubectl get pods -n production",
    "helm upgrade --dry-run app ./chart",
    "helm install --dry-run --debug app ./chart",
  ])("WR-08 negative for a11: %s → undefined", (command) => {
    expect(guardToolCall(bash(command), inside("a11-devops"))).toBeUndefined();
  });

  test.each([
    "kubectl -n prod apply -f x",
    "kubectl scale deploy/api --replicas=0 --namespace=prod",
    "kubectl delete pod x -n staging --context=dev-cluster-main",
    "kubectl delete ns production",
    "kubectl rollout restart deploy/api --kubeconfig ~/.kube/prod",
    // only `--dry-run`, `=client`, `=server` (and legacy `=true`) render; `=none`/`=false` apply for real
    "kubectl apply -n production --dry-run=none -f k.yaml",
    "kubectl apply -n production --dry-run=false -f k.yaml",
  ])("WR-08 positive for a11: %s → kubectl-prod-change", (command) => {
    expect(guardToolCall(bash(command), inside("a11-devops"))?.reason).toEndWith(": kubectl-prod-change)");
  });

  test.each(["helm upgrade app ./chart --dry-run=none", "helm install app ./chart --dry-run=false"])("WR-08: %s is a live helm change", (command) => {
    expect(guardToolCall(bash(command), inside("a11-devops"))?.reason).toEndWith(": helm-release-change)");
  });

  /** HOOK-02 spellings: attached/`=` flag values, clusters, global options before the verb, aliases and verb synonyms. */
  const A11 = "a11-devops";
  const A12 = "a12-release";
  const A07 = "a07-data";
  test.each<[string, string, string]>([
    ...[
      "kubectl -nprod apply -f k.yaml", "kubectl --namespace=prod apply -f k", "kubectl --namespace prod apply -f k", "kubectl -n=prod apply -f k",
      "kubectl --context prod-cluster apply -f k", "kubectl --context=prod-cluster apply -f k", "KUBECONFIG=~/.kube/prod kubectl apply -f k",
      "kubectl --context prod set image deploy/api api=img:2", "oc -n prod delete pod x",
      ...["create -f x", "replace -f x", "patch deploy a -p '{}'", "edit deploy a", "set image deploy/a a=i:1", "label pod a x=y", "annotate pod a x=y",
        "expose deploy a --port 80", "autoscale deploy a --max 3", "drain node1", "cordon node1", "uncordon node1", "taint nodes n k=v:NoSchedule",
        "run x --image=y", "cp f a:/tmp", "exec -it a -- ls", "debug a -it --image=b", "certificate approve csr1", "rollout undo deploy/a",
        "apply edit-last-applied deploy/a"].map((verb) => `kubectl -n prod ${verb}`),
    ].map((c): [string, string, string] => [c, A11, "kubectl-prod-change"]),
    ["helm --kube-context prod upgrade --install a ./c", A11, "helm-release-change"],
    ["helm uninstall app", A11, "helm-release-change"],
    ["terraform -chdir=infra apply -auto-approve", A11, "terraform-apply-destroy"],
    ["terraform apply -destroy", A11, "terraform-apply-destroy"],
    ["terraform state rm aws_instance.x", A11, "terraform-apply-destroy"],
    ["pulumi -C infra update", A11, "pulumi-up-destroy"],
    ["aws --profile prod s3 rm s3://b/x", A11, "cloud-destructive"],
    ["aws s3 sync . s3://b --delete", A11, "cloud-destructive"],
    ["git push -fu origin x", B05, "git-force-push"],
    ["git -C x push -f", B05, "git-force-push"],
    ["git -c k=v push --force", B05, "git-force-push"],
    ["git push origin +main", B05, "git-force-push"],
    ["git push --mirror", B05, "git-force-push"],
    ["git clean -xfd", B05, "git-clean-force"],
    ["git -c clean.requireForce=false clean -d", B05, "git-clean-force"],
    ["git branch -M main", B05, "git-branch-force-delete"],
    ["git checkout -f main", B05, "git-discard-repo"],
    ["rm -Rf /", B05, "rm-rf-protected"],
    ["rm -r -f ~", B05, "rm-rf-protected"],
    ["chmod -R 0777 .", B05, "chmod-777-recursive"],
    ["chmod -R o+w .", B05, "chmod-777-recursive"],
    ['mysql -e "DROP DATABASE x"', A07, "ddl-drop-truncate"],
    ["dropdb app", A07, "ddl-drop-truncate"],
    ["supabase db reset", A07, "prisma-migrate-reset"],
    ["bin/rails db:reset", A07, "prisma-migrate-reset"],
    ["prisma db push --force-reset", A07, "db-push-accept-data-loss"],
    ["git push origin :main", A12, "git-push-protected"],
    ["git push origin --delete main", A12, "git-push-protected"],
    ["gh pr merge 3", A12, "git-push-protected"],
    ["gh -R o/r release create v1", A12, "gh-release-create"],
    ["npm --registry x publish", A12, "package-publish"],
    ["cargo publish", A12, "package-publish"],
    ["twine upload dist/*", A12, "package-publish"],
    ["docker --context x push img", A12, "docker-push"],
    ["docker buildx build --push -t img .", A12, "docker-push"],
    ["fly -a app deploy", A12, "fly-deploy"],
    ["npx vercel@latest deploy --target production", A12, "vercel-prod"],
    ["vc --prod", A12, "vercel-prod"],
  ])("HOOK-02 spelling: %s blocks for %s as %s", (command, agent, id) => {
    expect(guardToolCall(bash(command), inside(agent))?.reason).toEndWith(`: ${id})`);
  });

  test.each<[string, string]>([
    ...[
      "kubectl get pods -n production", "kubectl describe deploy -n prod x", "kubectl logs -n prod x", "kubectl apply -f k.yaml", "kubectl apply -n staging -f k.yaml",
      "kubectl -n prod rollout status deploy/a", "kubectl -n prod apply view-last-applied deploy/a", "kubectl set image deploy/api api=img:v1.2.3",
      "kubectl -n prod port-forward svc/a 8080", "kubectl -n prod diff -f k.yaml", "kubectl config use-context prod", "helm list", "helm template x ./c",
      "helm plugin install https://x", "helm status app", "terraform plan", "terraform plan -destroy", "terraform init", "terraform state list", "pulumi preview",
      "aws s3 ls", "aws s3 cp x s3://b/", "gcloud config list",
    ].map((c): [string, string] => [c, A11]),
    ...["git push origin feature", "git push -u origin feat/x", "git branch -d feature", "git checkout -b x", "git switch -c x", "git stash pop", "git clean -n",
      "chmod -R 755 .", "chmod -R g+w .", "chmod 777 file"].map((c): [string, string] => [c, B05]),
    ...["npm publish --dry-run", "cargo publish --dry-run", "docker build -t x .", "docker pull x", "vercel deploy", "fly status", "gh release view v1",
      "gh pr create -f", "gh api repos/o/r/releases", "gradle publishToMavenLocal"].map((c): [string, string] => [c, A12]),
    ...['psql -c "SELECT 1"', "prisma migrate dev", "prisma db push", "rails db:migrate"].map((c): [string, string] => [c, A07]),
  ])("HOOK-02 benign: %s passes for %s", (command, agent) => {
    expect(guardToolCall(bash(command), inside(agent))).toBeUndefined();
  });

  test("a `--` operand names the target: flux suspend|resume and kubectl change verbs block on a prod name after it, exec|run|debug|attach keep their pod command", () => {
    expect(guardToolCall(bash("flux suspend kustomization -- prod"), inside("a22-gitops"))?.reason).toBe("BLOCKED needs: human-approval (prod_infra: flux-change)");
    expect(guardToolCall(bash("flux resume helmrelease -- prod-api"), inside("a22-gitops"))?.reason).toBe("BLOCKED needs: human-approval (prod_infra: flux-change)");
    expect(guardToolCall(bash("flux suspend kustomization -- dev"), inside("a22-gitops"))).toBeUndefined();
    expect(guardToolCall(bash("kubectl delete -- deployment/prod-api"), inside(A11))?.reason).toBe("BLOCKED needs: human-approval (prod_infra: kubectl-prod-change)");
    expect(guardToolCall(bash("kubectl exec pod -- ls prod"), inside(A11))).toBeUndefined();
  });

  /** Every exemption token counts only in its own syntactic slot: never as a flag value, a later positional, or after `--`. */
  test.each<[string, string, string]>([
    ["kubectl -n production exec pod -- python3 -c 'x' --dry-run=client", A11, "kubectl-prod-change"],
    ["kubectl -n prod apply -l --dry-run -f k.yaml", A11, "kubectl-prod-change"],
    ["kubectl -n prod rollout restart deploy/status", A11, "kubectl-prod-change"],
    ["kubectl -n prod apply -f view-last-applied", A11, "kubectl-prod-change"],
    ["kubectl -n prod --as get delete ns x", A11, "kubectl-prod-change"],
    ["helm upgrade plugin ./chart", A11, "helm-release-change"],
    ["helm upgrade app ./c --set --dry-run", A11, "helm-release-change"],
    ["helm upgrade app ./c -- --dry-run", A11, "helm-release-change"],
    ["npm publish -- --dry-run", A12, "package-publish"],
    ["cargo publish -- --dry-run", A12, "package-publish"],
    ["gradle publishToMavenLocal publish", A12, "package-publish"],
    ["gzip -- -c .swarm/tasks.db", B05, "protected-path-mutate"],
    ["gzip -S -c .swarm/tasks.db", B05, "protected-path-mutate"],
    ["cd .swarm && wget -- -O https://h/tasks.db", B05, "protected-path-shell"],
    ["echo x > /dev/null/../../nonexistent-guard-cwd/.swarm/x", B05, "protected-path-shell"],
    ["PWD=/nonexistent-guard-cwd/.swarm; echo x > $PWD/tasks.db", B05, "protected-path-shell"],
    ["TMPDIR=.swarm; echo x > $TMPDIR/x", B05, "protected-path-shell"],
    ["bun --cwd . scripts/ts/orch_plan.ts", B05, "swarm-state"],
    ["node -r ./x.js scripts/ts/orch_plan.ts", B05, "swarm-state"],
  ])("exemption slot: %s blocks for %s as %s", (command, agent, id) => {
    expect(guardToolCall(bash(command), inside(agent))?.reason).toMatch(new RegExp(`[(: ]${id}\\)$`));
  });

  test.each<[string, string]>([
    ["kubectl -n prod exec pod --dry-run=client -- ls", A11], ["helm upgrade app ./c --dry-run", A11], ["helm plugin install https://x", A11],
    ["npm publish --dry-run", A12], ["gzip -c .swarm/tasks.db > /tmp/x.gz", B05], ["gzip -S .z -c .swarm/tasks.db > /tmp/x", B05],
    ["cd .swarm && echo x > /dev/null", B05], ["echo x > $PWD/out.txt", B05], ["echo x > $TMPDIR/x", B05],
  ])("exemption slot: %s still passes for %s", (command, agent) => {
    expect(guardToolCall(bash(command), inside(agent))).toBeUndefined();
  });

  test("top-level session with SWARM_TASK_ID and no session_init is in the swarm", () => {
    const facts: GuardFacts = { ...main, env: { SWARM_TASK_ID: "T-1" } };
    blockedWith(guardToolCall(bash("git reset --hard"), facts), "destructive");
    const { run } = guardHandler({ activeTools: ["task"] });
    process.env.SWARM_TASK_ID = "T-1";
    expect((run(bash("npm publish"), fakeCtx(CWD)) as { reason?: string }).reason).toBe(
      "BLOCKED needs: human-approval (prod_high_risk: package-publish)",
    );
  });

  test("IN-05: an agent-less session_init is a child, not the top level: the SWARM_TASK_ID/SWARM_AGENT fallback never applies", () => {
    const { run } = guardHandler({ activeTools: ["task"] });
    const agentless = fakeCtx(CWD, [{ type: "session_init", restrictToolNames: false, tools: [] }]);
    process.env.SWARM_TASK_ID = "T-1";
    expect(run(bash("git reset --hard"), agentless)).toBeUndefined();
    process.env.SWARM_AGENT = "a05-backend";
    expect(run(bash("git reset --hard"), agentless)).toBeUndefined();
    expect(callingAgent(agentless)).toBeUndefined();
    // the same env in a session without any session_init is the headless swarm session it names
    expect(callingAgent(fakeCtx(CWD))).toBe("a05-backend");
    expect((run(bash("git reset --hard"), fakeCtx(CWD)) as { reason?: string }).reason).toEndWith(": git-reset-hard)");
  });

  test("a generic task child (not a swarm slug) is outside", () => {
    expect(guardToolCall(bash("git reset --hard"), inside("task"))).toBeUndefined();
  });

  test.each([undefined, null, {}, { command: "" }, { command: "   \n" }, { command: 42 }, { command: ["rm", "-rf", "/"] }, "rm -rf /"])(
    "bash input %p has nothing to classify",
    (input) => {
      expect(guardToolCall(call("bash", input), inside())).toBeUndefined();
      expect(guardToolCall(call("bash", input), main)).toBeUndefined();
    },
  );
});

describe("HOOK-02 v2.3 lanes: cluster tooling and chain rows", () => {
  const insideAs = (agent: string): GuardFacts => ({ agent, restricted: false, planMode: false, hasTask: true, topLevel: false, env: {}, cwd: CWD, home: HOME, tmp: TMP, runtimeRoots: [] });
  const bash = (command: string) => call("bash", { command });
  const A21 = "a21-cluster-ops";
  const A22 = "a22-gitops";
  const A23 = "a23-mesh-network";
  const A25 = "a25-resilience";
  const A26 = "a26-smart-contract";
  const A28 = "a28-chain-infra";

  test("a rule's agents are real swarm slugs (messaging only, so a typo would silently name nobody)", () => {
    for (const rule of RULES) for (const agent of rule.agents ?? []) expect(SWARM_SLUGS).toContain(agent);
  });

  test.each<[string, string, string]>([
    // argocd: a prod marker in an operand or a destination flag, or prune/force/replace
    ["argocd app sync prod-api", A22, "argocd-change"],
    ["argocd app sync api --prune", A22, "argocd-change"],
    ["argocd app sync api --replace", A22, "argocd-change"],
    ["argocd app sync api --force --server staging.example.com", A22, "argocd-change"],
    ["argocd app delete api --force", A22, "argocd-change"],
    ["argocd app rollback api 3 --server argocd.prod.example.com", A22, "argocd-change"],
    ["argocd app set api -p production", A22, "argocd-change"],
    ["argocd app patch api --patch '{}' --dest-namespace production", A22, "argocd-change"],
    ["argocd app terminate-op prod-api", A22, "argocd-change"],
    ["argocd app --server argocd.example.com sync prod-api", A22, "argocd-change"],
    ["argocd cluster add prod-ctx", A21, "argocd-change"],
    ["argocd cluster rm https://prod.example.com", A21, "argocd-change"],
    ["argocd repo add https://git.example.com/org/main-app", A22, "argocd-change"],
    ["argocd proj delete production", A22, "argocd-change"],
    // the dry-run exemption counts only as argocd's own flag
    ["argocd app sync prod-api --dry-run=false", A22, "argocd-change"],
    ["argocd app sync prod-api --helm-set --dry-run", A22, "argocd-change"],
    ["argocd app sync prod-api -- --dry-run", A22, "argocd-change"],
    // argocd's own `--` only ends its options: the operand after it is the application it syncs
    ["argocd app sync -- prod-api", A22, "argocd-change"],
    ["argocd app sync api --prune --dry-run=none", A22, "argocd-change"],
    ["flux delete helmrelease api -n apps", A22, "flux-change"],
    ["flux uninstall", A22, "flux-change"],
    ["flux -n flux-system bootstrap git --url=ssh://g/f", A22, "flux-change"],
    ["flux suspend kustomization apps --context prod-cluster", A22, "flux-change"],
    ["flux resume helmrelease main-app", A22, "flux-change"],
    ["flux resume kustomization apps -n production", A22, "flux-change"],
    ["istioctl install -y", A23, "mesh-lifecycle"],
    ["istioctl -i istio-system upgrade", A23, "mesh-lifecycle"],
    ["istioctl experimental uninstall --purge", A23, "mesh-lifecycle"],
    ["linkerd install --crds", A23, "mesh-lifecycle"],
    ["linkerd viz install", A23, "mesh-lifecycle"],
    ["cilium upgrade --version 1.16.0", A23, "mesh-lifecycle"],
    ["velero restore create --from-backup nightly", A25, "velero-change"],
    ["velero -n backups restore create r1 --from-backup b", A25, "velero-change"],
    ["velero backup delete b1 --confirm", A25, "velero-change"],
    ["velero restore delete r1", A25, "velero-change"],
    ["velero schedule delete s1", A25, "velero-change"],
    ["velero install --provider aws", A25, "velero-change"],
    ["velero delete backup b1", A25, "velero-change"],
    ["eksctl create cluster --name c", A21, "managed-cluster-lifecycle"],
    ["eksctl upgrade cluster -f c.yaml --approve", A21, "managed-cluster-lifecycle"],
    ["eksctl update addon --cluster c --name vpc-cni", A21, "managed-cluster-lifecycle"],
    ["eksctl delete cluster --name c", A21, "managed-cluster-lifecycle"],
    ["gcloud container clusters upgrade c --master --zone z", A21, "managed-cluster-lifecycle"],
    ["gcloud --project p container clusters resize c --num-nodes 5", A21, "managed-cluster-lifecycle"],
    ["gcloud beta container clusters create-auto c --region r", A21, "managed-cluster-lifecycle"],
    ["gcloud container node-pools create pool --cluster c", A21, "managed-cluster-lifecycle"],
    ["az aks upgrade -g rg -n c --kubernetes-version 1.30", A21, "managed-cluster-lifecycle"],
    ["az aks scale -g rg -n c --node-count 5", A21, "managed-cluster-lifecycle"],
    ["az aks update -g rg -n c", A21, "managed-cluster-lifecycle"],
    ["az aks nodepool add -g rg --cluster-name c -n pool", A21, "managed-cluster-lifecycle"],
    ["az aks nodepool upgrade -g rg --cluster-name c -n pool", A21, "managed-cluster-lifecycle"],
    ["kubeadm init", A21, "kubeadm-lifecycle"],
    ["kubeadm join 10.0.0.1:6443 --token t --discovery-token-ca-cert-hash sha256:x", A21, "kubeadm-lifecycle"],
    ["kubeadm reset --force", A21, "kubeadm-lifecycle"],
    ["kubeadm upgrade node", A21, "kubeadm-lifecycle"],
    ["kubeadm upgrade", A21, "kubeadm-lifecycle"],
    // a selector that picks prod apps, and the argocd verbs that delete a resource or change a project/cluster
    ["argocd app sync -l env=prod", A22, "argocd-change"],
    ["argocd app sync --selector=env=prod", A22, "argocd-change"],
    ["argocd app delete-resource prod-api --kind Pod --name p", A22, "argocd-change"],
    ["argocd appset delete prod-set", A22, "argocd-change"],
    ["argocd appset create prod-set.yaml", A22, "argocd-change"],
    // `app actions run` changes the app's workloads: the production checks apply, while `actions list` only reads
    ["argocd app actions run prod-api restart --kind Deployment", A22, "argocd-change"],
    // creating an app is a write: a production destination, name or server, or a prune, blocks as the sync of the same app does
    ["argocd app create prod-api --repo https://git.example.com/r --path p --dest-server https://k.example.com --dest-namespace production --sync-policy automated", A22, "argocd-change"],
    ["argocd app create api --dest-namespace production --sync-policy automated", A22, "argocd-change"],
    ["argocd app create api --dest-server https://argocd.prod.example.com", A22, "argocd-change"],
    ["argocd app create api --dest-namespace staging --prune", A22, "argocd-change"],
    ["argocd proj add-destination prod https://k.example.com ns", A22, "argocd-change"],
    ["argocd proj remove-source production https://git.example.com/r", A22, "argocd-change"],
    ["argocd cluster set prod-ctx --namespace x", A21, "argocd-change"],
    ["argocd cluster rotate-auth prod-ctx", A21, "argocd-change"],
    // `--help=false` is no help request
    ["kubeadm reset --help=false", A21, "kubeadm-lifecycle"],
    ["kubeadm reset --cri-socket --help", A21, "kubeadm-lifecycle"],
    ["kubeadm init --config --help", A21, "kubeadm-lifecycle"],
    ["argocd app sync prod-api --revision --help", A22, "argocd-change"],
    // a directory, container or --cwd value named like the tool may come before the tool itself
    ["docker run --name argocd img argocd app sync prod-api", A22, "argocd-change"],
    ["docker run -w /opt/kubeadm img kubeadm reset -f", A21, "kubeadm-lifecycle"],
    ["docker run -w /opt/velero img velero uninstall", A25, "velero-change"],
    // velero's global options (`-v 5`, `--log_dir d`) and a launcher that puts words before the verb (`--entrypoint velero img …`)
    ["velero -v 5 restore create --from-backup b", A25, "velero-change"],
    ["velero --log_dir /tmp backup delete b", A25, "velero-change"],
    ["docker run --rm --entrypoint velero img restore create --from-backup b", A25, "velero-change"],
    ["eksctl utils update-kube-proxy --cluster c", A21, "managed-cluster-lifecycle"],
    ["eksctl utils update-coredns --cluster c --approve", A21, "managed-cluster-lifecycle"],
  ])("cluster spelling: %s blocks for %s as %s", (command, agent, id) => {
    expect(guardToolCall(bash(command), insideAs(agent))?.reason).toBe(`BLOCKED needs: human-approval (prod_infra: ${id})`);
  });

  test.each<[string, string]>([
    // argocd read verbs, dry-run, a non-prod target without prune/force/replace
    ...[
      "argocd app get prod-api", "argocd app list", "argocd app diff prod-api", "argocd app history prod-api", "argocd app logs prod-api",
      "argocd app wait prod-api --health", "argocd app manifests prod-api", "argocd app sync api", "argocd app sync staging-api",
      "argocd app sync prod-api --dry-run", "argocd app sync prod-api --dry-run=client", "argocd app sync prod-api --prune --dry-run",
      "argocd app sync api --prune=false", "argocd app sync api --server argocd.staging.example.com", "argocd cluster list", "argocd cluster get prod-ctx",
      "argocd repo list", "argocd proj get production", "argocd cluster add staging-ctx", "argocd version", "argocd login argocd.prod.example.com",
      "argocd app actions list prod-api", "argocd app actions run staging-api restart --kind Deployment",
    ].map((c): [string, string] => [c, A22]),
    ...[
      "flux get kustomizations -n production", "flux check", "flux get helmreleases --context prod", "flux suspend kustomization apps -n flux-system",
      "flux resume kustomization apps", "flux reconcile kustomization prod-apps", "flux logs -n production", "flux version", "flux export source git x",
    ].map((c): [string, string] => [c, A22]),
    ...[
      "istioctl analyze -n production", "istioctl manifest generate --set profile=demo", "istioctl x precheck", "istioctl version", "istioctl proxy-status",
      "istioctl analyze -f install.yaml", "linkerd check", "linkerd viz stat deploy -n production", "cilium status", "cilium connectivity test", "cilium hubble port-forward",
    ].map((c): [string, string] => [c, A23]),
    ...[
      "velero backup create nightly", "velero backup get", "velero backup describe nightly", "velero restore get", "velero restore describe r1",
      "velero schedule get", "velero schedule create nightly --schedule '0 1 * * *'", "velero version", "velero backup logs nightly",
    ].map((c): [string, string] => [c, A25]),
    ...[
      "eksctl get cluster", "eksctl get nodegroup --cluster c", "eksctl version", "eksctl utils describe-stacks --cluster c",
      "gcloud container clusters list", "gcloud container clusters describe c --zone z", "gcloud container clusters get-credentials c --zone z",
      "gcloud container node-pools list --cluster c", "gcloud config list",
      "az aks show -g rg -n c", "az aks list", "az aks get-credentials -g rg -n c", "az aks nodepool list -g rg --cluster-name c",
      "az aks nodepool show -g rg --cluster-name c -n pool", "az aks get-upgrades -g rg -n c", "az account show",
      "kubeadm upgrade plan", "kubeadm upgrade diff v1.30.0", "kubeadm token list", "kubeadm version", "kubeadm certs check-expiration", "kubeadm config print init-defaults",
    ].map((c): [string, string] => [c, A21]),
    // `--help`/`-h` print usage whatever verb they follow; a text printer or searcher never runs its operands
    ...[
      "argocd app sync prod-api --help", "argocd cluster add prod-ctx -h", "echo argocd app sync prod-api", "grep -rn argocd app sync prod docs",
      "flux delete --help", "flux uninstall -h", "flux bootstrap github --help", "echo flux uninstall",
      "istioctl install --help", "linkerd upgrade -h", "cilium uninstall --help", "printf '%s' istioctl install",
      "velero restore create --help", "velero uninstall -h", "echo velero uninstall",
      "eksctl create cluster --help", "eksctl delete cluster -h", "gcloud container clusters create --help", "az aks create --help", "az aks upgrade -h", "echo eksctl create cluster",
      "kubeadm init --help", "kubeadm reset -h", "kubeadm upgrade apply --help", "echo kubeadm reset",
      "argocd appset list", "argocd appset get prod-set", "argocd proj role create prod r", "argocd cluster get prod-ctx",
      "argocd app create api --dest-namespace staging --sync-policy automated", "argocd app create api --dest-namespace staging --dry-run", "argocd app create --help",
    ].map((c): [string, string] => [c, A21]),
  ])("cluster read-only or exempt form: %s passes for %s", (command, agent) => {
    expect(guardToolCall(bash(command), insideAs(agent))).toBeUndefined();
  });

  test.each([
    ["gcloud container clusters delete c --zone z", "cloud-destructive"],
    ["az aks delete -g rg -n c", "cloud-destructive"],
    ["az aks nodepool delete -g rg --cluster-name c -n pool", "cloud-destructive"],
  ])("a managed-cluster delete is the existing cloud-destructive row, not a second one: %s", (command, id) => {
    expect(guardToolCall(bash(command), insideAs(A21))?.reason).toBe(`BLOCKED needs: human-approval (prod_infra: ${id})`);
  });

  test.each<[string, string]>([
    ["cast send 0xabc 'transfer(address,uint256)' 0xdef 1", "chain-broadcast"],
    ["cast s 0xabc 'f()'", "chain-broadcast"],
    ["cast publish 0x02f8", "chain-broadcast"],
    ["cast rpc eth_sendRawTransaction 0x02f8", "chain-broadcast"],
    ["cast rpc --rpc-url sepolia eth_sendTransaction '{}'", "chain-broadcast"],
    ["forge script script/Deploy.s.sol --rpc-url sepolia --broadcast", "chain-broadcast"],
    ["forge script script/Deploy.s.sol:Deploy --broadcast --verify", "chain-broadcast"],
    ["forge script script/Deploy.s.sol --resume", "chain-broadcast"],
    ["forge create src/C.sol:C --rpc-url sepolia", "chain-broadcast"],
    ["npx hardhat run scripts/deploy.ts --network sepolia", "chain-broadcast"],
    ["npx hardhat --network sepolia run scripts/deploy.ts", "chain-broadcast"],
    ["yarn hardhat deploy --network=arbitrum", "chain-broadcast"],
    ["hardhat ignition deploy ignition/modules/C.ts --network mainnet", "chain-broadcast"],
    ["HARDHAT_NETWORK=mainnet npx hardhat run scripts/deploy.ts", "chain-broadcast"],
    // an env prefix on a shell -c wrapper is the network its child runs with
    ['HARDHAT_NETWORK=mainnet bash -c "hardhat run scripts/deploy.ts"', "chain-broadcast"],
    ["truffle migrate --network mainnet", "chain-broadcast"],
    ["truffle deploy --network goerli", "chain-broadcast"],
    ["brownie run scripts/deploy.py --network mainnet", "chain-broadcast"],
    ["brownie run scripts/deploy.py --network mainnet-fork", "chain-broadcast"],
    ["solana transfer Recipient 1 --allow-unfunded-recipient", "chain-broadcast"],
    ["solana -u mainnet-beta transfer Recipient 1", "chain-broadcast"],
    ["solana program deploy target/deploy/p.so", "chain-broadcast"],
    ["solana program upgrade target/deploy/p.so ProgramId", "chain-broadcast"],
    ["anchor deploy", "chain-broadcast"],
    ["anchor deploy --provider.cluster devnet", "chain-broadcast"],
    ["anchor upgrade target/deploy/p.so --program-id P --provider.cluster mainnet", "chain-broadcast"],
    ["anchor migrate --provider.cluster https://api.mainnet-beta.solana.com", "chain-broadcast"],
    ["spl-token transfer Mint 1 Recipient", "chain-broadcast"],
    ["spl-token --url devnet mint Mint 10", "chain-broadcast"],
    ["spl-token burn Account 1", "chain-broadcast"],
    ["spl-token approve Account 1 Delegate", "chain-broadcast"],
    ["near send a.testnet b.testnet 1", "chain-broadcast"],
    ["near call c.testnet m '{}' --accountId a.testnet", "chain-broadcast"],
    ["near deploy --accountId a.testnet --wasmFile c.wasm", "chain-broadcast"],
    ["starkli invoke 0xabc transfer 0x1 1", "chain-broadcast"],
    ["starkli deploy 0xclass", "chain-broadcast"],
    ["starkli declare c.json", "chain-broadcast"],
    ["sui client call --package 0x1 --module m --function f", "chain-broadcast"],
    ["sui client publish --gas-budget 100000000", "chain-broadcast"],
    ["sui client transfer --to 0x1 --object-id 0x2", "chain-broadcast"],
    ["aptos move publish --named-addresses a=default", "chain-broadcast"],
    ["aptos move run --function-id 0x1::m::f", "chain-broadcast"],
    // key material blocks whatever the network
    ["cast call 0xabc 'f()' --private-key 0x01", "chain-key-material"],
    ["forge test --private-key=0x01", "chain-key-material"],
    ["hardhat run s.ts --network localhost --private-key 0x01", "chain-key-material"],
    ["forge script S.s.sol --mnemonic 'a b c'", "chain-key-material"],
    ["forge script S.s.sol --mnemonic-path m.txt", "chain-key-material"],
    ["somecli --secret-key k", "chain-key-material"],
    ["bash -c 'forge test --private-key 0x01'", "chain-key-material"],
    ["cast wallet new", "chain-key-material"],
    ["cast wallet new-mnemonic --words 12", "chain-key-material"],
    ["cast wallet vanity --starts-with 0xdead", "chain-key-material"],
    ["cast wallet import deployer --interactive", "chain-key-material"],
    ["cast wallet private-key 'a b c'", "chain-key-material"],
    ["cast wallet decrypt-keystore deployer", "chain-key-material"],
    ["cast wallet sign 'hello'", "chain-key-material"],
    ["solana-keygen new --outfile id.json", "chain-key-material"],
    ["solana-keygen recover prompt:", "chain-key-material"],
    ["geth account new", "chain-key-material"],
    ["geth --datadir d account import k.txt", "chain-key-material"],
    // cast's short aliases and its other subcommands that submit: `p` publish, `rp` rpc, `bs` batch-send, erc20/erc4626/safe writes
    ["cast p 0x02f8", "chain-broadcast"],
    ["cast rp eth_sendRawTransaction 0x02f8", "chain-broadcast"],
    ["cast rpc personal_sendTransaction '{}' pw", "chain-broadcast"],
    ["cast rpc wallet_sendCalls '{}'", "chain-broadcast"],
    ["cast rpc eth_sendBundle '{}'", "chain-broadcast"],
    ["cast bs --rpc-url sepolia", "chain-broadcast"],
    ["cast batch-send --rpc-url sepolia", "chain-broadcast"],
    ["cast erc20 transfer 0xtoken 0xdef 1", "chain-broadcast"],
    ["cast erc20 approve 0xtoken 0xdef 1", "chain-broadcast"],
    ["cast erc20 a 0xtoken 0xdef 1", "chain-broadcast"],
    ["cast erc20 m 0xtoken 0xdef 1", "chain-broadcast"],
    ["cast erc20-token burn 0xtoken 1", "chain-broadcast"],
    ["cast vault deposit 0xvault 1", "chain-broadcast"],
    ["cast erc4626 redeem 0xvault 1", "chain-broadcast"],
    ["cast safe execute 0xsafe 0xhash", "chain-broadcast"],
    ["cast safe propose 0xsafe 0xto 0x", "chain-broadcast"],
    ["cast tip20 create name SYM USD 0xadmin", "chain-broadcast"],
    // global options before the verb, and a launcher that puts the image before it
    ["cast --color never send 0xabc 'f()'", "chain-broadcast"],
    ["cast -j 4 publish 0x02f8", "chain-broadcast"],
    ["forge --color never create src/C.sol:C", "chain-broadcast"],
    ["forge c src/C.sol:C --rpc-url sepolia", "chain-broadcast"],
    ["docker run --rm --entrypoint cast ghcr.io/foundry-rs/foundry send 0xabc 'f()'", "chain-broadcast"],
    ["docker run --entrypoint forge img script S.s.sol --broadcast", "chain-broadcast"],
    // hardhat's global `--max-memory N` before the verb; a second `--network` cannot launder the first; console/exec/test reach the chain too
    ["hardhat --max-memory 4096 run scripts/deploy.ts --network sepolia", "chain-broadcast"],
    ["hardhat run scripts/deploy.ts --network localhost --network mainnet", "chain-broadcast"],
    ["hardhat console --network mainnet", "chain-broadcast"],
    ["hardhat test --network sepolia", "chain-broadcast"],
    ["truffle exec scripts/s.js --network mainnet", "chain-broadcast"],
    ["truffle console --network mainnet", "chain-broadcast"],
    ["truffle test --network mainnet", "chain-broadcast"],
    ["brownie console --network mainnet", "chain-broadcast"],
    ["brownie test --network mainnet", "chain-broadcast"],
    // solana buffers, upgrades and stake moves; anchor idl and a remote test; spl-token account and authority changes
    ["solana program write-buffer target/deploy/p.so", "chain-broadcast"],
    ["solana program close ProgramId", "chain-broadcast"],
    ["solana program set-upgrade-authority ProgramId --new-upgrade-authority X", "chain-broadcast"],
    ["solana --ws wss://api.mainnet-beta.solana.com transfer Recipient 1", "chain-broadcast"],
    ["solana delegate-stake StakeAccount VoteAccount", "chain-broadcast"],
    ["solana withdraw-stake StakeAccount Recipient 1", "chain-broadcast"],
    ["anchor idl init -f target/idl/p.json ProgramId", "chain-broadcast"],
    ["anchor idl upgrade -f target/idl/p.json ProgramId --provider.cluster mainnet", "chain-broadcast"],
    ["anchor test --provider.cluster devnet --skip-local-validator", "chain-broadcast"],
    ["spl-token create-token", "chain-broadcast"],
    ["spl-token create-account Mint", "chain-broadcast"],
    ["spl-token close-account Account", "chain-broadcast"],
    ["spl-token authorize Mint mint --disable", "chain-broadcast"],
    ["spl-token wrap 1", "chain-broadcast"],
    // near-cli(-rs), sui and aptos: other ways to move funds or deploy
    ["near send-near a.testnet b.testnet 1", "chain-broadcast"],
    ["near create-account x.testnet --masterAccount a.testnet", "chain-broadcast"],
    ["near --networkId mainnet call c.near m '{}' --accountId a.near", "chain-broadcast"],
    ["near tokens a.testnet send-near b.testnet '1 NEAR' network-config testnet sign-with-keychain send", "chain-broadcast"],
    ["near contract call-function as-transaction c.testnet m json-args '{}' prepaid-gas '30 Tgas' attached-deposit '0 NEAR' sign-as a.testnet network-config testnet sign-with-keychain send", "chain-broadcast"],
    ["near contract deploy c.testnet use-file c.wasm without-init-call network-config testnet sign-with-keychain send", "chain-broadcast"],
    ["sui client split-coin --coin-id 0x1 --amounts 1", "chain-broadcast"],
    ["sui client ptb --move-call 0x1::m::f", "chain-broadcast"],
    ["sui client execute-signed-tx --tx-bytes b --signatures s", "chain-broadcast"],
    ["aptos move deploy-object --address-name a", "chain-broadcast"],
    ["aptos account transfer --account 0x1 --amount 1", "chain-broadcast"],
    ["aptos stake add-stake --amount 1", "chain-broadcast"],
    // key material: the multi-key and raw flags, and the wallet verbs and aliases that print or take a key
    ["forge script S.s.sol --private-keys 0x01,0x02", "chain-key-material"],
    ["forge script S.s.sol --mnemonics m.txt", "chain-key-material"],
    ["forge script S.s.sol --mnemonic-paths m.txt", "chain-key-material"],
    ["forge script S.s.sol --mnemonic-passphrases pw", "chain-key-material"],
    ["cast call 0xabc --mnemonic-passphrase=pw", "chain-key-material"],
    ["cast wallet public-key --raw-private-key 0x01", "chain-key-material"],
    ["cast wallet address 0x01", "chain-key-material"],
    ["cast wallet a 0x01", "chain-key-material"],
    ["cast wallet derive 'a b c'", "chain-key-material"],
    ["cast w d 'a b c'", "chain-key-material"],
    ["cast wallet sa --nonce 1", "chain-key-material"],
    ["cast wallet derive-private-key 'a b c' 1", "chain-key-material"],
    ["cast --color never wallet new", "chain-key-material"],
    ["docker run --rm img cast wallet new", "chain-key-material"],
    // HARDHAT_NETWORK exported or assigned earlier in the same command line is the network of the run after it
    ["export HARDHAT_NETWORK=mainnet && npx hardhat run scripts/deploy.ts", "chain-broadcast"],
    ["HARDHAT_NETWORK=mainnet; npx hardhat deploy", "chain-broadcast"],
    ["docker run --name cast --network host img cast send 0xabc 'f()'", "chain-broadcast"],
    ["docker run -w /src/forge img forge script S.s.sol --broadcast", "chain-broadcast"],
    ["docker run -w /opt/solana img solana transfer Recipient 1", "chain-broadcast"],
    ["docker run -w /opt/cast img cast wallet new", "chain-key-material"],
    // a launcher's own `--` right after the tool: the tool's arguments follow it
    ["npm exec hardhat -- run scripts/deploy.ts --network sepolia", "chain-broadcast"],
    ["npm --prefix hardhat run hardhat -- run scripts/deploy.ts --network sepolia", "chain-broadcast"],
    ["pnpm exec forge -- script S.s.sol --broadcast", "chain-broadcast"],
    ["npm exec -- cast wallet new", "chain-key-material"],
  ])("chain spelling: %s blocks as %s for a26", (command, id) => {
    const capability = id === "chain-broadcast" ? "chain_broadcast" : "chain_key_material";
    expect(guardToolCall(bash(command), insideAs(A26))?.reason).toBe(`BLOCKED needs: human-approval (${capability}: ${id})`);
    expect(guardToolCall(bash(command), { ...insideAs(A26), agent: undefined, topLevel: true })).toBeUndefined();
  });

  test.each([
    "cast call 0xabc 'owner()(address)'", "cast balance 0xabc", "cast block latest", "cast 4byte 0xa9059cbb", "cast keccak 'x'", "cast to-wei 1 ether",
    "cast rpc eth_blockNumber", "cast rpc --rpc-url sepolia eth_getBalance 0xabc latest", "cast wallet list", "cast wallet verify --address 0xabc m s",
    "forge test", "forge build", "forge fmt", "forge test -vvv --fork-url sepolia", "forge script script/Deploy.s.sol", "forge script script/Deploy.s.sol --rpc-url sepolia",
    "forge script script/Deploy.s.sol --broadcast=false", "forge inspect C abi", "anvil", "anvil --fork-url sepolia",
    "hardhat test", "npx hardhat compile", "hardhat run scripts/deploy.ts", "hardhat run scripts/deploy.ts --network localhost",
    "npx hardhat run scripts/deploy.ts --network hardhat", "npx hardhat --network anvil run scripts/deploy.ts", "hardhat deploy --network local",
    "hardhat ignition deploy ignition/modules/C.ts --network localhost", "HARDHAT_NETWORK=localhost npx hardhat run s.ts", "hardhat ignition visualize ignition/modules/C.ts",
    "truffle migrate --network development", "truffle compile", "truffle test", "brownie run scripts/deploy.py --network development", "brownie compile", "brownie test",
    "solana balance", "solana -u devnet balance", "solana program show ProgramId", "solana address", "solana-test-validator",
    "anchor build", "anchor test", "anchor deploy --provider.cluster localnet", "anchor deploy --provider.cluster http://127.0.0.1:8899", "anchor migrate --provider.cluster l",
    "spl-token balance Mint", "spl-token accounts", "near view c.testnet m '{}'", "near state a.testnet", "starkli call 0xabc balance_of 0x1", "starkli balance 0xabc",
    "sui client balance", "sui client objects", "sui move build", "aptos move compile", "aptos move test", "aptos account list",
    "solana-keygen pubkey id.json", "solana-keygen verify PubKey id.json", "geth account list", "geth version", "echo private-key", "cat mnemonic.md",
    // --help prints usage; a text printer or searcher never runs or feeds its operands
    "cast send --help", "cast s -h", "cast publish --help", "forge create --help", "forge c -h", "forge script --broadcast --help", "hardhat run --help", "hardhat deploy -h",
    "truffle migrate --help", "anchor deploy --help", "solana transfer --help", "solana program deploy -h", "spl-token transfer --help", "near call --help", "starkli invoke --help",
    "sui client call --help", "aptos move publish --help", "echo cast send 0xabc", "echo forge create", "printf '%s\\n' forge script --broadcast", "grep -rn cast send src",
    "rg forge create contracts", "man cast", "which forge", "cast wallet new --help", "solana-keygen new -h", "geth account new --help",
    "grep -rn -- '--private-key' docs", "rg -e '--private-key' .", "git grep -- --mnemonic", "grep -r private-key .",
    // cast, hardhat, anchor and the rest that only read, or stay local, or sign offline
    "cast erc20 balance 0xtoken 0xabc", "cast erc20 allowance 0xtoken 0xa 0xb", "cast vault info 0xvault", "cast safe simulate 0xsafe 0xhash", "cast safe list-delegates 0xsafe",
    "cast --color never call 0xabc 'f()'", "cast mktx 0xabc 'f()'", "cast wallet address --account deployer", "cast wallet verify --address 0xabc m s",
    "hardhat --max-memory 4096 test", "hardhat console", "hardhat test --network localhost", "truffle exec scripts/s.js", "truffle test", "brownie console", "brownie test",
    "anchor test", "anchor test --provider.cluster localnet", "anchor idl fetch ProgramId", "spl-token display Mint", "spl-token supply Mint",
    "near contract call-function as-read-only c.testnet m json-args '{}' network-config testnet now", "near tokens a.testnet view-near-balance network-config testnet now",
    "sui client dry-run-transaction --tx-bytes b", "aptos move view --function-id 0x1::m::f", "aptos stake show-stake-pool --owner-address 0x1",
    "export HARDHAT_NETWORK=localhost && npx hardhat run scripts/deploy.ts", "export HARDHAT_NETWORK=mainnet && npx hardhat run s.ts --network localhost",
    "cast send --rpc-url http://localhost:8545 0xabc 'f()'", "cast send --rpc-url http://127.0.0.1:8545 0xabc 'f()'", "hardhat run --network localhost scripts/deploy.ts",
  ])("chain read-only or local form: %s passes for a26", (command) => {
    expect(guardToolCall(bash(command), insideAs(A26))).toBeUndefined();
  });

  test("the chain rows guard every swarm session: `agents` is messaging, the new lanes and the old ones alike", () => {
    for (const slug of SWARM_SLUGS) {
      expect(guardToolCall(bash("cast send 0xabc 'f()'"), insideAs(slug))?.reason).toBe("BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)");
      expect(guardToolCall(bash("cast call 0xabc 'f()' --private-key 0x01"), insideAs(slug))?.reason).toBe("BLOCKED needs: human-approval (chain_key_material: chain-key-material)");
    }
  });

  test("a chain command inside a chained line is found in whichever segment it sits", () => {
    expect(guardToolCall(bash("forge build && forge script S.s.sol --broadcast"), insideAs(A28))?.reason).toEndWith(": chain-broadcast)");
    expect(guardToolCall(bash("cd contracts && sudo -E cast send 0xabc 'f()'"), insideAs(A28))?.reason).toEndWith(": chain-broadcast)");
    expect(guardToolCall(bash("forge build && forge test"), insideAs(A28))).toBeUndefined();
  });

  test("a bare hardhat run on an inherited non-local HARDHAT_NETWORK needs approval, the bash tool's env included", () => {
    const CHAIN = "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)";
    const inherited = { ...insideAs(A26), env: { HARDHAT_NETWORK: "mainnet" } };
    expect(guardToolCall(bash("hardhat run scripts/deploy.ts"), inherited)?.reason).toBe(CHAIN);
    expect(guardToolCall(call("bash", { command: "npx hardhat run scripts/deploy.ts", env: { HARDHAT_NETWORK: "sepolia" } }), insideAs(A26))?.reason).toBe(CHAIN);
    // the run's own prefix is its network; a bare run after a local one still inherits mainnet
    expect(guardToolCall(bash("HARDHAT_NETWORK=localhost npx hardhat run scripts/deploy.ts"), inherited)).toBeUndefined();
    expect(guardToolCall(bash("HARDHAT_NETWORK=localhost npx hardhat test; hardhat run scripts/deploy.ts"), inherited)?.reason).toBe(CHAIN);
    // a substitution expands before the command's own prefix assignment: the inherited mainnet still reaches the run inside it
    expect(guardToolCall(bash('HARDHAT_NETWORK=localhost echo "$(hardhat run scripts/deploy.ts)"'), inherited)?.reason).toBe(CHAIN);
    // the cut sits in a single-quoted -c body: the child shell expands it under its own localhost, but the outer command still reaches it, so it is checked under the inherited mainnet (fail closed)
    expect(guardToolCall(bash("HARDHAT_NETWORK=localhost bash -c 'echo \"$(hardhat run scripts/deploy.ts)\"'"), inherited)?.reason).toBe(CHAIN);
    // a `#` inside a parameter expansion is not a comment: the substitution after it still runs under the inherited mainnet
    expect(guardToolCall(bash("bash -c 'echo ${SWARM_REVIEW_UNSET:- # $(hardhat run scripts/deploy.ts)}'"), inherited)?.reason).toBe(CHAIN);
  });

  test("a prefix on a command with no nested command does not persist: an inherited local network stays local for the next run", () => {
    const inherited = { ...insideAs(A26), env: { HARDHAT_NETWORK: "localhost" } };
    expect(guardToolCall(bash("HARDHAT_NETWORK=mainnet true; hardhat run scripts/deploy.ts"), inherited)).toBeUndefined();
  });

  test("a bare hardhat run with a local or no environment network behaves as before", () => {
    expect(guardToolCall(bash("hardhat run scripts/deploy.ts"), insideAs(A26))).toBeUndefined();
    expect(guardToolCall(bash("hardhat run scripts/deploy.ts"), { ...insideAs(A26), env: { HARDHAT_NETWORK: "localhost" } })).toBeUndefined();
    expect(guardToolCall(bash("hardhat run scripts/deploy.ts"), { ...insideAs(A26), env: { HARDHAT_NETWORK: "" } })).toBeUndefined();
    expect(guardToolCall(call("bash", { command: "hardhat run scripts/deploy.ts", env: { HARDHAT_NETWORK: "hardhat" } }), { ...insideAs(A26), env: { HARDHAT_NETWORK: "mainnet" } })).toBeUndefined();
  });

  test("the last HARDHAT_NETWORK prefix assignment is the network the run sees; a HARDHAT_NETWORK argument is not an assignment", () => {
    const inherited = { ...insideAs(A26), env: { HARDHAT_NETWORK: "mainnet" } };
    expect(guardToolCall(bash("HARDHAT_NETWORK=localhost HARDHAT_NETWORK=mainnet npx hardhat run scripts/deploy.ts"), insideAs(A26))?.reason).toBe(
      "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)",
    );
    expect(guardToolCall(bash("HARDHAT_NETWORK=mainnet HARDHAT_NETWORK=localhost npx hardhat run scripts/deploy.ts"), insideAs(A26))).toBeUndefined();
    expect(guardToolCall(bash("hardhat run scripts/deploy.ts HARDHAT_NETWORK=localhost"), inherited)?.reason).toBe(
      "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)",
    );
  });

  test("an inherited mainnet is overridden by a local export earlier on the line, so the bare run passes", () => {
    const inherited = { ...insideAs(A26), env: { HARDHAT_NETWORK: "mainnet" } };
    expect(guardToolCall(bash("export HARDHAT_NETWORK=localhost && hardhat run scripts/deploy.ts"), inherited)).toBeUndefined();
  });

  test("an exec -a operand that assigns blocks: the wrapper's argv[0] cannot be modelled, so every framework run on the line needs approval", () => {
    const inherited = { ...insideAs(A26), env: { HARDHAT_NETWORK: "mainnet" } };
    expect(guardToolCall(bash("HARDHAT_NETWORK=mainnet exec -a HARDHAT_NETWORK=localhost hardhat run scripts/deploy.ts"), insideAs(A26))?.reason).toBe(
      "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)",
    );
    expect(guardToolCall(bash("exec -a HARDHAT_NETWORK=localhost hardhat run scripts/deploy.ts"), inherited)?.reason).toBe(
      "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)",
    );
    expect(guardToolCall(bash("exec -a HARDHAT_NETWORK=localhost hardhat test"), insideAs(A26))?.reason).toBe(
      "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)",
    );
  });

  test("a redirection that may fail keeps the earlier network: unset behind a failing redirect leaves inherited mainnet; /dev/null and a stdout dup do not", () => {
    const inherited = { ...insideAs(A26), env: { HARDHAT_NETWORK: "mainnet" } };
    expect(guardToolCall(bash("unset HARDHAT_NETWORK >/dev/null/not-a-directory; hardhat run scripts/deploy.ts"), inherited)?.reason).toBe(
      "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)",
    );
    expect(guardToolCall(bash("unset HARDHAT_NETWORK >/dev/null; hardhat run scripts/deploy.ts"), inherited)).toBeUndefined();
    expect(guardToolCall(bash("export HARDHAT_NETWORK=localhost 2>&1; hardhat run scripts/deploy.ts"), inherited)).toBeUndefined();
  });

  test("a locale-quoted $\"...\" is a double-quoted string: a printf -v written through it needs approval, a locale-quoted print does not", () => {
    expect(guardToolCall(bash('export HARDHAT_NETWORK=localhost; opt=-v; printf $"$opt" HARDHAT_NETWORK mainnet; hardhat run scripts/deploy.ts'), insideAs(A26))?.reason).toBe(
      "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)",
    );
    expect(guardToolCall(bash('printf $"cost\\n"; hardhat test'), insideAs(A26))).toBeUndefined();
  });

  test("an assigning expansion in a printf argument is a write: export HARDHAT_NETWORK=; printf ${HARDHAT_NETWORK:=mainnet} needs approval, a plain or default expansion does not", () => {
    expect(guardToolCall(bash("export HARDHAT_NETWORK=; printf '%s' \"${HARDHAT_NETWORK:=mainnet}\"; hardhat run scripts/deploy.ts"), insideAs(A26))?.reason).toBe(
      "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)",
    );
    expect(guardToolCall(bash("export HARDHAT_NETWORK=localhost; printf '%s' \"$HARDHAT_NETWORK\"; hardhat run scripts/deploy.ts"), insideAs(A26))).toBeUndefined();
    expect(guardToolCall(bash("printf '%s' \"${HARDHAT_NETWORK:-mainnet}\"; hardhat test"), insideAs(A26))).toBeUndefined();
  });

  test("the last export on the line decides: a mainnet export after a local one still needs approval", () => {
    expect(guardToolCall(bash("export HARDHAT_NETWORK=localhost; export HARDHAT_NETWORK=mainnet; hardhat run scripts/deploy.ts"), insideAs(A26))?.reason).toBe(
      "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)",
    );
  });

  test("unset clears an inherited mainnet to the unnamed chain, so the bare run passes", () => {
    const inherited = { ...insideAs(A26), env: { HARDHAT_NETWORK: "mainnet" } };
    expect(guardToolCall(bash("unset HARDHAT_NETWORK && hardhat run scripts/deploy.ts"), inherited)).toBeUndefined();
  });

  test("unset -n is a write the guard cannot read: the inherited mainnet stays possible, so the bare run needs approval", () => {
    const inherited = { ...insideAs(A26), env: { HARDHAT_NETWORK: "mainnet" } };
    expect(guardToolCall(bash("unset -n HARDHAT_NETWORK; hardhat run scripts/deploy.ts"), inherited)?.reason).toBe(
      "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)",
    );
  });

  test("env HARDHAT_NETWORK=x never persists past its command: the inherited mainnet still needs approval", () => {
    const inherited = { ...insideAs(A26), env: { HARDHAT_NETWORK: "mainnet" } };
    expect(guardToolCall(bash("env HARDHAT_NETWORK=localhost true; hardhat run scripts/deploy.ts"), inherited)?.reason).toBe(
      "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)",
    );
  });

  test("a write inside a loop may run on any iteration: the mainnet export within the body needs approval after it", () => {
    const inherited = { ...insideAs(A26), env: { HARDHAT_NETWORK: "mainnet" } };
    expect(
      guardToolCall(
        bash("for i in 1 2; do export HARDHAT_NETWORK=localhost; export HARDHAT_NETWORK=mainnet; done; hardhat run scripts/deploy.ts"),
        inherited,
      )?.reason,
    ).toBe("BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)");
  });

  test("a loop body may run zero times: a local export inside it leaves the inherited mainnet possible", () => {
    const inherited = { ...insideAs(A26), env: { HARDHAT_NETWORK: "mainnet" } };
    expect(guardToolCall(bash("for i in 1; do export HARDHAT_NETWORK=localhost; done; hardhat run scripts/deploy.ts"), inherited)?.reason).toBe(
      "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)",
    );
  });

  test("source and . are unreadable writes: a local export before them keeps a non-local value possible, and a bare run with no writes still passes", () => {
    const CHAIN = "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)";
    expect(guardToolCall(bash("export HARDHAT_NETWORK=localhost; source ./env.sh; hardhat run scripts/deploy.ts"), insideAs(A26))?.reason).toBe(CHAIN);
    expect(guardToolCall(bash("export HARDHAT_NETWORK=localhost; . ./env.sh; hardhat run scripts/deploy.ts"), insideAs(A26))?.reason).toBe(CHAIN);
    expect(guardToolCall(bash("hardhat run scripts/deploy.ts"), insideAs(A26))).toBeUndefined();
  });

  test("a reader that names or expands HARDHAT_NETWORK is not a write: local tests with no environment network still pass", () => {
    expect(guardToolCall(bash("printenv HARDHAT_NETWORK; hardhat test"), insideAs(A26))).toBeUndefined();
    expect(guardToolCall(bash("printf '%s' \"$PWD\"; hardhat test"), insideAs(A26))).toBeUndefined();
  });

  test("a reader that names HARDHAT_NETWORK does not hide an inherited mainnet: the bare run still needs approval", () => {
    const inherited = { ...insideAs(A26), env: { HARDHAT_NETWORK: "mainnet" } };
    expect(guardToolCall(bash("printenv HARDHAT_NETWORK; hardhat run scripts/deploy.ts"), inherited)?.reason).toBe(
      "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)",
    );
  });

  test("a printf -v target built at run time is unknown: it may be HARDHAT_NETWORK, so the run needs approval", () => {
    expect(guardToolCall(bash('printf -v"$target" mainnet; hardhat run scripts/deploy.ts'), insideAs(A26))?.reason).toBe(
      "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)",
    );
  });

  test("an attached printf -vNAME names HARDHAT_NETWORK literally: the run needs approval", () => {
    expect(guardToolCall(bash("printf -vHARDHAT_NETWORK mainnet; hardhat run scripts/deploy.ts"), insideAs(A26))?.reason).toBe(
      "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)",
    );
  });

  test("a read into a dynamic name is unknown: with inherited mainnet the run still needs approval", () => {
    const inherited = { ...insideAs(A26), env: { HARDHAT_NETWORK: "mainnet" } };
    expect(guardToolCall(bash('read -r "$v" <<< x; hardhat run scripts/deploy.ts'), inherited)?.reason).toBe(
      "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)",
    );
  });

  test("readers that name or expand HARDHAT_NETWORK stay readers: printenv and a printf format do not hide a local test run", () => {
    expect(guardToolCall(bash("printenv HARDHAT_NETWORK; printf '%s' \"$PWD\"; hardhat test"), insideAs(A26))).toBeUndefined();
  });

  test("getopts after -- is a writer token: it may write HARDHAT_NETWORK, so the run needs approval", () => {
    const inherited = { ...insideAs(A26), env: { HARDHAT_NETWORK: "mainnet" } };
    expect(guardToolCall(bash("export HARDHAT_NETWORK=localhost; getopts -- m HARDHAT_NETWORK -m; hardhat run scripts/deploy.ts"), inherited)?.reason).toBe(
      "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)",
    );
  });

  test("a plain export of a local network still passes: the inherited mainnet is overridden on the line", () => {
    const inherited = { ...insideAs(A26), env: { HARDHAT_NETWORK: "mainnet" } };
    expect(guardToolCall(bash("export HARDHAT_NETWORK=localhost; hardhat run scripts/deploy.ts"), inherited)).toBeUndefined();
  });

  test("a plain static export of another name changes nothing: a bare test run with no environment network passes", () => {
    expect(guardToolCall(bash("export OTHER=1; hardhat test"), insideAs(A26))).toBeUndefined();
  });

  test("a printf whose first argument is an expansion may be -v: it is a writer token, so the run needs approval", () => {
    expect(guardToolCall(bash('opt=-v; printf "$opt" HARDHAT_NETWORK mainnet; hardhat run scripts/deploy.ts'), insideAs(A26))?.reason).toBe(
      "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)",
    );
  });

  test("the same expanded -v write, after a local export, with an inherited mainnet also needs approval", () => {
    const inherited = { ...insideAs(A26), env: { HARDHAT_NETWORK: "mainnet" } };
    expect(guardToolCall(bash('export HARDHAT_NETWORK=localhost; opt=-v; printf "$opt" HARDHAT_NETWORK mainnet; hardhat run scripts/deploy.ts'), inherited)?.reason).toBe(
      "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)",
    );
  });

  test("a printf with a static format and an expanded value is a reader: a bare test run with no environment network passes", () => {
    expect(guardToolCall(bash('printf "%s\\n" "$x"; hardhat test'), insideAs(A26))).toBeUndefined();
  });

  test("a for loop over a name other than HARDHAT_NETWORK writes no network: a bare test run in its body passes, and for HARDHAT_NETWORK needs approval", () => {
    expect(guardToolCall(bash("for i in 1 2; do hardhat test; done"), insideAs(A26))).toBeUndefined();
    expect(guardToolCall(bash("for HARDHAT_NETWORK in mainnet; do :; done; hardhat run scripts/deploy.ts"), insideAs(A26))?.reason).toBe(
      "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)",
    );
  });

  test("unset and export -p that name no network leave a local run unblocked; an unset of HARDHAT_NETWORK still clears an inherited mainnet", () => {
    const inherited = { ...insideAs(A26), env: { HARDHAT_NETWORK: "mainnet" } };
    expect(guardToolCall(bash("unset OTHER; hardhat test"), insideAs(A26))).toBeUndefined();
    expect(guardToolCall(bash("export HARDHAT_NETWORK=localhost; unset OTHER; hardhat run scripts/deploy.ts"), inherited)).toBeUndefined();
    expect(guardToolCall(bash("export HARDHAT_NETWORK=localhost; export -p >/dev/null; hardhat run scripts/deploy.ts"), inherited)).toBeUndefined();
    expect(guardToolCall(bash("unset -v HARDHAT_NETWORK; hardhat run scripts/deploy.ts"), inherited)).toBeUndefined();
  });

  test("a single-quoted dollar is literal, not an expansion: a bare test run with no environment network passes", () => {
    expect(guardToolCall(bash("printf 'cost: $5\\n'; hardhat test"), insideAs(A26))).toBeUndefined();
  });

  test("quoted or literal text is not an expansion: a printf format that starts with text, printed templates and literal (( do not request approval", () => {
    expect(guardToolCall(bash('printf "cost: $5\\n"; hardhat test'), insideAs(A26))).toBeUndefined();
    expect(guardToolCall(bash('printf "$%s\\n" 5; hardhat test'), insideAs(A26))).toBeUndefined();
    expect(guardToolCall(bash("printf '%s' '${CONFIG:=value}'; hardhat test"), insideAs(A26))).toBeUndefined();
    expect(guardToolCall(bash("hardhat test --grep '((x))'"), insideAs(A26))).toBeUndefined();
    expect(guardToolCall(bash('hardhat test --grep "(("'), insideAs(A26))).toBeUndefined();
  });

  test("a -v after the printf format is a value, not an option: a bare test run with no environment network passes", () => {
    expect(guardToolCall(bash("printf '%s' -v; hardhat test"), insideAs(A26))).toBeUndefined();
  });

  test("an expanded printf -v with no inherited network needs approval for a bare test run too", () => {
    expect(guardToolCall(bash('opt=-v; printf "$opt" HARDHAT_NETWORK mainnet; hardhat test'), insideAs(A26))?.reason).toBe(
      "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)",
    );
  });

  test("round-12 regression: a nameref and a loop variable cannot alias HARDHAT_NETWORK past a later plain export, no inherited network", () => {
    const line = "declare -n target=HARDHAT_NETWORK; export HARDHAT_NETWORK=localhost; for target in HARDHAT_NETWORK; do target=mainnet; hardhat run scripts/deploy.ts; done";
    expect(guardToolCall(bash(line), insideAs(A26))?.reason).toBe("BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)");
  });

  test("round-12 regression: the same line with an inherited mainnet also needs approval", () => {
    const line = "declare -n target=HARDHAT_NETWORK; export HARDHAT_NETWORK=localhost; for target in HARDHAT_NETWORK; do target=mainnet; hardhat run scripts/deploy.ts; done";
    const inherited = { ...insideAs(A26), env: { HARDHAT_NETWORK: "mainnet" } };
    expect(guardToolCall(bash(line), inherited)?.reason).toBe("BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)");
  });

  test("a plain assignment after a compound keyword is still a write: an if body that sets mainnet needs approval", () => {
    expect(guardToolCall(bash("if true; then HARDHAT_NETWORK=mainnet; fi; hardhat run scripts/deploy.ts"), insideAs(A26))?.reason).toBe(
      "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)",
    );
  });

  test("a quoted assignment is a command, not a write: it does not override an inherited mainnet", () => {
    const inherited = { ...insideAs(A26), env: { HARDHAT_NETWORK: "mainnet" } };
    expect(guardToolCall(bash("'HARDHAT_NETWORK=localhost'; hardhat run scripts/deploy.ts"), inherited)?.reason).toBe(
      "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)",
    );
    expect(guardToolCall(bash("\\HARDHAT_NETWORK=localhost; hardhat run scripts/deploy.ts"), inherited)?.reason).toBe(
      "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)",
    );
    // with no inherited network the quoted word writes nothing and nobody writes unreadably, so the bare run passes
    expect(guardToolCall(bash("'HARDHAT_NETWORK=localhost'; hardhat run scripts/deploy.ts"), insideAs(A26))).toBeUndefined();
  });

  test("an if-prefixed quoted assignment stays blocked: stripping the keyword must not invent a plain write", () => {
    const inherited = { ...insideAs(A26), env: { HARDHAT_NETWORK: "mainnet" } };
    expect(guardToolCall(bash("if 'HARDHAT_NETWORK=localhost'; then :; fi; hardhat run scripts/deploy.ts"), inherited)?.reason).toBe(
      "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)",
    );
  });

  test("an assignment expansion in an argument is a writer: ${HARDHAT_NETWORK:=mainnet} needs approval with no environment network", () => {
    expect(guardToolCall(bash(": ${HARDHAT_NETWORK:=mainnet}; hardhat run scripts/deploy.ts"), insideAs(A26))?.reason).toBe(
      "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)",
    );
  });

  test("an arithmetic assignment in an argument is a writer: $((HARDHAT_NETWORK=1)) needs approval with no environment network", () => {
    expect(guardToolCall(bash("echo $((HARDHAT_NETWORK=1)); hardhat run scripts/deploy.ts"), insideAs(A26))?.reason).toBe(
      "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)",
    );
  });

  test("a quoted x=$PWD is a reader: a bare test run with no environment network passes", () => {
    expect(guardToolCall(bash("printf '%s' \"x=$PWD\"; hardhat test"), insideAs(A26))).toBeUndefined();
  });

  test("P2: a local export that succeeds before && decides the run after it; the same export before ; leaves the inherited mainnet possible", () => {
    const inherited = { ...insideAs(A26), env: { HARDHAT_NETWORK: "mainnet" } };
    expect(guardToolCall(bash("export HARDHAT_NETWORK=localhost >local.log && hardhat test"), inherited)).toBeUndefined();
    expect(guardToolCall(bash("export HARDHAT_NETWORK=localhost >local.log; hardhat test"), inherited)?.reason).toBe(
      "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)",
    );
  });

  test("a run that a failed redirection skips never happens: unset behind a failing && redirect leaves the run out of the decision", () => {
    const inherited = { ...insideAs(A26), env: { HARDHAT_NETWORK: "mainnet" } };
    expect(guardToolCall(bash("unset HARDHAT_NETWORK >/dev/null/x && hardhat run scripts/deploy.ts"), inherited)).toBeUndefined();
  });

  test("env -S splits its line like a command: an assignment in the split line blocks, a plain split line is judged as its command", () => {
    const CHAIN = "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)";
    expect(guardToolCall(bash("env -S 'HARDHAT_NETWORK=localhost hardhat test'"), insideAs(A26))?.reason).toBe(CHAIN);
    expect(guardToolCall(bash("env -S 'hardhat run --network mainnet scripts/x.ts'"), insideAs(A26))?.reason).toBe(CHAIN);
    expect(guardToolCall(bash("env -S 'hardhat test'"), insideAs(A26))).toBeUndefined();
  });

  test("eval, source, an expansion assigning the network, and an append each make every framework run on the line need approval", () => {
    const CHAIN = "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)";
    for (const command of [
      'eval "x=1"; hardhat test',
      "source ./env.sh; hardhat test",
      "HARDHAT_NETWORK=$X hardhat test",
      'export HARDHAT_NETWORK="$N"; hardhat test',
      "HARDHAT_NETWORK+=x hardhat run scripts/deploy.ts",
    ]) {
      expect({ command, reason: guardToolCall(bash(command), insideAs(A26))?.reason }).toEqual({ command, reason: CHAIN });
    }
  });

  test("a loop body that writes mainnet after an if still reaches the run on a later iteration; the same loop after a local export and outside the loop passes", () => {
    const CHAIN = "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)";
    expect(guardToolCall(bash("for i in 1 2; do if [ $i = 2 ]; then hardhat run scripts/deploy.ts; fi; export HARDHAT_NETWORK=mainnet; done"), insideAs(A26))?.reason).toBe(CHAIN);
    expect(
      guardToolCall(bash("export HARDHAT_NETWORK=localhost; for i in 1 2; do if [ $i = 2 ]; then hardhat run scripts/deploy.ts; fi; done; export HARDHAT_NETWORK=mainnet"), insideAs(A26)),
    ).toBeUndefined();
  });

  test("the --network flag decides a hardhat run: a local one passes, a mainnet one blocks", () => {
    const CHAIN = "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)";
    expect(guardToolCall(bash("hardhat run --network localhost scripts/deploy.ts"), insideAs(A26))).toBeUndefined();
    expect(guardToolCall(bash("hardhat run --network mainnet scripts/x.ts"), insideAs(A26))?.reason).toBe(CHAIN);
  });

  test("cast and forge judge the RPC endpoint: a loopback or local endpoint passes; no endpoint, a variable, a repeated flag or a non-local one blocks", () => {
    const CHAIN = "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)";
    expect(guardToolCall(bash("cast send --rpc-url http://127.0.0.1:8545 0xabc 'f()'"), insideAs(A26))).toBeUndefined();
    expect(guardToolCall(bash("forge create src/C.sol:C --rpc-url http://localhost:8545"), insideAs(A26))).toBeUndefined();
    expect(guardToolCall(bash("forge script S.s.sol --broadcast --rpc-url http://127.0.0.1:8545"), insideAs(A26))).toBeUndefined();
    for (const command of [
      "cast send 0xabc 'f()'",
      "cast send --rpc-url $RPC 0xabc 'f()'",
      "cast send --rpc-url http://localhost:8545 --rpc-url https://mainnet.example 0xabc 'f()'",
      "cast send -r sepolia 0xabc 'f()'",
      "cast --rpc-url sepolia send 0xabc 'f()'",
    ]) {
      expect({ command, reason: guardToolCall(bash(command), insideAs(A26))?.reason }).toEqual({ command, reason: CHAIN });
    }
  });

  test("exec with a plain local operand is not an assignment: a bare test run through it passes", () => {
    expect(guardToolCall(bash("exec localhost hardhat test"), insideAs(A26))).toBeUndefined();
  });

  test("an unnamed truffle migrate needs approval: its network is in the config the guard does not read", () => {
    expect(guardToolCall(bash("truffle migrate"), insideAs(A26))?.reason).toBe("BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)");
    expect(guardToolCall(bash("truffle deploy"), insideAs(A26))?.reason).toBe("BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)");
    expect(guardToolCall(bash("truffle exec scripts/deploy.js"), insideAs(A26))?.reason).toBe("BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)");
  });

  test("an unnamed brownie run needs approval: its network is in the config the guard does not read", () => {
    expect(guardToolCall(bash("brownie run scripts/deploy.py"), insideAs(A26))?.reason).toBe("BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)");
  });

  test("unnamed truffle test, brownie test and a truffle exec of a non-deploy script stay local", () => {
    expect(guardToolCall(bash("truffle test"), insideAs(A26))).toBeUndefined();
    expect(guardToolCall(bash("brownie test"), insideAs(A26))).toBeUndefined();
    expect(guardToolCall(bash("truffle exec scripts/s.js"), insideAs(A26))).toBeUndefined();
  });

  test("named development runs of truffle migrate and brownie run pass; the config is not read for a named local network", () => {
    expect(guardToolCall(bash("truffle migrate --network development"), insideAs(A26))).toBeUndefined();
    expect(guardToolCall(bash("brownie run scripts/deploy.py --network development"), insideAs(A26))).toBeUndefined();
  });

  test("a negated export's successor sees both paths: ! export before &&, ; or || leaves inherited mainnet possible, so the run blocks", () => {
    const CHAIN = "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)";
    const inherited = { ...insideAs(A26), env: { HARDHAT_NETWORK: "mainnet" } };
    expect(guardToolCall(bash("! export HARDHAT_NETWORK=localhost >/dev/null/not-a-directory && hardhat run scripts/deploy.ts"), inherited)?.reason).toBe(CHAIN);
    expect(guardToolCall(bash("! export HARDHAT_NETWORK=localhost >/dev/null && hardhat run scripts/deploy.ts"), inherited)?.reason).toBe(CHAIN);
    expect(guardToolCall(bash("! export HARDHAT_NETWORK=localhost; hardhat run scripts/deploy.ts"), inherited)?.reason).toBe(CHAIN);
    expect(guardToolCall(bash("! export HARDHAT_NETWORK=localhost || hardhat run scripts/deploy.ts"), inherited)?.reason).toBe(CHAIN);
  });

  test("a negated export of a local network still passes a local test run when no network is inherited", () => {
    expect(guardToolCall(bash("! export HARDHAT_NETWORK=localhost >/dev/null && hardhat test"), insideAs(A26))).toBeUndefined();
  });

  test("! hardhat test with inherited mainnet blocks on the inherited network, as before the negation fix", () => {
    const CHAIN = "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)";
    expect(guardToolCall(bash("! hardhat test"), { ...insideAs(A26), env: { HARDHAT_NETWORK: "mainnet" } })?.reason).toBe(CHAIN);
  });

  test("a plain export that writes through /dev/null still decides the && run: local passes even with inherited mainnet", () => {
    const inherited = { ...insideAs(A26), env: { HARDHAT_NETWORK: "mainnet" } };
    expect(guardToolCall(bash("export HARDHAT_NETWORK=localhost >/dev/null && hardhat test"), inherited)).toBeUndefined();
  });

  test("a bare RPC name is not proof of a local endpoint: foundry resolves it through foundry.toml, so local, localhost and anvil block", () => {
    const CHAIN = "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)";
    expect(guardToolCall(bash("forge script script/Deploy.s.sol --broadcast --rpc-url local --account deployer"), insideAs(A26))?.reason).toBe(CHAIN);
    expect(guardToolCall(bash("forge script script/Deploy.s.sol --broadcast --rpc-url localhost --account deployer"), insideAs(A26))?.reason).toBe(CHAIN);
    expect(guardToolCall(bash("cast send --rpc-url anvil 0xabc 'f()'"), insideAs(A26))?.reason).toBe(CHAIN);
    expect(guardToolCall(bash("cast send -r local 0xabc 'f()'"), insideAs(A26))?.reason).toBe(CHAIN);
  });

  test("a literal loopback URL is local: http://127.0.0.1:8545 and http://localhost:8545 pass, with or without a path", () => {
    expect(guardToolCall(bash("forge script script/Deploy.s.sol --broadcast --rpc-url http://127.0.0.1:8545 --account deployer"), insideAs(A26))).toBeUndefined();
    expect(guardToolCall(bash("forge script script/Deploy.s.sol --broadcast --rpc-url http://localhost:8545 --account deployer"), insideAs(A26))).toBeUndefined();
    expect(guardToolCall(bash("cast send --rpc-url http://localhost:8545/rpc 0xabc 'f()'"), insideAs(A26))).toBeUndefined();
  });

  test("a remote RPC URL blocks, and a host that only starts with a loopback name is not loopback", () => {
    const CHAIN = "BLOCKED needs: human-approval (chain_broadcast: chain-broadcast)";
    expect(guardToolCall(bash("forge script script/Deploy.s.sol --broadcast --rpc-url https://mainnet.example.org --account deployer"), insideAs(A26))?.reason).toBe(CHAIN);
    expect(guardToolCall(bash("cast send --rpc-url http://localhost.evil.example 0xabc 'f()'"), insideAs(A26))?.reason).toBe(CHAIN);
    expect(guardToolCall(bash("cast send --rpc-url http://127.0.0.1@mainnet.example 0xabc 'f()'"), insideAs(A26))?.reason).toBe(CHAIN);
  });

  test("read-only cast and forge forms with a bare RPC name stay passing: no submit, nothing reaches a network", () => {
    expect(guardToolCall(bash("cast call --rpc-url local 0xabc 'f()'"), insideAs(A26))).toBeUndefined();
    expect(guardToolCall(bash("forge test --fork-url local"), insideAs(A26))).toBeUndefined();
    expect(guardToolCall(bash("cast rpc --rpc-url local eth_blockNumber"), insideAs(A26))).toBeUndefined();
  });
});

describe("HOOK-03 shell twin and D-08", () => {
  const B05 = "a05-backend";
  const facts = (agent: string | undefined, extra: Partial<GuardFacts> = {}): GuardFacts => ({
    agent, restricted: false, planMode: false, hasTask: true, topLevel: agent === undefined, env: {}, cwd: CWD, home: HOME, tmp: TMP, runtimeRoots: [], ...extra,
  });
  const bash = (command: string) => call("bash", { command });
  const SWARM_STATE = "BLOCKED needs: human-approval (swarm-state)";
  const ORCH = [
    "python3 scripts/orch_status.py --ingest r.json",
    "python3 scripts/orch_status.py --transition T-1 DONE",
    "python3 scripts/orch_plan.py --brief-text x",
    "bun scripts/ts/orch_plan.ts",
    "scripts/ts/orch_status.ts --ingest x",
    "cd /repo && SWARM_DIR=/tmp python3 ./scripts/orch_status.py --ingest=r.json",
    // T-05-24: an absolute-path env wrapper
    "/usr/bin/env python3 scripts/orch_status.py --ingest r.json",
    // WR-02: the executed word's basename, `-m` module runs and versioned interpreters count too
    "cd scripts && python3 orch_status.py --ingest r.json",
    "python3 -m scripts.orch_plan --brief-text x",
    "python3 -m scripts.orch_status --ingest=r.json",
    "python3.12 scripts/orch_status.py --transition T-1 DONE",
    "cd scripts/ts && bun orch_plan.ts",
    "python3 other/orch_plan.py",
  ];

  test.each(ORCH)("HOOK-03 shell: %s blocks for a05, passes for A01 with task and for main", (command) => {
    expect(guardToolCall(bash(command), facts(B05))).toEqual({ block: true, reason: SWARM_STATE });
    expect(guardToolCall(bash(command), facts(A01))).toBeUndefined();
    expect(guardToolCall(bash(command), facts(undefined))).toBeUndefined();
  });

  test("HOOK-03 shell: read-only orch_status from a05 passes", () => {
    expect(guardToolCall(bash("python3 scripts/orch_status.py --history"), facts(B05))).toBeUndefined();
  });

  /** Phase 5 D-1: every swarm agent, the gate's owner included, records gates only through swarm_gate, never the shell. */
  test.each([
    ["python3 scripts/review_gate.py --task T-1", B05, true],
    ["python3 scripts/review_gate.py --task T-1", "a09-reviewer", true],
    ["python3 scripts/review_gate.py --task T-1", "a08-qa", true],
    ["python3 scripts/rev_gate.py --task T-1", "a09-reviewer", true],
    ["cd /tmp/ws && python3 scripts/rev_gate.py --task T-1 --json", "a09-reviewer", true],
    ["python3 scripts/qa_gate.py", "a08-qa", true],
    ["bun scripts/ts/qa_gate.ts", "a09-reviewer", true],
    ["python3 scripts/sec_gate.py", "a10-security", true],
    ["python3 scripts/rel_plan.py", "a12-release", true],
    ["python3 scripts/rel_plan.py", B05, true],
    ["python3 scripts/unknown_gate.py", "a09-reviewer", false],
    ["bun run scripts/ts/sec_gate.ts", "a10-security", true],
    ["uv run python scripts/rel_plan.py", B05, true],
    ["/usr/bin/python3 -X dev /repo/scripts/qa_gate.py", "a08-qa", true],
    ["./scripts/rev_gate.py --task T-1", "a09-reviewer", true],
    // T-05-24: an absolute-path env wrapper
    ["/usr/bin/env python3 scripts/rev_gate.py", B05, true],
    ["/usr/bin/env python3 scripts/rev_gate.py --task T-1", "a09-reviewer", true],
    ["/bin/env -i /usr/bin/python3 scripts/qa_gate.py", "a08-qa", true],
    // WR-02: the probe spellings of the owner's own run
    ["cd scripts && python3 rev_gate.py --task-id T-rev --json", "a09-reviewer", true],
    ["cd /opt/agent-swarm/scripts && python3 ./rev_gate.py --task-id T-rev --json", "a09-reviewer", true],
    ["python3 -m scripts.rev_gate --task-id T-rev --json", "a09-reviewer", true],
    ["python3.12 scripts/rev_gate.py --task-id T-rev --json", "a09-reviewer", true],
    ["PYTHONPATH=. python -m scripts.rev_gate --task-id T-rev", "a09-reviewer", true],
    ["bun scripts/ts/rev_gate.ts", "a09-reviewer", true],
    ["python3 -mscripts.sec_gate", "a10-security", true],
    ["python3 -B -m scripts.rel_plan", "a12-release", true],
    ["/usr/bin/python3.11 -X dev scripts/qa_gate.py", "a08-qa", true],
    ["python3 -m scripts.unknown_gate", "a09-reviewer", false],
    ["python3.12 -m pytest tests/test_rev_gate.py", "a09-reviewer", false],
    // T-05-22: the program is the redirected script, or the script was copied aside in the same command
    ["python3 < scripts/rev_gate.py", "a09-reviewer", true],
    ["python3 - --task-id T < scripts/rev_gate.py", "a09-reviewer", true],
    ["cp scripts/rev_gate.py /tmp/r.py && python3 /tmp/r.py", "a05-backend", true],
    ["pypy3 scripts/rev_gate.py", "a09-reviewer", true],
    ["uv run --with pyyaml python scripts/rev_gate.py", "a09-reviewer", true],
    ["uv run --python 3.12 scripts/rev_gate.py", "a09-reviewer", true],
    ["python3 \"/srv/my swarm/scripts/sec_gate.py\"", "a10-security", true],
    ["echo x | python3 -c 'print(1)'", "a09-reviewer", false],
  ])("gate script: %s from %s blocks=%p", (command, agent, blocks) => {
    const res = guardToolCall(bash(command), facts(agent));
    if (blocks) expect(res).toEqual({ block: true, reason: "BLOCKED needs: human-approval (gate: gate-script-shell)" });
    else expect(res).toBeUndefined();
    expect(guardToolCall(bash(command), facts(undefined))).toBeUndefined();
  });

  const GATE = "BLOCKED needs: human-approval (gate: gate-script-shell)";
  const INTERP = "BLOCKED needs: human-approval (destructive: interpreter-stdin)";
  test.each([
    ["python3 -<scripts/rev_gate.py --task-id T", GATE],
    ["bash -c 'python3 -' < scripts/rev_gate.py", GATE],
    ["python3 -W 'ignore:<x' - --task-id T --root /workspace < scripts/rev_gate.py", GATE],
    ["python3 -W '<x' - < scripts/rev_gate.py", GATE],
    ["cp scripts/re[v]_gate.py /tmp/r.py", GATE],
    ["cp 'scripts/rev_gate.py' /tmp/r.py", GATE],
    ["printf 'print(1)\\n' | bash -c 'python3 -'", INTERP],
    ["echo x | python3 -Wonce", INTERP],
    ["echo x | python3 - '<scripts/rev_gate.py'", INTERP],
    ["echo x | python3 -Xtracemalloc", INTERP],
    ["python3 < /dev/null", INTERP],
  ])("stdin and copy hardening: %s blocks as %s", (command, reason) => {
    expect(guardToolCall(bash(command), facts(B05))).toEqual({ block: true, reason });
    expect(guardToolCall(bash(command), facts(undefined))).toBeUndefined();
  });

  test.each([
    "echo x | python3 -c 'print(1)'",
    "echo x | python3 -Bc 'print(1)'",
    "python3 --version < /dev/null",
    "python3 --help < /dev/null",
    "python3 -V < /dev/null",
    "python3 -h < /dev/null",
    "bash -c 'python3 -' '<scripts/rev_gate.py'",
    "python3 - '<scripts/rev_gate.py'",
    "cp 'scripts/re[v]_gate.py' /tmp/r.py",
    "cp notes.txt /tmp/n.txt",
    "cp .swarm/tasks.db /tmp/tasks.db",
    "printf 'print(1)\\n' | bash -c \"$VAR\"",
    "bash -c \"$VAR\" < scripts/rev_gate.py",
  ])("stdin and copy hardening: %s passes", (command) => {
    expect(guardToolCall(bash(command), facts(B05))).toBeUndefined();
  });

  /** WR-02: each gate stem, run by its owner or another agent through each spelling, is a run. */
  const GATE_OWNER: Record<string, string> = {
    qa_gate: "a08-qa", quality_gate: "a08-qa", rev_gate: "a09-reviewer", review_gate: "a09-reviewer",
    sec_gate: "a10-security", security_gate: "a10-security", rel_plan: "a12-release", release_gate: "a12-release",
  };
  test.each(
    Object.entries(GATE_OWNER).flatMap(([stem, owner]) =>
      [`cd scripts && python3 ${stem}.py --json`, `python3 -m scripts.${stem} --json`, `python3.12 scripts/${stem}.py`, `bun scripts/ts/${stem}.ts`].flatMap(
        (command) => [[command, owner], [command, B05]],
      ),
    ),
  )("WR-02 gate script: %s from %s blocks", (command, agent) => {
    expect(guardToolCall(bash(command), facts(agent))).toEqual({ block: true, reason: "BLOCKED needs: human-approval (gate: gate-script-shell)" });
    expect(guardToolCall(bash(command), facts(undefined))).toBeUndefined();
  });

  /** WR-07: reading, testing or linting the scripts is not running them; only an execution counts. */
  test.each([
    "cat scripts/orch_plan.py",
    "grep -n foo scripts/ts/orch_status.ts",
    "python3 -m pytest tests/test_release_gate.py",
    "python3 -m pytest tests/test_hook.py scripts/rev_gate.py",
    "cat scripts/qa_gate.py",
    "ruff check scripts/rev_gate.py",
    "git diff scripts/qa_gate.py",
    "bun test tests/ts/orch_plan.test.ts",
    "python3 scripts/orch_status.py --history",
    "bun scripts/ts/orch_status.ts --history",
    // WR-02: basename and module matching still leave non-executions alone
    "python3 -m scripts.orch_status --history",
    "cd scripts && python3 orch_status.py --history",
    "cd scripts && cat rev_gate.py",
    "cd scripts && grep -n verdict rev_gate.py qa_gate.py",
    "python3 -m pytest scripts/rev_gate.py",
    "pytest tests/test_rev_gate.py",
    "python3.12 -m pytest tests/test_qa_gate.py",
    "ruff check rev_gate.py",
    "bun test tests/ts/rel_plan.test.ts",
  ])("WR-07: %s passes for a05, a08 and a09", (command) => {
    for (const agent of [B05, "a08-qa", "a09-reviewer"]) expect(guardToolCall(bash(command), facts(agent))).toBeUndefined();
  });

  test.each(["bun run scripts/ts/orch_status.ts --transition T-1 DONE", "uv run python scripts/orch_plan.py", "/usr/bin/python3 -X dev /repo/scripts/orch_plan.py"])(
    "WR-07: %s is a run and blocks for a05",
    (command) => {
      expect(guardToolCall(bash(command), facts(B05))).toEqual({ block: true, reason: SWARM_STATE });
    },
  );

  test.each([{}, { code: "1+1" }, null])("D-08: eval %p blocks inside, passes in main", (input) => {
    expect(guardToolCall(call("eval", input), facts(B05))).toEqual({ block: true, reason: "BLOCKED needs: human-approval (eval: eval-in-swarm)" });
    expect(guardToolCall(call("eval", input), facts(undefined))).toBeUndefined();
  });

  test.each([
    "xd://run_code",
    "xd://run_code?code=1",
    "XD://Run_Code",
    "xd://debug",
    "xd://debug?x=1",
    "xd://run_code/session",
  ])("D-08: write %s blocks inside, passes in main", (path) => {
    const blocked = { block: true, reason: "BLOCKED needs: human-approval (eval: xd-device)" };
    expect(guardToolCall(call("write", { path, content: "1+1" }), facts(B05))).toEqual(blocked);
    expect(guardToolCall(call("write", { file_path: path }), facts(B05))).toEqual(blocked);
    expect(guardToolCall(call("write", { path }), facts(undefined))).toBeUndefined();
  });

  test("D-08: write xd://lsp passes inside", () => {
    expect(guardToolCall(call("write", { path: "xd://lsp", content: "{}" }), facts(B05))).toBeUndefined();
  });

  test("S4 scoped substrate: the registered guard admits own-token calls but not another agent or operator MCP", () => {
    process.env.SWARM_AGENT = B05;
    process.env.SWARM_SUBSTRATE_AGENT = B05;
    process.env.SUBSTRATE_TOKEN = "tok-a05";
    const { run } = guardHandler({ activeTools: ["task"] });
    const own = fakeCtx(CWD);
    expect(run(call("mcp__substrate_memory_brief", {}), own)).toBeUndefined();
    expect(run(call("write", { path: "xd://mcp__substrate_events_emit", content: "{}" }), own)).toBeUndefined();
    expect(run(call("mcp__substrate_graph_get", {}), agentCtx(CWD, A01, [], false))?.block).toBe(true);
    expect(run(call("mcp__linear_save_issue", {}), own)?.block).toBe(true);
    expect(run(call("write", { path: "xd://mcp__notion_update_page", content: "{}" }), own)?.block).toBe(true);
  });

  test("S4 scoped substrate: a token alone, missing token, or mismatched scope never unlocks MCP", () => {
    for (const env of [
      { SUBSTRATE_TOKEN: "tok-a05" },
      { SWARM_SUBSTRATE_AGENT: B05 },
      { SWARM_SUBSTRATE_AGENT: A01, SUBSTRATE_TOKEN: "tok-a01" },
      { SWARM_SUBSTRATE_AGENT: B05, SUBSTRATE_TOKEN: "tok-a05", SUBSTRATE_DISABLED: "1" },
    ]) {
      const inside = facts(B05, { env });
      expect(guardToolCall(call("mcp__substrate_memory_brief"), inside)?.block).toBe(true);
      expect(guardToolCall(call("write", { path: "xd://mcp__substrate_events_emit" }), inside)?.block).toBe(true);
    }
  });

  test("S4 scoped substrate: namespace lookalikes and mixed device targets remain blocked", () => {
    const inside = facts(B05, { env: { SWARM_SUBSTRATE_AGENT: B05, SUBSTRATE_TOKEN: "tok-a05" } });
    for (const path of [
      "xd://mcp__substrate_admin_delete",
      "xd://mcp__substrate_graph_get/other",
      "xd://mcp__substrate_graph_get?server=linear",
      "xd://mcp__substrate_other_graph_get",
    ]) expect(guardToolCall(call("write", { path }), inside)?.block).toBe(true);
    expect(guardToolCall(call("write", {
      path: "xd://mcp__substrate_graph_get", file_path: "xd://mcp__linear_save_issue",
    }), inside)?.block).toBe(true);
  });

  test("S4 scoped substrate: A01's depth cap still precedes the own-token exception", () => {
    const inside = { ...capped, env: { SWARM_SUBSTRATE_AGENT: A01, SUBSTRATE_TOKEN: "tok-a01" } };
    expect(guardToolCall(call("mcp__substrate_graph_get"), inside)?.reason).toStartWith(DEPTH_PREFIX);
  });

  /** T-06-07: operator-credential MCP tools and their devices remain closed to every swarm session. */
  const MCP_TOOLS = ["mcp__linear_save_issue", "mcp__notion_update_page", "mcp__linear__delete_comment", "MCP__Greptile_Review"];
  const MCP_DEVICES = ["xd://mcp__linear_save_issue", "XD://MCP__Notion_Create_Pages", "xd://mcp__linear_save_issue?x=1", "xd://mcp__relume_get_component/sub", "  xd://mcp__aio_status  "];
  const headless = (): GuardFacts => facts(undefined, { env: { SWARM_AGENT: B05 } });
  const inSwarm: [string, () => GuardFacts][] = [["a05", () => facts(B05)], ["a01", () => facts(A01)], ["headless SWARM_AGENT", headless]];

  test.each(inSwarm)("T-06-07: %s blocks operator-credential MCP tools and device writes", (_, inside) => {
    for (const name of MCP_TOOLS) {
      expect(guardToolCall(call(name, { id: "x" }), inside())).toEqual({ block: true, reason: `BLOCKED needs: human-approval (mcp: ${name})` });
    }
    for (const path of MCP_DEVICES) {
      const blocked = { block: true, reason: `BLOCKED needs: human-approval (mcp: ${path.trim()})` };
      expect(guardToolCall(call("write", { path, content: "{}" }), inside())).toEqual(blocked);
      expect(guardToolCall(call("write", { file_path: path, content: "{}" }), inside())).toEqual(blocked);
    }
  });

  test("T-06-07: the main session and a generic task child still reach MCP tools and devices", () => {
    for (const outside of [facts(undefined), facts("task")]) {
      for (const name of MCP_TOOLS) expect(guardToolCall(call(name, { id: "x" }), outside)).toBeUndefined();
      for (const path of MCP_DEVICES) expect(guardToolCall(call("write", { path, content: "{}" }), outside)).toBeUndefined();
    }
  });

  test("T-06-07: through the handler, a05, A01 and a headless SWARM_AGENT session block; main passes", () => {
    const { run } = guardHandler({ activeTools: ["task"] });
    const expected = { block: true, reason: "BLOCKED needs: human-approval (mcp: mcp__linear_save_issue)" };
    const device = { block: true, reason: "BLOCKED needs: human-approval (mcp: xd://mcp__linear_save_issue)" };
    for (const ctx of [agentCtx(CWD, B05, [], false), a01Ctx()]) {
      expect(run(call("mcp__linear_save_issue", {}), ctx)).toEqual(expected);
      expect(run(call("write", { path: "xd://mcp__linear_save_issue", content: "{}" }), ctx)).toEqual(device);
    }
    expect(run(call("mcp__linear_save_issue", {}), fakeCtx(CWD))).toBeUndefined();
    expect(run(call("write", { path: "xd://mcp__linear_save_issue", content: "{}" }), fakeCtx(CWD))).toBeUndefined();
    process.env.SWARM_AGENT = B05;
    expect(run(call("mcp__linear_save_issue", {}), fakeCtx(CWD))).toEqual(expected);
    expect(run(call("write", { path: "xd://mcp__linear_save_issue", content: "{}" }), fakeCtx(CWD))).toEqual(device);
  });

  test.each([
    ["xd://lsp", undefined],
    ["xd://ast_edit", undefined],
    ["xd://mcp_x", undefined],
    ["xd://run_code", "BLOCKED needs: human-approval (eval: xd-device)"],
    ["xd://debug?x=1", "BLOCKED needs: human-approval (eval: xd-device)"],
  ])("T-06-07: write %s keeps its outcome inside", (path, blocked) => {
    const res = guardToolCall(call("write", { path, content: JSON.stringify({ ops: [], paths: ["src"] }) }), facts(B05));
    expect(res).toEqual(blocked === undefined ? undefined : { block: true, reason: blocked });
  });

  const PROTECTED = [".swarm/tasks.db", "./.omp/config.yml", "~/.omp/agent/config.yml", `${CWD}/.swarm/x`, `${HOME}/.omp/y`, "src/../.swarm/z"];
  test.each(PROTECTED.flatMap((path) => [["write", path], ["edit", path]]))("D-08: %s %s blocks inside, passes in main", (tool, path) => {
    expect(guardToolCall(call(tool, { path, content: "x" }), facts(B05))).toEqual({
      block: true, reason: "BLOCKED needs: human-approval (protected_path: protected-path-write)",
    });
    expect(guardToolCall(call(tool, { path, content: "x" }), facts(undefined))).toBeUndefined();
  });

  /** omp's hashline (default), apply_patch and sloppy edit modes name the file inside `input`; write can drive xd://ast_edit. */
  const INPUT_SHAPED: [string, string, unknown][] = [
    ["edit", "hashline header with tag", { input: "[.omp/config.yml#AB12]\nPUT 1.=1:\n+extensions: []\n" }],
    ["edit", "hashline header without tag", { input: "*** Begin Patch\n[.swarm/tasks.db]\nPUT >$:\n+x\n*** End Patch\n" }],
    ["edit", "hashline header after a clean section", { input: "[src/app.ts#1A2B]\nPUT 1.=1:\n+a\n[~/.omp/agent/config.yml#C3D4]\nPUT 1.=1:\n+b\n" }],
    ["edit", "hashline MV into .swarm", { input: "[src/app.ts#1A2B]\nMV .swarm/app.ts\n" }],
    ["edit", "apply_patch Update File", { input: "*** Begin Patch\n*** Update File: .swarm/tasks.db\n@@\n-a\n+b\n*** End Patch\n" }],
    ["edit", "apply_patch Add File", { input: "*** Begin Patch\n*** Add File: .omp/config.yml\n+x\n*** End Patch\n" }],
    ["edit", "apply_patch Move to", { input: `*** Begin Patch\n*** Update File: src/a.ts\n*** Move to: ${CWD}/.omp/a.ts\n@@\n-a\n+b\n*** End Patch\n` }],
    ["edit", "apply_patch Delete File", { input: "*** Begin Patch\n*** Delete File: ~/.omp/x\n*** End Patch\n" }],
    ["write", "xd://ast_edit paths", { path: "xd://ast_edit", content: JSON.stringify({ ops: [{ pat: "a", out: "b" }], paths: ["src/x.ts", ".omp/config.yml"] }) }],
    ["write", "xd://ast_edit paths via file_path", { file_path: "xd://ast_edit", content: JSON.stringify({ ops: [], paths: [".swarm/tasks.db"] }) }],
    ["write", "XD://AST_EDIT?x=1 paths (any case, query)", { path: "XD://AST_EDIT?x=1", content: JSON.stringify({ ops: [], paths: ["~/.omp/x"] }) }],
    ["write", "xd://Ast_Edit/sub paths", { path: " xd://Ast_Edit/sub", content: JSON.stringify({ ops: [], paths: [".omp/config.yml"] }) }],
  ];
  test.each(INPUT_SHAPED)("D-08: %s %s blocks inside, passes in main", (tool, _shape, input) => {
    expect(guardToolCall(call(tool, input), facts(B05))).toEqual({
      block: true, reason: "BLOCKED needs: human-approval (protected_path: protected-path-write)",
    });
    expect(guardToolCall(call(tool, input), facts(undefined))).toBeUndefined();
  });

  test.each<[string, unknown]>([
    ["hashline into src", { input: "[src/app.ts#1A2B]\nPUT 1.=1:\n+x\n" }],
    ["hashline body row that looks like a header", { input: "[src/app.ts#1A2B]\nPUT 1.=1:\n+[.omp/config.yml#AB12]\n" }],
    ["apply_patch into src", { input: "*** Begin Patch\n*** Update File: src/app.ts\n@@\n-a\n+b\n*** End Patch\n" }],
    ["apply_patch body mentioning .swarm", { input: "*** Begin Patch\n*** Update File: docs/x.md\n@@\n-a\n+see .swarm/tasks.db\n*** End Patch\n" }],
    ["ast_edit on src", { path: "xd://ast_edit", content: JSON.stringify({ ops: [], paths: ["src"] }) }],
    ["ast_edit with unparsable content", { path: "xd://ast_edit", content: "{not json" }],
    ["input that is not a string", { input: 42 }],
  ])("D-08: edit %s passes inside", (_shape, input) => {
    expect(guardToolCall(call("edit", input), facts(B05))).toBeUndefined();
  });

  test.each(["src/app.ts", "docs/omp.md", ".swarmish/x", `${CWD}/a.omp`])("D-08: write %s passes", (path) => {
    expect(guardToolCall(call("write", { path }), facts(B05))).toBeUndefined();
  });

  test.each(["echo x > .swarm/a", "tee -a .omp/config.yml", "cp f ~/.omp/x", "mv a .swarm/b", "cat a >> ~/.omp/b", "install -t .swarm f"])(
    "D-08 shell: %s blocks inside, passes in main",
    (command) => {
      expect(guardToolCall(bash(command), facts(B05))).toEqual({
        block: true, reason: "BLOCKED needs: human-approval (protected_path: protected-path-shell)",
      });
      expect(guardToolCall(bash(command), facts(undefined))).toBeUndefined();
    },
  );

  test.each([
    "cat .swarm/tasks.db", "cp .omp/config.yml /tmp/x.yml", "echo x > out.txt", "ls 2>&1 | tee log.txt",
    // WR-06 negatives: reads and mutations elsewhere
    "sed 's/a/b/' .omp/config.yml", "sed -i 's/a/b/' README.md", "sqlite3 /tmp/x.db 'SELECT 1'", "tar -tf .swarm/a.tar", "tar -xf a.tar -C build",
    "dd if=.swarm/tasks.db of=/tmp/x", "chmod 755 x", "mkdir -p build/out", "touch out.txt", "ln -s /a /b", "rsync -a .swarm/ /tmp/bak/",
    "unzip a.zip -d build", "rmdir build", "truncate -s0 log.txt", "mv a.txt b.txt", "mv -t build a.txt", "rsync -a --remove-source-files out/ /tmp/bak/",
  ])("D-08 shell: %s passes", (command) => {
    expect(guardToolCall(bash(command), facts(B05))).toBeUndefined();
  });

  /** WR-06: in-place mutation of the state dirs through other tools; rm of them is the destructive row. */
  test.each([
    ["truncate -s0 .swarm/tasks.db", "protected_path: protected-path-mutate"],
    ["sed -i 's/a/b/' .omp/config.yml", "protected_path: protected-path-mutate"],
    ["sed -i -e 's/a/b/' .omp/config.yml", "protected_path: protected-path-mutate"],
    ["sed --in-place=.bak 's/a/b/' ~/.omp/agent/config.yml", "protected_path: protected-path-mutate"],
    ["sqlite3 ~/.omp/x.db 'DELETE FROM t'", "protected_path: protected-path-mutate"],
    ["chmod 600 .swarm/keys", "protected_path: protected-path-mutate"],
    ["chown u:g .swarm", "protected_path: protected-path-mutate"],
    ["ln -s /tmp/x .swarm/tasks.db", "protected_path: protected-path-mutate"],
    ["rsync -a src/ .swarm/", "protected_path: protected-path-mutate"],
    ["dd if=/dev/zero of=.swarm/tasks.db", "protected_path: protected-path-mutate"],
    ["tar -xf a.tar -C .swarm", "protected_path: protected-path-mutate"],
    ["tar xf a.tar --directory=.swarm", "protected_path: protected-path-mutate"],
    ["unzip a.zip -d ~/.omp", "protected_path: protected-path-mutate"],
    ["touch .swarm/x", "protected_path: protected-path-mutate"],
    ["mkdir -p .omp/extensions", "protected_path: protected-path-mutate"],
    ["rmdir .swarm/plans", "protected_path: protected-path-mutate"],
    ["unlink .omp/config.yml", "protected_path: protected-path-mutate"],
    ["mv .swarm/tasks.db /tmp/x", "protected_path: protected-path-mutate"],
    ["mv -t /tmp .omp/config.yml", "protected_path: protected-path-mutate"],
    ["mv --target-directory=/tmp a .swarm/keys", "protected_path: protected-path-mutate"],
    ["rsync -a --remove-source-files .swarm/results/ /tmp/bak/", "protected_path: protected-path-mutate"],
    ["rm .swarm/tasks.db", "destructive: rm-rf-protected"],
    ["rm -f .swarm/tasks.db", "destructive: rm-rf-protected"],
    ["rm -r .swarm", "destructive: rm-rf-protected"],
    ["rm -r .git", "destructive: rm-rf-protected"],
    ["rm -f ~/.omp/agent/config.yml", "destructive: rm-rf-protected"],
    [`rm ${CWD}/.omp/config.yml`, "destructive: rm-rf-protected"],
  ])("WR-06: %s blocks inside naming %s, passes in main", (command, tail) => {
    expect(guardToolCall(bash(command), facts(B05))).toEqual({ block: true, reason: `BLOCKED needs: human-approval (${tail})` });
    expect(guardToolCall(bash(command), facts(undefined))).toBeUndefined();
  });

  test("WR-06: sqlite3 on the Task Store is swarm-state for a05 and a protected-path mutation for A01", () => {
    const write = bash("sqlite3 .swarm/tasks.db \"UPDATE tasks SET state='DONE'\"");
    expect(guardToolCall(write, facts(B05))).toEqual({ block: true, reason: SWARM_STATE });
    expect(guardToolCall(bash(`sqlite3 ${CWD}/.swarm/tasks.db .dump`), facts(B05))).toEqual({ block: true, reason: SWARM_STATE });
    expect(guardToolCall(write, facts(A01))).toEqual({ block: true, reason: "BLOCKED needs: human-approval (protected_path: protected-path-mutate)" });
    expect(guardToolCall(write, facts(undefined))).toBeUndefined();
  });

  /** A `cd`/`pushd`/`env -C` earlier in the command moves where later relative targets resolve (Greptile P1). */
  test.each([
    ["cd .swarm && touch tasks.db", "protected_path: protected-path-mutate"],
    ["cd .omp; echo x > config.yml", "protected_path: protected-path-shell"],
    ["cd sub && cd ../.swarm && rm tasks.db", "destructive: rm-rf-protected"],
    ["cd .swarm/results && sed -i s/a/b/ x", "protected_path: protected-path-mutate"],
    [`pushd ${CWD}/.omp && tee config.yml`, "protected_path: protected-path-shell"],
    ["cd ~/.omp/agent && echo x >> config.yml", "protected_path: protected-path-shell"],
    ["cd -P -- .swarm && truncate -s0 tasks.db", "protected_path: protected-path-mutate"],
    ["(cd .swarm; touch tasks.db)", "protected_path: protected-path-mutate"],
    ['bash -c "cd .swarm && touch tasks.db"', "protected_path: protected-path-mutate"],
    ["cd .swarm && bash -c 'echo x > tasks.db'", "protected_path: protected-path-shell"],
    ["cd .git && rm -r objects", "destructive: rm-rf-protected"],
    ["cd .. && rm -rf nonexistent-guard-cwd", "destructive: rm-rf-protected"],
    // env -C / sudo -D run the command (and its sh -c payload) in that directory
    [`env -C ${CWD}/.swarm sh -c 'echo x > tasks.db'`, "protected_path: protected-path-shell"],
    ["env --chdir=.omp touch config.yml", "protected_path: protected-path-mutate"],
    ["env -iC .swarm touch tasks.db", "protected_path: protected-path-mutate"],
    ["sudo -D .swarm touch tasks.db", "protected_path: protected-path-mutate"],
    // a directory that cannot be resolved fails closed for every later relative write target
    ['cd "$DIR" && touch x', "protected_path: protected-path-mutate"],
    ["cd - && echo x > y", "protected_path: protected-path-shell"],
    ["cd $(dirname .swarm/x) && touch tasks.db", "protected_path: protected-path-mutate"],
    ['cd .swarm && echo "$(touch tasks.db)"', "protected_path: protected-path-mutate"],
    // scope: a move that may fail or may not run keeps the old directory too; only restored scopes drop it
    ["true || cd .swarm; touch tasks.db", "protected_path: protected-path-mutate"],
    ["cd .swarm; cd /nonexistent; touch tasks.db", "protected_path: protected-path-mutate"],
    ["pushd /nonexistent; cd .swarm; popd; touch tasks.db", "protected_path: protected-path-mutate"],
    ["cd .swarm; pushd /; pushd /nonexistent; popd; touch tasks.db", "protected_path: protected-path-mutate"],
    ["cd .swarm; cd .. & touch tasks.db", "protected_path: protected-path-mutate"],
    ["cd .swarm; nohup cd ..; touch tasks.db", "protected_path: protected-path-mutate"],
    ["cd .swarm; for d in; do cd ..; done; touch tasks.db", "protected_path: protected-path-mutate"],
    ["if cd .swarm; then touch tasks.db; fi", "protected_path: protected-path-mutate"],
    ["echo | cd .swarm; touch tasks.db", "protected_path: protected-path-mutate"],
    ["{ cd .swarm; }; touch tasks.db", "protected_path: protected-path-mutate"],
    ["(cd .swarm; case x in a) touch tasks.db;; esac)", "protected_path: protected-path-mutate"],
    ["pushd .swarm; popd +1; touch tasks.db", "protected_path: protected-path-mutate"],
  ])("D-08 cd: %s blocks inside naming %s, passes in main", (command, tail) => {
    expect(guardToolCall(bash(command), facts(B05))).toEqual({ block: true, reason: `BLOCKED needs: human-approval (${tail})` });
    expect(guardToolCall(bash(command), facts(undefined))).toBeUndefined();
  });

  test.each([
    "cd src && touch a.ts", "cd .swarm && cat tasks.db", "cd .swarm && ls; cd .. && git status", "cd /tmp && echo x > out.txt",
    'cd "$DIR" && echo x > /tmp/out.txt', "env -C build make", "cd build && make -j$(nproc)", "pushd src && sed -i s/a/b/ x.ts && popd",
    // restored or one-command scopes do not reach later writes
    "pushd .omp; popd; echo x > out.txt", "(cd .swarm && cat x); echo y > out.txt", "env -C .swarm true; touch a.txt",
    "cd .swarm; cd ..; touch a.txt", "cd .swarm | touch a.txt", "cd .swarm & touch a.txt", "sudo -D .swarm true && touch a.txt",
  ])("D-08 cd: %s passes", (command) => {
    expect(guardToolCall(bash(command), facts(B05))).toBeUndefined();
  });

  test("D-08 cd: sqlite3 opened after cd .swarm is swarm-state for a05", () => {
    expect(guardToolCall(bash("cd .swarm && sqlite3 tasks.db \"UPDATE tasks SET state='DONE'\""), facts(B05))).toEqual({ block: true, reason: SWARM_STATE });
    expect(guardToolCall(bash(`env -C ${CWD}/.swarm sqlite3 tasks.db .dump`), facts(B05))).toEqual({ block: true, reason: SWARM_STATE });
  });

  /** Compound keywords and a lone `&` put a command behind them; every row still sees it. */
  test.each([
    ["if true; then git push -f; fi", "destructive: git-force-push"],
    ["for i in 1; do touch .swarm/x; done", "protected_path: protected-path-mutate"],
    ["true & git push --force", "destructive: git-force-push"],
    ["! git push -f", "destructive: git-force-push"],
    ["while false; do :; done; until true; do rm -rf /; done", "destructive: rm-rf-protected"],
    ["if false; then :; elif true; then git reset --hard; else :; fi", "destructive: git-reset-hard"],
    ["make |& git push -f", "destructive: git-force-push"],
    // `>|` forms are redirections, not pipes
    ["echo x >| .swarm/tasks.db", "protected_path: protected-path-shell"],
    ["echo x 2>| .omp/config.yml", "protected_path: protected-path-shell"],
    ["echo x &>| .swarm/tasks.db", "protected_path: protected-path-shell"],
    // busybox/toybox run the applet named next
    ["busybox tee .swarm/tasks.db", "protected_path: protected-path-shell"],
    ["busybox cp /tmp/x .swarm/tasks.db", "protected_path: protected-path-shell"],
    ["toybox mv .swarm/tasks.db /tmp/x", "protected_path: protected-path-mutate"],
    ["/bin/busybox rm -rf .swarm", "destructive: rm-rf-protected"],
    ["sudo busybox sed -i s/a/b/ .omp/config.yml", "protected_path: protected-path-mutate"],
  ])("keywords: %s blocks inside naming %s", (command, tail) => {
    expect(guardToolCall(bash(command), facts(B05))).toEqual({ block: true, reason: `BLOCKED needs: human-approval (${tail})` });
    expect(guardToolCall(bash(command), facts(undefined))).toBeUndefined();
  });

  test.each([
    "if [ -f x ]; then echo ok; fi", "make &> build.log", "ls 2>&1 | tee log.txt", "sleep 1 & wait; echo x > out.txt", "cat a >&2",
    "ls | tee out.txt", "false || echo x > out.txt", "echo x >| out.txt", "busybox ls .swarm", "busybox --list", "toybox cat .omp/config.yml",
  ])(
    "keywords: %s passes",
    (command) => {
      expect(guardToolCall(bash(command), facts(B05))).toBeUndefined();
    },
  );

  const MUTATE = "protected_path: protected-path-mutate";
  const SHELL = "protected_path: protected-path-shell";
  const RM = "destructive: rm-rf-protected";
  const STDIN = "destructive: shell-stdin";
  /** Lexical spellings bash reads as the plain command: quoting, escapes, ANSI-C `$'…'`, `${IFS}`, brace lists, redirection placement. */
  test.each([
    ["echo x \\> | tee .swarm/tasks.db", SHELL],
    ["echo x \\>& git push -f", "destructive: git-force-push"],
    ['busybox "rm" -rf .swarm', RM],
    ["toybox 'tee' .swarm/x", SHELL],
    ["busybox \\tee .swarm/x", SHELL],
    ['"rm" -rf .swarm', RM],
    ["'git' push -f", "destructive: git-force-push"],
    ["\\rm -rf .swarm", RM],
    ['r""m -rf .swarm', RM],
    ['"sudo" rm -rf .swarm', RM],
    ['sed "-i" s/a/b/ .omp/config.yml', MUTATE],
    ["$'touch' .swarm/x", MUTATE],
    ["touch $'\\x2eswarm/x'", MUTATE],
    ["echo $'\\''; touch .swarm/x; echo '", MUTATE],
    ['sh -c "touch .sw\\arm/x"', MUTATE],
    ["touch${IFS}.swarm/x", MUTATE],
    ["{touch,.swarm/x}", MUTATE],
    ["2>/dev/null rm -rf .swarm", RM],
    ["cp x .swarm/y 2>/dev/null", SHELL],
    ["> .swarm/x", SHELL],
    ["cat <> .swarm/tasks.db", SHELL],
    ["cmd >& .swarm/x", SHELL],
    ["exec 3>.swarm/x", SHELL],
    ["touch .SWARM/x", MUTATE],
  ])("lexical: %s blocks inside naming %s", (command, tail) => {
    expect(guardToolCall(bash(command), facts(B05))).toEqual({ block: true, reason: `BLOCKED needs: human-approval (${tail})` });
    expect(guardToolCall(bash(command), facts(undefined))).toBeUndefined();
  });

  /** Targets and commands bash only knows at run time fail closed: expansions, cut substitutions, `~user`, `/proc/*\/cwd`, globs that may name a state dir. */
  test.each([
    ['echo x > "$(printf .swarm/tasks.db)"', SHELL],
    ["echo x > $(echo .swarm)/tasks.db", SHELL],
    ["touch $(echo .swarm)/x", MUTATE],
    ['echo x > "$F"', SHELL],
    ['echo x | tee "$SWARM_DIR/tasks.db"', SHELL],
    ["rm -rf $(mktemp -d)", RM],
    ['for f in *.log; do rm "$f"; done', RM],
    ["echo x > ~root/x", SHELL],
    ["cd .swarm; cd ..; touch ~-/tasks.db", MUTATE],
    ["cd .swarm && touch /proc/self/cwd/x", MUTATE],
    ["HOME=/nonexistent-guard-cwd/.swarm; touch ~/x", MUTATE],
    ["CDPATH=/nonexistent-guard-cwd/.swarm cd x && touch y", MUTATE],
    ["$RM -rf .swarm", MUTATE],
    ["$(which rm) -rf .swarm", MUTATE],
    ["/bin/r? -rf .swarm", MUTATE],
    ["touch .swar[m]/x", MUTATE],
    ["rm -rf .sw*", RM],
    ["echo x > .{swarm,x}/tasks.db", SHELL],
    ["touch .{a..z}warm/x", MUTATE],
    ["touch .sw@(a)rm/x", MUTATE],
    ["rm -rf .gi?", RM],
    ["rm -rf .*", RM],
    ["rm -rf /tmp/{x,..}", RM],
    ["touch /nonexistent-guard-cwd/.?/x", MUTATE],
    ["shopt -s dotglob", "protected_path: glob-dotfiles"],
    ["GLOBIGNORE=x", "protected_path: glob-dotfiles"],
    ["shopt -qs extglob dotglob", "protected_path: glob-dotfiles"],
    ["declare -x GLOBIGNORE=.", "protected_path: glob-dotfiles"],
    ["unsetopt no_glob_dots", "protected_path: glob-dotfiles"],
    ["set -o GLOB_DOTS", "protected_path: glob-dotfiles"],
    ["zsh --globdots -c 'rm -r *'", "protected_path: glob-dotfiles"],
    ["echo x > /proc/self/cwd/.swarm/x", SHELL],
    ["echo x > /proc/self/" + "root/nonexistent-guard-cwd/.swarm/x", SHELL],
    ["echo x > /proc/1/cwd/x", SHELL],
    ["echo x > /proc/1/fd/3", SHELL],
  ])("fail closed: %s blocks inside naming %s", (command, tail) => {
    expect(guardToolCall(bash(command), facts(B05))).toEqual({ block: true, reason: `BLOCKED needs: human-approval (${tail})` });
    expect(guardToolCall(bash(command), facts(undefined))).toBeUndefined();
  });

  test.each([
    "grep -R dotglob .", 'git commit -m "fix dotglob handling"', "echo GLOBIGNORE", "man shopt", "shopt dotglob", "shopt -u dotglob",
    'git commit -m "GLOBIGNORE=x is unsafe"', "setopt noglobdots", "echo x > /proc/self/fd/1", "echo x > /proc/$$/fd/2",
    "echo x > /proc/thread-self/fd/0", "cmd 2> /proc/self/fd/1", "echo x > /dev/fd/3",
  ])("dotglob and descriptors: %s passes", (command) => {
    expect(guardToolCall(bash(command), facts(B05))).toBeUndefined();
  });

  /** Commands run by another command (find, xargs, parallel, runners, stdin scripts) and the less common writers. */
  test.each([
    ["find . -name tasks.db -exec rm {} \\;", RM],
    ["find . -name '*.pyc' -delete", MUTATE],
    ["find . -exec sh -c 'touch .swarm/x' \\;", MUTATE],
    ["find . -fprint .swarm/x", MUTATE],
    ["echo .swarm/tasks.db | xargs rm", RM],
    ["ls | xargs -I{} sh -c 'rm {}'", RM],
    ["parallel rm ::: a", RM],
    ["setsid rm -rf .swarm", RM],
    ["flock /tmp/l -c 'touch .swarm/x'", MUTATE],
    ["flock /tmp/l touch .swarm/x", MUTATE],
    ["su -c 'touch .swarm/x' root", MUTATE],
    ["env -S 'touch .swarm/x'", MUTATE],
    ["watch -n1 'touch .swarm/x'", MUTATE],
    ["exec -a x rm -rf .swarm", RM],
    ["pkexec touch .swarm/x", MUTATE],
    ["alias ls='rm -rf .swarm'", RM],
    ["trap 'touch .swarm/x' EXIT", MUTATE],
    ["bash -o pipefail -c 'touch .swarm/x'", MUTATE],
    ["bash <<EOF\ntouch .swarm/x\nEOF", STDIN],
    ["sh <<< 'touch .swarm/x'", STDIN],
    ["echo 'touch .swarm/x' | sh", STDIN],
    ["sudo -e .omp/config.yml", MUTATE],
    ["perl -pi -e 's/a/b/' .omp/config.yml", MUTATE],
    ["awk -i inplace '{print}' .omp/config.yml", MUTATE],
    ["sort -o .swarm/tasks.db x", SHELL],
    ["curl -o .swarm/tasks.db http://x", SHELL],
    ["cd .swarm && curl -O http://x/tasks.db", SHELL],
    ["install -d .swarm/x", MUTATE],
    ["ln .swarm/tasks.db /tmp/x", MUTATE],
    ["cp -al .swarm /tmp/x", MUTATE],
    ["npx tsx scripts/ts/orch_plan.ts", "swarm-state"],
    ["gzip .swarm/tasks.db", MUTATE],
    ["tar -cf .swarm/tasks.db src", MUTATE],
    ["zip .swarm/x.zip a", MUTATE],
    ["sponge .omp/config.yml", MUTATE],
    ["git checkout -- .omp/config.yml", MUTATE],
    ["git -C .omp checkout -- config.yml", MUTATE],
    ["git push --all origin", "prod_high_risk: git-push-protected"],
  ])("runners and writers: %s blocks inside naming %s", (command, tail) => {
    expect(guardToolCall(bash(command), facts(B05))).toEqual({ block: true, reason: `BLOCKED needs: human-approval (${tail})` });
    expect(guardToolCall(bash(command), facts(undefined))).toBeUndefined();
  });

  test.each([
    "echo x > /dev/null", "cmd 2>&1", "make 2>&1 | tee out.txt", "cmd >&2", "ls *.ts > out.txt", "find . -name x -exec grep y {} \\;",
    'echo x > "$HOME/notes.txt"', 'echo x > "$TMPDIR/x"', 'echo "$(date)" > build.log', "rm -rf *", "rm -r ?omp", "cp a{,.bak}",
    "echo '>' | tee out.txt", "[ -f .swarm/x ] && echo ok", '"$PYTHON" -m pytest', "git ls-files | xargs wc -l", "ls | xargs -I{} cp {} /tmp/",
    "gzip -c .swarm/tasks.db > /tmp/x.gz", "tar -czf out.tgz src", "git checkout main", "kubectl get pods -o json",
    "curl -sSL https://example.com -o /tmp/x.html", "cat <<EOF > notes.md\ntouch .swarm/x\nEOF", 'git commit -m "fix: a -> b"',
  ])("lexical: %s passes", (command) => {
    expect(guardToolCall(bash(command), facts(B05))).toBeUndefined();
  });

  /** Output-file options are per command; a substitution body runs in its holder's directory as a subshell. */
  test.each([
    ["curl -o .swarm/x https://x", SHELL],
    ["curl -sSLo .swarm/x https://x", SHELL],
    ["wget -qO.swarm/x https://x", SHELL],
    ["sort -o .swarm/x in", SHELL],
    ["gcc -o .omp/x a.c", SHELL],
    ["go test -coverprofile=.swarm/c.out ./...", SHELL],
    ['cd .swarm && echo "$(date)" > tasks.db', SHELL],
    ['echo "$(cd .swarm && touch x)"', MUTATE],
    ['cd "$(pwd)/.swarm" && touch x', MUTATE],
    ["$(touch .swarm/x)", MUTATE],
    ["cd .swarm && cat <<EOF\n$(touch x)\nEOF", MUTATE],
    ['cd .swarm && bash -c "echo $(touch x)"', MUTATE],
  ])("scoped: %s blocks inside naming %s", (command, tail) => {
    expect(guardToolCall(bash(command), facts(B05))).toEqual({ block: true, reason: `BLOCKED needs: human-approval (${tail})` });
    expect(guardToolCall(bash(command), facts(undefined))).toBeUndefined();
  });

  /** Everyday development commands a specialist runs must never need approval. */
  test.each([
    "grep -o .swarm/tasks.db log.txt", "rg -o foo .swarm", 'cd build && echo "$(date)" > out.txt',
    'cd src && cat "$(git rev-parse --show-toplevel)/README.md" > /tmp/r', "echo $(git rev-parse HEAD) > VERSION",
    "npm install", "npm ci", "npm run build", "npm test -- --watch=false", "npx prettier --check .", "bun install", "bun run test",
    "bun test test/x.test.ts", "bunx tsc --noEmit", "pip install -r requirements.txt", "python3 -m pytest -q", "pytest -x tests/test_a.py",
    "git add -A", 'git commit -m "feat: x"', "git status --short", "git diff HEAD~1 -- src/", "git checkout -b feat/x", "git switch main",
    "git stash", "git pull --rebase", "git push -u origin feat/x", "make -j8 test", "cargo build --release", "cargo test", "go test ./...",
    "go build -o bin/app ./cmd/app", "docker build -t app .", "docker compose up -d", "ls -la > files.txt", "echo done | tee -a log.txt",
    "mkdir -p dist && cp -r src/*.md dist/", "rm -rf node_modules dist", "find . -name '*.ts' | xargs grep -l foo", "tar -czf dist.tgz dist",
    "curl -o /tmp/x.json https://x", "uvicorn app:app --reload &", "export NODE_ENV=test && bun test", "ruff check . --fix", "tsc -p tsconfig.json",
    "cd omp && bun run test 2>&1 | tail -20", "for f in src/*.ts; do echo $f; done", "wc -l $(git ls-files '*.py')", "date > build/stamp.txt",
  ])("common: %s passes", (command) => {
    expect(guardToolCall(bash(command), facts(B05))).toBeUndefined();
  });

  /** eval, watch, parallel and `sudo -s|-i` join all their arguments into one command line; `env -S` splits its string and appends the rest. */
  const FORCE = "destructive: git-force-push";
  test.each([
    ['eval "git push" --force', FORCE],
    ['eval git push "--force"', FORCE],
    ["eval 'git' 'push' '-f'", FORCE],
    ["eval -- git push -f", FORCE],
    ["eval $'git push \\x2df'", FORCE],
    ['env -S "git push" --force', FORCE],
    ["env -S'git push' -f", FORCE],
    ['watch -n 1 "git push" --force', FORCE],
    ['sudo -i "git push" -f', FORCE],
    ["nice -n 5 eval 'git push' -f", FORCE],
    ['eval "echo x" "> .swarm/y"', SHELL],
    ["alias a=ls b='rm -rf .swarm'", RM],
    ["trap -- 'touch .swarm/x' EXIT", MUTATE],
    ["su -c'touch .swarm/x' root", MUTATE],
    ["su --command='touch .swarm/x' root", MUTATE],
    ["su - root -c 'touch .swarm/x'", MUTATE],
    ["flock /tmp/l -c'touch .swarm/x'", MUTATE],
    ["script -q -c 'touch .swarm/x' /dev/null", MUTATE],
    // a shell's `-c` line: attached, clustered, or after `--`
    ["bash -c'touch .swarm/x'", MUTATE],
    ['sh -c"git push --force"', FORCE],
    ["bash -lc'git push -f'", FORCE],
    ['sh -ec"touch .swarm/x"', MUTATE],
    ["bash -cl 'git push -f'", FORCE],
    ["sh -c -- 'git push -f'", FORCE],
    ["/bin/zsh -o pipefail -c 'git push -f'", FORCE],
    // a shell reading its script from stdin is blocked whatever the script: it may be built at run time
    ["/bin/echo 'touch .swarm/x' | sh", STDIN],
    ["/usr/bin/printf '%s\\n' 'git push --force' | bash", STDIN],
    ["command echo 'ls' | sh", STDIN],
    ["echo -e 'touch .swarm/x' | sh", STDIN],
    ["cat <<< 'git push -f' | sh", STDIN],
    ["(echo ls) | { bash; }", STDIN],
    ["sh -s -- -y", STDIN],
    ["bash /dev/stdin < s.sh", STDIN],
    // a command that really reassigns HOME makes `~` unknowable
    ["HOME=.swarm; echo x > ~/tasks.db", SHELL],
    ["export HOME=/x; echo > ~/y", SHELL],
    ["read -r HOME; echo > ~/y", SHELL],
    ["eval 'HOME=/x'; touch ~/y", MUTATE],
  ])("joined lines: %s blocks inside naming %s", (command, tail) => {
    expect(guardToolCall(bash(command), facts(B05))).toEqual({ block: true, reason: `BLOCKED needs: human-approval (${tail})` });
    expect(guardToolCall(bash(command), facts(undefined))).toBeUndefined();
  });

  test.each([
    "bash -c 'echo ok' 'git push -f'", 'eval "echo ok"', "watch -n 5 ls", "alias ll='ls -la'", "trap 'rm -f /tmp/lock' EXIT",
    "echo HOME > ~/results.txt", "grep HOME .env > /tmp/x", "echo $HOME > ~/y", "bash -lc 'echo ok'", "/bin/echo 'git push -f'",
    "printf 'git push -f\\n' > notes.txt", "echo 'touch .swarm/x' | cat",
    // a shell with its own script or `-c` line, and non-shells ending in `sh`, may take stdin
    "bash script.sh < in.txt", "echo x | bash -c 'cat'", "echo x | ssh host cat", "mosh host < /dev/null", "source env.sh",
    "echo ls | bash -cl 'cat'",
  ])("joined lines: %s passes", (command) => {
    expect(guardToolCall(bash(command), facts(B05))).toBeUndefined();
  });

  test("bash tool cwd and env: the command starts in `cwd`; HOME, CDPATH, GLOBIGNORE and BASHOPTS reach the shell", () => {
    const run = (input: Record<string, unknown>) => guardToolCall(call("bash", input), facts(B05));
    expect(run({ command: "touch tasks.db", cwd: ".swarm" })).toEqual({ block: true, reason: `BLOCKED needs: human-approval (${MUTATE})` });
    expect(run({ command: "touch tasks.db", cwd: "src" })).toBeUndefined();
    // an internal URL cwd is a directory the guard cannot place
    expect(run({ command: "touch tasks.db", cwd: "local://x" })).toEqual({ block: true, reason: `BLOCKED needs: human-approval (${MUTATE})` });
    expect(run({ command: "touch ~/tasks.db", env: { HOME: `${CWD}/.swarm` } })).toEqual({ block: true, reason: `BLOCKED needs: human-approval (${MUTATE})` });
    expect(run({ command: "cd x && touch y", env: { CDPATH: `${CWD}/.swarm` } })).toEqual({ block: true, reason: `BLOCKED needs: human-approval (${MUTATE})` });
    for (const key of ["GLOBIGNORE", "BASHOPTS"]) {
      expect(run({ command: "ls", env: { [key]: "x" } })).toEqual({ block: true, reason: "BLOCKED needs: human-approval (protected_path: glob-dotfiles)" });
    }
    expect(guardToolCall(call("bash", { command: "touch tasks.db", cwd: ".swarm" }), facts(undefined))).toBeUndefined();
  });

  test("through the handler: eval, protected write and orch shell block for a05; main untouched", () => {
    const { run } = guardHandler({ activeTools: ["task"] });
    const a05 = agentCtx(CWD, B05, [], false);
    expect((run(call("eval", { code: "x" }), a05) as { block?: boolean }).block).toBe(true);
    expect((run(call("write", { path: ".omp/config.yml" }), a05) as { block?: boolean }).block).toBe(true);
    expect((run(bash(ORCH[0]), a05) as { reason?: string }).reason).toBe(SWARM_STATE);
    for (const ev of [call("eval", { code: "x" }), call("write", { path: ".omp/config.yml" }), bash(ORCH[0])]) {
      expect(run(ev, fakeCtx(CWD))).toBeUndefined();
    }
  });

  /** D-08: the agent-swarm runtime root (the live guard, the gate scripts the runner runs with keys) is not writable. */
  const ROOT = "/nonexistent-swarm-root";
  const WS = "/nonexistent-ws";
  const inWs = (cwd = WS) => facts(B05, { cwd, runtimeRoots: [ROOT] });
  const RUNTIME_WRITES: [string, unknown, string][] = [
    ["write", { path: `${ROOT}/omp/src/guard.ts`, content: "x" }, "protected-path-write"],
    ["write", { file_path: `${ROOT}/agents.json`, content: "x" }, "protected-path-write"],
    ["edit", { input: `[${ROOT}/scripts/qa_gate.py#AB12]\nPUT 1.=1:\n+x\n` }, "protected-path-write"],
    ["write", { path: "xd://ast_edit", content: JSON.stringify({ ops: [], paths: [`${ROOT}/swarm/gates.py`] }) }, "protected-path-write"],
    ["bash", { command: `echo x > ${ROOT}/scripts/qa_gate.py` }, "protected-path-shell"],
    ["bash", { command: `cp x ${ROOT}/scripts/rev_gate.py` }, "protected-path-shell"],
    ["bash", { command: `cd ${ROOT}/scripts && touch qa_gate.py` }, "protected-path-mutate"],
    ["bash", { command: `sed -i s/a/b/ ${ROOT}/hooks/autonomous_run.py` }, "protected-path-mutate"],
    ["bash", { command: `mv ${ROOT}/scripts/qa_gate.py /tmp/x` }, "protected-path-mutate"],
    ["bash", { command: `echo x &>| ${ROOT}/swarm/envelope.py` }, "protected-path-shell"],
    ["bash", { command: `busybox cp /tmp/x ${ROOT}/scripts/qa_gate.py` }, "protected-path-shell"],
    ["bash", { command: "touch /nonexistent-swarm-roo?/scripts/qa_gate.py" }, "protected-path-mutate"],
    ["bash", { command: "echo x > /nonexistent-swarm-root/{scripts,x}/qa_gate.py" }, "protected-path-shell"],
    ["bash", { command: "echo x > /NONEXISTENT-SWARM-ROOT/scripts/qa_gate.py" }, "protected-path-shell"],
    ["bash", { command: `cd ../nonexistent-swarm-root && echo x >> omp/src/guard.ts` }, "protected-path-shell"],
  ];
  test.each(RUNTIME_WRITES)("runtime root: %s %p blocks from another workspace naming %s", (tool, input, id) => {
    expect(guardToolCall(call(tool, input), inWs())).toEqual({ block: true, reason: `BLOCKED needs: human-approval (protected_path: ${id})` });
    expect(guardToolCall(call(tool, input), facts(undefined, { cwd: WS, runtimeRoots: [ROOT] }))).toBeUndefined();
  });

  test("runtime root: rm of a runtime file is destructive", () => {
    expect(guardToolCall(bash(`rm ${ROOT}/scripts/sec_gate.py`), inWs())).toEqual({ block: true, reason: "BLOCKED needs: human-approval (destructive: rm-rf-protected)" });
  });

  test.each<[string, unknown]>([
    ["bash", { command: `cat ${ROOT}/scripts/qa_gate.py` }], ["bash", { command: `python3 ${ROOT}/scripts/code_checks.py --json` }],
    ["bash", { command: `cp ${ROOT}/scripts/x.py ./x.py` }], ["bash", { command: "echo x > out.txt" }],
    ["bash", { command: `echo x > ${ROOT}-other/x` }], ["write", { path: "src/app.ts", content: "x" }],
  ])("runtime root: %s %p passes from another workspace", (tool, input) => {
    expect(guardToolCall(call(tool, input), inWs())).toBeUndefined();
  });

  test.each([ROOT, `${ROOT}/omp`])("runtime root: a session working in the checkout (cwd %s) writes it (self-development residual)", (cwd) => {
    expect(guardToolCall(bash(`echo x > ${ROOT}/scripts/foo.py`), inWs(cwd))).toBeUndefined();
    expect(guardToolCall(call("write", { path: `${ROOT}/omp/src/guard.ts`, content: "x" }), inWs(cwd))).toBeUndefined();
    expect(guardToolCall(bash(`echo x > ${ROOT}/.swarm/tasks.db`), inWs(cwd))).toEqual({
      block: true, reason: "BLOCKED needs: human-approval (protected_path: protected-path-shell)",
    });
  });

  test("runtime root through the handler: the package's repo is protected from another workspace, not from itself", () => {
    const { run } = guardHandler({ activeTools: ["task"] });
    const write = call("write", { path: join(REPO_ROOT, "scripts", "qa_gate.py"), content: "x" });
    expect(run(write, agentCtx(WS, B05, [], false))).toEqual({ block: true, reason: "BLOCKED needs: human-approval (protected_path: protected-path-write)" });
    expect(run(write, agentCtx(REPO_ROOT, B05, [], false))).toBeUndefined();
    expect(run(write, fakeCtx(WS))).toBeUndefined();
  });

  const MALFORMED: [string, unknown][] = [
    ["bash", null], ["bash", { command: 7 }], ["bash", { command: { x: 1 } }], ["write", null], ["write", {}], ["write", { path: 3 }],
    ["edit", { path: null }], ["edit", "path"], ["read", undefined], ["yield", null], ["eval", undefined], ["write", { path: "" }],
  ];
  test.each(MALFORMED)("malformed %s %p never throws inside or outside", (tool, input) => {
    for (const f of [facts(B05), facts(undefined), facts(A01), facts(undefined, { env: { SWARM_TASK_ID: "T" } }), facts(B05, { cwd: "", home: "", tmp: "" })]) {
      expect(() => guardToolCall(call(tool, input), f)).not.toThrow();
    }
    expect(guardToolCall(call(tool, input), facts(undefined))).toBeUndefined();
  });
});

describe("private signer file reads", () => {
  const KEY = `${HOME}/.config/agent-swarm/signing-key.hex`;
  const CONFIGURED = "/nonexistent-private/signer.hex";
  const facts: GuardFacts = {
    ...capped, agent: "a05-backend", hasTask: true, cwd: CWD, home: HOME,
  };
  const blockedRead = { block: true, reason: "BLOCKED needs: human-approval (signer_credential: signer-file-read)" };
  const blockedShell = { block: true, reason: "BLOCKED needs: human-approval (signer_credential: signer-file-shell)" };
  const blockedWrite = { block: true, reason: "BLOCKED needs: human-approval (protected_path: protected-path-write)" };
  const blockedMutate = { block: true, reason: "BLOCKED needs: human-approval (protected_path: signer-file-write)" };

  test("signer files are protected against writes, output redirects and diff/cmp reads", () => {
    expect(guardToolCall(call("write", { path: KEY, content: "x" }), facts)).toEqual(blockedWrite);
    expect(guardToolCall(call("edit", { path: KEY, old: "a", new: "b" }), facts)).toEqual(blockedWrite);
    expect(guardToolCall(call("write", { path: `${CWD}/notes.txt`, content: "x" }), facts)).toBeUndefined();
    const bash = (command: string) => guardToolCall(call("bash", { command }), facts);
    // output redirects, tee and dd write the seed: the write rule reports them, in attached and detached forms
    for (const command of [`echo x > ${KEY}`, `echo x >> ${KEY}`, `echo x &> ${KEY}`, `echo x >& ${KEY}`, `echo x>${KEY}`,
      `echo x>>${KEY}`, `tee ${KEY}`, `tee -a ${KEY}`, `dd if=/dev/zero of=${KEY}`, `dd of=${KEY} if=/dev/zero`]) {
      expect(bash(command)).toEqual(blockedMutate);
    }
    expect(bash("echo x > /dev/null 2>&1")).toBeUndefined();
    // comparison tools print the bytes they read, including the attached file-valued option forms
    for (const command of [`diff /dev/null ${KEY}`, `cmp ${KEY} /dev/null`, `diff --from-file=${KEY} /dev/null`,
      `diff --to-file=${KEY} /dev/null`]) {
      expect(bash(command)).toEqual(blockedShell);
    }
    // the spellings the review named: a home-relative attached redirect, and a quoted $HOME --from-file= operand
    expect(bash("echo x>~/.config/agent-swarm/signing-key.hex")).toEqual(blockedMutate);
    expect(bash('diff --from-file="$HOME/.config/agent-swarm/signing-key.hex" /dev/null')).toEqual(blockedShell);
    expect(bash('tee "$HOME/.config/agent-swarm/signing-key.hex"')).toEqual(blockedMutate);
  });

  test("bounded deny-only metadata normalizes absolute paths without replacing ordinary private sources", () => {
    expect(parseSignerDenyPaths(undefined)).toEqual([]);
    expect(parseSignerDenyPaths("[]")).toEqual([]);
    expect(parseSignerDenyPaths(JSON.stringify([`${CONFIGURED}/../signer.hex`, CONFIGURED]))).toEqual([CONFIGURED]);
    expect(parseSignerDenyPaths(JSON.stringify(Array(32).fill(CONFIGURED)))).toEqual([CONFIGURED]);
    const inheritedFacts = {
      ...facts, signerReadPaths: parseSignerDenyPaths(JSON.stringify([CONFIGURED])),
      env: { SWARM_ED25519_KEY_FILE: "/different-private/custom.source", XDG_CONFIG_HOME: "/nonexistent-xdg" },
    };
    for (const path of [CONFIGURED, KEY, "/different-private/custom.source", "/nonexistent-xdg/agent-swarm/signing-key.hex"]) {
      expect(guardToolCall(call("read", { path }), inheritedFacts)).toEqual(blockedRead);
      expect(guardToolCall(call("bash", { command: `cat ${path}` }), inheritedFacts)).toEqual(blockedShell);
    }
  });

  test.each([
    "", "not-json", "null", "{}", JSON.stringify(CONFIGURED), JSON.stringify([null]),
    JSON.stringify([1]), JSON.stringify(["relative/private.source"]), JSON.stringify(["/private\0source"]),
    JSON.stringify(Array(33).fill(CONFIGURED)), JSON.stringify([`/${"x".repeat(4096)}`]), " ".repeat(131073),
  ])("malformed or excessive deny metadata fails closed without echoing its value: %p", (value) => {
    expect(() => parseSignerDenyPaths(value)).toThrow("invalid signer deny-path policy");
    process.env.SWARM_SIGNER_DENY_PATHS = value;
    const { run } = guardHandler();
    expect(run(call("read", { path: "src/guard.ts" }), agentCtx(CWD, "a05-backend", [], false))).toEqual({
      block: true, reason: `${GUARD_ERROR_PREFIX}invalid signer deny-path policy)`,
    });
    expect(run(call("read", { path: "src/guard.ts" }), fakeCtx(CWD))).toBeUndefined();
  });

  test.each([
    KEY, `${KEY}:1-4`, `${KEY}:raw`, `${KEY}:img`,
    "~/.config/agent-swarm/signing-key.hex", "$HOME/.config/agent-swarm/signing-key.hex",
    "${HOME}/.config/agent-swarm/signing-key.hex",
    `${HOME}/.config/agent-swarm/../agent-swarm/signing-key.hex`,
    `../${HOME.slice(1)}/.config/agent-swarm/signing-key.hex`,
    `file://${KEY}`, `${HOME}/.config/agent-swarm/signing-*.hex`,
  ])("read and grep refuse %s without exposing contents", (path) => {
    for (const tool of ["read", "grep"]) {
      expect(guardToolCall(call(tool, { path, pattern: "." }), facts)).toEqual(blockedRead);
      expect(guardToolCall(call(tool, { file_path: path, pattern: "." }), facts)).toEqual(blockedRead);
    }
  });

  test.each([
    { SWARM_ED25519_KEY_FILE: CONFIGURED },
    { SWARM_ED25519_KEY_FILE: "../nonexistent-private/signer.hex" },
    { XDG_CONFIG_HOME: "/nonexistent-private", SWARM_ED25519_KEY_FILE: CONFIGURED },
  ])("configured references remain private with env %p", (env) => {
    const configuredFacts = { ...facts, env };
    for (const path of [CONFIGURED, "$SWARM_ED25519_KEY_FILE", "${SWARM_ED25519_KEY_FILE}"]) {
      expect(guardToolCall(call("read", { path }), configuredFacts)).toEqual(blockedRead);
      expect(guardToolCall(call("grep", { path, pattern: "." }), configuredFacts)).toEqual(blockedRead);
    }
    expect(guardToolCall(call("read", { path: KEY }), configuredFacts)).toEqual(blockedRead);
  });

  test.each(["secrets/key.hex", "~/literal-key.hex", " private-key.hex", "$literal-key.hex"])(
    "literal configured Path references remain protected: %s", (reference) => {
      const configuredFacts = { ...facts, env: { SWARM_ED25519_KEY_FILE: reference } };
      expect(guardToolCall(call("read", { path: reference }), configuredFacts)).toEqual(blockedRead);
      expect(guardToolCall(call("grep", { path: reference, pattern: "." }), configuredFacts)).toEqual(blockedRead);
      expect(guardToolCall(call("bash", { command: `cat '${reference}'` }), configuredFacts)).toEqual(blockedShell);
    },
  );

  test("XDG source and recursive grep roots are protected, directory discovery is not a content read", () => {
    const xdgFacts = { ...facts, env: { XDG_CONFIG_HOME: "/nonexistent-private" } };
    for (const path of ["/nonexistent-private/agent-swarm/signing-key.hex", "$XDG_CONFIG_HOME/agent-swarm/signing-key.hex"]) {
      expect(guardToolCall(call("read", { path }), xdgFacts)).toEqual(blockedRead);
    }
    for (const path of [HOME, `${HOME}/.config`, `${HOME}/**/signing-key.hex`, "/", `src;${HOME}/.config`]) {
      expect(guardToolCall(call("grep", { path, pattern: "." }), facts)).toEqual(blockedRead);
    }
    expect(guardToolCall(call("grep", { pattern: "." }), { ...facts, cwd: HOME })).toEqual(blockedRead);
    expect(guardToolCall(call("grep", { path: "*.hex", pattern: "." }), { ...facts, cwd: HOME })).toEqual(blockedRead);
    expect(guardToolCall(call("grep", { path: "*.ts", pattern: "." }), { ...facts, cwd: HOME })).toBeUndefined();
    for (const path of [HOME, `${HOME}/.config`, `${HOME}/.config/agent-swarm`]) {
      expect(guardToolCall(call("read", { path }), facts)).toBeUndefined();
      expect(guardToolCall(call("glob", { path: `${path}/*` }), facts)).toBeUndefined();
    }
  });

  test.each([
    `cat ${KEY}`, `head -c 1 ${KEY}`, `tail -n 1 ${KEY}`, `xxd ${KEY}`, `od -An -tx1 ${KEY}`,
    `strings ${KEY}`, `base64 ${KEY}`, `wc -c ${KEY}`,
    `sed -n '1p' ${KEY}`, `awk '{print}' ${KEY}`,
    `grep -n . ${KEY}`, `grep -e . ${KEY}`, `grep -f ${KEY} public.txt`,
    `grep -ne. ${KEY}`, `grep -nf${KEY} public.txt`, `sed -nf${KEY} public.txt`,
    `sort --files0-from=${KEY}`, `openssl base64 -in ${KEY}`, `jq -R . ${KEY}`,
    `git diff --no-index /dev/null ${KEY}`,
    `rg . ${KEY}`, `rg --file=${KEY} public.txt`, `grep -R . ${HOME}/.config`,
    `rg . ${HOME}/.config`, `cat ${HOME}/.config/agent-swarm/signing-*.hex`,
    `cat ${HOME}/.config/agent-swarm/{public.txt,signing-key.hex}`,
    `dd if=${KEY} of=/tmp/public-copy`, `cp ${KEY} /tmp/public-copy`,
    `tar -czf /tmp/copy.tgz ${KEY}`, `rsync -a ${HOME}/.config/agent-swarm/ /tmp/public-copy/`,
    `cp -r ${HOME}/.config /tmp/public-copy`, `tar -czf /tmp/copy.tgz ${HOME}/.config`,
    `zip -r /tmp/copy.zip ${HOME}/.config`,
    `cat < ${KEY}`, `cat 3<${KEY} /dev/fd/3`, `read -r value < ${KEY}`,
    `cat <> ${KEY}`, `printf '%s' "$(cat ${KEY})"`,
    `sh -c 'cat ${KEY}'`, `/usr/bin/env cat ${KEY}`, `busybox cat ${KEY}`,
    `cd ${HOME}/.config/agent-swarm && cat signing-key.hex`,
    `env -C ${HOME}/.config/agent-swarm cat signing-key.hex`,
    'cat "$HOME/.config/agent-swarm/signing-key.hex"',
    `HOME=/unrelated cat ${KEY}`,
    'cd "$DIR" && cat signing-key.hex',
  ])("recognized shell reader refuses %s", (command) => {
    expect(guardToolCall(call("bash", { command }), facts)).toEqual(blockedShell);
    expect(guardToolCall(call("bash", { command }), { ...facts, agent: undefined, topLevel: true })).toBeUndefined();
  });

  test("configured shell references and bash cwd/env cannot unprotect the original source", () => {
    const configuredFacts = { ...facts, env: { SWARM_ED25519_KEY_FILE: CONFIGURED } };
    for (const command of [`cat ${CONFIGURED}`, 'cat "$SWARM_ED25519_KEY_FILE"', 'cat "${SWARM_ED25519_KEY_FILE}"']) {
      expect(guardToolCall(call("bash", { command }), configuredFacts)).toEqual(blockedShell);
    }
    expect(guardToolCall(call("bash", { command: "cat signing-key.hex", cwd: `${HOME}/.config/agent-swarm` }), facts)).toEqual(blockedShell);
    expect(guardToolCall(call("bash", { command: `cat ${KEY}`, env: { HOME: "/unrelated" } }), facts)).toEqual(blockedShell);
    expect(guardToolCall(call("bash", {
      command: 'cat "$SWARM_ED25519_KEY_FILE"', env: { SWARM_ED25519_KEY_FILE: KEY },
    }), facts)).toEqual(blockedShell);
    expect(guardToolCall(call("bash", {
      command: 'cat "$XDG_CONFIG_HOME/agent-swarm/signing-key.hex"', env: { XDG_CONFIG_HOME: `${HOME}/.config` },
    }), facts)).toEqual(blockedShell);
  });

  test.each([
    "src/guard.ts", "tests/fixtures/signing-key.hex", `${CWD}/.config/agent-swarm/signing-key.hex`,
    `${KEY}.public`, `${HOME}/.config/agent-swarmish/signing-key.hex`, `${HOME}-other/.config/agent-swarm/signing-key.hex`,
  ])("unrelated source and misleading paths remain readable: %s", (path) => {
    for (const tool of ["read", "grep"]) expect(guardToolCall(call(tool, { path, pattern: "." }), facts)).toBeUndefined();
    expect(guardToolCall(call("bash", { command: `cat ${path}` }), facts)).toBeUndefined();
  });

  test.each([
    `stat ${KEY}`, `ls -l ${KEY}`, `[ -f ${KEY} ] && echo exists`, `printf '%s' ${KEY}`,
    `rg --files ${HOME}/.config`, `grep '${KEY}' src/guard.ts`, `sed 's|${KEY}|private|' src/guard.ts`,
    `cat <<'EOF'\n${KEY}\nEOF`, `cat <<< '${KEY}'`, `cat '${KEY}:1-4'`,
    "cat scripts/orch_plan.py", "grep -n signer swarm/envelope.py", "cat tests/fixtures/signing-key.hex",
  ])("metadata, path-as-data and public-source shell reads remain available: %s", (command) => {
    expect(guardToolCall(call("bash", { command }), facts)).toBeUndefined();
  });

  test("every swarm identity is protected; unsigned legacy planning and canonical bridge calls stay untouched", () => {
    for (const agent of SWARM_SLUGS) expect(guardToolCall(call("read", { path: KEY }), { ...facts, agent })).toEqual(blockedRead);
    expect(guardToolCall(call("read", { path: KEY }), { ...facts, agent: undefined, topLevel: true, env: { SWARM_TASK_ID: "T" } })).toEqual(blockedRead);
    expect(guardToolCall(call("read", { path: KEY }), { ...facts, agent: undefined, topLevel: true })).toBeUndefined();
    expect(guardToolCall(call("swarm_plan", { description: "plan" }), { ...facts, agent: A01 })).toBeUndefined();
    expect(guardToolCall(call("read", { path: KEY }), { ...facts, agent: A01, hasTask: false })?.reason).toStartWith(DEPTH_PREFIX);
  });

  test("registered consumer handler protects the configured source without bridge or filesystem reads", () => {
    process.env.SWARM_ED25519_KEY_FILE = CONFIGURED;
    const { run } = guardHandler({ activeTools: ["task"] });
    const ctx = agentCtx(CWD, "a05-backend", [], false);
    expect(run(call("read", { path: `${CONFIGURED}:raw` }), ctx)).toEqual(blockedRead);
    expect(run(call("grep", { path: CONFIGURED, pattern: "." }), ctx)).toEqual(blockedRead);
    expect(run(call("bash", { command: `head -c 1 ${CONFIGURED}` }), ctx)).toEqual(blockedShell);
    expect(run(call("read", { path: "src/guard.ts" }), ctx)).toBeUndefined();
  });

  test("registered headless consumer protects deny-only custom sources after loadable signer input is stripped", () => {
    const source = "/nonexistent-controller-private/custom-signer.source";
    process.env.SWARM_AGENT = "a05-backend";
    process.env.SWARM_SIGNER_DENY_PATHS = JSON.stringify([
      "/nonexistent-controller-private/intermediate/../custom-signer.source",
    ]);
    process.env.XDG_CONFIG_HOME = "/nonexistent-controller-xdg";
    expect(process.env.SWARM_ED25519_KEY_FILE).toBeUndefined();
    const { run } = guardHandler({ activeTools: ["task"] });
    const ctx = fakeCtx(CWD);
    for (const path of [source, `${source}:raw`, `file://${source}`, "~/.config/agent-swarm/signing-key.hex",
      "/nonexistent-controller-xdg/agent-swarm/signing-key.hex"]) {
      expect(run(call("read", { path }), ctx)).toEqual(blockedRead);
      expect(run(call("grep", { path, pattern: "." }), ctx)).toEqual(blockedRead);
    }
    expect(run(call("grep", { path: "/nonexistent-controller-private", pattern: "." }), ctx)).toEqual(blockedRead);
    for (const command of [
      `cat ${source}`, `cat < ${source}`, `read -r value < ${source}`,
      `cp ${source} /tmp/public-copy`, `dd if=${source} of=/tmp/public-copy`,
      `grep -n . ${source}`, `cd /nonexistent-controller-private && cat custom-signer.source`,
      `SWARM_SIGNER_DENY_PATHS='[]' HOME=/unrelated cat ${source}`,
    ]) {
      expect(run(call("bash", { command }), ctx)).toEqual(blockedShell);
      expect(run(call("bash", {
        command, cwd: "/unrelated",
        env: { HOME: "/unrelated", XDG_CONFIG_HOME: "/unrelated", SWARM_ED25519_KEY_FILE: "/unrelated/public",
          SWARM_SIGNER_DENY_PATHS: "[]" },
      }), ctx)).toEqual(blockedShell);
    }
    expect(run(call("bash", {
      command: "cat custom-signer.source", cwd: "/nonexistent-controller-private",
      env: { SWARM_SIGNER_DENY_PATHS: "[]" },
    }), ctx)).toEqual(blockedShell);
    // Captured policy is additive: a tool override may name another source, never erase the controller's.
    expect(run(call("bash", {
      command: 'cat "$SWARM_ED25519_KEY_FILE"', env: { SWARM_ED25519_KEY_FILE: "/nonexistent-tool-private/other.source" },
    }), ctx)).toEqual(blockedShell);
    for (const command of [
      `stat ${source}`, `ls -l ${source}`, `[ -f ${source} ] && echo exists`,
      `printf '%s' ${source}`, `grep '${source}' src/guard.ts`, "cat src/guard.ts",
      "cat tests/fixtures/signing-key.hex",
    ]) expect(run(call("bash", { command }), ctx)).toBeUndefined();
    expect(run(call("read", { path: "/nonexistent-controller-private" }), ctx)).toBeUndefined();
    expect(run(call("glob", { path: "/nonexistent-controller-private/*" }), ctx)).toBeUndefined();
    expect(run(call("read", { path: "src/guard.ts" }), ctx)).toBeUndefined();
    expect(run(call("grep", { path: "src", pattern: "signer" }), ctx)).toBeUndefined();
    expect(process.env.SWARM_ED25519_KEY_FILE).toBeUndefined();
  });

  test.each([
    `echo x > ${KEY}`, `echo x >> ${KEY}`, `tee ${KEY}`, `dd if=/dev/zero of=${KEY}`, `truncate -s0 ${KEY}`,
    `chmod 600 ${KEY}`, `ln -sf /tmp/x ${KEY}`, `rm -f ${KEY}`, `rm -rf ${HOME}/.config/agent-swarm`,
    `curl -o ${KEY} https://example.invalid/key`, `cp /tmp/x ${HOME}/.config/agent-swarm/`,
    `install -m 600 /tmp/x ${HOME}/.config/agent-swarm/`, `cd ${HOME}/.config/agent-swarm && echo x > signing-key.hex`,
  ])("shell writes, copies and deletes of signer material are refused: %s", (command) => {
    expect(guardToolCall(call("bash", { command }), facts)).toEqual(blockedMutate);
    expect(guardToolCall(call("bash", { command: `cp /tmp/x ${HOME}/.config/agent-swarmish/` }), facts)).toBeUndefined();
    expect(guardToolCall(call("bash", { command: `echo x > ${CWD}/out.txt` }), facts)).toBeUndefined();
  });

  test.each([KEY, "~/.config/agent-swarm/signing-key.hex"])("write and edit of signer material are refused: %s", (path) => {
    expect(guardToolCall(call("write", { path, content: "x" }), facts)).toEqual(blockedWrite);
    expect(guardToolCall(call("edit", { path, input: "PUT 1.=1:\n+x" }), facts)).toEqual(blockedWrite);
    expect(guardToolCall(call("edit", { input: `[${KEY}]\nPUT 1.=1:\n+x` }), facts)).toEqual(blockedWrite);
    expect(guardToolCall(call("write", { path: "src/guard.ts", content: "x" }), facts)).toBeUndefined();
  });

  test.each([
    `diff /dev/null ${KEY}`, `diff ${KEY} /dev/null`, `diff3 /dev/null ${KEY} /dev/null`, `cmp ${KEY} /dev/null`,
    `cmp -l /dev/null ${KEY}`, `comm -12 ${KEY} /dev/null`, `sdiff ${KEY} /dev/null`, `colordiff /dev/null ${KEY}`,
    `diff --from-file=${KEY} /dev/null`, `diff --to-file ${KEY} /dev/null`, `git diff --no-index /dev/null ${KEY}`,
  ])("comparison tools that print signer operands are refused: %s", (command) => {
    expect(guardToolCall(call("bash", { command }), facts)).toEqual(blockedShell);
    expect(guardToolCall(call("bash", { command: "diff src/guard.ts /dev/null" }), facts)).toBeUndefined();
  });
});
