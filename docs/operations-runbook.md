# Operations runbook

Commands assume the repo root. The registry command is `python3 -m swarm.tool_registry`. Docs and contract tests are generated; do not hand-edit `docs/tools/`, `docs/skills/` or `tests/generated/test_tool_contracts.py`. The v2.2 runtime (scheduler, telemetry, handover, skills, bar) is described in [swarm-command-center.md](swarm-command-center.md).

CI (`.github/workflows/ci.yml`) has six jobs on every push to `main` and every pull request, all on the Omega runner pool: `test` (`ruff`, `pytest`, `build_agents.py --check`, `scripts/_check_pins.py`, the tool registry `check`, `docs --check` and `gen-tests --check`, the skill registry `check` and `docs --check`, `scripts/_registry_intake.py --check`, both TS suites), `rust` (build and test), `clippy` (`cargo fmt --check` and clippy), `parity-rust`, `browser` and `supply-chain`. `.github/workflows/wheel.yml` (job `wheel`, then `install-and-gate`) is the one GitHub-hosted workflow. Commands, knobs and rollback are in "CI gates" below. `.pre-commit-config.yaml` runs the registry, skill and intake checks (not the TS suites or pytest) before a commit when the hook is installed. Both are read-only: drift fails instead of silently regenerating files. CI remains the enforced branch gate.

## Enable the commit-time drift gate

Install the optional `pre-commit` development tool, then run:

```bash
pre-commit install
pre-commit run --all-files
```

The hooks use the existing `python3` runtime, pass no filenames, and run even when the staged files are unrelated to the registry. At commit time, pre-commit temporarily stashes unstaged changes, so the checks see the staged snapshot. If a hook fails, regenerate the docs and contract tests using the commands below, stage the generated files with the registry change, and retry.

Without pre-commit, run `python3 -m swarm.tool_registry check`, `docs --check`, and `gen-tests --check` locally before pushing. The swarm runtime does not depend on pre-commit, and the existing CI checks remain active.

## CI gates

No job has run on GitHub yet: the first run happens at the pull request that ships this branch. Until then the local equivalents below are the evidence, and everything in "Pool unknowns" is unconfirmed. Every pool job runs on `[self-hosted, linux, x64, omega-ci]`, is skipped on pull requests from forks (`head.repo.full_name == github.repository` guard), and starts with `scripts/ci_host_facts.sh`, which prints kernel, glibc, CPU count, load, memory, disk, `HOME` writability, tool versions (`rustup cargo rustc python3 node bun google-chrome pkg-config`), `Py_ENABLE_SHARED`, the OpenSSL version and egress probes, and always exits 0. The Rust jobs then run `scripts/ci_rust_toolchain.sh` (installs the channel pinned in `rust-toolchain.toml` with `rustfmt`, `clippy` and the `wasm32-unknown-unknown` target; exits 1 when `rustup` is absent). Timeouts: 120 minutes for `test`, `rust`, `clippy` and `parity-rust`, 30 for `browser` and `supply-chain`. Run the commands from the repo root.

| Job | Local equivalent | A failure means |
| --- | --- | --- |
| `test` | `SWARM_PURE_PYTHON=1 python3 -m pytest`, plus the other steps of the job in order: `ruff check .`, `python3 scripts/build_agents.py --check`, `python3 scripts/_check_pins.py`, `python3 -m swarm.tool_registry check`, `... docs --check`, `... gen-tests --check`, `python3 -m swarm.skill_registry check`, `... docs --check`, `python3 scripts/_registry_intake.py --check`, `bun test tests/ts`, `cd omp && bun run test` | A test, lint, pin or drift check failed; the step name says which. Drift: regenerate as in "Regenerate docs" and commit the result. Install `pytest ruff pyyaml cryptography` into a venv first, as the job does |
| `rust` | `cargo build --workspace --locked`, then `cargo test --workspace --locked --no-fail-fast` | The workspace does not build or a Rust test failed (`--no-fail-fast` lists every failing test binary). Needs Python 3.12 with a shared libpython, `libssl-dev` and `pkg-config`. |
| `clippy` | `cargo fmt --all --check`, then `cargo clippy --workspace --all-targets --locked -- -D warnings` | A file is not rustfmt-formatted (`cargo fmt --all` fixes it) or clippy reports any warning, which is an error here |
| `parity-rust` | `scripts/ci_parity_rust.sh <out dir>` | The Python truth leg and a Rust leg (host or sandbox backend) of `tests/parity/harness.py` disagree on a case, a leg did not end in `N/N cases pass` with N at least 11, or the read-route fault tests failed. The read-route budget test is a record, not a threshold. Exit 2: no out dir given. The records land in `<out dir>`; CI uploads them as `parity-out`. When `import swarm._native` fails the script builds it with `maturin develop` into a venv (needs PyPI) and leaves a gitignored `swarm/_native*.so` in the tree |
| `browser` | `cd web && npm ci --ignore-scripts`, then `python3 scripts/_ci_browser_gate.py` | Exit 1: `tests/acceptance/browser_headers.mjs` or `browser_sab.mjs` exited non-zero, printed a `FAIL` line, printed fewer than 5 (headers) or 20 (sab) `PASS` lines, or took over 300 s. Exit 3: the gate did not run (no Chrome, or no node); that is a runner problem, not a code failure |
| `supply-chain` | `bash scripts/ci_cargo_deny.sh [report]` (report defaults to `./deny-report.txt`) | Exit 1 or 2 before cargo-deny: an ignore in `deny.toml` is malformed, expired or too far out (exit 2: `deny.toml` unreadable). Otherwise `cargo deny --locked check` found an advisory, a yanked crate, a disallowed licence or an unknown registry or git source; the script's own exit 2 means cargo is absent, the pinned cargo-deny could not be installed, or its SHA-256 did not match. Downloads cargo-deny 0.20.2 from GitHub, and cargo-deny fetches the advisory database from github.com |
| `wheel` (`wheel.yml`) | `scripts/ci_build_wheel.sh dist` (needs Docker and the pinned toolchain installed with rustup; `SWARM_WHEEL_NO_DOCKER=1` builds directly on a manylinux_2_28 host), after `mypy` (strict, repo config) passes | `mypy` found a type error, or the abi3-py312 `manylinux_2_28` x86_64 wheel did not build or exceeded the glibc 2.28 floor |
| `install-and-gate` (`wheel.yml`) | In a clean Python 3.12 environment: `python -m pip install dist/*.whl mypy==1.18.2 pytest==9.1.1`; from a directory outside the repo run `python -c "import swarm._native"` and `python -m mypy.stubtest swarm._native`; then, in a directory holding only `tests/parity/test_extension_parity.py`, `tests/parity/test_extension_threads.py` and `scripts/orch_status.py` (no `swarm/` package), run `python -m pytest -o addopts="" -q tests/parity/test_extension_parity.py tests/parity/test_extension_threads.py` | The installed wheel does not import, its stubs disagree with the extension, or a parity test fails against the wheel instead of the source tree |

