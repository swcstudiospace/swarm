# omp contract freeze (P1 / Wave A)

Read-only snapshot of the omp host contract: `omp/src/bridge.ts`, `omp/src/tools.ts`, and the one Python spawner outside them, `omp/src/commands.ts`. No file under `omp/` was modified. No bun, python, or test command was run. Every value below is copied from the source; `vectors.json` holds the machine-readable copy (ids `C-*`, `A-*`, `X-*`).

Line numbers are the line numbers of the files as read. Quotes are verbatim.

Vector format: each `vectors.json` value is the exact source spelling, stored as a string (numbers keep their underscores, e.g. `600_000`). `test_vectors_replay.py` matches a string value, or each element of a list value, as a substring of the cited line and the three lines after it; any other value is compared as `json.dumps`. Dict, object and multi-token records are split into one record per source token (`-01`, `-02`, ...), each citing its own line.

---

## 1. Spawned Python commands

There are two Python spawn paths.

### 1.1 Generic bridge spawn (`runScript`), the only `Bun.spawn` of python

Source `omp/src/bridge.ts:136-142`:

```ts
    child = Bun.spawn(["python3", path, ...args, "--json"], {
      cwd: root,
      env,
      stdin: "ignore",
      stdout: "pipe",
      stderr: "pipe",
      detached: true, // the child leads its own process group, so a group kill reaches grandchildren
    });
```

Path, `omp/src/bridge.ts:125`:

```ts
  const path = resolve(root, "scripts", `${script}.py`);
```

Root, `omp/src/bridge.ts:124`: `  const root = swarmRoot();`

Env, `omp/src/bridge.ts:127-132`:

```ts
  const dir = swarmDir(cwd); // D-08: env SWARM_DIR (absolute) → <git toplevel of cwd>/.swarm → <cwd>/.swarm
  const env: Record<string, string | undefined> = { ...process.env, SWARM_DIR: dir };
  // python runs in the runtime root, so a relative key reference must be made absolute against the session workspace here
  const keyFile = env.SWARM_ED25519_KEY_FILE;
  if (keyFile && !isAbsolute(keyFile)) env.SWARM_ED25519_KEY_FILE = resolve(cwd, keyFile);
  for (const key of STRIPPED_ENV) delete env[key];
```

Doc line, `omp/src/bridge.ts:120`: `/** Spawn \`python3 <SWARM_ROOT>/scripts/<script>.py <args> --json\` and map the exit contract (0/1/2) to a result or error. */`

Script-name gate, `omp/src/bridge.ts:123`: `  if (!SCRIPT_NAME.test(script)) throw new SwarmToolError("E-INPUT", \`invalid script name ${JSON.stringify(script)}\`);`

Missing interpreter, `omp/src/bridge.ts:145`: `    throw new SwarmToolError("E-DEP", \`cannot start python3: ${e instanceof Error ? e.message : String(e)}\`);`

Resulting argv: `python3 <root>/scripts/<script>.py <args...> --json`, cwd = `<root>` (the realpath of the agent-swarm repo, `omp/src/bridge.ts:68-70`), no shell.

### 1.2 Per-tool argv (all reach `runScript` through `bridge(...)`)

Every argv begins with `rootArg(ctx)`, `omp/src/tools.ts:136`:

```ts
  return `--root=${gitToplevel(ctx.cwd) ?? ctx.cwd}`;
```

| Record id | Caller (source) | Script | Argv, in order (conditional tokens marked) |
|---|---|---|---|
| A-ORCH-PLAN | `tools.ts:144-150` (`planArgs`), used by `tools.ts:316` (swarm_plan) and `commands.ts:265-266` (/swarm) | `orch_plan` | `--root=…`, `--brief-text=<brief>`, `--pattern=<p>`, `--risk-class=<r>`, then if set: `--prefix=`, `--correlation-id=`, `--priority=`, one `--acceptance=` per item, `--slices-json=<JSON>`, `--dry-run` |
| A-ORCH-STATUS-STATUS | `tools.ts:212-214` (swarm_status) | `orch_status` | `--root=…`, then if set `--correlation-id=` |
| A-ORCH-STATUS-INGEST | `tools.ts:340-342` (swarm_ingest) | `orch_status` | `--root=…`, `--task-id=`, `--ingest=<file>` |
| A-ORCH-STATUS-TRANSITION | `tools.ts:367-369` (swarm_transition) | `orch_status` | `--root=…`, `--transition`, `<task_id>`, `<state>` (two space-separated values), `--reason=`, then if dry_run `--dry-run` |
| A-ORCH-ASSIGN | `tools.ts:248-252` (swarm_assignment) | `orch_assign` | `--root=…`, `--task-id=`, `--correlation-id=` |
| A-GATE | `tools.ts:406-427` (swarm_gate) | `GATE_SCRIPTS[gate]` | `--root=…`, `--task-id=`, `--correlation-id=`, then if per_target_findings given `--per-target-findings=<file>`, then if dry_run `--dry-run`; timeoutMs = QA_GATE_TIMEOUT_MS for quality only |
| A-CATALOG | `tools.ts:461-465` (swarm_catalog) | `swarm_tool` | `--root=…`, `catalog`, `--caller=`, then if set `--kind=`, `--query=` |
| A-TOOL-CALL | `tools.ts:500-503` (swarm_tool_call) | `swarm_tool` | `--root=…`, `call`, `--caller=`, `--name=`, `--input=<JSON>`, then if plan mode `--plan-mode` |

Quoted argv lines:

