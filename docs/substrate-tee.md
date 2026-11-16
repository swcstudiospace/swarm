# Swarm → Agent Substrate event tee (ADR 0001, phase S1)

Implements S1 criteria 3–6 and 8 of
[ADR 0001](https://github.com/swcstudiospace/agent-substrate/blob/main/docs/adr/0001-agent-swarm-as-sdlc-mesh-on-substrate.md)
(Linear SPE-5052): one `correlation_id` is one Graph ID, every run-log record is attributed to the agent that wrote it and
teed to substrate-mcp's `/events`, and none of it can change what the swarm does when substrate is absent.

Code: `swarm/substrate_client.py` (HTTP), `swarm/substrate_tee.py` (tables, Graph ID, cache, dedupe, tee),
`swarm/runlog.py` (the hook), `scripts/orch_plan.py` (binding + `graph_register`).
Tests: `tests/test_substrate_tee.py`.

## Environment

| Variable | Meaning |
|---|---|
| `SUBSTRATE_URL` | Base URL of substrate-mcp. Empty/unset (or not `http(s)://`) = the whole integration is off; no socket is opened, no sqlite file is created. |
| `SUBSTRATE_DISABLED=1` | Force off even when `SUBSTRATE_URL` is set. |
| `SUBSTRATE_TOKEN` | This process's bearer token (sent as `Authorization: Bearer …`). The server maps it to a surface via `SUBSTRATE_TOKEN_<SURFACE, '-'→'_', upper>`, e.g. `SUBSTRATE_TOKEN_SWARM_A05_BE`. It is only ever placed in that header; it is never logged, printed or put in an event. |
| `SUBSTRATE_TOKEN_<SURFACE>` | Optional per-surface override, read by this client under the same name the server uses, e.g. `SUBSTRATE_TOKEN_SWARM_A08_QA`. See *Tokens*. |
| `SUBSTRATE_GRAPH_ID` | Graph ID to offer for a new run (see *Graph ID*). |
| `SWARM_REPLICA` | Replica name in the session id; default `r0`; 1-64 characters of `[A-Za-z0-9._-]` (see *Session*). |
| `SWARM_LEASE_TTL_S` | TTL the runner asks for on each task's lease; see [substrate-leases.md](substrate-leases.md). |

**Transport and time bounds.** Every request goes through one seam, `substrate_client._open(req, timeout)`, which builds a
urllib opener per call: `ProxyHandler()` (so `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY`, upper or lower case, are honoured
as set at that moment), a redirect handler that refuses every redirect, and HTTP/HTTPS handlers whose connections register
their socket with the request as soon as TCP is up (again before the TLS handshake). The exchange runs on a daemon worker
thread, one per request; the socket timeout is 1.5 s (`TIMEOUT_S`) per socket operation. The caller waits at most
`TIMEOUT_S + DEADLINE_SLACK_S` (1.5 s + 0.5 s) from entry, over slot acquisition, DNS, connect, headers and body alike. At that deadline it
marks the request dead and shuts the registered socket down (`SHUT_RDWR`), which ends a blocked connect, header read or
body read in the worker at once; a connect that completes later is shut down and closed on the spot. Only a worker stuck in
DNS (no socket yet) can outlive its deadline, and at most 4 workers (`MAX_IN_FLIGHT`) exist at once. With none free, a
request waits for a slot within the same deadline. If that wait expires, it raises `RequestBusy` without starting a
worker or outage back-off: `mcp_call_outcome` returns `busy`, optional tee calls return `None`, and lease dispatch defers.
The body is read up to `MAX_BODY_BYTES` + 1 (1 MiB cap); more is rejected. A deadline after a worker starts, socket error
or oversized reply counts as a network failure, whatever the status (including an unreadable non-2xx body): the call
returns `None` and all substrate calls of that process pause for 30 s, so a dead host costs one timeout, not one per record.

**Redirects.** Redirects are never followed: a 3xx is just a non-2xx answer, so the bearer is never re-sent anywhere. The
bearer is also added with `Request.add_unredirected_header`. Through an HTTP proxy the bearer travels only as the
`Authorization` header of the forwarded request (never in a proxy `CONNECT`).

## Tokens

S4 provisions **one `SUBSTRATE_TOKEN` per agent process**: an agent's process holds only its own surface's token, and that
is all the client needs. The install writes it to that agent's 0600 env file outside the workspace, and `swarm_run.py`
starts each agent session with it and with every other `SUBSTRATE_TOKEN*` stripped
([substrate-workspace.md](substrate-workspace.md)).

**Each teed row carries the token of the agent it is attributed to**, found by `workspace.credential(agent, root)`, the
one lookup the agent's session, the leases the runner holds for it and its handoffs use too:

1. that agent's env file for the workspace (`root`, the script's `--root`; the runner's `--repo`), when the file exists.
   A file that exists but is unusable (not 0600, not this user's, not exactly one `SUBSTRATE_TOKEN=` line) stops the
   lookup: the row stays local;
2. else `SUBSTRATE_TOKEN_<SURFACE, '-'→'_', upper>` in the environment, the same secret under the server's name, for a
   process deliberately given several tokens (a pre-S4 runner, whose gate children `qa_gate` A08, `rev_gate` A09,
   `sec_gate` A10, `rel_plan` A12 inherit its environment);
3. else nothing. **A row is never sent with `SUBSTRATE_TOKEN`**: in the runner that is A01's, and an A05 row sent with
   it would be misattributed or refused. The row stays local and the first such row per agent and process prints one
   `[substrate-tee] <agent> run-log rows stay local: …` line on stderr, naming what is missing and never a token.

The request carries that token as `SUBSTRATE_TOKEN` with every other token variable left out. Outside the tee,
`rest_post` and `mcp_call` still take a keyword-only `surface`: when it is given, the token is
`SUBSTRATE_TOKEN_<SURFACE>` if that is set and non-blank, else `SUBSTRATE_TOKEN`; without it, always `SUBSTRATE_TOKEN`.
`graph_bind` (bind and forward lookup) and `graph_register` pass `swarm-a01-orch`, so they speak as the runner (A01).
No token is ever logged, printed or returned.

## Identity

`surface` is the emitting agent's surface; the server refuses (REST 403 / MCP `isError`) a body whose `surface` differs from
the token's, it never re-attributes. The tee drops such an event (and releases its dedupe claim).

| Agent | Surface | Agent | Surface |
|---|---|---|---|
| A01 | `swarm-a01-orch` | A16 | `swarm-a16-cloud` |
| A02 | `swarm-a02-req` | A17 | `swarm-a17-plat` |
| A03 | `swarm-a03-arch` | A18 | `swarm-a18-supply` |
| A04 | `swarm-a04-uxd` | A19 | `swarm-a19-event` |
| A05 | `swarm-a05-be` | A20 | `swarm-a20-finops` |
| A06 | `swarm-a06-fe` | A21 | `swarm-a21-k8s` |
| A07 | `swarm-a07-data` | A22 | `swarm-a22-gitops` |
| A08 | `swarm-a08-qa` | A23 | `swarm-a23-mesh` |
| A09 | `swarm-a09-rev` | A24 | `swarm-a24-ksec` |
| A10 | `swarm-a10-sec` | A25 | `swarm-a25-resil` |
| A11 | `swarm-a11-devops` | A26 | `swarm-a26-sol` |
| A12 | `swarm-a12-rel` | A27 | `swarm-a27-audit` |
| A13 | `swarm-a13-obs` | A28 | `swarm-a28-chain` |
| A14 | `swarm-a14-maint` | A29 | `swarm-a29-dapp` |
| A15 | `swarm-a15-doc` | A30 | `swarm-a30-custody` |

`AGENT_SURFACES` in `substrate_tee.py` is this table (30 surfaces); a test checks it against `agents.json` (`swarm-<id>-<code>`, lower case). The closed set of surfaces lives on the agent-substrate server, not in this repository: A16 to A30 must exist there before their tokens work (see [substrate-workspace.md](substrate-workspace.md)).

**Which agent emitted a record.** `Ctx.emit` already stamps `source = "<agent id>@<script>"` (the id the script was constructed
with, e.g. `AgentScript("A05", "code_checks", …)`), so no change to `script_base.py` was needed: the agent id is the
`A\d\d` prefix of `source`. The one non-agent source with an identity is `swarm.envelope` (`security.dev_key`), whose
payload names the signing agent in `payload.source`. A record that names no known agent is not tee'd. Events a runner
writes on behalf of others (`task.result.raw`, `escalation.request` from `swarm_run`) are attributed to the runner, A01.

**Session.** `session_id = <AGENT_ID>@<replica>:<graph_id>`, e.g. `A05@r0:ut-mabc123-0123abcd`: one (agent, replica,
Graph ID) triple. Substrate chains events per session and its `prev` lookup ignores the graph, so a session shared across
graphs would interleave chains; including the Graph ID prevents a false "broken chain" in `events_verify`. The tee and the
lease bridge build it through one function, `substrate_tee.session_id()`. `replica_id()` rejects a replica outside
`[A-Za-z0-9._-]{1,64}`, the shape the substrate accepts for a swarm session. The tee then sends nothing, and `swarm_run`
refuses to start (`E-INPUT`) when leasing is on.

**Event body** (`POST {SUBSTRATE_URL}/events`): `kind`, `summary` (`<swarm type> <task_id or correlation_id>`), `surface`,
`session_id`, `graph_id`, `node_id` (= `task_id`, omitted when the record has none), `actor: "agent"`, `repo` (the `owner/name`
slug parsed from `git remote get-url origin`, the same value every other surface and the substrate CLI use, so swarm events
show up in other agents' briefs; the directory name when there is no origin remote or git fails; memoized per process, also
used for `graph_register`), and `payload`:
`{correlation_id, msg_id, swarm_type, status?, trace_id?, causation_id?}`. `status` is the record payload's own `status` when
it is a string. `trace_id`/`causation_id` are copied from the record when present (the run log does not carry them yet).
Only 2xx counts as accepted.

`runlog.emit()` adds an additive `msg_id` (uuid4 hex) to every JSONL record and drops any substrate `lease_id` from the
payload (`redact_leases`, see [substrate-leases.md](substrate-leases.md)); all other fields and the file format are
unchanged. The tee is called after the local write.

## Swarm type → EVENT_KINDS

`EVENT_KINDS` is closed (`session.start, prompt, claim, tool.call, file.edit, shell, commit, pr, handoff, session.end, note,
warning`); the swarm does not widen it. The table is `TYPE_KINDS` / `PREFIX_KINDS` in `substrate_tee.py`. **Every** type the
repo emits is listed; a test parses (with `ast`) every `emit(` / `ctx.emit(` call in `swarm/`, `scripts/` and `hooks/`, fails on any type
that is unmapped or any emit site whose type is not a string literal, and fails on a table entry nothing emits. An unmapped
type at runtime is skipped, never downgraded to `note`. The exact swarm type always rides in `payload.swarm_type`.

| Swarm type | Emitted by | Kind | Why |
|---|---|---|---|
| `plan.updated` | `orch_plan` | `note` | Plan/bookkeeping record; no tool ran, nothing needs attention. |
| `task.transition` | `orch_status` | `note` | State-machine bookkeeping. |
| `task.result.raw` | `swarm_run` | `note` | Raw agent output captured before validation. |
| `task.claimed` | `results.apply_result` | `claim` | An agent took a task: the closest EVENT_KINDS meaning. |
| `task.stranded` | `swarm_run.recover_stranded` | `warning` | A later run failed a task left `IN_PROGRESS` past `notes.running_deadline` because its runner died. |
| `gate.verdict.unrecorded` | `swarm.verdicts` | `warning` | A gate ran but its verdict was not recorded against a task. |
| `gate.findings.coerced` | `swarm_run` | `warning` | A gate's findings had to be coerced into shape. |
| `gate.findings.synthesized` | `swarm_run` | `warning` | Findings were synthesized rather than reported. |
| `gate.findings.unattributed` | `swarm_run` | `warning` | Findings named no known gate target. |
| `task.result.rejected` | `results.reject`, `orch_status` | `warning` | A result failed validation; the task is failed or left as is. |
| `task.result.deduplicated` | `results.apply_result` | `note` | An ingest replay was skipped because its result was already applied; nothing was written. |
| `orch_status.effects_lost` | `orch_status` | `warning` | A transition served by the Rust engine committed, but its effects manifest was missing, broken or could not be replayed; the answer was printed and nothing was run again. |
| `escalation.request` | `results.reconcile`, `swarm_run` | `warning` | Rework/attempts exhausted; a human is needed. (`handoff` is the substrate's own kind for `coord_handoff` packets, which the server writes; the swarm never emits it.) |
| `security.dev_key` | `swarm.envelope` | `warning` | An envelope was signed with the development key. |
| `lease.denied` | `substrate_lease` (runner) | `note` | Another session holds the task's node; the task waits. |
| `lease.unleased` | `substrate_lease` (runner) | `warning` | Dispatched without a lease because the substrate could not answer (fail-open). |
| `lease.refused` | `substrate_lease` (runner) | `warning` | The substrate refused this agent's claim; the task is not dispatched. |
| `lease.lost` | `substrate_lease` (runner) | `warning` | A refused heartbeat stopped a session or dropped a held lease. |
| `lease.unsettled` | `substrate_lease`, `orch_status` | `warning` | A release or completion did not land. |
| `handoff.unsent` | `substrate_handoff` (runner) | `warning` | The substrate did not take a boundary's packet; it is retried on later rounds. |
| `handoff.refused` | `substrate_handoff` (runner) | `warning` | The packet was refused, or its sender has no token of its own; not written, not retried. |
| `handoff.unsigned` | `substrate_handoff` (runner) | `warning` | The substrate has no `SUBSTRATE_HANDOFF_KEY`: packets are recorded unsigned. Once per run. |
| `handoff.misattributed` | `substrate_handoff` (runner) | `warning` | The ledger recorded a packet under another surface than its sender's: a token configured under the wrong name. |
| `script.<name>` (prefix) | `AgentScript.main` | `tool.call` | Exit record of one agent script run. |
| `script.<name>.error` (prefix) | `AgentScript.main` | `tool.call` | Same, for a failed run; `payload.status` is `error`. |

Gate verdict types map to `warning`, plan/transition types to `note`, script-exit types to `tool.call`. The signed
`gate.verdict` envelopes themselves are written to `.swarm/verdicts/` and are not run-log records, so they are not tee'd
in S1; the gate script's own `script.<gate>` exit record is.

## Graph ID and binding

Format unchanged: `ut-<base36 epoch ms>-<8 hex>` (`mint_graph_id()`).

**Offer order** (`resolve_graph_id`): `--graph-id` (orch_plan) > env `SUBSTRATE_GRAPH_ID` > a Graph ID found in the brief or
spec text (`ut-[0-9a-z]+-[0-9a-f]{8}`, e.g. in `<ISSUES graphId="…">` or `Graph ID: …`) > freshly minted. Invalid explicit
or env values are ignored by the resolver; `orch_plan --graph-id` rejects a malformed value with `E-INPUT`.

**Binding.** After a *new* plan is created (not on the reuse path, not on `--dry-run`), `orch_plan` offers the id via the MCP
tool `graph_bind {correlation_id, graph_id}` (`POST {SUBSTRATE_URL}/mcp`, stateless JSON-RPC `tools/call`, JSON or SSE
reply) and **always adopts the `graph_id` the server returns**, including when `conflict` is true (another run bound the
correlation first). It then registers the node set: `graph_register {graph_id, repo, surface: "swarm-a01-orch", status:
"planned", nodes: [{node_id: <task_id>}…]}`. This happens before `plan.updated` is emitted so that event and the script
exit record are tee'd. Nothing time-varying enters the brief text or `notes_json`, so re-running the same plan still hits the
reuse path (and does not bind or register again).

**Cache.** The adopted binding is stored in `<swarm dir>/substrate-tee.db`, table
`bindings_by_server(substrate_url, correlation_id, graph_id, created)`, primary key `(substrate_url, correlation_id)`, keyed
by the normalised `SUBSTRATE_URL` (trimmed, no trailing `/`): only rows for the current server are read or written, so
pointing the swarm at another server never reuses the old server's Graph ID; it asks the new one. The pre-existing
`bindings` table (not keyed by server) is left in place and no longer read. Later scripts (separate processes) tee without
asking. On a cache miss the tee does a forward lookup
`graph_bind {correlation_id}`; `existing` is cached, `unbound` or no answer means the record is skipped (no event, and no
Graph ID is ever invented by the tee). Nothing is cached when substrate did not answer a bind. An unbound/unanswered
lookup is remembered in process memory for 60 s, so a burst of records for one unbound run costs one forward lookup;
that negative result is never written to sqlite, and a successful lookup or `bind_graph` clears it. Explicit
authentication/identity refusals skip the optional tee without entering this negative cache. Lease consumers bypass
negative entries and report such refusals as `lease.refused`, never as permission to dispatch unleased.

## Dry runs

A `--dry-run` invocation never tees. `runlog.emit` takes a keyword-only `tee` (default `True`); `Ctx.emit` passes
`tee=not ctx.dry_run`, and the exit record `AgentScript.main()` writes goes through `Ctx.emit`. So a dry run with an
explicit (`--correlation-id`) or inherited (`SWARM_CORRELATION_ID`) bound correlation still writes its local JSONL records
but makes no substrate request at all (no lookup, no event).

## Dedupe

Same sqlite file, table `seen(surface, msg_id, first_seen, state)`, primary key `(surface, msg_id)`, indexed on `first_seen`,
24 h window. A row is `pending` (claimed, request in flight) or `sent` (substrate answered 2xx). Before sending, the tee
claims the key in one transaction: an atomic `INSERT OR IGNORE` of a `pending` row, else an `UPDATE` that re-claims (new
timestamp, `pending`) a `pending` row older than 60 s (its sender was killed between claim and send) or a row past the
window. A `sent` row inside the window or a `pending` row younger than 60 s is not re-claimable, so a republished record (or
two processes racing it) produces exactly one request. A 2xx marks the row `sent`; anything else deletes the `pending` row so
a later republish can try again. A database from before this change gains the `state` column with default `sent`. Expired
rows are pruned at most once per 60 s per process, not on every event.

## Fail-open guarantees

- No `SUBSTRATE_URL`, `SUBSTRATE_DISABLED=1`, or a non-http(s) URL: no request, no sqlite file; behaviour and output are
  unchanged (the JSONL record only gains `msg_id`).
- Timeout (1.5 s per socket operation, 2 s for the whole request), connection error, non-2xx, malformed or oversized reply,
  `isError`, an unmapped type, an unattributable record, an
  unbound correlation: the tee sends nothing more and returns. `rest_post`, `mcp_call`, `tee` and `runlog`'s call to it
  never raise; `orch_plan`'s bind/register block is wrapped as well, so the plan, its output fields and the exit code are
  identical with substrate unreachable.
- At most four request workers run at once. Waiting for a free slot shares the request's 2 s deadline. If no slot becomes
  available, `mcp_call_outcome` returns `busy` (the optional clients return `None`) without starting outage back-off.
  Lease claims defer rather than dispatching unleased; a busy Graph ID lookup is never cached as unbound.
- Tokens are never logged, printed or returned, and never follow a redirect.
- `--dry-run` never sends anything to substrate.

## Not in S1

- Fetching a brief from substrate before planning (S2+), and any consumption of other agents' briefs.
- Leases and handoffs (S2): the runner's `graph_claim` / `graph_heartbeat` / `graph_release` / `graph_complete` are
  documented in [substrate-leases.md](substrate-leases.md), its `coord_handoff` packets in
  [substrate-handoffs.md](substrate-handoffs.md).
- Tee'ing signed `swarm.v1` envelopes that are not run-log records (verdict files, `task.assign`).
- `trace_id`/`causation_id` production in the run log (they are forwarded when a record carries them).
- A `--graph-id` entry in the omp extension's `orch_plan` tool schema (`omp/src/tools.ts`); the CLI flag and the
  `SUBSTRATE_GRAPH_ID` env var work for it already.
- Binding for plans created by paths other than `orch_plan` (e.g. `scripts/swarm_run.py` on an unbound correlation tees
  nothing until the correlation is bound).
