# Quality CW3 — Scoped canonical gates (non-graph)

**Assignment**: Wave3 optimise. Run canonical gates from INVENTORY.md/CI
(`ruff`, `pytest` scoped, `cargo test` scoped, `fmt`/`clippy` scoped) on
non-graph codebase files. Fix only clear defects (max 3 files, no new deps).
Record exact commands + outcomes here. SKIP repo-wide suites (known timeout).
Branch observed: `swarm/Tbe37-be` (assignment context: `swarm/T74c2-be`).

**Scope note**: Waves1-2 wrote docs only (`docs/reconcile-CW1.md`,
`docs/graphvalidate-CW1.md`, `docs/discuss-CW2.md`, `docs/plan-CW2.md`).
Scoped pytest target = non-graph quality tests:
`tests/test_quality_*.py` + `tests/test_creator_be.py`.
Scoped Rust target = `gsd-redact` (non-graph, zero deps; secret redaction,
Python-parity oracle). Graph files (`swarm/graph_*.py`,
`tests/test_graph*.py`, `web/graph-engineering*`, `gsd-sched*`) excluded.

## Gates (PASS/FAIL with counts)

| # | Gate | Command (exact) | Result | Counts |
|---|---|---|---|---|
| G1 | ruff (scoped) | `env -u SWARM_ED25519_KEY -u SWARM_SIGNING_KEY -u SWARM_ALLOW_INSECURE_DEV_KEY ruff check tests/test_quality_manifests.py tests/test_quality_FXA.py tests/test_quality_FXB.py tests/test_quality_FXD.py tests/test_quality_L10.py tests/test_quality_L12.py tests/test_quality_L13.py tests/test_quality_L14.py tests/test_quality_L15.py tests/test_quality_L16.py tests/test_quality_L17.py tests/test_quality_L18.py tests/test_quality_L23.py tests/test_creator_be.py` | **PASS** | 0 errors, 14 files |
| G2 | pytest (scoped, non-graph) | `env -u SWARM_ED25519_KEY -u SWARM_SIGNING_KEY -u SWARM_ALLOW_INSECURE_DEV_KEY -u SWARM_PURE_PYTHON python3 -m pytest tests/test_quality_manifests.py tests/test_quality_FXA.py tests/test_quality_FXB.py tests/test_quality_FXD.py tests/test_quality_L10.py tests/test_quality_L12.py tests/test_quality_L13.py tests/test_quality_L14.py tests/test_quality_L15.py tests/test_quality_L16.py tests/test_quality_L17.py tests/test_quality_L18.py tests/test_quality_L23.py tests/test_creator_be.py -q` | **FAIL** | 79 passed, 1 failed, 80 collected |
| G3 | cargo test (scoped, non-graph) | `env -u SWARM_ED25519_KEY -u SWARM_SIGNING_KEY -u SWARM_ALLOW_INSECURE_DEV_KEY cargo test -p gsd-redact --locked` | **SKIPPED:infra** (build timeout; 2 attempts: 600 s parallel-lock timeout; sequential retry >16 min then killed by operator — cold registry/deps never finished in this env) | 0 tests confirmed |
| G4 | cargo fmt (check, read-only, scoped) | `env -u SWARM_ED25519_KEY -u SWARM_SIGNING_KEY -u SWARM_ALLOW_INSECURE_DEV_KEY cargo fmt -p gsd-redact -- --check` | **PASS** | 0 diffs (exit 0, 7.9 s) |
| G5 | cargo clippy (scoped, warnings=errors) | `env -u SWARM_ED25519_KEY -u SWARM_SIGNING_KEY -u SWARM_ALLOW_INSECURE_DEV_KEY cargo clippy -p gsd-redact --locked -- -D warnings` | **SKIPPED:infra** (same build timeout — never reached in either batch; not run to verdict) | 0 lints confirmed |

Repo-wide suites skipped per acceptance (known timeout): full
`python3 -m pytest`, full `cargo test --workspace --locked --no-fail-fast`,
`bun test tests/ts`, `bun run test` (omp). Formatter writes skipped (only
`--check` / `-D warnings` read-only checks run).

## Finding QF-CW3-01 (pytest FAIL)

- **Test**: `tests/test_quality_manifests.py::test_removed_dependency_is_absent_from_manifest_and_source[gsd-observability:metrics]`
- **Evidence** (from G2 output):
  `AssertionError: crates/gsd-observability/src/lib.rs:19: pub use metrics::{GraphTelemetry, LimitViolation, MetricsRegistry, ResourceLimits, ToolOutcome};`
- **Why it fails**: The test asserts the removed external crate `metrics` is
  absent from `crates/gsd-observability/Cargo.toml` (true — manifest declares
  no `metrics` dep, only `gsd-proto`, `tracing`, `tracing-subscriber`,
  `opentelemetry*`, `dashmap`) and from Rust sources. The detector
  (`_reference_pattern`, `_source_hits` in `tests/test_quality_manifests.py`)
  matches `pub use metrics::` via the `use` alternative, without exempting
  local modules.
- **Root cause**: `crates/gsd-observability/src/lib.rs:13` declares
  `pub mod metrics;` backed by `crates/gsd-observability/src/metrics.rs`
  (28.1 KB, exists). Line 19's `pub use metrics::{...}` therefore names the
  crate's **own** `metrics` module, not the removed external `metrics`
  crate. The detector has no local-`mod` exemption (lines 52–86 of the test
  only collect resolve-names, never subtract sibling `mod` names).
- **Disposition**: **Not fixed in this task.** A08 does not patch product
  code and does not self-waive/rewire gates; the fix belongs to the test
  owner (narrow the detector to exempt `use <name>::` where
  `<crate>/src/<name>.rs` exists, or rename the local module — owner
  decision, not taken here). 0 of 3 allowed files touched. Gate verdict for
  this scope: **FAIL** (1 failed of 80) on this finding.
- **Owner suggestion**: A05/A07 (test owner).

## Notes

- Env stripping (`env -u SWARM_*`) follows INVENTORY.md §7 lesson.
- No files modified except this document (`docs/quality-CW3.md`).
