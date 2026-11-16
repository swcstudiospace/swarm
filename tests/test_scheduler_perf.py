"""Protocol and qualification tests for the paired scheduler sampler (tests/perf/scheduler_engines.py, Phase 14.4, S5.2 and S6).

The sampler's protocol is tested on fake legs (pairing, counts, percentiles, refusals, the native witness) and the qualifier on controlled
records (gain, no gain, equality, one percentile only, counts, warm-ups, watcher, load, failed subprocess). Nothing here times a scheduler,
asserts a machine speed or claims a speed-up. Fake legs test the protocol only."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("scheduler_engines_perf", ROOT / "tests" / "perf" / "scheduler_engines.py")
assert _SPEC is not None and _SPEC.loader is not None
sched = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = sched
_SPEC.loader.exec_module(sched)

A2 = sched.FIXTURES[0]
QUIET_A2 = "result: quiet at 12:00:00 UTC after 20 checks (load below 1.5 for 20 consecutive checks, interval 30s, rule: A2)"


class FakeLeg:
    """A leg that runs nothing and returns observations: the protocol under test, never a measurement."""

    def __init__(self, *, rust_ns=lambda j: 500, python_ns=1000, fail_tag=None, fail_rc=2, fallback_tag=None, parity_tag=None):
        self.rust_ns = rust_ns
        self.python_ns = python_ns
        self.fail_tag = fail_tag
        self.fail_rc = fail_rc
        self.fallback_tag = fallback_tag
        self.parity_tag = parity_tag
        self.calls: list[tuple[str, str]] = []

    def __call__(self, engine, tag):
        self.calls.append((engine, tag))
        j = int(tag[len("sample-"):]) if tag.startswith("sample-") else -1
        wall = self.python_ns if engine == "python" else self.rust_ns(j)
        rc = self.fail_rc if (tag == self.fail_tag and engine == "rust") else 0
        served = "python" if (tag == self.fallback_tag and engine == "rust") else engine
        store = [[["T-a", "DONE"]], 3]
        if tag == self.parity_tag and engine == "rust":
            store = [[["T-a", "IN_REVIEW"]], 3]
        result = {"status": "ok", "complete": True, "counts": {"DONE": 1}, "escalated": [], "scheduler": "continuous",
                  "max_parallel": 4, "effective_parallel": 4}
        return sched.Observation(engine, wall, rc, served, result, store, ["task.result"], f"scheduler_engine: {served}")


def _quiet_watcher(path: Path) -> str:
    """A watch_quiet.sh log whose last line is the A2 quiet line, written now: the run starts at most a few seconds later."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("an earlier window\n" + QUIET_A2 + "\n", encoding="utf-8")
    return str(path)


@pytest.fixture()
def hermetic_run(tmp_path, now, monkeypatch):
    """A fixed release fingerprint and a fresh quiet watcher log: the pre-flight passes, and the test controls what runs next."""
    fake = dict(now, binary={"path": str(tmp_path / "release" / "gsd_engine"), "sha256": "0" * 64}, extension_debug_sections=0)
    monkeypatch.setattr(sched, "fingerprint_now", lambda: fake)
    monkeypatch.setattr(sched, "_load1_now", lambda: 0.3)  # a quiet host at the start: the busy-host test overrides this
    return fake, _quiet_watcher(tmp_path / "watch.log")


def _elf_with_sections(names: list[str]) -> bytes:
    """A minimal little-endian ELF64 file whose section headers carry exactly `names`, plus the null and string-table sections."""
    strtab = b"\0" + b"".join(n.encode("ascii") + b"\0" for n in names) + b".shstrtab\0"
    offsets: dict[str, int] = {}
    pos = 1
    for n in names:
        offsets[n] = pos
        pos += len(n) + 1
    headers = [bytes(64)] + [struct.pack("<IIQQQQIIQQ", offsets[n], 1, 0, 0, 0, 0, 0, 0, 1, 0) for n in names]
    headers.append(struct.pack("<IIQQQQIIQQ", pos, 3, 0, 0, 64, len(strtab), 0, 0, 1, 0))
    shoff = 64 + len(strtab)
    ehdr = b"\x7fELF" + bytes([2, 1, 1]) + bytes(9) + struct.pack(
        "<HHIQQQIHHHHHH", 2, 62, 1, 0, 0, shoff, 0, 64, 0, 0, 64, len(headers), len(headers) - 1)
    return ehdr + strtab + b"".join(headers)

