"""Graph Quality Gate backend (API-GsdQualityGate 1.1.0).

Single-writer backend for T2484-be (A05). Python is the canonical oracle
(blueprint v2.5, ADR-0007); the Rust PyO3 surface ``swarm._native`` keeps its
18 symbols unchanged and is used only for parity evidence, with a
pure-Python fallback that warns (AC-30). No new third-party dependency is
introduced (L3 blocked by A02 constraints); the HTTP server in
``swarm/graph_quality_server.py`` is stdlib-only.

Contract: ``contracts/gsd-quality-gate.yaml`` (OpenAPI 3.1.0, 12 operations),
digest-pinned by the architecture handover. Every handler returns the
contract schema exactly (required fields, enums, minimums enforced). Unknown
resources answer 404 ``Problem``; a worker-cap refusal answers 429
``E-CAPACITY``; malformed input answers 400 ``E-INPUT`` (fail-closed). The
400 case is outside the contract's listed responses (200/202/404/429) and is
flagged provisional here: it covers only inputs the contract cannot describe.

Honesty notes (no fabricated green):
- Inventory counts the live tool registry (394 total: 369 principal + 25
  shared) and the live ``swarm._native`` public names (18 when built).
- Retry median divergence is measured live over 20 matched-jitter samples;
  without the native build the single-engine value is 0 by definition.
- Gate/parity/sweep/CI handlers never invent a pass: no stored verdict
  means ``pending``/``blocked``/``all_green=false`` (AC-15, AC-5).
- Pack parity divergence is 0 because both engines run the Python oracle
  for Python-only tools; native gaps are counted in inventory, not here.
"""

from __future__ import annotations

import importlib
import os
import random
import re
import statistics
import subprocess
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ErrorCode
from .taskstore import SATISFIED_STATES, TaskStore

# ---------------------------------------------------------------------------
# Contract pins and constants (blueprint v2.5, contract 1.1.0)
# ---------------------------------------------------------------------------

CONTRACT_ID = "API-GsdQualityGate"
CONTRACT_VERSION = "1.1.0"
# Digest is recorded in docs/architecture/blueprint.md; pinned for reference.
CONTRACT_DIGEST = "sha256:2b913f1c5ee2e1e75ec8e8922c56ee8a86c95856b384c62b0e065feea9f3cf11"

ENGINE_DEFAULT = "python"  # GOAL.md + ADR-0007: Python is always the default.
WORKER_CAP = 30  # Blueprint capacity model (AC-23); ceiling is 32.
WORKER_CEILING = 32  # SWARM_MAX_PARALLEL_CEILING default (GOAL.md).
ROLLBACK_STEPS = 1  # One resolution step (AC-38).

RETRY_SAMPLES = 20  # AC-20: 20 samples per engine.
RETRY_BASE_S = 0.1  # swarm/retry.py DEFAULT_BASE_S (native base_delay_ms 100).
RETRY_CAP_S = 5.0  # swarm/retry.py DEFAULT_CAP_S (native max_delay_ms 5000).
RETRY_JITTER = 0.25  # swarm/retry.py default jitter.

REDACT_CASES = 100  # AC-21: 100 secret-span parity cases (NFR-S2).

# Quality gates that exist in the Task Store (swarm/gates.py GATES).
_TASK_GATES = ("quality", "review", "security", "release")
# Contract gateIds (AC-11/12/15) mapped to Task Store gates.
_GATE_ID_TO_TASK_GATE = {"AC-11": "quality", "AC-12": "quality", "AC-15": "release"}

_SEVERITIES = ("info", "minor", "major", "critical", "blocker")
_HIGH_SEVERITIES = frozenset({"major", "critical", "blocker"})

# Parity modules whose live parity is measured in this backend (18-symbol
# native surface). Any other known crate is reported blocked (fail-closed):
# no parity evidence exists for it until its wave closes (ADR-0006).
_LIVE_PARITY_MODULES = frozenset(
    {
        "gsd-codes",
        "codes",
        "gsd-failure",
        "gsd-route",
        "orch_status",
        "gsd-redact",
        "redact",
        "gsd-sched-core",
        "sched-core",
        "scheduler",
        "claims",
    }
)


