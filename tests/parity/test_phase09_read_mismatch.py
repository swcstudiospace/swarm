"""Phase 9 packet R, the harness side (C16): a controlled mismatch through the existing `orch_status` comparison.

`harness.orch_status_leg` runs the real `scripts/orch_status.py` on a seeded scratch store, then runs `harness._rust_checks`
against the Rust leg. That leg is a deliberate test double: a shell script that records each start and prints a fixed JSON object
that differs from Python's. The double corrupts the comparison leg only. It is not a `gsd_engine` binary and is not native-serving
evidence; the `rust` label on its check is what the harness assigns to that leg.

The comparison must fail, and `_record_mismatch` must write one fixture with the three legs and the signing key redacted. With
`GSD_ENGINE` unset the selector answers with Python's bytes, and no binary starts. The fixture is written under pytest's temporary
directory: `harness.REPO` and `harness.FIXTURES` are redirected there, so the checkout's `tests/parity/fixtures` is not touched.
"""
from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness  # noqa: E402

ARGS = ["--history", "T-1"]
DOUBLE_STDOUT = '{"status": "ok", "test_double": "corrupted comparison leg"}\n'


def _double(tmp_path: Path) -> str:
    """The comparison-leg test double. Each start appends one byte to `invoked`; it prints DOUBLE_STDOUT and exits 0."""
    directory = tmp_path / "double"
    directory.mkdir()
    path = directory / "gsd_engine_test_double"
    marker = directory / "invoked"
    path.write_text(
        "#!/bin/sh\n"
        f"printf x >> {shlex.quote(str(marker))}\n"
        "printf '%s\\n' '{\"status\": \"ok\", \"test_double\": \"corrupted comparison leg\"}'\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return str(path)


def _mismatched(tmp_path: Path, monkeypatch) -> tuple[harness.Sandbox, harness.Probe]:
    """A seeded sandbox whose existing comparison has been run once against the corrupting double."""
    monkeypatch.setattr(harness, "REPO", tmp_path)
    monkeypatch.setattr(harness, "FIXTURES", tmp_path / "fixtures")
    sandbox = harness.Sandbox(tmp_path / "case", "phase09_read_mismatch", rust_bin=_double(tmp_path))
    harness.seed_tasks(sandbox, "T-1")
    truth = harness.orch_status_leg(sandbox, ARGS)
    return sandbox, truth


def test_comparison_fails_on_a_controlled_mismatch_and_retains_a_scrubbed_fixture(tmp_path, monkeypatch) -> None:
    sandbox, truth = _mismatched(tmp_path, monkeypatch)
    assert truth.exit_code == 0 and truth.payload is not None
    assert (tmp_path / "double" / "invoked").exists(), "the comparison leg did not run the double"

    failed = [check for check in sandbox.parity if not check.ok]
    assert len(failed) == 1, [check.label for check in sandbox.parity]
    assert "rust answers" in failed[0].label, failed[0].label

    fixtures = sorted((tmp_path / "fixtures").glob("*.json"))
    assert len(fixtures) == 1, fixtures
    assert f"fixture fixtures/{fixtures[0].name}" in failed[0].detail, failed[0].detail

    text = fixtures[0].read_text(encoding="utf-8")
    assert sandbox.signing_key not in text, "the fixture carries the signing key"
    body = json.loads(text)
    assert body["argv"] == ARGS and body["mutating"] is False
    assert set(body["legs"]) >= {"python", "rust", "default"}
    assert body["legs"]["rust"]["stdout"] == DOUBLE_STDOUT
    assert body["legs"]["python"]["stdout"] == truth.stdout.decode("utf-8")

    default = [check for check in sandbox.parity if "default selector" in check.label]
    assert len(default) == 1 and default[0].ok, [check.label for check in sandbox.parity]


def test_python_answers_with_its_own_bytes_and_no_binary_starts_after_the_mismatch(tmp_path, monkeypatch) -> None:
    sandbox, truth = _mismatched(tmp_path, monkeypatch)
    assert any(not check.ok for check in sandbox.parity), "the failed comparison was not observed"
    marker = tmp_path / "double" / "invoked"
    assert marker.exists(), "the comparison leg did not run the double"
    marker.unlink()

    default = harness.invoke(sandbox, "orch_status", ARGS, engine="default")
    explicit = harness.invoke(sandbox, "orch_status", ARGS, engine="python")

    assert not marker.exists(), "a binary was invoked while Python was selected"
    assert not sandbox.started_marker.exists()
    for probe in (default, explicit):
        assert probe.exit_code == truth.exit_code
        assert probe.stdout == truth.stdout
        assert probe.payload is not None