# ---------------------------------------------------------------------------------------------------------------- sampler protocol


def test_pairs_alternate_are_counted_and_take_nearest_rank_ratios():
    leg = FakeLeg(rust_ns=lambda j: 500 + 10 * j)
    pairs: list = []
    rec = sched.measure_fixture(A2, leg, sched.PAIRED_SAMPLES, sched.PAIRED_WARMUP, pairs)
    assert [p["index"] for p in pairs] == list(range(50))
    assert all(p["order"] == (["python", "rust"] if p["index"] % 2 == 0 else ["rust", "python"]) for p in pairs)
    assert len(leg.calls) == 2 * (1 + 3 + 50)  # the verify pair, three warm-up pairs, fifty measured pairs
    assert rec["ratio"]["p50"] == pytest.approx(0.74)  # rank 25 of the ratios 0.50 to 0.99 is j = 24
    assert rec["ratio"]["p95"] == pytest.approx(0.97)  # rank 48 is j = 47
    assert rec["skipped"] == 0 and rec["warmup_pairs"] == 3 and rec["samples"] == 50


@pytest.mark.parametrize("n,pct,want", [(50, 50, 25), (50, 95, 48), (100, 95, 95), (3, 50, 2), (1, 95, 1)])
def test_nearest_rank_is_the_ceiling_rank(n, pct, want):
    assert sched.nearest_rank(list(range(1, n + 1)), pct) == want


def test_nearest_rank_has_no_value_for_an_empty_sample():
    with pytest.raises(ValueError):
        sched.nearest_rank([], 50)


def test_a_failing_sample_refuses_and_keeps_only_the_pairs_before_it():
    pairs: list = []
    with pytest.raises(sched.Refused, match="exit 2"):
        sched.measure_fixture(A2, FakeLeg(fail_tag="sample-10", fail_rc=2), 50, 3, pairs)
    assert len(pairs) == 10


def test_a_failure_in_verification_refuses_before_any_measured_pair():
    pairs: list = []
    with pytest.raises(sched.Refused, match="verify"):
        sched.measure_fixture(A2, FakeLeg(fail_tag="verify", fail_rc=2), 50, 3, pairs)
    assert pairs == []


def test_a_rust_leg_answered_by_python_is_a_fallback_and_refuses():
    with pytest.raises(sched.Refused, match="fallback"):
        sched.measure_fixture(A2, FakeLeg(fallback_tag="sample-4"), 50, 3, [])


def test_a_result_mismatch_between_the_engines_refuses():
    with pytest.raises(sched.Refused, match="parity mismatch in store"):
        sched.measure_fixture(A2, FakeLeg(parity_tag="sample-2"), 50, 3, [])


@pytest.mark.parametrize("lines,want", [
    (["scheduler_engine: rust"], "rust"),
    (["scheduler_engine: python"], "python"),
    ([], "unknown"),
    (["scheduler_engine: rust", "scheduler_engine: python"], "unknown"),
], ids=["rust", "python", "no-line", "two-lines"])
def test_the_engine_that_answered_is_read_from_the_scheduler_log(lines, want):
    assert sched.served_by(lines) == want


def test_a_failed_subprocess_is_timed_with_its_exit_code_and_refused_as_a_sample(tmp_path):
    run = sched.execute_leg([sys.executable, "-c", "import sys; sys.exit(3)"], ROOT, dict(os.environ))
    assert run.rc == 3 and run.wall_ns > 0
    problems = sched.leg_problems(A2, sched.observe("python", run, tmp_path))
    assert any("exit 3" in p for p in problems)
    assert any("not a JSON object" in p for p in problems)


def test_the_paired_and_qualify_flags_exist_and_parse():
    args = sched.build_parser().parse_args(["--scheduler-paired", "--json-out", "rec.json", "--samples", "50", "--warmup", "3",
                                            "--watcher-log", "watch.log"])
    assert args.scheduler_paired is True
    assert args.json_out == Path("rec.json") and args.samples == 50 and args.warmup == 3 and args.watcher_log == "watch.log"
    assert sched.build_parser().parse_args(["--qualify-json", "rec.json"]).qualify_json == Path("rec.json")


