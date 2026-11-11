"""The 13 shared handover tools through the registry call path (`call_tool`): contracts, caller identity, the
A->B round trip, bounded bundles, artifact and definition-of-done checks, Task Store views, lease-id secrecy and the
read/write tier boundary. The tools come from the merged registry (agents.json)."""
import hashlib
import importlib
import importlib.util
import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
NAMES = [
    "shared_handover_write", "shared_handover_read", "shared_handover_validate", "shared_handover_bundle",
    "shared_handover_verify", "shared_handover_receipt", "shared_artifact_digest", "shared_artifact_check",
    "shared_dod_check", "shared_open_findings", "shared_attempt_history", "shared_lease_state", "shared_open_questions",
]
WRITE_TOOLS = {"shared_handover_write", "shared_handover_receipt"}
SECRET = "LEASE-SECRET-9f2c41d7"
ALL_AGENTS = [f"A{n:02d}" for n in range(1, 31)]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Fresh imports, an isolated SWARM_DIR and a workspace cwd; the tools come from the merged registry."""
    monkeypatch.setenv("SWARM_DIR", str(tmp_path / ".swarm"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "swarm").mkdir()
    (tmp_path / "swarm" / "__init__.py").write_text("# example file for the artifact_digest example\n")
    for name in ("SWARM_HANDOVER_MAX_BYTES", "SWARM_HANDOVER_STALE_S"):
        monkeypatch.delenv(name, raising=False)

    class E:
        ho = importlib.import_module("swarm.handover")
        sh = importlib.import_module("swarm.tools.shared_handover")
        support = importlib.import_module("swarm.tools.support")
        tr = importlib.import_module("swarm.tool_registry")
        taskstore = importlib.import_module("swarm.taskstore")
        gates = importlib.import_module("swarm.gates")
        root = tmp_path
        sdir = tmp_path / ".swarm"
        data = None

        @classmethod
        def registry(cls) -> dict:
            if cls.data is None:
                data = cls.tr.load_document()
                cls.data = data
            return cls.data

        @classmethod
        def call(cls, name, caller, payload, **kw):
            return cls.tr.call_tool(name, caller, payload, data=cls.registry(), **kw)

    return E


def task_ids(env):
    return [t["task_id"] for t in env.taskstore.TaskStore(root=env.root).list()]


def make_world(env):
    """U-req (A02, BLOCKED with needs) <- T-be (A05, attempt 2, lease mirror, failing review, one artifact)."""
    store = env.taskstore.TaskStore(root=env.root)
    store.create(task_id="U-req", correlation_id="corr-1", capability="req.spec", agent_id="A02", title="Write requirements")
    store.create(task_id="T-be", correlation_id="corr-1", capability="be.code", agent_id="A05", title="Implement the export endpoint",
                 acceptance=["The export endpoint returns CSV", "Rows stream in memory-safe chunks"], depends_on=["U-req"],
                 risk_class="medium")
    for state in ("VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS"):
        store.transition("U-req", state)
    store.transition("U-req", "BLOCKED", reason="needs a rate limit decision")
    store.set_notes("U-req", result={"state": "BLOCKED", "needs": ["Which rate limit applies?", "Who owns the API key?"],
                                     "summary_md": "Waiting on product"})
    for state in ("VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS"):
        store.transition("T-be", state)
    store.set_notes("T-be", lease={"lease_id": SECRET, "agent": "A05", "surface": "omp", "state": "held", "ttl_s": 600,
                                   "graph_id": "g-1", "at": time.time()})
    store.transition("T-be", "FAILED", reason=f"E-TIMEOUT: tests hung, lost lease {SECRET}")
    store.transition("T-be", "RETRY", reason="retry after timeout")
    store.transition("T-be", "CLAIMED")
    store.transition("T-be", "IN_PROGRESS")
    store.add_artifact("T-be", kind="file", uri="src/export.py", digest="sha256:" + "b" * 64, producer="A05")
    f1 = env.gates.make_finding("F1", "major", "bug", "Null dereference in export", location="src/export.py:10")
    f2 = env.gates.make_finding("F2", "minor", "style", "Rename helper")
    store.record_verdict("T-be", env.gates.make_verdict(gate="review", task_id="T-be", agent_id="A09", findings=[f1, f2],
                                                        correlation_id="corr-1", root=env.root, audit=False))
    store.record_verdict("T-be", env.gates.make_verdict(gate="quality", task_id="T-be", agent_id="A08", findings=[],
                                                        correlation_id="corr-1", root=env.root, audit=False))
    store.record_verdict("T-be", env.gates.make_verdict(
        gate="security", task_id="T-be", agent_id="A10", findings=[env.gates.make_finding("F3", "critical", "vuln", "Old")],
        correlation_id="corr-1", root=env.root, audit=False, expires_s=0))  # failing but already expired
    return store


def handover_payload(**over):
    body = {
        "task_id": "T-be", "to_agent": "A08", "goal": "Implement the CSV export endpoint",
        "completed": ["Added the /export route"], "remaining": ["Write the unit tests"],
        "decisions": [{"decision": "Stream rows", "rationale": "Exports can be large"}],
        "artifacts": [{"kind": "file", "uri": "src/export.py", "digest": "sha256:" + "b" * 64, "producer": "A05"}],
        "open_questions": ["Is 50k rows the maximum?"], "constraints": ["Standard library only"],
        "verification": {"status": "partial", "confidence": 0.6, "checks": ["unit: 3 passed"]},
    }
    body.update(over)
    return body


def tree(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if "__pycache__" not in p.parts}


# ---- contracts ------------------------------------------------------------------------------------------------
def test_fragment_or_registry_holds_the_13_tools_with_valid_contracts(env, tmp_path):
    data = env.registry()
    by_name = {t["name"]: t for t in data["shared_tools"]}
    assert set(NAMES) <= set(by_name)
    for name in NAMES:
        tool = by_name[name]
        assert tool["owner"] == "shared" and tool["permitted_callers"] == ALL_AGENTS, name
        assert tool["side_effect_tier"] == ("WRITE" if name in WRITE_TOOLS else "READ"), name
        assert tool["errors"] == ["INVALID_INPUT", "UNAUTHORIZED", "DEPENDENCY_UNAVAILABLE", "TIMEOUT", "PARTIAL_SUCCESS"]
        assert set(tool["examples"]) == {"success", "invalid_input", "unauthorized_caller", "dependency", "timeout", "partial"}
        assert tool["entrypoint"] == "swarm/tools/shared_handover.py"
        assert tool["input_schema"]["additionalProperties"] is False and "probe" in tool["input_schema"]["properties"]
    merged = tmp_path / "merged-agents.json"
    merged.write_text(json.dumps(data), encoding="utf-8")
    problems = [e for e in env.tr.validate_registry(merged, root=ROOT) if any(n in e for n in NAMES)]
    assert problems == []


def test_every_example_behaves_as_declared(env):
    for name in NAMES:
        tool = {t["name"]: t for t in env.registry()["shared_tools"]}[name]
        ex = tool["examples"]
        first = tool["permitted_callers"][0]
        assert env.call(name, first, ex["success"])["state"] in ("SUCCESS", "INVALID_INPUT"), name
        bad = env.call(name, first, ex["invalid_input"])
        assert bad["state"] == "INVALID_INPUT" and bad["field"], name
        refused = env.call(name, ex["unauthorized_caller"], ex["success"])
        assert refused["state"] == "UNAUTHORIZED" and refused["target"] == name
        assert env.call(name, first, ex["dependency"])["state"] == "DEPENDENCY_UNAVAILABLE"
        assert env.call(name, first, ex["timeout"], timeout_s=0.05)["state"] == "TIMEOUT"
        partial = env.call(name, first, ex["partial"])
        assert partial["state"] == "PARTIAL_SUCCESS" and 0 <= partial["completed_fraction"] <= 1


def test_every_agent_may_call_every_tool(env):
    for caller in ALL_AGENTS:
        for name in ("shared_handover_read", "shared_open_questions", "shared_lease_state"):
            assert env.call(name, caller, {"task_id": "T-none"})["state"] == "SUCCESS", (caller, name)


# ---- A -> B round trip ----------------------------------------------------------------------------------------
def test_a_writes_b_continues_from_the_document_and_a_bounded_bundle(env):
    make_world(env)
    written = env.call("shared_handover_write", "A05", handover_payload())
    assert written["state"] == "SUCCESS", written
    assert written["handover_id"] == "T-be.1" and written["seq"] == 1 and written["status"] == "ok"
    assert written["duplicate"] is False and written["summarized"] is False and written["digest"].startswith("sha256:")

    read = env.call("shared_handover_read", "A08", {"task_id": "T-be"})
    assert read["found"] is True and read["status"] == "ok" and read["reasons"] == []
    doc = read["handover"]
    assert (doc["from_agent"], doc["to_agent"]) == ("A05", "A08")  # agent ids, never slugs
    assert doc["goal"] == "Implement the CSV export endpoint" and doc["remaining"] == ["Write the unit tests"]
    assert doc["constraints"] == ["Standard library only"] and doc["artifacts"][0]["digest"] == "sha256:" + "b" * 64
    assert doc["digest"] == written["digest"] and doc["correlation_id"] == "corr-1"  # plan correlation taken from the Task Store

    bundle = env.call("shared_handover_bundle", "A08", {"task_id": "T-be"})
    assert bundle["state"] == "SUCCESS" and bundle["task_known"] is True and bundle["truncated"] is False
    assert bundle["task"]["agent"] == "A05" and bundle["task"]["state"] == "IN_PROGRESS" and bundle["task"]["attempt"] == 2
    assert bundle["goal"] == "Implement the CSV export endpoint"
    assert bundle["acceptance"] == ["The export endpoint returns CSV", "Rows stream in memory-safe chunks"]
    assert bundle["handover"]["status"] == "ok" and bundle["handover"]["document"]["remaining"] == ["Write the unit tests"]
    assert [u["task_id"] for u in bundle["upstream"]] == ["U-req"]
    assert bundle["upstream"][0]["state"] == "BLOCKED" and bundle["upstream"][0]["satisfied"] is False
    assert bundle["upstream"][0]["needs"] == ["Which rate limit applies?", "Who owns the API key?"]
    assert any(a["uri"] == "src/export.py" for a in bundle["artifacts"])
    gates = {v["gate"]: v for v in bundle["verdicts"]}
    assert gates["review"]["verdict"] == "fail" and gates["review"]["blocking"] == 1 and gates["review"]["agent"] == "A09"
    assert gates["review"]["signature"] == "valid" and gates["quality"]["verdict"] == "pass"
    assert bundle["lease"]["has_lease"] is True and bundle["lease"]["agent"] == "A05"
    assert bundle["attempts"]["attempt"] == 2 and "E-TIMEOUT" in bundle["attempts"]["last_error"]
    assert {q["origin"] for q in bundle["open_questions"]} == {"upstream", "handover"}
    assert bundle["bytes"] == len(env.ho.canonical(bundle)) and bundle["bytes"] <= 2 * env.ho.max_bytes_default()

    receipt = env.call("shared_handover_receipt", "A08", {"task_id": "T-be", "digest": written["digest"],
                                                          "understood": ["Implement the export endpoint"],
                                                          "questions": ["Is 50k rows the maximum?"]})
    assert receipt["state"] == "SUCCESS" and receipt["duplicate"] is False and receipt["handover_id"] == "T-be.1"
    again = env.call("shared_handover_receipt", "A08", {"task_id": "T-be", "digest": written["digest"]})
    assert again["duplicate"] is True and again["receipt"] == receipt["receipt"]

    verify = env.call("shared_handover_verify", "A08", {"task_id": "T-be"})
    assert verify["status"] == "ok" and verify["ready"] is False  # upstream U-req is still BLOCKED
    assert verify["upstream"] == [{"task_id": "U-req", "state": "BLOCKED", "satisfied": False}]
    assert any("U-req is BLOCKED" in r for r in verify["reasons"])


def test_the_bundle_stays_bounded_when_everything_is_large(env, monkeypatch):
    monkeypatch.setenv("SWARM_HANDOVER_MAX_BYTES", "4096")
    store = make_world(env)
    for n in range(40):
        store.add_artifact("T-be", kind="file", uri=f"src/gen{n}.py", digest="sha256:" + f"{n:x}".rjust(64, "c"), producer="A05")
    for n in range(12):
        tid = f"U-{n}"
        store.create(task_id=tid, correlation_id="corr-1", capability="req.spec", agent_id="A02")
        store.set_notes(tid, result={"summary_md": "s" * 2000, "needs": [f"question {n}-{k} " + "q" * 200 for k in range(10)]})
    store.update("T-be", depends_on=json.dumps(["U-req"] + [f"U-{n}" for n in range(12)]))
    store.conn.commit()
    big = handover_payload(completed=[f"step {i} " + "x" * 300 for i in range(100)], remaining=[f"todo {i}" for i in range(5)])
    assert env.call("shared_handover_write", "A05", big)["summarized"] is True
    bundle = env.call("shared_handover_bundle", "A08", {"task_id": "T-be"})
    assert bundle["state"] == "SUCCESS"
    assert bundle["bytes"] == len(env.ho.canonical(bundle)) <= 2 * 4096
    assert bundle["truncated"] is True
    assert bundle["handover"]["document"]["remaining"] == big["remaining"]  # the mandatory sections were not cut


# ---- caller identity ------------------------------------------------------------------------------------------
def test_from_agent_must_be_the_authenticated_caller(env):
    wrong = env.call("shared_handover_write", "A05", handover_payload(from_agent="A02", correlation_id="corr-1"))
    assert wrong["state"] == "INVALID_INPUT" and wrong["field"] == "from_agent" and "A05" in wrong["message"]
    slug = env.call("shared_handover_write", "A05", handover_payload(from_agent="a05-backend", correlation_id="corr-1"))
    assert slug["state"] == "INVALID_INPUT" and slug["field"] == "from_agent"
    same = env.call("shared_handover_write", "A05", handover_payload(from_agent="A05", correlation_id="corr-1"))
    assert same["state"] == "SUCCESS"
    direct = env.sh.handover_write(handover_payload(correlation_id="corr-1"))  # no registry, no authenticated caller
    assert direct["state"] == "INVALID_INPUT" and "no authenticated caller" in direct["message"]
    assert not (env.sdir / "handovers" / "corr-2").exists()


def test_to_agent_must_be_a_known_agent_id_and_documents_name_fields(env):
    for target in ("a08-qa", "A99", "nobody"):
        result = env.call("shared_handover_write", "A05", handover_payload(to_agent=target, correlation_id="corr-1"))
        assert result["state"] == "INVALID_INPUT" and result["field"] == "to_agent", target
    blank = env.call("shared_handover_write", "A05", handover_payload(goal="   ", correlation_id="corr-1"))
    assert blank["state"] == "INVALID_INPUT" and blank["field"] == "goal" and "/goal" in blank["message"]
    digest = env.call("shared_handover_write", "A05", handover_payload(
        artifacts=[{"kind": "file", "uri": "a", "digest": "abc", "producer": "A05"}], correlation_id="corr-1"))
    assert digest["field"] == "artifacts" and "/artifacts/0/digest" in digest["message"]
    assert env.call("shared_handover_write", "A05", {**handover_payload(correlation_id="corr-1"), "bogus": 1})["field"] == "bogus"


def test_correlation_comes_from_the_plan_and_a_conflicting_one_is_refused(env):
    make_world(env)
    conflict = env.call("shared_handover_write", "A05", handover_payload(correlation_id="corr-other"))
    assert conflict["state"] == "INVALID_INPUT" and conflict["field"] == "correlation_id"
    unknown = env.call("shared_handover_write", "A05", handover_payload(task_id="T-unplanned"))
    assert unknown["state"] == "INVALID_INPUT" and unknown["field"] == "correlation_id"
    (env.sdir / "latest_correlation").write_text("corr-latest\n")
    assert env.call("shared_handover_write", "A05", handover_payload(task_id="T-unplanned"))["state"] == "SUCCESS"
    assert (env.sdir / "handovers" / "corr-latest" / "T-unplanned.1.json").is_file()


def test_write_is_idempotent_and_redacts(env):
    payload = handover_payload(correlation_id="corr-1", goal="Use token ghp_" + "Q" * 36)
    first = env.call("shared_handover_write", "A05", payload)
    again = env.call("shared_handover_write", "A05", payload)
    assert first["redacted"] is True and again["duplicate"] is True and again["digest"] == first["digest"]
    read = env.call("shared_handover_read", "A08", {"task_id": "T-be"})
    assert read["handover"]["goal"] == "Use token [REDACTED]"


def test_read_reports_each_status_without_failing(env, monkeypatch):
    assert env.call("shared_handover_read", "A08", {"task_id": "T-be"}) == {
        "state": "SUCCESS", "found": False, "status": "missing", "reasons": ["no handover for task T-be recorded"], "handover": {}}
    env.call("shared_handover_write", "A05", handover_payload(correlation_id="corr-1"))
    (env.sdir / "handovers" / "corr-1" / "T-be.1.json").write_text("{", encoding="utf-8")
    bad = env.call("shared_handover_read", "A08", {"task_id": "T-be"})
    assert bad["state"] == "SUCCESS" and bad["found"] is False and bad["status"] == "malformed"
    absent = env.call("shared_handover_read", "A08", {"task_id": "T-be", "seq": 4})  # a seq the task lacks is "no such handover", never another one
    assert absent == {"state": "SUCCESS", "found": False, "status": "missing", "reasons": ["no handover 4 of task T-be recorded"], "handover": {}}
    assert env.call("shared_handover_read", "A08", {"task_id": "T-be", "seq": 0})["field"] == "seq"
    assert env.call("shared_handover_read", "A08", {"task_id": "../x"})["field"] == "task_id"


def test_validate_tool_checks_a_draft_and_reports_size(env):
    draft = {k: v for k, v in handover_payload().items() if k != "to_agent"}
    draft.update(correlation_id="corr-1", from_agent="A05", to_agent="A08", blockers=[])
    ok = env.call("shared_handover_validate", "A05", {"document": draft, "draft": True})
    assert ok["valid"] is True and ok["errors"] == [] and 0 < ok["bytes"] < ok["cap"] and ok["would_summarize"] is False
    assert env.call("shared_handover_validate", "A05", {"document": draft})["valid"] is False  # generated fields missing
    broken = env.call("shared_handover_validate", "A05", {"document": {**draft, "goal": "", "to_agent": "a08-qa"}, "draft": True})
    assert broken["valid"] is False and broken["bytes"] == 0
    assert {e.split(":")[0] for e in broken["errors"]} >= {"/goal", "/to_agent"}
    assert not (env.sdir / "handovers").exists()  # validating writes nothing


# ---- receipts -------------------------------------------------------------------------------------------------
def test_only_the_addressed_receiver_may_acknowledge(env):
    written = env.call("shared_handover_write", "A05", handover_payload(correlation_id="corr-1"))
    sender = env.call("shared_handover_receipt", "A05", {"task_id": "T-be", "digest": written["digest"]})
    assert sender["state"] == "INVALID_INPUT" and "addressed to A08, not to A05" in sender["message"]
    ghost = env.call("shared_handover_receipt", "A08", {"task_id": "T-be", "digest": "sha256:" + "9" * 64})
    assert ghost["state"] == "INVALID_INPUT" and ghost["field"] == "digest"
    assert env.sh.handover_receipt({"task_id": "T-be", "digest": written["digest"]})["field"] == "task_id"
    ok = env.call("shared_handover_receipt", "A08", {"task_id": "T-be", "digest": written["digest"], "accepted": False,
                                                     "questions": ["What is the row cap?"]})
    assert ok["accepted"] is False and ok["questions"] == 1


# ---- artifacts ------------------------------------------------------------------------------------------------
def test_artifact_digest_and_check_over_a_real_file(env):
    (env.root / "out").mkdir()
    target = env.root / "out" / "report.csv"
    target.write_bytes(b"a,b\n1,2\n")
    digest = "sha256:" + hashlib.sha256(b"a,b\n1,2\n").hexdigest()
    assert env.call("shared_artifact_digest", "A05", {"path": "out/report.csv"}) == {
        "state": "SUCCESS", "digest": digest, "bytes": 8, "path": "out/report.csv"}
    assert env.call("shared_artifact_digest", str("A05"), {"path": str(target)})["digest"] == digest
    for path in ("out/missing.csv", "out", "/etc/hostname", "../outside.txt"):
        refused = env.call("shared_artifact_digest", "A05", {"path": path})
        assert refused["state"] == "INVALID_INPUT" and refused["field"] == "path", path

    outputs = [
        {"kind": "file", "uri": "out/report.csv", "digest": digest, "producer": "A05"},
        {"kind": "file", "uri": f"file://{target}", "digest": digest, "producer": "A05"},
        {"kind": "file", "uri": "out/report.csv", "digest": "sha256:" + "0" * 64, "producer": "A05"},
        {"kind": "file", "uri": "out/gone.csv", "digest": digest, "producer": "A05"},
        {"kind": "file", "uri": "out/report.csv", "digest": digest, "producer": "A02"},
        {"kind": "file", "uri": "out/report.csv", "digest": "", "producer": "A05"},
        {"kind": "pr", "uri": "https://github.com/o/r/pull/1", "digest": digest, "producer": "A05"},
        {"kind": "file", "uri": "/etc/hostname", "digest": digest, "producer": "A05"},
    ]
    result = env.call("shared_artifact_check", "A08", {"task_id": "T-be", "agent": "A05", "outputs": outputs})
    assert result["state"] == "SUCCESS" and result["expected_producer"] == "A05"
    assert [r["status"] for r in result["results"]] == [
        "ok", "ok", "digest_mismatch", "missing", "producer_mismatch", "no_digest", "unchecked", "unchecked"]
    mismatch, missing, wrong_producer, remote, outside = (result["results"][i] for i in (2, 3, 4, 6, 7))
    assert mismatch["digest_match"] == "no" and mismatch["actual_digest"] == digest
    assert missing["exists"] == "no" and wrong_producer["producer_ok"] == "no"
    assert remote["exists"] == "unchecked" and "not a local file" in remote["reason"]
    assert outside["exists"] == "unchecked" and "outside the workspace" in outside["reason"]
    assert (result["checked"], result["ok_count"], result["failed"], result["unchecked"]) == (8, 2, 3, 3)  # unchecked: remote, outside, no digest
    assert result["ok"] is False and result["verified"] is False
    clean = env.call("shared_artifact_check", "A08", {"task_id": "T-be", "agent": "A05", "outputs": outputs[:2]})
    assert clean["ok"] is True and clean["verified"] is True and clean["failed"] == 0


def test_artifact_check_defaults_to_the_tasks_own_outputs(env):
    make_world(env)
    (env.root / "src").mkdir()
    (env.root / "src" / "export.py").write_text("print('x')\n", encoding="utf-8")
    env.call("shared_handover_write", "A05", handover_payload(artifacts=[
        {"kind": "file", "uri": "src/export.py", "digest": "sha256:" + "b" * 64, "producer": "A05"}]))
    result = env.call("shared_artifact_check", "A08", {"task_id": "T-be"})
    assert result["expected_producer"] == "A05" and result["task_known"] is True
    assert {r["uri"] for r in result["results"]} == {"src/export.py"}  # handover + store rows are one artifact
    assert result["results"][0]["status"] == "digest_mismatch" and result["ok"] is False


# ---- definition of done ---------------------------------------------------------------------------------------
def test_dod_check_is_met_only_with_evidence(env):
    result = {
        "summary_md": "- Added the export endpoint; it returns CSV for any dataset\n- Rows stream in chunks, memory stays flat\n"
                      "- Docs still pending",
        "outputs": [{"kind": "file", "uri": "src/export.py"}],
        "remaining": ["Write the migration guide"],
        "verification": {"status": "partial", "confidence": 0.5, "checks": [
            {"criterion": "rate limit enforced", "status": "pass"}, {"criterion": "audit log written", "status": "fail"}]},
    }
    criteria = ["The export endpoint returns CSV", "Rows stream in memory-safe chunks", "Documentation is complete",
                "Write the migration guide", "All endpoints are rate limited", "Rate limit enforced on export",
                "Audit log written for every export", "and the of"]
    out = env.call("shared_dod_check", "A08", {"criteria": criteria, "result": result})
    status = {r["criterion"]: r for r in out["results"]}
    assert status["The export endpoint returns CSV"]["status"] == "met"
    assert "summary_md" in status["The export endpoint returns CSV"]["evidence"][0]
    assert status["Rows stream in memory-safe chunks"]["status"] == "met"
    assert status["Documentation is complete"]["status"] == "unknown" and "no evidence" in status["Documentation is complete"]["reason"]
    assert status["Write the migration guide"]["status"] == "unknown"  # the guide is listed as remaining
    assert "outstanding" in status["Write the migration guide"]["reason"]
    assert status["All endpoints are rate limited"]["status"] == "unknown"
    assert status["Rate limit enforced on export"]["status"] == "met"
    assert status["Audit log written for every export"]["status"] == "unknown"  # a failing check is not evidence of done
    assert status["and the of"]["status"] == "unknown" and "no checkable terms" in status["and the of"]["reason"]
    assert (out["total"], out["met"], out["unknown"], out["all_met"]) == (8, 3, 5, False)


def test_dod_check_never_guesses_met(env):
    criteria = ["The export endpoint returns CSV", "Rows stream in memory-safe chunks"]
    for empty in ({}, {"summary_md": ""}, {"summary_md": "Nothing relevant happened here"}, {"outputs": []}):
        out = env.call("shared_dod_check", "A08", {"criteria": criteria, "result": empty})
        assert out["met"] == 0 and out["all_met"] is False and {r["status"] for r in out["results"]} == {"unknown"}, empty
    negated = env.call("shared_dod_check", "A08", {"criteria": criteria[:1], "result": {
        "summary_md": "The export endpoint does not return CSV yet"}})
    assert negated["results"][0]["status"] == "unknown"  # the same words, negated
    unchecked = env.call("shared_dod_check", "A08", {"criteria": criteria[:1], "result": {"summary_md": "- [ ] export endpoint returns CSV"}})
    assert unchecked["results"][0]["status"] == "unknown"
    ref = env.call("shared_dod_check", "A08", {"criteria": ["AC3: export works"], "result": {"completed": ["AC3 covered by test_export"]}})
    assert ref["results"][0]["status"] == "met"
    refused = env.call("shared_dod_check", "A08", {"result": {}})
    assert refused["state"] == "INVALID_INPUT" and refused["field"] == "criteria"


def test_dod_check_does_not_mark_a_contradicted_criterion_met(env):
    """'remains unfixed' contradicts 'No critical vulnerabilities remain'. _NEGATIVE lacks remains/unfixed/still/open,
    so the summary line counts as affirmative evidence and the criterion is marked met."""
    out = env.call("shared_dod_check", "A08", {"criteria": ["No critical vulnerabilities remain"], "result": {
        "summary_md": "Critical vulnerability found in parser; remains unfixed."}})
    assert out["results"][0]["status"] == "unknown"
    assert out["all_met"] is False


def test_dod_check_takes_the_tasks_acceptance_criteria(env):
    make_world(env)
    out = env.call("shared_dod_check", "A08", {"task_id": "T-be", "result": {"summary_md": "The export endpoint returns CSV"}})
    assert [r["status"] for r in out["results"]] == ["met", "unknown"] and out["total"] == 2


# ---- Task Store views -----------------------------------------------------------------------------------------
def test_open_findings_lists_only_unexpired_failing_findings(env):
    make_world(env)
    out = env.call("shared_open_findings", "A08", {"task_id": "T-be"})
    assert [(f["gate"], f["agent"], f["id"], f["severity"], f["blocking"]) for f in out["findings"]] == [
        ("review", "A09", "F1", "major", True), ("review", "A09", "F2", "minor", False)]
    assert out["count"] == 2 and out["blocking"] == 1 and out["findings"][0]["location"] == "src/export.py:10"
    assert out["findings"][0]["expires_in_s"] > 0
    assert all(f["id"] != "F3" for f in out["findings"])  # expired security verdict
    assert env.call("shared_open_findings", "A08", {"task_id": "U-req"})["findings"] == []
    assert env.call("shared_open_findings", "A08", {"task_id": "T-none"})["task_known"] is False


def test_attempt_history_shows_transitions_attempts_and_last_error(env):
    make_world(env)
    out = env.call("shared_attempt_history", "A05", {"task_id": "T-be"})
    assert (out["task_state"], out["attempt"], out["max_attempts"], out["retries_left"]) == ("IN_PROGRESS", 2, 3, 1)
    assert out["rework_loops"] == 0 and out["max_rework_loops"] == 2
    assert [t["to"] for t in out["transitions"]][-4:] == ["FAILED", "RETRY", "CLAIMED", "IN_PROGRESS"]
    assert out["transition_count"] == len(out["transitions"]) and out["last_error"].startswith("FAILED: E-TIMEOUT")
    empty = env.call("shared_attempt_history", "A05", {"task_id": "T-none"})
    assert empty["task_known"] is False and empty["transitions"] == [] and empty["attempt"] == 0


def test_open_questions_are_the_unresolved_needs_of_upstream_and_the_handover(env):
    store = make_world(env)
    env.call("shared_handover_write", "A05", handover_payload())
    out = env.call("shared_open_questions", "A08", {"task_id": "T-be"})
    assert [(q["origin"], q["question"]) for q in out["questions"]] == [
        ("upstream", "Which rate limit applies?"), ("upstream", "Who owns the API key?"), ("handover", "Is 50k rows the maximum?")]
    assert out["questions"][0]["task_id"] == "U-req" and out["questions"][0]["task_state"] == "BLOCKED"
    store.transition("U-req", "CLAIMED")  # released and picked up again
    store.transition("U-req", "IN_PROGRESS")
    store.transition("U-req", "IN_REVIEW")  # upstream answered and delivered: its needs are resolved
    after = env.call("shared_open_questions", "A08", {"task_id": "T-be"})
    assert [q["origin"] for q in after["questions"]] == ["handover"] and after["count"] == 1


def test_verify_checks_handover_status_and_upstream(env):
    store = make_world(env)
    assert env.call("shared_handover_verify", "A08", {"task_id": "T-be"})["status"] == "missing"
    env.call("shared_handover_write", "A05", handover_payload())
    blocked = env.call("shared_handover_verify", "A08", {"task_id": "T-be"})
    assert blocked["status"] == "ok" and blocked["ready"] is False  # upstream U-req is still BLOCKED
    for state in ("CLAIMED", "IN_PROGRESS", "IN_REVIEW"):
        store.transition("U-req", state)
    ready = env.call("shared_handover_verify", "A08", {"task_id": "T-be"})
    assert ready["status"] == "ok" and ready["ready"] is True and ready["reasons"] == []
    assert ready["handover_id"] == "T-be.1" and ready["upstream"][0]["satisfied"] is True
    assert "not used" in ready["substrate"]
    store.update("T-be", correlation_id="corr-2")  # the plan moved on: the old handover no longer counts
    stale = env.call("shared_handover_verify", "A08", {"task_id": "T-be"})
    assert stale["status"] == "stale" and stale["ready"] is False and "correlation mismatch" in stale["reasons"][0]


def test_lease_state_is_a_redacted_mirror(env):
    make_world(env)
    out = env.call("shared_lease_state", "A05", {"task_id": "T-be"})
    lease = out["lease"]
    assert lease["has_lease"] is True and (lease["agent"], lease["state"], lease["ttl_s"]) == ("A05", "held", 600)
    assert lease["surface"] == "omp" and lease["graph_id"] == "g-1" and lease["expired"] is False
    assert SECRET not in json.dumps(out) and "lease_id" not in json.dumps(out)
    none = env.call("shared_lease_state", "A05", {"task_id": "U-req"})["lease"]
    assert none["has_lease"] is False and none["state"] == "none"


def test_lease_ids_never_appear_in_any_tool_output(env):
    """The lease id is the holder's fencing token: not in a lease view, a bundle, a transition reason, a handover or a read."""
    make_world(env)
    written = env.call("shared_handover_write", "A05", handover_payload(
        goal=f"Continue; the old lease {SECRET} was lost", open_questions=[f"Who holds {SECRET}?"]))
    assert written["state"] == "SUCCESS"
    payloads = {
        "shared_handover_write": handover_payload(goal=f"Again with {SECRET}", task_id="T-be"),
        "shared_handover_read": {"task_id": "T-be"}, "shared_handover_bundle": {"task_id": "T-be"},
        "shared_handover_verify": {"task_id": "T-be"}, "shared_open_questions": {"task_id": "T-be"},
        "shared_open_findings": {"task_id": "T-be"}, "shared_attempt_history": {"task_id": "T-be"},
        "shared_lease_state": {"task_id": "T-be"},
        "shared_artifact_check": {"task_id": "T-be"},
        "shared_dod_check": {"task_id": "T-be", "result": {"summary_md": f"done under {SECRET}"}},
        "shared_handover_validate": {"document": {"task_id": "T-be"}, "draft": True},
    }
    for name, payload in payloads.items():
        out = env.call(name, "A08" if name != "shared_handover_write" else "A05", payload)
        assert out["state"] == "SUCCESS", (name, out)
        assert SECRET not in json.dumps(out), name
    history = env.call("shared_attempt_history", "A05", {"task_id": "T-be"})
    assert any("[REDACTED]" in t["reason"] for t in history["transitions"])  # the reason that carried it is scrubbed
    assert "lease_id" not in json.dumps(env.call("shared_handover_bundle", "A08", {"task_id": "T-be"}))