`omp/src/tools.ts:144` `  const args = [rootArg(ctx), \`--brief-text=${params.brief}\`, \`--pattern=${params.pattern}\`, \`--risk-class=${params.risk_class}\`];`
`omp/src/tools.ts:145` `  if (params.prefix) args.push(\`--prefix=${params.prefix}\`);`
`omp/src/tools.ts:146` `  if (params.correlation_id) args.push(\`--correlation-id=${params.correlation_id}\`);`
`omp/src/tools.ts:147` `  if (params.priority) args.push(\`--priority=${params.priority}\`);`
`omp/src/tools.ts:148` `  for (const item of params.acceptance ?? []) args.push(\`--acceptance=${item}\`);`
`omp/src/tools.ts:149` `  if (params.slices?.length) args.push(\`--slices-json=${JSON.stringify(params.slices)}\`);`
`omp/src/tools.ts:150` `  if (params.dry_run) args.push("--dry-run");`
`omp/src/tools.ts:316` `      return toolResult(await bridge({ script: "orch_plan", args: planArgs(ctx, params), cwd: ctx.cwd, signal }));`
`omp/src/commands.ts:265` `        const args = planArgs(ctx, { ...parsed, correlation_id: swarmCorrelationId(parsed) });`
`omp/src/commands.ts:266` `        res = await bridge({ script: "orch_plan", args, cwd: ctx.cwd });`
`omp/src/tools.ts:212` `      const args = [rootArg(ctx)];`
`omp/src/tools.ts:213` `      if (params.correlation_id) args.push(\`--correlation-id=${params.correlation_id}\`);`
`omp/src/tools.ts:214` `      const res = await bridge({ script: "orch_status", args, cwd: ctx.cwd, signal });`
`omp/src/tools.ts:340` `      const file = writeInputFile(ctx.cwd, "ingest", toolCallId, params.result);`
`omp/src/tools.ts:341` `      const args = [rootArg(ctx), \`--task-id=${params.task_id}\`, \`--ingest=${file}\`];`
`omp/src/tools.ts:366-367` `      // --transition takes two space-form values (nargs=2): ...` / `      const args = [rootArg(ctx), "--transition", params.task_id, params.state, \`--reason=${params.reason}\`];`
`omp/src/tools.ts:368` `      if (params.dry_run) args.push("--dry-run");`
`omp/src/tools.ts:250` `          args: [rootArg(ctx), \`--task-id=${params.task_id}\`, \`--correlation-id=${params.correlation_id}\`],`
`omp/src/tools.ts:407` `      const script = GATE_SCRIPTS[params.gate];`
`omp/src/tools.ts:421` `      const args = [rootArg(ctx), \`--task-id=${params.task_id}\`, \`--correlation-id=${params.correlation_id}\`];`
`omp/src/tools.ts:423` `        args.push(\`--per-target-findings=${writeInputFile(ctx.cwd, "findings", toolCallId, params.per_target_findings)}\`);`
`omp/src/tools.ts:425` `      if (params.dry_run) args.push("--dry-run");`
`omp/src/tools.ts:426` `      const timeoutMs = params.gate === "quality" ? QA_GATE_TIMEOUT_MS : undefined;`
`omp/src/tools.ts:427` `      return toolResult(await bridge({ script, args, cwd: ctx.cwd, signal, timeoutMs }));`
`omp/src/tools.ts:462` `      const args = [rootArg(ctx), "catalog", \`--caller=${caller}\`];`
`omp/src/tools.ts:463` `      if (params.kind !== undefined) args.push(\`--kind=${params.kind}\`);`
`omp/src/tools.ts:464` `      if (params.query !== undefined) args.push(\`--query=${params.query}\`);`
`omp/src/tools.ts:500` `      const args = [rootArg(ctx), "call", \`--caller=${caller}\`, \`--name=${params.name}\`, \`--input=${input}\`];`
`omp/src/tools.ts:502` `      if (inPlanMode(ctx)) args.push("--plan-mode");`
`omp/src/tools.ts:503` `      const res = await bridge({ script: "swarm_tool", args, cwd: ctx.cwd, signal });`

Note: the `/swarm` orch_plan call (`commands.ts:266`) passes no `signal` and no `timeoutMs`, so it gets `DEFAULT_TIMEOUT_MS` (see §4).

### 1.3 Second Python spawner: `/swarm` startup record (outside `runScript`)

`omp/src/commands.ts:250`:

```ts
        const lres = spawnSync("python3", ["-c", SWARM_LIFECYCLE_CODE, r, earlyCorr, ctx.cwd], { encoding: "utf8", cwd: ctx.cwd, env: { ...process.env, SWARM_ROOT: r } });
```

`omp/src/commands.ts:7`: `import { spawnSync } from "node:child_process";`

Argv: `python3 -c <SWARM_LIFECYCLE_CODE> <r> <earlyCorr> <ctx.cwd>`. No `--root`, no `--json`, no timeout, no group kill, no `detached`. The inline script is `omp/src/commands.ts:235` (single line; copied in full in `vectors.json` record `A-LIFECYCLE-PY`).

It is called once per `/swarm` before planning (`commands.ts:245-256`) and its failures are non-fatal (`commands.ts:257-259`). It is recorded here because the freeze must cover every spawned Python command.

---

## 2. `--json` stdout protocol

- Every `runScript` child gets `--json` appended last (`bridge.ts:136`).
- stdout and stderr are read to completion and the child is awaited (`bridge.ts:165-169`):

```ts
    const [stdout, stderr, code] = await Promise.all([
      new Response(child.stdout).text(),
      new Response(child.stderr).text(),
      child.exited,
    ]);
```

- stdout must be exactly one JSON **object** (not array, not scalar). `omp/src/bridge.ts:95-102`:

```ts
function parseObject(stdout: string): Record<string, unknown> | undefined {
  try {
    const v: unknown = JSON.parse(stdout);
    return typeof v === "object" && v !== null && !Array.isArray(v) ? (v as Record<string, unknown>) : undefined;
  } catch {
    return undefined;
  }
}
```

- Parse call, `omp/src/bridge.ts:175`: `    const json = parseObject(stdout);`
- Non-object stdout → `E-INTERNAL` (`bridge.ts:177`, §3).
- stderr is used only in error text: `usage error` (`bridge.ts:176`), `unexpected exit` (`bridge.ts:186`), and the non-JSON case (`bridge.ts:177`), each sliced to the last `TAIL` = 400 characters (`bridge.ts:57`: `const TAIL = 400;`).
- Success returns `{ exitCode: 0, json, swarmDir: dir }` (`bridge.ts:178`); exit 1 returns the same shape (`bridge.ts:179`).
- The `BridgeResult` type (`bridge.ts:45-49`) is `{ exitCode: 0 | 1; json: Record<string, unknown>; swarmDir: string }`.

---

## 3. Exit-to-verdict mapping

Evaluated in this order, `omp/src/bridge.ts:171-186`. Lines quoted verbatim:

```ts
    if (signal?.aborted) throw abortError(script, signal);                        // 171
    if (stopped === "timeout") throw new SwarmToolError("E-TIMEOUT", `${script} exceeded ${timeoutMs} ms and was killed`);   // 172
    if (stopped === "abort") throw new SwarmToolError("E-INTERNAL", `${script} cancelled (omp session shutdown); re-check with swarm_status`); // 173
    const json = parseObject(stdout);                                              // 175
    if (code === 2 && !stdout.trim()) throw new SwarmToolError("E-INPUT", `${script} usage error: ${stderr.trim().slice(-TAIL)}`); // 176
    if (!json) throw new SwarmToolError("E-INTERNAL", `${script} exit ${code}, stdout is not a JSON object: ${(stdout.trim() || stderr.trim()).slice(-TAIL)}`); // 177
    if (code === 0) return { exitCode: 0, json, swarmDir: dir };                  // 178
    if (code === 1 && json.status === "fail") return { exitCode: 1, json, swarmDir: dir }; // 179
    if (code === 2) {                                                              // 180
      const err = json.error as { code?: unknown; message?: unknown } | undefined; // 181
      if (err && typeof err.code === "string" && Object.hasOwn(ERROR_CODES, err.code)) { // 182
        throw new SwarmToolError(err.code as ErrorCode, typeof err.message === "string" ? err.message : "unknown error", json); // 183
      }                                                                            // 184
    }
    throw new SwarmToolError("E-INTERNAL", `${script} unexpected exit ${code}: ${(stderr.trim() || stdout.trim()).slice(-TAIL)}`, json); // 186
```

Abort reason, `omp/src/bridge.ts:77`:

```ts
  if (typeof reason === "object" && reason !== null && "name" in reason && reason.name === "TimeoutError") return new SwarmToolError("E-TIMEOUT", `${script} deadline exceeded: ${String(signal.reason)}`);
```

Other abort, `omp/src/bridge.ts:78-81`:

```ts
  return new SwarmToolError(
    "E-INTERNAL",
    `${script} cancelled; Task Store state may be partially applied — re-check with swarm_status before retrying`,
  );
```

Summary table (the source is the authority; this restates it):

| Condition (evaluated in order) | Result | Source |
|---|---|---|
| signal aborted, reason `TimeoutError` | throw `E-TIMEOUT` | bridge.ts:77 |
| signal aborted, other reason | throw `E-INTERNAL` | bridge.ts:78-81 |
| stopped by timeout | throw `E-TIMEOUT` | bridge.ts:172 |
| stopped by abort (session shutdown) | throw `E-INTERNAL` | bridge.ts:173 |
| exit 2, empty stdout | throw `E-INPUT` | bridge.ts:176 |
| stdout not a JSON object | throw `E-INTERNAL` | bridge.ts:177 |
| exit 0 | `BridgeResult{exitCode:0}`; no check on `json.status` | bridge.ts:178 |
| exit 1 and `json.status === "fail"` | `BridgeResult{exitCode:1}` → tool text `FAIL: <summary>` | bridge.ts:179; tools.ts:157 |
| exit 2 with `json.error.code` in `ERROR_CODES` | throw that code | bridge.ts:180-183 |
| anything else | throw `E-INTERNAL` | bridge.ts:186 |

`FAIL:` prefix, `omp/src/tools.ts:157`:

```ts
  const text = [res.exitCode === 1 ? `FAIL: ${summary}` : summary, ...extra].join("\n");
```

Verdict semantics: the bridge does not compute a verdict. `omp/src/tools.ts:34` states "the script derives and signs the verdict." Exit 1 with `status: "fail"` is data for the agent, not an error (`omp/src/tools.ts:154`: `/** Exit 1 (status fail: a failing verdict, a release freeze) is data the agent must read, so it is returned with \`FAIL:\`. */`).

Other consumers of the exit code:
- Assignment: `omp/src/tools.ts:170`: `  if (res.exitCode !== 0 || result.status !== "ok" || result.assignment_verified !== true ||`
- `/swarm`: `omp/src/commands.ts:271`: `      if (res.exitCode !== 0) {` and `:275` `        return notify(ctx, \`/swarm: orch_plan exit ${res.exitCode}: ${detail}\`, "error");`

Gap, not a defect: exit 0 is accepted with any JSON object, regardless of `json.status`. Only the assignment path checks `status === "ok"` (`tools.ts:170`).

