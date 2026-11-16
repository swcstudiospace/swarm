"""The cargo-deny ignore policy (D-P23): every advisory ignore is time-boxed with `expires YYYY-MM-DD` in its reason.

The table tests drive scripts/_check_deny_ignores.py as a subprocess on temporary deny files with a fixed clock;
`test_real_deny_toml_has_no_expired_ignore` runs it on the real deny.toml with the real clock and goes red once an
ignore expires (or when one is added with an expiry more than 120 days away)."""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "_check_deny_ignores.py"
TODAY = "2026-10-28"


def _entry(ident: str = "RUSTSEC-2026-0215", reason: str = "unmaintained; expires 2027-01-26") -> str:
    return f'{{ id = "{ident}", reason = "{reason}" }}'


def _deny(*entries: str) -> str:
    return "[advisories]\nversion = 2\nignore = [\n" + "".join(f"    {entry},\n" for entry in entries) + "]\n"


# (case id, deny.toml text, expected exit code, text expected in the output)
CASES = [
    ("valid", _deny(_entry(), _entry("RUSTSEC-2025-0141", "x; expires 2026-12-01")), 0, "2 ignores, nearest expiry 2026-12-01"),
    ("expires today", _deny(_entry(reason="expires 2026-10-28")), 0, "1 ignores, nearest expiry 2026-10-28"),
    ("expired yesterday", _deny(_entry(reason="expires 2026-10-27")), 1, "expired on 2026-10-27"),
    ("120 days ahead", _deny(_entry(reason="expires 2027-02-25")), 0, "nearest expiry 2027-02-25"),
    ("121 days ahead", _deny(_entry(reason="expires 2027-02-26")), 1, "more than 120 days"),
    ("missing expiry", _deny(_entry(reason="unmaintained, no date")), 1, "expires YYYY-MM-DD"),
    ("impossible date", _deny(_entry(reason="expires 2026-13-45")), 1, "not a calendar date"),
    ("empty reason", _deny(_entry(reason="")), 1, "reason is missing or empty"),
    ("missing reason", _deny('{ id = "RUSTSEC-2026-0215" }'), 1, "reason is missing or empty"),
    ("bad id", _deny(_entry(ident="RUSTSEC-26-215")), 1, "id must match"),
    ("bare string entry", _deny('"RUSTSEC-2026-0215"'), 1, "bare string entry"),
    ("one bad among good", _deny(_entry(), _entry("RUSTSEC-2025-0141", "no date")), 1, "ignore[1]"),
    ("no ignores", _deny(), 0, "0 ignores, nearest expiry none"),
    ("no advisories table", '[bans]\nmultiple-versions = "warn"\n', 0, "0 ignores"),
    ("unparsable file", "[advisories\nignore = ", 2, "cannot read"),
]


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False, timeout=30)


@pytest.mark.parametrize(("text", "code", "expected"), [case[1:] for case in CASES], ids=[case[0] for case in CASES])
def test_ignore_policy(tmp_path: Path, text: str, code: int, expected: str) -> None:
    deny = tmp_path / "deny.toml"
    deny.write_text(text, encoding="utf-8")
    result = _run("--deny-toml", str(deny), "--today", TODAY)
    assert result.returncode == code, result.stdout + result.stderr
    assert expected in result.stdout + result.stderr


def test_missing_file_exits_2(tmp_path: Path) -> None:
    result = _run("--deny-toml", str(tmp_path / "absent.toml"), "--today", TODAY)
    assert result.returncode == 2
    assert "cannot read" in result.stderr


def test_real_deny_toml_has_no_expired_ignore() -> None:
    result = _run("--deny-toml", str(ROOT / "deny.toml"))
    assert result.returncode == 0, result.stdout + result.stderr