# ---- tier boundary --------------------------------------------------------------------------------------------
def test_read_tools_change_nothing_on_disk(env):
    """Reads leave every handover/scratch/status/task-store file untouched; the append-only run log `events.jsonl` is the
    one exception (call_tool audits each call into it once `.swarm` exists)."""
    make_world(env)
    env.call("shared_handover_write", "A05", handover_payload())
    (env.root / "f.txt").write_text("x")

    def data_tree() -> set[str]:
        return {p for p in tree(env.root) if p != ".swarm/events.jsonl"}

    def data_mtimes() -> dict:
        return {p: p.stat().st_mtime_ns for p in env.root.rglob("*")
                if p.is_file() and "__pycache__" not in p.parts and p != env.sdir / "events.jsonl"}

    before = data_tree()
    mtimes = data_mtimes()
    reads = [n for n in NAMES if n not in WRITE_TOOLS]
    for name in reads:
        tool = {t["name"]: t for t in env.registry()["shared_tools"]}[name]
        payload = dict(tool["examples"]["success"])
        if "task_id" in payload:
            payload["task_id"] = "T-be"
        out = env.call(name, "A08", payload)
        assert out["state"] == "SUCCESS", (name, out)
    assert data_tree() == before
    assert data_mtimes() == mtimes


def test_read_tools_do_not_create_the_task_store_or_swarm_dir(env):
    for name in NAMES:
        if name in WRITE_TOOLS:
            continue
        tool = {t["name"]: t for t in env.registry()["shared_tools"]}[name]
        assert env.call(name, "A08", tool["examples"]["success"])["state"] == "SUCCESS", name
    assert not env.sdir.exists()