### Knobs

- `CHROME_PATH`: when set and non-empty it is the only Chrome candidate and must be an executable file; there is no fallback to `PATH`, so a typo cannot test another browser (the gate exits 3). Unset, the gate takes the first of `google-chrome`, `google-chrome-stable`, `chromium`, `chromium-browser` on `PATH`.
- Repository variable `BROWSER_GATE=skip` (Settings, Secrets and variables, Actions, Variables): the `browser` job is skipped without a code change. A skipped job shows as skipped and is not evidence that the browser gate passed.
- Browser gate exit code 3: not run (no Chrome or no node). The job fails red with `browser acceptance NOT RUN` rather than passing silently.
- cargo-deny ignores: `deny.toml` ignores six advisories. cargo-deny has no expiry key, so every ignore reason ends in `expires YYYY-MM-DD` (all six say `expires 2027-01-26`). `scripts/_check_deny_ignores.py` runs first in `ci_cargo_deny.sh` and fails the job when an ignore has no valid id, no reason or no expiry, when the date has passed, or when it lies more than 120 days ahead (exit 0 valid, 1 violation, 2 unreadable `deny.toml`). Renewing or adding an ignore needs a new user decision; do not push a date out to make the job pass.
- `scripts/ci_host_facts.sh`: first step of every pool job; read-only apart from one `HOME` write test, prints no environment, never fails the job. Read its block at the top of a run's log first.

### Roll back a gate

Remove the job from `ci.yml`. If the check is on the repository's required-checks list, remove it from branch protection first: a pull request would otherwise wait forever for a check that no longer reports. Only a repository admin can change that list. Required-checks state is a repository setting and is not recorded in this repo. For `browser` alone, set the repository variable `BROWSER_GATE=skip` instead (and remove it later); no file changes. A job that is red only because the pool lacks a tool (`rustup`, Chrome, `libssl-dev`) is a pool-image request, not a reason to weaken the gate.

### Pool unknowns

Each row is unconfirmed until the first run. The first run's logs (host-facts block and each job's own output) settle them; the plan's evidence contract is the "First-run evidence contract" in `.planning/phases/14.2-ci-evidence-and-runner-decision/14.2-PLAN.md`, to be filled in after the pull request opens.

| Unknown | Needed by | Where the first run shows it |
| --- | --- | --- |
| `rustup` and `cargo` on the image | `rust`, `clippy`, `parity-rust`, `supply-chain` | host facts tool versions; `ci_rust_toolchain.sh` exits 1 with `rustup is not installed` |
| Writable `HOME` and `~/.cargo` | rustup toolchain install, cargo registry and git caches, `pip` | host facts `HOME writable: yes\|no`, `df` of `HOME` |
| Egress to `static.rust-lang.org`, `index.crates.io`, `registry.npmjs.org`, `pypi.org`, `github.com` | toolchain, crates, `npm ci`, `pip`, cargo-deny and its advisory database | host facts egress probes |
| Python 3.12 with a shared libpython | `rust`, `clippy`, `parity-rust` (PyO3 runs and links the interpreter) | host facts `Py_ENABLE_SHARED`; a PyO3 build-script or link error in the cargo step |
| `libssl-dev` and `pkg-config` | every cargo job (`openssl-sys` is built by the workspace) | host facts `pkg-config` and `openssl` lines; an `openssl-sys` build-script error |
| Node | `browser` | host facts `node`; `actions/setup-node` step |
| Chrome | `browser` | the gate's `chrome: <path> (<version>)` line, or exit 3 `NOT RUN: no Chrome`; host facts probes only `google-chrome` |
| Whether `~/.cargo` and `target` persist between jobs | the length of every Rust job (all build cold today) | not printed; compare the compile time of consecutive runs |
| How many jobs run at once | job duration, saturation | host facts `loadavg` and `nproc`; start times of the six jobs |

