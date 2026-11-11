#!/usr/bin/env python3
"""Copy the generated Cursor agents into another repo. No substrate, no MCP, no env files.

Writes only under ``<target>/.cursor/``:

- ``.cursor/agents/<slug>.md`` for each agents.json slug
- ``.cursor/rules/agent-swarm.mdc``
- ``.cursor/.agent-swarm-cursor.json`` (provenance: sha256 of each file this installer wrote)

A file that already matches is left untouched. A file this installer previously wrote (the
provenance stamp records its sha256) is updated when the source changes. A differing file
with no such record is refused, and nothing is written. Symlinks on the target path or on
a destination are refused the same way as ``_install_omp.unsafe_destinations``.

A real install stages each managed file in its own directory, replaces those files, then
publishes the stamp last with ``os.replace``. Each replaced or removed file's previous
bytes are kept in a sibling backup until the install succeeds. A backup path that
already exists is refused before any managed file changes. A failed step restores
from that backup via a staged ``os.replace`` (never truncating the live file in place)
and leaves the previous stamp. If one restore fails, the rest still run; backups of
unrestored files stay, and stderr names them. After the new stamp is published, a
backup that cannot be removed is named on stderr and the new export stays in place.
When the stamp is ours, a regular file
it records under ``.cursor/agents/`` that this export no longer produces is removed if its
sha256 still matches; a locally edited file, including one that is not valid UTF-8, is
left in place and named on stderr.

The target must be an existing directory outside this agent-swarm checkout and not ``$HOME``.
A follow-up run against swcstudiospace/programming-desk uses this script; that repo does not
vendor the swarm runtime.

``python3 scripts/build_agents.py --install-cursor <dir>`` calls ``install_cursor`` and does
not regenerate agents and does not run the substrate step.
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from swarm.manifest import load_manifest  # noqa: E402
from _install_omp import UnsafeDestination, unsafe_destinations  # noqa: E402

STAMP_REL = ".cursor/.agent-swarm-cursor.json"
STAMP_INSTALLER = "agent-swarm-cursor"
RULE_REL = ".cursor/rules/agent-swarm.mdc"
_BACKUP_SUFFIX = ".agent-swarm-bak"


class CursorInstallError(Exception):
    """The install cannot proceed. Exit 2, nothing written."""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stamp_text(files: dict[str, str]) -> str:
    payload = {
        "installer": STAMP_INSTALLER,
        "files": {rel: _sha(files[rel]) for rel in sorted(files)},
    }
    return json.dumps(payload, indent=2) + "\n"


def export_sources(source: Path | None = None) -> dict[str, str]:
    """Rel path under the target → file text, for the agents and the rule. No stamp."""
    root = source or ROOT
    files: dict[str, str] = {}
    missing: list[str] = []
    for agent in load_manifest(root / "agents.json"):
        rel = f".cursor/agents/{agent['slug']}.md"
        path = root / rel
        if not path.is_file() or path.is_symlink():
            missing.append(rel)
            continue
        files[rel] = path.read_text(encoding="utf-8")
    rule = root / RULE_REL
    if not rule.is_file() or rule.is_symlink():
        missing.append(RULE_REL)
    else:
        files[RULE_REL] = rule.read_text(encoding="utf-8")
    if missing:
        raise CursorInstallError(
            "error: Cursor export sources missing or symlinked: " + ", ".join(missing)
            + ". Run python3 scripts/build_agents.py in the agent-swarm checkout first. Nothing was written."
        )
    return files


def logical_target(raw: str) -> Path:
    """Absolute target directory. Refuse '..', a symlink on any component, a missing dir, $HOME, or this checkout."""
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    if ".." in p.parts:
        raise CursorInstallError(f"error: refusing target {raw}: the path contains '..'. Nothing was written.")
    cur = Path(p.parts[0])
    for part in p.parts[1:]:
        cur = cur / part
        if cur.is_symlink():
            raise UnsafeDestination(f"{cur} is a symlink")
    if not cur.is_dir():
        raise CursorInstallError(f"error: target {cur} is not an existing directory. Nothing was written.")
    home = Path.home()
    if cur == home:
        raise CursorInstallError(f"error: refusing target {cur}: it is $HOME. Nothing was written.")
    checkout = ROOT.resolve()
    try:
        inside = cur == checkout or cur.is_relative_to(checkout)
    except (OSError, ValueError):
        inside = False
    if inside:
        raise CursorInstallError(
            f"error: refusing target {cur}: it is inside the agent-swarm checkout, which already holds the "
            "generated Cursor agents. Install into the target repo. Nothing was written."
        )
    return cur


def _dest(target: Path, rel: str) -> Path:
    if not rel.startswith(".cursor/") or rel.startswith(".cursor/../") or ".." in Path(rel).parts:
        raise CursorInstallError(f"error: refusing to write outside .cursor/: {rel}. Nothing was written.")
    dest = target / rel
    cursor = target / ".cursor"
    if not dest.is_relative_to(cursor):
        raise CursorInstallError(f"error: refusing to write outside .cursor/: {rel}. Nothing was written.")
    return dest


def dir_refusal(root: Path, directory: Path) -> str | None:
    """Why ``directory``, or a directory between ``root`` (exclusive) and it, must not be used: a symlink, a
    non-directory, or a path outside ``root``. A missing component is fine: nothing below it exists yet. Every
    component is ``lstat``-ed, so a symlink anywhere on the chain is seen, not only the parent of a leaf. Reads only."""
    cur = directory
    while cur != root:
        if cur == cur.parent or not cur.is_relative_to(root):
            return f"{directory} is outside {root}"
        try:
            st = os.lstat(cur)
        except (FileNotFoundError, NotADirectoryError):
            cur = cur.parent
            continue
        except OSError as exc:
            return f"{cur} is unreadable ({exc.strerror})"
        if stat.S_ISLNK(st.st_mode):
            return f"{cur} is a symlink"
        if not stat.S_ISDIR(st.st_mode):
            return f"{cur} is not a directory"
        cur = cur.parent
    return None


def require_real_dirs(root: Path, directory: Path) -> None:
    """Raise ``UnsafeDestination`` unless ``directory`` and every directory down from ``root`` is real (``dir_refusal``)."""
    reason = dir_refusal(root, directory)
    if reason is not None:
        raise UnsafeDestination(reason)


def _open_nofollow_read(path: Path, root: Path) -> bytes | None:
    """Bytes of ``path``, or None when absent. Every directory from ``root`` down to ``path`` must be real
    (``require_real_dirs``); ``O_NOFOLLOW`` refuses a symlink at the leaf, even one swapped in after an ``is_symlink()``
    check, the same guard as ``build_agents._read_target``. A FIFO, device or directory is refused before it is read:
    a read would block or fail, and it is never an install file. A read error is refused, not raised raw."""
    require_real_dirs(root, path.parent)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise UnsafeDestination(f"{path} is a symlink") from None
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise UnsafeDestination(f"{path} is not a regular file")
        parts: list[bytes] = []
        while chunk := os.read(fd, 1 << 16):
            parts.append(chunk)
        return b"".join(parts)
    except OSError as exc:
        raise UnsafeDestination(f"{path} cannot be read ({exc.strerror})") from None
    finally:
        os.close(fd)


def _read_stamp(path: Path, root: Path) -> tuple[str | None, dict | None]:
    blob = _open_nofollow_read(path, root)
    if blob is None:
        return None, None
    raw = blob.decode("utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw, None
    return raw, data if isinstance(data, dict) else None


def _stamp_is_ours(stamp: dict | None, target: Path, sources: dict[str, str]) -> bool:
    if not stamp or stamp.get("installer") != STAMP_INSTALLER:
        return False
    recorded = stamp.get("files")
    if not isinstance(recorded, dict):
        return False
    for rel in sources:
        dest = _dest(target, rel)
        require_real_dirs(target, dest.parent)
        if dest.is_symlink():
            raise UnsafeDestination(f"{dest} is a symlink")
        if not dest.exists():
            continue
        prev = recorded.get(rel)
        if not isinstance(prev, str) or prev != _sha(dest.read_text(encoding="utf-8")):
            return False
    return True


def _agents_rel(rel: str) -> bool:
    """A managed agent file. Removals never leave ``.cursor/agents/``."""
    parts = Path(rel).parts
    name = parts[-1] if parts else ""
    return parts[:2] == (".cursor", "agents") and len(parts) == 3 and name.endswith(".md") and name not in (".", "..")


def plan_install(target: Path, sources: dict[str, str]) -> tuple[dict[str, str], list[str], list[str], list[str]]:
    """(writes, refusals, removals, kept).

    ``writes`` maps rel → new text. Refusals name files we must not overwrite.
    ``removals`` are installer-owned ``.cursor/agents/`` files the export no longer produces
    whose bytes still match the stamp. ``kept`` are recorded agent files we will not delete
    (local edits, or a symlink). An edited file is omitted from the new stamp: recording
    its new hash would make the next install treat the edit as ours and delete it.
    """
    expected = stamp_text(sources)
    stamp_path = _dest(target, STAMP_REL)
    raw, stamp = _read_stamp(stamp_path, target)
    ours = _stamp_is_ours(stamp, target, sources)
    recorded = stamp.get("files") if ours and isinstance(stamp, dict) else {}
    writes: dict[str, str] = {}
    refusals: list[str] = []
    for rel, content in sources.items():
        dest = _dest(target, rel)
        require_real_dirs(target, dest.parent)
        if dest.is_symlink():
            raise UnsafeDestination(f"{dest} is a symlink")
        if not dest.exists():
            writes[rel] = content
            continue
        current = dest.read_text(encoding="utf-8")
        if current == content:
            continue
        prev = recorded.get(rel) if isinstance(recorded, dict) else None
        if isinstance(prev, str) and prev == _sha(current):
            writes[rel] = content
        else:
            refusals.append(rel)
    if raw is None:
        writes[STAMP_REL] = expected
    elif raw != expected:
        if ours:
            writes[STAMP_REL] = expected
        else:
            refusals.append(STAMP_REL)
    removals: list[str] = []
    kept: list[str] = []
    if ours and isinstance(recorded, dict) and not refusals:
        for rel in sorted(recorded):
            if rel in sources or not _agents_rel(rel):
                continue
            prev = recorded.get(rel)
            if not isinstance(prev, str):
                continue
            dest = _dest(target, rel)
            require_real_dirs(target, dest.parent)
            if dest.is_symlink():
                kept.append(rel)
                continue
            if not dest.is_file():
                continue
            # byte hash: matches _sha() for unchanged UTF-8, and treats non-UTF-8 as a local edit
            if hashlib.sha256(dest.read_bytes()).hexdigest() == prev:
                removals.append(rel)
            else:
                kept.append(rel)
    if refusals:
        return {}, refusals, [], []
    return writes, [], removals, kept


def _staged_mode() -> int:
    """Mode of a file ``open()`` creates: 0o666 with the process umask applied. ``mkstemp`` creates 0600."""
    mask = os.umask(0)
    os.umask(mask)
    return 0o666 & ~mask


def _stage_text(dest: Path, text: str, root: Path) -> Path:
    """Temp file in ``dest``'s directory. Never created through a symlink, or under one below ``root``."""
    if dest.is_symlink():
        raise UnsafeDestination(f"{dest} is a symlink")
    parent = dest.parent
    require_real_dirs(root, parent)
    parent.mkdir(parents=True, exist_ok=True)
    require_real_dirs(root, parent)
    fd, name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".part", dir=os.fspath(parent))
    os.close(fd)
    path = Path(name)
    try:
        os.chmod(path, _staged_mode())
        path.write_text(text, encoding="utf-8")
    except Exception:
        if path.exists() and not path.is_symlink():
            path.unlink()
        raise
    return path