def test_write_tools_write_only_under_the_swarm_dir(env):
    before = tree(env.root)
    written = env.call("shared_handover_write", "A05", handover_payload(correlation_id="corr-1"))
    env.call("shared_handover_receipt", "A08", {"task_id": "T-be", "digest": written["digest"]})
    new = tree(env.root) - before
    assert new and all(p == ".swarm" or p.startswith(".swarm/") for p in new), new
    assert {p for p in new if p.endswith(".json")} >= {".swarm/handovers/corr-1/T-be.1.json"}


# ---- RV-B / skill-review fixes ---------------------------------------------------------------------------------
def _rework(env):
    store = make_world(env)
    store.transition("T-be", "IN_REVIEW")
    store.transition("T-be", "CHANGES_REQUESTED", reason="review found a defect")
    return store


def test_rework_intake_sees_the_findings_that_caused_the_rework_flagged_stale(env):
    """Skill reviewers: create a task, record a failing quality/review verdict, transition IN_REVIEW->CHANGES_REQUESTED, call
    shared_open_findings and shared_handover_bundle: both returned EMPTY, because StoreReader.verdicts kept only rows
    with ts >= notes.verdicts_since and transition(CHANGES_REQUESTED) sets verdicts_since=now."""
    _rework(env)
    out = env.call("shared_open_findings", "A05", {"task_id": "T-be"})
    assert [(f["gate"], f["id"], f["blocking"], f["stale"]) for f in out["findings"]] == [
        ("review", "F1", True, True), ("review", "F2", False, True)]
    assert out["count"] == 2 and out["blocking"] == 1 and out["stale_count"] == 2
    assert out["findings"][0]["kind"] == "bug"
    bundle = env.call("shared_handover_bundle", "A05", {"task_id": "T-be"})
    gates = {v["gate"]: v for v in bundle["verdicts"]}
    assert gates["review"]["verdict"] == "fail" and gates["review"]["stale"] is True and gates["review"]["blocking"] == 1
    assert gates["quality"]["verdict"] == "pass" and gates["quality"]["stale"] is True


