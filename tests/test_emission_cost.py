"""Unit E1 (phase 14.4): the per-event cost of the run log, the state store and the substrate tee dropped without changing a byte.

What the cuts rely on, and what pins it here:
- `swarm.paths.swarm_dir` remembers one resolution per input (SWARM_DIR, root, and the cwd where one of them is relative or absent):
  a changed input resolves again, `create=True` still looks for `.gitignore` on every call;
- `append_text` is the old `open(path, "a", encoding="utf-8").write(text)` as one write(2): same bytes, nothing buffered across calls;
- `SwarmStateStore` renders its JSON once and skips its mkdir when the state file is there;
- `workspace.credential` looks an agent's slug up once, through `manifest.agent_slug`, which never aliases the cached roster.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from conftest import ROOT


def _mod(name: str) -> Any:
    """The module as it is now in sys.modules: other tests drop `swarm*` from it, and the tee imports its own modules lazily."""
    return importlib.import_module(name)


# ---- swarm_dir: the cache is keyed by its inputs ----------------------------------------------------------------------------------------


def _old_swarm_dir(root: str | Path | None, env_val: str | None) -> Path:
    """The resolution of swarm_dir before the cache: every call resolved the cwd and the root itself."""
    paths = _mod("swarm.paths")
    if env_val:
        base = str(Path.cwd().resolve())
    else:
        base = str(Path(root).resolve() if root is not None else Path.cwd().resolve())
    return paths._resolve(env_val, base)


def test_swarm_dir_follows_every_input_that_decides_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _mod("swarm.paths")
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    link = tmp_path / "link"
    link.symlink_to(a)
    steps: list[tuple[str | None, str | Path | None, Path]] = [  # (SWARM_DIR, root, cwd)
        (str(a / ".swarm"), None, a),
        (str(b / ".swarm"), None, a),  # another absolute SWARM_DIR
        (str(a / ".swarm"), None, b),  # back to the first one: a cache hit must not leak the other
        ("rel/.swarm", None, a),  # a relative SWARM_DIR is anchored at the cwd ...
        ("rel/.swarm", None, b),  # ... so a new cwd is a new result
        (None, a, tmp_path),  # no SWARM_DIR: the root decides (no git repo here: <root>/.swarm)
        (None, b, tmp_path),
        (None, str(link), tmp_path),  # a symlinked root resolves to its target
        (None, "a", tmp_path),  # a relative root is anchored at the cwd ...
        (None, "a", a),
        (None, None, a),  # no root either: the cwd decides
        (None, None, b),
        ("", a, tmp_path),  # an empty SWARM_DIR is unset
    ]
    seen: set[Path] = set()
    for env_val, root, cwd in steps + steps[::-1]:  # twice, the second time from a warm cache and in the other order
        monkeypatch.chdir(cwd)
        if env_val is None:
            monkeypatch.delenv("SWARM_DIR", raising=False)
        else:
            monkeypatch.setenv("SWARM_DIR", env_val)
        got = paths.swarm_dir(root)
        assert got == _old_swarm_dir(root, env_val or None), (env_val, root, cwd)
        assert got.is_absolute()
        seen.add(got)
    assert len(seen) >= 5  # the steps really lead to different directories


def test_swarm_dir_past_the_cache_bound_still_resolves_each_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _mod("swarm.paths")
    for i in range(paths._DIRS_MAX * 2 + 3):
        monkeypatch.setenv("SWARM_DIR", str(tmp_path / f"d{i}"))
        assert paths.swarm_dir() == tmp_path / f"d{i}"
    assert len(paths._DIRS) <= paths._DIRS_MAX


def test_swarm_dir_create_looks_for_gitignore_on_every_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _mod("swarm.paths")
    state = tmp_path / ".swarm"
    monkeypatch.setenv("SWARM_DIR", str(state))
    paths.swarm_dir()
    assert not state.exists()  # resolving alone creates nothing
    paths.swarm_dir(create=True)
    assert (state / ".gitignore").read_text() == "*"
    (state / ".gitignore").unlink()  # removed behind the cache's back: the next create puts it back
    paths.swarm_dir(create=True)
    assert (state / ".gitignore").read_text() == "*"
    (state / ".gitignore").unlink()
    state.rmdir()  # the whole directory is gone: the next create makes it again
    paths.swarm_dir(create=True)
    assert (state / ".gitignore").read_text() == "*"


def test_swarm_dir_resolves_nothing_again_for_a_known_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The point of the cache: after the first call an absolute SWARM_DIR, or an absolute root, costs no Path.resolve and no git."""
    paths = _mod("swarm.paths")
    resolves: list[object] = []
    real_resolve = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda self, *a, **k: resolves.append(self) or real_resolve(self, *a, **k))
    runs: list[object] = []
    monkeypatch.setattr(paths.subprocess, "run", lambda *a, **k: runs.append(a) or subprocess.CompletedProcess(a, 1, "", ""))
    monkeypatch.setenv("SWARM_DIR", str(tmp_path / "s1"))
    for root in (None, tmp_path):
        paths.swarm_dir(root, create=True)
        resolves.clear()
        for _ in range(5):
            paths.swarm_dir(root, create=True)
        assert resolves == []
    monkeypatch.delenv("SWARM_DIR")
    paths.swarm_dir(tmp_path)
    resolves.clear()
    runs.clear()
    for _ in range(5):
        paths.swarm_dir(tmp_path)
    assert resolves == [] and runs == []