On the pool the `test` job took about 69 minutes (per its comment in `ci.yml`), and an earlier run on `main` was cancelled at 32.6 minutes with the cause unknown; the Rust jobs build cold and can run long.

### The wheel exception

`wheel.yml` is the one workflow on GitHub-hosted `ubuntu-latest` (decision D-P22, user, 2026-10-28), an exception to the rule that every job runs on the Omega pool. The manylinux wheel is built inside a Docker container (`scripts/ci_build_wheel.sh`), the `install-and-gate` job runs in `python:3.12-slim-bookworm`, and the pool's runners are unprivileged containers without Docker. Not chosen: `maturin --zig` on the pool, a Docker runner on the VPS `srv1778002`, and no wheel job. The workflow uses no secrets and a read-only token.

## Validate the omp shell guard

From `omp/`, run `bun run test`, not a bare full `bun test`: the extension registration test mocks `node:fs` and runs in its own process.

The raw shell scanners retain literal spans by index, and plain unquoted words are reused instead of rebuilt character by character. Quote, escape, comment, heredoc and operator handling is unchanged. The WR-09 regression still checks the original million-byte inputs, the 40× differential cap, and the 3× cap for doubling the same input shape from 400 KB to 800 KB.

The v2.3 guard rows for the Web3 and cluster domains are `chain-broadcast` and `chain-key-material` (capabilities `chain_broadcast` and `chain_key_material`), plus `argocd-change`, `flux-change`, `mesh-lifecycle`, `velero-change`, `managed-cluster-lifecycle` and `kubeadm-lifecycle` under `prod_infra`. A matching command in a swarm session is blocked with `BLOCKED needs: human-approval (<capability>: <rule id>)`.

## Private signer bootstrap


Use an existing operator-provisioned signer; never print it or paste it into an agent prompt.
The trusted signer accepts `SWARM_ED25519_KEY_FILE` (an absolute path; relative paths are rejected), or the conventional
`${XDG_CONFIG_HOME:-$HOME/.config}/agent-swarm/signing-key.hex` when raw signing variables are unset.
The file must be owned by the controller user, have no group/world permissions, and contain
exactly a 64-hex Ed25519 seed with an optional final newline. Symlink components, nonregular
files, unsafe modes, changed-during-read metadata, and invalid contents fail closed with redacted
errors. The conventional containing directory must also be owner-only.

```bash
export SWARM_ED25519_KEY_FILE="$HOME/.config/agent-swarm/signing-key.hex"
export SWARM_REQUIRE_KEY=1
python3 -c 'from swarm.envelope import real_key_configured, signing_config_error; print({"ready": real_key_configured(), "error": signing_config_error()})'
```

The probe reports readiness only; it does not emit key material. Canonical live/reused planning
signs and verifies the persisted-plan execution assignment before `/swarm` dispatches A01.
Dry-run/keyless/advisory planning is not authenticated execution. Specialist environments omit
raw signing variables and loadable private references, and agent-session flags disable discovery.
OMP receives separate `SWARM_SIGNER_DENY_PATHS` metadata so its guard also protects custom
controller key locations. This deny-only policy cannot load a signer, grants no authority,
and is excluded from prompts, argv, and dry-run diagnostics.

After leasing a specialist task `IN_PROGRESS`, A01 obtains its exact canonical prompt with
`swarm_assignment({task_id, correlation_id})`. The fixed `orch_assign` operation verifies
the persisted task/correlation/manifest target and real signature without changing state,
attempts, history, or logs. Only A01 can call it; no arbitrary code, envelope, agent, payload,
key, or path is accepted. Pass the verified prompt unchanged to the specialist.
Missing, unleased, child/advisory, mismatched, and dry-run requests fail closed.
Generic code-execution devices remain forbidden; this operation is not an eval exception.

Use normal OMP `write` approvals or `--approval-mode always-ask`; do not substitute a bypass.
Grok's unsupported headless transport is rejected before claims. The lexical shell guard protects
deterministically resolved signer reads, but is not an OS sandbox for opaque code or symlink aliases.
Run only trusted controller code and preserve normal approvals. Restart the OMP process to load
updated extension code; changing a separate shell cannot refresh an already-running host.


## Add a tool or a skill

