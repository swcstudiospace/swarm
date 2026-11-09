/**
 * agent-swarm omp extension entry (omp/package.json `omp.extensions`).
 * omp calls the factory once per session (subagents included), so it only registers:
 * no fs calls, no spawns, no `pi` action calls, no module-scope state (TOOL-01). Runtime actions such as
 * getActiveTools run only inside handlers.
 */
import { homedir, tmpdir } from "node:os";
import { resolve } from "node:path";
import { type Bridge, type Inflight, killInflight, runPy } from "./bridge.ts";
import { swarmCommand } from "./commands.ts";
import { callingAgent, inPlanMode, sessionAgent } from "./context.ts";
import { GUARD_ERROR_PREFIX, type GuardFacts, guardToolCall, inSwarm, ORCHESTRATOR, parseSignerDenyPaths } from "./guard.ts";
import { runtimeContext, swarmContext } from "./hooks.ts";
import type { ExtensionContext, ExtensionFactory } from "./omp-api.ts";
import { type BarEvent, createSwarmBar, type SwarmBarOptions } from "./swarm_bar.ts";
import { buildTools } from "./tools.ts";

function taskResult(event: unknown): { toolCallId: string | undefined; isError: boolean } | undefined {
  if (!event || typeof event !== "object" || !("toolName" in event) || event.toolName !== "task") return undefined;
  const toolCallId = "toolCallId" in event && typeof event.toolCallId === "string" ? event.toolCallId : undefined;
  return { toolCallId, isError: "isError" in event && event.isError === true };
}

export interface SwarmExtensionOptions {
  bridge: Bridge;
  /** Test seams of the third bar (clocks, timers, env, stderr); production passes none. */
  bar?: SwarmBarOptions;
}

export function createSwarmExtension({ bridge, bar: barOptions }: SwarmExtensionOptions): ExtensionFactory {
  return (pi) => {
    // WR-01: this session's children only — a sibling session's shutdown never reaches them
    const inflight: Inflight = new Set();
    // the third bar is per session, like inflight: a sibling session never paints this session's rows
    const bar = createSwarmBar(pi, barOptions);
    const scoped: Bridge = (req) => bridge({ ...req, inflight });
    for (const tool of buildTools(scoped)) pi.registerTool(tool);
    // ORCH-03 (D-11): the same scoped bridge, so session_shutdown also sweeps the command's python child
    pi.registerCommand("swarm", swarmCommand(pi, scoped, {}));
    // The bar is display-only: nothing it does may change a decision or throw into the host.
    const display = (fn: () => void): void => {
      try {
        fn();
      } catch {
        // display only
      }
    };
    const barEvent = (ctx: ExtensionContext, name: BarEvent): void => display(() => bar.event(ctx, name));
    // D-07: group-kill this session's in-flight python children when it ends (registration only, no I/O)
    pi.on("session_shutdown", () => {
      killInflight(inflight);
      display(() => bar.stop());
    });
    // DESIGN 6: mount at the first eligible event and keep the bar for the session; re-mount one deferral after the events that
    // move the other bars (the restack protocol keeps the order: swarm above Ultrathink)
    for (const name of ["session_start", "session_switch", "agent_start", "turn_start", "turn_end", "tool_execution_start", "tool_execution_end", "agent_end"] as const) {
      pi.on(name, (event, ctx) => {
        // omp emits tool_execution_end for every outcome, including a call another extension blocked or an abort: unlike
        // tool_result it always arrives, so it is what settles a `task` row whose call never ran
        if (name === "tool_execution_end") {
          display(() => {
            const result = taskResult(event);
            if (result) bar.taskResult(ctx, result.toolCallId, result.isError);
          });
        }
        barEvent(ctx, name);
      });
    }
    // HOOK-01 (D-09): SDLC prompts in a top-level session get the AgentSwarm context; fail-open on any error.
    // OPEN-3 R1: swarm-slug sessions get the runtime part instead (the two never apply to the same session).
    pi.on("before_agent_start", (event, ctx) => {
      barEvent(ctx, "before_agent_start");
      try {
        return swarmContext(event, ctx, process.env) ?? runtimeContext(event, ctx);
      } catch {
        return undefined;
      }
    });
    // HOOK-03/04 guard. omp turns a throwing handler into a block (D-02): an error blocks inside a swarm
    // session and returns undefined outside one, so the operator's own tools never break.
    pi.on("tool_call", (event, ctx) => {
      const env = process.env;
      let known: Pick<GuardFacts, "agent" | "topLevel"> = { agent: undefined, topLevel: false };
      try {
        const { restricted, topLevel } = sessionAgent(ctx);
        known = { agent: callingAgent(ctx), topLevel };
        // getActiveTools only for A01: the depth-cap check is the only reader (D-06).
        // The same list distinguishes a real cap from the advisor review surface.
        const activeTools = known.agent === ORCHESTRATOR ? pi.getActiveTools() : undefined;
        const hasTask = activeTools !== undefined ? activeTools.includes("task") : true;
        // D-08: the runtime roots bridge.swarmRoot() picks from (env SWARM_ROOT, the package's repo), spelled without fs
        const root = env.SWARM_ROOT?.trim();
        const runtimeRoots = [...(root ? [resolve(root)] : []), resolve(import.meta.dir, "..", "..")];
        const signerReadPaths = parseSignerDenyPaths(env.SWARM_SIGNER_DENY_PATHS);
        const facts = { ...known, restricted, planMode: inPlanMode(ctx), hasTask, activeTools, env, cwd: ctx.cwd, home: homedir(), tmp: tmpdir(), runtimeRoots, signerReadPaths };
        const decision = guardToolCall(event, facts);
        // an accepted `task` call is the bar's last-resort row source (the bus and live.json are richer when present)
        if (!decision?.block && event.toolName === "task") display(() => bar.taskSpawn(ctx, event.input, event.toolCallId));
        return decision;
      } catch (err) {
        if (!inSwarm({ ...known, env })) return undefined;
        return { block: true, reason: `${GUARD_ERROR_PREFIX}${err instanceof Error ? err.message : String(err)})` };
      }
    });
    // tool_result is the parent-side signal that the `task` call finished: it settles the rows the bus does not own.
    pi.on("tool_result", (event, ctx) => {
      display(() => {
        const result = taskResult(event);
        if (result) bar.taskResult(ctx, result.toolCallId, result.isError);
      });
    });
  };
}

export default createSwarmExtension({ bridge: runPy });
