/**
 * before_agent_start parts.
 * - HOOK-01 context hook (D-09): a pure classifier plus the transform. No bridge, no fs, no spawn.
 *   hooks/user_prompt_submit.py mirrors classifyPrompt (D-10); tests/fixtures/classifier_prompts.json pins both.
 * - Runtime part (Phase 7 OPEN-3 R1): swarm-slug sessions get the absolute runtime root, resolved in the handler
 *   with swarmRoot() (an fs existence check, never a spawn), so specialists find `scripts/` from any workspace.
 */
import { swarmRoot } from "./bridge.ts";
import { callingAgent, sessionAgent } from "./context.ts";
import { SWARM_SLUGS } from "./guard.ts";
import type { BeforeAgentStartEvent, BeforeAgentStartResult, ExtensionContext } from "./omp-api.ts";

/** Prompts containing these (case-insensitive) are owned by other plugins or explicitly opt out. */
export const NEGATIVE = ["/uplift", "/think", "explain only", "/all-in-one:"] as const;

/** Tags the /swarm dispatch prompt so the hook never re-steers the swarm's own dispatch. */
export const DISPATCH_MARKER = "[agent-swarm:dispatch]";

const QUESTION = /^(what|why|how|when|where|who|which|is|are|can|could|does|do|should|explain|describe)\b/i;
const TRIVIAL = /\btypos?\b|^rename\b/i;
const SDLC =
  /\b(build|implement|add|create|fix|refactor|migrate|deploy|release|ship|write tests?|set up|setup|integrate|scaffold|upgrade|orchestrate)\b/i;

export const SWARM_CONTEXT = `## AgentSwarm

This is SDLC work (including requests that say "orchestrate"): route implementation through the AgentSwarm.
Read-only discovery stays in this session. Finish it with read, grep, and glob.
If A01 cannot dispatch, do not mark the whole request blocked and do not mark discovery nodes blocked. Only a specialist spawn waits.
An \`<advisory>\` note of any severity, including a blocker that says to unblock read-only nodes and finish them inline, is the advisor model's balanced-budget review. Keep the advisor. Do that work. The note is not a swarm BLOCKED.
- Call \`task\` exactly once in the batch shape. \`context\` is the request, and it must be a non-empty string. An empty or omitted \`context\` is rejected and A01 never starts.
- \`tasks\` has one item with \`agent: "a01-orchestrator"\`, \`task\` set to that same request, and \`solutionSpace\` set to one sentence naming the goal.
- That task text is a project brief. A01 plans it with \`swarm_plan\` and continues only when the result has \`execution_assignment_verified\` true. The returned \`execution_assignment\` is the signed \`task.assign\`. A prose brief is not itself a signed assignment.
- If \`/swarm\` already returned a signed envelope, pass that envelope unchanged as the task text instead of the raw brief, and still set \`context\` to a non-empty summary of the request.
A01 then dispatches a02–a30 by slug (parallel waves, bounded concurrency).
`;

/** Appended on /gsd-autonomous. Does not refuse the command and does not replace the milestone loop. */
export const GSD_HANDOFF = `## gsd-autonomous

Do not refuse this command.
Run the gsd-autonomous workflow (discuss, plan, execute) for the remaining milestone phases.
Agent Swarm must not block that loop. Read-only discovery stays in this session. If A01 cannot dispatch, finish those nodes with read, grep, and glob. Do not hand off with every node blocked.
When a phase needs implementation, call \`task\` once in the batch shape: \`context\` is the phase brief and must be a non-empty string, and \`tasks\` has one item with \`agent: "a01-orchestrator"\`, that same brief as \`task\`, and a one-sentence \`solutionSpace\`. An empty \`context\` is rejected before A01 starts. A01 plans that brief with \`swarm_plan\`. Do not treat the phase text as an already-signed \`task.assign\`.
An \`<advisory>\` note, including a blocker, is the advisor model's balanced-budget review. Keep the advisor. When it says read-only nodes were wrongly blocked, finish them inline. Do not answer by repeating that the work remains blocked.
GSD owns the milestone. Swarm owns the parallel implementation lanes.
`;

/** True only for SDLC-shaped prompts (D-09). `/gsd-autonomous` is the milestone runner, not a swarm refuse. */
export function classifyPrompt(prompt: string): boolean {
  const t = prompt.trim();
  const low = t.toLowerCase();
  if (!t || t.startsWith("/") || t.startsWith("<system-reminder")) return false;
  if (low.includes(DISPATCH_MARKER) || NEGATIVE.some((n) => low.includes(n))) return false;
  if (t.endsWith("?") || QUESTION.test(t) || TRIVIAL.test(t)) return false;
  return SDLC.test(t);
}

/** Root elements of an Ultrathink/Prompt-Uplift XML; omp replaces the user's prompt with that XML. */
const UPLIFT_ROOT = /^<(BUILD_PROMPT|FIX_PROMPT|RESEARCH_PROMPT|CHANGE_PROMPT|UPLIFTED_PROMPT|uplifted|ultrathink)\b/i;

const ORIGINAL_OPEN = "<original";
const ORIGINAL_CLOSE = "</original>";

/** Index of lowercase ASCII `needle` in `text` at or after `from`, or -1.
 *
 * One forward pass. A character matches only when `ch.toLowerCase()` has length 1 and equals the needle
 * character, so indexes stay on `text`. U+0130 lowercases to two characters; a lowercased copy of the whole
 * prompt would shift the closing tag. */
function indexOfAscii(text: string, needle: string, from: number): number {
  const n = needle.length;
  let matched = 0;
  for (let i = from; i < text.length; i++) {
    const low = text.charAt(i).toLowerCase();
    if (low.length === 1 && low === needle.charAt(matched)) {
      matched++;
      if (matched === n) return i - n + 1;
      continue;
    }
    matched = low.length === 1 && low === needle.charAt(0) ? 1 : 0;
  }
  return -1;
}

