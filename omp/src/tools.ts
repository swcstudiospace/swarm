/**
 * Typed swarm tools. Each execute builds `--flag=value` argv and calls the injected Bridge exactly once;
 * Python decides state and signature validity; the assignment consumer also checks returned dispatch metadata.
 * D-01: every tool is hidden + essential, so only sessions whose agent frontmatter names it get it.
 * D-03: mutating tools refuse with E-POLICY in plan mode before any argv, file or bridge work.
 * WR-03: swarm_gate runs only the calling gate agent's own gate (GATE_AGENTS), E-POLICY otherwise.
 */
import { type Bridge, type BridgeResult, type Script, SwarmToolError, writeInputFile } from "./bridge.ts";
import { callingAgent, GATE_AGENTS, inPlanMode } from "./context.ts";
import { ORCHESTRATOR, SWARM_SLUGS } from "./guard.ts";
import type { ExtensionContext, JsonSchema, ToolDefinition, ToolResult } from "./omp-api.ts";
import { gitToplevel } from "./paths.ts";

/**
 * Schema pattern for ids that become argv values and (task ids) file names: mirrors swarm/script_base.py
 * TASK_ID_RE + no "..", which python enforces (CR-01). Never dash-leading, so argparse never reads a flag.
 */
export const ID_PATTERN = "^(?!.*\\.\\.)[A-Za-z0-9][A-Za-z0-9._-]{0,127}$";
const ID = { type: "string", pattern: ID_PATTERN, minLength: 1 } as const;

/** The 14 Task Store states (swarm/taskstore.py:22-36 TaskState). */
const STATES = [
  "CREATED", "VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS", "IN_REVIEW", "CHANGES_REQUESTED",
  "APPROVED", "DONE", "BLOCKED", "FAILED", "RETRY", "ESCALATED", "CANCELLED",
] as const;

/** orch_plan.py choices: --pattern (PATTERNS only: `custom` needs --plan <path>, which no tool exposes), --risk-class
 * (taskstore.py GATES_BY_RISK), --priority. */
export const PATTERNS = ["feature", "hotfix", "dependency", "cloud-native", "cluster", "web3", "parallel"] as const;
export const RISK_CLASSES = ["low", "medium", "high"] as const;
const PRIORITIES = ["P0", "P1", "P2", "P3"] as const;

/** Gate → script (parity mirror of swarm/verdicts.py GATE_SCRIPTS; omp/test/gate.test.ts pins equal);
 * the script derives and signs the verdict. */
const GATE_SCRIPTS = { quality: "qa_gate", review: "rev_gate", security: "sec_gate", release: "rel_plan" } as const satisfies Record<string, Script>;
type Gate = keyof typeof GATE_SCRIPTS;
/** Every gate has its owner in GATE_AGENTS (context.ts), and nothing else. */
GATE_AGENTS satisfies Record<Gate, string>;

/** Finding severities (copied from swarm/gates.py:10 SEVERITIES); BLOCKING_SEVERITY = "major". */
const SEVERITIES = ["info", "minor", "major", "critical", "blocker"] as const;
/** qa_gate's runner default plus time for canonical cleanup before the bridge kills its process group. */
const QA_GATE_TIMEOUT_S = 1800;
const QA_GATE_CLEANUP_S = 60;

/** A finding (swarm/gates.py make_finding fields). No verdict field anywhere: severities decide (D-09). */
const FINDING = {
  type: "object",
  properties: {
    severity: { type: "string", enum: SEVERITIES, description: "major, critical or blocker fails the target" },
    summary: { type: "string", minLength: 1, description: "What is wrong" },
    id: { type: "string" },
    kind: { type: "string", description: "e.g. semantic, security, functional" },
    evidence: { type: "string" },
    ac_ref: { type: ["string", "null"], description: "Acceptance criterion it violates" },
    owner_suggestion: { type: ["string", "null"], description: "Agent that should fix it, e.g. A05" },
    location: { type: ["string", "null"], description: "path:line" },
  },
  required: ["severity", "summary"],
  additionalProperties: false,
} as const;