def test_a_verdict_issued_after_the_rework_is_current_and_replaces_the_stale_one(env):
    store = _rework(env)
    time.sleep(0.01)
    f4 = env.gates.make_finding("F4", "major", "bug", "Still wrong after the fix", location="src/export.py:12")
    store.record_verdict("T-be", env.gates.make_verdict(gate="review", task_id="T-be", agent_id="A09", findings=[f4],
                                                        correlation_id="corr-1", root=env.root, audit=False))
    out = env.call("shared_open_findings", "A05", {"task_id": "T-be"})
    assert [(f["id"], f["stale"]) for f in out["findings"]] == [("F4", False)]  # F1/F2 belonged to the replaced verdict
    assert out["stale_count"] == 0 and out["blocking"] == 1
    gates = {v["gate"]: v for v in env.call("shared_handover_bundle", "A05", {"task_id": "T-be"})["verdicts"]}
    assert gates["review"]["stale"] is False and gates["quality"]["stale"] is True


def test_a_task_that_was_never_reworked_has_no_stale_findings(env):
    make_world(env)
    out = env.call("shared_open_findings", "A08", {"task_id": "T-be"})
    assert out["stale_count"] == 0 and not any(f["stale"] for f in out["findings"])
    assert not any(v["stale"] for v in env.call("shared_handover_bundle", "A08", {"task_id": "T-be"})["verdicts"])


def test_open_findings_output_schema_declares_kind_and_stale(env):
    """The handler returned `kind` but the output schema left `findings` an untyped array."""
    schema = {t["name"]: t for t in env.registry()["shared_tools"]}["shared_open_findings"]["output_schema"]
    item = schema["properties"]["findings"]["items"]
    assert {"kind", "stale", "blocking", "ac_ref", "expires_in_s"} <= set(item["properties"])
    assert item["additionalProperties"] is False and "stale_count" in schema["properties"]
    out = env.call("shared_open_findings", "A08", {"task_id": "T-none"})
    assert out["state"] == "SUCCESS"
    make_world(env)
    full = env.call("shared_open_findings", "A08", {"task_id": "T-be"})  # _finish validates it against the schema
    assert full["state"] == "SUCCESS" and set(full["findings"][0]) == set(item["properties"])


def test_bundle_is_a_read_path_and_survives_a_reader_with_a_lower_cap(env, monkeypatch):
    """RV-B #2a: write a doc whose mandatory fields take ~8 KiB, set SWARM_HANDOVER_MAX_BYTES=4096 in the reader: the bundle
    returned INVALID_INPUT field=goal "...alone take 8020 bytes" while handover_read and handover_verify said ok."""
    make_world(env)
    heavy = handover_payload(remaining=["r" * 1800, "s" * 1800], constraints=["c" * 1800, "d" * 1800])
    written = env.call("shared_handover_write", "A05", heavy)
    assert written["state"] == "SUCCESS" and written["bytes"] > 7000
    monkeypatch.setenv("SWARM_HANDOVER_MAX_BYTES", "4096")
    assert env.call("shared_handover_read", "A08", {"task_id": "T-be"})["status"] == "ok"
    bundle = env.call("shared_handover_bundle", "A08", {"task_id": "T-be"})
    assert bundle["state"] == "SUCCESS", bundle
    assert bundle["handover"]["document"]["remaining"] == heavy["remaining"]  # mandatory sections whole
    assert bundle["handover"]["document"]["constraints"] == heavy["constraints"]
    assert bundle["handover"]["status"] == "ok" and bundle["bytes"] == len(env.ho.canonical(bundle))
    assert bundle["over_cap"] is True and bundle["bytes"] > 2 * 4096  # reported, not hidden


def test_bundle_names_the_stored_digest_even_when_the_document_was_shortened(env, monkeypatch):
    """RV-B #2b: the summarized document had no `digest`, so a receiver building a handover_receipt from the bundle had
    nothing to acknowledge. `handover.digest` and `handover.handover_id` always name the stored file."""
    make_world(env)
    monkeypatch.setenv("SWARM_HANDOVER_MAX_BYTES", "4096")
    written = env.call("shared_handover_write", "A05", handover_payload(
        completed=[f"done item {i} " + "x" * 100 for i in range(30)]))
    assert written["state"] == "SUCCESS"
    monkeypatch.setenv("SWARM_HANDOVER_MAX_BYTES", "1024")  # the reader's cap is tiny: the document is shortened
    bundle = env.call("shared_handover_bundle", "A08", {"task_id": "T-be"})
    assert bundle["handover"]["digest"] == written["digest"] and bundle["handover"]["handover_id"] == written["handover_id"]
    assert bundle["handover"]["document"]["summarized"] is True and bundle["truncated"] is True
    receipt = env.call("shared_handover_receipt", "A08", {"task_id": "T-be", "digest": bundle["handover"]["digest"],
                                                          "understood": ["goal"]})
    assert receipt["state"] == "SUCCESS" and receipt["duplicate"] is False


def test_bundle_bound_is_enforced_at_default_style_settings(env, monkeypatch):
    """RV-B #2c/d: `room` was the whole handover cap, so a stored document never shrank and the bundle exceeded its bound
    (repro: truncated=True, bytes 12319 > cap 8192). The document now gets what the other sections leave."""
    store = make_world(env)
    monkeypatch.setenv("SWARM_HANDOVER_MAX_BYTES", "8192")
    for i in range(25):
        store.add_artifact("T-be", kind="file", uri=f"src/module_{i:02d}_" + "p" * 220 + ".py",
                           digest="sha256:" + f"{i:x}".rjust(64, "e"), producer="A05")
    written = env.call("shared_handover_write", "A05", handover_payload(
        completed=[f"done {i} " + "y" * 80 for i in range(30)],
        open_questions=[f"question {i} " + "q" * 260 for i in range(15)]))
    assert written["state"] == "SUCCESS" and 7000 < written["bytes"] <= 8192 and written["summarized"] is False
    bundle = env.call("shared_handover_bundle", "A08", {"task_id": "T-be"})
    assert bundle["state"] == "SUCCESS" and bundle["truncated"] is True
    assert bundle["bytes"] == len(env.ho.canonical(bundle)) <= 2 * 8192 and bundle["over_cap"] is False
    assert bundle["handover"]["document"]["remaining"] == ["Write the unit tests"]
    assert bundle["handover"]["digest"] == written["digest"]