---

## 4. Group-kill behaviour

1. **Process group.** `detached: true` at `omp/src/bridge.ts:142`: `      detached: true, // the child leads its own process group, so a group kill reaches grandchildren`.
2. **Kill primitive.** `omp/src/bridge.ts:84-88`:

```ts
function killGroup(pid: number, sig: "SIGTERM" | "SIGKILL"): void {
  try {
    process.kill(-pid, sig);
  } catch {} // group already gone
}
```

   (`-pid` addresses the process group; line 86 is `    process.kill(-pid, sig);`.)

3. **Stop handler.** `omp/src/bridge.ts:151-158`:

```ts
  const stop = (why: StopReason) => {
    if (stopped) return;
    stopped = why;
    killGroup(pid, "SIGTERM");
    // escalate if SIGTERM is ignored; cleared once the child has exited, so it never hits a reused pgid (WR-02)
    killTimer = setTimeout(() => killGroup(pid, "SIGKILL"), KILL_GRACE_MS);
    killTimer.unref();
  };
```

4. **Escalation.** SIGTERM first, SIGKILL after `KILL_GRACE_MS` = `3_000` (`omp/src/bridge.ts:56`: `const KILL_GRACE_MS = 3_000;`). Timer is `unref()`'d (`bridge.ts:157`) and cleared in `finally` (`bridge.ts:189`).
5. **Triggers.**
   - Deadline: `omp/src/bridge.ts:162`: `  const timer = setTimeout(() => stop("timeout"), timeoutMs);`; default `DEFAULT_TIMEOUT_MS = 600_000` (`bridge.ts:55`). `swarm_gate` quality overrides with `QA_GATE_TIMEOUT_MS` (`tools.ts:426`).
   - Abort: `omp/src/bridge.ts:160-161`: `  const onAbort = () => stop("abort");` / `  signal?.addEventListener("abort", onAbort, { once: true });`
   - Session shutdown: `omp/src/bridge.ts:91-93`: `export function killInflight(inflight: Inflight): void {` / `  for (const stop of [...inflight]) stop("abort");` / `}`. Registry insert at `bridge.ts:159`: `  inflight?.add(stop);`.
6. **Cleanup.** `omp/src/bridge.ts:187-191`:

```ts
  } finally {
    clearTimeout(timer);
    clearTimeout(killTimer);
    signal?.removeEventListener("abort", onAbort);
    inflight?.delete(stop);
  }
```

7. **Result after a kill.** A cancelled or timed-out child never returns success (D-07): `omp/src/bridge.ts:170` `    // D-07: never return after a cancellation — omp would record a half-applied mutation as success.` Mapping is in §3.

Not covered by the group kill: the `spawnSync` lifecycle child in `commands.ts:250` (§1.3). It has no timeout and no detached group.

---

## 5. Mirrored constants

Each entry quotes the source line(s). `vectors.json` holds the value.

### 5.1 `ID_PATTERN` and `ID`: `omp/src/tools.ts:18-19`

```ts
export const ID_PATTERN = "^(?!.*\\.\\.)[A-Za-z0-9][A-Za-z0-9._-]{0,127}$";
const ID = { type: "string", pattern: ID_PATTERN, minLength: 1 } as const;
```

Comment, `tools.ts:15-17`: `Schema pattern for ids that become argv values and (task ids) file names: mirrors swarm/script_base.py TASK_ID_RE + no "..", which python enforces (CR-01). Never dash-leading, so argparse never reads a flag.`

### 5.2 The 14 STATES: `omp/src/tools.ts:21-25`

```ts
/** The 14 Task Store states (swarm/taskstore.py:22-36 TaskState). */
const STATES = [
  "CREATED", "VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS", "IN_REVIEW", "CHANGES_REQUESTED",
  "APPROVED", "DONE", "BLOCKED", "FAILED", "RETRY", "ESCALATED", "CANCELLED",
] as const;
```

Count verified: 7 + 7 = 14.

### 5.3 The 7 PATTERNS: `omp/src/tools.ts:27-30`

```ts
/** orch_plan.py choices: --pattern (PATTERNS only: `custom` needs --plan <path>, which no tool exposes), --risk-class
 * (taskstore.py GATES_BY_RISK), --priority. */
export const PATTERNS = ["feature", "hotfix", "dependency", "cloud-native", "cluster", "web3", "parallel"] as const;
export const RISK_CLASSES = ["low", "medium", "high"] as const;
const PRIORITIES = ["P0", "P1", "P2", "P3"] as const;
```

Count verified: 7.

### 5.4 `GATE_SCRIPTS`: `omp/src/tools.ts:33-35`

```ts
/** Gate → script (parity mirror of swarm/verdicts.py GATE_SCRIPTS; omp/test/gate.test.ts pins equal);
 * the script derives and signs the verdict. */
const GATE_SCRIPTS = { quality: "qa_gate", review: "rev_gate", security: "sec_gate", release: "rel_plan" } as const satisfies Record<string, Script>;
```

Parity check is asserted in the source by `omp/test/gate.test.ts` (named in the comment; not read here).

### 5.5 `SEVERITIES` and BLOCKING: `omp/src/tools.ts:40-41`

```ts
/** Finding severities (copied from swarm/gates.py:10 SEVERITIES); BLOCKING_SEVERITY = "major". */
const SEVERITIES = ["info", "minor", "major", "critical", "blocker"] as const;
```

