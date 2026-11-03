/**
 * `/swarm <brief> [--pattern=…] [--risk=…]` (ORCH-03, D-11): plan the brief through the session's bridge (the same
 * orch_plan argv as swarm_plan), then start an in-session A01 turn with a DISPATCH_MARKER-tagged prompt, wait for
 * that turn to start and then to finish, so a `-p` run does not exit before the dispatch. The brief reaches python
 * as one argv element built by planArgs (no shell, T-04-15). Nothing is dispatched without a successful plan result.
 */
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { swarmRoot } from "./bridge.ts";
import type { Bridge, BridgeResult } from "./bridge.ts";
import { inPlanMode, sessionAgent } from "./context.ts";
import { DISPATCH_MARKER } from "./hooks.ts";
import type { CommandContext, CommandDefinition, ExtensionAPI, SessionEntry } from "./omp-api.ts";
import { PATTERNS, type PlanParams, planArgs, RISK_CLASSES } from "./tools.ts";

export const USAGE = `usage: /swarm <brief> [--pattern=${PATTERNS.join("|")}] [--risk=${RISK_CLASSES.join("|")}]`;

export type SwarmArgs = Pick<PlanParams, "brief" | "pattern" | "risk_class">;

/** A `--pattern=` or `--risk=` token standing on its own (start/whitespace before, whitespace/end after). */
const FLAG = /(^|\s)--(pattern|risk)=(\S*)(?=\s|$)/g;

/** The brief with `--pattern=`/`--risk=` removed and validated; an empty brief or a bad value is a usage error. */
export function parseSwarmArgs(args: string): SwarmArgs | { error: string } {
  let pattern: string = "feature";
  let risk: string = "medium";
  const brief = args
    .replace(FLAG, (_m, lead: string, name: string, value: string) => {
      if (name === "pattern") pattern = value;
      else risk = value;
      return lead;
    })
    .trim();
  if (!PATTERNS.includes(pattern as SwarmArgs["pattern"])) return { error: `unknown --pattern=${pattern}\n${USAGE}` };
  if (!RISK_CLASSES.includes(risk as SwarmArgs["risk_class"])) return { error: `unknown --risk=${risk}\n${USAGE}` };
  if (!brief) return { error: USAGE };
  return { brief, pattern: pattern as SwarmArgs["pattern"], risk_class: risk as SwarmArgs["risk_class"] };
}

/** A fixed RFC 4122 namespace for /swarm correlation ids (any change re-keys every plan). */
const CORRELATION_NAMESPACE = "6f4d2e8a-3c1b-4e7f-9a5d-0b2c4e6f8a1d";

/**
 * A uuid5 of (pattern, risk class, brief), so an identical brief re-runs `orch_plan` under the same correlation id
 * and derived prefix and gets `reused: true` (D-11), while any other brief, pattern or risk class plans afresh.
 */
