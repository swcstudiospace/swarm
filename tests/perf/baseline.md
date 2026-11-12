# Performance baseline

Status: two moderate-load runs recorded 2026-10-25 under the user's threshold (1-minute load below 4.0 for ten minutes): the 15:46 UTC run on the pre-fix native build, and the 21:31 UTC Phase 14 run on the final build. Neither is a quiet-host baseline, and under the Notes rule below neither is a promotion candidate. The Phase 14 quiet-host criterion is open.

**Phase 7 waiver (user, 2026-10-22; recorded in `.planning/STATE.md`):** the A2 perf baseline is waived with recorded debt, so Wave B and Wave C code proceed. The quiet-window baseline is still required before any default flip. The Phase 7 baseline was not measured; this waiver does not promote either moderate-load run below.

## Hardware fingerprint

- uname -a: Linux vps 6.8.0-147-generic #147-Ubuntu SMP PREEMPT_DYNAMIC Thu Sep 24 11:33:16 UTC 2026 x86_64 x86_64 x86_64 GNU/Linux
- nproc: 8
- /proc/loadavg (start): 1.70 2.50 3.42 3/1955 783458
- /proc/loadavg (end): 2.32 2.47 3.12 4/1996 813857
- python3 --version: Python 3.12.3
- bun --version: 1.4.0
- configuration: product (swarm._native importable)

## Load during the run

- Window reached at 15:40:54 UTC after the 1-minute load stayed below 4.0 for 20 consecutive 30-second checks. The last readings before the window were 2.99, 2.42 and 2.16.
- Run exit at 15:46:45 UTC, exit code 0.
- 35 samples at 10-second intervals while the run was alive: mean 2.43, p95 3.03, max 3.05. All were below 4.0.

## Method

`tests/perf/run.sh` runs three paths against the scripts in `scripts/`, each with `SWARM_DIR` set to a fresh `mktemp -d` directory that is removed on exit.

- After 3 warm-up runs, each path is timed over 50 samples using `date +%s%N`.
- Each path reports p50 and p95 in milliseconds (nearest-rank percentile, stdlib only).
- Paths:
  - (a) `python3 scripts/swarm_run.py --dry-run --json`
  - (b) `python3 scripts/qa_gate.py --dry-run --json`
  - (c) `python3 scripts/orch_status.py --json` (proxy for a bridge round trip)

## Results

| path | samples | p50 ms | p95 ms |
| ---- | ------- | ------ | ------ |
| (a) swarm_run --dry-run --json | 50 | 3379.351 | 4126.465 |
| (b) qa_gate --dry-run --json | 50 | 131.176 | 142.320 |
| (c) orch_status --json (bridge round-trip proxy) | 50 | 131.638 | 146.682 |

## Notes

- Moderate-load: the figures were recorded at the user's 4.0 threshold, decided 2026-10-25 after the host stayed busy at about 1.5 for two hours. Under the rule below they are not promotion candidates.
- Record the host load (`/proc/loadavg` at start and end) with every run. A run without recorded load is incomplete. Done above.
- Build measured: the 15:46 run used the in-tree `swarm/_native.abi3.so` built before the review fixes to the Rust crates. The Phase 14 run below used the release cdylib of the reviewed Rust, installed before the watcher was re-armed at 21:10:40 UTC.
- The run wrote no tracked file during its window: `.claude/swarm-state.json` (mtime 07:15) and `.planning/STATE.md` (mtime 14:44) both predate 15:40:54, and the sampler redirects its state paths.
- Numbers taken under load are not promotion candidates. Re-run on an idle host before using any figure as a baseline.

## Phase 14 re-measure on final code (window 21:24:40 UTC, exit 21:31:31 UTC)

- Watcher: 1-minute load below 4.0 for 20 consecutive 30-second checks. The last readings before the window were 2.78 at 21:23:40 and 3.46 at 21:24:10. Run exit 21:31:31 UTC, exit code 0.
- Hardware fingerprint: same host as the 15:46 record (uname, nproc 8, python3 3.12.3, bun 1.4.0, product configuration with swarm._native importable). Full text in `.planning/a2-quiet-run/quiet-run.txt`.
- /proc/loadavg at start 4.42 3.36 4.36; at end 15.05 11.45 7.51.
- Load during the run: 41 readings in `.planning/a2-quiet-run/load-during.txt`, mean 11.04, max 21.36. 39 of 41 readings were at or above 4.0. The load rose from about 4 to about 17 to 21 inside the run. This is contention, not a quiet host.
- Method as above: 3 warm-up runs and 50 samples per path.

| path | samples | p50 ms | p95 ms |
| ---- | ------- | ------ | ------ |
| (a) swarm_run --dry-run --json | 50 | 3835.231 | 6173.206 |
| (b) qa_gate --dry-run --json | 50 | 140.231 | 242.335 |
| (c) orch_status --json (bridge round-trip proxy) | 50 | 154.803 | 194.999 |

- Against the 15:46 run (mean load 2.43): (a) p50 +13.5%, p95 +49.6%; (b) p50 +6.9%, p95 +70.3%; (c) p50 +17.6%, p95 +32.9%. The mean load differs by about 4.5 times between the two runs, so these differences cannot be attributed to the code. No regression is claimed.
- Verdict: contended. The run is recorded as a moderate-load figure. It does not meet the Phase 14 criterion for a quiet-host baseline, because the load was at or above 4.0 for 39 of 41 readings. A quiet-host re-run is still required to close that criterion. No figure in this section is a promotion candidate.