Tools and skills are added through intake fragments, never by editing `agents.json` by hand. Full steps with the field list and limits are in [swarm-command-center.md](swarm-command-center.md) (sections 9.2 and 9.3). In short:

1. Handler `fn(payload) -> dict` in `swarm/tools/aNN_tools.py`, wrapped by `support.respond`. Owner prefix from `PREFIX` in `swarm/tool_registry.py` (`orch_`, `req_`, `arch_`, `ux_`, `be_`, `fe_`, `data_`, `qa_`, `rev_`, `sec_`, `devops_`, `rel_`, `obs_`, `maint_`, `docs_`, `cloud_`, `plat_`, `supply_`, `event_`, `finops_`, `k8s_`, `gitops_`, `mesh_`, `kpol_`, `resil_`, `sol_`, `audit_`, `chain_`, `dapp_`, `custody_`, `shared_`).
2. Fragment `registry/intake/<slug>.tools.json` (complete `swarm.tool.v1` entries; `<slug>` may be `shared`), and `registry/intake/grants.json` for extra callers of an existing tool.
3. `python3 scripts/_registry_intake.py --check`, then `--apply`.
4. `python3 -m swarm.tool_registry check`, `docs`, `gen-tests`, then `python3 scripts/build_agents.py`.
5. `python3 -m pytest -o addopts='' tests/generated/test_tool_contracts.py tests/test_capability_acceptance.py tests/test_tools_aNN.py tests/test_tools_aNN_existing.py` (the second file exists for the v2.1 tools of that agent; a new agent gets only `test_tools_aNN.py`). Tests never write into the checkout's `.swarm/`: `tests/conftest.py` gives each test its own `SWARM_DIR`.

A skill is `prompts/skills/<owner>/<short>.md`: `python3 -m swarm.skill_registry check`, then `python3 scripts/build_agents.py`, `python3 scripts/_write_skills.py` and `python3 -m swarm.skill_registry docs`.

Each agent holds 10 to 15 lane tools and 10 to 15 skills (`agents.json` `capability_policy`); the check fails outside that range. Do not invent tools or skills to reach a count: each needs a purpose, a contract and a test.

A shared tool has `owner: shared`, a `shared_` name and goes to `shared_tools` (the merge puts it there). `shared_baseline` tools must permit all 30 agents (validated). For any other shared tool, list the agents that already perform the operation; by convention a one-caller helper stays a lane tool (not validated).

## Change ACP permissions

Permissions are the manifest, not a second file.

- To allow a message, add the type to the sender's `produces` and the recipient's `consumes`. Both sides are required. Removing either side denies the message.
- To allow a tool, add the agent id to that tool's `permitted_callers`.
- `python3 -m swarm.tool_registry check` after the edit. `routing_table()` in `swarm/acp.py` picks up the new rows on the next process start. There is no peer list to update.

A denied message or tool returns `UNAUTHORIZED` and an audit entry `{caller, target, reason}`.

## Diagnose a dead or timing-out peer

1. Read the bus record `state`, `attempts`, `reason`, and `history`.
2. `DEAD_LETTERED` means the retry budget ran out. Open `Bus.dead` and read the stored envelope. The payload is intact. Compare `correlation_id` with the sender's log.
3. `reason: circuit open` means this peer already failed `breaker_threshold` times. Later calls do not wait for `timeout_hint_s`. Fix or restart the peer handler, then construct a new `Bus` (the breaker is in-memory).
4. Confirm the sender's own tool still works: `python3 -m swarm.tool_registry call --name <lane tool> --caller <agent> --input '<json>'`. A local SUCCESS next to a dead peer is expected.
5. `INVALID_INPUT` and `UNAUTHORIZED` are not peer health. Fix the envelope or the matrix. They do not move the breaker.

## Delivery states

| State | Operator action |
| --- | --- |
| `RESPONDED` | None. Match `correlation_id` to the request. |
| `INVALID_INPUT` | Read `errors` or `reason`. Fix the sender. Do not retry by hand more than once; the bus will not. |
| `UNAUTHORIZED` | Read the audit reason. Change `produces` / `consumes` or `permitted_callers` if the call should be legal. |
| `TIMED_OUT` | Intermediate. The bus already schedules the next attempt. |
| `FAILED` | Peer missing, handler crash, or circuit open. Read `reason`. |
| `DEAD_LETTERED` | Retry budget finished. Replay only after the peer is healthy, using the retained envelope. |

## Regenerate docs

```bash
python3 -m swarm.tool_registry check
python3 -m swarm.tool_registry docs
python3 -m swarm.tool_registry gen-tests
python3 -m swarm.tool_registry docs --check
python3 -m swarm.tool_registry gen-tests --check
python3 -m swarm.skill_registry docs
python3 -m swarm.skill_registry docs --check
```

`--check` exits 1 and prints the drifted paths. Commit the regenerated catalog, agent pages, and contract tests together. The pages always contain identity, handoffs, lane tools, shared tools, the ACP profile, and degradation. Empty lanes say so in a sentence; they do not use placeholder headings. The skill docs (`docs/skills/catalog.md`, `docs/skills/agents/<owner>.md`) are generated the same way.