def test_artifact_check_accepts_the_agent_as_a_slug(env):
    """RV-B #6: passing the slug "a05-backend" as `agent` marked every output producer_mismatch."""
    make_world(env)
    (env.root / "src").mkdir()
    (env.root / "src" / "export.py").write_text("print('x')\n", encoding="utf-8")
    digest = env.call("shared_artifact_digest", "A05", {"path": "src/export.py"})["digest"]
    outputs = [{"kind": "file", "uri": "src/export.py", "digest": digest, "producer": "A05"}]
    by_slug = env.call("shared_artifact_check", "A08", {"task_id": "T-be", "agent": "a05-backend", "outputs": outputs})
    assert by_slug["expected_producer"] == "A05" and by_slug["results"][0]["status"] == "ok" and by_slug["ok"] is True
    by_id = env.call("shared_artifact_check", "A08", {"task_id": "T-be", "agent": "A05", "outputs": outputs})
    assert by_id["results"][0]["status"] == "ok"
    wrong = env.call("shared_artifact_check", "A08", {"task_id": "T-be", "agent": "a09-reviewer", "outputs": outputs})
    assert wrong["results"][0]["status"] == "producer_mismatch"


def test_the_handover_write_keeps_token_shaped_task_ids(env):
    """RV-B #4 through the tool: a task id that looks like an API token is still a task id."""
    store = env.taskstore.TaskStore(root=env.root)
    store.create(task_id="sk-onboarding-flow-v2", correlation_id="corr-1", capability="be.code", agent_id="A05", title="t")
    out = env.call("shared_handover_write", "A05", handover_payload(task_id="sk-onboarding-flow-v2"))
    assert out["state"] == "SUCCESS" and out["handover_id"] == "sk-onboarding-flow-v2.1", out


def test_generated_contract_tests_run_in_an_isolated_swarm_dir(tmp_path):
    """RV-B #3: the generated tests used to set no SWARM_DIR, so `shared_handover_write` / `shared_log_emit` examples wrote
    into the real <repo>/.swarm (and into the active run's events.jsonl). The generator now emits an autouse fixture that
    gives every test its own SWARM_DIR; render a probe from the real generator output and run it with SWARM_DIR unset."""
    import subprocess
    import sys

    source = (ROOT / "tests" / "generated" / "test_tool_contracts.py").read_text(encoding="utf-8")
    assert "def _isolated_swarm_dir(" in source and "@pytest.fixture(autouse=True)" in source
    head = source.split("def test_unregistered_name_is_not_callable", 1)[0].replace(
        "from swarm.tool_registry import call_tool", "")
    (tmp_path / "test_probe.py").write_text(
        head + "\nimport os\nfrom pathlib import Path\n\n"
        "def test_probe(tmp_path):\n"
        "    assert Path(os.environ['SWARM_DIR']).parent == tmp_path\n"
        "    assert Path(os.environ['SWARM_DIR']) != Path.cwd() / '.swarm'\n", encoding="utf-8")
    env = {k: v for k, v in __import__("os").environ.items() if k != "SWARM_DIR"}
    proc = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-p", "no:randomly", str(tmp_path / "test_probe.py")],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ---- greploop: PR #37 findings ----------------------------------------------------------------------------------
def _two_receivers(env):
    make_world(env)
    to_docs = env.call("shared_handover_write", "A05", handover_payload(to_agent="A15", goal="Bundle the release docs"))
    to_release = env.call("shared_handover_write", "A05", handover_payload(to_agent="A14", goal="Record the release debt"))
    assert to_docs["state"] == to_release["state"] == "SUCCESS" and to_docs["digest"] != to_release["digest"]
    return to_docs, to_release


def test_each_receiver_reads_the_handover_addressed_to_it_not_the_latest_written(env):
    """Greptile #4: the release-record skill writes one handover to A15 and one to A14 under the same task id; after the
    second write A15's read, verify and bundle returned A14's handover and A15 never saw its own."""
    docs, release = _two_receivers(env)
    for caller, mine in (("A15", docs), ("A14", release)):
        read = env.call("shared_handover_read", caller, {"task_id": "T-be"})
        assert read["handover"]["to_agent"] == caller and read["handover"]["digest"] == mine["digest"], caller
        assert "context_only" not in read
        verify = env.call("shared_handover_verify", caller, {"task_id": "T-be"})
        assert verify["handover_id"] == mine["handover_id"] and "context_only" not in verify
        bundle = env.call("shared_handover_bundle", caller, {"task_id": "T-be"})
        assert bundle["handover"]["digest"] == mine["digest"] and bundle["goal"] == read["handover"]["goal"]
        assert bundle["handover"]["document"]["to_agent"] == caller and "context_only" not in bundle["handover"]
        receipt = env.call("shared_handover_receipt", caller, {"task_id": "T-be", "digest": bundle["handover"]["digest"]})
        assert receipt["state"] == "SUCCESS" and receipt["handover_id"] == mine["handover_id"], receipt


def test_a_caller_without_its_own_handover_gets_the_latest_as_context(env):
    docs, release = _two_receivers(env)
    read = env.call("shared_handover_read", "A08", {"task_id": "T-be"})
    assert read["context_only"] is True and read["handover"]["digest"] == release["digest"]
    assert env.call("shared_handover_verify", "A08", {"task_id": "T-be"})["context_only"] is True
    assert env.call("shared_handover_bundle", "A08", {"task_id": "T-be"})["handover"]["context_only"] is True
    refused = env.call("shared_handover_receipt", "A08", {"task_id": "T-be", "digest": release["digest"]})
    assert refused["state"] == "INVALID_INPUT" and "addressed to A14, not to A08" in refused["message"]


def test_artifact_check_judges_the_current_artifact_not_the_superseded_rows(env):
    """Greptile #3: apply_result appends a row per version, so after a rework the old digest stayed next to the new one and
    was reported as digest_mismatch although the file matched the latest row."""
    store = make_world(env)
    (env.root / "src").mkdir()
    target = env.root / "src" / "export.py"
    target.write_text("v1\n", encoding="utf-8")
    old = env.call("shared_artifact_digest", "A05", {"path": "src/export.py"})["digest"]
    store.add_artifact("T-be", kind="file", uri="src/export.py", digest=old, producer="A05")
    target.write_text("v2 after rework\n", encoding="utf-8")
    new = env.call("shared_artifact_digest", "A05", {"path": "src/export.py"})["digest"]
    time.sleep(0.01)
    store.add_artifact("T-be", kind="file", uri="src/export.py", digest=new, producer="A05")
    env.call("shared_handover_write", "A05", handover_payload(artifacts=[
        {"kind": "file", "uri": "src/export.py", "digest": new, "producer": "A05"}]))
    out = env.call("shared_artifact_check", "A08", {"task_id": "T-be"})
    stale_row = "sha256:" + "b" * 64  # make_world's own row, older than both
    assert [(r["uri"], r["status"]) for r in out["results"]] == [("src/export.py", "ok")], out
    assert out["ok"] is True and out["verified"] is True and out["failed"] == 0
    assert any("superseded" in n and old in n and stale_row in n for n in out["notes"]), out["notes"]
    # an explicit outputs list is checked as given: the caller asked about those exact digests
    explicit = env.call("shared_artifact_check", "A08", {"task_id": "T-be", "outputs": [
        {"kind": "file", "uri": "src/export.py", "digest": old, "producer": "A05"}]})
    assert explicit["results"][0]["status"] == "digest_mismatch"


def test_a_file_that_changed_after_the_latest_row_still_fails(env):
    store = make_world(env)
    (env.root / "src").mkdir()
    (env.root / "src" / "export.py").write_text("v1\n", encoding="utf-8")
    digest = env.call("shared_artifact_digest", "A05", {"path": "src/export.py"})["digest"]
    store.add_artifact("T-be", kind="file", uri="src/export.py", digest=digest, producer="A05")
    (env.root / "src" / "export.py").write_text("edited later\n", encoding="utf-8")
    out = env.call("shared_artifact_check", "A08", {"task_id": "T-be"})
    assert out["results"][0]["status"] == "digest_mismatch" and out["ok"] is False and out["verified"] is False


def test_dod_check_refuses_more_criteria_than_it_checks(env):
    """Greptile #10: criteria past the 100th were dropped, then `total` and `all_met` were computed on the subset."""
    result = {"summary_md": "The export endpoint returns CSV"}
    criteria = ["The export endpoint returns CSV"] * 100 + ["Never judged criterion without any evidence"]
    refused = env.call("shared_dod_check", "A08", {"criteria": criteria, "result": result})
    assert refused["state"] == "INVALID_INPUT" and refused["field"] == "criteria" and "101" in refused["message"]
    full = env.call("shared_dod_check", "A08", {"criteria": criteria[:100], "result": result})
    assert full["state"] == "SUCCESS" and full["total"] == 100 and full["all_met"] is True


def test_artifact_check_refuses_more_outputs_than_it_checks(env):
    (env.root / "src").mkdir()
    (env.root / "src" / "a.py").write_text("a\n", encoding="utf-8")
    digest = env.call("shared_artifact_digest", "A05", {"path": "src/a.py"})["digest"]
    good = {"kind": "file", "uri": "src/a.py", "digest": digest, "producer": "A05"}
    bad = {"kind": "file", "uri": "src/missing.py", "digest": digest, "producer": "A05"}
    refused = env.call("shared_artifact_check", "A08", {"task_id": "T-be", "agent": "A05", "outputs": [good] * 100 + [bad]})
    assert refused["state"] == "INVALID_INPUT" and refused["field"] == "outputs" and "101" in refused["message"]
    full = env.call("shared_artifact_check", "A08", {"task_id": "T-be", "agent": "A05", "outputs": [good] * 100})
    assert full["state"] == "SUCCESS" and full["checked"] == 100 and full["verified"] is True


# ---- greploop round 2: PR #37 findings --------------------------------------------------------------------------
def _resumed_task(env):
    """A01 left a checkpoint addressed to A05 under T-be; A05 resumed the task and hands its own work to A08."""
    make_world(env)
    incoming = env.call("shared_handover_write", "A01", handover_payload(to_agent="A05", goal="Resume the export endpoint"))
    outgoing = env.call("shared_handover_write", "A05", handover_payload(to_agent="A08", goal="Test the export endpoint"))
    assert incoming["state"] == outgoing["state"] == "SUCCESS" and outgoing["seq"] == incoming["seq"] + 1
    return incoming, outgoing