def _backup_path(dest: Path) -> Path:
    return dest.parent / f".{dest.name}{_BACKUP_SUFFIX}"


def _stage_bytes(parent: Path, prefix: str, data: bytes, root: Path) -> Path:
    """Temp file of ``data`` in ``parent``. Never created through a symlink, or under one below ``root``; not via write_text."""
    require_real_dirs(root, parent)
    parent.mkdir(parents=True, exist_ok=True)
    require_real_dirs(root, parent)
    fd, name = tempfile.mkstemp(prefix=prefix, suffix=".part", dir=os.fspath(parent))
    os.close(fd)
    path = Path(name)
    try:
        os.chmod(path, _staged_mode())
        path.write_bytes(data)
    except Exception:
        if path.exists() and not path.is_symlink():
            path.unlink()
        raise
    return path


def _replace_bytes(dest: Path, data: bytes, root: Path) -> None:
    """Replace ``dest`` from a staged temp. Does not truncate ``dest`` first."""
    if dest.is_symlink():
        raise UnsafeDestination(f"{dest} is a symlink")
    tmp = _stage_bytes(dest.parent, f".{dest.name}.", data, root)
    try:
        os.replace(tmp, dest)
    except Exception:
        if tmp.exists() and not tmp.is_symlink():
            tmp.unlink()
        raise