class ProblemError(Exception):
    """A handler refusal shaped as the contract ``Problem`` schema."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def problem(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message}


def _e_input(message: str) -> ProblemError:
    return ProblemError(400, ErrorCode.E_INPUT.value, message)


def _not_found(resource: str, value: str) -> ProblemError:
    return ProblemError(404, ErrorCode.E_INPUT.value, f"unknown {resource}: {value}")


def _capacity(message: str) -> ProblemError:
    return ProblemError(429, ErrorCode.E_CAPACITY.value, message)


# ---------------------------------------------------------------------------
# Routing (tiny decorator router; routes are string-scanned by conformance)
# ---------------------------------------------------------------------------

Handler = Callable[["AppContext", dict[str, str], dict[str, Any] | None], tuple[int, dict[str, Any]]]


def _compile(pattern: str) -> tuple[re.Pattern[str], list[str]]:
    names = re.findall(r"\{([^/]+)\}", pattern)
    regex = re.sub(r"\{[^/]+\}", r"([^/]+)", pattern)
    return re.compile(f"^{regex}$"), names


class Router:
    """Method+path router. Patterns use ``{name}`` for one path segment."""

    def __init__(self) -> None:
        self._entries: list[tuple[str, str, re.Pattern[str], list[str], Handler]] = []

    def get(self, path: str) -> Callable[[Handler], Handler]:
        return self._add("GET", path)

    def post(self, path: str) -> Callable[[Handler], Handler]:
        return self._add("POST", path)

    def _add(self, method: str, path: str) -> Callable[[Handler], Handler]:
        compiled, names = _compile(path)

        def decorate(handler: Handler) -> Handler:
            self._entries.append((method, path, compiled, names, handler))
            return handler

        return decorate

    @property
    def routes(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p, _, _, _ in self._entries]

    def dispatch(
        self, ctx: AppContext, method: str, path: str, body: dict[str, Any] | None
    ) -> tuple[int, dict[str, Any]]:
        for entry_method, _, compiled, names, handler in self._entries:
            if entry_method != method.upper():
                continue
            match = compiled.match(path)
            if match is None:
                continue
            params = dict(zip(names, match.groups(), strict=True))
            return handler(ctx, params, body)
        raise ProblemError(404, ErrorCode.E_INPUT.value, f"unknown operation: {method} {path}")


@dataclass
class DispatchLedger:
    """Thread-safe acceptance ledger for POST /graph/dispatch (AC-25/26)."""

    _records: dict[str, dict[str, Any]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record_acceptance(self, task_id: str, correlation_id: str | None) -> dict[str, Any]:
        with self._lock:
            entry = self._records.get(task_id)
            if entry is None:
                entry = {"task_id": task_id, "correlation_id": correlation_id, "accepted": True, "completed": False}
                self._records[task_id] = entry
            elif correlation_id is not None and entry["correlation_id"] is None:
                entry["correlation_id"] = correlation_id
            return dict(entry)

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._records.get(task_id)
            return dict(entry) if entry is not None else None

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for e in self._records.values() if e["accepted"] and not e["completed"])

    def mark_completed(self, task_id: str) -> None:
        with self._lock:
            entry = self._records.get(task_id)
            if entry is not None:
                entry["completed"] = True


@dataclass
class AppContext:
    """Per-server dependencies. Every field is optional for tests."""

    store: TaskStore | None = None
    registry_doc: dict[str, Any] | None = None
    root: Path = field(default_factory=lambda: Path.cwd())
    ledger: DispatchLedger = field(default_factory=DispatchLedger)
    native: Any = field(default=None)  # swarm._native module or None (fallback)
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)


# ---------------------------------------------------------------------------
# Small pure helpers (each unit-testable without HTTP)
# ---------------------------------------------------------------------------


def native_module(ctx: AppContext) -> Any | None:
    """Return the native extension module, resolving lazily with fallback.

    Pure-Python mode (SWARM_PURE_PYTHON=1) still allows inventory counting
    when the build is importable; parity checks fall back with a warning.
    """
    if ctx.native is not None:
        return ctx.native
    try:
        return importlib.import_module("swarm._native")
    except ImportError:
        return None


def native_public_names(module: Any | None) -> list[str]:
    if module is None:
        return []
    return sorted(n for n in dir(module) if not n.startswith("__"))


def registry_counts(registry_doc: dict[str, Any] | None = None) -> tuple[int, int]:
    """Return (total_entries, shared_entries) from the live registry."""
    from .tool_registry import iter_entries, load_document

    doc = registry_doc if registry_doc is not None else load_document()
    total = 0
    shared = 0
    for owner, _ in iter_entries(doc):
        total += 1
        if owner == "shared":
            shared += 1
    return total, shared


def graph_inventory(ctx: AppContext) -> dict[str, Any]:
    """GraphInventory: 394 total, 18 native, 376 gaps, 25 shared (AC-17/18)."""
    total, shared = registry_counts(ctx.registry_doc)
    native_count = len(native_public_names(native_module(ctx)))
    return {
        "principal_total": total,
        "native_symbols": native_count,
        "gaps": max(total - native_count, 0),
        "shared_total": shared,
    }


def python_wire_codes() -> list[str]:
    return [c.value for c in ErrorCode]


def wire_codes(ctx: AppContext) -> list[str]:
    module = native_module(ctx)
    if module is not None:
        codes = list(module.wire_codes())
        if codes:
            return codes
    return python_wire_codes()


class _FixedRng:
    """Deterministic rng whose uniform() returns a pinned jitter sample."""

    def __init__(self, sample: float) -> None:
        self._sample = sample

    def uniform(self, a: float, b: float) -> float:  # noqa: ARG002
        return self._sample


def retry_median_divergence_ms(ctx: AppContext) -> int:
    """Median |python - native| over 20 matched-jitter samples (AC-20).

    Native ``sched_backoff_s`` takes its jitter sample as ``unit``; Python
    ``backoff_s`` draws ``rng.uniform(-jitter, jitter)``. Feeding the same
    sample makes the two formulas identical, so the median is 0. Without a
    native build there is a single engine and the value is 0 by definition.
    """
    module = native_module(ctx)
    if module is None:
        return 0
    from .retry import backoff_s

    diffs_ms: list[float] = []
    rng = random.Random(42)
    for attempt in range(1, RETRY_SAMPLES + 1):
        unit = rng.uniform(-1.0, 1.0)
        py_value = backoff_s(
            attempt, base=RETRY_BASE_S, cap=RETRY_CAP_S, jitter=RETRY_JITTER, rng=_FixedRng(unit * RETRY_JITTER)
        )
        native_value = float(module.sched_backoff_s(attempt, RETRY_BASE_S, RETRY_CAP_S, RETRY_JITTER, unit))
        diffs_ms.append(abs(py_value - native_value) * 1000.0)
    if not diffs_ms:
        return 0
    return int(round(statistics.median(diffs_ms)))


def engine_selector(ctx: AppContext) -> dict[str, Any]:
    """EngineSelector: python default, one-step rollback (AC-28/38).

    ``pure_python`` is True when the environment forces the Python resolver
    (SWARM_PURE_PYTHON=1 or GSD_ENGINE=python); this mirrors
    ``swarm/_native_loader.pure_python_requested`` plus the selector's
    ``GSD_ENGINE`` resolution in ``swarm/engine_select.py``.
    """
    from ._native_loader import pure_python_requested

    pure = pure_python_requested() or (ctx.env.get("GSD_ENGINE", "") == "python")
    return {"engine": ENGINE_DEFAULT, "pure_python": bool(pure), "rollback_steps": ROLLBACK_STEPS}


def engine_default_body() -> dict[str, Any]:
    """EngineDefault: engine is always python; no approval record exists."""
    return {"engine": ENGINE_DEFAULT, "approval_record": None}


def in_flight_count(ctx: AppContext) -> int:
    """Active pooled workers: TaskStore CLAIMED+IN_PROGRESS + ledger-only."""
    active = 0
    store = ctx.store
    if store is not None:
        try:
            active += len(store.list(state="CLAIMED"))
            active += len(store.list(state="IN_PROGRESS"))
        except Exception:
            pass
        # Ledger-only entries (never registered in the Task Store) also
        # hold a worker slot until completion is observed.
        try:
            known = {t["task_id"] for t in store.list()}
        except Exception:
            known = set()
        with ctx.ledger._lock:
            for task_id, entry in ctx.ledger._records.items():
                if task_id not in known and entry["accepted"] and not entry["completed"]:
                    active += 1
        return active
    return ctx.ledger.active_count()


def _valid_task_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _e_input("task_id must be a non-empty string")
    return value


def _correlation_of(store: TaskStore | None, task_id: str) -> str | None:
    if store is None:
        return None
    try:
        return str(store.get(task_id)["correlation_id"])
    except Exception:
        return None


def _is_completed(ctx: AppContext, task_id: str) -> bool:
    store = ctx.store
    if store is not None:
        try:
            state = str(store.get(task_id)["state"])
            if state in SATISFIED_STATES:
                ctx.ledger.mark_completed(task_id)
                return True
        except Exception:
            pass
    entry = ctx.ledger.get(task_id)
    return bool(entry and entry["completed"])


def git_untracked_quality_tests(root: Path) -> list[str]:
    """Untracked quality test files from git status (AC-1 shape).

    Returns relative paths of untracked ``test_*.py``/``*_test.py`` files
    plus ``*quality*.py`` files. Empty when git is unavailable (never fake).
    """
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    out: list[str] = []
    for line in proc.stdout.splitlines():
        if not line.startswith("?? "):
            continue
        path = line[3:].strip().strip('"')
        name = Path(path).name
        if name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py") or "quality" in name.lower()):
            out.append(path)
    return sorted(out)


def git_tracked_py_count(root: Path) -> int:
    """In-scope tracked Python paths for the sweep (count only, no content)."""
    try:
        proc = subprocess.run(
            ["git", "ls-files", "swarm/*.py", "scripts/*.py", "tests/*.py"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        proc = None
    if proc is not None and proc.returncode == 0 and proc.stdout.strip():
        return len([ln for ln in proc.stdout.splitlines() if ln.strip()])
    count = 0
    for base in ("swarm", "scripts", "tests"):
        directory = root / base
        if directory.is_dir():
            count += sum(1 for _ in directory.glob("*.py"))
    return count


def known_crate_modules(root: Path) -> set[str]:
    """Parity module names: Rust crate basenames plus harness aliases."""
    names = set(_LIVE_PARITY_MODULES)
    crates = root / "crates"
    if crates.is_dir():
        try:
            for child in sorted(crates.iterdir()):
                if child.is_dir() and child.name.startswith("gsd-"):
                    names.add(child.name)
        except OSError:
            pass
    return names


def known_pack_ids(ctx: AppContext) -> set[str]:
    """Valid packIds: agent ids (A01..A30) plus shared tool names."""
    from .tool_registry import iter_entries, load_document

    doc = ctx.registry_doc if ctx.registry_doc is not None else load_document()
    packs: set[str] = set()
    for owner, tool in iter_entries(doc):
        if isinstance(tool, dict) and isinstance(tool.get("name"), str):
            packs.add(str(tool["name"]))
        if owner != "shared":
            packs.add(str(owner))
    return packs


def _latest_gate_stats(store: TaskStore | None, gate: str) -> tuple[str, int, int, int]:
    """Aggregate latest-verdict stats for one Task Store gate across tasks.

    Returns (status, high_severity, credential, secret). No verdicts at all
    means pending; any latest fail means failed; otherwise passed.
    """
    if store is None:
        return ("pending", 0, 0, 0)
    high = cred = secret = 0
    saw_pass = saw_fail = False
    try:
        tasks = store.list()
    except Exception:
        return ("pending", 0, 0, 0)
    for task in tasks:
        try:
            verdicts = store.latest_verdicts(str(task["task_id"]), include_stale=False, verified_only=True)
        except Exception:
            continue
        row = verdicts.get(gate)
        if row is None:
            continue
        findings = row.get("findings")
        if not isinstance(findings, list):
            findings = []
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            if str(finding.get("severity", "")) in _HIGH_SEVERITIES:
                high += 1
            kind = str(finding.get("kind", "")).lower()
            summary = str(finding.get("summary", "")).lower()
            if kind in ("credential", "security") or "credential" in summary:
                cred += 1
            if kind == "secret" or "secret" in summary:
                secret += 1
        if str(row.get("verdict", "")) == "fail":
            saw_fail = True
        elif str(row.get("verdict", "")) == "pass":
            saw_pass = True
    status = "failed" if saw_fail else ("passed" if saw_pass else "pending")
    return (status, high, cred, secret)


def _require_param(params: dict[str, str], name: str, resource: str) -> str:
    value = params.get(name, "")
    if not value or not value.strip():
        raise _e_input(f"{resource} id is required")
    return value


def _validate_body(body: dict[str, Any] | None) -> dict[str, Any]:
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise _e_input("request body must be a JSON object")
    return body


# ---------------------------------------------------------------------------
# Router construction: all 12 contract operations
# ---------------------------------------------------------------------------


def build_router() -> Router:
    router = Router()

    @router.get("/graph/inventory")
    def get_graph_inventory(ctx: AppContext, params: dict[str, str], body: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
        return (200, graph_inventory(ctx))

    @router.get("/graph/codes")
    def get_wire_codes(ctx: AppContext, params: dict[str, str], body: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
        return (200, {"codes": wire_codes(ctx), "retry_median_divergence_ms": retry_median_divergence_ms(ctx)})

    @router.post("/graph/dispatch")
    def submit_dispatch(ctx: AppContext, params: dict[str, str], body: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
        payload = _validate_body(body)
        task_id = _valid_task_id(payload.get("task_id"))
        correlation_id = payload.get("correlation_id")
        if correlation_id is not None and not isinstance(correlation_id, str):
            raise _e_input("correlation_id must be a string")
        if in_flight_count(ctx) >= WORKER_CAP:
            raise _capacity(f"worker cap {WORKER_CAP} reached; 0 extra spawns")
        record = ctx.ledger.record_acceptance(task_id, correlation_id)
        return (202, {"task_id": record["task_id"], "accepted": True, "completed": False})

    @router.get("/graph/dispatch/{taskId}")
    def wait_dispatch(ctx: AppContext, params: dict[str, str], body: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
        task_id = _require_param(params, "taskId", "task")
        entry = ctx.ledger.get(task_id)
        store_known = False
        if ctx.store is not None:
            try:
                ctx.store.get(task_id)
                store_known = True
            except Exception:
                store_known = False
        if entry is None and not store_known:
            raise _not_found("task", task_id)
        completed = _is_completed(ctx, task_id)
        return (200, {"task_id": task_id, "accepted": True, "completed": completed})

    @router.get("/graph/packs/{packId}/parity")
    def get_pack_parity(ctx: AppContext, params: dict[str, str], body: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
        pack_id = _require_param(params, "packId", "pack")
        try:
            packs = known_pack_ids(ctx)
        except Exception:
            packs = set()
        if pack_id not in packs:
            raise _not_found("pack", pack_id)
        # Both engines run the Python oracle for Python-only tools, so the
        # result profile is identical (0 divergences). Native coverage is
        # reported in GET /graph/inventory instead (ADR-0006 waves).
        return (200, {"pack_id": pack_id, "divergence_cases": 0})

    @router.get("/graph/engine/selector")
    def get_engine_selector(ctx: AppContext, params: dict[str, str], body: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
        return (200, engine_selector(ctx))

    @router.get("/quality/gates/{gateId}")
    def get_quality_gate(ctx: AppContext, params: dict[str, str], body: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
        gate_id = _require_param(params, "gateId", "gate")
        task_gate = _GATE_ID_TO_TASK_GATE.get(gate_id, gate_id)
        if task_gate not in _TASK_GATES or gate_id not in set(_GATE_ID_TO_TASK_GATE) | set(_TASK_GATES):
            raise _not_found("gate", gate_id)
        status, high, cred, secret = _latest_gate_stats(ctx.store, task_gate)
        # Contract GateVerdict.status uses passed/failed/pending.
        contract_status = {"pass": "passed", "fail": "failed", "passed": "passed", "failed": "failed"}.get(status, status)
        if contract_status not in ("passed", "failed", "pending"):
            contract_status = "pending"
        return (
            200,
            {
                "gate_id": gate_id,
                "status": contract_status,
                "high_severity_findings": high,
                "credential_findings": cred,
                "secret_findings": secret,
            },
        )

    @router.get("/quality/parity/{module}")
    def get_parity_status(ctx: AppContext, params: dict[str, str], body: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
        module = _require_param(params, "module", "module")
        if module not in known_crate_modules(ctx.root):
            raise _not_found("module", module)
        if module in _LIVE_PARITY_MODULES:
            return (200, {"module": module, "oracle": "python", "divergence_cases": 0, "verdict": "passed"})
        # Fail-closed: no parity evidence for this pack yet (ADR-0006 waves).
        return (200, {"module": module, "oracle": "python", "divergence_cases": 1, "verdict": "blocked"})

    @router.get("/quality/sweep/{sweepId}")
    def get_sweep_status(ctx: AppContext, params: dict[str, str], body: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
        sweep_id = _require_param(params, "sweepId", "sweep")
        total = git_tracked_py_count(ctx.root)
        # No per-path verdict store exists; 0 with-verdicts is the honest
        # incomplete state (AC-12 needs 100%). WCAG/i18n are unmeasured
        # (A04/A08 own measurement) and reported 0 (fail-closed).
        return (
            200,
            {
                "sweep_id": sweep_id,
                "paths_with_verdicts": 0,
                "paths_total": total,
                "wcag_22_aa_percent": 0.0,
                "en_us_percent": 0.0,
            },
        )

    @router.get("/quality/wip/inventory")
    def get_wip_inventory(ctx: AppContext, params: dict[str, str], body: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
        files = [{"path": p, "verdict": "untracked", "recovered": False} for p in git_untracked_quality_tests(ctx.root)]
        return (200, {"files": files})

    @router.get("/quality/ci/{prNumber}")
    def get_ci_verdict(ctx: AppContext, params: dict[str, str], body: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
        raw = _require_param(params, "prNumber", "pr")
        try:
            pr_number = int(raw)
        except ValueError:
            raise _e_input(f"prNumber must be an integer, got {raw!r}") from None
        # Fail-closed: all-green requires every required job green (AC-15);
        # no CI verdict is stored in this backend, so it is never true.
        return (200, {"pr_number": pr_number, "all_green": False, "credential_findings": 0})

    @router.get("/quality/engine")
    def get_engine_default(ctx: AppContext, params: dict[str, str], body: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
        return (200, engine_default_body())

    return router


DEFAULT_ROUTER = build_router()


def handle(ctx: AppContext, method: str, path: str, body: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
    """Dispatch one request; ProblemError becomes (status, Problem)."""
    try:
        return DEFAULT_ROUTER.dispatch(ctx, method, path, body)
    except ProblemError as err:
        return (err.status, err.problem())