# ---- append_text: the old append, in one write -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("before,text", [
    (None, "one line\n"),
    (b"", "x"),
    (b"kept\n", "second\n"),
    (b"kept without newline", "next\n"),
    (b"", "héllo ✓ 日本語 😀\n"),
    (b"", ("y" * 100_000) + "\n"),  # larger than any io buffer
    (b"", ""),
])
def test_append_text_leaves_the_bytes_open_append_leaves(tmp_path: Path, before: bytes | None, text: str) -> None:
    paths = _mod("swarm.paths")
    old, new = tmp_path / "old.log", tmp_path / "new.log"
    for path in (old, new):
        if before is not None:
            path.write_bytes(before)
    with old.open("a", encoding="utf-8") as fh:
        fh.write(text)
    paths.append_text(new, text)
    assert new.read_bytes() == old.read_bytes()
    assert new.stat().st_mode == old.stat().st_mode  # the same creation mode (0o666 and the umask)


def test_append_text_that_cannot_be_encoded_fails_as_open_append_does(tmp_path: Path) -> None:
    paths = _mod("swarm.paths")
    old, new = tmp_path / "old.log", tmp_path / "new.log"
    with pytest.raises(UnicodeEncodeError), old.open("a", encoding="utf-8") as fh:
        fh.write("lone surrogate \ud800\n")
    with pytest.raises(UnicodeEncodeError):
        paths.append_text(new, "lone surrogate \ud800\n")
    assert new.read_bytes() == old.read_bytes() == b""  # both created the file and wrote nothing
    paths.append_text(new, "still usable\n")  # and no descriptor or lock is left behind
    assert new.read_bytes() == b"still usable\n"


def test_append_text_in_a_missing_directory_raises_as_open_does(tmp_path: Path) -> None:
    paths = _mod("swarm.paths")
    with pytest.raises(FileNotFoundError):
        paths.append_text(tmp_path / "nope" / "x.log", "a\n")