def _save_backup(dest: Path, data: bytes, root: Path) -> None:
    bak = _backup_path(dest)
    require_real_dirs(root, bak.parent)
    if bak.is_symlink():
        raise UnsafeDestination(f"{bak} is a symlink")
    if bak.exists():
        raise ExistingBackup([bak])
    _replace_bytes(bak, data, root)


def _drop_backup(dest: Path) -> None:
    bak = _backup_path(dest)
    if bak.is_symlink():
        raise UnsafeDestination(f"{bak} is a symlink")
    if bak.is_file():
        bak.unlink()


class RestoreIncomplete(Exception):
    """One or more files could not be put back. ``unrestored`` is (live path, backup path)."""

    def __init__(self, unrestored: list[tuple[Path, Path]]):
        self.unrestored = unrestored
        super().__init__(unrestored)


class ExistingBackup(Exception):
    """A sibling backup is already present. Nothing is overwritten."""

    def __init__(self, paths: list[Path]):
        self.paths = paths
        super().__init__(paths)


class BackupCleanupIncomplete(Exception):
    """The new export is in place, and one or more backups could not be removed."""

    def __init__(self, left: list[Path]):
        self.left = left
        super().__init__(left)


def _publish(dest: Path, text: str, replaced: list[tuple[Path, bytes | None]], root: Path) -> None:
    """Stage ``text`` and replace ``dest``. ``replaced`` records the previous bytes (None if new).

    Previous bytes are copied to a sibling backup before the replace. The backup stays until
    the install succeeds.
    """
    require_real_dirs(root, dest.parent)
    prev = dest.read_bytes() if dest.is_file() and not dest.is_symlink() else None
    if dest.exists() and (dest.is_symlink() or not dest.is_file()):
        raise UnsafeDestination(f"{dest} is a symlink") if dest.is_symlink() else OSError(f"{dest} is not a regular file")
    if prev is not None:
        _save_backup(dest, prev, root)
    try:
        tmp = _stage_text(dest, text, root)
        try:
            os.replace(tmp, dest)
        except Exception:
            if tmp.exists() and not tmp.is_symlink():
                tmp.unlink()
            raise
    except Exception:
        # dest was not replaced; drop the backup so a failed stage leaves no extra file
        if prev is not None:
            _drop_backup(dest)
        raise
    replaced.append((dest, prev))


