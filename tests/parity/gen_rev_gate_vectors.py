"""Generates the `rev_gate` parity report from the real `scripts/rev_gate.py` (phase 14.5, unit U3.6d).

Every cell of `_rev_gate_corpus.CASES` runs on Python (the oracle, unmodified) and, when `--binary` is given, through the
`gsd_engine` binary. The report lists each cell with its design (served or declined), what the binary did, and whether a
served cell's reply equals Python's after the envelope's clock and random fields are masked.

    python3 tests/parity/gen_rev_gate_vectors.py --binary target/release/gsd_engine [--out report.json]

Exit 0 when every served cell is answered by the binary with a reply equal to Python's, and every declined cell is refused
with `E-DEP` `not_started`. Without `--binary` it only runs the oracle and reports the design counts.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _rev_gate_corpus as corpus  # noqa: E402


def _cell(case: corpus.Case, scratch: Path, binary: str | None) -> dict:
    root_p, argv_p, env_p = corpus.materialize(case, scratch / "py")
    oracle = corpus.run_python(root_p, argv_p, env_p)
    row: dict = {
        "name": case.name,
        "design": "served" if case.served else "declined",
        "python_exit": oracle.returncode,
    }
    if binary is None:
        return row
    root_r, argv_r, env_r = corpus.materialize(case, scratch / "rs")
    code, reply = corpus.run_binary(binary, root_r, argv_r, env_r)
    row["binary_exit"] = code
    if case.served:
        row["binary"] = "answered" if corpus.answered(code, reply) else "not-answered"
        if row["binary"] == "answered" and case.json:
            same_exit = oracle.returncode == code
            same_body = corpus.comparable(json.loads(oracle.stdout), argv_p) == corpus.comparable(reply, argv_r)
            row["equal"] = same_exit and same_body
        else:
            row["equal"] = False
            row["binary_error"] = reply.get("error")
    else:
        row["binary"] = "declined" if corpus.declined(code, reply) else "not-declined"
        row["binary_error"] = reply.get("error") if isinstance(reply, dict) else None
    return row


def build_report(binary: str | None) -> dict:
    cells: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="rev-gate-corpus-") as tmp:
        for case in corpus.CASES:
            cells.append(_cell(case, Path(tmp) / case.name, binary))
    served = [cell for cell in cells if cell["design"] == "served"]
    declined = [cell for cell in cells if cell["design"] == "declined"]
    counts = {
        "cells": len(cells),
        "served": len(served),
        "declined": len(declined),
    }
    if binary is not None:
        counts["served_answered_by_binary"] = sum(cell.get("binary") == "answered" for cell in served)
        counts["served_equal_to_python"] = sum(cell.get("equal") is True for cell in served)
        counts["declined_refused_by_binary"] = sum(cell.get("binary") == "declined" for cell in declined)
    return {"counts": counts, "cells": cells}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--binary", help="the gsd_engine binary to run the served and declined cells through")
    parser.add_argument("--out", help="write the JSON report here as well as printing it")
    args = parser.parse_args(argv)
    report = build_report(args.binary)
    text = json.dumps(report, indent=2, ensure_ascii=False)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    if args.binary is None:
        return 0
    counts = report["counts"]
    ok = (counts["served_answered_by_binary"] == counts["served"]
          and counts["served_equal_to_python"] == counts["served"]
          and counts["declined_refused_by_binary"] == counts["declined"])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