def test_scheduler_paired_without_json_out_is_a_usage_error():
    with pytest.raises(SystemExit) as exc:
        sched.main(["--scheduler-paired"])
    assert exc.value.code == 2


# ------------------------------------------------------------------------------------------------------------------- qualifier


@pytest.fixture(scope="module")
def now():
    """The current fingerprint with its binary path fixed to the release location: these records are release measurements, whatever
    build the checkout holds, so the protocol tests do not depend on it."""
    fp = sched.fingerprint_now()
    fp["binary"] = {"path": str(sched.REPO / "target" / "release" / "gsd_engine"), "sha256": fp["binary"]["sha256"]}
    fp["extension_debug_sections"] = 0
    if fp["host"]["rustc"] in (None, "", "unavailable"):  # a probe that failed in this process is not the toolchain under test
        fp["host"]["rustc"] = "rustc test-fixed (the probe could not run rustc in this process)"
    return fp


STARTED_EPOCH = 1_000_000.0  # the run's start in these records; their watcher log was written 60 s before it


def make_record(now, ratio=lambda fid, j: 0.5, *, samples=50, warmup=3, watcher=QUIET_A2, loads=(0.3, 0.9, 0.4)):
    """A complete, quiet, A2 record whose pairs have the given Rust/Python ratio. Python takes one millisecond in every pair. A ready pass
    dispatches min(tasks, ready_dispatch_expectation()) tasks, the other fixtures all of theirs, and nothing is cancelled."""
    fixtures = {}
    for fx in sched.FIXTURES:
        dispatched = min(fx.tasks, sched.ready_dispatch_expectation()) if fx.mode == "ready" else fx.tasks
        pairs = []
        for j in range(samples):
            rs = round(1_000_000 * ratio(fx.id, j))
            order = ["python", "rust"] if j % 2 == 0 else ["rust", "python"]
            pairs.append({"index": j, "order": order, "python_ns": 1_000_000, "rust_ns": rs,
                          "python_rc": fx.expect_rc, "rust_rc": fx.expect_rc, "ratio": rs / 1_000_000,
                          "counts": {e: {"READY": fx.tasks - dispatched, "IN_REVIEW": dispatched} for e in ("python", "rust")},
                          "cancelled": {"python": None, "rust": None},
                          "dispatched": {"python": dispatched, "rust": dispatched}})
        fixtures[fx.id] = {
            "id": fx.id, "digest": now["fixtures"][fx.id], "status": "ok", "reason": "", "samples": samples, "warmup_pairs": warmup,
            "skipped": 0, "expect": {"rc": fx.expect_rc, "status": fx.expect_status}, "parity": {"ok": True, "mismatches": []},
            "served_by": {"python": "python", "rust": "rust"},
            "witness": {"python": "scheduler_engine: python", "rust": "scheduler_engine: rust"}, "pairs": pairs,
        }
    state = {"mtime_epoch": STARTED_EPOCH - 60.0, "mtime_utc": "1970-01-12T13:45:40Z", "last_line": watcher}
    fingerprint = copy.deepcopy({"source": now["source"], "extension": now["extension"], "binary": now["binary"],
                                 "fixtures": now["fixtures"], "host": now["host"],
                                 "extension_debug_sections": now["extension_debug_sections"]})
    return {
        "kind": sched.RECORD_KIND, "schema": sched.RECORD_SCHEMA, "status": "ok", "reason": "",
        "protocol": {"samples": samples, "warmup": warmup},
        "started_epoch": STARTED_EPOCH,
        "fingerprint": fingerprint,
        "fingerprint_end": copy.deepcopy(fingerprint),
        "watcher": {"log": "watch.log", "at_start": dict(state), "at_end": dict(state), "last_line": watcher},
        "load": {"readings": [{"at": at, "load1": load1, "watcher": watcher}
                              for at, load1 in zip(("before", "during", "after"), loads)]},
        "fixtures": fixtures,
    }


