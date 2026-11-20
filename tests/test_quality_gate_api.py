"""T3742-be — unit tests for API-GsdQualityGate 1.0.0 handlers.

Covers the backend-side acceptance criteria: AC-1 (WIP inventory),
AC-5 (parity), AC-6/AC-16 (engine default), AC-11/AC-12 (gates,
sweep) and AC-15 (CI verdict).  All fixtures are hermetic tmp dirs;
the only real-repo touch is one smoke test through handle().
"""

import json

import pytest

from swarm import quality_gate as qg


@pytest.fixture()
def root(tmp_path):
    (tmp_path / "swarm").mkdir()
    (tmp_path / "tests" / "parity" / "records").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    return tmp_path


def _write(path, text=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# --- AC-6/AC-16 engine default -----------------------------------------

def test_engine_default_is_python(root):
    status, payload = qg.handle("GET", "/quality/engine", root)
    assert status == 200
    assert payload == {"engine": "python", "approval_record": None}


def test_engine_names_approval_record_when_one_exists(root):
    _write(root / "docs" / "approvals" / "engine-flip.md", "approved\n")
    status, payload = qg.handle("GET", "/quality/engine", root)
    assert status == 200
    assert payload["engine"] == "python"
    assert payload["approval_record"] == "docs/approvals/engine-flip.md"


def test_engine_never_flips(root):
    # no input can make this API answer rust: enum is [python]
    for path in ("/quality/engine", "/quality/engine?engine=rust"):
        status, payload = qg.handle("GET", path, root)
        assert payload["engine"] == "python"


# --- AC-11/AC-12 gates --------------------------------------------------

@pytest.mark.parametrize("gate_id", ["lint-scan", "sweep-report", "ci-gate"])
def test_known_gates_report_pending_without_record(root, gate_id):
    status, payload = qg.handle("GET", f"/quality/gates/{gate_id}", root)
    assert status == 200
    assert payload["gate_id"] == gate_id
    assert payload["status"] == "pending"
    assert payload["high_severity_findings"] == 0
    assert payload["credential_findings"] == 0
    assert payload["secret_findings"] == 0


def test_unknown_gate_is_404_problem(root):
    status, payload = qg.handle("GET", "/quality/gates/nope", root)
    assert status == 404
    assert set(payload) == {"code", "message"}
    assert payload["code"] == "unknown_gate"


def test_recorded_gate_verdict_wins(root):
    _write(root / "docs" / "gates" / "lint-scan.json",
           json.dumps({"gate_id": "lint-scan", "status": "passed",
                       "high_severity_findings": 0, "credential_findings": 0}))
    status, payload = qg.handle("GET", "/quality/gates/lint-scan", root)
    assert status == 200
    assert payload["status"] == "passed"
    assert payload["credential_findings"] == 0


# --- AC-5 parity --------------------------------------------------------

def test_unknown_module_is_404(root):
    status, payload = qg.handle("GET", "/quality/parity/nope", root)
    assert status == 404
    assert payload["code"] == "unknown_module"


def test_module_without_evidence_is_blocked(root):
    (root / "tests" / "parity" / "records" / "14.5").mkdir(parents=True)
    status, payload = qg.handle("GET", "/quality/parity/14.5", root)
    assert status == 200
    assert payload["oracle"] == "python"
    assert payload["verdict"] == "blocked"
    assert payload["divergence_cases"] == 0


def test_recorded_parity_evidence_wins(root):
    _write(root / "tests" / "parity" / "records" / "14.5" / "parity.json",
           json.dumps({"divergence_cases": 0, "verdict": "passed"}))
    status, payload = qg.handle("GET", "/quality/parity/14.5", root)
    assert payload["verdict"] == "passed"
    assert payload["divergence_cases"] == 0


# --- AC-11/AC-12 sweep --------------------------------------------------

def test_sweep_counts_sources_with_test_counterparts(root):
    _write(root / "swarm" / "alpha.py", "x = 1\n")
    _write(root / "swarm" / "beta.py", "y = 2\n")
    _write(root / "tests" / "test_alpha.py", "def test_a():\n    pass\n")
    status, payload = qg.handle("GET", "/quality/sweep/repo", root)
    assert status == 200
    assert payload["sweep_id"] == "repo"
    assert payload["paths_total"] == 2
    assert payload["paths_with_verdicts"] == 1


# --- AC-1 WIP inventory -------------------------------------------------

def test_wip_inventory_lists_untracked_quality_tests_only(root, monkeypatch):
    monkeypatch.setattr(
        qg, "_git_porcelain",
        lambda _root: ("?? tests/test_quality_L10.py\n"
                       "?? tests/test_quality_manifests.py\n"
                       "?? web/quality-L21.test.mjs\n"
                       "?? swarm/quality_gate.py\n"
                       " M Cargo.lock\n"),
    )
    status, payload = qg.handle("GET", "/quality/wip/inventory", root)
    assert status == 200
    paths = [f["path"] for f in payload["files"]]
    assert paths == ["tests/test_quality_L10.py",
                     "tests/test_quality_manifests.py",
                     "web/quality-L21.test.mjs"]
    assert all(f["verdict"] == "untracked" and f["recovered"] is False for f in payload["files"])


def test_wip_inventory_empty_when_tree_clean(root, monkeypatch):
    monkeypatch.setattr(qg, "_git_porcelain", lambda _root: "")
    status, payload = qg.handle("GET", "/quality/wip/inventory", root)
    assert payload == {"files": []}


# --- AC-15 CI verdict ---------------------------------------------------

def test_ci_verdict_defaults_to_fail_closed(root):
    status, payload = qg.handle("GET", "/quality/ci/50", root)
    assert status == 200
    assert payload == {"pr_number": 50, "all_green": False}


def test_ci_verdict_all_green_only_from_record(root):
    _write(root / "docs" / "gate-verdicts" / "pr-50.json",
           json.dumps({"pr_number": 50, "all_green": True, "credential_findings": 0}))
    status, payload = qg.handle("GET", "/quality/ci/50", root)
    assert payload["all_green"] is True
    assert payload["credential_findings"] == 0


def test_ci_verdict_rejects_non_integer_pr(root):
    status, payload = qg.handle("GET", "/quality/ci/abc", root)
    assert status == 404
    assert payload["code"] == "unknown_pr"


# --- routing / WSGI smoke ----------------------------------------------

def test_unknown_route_is_404_problem(root):
    status, payload = qg.handle("GET", "/quality/nope", root)
    assert status == 404
    assert payload["code"] == "not_found"


def test_all_contract_routes_are_registered():
    expected = [
        ("GET", "/quality/gates/{gateId}"),
        ("GET", "/quality/parity/{module}"),
        ("GET", "/quality/sweep/{sweepId}"),
        ("GET", "/quality/wip/inventory"),
        ("GET", "/quality/ci/{prNumber}"),
        ("GET", "/quality/engine"),
    ]
    registered = {(m, t) for m, t, _ in qg.router.routes}
    assert registered == set(expected)


@pytest.mark.parametrize("template", ["/quality/gates/{gateId}", "/quality/parity/{module}",
                                      "/quality/sweep/{sweepId}", "/quality/ci/{prNumber}"])
def test_path_params_are_single_and_named(template):
    params = qg.router.path_params(template)
    assert len(params) == 1
    assert qg.router.path_params(template) == qg.router.path_params(template)


def test_wsgi_application_serves_engine():
    captured = {}

    def start_response(status_line, headers):
        captured["status"] = status_line
        captured["headers"] = dict(headers)

    body = b"".join(qg.application({"REQUEST_METHOD": "GET", "PATH_INFO": "/quality/engine"},
                                   start_response))
    assert captured["status"] == "200 OK"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert json.loads(body)["engine"] == "python"


def test_real_checkout_smoke():
    """handle() against the owning checkout never crashes (shape only)."""
    for path in ("/quality/engine", "/quality/gates/ci-gate", "/quality/sweep/repo",
                 "/quality/ci/1"):
        status, payload = qg.handle("GET", path)
        assert status == 200
    status, payload = qg.handle("GET", "/quality/wip/inventory")
    assert status == 200
    assert isinstance(payload["files"], list)
