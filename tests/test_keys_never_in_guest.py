"""Signing keys never enter the guest (phase 14.5, unit U3.4e: everything that exists today).

What is proved here, and the part of the key-custody design each test pins:

A. DEPENDENCY GRAPH. `gsd-route` and `gsd-guest` (the code the sandboxed guest links) reach none of `gsd-envelope`, `ed25519-dalek`,
   `curve25519-dalek`, `hmac`, `gsd-security` through normal or build dependencies, on every platform and for the wasm32 platform, with
   all features on. Dev dependencies are NOT followed: they are compiled only for `cargo test` of that crate and never reach the guest
   component or a host library, so a test helper may use key code without moving a key into the guest. Positive controls keep the
   deny-list from being vacuous: `gsd-envelope` itself reaches the three crypto crates, `gsd-sandbox` reaches `gsd-security`, and the
   walker finds `gsd-codes` from `gsd-route`. The test is NOT applied to `gsd-sandbox` (it reaches ed25519-dalek through `gsd-security`)
   nor to `gsd-taskwriter` (it reaches `hmac` through sqlx): both are host crates and may hold key code.
B. CANARY over everything that exists: valid-shaped canary values in `SWARM_SIGNING_KEY`, `SWARM_ED25519_KEY` and
   `SWARM_ED25519_KEY_FILE` (a path that carries a random marker) go into the environment of the REAL `gsd_engine` debug binary, reached
   through the `scripts/orch_status.py` selector and called directly, for a transition and both read routes on both backends. Nothing
   the run leaves (stdout, stderr, every file under the repository, state, temp and home directories, the SQLite files with their
   WAL and SHM, the run log, `live.json`) may hold a canary in its raw, upper-case, hex, 0x, space-separated hex, `\\x..`, decimal
   list, base64 (every alignment, both alphabets), UTF-16 (both byte orders), decoded-key-byte or 16-digit prefix/suffix form. A `git`
   shim on PATH records the environment of the `git` child the binary spawns: no key variable reaches it. A failing assertion names the
   variable and the form that matched, never a value.
   The tests run against a binary of a KNOWN build: the build is probed (`probe_build`), recorded in the junit file
   (`gsd_engine_build`), put into every failure message, and may be declared with `GSD_ENGINE_BUILD`. The sandbox write legs
   prove the write guest only on a `--features sandbox-write` build; on the default build they check the named refusal (E-DEP,
   "no write guest", write_state not_started) and then SKIP with "not covered", so they never read as a pass. Under `CI` a
   missing binary FAILS instead of skipping, and a binary older than the Rust sources fails (`newer_sources`).
   Error legs (invalid JSON or UTF-8, oversize input, a corrupt Task Store, a missing swarm directory, a locked database) run with
   `RUST_BACKTRACE=1`; a hostile request `env` map (keys and `SWARM_DIR`) is dropped except for the allowlisted `SWARM_DIR`; a canary
   key file under `$HOME/.config/agent-swarm/` and one named by `SWARM_ED25519_KEY_FILE` (with canary CONTENT) are never read into
   anything the run prints or writes, and an APPROVED transition is refused before any write.
C. SELECTOR. The request the selector writes to the binary's stdin (`RUST_ENV_KEYS`) never carries a key variable. The environment it
   starts the binary with (`_binary_env(route)`) is an explicit allowlist, and only a real transition to APPROVED
   (`transition-approved`) and an ingest (`ingest`, which verifies verdict rows) are handed key material: the five signing variables plus `SWARM_AGENT_SESSION` and `XDG_CONFIG_HOME`, which
   the binary's key policy reads beside them. `ROUTE_KEY_POLICY` is the one place that says which route may receive which key variable
   (an edit of `_binary_env` and of the table must go together), and a positive pin says the APPROVED route IS handed all seven names.
D. DRIFT. Every `SWARM_*(KEY|SECRET|TOKEN|SEED|PEM|PRIVATE)` name the key code and the scripts mention is in `SIGNING_KEY_ENV` of
   the selector, and the two lists that strip signing material from child sessions (`hook_trigger.SIGNING_ENV_STRIPPED`,
   `swarm_run.AGENT_SESSION_STRIPPED`) cover it. A divergence is a finding and is pinned with `xfail(strict=True)`.

The canaries are made at run time, held in memory and never written to a file or printed. The binary is `target/debug/gsd_engine` (or
`GSD_ENGINE_BIN`); outside CI the real-binary tests skip, naming the reason, without it.

What U3.4d added and is covered here: the key-reading path of a transition to APPROVED with canaries (R2: `R2_CASES`, the real binary
through the selector and directly, on both backends) and the reverse-dependency pin of `gsd-envelope`. The guest request and reply of an
APPROVED transition (`approval` facts: booleans and gate names) are checked by the Rust half. Not here, waiting for later units: the
guest's linear memory after a call, and the effects of the sign routes (U3.6a). The Rust half of this unit is
`crates/gsd-adapter/tests/keys_never_in_guest.rs`, `crates/gsd-sandbox/tests/guest_imports.rs` and
`crates/gsd-envelope/tests/key_error_canary.rs`.
"""

from __future__ import annotations

import ast
import base64
import copy
import functools
import hashlib
import importlib.util
import inspect
import json
import os
import pwd
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Collection, Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from conftest import ROOT

from swarm.taskstore import TaskStore

# The selector under test; ORCH_STATUS_SCRIPT points the file at a mutated copy when the teeth of these tests are checked.
SCRIPT = Path(os.environ.get("ORCH_STATUS_SCRIPT") or ROOT / "scripts" / "orch_status.py")

# ---------------------------------------------------------------------------------------------------------------------------------
# A. Dependency graph
# ---------------------------------------------------------------------------------------------------------------------------------

WASM_TARGET = "wasm32-unknown-unknown"
GUARDED = ("gsd-route", "gsd-guest")
DENIED = frozenset({"gsd-envelope", "ed25519-dalek", "curve25519-dalek", "hmac", "gsd-security"})
CRYPTO = frozenset({"ed25519-dalek", "curve25519-dalek", "hmac"})
# Edge kinds of cargo metadata that end up in a built artifact: normal (None) and build. Dev dependencies are left out on purpose.
SHIPPED: frozenset[str | None] = frozenset({None, "build"})

Metadata = dict[str, Any]


class MetadataUnavailable(Exception):
    """`cargo metadata` could not resolve the graph offline."""


@functools.cache
def _metadata(platform: str | None) -> Metadata:
    command = ["cargo", "metadata", "--offline", "--locked", "--format-version", "1", "--all-features"]
    if platform is not None:
        command += ["--filter-platform", platform]
    # The suite points HOME at a scratch directory for every test; cargo needs the real one to find the registry and the toolchain.
    env = {**os.environ, "HOME": pwd.getpwuid(os.getuid()).pw_dir}
    try:
        done = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=280, check=False)
    except FileNotFoundError as exc:
        raise MetadataUnavailable("cargo is not installed") from exc
    if done.returncode != 0:
        raise MetadataUnavailable(done.stderr[-600:])
    parsed: Metadata = json.loads(done.stdout)
    return parsed


def reachable(meta: Metadata, root: str, kinds: frozenset[str | None] = SHIPPED) -> set[str]:
    """Names of the packages reachable from workspace member `root` over edges of `kinds` (the root itself is not included)."""
    names = {pkg["id"]: pkg["name"] for pkg in meta["packages"]}
    members = {names[pid]: pid for pid in meta["workspace_members"]}
    if root not in members:
        raise AssertionError(f"{root} is not a workspace member")
    edges = {node["id"]: node["deps"] for node in meta["resolve"]["nodes"]}
    seen: set[str] = set()
    pending = [members[root]]
    while pending:
        for dep in edges[pending.pop()]:
            if dep["pkg"] not in seen and any(kind["kind"] in kinds for kind in dep["dep_kinds"]):
                seen.add(dep["pkg"])
                pending.append(dep["pkg"])
    return {names[pid] for pid in seen}


def with_edge(meta: Metadata, source: str, target: str, kind: str | None) -> Metadata:
    """A scratch copy of `meta` with one more dependency edge from workspace member `source` to the package named `target`."""
    scratch = copy.deepcopy(meta)
    ids = {pkg["name"]: pkg["id"] for pkg in scratch["packages"]}
    for node in scratch["resolve"]["nodes"]:
        if node["id"] == ids[source]:
            node["deps"].append({"name": target.replace("-", "_"), "pkg": ids[target], "dep_kinds": [{"kind": kind, "target": None}]})
            return scratch
    raise AssertionError(f"no resolve node for {source}")


@pytest.fixture(params=[None, WASM_TARGET], ids=["all-platforms", WASM_TARGET])
def graph(request: pytest.FixtureRequest) -> Metadata:
    try:
        return _metadata(request.param)
    except MetadataUnavailable as exc:
        if request.param is None:
            pytest.fail(f"cargo metadata --offline --locked failed: {exc}")
        pytest.skip(f"cargo metadata cannot resolve {request.param} offline ({exc}); the all-platform check still runs")


@pytest.mark.parametrize("crate", GUARDED)
def test_guest_crates_reach_no_key_code(graph: Metadata, crate: str) -> None:
    reached = reachable(graph, crate)
    assert "gsd-codes" in reached, f"the walker found no dependencies of {crate}: the graph is empty or unreadable"
    if crate == "gsd-guest":
        assert "gsd-route" in reached
    assert not DENIED & reached, f"{crate} reaches key code: {sorted(DENIED & reached)}"


def test_the_deny_list_is_not_vacuous() -> None:
    meta = _metadata(None)
    assert CRYPTO <= reachable(meta, "gsd-envelope"), "gsd-envelope no longer reaches the crypto crates: the deny-list names stale crates"
    assert "gsd-security" in reachable(meta, "gsd-sandbox"), "gsd-sandbox no longer reaches gsd-security: the deny-list names a stale crate"
    assert "gsd-envelope" in {pkg["name"] for pkg in meta["packages"]}


# The host crates are the only ones that may reach the key code (unit U3.4d): `gsd-taskwriter` depends on `gsd-envelope` (it verifies the
# verdict rows of an APPROVED and signs a gate's verdict), and `gsd-adapter` reaches it through the writer. `gsd-route`, `gsd-guest` and
# `gsd-sandbox` never do (the sandbox reaches `ed25519-dalek` through `gsd-security`, which is why this pin names `gsd-envelope` itself).
KEY_CODE_DEPENDENTS = frozenset({"gsd-adapter", "gsd-taskwriter"})
NEVER_REACH_ENVELOPE = ("gsd-route", "gsd-guest", "gsd-sandbox")


def workspace_reaching(meta: Metadata, target: str, kinds: frozenset[str | None] = SHIPPED) -> set[str]:
    """The workspace members from which `target` is reachable: the reverse dependencies of `target` in the whole workspace."""
    names = {pkg["id"]: pkg["name"] for pkg in meta["packages"]}
    return {names[pid] for pid in meta["workspace_members"] if target in reachable(meta, names[pid], kinds)}


def test_only_the_writer_and_the_adapter_reach_gsd_envelope(graph: Metadata) -> None:
    reaching = workspace_reaching(graph, "gsd-envelope")
    assert reaching <= KEY_CODE_DEPENDENTS, f"crates that reach gsd-envelope and must not: {sorted(reaching - KEY_CODE_DEPENDENTS)}"
    assert not set(NEVER_REACH_ENVELOPE) & reaching
    if graph is _metadata(None):
        # The positive control: the crates that must reach it do, so a rename or a dropped dependency cannot make this pass for nothing.
        assert reaching == KEY_CODE_DEPENDENTS, f"the host crates no longer reach gsd-envelope: {sorted(reaching)}"


@pytest.mark.parametrize("intruder", [*NEVER_REACH_ENVELOPE, "gsd-codes", "gsd-failure"])
@pytest.mark.parametrize("kind", [None, "build"])
def test_the_reverse_dependency_pin_flags_a_crate_that_starts_to_reach_gsd_envelope(intruder: str, kind: str | None) -> None:
    meta = _metadata(None)
    assert workspace_reaching(meta, "gsd-envelope") == KEY_CODE_DEPENDENTS
    mutated = with_edge(meta, intruder, "gsd-envelope", kind)
    extra = workspace_reaching(mutated, "gsd-envelope") - KEY_CODE_DEPENDENTS
    assert intruder in extra, f"a new edge {intruder} -> gsd-envelope went unnoticed"


@pytest.mark.parametrize(
    ("source", "kind", "followed"),
    [
        ("gsd-route", None, True),  # a normal edge
        ("gsd-route", "build", True),  # a build edge: it runs at build time on the host, but is still graph input of the artifact
        ("gsd-codes", None, True),  # a transitive edge: guest -> route -> codes -> envelope
        ("gsd-route", "dev", False),  # dev edges are not part of the artifact
    ],
)
def test_the_walker_flags_an_added_edge_to_gsd_envelope(graph: Metadata, source: str, kind: str | None, followed: bool) -> None:
    mutated = with_edge(graph, source, "gsd-envelope", kind)
    for crate in GUARDED:
        hits = DENIED & reachable(mutated, crate)
        if followed:
            assert {"gsd-envelope", "ed25519-dalek", "curve25519-dalek", "hmac"} <= hits
        else:
            assert not hits