def test_concurrent_big_records_never_interleave(tmp_path: Path) -> None:
    """One write(2) per record: records longer than an io buffer from eight threads still come out as whole lines."""
    paths = _mod("swarm.paths")
    log = tmp_path / "x.log"
    per_thread, size = 30, 40_000

    def work(tag: str) -> None:
        for i in range(per_thread):
            paths.append_text(log, json.dumps({"tag": tag, "i": i, "pad": tag * size}) + "\n")

    threads = [threading.Thread(target=work, args=(chr(ord("a") + n),)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = log.read_text().splitlines()
    assert len(lines) == 8 * per_thread
    for line in lines:
        record = json.loads(line)  # a torn or mixed line does not parse
        assert record["pad"] == record["tag"] * size


# ---- runlog.emit: the same record in the same bytes --------------------------------------------------------------------------------------

_FAKE_KEY = "sk-" + "a1B2c3D4" * 4  # a token shape built at run time, not a credential
EVENTS: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
    ("phase.reconcile", {"phase": "reconcile", "round": 3}, {"correlation_id": "c-1", "source": "A01@swarm_run"}),
    ("note", {"text": "héllo ✓ 日本語 😀", "nested": {"a": [1, 2.5, None, True], "b": ("t", "u")}}, {"source": "x"}),
    ("secret", {"token": _FAKE_KEY, "task_id": "sk-onboarding-flow-v2", "note": f"key {_FAKE_KEY}"}, {"task_id": "T-1", "source": "x"}),
    ("lease", {"lease_id": "L-secret", "notes": json.dumps({"lease_id": "L-secret", "k": 1}), "k": "v"}, {"source": "x"}),
    ("big", {"blob": "x" * 60_000, "rows": [{"i": i} for i in range(100)]}, {"source": "x"}),
    ("empty", {}, {"source": ""}),
]


@pytest.mark.parametrize("event_type,payload,kw", EVENTS, ids=[e[0] for e in EVENTS])
def test_emit_writes_exactly_the_record_it_returns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event_type: str, payload: dict[str, Any],
                                                   kw: dict[str, Any]) -> None:
    runlog = _mod("swarm.runlog")
    state = tmp_path / ".swarm"
    monkeypatch.setenv("SWARM_DIR", str(state))
    first = runlog.emit(event_type, payload, tee=False, **kw)
    second = runlog.emit(event_type, payload, tee=False, **kw)
    reference = tmp_path / "reference.jsonl"  # the writer of the old emit, fed with the same records
    with reference.open("a", encoding="utf-8") as fh:
        for record in (first, second):
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    written = (state / "events.jsonl").read_bytes()
    assert written == reference.read_bytes()
    assert [json.loads(line) for line in written.decode("utf-8").splitlines()] == [first, second]
    assert first["msg_id"] != second["msg_id"]
    assert list(first) == ["ts", "type", "source", "correlation_id", "task_id", "payload", "msg_id"]  # the key order of the record
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", first["ts"])
    assert (state / ".gitignore").read_text() == "*"


def test_emit_keeps_redaction_lease_removal_and_identifier_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runlog = _mod("swarm.runlog")
    monkeypatch.setenv("SWARM_DIR", str(tmp_path / ".swarm"))
    by_type = {t: runlog.emit(t, p, tee=False, **kw)["payload"] for t, p, kw in EVENTS}
    assert _FAKE_KEY not in json.dumps(by_type["secret"]) and by_type["secret"]["token"] == "[REDACTED]"
    assert by_type["secret"]["task_id"] == "sk-onboarding-flow-v2"  # an identifier field is not free text
    assert "L-secret" not in json.dumps(by_type["lease"]) and by_type["lease"]["k"] == "v"
    assert by_type["note"]["nested"] == {"a": [1, 2.5, None, True], "b": ["t", "u"]}
    assert EVENTS[3][1]["lease_id"] == "L-secret"  # the caller's payload is left as it was


def test_emit_follows_swarm_dir_and_cwd_between_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runlog = _mod("swarm.runlog")
    one, two = tmp_path / "one", tmp_path / "two"
    monkeypatch.setenv("SWARM_DIR", str(one))
    runlog.emit("a", {}, source="s", tee=False)
    monkeypatch.setenv("SWARM_DIR", str(two))
    runlog.emit("b", {}, source="s", tee=False)
    monkeypatch.setenv("SWARM_DIR", "rel")
    monkeypatch.chdir(tmp_path)
    runlog.emit("c", {}, source="s", tee=False)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    runlog.emit("d", {}, source="s", tee=False)
    types = {p: [json.loads(line)["type"] for line in (p / "events.jsonl").read_text().splitlines()] for p in (one, two, tmp_path / "rel", elsewhere / "rel")}
    assert list(types.values()) == [["a"], ["b"], ["c"], ["d"]]