/** swarm_tool.py --kind choices. */
const CATALOG_KINDS = ["tools", "skills", "all"] as const;
/** Registry tool names (swarm/tool_registry.py `_conventional_name`): lowercase, digits, underscore. */
const TOOL_NAME_PATTERN = "^[a-z][a-z0-9_]{0,63}$";
/** One argv value is capped by the kernel at 128 KiB; swarm_tool.py enforces the same 100 000 byte ceiling. */
const MAX_TOOL_INPUT_BYTES = 100_000;

type Details = Record<string, unknown>;

/** A registered swarm tool as omp sees it (params are the model's JSON, shaped by `parameters`). */
export type SwarmTool = ToolDefinition<Record<string, unknown>, Details>;

export type StatusParams = { correlation_id?: string };
export type PlanParams = {
  brief: string;
  pattern: (typeof PATTERNS)[number];
  risk_class: (typeof RISK_CLASSES)[number];
  prefix?: string;
  correlation_id?: string;
  priority?: (typeof PRIORITIES)[number];
  acceptance?: string[];
  dry_run?: boolean;
  /** Disjoint blast radii. Present on a parallel plan; also selects parallel when the pattern is feature. */
  slices?: Array<{ id: string; paths: string[]; agent?: string; capability?: string; title?: string; acceptance?: string[] }>;
};
export type IngestParams = { task_id: string; result: Record<string, unknown> };
export type AssignmentParams = { task_id: string; correlation_id: string };
export type Finding = { severity: (typeof SEVERITIES)[number]; summary: string } & Partial<
  Record<"id" | "kind" | "evidence", string> & Record<"ac_ref" | "owner_suggestion" | "location", string | null>
>;
export type GateParams = {
  gate: Gate;
  task_id: string;
  correlation_id: string;
  per_target_findings?: Record<string, Finding[]>;
  /** Quality only: per-invocation runner seconds, integer 1..7200; does not extend the task/session budget. */
  timeout_s?: number;
  dry_run?: boolean;
};
export type CatalogParams = { kind?: (typeof CATALOG_KINDS)[number]; query?: string };
export type ToolCallParams = { name: string; input: Record<string, unknown> };
export type TransitionParams = { task_id: string; state: (typeof STATES)[number]; reason: string; dry_run?: boolean };

interface ToolSpec<P> {
  name: string;
  label: string;
  description: string;
  parameters: JsonSchema;
  /** D-03: refused in plan mode before anything else runs. */
  mutating: boolean;
  run(toolCallId: string, params: P, signal: AbortSignal | undefined, ctx: ExtensionContext): Promise<ToolResult<Details>>;
}

/** D-01 registration shape plus the D-03 plan-mode refusal for mutating tools. */
function define<P>({ mutating, run, ...meta }: ToolSpec<P>): ToolDefinition<P, Details> {
  return {
    ...meta,
    // schemas reuse module constants (enums, FINDING); each session gets its own copy, so no mutable state is shared
    parameters: structuredClone(meta.parameters),
    hidden: true,
    loadMode: "essential",
    async execute(toolCallId, params, signal, _onUpdate, ctx) {
      if (mutating && inPlanMode(ctx)) {
        throw new SwarmToolError(
          "E-POLICY",
          `${meta.name} refused: omp is in plan mode (read-only), so the Task Store was not touched; ` +
            "leave plan mode to change it (swarm_status still works)",
        );
      }
      return run(toolCallId, params, signal, ctx);
    },
  };
}

/** `--root=<git toplevel of ctx.cwd>` (D-06); a cwd outside git is passed as-is. */
function rootArg(ctx: ExtensionContext): string {
  return `--root=${gitToplevel(ctx.cwd) ?? ctx.cwd}`;
}