def test_a_complete_quiet_record_with_a_gain_qualifies(now):
    assert sched.qualify_record(make_record(now, lambda fid, j: 0.5), now) == []


def test_a_ratio_just_below_one_still_qualifies(now):
    assert sched.qualify_record(make_record(now, lambda fid, j: 0.99), now) == []


def test_no_gain_fails_both_percentiles(now):
    reasons = sched.qualify_record(make_record(now, lambda fid, j: 1.2), now)
    assert len(reasons) == 2 * len(sched.REQUIRED_FIXTURES)
    assert any("p50" in r for r in reasons) and any("p95" in r for r in reasons)


def test_equality_at_one_is_not_a_speed_up(now):
    reasons = sched.qualify_record(make_record(now, lambda fid, j: 1.0), now)
    assert len(reasons) == 2 * len(sched.REQUIRED_FIXTURES)
    assert all("not strictly below 1.00" in r for r in reasons)


def test_a_p95_only_failure_is_refused(now):
    # the p50 of a nearest-rank series is never above its p95, so the single-percentile failure that can occur is p95 alone
    reasons = sched.qualify_record(make_record(now, lambda fid, j: 1.5 if j >= 47 else 0.9), now)
    assert len(reasons) == len(sched.REQUIRED_FIXTURES)
    assert all("p95" in r and "p50" not in r for r in reasons)


def test_wrong_sample_counts_are_refused(now):
    reasons = sched.qualify_record(make_record(now, samples=49), now)
    assert any("wrong sample count" in r for r in reasons)
    assert any("measured pairs" in r for r in reasons)


def test_wrong_warmup_counts_are_refused(now):
    reasons = sched.qualify_record(make_record(now, warmup=2), now)
    assert any("wrong warm-up count" in r for r in reasons)
    assert any("warm-up pairs" in r for r in reasons)


@pytest.mark.parametrize("line,expected", [
    ("result: not quiet at 12:00:00 UTC after 20 checks (last load1 2.10, 0 of 20 consecutive below 1.5, interval 30s, rule: A2)",
     "contended watcher"),
    ("result: quiet at 12:00:00 UTC after 20 checks (load below 4.0 for 20 consecutive checks, interval 30s, rule: relaxed)", "relaxed watcher"),
    ("result: quiet at 12:00:00 UTC after 20 checks (THRESH 2.0, NEED 20, INTERVAL_S 30, rule: custom)", "non-A2 watcher"),
    (None, "missing watcher result"),
], ids=["contended", "relaxed", "non-a2", "missing"])
def test_a_watcher_that_is_not_a_quiet_a2_window_is_refused(now, line, expected):
    reasons = sched.qualify_record(make_record(now, watcher=line), now)
    assert any(expected in r for r in reasons)


@pytest.mark.parametrize("loads", [(0.3, 1.5, 0.4), (2.0, 0.9, 0.4)], ids=["during-at-1.5", "before-at-2.0"])
def test_load_at_or_above_one_and_a_half_is_contended(now, loads):
    reasons = sched.qualify_record(make_record(now, loads=loads), now)
    assert any(r.startswith("load >= 1.5") for r in reasons)


def test_a_failed_subprocess_sample_is_refused(now):
    rec = make_record(now)
    rec["fixtures"]["ready-50"]["pairs"][7]["rust_rc"] = 3
    reasons = sched.qualify_record(rec, now)
    assert any(r.startswith("failing sample: fixture ready-50 pair 7") for r in reasons)


def test_a_native_fallback_is_refused(now):
    rec = make_record(now)
    rec["fixtures"]["fake-omp-12"]["served_by"]["rust"] = "python"
    assert any("native fallback" in r and "fake-omp-12" in r for r in sched.qualify_record(rec, now))


def test_a_parity_mismatch_is_refused(now):
    rec = make_record(now)
    rec["fixtures"]["ready-500"]["parity"] = {"ok": False, "mismatches": ["store"]}
    assert any("parity mismatch" in r and "ready-500" in r for r in sched.qualify_record(rec, now))


def test_a_skipped_sample_is_refused(now):
    rec = make_record(now)
    rec["fixtures"]["a2-path-a"]["skipped"] = 1
    assert any("skipped samples" in r and "a2-path-a" in r for r in sched.qualify_record(rec, now))