def test_a_killed_process_loses_no_event_that_emit_returned(tmp_path: Path) -> None:
    """Nothing is buffered across calls: every emit that returned is in the file when the process dies by SIGKILL on the next line."""
    state = tmp_path / ".swarm"
    code = ("import os, signal, sys\nsys.path.insert(0, %r)\nfrom swarm.runlog import emit\n"
            "for i in range(5):\n    r = emit('k', {'i': i}, source='s', tee=False)\n    print(r['msg_id'], flush=True)\n"
            "os.kill(os.getpid(), signal.SIGKILL)\n") % str(ROOT)
    env = {**os.environ, "SWARM_DIR": str(state), "SWARM_PURE_PYTHON": "1"}
    done = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == -signal.SIGKILL, done.stderr
    ids = done.stdout.split()
    assert len(ids) == 5
    assert [json.loads(line)["msg_id"] for line in (state / "events.jsonl").read_text().splitlines()] == ids


# ---- emit(tee=True): the tee decides as before, now without a manifest decode per event --------------------------------------------------


@pytest.fixture()
def aged_roster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A copy of agents.json old enough for the manifest cache (it never caches a file changed in the last 2 s)."""
    manifest = _mod("swarm.manifest")
    copy = tmp_path / "agents.json"
    copy.write_text((ROOT / "agents.json").read_text(encoding="utf-8"), encoding="utf-8")
    os.utime(copy, ns=(1_700_000_000 * 10**9,) * 2)
    monkeypatch.setattr(manifest, "AGENTS_FILE", copy)
    return copy


def _tee_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Substrate configured, no agent token anywhere: the tee must keep the row local and say so once. Returns the HTTP posts (none)."""
    tee_mod, client = _mod("swarm.substrate_tee"), _mod("swarm.substrate_client")
    posts: list[object] = []
    monkeypatch.setattr(client, "rest_post", lambda *a, **k: posts.append(a) or None)
    monkeypatch.setattr(tee_mod, "_UNSENT", {})
    monkeypatch.setenv("SUBSTRATE_URL", "http://127.0.0.1:9")
    for var in [k for k in os.environ if k.startswith("SUBSTRATE_TOKEN")] + ["SUBSTRATE_DISABLED", "SUBSTRATE_OPERATOR_TOKENS"]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("SWARM_DIR", str(tmp_path / ".swarm"))
    return posts


def test_emit_with_tee_and_no_token_keeps_the_row_local_and_says_so_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, aged_roster: Path,
                                                                        capsys: pytest.CaptureFixture[str]) -> None:
    runlog, tee_mod = _mod("swarm.runlog"), _mod("swarm.substrate_tee")
    posts = _tee_world(tmp_path, monkeypatch)
    records = [runlog.emit("spawn.event", {"subagent_id": "s", "task_id": "T-1"}, source="A01@subagent_lifecycle", correlation_id="c-1",
                           task_id="T-1", root=tmp_path, tee=True) for _ in range(3)]
    assert posts == []  # nothing left the machine
    lines = (tmp_path / ".swarm" / "events.jsonl").read_text().splitlines()
    assert [json.loads(line) for line in lines] == records  # the local row is written first, whole
    err = capsys.readouterr().err
    assert err.count("[substrate-tee] A01 run-log rows stay local: no token of its own") == 1
    assert "a01-orchestrator.env does not exist and SUBSTRATE_TOKEN_SWARM_A01_ORCH is unset" in err
    assert tee_mod.tee(records[0], tmp_path) is False


def test_the_tee_decodes_no_agent_record_per_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, aged_roster: Path) -> None:
    runlog, manifest = _mod("swarm.runlog"), _mod("swarm.manifest")
    _tee_world(tmp_path, monkeypatch)

    def emit_one() -> None:
        runlog.emit("spawn.event", {"task_id": "T-1"}, source="A01@subagent_lifecycle", correlation_id="c-1", task_id="T-1", root=tmp_path)

    emit_one()  # warms the roster cache
    decoded: list[str] = []
    real_loads = manifest.json.loads
    monkeypatch.setattr(manifest.json, "loads", lambda text, *a, **k: decoded.append(text[:20]) or real_loads(text, *a, **k))
    for _ in range(10):
        emit_one()
    assert decoded == []  # the slug comes out of the cached record as a str; before, two json.loads of the whole agent per event


