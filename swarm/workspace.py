"""What an installed swarm workspace holds for the substrate (ADR 0001 S4; INST-01..04), shared by the installer
(`scripts/_install_substrate.py`), the runner (`scripts/swarm_run.py`) and the lease bridge (`swarm/substrate_lease.py`).

Two things, one per side of the token:

1. **The MCP entry.** `substrate_mcp.json` is the spec agent-substrate's projector emits
   (`bun packages/projector/src/swarm-workspace.ts --spec`), vendored because this repository is public and installs
   without that private checkout. It names the file each runtime reads and the entry, which spells the token as
   `${SUBSTRATE_TOKEN}`; each runtime expands it from its own environment.

2. **The agent's env file.** One per workspace and agent, outside the workspace and every git checkout:

       ${XDG_CONFIG_HOME:-~/.config}/agent-swarm/agents/<workspace key>/<slug>.env     (dir 0700, file 0600)

   holding exactly `SUBSTRATE_TOKEN=<that agent's token>`. The client name is `SUBSTRATE_TOKEN`; the server knows the
   same secret as `SUBSTRATE_TOKEN_<SURFACE>` (docs/substrate-workspace.md). The runner starts each agent session with
   this token and no other, and holds that agent's lease with it.

0600 separates OS accounts, not processes of one account: every agent `swarm_run.py` starts runs as the same user and
could read its siblings' files. The identities are attributable, not unforgeable, among agents sharing an account.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Mapping

from .manifest import get_agent

SPEC: dict[str, Any] = json.loads((Path(__file__).resolve().parent / "substrate_mcp.json").read_text(encoding="utf-8"))
SERVER: str = SPEC["server"]
# Claude Code's name for every tool of the server; it goes in an agent's `tools:` and in `--allowedTools`.
CLAUDE_TOOLS = f"mcp__{SERVER}"
TOKEN = "SUBSTRATE_TOKEN"


class EnvFileProblem(ValueError):
    """An agent env file that cannot be used, with what the operator has to fix."""


def carries_token(key: str) -> bool:
    """Whether an environment variable can hand a process a token that is not its own: the client's
    `SUBSTRATE_TOKEN`, the server-side `SUBSTRATE_TOKEN_<SURFACE>` names, and the operator list."""
    return key.startswith(TOKEN) or key == "SUBSTRATE_OPERATOR_TOKENS"


def server_var(surface: str) -> str:
    """`swarm-a05-be` -> `SUBSTRATE_TOKEN_SWARM_A05_BE`: where the server, and the installer, read that agent's secret."""
    return f"{TOKEN}_{surface.upper().replace('-', '_')}"


def mcp_config(workspace: str | Path, runtime: str) -> Path:
    """The project file `runtime` reads the substrate entry from."""
    path: str = SPEC["runtimes"][runtime]["path"]
    return Path(workspace) / path


def projected_mcp(workspace: str | Path, runtime: str) -> bool:
    """Only the exact projected server, including its environment references, authorizes automatic MCP flags."""
    path = mcp_config(workspace, runtime)
    try:
        raw = file_state(path)
        if raw is None:
            return False
        if SPEC["runtimes"][runtime]["format"] == "toml":
            doc = tomllib.loads(raw.data.decode("utf-8"))
            key, expected = "mcp_servers", SPEC["toml"]["entry"]
        else:
            doc = json.loads(raw.data)
            key, expected = "mcpServers", SPEC["json"]
        servers = doc.get(key) if isinstance(doc, dict) else None
        return isinstance(servers, dict) and servers.get(SERVER) == expected
    except (OSError, ValueError):
        return False


def env_dir(workspace: str | Path, env: Mapping[str, str] | None = None) -> Path:
    """Where the workspace's agent env files live. Keyed by a hash of the workspace's real path, so installing a second
    workspace, which may talk to another substrate, never overwrites the first one's tokens."""
    e = os.environ if env is None else env
    xdg = (e.get("XDG_CONFIG_HOME") or "").strip()
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else Path(e.get("HOME") or Path.home()) / ".config"
    key = hashlib.sha256(str(Path(workspace).resolve()).encode("utf-8")).hexdigest()[:16]
    return base / "agent-swarm" / "agents" / key