def test_a_failed_fixture_or_run_is_refused(now):
    rec = make_record(now)
    rec["fixtures"]["ready-50"]["status"] = "failed"
    rec["fixtures"]["ready-50"]["reason"] = "ready-50: exit 2"
    rec["status"] = "failed"
    rec["reason"] = "ready-50: exit 2"
    reasons = sched.qualify_record(rec, now)
    assert any("status 'failed'" in r and "ready-50" in r for r in reasons)
    assert any(r.startswith("run status 'failed'") for r in reasons)


def test_a_missing_fixture_is_refused(now):
    rec = make_record(now)
    del rec["fixtures"]["fake-omp-12"]
    assert sched.qualify_record(rec, now) == ["missing fixture fake-omp-12"]


def test_a_fingerprint_mismatch_is_refused(now):
    rec = make_record(now)
    rec["fingerprint"]["source"]["scripts/swarm_run.py"] = "0" * 64
    rec["fingerprint"]["fixtures"]["ready-50"] = "0" * 64
    reasons = sched.qualify_record(rec, now)
    assert any(r.startswith("fingerprint mismatch: source") for r in reasons)
    assert any(r.startswith("fingerprint mismatch: fixtures") for r in reasons)


def test_a_binary_mismatch_is_refused(now):
    rec = make_record(now)
    rec["fingerprint"]["binary"] = {"path": "/elsewhere/gsd_engine", "sha256": "0" * 64}
    assert any(r.startswith("fingerprint mismatch: binary") for r in sched.qualify_record(rec, now))


def test_a_swapped_pair_order_is_refused(now):
    rec = make_record(now)
    rec["fixtures"]["fake-omp-12"]["pairs"][1]["order"] = ["python", "rust"]
    assert any(r.startswith("pairing: fixture fake-omp-12 pair 1") for r in sched.qualify_record(rec, now))


def test_a_non_positive_wall_time_is_a_malformed_pair(now):
    rec = make_record(now)
    rec["fixtures"]["a2-path-a"]["pairs"][0]["rust_ns"] = 0
    assert any(r.startswith("malformed pair 0 in fixture a2-path-a") for r in sched.qualify_record(rec, now))


def test_a_record_that_is_not_an_object_is_malformed(now):
    assert sched.qualify_record(["not", "a", "record"], now)[0].startswith("malformed record")


def test_the_qualify_flag_exits_zero_only_for_the_gain_record(tmp_path, now, monkeypatch):
    monkeypatch.setattr(sched, "fingerprint_now", lambda: now)  # the flag re-reads the fingerprint: pin it, so a live probe cannot decide
    gain, flat, bad = tmp_path / "gain.json", tmp_path / "flat.json", tmp_path / "bad.json"
    gain.write_text(json.dumps(make_record(now, lambda fid, j: 0.5)), encoding="utf-8")
    flat.write_text(json.dumps(make_record(now, lambda fid, j: 1.0)), encoding="utf-8")
    bad.write_text("{", encoding="utf-8")
    assert sched.main(["--qualify-json", str(gain)]) == 0
    assert sched.main(["--qualify-json", str(flat)]) == 1
    assert sched.main(["--qualify-json", str(bad)]) == 1
    assert sched.main(["--qualify-json", str(tmp_path / "absent.json")]) == 1


# ------------------------------------------------------------ review round 2: dispatch (B1), cancellation (M1), watcher (M2),
# fingerprint sources and host (M3, m2), exceptions (m1)


def test_a_ready_pass_that_dispatches_the_wrong_count_is_refused(now):
    rec = make_record(now)
    rec["fixtures"]["ready-50"]["pairs"][3]["dispatched"]["rust"] = 0  # a no-op pass
    assert any(r.startswith("dispatch: fixture ready-50 pair 3 rust") for r in sched.qualify_record(rec, now))


def test_the_sampler_refuses_a_ready_leg_that_dispatched_the_wrong_count():
    fx = sched.FIXTURES[2]  # ready-50: one round takes min(50, READY_PASS_PARALLEL, A05 max_parallel) tasks
    obs = sched.Observation("rust", 1, 1, "rust", {"status": "fail", "counts": {"READY": 50}, "cancelled": None, "dispatched": 0}, [], [],
                            "scheduler_engine: rust")
    assert any(f"dispatched 0, the one-round pass takes {sched.ready_dispatch_expectation()}" in p for p in sched.leg_problems(fx, obs))