BLOCKING maps to major **only in this comment**. Grep of `omp/src` for `BLOCKING` finds exactly one match (`tools.ts:40`); no identifier, export, or branch implements it. The fail threshold in code is the list order, and the gate descriptions state it in prose: `tools.ts:49` `severity: { type: "string", enum: SEVERITIES, description: "major, critical or blocker fails the target" },`.

### 5.6 `QA_GATE_TIMEOUT_MS`: `omp/src/tools.ts:42-43`

```ts
/** qa_gate's own --timeout default (1800 s) plus a margin, so the bridge never kills a test run first. */
const QA_GATE_TIMEOUT_MS = (1800 + 60) * 1000;
```

Source expression copied as the value: `(1800 + 60) * 1000`. Its evaluation is 1 860 000 ms, which is arithmetic on the literal expression. Used only at `tools.ts:426`.

### 5.7 FINDING schema: `omp/src/tools.ts:45-60`

```ts
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
```

The schema is used as `per_target_findings` item schema at `omp/src/tools.ts:395` (`additionalProperties: { type: "array", items: FINDING }`). `vectors.json` `C-FINDING-SCHEMA` has the `enum` resolved to the SEVERITIES list.

### 5.8 The 100 KB cap: `omp/src/tools.ts:66-67`

```ts
/** One argv value is capped by the kernel at 128 KiB; swarm_tool.py enforces the same 100 000 byte ceiling. */
const MAX_TOOL_INPUT_BYTES = 100_000;
```

Enforced at `omp/src/tools.ts:496-498`:

```ts
      const input = JSON.stringify(params.input);
      if (Buffer.byteLength(input, "utf8") > MAX_TOOL_INPUT_BYTES) {
        throw new SwarmToolError("E-INPUT", `swarm_tool_call input exceeds ${MAX_TOOL_INPUT_BYTES} bytes`);
```

The cap is 100 000 **bytes** (UTF-8 byte length of the JSON input), inclusive comparison is `>`, so 100 000 is allowed.

### 5.9 Timeouts and tail length

`omp/src/bridge.ts:55-57`:

```ts
const DEFAULT_TIMEOUT_MS = 600_000;
const KILL_GRACE_MS = 3_000;
const TAIL = 400;
```

### 5.10 Other tool-level constants (mirrors used in argv and schemas)

`omp/src/tools.ts:31`: `const PRIORITIES = ["P0", "P1", "P2", "P3"] as const;` (also `tools.ts:30`: `export const RISK_CLASSES = ["low", "medium", "high"] as const;`)

`omp/src/tools.ts:62-65`:

```ts
/** swarm_tool.py --kind choices. */
const CATALOG_KINDS = ["tools", "skills", "all"] as const;
/** Registry tool names (swarm/tool_registry.py `_conventional_name`): lowercase, digits, underscore. */
const TOOL_NAME_PATTERN = "^[a-z][a-z0-9_]{0,63}$";
```

### 5.11 Bridge constants

`omp/src/bridge.ts:15-16`:

```ts
export type Script = "orch_plan" | "orch_status" | "orch_assign" | "qa_gate" | "rev_gate" | "sec_gate" | "rel_plan" | "swarm_tool";
export type ErrorCode = "E-INPUT" | "E-TIMEOUT" | "E-DEP" | "E-CAPACITY" | "E-CONTRACT" | "E-POLICY" | "E-INTERNAL";
```

`omp/src/bridge.ts:18-20`:

```ts
const ERROR_CODES: Record<ErrorCode, true> = {
  "E-INPUT": true, "E-TIMEOUT": true, "E-DEP": true, "E-CAPACITY": true, "E-CONTRACT": true, "E-POLICY": true, "E-INTERNAL": true,
};
```

`omp/src/bridge.ts:58-59`:

```ts
const SCRIPT_NAME = /^[a-z][a-z0-9_]*$/;
const STRIPPED_ENV = ["SWARM_TASK_ID", "SWARM_CORRELATION_ID"]; // argparse defaults (swarm/script_base.py)
```

---

## 6. Schemas used on the wire (summary)

- `ID` (`tools.ts:19`): string, `pattern` = `ID_PATTERN`, `minLength` 1. Applied to task_id, correlation_id, prefix.
- `FINDING` (`tools.ts:45-60`): §5.7.
- `swarm_transition.state`: `enum: STATES` (`tools.ts:357`).
- `swarm_plan.pattern`: `enum: PATTERNS` (`tools.ts:282`). `swarm_plan.slices[].id`: `^[a-z][a-z0-9-]{0,31}$` (`tools.ts:293`).

---

## 7. Findings to resolve before P2 (not fixed in this phase)

1. **Second Python spawner.** `omp/src/commands.ts:250` calls `spawnSync("python3", ...)` outside `runPy`. The comment at `omp/src/bridge.ts:5-6` says "`runPy` is the only Bun.spawn call under omp/src" (true: `bridge.ts:136` is the only `Bun.spawn(python)`), and `bridge.ts:5` says the "exactly one spawner" covers the python script children. `commands.ts:250` is a python child that is not one of them. Decide whether the lifecycle write belongs behind `runPy`.
2. **`BLOCKING` has no code identifier.** Only the comment at `tools.ts:40`.
3. **Exit 0 is not status-checked** in the bridge (`bridge.ts:178`); only the assignment path checks `status === "ok"` (`tools.ts:170`).
4. **Exit 1 without `status: "fail"`** falls to `E-INTERNAL` (`bridge.ts:179` then `186`). That is the intended contract, recorded here so a test pins it.
5. **`/swarm` treats every non-zero exit as an error** (`commands.ts:271`), including exit 1 that the tool path would return as `FAIL:`. The two paths differ on purpose or by accident; the freeze records the current behaviour.
6. **Argv limit wording.** `tools.ts:66` says the kernel caps one argv value at 128 KiB; the enforced application cap is 100 000 bytes (`tools.ts:67`). Both are recorded; the application cap is the one the bridge enforces.
7. **`commands.ts:235` is one long line.** The tool output truncated it at 512 bytes; the full text was read raw and is in `vectors.json` (`A-LIFECYCLE-PY`).