def _restore_one(path: Path, data: bytes | None, root: Path) -> Path | None:
    """Put ``path`` back. Returns the backup path when this file could not be restored."""
    bak = _backup_path(path)
    try:
        if data is None:
            if path.is_symlink():
                raise UnsafeDestination(f"{path} is a symlink")
            if path.is_file():
                path.unlink()
        else:
            blob = bak.read_bytes() if bak.is_file() and not bak.is_symlink() else data
            _replace_bytes(path, blob, root)
    except (OSError, UnsafeDestination):
        return bak
    _drop_backup(path)
    return None


def _rollback(replaced: list[tuple[Path, bytes | None]], removed: list[tuple[Path, bytes]], root: Path) -> list[tuple[Path, Path]]:
    """Restore every file. A failure is recorded and the remaining files are still restored."""
    unrestored: list[tuple[Path, Path]] = []
    for path, data in reversed(removed):
        failed = _restore_one(path, data, root)
        if failed is not None:
            unrestored.append((path, failed))
    for path, prev in reversed(replaced):
        failed = _restore_one(path, prev, root)
        if failed is not None:
            unrestored.append((path, failed))
    return unrestored


def _backup_conflicts(dest_root: Path, writes: dict[str, str], removals: list[str]) -> list[Path]:
    """Backup paths this install would overwrite. Checked before any managed file changes."""
    dests: list[Path] = [_dest(dest_root, rel) for rel in sorted(r for r in writes if r != STAMP_REL)]
    dests.extend(_dest(dest_root, rel) for rel in removals if _agents_rel(rel))
    if STAMP_REL in writes:
        dests.append(_dest(dest_root, STAMP_REL))
    found: list[Path] = []
    for dest in dests:
        require_real_dirs(dest_root, dest.parent)
        if not (dest.is_file() and not dest.is_symlink()):
            continue
        bak = _backup_path(dest)
        if bak.is_symlink():
            raise UnsafeDestination(f"{bak} is a symlink")
        if bak.exists():
            found.append(bak)
    return found


