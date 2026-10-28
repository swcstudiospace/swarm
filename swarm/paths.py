"""Single lazy resolver for Swarm state (D-09..D-11).

Order: env SWARM_DIR (made absolute) -> <git toplevel of root or cwd>/.swarm -> <root>/.swarm.
Resolved per call; nothing is computed at import time.
"""
from __future__ import annotations
import functools
import os
import subprocess
from pathlib import Path


@functools.lru_cache(maxsize=64)
def _resolve(env_val: str | None, base: str) -> Path:
    if env_val:
        return Path(base, env_val).resolve()
    try:
        proc = subprocess.run(["git", "-C", base, "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True, check=False)
    except OSError:
        proc = None
    if proc is not None and proc.returncode == 0 and proc.stdout.strip():
        return Path(proc.stdout.strip()).resolve() / ".swarm"
    return Path(base) / ".swarm"


def swarm_dir(root: str | Path | None = None, *, create: bool = False) -> Path:
    """Absolute Swarm state dir; ``create`` also writes ``.gitignore`` = ``*`` if absent."""
    env_val = os.environ.get("SWARM_DIR") or None
    if env_val:  # a relative env value is anchored at the caller's cwd
        base = str(Path.cwd().resolve())
    else:
        base = str(Path(root).resolve() if root is not None else Path.cwd().resolve())
    d = _resolve(env_val, base)
    if create:
        ignore = d / ".gitignore"
        if not ignore.exists():  # one stat on the common path; the directory and the file are made only when the file is missing
            d.mkdir(parents=True, exist_ok=True)
            try:
                with ignore.open("x", encoding="utf-8") as fh:
                    fh.write("*")
            except FileExistsError:
                pass
    return d


def latest_correlation(root: str | Path | None = None) -> str | None:
    f = swarm_dir(root) / "latest_correlation"
    return f.read_text().strip() if f.exists() else None


def logging_paths(root: str | Path | None = None) -> dict[str, str]:
    """Spec for all logging paths used by the logger unit.
    Emits at phases go to events.jsonl; end-of-run summaries and autonomous to their logs;
    state for lifecycle projections.
    """
    d = swarm_dir(root, create=False)
    return {
        "dir": str(d),
        "events": str(d / "events.jsonl"),
        "autonomous_log": str(d / "autonomous.log"),
        "swarm_state": str(d / "swarm-state.json"),
        "latest_correlation": str(d / "latest_correlation"),
        "tasks_db": str(d / "tasks.db"),
        "results": str(d / "results"),
    }