def _old_credential(workspace_mod: Any, agent_id: str | None, workspace: str | Path | None, env: dict[str, str]) -> tuple[str | None, str, str]:
    """`credential` before E1: get_agent(...)["slug"] looked up on each of its two branches."""
    manifest = _mod("swarm.manifest")
    surface = _mod("swarm.substrate_tee").AGENT_SURFACES.get(agent_id or "")
    if surface is None:
        return None, "", f"no substrate surface for agent {agent_id!r}"
    path = None
    if workspace is not None:
        try:
            path = workspace_mod.env_file(workspace, manifest.get_agent(agent_id or "")["slug"], env)
        except KeyError:
            return None, "", f"no agent {agent_id!r} in the manifest"
        try:
            workspace_mod.check_env_directory(path.parent)
        except (workspace_mod.EnvFileProblem, OSError) as exc:
            return None, str(path), str(exc)
        if os.path.lexists(path):
            try:
                return workspace_mod.read_token(path), str(path), ""
            except (workspace_mod.EnvFileProblem, OSError) as exc:
                return None, str(path), str(exc)
    var = workspace_mod.server_var(surface)
    given = (env.get(var) or "").strip()
    if workspace_mod.valid_token(given):
        return given, f"${var}", ""
    try:
        slug = manifest.get_agent(agent_id or "")["slug"]
    except KeyError:
        slug = ""
    if (env.get("SWARM_AGENT_SESSION") or "").strip() == "1" and slug and (env.get("SWARM_AGENT") or "").strip() == slug:
        session_token = (env.get(workspace_mod.TOKEN) or "").strip()
        if workspace_mod.valid_token(session_token):
            return session_token, f"${workspace_mod.TOKEN}", ""
    return None, "", f"{path or 'no workspace'}{' does not exist' if path else ''} and {var} is unset"


def test_credential_answers_as_before_in_every_branch(tmp_path: Path) -> None:
    workspace = _mod("swarm.workspace")
    ws = tmp_path / "ws"
    ws.mkdir()
    base = {"XDG_CONFIG_HOME": str(tmp_path / "xdg"), "HOME": str(tmp_path / "home")}
    token_var = "SUBSTRATE_TOKEN_SWARM_A05_BE"
    with_file = tmp_path / "with-file"  # a workspace whose env dir holds A05's env file: the first branch
    with_file.mkdir()
    workspace.write_token(workspace.env_file(with_file, "a05-backend", base), "file-token")
    cases: list[tuple[str | None, Path | None, dict[str, str]]] = [
        ("A05", None, {}),  # no workspace, no variable
        ("A05", None, {token_var: "env-token"}),  # the server-named variable
        ("A05", None, {token_var: "   "}),  # blank is unset
        ("A05", None, {"SWARM_AGENT_SESSION": "1", "SWARM_AGENT": "a05-backend", "SUBSTRATE_TOKEN": "session-token"}),  # the agent's own session
        ("A05", None, {"SWARM_AGENT_SESSION": "1", "SWARM_AGENT": "a06-frontend", "SUBSTRATE_TOKEN": "someone-else"}),  # another agent's session
        ("A05", ws, {}),  # a workspace without a file
        ("A05", ws, {token_var: "env-token"}),
        ("A05", with_file, {token_var: "env-token"}),  # the file wins
        ("A05", with_file, {}),
        ("A99", ws, {}),  # no surface
        (None, ws, {}),
        ("A01", None, {"SWARM_AGENT_SESSION": "1", "SWARM_AGENT": "a01-orchestrator", "SUBSTRATE_TOKEN": "t"}),
    ]
    for agent, where, extra in cases:
        env = {**base, **extra}
        got = workspace.credential(agent, where, env)
        assert got == _old_credential(workspace, agent, where, env), (agent, where, extra)
    assert workspace.credential("A05", with_file, base) == ("file-token", str(workspace.env_file(with_file, "a05-backend", base)), "")
    assert workspace.credential("A05", None, {**base, token_var: "env-token"})[0] == "env-token"
    assert workspace.credential("A05", None, {**base, "SWARM_AGENT_SESSION": "1", "SWARM_AGENT": "a05-backend", "SUBSTRATE_TOKEN": "s"})[0] == "s"
    assert "a05-backend.env does not exist" in workspace.credential("A05", ws, base)[2]