def _apply(dest_root: Path, writes: dict[str, str], removals: list[str]) -> None:
    """Replace managed files, drop installer-owned leftovers, then publish the stamp last.

    On failure, restore every file already replaced or removed and leave the old stamp.
    Raises RestoreIncomplete when a restore itself fails; those backups are left in place.
    An existing backup is refused before the first replace. A backup that cannot be
    removed after the stamp is published raises BackupCleanupIncomplete and is left behind.
    """
    conflicts = _backup_conflicts(dest_root, writes, removals)
    if conflicts:
        raise ExistingBackup(conflicts)
    replaced: list[tuple[Path, bytes | None]] = []
    removed: list[tuple[Path, bytes]] = []
    try:
        for rel in sorted(r for r in writes if r != STAMP_REL):
            _publish(_dest(dest_root, rel), writes[rel], replaced, dest_root)
        for rel in removals:
            if not _agents_rel(rel):
                continue
            path = _dest(dest_root, rel)
            require_real_dirs(dest_root, path.parent)
            if path.is_symlink() or not path.is_file():
                continue
            data = path.read_bytes()
            _save_backup(path, data, dest_root)
            try:
                path.unlink()
            except OSError:
                # The live file is still in place, so this backup is unused. Leaving it
                # would make the next install refuse the path (r4224206191).
                try:
                    _drop_backup(path)
                except (OSError, UnsafeDestination):
                    pass
                raise
            removed.append((path, data))
        if STAMP_REL in writes:
            _publish(_dest(dest_root, STAMP_REL), writes[STAMP_REL], replaced, dest_root)
    except (OSError, UnsafeDestination, ExistingBackup) as exc:
        unrestored = _rollback(replaced, removed, dest_root)
        if unrestored:
            raise RestoreIncomplete(unrestored) from exc
        raise
    else:
        left: list[Path] = []
        for path, prev in replaced:
            if prev is None:
                continue
            try:
                require_real_dirs(dest_root, path.parent)
                _drop_backup(path)
            except (OSError, UnsafeDestination):
                left.append(_backup_path(path))
        for path, _data in removed:
            try:
                require_real_dirs(dest_root, path.parent)
                _drop_backup(path)
            except (OSError, UnsafeDestination):
                left.append(_backup_path(path))
        if left:
            raise BackupCleanupIncomplete(left)


def _recorded_agent_rels(target: Path) -> list[str]:
    """Agent paths the previous stamp records. Empty when there is no stamp of ours."""
    try:
        _raw, stamp = _read_stamp(target / STAMP_REL, target)
    except (OSError, UnicodeDecodeError, UnsafeDestination):
        return []
    files = stamp.get("files") if isinstance(stamp, dict) else None
    if not isinstance(files, dict):
        return []
    return [rel for rel in files if isinstance(rel, str) and _agents_rel(rel)]