def env_file(workspace: str | Path, slug: str, env: Mapping[str, str] | None = None) -> Path:
    return env_dir(workspace, env) / f"{slug}.env"


def valid_token(value: str) -> bool:
    """A token goes into one env-file line and one HTTP header, so it may hold no whitespace or control characters."""
    return bool(value) and all(c.isprintable() and not c.isspace() for c in value)


def _private_directory(path: Path, st: os.stat_result) -> None:
    if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
        raise EnvFileProblem(
            f"{path} must be a private directory owned by this user (0700); fix its ownership/permissions "
            "or choose another XDG_CONFIG_HOME, then re-run; the installer never changes existing directory permissions")


def _open_directory(path: Path, *, create: bool = False, private: bool = False,
                    created: list[tuple[int, str, os.stat_result]] | None = None) -> int:
    """Walk from the root with pinned, no-follow directory descriptors, never through an ancestor symlink."""
    path = path.absolute()
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    current = Path(path.anchor)
    try:
        for part in path.parts[1:]:
            current /= part
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, 0o700 if private else 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
                else:
                    if created is not None:
                        created.append((os.dup(fd), part, os.stat(part, dir_fd=fd, follow_symlinks=False)))
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        if private:
            _private_directory(path, os.fstat(fd))
        return fd
    except BaseException:
        os.close(fd)
        raise


def check_env_directory(path: Path) -> None:
    """Read-only preflight; a missing directory can be created, but an existing unsafe directory cannot be repaired."""
    try:
        fd = _open_directory(path, private=True)
    except FileNotFoundError:
        return
    except OSError:
        raise EnvFileProblem(
            f"{path} cannot be opened without following a symlink; use an accessible, user-owned private directory "
            "(0700), or choose another XDG_CONFIG_HOME") from None
    os.close(fd)


@dataclass(frozen=True)
class FileState:
    identity: tuple[int, ...]
    mode: int
    uid: int
    data: bytes = field(repr=False)


