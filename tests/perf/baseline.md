# Performance baseline (template)

## Hardware fingerprint

Fill from `tests/perf/run.sh` output.

- uname -a:
- nproc:
- /proc/loadavg (start):
- /proc/loadavg (end):
- python3 --version:
- bun --version:

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
| (a) swarm_run --dry-run --json | | | |
| (b) qa_gate --dry-run --json | | | |
| (c) orch_status --json (bridge round-trip proxy) | | | |

## Notes

- Record the host load (`/proc/loadavg` at start and end) with every run. A run without recorded load is incomplete.
- Numbers taken under load are not promotion candidates. Re-run on an idle host before using any figure as a baseline.