---

## 8. ExtensionAPI versioning

Status: **Default-frozen kept.** Decision recorded 2026-10-25 by the user, Wave A gate A3 (run ut-mvnofpc5-51f2ca97). The surface stays frozen without a version scheme, and the two options below are not chosen; the default holds until the omp host owners decide otherwise. Any later change to spawned-command bytes or to the stdout schema triggers an omp-mirror review before it lands (A3 done-criteria in `plan/execution-waves.md`).

- The open question is how the omp ExtensionAPI version is pinned against this bridge contract. Two options are on the table with the omp host owners: **pin by commit** (the contract records the omp commit it was frozen against) or **omission** (the contract does not record an ExtensionAPI version at all).
- **Default stays frozen.** Until the host owners decide, this document and `vectors.json` describe the current source as the contract. No version field is added and no version gate is added to the bridge or tools.
- No change in this phase depends on the decision. The decision is pending; it is not resolved here.

---

## 9. Surface pins (U6.3)

Each id below is a `vectors.json` record of kind `constant` (ids `S-*`). The Rust mirror is `crates/gsd-sdk/src/omp_surface_pins.rs`, and its tests replay every record. A string value is the literal the source spells, without the surrounding quotes. A number keeps its source underscores (`30_000`). A list is replayed element by element. The 30-slug array (`guard.ts:17-24`) and the four gate pairs are split into records that each cite a line whose four-line window holds their values.

- `S-ORCHESTRATOR` (`omp/src/guard.ts:14`): orchestrator slug `a01-orchestrator`, named in the task-depth-cap reason.
- `S-SWARM-STATE-REASON` (`omp/src/guard.ts:32`): refusal reason for swarm-state tools. Operators and the A01 dispatcher read it.
- `S-SWARM-ASSIGNMENT-REASON` (`omp/src/guard.ts:33`): refusal reason for swarm-assignment. Operators and the A01 dispatcher read it.
- `S-GUARD-ERROR-PREFIX` (`omp/src/guard.ts:35`): prefix of the refusal returned when the guard throws in a swarm session. index.ts appends the error message and `)`.
- `S-SWARM-SLUGS-01` (`omp/src/guard.ts:18`): `SWARM_SLUGS` elements 1 to 20, `a01-orchestrator` through `a20-finops`.
- `S-SWARM-SLUGS-02` (`omp/src/guard.ts:22`): `SWARM_SLUGS` elements 21 to 30, `a21-cluster-ops` through `a30-custody-compliance`.
- `S-RULE-ID-01` (`omp/src/guard.ts:2930`): rule id `assignment-script-shell` of the `RULES` table.
- `S-RULE-ID-02` (`omp/src/guard.ts:2943`): rule id `orch-state-shell`.
- `S-RULE-ID-03` (`omp/src/guard.ts:2959`): rule id `signer-file-shell`.
- `S-RULE-ID-04` (`omp/src/guard.ts:2981`): rule id `protected-path-shell`.
- `S-RULE-ID-05` (`omp/src/guard.ts:3012`): rule id `gate-script-shell`.
- `S-RULE-ID-06` (`omp/src/guard.ts:3062`): rule id `git-force-push`. Rule ids are read by the A01 dispatcher and kept stable.
- `S-GATE-AGENTS-01` (`omp/src/context.ts:15`): `GATE_AGENTS` pair `["quality", "a08-qa"]`, the only agent that runs the quality gate.
- `S-GATE-AGENTS-02` (`omp/src/context.ts:15`): `GATE_AGENTS` pair `["review", "a09-reviewer"]`.
- `S-GATE-AGENTS-03` (`omp/src/context.ts:15`): `GATE_AGENTS` pair `["security", "a10-security"]`.
- `S-GATE-AGENTS-04` (`omp/src/context.ts:15`): `GATE_AGENTS` pair `["release", "a12-release"]`.
- `S-SWARM-AGENT-ENV` (`omp/src/context.ts:33`): env var that names the calling agent in a headless `-p` session.
- `S-RUNTIME-HEADING` (`omp/src/hooks.ts:152`): heading of the runtime part. A prior part that starts with it means the runtime part is already present.
- `S-RUNTIME-ROOT-LABEL` (`omp/src/hooks.ts:155`): label of the runtime root line that the omp preamble reads.
- `S-HOOK01-HEADING` (`omp/src/hooks.ts:24`): heading of the HOOK-01 context part, the first line of `SWARM_CONTEXT`.
- `S-DISPATCH-MARKER` (`omp/src/hooks.ts:17`): marker that tags the `/swarm` dispatch prompt so the HOOK-01 hook never re-steers it.
- `S-SWARM-FLAG-PATTERN` (`omp/src/commands.ts:16`): the `--pattern=` flag of `/swarm`, from the usage line.
- `S-SWARM-FLAG-RISK` (`omp/src/commands.ts:16`): the `--risk=` flag of `/swarm`, from the usage line.
- `S-COMMAND-SWARM` (`omp/src/index.ts:39`): name the extension registers as a command, `swarm`.
- `S-TOOL-NAME-01` (`omp/src/tools.ts:197`): tool `swarm_status`. `buildTools` builds it and `index.ts:37` registers it.
- `S-TOOL-NAME-02` (`omp/src/tools.ts:220`): tool `swarm_assignment`.
- `S-TOOL-NAME-03` (`omp/src/tools.ts:269`): tool `swarm_plan`.
- `S-TOOL-NAME-04` (`omp/src/tools.ts:321`): tool `swarm_ingest`.
- `S-TOOL-NAME-05` (`omp/src/tools.ts:347`): tool `swarm_transition`.
- `S-TOOL-NAME-06` (`omp/src/tools.ts:374`): tool `swarm_gate`.
- `S-TOOL-NAME-07` (`omp/src/tools.ts:444`): tool `swarm_catalog`.
- `S-TOOL-NAME-08` (`omp/src/tools.ts:473`): tool `swarm_tool_call`.
- `S-BAR-WIDGET-KEY` (`omp/src/swarm_bar.ts:31`): widget key `agent-swarm` that the status bar mounts under.
- `S-BAR-ENV-SWITCH` (`omp/src/swarm_bar.ts:2567`): env var `SWARM_BAR`, the status bar switch.
- `S-BAR-OFF-VALUE` (`omp/src/swarm_bar.ts:2567`): `SWARM_BAR` value `off`, which disables the bar.
- `S-BAR-LINGER-S-ENV` (`omp/src/swarm_bar.ts:2839`): env var `SWARM_BAR_LINGER_S`, the linger time after a settled row in seconds.
- `S-BAR-LINGER-S-DEFAULT` (`omp/src/swarm_bar.ts:2839`): default linger of 20 s, used when the env value is not a positive number.
- `S-BAR-FAIL-LINGER-S-ENV` (`omp/src/swarm_bar.ts:2840`): env var `SWARM_BAR_FAIL_LINGER_S`, the linger time after a failed row in seconds.
- `S-BAR-FAIL-LINGER-S-DEFAULT` (`omp/src/swarm_bar.ts:2840`): default failed-row linger of 600 s.
- `S-BAR-REDUCED-MOTION-ENV` (`omp/src/swarm_bar.ts:2842`): env var `SWARM_BAR_REDUCED_MOTION`.
- `S-BAR-REDUCED-MOTION-ON` (`omp/src/swarm_bar.ts:2842`): `SWARM_BAR_REDUCED_MOTION` value `1`, which turns reduced motion on.
- `S-BAR-IDLE-POLL-ENV` (`omp/src/swarm_bar.ts:2845`): env var `SWARM_BAR_IDLE_POLL_MS`, the idle poll interval in ms.
- `S-BAR-IDLE-POLL-DEFAULT` (`omp/src/swarm_bar.ts:2508`): default idle poll interval of 2000 ms.
- `S-BAR-IDLE-POLL-MIN` (`omp/src/swarm_bar.ts:2509`): lower clamp of the idle poll interval, 500 ms.
- `S-BAR-IDLE-POLL-MAX` (`omp/src/swarm_bar.ts:2510`): upper clamp of the idle poll interval, 30_000 ms.

