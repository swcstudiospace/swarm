# AgentSwarm — 30-Agent Software Engineering Swarm

A complete design specification for a distributed swarm of 30 specialized AI agents that plan, build, verify, ship, and maintain production software collaboratively — with parallel execution, dynamic workload balancing, and self-organization. A01–A15 cover the SDLC; A16–A30 add three domains of five agents each: cloud-native, cluster and Web3.

## Document map

| # | Document | Contents |
|---|----------|----------|
| 1 | [01-architecture.md](01-architecture.md) | Swarm topology, 30-agent roster, task model, load balancing, self-organization, rework/escalation |
| 2 | [02-message-protocol.md](02-message-protocol.md) | Envelope `swarm.v1`, subjects & delivery semantics, task lifecycle state machine, shared error taxonomy, autonomy levels L0–L4 |
| 3 | [agents/](agents/) | Full specifications A01–A30 (7 sections each: purpose, stack, I/O formats, decision logic & autonomy, error handling, metrics, security) |
| 4 | [04-integration-plan.md](04-integration-plan.md) | Interaction matrix, data-sharing substrate, reference event flows, contract governance, conflict-resolution ladder, autonomy interlocks |
| 5 | [05-deployment-guide.md](05-deployment-guide.md) | Packaging, agent manifests, bootstrap sequence, config matrix, profiles, rollout, swarm observability, security baseline, sizing |
| 6 | [06-testing-protocols.md](06-testing-protocols.md) | 7-level validation: golden tasks → contracts → integration flows → chaos → E2E benchmark → security red-team → prod invariants |
| 7 | [07-scalability.md](07-scalability.md) | Manifest schema, new-agent onboarding (shadow→probation→full), traffic shaping, contract evolution, multi-swarm |
| 8 | [CLAUDE.md](CLAUDE.md) · [prompts/](prompts/) · [scripts/](scripts/) · [swarm/](swarm/) | Runnable implementation: subagent prompts, per-agent Python tools, orchestration runtime |
| 9 | [docs/substrate-integration.md](docs/substrate-integration.md) | Platform pointer: how the spec's substrates map onto [Agent Substrate](https://github.com/swcstudiospace/agent-substrate) |
| 10 | [docs/swarm-command-center.md](docs/swarm-command-center.md) · [docs/swarm-bar.md](docs/swarm-bar.md) · [docs/operations-runbook.md](docs/operations-runbook.md) | v2.2 runtime: continuous scheduler, telemetry, handover, tools and skills per agent, the omp bar; how-to guides, configuration, troubleshooting, known limits |
| 11 | [docs/tools/catalog.md](docs/tools/catalog.md) · [docs/skills/catalog.md](docs/skills/catalog.md) | Generated per-agent catalogs of the 30 agents' registered tools and skills |

## Platform

