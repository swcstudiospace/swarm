"""Roster cache in swarm.manifest: hits, invalidation, isolation, parity with an uncached read."""
from __future__ import annotations
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from swarm import manifest
from swarm.manifest import AGENTS_FILE, by_capability, get_agent, load_manifest

REAL_FILE = AGENTS_FILE


def _real_agents() -> list[dict[str, Any]]:
    data = json.loads(REAL_FILE.read_text(encoding="utf-8"))
    agents = data["agents"] if isinstance(data, dict) else data
    return list(agents)


def _write(path: Path, agents: list[dict[str, Any]], *, old: bool = True) -> Path:
    path.write_text(json.dumps({"agents": agents}), encoding="utf-8")
    if old:
        stamp = time.time_ns() - 10_000_000_000
        os.utime(path, ns=(stamp, stamp))
    return path


@pytest.fixture
def validations(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    real = manifest.validate_agents

    def counting(agents: object) -> list[str]:
        calls.append(1)
        return real(agents)

    monkeypatch.setattr(manifest, "validate_agents", counting)
    manifest._cache.clear()
    return calls


def test_hit_validates_once(tmp_path: Path, validations: list[int]) -> None:
    agents = _real_agents()[:3]
    f = _write(tmp_path / "a.json", agents)
    cap = agents[0]["capabilities"][0]
    for _ in range(3):
        get_agent(agents[0]["id"], f)
        by_capability(cap, f)
        assert load_manifest(f) == agents
    assert len(validations) == 1


def test_rewrite_invalidates(tmp_path: Path, validations: list[int]) -> None:
    agents = _real_agents()
    f = _write(tmp_path / "a.json", agents[:3])
    assert len(load_manifest(f)) == 3
    _write(f, agents[:2])
    assert load_manifest(f) == agents[:2]
    assert len(validations) == 2


def test_recent_file_never_cached(tmp_path: Path, validations: list[int]) -> None:
    agents = _real_agents()[:2]
    f = _write(tmp_path / "a.json", agents, old=False)
    for _ in range(3):
        assert load_manifest(f) == agents
    assert len(validations) == 3


def test_results_are_isolated(tmp_path: Path, validations: list[int]) -> None:
    agents = _real_agents()[:3]
    f = _write(tmp_path / "a.json", agents)
    cap = agents[0]["capabilities"][0]
    for fetch in (
        lambda: get_agent(agents[0]["id"], f),
        lambda: by_capability(cap, f)[0],
        lambda: load_manifest(f)[0],
    ):
        first = fetch()
        first["capabilities"].append("zzz-mutated")
        first["injected"] = True
        second = fetch()
        assert second == agents[0]
        assert "injected" not in second
    assert len(validations) == 1


def test_invalid_roster_not_cached(tmp_path: Path, validations: list[int]) -> None:
    bad = _real_agents()[:2]
    del bad[0]["slug"]
    f = _write(tmp_path / "bad.json", bad)
    messages: list[str] = []
    for _ in range(3):
        with pytest.raises(ValueError) as exc:
            load_manifest(f)
        messages.append(str(exc.value))
    assert len(set(messages)) == 1
    assert "bad.json" in messages[0]
    assert len(validations) == 3


def test_parity_with_uncached_reference() -> None:
    reference = _real_agents()
    manifest._cache.clear()
    for a in reference:
        for ident in (a["id"], a["code"], a["slug"], a["slug"].upper()):
            low = ident.lower()
            expected = next(x for x in reference if low in (x["id"].lower(), x["code"].lower(), x["slug"].lower()))
            assert get_agent(ident) == expected
    with pytest.raises(KeyError) as exc:
        get_agent("no-such-agent")
    assert exc.value.args[0] == "no agent matches 'no-such-agent'"
    caps = {c for a in reference for c in a["capabilities"]} | {"no.such.capability"}
    for cap in caps:
        assert by_capability(cap) == [a for a in reference if cap in a["capabilities"]]
    assert load_manifest() == reference


def test_concurrent_mixed_calls(tmp_path: Path, validations: list[int]) -> None:
    agents = _real_agents()[:3]
    f = _write(tmp_path / "a.json", agents)
    cap = agents[0]["capabilities"][0]
    expected_caps = [a for a in agents if cap in a["capabilities"]]
    errors: list[BaseException] = []

    def work() -> None:
        try:
            for i in range(25):
                if i % 3 == 0:
                    assert load_manifest(f) == agents
                elif i % 3 == 1:
                    assert get_agent(agents[1]["slug"], f) == agents[1]
                else:
                    assert by_capability(cap, f) == expected_caps
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_manifest(tmp_path / "nope.json")