/**
 * The orch_plan argv (shared by swarm_plan and `/swarm`, D-11: one convention). `--brief-text=` only: `--brief <path>`
 * would read any file, and `--plan` takes a model path (T-03-08).
 */
export function planArgs(ctx: ExtensionContext, params: PlanParams): string[] {
  const args = [rootArg(ctx), `--brief-text=${params.brief}`, `--pattern=${params.pattern}`, `--risk-class=${params.risk_class}`];
  if (params.prefix) args.push(`--prefix=${params.prefix}`);
  if (params.correlation_id) args.push(`--correlation-id=${params.correlation_id}`);
  if (params.priority) args.push(`--priority=${params.priority}`);
  for (const item of params.acceptance ?? []) args.push(`--acceptance=${item}`);
  if (params.slices?.length) args.push(`--slices-json=${JSON.stringify(params.slices)}`);
  if (params.dry_run) args.push("--dry-run");
  return args;
}

/** Exit 1 (status fail: a failing verdict, a release freeze) is data the agent must read, so it is returned with `FAIL:`. */
function toolResult(res: BridgeResult, extra: string[] = []): ToolResult<Details> {
  const summary = typeof res.json.summary === "string" ? res.json.summary : JSON.stringify(res.json);
  const text = [res.exitCode === 1 ? `FAIL: ${summary}` : summary, ...extra].join("\n");
  return { content: [{ type: "text", text }], details: res.json };
}
function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

const ASSIGNMENT_HEADING = "# task.assign (signed envelope)\n```json\n";
/**
 * omp sends only `content` to the model. `details` is UI and log metadata, so the
 * fields A01 requires (`status`, `assignment_verified`, `agent_slug`) have to lead
 * the text. The header is one JSON line of fields `validAssignment` already accepted
 * and carries no signature. The first marker splits the exact prompt back out, even
 * when that prompt contains the same line.
 */
const ASSIGNMENT_PROMPT_MARKER = "\n--- prompt ---\n";

function assignmentVisibleText(result: Record<string, unknown>): string {
  const header = JSON.stringify({
    status: result.status,
    assignment_verified: result.assignment_verified,
    task_id: result.task_id,
    correlation_id: result.correlation_id,
    agent_slug: result.agent_slug,
  });
  return `${header}${ASSIGNMENT_PROMPT_MARKER}${result.prompt as string}`;
}

/**
 * omp sends only `content` to the model. A plan summary hides
 * `execution_assignment_verified` and the signed envelope in `details`, so A01
 * yields BLOCKED after Python has already signed the controller assignment.
 * The envelope is included only when that flag is strictly true.
 */
function planVisibleText(result: Record<string, unknown>): string {
  const verified = result.execution_assignment_verified === true;
  const visible: Record<string, unknown> = {
    status: result.status,
    correlation_id: result.correlation_id,
    pattern: result.pattern,
    reused: result.reused === true,
    execution_assignment_verified: verified,
    summary: typeof result.summary === "string" ? result.summary : "",
  };
  if (verified && isObject(result.execution_assignment)) {
    visible.execution_assignment = result.execution_assignment;
  }
  return JSON.stringify(visible);
}

function planResult(res: BridgeResult): ToolResult<Details> {
  if (!isObject(res.json)) return toolResult(res);
  const text = planVisibleText(res.json);
  return { content: [{ type: "text", text: res.exitCode === 1 ? `FAIL: ${text}` : text }], details: res.json };
}

