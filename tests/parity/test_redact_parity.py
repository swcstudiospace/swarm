"""Phase 14.1.1 parity: the Rust span scanner (`swarm._native.redact_spans`) against the pure-Python oracle (`swarm.redact.redact_spans`).

Both legs run over the whole corpus of `_redact_corpus.py` in child processes, sharded over the cores. The pure leg runs with
SWARM_PURE_PYTHON=1 and never loads the extension; the native leg imports `swarm._native` (or the Python file named by
`SWARM_REDACT_PARITY_NATIVE`, a hook that lets the comparison itself be rehearsed against a stand-in) and calls its `redact_spans` directly.
The span lists must be equal for every text. A text with a lone surrogate cannot cross into Rust (UnicodeEncodeError); the native leg
reports it as `unencodable` and the test asserts exactly that, because the Python adapter answers such a call from the oracle.

The two native tests skip, with the reason, when the extension or its `redact_spans` is missing; they never fail for that. A failure log
names the source of the first differing input and prints it truncated, with every range either leg reports (and every long token-like
run) elided, so it carries no credential. The corpus is built in memory at run time: no secret-shaped literal is written to the repository.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pytest  # type: ignore[import-not-found, unused-ignore]  # absent from a mypy environment that is not this interpreter's
from _redact_corpus import MAX_TEXT_CHARS, SOURCES, build_corpus, build_corpus_report, helpers

REPO = Path(__file__).resolve().parents[2]
OVERRIDE_ENV = "SWARM_REDACT_PARITY_NATIVE"
LEG_TIMEOUT_S = 150
UNENCODABLE = "unencodable"

Spans = list[list[int]]
LegValue = Spans | str  # spans, or a tag: "unencodable", "badshape", "raised:<ExceptionType>"

# Shared by the probe and the legs: load the native module (or the stand-in named by the override env var).
_LOAD_NATIVE = r"""
import importlib.util, os, sys
sys.path.insert(0, sys.argv[1])
def load():
    override = os.environ.get("SWARM_REDACT_PARITY_NATIVE")
    if override:
        spec = importlib.util.spec_from_file_location("_redact_parity_native_override", override)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    from swarm._native_loader import load_native
    return load_native()
"""

_PROBE = _LOAD_NATIVE + r"""
try:
    native = load()
except ImportError as exc:
    print("import:" + str(exc)[:300])
else:
    print("ok" if callable(getattr(native, "redact_spans", None)) else "missing")
"""

_LEG = _LOAD_NATIVE + r"""
import json
leg, corpus_path, shard, shards, out_path = sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5]), sys.argv[6]
with open(corpus_path, encoding="utf-8") as handle:
    # the transport is JSON of latin-1 text over surrogatepass UTF-8: JSON itself would merge a high and a low surrogate escape into one character
    texts = [t.encode("latin-1").decode("utf-8", "surrogatepass") for t in json.load(handle)]
results = []
if leg == "pure":
    from swarm.redact import redact_spans
    for i in range(shard, len(texts), shards):
        results.append([i, [[a, b] for a, b in redact_spans(texts[i])]])
else:
    fn = load().redact_spans
    for i in range(shard, len(texts), shards):
        try:
            raw = fn(texts[i])
        except UnicodeEncodeError:
            value = "unencodable"
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:  # a Rust panic is a BaseException subclass
            value = "raised:" + type(exc).__name__
        else:
            ok = isinstance(raw, list) and all(
                type(sp) is tuple and len(sp) == 2 and type(sp[0]) is int and type(sp[1]) is int for sp in raw)
            value = [[a, b] for a, b in raw] if ok else "badshape"
        results.append([i, value])
with open(out_path, "w", encoding="utf-8") as handle:
    json.dump(results, handle)
"""


def _env(*, pure: bool) -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", str(REPO)), "SWARM_REDACT_ENGINE": "python"}
    override = os.environ.get(OVERRIDE_ENV)
    if override:
        env[OVERRIDE_ENV] = override
    if pure or override:  # a stand-in is plain Python over the oracle: it must not need the extension either
        env["SWARM_PURE_PYTHON"] = "1"
    return env


def native_status() -> tuple[bool, str]:
    """Whether the native leg can run, and the reason when it cannot."""
    proc = subprocess.run([sys.executable, "-c", _PROBE, str(REPO)], cwd=REPO, env=_env(pure=False), capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        return False, f"probing swarm._native failed: {proc.stderr.strip()[-300:]}"
    answer = proc.stdout.strip()
    if answer == "ok":
        return True, ""
    if answer == "missing":
        return False, "swarm._native has no redact_spans: the extension is built without the Rust redaction engine (Phase 14.1.1)"
    return False, f"swarm._native is not importable ({answer.removeprefix('import:')})"


# ---- comparison and failure report --------------------------------------------------------------------------------------------------


def encodable(text: str) -> bool:
    """Whether `text` can cross into Rust (no lone surrogate)."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


@dataclass
class Mismatch:
    """One text whose native answer differs from the oracle's."""

    index: int
    source: str
    oracle: LegValue
    native: LegValue