Not pinned here: the multi-line `DEPTH_REASON` and the `/swarm` dispatch prompt bodies, which are large template literals with computed parts.

**Addendum (2026-11-02, lanes merge `135d12b`).** The QA-gate lane changed `omp/src/tools.ts`: the QA timeout is now `QA_GATE_TIMEOUT_S = 1800` and `QA_GATE_CLEANUP_S = 60` (lines 43 to 44), and the per-call timeout is computed at lines 445 to 447 for the `quality` gate only. Records `C-QA-GATE-TIMEOUT-MS-01` and `A-GATE-07` were re-spelled to match; `QA_GATE_CLEANUP_S` is not a separate record, which keeps the 211-record count the replay test pins. The rest of this snapshot's line numbers are as read before the merge; `test_vectors_replay.py` is the authority for current citations.

**Addendum (2026-11-02, correction pass).**
- OSA-02 is reverted. `omp/src/bridge.ts` is back to its frozen form: a child stopped by timeout is E-TIMEOUT even when it exited 0 with JSON, and the row "stopped by timeout → E-TIMEOUT (`bridge.ts:172`)" and the §7 rule "a cancelled or timed-out child never returns success" hold again. Changing that contract needs an explicit decision; it is recorded in the audit status decision list, not applied.
- The OSA-01 fix adds the RULES row `unjudged-substitution` after `nested-shell-depth` in `omp/src/guard.ts`, which moved the guard lines six records cite. They now cite `guard.ts:2965` (S-RULE-ID-01), `2978` (-02), `2994` (-03), `3016` (-04), `3047` (-05) and `3097` (-06). The record count stays 211.
- `tests/omp_contract/test_vectors_replay.py` passes on this tree.

**Addendum (2026-11-03, OSA-03/OSA-04 re-spelling).** OSA-03 (`omp/src/bridge.ts`, `runScript`: a child that exits 0 with a JSON result settles on its exit within the `EXIT_PIPE_GRACE_MS` pipe grace of 500 ms; the `collect` helper accumulates the pipes) and OSA-04 (`omp/src/commands.ts`, the lifecycle python3 call: `spawnSync` is replaced by the async `runLifecycle` with a detached group, `LIFECYCLE_TIMEOUT_MS` 4000 and a pipe grace; argv, cwd and the `SWARM_ROOT` env are unchanged) removed two tokens that the replay pinned. These two records are re-spelled; each keeps its id, kind and pinned behaviour:
- `A-LIFECYCLE-PY-01` (`commands.ts:267`): old token `spawnSync`, new token `spawn("python3", args`. The lifecycle child is still started as `python3` with the same argv, through the async `spawn` that replaced `spawnSync`.
- `A-LIFECYCLE-PY-07` (`commands.ts:270`): old token `encoding: "utf8"`, new token `setEncoding("utf8")`. The option moved onto the stdout and stderr streams, so the lifecycle text is still decoded as UTF-8.

