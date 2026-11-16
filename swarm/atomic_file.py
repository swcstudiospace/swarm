"""Atomic text-file replacement for the JSON projections (state, claude state, cancel flag).

The temp file sits beside the target and is renamed over it. Its name carries a random suffix as well as the pid:
threads of one process share a pid, so a pid-only (or fixed) name lets two writers clobber each other's temp file and
the loser's rename fails or publishes a torn file. Guarantee: a reader sees the old or the new complete file, never
a partial one. No fsync: these files are projections that are rebuilt, so durability across power loss is not bought.
"""

from __future__ import annotations
import os
import uuid
from pathlib import Path


def write_text_atomic(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write `text` to `path` via a unique hidden sibling and `os.replace`. The parent directory must exist."""
    # Plain file creation (not mkstemp) so the umask-derived mode matches a normal write_text.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(text, encoding=encoding)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
