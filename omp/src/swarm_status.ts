/**
 * `/swarm-status <correlation-id>` (14.7 Wave 1, Task 4): a read-only host view of one plan. It makes exactly one
 * bridge call, `orch_status` with `--correlation-id`, and shows its summary. It never falls back to the latest
 * correlation, writes nothing, and starts no model turn (no sendUserMessage, no provider call).
 */
import type { Bridge, BridgeResult } from "./bridge.ts";
import type { CommandContext, CommandDefinition } from "./omp-api.ts";
import { ID_PATTERN } from "./tools.ts";

export const SWARM_STATUS_USAGE = "usage: /swarm-status <correlation-id>";

/** Exactly one correlation id, surrounding whitespace ignored; anything else is a usage error and never reaches the bridge. */
export function parseSwarmStatusArgs(args: string): { correlationId: string } | { error: string } {
  const correlationId = args.trim();
  if (correlationId === "" || /\s/.test(correlationId)) return { error: SWARM_STATUS_USAGE };
  if (!new RegExp(ID_PATTERN).test(correlationId)) {
    return { error: `/swarm-status refused: invalid correlation id ${JSON.stringify(correlationId)}` };
  }
  return { correlationId };
}

/**
 * Shows the text to the operator. With a UI it is a notification; without one (`-p`, rpc) it goes to stderr, never
 * stdout, which `--mode json` owns. Unlike /swarm, the info level is shown too, since the result is the whole point.
 */
function show(ctx: CommandContext, text: string, level: "info" | "warning" | "error"): void {
  if (ctx.hasUI) ctx.ui?.notify(text, level);
  else process.stderr.write(`[/swarm-status] ${text}\n`);
}

export function swarmStatusCommand(bridge: Bridge): CommandDefinition {
  return {
    description: "Show the AgentSwarm Task Store for one correlation id (read-only; no model turn)",
    async handler(args, ctx) {
      const parsed = parseSwarmStatusArgs(args);
      if ("error" in parsed) return show(ctx, parsed.error, "warning");
      let res: BridgeResult;
      try {
        res = await bridge({ script: "orch_status", args: [`--correlation-id=${parsed.correlationId}`], cwd: ctx.cwd });
      } catch (err) {
        return show(ctx, `/swarm-status: ${err instanceof Error ? err.message : String(err)}`, "error");
      }
      if (res.exitCode !== 0) {
        const err = res.json.error as { message?: unknown } | undefined;
        const detail =
          typeof err?.message === "string" ? err.message : typeof res.json.summary === "string" ? res.json.summary : JSON.stringify(res.json);
        return show(ctx, `/swarm-status: orch_status exit ${res.exitCode}: ${detail}`, "error");
      }
      return show(ctx, typeof res.json.summary === "string" ? res.json.summary : JSON.stringify(res.json), "info");
    },
  };
}