## Skill regeneration

Sources are `prompts/skills/<owner>/<short>.md`; everything under `omp/skills/` and `skills/` is generated.

```bash
python3 -m swarm.skill_registry check        # names, tools, sections, counts, near-duplicates; one message per problem
python3 scripts/build_agents.py              # omp/skills/<name>/SKILL.md (hidden) and each agent's index skill
python3 scripts/_write_skills.py             # skills/** and omp/skills/**; validates first, writes nothing on an error
python3 -m swarm.skill_registry docs         # docs/skills/**
python3 scripts/build_agents.py --check      # drift gate
```

`build_agents.py` refuses an autoload name that has no generated skill. A skill the check rejects is not rendered. If `check` reports `names no tool or script it uses`, every numbered step of `## Procedure` must cite a registry tool from `tools:` or a host tool (`read`, `grep`, `bash`, ...).

## Registry intake

`registry/intake/` holds fragments that are not yet in `agents.json`: `<slug>.tools.json` (array of tool entries) and `grants.json` (`{tool: [agent ids]}`). Normally it is empty.

```bash
python3 scripts/_registry_intake.py --check   # validate the registry as it would be after the merge; changes nothing
python3 scripts/_registry_intake.py --apply   # merge into agents.json, then delete the applied fragments
```

- Both modes print every error and exit non-zero; `--apply` then changes nothing (no byte of `agents.json`, no fragment).
- `--apply` holds an exclusive `flock` on `registry/intake/.lock` from planning to the last delete. Two applies queue; the lock dies with the process, so a leftover `.lock` file is harmless and never needs deleting.
- A fragment edited while an apply runs is kept for the next run. A crash between the `agents.json` write and the delete is repaired by the next run (the already merged fragment is reported as applied and removed).
- After `--apply` run the tool-registry `check`, `docs`, `gen-tests` and `scripts/build_agents.py` (see above); unauthorized-caller examples are rewritten by the merge when a grant makes the old probe an authorized caller.

## Read `live.json`

`<swarm_dir>/live.json` (schema `swarm.live.v1`) is what the bar and `scripts/swarm_live.py` read.

```bash
python3 scripts/swarm_live.py                 # table; first line is the swarm summary
python3 scripts/swarm_live.py --json          # {"source": "live.json" | "task-store", "live": {...}}
python3 -c 'from swarm.telemetry import read_live; print(read_live())'   # None when absent or malformed
```

- `swarm.state` is `idle` (no runs), `running` (any run running, handing off, starting, queued or waiting), `failed` (nothing live and a run failed or timed out) or `completed`. `queued` counts queued and starting runs, `running` counts running and handing-off runs. `max_parallel` is the cap, `effective_parallel` the cap after the rate-limit and failure-circuit steps; `degraded` names the active fallback or is `null`.
- Per run: `elapsed_s` (counted from the queue time, restarted when the process starts), `state_elapsed_s` in the current state, `since_activity_s` since the last tool or turn (omp reports tools; claude and grok are pinged), `stalled`, `activity`, `last_tool`, `error`. Values are producer-monotonic as of `updated_wall`; do not subtract wall clocks to get elapsed time.
- `source: task-store` means no scheduler wrote a fresh file (older than three heartbeats, i.e. `3 x SWARM_HEARTBEAT_S`): the view was derived from the Task Store and `events.jsonl`. `producer` in the file tells who wrote it: `scheduler` or `derived`.
- A missing file is normal before the first run or transition. A file that never changes while tasks are `IN_PROGRESS` means the runner is gone; check `pgrep -af swarm_run.py`.
- The omp bar picks up a `live.json` written by another process (a detached run, a second terminal) within `SWARM_BAR_IDLE_POLL_MS` (default 2000 ms, clamped 500..30000): while idle it only `stat`s the file at that period, and the full read and redraw run only when the file changed.

## Stalled agent

1. `python3 scripts/swarm_live.py`. A row marked `stalled` is `running` with no activity for longer than `SWARM_STALL_AFTER_S` (180 s). Only omp sessions stream activity; claude and grok sessions are pinged while their process lives and do not read as stalled, so for them use `--task-timeout`.
2. Read the evidence: `grep swarm.run.stalled "$SWARM_DIR/events.jsonl"` (one event per stall episode, with `since_activity_s`), the last `swarm.run.activity` of that `run_id`, and `results/<task_id>.a<N>.md` (partial output; the runner redacts secrets from it before writing, so a `[REDACTED]` there is expected; the redactor is best effort, see "Redaction" in [swarm-command-center.md](swarm-command-center.md), so treat the file as sensitive and do not paste it into tickets unreviewed).
3. A session that is busy but quiet (one long tool call) is not broken: raise `SWARM_STALL_AFTER_S` for that run instead of killing it.
4. A wedged session is ended by `--task-timeout` (default 1800 s): its process group gets SIGTERM, then SIGKILL after 10 s, the partial output is kept and the task fails `E-TIMEOUT` and is retried while attempts remain. To end it sooner, cancel the swarm (next section).
5. After a hard kill, tasks may be left `IN_PROGRESS` with `notes.running`; the next `swarm_run.py` fails them `E-TIMEOUT: stranded` once `notes.running_deadline` has passed and retries them.