def test_the_producers_read_back_is_pinned_to_the_seq_it_wrote(env):
    """Greptile P1: a producer on a resumed task read back the checkpoint addressed to itself, not the handover just written."""
    incoming, outgoing = _resumed_task(env)
    unpinned = env.call("shared_handover_read", "A05", {"task_id": "T-be"})
    assert unpinned["handover"]["handover_id"] == incoming["handover_id"]  # the receiver view: what is addressed to A05
    back = env.call("shared_handover_read", "A05", {"task_id": "T-be", "seq": outgoing["seq"]})
    assert back["status"] == "ok" and back["handover"]["handover_id"] == outgoing["handover_id"]
    assert back["handover"]["digest"] == outgoing["digest"] and back["handover"]["to_agent"] == "A08"
    assert back["context_only"] is True  # addressed to the next agent, not to the producer: no receipt for the producer
    verify = env.call("shared_handover_verify", "A05", {"task_id": "T-be", "seq": outgoing["seq"]})
    assert verify["handover_id"] == outgoing["handover_id"]
    bundle = env.call("shared_handover_bundle", "A05", {"task_id": "T-be", "seq": outgoing["seq"]})
    assert bundle["handover"]["digest"] == outgoing["digest"] and bundle["goal"] == "Test the export endpoint"
    older = env.call("shared_handover_bundle", "A05", {"task_id": "T-be", "seq": incoming["seq"]})
    assert older["handover"]["digest"] == incoming["digest"] and "context_only" not in older["handover"]


def test_a_seq_the_task_does_not_have_is_missing_for_every_handover_reader(env):
    _resumed_task(env)
    for tool in ("shared_handover_read", "shared_handover_verify", "shared_handover_bundle", "shared_artifact_check"):
        absent = env.call(tool, "A05", {"task_id": "T-be", "seq": 99})
        assert absent["state"] == "SUCCESS", (tool, absent)
        if tool == "shared_handover_read":
            assert absent["found"] is False and absent["status"] == "missing" and absent["handover"] == {}
        elif tool == "shared_handover_verify":
            assert absent["status"] == "missing" and absent["ready"] is False and absent["handover_id"] == ""
        elif tool == "shared_handover_bundle":
            assert absent["handover"]["status"] == "missing" and absent["handover"]["document"] == {}
        else:
            assert any("no handover with seq 99" in n for n in absent["notes"]), absent  # only the stored rows, no other handover's artifacts
            assert not [r for r in absent["results"] if "handover" in str(r.get("inherited"))], absent
        for bad in (0, -1, "2", True, 1.5):
            refused = env.call(tool, "A05", {"task_id": "T-be", "seq": bad})
            assert refused["state"] == "INVALID_INPUT" and refused["field"] == "seq", (tool, bad, refused)


def _digest_of(env, name, text):
    (env.root / "src").mkdir(exist_ok=True)
    (env.root / "src" / name).write_text(text, encoding="utf-8")
    return env.call("shared_artifact_digest", "A05", {"path": f"src/{name}"})["digest"]


def test_a_result_without_a_digest_keeps_the_digest_the_handover_supplied(env):
    """Greptile P2: the emit skill writes the handover before the final task.result, whose outputs may omit `digest`; the
    newer digest-less row replaced the handover's digest (`no_digest`) and later edits went unnoticed."""
    store = make_world(env)
    digest = _digest_of(env, "out.py", "v1\n")
    env.call("shared_handover_write", "A05", handover_payload(artifacts=[
        {"kind": "file", "uri": "src/out.py", "digest": digest, "producer": "A05"}]))
    time.sleep(0.02)
    store.add_artifact("T-be", kind="file", uri="src/out.py", digest="", producer="A05")  # task.result output without a digest
    out = env.call("shared_artifact_check", "A08", {"task_id": "T-be"})
    (row,) = [r for r in out["results"] if r["uri"] == "src/out.py"]
    assert row["status"] == "ok" and row["digest_match"] == "yes" and row["actual_digest"] == digest
    assert row["inherited"] == {"digest": digest, "source": "handover T-be.1"}
    assert not [r for r in out["results"] if r["uri"] == "src/out.py" and r["status"] == "no_digest"]
    (env.root / "src" / "out.py").write_text("v1 edited after the handover\n", encoding="utf-8")
    changed = env.call("shared_artifact_check", "A08", {"task_id": "T-be"})
    (row,) = [r for r in changed["results"] if r["uri"] == "src/out.py"]
    assert row["status"] == "digest_mismatch" and digest in row["reason"] and changed["ok"] is False


def test_a_digestless_row_keeps_the_newest_known_digest_and_lists_the_older_one_as_superseded(env):
    store = make_world(env)
    old = _digest_of(env, "out.py", "v1\n")
    env.call("shared_handover_write", "A05", handover_payload(artifacts=[
        {"kind": "file", "uri": "src/out.py", "digest": old, "producer": "A05"}]))
    time.sleep(0.02)
    new = _digest_of(env, "out.py", "v2 after rework\n")
    store.add_artifact("T-be", kind="file", uri="src/out.py", digest=new, producer="A05")
    time.sleep(0.02)
    store.add_artifact("T-be", kind="file", uri="src/out.py", digest="", producer="A05")
    out = env.call("shared_artifact_check", "A08", {"task_id": "T-be"})
    (row,) = [r for r in out["results"] if r["uri"] == "src/out.py"]
    assert row["status"] == "ok" and row["inherited"] == {"digest": new, "source": "stored result row"}
    assert any("superseded" in n and old in n and new not in n for n in out["notes"]), out["notes"]
    (env.root / "src" / "out.py").write_text("v1\n", encoding="utf-8")  # back to a superseded digest: the newest one wins
    reverted = env.call("shared_artifact_check", "A08", {"task_id": "T-be"})
    (row,) = [r for r in reverted["results"] if r["uri"] == "src/out.py"]
    assert row["status"] == "digest_mismatch"


def test_artifact_check_with_a_seq_reads_the_artifacts_of_that_handover(env):
    _incoming, outgoing = _resumed_task(env)  # both handovers declare src/export.py with the b* digest
    first_digest = _digest_of(env, "first.py", "first\n")
    env.call("shared_handover_write", "A01", handover_payload(to_agent="A05", goal="Resume again", artifacts=[
        {"kind": "file", "uri": "src/first.py", "digest": first_digest, "producer": "A01"}]))
    pinned = env.call("shared_artifact_check", "A05", {"task_id": "T-be", "seq": outgoing["seq"]})
    assert "src/first.py" not in [r["uri"] for r in pinned["results"]]
    default = env.call("shared_artifact_check", "A05", {"task_id": "T-be"})  # A05's newest incoming one
    assert "src/first.py" in [r["uri"] for r in default["results"]]


def test_the_bundle_lists_one_entry_for_a_file_the_result_repeats_without_a_digest(env):
    store = make_world(env)
    digest = _digest_of(env, "out.py", "v1\n")
    env.call("shared_handover_write", "A05", handover_payload(artifacts=[
        {"kind": "file", "uri": "src/out.py", "digest": digest, "producer": "A05"}]))
    store.add_artifact("T-be", kind="file", uri="src/out.py", digest="", producer="A05")
    bundle = env.call("shared_handover_bundle", "A08", {"task_id": "T-be"})
    assert [a["digest"] for a in bundle["artifacts"] if a["uri"] == "src/out.py"] == [digest]


def test_open_questions_of_a_task_with_two_receivers_are_those_of_the_handover_addressed_to_the_caller(env):
    make_world(env)
    env.call("shared_handover_write", "A05", handover_payload(to_agent="A15", open_questions=["Docs question?"]))
    env.call("shared_handover_write", "A05", handover_payload(to_agent="A14", goal="Record the debt", open_questions=["Debt question?"]))
    mine = [q["question"] for q in env.call("shared_open_questions", "A15", {"task_id": "T-be"})["questions"] if q["origin"] == "handover"]
    assert mine == ["Docs question?"]


# ---- greploop round 3: PR #37 findings --------------------------------------------------------------------------
CONSUMERS = ("A05", "A06")
ACK = "ack contract-api 2.0.0"


def _breaking_change_reply_scan(env, caller, task_id, consumers=CONSUMERS, ack=ACK):
    """Step 8 of `a03-architect--breaking-change`, as the skill words it: read the newest handover addressed to the
    caller (none: no reply); its `handover_id` ends in `.<N>`; read `seq` = 1..N, a `missing` below N is a gap and is
    skipped, an entry that is no document (`found` false) is skipped; count a reply whose `from_agent` is a consumer and
    whose `decisions` hold the ack. A first read that is `malformed` has no id: stop after three consecutive `missing`.
    Returns (the acks, the last seq read; 0 when nothing is addressed to the caller)."""
    first = env.call("shared_handover_read", caller, {"task_id": task_id})
    assert first["state"] == "SUCCESS", first
    if first["status"] == "missing" or first.get("context_only"):
        return {}, 0
    last = int(first["handover"]["handover_id"].rsplit(".", 1)[1]) if first["handover"] else None
    acks, seq, run = {}, 0, 0
    while last is None or seq < last:
        seq += 1
        assert seq < 50, "the scan did not end"
        read = env.call("shared_handover_read", caller, {"task_id": task_id, "seq": seq})
        assert read["state"] == "SUCCESS", (seq, read)  # a gap and the end of the history are never errors
        run = run + 1 if read["status"] == "missing" else 0
        if last is None and run == 3:
            return acks, seq
        if read["found"]:
            doc = read["handover"]
            if doc["from_agent"] in consumers and any(ack in d["decision"] for d in doc["decisions"]):
                acks[doc["from_agent"]] = "2.0.0"
    return acks, seq


def _architect_asks(env):
    make_world(env)
    for consumer in CONSUMERS:  # step 7: one handover per consumer
        env.call("shared_handover_write", "A03", handover_payload(to_agent=consumer, goal="Acknowledge the breaking change",
                                                                    open_questions=[ACK], artifacts=[]))


def _consumer_replies(env, consumer, **over):
    body = handover_payload(to_agent="A03", goal=f"{consumer} acknowledges", artifacts=[], open_questions=[],
                            decisions=[{"decision": ACK, "rationale": f"{consumer} migrated"}])
    body.update(over)
    return env.call("shared_handover_write", consumer, body)


def test_the_breaking_change_reply_scan_ends_cleanly_when_every_consumer_replied(env):
    _architect_asks(env)
    for consumer in CONSUMERS:
        assert _consumer_replies(env, consumer)["state"] == "SUCCESS"
    acks, end = _breaking_change_reply_scan(env, "A03", "T-be")
    assert acks == {"A05": "2.0.0", "A06": "2.0.0"} and end == 4  # seq 1-2 asks, 3-4 replies: 4 is the newest addressed to A03


def test_the_breaking_change_reply_scan_ends_cleanly_when_no_consumer_replied(env):
    _architect_asks(env)
    assert _breaking_change_reply_scan(env, "A03", "T-be") == ({}, 0)  # the newest handover is addressed to a consumer
    assert _breaking_change_reply_scan(env, "A03", "T-none") == ({}, 0)  # a task that has no handover at all


def test_the_breaking_change_reply_scan_goes_past_a_handover_that_is_no_document(env):
    _architect_asks(env)
    _consumer_replies(env, "A05")
    (env.sdir / "handovers" / "corr-1" / "T-be.3.json").write_text("{", encoding="utf-8")  # a damaged reply in the middle
    _consumer_replies(env, "A06")
    assert env.call("shared_handover_read", "A03", {"task_id": "T-be", "seq": 3})["status"] == "malformed"
    acks, end = _breaking_change_reply_scan(env, "A03", "T-be")
    assert acks == {"A06": "2.0.0"} and end == 4


