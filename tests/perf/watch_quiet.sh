#!/usr/bin/env bash
# A2 quiet-window watcher. Blocks until the host has been quiet, then exits 0; exits non-zero when it gives up first.
#
# Quiet means the 1-minute load average (first field of /proc/loadavg) is below THRESH at NEED consecutive checks
# INTERVAL_S seconds apart. A check at or above THRESH resets the count. Every check records the whole /proc/loadavg
# line. The A2 rule (Phase 14.3, STATE.md P-A2) is THRESH 1.5, NEED 20, INTERVAL_S 30: the 20th check lands 9.5 minutes
# after the first, which is the ten-minute window. The last line printed is `result: quiet ...` or `result: not quiet ...`;
# it ends with `rule: A2` only for the A2 parameters, otherwise `rule: relaxed`, and only an `rule: A2` quiet result
# lets tests/perf/run.sh (A2_WATCHER_LOG) count a run as evidence.
#
# This script only watches; it starts nothing. Chain the run after it:
#   WATCH_LOG=<file> tests/perf/watch_quiet.sh && A2_WATCHER_LOG=<file> A2_ENGINE_LEGS=1 GSD_ENGINE_BIN=<bin> tests/perf/run.sh
#
# Environment (all optional):
#   THRESH      1-minute load must be below this (default 1.5)
#   NEED        consecutive quiet checks required (default 20)
#   INTERVAL_S  seconds between checks (default 30); lower it only to test the watcher itself
#   MAX_CHECKS  give up after this many checks and exit 1; 0 waits for ever (default 0)
#   WATCH_LOG   file the output lines are appended to as well as printed (default none)
# Exit: 0 quiet window reached; 1 gave up (MAX_CHECKS) before one; 2 bad parameter.
set -u

THRESH="${THRESH:-1.5}"
NEED="${NEED:-20}"
INTERVAL_S="${INTERVAL_S:-30}"
MAX_CHECKS="${MAX_CHECKS:-0}"
WATCH_LOG="${WATCH_LOG:-}"

bad() {
  echo "error: $1" >&2
  exit 2
}
[[ "$THRESH" =~ ^[0-9]+([.][0-9]+)?$ ]] || bad "THRESH must be a non-negative number, got '$THRESH'"
[[ "$NEED" =~ ^[1-9][0-9]*$ ]] || bad "NEED must be a positive integer, got '$NEED'"
[[ "$INTERVAL_S" =~ ^[1-9][0-9]*$ ]] || bad "INTERVAL_S must be a positive integer, got '$INTERVAL_S'"
[[ "$MAX_CHECKS" =~ ^[0-9]+$ ]] || bad "MAX_CHECKS must be a non-negative integer, got '$MAX_CHECKS'"

rule="relaxed"
if [[ "$THRESH" == "1.5" && "$NEED" == "20" && "$INTERVAL_S" == "30" ]]; then
  rule="A2"
fi

say() {
  echo "$1"
  if [[ -n "$WATCH_LOG" ]]; then
    echo "$1" >>"$WATCH_LOG"
  fi
}

say "$(date -u +%H:%M:%S) armed: 1-min load below $THRESH for $NEED consecutive checks ${INTERVAL_S}s apart (rule: $rule)"
consec=0
checks=0
while true; do
  line="$(cat /proc/loadavg)"
  load1="${line%% *}"
  checks=$((checks + 1))
  if awk -v l="$load1" -v t="$THRESH" 'BEGIN { exit !(l < t) }'; then
    consec=$((consec + 1))
  else
    consec=0
  fi
  say "$(date -u +%H:%M:%S) check=$checks load1=$load1 consecutive=$consec loadavg=\"$line\""
  if [[ "$consec" -ge "$NEED" ]]; then
    say "result: quiet at $(date -u +%H:%M:%S) UTC after $checks checks (load below $THRESH for $NEED consecutive checks, interval ${INTERVAL_S}s, rule: $rule)"
    exit 0
  fi
  if [[ "$MAX_CHECKS" -gt 0 && "$checks" -ge "$MAX_CHECKS" ]]; then
    say "result: not quiet at $(date -u +%H:%M:%S) UTC after $checks checks (last load1 $load1, $consec of $NEED consecutive below $THRESH, interval ${INTERVAL_S}s, rule: $rule)"
    exit 1
  fi
  sleep "$INTERVAL_S"
done
