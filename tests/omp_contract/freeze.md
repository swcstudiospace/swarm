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

Status: **OPEN. Decision pending with the omp host owners.**

- The open question is how the omp ExtensionAPI version is pinned against this bridge contract. Two options are on the table with the omp host owners: **pin by commit** (the contract records the omp commit it was frozen against) or **omission** (the contract does not record an ExtensionAPI version at all).
- **Default stays frozen.** Until the host owners decide, this document and `vectors.json` describe the current source as the contract. No version field is added and no version gate is added to the bridge or tools.
- No change in this phase depends on the decision. The decision is pending; it is not resolved here.