def _file_state(fd: int, name: str) -> FileState | None:
    try:
        source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        return None
    with os.fdopen(source, "rb") as stream:
        st = os.fstat(stream.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise EnvFileProblem(f"{name} is not a regular file")
        data = stream.read()
        after = os.fstat(stream.fileno())
        if (st.st_mtime_ns, st.st_ctime_ns, st.st_size) != (after.st_mtime_ns, after.st_ctime_ns, after.st_size):
            raise EnvFileProblem(f"{name} changed while being read; re-run")
    return FileState((st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size), stat.S_IMODE(st.st_mode), st.st_uid, data)


def file_state(path: Path, *, private: bool = False) -> FileState | None:
    try:
        fd = _open_directory(path.parent, private=private)
    except FileNotFoundError:
        return None
    try:
        return _file_state(fd, path.name)
    finally:
        os.close(fd)


def token_value(path: Path, state: FileState | None) -> str:
    if state is None:
        raise EnvFileProblem(f"{path} does not exist")
    if state.uid != os.getuid():
        raise EnvFileProblem(f"{path} is not owned by this user")
    if state.mode & 0o077:
        raise EnvFileProblem(f"{path} is mode {state.mode:04o}; it must be 0600")
    try:
        lines = [line for line in state.data.decode("utf-8").splitlines() if line.strip()]
    except UnicodeError:
        raise EnvFileProblem(f"{path} must hold a UTF-8 token line") from None
    value = lines[0].removeprefix(f"{TOKEN}=") if len(lines) == 1 and lines[0].startswith(f"{TOKEN}=") else ""
    if not valid_token(value):
        raise EnvFileProblem(f"{path} must hold exactly one line {TOKEN}=<token> and nothing else")
    return value


def read_token(path: Path) -> str:
    """Read only through safe ancestors and a private, user-owned env directory, even for a valid 0600 token."""
    return token_value(path, file_state(path, private=True))


def _stage_file(fd: int, data: bytes, mode: int) -> tuple[str, FileState]:
    for _ in range(16):
        name = f".substrate-{secrets.token_hex(16)}.tmp"
        try:
            target = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        except FileExistsError:
            continue
        try:
            with os.fdopen(target, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fchmod(stream.fileno(), mode)
            state = _file_state(fd, name)
            if state is None:
                raise EnvFileProblem("substrate staging file disappeared; re-run")
            return name, state
        except BaseException:
            os.unlink(name, dir_fd=fd)
            raise
    raise FileExistsError("cannot allocate an exclusive substrate staging file")


@dataclass
class _Write:
    path: Path
    fd: int
    private: bool
    before: FileState | None = field(repr=False)
    staged: str = ""
    restore: str = ""
    installed: FileState | None = field(default=None, repr=False)
    committed: bool = False


class FileTransaction:
    """Bounded install transaction: stage every new/old file first, then publish; roll back only our own versions.

    Directory descriptors pin all I/O, including rollback and secret cleanup, if a pathname is exchanged meanwhile.
    This is process-failure rollback, not a crash-recovery journal or a multi-file atomic view for concurrent readers.
    """

    def __init__(self) -> None:
        self.writes: list[_Write] = []
        self.created: list[tuple[int, str, os.stat_result]] = []
        self.complete = False

    def __enter__(self) -> FileTransaction:
        return self

    def stage(self, path: Path, data: bytes, *, private: bool = False,
              expected: FileState | None = None, check_expected: bool = False) -> None:
        fd = _open_directory(path.parent, create=True, private=private, created=self.created)
        item = _Write(path, fd, private, None)
        self.writes.append(item)
        item.before = _file_state(fd, path.name)
        if check_expected and item.before != expected:
            raise EnvFileProblem(f"{path} changed while the install ran; re-run")
        mode = 0o600 if private else item.before.mode if item.before else 0o644
        item.staged, item.installed = _stage_file(fd, data, mode)
        if item.before is not None:
            item.restore, _ = _stage_file(fd, item.before.data, item.before.mode)

    def _check(self, item: _Write) -> None:
        current = _open_directory(item.path.parent, private=item.private)
        try:
            a, b = os.fstat(current), os.fstat(item.fd)
            if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino) or _file_state(item.fd, item.path.name) != item.before:
                raise EnvFileProblem(f"{item.path} changed while the install ran; re-run")
            if _file_state(item.fd, item.staged) != item.installed:
                raise EnvFileProblem(f"{item.path}: staging file changed while the install ran; re-run")
        finally:
            os.close(current)

    def commit(self) -> None:
        for item in self.writes:
            self._check(item)
        for item in self.writes:
            self._check(item)
            os.replace(item.staged, item.path.name, src_dir_fd=item.fd, dst_dir_fd=item.fd)
            item.staged = ""
            item.committed = True
        self.complete = True

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        problems = []
        try:
            if not self.complete:
                for item in reversed(self.writes):
                    if not item.committed:
                        continue
                    try:
                        if _file_state(item.fd, item.path.name) != item.installed:
                            problems.append(f"{item.path}: external intervening change preserved")
                        elif item.restore:
                            os.replace(item.restore, item.path.name, src_dir_fd=item.fd, dst_dir_fd=item.fd)
                            item.restore = ""
                        else:
                            os.unlink(item.path.name, dir_fd=item.fd)
                    except (OSError, EnvFileProblem):
                        if item.restore:
                            try:
                                flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
                                rfd = os.open(item.restore, flags, dir_fd=item.fd)
                                try:
                                    rst = os.fstat(rfd)
                                    if stat.S_ISREG(rst.st_mode):
                                        os.fchmod(rfd, 0o600)
                                finally:
                                    os.close(rfd)
                            except OSError:
                                pass
                            recovery = item.path.parent / item.restore
                            item.restore = ""  # retain on disk: do not unlink in cleanup
                            problems.append(f"{item.path}: rollback could not restore this file; recovery copy retained at {recovery}")
                        else:
                            problems.append(f"{item.path}: rollback could not restore this file")
        finally:
            for item in self.writes:
                for name in (item.staged, item.restore):
                    if name:
                        try:
                            os.unlink(name, dir_fd=item.fd)
                        except FileNotFoundError:
                            pass
                        except OSError:
                            problems.append(f"{item.path}: could not remove a staging file")
                os.close(item.fd)
            for fd, name, st in reversed(self.created):
                try:
                    now = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if not self.complete and (now.st_dev, now.st_ino) == (st.st_dev, st.st_ino):
                        os.rmdir(name, dir_fd=fd)  # only empty directories we created
                except OSError:
                    pass
                finally:
                    os.close(fd)
        if problems:
            raise EnvFileProblem("; ".join(problems)) from exc


def write_token(path: Path, token: str) -> None:
    """Publish a token atomically, private from the first byte; never repair an unsafe existing env directory."""
    if not valid_token(token):
        raise EnvFileProblem(f"refusing to write an empty or multi-line token to {path}")
    with FileTransaction() as transaction:
        transaction.stage(path, f"{TOKEN}={token}\n".encode("utf-8"), private=True)
        transaction.commit()


def credential(agent_id: str | None, workspace: str | Path | None,
               env: Mapping[str, str] | None = None) -> tuple[str | None, str, str]:
    """(token, source, problem): THE lookup of an agent's own token, shared by its session (`child_env`), the leases the
    runner holds for it, its handoffs and the event tee, so all four always speak as the same identity.

    1. Its env file for `workspace`, when that file exists. A file that exists but is unusable (mode, owner, content)
       refuses: the operator put it there and it is wrong, so nothing else is tried.
    2. Else `SUBSTRATE_TOKEN_<SURFACE>` in `env`: the same secret under the server's name, deliberately given to this
       process (a pre-S4 runner holding every agent's token).
    3. Else `SUBSTRATE_TOKEN` in `env` only when running inside this agent's own child session (`SWARM_AGENT_SESSION == "1"`
       and `SWARM_AGENT == slug`), where `child_env` stripped the server-specific variables.
    4. Else nothing. Never `SUBSTRATE_TOKEN` in the runner: that is A01's, and a claim or a run-log row of another
       agent sent with it would be misattributed, or refused for naming another surface."""
    from .substrate_tee import AGENT_SURFACES  # lazy: the tee imports this module

    e = os.environ if env is None else env
    surface = AGENT_SURFACES.get(agent_id or "")
    if surface is None:
        return None, "", f"no substrate surface for agent {agent_id!r}"
    path = None
    if workspace is not None:
        try:
            path = env_file(workspace, get_agent(agent_id or "")["slug"], e)
        except KeyError:
            return None, "", f"no agent {agent_id!r} in the manifest"
        try:
            check_env_directory(path.parent)
        except (EnvFileProblem, OSError) as exc:
            return None, str(path), str(exc)
        if os.path.lexists(path):
            try:
                return read_token(path), str(path), ""
            except (EnvFileProblem, OSError) as exc:
                return None, str(path), str(exc)
    var = server_var(surface)
    given = (e.get(var) or "").strip()
    if valid_token(given):
        return given, f"${var}", ""
    try:
        slug = get_agent(agent_id or "")["slug"]
    except KeyError:
        slug = ""
    if (e.get("SWARM_AGENT_SESSION") or "").strip() == "1" and slug and (e.get("SWARM_AGENT") or "").strip() == slug:
        session_token = (e.get(TOKEN) or "").strip()
        if valid_token(session_token):
            return session_token, f"${TOKEN}", ""
    return None, "", f"{path or 'no workspace'}{' does not exist' if path else ''} and {var} is unset"


def child_env(base: Mapping[str, str], workspace: str | Path, agent_id: str) -> tuple[dict[str, str], str, str]:
    """(env, token source, problem) for one agent process: `base` without any variable that carries a token, plus that
    agent's own `SUBSTRATE_TOKEN` from `credential` when it has one."""
    env = {k: v for k, v in base.items() if not carries_token(k)}
    if (base.get("SUBSTRATE_DISABLED") or "").strip() == "1":
        env.pop("SUBSTRATE_URL", None)
        return env, "", "SUBSTRATE_DISABLED is 1"
    token, source, problem = credential(agent_id, workspace, base)
    if token is not None:
        env[TOKEN] = token
    return env, source, problem