def test_credential_with_an_agent_the_roster_lacks_reports_it(tmp_path: Path, aged_roster: Path) -> None:
    workspace, manifest = _mod("swarm.workspace"), _mod("swarm.manifest")
    aged_roster.write_text(json.dumps([a for a in manifest.load_manifest() if a["id"] == "A01"]), encoding="utf-8")
    os.utime(aged_roster, ns=(1_700_000_100 * 10**9,) * 2)  # a changed mtime is a new cache key
    env = {"XDG_CONFIG_HOME": str(tmp_path / "xdg"), "HOME": str(tmp_path)}
    assert workspace.credential("A05", tmp_path, env) == (None, "", "no agent 'A05' in the manifest")  # A05 has a surface, the roster no A05
    assert workspace.credential("A05", None, env)[2].endswith("SUBSTRATE_TOKEN_SWARM_A05_BE is unset")
    assert workspace.credential("A01", None, env)[2].endswith("SUBSTRATE_TOKEN_SWARM_A01_ORCH is unset")
    assert manifest.agent_slug("A01") == "a01-orchestrator"


# ---- manifest.agent_slug -----------------------------------------------------------------------------------------------------------------


def test_agent_slug_is_get_agent_slug_for_every_spelling(aged_roster: Path) -> None:
    manifest = _mod("swarm.manifest")
    agents = manifest.load_manifest()
    assert len(agents) >= 30
    for agent in agents:
        for spelling in (agent["id"], agent["code"], agent["slug"], agent["id"].lower(), agent["slug"].upper()):
            slug = manifest.agent_slug(spelling)
            assert slug == manifest.get_agent(spelling)["slug"] == agent["slug"]
            assert type(slug) is str
    for missing in ("", "A99", "nobody"):
        with pytest.raises(KeyError) as old:
            manifest.get_agent(missing)
        with pytest.raises(KeyError) as new:
            manifest.agent_slug(missing)
        assert new.value.args == old.value.args == (f"no agent matches {missing!r}",)