@dataclass
class Comparison:
    """The result of comparing two legs over a corpus."""

    compared: int = 0
    unencodable: int = 0
    mismatches: list[Mismatch] = field(default_factory=list)
    unencodable_violations: list[Mismatch] = field(default_factory=list)


def compare_legs(corpus: list[tuple[str, str]], pure: list[LegValue], native: list[LegValue]) -> Comparison:
    """Span lists must be equal for every encodable text; a text with a lone surrogate must be `unencodable` natively."""
    result = Comparison()
    for index, ((source, text), want, got) in enumerate(zip(corpus, pure, native, strict=True)):
        if encodable(text):
            result.compared += 1
            if want != got:
                result.mismatches.append(Mismatch(index, source, want, got))
        else:
            result.unencodable += 1
            if got != UNENCODABLE:
                result.unencodable_violations.append(Mismatch(index, source, want, got))
    return result


_LONG_RUN = re.compile(r"[A-Za-z0-9+/=_~.%-]{12,}")


def _pairs(value: LegValue) -> set[tuple[int, int]]:
    return {(a, b) for a, b in value} if isinstance(value, list) else set()


def elide(text: str, oracle: LegValue, native: LegValue, *, width: int = 160) -> str:
    """A window of `text` around its first differing range, with every range either leg reports replaced by `<elided N>` and every
    token-like run of 12 or more characters by `<run N>`: a log of it holds no credential, only the shape around it."""
    want, got = _pairs(oracle), _pairs(native)
    differing = sorted(want ^ got)
    focus = differing[0][0] if differing else 0
    low = max(0, focus - 60)
    high = min(len(text), low + width)
    pieces: list[str] = []
    cursor = low
    merged: list[list[int]] = []
    for start, end in sorted(want | got):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    for start, end in merged:
        start, end = max(start, low), min(end, high)
        if start >= end:
            continue
        pieces.append(text[cursor:start])
        pieces.append(f"<elided {end - start}>")
        cursor = end
    pieces.append(text[cursor:high])
    shown = _LONG_RUN.sub(lambda run: f"<run {len(run[0])}>", "".join(pieces))
    return ("..." if low else "") + ascii(shown) + ("..." if high < len(text) else "")


def _spans_text(value: LegValue, limit: int = 20) -> str:
    if isinstance(value, str):
        return value
    head = ", ".join(f"({a},{b})" for a, b in value[:limit])
    return f"[{head}{', ...' if len(value) > limit else ''}] ({len(value)} spans)"


def describe(mismatch: Mismatch, text: str) -> str:
    """One mismatch as a log entry: source, index, length, the elided window and both span lists."""
    return (f"source={mismatch.source} index={mismatch.index} length={len(text)}\n"
            f"  text(elided): {elide(text, mismatch.oracle, mismatch.native)}\n"
            f"  oracle: {_spans_text(mismatch.oracle)}\n"
            f"  native: {_spans_text(mismatch.native)}")


def report(comparison: Comparison, corpus: list[tuple[str, str]], *, shown: int = 3) -> str:
    """The assertion message of a failed comparison."""
    by_source = Counter(m.source for m in comparison.mismatches)
    lines = [f"{len(comparison.mismatches)} of {comparison.compared} texts differ; per source: {dict(by_source)}"]
    lines += [describe(m, corpus[m.index][1]) for m in comparison.mismatches[:shown]]
    return "\n".join(lines)


# ---- running the legs -----------------------------------------------------------------------------------------------------------------


@dataclass
class Legs:
    """The corpus and the two legs' answers, index-aligned."""

    corpus: list[tuple[str, str]]
    pure: list[LegValue]
    native: list[LegValue]


def _collect(leg: str, procs: list[tuple[subprocess.Popen[bytes], Path, Path]]) -> list[LegValue]:
    answers: dict[int, LegValue] = {}
    try:
        for proc, out, err in procs:
            code = proc.wait(timeout=LEG_TIMEOUT_S)
            assert code == 0, f"{leg} leg exited {code}: {err.read_text(errors='replace')[-2000:]}"
            for index, value in json.loads(out.read_text(encoding="utf-8")):
                answers[index] = value
    finally:
        for proc, _, _ in procs:
            if proc.poll() is None:
                proc.kill()
    return [answers[i] for i in range(len(answers))]


_CACHE: dict[str, list[tuple[str, str]]] = {}


def corpus() -> list[tuple[str, str]]:
    """The corpus, built once per test process."""
    if "corpus" not in _CACHE:
        _CACHE["corpus"] = build_corpus()
    return _CACHE["corpus"]


