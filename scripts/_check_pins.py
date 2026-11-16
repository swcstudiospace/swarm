#!/usr/bin/env python3
"""Verify the Rust pin sources against the committed digest (Wave A criterion A0, Phase 6 R1).

The pin sources are rust-toolchain.toml (raw bytes) and the [workspace.metadata.gsd-pins] table of
Cargo.toml (canonical JSON, so comments and layout do not move the digest but values do). The
digest lives in scripts/pins.sha256. After an intentional pin change, review it, rerun with --write,
and commit the new digest with the pin change.

Exit codes: 0 the digest matches, 1 the pin sources changed, 2 a source is unreadable or malformed
(fail closed).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIGEST = Path("scripts") / "pins.sha256"
SEPARATOR = b"\n--gsd-pins--\n"


class PinError(Exception):
    """A pin source is missing or malformed."""


def pin_digest(root: Path) -> str:
    try:
        toolchain = (root / "rust-toolchain.toml").read_bytes()
        tomllib.loads(toolchain.decode("utf-8"))
        cargo = tomllib.loads((root / "Cargo.toml").read_text(encoding="utf-8"))
        pins = cargo["workspace"]["metadata"]["gsd-pins"]
        canonical = json.dumps(pins, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        raise PinError(f"pin sources unreadable or malformed ({type(exc).__name__})") from exc
    return hashlib.sha256(toolchain + SEPARATOR + canonical).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the Rust pin sources against scripts/pins.sha256.")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root (default: this checkout)")
    parser.add_argument("--write", action="store_true", help="record the current digest instead of checking it")
    args = parser.parse_args(argv)
    try:
        current = pin_digest(args.root)
    except PinError as exc:
        print(f"pin check: {exc}", file=sys.stderr)
        return 2
    digest_path = args.root / DIGEST
    if args.write:
        try:
            digest_path.write_text(current + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"pin check: cannot write {DIGEST} ({exc.strerror})", file=sys.stderr)
            return 2
        print(f"pin check: recorded {current} in {DIGEST}")
        return 0
    try:
        recorded = digest_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        print(f"pin check: cannot read {DIGEST} ({type(exc).__name__})", file=sys.stderr)
        return 2
    if recorded != current:
        print(
            f"pin check: the pin sources changed (recorded {recorded}, current {current}); "
            "review the change, then rerun with --write and commit the digest",
            file=sys.stderr,
        )
        return 1
    print(f"pin check: ok ({current})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
