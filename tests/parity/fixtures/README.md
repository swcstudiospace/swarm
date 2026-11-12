# Parity fixtures

One JSON file per recorded invocation, named `<case>.<nn>.json`. `<case>` is one of the twelve case names in
`tests/parity/harness.py`. `<nn>` is a two-digit index starting at `00` within that case. A case that makes several
invocations therefore has several files.

## Record fields

| field | type | meaning |
|---|---|---|
| `name` | string | Unique record name, `<case>.<nn>`, identical to the file stem. |
| `argv` | array of strings | `argv[0]` is the script file under `scripts/` (for example `"orch_status.py"`). The remaining elements are its arguments, exactly as typed. |
| `stdin` | string | Text fed to stdin. `""` when the invocation reads no stdin. |
| `expected_exit` | integer | Exit code the Python script returned. Python is the truth; the comparison is exact. |
| `expected_json_keys` | array of strings | Top-level keys that must be present in the single JSON object printed on stdout. |

Example (`tests/parity/fixtures/transition_legal_illegal.01.json`):

```json
{
  "name": "transition_legal_illegal.01",
  "argv": ["orch_status.py", "--transition", "T-illegal-1", "DONE", "--reason", "parity illegal"],
  "stdin": "",
  "expected_exit": 2,
  "expected_json_keys": ["status", "error"]
}
```

## Conventions

- The runner appends `--root <workspace>` and `--json` to `argv` and sets `SWARM_DIR` to a temporary directory it
  creates per case. None of these are recorded; a record never names a path.
- A record never references the repository's `.swarm/`. Every invocation runs with a scrubbed environment, so no host
  `SWARM_*` or `SUBSTRATE_*` variable reaches the script.
- State that an invocation depends on (seeded tasks, recorded verdicts, a replacement registry) is not part of the
  record. `harness.py` builds that state in code for each case, so the record describes only the invocation and its
  expected outcome.
- Records are compared with the exit code exact and stdout parsed as one JSON object. Stdout is not compared byte for
  byte, because the emitted JSON includes temporary paths.

## Status

This directory currently holds only this document. `harness.py` constructs the same invocations in code; no on-disk
records are committed yet.