@pytest.fixture(scope="module")  # type: ignore[untyped-decorator, unused-ignore]
def legs(tmp_path_factory: pytest.TempPathFactory) -> Legs:
    """Both legs over the whole corpus (skips when the native leg cannot run)."""
    available, reason = native_status()
    if not available:
        pytest.skip(reason)
    pairs = corpus()
    work = tmp_path_factory.mktemp("redact-parity")
    corpus_path = work / "corpus.json"
    corpus_path.write_text(json.dumps([text.encode("utf-8", "surrogatepass").decode("latin-1") for _, text in pairs]), encoding="utf-8")
    shards = max(1, min(8, os.cpu_count() or 1))
    pure_procs = _start("pure", corpus_path, work, shards)
    native_procs = _start("native", corpus_path, work, shards)
    pure = _collect("pure", pure_procs)
    native = _collect("native", native_procs)
    assert len(pure) == len(native) == len(pairs)
    return Legs(pairs, pure, native)


def _start(leg: str, corpus_path: Path, out_dir: Path, shards: int) -> list[tuple[subprocess.Popen[bytes], Path, Path]]:
    procs: list[tuple[subprocess.Popen[bytes], Path, Path]] = []
    for shard in range(shards):
        out = out_dir / f"{leg}-{shard}.json"
        err = out_dir / f"{leg}-{shard}.err"
        cmd = [sys.executable, "-c", _LEG, str(REPO), leg, str(corpus_path), str(shard), str(shards), str(out)]
        with err.open("wb") as err_handle:
            procs.append((subprocess.Popen(cmd, cwd=REPO, env=_env(pure=leg == "pure"), stdout=subprocess.DEVNULL, stderr=err_handle), out, err))
    return procs


# ---- tests ----------------------------------------------------------------------------------------------------------------------------


def test_native_spans_equal_the_oracle_on_every_corpus_text(legs: Legs) -> None:
    assert sum(1 for spans in legs.pure if spans) > 10_000, "the oracle leg found almost nothing: the legs did not run over the corpus"
    comparison = compare_legs(legs.corpus, legs.pure, legs.native)

    assert comparison.compared > 10_000
    if comparison.mismatches:
        pytest.fail(report(comparison, legs.corpus), pytrace=False)


def test_a_text_with_a_lone_surrogate_is_unencodable_natively(legs: Legs) -> None:
    comparison = compare_legs(legs.corpus, legs.pure, legs.native)

    assert comparison.unencodable >= 50
    if comparison.unencodable_violations:
        pytest.fail(report(Comparison(0, 0, comparison.unencodable_violations), legs.corpus), pytrace=False)
    for (_, text), value in zip(legs.corpus, legs.native, strict=True):
        assert (value == UNENCODABLE) == (not encodable(text))


def test_the_corpus_is_deterministic_and_covers_every_source() -> None:
    first = corpus()
    second = build_corpus_report()

    assert second.pairs == first
    texts = [text for _, text in first]
    assert len(set(texts)) == len(texts) >= 10_000
    assert all(len(text) <= MAX_TEXT_CHARS for text in texts)
    assert {label for label, _ in first} == set(SOURCES)
    assert any(not encodable(text) for text in texts) and any(len(text) < 6 for text in texts)
    assert max(len(text) for text in texts) >= 8000  # the floods are in


def test_a_failure_report_names_the_source_and_both_span_lists_and_elides_the_secret() -> None:
    secret = helpers().expand_tokens("{{tok:ghp:1}}") + helpers().expand_tokens("{{tok:plain:2:30}}")
    text = f"run curl -H 'x' token={secret} then {secret}-tail done"
    start = text.index(secret)
    oracle: Spans = [[start, start + len(secret)]]
    corpus_pairs = [("gen.example", "x" * 20), ("fixture.example", text)]
    comparison = compare_legs(corpus_pairs, [[], oracle], [[], []])

    assert [(m.index, m.source) for m in comparison.mismatches] == [(1, "fixture.example")]
    message = report(comparison, corpus_pairs)
    assert "fixture.example" in message and f"oracle: [({start},{start + len(secret)})] (1 spans)" in message and "native: [] (0 spans)" in message
    assert "token=" in message and "<elided" in message
    assert secret[:8] not in message and secret[-8:] not in message  # not the range both legs agree on, not the same value read again
    unflagged = elide(text, [], [])
    assert secret[:8] not in unflagged and "<run" in unflagged  # a secret neither leg reports is still not printed


def test_the_comparison_flags_a_dropped_span_and_judges_surrogates_by_the_unencodable_tag() -> None:
    pairs = [("a", "password=hunter2 x"), ("b", "\ud800 password=hunter2"), ("c", "nothing here"), ("d", "\ud800 nothing here"), ("e", "token=abcdef")]
    pure: list[LegValue] = [[[9, 16]], [[10, 17]], [], [], [[6, 12]]]

    good = compare_legs(pairs, pure, [[[9, 16]], UNENCODABLE, [], UNENCODABLE, [[6, 12]]])
    assert (good.compared, good.unencodable, good.mismatches, good.unencodable_violations) == (3, 2, [], [])

    bad = compare_legs(pairs, pure, [[], [[10, 17]], UNENCODABLE, [], [[6, 12]]])
    assert [m.index for m in bad.mismatches] == [0, 2]  # a dropped span, and `unencodable` for a text that encodes
    assert [m.index for m in bad.unencodable_violations] == [1, 3]  # a surrogate text must never be answered with spans