def test_the_breaking_change_reply_scan_reads_past_a_gap_in_the_history(env):
    """Round 5: a history that has a gap (older writers left empty reservations, a restored backup lost a file) used to
    end the scan at the first `missing` seq, so a saved acknowledgement behind the gap was reported absent."""
    _architect_asks(env)
    _consumer_replies(env, "A05")
    _consumer_replies(env, "A06")
    gone = env.sdir / "handovers" / "corr-1" / "T-be.3.json"
    gone.unlink()  # seq 3 is gone: A05's reply is lost, A06's reply (seq 4) is behind the gap
    assert env.call("shared_handover_read", "A03", {"task_id": "T-be", "seq": 3})["status"] == "missing"
    acks, end = _breaking_change_reply_scan(env, "A03", "T-be")
    assert acks == {"A06": "2.0.0"} and end == 4
    seq = 1  # the old procedure (stop at the first `missing`) never reaches seq 4
    while env.call("shared_handover_read", "A03", {"task_id": "T-be", "seq": seq})["status"] != "missing":
        seq += 1
    assert seq == 3


def test_writers_that_die_never_create_a_gap_for_the_reply_scan(env, monkeypatch):
    _architect_asks(env)

    def boom(*_a, **_k):
        raise RuntimeError("writer died after its temp write")

    with monkeypatch.context() as m:
        m.setattr(env.ho.os, "link", boom)
        try:
            died = _consumer_replies(env, "A05")
        except RuntimeError:
            died = {"state": "FAILED"}
    assert died["state"] != "SUCCESS"
    _consumer_replies(env, "A06")
    acks, end = _breaking_change_reply_scan(env, "A03", "T-be")
    assert acks == {"A06": "2.0.0"} and end == 3  # the surviving reply took seq 3: no gap
    assert not list((env.sdir / "handovers" / "corr-1").glob("T-be.4.json"))


def test_handover_readers_agree_with_a_reference_model_on_random_histories(env):
    """Differential audit: random histories (valid handovers by random writers, damaged files, gaps) against a model of
    the contract: `seq` n is `missing` iff no readable file is named `<task>.<n>.json`, else `malformed` or a document of
    exactly that file; read, verify and bundle report the same status and handover id."""
    import random

    make_world(env)
    folder = env.sdir / "handovers" / "corr-1"
    for trial in range(25):
        rng = random.Random(trial)
        task = f"T-rand{trial}"
        env.taskstore.TaskStore(root=env.root).create(task_id=task, correlation_id="corr-1", capability="be.code", agent_id="A05", title="r")
        model: dict[int, tuple[str, str]] = {}
        for _ in range(rng.randint(0, 6)):
            writer, target = rng.sample(ALL_AGENTS, 2)
            out = env.call("shared_handover_write", writer, handover_payload(task_id=task, to_agent=target, artifacts=[], goal=f"g{rng.random()}"))
            assert out["state"] == "SUCCESS", out
            model[out["seq"]] = (writer, out["handover_id"])
        for seq in list(model):
            if rng.random() < 0.25:
                (folder / f"{task}.{seq}.json").write_text("{", encoding="utf-8")
                model[seq] = ("", "")
            elif rng.random() < 0.2:
                (folder / f"{task}.{seq}.json").unlink()
                del model[seq]
        top = max(model, default=0)
        for seq in range(1, top + 4):
            read = env.call("shared_handover_read", "A01", {"task_id": task, "seq": seq})
            verify = env.call("shared_handover_verify", "A01", {"task_id": task, "seq": seq})
            bundle = env.call("shared_handover_bundle", "A01", {"task_id": task, "seq": seq})
            assert read["state"] == verify["state"] == bundle["state"] == "SUCCESS", (trial, seq)
            if seq not in model:
                assert (read["status"], read["found"]) == ("missing", False), (trial, seq, read)
                assert verify["status"] == bundle["handover"]["status"] == "missing" and verify["ready"] is False
            elif model[seq][0] == "":
                assert (read["status"], read["found"]) == ("malformed", False), (trial, seq, read)
                assert verify["status"] == bundle["handover"]["status"] == "malformed"
            else:
                assert read["found"] is True and read["handover"]["handover_id"] == model[seq][1], (trial, seq, read)
                assert read["handover"]["from_agent"] == model[seq][0]
                assert verify["handover_id"] == bundle["handover"]["handover_id"] == model[seq][1]
                assert verify["status"] == read["status"] == bundle["handover"]["status"]


def test_unpinned_read_follows_the_addressing_rule_of_a_reference_model_on_random_histories(env):
    """Differential audit of the selection order without `seq`: newest handover addressed to the caller, else the newest as
    context; a damaged file met on the way (the newest, or newer than the addressed one) makes the read `malformed`."""
    import random

    make_world(env)
    folder = env.sdir / "handovers" / "corr-1"
    for trial in range(30):
        rng = random.Random(1000 + trial)
        task = f"T-sel{trial}"
        env.taskstore.TaskStore(root=env.root).create(task_id=task, correlation_id="corr-1", capability="be.code", agent_id="A05", title="r")
        docs: list[tuple[int, str, str, str]] = []  # seq, from, to, id ("" when damaged)
        for _ in range(rng.randint(1, 6)):
            writer, target = rng.sample(["A01", "A05", "A06", "A08"], 2)
            out = env.call("shared_handover_write", writer, handover_payload(task_id=task, to_agent=target, artifacts=[], goal=f"g{rng.random()}"))
            docs.append((out["seq"], writer, target, out["handover_id"]))
        if rng.random() < 0.4:
            victim = rng.randrange(len(docs))
            (folder / f"{task}.{docs[victim][0]}.json").write_text("{", encoding="utf-8")
            docs[victim] = (docs[victim][0], "", "", "")
        for caller in ("A01", "A05", "A06", "A08", "A12"):
            read = env.call("shared_handover_read", caller, {"task_id": task})
            newest = docs[-1]
            if not newest[3]:
                expect, status, context = None, "malformed", False
            elif newest[2] == caller:
                expect, status, context = newest, "ok", False
            else:
                expect, status, context = newest, "ok", True
                for cand in reversed(docs[:-1]):
                    if not cand[3]:
                        expect, status = None, "malformed"
                        break
                    if cand[2] == caller:
                        expect, context = cand, False
                        break
            assert read["status"] == status, (trial, caller, read, docs)
            if expect is None:
                assert read["found"] is False
            else:
                assert read["handover"]["handover_id"] == expect[3], (trial, caller, docs)
                assert read.get("context_only", False) is context, (trial, caller, docs)


def test_a_pinned_seq_names_the_plans_handover_when_a_stale_correlation_reuses_the_number(env):
    make_world(env)
    old = env.sdir / "handovers" / "corr-old"
    old.mkdir(parents=True)
    env.call("shared_handover_write", "A05", handover_payload())  # corr-1 (the plan), seq 1
    (old / "T-be.1.json").write_text((env.sdir / "handovers" / "corr-1" / "T-be.1.json").read_text().replace('"corr-1"', '"corr-old"'), encoding="utf-8")
    pinned = env.call("shared_handover_read", "A08", {"task_id": "T-be", "seq": 1})
    assert pinned["status"] == "ok" and pinned["handover"]["correlation_id"] == "corr-1"
    assert env.call("shared_handover_read", "A08", {"task_id": "T-be", "seq": 2})["status"] == "missing"


# ---- RV-I review fixes ------------------------------------------------------------------------------------------
def test_registry_references_are_unchecked_never_missing(env):
    """RV-I P2-2: `ghcr.io/org/app@sha256:...` has a `/` before its first `:`, so it was read as a workspace path and
    reported `missing` (failed 1, ok false) although the skills promise `unchecked` for non-file uris."""
    (env.root / "v1.2").mkdir()
    (env.root / "v1.2" / "app.js").write_text("x\n", encoding="utf-8")
    refs = ["ghcr.io/org/app@sha256:" + "a" * 64, "registry.example.com/app:1.2", "docker.io/library/nginx",
            "org/app:1.2", "localhost:5000/app:1", "oci://reg/app"]
    outputs = [{"kind": "image", "uri": uri, "digest": "sha256:" + "a" * 64, "producer": "A11"} for uri in refs]
    outputs.append({"kind": "file", "uri": "v1.2/app.js", "digest": "", "producer": "A11"})  # a dotted workspace directory is a file
    outputs.append({"kind": "file", "uri": "./org/app:1.2", "digest": "", "producer": "A11"})  # ./ always names a file
    result = env.call("shared_artifact_check", "A12", {"task_id": "T-build", "agent": "A11", "outputs": outputs})
    statuses = [r["status"] for r in result["results"]]
    assert statuses == ["unchecked"] * len(refs) + ["no_digest", "missing"], result["results"]
    assert all(r["exists"] == "unchecked" and "not a local file" in r["reason"] for r in result["results"][:len(refs)])
    assert result["failed"] == 1 and result["unchecked"] == len(refs) + 1


def _check_one(env, uri, kind):
    out = {"kind": kind, "uri": uri, "digest": "sha256:" + "a" * 64, "producer": "A11"}
    return env.call("shared_artifact_check", "A12", {"task_id": "T-build", "agent": "A11", "outputs": [out]})["results"][0]


@pytest.mark.parametrize("uri", ["v1.2/app.js", "ghcr.io/x/y.md", "docker.io/org/app:1.2", "org/app:1.2", "app:1.2",
                                 "localhost:5000/app", "docs.v2/a.md"])
def test_an_absent_local_file_is_missing_not_unchecked(env, uri):
    """A dotted first directory or a colon in the last segment is not a registry reference: without a URL scheme, a digest
    pin or a reference kind the uri is a workspace path, and an absent file stops intake."""
    one = _check_one(env, uri, "file")
    assert (one["status"], one["exists"]) == ("missing", "no"), one
    result = env.call("shared_artifact_check", "A12", {"task_id": "T-build", "agent": "A11", "outputs": [
        {"kind": "file", "uri": uri, "digest": "sha256:" + "a" * 64, "producer": "A11"}]})
    assert result["ok"] is False and result["failed"] == 1 and result["unchecked"] == 0


@pytest.mark.parametrize("uri,kind", [
    ("ghcr.io/org/app@sha256:" + "a" * 64, "file"), ("ghcr.io/org/app@sha512:" + "a" * 128, "file"),
    ("docker.io/org/app:1.2", "image"), ("docker.io/org/app:1.2", "Container"), ("pkg/lib:1.0", "package"),
    ("reg.io/x/y", "registry"), ("example.com/page", "url"), ("example.com/page", "link"),
    ("https://example.com/a.js", "file"), ("oci://reg/app", "file")])
def test_only_declared_reference_forms_are_unchecked(env, uri, kind):
    one = _check_one(env, uri, kind)
    assert (one["status"], one["exists"]) == ("unchecked", "unchecked") and "not a local file" in one["reason"], one