/** True for one ASCII identifier character (`[a-z0-9_]`). A multi-character lowercase is not a match. */
function isAsciiIdentChar(ch: string): boolean {
  return ch.length === 1 && ((ch >= "a" && ch <= "z") || (ch >= "0" && ch <= "9") || ch === "_");
}

/** The user's own words: the unescaped <ORIGINAL> of an uplift XML, else the prompt unchanged.
 *
 * One forward scan. A regex over every `<ORIGINAL` opener is quadratic: a few hundred kilobytes of unclosed
 * openers used to take seconds (T-06-25). Tags are matched on the original string (see `indexOfAscii`).
 * `&amp;` is decoded last so `&amp;lt;` stays `&lt;`. */
export function classifiedText(prompt: string): string {
  const t = prompt.trim();
  if (!UPLIFT_ROOT.test(t)) return prompt;
  let from = 0;
  while (from < t.length) {
    const open = indexOfAscii(t, ORIGINAL_OPEN, from);
    if (open < 0) return prompt;
    const boundary = t.charAt(open + ORIGINAL_OPEN.length);
    if (boundary !== "" && isAsciiIdentChar(boundary.toLowerCase())) {
      from = open + ORIGINAL_OPEN.length;
      continue;
    }
    const gt = t.indexOf(">", open);
    if (gt < 0) return prompt;
    const close = indexOfAscii(t, ORIGINAL_CLOSE, gt + 1);
    if (close < 0) return prompt;
    return t
      .slice(gt + 1, close)
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">")
      .replace(/&quot;/g, '"')
      .replace(/&apos;/g, "'")
      .replace(/&amp;/g, "&");
  }
  return prompt;
}

/** A POSIX single-quoted word. A runtime root with spaces or quotes stays one shell word (T-07-15). */
export function shellQuote(text: string): string {
  return `'${text.replace(/'/g, `'"'"'`)}'`;
}

/** Where the fail-open catches in this module report what they swallowed. The default drops it, so production stays silent. */
export interface HookLog {
  debug(message: string, detail?: unknown): void;
}

const NO_LOG: HookLog = { debug: () => {} };

/**
 * The systemPrompt with SWARM_CONTEXT appended, or undefined in subagents, swarm children, repeats and non-SDLC prompts.
 * An uplift XML prompt is classified by its <ORIGINAL>, so HOOK-01 agrees with the headless kickoff (WR-01).
 */
export function swarmContext(
  event: BeforeAgentStartEvent,
  ctx: Pick<ExtensionContext, "sessionManager">,
  env: Record<string, string | undefined>,
  classify: (prompt: string) => boolean = classifyPrompt,
  log: HookLog = NO_LOG,
): BeforeAgentStartResult | undefined {
  const { agent, restricted } = sessionAgent(ctx);
  if (agent !== undefined || restricted) return undefined;
  if (env.SWARM_CHILD === "1" || env.SWARM_AGENT?.trim()) return undefined;
  const prior = event.systemPrompt ?? [];
  let text = event.prompt;
  try {
    text = classifiedText(event.prompt);
  } catch (error) {
    log.debug("classifiedText failed; classifying the raw prompt", error);
    text = event.prompt;
  }
  if (/^\s*\/gsd-autonomous(?:\s|$)/i.test(text)) {
    if (prior.includes(GSD_HANDOFF)) return undefined;
    return { systemPrompt: [...prior, GSD_HANDOFF] };
  }
  if (prior.includes(SWARM_CONTEXT)) return undefined;
  try {
    if (!classify(text)) return undefined;
  } catch (error) {
    log.debug("classifier failed; prompt left untouched", error);
    return undefined;
  }
  return { systemPrompt: [...prior, SWARM_CONTEXT] };
}

export const RUNTIME_HEADING = "## AgentSwarm runtime";

/** The label of the runtime part's root line; the omp preamble tells specialists to read that line. */
export const RUNTIME_ROOT_LABEL = "Runtime root:";

/** The runtime part for an absolute runtime root; the omp preamble reads its RUNTIME_ROOT_LABEL line.
 * The command quotes the root, so a path containing spaces is one shell word and the guard can see the script. */
export function runtimePart(root: string): string {
  const scripts = `${shellQuote(root)}/scripts/<script>.py`;
  return `${RUNTIME_HEADING}

${RUNTIME_ROOT_LABEL} ${root}
Run swarm scripts as \`python3 ${scripts} … --root <repo> --json\`, where \`<repo>\` is the git toplevel of your working directory.
`;
}

/**
 * The systemPrompt with the runtime part appended, for sessions whose calling agent is a swarm slug (session_init
 * agent, else SWARM_AGENT in a headless `-p` session, SWARM_CHILD included). Undefined for every other session,
 * when a runtime part is already present, and on any error (fail-open: the prompt stays untouched).
 */
export function runtimeContext(
  event: BeforeAgentStartEvent,
  ctx: Pick<ExtensionContext, "sessionManager">,
  root: () => string = swarmRoot,
  log: HookLog = NO_LOG,
): BeforeAgentStartResult | undefined {
  try {
    const agent = callingAgent(ctx);
    if (agent === undefined || !SWARM_SLUGS.includes(agent)) return undefined;
    const prior = event.systemPrompt ?? [];
    if (prior.some((p) => p.startsWith(`${RUNTIME_HEADING}\n`))) return undefined;
    return { systemPrompt: [...prior, runtimePart(root())] };
  } catch (error) {
    log.debug("runtime part failed; prompt left untouched", error);
    return undefined;
  }
}