def test_agent_slug_sees_a_roster_rewritten_mid_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The contract of the manifest cache holds for the new accessor: keyed by path, mtime, size and inode; a file changed in the last
    2 s is never cached; the roster path is read per call (here through AGENTS_FILE and through an explicit path)."""
    manifest = _mod("swarm.manifest")
    row = dict(manifest.load_manifest(ROOT / "agents.json")[0])
    old_ns = 1_700_000_000 * 10**9

    def write(path: Path, slug: str, *, aged_ns: int | None) -> None:
        path.write_text(json.dumps([{**row, "slug": slug, "id": "A77", "code": "ZX1"}]), encoding="utf-8")
        if aged_ns is not None:
            os.utime(path, ns=(aged_ns, aged_ns))

    one, two = tmp_path / "one.json", tmp_path / "two.json"
    write(one, "a77-one", aged_ns=old_ns)  # old enough to be cached
    write(two, "a77-two", aged_ns=old_ns)
    monkeypatch.setattr(manifest, "AGENTS_FILE", one)
    assert manifest.agent_slug("a77") == "a77-one"
    assert manifest.agent_slug("a77") == "a77-one"  # cached
    assert manifest.agent_slug("a77", two) == "a77-two"  # another path, another record
    monkeypatch.setattr(manifest, "AGENTS_FILE", two)  # another roster file for the same call
    assert manifest.agent_slug("a77") == "a77-two"
    write(two, "a77-three", aged_ns=old_ns + 500 * 10**9)  # same path rewritten: mtime changed, so the key changed
    assert manifest.agent_slug("a77") == "a77-three"
    write(two, "a77-four", aged_ns=None)  # a file changed just now is not cached at all ...
    assert manifest.agent_slug("a77") == "a77-four"
    write(two, "a77-five", aged_ns=None)  # ... so a rewrite in the same instant is still seen
    assert manifest.agent_slug("a77") == "a77-five"
    assert manifest.get_agent("a77")["slug"] == "a77-five"
    with pytest.raises(KeyError):
        manifest.agent_slug("a01-orchestrator")  # the real roster's agents are not in the rewritten file


def test_agent_slug_hands_out_no_part_of_the_cached_record(aged_roster: Path) -> None:
    manifest = _mod("swarm.manifest")
    manifest.agent_slug("A01")  # cached now
    manifest.get_agent("A01")["slug"] = "mutated"
    manifest.load_manifest()[0]["slug"] = "mutated"
    assert manifest.agent_slug("A01") == "a01-orchestrator" == manifest.get_agent("A01")["slug"]


# ---- SwarmStateStore and the audit log ---------------------------------------------------------------------------------------------------

AUDIT = re.compile(r"(?P<ts>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) event=(?P<event>\S+) subagent_id=(?P<sub>.*) task_slice=(?P<slc>.*) state=(?P<st>.*) identity=(?P<idt>.*)")


def _store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Path, Path]:
    state_store = _mod("swarm.state_store")
    state, claude = tmp_path / ".swarm", tmp_path / "claude"
    monkeypatch.setenv("SWARM_DIR", str(state))
    monkeypatch.setenv("SWARM_CLAUDE_STATE_DIR", str(claude))
    return state_store.get_swarm_state_store(root=tmp_path), state, claude


def test_store_creates_its_two_state_files_and_leaves_existing_ones(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_store = _mod("swarm.state_store")
    store, state, claude = _store(tmp_path, monkeypatch)
    for path in (claude / "swarm-state.json", state / "swarm-state.json"):
        doc = json.loads(path.read_text())
        assert doc["swarm_state"] == "INITIALIZING" and doc["version"] == "1.0" and doc["progress"] == {}
        assert path.read_text() == json.dumps(doc, indent=2) + "\n"
    (claude / "swarm-state.json").write_text('{"kept": true}\n')
    state_store.get_swarm_state_store(root=tmp_path)  # a second store finds both files and writes neither
    assert (claude / "swarm-state.json").read_text() == '{"kept": true}\n'
    assert json.loads((state / "swarm-state.json").read_text())["swarm_state"] == "INITIALIZING"
    (claude / "swarm-state.json").unlink()
    claude.rmdir()  # the directory is gone with the file: the next store makes both again
    state_store.get_swarm_state_store(root=tmp_path)
    assert json.loads((claude / "swarm-state.json").read_text())["swarm_state"] == "INITIALIZING"
    assert store.claude_path == claude / "swarm-state.json"


def test_update_writes_both_files_with_one_rendering_and_one_audit_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, state, claude = _store(tmp_path, monkeypatch)
    data = store.update("RUNNING", corr="c-1", progress={"done": 1}, extra={"subagent_id": "subagent-T-1", "slice_id": "S"})
    expected = json.dumps(data, indent=2) + "\n"
    assert (claude / "swarm-state.json").read_text() == (state / "swarm-state.json").read_text() == expected
    assert data["swarm_state"] == "RUNNING" and data["correlation_id"] == "c-1" and data["progress"] == {"done": 1}
    match = AUDIT.fullmatch((state / "gsd-lifecycle-audit.log").read_text().splitlines()[-1])
    assert match and (match["event"], match["sub"], match["slc"], match["st"], match["idt"]) == ("state_update", "subagent-T-1", "S", "RUNNING", "c-1")
    # a state outside the closed set maps as before
    assert store.update("STREAMING")["swarm_state"] == "RUNNING" and store.update("nonsense")["swarm_state"] == "INITIALIZING"


def test_update_with_an_unserialisable_extra_writes_no_state_file_but_still_returns_and_audits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, state, claude = _store(tmp_path, monkeypatch)
    before = (claude / "swarm-state.json").read_text(), (state / "swarm-state.json").read_text()
    odd = object()
    data = store.update("RUNNING", extra={"x": odd})
    assert data["swarm_state"] == "RUNNING" and data["x"] is odd  # returned, as before
    assert ((claude / "swarm-state.json").read_text(), (state / "swarm-state.json").read_text()) == before
    assert not list(claude.glob(".swarm-state.json.*"))  # and no temp file is left
    assert AUDIT.fullmatch((state / "gsd-lifecycle-audit.log").read_text().splitlines()[-1])


def test_audit_line_format_truncation_and_newlines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, state, _ = _store(tmp_path, monkeypatch)
    store.append_gsd_audit("ev", subagent_id="a\nb" + "s" * 200, task_slice="x" * 150, state="ST\nATE" + "z" * 60, identity="i\nd")
    store.append_gsd_audit("ev2")
    store.append_gsd_audit("ev3", subagent_id="é✓", root=tmp_path)
    lines = (state / "gsd-lifecycle-audit.log").read_bytes().decode("utf-8").split("\n")
    assert lines[-1] == "" and len(lines) == 4  # every entry ends in one newline
    first, second, third = (AUDIT.fullmatch(line) for line in lines[:3])
    assert first and second and third
    assert first["sub"] == ("a b" + "s" * 200)[:100] and first["slc"] == "x" * 100 and first["st"] == ("ST ATE" + "z" * 60)[:32] and first["idt"] == "i d"
    assert (second["event"], second["sub"], second["slc"], second["st"], second["idt"]) == ("ev2", "", "", "", "")
    assert third["sub"] == "é✓"


def test_gsd_line_goes_only_to_an_existing_state_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, _, _ = _store(tmp_path, monkeypatch)
    mirror = tmp_path / "STATE.md"
    monkeypatch.setenv("SWARM_GSD_STATE_FILE", str(mirror))
    store.append_gsd_line("RUNNING", state="RUNNING")
    assert not mirror.exists()  # never created
    mirror.write_text("head\n")
    store.append_gsd_line("c-1 IN_PROGRESS 1/2", root=tmp_path, state="IN_PROGRESS")
    store.append_gsd_line("bare")
    assert mirror.read_text() == "head\nswarm: c-1 IN_PROGRESS 1/2 state=IN_PROGRESS\nswarm: bare\n"
    monkeypatch.setenv("SWARM_GSD_STATE_FILE", "")
    store.append_gsd_line("disabled")
    assert mirror.read_text().endswith("swarm: bare\n")


# ---- the streams built on them ----------------------------------------------------------------------------------------------------------


def test_the_gsd_and_status_streams_keep_their_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    observability, runlog = _mod("swarm.observability"), _mod("swarm.runlog")
    state = tmp_path / ".swarm"
    monkeypatch.setenv("SWARM_DIR", str(state))
    monkeypatch.setenv("SWARM_CLAUDE_STATE_DIR", str(tmp_path / "claude"))
    monkeypatch.delenv("SUBSTRATE_URL", raising=False)
    sid = "subagent-T-0001-0123456789ab-1790000000000"
    omp = observability.emit_gsd_omp("spawn", subagent_id=sid, max=4, wave1="gsd-autonomous", root=tmp_path, dry_run=True, corr="c-1")
    streamed = observability.stream_subagent_status(sid, "spawned", phase="launch", wave="wave1", root=tmp_path, dry_run=True, corr="c-1")
    types = [e["type"] for e in runlog.read_events(root=tmp_path)]
    assert types == ["gsd.autonomous.omp", "per-unit", "subagent.status.stream"]  # per-unit first: stream_subagent_status emits it before its own
    assert omp["source"] == "A01@omp_gsd" and omp["task_id"] == "T-0001" and omp["payload"]["state"] == "spawn" and omp["payload"]["max"] == 4
    assert streamed["source"] == "A01@subagent_lifecycle" and streamed["payload"]["phase"] == "launch"
    audit = [AUDIT.fullmatch(line) for line in (state / "gsd-lifecycle-audit.log").read_text().splitlines()]
    assert [m["event"] if m else None for m in audit] == ["emit_gsd_omp", "stream_subagent_status"]
    assert all(m and m["sub"] == sid for m in audit)
    for name in ("events.jsonl", "gsd-lifecycle-audit.log", "swarm-state.json", ".gitignore"):
        assert (state / name).exists()