## Cancel a swarm

```bash
kill -TERM <runner pid>        # or SIGINT / SIGHUP; a second signal skips the wait and kills at once
python3 scripts/swarm_run.py --repo /path/to/codebase --swarm-timeout 3600   # cancel by itself after an hour
```

1. The runner writes `<swarm_dir>/cancel/<correlation_id>` (`{requested_wall, reason, grace_s}`) and dispatches nothing new. Sessions that call `shared_cancel_check` see it and hand over what exists (`swarm-cooperative-cancel`).
2. After the wait the runner kills the remaining process groups (SIGTERM, then SIGKILL) and ends every run record (`cancelled`, or `timeout` for `--swarm-timeout`). Unfinished tasks end FAILED with `E-TIMEOUT: runner shutdown` (or `swarm timeout`) and are retried on the next run. The runner exits `128 + signal`.
3. The wait is `min(SWARM_CANCEL_GRACE_S, SWARM_SHUTDOWN_BUDGET_S - 8)` seconds: 7 s with the defaults 10 and 15. Raise `SWARM_SHUTDOWN_BUDGET_S` (not only the grace) to lengthen it. `hooks/autonomous_run.py` sets the budget from `SWARM_AUTONOMOUS_RUN_GRACE_S` (15) and SIGTERMs the runner at `SWARM_AUTONOMOUS_RUN_CAP_S` (3600).
4. Confirm nothing is left: `pgrep -af 'omp -p|claude -p'` shows no session of this run, and `python3 scripts/orch_status.py` shows no task you did not expect in `IN_PROGRESS`. The flag is removed when the runner exits; a stale one is cleared when the next run of that plan starts.

## Rate limits and backpressure

