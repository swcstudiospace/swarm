"""Quality runner detection follows repository evidence, not executable availability.

pytest uses the current interpreter; .NET remains required when dotnet is absent.
The TypeScript twin stays a pass-through to the canonical Python entrypoint.
"""
import importlib.util
import shutil
import sys
from pathlib import Path


import pytest
ROOT = Path(__file__).resolve().parent.parent


def _load_qa_gate():
    path = ROOT / "scripts" / "qa_gate.py"
    spec = importlib.util.spec_from_file_location("qa_gate_detection_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pytest_detected_when_importable_but_not_on_path(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_sample.py").write_text("def test_ok():\n    assert True\n")
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    assert shutil.which("pytest") is None
    assert importlib.util.find_spec("pytest") is not None

    runners = [r for r in _load_qa_gate().detect_runners(root) if r["name"] == "pytest"]
    assert len(runners) == 1
    assert runners[0]["cmd"][:3] == [sys.executable, "-m", "pytest"]


def test_ts_twin_documents_interpreter_detection():
    twin = (ROOT / "scripts" / "ts" / "qa_gate.ts").read_text(encoding="utf-8")
    source = (ROOT / "scripts" / "qa_gate.py").read_text(encoding="utf-8")
    assert 'passthrough("qa_gate")' in twin
    assert "find_spec" in twin
    assert "importlib.util.find_spec" in source
    assert 'which("pytest")' not in source
    assert "which('pytest')" not in source


@pytest.mark.parametrize("relative", ["App.sln", "App.slnx", "App.slnf", "src/App/App.csproj",
                                    "src/App/App.fsproj", "src/App/App.vbproj"])
def test_dotnet_detected_without_sdk(tmp_path, monkeypatch, relative):
    project = tmp_path / relative
    project.parent.mkdir(parents=True, exist_ok=True)
    project.write_text("")
    monkeypatch.setenv("PATH", "")
    assert shutil.which("dotnet") is None
    assert [r["name"] for r in _load_qa_gate().detect_runners(tmp_path)] == ["dotnet"]


@pytest.mark.parametrize("relative", ["bin/App.csproj", "obj/App.csproj", "node_modules/pkg/App.csproj",
                                    ".git/App.sln", ".swarm/App.sln"])
def test_generated_or_external_dotnet_projects_are_not_workloads(tmp_path, relative):
    project = tmp_path / relative
    project.parent.mkdir(parents=True)
    project.write_text("")
    assert not any(r["name"] == "dotnet" for r in _load_qa_gate().detect_runners(tmp_path))


def test_dotnet_coexists_with_other_workload_detection(tmp_path, monkeypatch):
    (tmp_path / "App.csproj").write_text("")
    (tmp_path / "pytest.ini").write_text("")
    monkeypatch.setenv("PATH", "")
    assert {r["name"] for r in _load_qa_gate().detect_runners(tmp_path)} == {"pytest", "dotnet"}
