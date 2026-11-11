#!/usr/bin/env python3
"""Copy the Grok Bot seat map and lane skills into another tree.

Writes only under ``<target>/grokbot/``:

- ``grokbot/swarm/seat-map.json``
- ``grokbot/skills/swarm-<lane>/SKILL.md`` for each generated lane
- ``grokbot/skills/swarm-cloud-dispatch/SKILL.md`` (the hand-written dispatch skill; this installer owns that copy)
- ``grokbot/.agent-swarm-grokbot.json`` (provenance: sha256 of the bytes of each file this installer wrote)

A file that already matches is left untouched. A file this installer previously wrote (the
stamp records its sha256) is updated when the source changes. A differing file with no such
record is refused, and nothing is written. A file the stamp does not record is never deleted.

Symlinks on the target path or on a destination are refused the same way as
``_install_cursor.logical_target`` and ``_install_omp.unsafe_destinations``. The byte
replace, backup and restore steps are the helpers fixed in ``_install_cursor`` (stage, then
``os.replace``; a failed step restores from the backup and does not truncate the live file).

The target must be an existing directory outside this agent-swarm checkout and not ``$HOME``.
Desk Lead runs this against the box or programming-desk only after Ming approves. This
script does not perform that install.

``--dry-run`` lists the paths and writes nothing. ``--check`` exits 1 when the target would
change and writes nothing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from _install_cursor import (  # noqa: E402
    BackupCleanupIncomplete,
    CursorInstallError,
    ExistingBackup,
    RestoreIncomplete,
    _backup_path,
    _drop_backup,
    _open_nofollow_read,
    _publish,
    _rollback,
    _save_backup,
    logical_target,
    require_real_dirs,
)
from _install_omp import UnsafeDestination, unsafe_destinations  # noqa: E402

STAMP_REL = "grokbot/.agent-swarm-grokbot.json"
STAMP_INSTALLER = "agent-swarm-grokbot"
DISPATCH_REL = "grokbot/skills/swarm-cloud-dispatch/SKILL.md"
SEAT_MAP_REL = "grokbot/swarm/seat-map.json"


class GrokbotInstallError(CursorInstallError):
    """The install cannot proceed. Exit 2, nothing written."""


def _text_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _bytes_sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stamp_text(files: dict[str, str]) -> str:
    payload = {
        "installer": STAMP_INSTALLER,
        "files": {rel: _text_sha(files[rel]) for rel in sorted(files)},
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _declared_lanes(seat_text: str, missing: list[str]) -> set[str]:
    """Lanes named by the seat map. An unreadable map is a missing source, not an empty export."""
    try:
        seat_doc = json.loads(seat_text)
    except json.JSONDecodeError:
        missing.append(f"{SEAT_MAP_REL} (not valid JSON)")
        return set()
    lanes: set[str] = set()
    roles = seat_doc.get("roles") if isinstance(seat_doc, dict) else None
    if isinstance(roles, list):
        for role in roles:
            lane = role.get("lane") if isinstance(role, dict) else None
            if isinstance(lane, str) and lane:
                lanes.add(lane)
    if not lanes:
        missing.append(f"{SEAT_MAP_REL} (no lanes)")
    return lanes


def export_sources(source: Path | None = None) -> dict[str, str]:
    """Rel path under the target → file text. Includes the dispatch skill. No stamp."""
    root = source or ROOT
    files: dict[str, str] = {}
    missing: list[str] = []
    seat = root / SEAT_MAP_REL
    if not seat.is_file() or seat.is_symlink():
        missing.append(SEAT_MAP_REL)
    else:
        files[SEAT_MAP_REL] = seat.read_text(encoding="utf-8")
    skills = root / "grokbot" / "skills"
    if skills.is_dir() and not skills.is_symlink():
        for skill in sorted(skills.glob("swarm-*/SKILL.md")):
            rel = skill.relative_to(root).as_posix()
            if skill.is_symlink() or skill.parent.is_symlink():
                missing.append(rel)
                continue
            files[rel] = skill.read_text(encoding="utf-8")
    else:
        missing.append("grokbot/skills")
    if DISPATCH_REL not in files:
        missing.append(DISPATCH_REL)
    if SEAT_MAP_REL in files:
        lanes = _declared_lanes(files[SEAT_MAP_REL], missing)
        for lane in sorted(lanes):
            rel = f"grokbot/skills/swarm-{lane}/SKILL.md"
            if rel not in files:
                missing.append(rel)
    if missing:
        raise GrokbotInstallError(
            "error: Grok Bot export sources missing or symlinked: " + ", ".join(dict.fromkeys(missing))
            + ". Run python3 scripts/build_agents.py in the agent-swarm checkout first. Nothing was written."
        )
    return files


def _dest(target: Path, rel: str) -> Path:
    if rel.startswith("/") or ".." in Path(rel).parts or not rel.startswith("grokbot/"):
        raise GrokbotInstallError(f"error: refusing to write outside grokbot/: {rel}. Nothing was written.")
    dest = target / rel
    root = target / "grokbot"
    if not dest.is_relative_to(root):
        raise GrokbotInstallError(f"error: refusing to write outside grokbot/: {rel}. Nothing was written.")
    return dest


def _managed(rel: str) -> bool:
    """A file this installer may remove when the export no longer produces it."""
    parts = Path(rel).parts
    if parts[:2] == ("grokbot", "swarm") and len(parts) == 3:
        return True
    return (
        len(parts) == 4
        and parts[:2] == ("grokbot", "skills")
        and parts[2].startswith("swarm-")
        and parts[3] == "SKILL.md"
    )


def _read_stamp(path: Path, root: Path) -> tuple[str | None, dict | None]:
    blob = _open_nofollow_read(path, root)
    if blob is None:
        return None, None
    try:
        raw = blob.decode("utf-8")
    except UnicodeDecodeError:
        return "\0", None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw, None
    return raw, data if isinstance(data, dict) else None


def _same_text(dest: Path, content: str, root: Path) -> bool:
    try:
        blob = _open_nofollow_read(dest, root)
    except (UnsafeDestination, OSError):
        return False
    return blob is not None and blob == content.encode("utf-8")


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
        if not isinstance(prev, str) or prev != _bytes_sha(dest.read_bytes()):
            return False
    return True


def _recorded_rels(target: Path) -> list[str]:
    try:
        _raw, stamp = _read_stamp(target / STAMP_REL, target)
    except (OSError, UnsafeDestination):
        return []
    files = stamp.get("files") if isinstance(stamp, dict) else None
    if not isinstance(files, dict):
        return []
    return [rel for rel in files if isinstance(rel, str)]


def plan_install(target: Path, sources: dict[str, str]) -> tuple[dict[str, str], list[str], list[str], list[str]]:
    """(writes, refusals, removals, kept). See the module docstring."""
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
        if _same_text(dest, content, target):
            continue
        prev = recorded.get(rel) if isinstance(recorded, dict) else None
        current = _bytes_sha(dest.read_bytes()) if dest.is_file() else None
        if isinstance(prev, str) and prev == current:
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
            if rel in sources or rel == STAMP_REL or not _managed(rel):
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
            if _bytes_sha(dest.read_bytes()) == prev:
                removals.append(rel)
            else:
                kept.append(rel)
    if refusals:
        return {}, refusals, [], []
    return writes, [], removals, kept


def _check_symlinks(target: Path, sources: dict[str, str]) -> None:
    """Refuse a symlink on a managed path or on grokbot/ itself, before any read of a candidate."""
    rels = list(sources) + [STAMP_REL, *_recorded_rels(target)]
    dests = [_dest(target, rel) for rel in rels]
    dests.append(target / "grokbot")
    dests.append(target / "grokbot" / "skills")
    dests.append(target / "grokbot" / "swarm")
    reasons = unsafe_destinations(target, dests)
    if reasons:
        raise UnsafeDestination("; ".join(reasons))


def _backup_conflicts(dest_root: Path, writes: dict[str, str], removals: list[str]) -> list[Path]:
    dests = [_dest(dest_root, rel) for rel in sorted(writes)]
    dests.extend(_dest(dest_root, rel) for rel in removals)
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
    The restore goes through ``_install_cursor._rollback`` (staged ``os.replace``).
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
            if not _managed(rel):
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


def install_grokbot(
    target: Path | str,
    *,
    dry_run: bool = False,
    check: bool = False,
    source: Path | None = None,
    out=None,
    err=None,
) -> int:
    """Copy the Grok Bot export into ``target``. Returns 0, 1 (``--check`` drift), or 2 (refused)."""
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
        print(f"error: {exc}. Restored the previous Grok Bot export; the stamp was left unchanged.", file=stderr)
        return 2
    if paths or removals:
        print(f"installed {len(paths)} file(s) under {dest_root / 'grokbot'}", file=stderr)
    else:
        print("up-to-date", file=stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", required=True, help="existing directory to copy the Grok Bot export into")
    ap.add_argument("--dry-run", action="store_true", help="print the grokbot/** paths that would be written; write nothing")
    ap.add_argument("--check", action="store_true", help="exit 1 if the target would change; write nothing")
    args = ap.parse_args(argv)
    return install_grokbot(args.target, dry_run=args.dry_run, check=args.check)


if __name__ == "__main__":
    sys.exit(main())
