---
phase: targeted-core-gsd-review
reviewed: 2026-11-10T00:00:00Z
depth: standard
files_reviewed: 28
files_reviewed_list:
  - crates/gsd-sched-core/src/lib.rs
  - crates/gsd-sched-core/src/runtime.rs
  - crates/gsd-sched-core/src/pool.rs
  - crates/gsd-sched-core/src/select.rs
  - crates/gsd-sched-core/src/ports.rs
  - crates/gsd-envelope/src/lib.rs
  - crates/gsd-envelope/src/envelope.rs
  - crates/gsd-taskwriter/src/lib.rs
  - crates/gsd-taskwriter/src/store.rs
  - crates/gsd-taskwriter/src/error.rs
  - crates/gsd-taskwriter/src/apply.rs
  - crates/gsd-redact/src/lib.rs
  - crates/gsd-sandbox/src/lib.rs
  - crates/gsd-sandbox/src/wasmtime_host_bridge.rs
  - crates/gsd-security/src/lib.rs
  - crates/gsd-adapter/src/sandbox_engine.rs
  - swarm/taskstore.py
  - swarm/envelope.py
  - swarm/redact.py
  - swarm/rust_scheduler.py
  - swarm/limiter.py
  - scripts/swarm_run.py
  - omp/src/guard.ts
  - omp/src/bridge.ts
  - omp/src/hooks.ts
  - swarm/claims.py
  - swarm/errors.py
  - swarm/gates.py
findings:
  critical: 0
  warning: 4
  info: 5
  total: 9
status: issues_found
---

# Targeted Code Review: GSD-Core (crates/gsd-*, swarm/, scripts/, omp/src/)

**Reviewed:** 2026-11-10T00:00:00Z
**Depth:** standard (with cross-file traces on scheduler/envelope/redaction/sandbox boundaries)
**Files Reviewed:** 28 (prioritized: scheduler, sandbox, envelope, taskstore, redaction + supporting)
**Status:** issues_found

## Summary

Reviewed core production paths for the native scheduler (Rust/Python), signed envelopes, Task Store writers/transitions, PII redaction (parity), Wasmtime sandbox + host bridge + capability tokens, OMP guard/bridge, and supporting run logic. 

Design is production-grade in key areas: `#![forbid(unsafe_code)]` + `#![warn(missing_docs)]` in Rust libs, all fallible paths return `Result` (no panics on bad input in lib code), Python raises `SwarmError(E-*)` per AGENTS.md, SQLite uses `BEGIN IMMEDIATE` + conditional UPDATE + rowcount checks for contract violations, redaction/envelope are parity oracles with graceful native fallback, sandbox uses capability tokens + traps for policy, guard blocks dangerous actions.

Clippy clean on reviewed crates (no warnings under correctness/suspicious; full workspace run timed out but targeted lib clean). No trivial fixes applied.

AGENTS.md conventions largely followed (script skeletons, SwarmError codes, TS twins for non-_ scripts, naming). Rust side respects "no panics in lib", lifetime/Arc discipline in pool/runtime (no locks held across ports/callbacks).

No critical/blocker defects proved in the reviewed scope (fail-closed key handling, constant-time sig compare via subtle, param queries, explicit declines to Python oracle, lease/claim release on all paths including cancel). 

## Critical Issues

No CR- findings. All reviewed paths either fail-closed on bad state/keys or degrade safely. No injection, auth bypass, data loss, or panics-in-lib proved.

## Warnings

### WR-01: Complex hand-written shell command parser in OMP guard risks incomplete coverage for production-change blocks

**File:** `omp/src/guard.ts:300-900+` (shellWords, dequote, scanQuoted, ansiEscape, normalizeSegments, findCommands, kubectlProdChange, frameworkDeploy, RULES, unnamed* functions ~2000+ LOC total for command classification)

**Issue:** The prod marker / dangerous write detection (D-04 etc) relies on a large custom shell parser (quote handling with $'ansi', backslash, nesting cap=3, flag value eaters for helm/kubectl, top-level split, assignment detection, network write tracing). A missed form (e.g., unusual quoting, `env -S`, indirect via var in certain compounds, `sh -c` deeper, k8s apply with specific --kubeconfig patterns, chain `forge broadcast` variants) could allow a prod-changing command to execute inside a swarm session (security bypass) or cause excessive blocking (UX).

No concrete bypass found in spot checks, but the code is high cyclomatic complexity with many ad-hoc regex/state machines. Cross-ref to RULES and test surface (guard.test.ts) shows good cases but edge coverage is manual.

**Fix:** 
- Add exhaustive adversarial test vectors for new shell forms (e.g., from real CI logs).
- Consider extracting a small tested shell tokenizer or document "best effort" and pair with runtime audit hooks.
- At minimum: add `// TODO(audit): re-review parser on any shell grammar change` and increase test matrix for kubectl/helm/forge variants.

### WR-02: Redaction implementation duplicated across Python oracle and Rust port; divergence risk on security boundary

