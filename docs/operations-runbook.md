# Operations runbook

Commands assume the repo root. The registry command is `python3 -m swarm.tool_registry`. Docs and contract tests are generated; do not hand-edit `docs/tools/`, `docs/skills/` or `tests/generated/test_tool_contracts.py`. The v2.2 runtime (scheduler, telemetry, handover, skills, bar) is described in [swarm-command-center.md](swarm-command-center.md).

CI (`.github/workflows/ci.yml`) has seven jobs on every push to `main` and every pull request, all on the Omega runner pool: `test` (`ruff`, `pytest`, `build_agents.py --check`, `scripts/_check_pins.py`, the tool registry `check`, `docs --check` and `gen-tests --check`, the skill registry `check` and `docs --check`, `scripts/_registry_intake.py --check`, both TS suites), `rust` (build and test), `clippy` (`cargo fmt --check` and clippy), `parity-rust`, `scheduler` (`scripts/ci_scheduler_rust.sh OUT_DIR`, the fail-closed native scheduler gate; it needs the repository variable `SCHEDULER_CONTINUOUS_SHA256` and no feature-branch trigger yet), `browser` and `supply-chain`. `.github/workflows/wheel.yml` (job `wheel`, then `install-and-gate`) is the one GitHub-hosted workflow. Commands, knobs and rollback are in "CI gates" below. `.pre-commit-config.yaml` runs the registry, skill and intake checks (not the TS suites or pytest) before a commit when the hook is installed. Both are read-only: drift fails instead of silently regenerating files. CI remains the enforced branch gate.

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
| How many jobs run at once | job duration, saturation | host facts `loadavg` and `nproc`; start times of the seven jobs |

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

## Select the scheduler engine

The continuous scheduler in `scripts/swarm_run.py` (`Scheduler`) has two engines. The Python `Scheduler` is the default and the oracle. The native runtime is opt-in: `run_scheduler` in the `swarm._native` extension (crates `gsd-sched-core` and `gsd-python`). This section changes no default.

**Status of the switch.** The `GSD_ENGINE` selector for the scheduler is not wired yet (phase 14.4, unit S3.2). `scripts/swarm_run.py` does not read `GSD_ENGINE`, so every swarm run uses the Python `Scheduler` whatever that variable is set to. The extension exports `run_scheduler`, but no production run calls it. `GSD_ENGINE_BIN` is not read by the scheduler: its native runtime is the extension, not the `gsd_engine` binary. The flip record is [tests/parity/flip_record_scheduler.md](../tests/parity/flip_record_scheduler.md); its status is "not flipped".

**The switch once the selector is wired.** The selector reads `GSD_ENGINE` in `run()`, before any dispatch:

| `GSD_ENGINE` | Scheduler |
|---|---|
| unset or empty | the default, `python` (the planned module constant `SCHEDULER_DEFAULT_ENGINE` is added with the selector) |
| `python` | the Python `Scheduler`, the oracle |
| `rust` | the native runtime, composed around the same `Scheduler` controller |
| anything else | refused before anything is dispatched: `E-INPUT`, exit 2, the value is not echoed |

- `SWARM_PURE_PYTHON=1` (exactly `1`) forces Python and wins over `GSD_ENGINE`.
- A stale extension without `run_scheduler` warns once at selection time and the run uses Python. The engine never changes in the middle of a run.
- Both engines log `scheduler_engine: <name>`. Check that line in the run log to see which engine answered.
- `SWARM_SCHEDULER=rounds` is unchanged by this switch. Rounds mode must also match on both engines before any flip (flip record, criterion 6).
- Rollback is one switch: `GSD_ENGINE=python`. Both engines use the same Task Store, leases, handoffs and telemetry, all in Python, so a rollback needs no data migration.

**Architecture B (approved).** Rust owns the worker pool threads and the decision loop, on the calling main thread. Python keeps the rest: session execution (`_worker`, `execute_one`), the Task Store, telemetry (`events.jsonl`, `live.json`), leases, handoffs, signals and abort, the final run result, and signing. The two sides meet only through sixteen Python callbacks: `begin_pass`, `agent_for`, `claims_for`, `block`, `lease`, `observe_pass`, `submit`, `worker`, `internal_failure`, `complete`, `set_retry_after`, `random_unit`, `cancel_state`, `cancel_begin`, `kill` and `log`. Signing keys never enter Rust.

**Approved limits.** These are the exact limits. Nothing wider is approved.

- Priority: an unknown or missing priority is P2, Python's own fallback. Rust does not invent a rank.
- Cooperative cancel: the shutdown budget starts at the Python-observed `CancelRequest`, not at kernel delivery of the signal. A Python callback that is already running is not force-preempted. The budget covers the native side's finite, cancel-aware waits, and callback and GIL delay count against it. A worker stuck after the kill must not keep the process alive. Both points are proved only by the S5.4 tests, which are still pending.
- Panics: a scoped native panic guard. It applies only to marked native scheduler work. A panic there fails that task with `E-INTERNAL`; any other panic goes to the hook installed before it. There is no debug API and no new dependency.
- Keys: keys never enter Rust. Signing and key material stay in Python.
- Speed: no speed-up is claimed. Python stays the default until both paired p50 and paired p95 ratios (Rust over Python) are strictly below 1.00 on a qualifying quiet host (1-minute load below 1.5 for ten minutes), and the user has explicitly approved the flip, recorded in `.planning/STATE.md`. A ratio of exactly 1.00 fails. The existing A2 records are moderate-load runs and do not count.