def _check_symlinks(target: Path, sources: dict[str, str]) -> None:
    """Refuse a symlink on a new file, a recorded agent path, or ``.cursor`` / ``.cursor/agents``.

    Recorded agent paths are included so an export that produces no agents still checks the
    directory it would delete from. Runs before plan_install reads a retirement candidate.
    """
    rels = list(sources) + [STAMP_REL, *(_recorded_agent_rels(target))]
    dests = [_dest(target, rel) for rel in rels]
    dests.append(target / ".cursor")
    dests.append(target / ".cursor" / "agents")
    reasons = unsafe_destinations(target, dests)
    if reasons:
        raise UnsafeDestination("; ".join(reasons))


def install_cursor(
    target: Path | str,
    *,
    dry_run: bool = False,
    check: bool = False,
    source: Path | None = None,
    out=None,
    err=None,
) -> int:
    """Copy the Cursor export into ``target``. Returns 0 (ok or no-op), 1 (``--check`` drift), or 2 (refused)."""
    stdout = out or sys.stdout
    stderr = err or sys.stderr
    if dry_run and check:
        print("error: --dry-run and --check both write nothing; pass only one.", file=stderr)
        return 2
    try:
        dest_root = logical_target(os.fspath(target))
        sources = export_sources(source)
        _check_symlinks(dest_root, sources)
        writes, refusals, removals, kept = plan_install(dest_root, sources)
    except UnsafeDestination as exc:
        print(
            f"error: refusing to install into {target}: {exc}; the installer never writes through a symlink. "
            "Nothing was written.",
            file=stderr,
        )
        return 2
    except CursorInstallError as exc:
        print(exc, file=stderr)
        return 2
    except (OSError, UnicodeDecodeError) as exc:
        print(f"error: {exc}. Nothing was written.", file=stderr)
        return 2
    if refusals:
        print(
            "error: refusing to overwrite "
            + ", ".join(refusals)
            + ": the file differs and this installer did not write it. Nothing was written.",
            file=stderr,
        )
        return 2
    paths = sorted(writes)
    for rel in kept:
        why = "symlink" if _dest(dest_root, rel).is_symlink() else "local edits"
        print(f"kept: {rel} ({why}; left in place)", file=stderr)
    if check:
        if paths or removals:
            print("stale:", ", ".join(paths), file=stdout)
        else:
            print("up-to-date", ", ".join(paths), file=stdout)
        for rel in removals:
            print(f"remove: {rel}", file=stdout)
        return 1 if paths or removals else 0
    if dry_run:
        for rel in paths:
            print(rel, file=stdout)
        for rel in removals:
            print(f"remove: {rel}", file=stdout)
        if not paths and not removals:
            print("up-to-date", file=stderr)
        return 0
    try:
        _apply(dest_root, writes, removals)
    except BackupCleanupIncomplete as exc:
        named = ", ".join(os.fspath(path) for path in exc.left)
        print(
            f"error: install completed but left backups behind: {named}. The new export is in place.",
            file=stderr,
        )
        return 2
    except ExistingBackup as exc:
        named = ", ".join(os.fspath(path) for path in exc.paths)
        print(
            f"error: refusing to install: backup already exists: {named}. Nothing was written.",
            file=stderr,
        )
        return 2
    except RestoreIncomplete as exc:
        named = ", ".join(f"{path} (backup {bak})" for path, bak in exc.unrestored)
        print(
            f"error: restore incomplete: {named}. The stamp was left unchanged.",
            file=stderr,
        )
        return 2
    except UnsafeDestination as exc:
        print(
            f"error: refusing to install into {target}: {exc}; the installer never writes through a symlink. "
            "Nothing was written.",
            file=stderr,
        )
        return 2
    except OSError as exc:
        print(f"error: {exc}. Restored the previous Cursor export; the stamp was left unchanged.", file=stderr)
        return 2
    if paths or removals:
        print(f"installed {len(paths)} file(s) under {dest_root / '.cursor'}", file=stderr)
    else:
        print("up-to-date", file=stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", required=True, help="existing repo directory to copy .cursor/ agents into")
    ap.add_argument("--dry-run", action="store_true", help="print the .cursor/** paths that would be written; write nothing")
    ap.add_argument("--check", action="store_true", help="exit 1 if the target would change; write nothing")
    args = ap.parse_args(argv)
    return install_cursor(args.target, dry_run=args.dry_run, check=args.check)


if __name__ == "__main__":
    sys.exit(main())