/** Python verifies the signature; this consumer refuses mismatched dispatch metadata or an unrelated prompt. */
function validAssignment(res: BridgeResult, params: AssignmentParams): boolean {
  const result = res.json;
  if (!isObject(result)) return false;
  if (res.exitCode !== 0 || result.status !== "ok" || result.assignment_verified !== true ||
      result.task_id !== params.task_id || result.correlation_id !== params.correlation_id ||
      typeof result.agent_slug !== "string" || !SWARM_SLUGS.includes(result.agent_slug) ||
      typeof result.prompt !== "string" || !isObject(result.assignment)) return false;
  const envelope = result.assignment;
  const payload = envelope.payload;
  if (envelope.type !== "task.assign" || envelope.schema !== "swarm.v1.task.assign" ||
      envelope.source !== "A01@runner" || envelope.correlation_id !== params.correlation_id ||
      typeof envelope.sig !== "string" || !/^(?:hmac|ed25519):.+$/.test(envelope.sig) ||
      typeof envelope.target !== "string" || result.agent_slug.split("-")[0].toUpperCase() !== envelope.target ||
      !isObject(payload) || payload.task_id !== params.task_id || payload.correlation_id !== params.correlation_id ||
      payload.agent_id !== envelope.target) return false;
  // Memory precedes the real envelope and may quote the same heading and fence. Like orch_assign.py, count only
  // blocks that carry the verified envelope and require exactly one; forged or unrelated blocks never match.
  const expected = JSON.stringify(envelope);
  const matches = result.prompt.split(ASSIGNMENT_HEADING).slice(1).filter((block) => {
    try {
      return JSON.stringify(JSON.parse(block.split("\n```", 1)[0])) === expected;
    } catch {
      return false;
    }
  });
  return matches.length === 1;
}

