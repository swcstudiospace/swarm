"""The pin-hash gate (Wave A criterion A0): the committed digest matches the pin sources, a pinned value
change moves the digest, comment and layout changes do not, and an unreadable source fails closed."""

import importlib.util
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "_check_pins.py"

CARGO = """[workspace]
members = []

[workspace.metadata.gsd-pins]
wasmtime = "50.0.0"
guest_target = "wasm32-unknown-unknown"
"""
TOOLCHAIN = '[toolchain]\nchannel = "1.90.0"\n'


def _load():
    spec = importlib.util.spec_from_file_location("_check_pins", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tree(path: Path, cargo: str = CARGO, toolchain: str = TOOLCHAIN) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "Cargo.toml").write_text(cargo, encoding="utf-8")
    (path / "rust-toolchain.toml").write_text(toolchain, encoding="utf-8")
    return path


def _run(root: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), *extra],
        capture_output=True,
        text=True,
    )


def test_the_committed_digest_matches_this_checkout():
    result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, cwd=ROOT)
    assert result.returncode == 0, result.stderr


def test_workspace_wasmtime_dependency_matches_the_exact_security_pin():
    cargo = tomllib.loads((ROOT / "Cargo.toml").read_text(encoding="utf-8"))
    workspace = cargo["workspace"]
    runtime = workspace["dependencies"]["wasmtime"]
    version = workspace["metadata"]["gsd-pins"]["wasmtime"]
    assert version == "50.0.1"
    assert runtime["version"] == f"={version}"
    assert "component-model" in runtime["features"]


def test_a_pinned_value_change_moves_the_digest(tmp_path):
    pin_digest = _load().pin_digest
    base = pin_digest(_tree(tmp_path / "base"))
    bumped = pin_digest(_tree(tmp_path / "bumped", cargo=CARGO.replace("50.0.0", "51.0.0")))
    assert base != bumped


def test_a_toolchain_change_moves_the_digest(tmp_path):
    pin_digest = _load().pin_digest
    base = pin_digest(_tree(tmp_path / "base"))
    bumped = pin_digest(_tree(tmp_path / "bumped", toolchain=TOOLCHAIN.replace("1.90.0", "1.91.0")))
    assert base != bumped


def test_comments_and_layout_do_not_move_the_digest(tmp_path):
    pin_digest = _load().pin_digest
    base = pin_digest(_tree(tmp_path / "base"))
    reformatted = CARGO.replace(
        'wasmtime = "50.0.0"',
        '# the wasmtime pin\nwasmtime   =   "50.0.0"',
    )
    assert pin_digest(_tree(tmp_path / "reformatted", cargo=reformatted)) == base


def test_a_recorded_digest_that_differs_fails_the_check(tmp_path):
    root = _tree(tmp_path / "tree")
    (root / "scripts").mkdir()
    (root / "scripts" / "pins.sha256").write_text("0" * 64 + "\n", encoding="utf-8")
    result = _run(root)
    assert result.returncode == 1
    assert "pin sources changed" in result.stderr


def test_a_missing_digest_fails_closed(tmp_path):
    result = _run(_tree(tmp_path / "tree"))
    assert result.returncode == 2


def test_a_source_without_the_pin_table_fails_closed(tmp_path):
    root = _tree(tmp_path / "tree", cargo="[workspace]\nmembers = []\n")
    result = _run(root, "--write")
    assert result.returncode == 2
    assert not (root / "scripts" / "pins.sha256").exists()


def test_a_toml_date_in_the_pin_table_fails_closed_under_write(tmp_path):
    cargo = CARGO + "released = 2026-10-25\n"
    root = _tree(tmp_path / "tree", cargo=cargo)
    (root / "scripts").mkdir()
    result = _run(root, "--write")
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert not (root / "scripts" / "pins.sha256").exists()


def test_a_digest_file_with_invalid_utf8_fails_closed(tmp_path):
    root = _tree(tmp_path / "tree")
    (root / "scripts").mkdir()
    (root / "scripts" / "pins.sha256").write_bytes(b"\xff\xfe\n")
    result = _run(root)
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_write_into_a_root_without_a_scripts_directory_fails_closed(tmp_path):
    root = _tree(tmp_path / "tree")
    result = _run(root, "--write")
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert not (root / "scripts" / "pins.sha256").exists()


def test_an_invalid_toolchain_file_fails_closed_under_write(tmp_path):
    root = _tree(tmp_path / "tree", toolchain="[toolchain\nchannel = 1.90.0\n")
    (root / "scripts").mkdir()
    result = _run(root, "--write")
    assert result.returncode == 2
    assert not (root / "scripts" / "pins.sha256").exists()


def test_an_invalid_toolchain_file_fails_closed_under_check(tmp_path):
    root = _tree(tmp_path / "tree", toolchain="[toolchain\nchannel = 1.90.0\n")
    (root / "scripts").mkdir()
    (root / "scripts" / "pins.sha256").write_text("0" * 64 + "\n", encoding="utf-8")
    result = _run(root)
    assert result.returncode == 2