def test_a_cancelled_task_is_refused(now):
    rec = make_record(now)
    rec["fixtures"]["fake-omp-12"]["pairs"][0]["counts"]["rust"]["CANCELLED"] = 1
    assert any(r.startswith("cancelled task: fixture fake-omp-12 pair 0 rust") for r in sched.qualify_record(rec, now))


def test_a_cancelled_run_is_refused(now):
    rec = make_record(now)
    rec["fixtures"]["a2-path-a"]["pairs"][4]["cancelled"]["python"] = "shutdown requested"
    assert any(r.startswith("cancelled run: fixture a2-path-a pair 4 python") for r in sched.qualify_record(rec, now))


def test_the_sampler_refuses_a_leg_whose_counts_or_cancel_field_show_cancellation():
    fx = sched.FIXTURES[1]  # fake-omp-12
    counted = sched.Observation("python", 1, 0, "python", {"status": "ok", "counts": {"CANCELLED": 2}, "cancelled": None, "dispatched": 12},
                                [], [], "scheduler_engine: python")
    stopped = sched.Observation("python", 1, 0, "python", {"status": "ok", "counts": {}, "cancelled": "shutdown", "dispatched": 12},
                                [], [], "scheduler_engine: python")
    assert any("CANCELLED" in p for p in sched.leg_problems(fx, counted))
    assert any("cancelled run" in p for p in sched.leg_problems(fx, stopped))


@pytest.mark.parametrize("age,refused", [(900.0, False), (901.0, True), (-5.0, True)], ids=["at-the-bound", "over-the-bound", "future"])
def test_the_watcher_line_must_be_written_at_most_900_seconds_before_the_run(now, age, refused):
    rec = make_record(now)
    rec["watcher"]["at_start"]["mtime_epoch"] = rec["started_epoch"] - age
    rec["watcher"]["at_end"]["mtime_epoch"] = rec["started_epoch"] - age
    reasons = sched.qualify_record(rec, now)
    assert any(r.startswith("stale or future watcher result") for r in reasons) is refused


def test_a_watcher_log_that_changed_during_the_run_is_refused(now):
    rec = make_record(now)
    rec["watcher"]["at_end"]["mtime_epoch"] += 30.0
    assert any(r.startswith("watcher log changed during the run") for r in sched.qualify_record(rec, now))


def test_a_load_reading_that_saw_another_watcher_line_is_refused(now):
    rec = make_record(now)
    rec["load"]["readings"][1]["watcher"] = ("result: not quiet at 12:00:30 UTC after 21 checks (last load1 2.20, 0 of 20 consecutive "
                                             "below 1.5, interval 30s, rule: A2)")
    assert any(r.startswith("watcher changed during the run") for r in sched.qualify_record(rec, now))


def test_the_fingerprint_sources_cover_orch_plan_and_every_swarm_module(now):
    swarm_py = {p.relative_to(ROOT).as_posix() for p in (ROOT / "swarm").glob("**/*.py") if "__pycache__" not in p.parts}
    assert swarm_py
    assert "scripts/orch_plan.py" in now["source"]
    assert {k for k in now["source"] if k.startswith("swarm/") and k.endswith(".py")} == swarm_py


def test_dropping_orch_plan_from_the_fingerprint_is_a_mismatch(now):
    rec = make_record(now)
    del rec["fingerprint"]["source"]["scripts/orch_plan.py"]
    assert any(r.startswith("fingerprint mismatch: source at start") for r in sched.qualify_record(rec, now))


def test_a_missing_end_fingerprint_is_refused(now):
    rec = make_record(now)
    del rec["fingerprint_end"]
    assert any(r.startswith("malformed record: no end fingerprint") for r in sched.qualify_record(rec, now))