The other 48 mismatched records moved only their line range; their id, kind and token are unchanged: `A-SPAWN-GENERIC-01`, `-02`, `-04`, `-05`, `-06`, `-08`, `-10` to `-14` cite `bridge.ts:142` to `161`; `A-ORCH-PLAN-13` cites `commands.ts:345`; `A-LIFECYCLE-PY-02` cites `commands.ts:267`; `A-LIFECYCLE-PY-03` to `-06`, `-08` and `-09` cite `commands.ts:323`; `X-TIMEOUT-STOP-01` and `-02` cite `bridge.ts:203`; `X-CANCEL-STOP-01` and `-02` cite `bridge.ts:204`; the `X-EXIT-*` records and `X-EXIT-OTHER` cite `bridge.ts:206` to `217`; `X-COMMAND-NONZERO-01` and `-02` cite `commands.ts:351` and `355`; `X-GROUP-KILL-TERM-01` to `-03` cite `bridge.ts:173` and `175`; `S-RULE-ID-01` to `-06` cite `guard.ts:3028`, `3041`, `3057`, `3079`, `3110` and `3160`. The `S-RULE-ID` citations supersede the 2026-11-02 ones (`2965` to `3097`), which sat 63 lines above the current rule rows.

The pinned behaviour of every record still holds in the current source: a child stopped by timeout gives `E-TIMEOUT` (`bridge.ts:203`), an aborted child gives `E-INTERNAL` (`bridge.ts:204`), exit 2 with empty stdout gives `E-INPUT` (`bridge.ts:207`), non-JSON stdout gives `E-INTERNAL` (`bridge.ts:208`), the `planArgs` correlation is unchanged (`commands.ts:345`), and the `Bun.spawn` options are unchanged (`bridge.ts:156` to `161`). No record was changed on behaviour grounds.

Re-point (2026-11-03, after the GuardPerf4 edit `5cc9cb9` to `omp/src/guard.ts`): `S-RULE-ID-01` to `-06` moved from `guard.ts:3028`, `3041`, `3057`, `3079`, `3110` and `3160` to `guard.ts:3095`, `3108`, `3124`, `3146`, `3177` and `3227`. Line-only: the ids, kinds and values are unchanged, and no token was re-spelled.

Re-point (2026-11-03): `A-SPAWN-GENERIC-03`, `A-SPAWN-GENERIC-07` and `A-SPAWN-GENERIC-09` were re-pointed to the code they describe. They had matched only by coincidence, at `bridge.ts:127` (`swarmDir(cwd)` in `writeInputFile`), `bridge.ts:136` (a doc comment for `python3` and `--json`). They now cite `bridge.ts:146` (`swarmDir(cwd)` in `runScript`) and `bridge.ts:155` (the `Bun.spawn` call, `python3` and `--json`). Line-only: the ids, kinds and tokens are unchanged.

A3 review (2026-11-03). The lifecycle child's argv (`python3`, `-c`, the lifecycle code, the root, the correlation id and the cwd) and its env (the process env with `SWARM_ROOT` set) are unchanged. The stdout and stderr schema is unchanged. Only the spawn mechanism changed: `spawnSync` became an async `spawn` with a detached group, stdin ignored, a 4 s bound and a group kill. Whether this triggers the A3 omp-mirror review is not decided in this file; it is open as the decision item "A3 review of the lifecycle spawn change" in `.planning/quality/AUDIT-STATUS-2026-11-03.md`.

Re-point (2026-11-03, after the quoted-pairs edit `6184630` to `omp/src/guard.ts`): `S-RULE-ID-01` to `-06` now cite `guard.ts:3108`, `3121`, `3137`, `3159`, `3190` and `3240`. Line-only. The citations they replace (`3095`, `3108`, `3124`, `3146`, `3177`, `3227`) were stale after the 13-line shift.

Re-point (2026-11-06, after the OSA-03 grace branch and the quality WIP's line shifts in `omp/src/bridge.ts` and `omp/src/commands.ts`). Line-only; no value changed. 69 records whose values sat in the file but past their windows moved with the inserted lines (block shifts of +23 in the `bridge.ts` spawn, stop, exit and kill blocks, +20 in the abort and `killGroup` blocks, +4 in `commands.ts`). `X-GROUP-KILL-TERM-02` and `-03` now pin the kill timer at `:198`, where HEAD's `:175` moved. `A-LIFECYCLE-PY-01` and `-07` were already re-spelled at HEAD (`ff810c7`), so only their lines moved (`:267` to `:271`, `:270` to `:274`). `X-EXIT-0-01` (`code === 0`) moved from `bridge.ts:209` to `:232`: at `:209` its window also held the OSA-03 grace condition at `:211`, a coincidental match. Eleven records that were exact at HEAD had drifted to the edge of their windows and are re-pinned to their lines: `C-SCRIPT-UNION` (`:16`), `C-ERROR-CODE-UNION` (`:17`), `C-ERROR-CODES-01` to `-07` (`:20`), `C-DEFAULT-TIMEOUT-MS` (`:58`) and `C-KILL-GRACE-MS` (`:59`). A HEAD-to-WIP line mapping over all 211 records found no other coincidental match. The A3 question at line 535 is unchanged.

A3 review (2026-11-06): decided by the user as not required. No contract record is held. The lifecycle spawn change stands as recorded in the re-point entry above.