**What is still open.** The selector (S3.2), the port tests (`tests/test_scheduler_ports.py`), the thread and stuck-worker tests (`tests/parity/test_scheduler_threads.py`), the local S5 green, the feature-branch CI run, the quiet-host paired A2 run (S6), and the user's approval with an exercised rollback (S7). Until these are recorded, the default is Python and nothing in this section is a flip.

**What operators do now.** Nothing changes: the default is Python. If you suspect the continuous scheduler, use `SWARM_SCHEDULER=rounds` (see above) and report what you saw. Once the selector is wired, use `GSD_ENGINE=rust` only in a test or canary run, and set `GSD_ENGINE=python` to roll back.

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

The read routes `orch_status` list and `orch_status --history TASK_ID` can run on the Rust engine (`gsd_engine`). **Today the default is still `python`: no route is flipped.** The three constants the flip changes are `DEFAULT_ENGINE = "python"` (`scripts/orch_status.py`), `ORCH_STATUS_READ_DEFAULT = Engine::Python` (`crates/gsd-adapter/src/route_table.rs`) and `DEFAULT_BACKEND = Backend::Host` (`crates/gsd-adapter/src/bin/gsd_engine.rs`); nothing else changes the default of the read routes. The write routes (`--transition`, `--ingest`, the gates, `orch_plan` and `orch_assign`) run on Rust only when a call asks for them (`GSD_ENGINE=rust`); each has its own default row, all `python` (see [Write routes](#write-routes)). The evidence, the decisions on the recorded divergences and the open items are in [tests/parity/flip_record.md](../tests/parity/flip_record.md).

Writes stay in Python unless a call asks for a Rust write route: `--transition`, `--ingest`, `qa_gate`, `rel_plan`, `rev_gate`, `sec_gate`, `orch_plan` or `orch_assign` (OD-13 was reopened for the writes by decisions D-P17 and D-P18 of phase 14.5). A mutating argv the binary does not serve (a `--transition` or `--ingest` outside its grammar) is refused with `E-DEP` before any store read, and `E-DEP` is a fallback code, so the Python body runs the call once. A refusal that says `write_state = "not_started"` wrote nothing, so a fallback never applies a write twice. Its read path does run the idempotent schema statements in `open_store` (`crates/gsd-adapter/src/strangler_seam.rs`): they create the Swarm state directory and `tasks.db` when missing, and add the verdict envelope columns when absent.

### Resolution order

`scripts/orch_status.py` (`select_engine`) decides the engine for one call in this order; the first rule that applies wins:

1. `SWARM_PURE_PYTHON=1` (exactly `1`): the Python body answers. Nothing else is read, so a bad `GSD_ENGINE` is not reported.
2. `GSD_ENGINE=python`: the Python body answers. `GSD_ENGINE_BIN` and `GSD_RUST_BACKEND` are ignored.
3. `GSD_ENGINE=rust`: explicit. Every call goes to the binary, the pre-filter below is not applied, and a missing binary is an error.
4. `GSD_ENGINE` unset or empty: the route's default row. For the read routes that is `DEFAULT_ENGINE`, which is `"python"` today, so the Python body answers and the binary is never looked for. For `--transition` and `--ingest` it is `DEFAULT_ENGINE_WRITE` and `DEFAULT_ENGINE_INGEST`; for each gate and plan route it is the `default` of that script's selector block (`"python"`). Once a row is `"rust"`, a default call goes to the binary only when a binary is found **and** the route's pre-filter holds (`rust_serves` for the read route, `rust_serves_write`, `rust_serves_ingest`, or the route's `claims` in the other scripts); otherwise the Python body answers, silently.
5. Any other value: exit 2 with `E-INPUT` ("GSD_ENGINE must be 'python' or 'rust'"), and the value is not echoed.

Where the selector lives. The flow of this order (value parsing, binary lookup, the outer timeout, the read, write and ingest outcome tables, the key-free binary environment) is `swarm/engine_select.py`; a script only describes its routes there as data and hooks (`engine_select.Route` per argv grammar, `Selector` per script). `scripts/orch_status.py` holds the three routes of `orch_status` (the read route, `--transition` and `--ingest`), their default rows (`DEFAULT_ENGINE`, `DEFAULT_ENGINE_WRITE`, `DEFAULT_ENGINE_INGEST`), their grammars and their key grants, and still exports every name it exported before. `orch_plan`, `orch_assign`, `qa_gate`, `rev_gate`, `sec_gate` and `rel_plan` each build their own `engine_select.Route` inline in their `__main__` block. That block runs only when `GSD_ENGINE` is set to a value other than `python` or empty, so an unset, empty or `python` `GSD_ENGINE` never imports the selector. Their `ENGINE_ROUTES` tuple is an empty registry hook: a non-empty value replaces the inline route. A bad `GSD_ENGINE` value is `E-INPUT` as above.

Variables:

- `GSD_ENGINE`: as above.
- `GSD_ENGINE_BIN`: the built `gsd_engine` binary. When it is set it is the only candidate (a path that is not an executable file is not replaced by the release binary).
- `GSD_RUST_BACKEND`: `host` runs host Rust, `sandbox` runs the Wasm guest first; unset or empty is the binary's `DEFAULT_BACKEND` (`host` today). Any other value exits 2 with `E-INPUT` and no Python fallback. The selector checks it whenever it is about to use the binary (explicit `rust`, or a default call that reaches the binary), before it reports a missing binary; it ignores the variable when the Python body answers without the binary. The binary rejects an unknown value itself with the same code. `sandbox` stays opt-in.
- `GSD_RUST_TIMEOUT_S`: see Timeouts.
- `SWARM_PURE_PYTHON`: see rule 1.

### Finding the binary

`GSD_ENGINE_BIN` if it is set and not empty; otherwise `<repo>/target/release/gsd_engine` if that is an executable file; otherwise none. A default call with no usable binary (none found, a file that is not executable, or one that cannot be started) runs the Python body with no error and no message. Explicit `rust` with no binary exits 2 with `E-DEP` ("GSD_ENGINE=rust needs GSD_ENGINE_BIN set to the gsd_engine binary"); with a named binary that cannot be started it exits 2 with `E-DEP` ("could not be started") and does not fall back. The wheel does not ship the binary.

### The pre-filter `rust_serves`

`rust_serves(argv)` is true exactly for the argv the binary serves: the read grammar of `parse_orch_status_read` plus `--json`. Accepted: `--json` and `--dry-run` (neither with an inline `=value`); `--root`, `--repo`, `--correlation-id`, `--task-id` and `--history` with a value, as `--flag=value` or `--flag value` (a separate value must not start with `-`); and `--json` at least once. Refused, so the Python body answers without starting the binary: a positional argument, an unknown or abbreviated flag, `--transition`, `--ingest`, `--advisory`, `--reason`, a missing value, and any argv without `--json` (the binary does **not** append `--json` itself; the SDK bridge and the parity harness do). The parity harness checks this function against the binary's own routing on a corpus of argv. The pre-filter applies to default-resolved calls only, so it has no effect while `DEFAULT_ENGINE` is `"python"`.

### Failover notice

With `GSD_RUST_BACKEND=sandbox`, a guest fault other than a timeout is answered by host Rust inside the binary, and the binary prints exactly one line to its stderr:

```
gsd_engine: sandbox fault <CODE>; answered by host Rust
```

`<CODE>` is the wire code of the fault (for example `E-DEP` or `E-INTERNAL`); the line carries no path, message or environment value. Stdout and the exit code are unchanged. The selector does not capture the binary's stderr, so the line reaches the caller's stderr. A guest answer (an error it reports included) and `E-TIMEOUT` print no notice. The harness records a failover as `fallback:<CODE>`.

### Timeouts

The binary applies a 600 s per-call deadline (`crates/gsd-adapter/src/bin/gsd_engine.rs`); it does not cover the binary's start-up or its stdin read. The selector therefore bounds the whole subprocess at 660 s (`RUST_CALL_TIMEOUT_S`). `GSD_RUST_TIMEOUT_S`, a positive integer number of seconds in ASCII digits, overrides the 660 s; any other value (empty, `0`, negative, decimal, text) is ignored. When the bound passes, Python kills the binary and the selector prints `E-TIMEOUT` (exit 2, "the gsd_engine binary did not answer within the call deadline"). `E-TIMEOUT` is never retried (D-P5-21: `invoke` never retries) and never falls back, explicit or not, and the selector writes no run-log record for it. Do not wrap the call in a retry loop. To get the Python answer, run the same call with `GSD_ENGINE=python`; the selector does not do that itself.

### Fallback table

What the selector (`run_rust`) does with one binary run. The binary's own sandbox failover (GA3) happens before this: a guest fault other than a timeout is answered by host Rust from the same snapshot (`E-CAPACITY` for a memory refusal, `E-DEP` for a module that fails to load or pin-check, `E-INTERNAL` for any other trap that is not a deadline). A reply the guest itself reports as an error passes through. Fuel exhaustion does not fail over: the read route always sets an epoch deadline, so `classify` (`crates/gsd-sandbox/src/wasmtime_limits.rs`) maps it to `E-TIMEOUT`.

| The binary | The selector | Final? |
|---|---|---|
| Exit 0 or 1, stdout is one JSON object | Writes the run-log record (below), then prints the binary's stdout and returns its exit code | Yes |
| Exit 0 or 1, and the run-log record cannot be written | The Python body answers, on its own rules | Python answers |
| Exit 2, `error.code` is `E-INTERNAL`, `E-DEP`, `E-CAPACITY`, `E-CONTRACT` or `E-INPUT` | The Python body answers (this includes every refusal of a mutating or non-JSON argv, a verdict row with a signed envelope, an unknown task and a malformed id) | Python answers |
| Exit 2 with a JSON object that has no typed `error.code` | The Python body answers | Python answers |
| Exit 2, `error.code` is any other code (`E-TIMEOUT`, `E-POLICY`) | Writes a `script.orch_status.error` record, prints the binary's reply, exit 2. Python does not run | Yes |
| Stdout is not a JSON object (crash, garbage, empty), or the exit code is not 0, 1 or 2 | The Python body answers | Python answers |
| The binary cannot be started (missing, not executable) | Default call: the Python body answers. Explicit `rust`: `E-DEP`, exit 2 | Explicit: yes |
| The binary does not answer within the bound | Kills it, prints `E-TIMEOUT`, exit 2, writes no record | Yes |
| An argument cannot be encoded as UTF-8 for the request | The Python body answers without starting the binary | Python answers |

The Python body prints its own bytes, which are text unless the caller passed `--json`; a default call always carries `--json` (the pre-filter), so only explicit `rust` can print text.

### Run-log record (D1)

The Python path writes one `script.orch_status` record to `$SWARM_DIR/events.jsonl` for every result (`script.orch_status.error` for exit 2), and the substrate tee and `scripts/receipt_export.py` read it. The binary writes none. After a Rust answer the selector calls `AgentScript.record_external_reply` (`swarm/script_base.py`), which builds the same record through the same `emit` path, with `tee` off for `--dry-run`, before it prints. Every call therefore appends exactly one record, whichever engine answered. The selector's own refusals (`E-TIMEOUT`, `E-DEP` for a missing binary, `E-INPUT` for a bad `GSD_ENGINE` or `GSD_RUST_BACKEND`) write none.

For a committed `--transition` the binary prints the task row it committed, before the lease settlement the selector replays after it (`notes.lease` released, completed or unsettled, as `substrate_lease.settle_mirrored` leaves it). Python prints and records the row after the settlement, so when the row read after the replay differs from the reply's `task`, the selector replaces that `task` (the key order of the reply is kept) and prints and records the reply as `AgentScript._print` renders `--json`; when the rows are equal, or there is no row to read (a dry run, a lost manifest, a failed replay), the binary's bytes are printed untouched.

### The `--transition` route and APPROVED

`orch_status --transition TASK STATE` runs on the binary only under `GSD_ENGINE=rust` (the default engine of the route is `python` until the flip unit, which needs the user's approval). The rule of the route is that Rust answers only what it can prove byte-identical to Python and refuses every other case BEFORE the first write with `E-DEP` and `error.details.write_state = "not_started"`; the selector then lets the Python body answer once. After a commit nothing is re-run.

**A transition to APPROVED.** The binary reads, inside the writer's own `BEGIN IMMEDIATE` transaction (one snapshot with the write, never before the lock), what `TaskStore.transition` reads (`swarm/taskstore.py`, the `to_state is S.APPROVED` block), and refuses in Python's order with Python's texts, all `E-POLICY`, final, nothing written, never retried:

1. `fail-closed: SWARM_REQUIRE_KEY=1 but no signing key configured`: `SWARM_REQUIRE_KEY=1` and no real key (a loadable `SWARM_ED25519_KEY` seed or a `SWARM_SIGNING_KEY` that is not the public dev key). The dev-key opt-in does not help;
2. `fail-closed: no signing key configured`: no real key, and `SWARM_ALLOW_INSECURE_DEV_KEY=1` does not apply (it needs no Ed25519 seed, loadable or not, and no `SWARM_REQUIRE_KEY=1`);
3. `fail-closed: <task> lacks passing gates [...]`: a required gate (`notes.gates`, else the gates of the risk class) has no current, verified, unexpired `pass` or `waive` row. A row counts only if its signed envelope verifies under the configured keys, names this gate, task, verdict and correlation, was issued after `notes.verdicts_since`, is not a `dry_run` row on a task that was not dispatched in a dry run, and has not expired; the highest id per gate wins;
4. `fail-closed: <task> has unaccepted gates [...]`: a passing row whose signed `gate_task` is not `IN_REVIEW`, `APPROVED` or `DONE` (or does not exist).

With both keys set an `hmac:` row also verifies (as in Python). A transition from a state that cannot reach APPROVED is refused with Python's `illegal transition` text and reads no verdict row (the keys are still read first, so a key-file reference sends even that call to Python, which gives the same text).

**Where the keys come from.** `gsd_engine` reads `SWARM_SIGNING_KEY` and `SWARM_ED25519_KEY` (with `SWARM_ED25519_KEY_FILE`, `SWARM_AGENT_SESSION`, `SWARM_ALLOW_INSECURE_DEV_KEY`, `SWARM_REQUIRE_KEY`, `XDG_CONFIG_HOME` and `HOME`) from its own environment, once per call, only for a transition whose target is `APPROVED`, before the transaction opens. The selector starts the binary with an explicit allowlist of names, never a prefix: `PATH`, `HOME`, `TMPDIR`, `LANG`, `LC_ALL` and `GSD_RUST_BACKEND` for every route, and the key variables above (`APPROVED_KEY_ENV` in `scripts/orch_status.py`) only for a real `--transition T APPROVED` (a `--dry-run` and every other target get none). The swarm directory and the ids travel in the request. Keys never enter the Wasm guest: on the sandbox backend the guest receives two booleans, `require_key`, and gate names, never a key, a signature or an envelope, and the host recomputes the decision and requires the guest's to equal it. `tests/test_keys_never_in_guest.py` and `crates/gsd-adapter/tests/keys_never_in_guest.rs` hold this with canary keys (`ROUTE_KEY_POLICY` is the table `_binary_env` must match).

**The `findings` column and the clock.** Python's `_latest_rows` runs `json.loads(findings)` on EVERY verdict row of the task with `ts >= notes.verdicts_since`, a required gate or not and a `NULL` gate or not, so a `NULL`, empty or invalid `findings` on any of them makes `missing_gates` raise and the transition is refused with Python's own text (`E-INPUT` `JSONDecodeError` or `E-INTERNAL` `TypeError`), even when every signed envelope is intact. The binary makes the same check on the same rows and, when it fails, writes nothing and lets Python answer (a `findings` text Python reads and the binary does not, such as `NaN`, goes to Python too). A row older than `verdicts_since` is never read by either. The expiry of a verdict (`issued_at + expires_s > now`) is compared with a wall-clock reading taken INSIDE the write lock, after the rows were read, as Python reads `time.time()` inside its lock: a verdict that expired while the call waited for the lock (up to the 30 second busy timeout) is expired. The call's own earlier reading stays the `updated_at` and the audit `ts` of the write.

**Core dumps.** The selector starts the APPROVED child, which holds the signing keys in its environment and heap for the length of the call, with `RLIMIT_CORE` 0 (soft and hard, `_no_core_dumps` in `scripts/orch_status.py`, a `preexec_fn`), so a crash leaves no core file that carries them. Every other child inherits the selector's own limit. A child that cannot be given the limit is not started: Python answers by default and the call is `E-DEP` under `GSD_ENGINE=rust`. This does not decide whether a same-user process may read the child's `/proc/<pid>/environ` (a recorded open decision).

**Fallback rows (the binary writes nothing and Python answers).** `SWARM_ED25519_KEY_FILE` is set while no raw key variable is (the binary never opens a key file; the path is not in the refusal); no raw key variable is set and `${XDG_CONFIG_HOME:-$HOME/.config}/agent-swarm/signing-key.hex` exists or cannot be classified the way Python's `O_NOFOLLOW` walk does (a relative `XDG_CONFIG_HOME` or `HOME`, a symlinked or unreadable ancestor); a signing variable is not UTF-8; a verdict row whose envelope text Python reads but the binary cannot represent exactly (a lone surrogate escape, an integer beyond 64 bits, `NaN`, nesting beyond 128); a task row whose gate facts the binary does not reproduce (a risk class outside `low`, `medium` and `high` with no `notes.gates`, `notes.gates` that is not a list of strings, `notes.verdicts_since` that is not a number, a `correlation_id` that is not text); a store with no schema; and, on the sandbox backend, a binary built without the `sandbox-write` feature.

**Audit event.** A transition emits no `security.dev_key` event in Python (only signing does: `sign_envelope` of `swarm/envelope.py`, the gate issue tail), so the dev-key opt-in does not add one here; the parity cells compare the whole run log.

### Build and run

Build the binary, then run the read routes on it (`--json` is needed for the binary to serve the call):

```
cargo build -p gsd-adapter --bin gsd_engine --release --locked
GSD_ENGINE=rust GSD_ENGINE_BIN="$PWD/target/release/gsd_engine" python3 scripts/orch_status.py --repo <repo> --correlation-id <id> --json
GSD_ENGINE=rust GSD_RUST_BACKEND=sandbox GSD_ENGINE_BIN="$PWD/target/release/gsd_engine" python3 scripts/orch_status.py --repo <repo> --history <task_id> --json
```

Parity harness, Python leg and Rust legs:

```
python3 tests/parity/harness.py
GSD_ENGINE_BIN="$PWD/target/release/gsd_engine" python3 tests/parity/harness.py --rust [--backend host|sandbox] [--json-out out.json]
tests/parity/record_parity.sh "$PWD/target/release/gsd_engine" <output directory>
```

Each harness run exits 0 only when all 12 cases pass. `--rust` exits 1 when `GSD_ENGINE_BIN` is unset, not executable or cannot start; it never skips. A byte mismatch writes `tests/parity/fixtures/<case>-<digest>.json` and fails the case. Every check records who answered (`rust`, `fallback:<CODE>` or `python`); `--json-out` writes the per-check results and the counts, which are per check and not per route call. `record_parity.sh` runs five legs (Python, Rust host, Rust sandbox, and the Python and Rust host legs with `SWARM_TASKSTORE_WAL=1`) and keeps the outputs with the binary's sha256, the git head and the load.

### Rollback drill

The rollback is one switch: `GSD_ENGINE=python` (and `SWARM_PURE_PYTHON=1`, which also forces pure Python). Run this drill on a copy of a real `.swarm/` store after any change to the engine default, to see that the opt-outs answer with Python's bytes, leave the store alone and write one record per call. Run it from the repository checkout, with the signing environment the real runs use (the key is never printed), `REPO` the workspace root and `TASK` a task id of the store. It works on copies, so the live store is not touched. The list reply carries an `events` count, so every call starts from a fresh copy of the same state; otherwise the bytes differ by that count.

```
export REPO=/path/to/workspace TASK=T-0001        # TASK: any task id from the store
export SWARM_DIR=$REPO/.swarm                     # the store under test, as the real runs resolve it
LIVE="$SWARM_DIR"; DRILL=$(mktemp -d)
cp -a "$LIVE" "$DRILL/start"
export SWARM_DIR="$DRILL/run"
count() { grep -c '"type": "script.orch_status"' "$SWARM_DIR/events.jsonl" 2>/dev/null || true; }
db()    { sha256sum "$SWARM_DIR/tasks.db" | cut -d' ' -f1; }
call() {   # call NAME [VAR=value ...] command ...
  name=$1; shift
  rm -rf "$SWARM_DIR"; cp -a "$DRILL/start" "$SWARM_DIR"
  before_db=$(db); before_n=$(count); before_n=${before_n:-0}
  env -u GSD_ENGINE -u SWARM_PURE_PYTHON "$@" > "$DRILL/$name.out" 2> "$DRILL/$name.err"; code=$?
  echo "$name exit=$code records_added=$(( $(count) - before_n )) db_unchanged=$([ "$before_db" = "$(db)" ] && echo yes || echo NO) stderr_bytes=$(wc -c < "$DRILL/$name.err")"
}
for kind in list history; do
  if [ $kind = list ]; then extra=(); else extra=(--history "$TASK"); fi
  run=(python3 scripts/orch_status.py --repo "$REPO" "${extra[@]}" --json)
  call $kind.python  GSD_ENGINE=python    "${run[@]}"
  call $kind.pure    SWARM_PURE_PYTHON=1  "${run[@]}"
  call $kind.default                      "${run[@]}"
  for leg in pure default; do
    cmp "$DRILL/$kind.python.out" "$DRILL/$kind.$leg.out" && echo "$kind.$leg: bytes equal GSD_ENGINE=python"
  done
done
```

Pass criteria, for each of the six calls: `exit=0`, `records_added=1`, `db_unchanged=yes`, and for both calls that are not `GSD_ENGINE=python`, the `bytes equal` line. `GSD_ENGINE=python` and `SWARM_PURE_PYTHON=1` must never start the binary; to see that, add `GSD_ENGINE_BIN=<a script that appends to a marker file and answers differently>` to the `call` lines and check the marker (`tests/test_orch_status_rollback.py` does this). While the default is `python` all three legs are the Python body. After a flip the `default` leg may be answered by the binary (the `list` call always can; `history` on a task with a signed verdict is answered by Python after a failed Rust attempt, divergence D2); the criteria are the same, and a `default` leg that differs from `GSD_ENGINE=python` is a parity failure. The drill assumes the default rollback-journal mode: with `SWARM_TASKSTORE_WAL=1` the database file can change on open, so compare the replies and the record count only. Delete `$DRILL` when done.

The same drill is pinned by `tests/test_orch_status_rollback.py` on a throwaway signed store: opt-outs never start a fake binary and print Python's bytes; a missing, non-executable, crashing, garbage-printing, `E-DEP`-answering or hanging binary under the default gives Python's bytes (a hang is `E-TIMEOUT` once the default is `rust`); rolling back after a served call leaves the store and the earlier record alone.

### Known differences from the Python route

Recorded here, not fixed:

- The binary does not append `--json`. It passes argv through and serves only argv that contains `--json`; the SDK bridge and the harness append it. A fallback to the Python body runs `scripts/orch_status.py` with the caller's argv, so it prints text unless the caller passed `--json` (possible under explicit `rust` only).
- The Rust list path used to write no run-log event. Through the selector it now does (above); the binary run directly writes none.
- The selector bounds the subprocess at 660 s (`GSD_RUST_TIMEOUT_S` overrides); the binary's own deadline is 600 s.
- The binary reads stdin without a size cap. The 100 000-byte cap applies to `--input` only.
- A misspelt `GSD_RUST_BACKEND` fails closed with `E-INPUT`, exit 2, no Python fallback, whenever the selector is about to use the binary; otherwise it is ignored.
- Rust's error envelope has `details: {}` and no `trace`; Python's `trace` exists only for untyped exceptions (divergence D3, accepted).
- A history call on a task with a signed gate verdict, and a list that has an `IN_REVIEW` task with a signed row, is refused `E-DEP` by Rust and answered by Python (divergence D2, accepted): such a call pays one failed Rust attempt.
- The default route of every route is `python`. `parse_engine(None)` stays `Python`; the per-route defaults decide for an unset or empty `GSD_ENGINE`: `ORCH_STATUS_READ_DEFAULT`, `ORCH_STATUS_TRANSITION_DEFAULT` and `ORCH_STATUS_INGEST_DEFAULT` in `route_table.rs` (`default_engine`), `DEFAULT_ENGINE`, `DEFAULT_ENGINE_WRITE` and `DEFAULT_ENGINE_INGEST` in `scripts/orch_status.py`, and the inline `default` of each gate and plan route's selector block. All of them are `python`.

### Open sign-offs and blockers

- Sign-offs for the default guest pin `91b0970e...` (commit `54fd751`): the user's answer D-P38 item 1 ("Accept 91b0970e... and re-sign", 2026-10-30, `.planning/STATE.md`), and the records `tests/parity/records/14.5/A10-guest-pin-91b0970e.md` (verdict SECURED, scoped to the default read-only guest) and `tests/parity/records/14.5/A11-guest-pin-reproduce.md` (REPRODUCIBLE). The A10 scope does not clear any write route: finding F1 (the canary scan does not cover the P2 request bytes) must be closed, with a test, before any write flip. The earlier sign-off of `ffa7b28a7879da7ea99a20c83999d0b79e757bd084878909dcce0054fe66cade` (2026-10-26, scope recorded 2026-10-28) is for the previous default and is no longer the default pin. Details: [tests/parity/flip_record.md](../tests/parity/flip_record.md), "Write routes (Phase 14.5)".
- The default guest pin test (`built_guest_digest_matches_the_committed_pin`, `crates/gsd-sandbox/src/lib.rs`) matches `91b0970e...` at HEAD. The D-P25 mismatch with `ffa7b28a...` is closed by the re-pin in `54fd751`.
- A2 quiet-host performance baseline (U3.8a): not measured. It needs a 1-minute load below 1.5 for ten minutes, and `tests/perf/watch_quiet.sh` waits for that. The user answered "Not now" on 2026-10-30 (D-P36). `tests/perf/baseline.md` has no write-route leg. A default flip needs it.
- The user's approval of the flip. Until then the engine default is Python and `GSD_RUST_BACKEND=sandbox` stays opt-in.

## Write routes

Status on 2026-10-30 (Phase 14.5, unit U3.8b): **not flipped.** Each write route below is opt-in: its default is `python`, and `GSD_ENGINE=rust` is the only way a call reaches Rust. No default has changed. The per-route evidence is in [tests/parity/flip_record.md](../tests/parity/flip_record.md), section "Write routes (Phase 14.5)". The phase is not complete: the quiet-host latency record (U3.8a) and the user's flip approvals (U3.8c) are pending, and so are the rollback drills.

Guest pins: the default sandbox guest is `91b0970e...` (`crates/gsd-sandbox/guest-module.sha256`); the write guest for the second batch is `50b06310...` (`crates/gsd-sandbox/guest-module-write.sha256`). Both come from commit `54fd751`. The sandbox backend serves a write only in a binary built with the `sandbox-write` feature; otherwise it refuses the write call (`tests/parity/harness.py`, `SANDBOX_REFUSES`).

| Route | With `GSD_ENGINE=rust` | Declined: Python answers, nothing written | Default |
|---|---|---|---|
| `orch_status --transition TASK STATE [--reason R] --json`, target not APPROVED | Host writer (`gsd-taskwriter`): one `BEGIN IMMEDIATE` transaction; decide, validate the plan, apply, commit. The binary then writes an effects manifest that the selector replays in Python's order | An argv outside the grammar; an invalid state name; `notes` or verdict rows that Python reads and the binary cannot represent; the `CLAIMED` attempt and rework edge cases the matrix records as declined (R7 record: 64 of 786 checks) | `python` (`ORCH_STATUS_TRANSITION_DEFAULT`, `DEFAULT_ENGINE_WRITE`) |
| `orch_status --transition TASK APPROVED --json` | As above, plus the signing keys, read once by the binary inside the writer's transaction. Python's `E-POLICY` texts are final, not fallbacks | Every row in "The `--transition` route and APPROVED" above | `python` |
| `orch_status --ingest PATH [--advisory] --json` | Host ingest in one transaction; the effects manifest carries the inline transition | An invalid task result writes nothing and falls back before any write; an argv outside the grammar. A replayed result applies once (`notes.ingest_keys`) | `python` (`ORCH_STATUS_INGEST_DEFAULT`, `DEFAULT_ENGINE_INGEST`) |
| `qa_gate --json` | Host runner detection, count regexes, findings and subprocess; the verdict is written by `issue_gate` | Without `--json`; `--dry-run`; a store-derived risk class | `python` (inline) |
| `rel_plan --json` | Host release plan for the no-verdict path (every required gate absent) | `--dry-run`; a task that already has a verdict row (its signature check stays Python); an argv outside the grammar | `python` (inline) |
| `rev_gate --json` | Host reviewer gate: git diff, the seven mechanical checks and the scanner regexes | `--dry-run`; any git output or scanned file that is not ASCII; an argv outside the grammar | `python` (inline) |
| `sec_gate --json` | Host security gate: file walk, scanners, four dependency audits, allow list | A dependency audit output the route cannot prove equal; an exit code it cannot prove equal (exit 1 only with status `fail`) | `python` (inline) |
| `orch_plan` (derive and create) | Host derivation; one transaction creates the rows; the plan file and `latest_correlation` are written after the commit | A substrate URL, custom or blast-radius plans, an argv outside the grammar, a missing `tasks.db` or `effects_path` | `python` (inline) |
| `orch_assign --json` (verify) | Host verification of a signed assignment | A task with dependencies or a live substrate URL; a complex prompt | `python` (inline) |
| `swarm_tool catalog` | **No route.** `GSD_ENGINE=rust` gives `E-DEP` for every `swarm_tool` call (`RustEngine::swarm_tool`, `crates/gsd-adapter/src/strangler_seam.rs`). The catalog exists only as a guest mode (`crates/gsd-route/src/catalog.rs`) | Every call | `python` |

A decline is `E-DEP` with `error.details.write_state = "not_started"`, and the Python body answers once. Rollback is one switch: `GSD_ENGINE=python` (`SWARM_PURE_PYTHON=1` also forces Python). The write-route rollback drill has not been run.

After a commit nothing is run again. `swarm/engine_select.py` (`run_write`) maps the binary's outcome: exit 0 or 1 with a JSON object is final; exit 2 with `write_state` `final` is the binary's own refusal, printed as it is; anything else (the call deadline, a signal, an exit code outside 0 to 2, an unparsable reply, `E-INTERNAL` without `write_state`, an unknown `write_state`) is an unknown outcome, answered with a typed `E-INTERNAL` and no Python run.

Keys: the binary reads signing keys only for a transition to APPROVED and only where a route's grant names them. Keys are never in the Wasm guest (A10 check 1). The canary scan (`crates/gsd-adapter/tests/keys_never_in_guest.rs`) covers the request and reply bytes of the APPROVED transition, ingest, plan and catalog modes (commit `d0617a5`). Open: the read route's request is not covered by a canary test; it is covered by its typed encoder (A10, `tests/parity/records/14.5/A10-guest-pin-91b0970e.md`).

Known gaps, as of this section: no kill -9 evidence for the APPROVED target, the four gate routes or `orch_plan` (see the flip record); `swarm_tool catalog` has no route; no quiet-host record (U3.8a); no flip approval (U3.8c).


## Rust redaction engine (opt-in)

`swarm.redact` finds secret spans in pure Python by default. Set `SWARM_REDACT_ENGINE=rust` in the runner's environment to compute them in `swarm._native.redact_spans` instead (crate `gsd-redact`, bound by `gsd-python`). The Python module stays the oracle: the Rust spans must equal its spans for every input. `redact()` and `redact_text()` behave the same under either engine. Unset, empty or `python` is the default.

Fallback, so redaction never fails on configuration: `SWARM_PURE_PYTHON=1` wins over `rust` (one warning). Any other value warns once and uses Python. A missing extension, an extension without `redact_spans` (stale build) or an exception from the Rust call uses Python for that call. Each reason logs one `swarm.redact` warning per process. A string holding a lone surrogate cannot be encoded for Rust and is redacted by Python without a warning. A Rust panic surfaces as an `E-INTERNAL` `ValueError`, which the adapter treats as a failed call.

## Task Store journal mode (opt-in WAL)

The default is the rollback journal. Set `SWARM_TASKSTORE_WAL=1` in the runner's environment to make `TaskStore` open `tasks.db` in WAL mode with `synchronous=NORMAL` (set on open, fail-open to the current mode; any other value does nothing).

Measured effect: 400 commits run 2.0x faster wall (5.17 to 2.58 ms per op) and a 13-task dry run 1.25x faster wall. Real runs are dominated by model sessions, so the gain there is small.

Durability: a committed transition survives a process crash; after an OS crash or power loss the last few commits can roll back while the file stays consistent. `tasks.db-wal` and `tasks.db-shm` sit beside the database while a connection is open, so the directory must be writable, and `.swarm/.gitignore` already ignores them. A read-only opener (`StoreReader`, `ReadOnlyTaskStore`) also creates those side files. Never copy `tasks.db` alone while a connection is open: copy it with its `-wal` file, or close every connection first.

Check it: `sqlite3 .swarm/tasks.db 'PRAGMA journal_mode'`. The mode is stored in the database file, so once set it stays after the variable is unset or the code is reverted. To revert: `sqlite3 .swarm/tasks.db 'PRAGMA journal_mode=DELETE'` with no other connection open.

Known interaction: with WAL on, `tests/test_scheduler_continuous.py::test_a_success_between_failures_resets_the_streak` fails because commit latency changes retry-versus-priority ordering. Turning WAL on by default needs that test and the two read-only tests (`tests/test_shared_handover.py::test_read_tools_change_nothing_on_disk`, `omp/test/tools.int.test.ts:165`) reviewed by a human.