export function buildTools(bridge: Bridge): SwarmTool[] {
  const status = define<StatusParams>({
    name: "swarm_status",
    label: "Swarm status",
    description:
      "List the swarm Task Store for a correlation (default: the latest): per task its state, agent, attempt, " +
      "ready flag, required gates and verdicts, plus counts, escalations and completion. The result also names " +
      "the absolute Task Store path (`db`, `swarm_dir`), which is identical from any cwd in the repo. Read-only.",
    parameters: {
      type: "object",
      properties: {
        correlation_id: { ...ID, description: "Correlation id of the plan to list (omit for the latest plan)" },
      },
      additionalProperties: false,
    },
    mutating: false,
    async run(_toolCallId, params, signal, ctx) {
      const args = [rootArg(ctx)];
      if (params.correlation_id) args.push(`--correlation-id=${params.correlation_id}`);
      const res = await bridge({ script: "orch_status", args, cwd: ctx.cwd, signal });
      return toolResult(res, typeof res.json.db === "string" ? [`db: ${res.json.db}`] : []);
    },
  });

  const assignment = define<AssignmentParams>({
    name: "swarm_assignment",
    label: "Swarm assignment",
    description:
      "Obtain the canonical verified signed task.assign envelope and exact specialist prompt for an existing " +
      "IN_PROGRESS leased task in this correlation. A01 only; refused in plan mode. Read-only Task Store access, " +
      "but grants dispatch authority. No caller-supplied envelope, agent, payload or signer input. " +
      "The model-visible text is one JSON object with status, assignment_verified, task_id, correlation_id and " +
      "agent_slug, then a line `--- prompt ---`, then the exact specialist prompt. Dispatch only when status is " +
      "\"ok\" and assignment_verified is true. Pass only the text after the first `--- prompt ---` marker, unchanged.",
    parameters: {
      type: "object",
      properties: {
        task_id: { ...ID, description: "Existing leased task id" },
        correlation_id: { ...ID, description: "The leased task's plan correlation id" },
      },
      required: ["task_id", "correlation_id"],
      additionalProperties: false,
    },
    mutating: false,
    async run(_toolCallId, params, signal, ctx) {
      // Direct execute callers must meet the same authority boundary as the tool_call guard.
      if (callingAgent(ctx) !== ORCHESTRATOR || inPlanMode(ctx)) {
        throw new SwarmToolError("E-POLICY", "swarm_assignment refused: only A01 outside plan mode may obtain dispatch authority");
      }
      if (!isObject(params) || Object.keys(params).some((key) => key !== "task_id" && key !== "correlation_id") ||
          typeof params.task_id !== "string" || !new RegExp(ID_PATTERN).test(params.task_id) ||
          typeof params.correlation_id !== "string" || !new RegExp(ID_PATTERN).test(params.correlation_id)) {
        throw new SwarmToolError("E-INPUT", "swarm_assignment requires only valid task_id and correlation_id");
      }
      let res: BridgeResult;
      try {
        res = await bridge({
          script: "orch_assign",
          args: [rootArg(ctx), `--task-id=${params.task_id}`, `--correlation-id=${params.correlation_id}`],
          cwd: ctx.cwd,
          signal,
        });
      } catch (error) {
        // Never forward a failed child's stdout, stderr or details: they may contain invalid authority material.
        throw new SwarmToolError(
          error instanceof SwarmToolError ? error.code : "E-INTERNAL",
          "swarm_assignment failed: canonical assignment unavailable",
        );
      }
      if (!validAssignment(res, params)) {
        throw new SwarmToolError("E-CONTRACT", "swarm_assignment refused: invalid canonical assignment response");
      }
      return { content: [{ type: "text", text: assignmentVisibleText(res.json) }], details: res.json };
    },
  });

  const plan = define<PlanParams>({
    name: "swarm_plan",
    label: "Swarm plan",
    description:
      "Plan a swarm run from a brief (A01): creates the pattern's task DAG in the Task Store (PLANNED, gates " +
      "derived from the risk class) under one correlation id. Re-running with the same brief, pattern, risk class, " +
      "priority, acceptance and correlation_id reuses the existing plan (`reused: true`) instead of duplicating it. " +
      "dry_run lists the tasks that would be created without writing. The model-visible text is one JSON object " +
      "with status, correlation_id, pattern, reused, execution_assignment_verified and summary. When " +
      "execution_assignment_verified is true, that object also contains execution_assignment, the signed " +
      "swarm.v1.task.assign envelope. Continue only then. A dry-run or unsigned plan sets the flag false and " +
      "omits the envelope.",
    parameters: {
      type: "object",
      properties: {
        brief: { type: "string", minLength: 1, description: "The brief text (not a path)" },
        pattern: {
          type: "string",
          enum: PATTERNS,
          description:
            "DAG pattern: feature (13 tasks), hotfix (8), dependency (8), cloud-native (16: cloud architecture, events, golden path, cost, image policy before the build and image evidence after it), cluster (14: capacity, traffic, admission policy, GitOps, network and policy scans after the manifests, resilience; manifests are risk high), web3 (15: custody policy, contracts, audit, custody compliance, dapp; contracts and dapp are risk high), parallel (one lane per slices[] entry, then one Greptile review). parallel requires slices.",
        },
        slices: {
          type: "array",
          minItems: 2,
          description: "Disjoint blast radii for pattern parallel. The same agent may own more than one slice. Paths must not overlap.",
          items: {
            type: "object",
            properties: {
              id: { type: "string", pattern: "^[a-z][a-z0-9-]{0,31}$" },
              paths: { type: "array", items: { type: "string", minLength: 1 }, minItems: 1 },
              agent: { type: "string", description: "Implementer id or slug, for example A05. Omit only when capability names an implementer." },
              capability: { type: "string" },
              title: { type: "string" },
              acceptance: { type: "array", items: { type: "string", minLength: 1 } },
            },
            required: ["id", "paths"],
            additionalProperties: false,
          },
        },
        risk_class: { type: "string", enum: RISK_CLASSES, description: "Decides the required gates per task" },
        prefix: { ...ID, description: "Task id prefix (default: T + 4 hex of the correlation id)" },
        correlation_id: { ...ID, description: "Correlation id (default: a new uuid); reuse it to make a re-run idempotent" },
        priority: { type: "string", enum: PRIORITIES, description: "Task priority (default P2)" },
        acceptance: { type: "array", items: { type: "string", minLength: 1 }, description: "Acceptance criteria" },
        dry_run: { type: "boolean", description: "List the tasks that would be created without writing" },
      },
      required: ["brief", "pattern", "risk_class"],
      additionalProperties: false,
    },
    mutating: true,
    async run(_toolCallId, params, signal, ctx) {
      return planResult(await bridge({ script: "orch_plan", args: planArgs(ctx, params), cwd: ctx.cwd, signal }));
    },
  });

  const ingest = define<IngestParams>({
    name: "swarm_ingest",
    label: "Swarm ingest",
    description:
      "Ingest an agent's task.result v1 object for a leased task (A01): the same result path the headless runner " +
      "uses, so the task moves to the result's state (e.g. IN_REVIEW) and the DAG is reconciled. The result is " +
      "validated by python against the task.result v1 contract: required task_id and state " +
      "(IN_PROGRESS | IN_REVIEW | FAILED | BLOCKED), optional outputs, metrics, summary_md, needs, error, verdicts, gate.",
    parameters: {
      type: "object",
      properties: {
        task_id: { ...ID, description: "Task the result belongs to" },
        result: { type: "object", description: "A task.result v1 object (validated by python, not here)" },
      },
      required: ["task_id", "result"],
      additionalProperties: false,
    },
    mutating: true,
    async run(toolCallId, params, signal, ctx) {
      // D-05: --ingest takes a path, so the bridge writes the object under SWARM_DIR/results (never a model path)
      const file = writeInputFile(ctx.cwd, "ingest", toolCallId, params.result);
      const args = [rootArg(ctx), `--task-id=${params.task_id}`, `--ingest=${file}`];
      return toolResult(await bridge({ script: "orch_status", args, cwd: ctx.cwd, signal }));
    },
  });

  const transition = define<TransitionParams>({
    name: "swarm_transition",
    label: "Swarm transition",
    description:
      "Move one Task Store task to a new state along the legal state machine (A01 only), e.g. lease a ready task " +
      "PLANNED → CLAIMED → IN_PROGRESS. An illegal transition fails with E-CONTRACT and leaves the task unchanged. " +
      "Every transition is audited with the reason. dry_run reports the transition without writing.",
    parameters: {
      type: "object",
      properties: {
        task_id: { ...ID, description: "Task id to transition" },
        state: { type: "string", enum: STATES, description: "Target state" },
        reason: { type: "string", minLength: 1, description: "Why (recorded in the task's audit history)" },
        dry_run: { type: "boolean", description: "Report the transition without writing" },
      },
      required: ["task_id", "state", "reason"],
      additionalProperties: false,
    },
    mutating: true,
    async run(_toolCallId, params, signal, ctx) {
      // --transition takes two space-form values (nargs=2): the schema forbids a dash-leading id and enumerates state
      const args = [rootArg(ctx), "--transition", params.task_id, params.state, `--reason=${params.reason}`];
      if (params.dry_run) args.push("--dry-run");
      return toolResult(await bridge({ script: "orch_status", args, cwd: ctx.cwd, signal }));
    },
  });

  const gate = define<GateParams>({
    name: "swarm_gate",
    label: "Swarm gate",
    description:
      "Run your gate script for a leased (IN_PROGRESS) gate task: quality → qa_gate, review → rev_gate, " +
      "security → sec_gate, release → rel_plan. The script derives the verdict from findings, signs it and records " +
      "one verdict row per target of the gate task; you never state a verdict. Review, quality and security: " +
      "per_target_findings maps each target task id to your findings for it; the script's own findings are added to " +
      "every target (review: a target you omit gets every finding; quality and security: only the script's); a key " +
      "that is not a target of the gate task is refused (E-INPUT). " +
      "To fail a target, include at least " +
      "one finding of severity major or higher (major, critical or blocker); minor and info findings pass. " +
      "A failing verdict is returned as text starting `FAIL:`, not as an error. Quality alone accepts timeout_s " +
      "(integer 1..7200 seconds); omission preserves the runner's 1800-second default. It does not extend task, " +
      "lease or session limits. Call this before swarm_ingest of the gate task's result.",
    parameters: {
      type: "object",
      properties: {
        gate: { type: "string", enum: Object.keys(GATE_SCRIPTS), description: "Which gate you run" },
        task_id: { ...ID, description: "Your leased gate task id (its notes name the targets)" },
        correlation_id: { ...ID, description: "The gate task's correlation id" },
        per_target_findings: {
          type: "object",
          additionalProperties: { type: "array", items: FINDING },
          description:
            "review, quality and security gates: {target task id: [finding]}. To fail a target include at least " +
            "one finding of severity major, critical or blocker; an empty list adds nothing to it.",
        },
        timeout_s: {
          type: "integer", minimum: 1, maximum: 7200,
          description: "Quality only: runner deadline in seconds, plus 60 seconds of outer cleanup supervision; " +
            "omitted: 1800-second runner, 1860-second supervisor. Independent task/lease/session limits still apply.",
        },
        dry_run: { type: "boolean", description: "Canned dry-run verdict (per_target_findings is ignored)" },
      },
      required: ["gate", "task_id", "correlation_id"],
      additionalProperties: false,
    },
    mutating: true,
    async run(toolCallId, params, signal, ctx) {
      const script = GATE_SCRIPTS[params.gate];
      if (!script) throw new SwarmToolError("E-INPUT", `unknown gate ${JSON.stringify(params.gate)}`);
      const agent = callingAgent(ctx);
      if (agent !== GATE_AGENTS[params.gate]) {
        throw new SwarmToolError(
          "E-POLICY",
          `swarm_gate refused: the ${params.gate} gate is run only by ${GATE_AGENTS[params.gate]}, ` +
            `not ${agent === undefined ? "an unidentified session" : JSON.stringify(agent)}`,
        );
      }
      if ("timeout_s" in params) {
        if (params.gate !== "quality") {
          throw new SwarmToolError("E-INPUT", "timeout_s is accepted only by the quality gate");
        }
        if (typeof params.timeout_s !== "number" || !Number.isInteger(params.timeout_s) ||
            params.timeout_s < 1 || params.timeout_s > 7200) {
          throw new SwarmToolError("E-INPUT", "timeout_s must be an integer from 1 to 7200 seconds");
        }
      }
      if (params.per_target_findings !== undefined && params.gate === "release") {
        throw new SwarmToolError("E-INPUT", "per_target_findings is not accepted by the release gate");
      }
      // argv from whitelisted fields only: extra model keys (e.g. a forged verdict) never reach python (T-03-06)
      const args = [rootArg(ctx), `--task-id=${params.task_id}`, `--correlation-id=${params.correlation_id}`];
      if (params.timeout_s !== undefined) args.push(`--timeout=${params.timeout_s}`);
      if (params.per_target_findings !== undefined) {
        args.push(`--per-target-findings=${writeInputFile(ctx.cwd, "findings", toolCallId, params.per_target_findings)}`);
      }
      if (params.dry_run) args.push("--dry-run");
      const timeoutMs = params.gate === "quality"
        ? ((params.timeout_s ?? QA_GATE_TIMEOUT_S) + QA_GATE_CLEANUP_S) * 1000
        : undefined;
      return toolResult(await bridge({ script, args, cwd: ctx.cwd, signal, timeoutMs }));
    },
  });

  /** The session's own agent slug; a tool call is authorized for it and for nobody the model names (DESIGN 5.3). */
  function sessionCaller(tool: string, ctx: ExtensionContext): string {
    const agent = callingAgent(ctx);
    if (agent === undefined || !SWARM_SLUGS.includes(agent)) {
      throw new SwarmToolError(
        "E-POLICY",
        `${tool} refused: the session has no swarm agent identity, so there is no caller to authorize`,
      );
    }
    return agent;
  }

  const catalog = define<CatalogParams>({
    name: "swarm_catalog",
    label: "Swarm catalog",
    description:
      "List the registry tools you may call with swarm_tool_call (name, purpose, side-effect tier, timeout) and the " +
      "skills available to you (your own plus shared), optionally filtered by kind and by words matched against " +
      "name and purpose. The list is computed for the calling agent only. Read-only; works in plan mode.",
    parameters: {
      type: "object",
      properties: {
        kind: { type: "string", enum: CATALOG_KINDS, description: "tools, skills or all (default all)" },
        query: { type: "string", minLength: 1, maxLength: 200, description: "Words that must all appear in the name or purpose" },
      },
      additionalProperties: false,
    },
    mutating: false,
    async run(_toolCallId, params, signal, ctx) {
      const caller = sessionCaller("swarm_catalog", ctx);
      // argv from whitelisted fields only; the caller is the session identity, never a model field
      const args = [rootArg(ctx), "catalog", `--caller=${caller}`];
      if (params.kind !== undefined) args.push(`--kind=${params.kind}`);
      if (params.query !== undefined) args.push(`--query=${params.query}`);
      const res = await bridge({ script: "swarm_tool", args, cwd: ctx.cwd, signal });
      const { tools, skills, skills_unavailable: unavailable } = res.json;
      const listing = JSON.stringify({ tools, skills, ...(unavailable === undefined ? {} : { skills_unavailable: unavailable }) });
      return { content: [{ type: "text", text: `${String(res.json.summary)}\n${listing}` }], details: res.json };
    },
  });

  const toolCall = define<ToolCallParams>({
    name: "swarm_tool_call",
    label: "Swarm tool call",
    description:
      "Call one registry tool you are permitted to use (see swarm_catalog) with a JSON input object. Returns the " +
      "registry result object unchanged: state SUCCESS, INVALID_INPUT, UNAUTHORIZED, DEPENDENCY_UNAVAILABLE, TIMEOUT " +
      "or PARTIAL_SUCCESS plus the tool's fields; read `state` before using any field. READ tools work in plan " +
      "mode; a WRITE tool is refused there. The caller is your own agent identity and cannot be changed.",
    parameters: {
      type: "object",
      properties: {
        name: { type: "string", pattern: TOOL_NAME_PATTERN, description: "Registered tool name, for example be_route_norm" },
        input: { type: "object", description: "The tool's input object; python validates it against the tool's input schema" },
      },
      required: ["name", "input"],
      additionalProperties: false,
    },
    mutating: false,
    async run(_toolCallId, params, signal, ctx) {
      const caller = sessionCaller("swarm_tool_call", ctx);
      if (!isObject(params) || typeof params.name !== "string" || !new RegExp(TOOL_NAME_PATTERN).test(params.name) ||
          !isObject(params.input)) {
        throw new SwarmToolError("E-INPUT", "swarm_tool_call requires a tool name and an input object");
      }
      const input = JSON.stringify(params.input);
      if (Buffer.byteLength(input, "utf8") > MAX_TOOL_INPUT_BYTES) {
        throw new SwarmToolError("E-INPUT", `swarm_tool_call input exceeds ${MAX_TOOL_INPUT_BYTES} bytes`);
      }
      const args = [rootArg(ctx), "call", `--caller=${caller}`, `--name=${params.name}`, `--input=${input}`];
      // python knows each tool's tier: in plan mode it refuses anything above READ before importing the handler
      if (inPlanMode(ctx)) args.push("--plan-mode");
      const res = await bridge({ script: "swarm_tool", args, cwd: ctx.cwd, signal });
      const result = isObject(res.json.result) ? res.json.result : res.json;
      return { content: [{ type: "text", text: JSON.stringify(result) }], details: result };
    },
  });

  return [status, plan, assignment, ingest, transition, gate, catalog, toolCall];
}
