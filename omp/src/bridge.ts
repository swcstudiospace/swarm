/**
 * The single Python bridge (TOOL-06, D-06..D-08). Every swarm tool reaches the canonical
 * `scripts/<script>.py` through `runPy`; Python stays the only authority for state and validation.
 *
 * D-06 scope note: "exactly one spawner" covers the python script children, and `runPy` is the only
 * Bun.spawn call under omp/src. The git toplevel lookup (`gitToplevel`/`swarmDir` in ./paths.ts) is a
 * read-only Bun.spawnSync of git, called only at execute time; it is not a second python spawner.
 *
 * Nothing here runs at import: the root and SWARM_DIR are resolved inside each call.
 */
import { existsSync, mkdirSync, realpathSync, writeFileSync } from "node:fs";
import { isAbsolute, resolve } from "node:path";
import { swarmDir } from "./paths.ts";

export type Script = "orch_plan" | "orch_status" | "orch_assign" | "qa_gate" | "rev_gate" | "sec_gate" | "rel_plan" | "swarm_tool";
export type ErrorCode = "E-INPUT" | "E-TIMEOUT" | "E-DEP" | "E-CAPACITY" | "E-CONTRACT" | "E-POLICY" | "E-INTERNAL";

const ERROR_CODES: Record<ErrorCode, true> = {
  "E-INPUT": true, "E-TIMEOUT": true, "E-DEP": true, "E-CAPACITY": true, "E-CONTRACT": true, "E-POLICY": true, "E-INTERNAL": true,
};

export class SwarmToolError extends Error {
  constructor(readonly code: ErrorCode, detail: string, readonly details?: unknown) {
    // swarm/errors.py messages already start "E-XXX: " — never double-prefix.
    // Policy refusals keep the code for callers but must not put that token in Error.message.
    const prefixed = detail.startsWith(`${code}:`) ? detail : `${code}: ${detail}`;
    const message =
      code === "E-POLICY"
        ? prefixed.replace(/^E-POLICY:\s*/, "").replaceAll("E-POLICY", "").trim() || "refused"
        : prefixed;
    super(message);
    this.name = "SwarmToolError";
  }
}

export interface BridgeRequest {
  script: Script;
  args: string[];
  cwd: string;
  signal?: AbortSignal;
  timeoutMs?: number;
  /** The calling session's in-flight registry (set by the extension factory); the child is swept with it. */
  inflight?: Inflight;
}
export interface BridgeResult {
  exitCode: 0 | 1;
  json: Record<string, unknown>;
  swarmDir: string;
}
export type Bridge = (req: BridgeRequest) => Promise<BridgeResult>;

/** Internal request: any script stem under `<SWARM_ROOT>/scripts/` (tools use the `Script` union). */
export type ScriptRequest = Omit<BridgeRequest, "script"> & { script: string };

const DEFAULT_TIMEOUT_MS = 600_000;
const KILL_GRACE_MS = 3_000;
const TAIL = 400;
const SCRIPT_NAME = /^[a-z][a-z0-9_]*$/;
const STRIPPED_ENV = ["SWARM_TASK_ID", "SWARM_CORRELATION_ID"]; // argparse defaults (swarm/script_base.py)

type StopReason = "abort" | "timeout";
/** Stop handles of one omp session's in-flight children (WR-01): one registry per extension factory call. */
export type Inflight = Set<(why: StopReason) => void>;

/** env SWARM_ROOT (trimmed, when it holds scripts/orch_plan.py) → realpath of the package's repo → E-DEP. */
export function swarmRoot(): string {
  const env = process.env.SWARM_ROOT?.trim();
  if (env && existsSync(resolve(env, "scripts", "orch_plan.py"))) return resolve(env);
  const pkg = resolve(import.meta.dir, "..", "..");
  if (existsSync(resolve(pkg, "scripts", "orch_plan.py"))) return realpathSync(pkg);
  throw new SwarmToolError("E-DEP", "agent-swarm scripts not found (no scripts/orch_plan.py); set SWARM_ROOT");
}

/** A deadline (omp `--max-time`, AbortSignal.timeout) aborts with a TimeoutError reason → E-TIMEOUT; anything else is a user cancel. */
function abortError(script: string, signal: AbortSignal): SwarmToolError {
  const reason: unknown = signal.reason;
  if (typeof reason === "object" && reason !== null && "name" in reason && reason.name === "TimeoutError") return new SwarmToolError("E-TIMEOUT", `${script} deadline exceeded: ${String(signal.reason)}`);
  return new SwarmToolError(
    "E-INTERNAL",
    `${script} cancelled; Task Store state may be partially applied — re-check with swarm_status before retrying`,
  );
}

function killGroup(pid: number, sig: "SIGTERM" | "SIGKILL"): void {
  try {
    process.kill(-pid, sig);
  } catch {} // group already gone
}

/** Group-kill one session's in-flight children (that session's `session_shutdown` handler). */
export function killInflight(inflight: Inflight): void {
  for (const stop of [...inflight]) stop("abort");
}

function parseObject(stdout: string): Record<string, unknown> | undefined {
  try {
    const v: unknown = JSON.parse(stdout);
    return typeof v === "object" && v !== null && !Array.isArray(v) ? (v as Record<string, unknown>) : undefined;
  } catch {
    return undefined;
  }
}

