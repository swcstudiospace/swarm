"""Single lazy resolver for Swarm state (D-09..D-11).

Order: env SWARM_DIR (made absolute) -> <git toplevel of root or cwd>/.swarm -> <root>/.swarm.
Resolved per call from the environment and the working directory; nothing is computed at import time. The result of one
resolution is remembered under the inputs that decide it (SWARM_DIR, root, and the cwd where one of them is relative or
absent), so a changed SWARM_DIR, root or cwd resolves again; only a symlink re-pointed under an unchanged input string is not seen.
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


_DIRS: dict[tuple[str | None, str | None, str | None], tuple[Path, str]] = {}
_DIRS_MAX = 256


def swarm_dir(root: str | Path | None = None, *, create: bool = False) -> Path:
    """Absolute Swarm state dir; ``create`` also writes ``.gitignore`` = ``*`` if absent (one stat when it is there)."""
    env_val = os.environ.get("SWARM_DIR") or None
    root_text = None if root is None else os.fspath(root)
    # The cwd decides the result only through a relative SWARM_DIR, or (without SWARM_DIR) a relative or absent root.
    anchored = os.path.isabs(env_val) if env_val else (root_text is not None and os.path.isabs(root_text))
    key = (env_val, None if env_val else root_text, None if anchored else os.getcwd())
    hit = _DIRS.get(key)
    if hit is None:
        if env_val:  # a relative env value is anchored at the caller's cwd
            base = str(Path.cwd().resolve())
        else:
            base = str(Path(root).resolve() if root is not None else Path.cwd().resolve())
        d = _resolve(env_val, base)
        if len(_DIRS) >= _DIRS_MAX:
            _DIRS.clear()
        hit = _DIRS[key] = (d, os.path.join(d, ".gitignore"))
    d, ignore_text = hit
    if create and not os.path.exists(ignore_text):  # the directory and the file are made only when the file is missing
        d.mkdir(parents=True, exist_ok=True)
        try:
            with (d / ".gitignore").open("x", encoding="utf-8") as fh:
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


def append_text(path: str | os.PathLike[str], text: str) -> None:
    """Append `text` (UTF-8) to `path`, creating it, with one write(2) on an O_APPEND descriptor: the record of a call
    reaches the file whole and in call order, and nothing is buffered beyond the call (a SIGKILL after the return loses nothing).
    The bytes are those of `open(path, "a", encoding="utf-8").write(text)`; a text that cannot be encoded raises
    UnicodeEncodeError after the file was opened and created, as before."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o666)
    try:
        view = memoryview(text.encode("utf-8"))
        while view:  # a regular file takes the whole record in one call; only a signal or a full device splits it
            view = view[os.write(fd, view):]
    finally:
        os.close(fd)