AgentSwarm runs on **[Agent Substrate](https://github.com/swcstudiospace/agent-substrate)**, the shared
platform providing four of the infrastructure substrates this spec assumes ([01 §2.2](01-architecture.md),
[04 §2](04-integration-plan.md)): events over substrate-mcp, governed memory, leases and handoffs, and the
Graph ID a `correlation_id` maps onto. The artifact registry, secrets and the OTel telemetry plane are not
in that set. The integration ADR lives in that repo
(`docs/adr/*-swarm-substrate-integration.md`), tracked as Linear
[SPE-5050](https://linear.app/swcstudio/issue/SPE-5050) /
[SPE-5051](https://linear.app/swcstudio/issue/SPE-5051);
[docs/substrate-integration.md](docs/substrate-integration.md) is the mirror pointer here. No swarm
runtime behaviour depends on it today.

## The 30 agents

| ID | Code | Role | Phase |
|----|------|------|-------|
| | | **Core SDLC (A01–A15)** | |
| A01 | `ORCH` | Swarm Orchestrator — planning, scheduling, arbitration | Control |
| A02 | `REQ` | Requirements Engineer — stories, acceptance criteria | Requirements |
| A03 | `ARCH` | Solution Architect — blueprint, contracts, ADRs | Design |
| A04 | `UXD` | UX Designer — design system, UX specs, a11y | Design |
| A05 | `BE` | Backend Engineer — service implementation | Coding |
| A06 | `FE` | Frontend Engineer — UI implementation | Coding |
| A07 | `DATA` | Data Engineer — models, migrations, data contracts | Coding / Data |
| A08 | `QA` | Test Engineer — quality gate, test automation | Testing |
| A09 | `REV` | Code Reviewer — review gate, standards | Quality |
| A10 | `SEC` | Security Auditor — security gate, supply chain | Cross-cutting |
| A11 | `DEVOPS` | DevOps / Platform — IaC, CI, environments | Deployment |
| A12 | `REL` | Release Manager — progressive delivery, rollback | Deployment |
| A13 | `OBS` | Observability / SRE — SLOs, alerts, incidents | Monitoring |
| A14 | `MAINT` | Maintenance Engineer — patches, debt, EOL | Maintenance |
| A15 | `DOC` | Documentation Engineer — docs, runbooks, references | Cross-cutting |
| | | **Cloud-Native domain (A16–A20)** | |
| A16 | `CLOUD` | Cloud-Native Architect — landing zones, regional resilience, residency | Design |
| A17 | `PLAT` | Platform Engineer — golden paths, service catalog, platform APIs | Deployment |
| A18 | `SUPPLY` | Supply-Chain Security Engineer — images, SBOM, provenance, signatures | Cross-cutting |
| A19 | `EVENT` | Event-Driven & Serverless Engineer — brokers, event schemas, consumers | Coding |
| A20 | `FINOPS` | FinOps & GreenOps Analyst — cost, budgets, carbon | Maintenance |
| | | **Cluster domain (A21–A25)** | |
| A21 | `K8S` | Kubernetes Cluster Operator — upgrades, capacity, node pools | Deployment |
| A22 | `GITOPS` | GitOps & Progressive Delivery Engineer — Argo CD/Flux, overlays, rollouts | Deployment |
| A23 | `MESH` | Service Mesh & Networking Engineer — mTLS, ingress, NetworkPolicy | Deployment |
| A24 | `KSEC` | Cluster Security & Policy Engineer — admission policy, RBAC, Pod Security | Cross-cutting |
| A25 | `RESIL` | Resilience & Chaos Engineer — PDB, probes, backup, DR, chaos | Monitoring |
| | | **Web3 domain (A26–A30)** | |
| A26 | `SOL` | Smart Contract Engineer — contracts, tests, gas, deployment plans | Coding |
| A27 | `AUDIT` | Smart Contract Auditor — static analysis, invariants, attack models | Quality |
| A28 | `CHAIN` | Blockchain Infrastructure Engineer — nodes, RPC policy, indexers | Deployment |
| A29 | `DAPP` | dApp & Wallet Integration Engineer — wallets, signing flows | Coding |
| A30 | `CUSTODY` | Web3 Custody & Compliance Engineer — multisig, key ceremonies, screening | Deployment |

A01–A15 are the core SDLC agents. A16–A30 are three specialist domains of five agents each: Cloud-Native (A16–A20), Cluster (A21–A25) and Web3 (A26–A30). No new agent owns a gate: A08, A09, A10 and A12 remain the only gate owners, and the domain agents' work flows through them. Each agent has a shared baseline of tools and skills plus 10 to 15 of its own; the per-agent lists are the generated catalogs [docs/tools/catalog.md](docs/tools/catalog.md) and [docs/skills/catalog.md](docs/skills/catalog.md).

**Complementarity guarantee:** every SDLC phase has exactly one accountable (single-writer) agent per artifact class; overlap is limited to consumer/producer relationships, and every artifact row has ≥ 1 consumer (see interaction matrix).

## Core invariants (quick reference)

1. Single-writer artifact ownership; all other agents propose via messages.
2. Fail-closed gates — no quality/review/security verdict ⇒ no approval; no all-green verdicts ⇒ no promotion.
3. Bounded rework (max 2 auto loops) → arbitration → human escalation with evidence.
4. Everything correlated: one business request = one `correlation_id` from brief to release record.
5. Autonomy ceilings L0–L4 per action class; runtime policy can lower, never raise.

## Runnable swarm (Claude Code subagents)

The spec is executable. Each agent is a Claude Code subagent in [.claude/agents/](.claude/agents/) whose
XML-tagged system prompt lives in [prompts/](prompts/) and whose tools are Python scripts in [scripts/](scripts/),
built on the runtime in [swarm/](swarm/) (no Python dependencies, plus the required PyO3 extension `swarm._native`; signed `swarm.v1` envelopes, SQLite Task Store with the
lifecycle state machine, fail-closed gates, manifest registry). [agents.json](agents.json) is the manifest.

```bash
python3 scripts/orch_plan.py --repo /path/to/codebase --brief brief.md --pattern feature   # brief → task DAG in <repo>/.swarm
python3 scripts/swarm_run.py --repo /path/to/codebase --runtime omp  # normal OMP approvals; --runtime claude also supported; --max-parallel N (default 4), --swarm-timeout S
python3 scripts/swarm_run.py --repo /path/to/codebase --dry-run --runtime omp   # simulate the DAG offline; not acceptance
bun scripts/ts/req_lint.ts --json                                     # TypeScript twin of any scripts/*.py
python3 scripts/build_agents.py                                      # regenerate .claude/agents, .grok/agents, .cursor/agents, omp/agents, omp/skills (--check)
python3 scripts/_install_cursor.py --target /path/to/repo --dry-run  # list the Cursor files a target repo would gain; writes nothing
python3 scripts/build_agents.py --install-workspace /path/to/workspace  # Claude + Grok agents, skills, hook, the omp package and substrate-mcp (see below)
python3 scripts/orch_status.py --repo /path/to/codebase           # status, gates, escalations (same --repo as the plan)
python3 scripts/swarm_live.py --repo /path/to/codebase            # live run table: state, elapsed time, last activity
python3 scripts/swarm_tool.py catalog --caller A05                # tools and skills an agent may use (call: --name N --input JSON)
python3 -m swarm.tool_registry check && python3 -m swarm.skill_registry check   # registry and skill validation
python3 scripts/receipt_export.py --repo /path/to/codebase --json  # desk verification receipt; swarm verdicts stay advisory
python3 -m pytest -q                                                # runtime + orchestration tests
```

`orch_plan.py --pattern` takes `feature` (13 tasks), `hotfix` (8), `dependency` (8), the three domain patterns `cloud-native` (16 tasks, with A16–A20), `cluster` (14, with A21–A25) and `web3` (15, with A26–A30), each also running the core gates and release agents, `parallel` (one short lane per disjoint path set) and `custom --plan plan.json`. The `cluster` and `web3` patterns pin their implementer tasks to risk class `high` (all four gates); `cloud-native` follows `--risk-class`. A file path in a graph plan (`scripts/orch_from_graph.py`) routes to the owning specialist, e.g. `*.sol` to A26, `charts/` to A22, `Dockerfile*` to A11.

`scripts/receipt_export.py` (and `scripts/ts/receipt_export.ts`) reads the Task Store the status command uses and prints a desk verification receipt. Swarm verdicts are marked advisory, `approved_by` stays empty, and a credential-shaped string that survives redaction exits 2 without writing `--out`.

Trusted controllers can load an **existing** private Ed25519 seed through `SWARM_ED25519_KEY_FILE` (an absolute path; the OMP bridge resolves a relative value against the session workspace before the controller runs),
or from `${XDG_CONFIG_HOME:-$HOME/.config}/agent-swarm/signing-key.hex` when neither raw signing
variable is set. References must resolve to an owner-only regular file without symlink components;
the conventional directory must also be private. Key contents never belong in prompts, argv,
logs, or tracked files. See [private signer bootstrap](docs/operations-runbook.md#private-signer-bootstrap).

Live/reused `orch_plan` responses include a cryptographically verified `execution_assignment`
when a real signer is available. `/swarm` refuses missing, unsigned, or mismatched kickoff
assignments instead of dispatching an unauthenticated A01 turn. Specialist sessions cannot
auto-load private references. OMP uses normal `write` approvals (or `--approval-mode always-ask`);
Grok headless dispatch currently fails closed because its installed interface does not support
the old stdin transport. Start a fresh OMP process after updating the extension.

The omp `tool_call` guard (`omp/src/guard.ts`) covers every swarm session. It blocks with `BLOCKED needs: human-approval` the Web3 rows `chain-broadcast` and `chain-key-material` and the cluster lifecycle rows under `prod_infra` (`argocd-change`, `flux-change`, `mesh-lifecycle`, `velero-change`, `managed-cluster-lifecycle`, `kubeadm-lifecycle`). Its per-agent lists are documentation, not an allow list. See [AGENTS.md](AGENTS.md) (HOOK-02).

Or, inside Claude Code, ask for the `a01-orchestrator` subagent: it plans, then delegates each ready task to
`a02-requirements` … `a30-custody-compliance` via the Agent tool. See [CLAUDE.md](CLAUDE.md) for the full layout and rules.

### Swarm Command Center (v2.2)

`swarm_run.py` runs a continuous scheduler: a slot is refilled the moment a session ends, bounded by `--max-parallel` (`SWARM_MAX_PARALLEL`, default 4, ceiling 32) and each agent's own limit, with a lifecycle state machine, cancellation, timeouts, backoff and rate-limit backpressure. One telemetry channel (`.swarm/events.jsonl` and `.swarm/live.json`) feeds the logs, `scripts/swarm_live.py` and the swarm bar of omp (always there, two full-width rows above the editor) that shows every subagent's state and elapsed time and the session's token savings. Agents hand work over with a versioned, validated `swarm.handover.v1` document. Every agent has a shared baseline of tools and skills plus 10 to 15 of its own, found with `swarm_catalog` and called with `swarm_tool_call`. Start at [docs/swarm-command-center.md](docs/swarm-command-center.md) (architecture, how to add an agent, tool or skill, configuration, troubleshooting, security model, known limits) and [docs/swarm-bar.md](docs/swarm-bar.md) (reading the bar).

## Install into a workspace

`build_agents.py --install-workspace` regenerates the agents, copies the Claude/Grok agents, skills and
UserPromptSubmit hook into the workspace, then installs the omp targets (`omp/agents/`, `omp/skills/` and the
`omp/` extension package). It also wires substrate-mcp: the `substrate` MCP entry in `<ws>/.mcp.json` (Claude, omp)
and `<ws>/.grok/config.toml` (Grok), naming `${SUBSTRATE_TOKEN}` and never a value, plus one 0600 env file per agent
outside the workspace holding that agent's `SUBSTRATE_TOKEN`, taken from `SUBSTRATE_TOKEN_<SURFACE>` in your
environment ([docs/substrate-workspace.md](docs/substrate-workspace.md)). The install step writes only inside `<ws>` (never into this repo or `~/.omp`), except the per-agent substrate credential env files under `${XDG_CONFIG_HOME:-~/.config}/agent-swarm/agents/` (outside the workspace by design; skipped with `--no-substrate`); the regeneration pass may update this repo's tracked generated files.
It refuses (exit 2, nothing written) a workspace inside this checkout, equal to `$HOME` or inside `~/.omp`, any
destination reached through a symlink (the file or a parent dir under `<ws>`), a runtime that cannot call MCP, and an
agent whose token it cannot deliver; for the last it prints what you must create. The substrate wiring needs one token per agent, 30 in all (`SUBSTRATE_TOKEN_<SURFACE>`); the surfaces of A16–A30 must exist on the agent-substrate server, otherwise install with `--no-substrate`.

```bash
python3 scripts/build_agents.py --install-workspace /path/to/ws                  # omp link mode (default)
python3 scripts/build_agents.py --install-workspace /path/to/ws --omp-mode copy  # omp agents + skills only
python3 scripts/build_agents.py --install-workspace /path/to/ws --dry-run        # print the plan, config diff and env files, write nothing
python3 scripts/build_agents.py --install-workspace /path/to/ws --no-substrate   # skip substrate-mcp install writes
python3 scripts/build_agents.py --install-workspace /path/to/ws --with-a01-complete-hook  # also register the A01-complete Stop hook (off by default)
```

- **A01-complete Stop hook** (`hooks/on_a01_complete.py`): not registered by default, because it can detach the
  unattended runner. `--with-a01-complete-hook` registers it for Claude and Grok; a reinstall without the flag removes
  an earlier registration of it and leaves any other Stop hook alone. Registered, it detaches the runner only when
  opt-in is on (`AIO_SWARM_AFTER_ORCH` or `SWARM_AFTER_ORCH` set to `1`, `true`, `yes`, or `on`), a real signing key
  is configured, `SWARM_ALLOW_AUTONOMOUS=1` is set and no cloud-agent marker (`CURSOR_AGENT`, `CURSOR_CLOUD_AGENT`,
  `CLOUD_AGENT`) is set. Once that opt-in is on and a completion signal was detected, any other case records the
  reason on the hook-fired event and spawns nothing. With opt-in off, or with no completion signal, the hook returns
  without writing that event.

- **Link** (default) adds this checkout's `omp/` realpath to `extensions:` in `<ws>/.omp/config.yml`, so that file
  holds a host path by design. omp reads it from the cwd only: start omp at `<ws>`. When the file has no
  `extensions` key, the installer carries over your inherited list, because a project list replaces it. The source
  is `<ws>/.omp/settings.json`, else `config.yml|config.yaml` in your omp user agent dir (the profile dir via
  `OMP_PROFILE`/`PI_PROFILE`, else `PI_CODING_AGENT_DIR`, else `~/.omp/agent`), else that dir's `settings.json`.
  A user YAML without `extensions` suppresses the legacy `settings.json`.
- **Copy** (`--omp-mode copy`) copies the agents and skills into `<ws>/.omp/agents` and `<ws>/.omp/skills`:
  no tools, no guard, no `/swarm` command and no context hook. Re-run it after every regeneration.
- Both modes print `WARNING shadow:` for each agent or skill with the same `name` that omp would load
  first: project `.omp/agents|skills` in `<ws>` or its ancestors, your user `agents|skills` dirs (profile-aware),
  earlier `extensions:` entries and `skills.customDirectories`. Only files omp would load count: agents need
  `name` and `description`; skills need `description` and are skipped on `enabled: false`. `.claude/*` and
  `.agents/skills` do not shadow.
- Start a fresh omp session after every install or `build_agents.py` regeneration: extensions and agents load at
  session start.
- `task.maxRecursionDepth`: omp's default of 2 is enough (main session → `a01-orchestrator` → specialists, which
  never get `task`). Set 3 only when A01 is itself spawned by another subagent; otherwise A01 stops with
  `BLOCKED needs: depth`. Never set a negative (unlimited) value.

  ```yaml
  # <ws>/.omp/config.yml or ~/.omp/agent/config.yml
  task:
    maxRecursionDepth: 3
  ```

A relocated `omp/` package needs `SWARM_ROOT` pointing at an agent-swarm checkout, since its tools run the
Python scripts there.

## Cursor cloud agents

`.cursor/agents/` holds one generated subagent per slug (`a01-orchestrator` … `a30-custody-compliance`). `python3 scripts/build_agents.py` writes them from `prompts/` plus a Cursor preamble, and `--check` fails if a file drifts. Frontmatter is `name`, `description`, and `model: inherit` ([Cursor subagents](https://cursor.com/docs/subagents)).

Cursor cloud sessions are advisory. No signing key is present (`SWARM_ED25519_KEY`, `SWARM_SIGNING_KEY` and `SWARM_REQUIRE_KEY` unset), so gate scripts record nothing and nothing from that session counts as APPROVED. A missing signing key is the expected Cursor state and is not E-DEP: the agent accepts an unsigned `task.assign` from the parent session or `a01-orchestrator` and does not sign. Every gate script (`qa_gate`, `rev_gate`, `sec_gate`, `rel_plan`) runs only as a non-recording preview through python3 with `SWARM_AGENT_SESSION=1` in its environment. The bun twin `scripts/ts/sec_gate.ts` is not that preview. Non-gate results use `orch_status.py --ingest --advisory`, which saves the result and does not attempt APPROVED or DONE. The script writes an advisory envelope file and records no verdict rows. The agent never sets `SWARM_SIGNING_KEY`, `SWARM_ED25519_KEY` or `SWARM_ALLOW_INSECURE_DEV_KEY`, never signs or records a verdict, and never ingests a gate result or transitions a task to APPROVED or DONE. A gate result that fails, is refused or is unrecorded is advisory, never a pass. `render_cursor` rewrites A01's in-session ingest lines so a gate result is not ingested: A01 transitions that gate task to BLOCKED (`advisory preview recorded no verdict rows; human records the gate`), stops the scheduling loop, and leaves dependents unscheduled. A missing Task Store, `python3` or git is still E-DEP. The merge gate is Greptile, run by Desk Quality. A Cursor agent never merges a pull request, enables auto-merge, pushes to a protected branch or deletes a branch. Work ends at a draft PR, and a human merges after that gate. Where the shared prompt grants a merge or an auto-merge, `render_cursor` rewrites the line into draft-PR wording (A14 and A09). Worker branches follow the host repository. In a Programming Desk repo that is `bot-0N-<seat>/<task_id>`, where `bot-0N-<seat>` is the `ownership.yaml` owner of the files being changed, because the desk `gates.yml` rejects any prefix other than `^bot-0[0-6]-[a-z0-9-]+$`. `render_cursor` rewrites `swarm/<task_id>` (A05, A06) and `auto-fix/*` (A09) to `<seat-prefix>/<task_id>`. The substitution table raises if an expected pattern is missing from the prompt body. Those rewrites are Cursor-only; the Claude, Grok and omp renders keep the shared prompt. Nesting stops at two levels: the parent may spawn `a01-orchestrator`, A01 may spawn the other slugs, and those specialists must not spawn further.

A target repo does not need to vendor this runtime. Pin a checkout of agent-swarm and point `SWARM_ROOT` at it (absolute path). The agents then call:

```bash
python3 "$SWARM_ROOT/scripts/<tool>.py" --root <target repo> --json
```

`<target repo>` is the git toplevel of the repo being edited. Repo-local `scripts/` is used only when `SWARM_ROOT` is unset and the working tree is this agent-swarm checkout. In any other repo, including one that already has its own `scripts/` directory, the agents stop until `SWARM_ROOT` is set. That is the setup a follow-up install into `swcstudiospace/programming-desk` depends on.

The installer copies only `.cursor/` (the 30 agents, `.cursor/rules/agent-swarm.mdc`, and a provenance stamp). It does not write MCP config or env files and it does not wire substrate.

```bash
python3 scripts/_install_cursor.py --target /path/to/repo            # copy
python3 scripts/_install_cursor.py --target /path/to/repo --dry-run  # paths only, write nothing
python3 scripts/_install_cursor.py --target /path/to/repo --check    # exit 1 if the copy would change
python3 scripts/build_agents.py --install-cursor /path/to/repo       # same copy, no regeneration, no substrate
```

It refuses a symlink on the way, a target inside this checkout or equal to `$HOME`, and any differing file it did not previously write. A second run against an unchanged tree writes nothing. `grokbot/skills/swarm-cloud-dispatch/SKILL.md` is the Grok Bot side of the same handoff: install in a pull request, then brief a Cursor cloud agent to run `a01-orchestrator`. The result comes back as a draft pull request.

## Grok Bot seat map

`python3 scripts/build_agents.py` also writes `grokbot/swarm/seat-map.json` and one `grokbot/skills/swarm-<lane>/SKILL.md` per manifest lane (`control`, `delivery`, `code`, `verify`, `ops`, `sustain`). `--check` fails if those files drift. The generator does not write under `.grok/` and does not touch the hand-written `grokbot/skills/swarm-cloud-dispatch/SKILL.md`. There is no role beyond A30.

The map lists each of the 30 roles with a home (`desk-lead`, a desk seat, `executor`, `routine` or `cloud`), the seat that verifies the work, path globs, that seat's verification tools, and the autonomy ceiling from `agents.json`. Android, iOS and desktop are routing rules, because the swarm has no mobile or desktop role: `android/**` (and Kotlin/Gradle) to `bot-03-android`, `ios/**` (and Swift/Xcode) to `bot-04-ios`, desktop shells (`desktop/**`, `electron/**`, `tauri/**`) to `bot-02-web-edge`. A routing rule wins over a role path glob. When two roles both match, the longest path glob wins, and an equal length goes to the lowest role id, so the shared `scripts/code_checks.py` and `scripts/ts/code_checks.ts` (A05 and A06) resolve to A05. A lead-owned doc stays with Desk Lead when its literal glob is longer than `docs/**`. UX design paths stay with `bot-01-systems-backend`, matching `design/**` in the desk ownership manifest.

Each lane skill reads prompts and runs scripts from `$SWARM_ROOT`, the pinned agent-swarm checkout, and passes `--root` for the target repo. A target such as programming-desk does not contain the swarm runtime.

```bash
python3 scripts/build_agents.py                                          # regenerate the seat map and lane skills
python3 scripts/build_agents.py --check                                  # fail if the export drifts
python3 scripts/_install_grokbot.py --target /path/to/dest --dry-run     # list grokbot/** paths; write nothing
python3 scripts/_install_grokbot.py --target /path/to/dest               # copy, then write the sha256 stamp
python3 scripts/_install_grokbot.py --target /path/to/dest --check       # exit 1 if the copy would change
```

The installer writes a stamp at `grokbot/.agent-swarm-grokbot.json` with the sha256 of each file it wrote, including the dispatch skill. It refuses a symlink on the way, a target inside this checkout or equal to `$HOME`, and any differing file it did not previously write. It also refuses a source whose seat map names a lane with no skill file, so a partial export cannot delete an installed lane skill. It never deletes a file the stamp does not record. A failed install restores the previous files through the same staged replace the Cursor installer uses.

Desk Lead installs this onto the box and onto programming-desk only after Ming approves that copy. This repository does not run the installer against either of those trees.

## Suggested reading order

Operators: 01 → 04 → 05 → 06 · Agent developers: 02 → your agent spec in 03/ → 07 · Auditors/security: agent §7 sections + 06 §S · Integration work: 02 → 04.