@pytest.mark.parametrize("uri", ["ghcr.io/org/app:1.2", "ghcr.io/org/app", "registry.example.com:5000/team/app:1",
                                 "localhost/app:1", "localhost:5000/app", "10.0.0.5:5000/app:2", "docker.io/library/nginx"])
def test_a_build_artifact_image_reference_is_unchecked(env, uri):
    """Round 5: `devops_build_record.py --image ghcr.io/org/app:1.2` saves the image under kind `build.artifact`; it was read
    as a workspace path and came back `missing`, which stops the shared intake skill."""
    for kind in ("build.artifact", "Build.Artifact", "provenance.attestation"):
        one = _check_one(env, uri, kind)
        assert (one["status"], one["exists"]) == ("unchecked", "unchecked") and "not a local file" in one["reason"], (kind, one)
    for kind in ("code", "file", "code.patch", "test.results"):  # every other kind keeps path semantics
        one = _check_one(env, uri, kind)
        assert (one["status"], one["exists"]) == ("missing", "no"), (kind, uri, one)


def test_a_build_artifact_path_is_checked_like_any_file(env):
    (env.root / "dist").mkdir()
    for uri in ("dist/app.tar", "v1.2/app.js", "org/app:1.2", "app:1.2"):  # no registry host: a path
        one = _check_one(env, uri, "build.artifact")
        assert (one["status"], one["exists"]) == ("missing", "no"), (uri, one)
    (env.root / "dist" / "app.tar").write_bytes(b"tar\n")
    digest = "sha256:" + hashlib.sha256(b"tar\n").hexdigest()
    out = {"kind": "build.artifact", "uri": "dist/app.tar", "digest": digest, "producer": "A11"}
    one = env.call("shared_artifact_check", "A12", {"task_id": "T-build", "agent": "A11", "outputs": [out]})["results"][0]
    assert (one["status"], one["exists"]) == ("ok", "yes"), one


def test_a_local_file_named_like_an_image_wins_for_a_build_artifact(env):
    (env.root / "ghcr.io" / "org").mkdir(parents=True)
    (env.root / "ghcr.io" / "org" / "app:1.2").write_bytes(b"x\n")
    digest = "sha256:" + hashlib.sha256(b"x\n").hexdigest()
    out = {"kind": "build.artifact", "uri": "ghcr.io/org/app:1.2", "digest": digest, "producer": "A11"}
    one = env.call("shared_artifact_check", "A12", {"task_id": "T-build", "agent": "A11", "outputs": [out]})["results"][0]
    assert (one["status"], one["exists"]) == ("ok", "yes"), one


def test_an_existing_local_file_wins_over_every_reference_form(env):
    (env.root / "ghcr.io" / "x").mkdir(parents=True)
    (env.root / "ghcr.io" / "x" / "y.md").write_text("doc\n", encoding="utf-8")
    digest = "sha256:" + hashlib.sha256(b"doc\n").hexdigest()
    for kind in ("file", "image", "url"):
        out = {"kind": kind, "uri": "ghcr.io/x/y.md", "digest": digest, "producer": "A11"}
        one = env.call("shared_artifact_check", "A12", {"task_id": "T-build", "agent": "A11", "outputs": [out]})["results"][0]
        assert (one["status"], one["exists"]) == ("ok", "yes"), (kind, one)


def test_one_unreadable_file_does_not_abort_the_check(env, monkeypatch):
    """RV-I P3-1: an OSError while digesting one output escaped as DEPENDENCY_UNAVAILABLE and lost every other result."""
    (env.root / "ok.py").write_text("print(1)\n", encoding="utf-8")
    (env.root / "locked.py").write_text("print(2)\n", encoding="utf-8")
    (env.root / "gone.py").write_text("print(3)\n", encoding="utf-8")
    real = env.sh._file_digest

    def flaky(path):
        if path.name == "locked.py":
            raise PermissionError(13, "Permission denied", str(path))
        if path.name == "gone.py":
            raise FileNotFoundError(2, "No such file", str(path))
        return real(path)

    monkeypatch.setattr(env.sh, "_file_digest", flaky)
    digest = "sha256:" + hashlib.sha256(b"print(1)\n").hexdigest()
    outputs = [{"kind": "file", "uri": name, "digest": digest, "producer": "A05"} for name in ("ok.py", "locked.py", "gone.py")]
    result = env.call("shared_artifact_check", "A08", {"task_id": "T-be", "agent": "A05", "outputs": outputs})
    assert result["state"] == "SUCCESS", result
    ok_row, locked, gone = result["results"]
    assert ok_row["status"] == "ok"
    assert locked["status"] == "missing" and locked["reason"] == "unreadable: Permission denied" and "locked.py" not in locked["reason"]
    assert gone["status"] == "missing" and gone["exists"] == "no"
    assert result["failed"] == 2 and result["ok"] is False


def test_artifact_check_reports_a_nul_uri_as_unchecked_and_keeps_other_outputs(env):
    """SW-03: Path.resolve() raises ValueError on a NUL byte inside _local_path, which no guard catches. One bad uri failed
    the whole check and the good output beside it was lost."""
    (env.root / "ok.py").write_text("print(1)\n", encoding="utf-8")
    digest = "sha256:" + hashlib.sha256(b"print(1)\n").hexdigest()
    outputs = [{"kind": "file", "uri": "out\x00.txt", "producer": "A05"},
               {"kind": "file", "uri": "ok.py", "digest": digest, "producer": "A05"}]
    result = env.call("shared_artifact_check", "A08", {"task_id": "T-be", "agent": "A05", "outputs": outputs})
    assert result["state"] == "SUCCESS", result
    bad, good = result["results"]
    assert bad["status"] == "unchecked" and good["status"] == "ok"


def test_validate_sizes_a_draft_with_the_id_the_write_will_use(env, monkeypatch):
    """RV-I P3-2: the pre-check used `pending.999999`, so a document near the cap of a long task id was declared fine and
    then summarized by the write (which sizes with `<task_id>.999999`)."""
    task_id = "T" + "a" * 119
    doc = {**handover_payload(task_id=task_id, completed=[f"step {i}: " + "x" * 300 for i in range(3)]),
           "correlation_id": "corr-size", "from_agent": "A05", "blockers": [],
           "created_wall": 1_760_000_000.5}  # fixed: the float's printed length would move the size by a byte
    first = env.call("shared_handover_validate", "A05", {"document": doc, "draft": True})
    assert first["valid"] is True and first["would_summarize"] is False
    short = env.ho.stored_size({**env.ho._prepare(doc, env.sh.SYSTEM_CLOCK), "handover_id": "pending.999999"})
    real = first["bytes"]
    assert short < real
    monkeypatch.setenv("SWARM_HANDOVER_MAX_BYTES", str(real - 1))  # fits with the short id, not with the real one
    assert short <= real - 1
    second = env.call("shared_handover_validate", "A05", {"document": doc, "draft": True})
    assert second["bytes"] == real and second["would_summarize"] is True
    written = env.ho.write_handover(doc, root=env.root, emit=False)
    assert written["summarized"] is True


def test_bundle_lists_each_artifact_once_with_its_current_digest(env):
    """RV-I P3-5a: after a rework the bundle returned the same uri twice (the old stored digest and the new one)."""
    store = make_world(env)
    store.add_artifact("T-be", kind="file", uri="src/export.py", digest="sha256:" + "c" * 64, producer="A05")
    bundle = env.call("shared_handover_bundle", "A05", {"task_id": "T-be"})
    mine = [a for a in bundle["artifacts"] if a["uri"] == "src/export.py"]
    assert [a["digest"] for a in mine] == ["sha256:" + "c" * 64], bundle["artifacts"]
    assert bundle["truncated"] is False


def test_bundle_says_when_it_dropped_artifacts(env):
    """RV-I P3-5b: 30 handover artifacts and one upstream artifact came back as 25 entries with `truncated: false`."""
    store = make_world(env)
    for i in range(30):
        store.add_artifact("T-be", kind="file", uri=f"src/part{i:02d}.py", digest="sha256:" + f"{i:02x}" * 32, producer="A05")
    bundle = env.call("shared_handover_bundle", "A05", {"task_id": "T-be"})
    assert len(bundle["artifacts"]) == 25 and bundle["truncated"] is True


# --- PY-09: the assignment's handover section must not hide faults (P3-03, P3-20) -----------------------------------------
@pytest.fixture()
def swarm_run(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_DIR", str(tmp_path / ".swarm"))
    spec = importlib.util.spec_from_file_location("swarm_run_handover_under_test", ROOT / "scripts" / "swarm_run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_handover_section_reraises_inner_import_error(swarm_run, monkeypatch, tmp_path):
    real = importlib.import_module

    def inner_dependency_missing(name, package=None):
        if name == "swarm.handover":  # the module is there, but one of its own imports is not
            raise ModuleNotFoundError("No module named 'swarm.missing_dep'", name="swarm.missing_dep")
        return real(name, package)

    monkeypatch.setattr(swarm_run.importlib, "import_module", inner_dependency_missing)
    task = {"agent_id": "A08", "correlation_id": "c1", "depends_on": ["T-up"]}
    with pytest.raises(ModuleNotFoundError) as raised:
        swarm_run.handover_section(task, tmp_path)
    assert raised.value.name == "swarm.missing_dep"


def test_handover_section_is_empty_when_the_module_is_absent(swarm_run, monkeypatch, tmp_path):
    real = importlib.import_module

    def absent(name, package=None):
        if name == "swarm.handover":
            raise ModuleNotFoundError("No module named 'swarm.handover'", name="swarm.handover")
        return real(name, package)

    monkeypatch.setattr(swarm_run.importlib, "import_module", absent)
    assert swarm_run.handover_section({"agent_id": "A08", "correlation_id": "c1", "depends_on": ["T-up"]}, tmp_path) == ""


def test_handover_section_logs_the_dependency_it_skips(swarm_run, monkeypatch, caplog, tmp_path):
    real = importlib.import_module

    def inspect(dep, *, root=None, to_agent=None, correlation_id=None):
        if dep == "T-bad":
            raise ValueError("handover document is not valid JSON")
        return {"status": "ok"}, {"goal": "ship the export", "from_agent": "A05", "to_agent": "A08"}

    fake = SimpleNamespace(inspect_handover=inspect)
    monkeypatch.setattr(swarm_run.importlib, "import_module", lambda name, package=None: fake if name == "swarm.handover" else real(name, package))
    task = {"agent_id": "A08", "correlation_id": "c1", "depends_on": ["T-bad", "T-good"]}
    with caplog.at_level(logging.DEBUG, logger="swarm.run"):
        section = swarm_run.handover_section(task, tmp_path)
    assert "ship the export" in section  # the good dependency still reaches the assignment
    skipped = [r for r in caplog.records if r.name == "swarm.run" and r.levelno == logging.DEBUG]
    assert len(skipped) == 1 and "T-bad" in skipped[0].getMessage()