@pytest.mark.parametrize("label,key", [("start", "fingerprint"), ("end", "fingerprint_end")])
@pytest.mark.parametrize("field,value", [("nproc", -1), ("rustc", "rustc 0.0.0 (changed)")])
def test_host_nproc_and_rustc_are_compared_at_the_start_and_the_end(now, label, key, field, value):
    rec = make_record(now)
    rec[key]["host"][field] = value
    assert any(r.startswith(f"fingerprint mismatch: host {field} at {label}") for r in sched.qualify_record(rec, now))


def test_an_unavailable_rustc_probe_is_refused_before_it_can_become_evidence(now):
    rec = make_record(now)
    rec["fingerprint"]["host"]["rustc"] = "unavailable"
    rec["fingerprint_end"]["host"]["rustc"] = "unavailable"
    assert any(r.startswith("unverified host: rustc") for r in sched.qualify_record(rec, now))


def test_an_unexpected_exception_becomes_the_run_reason_and_the_record_is_still_written(tmp_path, hermetic_run, monkeypatch):
    fake, watcher = hermetic_run

    def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(sched, "measure_fixture", boom)
    record_path = tmp_path / "record.json"
    assert sched.run_paired(record_path, 50, 3, watcher, tmp_path / "work") == 1
    rec = json.loads(record_path.read_text(encoding="utf-8"))
    assert rec["status"] == "failed" and "RuntimeError: boom" in rec["reason"]
    assert "fingerprint_end" in rec and sched.qualify_record(rec, fake)


def test_a_load_sampler_failure_does_not_mask_the_run_reason(tmp_path, hermetic_run, monkeypatch):
    _fake, watcher = hermetic_run

    def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    def stop_fails(self):
        self._stop.set()
        raise OSError("loadavg unreadable")

    monkeypatch.setattr(sched, "measure_fixture", boom)
    monkeypatch.setattr(sched.LoadSampler, "stop", stop_fails)
    record_path = tmp_path / "record.json"
    assert sched.run_paired(record_path, 50, 3, watcher, tmp_path / "work") == 1
    rec = json.loads(record_path.read_text(encoding="utf-8"))
    assert "RuntimeError: boom" in rec["reason"] and "loadavg" not in rec["reason"]


@pytest.mark.parametrize("line,reason", [
    ("result: not quiet at 12:00:00 UTC after 20 checks (load below 1.5 for 0 consecutive checks, rule: A2)", "contended watcher"),
    ("result: quiet at 12:00:00 UTC after 20 checks (load below 1.5 for 20 consecutive checks, interval 30s, rule: relaxed)", "relaxed watcher"),
])
def test_the_preflight_refuses_a_contended_or_relaxed_watcher_before_any_fixture_is_timed(tmp_path, hermetic_run, monkeypatch, line, reason):
    _fake, _watcher = hermetic_run
    watcher = tmp_path / "bad-watch.log"
    watcher.write_text(line + "\n", encoding="utf-8")
    timed: list[str] = []
    monkeypatch.setattr(sched, "measure_fixture", lambda *args, **kwargs: timed.append("timed"))
    record_path = tmp_path / "record.json"
    assert sched.run_paired(record_path, 50, 3, str(watcher), tmp_path / "work") == 1
    rec = json.loads(record_path.read_text(encoding="utf-8"))
    assert timed == [] and f"preflight: {reason}" in rec["reason"]


def test_a_debug_binary_is_refused_before_any_fixture_is_timed(tmp_path, hermetic_run, monkeypatch):
    fake, watcher = hermetic_run
    debug_fp = dict(fake, binary={"path": str(tmp_path / "target" / "debug" / "gsd_engine"), "sha256": "0" * 64})
    monkeypatch.setattr(sched, "fingerprint_now", lambda: debug_fp)
    timed: list[str] = []
    monkeypatch.setattr(sched, "measure_fixture", lambda *args, **kwargs: timed.append("timed"))
    record_path = tmp_path / "record.json"
    assert sched.run_paired(record_path, 50, 3, watcher, tmp_path / "work") == 1
    rec = json.loads(record_path.read_text(encoding="utf-8"))
    assert timed == [] and "preflight: debug build: the gsd_engine binary" in rec["reason"]
    assert sched.qualify_record(rec, debug_fp)  # the qualifier refuses the same record