1. A provider rate limit (HTTP 429, "rate limit", "overloaded", `Retry-After`) halves the effective parallelism once per burst (floor 1) and writes `swarm.degraded` with mode `reduced_parallelism`. One slot returns per successful run after `SWARM_RATE_COOLDOWN_S` (60 s); when the cap is whole again the degrade is cleared (`mode: recovered`).
2. Look: `grep -E 'swarm.degraded|swarm.scheduler.tick' "$SWARM_DIR/events.jsonl"` (the tick's `effective_parallel` against `max_parallel`, `skipped` with the reason each ready task waits: `no-free-slot`, `class-ceiling:<agent>`, `claim-conflict:<task>`, `lease:<status>`, `backoff:<s>`, `internal-failure`) or the `slots R/L` of the summary line.
3. The failed run is retried after `notes.retry_after` = capped exponential backoff (`SWARM_RETRY_BASE_S` 2, `SWARM_RETRY_CAP_S` 120, a provider `Retry-After` can lengthen it up to the cap) while attempts remain. A task left in `backoff` is not stuck: the loop sleeps to the earliest retry.
4. A task that keeps hitting limits: lower the global cap (`--max-parallel 2` or `SWARM_MAX_PARALLEL=2`) rather than raising the cooldown. One failed task does not reduce parallelism (it follows the retry ladder). `SWARM_FAIL_STREAK` (default 3, `0` off) consecutive failures of distinct tasks with no success in between halve it once (`swarm.degraded` with `reason: consecutive_failures`, `mode: reduced_parallelism`); a success resets the streak and recovers one slot after `SWARM_RATE_COOLDOWN_S`.
5. `SWARM_SCHEDULER=rounds` runs the old per-round barrier if you suspect the continuous scheduler; report what you saw.

## Handover verification failures

`shared_handover_verify {task_id}` (and the bar-less `python3 scripts/swarm_tool.py call --caller A08 --name shared_handover_verify --input '{"task_id": "<id>"}'`) returns `status`, `reasons`, `ready` and `upstream`.

| `status` | Meaning | Fix |
|---|---|---|
| `missing` | no file for that task id | Name the producer's task id; a fresh dependent task has none of its own. With every upstream task satisfied nothing was handed over and the task continues from its assignment; with an upstream task still open `swarm-handover-intake` escalates with reason `blocked` |
| `partial` | no remaining work, no artifacts and `verification.status` unverified | The writer adds `remaining` or `artifacts` and writes again |
| `stale` | older than `SWARM_HANDOVER_STALE_S` (21600 s), or written under another correlation than the task's plan | Re-run the producer; raise the limit only for a deliberately long pause |
| `malformed` | invalid schema or digest mismatch (edited or truncated file) | Never edit `handovers/**` by hand; the producer writes a new `seq` |

- `ready` is false while an upstream task is not yet reviewed or approved, whatever the handover says.
- A write is refused with `INVALID_INPUT` and the field (`/artifacts/0/digest`, `/to_agent`, `from_agent`): `from_agent` must be the calling agent id, ids are agent ids (`A05`), digests are `sha256:` plus 64 hex. If the mandatory sections (`goal`, `remaining`, `blockers`, `open_questions`, `constraints`) alone exceed `SWARM_HANDOVER_MAX_BYTES` (16384) the write is refused: shorten them.
- A receipt is refused when the digest matches no valid handover of that task id or it is addressed to another agent.
- Files live in `<swarm_dir>/handovers/<correlation_id>/<task_id>.<seq>.json`; receipts next to them as `<task_id>.receipt.<agent>.<digest16>.json`. `swarm.run.handover` in `events.jsonl` records each write.

## A registry call came back `PARTIAL_SUCCESS` with `output_withheld`

`swarm_tool_call` and `python3 -m swarm.tool_registry call` never return a result larger than `SWARM_TOOL_OUTPUT_MAX_BYTES` (default 262144, clamped 4096..16777216). A larger result is withheld, not truncated: the state is `PARTIAL_SUCCESS` with `completed_fraction: 0.0`, `output_withheld: true`, `output_bytes` (what it would have been), `limit_bytes` and a `message`. Narrow the input (fewer files, a shorter text, a tighter filter) and call again; raise the variable only for one deliberate caller. Related refusals: a `number` input that is NaN or Infinity is `INVALID_INPUT` (`expected a finite number`); a handler result that is not strict JSON is `DEPENDENCY_UNAVAILABLE`. The `swarm.tool.call` audit row (`grep swarm.tool.call "$SWARM_DIR/events.jsonl"`) is written only when `$SWARM_DIR` already exists or the tool is WRITE tier, so a READ call through `python3 -m swarm.tool_registry call` in a fresh workspace leaves no `.swarm/` behind (`scripts/swarm_tool.py` writes its own `script.swarm_tool` row and does create it).

## Select the engine for `orch_status` reads

The read routes `orch_status` list and `orch_status --history TASK_ID` can run on the Rust engine (`gsd_engine`). The default is `python`; no route is flipped by default. `--transition` and `--ingest` never run on Rust: the binary refuses them with `E-DEP` and Python answers. The evidence and open items are in [tests/parity/flip_record.md](../tests/parity/flip_record.md).

Writes stay in Python by design (OD-13; `.planning/phases/12-port-units-and-strangler-flips/12-PLAN.md:15`). A mutating argv reaching the binary (`--transition` or `--ingest`) is refused with `E-DEP` before any store read, and `E-DEP` is a fallback code, so the Python body runs the call once. The binary applies no transition and no ingest, so a fallback never applies a write twice. Its read path does run the idempotent schema statements in `open_store` (`crates/gsd-adapter/src/strangler_seam.rs`): they create the Swarm state directory and `tasks.db` when missing, and add the verdict envelope columns when absent.

`scripts/orch_status.py` reads three variables:

- `GSD_ENGINE`: unset, empty or `python` runs the Python body. `rust` asks the binary first. Any other value exits 2 with `E-INPUT` and does not echo the value.
- `GSD_ENGINE_BIN`: the built `gsd_engine` binary. Required with `GSD_ENGINE=rust`. Missing or not startable: exit 2 with `E-DEP`, no Python fallback.
- `GSD_RUST_BACKEND`: `host` (or unset) runs host Rust. `sandbox` runs the Wasm guest first. Read only when `GSD_ENGINE=rust`; any other value exits 2 with `E-INPUT`, no Python fallback. `sandbox` stays opt-in until the sign-offs below are recorded.

Fallback order for one call under `GSD_ENGINE=rust`:

1. The selector checks `GSD_RUST_BACKEND`, then starts `GSD_ENGINE_BIN` with one stdin object `{agent, script, argv, root, env}`.
2. With `sandbox`, a guest fault other than a timeout is answered by host Rust from the same snapshot, inside the binary (GA3). A reply the guest itself reports as an error passes through. A memory refusal (`E-CAPACITY`), a module that fails to load or pin-check (`E-DEP`) and any other trap that is not a deadline (`E-INTERNAL`) all fail over. Fuel exhaustion does not: the read route always sets an epoch deadline, so `classify` (`crates/gsd-sandbox/src/wasmtime_limits.rs`) maps fuel exhaustion to `E-TIMEOUT`, and the binary returns that error with no fallback. `E-TIMEOUT` never fails over.
3. Exit 0 or 1: the binary's stdout and exit code are printed.
4. Exit 2 with `E-INTERNAL`, `E-DEP`, `E-CAPACITY`, `E-CONTRACT`, `E-INPUT`, or no code: the Python body runs and prints its own bytes, which are text unless the caller passed `--json`. A memory refusal on the sandbox path fails over in step 2 and does not reach this step.
5. Exit 2 with any other code, `E-TIMEOUT` included: the binary's reply is printed and the exit is 2. Python does not run.
6. Any other exit code, or stdout that is not one JSON object (for example a crashed binary): the Python body runs.

`E-TIMEOUT` is never retried (D-P5-21: `invoke` never retries) and never falls back (`is_timeout` in `crates/gsd-adapter/src/sandbox_engine.rs`). Do not wrap the call in a retry loop. To get the Python answer, run the same call with `GSD_ENGINE` unset; the selector does not do that itself.

Build the binary, then run the read routes on it:

```
cargo build -p gsd-adapter --bin gsd_engine
GSD_ENGINE=rust GSD_ENGINE_BIN="$PWD/target/debug/gsd_engine" python3 scripts/orch_status.py --correlation-id <id>
GSD_ENGINE=rust GSD_RUST_BACKEND=sandbox GSD_ENGINE_BIN="$PWD/target/debug/gsd_engine" python3 scripts/orch_status.py --history <task_id>
```

Parity harness, Python leg and Rust leg:

```
python3 tests/parity/harness.py
GSD_ENGINE_BIN="$PWD/target/debug/gsd_engine" python3 tests/parity/harness.py --rust
```

Each exits 0 only when all 11 cases pass. `--rust` exits 1 when `GSD_ENGINE_BIN` is unset, not executable or cannot start; it never skips. A byte mismatch writes `tests/parity/fixtures/<case>-<digest>.json` and fails the case.

Known differences from the Python route. They are recorded here, not fixed:

- The binary's own answer is always JSON, because it appends `--json` to argv itself.
- A fallback to the Python body (steps 4 and 6) runs `scripts/orch_status.py` with the caller's argv and no added `--json`, so it prints text unless the caller passed `--json`. Under `GSD_ENGINE=rust` the output form depends on which side answers.
- The Rust list path writes no run-log event. The Python path emits `script.orch_status`.
- The binary applies a 600 s per-call deadline (`crates/gsd-adapter/src/bin/gsd_engine.rs`). The selector sets no timeout on the subprocess.
- The binary reads stdin without a size cap.
- Under `GSD_ENGINE=rust`, a misspelt `GSD_RUST_BACKEND` fails closed with `E-INPUT` and exit 2, with no Python fallback. The variable is read only under `GSD_ENGINE=rust`; otherwise it is ignored.
- The default route is `python`. `parse_engine(None)` stays `Python`.

Open sign-offs:

- A10 runtime sign-off: open (`plan/wasmer-spike.md:654`).
- A11 approval of the pin change: open (`plan/wasmer-spike.md:55`, `:572`).
- A2 quiet-host performance baseline: waived (`.planning/ROADMAP.md:82`) and not measured. A default flip needs it.
- `GSD_RUST_BACKEND=sandbox` stays opt-in until the A10 and A11 items are recorded.

## Rust redaction engine (opt-in)

`swarm.redact` finds secret spans in pure Python by default. Set `SWARM_REDACT_ENGINE=rust` in the runner's environment to compute them in `swarm._native.redact_spans` instead (crate `gsd-redact`, bound by `gsd-python`). The Python module stays the oracle: the Rust spans must equal its spans for every input. `redact()` and `redact_text()` behave the same under either engine. Unset, empty or `python` is the default.

Fallback, so redaction never fails on configuration: `SWARM_PURE_PYTHON=1` wins over `rust` (one warning). Any other value warns once and uses Python. A missing extension, an extension without `redact_spans` (stale build) or an exception from the Rust call uses Python for that call. Each reason logs one `swarm.redact` warning per process. A string holding a lone surrogate cannot be encoded for Rust and is redacted by Python without a warning. A Rust panic surfaces as an `E-INTERNAL` `ValueError`, which the adapter treats as a failed call.

## Task Store journal mode (opt-in WAL)

The default is the rollback journal. Set `SWARM_TASKSTORE_WAL=1` in the runner's environment to make `TaskStore` open `tasks.db` in WAL mode with `synchronous=NORMAL` (set on open, fail-open to the current mode; any other value does nothing).

Measured effect: 400 commits run 2.0x faster wall (5.17 to 2.58 ms per op) and a 13-task dry run 1.25x faster wall. Real runs are dominated by model sessions, so the gain there is small.

Durability: a committed transition survives a process crash; after an OS crash or power loss the last few commits can roll back while the file stays consistent. `tasks.db-wal` and `tasks.db-shm` sit beside the database while a connection is open, so the directory must be writable, and `.swarm/.gitignore` already ignores them. A read-only opener (`StoreReader`, `ReadOnlyTaskStore`) also creates those side files. Never copy `tasks.db` alone while a connection is open: copy it with its `-wal` file, or close every connection first.

Check it: `sqlite3 .swarm/tasks.db 'PRAGMA journal_mode'`. The mode is stored in the database file, so once set it stays after the variable is unset or the code is reverted. To revert: `sqlite3 .swarm/tasks.db 'PRAGMA journal_mode=DELETE'` with no other connection open.

Known interaction: with WAL on, `tests/test_scheduler_continuous.py::test_a_success_between_failures_resets_the_streak` fails because commit latency changes retry-versus-priority ordering. Turning WAL on by default needs that test and the two read-only tests (`tests/test_shared_handover.py::test_read_tools_change_nothing_on_disk`, `omp/test/tools.int.test.ts:165`) reviewed by a human.