/**
 * Bridge-owned input file for script flags that take a path (`--ingest`, `--per-target-findings`); the model
 * never supplies a path (T-03-08). Writes `<SWARM_DIR>/results/<kind>-<sanitised toolCallId>.json` under the
 * same SWARM_DIR that `runScript` hands to python for `cwd` (D-08), and returns the absolute path.
 */
export function writeInputFile(cwd: string, kind: "ingest" | "findings", toolCallId: string, value: unknown): string {
  const dir = swarmDir(cwd);
  mkdirSync(resolve(dir, "results"), { recursive: true });
  try {
    writeFileSync(resolve(dir, ".gitignore"), "*", { flag: "wx" }); // D-11 (as swarm/paths.py): never overwrite
  } catch {}
  const path = resolve(dir, "results", `${kind}-${toolCallId.replace(/[^A-Za-z0-9._-]/g, "_").slice(0, 120)}.json`);
  writeFileSync(path, JSON.stringify(value, null, 2));
  return path;
}

/** Spawn `python3 <SWARM_ROOT>/scripts/<script>.py <args> --json` and map the exit contract (0/1/2) to a result or error. */
export async function runScript({ script, args, cwd, signal, timeoutMs = DEFAULT_TIMEOUT_MS, inflight }: ScriptRequest): Promise<BridgeResult> {
  if (signal?.aborted) throw abortError(script, signal);
  if (!SCRIPT_NAME.test(script)) throw new SwarmToolError("E-INPUT", `invalid script name ${JSON.stringify(script)}`);
  const root = swarmRoot();
  const path = resolve(root, "scripts", `${script}.py`);
  if (!existsSync(path)) throw new SwarmToolError("E-DEP", `script not found: scripts/${script}.py under SWARM_ROOT`);
  const dir = swarmDir(cwd); // D-08: env SWARM_DIR (absolute) → <git toplevel of cwd>/.swarm → <cwd>/.swarm
  const env: Record<string, string | undefined> = { ...process.env, SWARM_DIR: dir };
  // python runs in the runtime root, so a relative key reference must be made absolute against the session workspace here
  const keyFile = env.SWARM_ED25519_KEY_FILE;
  if (keyFile && !isAbsolute(keyFile)) env.SWARM_ED25519_KEY_FILE = resolve(cwd, keyFile);
  for (const key of STRIPPED_ENV) delete env[key];

  let child: Bun.Subprocess<"ignore", "pipe", "pipe">;
  try {
    child = Bun.spawn(["python3", path, ...args, "--json"], {
      cwd: root,
      env,
      stdin: "ignore",
      stdout: "pipe",
      stderr: "pipe",
      detached: true, // the child leads its own process group, so a group kill reaches grandchildren
    });
  } catch (e) {
    throw new SwarmToolError("E-DEP", `cannot start python3: ${e instanceof Error ? e.message : String(e)}`);
  }

  const pid = child.pid;
  let stopped: StopReason | undefined;
  let killTimer: ReturnType<typeof setTimeout> | undefined;
  const stop = (why: StopReason) => {
    if (stopped) return;
    stopped = why;
    killGroup(pid, "SIGTERM");
    // escalate if SIGTERM is ignored; cleared once the child has exited, so it never hits a reused pgid (WR-02)
    killTimer = setTimeout(() => killGroup(pid, "SIGKILL"), KILL_GRACE_MS);
    killTimer.unref();
  };
  inflight?.add(stop);
  const onAbort = () => stop("abort");
  signal?.addEventListener("abort", onAbort, { once: true });
  const timer = setTimeout(() => stop("timeout"), timeoutMs);

  try {
    const [stdout, stderr, code] = await Promise.all([
      new Response(child.stdout).text(),
      new Response(child.stderr).text(),
      child.exited,
    ]);
    // D-07: never return after a cancellation — omp would record a half-applied mutation as success.
    if (signal?.aborted) throw abortError(script, signal);
    if (stopped === "timeout") throw new SwarmToolError("E-TIMEOUT", `${script} exceeded ${timeoutMs} ms and was killed`);
    if (stopped === "abort") throw new SwarmToolError("E-INTERNAL", `${script} cancelled (omp session shutdown); re-check with swarm_status`);

    const json = parseObject(stdout);
    if (code === 2 && !stdout.trim()) throw new SwarmToolError("E-INPUT", `${script} usage error: ${stderr.trim().slice(-TAIL)}`);
    if (!json) throw new SwarmToolError("E-INTERNAL", `${script} exit ${code}, stdout is not a JSON object: ${(stdout.trim() || stderr.trim()).slice(-TAIL)}`);
    if (code === 0) return { exitCode: 0, json, swarmDir: dir };
    if (code === 1 && json.status === "fail") return { exitCode: 1, json, swarmDir: dir };
    if (code === 2) {
      const err = json.error as { code?: unknown; message?: unknown } | undefined;
      if (err && typeof err.code === "string" && Object.hasOwn(ERROR_CODES, err.code)) {
        throw new SwarmToolError(err.code as ErrorCode, typeof err.message === "string" ? err.message : "unknown error", json);
      }
    }
    throw new SwarmToolError("E-INTERNAL", `${script} unexpected exit ${code}: ${(stderr.trim() || stdout.trim()).slice(-TAIL)}`, json);
  } finally {
    clearTimeout(timer);
    clearTimeout(killTimer);
    signal?.removeEventListener("abort", onAbort);
    inflight?.delete(stop);
  }
}

/** The production bridge: scripts limited to the `Script` union. */
export const runPy: Bridge = (req) => runScript(req);