def test_the_walker_does_not_follow_dev_edges_by_default_but_can() -> None:
    mutated = with_edge(_metadata(None), "gsd-route", "gsd-envelope", "dev")
    assert "gsd-envelope" not in reachable(mutated, "gsd-route")
    assert "gsd-envelope" in reachable(mutated, "gsd-route", frozenset({None, "build", "dev"}))


# ---------------------------------------------------------------------------------------------------------------------------------
# B. Canaries: forms, scanning, the real binary
# ---------------------------------------------------------------------------------------------------------------------------------

SECRET_VARS = ("SWARM_SIGNING_KEY", "SWARM_ED25519_KEY", "SWARM_ED25519_KEY_FILE")
FLAG_VARS = ("SWARM_ALLOW_INSECURE_DEV_KEY", "SWARM_REQUIRE_KEY")
KEY_VARS = (*SECRET_VARS, *FLAG_VARS)


def _b64_fragments(data: bytes) -> dict[str, bytes]:
    """The base64 text that `data` produces wherever it sits in a larger blob: one fragment per alignment and alphabet.

    The characters that depend on the neighbouring bytes (the head after a prefix of 1 or 2 bytes, the tail before a missing byte)
    are cut, so the fragment is a substring of the encoding of every blob that contains `data` at that alignment.
    """
    fragments: dict[str, bytes] = {}
    for offset in range(3):
        encoded = base64.b64encode(bytes(offset) + data).rstrip(b"=")
        head = (0, 2, 3)[offset]
        tail = (0, 1, 1)[(offset + len(data)) % 3]
        fragments[f"b64.{offset}"] = encoded[head : len(encoded) - tail]
        fragments[f"b64url.{offset}"] = fragments[f"b64.{offset}"].replace(b"+", b"-").replace(b"/", b"_")
    return fragments


def _byte_forms(prefix: str, data: bytes) -> dict[str, bytes]:
    """The shapes a dump or list of the bytes `data` takes in code and logs, keyed `<prefix><shape>`."""
    return {
        f"{prefix}dec-list": ", ".join(str(byte) for byte in data).encode(),
        f"{prefix}dec-list-tight": ",".join(str(byte) for byte in data).encode(),
        f"{prefix}x-escapes": "".join(f"\\x{byte:02x}" for byte in data).encode(),
        f"{prefix}0x-hex": b"0x" + data.hex().encode(),
        f"{prefix}0x-list": ", ".join(f"0x{byte:02x}" for byte in data).encode(),
        f"{prefix}hex-spaced": " ".join(f"{byte:02x}" for byte in data).encode(),
        f"{prefix}HEX-spaced": " ".join(f"{byte:02X}" for byte in data).encode(),
        f"{prefix}bytes-repr": repr(data)[2:-1].encode(),
    }


# A secret at least this long (in hex digits) is a key, and its prefix and suffix windows are a key id; a shorter one is a marker.
KEY_WINDOW_MIN_DIGITS = 32


def forms(value: str) -> dict[str, bytes]:
    """Every byte form of a secret `value` that a leak could take, keyed by a label that is safe to print."""
    raw = value.encode()
    found: dict[str, bytes] = {
        "utf8": raw,
        "utf8-upper": value.upper().encode(),
        "utf16le": value.encode("utf-16-le"),
        "utf16be": value.encode("utf-16-be"),
        "hex-of-text": raw.hex().encode(),
        "HEX-of-text": raw.hex().upper().encode(),
        **_b64_fragments(raw),
        **_byte_forms("text-", raw),
    }
    try:
        key_bytes = bytes.fromhex(value)
    except ValueError:
        return found
    found["key-bytes"] = key_bytes
    found.update({f"key-{label}": fragment for label, fragment in _b64_fragments(key_bytes).items()})
    found.update(_byte_forms("key-", key_bytes))
    if len(value) >= KEY_WINDOW_MIN_DIGITS:
        found.update(
            {
                "key-prefix16": value[:16].encode(),
                "key-suffix16": value[-16:].encode(),
                "key-PREFIX16": value[:16].upper().encode(),
                "key-SUFFIX16": value[-16:].upper().encode(),
                "key-first8-bytes": key_bytes[:8],
                "key-last8-bytes": key_bytes[-8:],
            }
        )
    return found


@dataclass(frozen=True, repr=False)
class Canaries:
    """Per-run canary values and the forms to search for. `env` holds the values: it is passed to child processes, never printed."""

    env: Mapping[str, str]
    needles: Mapping[str, Mapping[str, bytes]]

    def __repr__(self) -> str:
        return "Canaries(<redacted>)"


def make_canaries(base: Path) -> Canaries:
    """Valid-shaped canaries: two different 64-hex keys and a key file path that carries a random marker."""
    marker = secrets.token_hex(12)
    values = {
        "SWARM_SIGNING_KEY": secrets.token_hex(32),
        "SWARM_ED25519_KEY": secrets.token_hex(32),
        "SWARM_ED25519_KEY_FILE": str(base / f"keys-{marker}" / "ed25519.pem"),
    }
    assert values["SWARM_SIGNING_KEY"] != values["SWARM_ED25519_KEY"]
    needles: dict[str, Mapping[str, bytes]] = {name: forms(value) for name, value in values.items()}
    needles["KEY_FILE_MARKER"] = forms(marker)
    return Canaries({**values, "SWARM_ALLOW_INSECURE_DEV_KEY": "1", "SWARM_REQUIRE_KEY": "1"}, needles)


def secret_values(canaries: Canaries) -> list[str]:
    """The canary values that are secrets (not the `1` of the policy flags), for checks that a message holds none of them."""
    return [canaries.env[name] for name in SECRET_VARS]


def leaks(data: bytes, canaries: Canaries) -> list[str]:
    """`VARIABLE/form` of every canary form found in `data`. A value is never part of the answer."""
    return [f"{name}/{label}" for name, table in canaries.needles.items() for label, needle in table.items() if needle in data]


def require_clean(found: list[str], where: str) -> None:
    """Fails with the labels of `found` only. An `assert leaks(...) == []` would print the data and the canaries in its explanation."""
    if found:
        raise AssertionError(f"canary in {where}: {sorted(found)}")


def files_under(*roots: Path, exclude: Collection[Path] = ()) -> Iterator[Path]:
    """Every regular file under `roots` that is not a symlink and not in `exclude` (the files a test planted itself)."""
    skipped = {path.resolve() for path in exclude}
    for root in roots:
        for directory, _, names in os.walk(root):
            for name in names:
                path = Path(directory) / name
                if path.is_file() and not path.is_symlink() and path.resolve() not in skipped:
                    yield path


def tree_leaks(canaries: Canaries, *roots: Path, exclude: Collection[Path] = ()) -> list[str]:
    """`path: VARIABLE/form` for every file under `roots` that holds a canary form."""
    found: list[str] = []
    for path in files_under(*roots, exclude=exclude):
        found += [f"{path.name}: {hit}" for hit in leaks(path.read_bytes(), canaries)]
    return found


@pytest.mark.parametrize("variable", SECRET_VARS)
def test_the_scanner_finds_every_form_of_every_canary(tmp_path: Path, variable: str) -> None:
    canaries = make_canaries(tmp_path)
    for label, needle in canaries.needles[variable].items():
        assert f"{variable}/{label}" in leaks(b"\x00 before " + needle + b" after \xff", canaries)


@pytest.mark.parametrize("prefix", range(6))
@pytest.mark.parametrize("suffix", range(6))
def test_base64_fragments_match_wherever_the_secret_sits_in_a_larger_blob(prefix: int, suffix: int) -> None:
    secret = secrets.token_bytes(32)
    blob = secrets.token_bytes(prefix) + secret + secrets.token_bytes(suffix)
    fragments = _b64_fragments(secret)
    assert any(fragment in base64.b64encode(blob) for label, fragment in fragments.items() if label.startswith("b64."))
    assert any(fragment in base64.urlsafe_b64encode(blob) for label, fragment in fragments.items() if label.startswith("b64url."))


def test_the_scanner_is_quiet_on_clean_data_and_never_prints_a_value(tmp_path: Path) -> None:
    canaries = make_canaries(tmp_path)
    require_clean(leaks(b'{"status": "ok", "key": "abc"}' * 100, canaries), "clean data")
    state = tmp_path / "state"
    state.mkdir()
    (state / "events.jsonl").write_bytes(b"line\n" + canaries.needles["SWARM_ED25519_KEY"]["utf8"] + b"\n")
    (state / "tasks.db-wal").write_bytes(canaries.needles["KEY_FILE_MARKER"]["hex-of-text"])
    found = tree_leaks(canaries, state)
    assert {"events.jsonl: SWARM_ED25519_KEY/utf8", "tasks.db-wal: KEY_FILE_MARKER/hex-of-text"} <= set(found)
    # Each file reports the forms of the variable planted in it and of no other variable (the prefix and suffix windows overlap the
    # raw form on purpose, so the number of forms that match is not fixed).
    assert {hit.split(": ")[1].split("/")[0] for hit in found} == {"SWARM_ED25519_KEY", "KEY_FILE_MARKER"}
    joined = " ".join(found)
    assert not any(value in joined for value in secret_values(canaries))


@dataclass
class World:
    """A scratch repository with a Task Store holding task T-1 (CREATED), and the directories one run may write to."""

    base: Path
    root: Path
    swarm: Path
    home: Path
    tmp: Path
    claude: Path
    records: Path  # what the PATH shim and the binary wrapper write
    shim: Path
    binary: Path  # the hard link to the real binary; outside every scanned directory
    wal: bool = False  # the store is in WAL mode, as with SWARM_TASKSTORE_WAL=1

    def scanned(self) -> tuple[Path, ...]:
        return (self.root, self.home, self.tmp, self.claude, self.records)


def engine_path() -> Path:
    """Where the binary under test is: `GSD_ENGINE_BIN`, or the debug binary of the workspace."""
    named = os.environ.get("GSD_ENGINE_BIN")
    return Path(named) if named else ROOT / "target" / "debug" / "gsd_engine"


def binary_required() -> bool:
    """Under CI a job that names the binary (`GSD_ENGINE_BIN`) or the build (`GSD_ENGINE_BUILD`) it tests has promised one."""
    return bool(os.environ.get("CI")) and bool(os.environ.get("GSD_ENGINE_BIN") or os.environ.get("GSD_ENGINE_BUILD"))


def _real_binary() -> Path:
    """The binary under test. Without it the real-binary tests skip, naming the reason; where `binary_required` (the rust job of the
    CI workflow, which builds it and names it) they FAIL, so a job that forgot to build it cannot read as green. The `test` job runs
    the whole suite with `CI` set and no binary: it names none, so these tests skip there, as they always did."""
    path = engine_path()
    if not (path.is_file() and os.access(path, os.X_OK)):
        reason = f"no gsd_engine binary at {path}; build it with `cargo build -p gsd-adapter --bin gsd_engine` or set GSD_ENGINE_BIN"
        if binary_required():
            pytest.fail(f"{reason}. CI is set and this job names its binary or build: the real-binary tests must run, not skip")
        pytest.skip(f"{reason} (the rust job of the CI workflow runs these tests)")
    return path


# Directories of a crate that no binary is compiled from: tests, benches and cargo's own output.
NOT_BINARY_SOURCE_DIRS = frozenset({"tests", "benches", "target"})


def newer_sources(binary: Path, root: Path) -> list[str]:
    """The Rust sources under `root/crates` (tests and benches left out) and `Cargo.lock` that are newer than `binary`."""
    built = binary.stat().st_mtime_ns
    candidates = [root / "Cargo.lock"]
    for directory, subdirectories, names in os.walk(root / "crates"):
        subdirectories[:] = [name for name in subdirectories if name not in NOT_BINARY_SOURCE_DIRS]
        candidates += [Path(directory) / name for name in names if name.endswith(".rs")]
    return sorted(str(path.relative_to(root)) for path in candidates if path.is_file() and path.stat().st_mtime_ns > built)


BUILDS = ("default", "sandbox-write")
FAILOVER_NOTICE = b"sandbox fault"


def probe_build(binary: Path, base: Path) -> str:
    """Which build `binary` is, from what it does with a sandbox transition: `sandbox-write` when the write guest answered (ok and no
    failover notice), `default` when it refused with the named E-DEP (no write guest in this build). Anything else fails."""
    world = make_world(base, binary)
    outcome = run_direct(world, make_canaries(base), "transition", "sandbox")
    reply = json.loads(outcome.stdout)
    error = reply.get("error") or {}
    if reply.get("status") == "ok" and FAILOVER_NOTICE not in outcome.stderr:
        return "sandbox-write"
    if error.get("code") == "E-DEP" and "sandbox-write" in error.get("message", "") and (error.get("details") or {}).get("write_state") == "not_started":
        return "default"
    pytest.fail(
        f"cannot tell which build {binary} is: status {reply.get('status')!r}, error code {error.get('code')!r}, "
        f"failover notice {FAILOVER_NOTICE in outcome.stderr} (a failover means the write guest faulted and the host answered)"
    )


@pytest.fixture(scope="session")
def build(tmp_path_factory: pytest.TempPathFactory, record_testsuite_property: Callable[[str, object], None]) -> str:
    """The build of the binary under test (`default` or `sandbox-write`), recorded in the junit file."""
    flavour = probe_build(_real_binary(), tmp_path_factory.mktemp("probe"))
    record_testsuite_property("gsd_engine_build", flavour)
    return flavour