def test_a_debug_extension_is_refused_before_any_fixture_is_timed(tmp_path, hermetic_run, monkeypatch):
    fake, watcher = hermetic_run
    debug_fp = dict(fake, extension_debug_sections=2)
    monkeypatch.setattr(sched, "fingerprint_now", lambda: debug_fp)
    timed: list[str] = []
    monkeypatch.setattr(sched, "measure_fixture", lambda *args, **kwargs: timed.append("timed"))
    record_path = tmp_path / "record.json"
    assert sched.run_paired(record_path, 50, 3, watcher, tmp_path / "work") == 1
    rec = json.loads(record_path.read_text(encoding="utf-8"))
    assert timed == [] and "preflight: debug build: the native extension" in rec["reason"]


def test_a_busy_host_at_the_start_is_refused_before_any_fixture_is_timed(tmp_path, hermetic_run, monkeypatch):
    _fake, watcher = hermetic_run
    monkeypatch.setattr(sched, "_load1_now", lambda: 2.5)
    timed: list[str] = []
    monkeypatch.setattr(sched, "measure_fixture", lambda *args, **kwargs: timed.append("timed"))
    record_path = tmp_path / "record.json"
    assert sched.run_paired(record_path, 50, 3, watcher, tmp_path / "work") == 1
    rec = json.loads(record_path.read_text(encoding="utf-8"))
    assert timed == [] and "preflight: host not quiet at start" in rec["reason"]


class _QuietLoad:
    """A load sampler that reads nothing from the host: one quiet reading that carries the watcher's last line."""

    def __init__(self, watcher_log=None):
        self.readings: list[dict] = []
        self._watcher = watcher_log

    def start(self):
        self.readings.append({"at": "before", "elapsed_s": 0.0, "load1": 0.3, "line": "0.30 0.30 0.30",
                              "watcher": sched.watcher_last_line(self._watcher)})

    def stop(self):
        self.readings.append({"at": "after", "elapsed_s": 0.0, "load1": 0.3, "line": "0.30 0.30 0.30",
                              "watcher": sched.watcher_last_line(self._watcher)})


def test_a_complete_run_qualifies_through_run_paired_and_the_qualifier(tmp_path, hermetic_run, monkeypatch):
    """Producer to consumer: the record run_paired writes is accepted by qualify_record. The legs are fake and their timings are
    chosen, so this tests the pipeline and the refusals, never a speed."""
    fake, watcher = hermetic_run

    def leg_for(fx):
        n = sched.ready_dispatch_expectation()

        def leg(engine, tag):
            j = int(tag[len("sample-"):]) if tag.startswith("sample-") else -1
            result = {"status": fx.expect_status, "complete": fx.expect_status == "ok",
                      "counts": {"DONE": 1} if fx.mode != "ready" else {"READY": fx.tasks - n, "IN_REVIEW": n},
                      "cancelled": None, "escalated": [], "scheduler": "continuous", "max_parallel": 4, "effective_parallel": 4}
            result["dispatched"] = n if fx.mode == "ready" else fx.tasks
            wall = 1000 if engine == "python" else 500 + max(j, 0)
            return sched.Observation(engine, wall, fx.expect_rc, engine, result, [[["T-a", "DONE"]], 3], ["task.result"],
                                     f"scheduler_engine: {engine}")

        return leg

    monkeypatch.setattr(sched, "make_leg", lambda fx, work, omp_bin: leg_for(fx))
    monkeypatch.setattr(sched, "LoadSampler", _QuietLoad)
    record_path = tmp_path / "record.json"
    assert sched.run_paired(record_path, sched.PAIRED_SAMPLES, sched.PAIRED_WARMUP, watcher, tmp_path / "work") == 0
    rec = json.loads(record_path.read_text(encoding="utf-8"))
    assert rec["status"] == "ok" and sched.qualify_record(rec, fake) == []


def test_the_debug_section_probe_tells_a_dev_build_from_a_release_build(tmp_path):
    dev = tmp_path / "dev.so"
    dev.write_bytes(_elf_with_sections([".text", ".debug_info"]))
    release = tmp_path / "release.so"
    release.write_bytes(_elf_with_sections([".text"]))
    assert sched.elf_debug_sections(dev) == [".debug_info"]
    assert sched.elf_debug_sections(release) == []