**File:** `swarm/redact.py:1050+` ( _spans, _cli_spans, _pair_spans, _yaml_spans, _scan, _Reader/_Lexer + _native_spans fallback) and `crates/gsd-redact/src/{lib,scan,cli,yaml,pairs,lexer,reader,*.rs}` (hand-written scanners for same patterns)

**Issue:** Python `_spans` + detectors is the oracle. Rust gsd-redact reimplements equivalent leftmost/greedy/quote-aware scanners for parity (no regex lookbehind/backref support). `redact_spans` / `redact_text` when `SWARM_REDACT_ENGINE=rust` uses native and falls back on any error (good), but when active the spans may differ on Unicode edges, long values, quote nesting, YAML block scalars, or new token shapes. A divergence either leaks secrets (security) or mangles useful log text.

Parity harness and vectors exist (CI), but maintenance burden is high; any change must be ported exactly.

**Fix:** 
- Keep Python as source of truth; generate or snapshot-test exact span vectors from Python into Rust test vectors.
- Add a `tests/test_redact_parity.py` (or extend) that forces rust engine and asserts identical output on corpus including adversarial Unicode/lone-surrogate/long-secret cases.
- Document that native is optimization only.

### WR-03: Overly broad `except Exception` (BLE001) used for fail-open resilience in many core paths

**File:** Multiple, e.g. `swarm/taskstore.py:325,406,460,481`, `swarm/substrate_*.py:312,384,522,594,474`, `swarm/telemetry.py:205,482`, `swarm/redact.py: (native except)`, `scripts/swarm_run.py: (worker isolation)`

**Issue:** Per design (substrate client fail-open, telemetry observer must not break commit, worker isolation, redaction never fails a call), broad catches with `# noqa: BLE001` are used. This hides real bugs (e.g., logic errors inside the "should never fail" block become silent), reduces signal in logs, and makes it harder to prove contracts under failure. Some paths log at debug/warning, others swallow entirely.

While not incorrect per the fail-open policy in docs/AGENTS, it degrades maintainability and incident response.

**Fix:** 
- Replace with specific `except (sqlite3.Error, OSError, ...)` where the failure mode is known.
- For true "anything" cases, use a narrow `except Exception as exc: _log_and_return_safe(exc)` helper that always emits a structured event.
- Add unit tests that inject faults inside the broad blocks and assert the safe path + event was emitted (not just "did not crash").

### WR-04: gsd-security JWT capability tokens have incomplete validation and unbounded revocation map

**File:** `crates/gsd-security/src/lib.rs:60-100 (validate), 200+ (revoke + DashMap), ~300 (CapabilityTokenManager)`

**Issue:** 
- `jwt_expiry_hours` is stored and `SecurityConfig::new` sets a default but `validate()` only checks `capability_token_ttl_secs` bounds and secret; expiry_hours is not clamped or cross-checked against ttl (a token ttl > jwt exp would be surprising).
- `revoked_tokens: DashMap` grows with every revoke; prune happens on revoke but only retains `exp >= now`; under high churn (many short-lived threads) or clock skew an unbounded number of entries can accumulate until prune.
- Historical default is refused (good), min 32 bytes (good), HS256 + claims checks (sub/scope/resource) + exp/iss/aud (good).

Not a direct vuln (tokens are short-lived, issued only by host), but quality/maintainability and a potential DoS vector on the manager state.

**Fix:**
- Add `jwt_expiry_hours` validation in `validate()` (e.g. 1..=24 or tie to ttl).
- Bound the DashMap (e.g. max_entries or prune more aggressively on every authorize).
- Consider a TTLed set or document the memory bound.

## Info

### IN-01: Rust libs set `missing_docs` warn but some public items (internal helpers exposed via pub use or test fakes) lack docs

**File:** e.g. `crates/gsd-sched-core/src/ports.rs` (RecordingPorts etc), several `pub(crate)` that leak in tests.

**Fix:** Add `///` or `#[allow(missing_docs)]` on test-only.

### IN-02: Scheduler selection and Rust ports faithfully mirror Python oracle (order_ready, claims, leases, class ceiling, AIMD feed, backoff, cancel drain); no divergence found in traced paths

Good contract fidelity. `plan_pass`/`select_batch` + `_Ports` + `Runtime` use Result everywhere.

### IN-03: Envelope and TaskStore use "decline to Python" / "not-started" vs "final" distinction for safe strangler cutover; keys never cross to guest (per design)

Rust `gsd-envelope` declines key-file loads and non-representable JSON so Python oracle handles; taskwriter refuses before first write.

### IN-04: All reviewed scripts follow AGENTS.md skeleton (AgentScript or direct, SwarmError(E-*, ...), --dry-run/--json support); all non-_ have TS twins.

No naming or contract violations found.

### IN-05: No panics, unsafe, or avoidable allocation in hot scheduler paths; pool uses detached workers + catch_unwind + no lock across callbacks (lifetime/concurrency safety respected). Clippy clean.

---

_Reviewed: 2026-11-10_
_Reviewer: gsd-code-reviewer (targeted adversarial on core crates/swarm/scripts/omp)_
_Depth: standard_
_No source files were modified. Only REVIEW.md produced._