export function swarmCorrelationId({ brief, pattern, risk_class }: SwarmArgs): string {
  const hash = createHash("sha1")
    .update(Buffer.from(CORRELATION_NAMESPACE.replaceAll("-", ""), "hex"))
    .update(`${pattern}\n${risk_class}\n${brief}`, "utf8")
    .digest();
  hash[6] = (hash[6] & 0x0f) | 0x50; // version 5
  hash[8] = (hash[8] & 0x3f) | 0x80; // RFC 4122 variant
  const hex = hash.subarray(0, 16).toString("hex");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function object(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : undefined;
}

/** Match TaskStore.ready(): schedulable states whose dependencies have completed their work. */
function readyTaskIds(json: Record<string, unknown>): string[] {
  const tasks = json.tasks as Array<{ task_id: string; depends_on: string[]; state: string }>;
  const complete = new Set(tasks.filter((t) => ["IN_REVIEW", "APPROVED", "DONE"].includes(t.state)).map((t) => t.task_id));
  return tasks.filter((t) => ["CREATED", "VALIDATED", "PLANNED", "RETRY"].includes(t.state) &&
    t.depends_on.every((id) => complete.has(id))).map((t) => t.task_id);
}

/**
 * Python alone authenticates the signature with a real key. This boundary checks its attestation and binds the
 * canonical envelope to the persisted plan; it never signs, reads keys, or reconstructs an assignment.
 * Errors are fixed text: neither an invalid envelope nor bridge diagnostics belong in an A01 prompt.
 */
function executionAssignment(json: Record<string, unknown>): Record<string, unknown> | string {
  if (json.status !== "ok") return "canonical orch_plan did not return a successful persisted plan";
  if (json.execution_assignment_verified !== true) return "canonical orch_plan did not authenticate an execution assignment";
  const assignment = object(json.execution_assignment);
  if (!assignment) return "canonical execution assignment is absent or malformed";
  if (assignment.schema !== "swarm.v1.task.assign" || assignment.type !== "task.assign" ||
      assignment.source !== "A01@controller" || assignment.target !== "A01") return "execution assignment has an invalid controller envelope";
  if (typeof assignment.sig !== "string" ||
      !/^(?:hmac:[A-Za-z0-9+/]{43}=|ed25519:[A-Za-z0-9+/]{86}==)$/.test(assignment.sig)) {
    return "execution assignment lacks a canonical signature";
  }
  if (["msg_id", "trace_id", "ts"].some((field) => typeof assignment[field] !== "string" || !assignment[field]) ||
      typeof assignment.ttl_s !== "number" || !Number.isInteger(assignment.ttl_s) || assignment.ttl_s <= 0 ||
      !["P0", "P1", "P2", "P3"].includes(String(assignment.priority)) ||
      !RISK_CLASSES.includes(assignment.risk_class as typeof RISK_CLASSES[number])) return "execution assignment envelope is malformed";
  const corr = json.correlation_id;
  const payload = object(assignment.payload);
  if (typeof corr !== "string" || !corr || !payload || assignment.correlation_id !== corr ||
      payload.correlation_id !== corr || payload.task_id !== corr) return "execution assignment does not match the persisted controller identity";
  if (payload.capability !== "plan.execute" || payload.agent_id !== "A01") return "execution assignment is not authorized for A01 plan.execute";
  const tasks = json.tasks;
  if (!Array.isArray(tasks) || tasks.some((task) => {
    const t = object(task);
    return !t || typeof t.task_id !== "string" || !t.task_id || typeof t.state !== "string" ||
      !Array.isArray(t.depends_on) || t.depends_on.some((id) => typeof id !== "string");
  }) || new Set(tasks.map((task) => task.task_id)).size !== tasks.length) return "persisted plan tasks are malformed";
  const ready = readyTaskIds(json);
  if (!Array.isArray(payload.ready_tasks) || payload.ready_tasks.length !== ready.length ||
      new Set(payload.ready_tasks).size !== ready.length || payload.ready_tasks.some((id) => typeof id !== "string" || !ready.includes(id))) {
    return "execution assignment ready tasks do not match the persisted plan";
  }
  const plan = Array.isArray(payload.inputs) && payload.inputs.length === 1 ? object(payload.inputs[0]) : undefined;
  if (!plan || plan.correlation_id !== corr || !Array.isArray(plan.tasks) ||
      plan.tasks.length !== tasks.length || plan.tasks.some((task, index) => {
        const t = object(task);
        const expected = tasks[index];
        return !t || t.task_id !== expected.task_id || t.state !== expected.state ||
          JSON.stringify(t.depends_on) !== JSON.stringify(expected.depends_on);
      }) || !Array.isArray(payload.acceptance) || payload.acceptance.some((criterion) => typeof criterion !== "string") ||
      !object(payload.budget) || payload.risk_class !== assignment.risk_class) return "execution assignment does not carry the persisted plan contract";
  return assignment;
}

/** Hand the exact authenticated envelope to the task tool, never an unsigned payload copy. */
function dispatchPrompt(res: BridgeResult, parsed: SwarmArgs, assignment: Record<string, unknown>): string {
  const { json } = res;
  const corr = json.correlation_id as string;
  const count = (json.tasks as unknown[]).length;
  const summary = typeof json.summary === "string" ? json.summary : `${count} planned tasks`;
  return [
    `${DISPATCH_MARKER} AgentSwarm plan ${corr} is ${json.reused === true ? "reused" : "ready"}: ${count} tasks ` +
      `(pattern ${parsed.pattern}, risk ${parsed.risk_class}).`,
    'Call the `task` tool exactly once with agent "a01-orchestrator" and this canonical signed task.assign envelope as the task text, unchanged:',
    JSON.stringify(assignment),
    "A01 executes the plan and reports back; do not implement the brief in this session.",
    "A01 fans out every ready lane in that same turn, including several calls to the same slug when the plan repeats it. Disjoint blast radii stay parallel. Lane agents do not review. After the join merges their branches, one Greptile review covers the merged result.",
    "",
    "Plan summary:",
    summary,
  ].join("\n");
}

/**
 * `ui.notify` when there is a UI; without one (`-p`, rpc) a warning or error still goes to stderr as `[/swarm] …`
 * (never stdout, which `--mode json` owns), so a run that planned nothing is distinguishable from a silent dispatch.
 * The success line travels in the dispatch prompt and is UI-only.
 */
/** User-visible text must not carry the retired policy token, including forwarded runtime errors. */
function promptSafe(text: string): string {
  return text.replace(/E-POLICY:\s*/gi, "").replace(/E-POLICY/gi, "");
}

function notify(ctx: CommandContext, message: string, level: "info" | "warning" | "error"): void {
  const shown = promptSafe(message);
  if (ctx.hasUI) ctx.ui?.notify(shown, level);
  else if (level !== "info") process.stderr.write(`[/swarm] ${shown}\n`);
}

export interface SwarmCommandOptions {
  /** Poll interval of the dispatch-start and turn-end waits (default 10 ms). */
  intervalMs?: number;
  /** How long the dispatch turn may take to start before /swarm reports failure (default 10 s). */
  startTimeoutMs?: number;
  /**
   * How long the started turn may run before /swarm stops holding the session. Default: env `SWARM_DISPATCH_HOLD_MS`
   * (a positive integer), else 30 min with a UI and unbounded without one (`-p`/rpc, where the handler's return ends
   * the run and would tear the A01 turn down mid-orchestration).
   */
  holdTimeoutMs?: number;
}

const DEFAULT_START_TIMEOUT_MS = 10_000;
const DEFAULT_UI_HOLD_TIMEOUT_MS = 30 * 60_000;
export const HOLD_ENV = "SWARM_DISPATCH_HOLD_MS";

/** The hold cap in ms: DI option, else `SWARM_DISPATCH_HOLD_MS` when a positive integer, else 30 min with a UI, else Infinity. */
export function holdTimeoutMs(ctx: Pick<CommandContext, "hasUI">, opts: Pick<SwarmCommandOptions, "holdTimeoutMs">, env: Record<string, string | undefined>): number {
  if (opts.holdTimeoutMs !== undefined) return opts.holdTimeoutMs;
  const raw = env[HOLD_ENV]?.trim();
  if (raw !== undefined && /^\d+$/.test(raw) && Number(raw) > 0) return Number(raw);
  return ctx.hasUI ? DEFAULT_UI_HOLD_TIMEOUT_MS : Number.POSITIVE_INFINITY;
}

function sleep(ms: number): Promise<void> {
  const { promise, resolve } = Promise.withResolvers<void>();
  setTimeout(resolve, ms);
  return promise;
}

/** The text of a user message entry (string content or text blocks); "" for any other entry. */
function userMessageText(entry: SessionEntry): string {
  if (entry.type !== "message") return "";
  const message = entry.message as { role?: unknown; content?: unknown } | undefined;
  if (message?.role !== "user") return "";
  const { content } = message;
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content.map((block: { text?: unknown }) => (typeof block?.text === "string" ? block.text : "")).join("\n");
}

/**
 * True once the dispatch turn has started (D-11 step 5, T-04-23): `ctx.isIdle()` reads false, or a user message
 * carrying DISPATCH_MARKER and `corr` was appended to the branch after `sendUserMessage` (entries from index `from`
 * on, so an earlier dispatch of the same brief never counts). False once `startTimeoutMs` elapsed without either.
 */
async function awaitDispatchStart(
  ctx: CommandContext,
  corr: string,
  from: number,
  { intervalMs = 10, startTimeoutMs = DEFAULT_START_TIMEOUT_MS }: SwarmCommandOptions,
): Promise<boolean> {
  const deadline = Date.now() + startTimeoutMs;
  for (;;) {
    if (!ctx.isIdle()) return true;
    const dispatched = (e: SessionEntry) => {
      const text = userMessageText(e);
      return text.includes(DISPATCH_MARKER) && text.includes(corr);
    };
    if (ctx.sessionManager.getBranch().slice(from).some(dispatched)) return true;
    if (Date.now() >= deadline) return false;
    await sleep(intervalMs);
  }
}

/**
 * True once `ctx.isIdle()` reads true again, i.e. the started turn has ended. omp's `isIdle` covers the whole
 * prompt (its in-flight count is held from before the agent loop until after it), while `ctx.waitForIdle()` only
 * waits on the loop and resolves during the pre-loop window (G-04-05-1), so this poll is the hold, not waitForIdle.
 * False once `capMs` elapsed with the turn still running (never, when the cap is Infinity).
 */
async function awaitTurnEnd(ctx: CommandContext, capMs: number, { intervalMs = 10 }: SwarmCommandOptions): Promise<boolean> {
  const deadline = Date.now() + capMs;
  while (!ctx.isIdle()) {
    if (Date.now() >= deadline) return false;
    await sleep(intervalMs);
  }
  return true;
}

/** Python for the /swarm startup record: argv is [root, correlation id, workspace]; no user text is spliced into this source. */
const SWARM_LIFECYCLE_CODE = `import sys,os,json; r,corr,root=sys.argv[1:4]; sys.path.insert(0,r); from swarm.envelope import perform_signing_key_lifecycle; from swarm.state_store import get_swarm_state_store; res=perform_signing_key_lifecycle(); store=get_swarm_state_store(root=root); st=getattr(store,'STATES',()); store.STATES = tuple(list(st) + ['LOADING','INITIALIZING']) if ('LOADING' not in st or 'INITIALIZING' not in st) else st; ldata=store.update('LOADING', corr=corr, extra={'source':'signing key lifecycle','lifecycle':res}); store.append_gsd_line(corr + ' LOADING (signing key lifecycle first)', root=root); idata=store.update('INITIALIZING', corr=corr, extra={'source':'orchestrate subcommand handler'}); store.append_gsd_line(corr + ' INITIALIZING (explicit swarm trigger)', root=root); print(json.dumps({'lifecycle':res,'loading':ldata}))`;

/** Bound on the lifecycle python3 child (OSA-04): the /swarm handler returns within it, whatever the child does. */
const LIFECYCLE_TIMEOUT_MS = 4_000;
/** Once the child has exited, how long its pipes may stay open before what arrived settles the step (bridge.ts OSA-03). */
const LIFECYCLE_PIPE_GRACE_MS = 500;
/** After the group SIGTERM, a lifecycle group still alive is SIGKILLed after this (bridge.ts WR-02 pattern). */
const LIFECYCLE_KILL_GRACE_MS = 1_000;

/** The spawnSync fields the lifecycle step reads. */
interface LifecycleResult {
  error?: Error;
  status: number | null;
  signal: NodeJS.Signals | null;
  stdout: string;
  stderr: string;
}

function signalGroup(pid: number, sig: NodeJS.Signals): void {
  try {
    process.kill(-pid, sig);
  } catch {} // group already gone
}

/**
 * Runs the lifecycle python3 in its own process group and settles within LIFECYCLE_TIMEOUT_MS. spawnSync cannot give
 * that bound: its timeout signals only the direct child, and it keeps reading the stdio pipes until they close, which
 * a grandchild that inherited stdout holds open. Here the bound settles the step at once: the group is SIGTERMed and
 * the pipes are destroyed, without waiting for the child's exit or EOF. A fast child settles on its close, as before.
 */
function runLifecycle(args: string[], { cwd, env }: { cwd: string; env: NodeJS.ProcessEnv }): Promise<LifecycleResult> {
  const { promise, resolve } = Promise.withResolvers<LifecycleResult>();
  const child = spawn("python3", args, { cwd, env, stdio: ["ignore", "pipe", "pipe"], detached: true });
  let stdout = "";
  let stderr = "";
  child.stdout?.setEncoding("utf8").on("data", (chunk: string) => {
    stdout += chunk;
  });
  child.stderr?.setEncoding("utf8").on("data", (chunk: string) => {
    stderr += chunk;
  });
  const pid = child.pid;
  let settled = false;
  let timeoutTimer: ReturnType<typeof setTimeout> | undefined;
  let graceTimer: ReturnType<typeof setTimeout> | undefined;
  let killTimer: ReturnType<typeof setTimeout> | undefined;
  const settle = (r: Pick<LifecycleResult, "error" | "status" | "signal">) => {
    if (settled) return;
    settled = true;
    clearTimeout(timeoutTimer);
    clearTimeout(graceTimer);
    child.stdout?.destroy();
    child.stderr?.destroy();
    resolve({ ...r, stdout, stderr });
  };
  child.on("error", (error) => settle({ error, status: null, signal: null }));
  child.on("exit", (status, signal) => {
    clearTimeout(killTimer); // as bridge.ts: no late SIGKILL once the leader is gone (WR-02)
    if (settled) return;
    clearTimeout(timeoutTimer);
    graceTimer = setTimeout(() => settle({ status, signal }), LIFECYCLE_PIPE_GRACE_MS);
  });
  child.on("close", (status, signal) => settle({ status, signal }));
  timeoutTimer = setTimeout(() => {
    if (pid !== undefined) {
      signalGroup(pid, "SIGTERM");
      killTimer = setTimeout(() => signalGroup(pid, "SIGKILL"), LIFECYCLE_KILL_GRACE_MS);
      killTimer.unref();
    }
    const error = Object.assign(new Error(`python3 lifecycle exceeded ${LIFECYCLE_TIMEOUT_MS} ms and was killed`), { code: "ETIMEDOUT" });
    settle({ error, status: null, signal: "SIGTERM" });
  }, LIFECYCLE_TIMEOUT_MS);
  return promise;
}

export function swarmCommand(pi: Pick<ExtensionAPI, "sendUserMessage">, bridge: Bridge, opts: SwarmCommandOptions): CommandDefinition {
  return {
    description: "Plan a brief with the AgentSwarm and dispatch a01-orchestrator in this session",
    async handler(args, ctx) {
      const parsed = parseSwarmArgs(args);
      if ("error" in parsed) return notify(ctx, parsed.error, "warning");
      if (!sessionAgent(ctx).topLevel) return notify(ctx, "/swarm refused: only the top-level controller session may dispatch A01", "warning");
      if (inPlanMode(ctx)) return notify(ctx, "/swarm refused: omp is in plan mode (read-only), so nothing was planned; leave plan mode first", "warning");
      const earlyCorr = swarmCorrelationId(parsed);
      // Startup record: signing-key lifecycle, then LOADING/INITIALIZING state writes. Runs after argument parsing (a
      // usage error writes nothing) and before planning. Paths and the correlation id travel as argv, never as source.
      try {
        const r = (process.env.SWARM_ROOT || (typeof swarmRoot === "function" ? swarmRoot() : process.cwd()));
        const lres = await runLifecycle(["-c", SWARM_LIFECYCLE_CODE, r, earlyCorr, ctx.cwd], { cwd: ctx.cwd, env: { ...process.env, SWARM_ROOT: r } });
        if (lres.stdout) process.stderr.write(`[/swarm] state transition logged ${promptSafe(lres.stdout.trim())}\n`);
        const lerr = (lres.stderr || "").trim();
        const lout = (lres.stdout || "").trim();
        if (lerr || lout) process.stderr.write(`[/swarm] signing key lifecycle ${promptSafe(lerr || lout)}\n`);
        // TS-05: a lifecycle step that did not run (no python3, a non-zero exit, a signal) is reported with its reason; the
        // LOADING/INITIALIZING lines are not written, since they would claim a lifecycle that never happened
        if (lres.error || lres.status !== 0) {
          const why = lres.error ? lres.error.message : lres.signal ? `killed by ${lres.signal}` : `exit ${lres.status}`;
          notify(ctx, `/swarm: signing key lifecycle did not complete (${why}); swarm init state not logged`, "warning");
        } else {
          process.stderr.write(`[/swarm] swarm init state=LOADING for ${earlyCorr} (signing key lifecycle first; LOADING before INITIALIZING; reduced if no key; enterprise audit/integrity; gsd workflowz ultrathink orchestrate path; msgs shown at start)\n`);
          process.stderr.write(`[/swarm] swarm init state=INITIALIZING for ${earlyCorr} (before gsd workflowz, state logged, messages shown; no activity from prior unit triggers)\n`);
        }
      } catch (e) {
        process.stderr.write(`[/swarm] signing key lifecycle note (non-fatal): ${promptSafe(String(e))}\n`);
      }
      // explicit swarm trigger ...
      process.stderr.write(`[/swarm] explicit swarm trigger emit swarm ID ${earlyCorr}\n`);
      let res: BridgeResult;
      try {
        // the same brief → the same correlation id and prefix → orch_plan reuses instead of duplicating (D-11)
        const args = planArgs(ctx, { ...parsed, correlation_id: swarmCorrelationId(parsed) });
        res = await bridge({ script: "orch_plan", args, cwd: ctx.cwd });
      } catch (err) {
        // a conflicting brief arrives here as E-CONTRACT with the orch_plan hint (D-11 step 3)
        return notify(ctx, err instanceof Error ? err.message : String(err), "error");
      }
      if (res.exitCode !== 0) {
        const err = res.json.error as { message?: unknown } | undefined;
        const detail =
          typeof err?.message === "string" ? err.message : typeof res.json.summary === "string" ? res.json.summary : JSON.stringify(res.json);
        return notify(ctx, `/swarm: orch_plan exit ${res.exitCode}: ${detail}`, "error");
      }
      const assignment = executionAssignment(res.json);
      if (typeof assignment === "string") return notify(ctx, `/swarm dispatch refused: ${assignment}`, "error");
      const corr = res.json.correlation_id as string;
      const from = ctx.sessionManager.getBranch().length;
      pi.sendUserMessage(dispatchPrompt(res, parsed, assignment));
      // sendUserMessage starts the turn asynchronously: wait for it to start, then hold until it ends (G-04-05-1)
      if (!(await awaitDispatchStart(ctx, corr, from, opts))) {
        const seconds = (opts.startTimeoutMs ?? DEFAULT_START_TIMEOUT_MS) / 1000;
        return notify(ctx, `/swarm: dispatch did not start within ${seconds} s (plan ${corr})`, "error");
      }
      const capMs = holdTimeoutMs(ctx, opts, process.env);
      if (!(await awaitTurnEnd(ctx, capMs, opts))) {
        // the dispatch happened; only the hold gave up (a cap the operator set, or the interactive default)
        return notify(
          ctx,
          `/swarm: plan ${corr} dispatched to a01-orchestrator; its turn is still running after ${capMs / 1000} s, so /swarm stopped holding the session (${HOLD_ENV} raises the cap)`,
          "warning",
        );
      }
      // the turn is over; waitForIdle only drains the session's event handlers now
      await ctx.waitForIdle();
      notify(ctx, `/swarm: plan ${corr} dispatched to a01-orchestrator (${readyTaskIds(res.json).length} ready tasks)`, "info");
    },
  };
}