def make_world(base: Path, real: Path | None = None, *, wal: bool = False) -> World:
    world = World(
        base=base,
        root=base / "work",
        swarm=base / "work" / ".swarm",
        home=base / "home",
        tmp=base / "tmp",
        claude=base / "claude-state",
        records=base / "records",
        shim=base / "shim",
        binary=base / "bin" / "gsd_engine",
        wal=wal,
    )
    for directory in (world.root, world.home, world.tmp, world.records, world.shim, world.binary.parent):
        directory.mkdir(parents=True)
    git = shutil.which("git")
    assert git is not None, "git is required: the binary spawns it to find the repository"
    subprocess.run([git, "init", "-q", str(world.root)], check=True, env={"PATH": os.environ["PATH"], "HOME": str(world.home)})
    previous = os.environ.pop("SWARM_TASKSTORE_WAL", None)
    if wal:
        os.environ["SWARM_TASKSTORE_WAL"] = "1"
    try:
        store = TaskStore(world.swarm / "tasks.db")
    finally:
        os.environ.pop("SWARM_TASKSTORE_WAL", None)
        if previous is not None:
            os.environ["SWARM_TASKSTORE_WAL"] = previous
    store.create(task_id="T-1", correlation_id="C-1", capability="code.backend", title="canary task")
    store.conn.close()
    # Only the children of the gsd_engine binary are recorded: the Python selector runs git too, with the whole environment it holds.
    shim = world.shim / "git"
    shim.write_text(
        f'#!/bin/sh\nif [ "$(cat /proc/$PPID/comm)" = "gsd_engine" ]; then cat /proc/$$/environ > "{world.records}/git-env.$$"; fi\n'
        f'exec {git} "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    if real is not None:
        try:
            os.link(real, world.binary)
        except OSError:
            os.symlink(real, world.binary)
    return world


def _wrapper(world: World) -> Path:
    """The binary as the selector starts it: a launcher that records the environment and stdin it was given, then runs the real
    binary on the same stdin and records what the binary answered. Its stdout, stderr and exit code are the binary's."""
    path = world.binary.parent / "gsd_engine_launcher"
    path.write_text(
        f"#!{sys.executable}\n"
        "import subprocess, sys\n"
        "data = sys.stdin.buffer.read()\n"
        f"open({str(world.records / 'binary-stdin.json')!r}, 'wb').write(data)\n"
        f"open({str(world.records / 'binary-env')!r}, 'wb').write(open('/proc/self/environ', 'rb').read())\n"
        f"done = subprocess.run([{str(world.binary)!r}], input=data, capture_output=True, check=False)\n"
        f"open({str(world.records / 'binary-stdout')!r}, 'wb').write(done.stdout)\n"
        f"open({str(world.records / 'binary-stderr')!r}, 'wb').write(done.stderr)\n"
        "sys.stdout.buffer.write(done.stdout)\n"
        "sys.stdout.buffer.flush()\n"
        "sys.stderr.buffer.write(done.stderr)\n"
        "sys.exit(done.returncode)\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


ROUTES = {
    "transition": ["--json", "--transition", "T-1", "VALIDATED", "--reason", "canary"],
    "list": ["--json"],
    "history": ["--json", "--history", "T-1"],
}


@dataclass
class Outcome:
    code: int
    stdout: bytes
    stderr: bytes
    binary_stdout: bytes = b""  # what the binary itself answered, when the launcher recorded it (the selector driver)
    binary_stderr: bytes = b""


def _environ(world: World, canaries: Canaries) -> dict[str, str]:
    return {
        "PATH": f"{world.shim}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(world.home),
        "TMPDIR": str(world.tmp),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(ROOT),
        **canaries.env,
        **({"SWARM_TASKSTORE_WAL": "1"} if world.wal else {}),
    }


def run_selector(world: World, canaries: Canaries, route: str, backend: str, *, argv: list[str] | None = None) -> Outcome:
    """`scripts/orch_status.py` with GSD_ENGINE=rust, the canaries in its environment, and the launcher as the binary. `argv` replaces
    the route's own (the APPROVED cases of section F2 call the `transition` route with another target)."""
    launcher = _wrapper(world)
    env = {
        **_environ(world, canaries),
        "SWARM_GSD_STATE_FILE": "",
        "SWARM_CLAUDE_STATE_DIR": str(world.claude),
        "GSD_ENGINE": "rust",
        "GSD_ENGINE_BIN": str(launcher),
        # the identity check starts a child that holds a key only as the binary it names: the launcher, whose digest is given here
        "GSD_ENGINE_BIN_SHA256": hashlib.sha256(launcher.read_bytes()).hexdigest(),
        "GSD_RUST_BACKEND": backend,
    }
    done = subprocess.run(
        [sys.executable, str(SCRIPT), *(argv or ROUTES[route])], cwd=world.root, env=env, capture_output=True, timeout=120, check=False
    )
    recorded = [world.records / "binary-stdout", world.records / "binary-stderr"]
    answers = [path.read_bytes() if path.exists() else b"" for path in recorded]
    return Outcome(done.returncode, done.stdout, done.stderr, *answers)


def direct_request(world: World, route: str, request_env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The request the selector writes for `route`; `request_env` is its `env` map (the selector itself sends only RUST_ENV_KEYS)."""
    request: dict[str, Any] = {
        "agent": "A01",
        "script": "orch_status",
        "argv": ROUTES[route],
        "root": str(world.root),
        "env": dict(request_env or {}),
    }
    if route == "transition":
        effects = world.tmp / "effects.json"
        effects.write_text("", encoding="utf-8")
        request["effects_path"] = str(effects)
    return request


def run_binary(
    world: World, canaries: Canaries, stdin: bytes, backend: str = "host", *, extra_env: Mapping[str, str] | None = None
) -> Outcome:
    """The binary started the way a script with no selector would: the whole environment, canaries included, `stdin` as the request."""
    env = {**_environ(world, canaries), "GSD_RUST_BACKEND": backend, **(extra_env or {})}
    done = subprocess.run([str(world.binary)], input=stdin, cwd=world.root, env=env, capture_output=True, timeout=120, check=False)
    return Outcome(done.returncode, done.stdout, done.stderr)


def run_direct(world: World, canaries: Canaries, route: str, backend: str, *, request_env: Mapping[str, str] | None = None) -> Outcome:
    """The binary called directly with the request of `route`."""
    return run_binary(world, canaries, json.dumps(direct_request(world, route, request_env)).encode(), backend)


def environ_names(raw: bytes) -> list[str]:
    """Variable names of a /proc/<pid>/environ dump."""
    return [entry.split(b"=", 1)[0].decode(errors="replace") for entry in raw.split(b"\0") if entry]


def environ_values(raw: bytes) -> dict[str, str]:
    """Variables of a /proc/<pid>/environ dump, by name."""
    pairs = (entry.decode(errors="replace").partition("=") for entry in raw.split(b"\0") if entry)
    return {name: value for name, _, value in pairs}


def check_children(world: World, canaries: Canaries, *, spawned: bool) -> None:
    """The `git` children the shim saw got no variable but PATH and HOME, and no canary value."""
    dumps = sorted(world.records.glob("git-env.*"))
    if spawned:
        assert dumps, "no git child was observed: the shim is not on the child's PATH, so this check would be vacuous"
    for dump in dumps:
        raw = dump.read_bytes()
        names = environ_names(raw)
        assert set(names) <= {"PATH", "HOME"}, f"a git child received variables {sorted(set(names) - {'PATH', 'HOME'})}"
        assert not [name for name in names if "KEY" in name]
        require_clean(leaks(raw, canaries), f"the environment of a git child ({dump.name})")


def check_outcome(world: World, canaries: Canaries, outcome: Outcome, route: str, *, build: str = "unknown") -> None:
    """The run answered, and left no canary in what it printed or in any file. `build` is the engine build, named in every message."""
    reply = json.loads(outcome.stdout)
    refused = route == "transition" and reply.get("error", {}).get("details", {}).get("write_state") == "not_started"
    assert reply.get("status") == "ok" or refused, f"the route did not answer: the canary scan below would prove nothing [engine build: {build}]"
    require_clean(leaks(outcome.stdout, canaries), f"stdout [engine build: {build}]")
    require_clean(leaks(outcome.stderr, canaries), f"stderr [engine build: {build}]")
    require_clean(tree_leaks(canaries, *world.scanned()), f"a file [engine build: {build}]")


CASES = [(driver, route, backend) for driver in ("selector", "direct") for route in ROUTES for backend in ("host", "sandbox")]


def task_state(world: World, task_id: str = "T-1") -> str:
    """The state of `task_id` in the scratch Task Store, read on a plain connection."""
    connection = sqlite3.connect(world.swarm / "tasks.db")
    try:
        return str(connection.execute("SELECT state FROM tasks WHERE task_id = ?", (task_id,)).fetchone()[0])
    finally:
        connection.close()


def check_engine_answer(world: World, outcome: Outcome, route: str, backend: str, build: str, driver: str) -> str | None:
    """What the binary itself answered, by build. Returns None when the leg proves a guest answer, otherwise the reason the leg is
    NOT covered (the caller skips it with that reason, so it never reads as a pass).

    - sandbox transition on the default build: E-DEP, write_state not_started, a message that names the `sandbox-write` feature;
      called directly the store is untouched, through the selector Python then answers (the store is VALIDATED);
    - a sandbox leg that failed over (the notice on stderr: the guest faulted before its answer and the host answered): fine on the
      default build, whose read guest need not match its pin yet, but a FAILURE on the sandbox-write build, which must serve;
    - every other leg: status ok and no failover notice; a transition leaves the task VALIDATED.
    """
    answer = outcome.binary_stdout or outcome.stdout
    stderr = outcome.binary_stderr if outcome.binary_stdout else outcome.stderr
    reply = json.loads(answer)
    where = f"[engine build: {build}, {driver}-{route}-{backend}]"
    if route == "transition" and backend == "sandbox" and build == "default":
        error = reply.get("error") or {}
        assert reply.get("status") == "error" and error.get("code") == "E-DEP", f"expected the E-DEP of a build without the write guest {where}"
        assert (error.get("details") or {}).get("write_state") == "not_started", f"the refusal must come before any write {where}"
        assert "sandbox-write" in error.get("message", ""), f"the refusal must name the sandbox-write feature {where}"
        assert task_state(world) == ("VALIDATED" if driver == "selector" else "CREATED"), f"state after the refusal {where}"
        return "the default build has no write guest (E-DEP, nothing written), so no sandbox transition ran; build with --features sandbox-write"
    assert reply.get("status") == "ok", f"the binary did not answer ok: error code {(reply.get('error') or {}).get('code')!r} {where}"
    if route == "transition":
        assert task_state(world) == "VALIDATED", f"the transition did not commit {where}"
    if FAILOVER_NOTICE in stderr:
        assert build == "default", f"the sandbox failed over: the guest did not serve this call {where}"
        return "the sandbox failed over to the host (the guest of the default build did not serve), so the guest was not exercised"
    return None


@pytest.mark.parametrize(("driver", "route", "backend"), CASES, ids=["-".join(case) for case in CASES])
def test_no_canary_survives_a_run_of_the_real_binary(tmp_path: Path, build: str, driver: str, route: str, backend: str) -> None:
    world = make_world(tmp_path, _real_binary())
    canaries = make_canaries(tmp_path)
    outcome = (run_selector if driver == "selector" else run_direct)(world, canaries, route, backend)
    not_covered = check_engine_answer(world, outcome, route, backend, build, driver)
    check_outcome(world, canaries, outcome, route, build=build)
    # A refused sandbox transition (E-DEP, not_started) may spawn no git; every other leg runs the host snapshot code, which does.
    check_children(world, canaries, spawned=not (route == "transition" and backend == "sandbox" and build == "default"))
    if not_covered is not None:
        pytest.skip(f"not covered: {not_covered}")


@pytest.mark.parametrize("driver", ["selector", "direct"])
def test_no_canary_survives_a_transition_on_a_wal_store(tmp_path: Path, build: str, driver: str) -> None:
    """The same scan with the Task Store in WAL mode, so `tasks.db-wal` and `tasks.db-shm` exist and are searched: a reader that stays
    open keeps SQLite from deleting them when the binary's connection closes."""
    world = make_world(tmp_path, _real_binary(), wal=True)
    canaries = make_canaries(tmp_path)
    keeper = sqlite3.connect(world.swarm / "tasks.db")
    try:
        keeper.execute("SELECT count(*) FROM tasks").fetchall()
        outcome = (run_selector if driver == "selector" else run_direct)(world, canaries, "transition", "host")
        assert (world.swarm / "tasks.db-wal").exists() and (world.swarm / "tasks.db-shm").exists(), (
            "no WAL files: the scan would prove nothing"
        )
        check_outcome(world, canaries, outcome, "transition", build=build)
    finally:
        keeper.close()
    check_children(world, canaries, spawned=True)


@pytest.mark.parametrize("route", ROUTES)
def test_the_selector_gives_the_binary_no_key_and_a_request_of_known_shape(tmp_path: Path, route: str) -> None:
    world = make_world(tmp_path, _real_binary())
    canaries = make_canaries(tmp_path)
    run_selector(world, canaries, route, "host")
    environ = (world.records / "binary-env").read_bytes()
    names = environ_names(environ)
    assert "PATH" in names and "GSD_RUST_BACKEND" in names, "the launcher did not record the binary's environment"
    assert not set(names) & set(KEY_VARS)
    assert not [name for name in names if "KEY" in name]
    require_clean(leaks(environ, canaries), "the binary's environment")
    request = json.loads((world.records / "binary-stdin.json").read_bytes())
    expected = {"agent", "script", "argv", "root", "env"} | ({"effects_path"} if route == "transition" else set())
    assert set(request) == expected
    assert set(request["env"]) <= set(selector().RUST_ENV_KEYS)
    require_clean(leaks(json.dumps(request).encode(), canaries), "the binary's request")


def test_a_planted_canary_is_found_in_every_channel_and_the_message_names_no_value(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    canaries = make_canaries(tmp_path)
    ok = Outcome(0, b'{"status": "ok"}', b"")
    check_outcome(world, canaries, ok, "list")
    planted = {
        "stdout": Outcome(0, b'{"status": "ok", "x": "' + canaries.needles["SWARM_SIGNING_KEY"]["utf8"] + b'"}', b""),
        "stderr": Outcome(0, b'{"status": "ok"}', canaries.needles["SWARM_ED25519_KEY"]["hex-of-text"]),
    }
    for channel, outcome in planted.items():
        with pytest.raises(AssertionError) as caught:
            check_outcome(world, canaries, outcome, "list")
        assert channel in str(caught.value)
        assert not any(value in str(caught.value) for value in secret_values(canaries))
    for name, content in {
        "events.jsonl": canaries.needles["SWARM_SIGNING_KEY"]["b64.1"],
        "live.json": canaries.needles["SWARM_ED25519_KEY"]["key-bytes"],
        "tasks.db-wal": canaries.needles["KEY_FILE_MARKER"]["utf8"],
        "tasks.db-shm": canaries.needles["SWARM_ED25519_KEY_FILE"]["utf8"],
    }.items():
        target = world.swarm / name
        target.write_bytes(b"\x00" * 40 + content)
        with pytest.raises(AssertionError):
            check_outcome(world, canaries, ok, "list")
        target.unlink()
    check_outcome(world, canaries, ok, "list")


def test_a_key_variable_in_a_git_child_is_detected_and_the_shim_does_observe(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    canaries = make_canaries(tmp_path)
    # The shim records the environment of a git child of a process named gsd_engine and of no other parent.
    parent = world.shim / "gsd_engine"
    parent.write_text('#!/bin/sh\n"$@"\n', encoding="utf-8")
    parent.chmod(0o755)
    git = str(world.shim / "git")
    leaky = {
        "PATH": f"{world.shim}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(world.home),
        "SWARM_SIGNING_KEY": canaries.env["SWARM_SIGNING_KEY"],
    }
    subprocess.run([git, "--version"], env=leaky, check=True, capture_output=True)
    assert list(world.records.glob("git-env.*")) == []
    # Positive control: as the child of gsd_engine it records what it was given, a key variable included, and the check flags it.
    subprocess.run([str(parent), git, "--version"], env=leaky, check=True, capture_output=True)
    assert len(list(world.records.glob("git-env.*"))) == 1
    with pytest.raises(AssertionError, match="SWARM_SIGNING_KEY"):
        check_children(world, canaries, spawned=True)
    for dump in world.records.glob("git-env.*"):
        dump.unlink()
    with pytest.raises(AssertionError, match="no git child was observed"):
        check_children(world, canaries, spawned=True)
    # The fake parent is a shell and adds PWD to its children; `env -i` gives the git child exactly the two variables of the real one.
    clean = ["env", "-i", f"PATH={leaky['PATH']}", f"HOME={leaky['HOME']}"]
    subprocess.run([str(parent), *clean, git, "--version"], env=leaky, check=True, capture_output=True)
    check_children(world, canaries, spawned=True)


# ---------------------------------------------------------------------------------------------------------------------------------
# C. The selector's own environment
# ---------------------------------------------------------------------------------------------------------------------------------


@functools.cache
def selector() -> ModuleType:
    spec = importlib.util.spec_from_file_location("orch_status_keys_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def assert_no_key(env: Mapping[str, str], canaries: Canaries) -> None:
    assert not [name for name in env if name in KEY_VARS or "KEY" in name], "a key variable is in the environment"
    require_clean(leaks("\0".join(f"{name}={value}" for name, value in env.items()).encode(), canaries), "the environment")


@pytest.fixture
def host_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Canaries:
    """The process environment: the plain variables a run needs, a SWARM_DIR and a GSD_ setting, and every canary."""
    for name in list(os.environ):
        if name in KEY_VARS:
            monkeypatch.delenv(name)
    monkeypatch.setenv("SWARM_DIR", str(tmp_path / ".swarm"))
    monkeypatch.setenv("GSD_RUST_BACKEND", "host")
    return make_canaries(tmp_path)


@pytest.mark.parametrize("variable", KEY_VARS)
def test_the_binary_environment_of_a_route_that_verifies_nothing_drops_each_key_variable(
    host_env: Canaries, monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    monkeypatch.setenv(variable, host_env.env[variable])
    env = selector()._binary_env("transition")
    assert variable not in env
    assert_no_key(env, host_env)
    # Positive controls: the allowlist is not empty, so the absence above is the filter's doing; and a SWARM_ name nobody listed (the
    # swarm directory travels in the request's `env`) is not handed on by prefix.
    assert env["GSD_RUST_BACKEND"] == "host" and "PATH" in env
    assert "SWARM_DIR" not in env, "the binary's environment is an allowlist of names, not a SWARM_ prefix"


def test_the_binary_environment_of_such_a_route_drops_every_key_variable_at_once(host_env: Canaries, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in host_env.env.items():
        monkeypatch.setenv(name, value)
    for route in ("list", "history", "transition"):
        env = selector()._binary_env(route)
        assert_no_key(env, host_env)
        assert {"GSD_RUST_BACKEND", "PATH"} <= set(env)


def test_the_binary_environment_is_exactly_the_allowlist_and_the_routes_grant(host_env: Canaries, monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever the process holds, the binary gets only names of `BINARY_ENV_NAMES` and of the route's grant: not a prefix, not a rename."""
    module = selector()
    monkeypatch.setenv("GSD_ENGINE", "rust")
    monkeypatch.setenv("GSD_FUTURE_SETTING", "x")
    monkeypatch.setenv("SWARM_FUTURE_SETTING", "x")
    monkeypatch.setenv("SWARM_TASKSTORE_WAL", "1")
    monkeypatch.setenv("UNRELATED_TOKEN", "x")
    for name, value in host_env.env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("SWARM_AGENT_SESSION", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/x")
    for route, grant in module.ROUTE_KEY_ENV.items():
        env = module._binary_env(route)
        assert set(env) <= set(module.BINARY_ENV_NAMES) | set(grant), f"route {route}: {sorted(set(env) - set(module.BINARY_ENV_NAMES) - set(grant))}"
    with pytest.raises(KeyError):
        module._binary_env("a-route-nobody-listed")


def test_the_selector_tables_name_no_key_variable_outside_the_approved_route() -> None:
    module = selector()
    assert set(KEY_VARS) <= set(module.SIGNING_KEY_ENV), "a key variable of the host is missing from SIGNING_KEY_ENV"
    assert not [name for name in module.RUST_ENV_KEYS if name in KEY_VARS or "KEY" in name]
    assert not [name for name in module.BINARY_ENV_NAMES if "KEY" in name or name in module.APPROVED_KEY_ENV]
    assert {route: set(grant) & set(KEY_VARS) for route, grant in module.ROUTE_KEY_ENV.items()} == {
        route: set(allowed) for route, allowed in ROUTE_KEY_POLICY.items()
    }, "ROUTE_KEY_ENV of the selector and ROUTE_KEY_POLICY of this file must change together"


APPROVED_GRANT_NAMES = (*KEY_VARS, "SWARM_AGENT_SESSION", "XDG_CONFIG_HOME")
APPROVED_GRANT_LINE = 'APPROVED_KEY_ENV = SIGNING_KEY_ENV | {"SWARM_AGENT_SESSION", "XDG_CONFIG_HOME"}'


def approved_grant_gaps(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The names of `APPROVED_GRANT_NAMES` the process has and the selector does NOT hand the APPROVED child, and the routes that are handed
    one of them although they are not APPROVED. `XDG_CONFIG_HOME` is what lets the binary see the conventional key file Python would load:
    a binary without it would treat the dev key as active and approve rows Python refuses."""
    for name in APPROVED_GRANT_NAMES:
        monkeypatch.setenv(name, f"value-of-{name}")
    approved = binary_env_for(module, "transition-approved")
    gaps = [name for name in APPROVED_GRANT_NAMES if approved.get(name) != f"value-of-{name}"]
    for route in ("list", "history", "transition"):
        gaps += [f"{route} is handed {name}" for name in APPROVED_GRANT_NAMES if name in binary_env_for(module, route)]
    return gaps


def test_the_approved_route_is_handed_every_name_its_key_policy_reads_and_no_other_route_is(monkeypatch: pytest.MonkeyPatch) -> None:
    assert approved_grant_gaps(selector(), monkeypatch) == []


@pytest.mark.parametrize(
    ("dropped", "mutated_line"),
    [
        ("XDG_CONFIG_HOME", 'APPROVED_KEY_ENV = SIGNING_KEY_ENV | {"SWARM_AGENT_SESSION"}'),
        ("SWARM_AGENT_SESSION", 'APPROVED_KEY_ENV = SIGNING_KEY_ENV | {"XDG_CONFIG_HOME"}'),
        ("SWARM_ED25519_KEY_FILE", 'APPROVED_KEY_ENV = (SIGNING_KEY_ENV - {"SWARM_ED25519_KEY_FILE"}) | {"SWARM_AGENT_SESSION", "XDG_CONFIG_HOME"}'),
    ],
)
def test_the_approved_grant_pin_has_teeth(dropped: str, mutated_line: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    original = SCRIPT.read_text(encoding="utf-8")
    assert APPROVED_GRANT_LINE in original, "the line this mutation changes moved: update it with `APPROVED_KEY_ENV`"
    mutated = tmp_path / "orch_status_grant.py"
    mutated.write_text(original.replace(APPROVED_GRANT_LINE, mutated_line), encoding="utf-8")
    assert approved_grant_gaps(load_selector(mutated, "orch_status_grant"), monkeypatch) == [dropped]


def test_the_environment_check_has_teeth(host_env: Canaries) -> None:
    for variable in KEY_VARS:
        with pytest.raises(AssertionError):
            assert_no_key({"PATH": "/bin", variable: host_env.env[variable]}, host_env)
    with pytest.raises(AssertionError):
        assert_no_key({"PATH": "/bin", "SWARM_RENAMED": host_env.env["SWARM_SIGNING_KEY"]}, host_env)
    with pytest.raises(AssertionError):
        assert_no_key({"PATH": "/bin", "SWARM_RENAMED": host_env.needles["SWARM_ED25519_KEY"]["b64.2"].decode()}, host_env)
    assert_no_key({"PATH": "/bin", "SWARM_DIR": "/s"}, host_env)


# ---------------------------------------------------------------------------------------------------------------------------------
# D. The binary under test: CI presence, staleness, build (R1, R3)
# ---------------------------------------------------------------------------------------------------------------------------------


def test_the_binary_is_present_when_CI() -> None:
    """Under CI the binary a job promised must exist: this is the named failure of a job that never built it."""
    if not binary_required():
        pytest.skip("CI is not set, or this job names no binary or build: a missing binary skips the real-binary tests")
    path = engine_path()
    assert path.is_file() and os.access(path, os.X_OK), f"no executable gsd_engine at {path}: build it before the pytest step"


@pytest.mark.parametrize(
    ("ci", "binary", "declared_build", "outcome"),
    [
        ("true", "absent", False, pytest.fail.Exception),
        ("true", "not-executable", False, pytest.fail.Exception),
        ("true", "default-path", True, pytest.fail.Exception),
        ("true", "default-path", False, pytest.skip.Exception),  # the `test` job: CI set, no binary named
        (None, "absent", False, pytest.skip.Exception),
        (None, "not-executable", False, pytest.skip.Exception),
        (None, "default-path", True, pytest.skip.Exception),
    ],
)
def test_a_missing_binary_fails_where_a_job_promised_one_and_skips_otherwise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ci: str | None, binary: str, declared_build: bool, outcome: type[BaseException]
) -> None:
    target = tmp_path / "gsd_engine"
    if binary == "not-executable":
        target.write_text("#!/bin/sh\n", encoding="utf-8")
        target.chmod(0o644)
    if binary == "default-path":
        monkeypatch.delenv("GSD_ENGINE_BIN", raising=False)
        monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)  # no target/debug/gsd_engine below it
    else:
        monkeypatch.setenv("GSD_ENGINE_BIN", str(target))
    if declared_build:
        monkeypatch.setenv("GSD_ENGINE_BUILD", "default")
    else:
        monkeypatch.delenv("GSD_ENGINE_BUILD", raising=False)
    if ci is None:
        monkeypatch.delenv("CI", raising=False)
    else:
        monkeypatch.setenv("CI", ci)
    with pytest.raises(outcome):
        _real_binary()


def test_the_binary_is_not_older_than_the_sources() -> None:
    binary = _real_binary()
    stale = newer_sources(binary, ROOT)
    assert not stale, f"{binary} is older than {len(stale)} Rust source(s), e.g. {stale[:5]}: rebuild it before testing"


@pytest.mark.parametrize(
    ("changed", "reported"),
    [
        ("crates/a/src/lib.rs", True),
        ("crates/a/src/deep/mod.rs", True),
        ("crates/a/build.rs", True),
        ("Cargo.lock", True),
        ("crates/a/tests/t.rs", False),
        ("crates/a/benches/b.rs", False),
        ("crates/a/target/x.rs", False),
        ("crates/a/src/data.json", False),
    ],
)
def test_the_staleness_guard_reports_a_newer_source_and_nothing_else(tmp_path: Path, changed: str, reported: bool) -> None:
    names = [
        "crates/a/src/lib.rs",
        "crates/a/src/deep/mod.rs",
        "crates/a/build.rs",
        "Cargo.lock",
        "crates/a/tests/t.rs",
        "crates/a/benches/b.rs",
        "crates/a/target/x.rs",
        "crates/a/src/data.json",
    ]
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
        os.utime(path, ns=(500_000_000_000, 500_000_000_000))
    binary = tmp_path / "gsd_engine"
    binary.write_text("x", encoding="utf-8")
    os.utime(binary, ns=(1_000_000_000_000, 1_000_000_000_000))
    assert newer_sources(binary, tmp_path) == []
    os.utime(tmp_path / changed, ns=(2_000_000_000_000, 2_000_000_000_000))
    assert newer_sources(binary, tmp_path) == ([changed] if reported else [])


def test_the_build_under_test_is_the_recorded_one_and_the_one_declared(build: str) -> None:
    """The build is a recorded fact (the `gsd_engine_build` property of the junit file, from the `build` fixture), it is one of the
    two builds, and it is the one the job declared with GSD_ENGINE_BUILD."""
    assert build in BUILDS
    declared = os.environ.get("GSD_ENGINE_BUILD")
    assert declared in (None, build), f"GSD_ENGINE_BUILD declares the {declared!r} build but the binary is a {build!r} build"


def fake_engine(path: Path, stdout: dict[str, Any], stderr: str = "", code: int = 0) -> Path:
    """An executable that reads its request and answers `stdout` and `stderr` with exit code `code`, standing in for another build."""
    path.write_text(
        f"#!{sys.executable}\nimport sys\nsys.stdin.read()\nsys.stdout.write({json.dumps(stdout)!r})\nsys.stderr.write({stderr!r})\n"
        f"sys.exit({code})\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


DEFAULT_REFUSAL: dict[str, Any] = {
    "status": "error",
    "error": {"code": "E-DEP", "message": "E-DEP: no write guest in this build (rebuild with the sandbox-write feature)", "details": {"write_state": "not_started"}},
}
FAILOVER_LINE = "gsd_engine: sandbox fault E-DEP; answered by host Rust\n"


@pytest.mark.parametrize(
    ("stdout", "stderr", "flavour"),
    [
        (DEFAULT_REFUSAL, "", "default"),
        ({"status": "ok", "task": {}}, "", "sandbox-write"),
        ({"status": "ok", "task": {}}, FAILOVER_LINE, None),
        ({**DEFAULT_REFUSAL, "error": {**DEFAULT_REFUSAL["error"], "message": "E-DEP: something else"}}, "", None),
        ({**DEFAULT_REFUSAL, "error": {**DEFAULT_REFUSAL["error"], "details": {}}}, "", None),
        ({"status": "error", "error": {"code": "E-INTERNAL", "message": "x", "details": {}}}, "", None),
    ],
    ids=["named-refusal", "guest-answered", "failover-is-not-the-guest", "unnamed-refusal", "refusal-without-not-started", "other-error"],
)
def test_the_build_probe_tells_the_builds_apart_and_refuses_to_guess(tmp_path: Path, stdout: dict[str, Any], stderr: str, flavour: str | None) -> None:
    engine = fake_engine(tmp_path / "fake_engine", stdout, stderr)
    if flavour is None:
        with pytest.raises(pytest.fail.Exception):
            probe_build(engine, tmp_path / "probe")
    else:
        assert probe_build(engine, tmp_path / "probe") == flavour


def test_the_engine_answer_check_is_strict_about_the_build(tmp_path: Path) -> None:
    world = make_world(tmp_path / "w")
    refusal = Outcome(2, json.dumps(DEFAULT_REFUSAL).encode(), b"")
    answered = Outcome(0, b'{"status": "ok"}', b"")
    failed_over = Outcome(0, b'{"status": "ok"}', FAILOVER_LINE.encode())
    # The named refusal of the default build is accepted, and reported as "not covered" (the caller then skips the leg).
    reason = check_engine_answer(world, refusal, "transition", "sandbox", "default", "direct")
    assert reason is not None and "no write guest" in reason
    # An ok answer where the default build must refuse, or the refusal where the write build must answer, is not accepted.
    with pytest.raises(AssertionError, match="expected the E-DEP"):
        check_engine_answer(world, answered, "transition", "sandbox", "default", "direct")
    with pytest.raises(AssertionError, match="did not answer ok"):
        check_engine_answer(world, refusal, "transition", "sandbox", "sandbox-write", "direct")
    # An ok answer of a transition needs the commit: the scratch store is still CREATED.
    with pytest.raises(AssertionError, match="did not commit"):
        check_engine_answer(world, answered, "transition", "sandbox", "sandbox-write", "direct")
    # A failover is a failure on the build that must serve, and a leg that is not covered on the one whose read guest may not.
    with pytest.raises(AssertionError, match="failed over"):
        check_engine_answer(world, failed_over, "list", "sandbox", "sandbox-write", "direct")
    reason = check_engine_answer(world, failed_over, "list", "sandbox", "default", "direct")
    assert reason is not None and "failed over" in reason
    assert check_engine_answer(world, answered, "list", "sandbox", "sandbox-write", "direct") is None
    assert check_engine_answer(world, answered, "list", "host", "default", "direct") is None


# ---------------------------------------------------------------------------------------------------------------------------------
# E. The scanner's widened forms (R6)
# ---------------------------------------------------------------------------------------------------------------------------------


def test_the_byte_forms_have_the_shapes_a_dump_takes() -> None:
    table = forms("0cff")
    assert table["key-dec-list"] == b"12, 255"
    assert table["key-dec-list-tight"] == b"12,255"
    assert table["key-x-escapes"] == rb"\x0c\xff"
    assert table["key-0x-hex"] == b"0x0cff"
    assert table["key-0x-list"] == b"0x0c, 0xff"
    assert table["key-hex-spaced"] == b"0c ff"
    assert table["key-HEX-spaced"] == b"0C FF"
    assert table["key-bytes-repr"] == repr(b"\x0c\xff")[2:-1].encode()
    assert table["text-dec-list"] == b"48, 99, 102, 102"
    assert table["utf16le"] == b"0\x00c\x00f\x00f\x00"
    assert table["utf16be"] == b"\x000\x00c\x00f\x00f"


def test_a_key_has_prefix_and_suffix_windows_and_a_short_marker_or_path_has_none() -> None:
    key = secrets.token_hex(32)
    table = forms(key)
    assert table["key-prefix16"] == key[:16].encode() and table["key-suffix16"] == key[-16:].encode()
    assert table["key-PREFIX16"] == key[:16].upper().encode() and table["key-SUFFIX16"] == key[-16:].upper().encode()
    assert table["key-first8-bytes"] == bytes.fromhex(key)[:8] and table["key-last8-bytes"] == bytes.fromhex(key)[-8:]
    marker = forms(secrets.token_hex(12))
    assert not [label for label in marker if "prefix" in label.lower() or "suffix" in label.lower() or "8-bytes" in label]
    path = forms("/some/where/ed25519.pem")
    assert not [label for label in path if label.startswith("key-")]


WIDENED_TEXT = {"utf16be", "text-dec-list", "text-dec-list-tight", "text-x-escapes", "text-0x-hex", "text-0x-list", "text-hex-spaced", "text-HEX-spaced"}
WIDENED_KEY = {
    "key-dec-list", "key-dec-list-tight", "key-x-escapes", "key-0x-hex", "key-0x-list", "key-hex-spaced", "key-HEX-spaced", "key-bytes-repr",
    "key-prefix16", "key-suffix16", "key-PREFIX16", "key-SUFFIX16", "key-first8-bytes", "key-last8-bytes",
}  # fmt: skip


@pytest.mark.parametrize("variable", SECRET_VARS)
def test_the_canary_tables_hold_the_widened_forms(tmp_path: Path, variable: str) -> None:
    labels = set(make_canaries(tmp_path).needles[variable])
    assert WIDENED_TEXT <= labels
    assert (WIDENED_KEY <= labels) == (variable != "SWARM_ED25519_KEY_FILE")


# ---------------------------------------------------------------------------------------------------------------------------------
# F. Error legs, a hostile request env, key files (R7, R8, R10)
# ---------------------------------------------------------------------------------------------------------------------------------

BACKTRACE = {"RUST_BACKTRACE": "1", "RUST_LIB_BACKTRACE": "1"}


def _request(world: World, argv: list[str], **extra: Any) -> bytes:
    """The stdin object of a call with an empty `env` map, plus `extra` members."""
    return json.dumps({"agent": "A01", "script": "orch_status", "argv": argv, "root": str(world.root), "env": {}, **extra}).encode()


def _transition_request(world: World) -> bytes:
    effects = world.tmp / "effects.json"
    effects.write_text("", encoding="utf-8")
    return _request(world, ROUTES["transition"], effects_path=str(effects))


def _corrupt_store(world: World) -> None:
    (world.swarm / "tasks.db").write_bytes(b"this is not a SQLite database\n" * 400)


def _remove_swarm_dir(world: World) -> None:
    shutil.rmtree(world.swarm)


def _no_preparation(world: World) -> None:
    return None


@dataclass(frozen=True)
class ErrorLeg:
    """One way a call can fail, or be unusual: the stdin it sends, how the scratch world is broken first, and the typed answer."""

    stdin: Callable[[World], bytes]
    expected: str  # the error code of the answer, or "ok"
    prepare: Callable[[World], None] = _no_preparation


ERROR_LEGS = {
    "invalid-json": ErrorLeg(lambda world: b'{"agent": ', "E-INPUT"),
    "not-an-object": ErrorLeg(lambda world: b"[1, 2]", "E-INPUT"),
    "invalid-utf8": ErrorLeg(lambda world: b'{"agent": "\xff\xfe"}', "E-INPUT"),
    "oversize-input-flag": ErrorLeg(lambda world: _request(world, ["--json", "--input", "x" * 100_001]), "E-DEP"),
    "oversize-stdin": ErrorLeg(lambda world: _request(world, ROUTES["list"], padding="y" * 3_000_000), "ok"),
    "corrupt-store-transition": ErrorLeg(_transition_request, "E-DEP", _corrupt_store),
    "corrupt-store-list": ErrorLeg(lambda world: _request(world, ROUTES["list"]), "E-INTERNAL", _corrupt_store),
    "missing-swarm-dir-list": ErrorLeg(lambda world: _request(world, ROUTES["list"]), "ok", _remove_swarm_dir),
    "missing-swarm-dir-transition": ErrorLeg(_transition_request, "E-DEP", _remove_swarm_dir),
}


def check_error_leg(world: World, canaries: Canaries, outcome: Outcome, leg: str, build: str) -> None:
    """The call got its typed answer (exit code 0 for ok, 2 for an error reply), no panic text, and no canary in stdout, stderr or any file."""
    expected = ERROR_LEGS[leg].expected
    where = f"[engine build: {build}, leg: {leg}]"
    reply = json.loads(outcome.stdout)
    if expected == "ok":
        assert reply.get("status") == "ok" and outcome.code == 0, f"expected an ok answer {where}"
    else:
        assert reply.get("status") == "error" and outcome.code == 2, f"expected an error reply with exit code 2 {where}"
        assert (reply.get("error") or {}).get("code") == expected, f"expected the error code {expected} {where}"
    assert b"panicked" not in outcome.stderr, f"the binary panicked {where}"
    require_clean(leaks(outcome.stdout, canaries), f"stdout {where}")
    require_clean(leaks(outcome.stderr, canaries), f"stderr {where}")
    require_clean(tree_leaks(canaries, *world.scanned()), f"a file {where}")


@pytest.mark.parametrize("leg", ERROR_LEGS)
def test_an_error_leaves_no_canary_even_with_a_backtrace(tmp_path: Path, build: str, leg: str) -> None:
    world = make_world(tmp_path, _real_binary())
    canaries = make_canaries(tmp_path)
    ERROR_LEGS[leg].prepare(world)
    outcome = run_binary(world, canaries, ERROR_LEGS[leg].stdin(world), "host", extra_env=BACKTRACE)
    check_error_leg(world, canaries, outcome, leg, build)


def test_a_locked_store_leaves_no_canary_even_with_a_backtrace(tmp_path: Path, build: str) -> None:
    """A transition waits for the write lock for the busy timeout of 30 s, then answers a typed error (nothing written)."""
    world = make_world(tmp_path, _real_binary())
    canaries = make_canaries(tmp_path)
    holder = sqlite3.connect(world.swarm / "tasks.db", isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        outcome = run_binary(world, canaries, _transition_request(world), "host", extra_env=BACKTRACE)
    finally:
        holder.close()
    reply = json.loads(outcome.stdout)
    assert reply["error"]["code"] == "E-INTERNAL" and reply["error"]["details"]["write_state"] == "not_started", f"[engine build: {build}]"
    assert outcome.code == 2 and b"panicked" not in outcome.stderr
    assert task_state(world) == "CREATED"
    require_clean(leaks(outcome.stdout, canaries), f"stdout [engine build: {build}]")
    require_clean(leaks(outcome.stderr, canaries), f"stderr [engine build: {build}]")
    require_clean(tree_leaks(canaries, *world.scanned()), f"a file [engine build: {build}]")


LEAKS = {
    "message": ("import json, os, sys\nsys.stdin.read()\nprint(json.dumps({'status': 'error', 'error': {'code': 'E-INPUT', 'message': os.environ['SWARM_SIGNING_KEY']}}))\n"
                "sys.exit(2)\n"),
    "stderr-hex": ("import json, os, sys\nsys.stdin.read()\nprint(json.dumps({'status': 'error', 'error': {'code': 'E-INPUT'}}))\n"
                   "sys.stderr.write('aborted at ' + os.environ['SWARM_ED25519_KEY'].encode().hex())\nsys.exit(2)\n"),
    "file": ("import json, os, sys\nsys.stdin.read()\nopen(os.environ['TMPDIR'] + '/dump.bin', 'wb').write(os.environ['SWARM_SIGNING_KEY'].encode('utf-16-be'))\n"
             "print(json.dumps({'status': 'error', 'error': {'code': 'E-INPUT'}}))\nsys.exit(2)\n"),
    "decimal-list-on-stderr": ("import json, os, sys\nsys.stdin.read()\nprint(json.dumps({'status': 'error', 'error': {'code': 'E-INPUT'}}))\n"
                               "sys.stderr.write(str(list(bytes.fromhex(os.environ['SWARM_ED25519_KEY']))))\nsys.exit(2)\n"),
}
CLEAN = "import json, sys\nsys.stdin.read()\nprint(json.dumps({'status': 'error', 'error': {'code': 'E-INPUT', 'message': 'bad request'}}))\nsys.exit(2)\n"
PANIC = "import json, sys\nsys.stdin.read()\nprint(json.dumps({'status': 'error', 'error': {'code': 'E-INPUT'}}))\nsys.stderr.write('thread main panicked at src/x.rs')\nsys.exit(2)\n"


def test_the_error_leg_check_detects_a_panic(tmp_path: Path) -> None:
    engine = scripted_engine(tmp_path / "engine", PANIC)
    world = make_world(tmp_path / "w", engine)
    canaries = make_canaries(tmp_path)
    outcome = run_binary(world, canaries, ERROR_LEGS["invalid-json"].stdin(world), "host", extra_env=BACKTRACE)
    with pytest.raises(AssertionError, match="the binary panicked"):
        check_error_leg(world, canaries, outcome, "invalid-json", "scratch")


def scripted_engine(path: Path, source: str) -> Path:
    path.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.mark.parametrize("channel", [*LEAKS, "none"])
def test_the_error_leg_check_detects_a_leak_in_each_channel(tmp_path: Path, channel: str) -> None:
    engine = scripted_engine(tmp_path / "engine", CLEAN if channel == "none" else LEAKS[channel])
    world = make_world(tmp_path / "w", engine)
    canaries = make_canaries(tmp_path)
    outcome = run_binary(world, canaries, ERROR_LEGS["invalid-json"].stdin(world), "host", extra_env=BACKTRACE)
    if channel == "none":
        check_error_leg(world, canaries, outcome, "invalid-json", "scratch")
        return
    with pytest.raises(AssertionError) as caught:
        check_error_leg(world, canaries, outcome, "invalid-json", "scratch")
    assert "scratch" in str(caught.value) and "invalid-json" in str(caught.value)
    assert not any(value in str(caught.value) for value in secret_values(canaries))


def seed_store(swarm: Path, task_id: str) -> None:
    """A Task Store with one CREATED task, built by the Python store."""
    store = TaskStore(swarm / "tasks.db")
    store.create(task_id=task_id, correlation_id="C-ALT", capability="code.backend", title="alternate task")
    store.conn.close()


def hostile_request(world: World, hostile: Canaries, route: str) -> tuple[bytes, Path]:
    """A request whose `env` map holds every key variable (hostile canaries) and a SWARM_DIR that names an alternate store with the
    one task T-ALT; the transition is of T-ALT. Returns the stdin bytes and the alternate swarm directory."""
    alternate = world.tmp / "alternate" / ".swarm"
    alternate.mkdir(parents=True)
    seed_store(alternate, "T-ALT")
    request = direct_request(world, route, {**hostile.env, "SWARM_DIR": str(alternate)})
    if route == "transition":
        request["argv"] = ["--json", "--transition", "T-ALT", "VALIDATED", "--reason", "canary"]
    return json.dumps(request).encode(), alternate


def check_hostile_run(
    world: World, canaries: Canaries, hostile: Canaries, alternate: Path, outcome: Outcome, route: str, where: str
) -> None:
    """The call answered ok from the ALTERNATE store (so its allowlisted SWARM_DIR was used, and the key entries came in a request that
    was read and dropped, not one that was refused), and no canary of the process or of the request is in anything it left."""
    reply = json.loads(outcome.stdout)
    assert reply.get("status") == "ok", f"the call did not answer ok {where}"
    if route == "list":
        assert {task["task_id"] for task in reply["tasks"]} == {"T-ALT"}, f"SWARM_DIR of the request was not used {where}"
    else:
        assert reply["task"]["task_id"] == "T-ALT" and task_state(world) == "CREATED", f"SWARM_DIR of the request was not used {where}"
        connection = sqlite3.connect(alternate / "tasks.db")
        try:
            assert connection.execute("SELECT state FROM tasks WHERE task_id = 'T-ALT'").fetchone() == ("VALIDATED",)
        finally:
            connection.close()
    everything = Canaries(
        {**canaries.env, **hostile.env},
        {**canaries.needles, **{f"hostile.{name}": table for name, table in hostile.needles.items()}},
    )
    require_clean(leaks(outcome.stdout, everything), f"stdout {where}")
    require_clean(leaks(outcome.stderr, everything), f"stderr {where}")
    require_clean(tree_leaks(everything, *world.scanned()), f"a file {where}")
    # With SWARM_DIR given the binary needs no git to find the store, so no child is required; any it starts is still checked.
    check_children(world, everything, spawned=False)


@pytest.mark.parametrize("route", ["list", "transition"])
def test_a_hostile_request_env_is_dropped_and_only_the_swarm_dir_is_used(tmp_path: Path, build: str, route: str) -> None:
    world = make_world(tmp_path, _real_binary())
    canaries = make_canaries(tmp_path)
    hostile = make_canaries(tmp_path / "hostile")
    stdin, alternate = hostile_request(world, hostile, route)
    outcome = run_binary(world, canaries, stdin, "host")
    check_hostile_run(world, canaries, hostile, alternate, outcome, route, f"[engine build: {build}, route: {route}]")


HOSTILE_ENGINES = {
    "echoes-the-request-key": (
        "import json, sys\nenv = json.load(sys.stdin)['env']\n"
        "print(json.dumps({'status': 'ok', 'tasks': [{'task_id': 'T-ALT'}], 'note': env['SWARM_SIGNING_KEY']}))\n",
        "canary in stdout",
    ),
    "writes-the-request-key-to-a-file": (
        "import json, os, sys\nenv = json.load(sys.stdin)['env']\nopen(os.environ['TMPDIR'] + '/env.json', 'w').write(json.dumps(env))\n"
        "print(json.dumps({'status': 'ok', 'tasks': [{'task_id': 'T-ALT'}]}))\n",
        "canary in a file",
    ),
    "ignores-the-swarm-dir": (
        "import json, sys\nsys.stdin.read()\nprint(json.dumps({'status': 'ok', 'tasks': [{'task_id': 'T-1'}]}))\n",
        "SWARM_DIR of the request was not used",
    ),
    "drops-the-keys-and-uses-the-swarm-dir": (
        "import json, sys\nsys.stdin.read()\nprint(json.dumps({'status': 'ok', 'tasks': [{'task_id': 'T-ALT'}]}))\n",
        None,
    ),
}


@pytest.mark.parametrize("engine", HOSTILE_ENGINES)
def test_the_hostile_env_check_detects_a_binary_that_passes_the_request_keys_on(tmp_path: Path, engine: str) -> None:
    source, failure = HOSTILE_ENGINES[engine]
    world = make_world(tmp_path / "w", scripted_engine(tmp_path / "engine", source))
    canaries = make_canaries(tmp_path)
    hostile = make_canaries(tmp_path / "hostile")
    stdin, alternate = hostile_request(world, hostile, "list")
    outcome = run_binary(world, canaries, stdin, "host")
    if failure is None:
        check_hostile_run(world, canaries, hostile, alternate, outcome, "list", "[scratch]")
        return
    with pytest.raises(AssertionError, match=failure) as caught:
        check_hostile_run(world, canaries, hostile, alternate, outcome, "list", "[scratch]")
    assert not any(value in str(caught.value) for value in secret_values(hostile))


def plant_key_files(world: World, canaries: Canaries, *, via: str) -> tuple[Canaries, list[Path]]:
    """Canary CONTENT in the file `SWARM_ED25519_KEY_FILE` names and in `$HOME/.config/agent-swarm/signing-key.hex`.

    `via` is the one the run is given: `file-variable` sets only SWARM_ED25519_KEY_FILE, `conventional` sets no key variable at all
    (so a reader that consults key files would reach the conventional one). Returns the run's canaries (the content forms, and the
    variables the run is given) and the planted files, which a scan of the scratch tree must skip.
    """
    content = secrets.token_hex(32)
    named = Path(canaries.env["SWARM_ED25519_KEY_FILE"])
    conventional = world.home / ".config" / "agent-swarm" / "signing-key.hex"
    planted = [named, conventional]
    for path in planted:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content + "\n", encoding="utf-8")
    given = {"SWARM_ED25519_KEY_FILE": str(named)} if via == "file-variable" else {}
    return Canaries(given, {"KEY_FILE_CONTENT": forms(content)}), planted


def check_key_file_run(world: World, canaries: Canaries, planted: list[Path], outcome: Outcome, where: str) -> None:
    """An APPROVED transition was refused before any write (typed E-DEP, write_state not_started, task still IN_REVIEW), the planted
    files are untouched, and their content is in nothing the run printed or wrote."""
    reply = json.loads(outcome.stdout)
    error = reply.get("error") or {}
    assert reply.get("status") == "error" and error.get("code") == "E-DEP" and outcome.code == 2, f"expected the E-DEP refusal {where}"
    assert (error.get("details") or {}).get("write_state") == "not_started", f"the refusal must come before any write {where}"
    assert task_state(world) == "IN_REVIEW", f"the task moved {where}"
    require_clean(leaks(outcome.stdout, canaries), f"stdout {where}")
    require_clean(leaks(outcome.stderr, canaries), f"stderr {where}")
    require_clean(tree_leaks(canaries, *world.scanned(), exclude=planted), f"a file {where}")
    for path in planted:
        assert path.is_file() and len(path.read_bytes().strip()) == 64, f"a planted key file changed {where}"


def approved_request(world: World) -> bytes:
    """`--transition T-1 APPROVED` for the scratch task, which is first put into IN_REVIEW (the state APPROVED is legal from)."""
    connection = sqlite3.connect(world.swarm / "tasks.db")
    try:
        connection.execute("UPDATE tasks SET state = 'IN_REVIEW' WHERE task_id = 'T-1'")
        connection.commit()
    finally:
        connection.close()
    request = direct_request(world, "transition")
    request["argv"] = ["--json", "--transition", "T-1", "APPROVED", "--reason", "canary"]
    return json.dumps(request).encode()


@pytest.mark.parametrize("backend", ["host", "sandbox"])
@pytest.mark.parametrize("via", ["file-variable", "conventional"])
def test_key_file_content_is_never_read_into_a_run_that_is_refused(tmp_path: Path, build: str, via: str, backend: str) -> None:
    world = make_world(tmp_path, _real_binary())
    run_canaries, planted = plant_key_files(world, make_canaries(tmp_path), via=via)
    outcome = run_binary(world, run_canaries, approved_request(world), backend, extra_env=BACKTRACE)
    check_key_file_run(world, run_canaries, planted, outcome, f"[engine build: {build}, {via}, {backend}]")


KEY_FILE_LEAKS = {
    "stdout": "import json, os, sys\nsys.stdin.read()\n"
              "print(json.dumps({'status': 'error', 'error': {'code': 'E-DEP', 'message': open(os.environ['SWARM_ED25519_KEY_FILE']).read(), 'details': {'write_state': 'not_started'}}}))\n"
              "sys.exit(2)\n",
    "file": "import json, os, sys\nsys.stdin.read()\nopen(os.environ['TMPDIR'] + '/copy', 'w').write(open(os.environ['SWARM_ED25519_KEY_FILE']).read())\n"
            "print(json.dumps({'status': 'error', 'error': {'code': 'E-DEP', 'details': {'write_state': 'not_started'}}}))\nsys.exit(2)\n",
    "stderr": "import json, os, sys\nsys.stdin.read()\nsys.stderr.write(str(list(bytes.fromhex(open(os.environ['SWARM_ED25519_KEY_FILE']).read().strip()))))\n"
              "print(json.dumps({'status': 'error', 'error': {'code': 'E-DEP', 'details': {'write_state': 'not_started'}}}))\nsys.exit(2)\n",
}


@pytest.mark.parametrize("channel", KEY_FILE_LEAKS)
def test_the_key_file_check_detects_a_run_that_reads_the_file_into_each_channel(tmp_path: Path, channel: str) -> None:
    engine = scripted_engine(tmp_path / "engine", KEY_FILE_LEAKS[channel])
    world = make_world(tmp_path / "w", engine)
    run_canaries, planted = plant_key_files(world, make_canaries(tmp_path), via="file-variable")
    outcome = run_binary(world, run_canaries, approved_request(world), "host")
    with pytest.raises(AssertionError, match="KEY_FILE_CONTENT"):
        check_key_file_run(world, run_canaries, planted, outcome, "[scratch]")


def test_the_key_file_check_wants_a_typed_refusal_and_an_untouched_task(tmp_path: Path) -> None:
    ok = scripted_engine(tmp_path / "ok", "import sys\nsys.stdin.read()\nprint('{\"status\": \"ok\"}')\n")
    world = make_world(tmp_path / "w", ok)
    run_canaries, planted = plant_key_files(world, make_canaries(tmp_path), via="conventional")
    with pytest.raises(AssertionError, match="expected the E-DEP refusal"):
        check_key_file_run(world, run_canaries, planted, run_binary(world, run_canaries, approved_request(world)), "[scratch]")
    refused = fake_engine(tmp_path / "refused", DEFAULT_REFUSAL, code=2)
    other = make_world(tmp_path / "x", refused)
    other_canaries, other_planted = plant_key_files(other, make_canaries(tmp_path / "x"), via="conventional")
    outcome = run_binary(other, other_canaries, approved_request(other))
    check_key_file_run(other, other_canaries, other_planted, outcome, "[scratch]")
    other_planted[0].write_text("changed", encoding="utf-8")
    with pytest.raises(AssertionError, match="a planted key file changed"):
        check_key_file_run(other, other_canaries, other_planted, outcome, "[scratch]")


# ---------------------------------------------------------------------------------------------------------------------------------
# F2. R2: a transition to APPROVED that reads keys and signed verdict rows (unit U3.4d, merge condition of decision D-P30)
# ---------------------------------------------------------------------------------------------------------------------------------
#
# The task T-1 is in IN_REVIEW with one signed `review` verdict (risk class low), signed by the REAL `make_verdict` under canary keys.
# The binary then verifies it: Ed25519 only, HMAC only, both keys, the dev-key opt-in, SWARM_REQUIRE_KEY=1 without a key, no key, and a
# key-file reference. Every channel is scanned for every form of every canary: stdout, stderr, what the binary itself printed through the
# selector, the effects manifest, the Task Store files and the run log. The selector hands the key variables to the binary on this route
# (and only this one), so the launcher's record of its own environment is checked for names instead of scanned.

APPROVED_ARGV = ["--json", "--transition", "T-1", "APPROVED", "--reason", "canary"]
NO_KEY_TEXT = "fail-closed: no signing key configured"
REQUIRE_TEXT = "fail-closed: SWARM_REQUIRE_KEY=1 but no signing key configured"
SIGN_BOTH = ("SWARM_SIGNING_KEY", "SWARM_ED25519_KEY")
# case: (the variables the verdict is signed under, the variables the run is given, what the binary answers)
R2_CASES: dict[str, tuple[tuple[str, ...], tuple[str, ...], str]] = {
    "ed25519-only": (("SWARM_ED25519_KEY",), ("SWARM_ED25519_KEY",), "approved"),
    "hmac-only": (("SWARM_SIGNING_KEY",), ("SWARM_SIGNING_KEY",), "approved"),
    "both-keys": (SIGN_BOTH, SIGN_BOTH, "approved"),
    "dev-key-opt-in": (("SWARM_ALLOW_INSECURE_DEV_KEY",), ("SWARM_ALLOW_INSECURE_DEV_KEY",), "approved"),
    "require-key-without-a-key": (("SWARM_SIGNING_KEY",), ("SWARM_REQUIRE_KEY",), REQUIRE_TEXT),
    "no-key": (("SWARM_SIGNING_KEY",), (), NO_KEY_TEXT),
    "key-file-reference": (("SWARM_SIGNING_KEY",), ("SWARM_ED25519_KEY_FILE",), "E-DEP"),
}
R2_GRANT = (*KEY_VARS, "SWARM_AGENT_SESSION", "XDG_CONFIG_HOME")


def record_review_verdict(world: World, signing: Mapping[str, str], monkeypatch: pytest.MonkeyPatch, *, record: bool = True) -> dict[str, Any]:
    """Puts T-1 into IN_REVIEW and (when `record`) records a `review` verdict signed by the real `make_verdict` under `signing`, which
    is the whole key environment of the signing. Returns the verdict envelope."""
    from swarm.gates import make_verdict

    for name in KEY_VARS:
        monkeypatch.delenv(name, raising=False)
    for name, value in signing.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("SWARM_DIR", str(world.tmp / "sign-state"))
    store = TaskStore(world.swarm / "tasks.db")
    try:
        store.conn.execute("UPDATE tasks SET state = 'IN_REVIEW' WHERE task_id = 'T-1'")
        store.conn.commit()
        envelope = make_verdict(gate="review", task_id="T-1", agent_id="A09", correlation_id="C-1", audit=False, root=world.tmp / "sign-root")
        if record:
            store.record_verdict("T-1", envelope)
    finally:
        store.conn.close()
        for name in (*KEY_VARS, "SWARM_DIR"):
            monkeypatch.delenv(name, raising=False)
    return envelope


def given(canaries: Canaries, names: tuple[str, ...]) -> Canaries:
    """`canaries` with only `names` given to the run (the scan still looks for every canary)."""
    return Canaries({name: canaries.env[name] for name in names}, canaries.needles)


def approved_answer(outcome: Outcome, driver: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """(what the binary itself printed, what was printed to the caller) of an APPROVED run."""
    printed = json.loads(outcome.stdout)
    return (json.loads(outcome.binary_stdout) if driver == "selector" else printed), printed


def check_approved_run(
    world: World, canaries: Canaries, outcome: Outcome, case: str, driver: str, backend: str, build: str, where: str
) -> str | None:
    """The run answered as `case` says, left the task where the answer says, and no canary is in any channel. Returns why the guest was
    not exercised (a sandbox leg of a build with no write guest), else None."""
    expected = R2_CASES[case][2]
    binary, printed = approved_answer(outcome, driver)
    stderr = outcome.binary_stderr if driver == "selector" else outcome.stderr
    no_guest = backend == "sandbox" and build == "default"
    refused_before_write = no_guest or case == "key-file-reference"
    if refused_before_write:
        error = binary.get("error") or {}
        assert binary.get("status") == "error" and error.get("code") == "E-DEP", f"expected the E-DEP refusal before any write {where}"
        assert (error.get("details") or {}).get("write_state") == "not_started", f"the refusal must come before any write {where}"
        if no_guest:
            assert "sandbox-write" in error.get("message", ""), f"the refusal must name the sandbox-write feature {where}"
    elif expected == "approved":
        assert binary.get("status") == "ok", f"the binary did not approve {where}"
        assert FAILOVER_NOTICE not in stderr, f"the sandbox failed over: the guest did not serve this APPROVED {where}"
    else:
        error = binary.get("error") or {}
        assert error.get("code") == "E-POLICY" and expected in error.get("message", ""), f"expected the E-POLICY {expected!r} {where}"
        assert "write_state" not in (error.get("details") or {}) or error["details"]["write_state"] == "final", f"unexpected write_state {where}"
    # What the caller was told: through the selector Python answers whatever the binary refused before a write.
    if driver == "selector" and refused_before_write:
        final_expected = NO_KEY_TEXT if case == "key-file-reference" else expected
        if final_expected == "approved":
            assert printed.get("status") == "ok", f"Python did not answer the refusal with the approval {where}"
        else:
            assert final_expected in (printed.get("error") or {}).get("message", ""), f"Python's answer is not {final_expected!r} {where}"
        approved = final_expected == "approved"
    else:
        approved = expected == "approved" and not refused_before_write
    assert task_state(world) == ("APPROVED" if approved else "IN_REVIEW"), f"the state after the call {where}"
    for channel, data in (("stdout", outcome.stdout), ("stderr", outcome.stderr), ("the binary's stdout", outcome.binary_stdout),
                          ("the binary's stderr", outcome.binary_stderr)):
        require_clean(leaks(data, canaries), f"{channel} {where}")
    records = [world.records / "binary-env", world.records / "binary-stdin.json"]
    require_clean(tree_leaks(canaries, *world.scanned(), exclude=records), f"a file {where}")
    effects = world.tmp / "effects.json"
    if effects.exists():
        assert "security.dev_key" not in effects.read_text(encoding="utf-8"), f"Python's transition emits no security.dev_key {where}"
    events = world.swarm / "events.jsonl"
    assert not events.exists() or "security.dev_key" not in events.read_text(encoding="utf-8"), f"security.dev_key in the run log {where}"
    return "the default build has no write guest (E-DEP, nothing written), so no sandbox APPROVED ran" if no_guest else None


def approved_world(tmp_path: Path, case: str, monkeypatch: pytest.MonkeyPatch) -> tuple[World, Canaries]:
    world = make_world(tmp_path, _real_binary())
    canaries = make_canaries(tmp_path)
    signing = {name: canaries.env[name] for name in R2_CASES[case][0]}
    record_review_verdict(world, signing, monkeypatch)
    return world, canaries


def run_approved(world: World, canaries: Canaries, case: str, driver: str, backend: str) -> Outcome:
    run_with = given(canaries, R2_CASES[case][1])
    if driver == "selector":
        return run_selector(world, run_with, "transition", backend, argv=APPROVED_ARGV)
    request = direct_request(world, "transition")
    request["argv"] = APPROVED_ARGV
    return run_binary(world, run_with, json.dumps(request).encode(), backend, extra_env=BACKTRACE)


@pytest.mark.parametrize("backend", ["host", "sandbox"])
@pytest.mark.parametrize("driver", ["selector", "direct"])
@pytest.mark.parametrize("case", R2_CASES)
def test_approved_transition_with_keys_and_signed_rows_leaks_no_key_and_answers_as_python_does(
    tmp_path: Path, build: str, monkeypatch: pytest.MonkeyPatch, case: str, driver: str, backend: str
) -> None:
    world, canaries = approved_world(tmp_path, case, monkeypatch)
    outcome = run_approved(world, canaries, case, driver, backend)
    where = f"[engine build: {build}, {driver}-{case}-{backend}]"
    not_covered = check_approved_run(world, canaries, outcome, case, driver, backend, build, where)
    check_children(world, canaries, spawned=False)
    if driver == "selector":
        raw = (world.records / "binary-env").read_bytes()
        child = environ_values(raw)
        asked = set(R2_CASES[case][1])
        handed = set(child) & set(R2_GRANT)
        # Decision 1 (2026-10-30): a keyed child is started with HOME and XDG_CONFIG_HOME both naming an empty directory made for the
        # call, so XDG_CONFIG_HOME reaches it whatever the case asks; it must then be that directory, never the caller's value.
        unisolated = handed - {"XDG_CONFIG_HOME"}
        assert unisolated == asked - {"XDG_CONFIG_HOME"}, f"the selector handed {sorted(handed)}, the case sets {sorted(asked)} {where}"
        if "XDG_CONFIG_HOME" in child:
            assert child["XDG_CONFIG_HOME"] == child.get("HOME"), f"the child's XDG_CONFIG_HOME is not its own home {where}"
            assert child["HOME"] != os.environ.get("HOME") and child["XDG_CONFIG_HOME"] != os.environ.get("XDG_CONFIG_HOME"), (
                f"the child was given the caller's home {where}")
        for name in asked & set(SECRET_VARS):
            assert raw.count(canaries.env[name].encode()) == 1, f"the value of {name} is in the environment under another name {where}"
    if not_covered is not None:
        pytest.skip(f"not covered: {not_covered}")


@pytest.mark.parametrize("driver", ["selector", "direct"])
def test_the_approved_check_detects_a_key_in_each_channel_and_a_wrong_answer(tmp_path: Path, build: str, monkeypatch: pytest.MonkeyPatch, driver: str) -> None:
    case = "hmac-only"
    world, canaries = approved_world(tmp_path, case, monkeypatch)
    outcome = run_approved(world, canaries, case, driver, "host")
    where = "[scratch]"
    check_approved_run(world, canaries, outcome, case, driver, "host", build, where)
    key = canaries.needles["SWARM_SIGNING_KEY"]["utf8"]
    for channel in ("stdout", "stderr", "binary_stdout", "binary_stderr"):
        if driver == "direct" and channel.startswith("binary"):
            continue
        current = getattr(outcome, channel)
        # a reply stays one JSON object (the check reads it before it scans it); a stream just gets the key appended
        planted = json.dumps({**json.loads(current), "note": key.decode()}).encode() if channel.endswith("stdout") else current + b" " + key
        leaked = replace(outcome, **{channel: planted})
        with pytest.raises(AssertionError, match="canary in"):
            check_approved_run(world, canaries, leaked, case, driver, "host", build, where)
    planted = world.tmp / "leak.bin"
    planted.write_bytes(b"\x00" * 40 + canaries.needles["SWARM_ED25519_KEY"]["key-bytes"])
    with pytest.raises(AssertionError, match="canary in a file"):
        check_approved_run(world, canaries, outcome, case, driver, "host", build, where)
    planted.unlink()
    with pytest.raises(AssertionError, match="expected the E-POLICY"):
        check_approved_run(world, canaries, outcome, "no-key", driver, "host", build, where)
    (world.swarm / "events.jsonl").write_text('{"type": "security.dev_key"}\n', encoding="utf-8")
    with pytest.raises(AssertionError, match="security.dev_key"):
        check_approved_run(world, canaries, outcome, case, driver, "host", build, where)


def test_an_approved_transition_reads_the_verdict_rows_after_it_has_the_write_lock(tmp_path: Path, build: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second connection holds the write lock and the verdict the task is missing is committed while the binary waits: the binary must
    see it, which only a read inside its own transaction does. Nothing here is sensitive to how long the binary takes to reach the lock."""
    world = make_world(tmp_path, _real_binary())
    canaries = make_canaries(tmp_path)
    envelope = record_review_verdict(world, {"SWARM_SIGNING_KEY": canaries.env["SWARM_SIGNING_KEY"]}, monkeypatch, record=False)
    request = direct_request(world, "transition")
    request["argv"] = APPROVED_ARGV
    run_with = given(canaries, ("SWARM_SIGNING_KEY",))
    holder = sqlite3.connect(world.swarm / "tasks.db", timeout=30, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        env = {**_environ(world, run_with), "GSD_RUST_BACKEND": "host"}
        process = subprocess.Popen(
            [str(world.binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=world.root, env=env
        )
        assert process.stdin is not None
        process.stdin.write(json.dumps(request).encode())
        process.stdin.close()
        try:
            process.wait(timeout=3)
            raise AssertionError("the binary finished while another connection held the write lock: it did not wait for it")
        except subprocess.TimeoutExpired:
            pass
        now = envelope["payload"]["issued_at"]
        holder.execute(
            "INSERT INTO verdicts (task_id, gate, verdict, agent_id, findings, expires_at, ts, envelope_json, sig) VALUES (?,?,?,?,?,?,?,?,?)",
            ("T-1", "review", "pass", "A09", "[]", now + 86400, now, json.dumps(envelope), envelope["sig"]),
        )
        holder.execute("COMMIT")
        assert process.stdout is not None and process.stderr is not None
        out, err = process.stdout.read(), process.stderr.read()  # the request was written and closed above, so communicate() would flush it
        process.wait(timeout=120)
    finally:
        holder.close()
    reply = json.loads(out)
    assert reply.get("status") == "ok" and task_state(world) == "APPROVED", f"the row committed under the binary's feet was not seen [engine build: {build}]"
    require_clean(leaks(out + err, canaries), "the answer of a call that waited for the lock")


# ---------------------------------------------------------------------------------------------------------------------------------
# G. Drift of the key-variable lists, and the route-by-key policy (R5)
# ---------------------------------------------------------------------------------------------------------------------------------

KEY_NAME = re.compile(r"SWARM_[A-Z0-9_]*(?:KEY|SECRET|TOKEN|SEED|PEM|PRIVATE)[A-Z0-9_]*")
KEY_SOURCES = ("crates/gsd-envelope/src/keys.rs", "swarm/envelope.py")


def key_sources() -> list[Path]:
    """The files whose key-variable names must be withheld from the binary: the key code of both engines and every script."""
    return [ROOT / name for name in KEY_SOURCES] + sorted((ROOT / "scripts").glob("*.py"))


def key_names(text: str) -> set[str]:
    """Every environment variable name in `text` that looks like secret material."""
    return set(KEY_NAME.findall(text))


def undeclared_key_names(paths: list[Path], declared: Collection[str]) -> dict[str, list[str]]:
    """`file name: names` of the key-looking variables the files mention that `declared` does not hold."""
    found: dict[str, list[str]] = {}
    for path in paths:
        missing = sorted(key_names(path.read_text(encoding="utf-8")) - set(declared))
        if missing:
            found[path.name] = missing
    return found


def test_every_key_variable_the_sources_name_is_withheld_from_the_binary() -> None:
    sources = key_sources()
    assert undeclared_key_names(sources, selector().SIGNING_KEY_ENV) == {}
    # Not vacuous: the scan found the five known names, in at least the files that hold the key code.
    named = {path.name: key_names(path.read_text(encoding="utf-8")) for path in sources}
    assert set(KEY_VARS) <= set().union(*named.values())
    assert set(KEY_VARS) <= named["keys.rs"] | named["envelope.py"]
    assert len([name for name, names in named.items() if names]) >= 5


def test_the_drift_check_names_a_new_key_variable_and_ignores_the_rest(tmp_path: Path) -> None:
    source = tmp_path / "new_script.py"
    source.write_text(
        'A = "SWARM_HMAC_KEY"\nB = "SWARM_PRIVATE_PEM"\nC = "SWARM_SEED_FILE"\nD = "SWARM_API_TOKEN"\nE = "SWARM_DIR"\nF = "SWARM_AGENT_SESSION"\n'
        'G = "SWARM_SIGNING_KEY"\nH = "SWARM_ED25519_KEY_FILE"\n',
        encoding="utf-8",
    )
    assert undeclared_key_names([source], selector().SIGNING_KEY_ENV) == {
        "new_script.py": ["SWARM_API_TOKEN", "SWARM_HMAC_KEY", "SWARM_PRIVATE_PEM", "SWARM_SEED_FILE"]
    }
    assert undeclared_key_names([source], {*selector().SIGNING_KEY_ENV, "SWARM_HMAC_KEY", "SWARM_PRIVATE_PEM", "SWARM_SEED_FILE", "SWARM_API_TOKEN"}) == {}


def assigned_strings(path: Path, name: str) -> tuple[str, ...]:
    """The strings of the module-level literal assignment `name = (...)` of the Python file `path`, read without importing it."""
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            value = ast.literal_eval(node.value)
            assert isinstance(value, (tuple, list)) and all(isinstance(item, str) for item in value), f"{name} is not a list of strings"
            return tuple(value)
    raise AssertionError(f"{path.name} has no module-level {name}")


def test_the_list_reader_reads_a_literal_and_refuses_anything_else(tmp_path: Path) -> None:
    module = tmp_path / "m.py"
    module.write_text('X = ("A", "B")\nY = ["C"]\nZ = {"D"}\nW = ("E", 1)\n', encoding="utf-8")
    assert assigned_strings(module, "X") == ("A", "B") and assigned_strings(module, "Y") == ("C",)
    for name in ("Z", "W", "MISSING"):
        with pytest.raises(AssertionError):
            assigned_strings(module, name)


STRIP_LISTS = (("swarm/hook_trigger.py", "SIGNING_ENV_STRIPPED"), ("scripts/swarm_run.py", "AGENT_SESSION_STRIPPED"))
STRIP_CASES = [
    pytest.param(path, name, variable, id=f"{name}-{variable}")
    for path, name in STRIP_LISTS
    for variable in KEY_VARS
]


@pytest.mark.parametrize(("path", "name", "variable"), STRIP_CASES)
def test_a_list_that_strips_signing_material_from_child_sessions_holds_the_variable(path: str, name: str, variable: str) -> None:
    assert variable in assigned_strings(ROOT / path, name), f"{name} of {path} does not strip {variable}"


def test_the_two_lists_that_strip_signing_material_agree() -> None:
    first, second = (set(assigned_strings(ROOT / path, name)) for path, name in STRIP_LISTS)
    assert first == second, f"only in {STRIP_LISTS[0][1]}: {sorted(first - second)}; only in {STRIP_LISTS[1][1]}: {sorted(second - first)}"


# The one place that says which route may be handed which key variables (design item 13, decision D-P30). Only a transition whose
# target is APPROVED verifies signatures, so only it is handed the key variables; `ROUTE_KEY_ENV` of the selector says the same and
# `test_the_selector_tables_name_no_key_variable_outside_the_approved_route` holds the two together.
ROUTE_KEY_POLICY: dict[str, frozenset[str]] = {
    "list": frozenset(),
    "history": frozenset(),
    "transition": frozenset(),
    "transition-approved": frozenset(KEY_VARS),
    "ingest": frozenset(KEY_VARS),
}
PRESENCES: dict[str, tuple[str, ...]] = {
    "none": (),
    **{name: (name,) for name in KEY_VARS},
    "secrets": SECRET_VARS,
    "flags": FLAG_VARS,
    "all": KEY_VARS,
}


def load_selector(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def binary_env_for(module: ModuleType, route: str) -> dict[str, str]:
    """The environment the selector hands the binary for `route`. `_binary_env` takes the route since U3.4d: an old signature fails here."""
    if list(inspect.signature(module._binary_env).parameters) != ["route"]:
        pytest.fail("_binary_env takes no route: the policy table below is about routes, so the selector must say which route it starts")
    return dict(module._binary_env(route))


def check_route_key_policy(module: ModuleType, route: str, present: tuple[str, ...], monkeypatch: pytest.MonkeyPatch, canaries: Canaries) -> None:
    for name in KEY_VARS:
        monkeypatch.delenv(name, raising=False)
    for name in present:
        monkeypatch.setenv(name, canaries.env[name])
    env = binary_env_for(module, route)
    allowed = ROUTE_KEY_POLICY[route]
    handed = {name for name in env if name in KEY_VARS}
    assert handed == set(present) & allowed, f"route {route} with {sorted(present)} set was handed {sorted(handed)}"
    assert all(env[name] == canaries.env[name] for name in handed)
    blob = "\0".join(f"{name}={value}" for name, value in env.items())
    withheld = [name for name in present if name in SECRET_VARS and name not in allowed and canaries.env[name] in blob]
    assert not withheld, f"route {route}: the value of {withheld} is in the environment under another name"


@pytest.mark.parametrize("presence", PRESENCES)
@pytest.mark.parametrize("route", ROUTE_KEY_POLICY)
def test_each_route_is_handed_exactly_the_keys_the_policy_allows(
    host_env: Canaries, monkeypatch: pytest.MonkeyPatch, route: str, presence: str
) -> None:
    check_route_key_policy(selector(), route, PRESENCES[presence], monkeypatch, host_env)


def test_the_policy_table_covers_every_route_of_the_tests() -> None:
    assert set(ROUTES) <= set(ROUTE_KEY_POLICY)
    assert all(allowed <= set(KEY_VARS) for allowed in ROUTE_KEY_POLICY.values())


def allowlist_violations(module: ModuleType, monkeypatch: pytest.MonkeyPatch, canaries: Canaries) -> list[str]:
    """The names some route is handed that neither `BINARY_ENV_NAMES` nor the route's grant lists, with every key variable set."""
    monkeypatch.setenv("GSD_FUTURE_SETTING", "x")
    monkeypatch.setenv("SWARM_FUTURE_SETTING", "x")
    monkeypatch.setenv("SWARM_TASKSTORE_WAL", "1")
    monkeypatch.setenv("SWARM_AGENT_SESSION", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/x")
    for name, value in canaries.env.items():
        monkeypatch.setenv(name, value)
    found = []
    for route, grant in module.ROUTE_KEY_ENV.items():
        extra = set(module._binary_env(route)) - set(module.BINARY_ENV_NAMES) - set(grant)
        found += [f"{route}: {name}" for name in sorted(extra)]
    return found


def test_the_policy_checks_catch_a_selector_that_passes_a_key_by_prefix_or_to_the_wrong_route(
    host_env: Canaries, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original = SCRIPT.read_text(encoding="utf-8")
    assert allowlist_violations(selector(), monkeypatch, host_env) == []
    mutations = {
        "a prefix pass-through instead of the allowlist": ("if key in names}", 'if key in names or key.startswith(("GSD_", "SWARM_"))}'),
        "keys for a transition that is not APPROVED": ('"transition": frozenset(),', '"transition": APPROVED_KEY_ENV,'),
        "keys for the list route": ('"list": frozenset(),', '"list": SIGNING_KEY_ENV,'),
        "keys for the history route": ('"history": frozenset(),', '"history": SIGNING_KEY_ENV,'),
    }
    for label, (old, new) in mutations.items():
        assert old in original, f"{label}: the line this mutation changes moved: update it with `_binary_env`"
        mutated = tmp_path / "orch_status_mutated.py"
        mutated.write_text(original.replace(old, new), encoding="utf-8")
        module = load_selector(mutated, "orch_status_mutated")
        routes = ("list", "history", "transition")
        failed = False
        for route in routes:
            for variable in KEY_VARS:
                try:
                    check_route_key_policy(module, route, (variable,), monkeypatch, host_env)
                except AssertionError as caught:
                    failed = failed or "was handed" in str(caught)
        assert failed or allowlist_violations(module, monkeypatch, host_env), f"nothing noticed: {label}"
    changed = tmp_path / "orch_status_route.py"
    changed.write_text(original.replace("def _binary_env(route: str) -> dict[str, str]:", "def _binary_env() -> dict[str, str]:"), encoding="utf-8")
    with pytest.raises(pytest.fail.Exception, match="takes no route"):
        check_route_key_policy(load_selector(changed, "orch_status_route"), "list", (), monkeypatch, host_env)
