"""A18 supply-chain security tools: pure analyses over supplied build-time evidence.

A18 specifies and verifies supply-chain evidence (hardened images, SBOMs, SLSA provenance, signature policy, VEX).
It never holds signing keys, never accepts risk and never signs a gate verdict; every tool here only reads what
the caller passes in and returns a decision-ready result. A10 owns the security gate and vulnerability.report,
A11 owns the pipeline that builds.

A credential that contains a space is not masked, because the redaction rule works on whitespace-free tokens.

Tools:
  supply_dockerfile_lint      Dockerfile best-practice and hardening rules (tags, digests, USER, ADD, caches, secrets).
  supply_base_image_policy    base image references versus allowed registries, digest pinning, age and end of life.
  supply_sbom_validate        CycloneDX 1.x / SPDX 2.x JSON structure plus the NTIA minimum elements.
  supply_sbom_diff            components added, removed, version-changed (with downgrades) and license-changed.
  supply_slsa_level           SLSA v1.0 Build level (0..3) from an in-toto provenance statement and verification facts.
  supply_provenance_match     does an artifact digest appear in the provenance subjects.
  supply_signature_policy     verified signatures versus required signer rules (issuer + identity glob).
  supply_vex_apply            vulnerabilities plus OpenVEX statements -> effective exposure.
  supply_pin_audit            unpinned GitHub Actions, images, requirements and package.json dependencies.
  supply_license_policy       SPDX license expressions versus an allow/deny policy.
  supply_retention_plan       registry tag keep/delete plan under a retention policy (plan only, nothing is deleted).
  supply_attestation_bundle   required attestation kinds present, signed and bound to the artifact digest.

Findings are {id, severity, summary, location} with severity blocker|major|minor|info; `blocking_count` counts
blocker and major findings and a `pass`/`fail` verdict is `fail` exactly when it is above zero. At most
FINDINGS_CAP findings are listed (worst first); `findings_total` always counts all of them.

Reference tables mirror public sources and are small on purpose:
  * OCI image-spec: image reference grammar (distribution/reference) and digest format.
  * NTIA "The Minimum Elements For a Software Bill of Materials" (July 2021): the seven data fields. CISA's 2026 minimum elements
    update and replace that baseline with more fields; this module still checks the seven 2021 fields only.
  * SLSA v1.0 Build track (slsa.dev/spec/v1.0/levels) and the in-toto Statement v0.1/v1 + DSSE envelope.
  * OpenVEX v0.2.0 specification: statuses and the five `not_affected` justifications.
  * SPDX License List / SPDX 2.3 Annex D (license expressions); copyleft categories follow the FSF/SPDX conventions.
"""
from __future__ import annotations

import base64
import binascii
import bisect
import json
import re
import shlex
from collections.abc import Callable, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any, TypeAlias, TypedDict, TypeVar, cast

from swarm.tools.support import respond

FINDINGS_CAP = 200
_RANK = {"blocker": 0, "major": 1, "minor": 2, "info": 3}
_DIGEST_HEX_LEN = {"sha256": 64, "sha384": 96, "sha512": 128}
JsonObject = dict[str, Any]  # caller-supplied JSON objects and the result objects built from them: values are untyped JSON
Finding = dict[str, str]  # {id, severity, summary, location}: every value is a string
_T = TypeVar("_T")


# ---- shared helpers -----------------------------------------------------------------------------------------------


_NON_CREDENTIAL_USERS = frozenset({"gh", "go", "npm", "oci", "pkg"})  # ecosystem prefixes: one is a reference only when its password holds a /
_NPM_SCOPE = re.compile(r"@[A-Za-z0-9._-]+/")  # npm:@scope/name@1.0.0: for the npm user, a password that starts with a scope is a package name
_USER_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")
_WORD = re.compile(r"\S+")  # a credential never spans whitespace: each whitespace-free token is scanned on its own
_SHA_FOLLOW = re.compile(r"(?i:sha(?:256|384|512)):")  # an @ followed by a digest is not userinfo
_SCHEME_DELIMITER = re.compile(r"[/?#@:]")  # ends the user of a scheme-form credential, which may not hold a colon
_SCHEME_DELIMITER_NO_COLON = re.compile(r"[/?#@]")  # ends the user of the colon-less scheme form


def _bare_reference(user: str, token: str, colon: int, first: int) -> bool:
    """True when a bare `user:password@` is a reference, not a credential, and so stays visible. `token[colon + 1:first]` is the segment
    between the colon and the first @. Two shapes:
    (a) a user from _NON_CREDENTIAL_USERS whose segment contains a `/`: gh:o/r@v1 and pkg:npm/@s/a@1.0.0. The segment stops at the first @,
    so a `/` in a host path after it does not count: gh:hunter2@github.com/o/r@v1 is masked. The same user with no `/` before its first @
    (npm:hunter2@host, go:hunter2@host, gh:hunter2@github.com/o/r@v1) is masked.
    (b) the npm user whose password starts with a scope: npm:@s/a@1.0.0. Any other user with a password that starts with `@name/`
    (u:@abc/x@host, ci-user:@svc/x@host) is masked.
    Documented class, not masked: an npm scoped reference whose password begins with an npm scope (`@name/`) is a reference, so any text
    after that scope is taken as the reference, as in npm:@s/hunter2@host, which displays as it is.
    Clause (a) does not cover it: for npm:@s/... the segment before the first @ is empty, because that @ is the scope's own. The exemption
    stays because the name and version after an npm scope are free-form (ranges, dist-tags and prereleases), and npm credentials live in .npmrc,
    not in package references. A credential after the scope and a colon is still masked: npm:@s/x:hunter2@host gives
    npm:@s/***@host.
    Limits: gh:Sup/3r@host displays, since it is structurally a repository reference (its / is before its first @); a credential whose
    `/` comes before its first @ under one of the listed users (gh, go, npm, oci, pkg) is not masked. A value with a scheme
    (https://u:p@h) is caught by _bare_spans too, because the user follows the `//`."""
    if user.lower() in _NON_CREDENTIAL_USERS and "/" in token[colon + 1:first]:
        return True
    return user.lower() == "npm" and _NPM_SCOPE.match(token, colon + 1) is not None


def _last_userinfo_at(token: str) -> int:
    """Index of the last @ of a whitespace-free token that does not introduce a digest (sha256: and kin), or -1. A password runs to it."""
    at = token.rfind("@")
    while at >= 0 and _SHA_FOLLOW.match(token, at + 1):
        at = token.rfind("@", 0, at)
    return at


def _bare_spans(text: str) -> list[tuple[int, int, bool]]:
    """(start, end, is_reference) of each bare `user:password@` in `text`. The user is the run of user characters before a colon, of
    any length; the password of a credential runs to the last non-digest @ of the same whitespace-free token and may hold any other
    character, / ? and : included. Every colon of a token is judged, not only the first: a reference ends at its own first @ and the
    scan resumes at the next colon after it, so a reference never shields a credential that follows it; a credential covers the rest
    of the token up to its last @ and ends the scan. Linear: each colon costs a back-scan that stops at the previous colon (a colon is
    not a user character), and each reference's forward search stops at its own first @, so no two scans overlap."""
    spans: list[tuple[int, int, bool]] = []
    for word in _WORD.finditer(text):
        token, base = word.group(), word.start()
        last = _last_userinfo_at(token)
        colon = token.find(":")
        while colon != -1 and colon + 2 <= last:  # the password needs at least one character before that @
            if token[colon + 1:colon + 3] == "//":  # a scheme, handled by _url_spans
                colon = token.find(":", colon + 1)
                continue
            start = colon
            while start > 0 and token[start - 1] in _USER_CHARS:
                start -= 1
            first = token.find("@", colon + 1)  # the first @ after the colon: at most `last`, since `last` is an @
            if _bare_reference(token[start:colon], token, colon, first):
                spans.append((base + start, base + first + 1, True))
                colon = token.find(":", first + 1)
            else:
                spans.append((base + start, base + last + 1, False))
                break
    return spans


def _url_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of each `://user:password@` and `://user@` in `text`. The password may hold / ? or :, up to the last non-digest @ of
    its whitespace-free token; the user of the colon form holds no colon, and that of the colon-less form is any run before the @.
    Linear: each `://` costs a search to the next delimiter, and the next `://` always holds a delimiter, so the searches do not overlap."""
    spans: list[tuple[int, int]] = []
    for word in _WORD.finditer(text):
        token, base = word.group(), word.start()
        last = _last_userinfo_at(token)
        scheme = token.find("://")
        while scheme != -1:
            after = scheme + 3
            end = None
            delimiter = _SCHEME_DELIMITER.search(token, after)
            if delimiter and token[delimiter.start()] == ":" and last >= delimiter.start() + 2:
                end = last + 1
            else:
                at = _SCHEME_DELIMITER_NO_COLON.search(token, after)
                if at and token[at.start()] == "@" and at.start() - after >= 1 and not _SHA_FOLLOW.match(token, at.start() + 1):
                    end = at.start() + 1
            if end is not None:
                spans.append((base + scheme, base + end))
                scheme = token.find("://", end)
            else:
                scheme = token.find("://", scheme + 1)
    return spans


def _replace_url_spans(value: str) -> str:
    out, previous = [], 0
    for start, end in _url_spans(value):
        out.append(value[previous:start])
        out.append("://***@")
        previous = end
    out.append(value[previous:])
    return "".join(out)


def _replace_bare_spans(value: str) -> str:
    out, previous = [], 0
    for start, end, reference in _bare_spans(value):
        out.append(value[previous:start])
        out.append(value[start:end] if reference else "***@")  # a reference is kept; a credential keeps its @ and host
        previous = end
    out.append(value[previous:])
    return "".join(out)


def _bare_credential(text: str) -> bool:
    """True when `text` holds a bare `user:secret@host` (no scheme) that must not be echoed, by the rule of _bare_reference."""
    return any(not reference for _start, _end, reference in _bare_spans(text))


_TOKEN_SHAPES = re.compile(
    r"\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b|\bxox[abprs]-[A-Za-z0-9-]{10,}"
    r"|\bglpat-[A-Za-z0-9_-]{16,}|\bsk-[A-Za-z0-9_-]{20,}|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"
    r"|\b(?:[a-z]{3,8} ){11,23}[a-z]{3,8}\b"  # a recovery phrase: 12 to 24 lowercase words
)

def _scrub(text: object) -> str:
    """Every string a finding, output field, skipped entry or location echoes passes through here, so a credential cannot reach a result.
    The echo sites are: _show, _f, _clip and the echo helpers, which all call this; and the CLI scripts/supply_image_lint.py, which calls
    it in scan() on every skipped entry, every reason handed to swarm/scan_status.py:incomplete_findings and every finding location
    (one output boundary, so the walk's paths in _scan_dockerfile and _scan_workflow cannot leak), and in run() on the --path it echoes
    in its errors. What is
    removed: the user:password of a URL (the host stays), each bare `user:password@` that is not a reference (_bare_reference; the host
    after the @ stays), and well-known token shapes (GitHub, AWS, Slack, GitLab, JWT) and runs of 12-24 lowercase words (a recovery
    phrase). Digests, names and ids pass unchanged. A value that is only known to be malformed (an invalid digest, tag or reference) is
    not echoed at all by _echo_value and _echo_ref, the stricter helpers."""
    value = str(text)
    if "@" in value:  # every credential shape holds an @: a value without one is only checked for token shapes
        value = _replace_url_spans(value)
        value = _replace_bare_spans(value)
    return _TOKEN_SHAPES.sub("***", value)


def _show(text: object, limit: int) -> str:
    """Scrub first, then cut: a token cut in half would no longer match its shape."""
    return _scrub(text)[:limit]


def _respond(payload: JsonObject, work: Callable[[], JsonObject]) -> JsonObject:
    return cast(JsonObject, respond(payload, work))  # support.respond is untyped (Any); work() always returns a JsonObject


def _invalid(field: str, message: str) -> JsonObject:
    """The one INVALID_INPUT shape: {state, field, message}. `field` is an input property name."""
    return {"state": "INVALID_INPUT", "field": field, "message": _show(message, 400)}


def _dec(value: object) -> Decimal:
    """Exact decimal of the number as written (shortest repr)."""
    return Decimal(repr(value))


def _f(fid: str, severity: str, summary: str, location: str = "") -> Finding:
    return {"id": _show(fid, 120), "severity": severity, "summary": _show(summary, 240), "location": _show(location, 300)}


def _fit(items: Sequence[_T], limit: int) -> list[_T]:
    """The leading items (at most FINDINGS_CAP) whose JSON stays within `limit` bytes, so a result with maximal strings stays far below
    the 256 KiB result limit. Counts next to a list are always totals; only the listing is cut, never reordered."""
    out: list[_T] = []
    used = 2
    for item in items[:FINDINGS_CAP]:
        used += len(json.dumps(item)) + 2
        if used > limit:
            break
        out.append(item)
    return out


def _result(findings: list[Finding]) -> JsonObject:
    """The three findings keys every analysis returns: total, blocking (blocker+major) and the worst-first list (cut at 64 KB)."""
    ordered = sorted(findings, key=lambda f: (_RANK[f["severity"]], f["id"], f["location"], f["summary"]))
    return {
        "findings_total": len(ordered),
        "blocking_count": sum(1 for f in ordered if _RANK[f["severity"]] <= _RANK["major"]),
        "findings": _fit(ordered, 64_000),
    }


def _verdict(findings: list[Finding]) -> str:
    return "fail" if any(_RANK[f["severity"]] <= _RANK["major"] for f in findings) else "pass"


def _timestamp(value: object) -> datetime | None:
    """ISO-8601 with an explicit offset (or Z); naive and unparseable values give None."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _calendar_date(value: object) -> date | None:
    """A YYYY-MM-DD date, optionally followed by a time part (`T...`), as endoflife.date publishes it."""
    if not isinstance(value, str) or len(value) < 10 or (len(value) > 10 and value[10] not in "T "):
        return None
    try:
        if len(value) > 10:
            datetime.fromisoformat(value)  # the time part must be a real time, not trailing text
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _valid_digest(value: object) -> tuple[str, str] | None:
    """(algorithm, lowercase hex) for `sha256:<64 hex>` / sha384 / sha512 (OCI digest grammar), else None."""
    if not isinstance(value, str):
        return None
    algorithm, sep, hex_part = value.partition(":")
    hex_part = hex_part.lower()
    length = _DIGEST_HEX_LEN.get(algorithm.lower())
    if not sep or length is None or len(hex_part) != length or any(c not in "0123456789abcdef" for c in hex_part):
        return None
    return algorithm.lower(), hex_part


def _echo_ref(ref: str) -> str:
    """An image reference for a message. An `@` that does not introduce a valid digest may be `user:password@host`, so such a
    reference is not echoed at all."""
    if _bare_credential(ref):  # `user:secret@host/${X}`: the $ exemption below must not let a credential through
        return "(value with an @, not echoed)"
    sep, digest = ref.partition("@")[1:]
    if sep and "$" not in digest and _valid_digest(digest) is None:
        return "(reference with a malformed digest)"
    return _scrub(ref)


def _echo_value(text: str) -> str:
    """A caller-supplied string for a message or an output field. A value shaped like userinfo (`name:secret@host`: a colon before the
    @, the host after it) is not echoed at all, since `_scrub` only strips userinfo that follows a scheme. An ordinary name with an @
    (release@corp) is named as it is; any other value goes through `_scrub`."""
    value = str(text)
    return "(value with an @, not echoed)" if _bare_credential(value) else _scrub(value)


_COMPONENT = re.compile(r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*")
_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
_REGISTRY_ALIASES = {"index.docker.io": "docker.io", "registry-1.docker.io": "docker.io", "registry.hub.docker.com": "docker.io"}
_MUTABLE_BRANCHES = {"main", "master", "develop", "dev", "trunk", "head", "latest"}


def _parse_ref(ref: str) -> tuple[dict[str, str] | None, str]:
    """Parse an OCI/Docker image reference into {registry, repository, tag, digest} or (None, reason).

    Mirrors the distribution/reference grammar: the first path component is a registry host when it holds a `.`
    or `:` or is `localhost`; otherwise the image lives on docker.io (a bare name is `library/<name>`).
    """
    if not isinstance(ref, str) or not ref or len(ref) > 255 or any(c.isspace() for c in ref):
        return None, "empty, too long or contains whitespace"
    name, sep, digest = ref.partition("@")
    if sep and _valid_digest(digest) is None:
        return None, "the digest is not algorithm:hex of the right length"
    tag = ""
    last = name.rsplit("/", 1)[-1]
    if ":" in last:
        name, _, tag = name.rpartition(":")
        if not _TAG.fullmatch(tag):
            return None, "the tag is not a valid OCI tag"
    first, slash, rest = name.partition("/")
    if slash and ("." in first or ":" in first or first == "localhost"):
        registry, repository = first.lower(), rest
    else:
        registry, repository = "docker.io", name
    registry = _REGISTRY_ALIASES.get(registry, registry)
    if registry == "docker.io" and "/" not in repository:
        repository = f"library/{repository}"
    if not repository or any(not _COMPONENT.fullmatch(part) for part in repository.split("/")):
        return None, f"repository {repository!r} is not lowercase alphanumeric path components"
    return {"registry": registry, "repository": repository, "tag": tag, "digest": digest.lower()}, ""


def _ref_pinning(parsed: dict[str, str]) -> str:
    """digest | latest (no tag or :latest and no digest) | tag."""
    if parsed["digest"]:
        return "digest"
    return "latest" if parsed["tag"] in ("", "latest") else "tag"


# ---- Dockerfile parsing and lint ------------------------------------------------------------------------------------

_HEREDOC = re.compile(r"<<-?\s*([\"']?)([A-Za-z_][A-Za-z0-9_]*)\1")
_VAR = re.compile(r"\$\{(\w+)(?::?-([^}]*))?\}|\$(\w+)")
_UNSUPPORTED_VAR = re.compile(r"\$\{(?!\w+(?::?-[^}]*)?\})")


class _Unknown(str):
    """Marker for a build value this reader cannot evaluate. It compares equal to "" but is not a known empty value."""


_UNKNOWN = _Unknown()


_URL_CREDENTIALS = re.compile(r"[A-Za-z][A-Za-z0-9+.-]{0,20}://[^\s/:@]{1,200}:[^\s/@]{1,200}@")
_SECRET_WORDS = {"PASSWORD", "PASSWD", "PASSPHRASE", "SECRET", "SECRETS", "TOKEN", "TOKENS", "CREDENTIAL", "CREDENTIALS", "APIKEY"}
_SECRET_PAIRS = {("API", "KEY"), ("PRIVATE", "KEY"), ("ACCESS", "KEY"), ("SECRET", "KEY"), ("AUTH", "KEY")}
_NOT_SECRET_TAILS = {
    "FILE", "PATH", "DIR", "NAME", "ID", "URL", "URI", "ARN", "ENDPOINT", "HOST", "PORT", "LENGTH", "TTL", "TIMEOUT", "EXPIRY", "ENABLED", "REQUIRED",
    "TYPE", "ALGORITHM",
}
_BOOLEAN_VALUES = {"1", "0", "true", "false", "yes", "no", "on", "off", "enabled", "disabled"}  # a flag such as ALLOW_EMPTY_PASSWORD=yes
_SECRET_FILES = {".env", ".npmrc", ".pypirc", ".netrc", ".git-credentials", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"}
_ARCHIVE_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")
_PIPE_TO_SHELL = re.compile(r"\b(?:curl|wget)\b[^|;&]{0,400}\|\s{0,8}(?:sudo\s+)?(?:ba|da|z|k)?sh\b")
_APT = re.compile(r"\bapt(?:-get)?\b[^;&|]{0,200}\binstall\b")
_APK = re.compile(r"\bapk\b[^;&|]{0,200}\badd\b")
_RPM = re.compile(r"\b(?:yum|dnf|microdnf)\b[^;&|]{0,200}\binstall\b")
_PIP = re.compile(r"\bpip3?(?:\.\d+)?\s+(?:[^;&|]{0,200}\s)?install\b|-m\s+pip\s+install\b")
_BUILD_TOOLS = re.compile(
    r"(?<![\w-])(?:build-essential|gcc|g\+\+|make|mvn|gradlew?|(?:cargo|go) build|(?:npm|yarn|pnpm) (?:run )?build)(?![\w-])"
)
_TRUE_WORDS = {"1", "true", "yes", "on"}


def _secret_name(name: str) -> bool:
    """True for names that conventionally hold a secret (DB_PASSWORD, GITHUB_TOKEN, API_KEY ...), not *_FILE/*_PATH."""
    upper = name.upper().replace("-", "_")
    parts = upper.split("_")
    if parts[-1] in _NOT_SECRET_TAILS:
        return False
    if any(part in _SECRET_WORDS for part in parts) or any(pair in _SECRET_PAIRS for pair in zip(parts, parts[1:])):
        return True
    return any(word in upper for word in ("PASSWORD", "PASSWD", "APIKEY", "SECRET"))  # camelCase names such as dbPassword


_WHITESPACE = re.compile(r"\s+")


def _split_word(text: str) -> tuple[str, str]:
    """First whitespace-delimited word and the rest: a tab separates Dockerfile words exactly as a space does."""
    word, *rest = _WHITESPACE.split(text, maxsplit=1)
    return word, (rest[0] if rest else "")


def _split_flags(rest: str) -> tuple[list[str], str]:
    flags: list[str] = []
    while rest.startswith("--") and len(flags) < 50:
        token, rest = _split_word(rest)
        flags.append(token)
        rest = rest.lstrip()
    return flags, rest


def _tokens(text: str) -> list[str]:
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


class _Instruction(TypedDict):
    line: int
    kw: str
    flags: list[str]
    args: str
    text: str


class _Stage(TypedDict):
    line: int
    base: str
    name: str
    parent: int | None
    instructions: list[_Instruction]


def _parse_dockerfile(content: str) -> list[_Instruction]:
    """Logical instructions {line, kw, flags, args, text}: continuations joined, comments dropped, heredoc bodies kept."""
    lines = content.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n").split("\n")  # Docker ignores a UTF-8 byte order mark
    escape = "\\"
    index = 0
    while index < len(lines):  # parser directives are only recognised before the first instruction or comment
        match = re.fullmatch(r"#\s*([A-Za-z]+)\s*=\s*(\S+)\s*", lines[index])
        if not match:
            break
        if match.group(1).lower() == "escape" and match.group(2) in ("\\", "`"):
            escape = match.group(2)
        index += 1
    out: list[_Instruction] = []
    terminators: dict[str, list[int]] = {}  # stripped line -> ascending line numbers, so each heredoc terminator is a bisect, never a rescan
    for number, line in enumerate(lines):
        terminators.setdefault(line.strip(), []).append(number)
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        start = index + 1
        parts: list[str] = []
        while index < len(lines):
            current = lines[index].rstrip()
            index += 1
            stripped = current.strip()
            if not stripped or stripped.startswith("#"):
                if parts:
                    continue  # blank and comment lines inside a continued instruction are removed
                break
            if current.endswith(escape):
                parts.append(stripped[:-1].strip())
                continue
            parts.append(stripped)
            break
        logical = " ".join(p for p in parts if p)
        head, rest = _split_word(logical)
        keyword = head.upper()
        body: list[str] = []
        if keyword in ("RUN", "COPY", "ADD"):
            for _quote, delimiter in _HEREDOC.findall(logical):
                candidates = terminators.get(delimiter, [])
                position = bisect.bisect_left(candidates, index)
                if position == len(candidates):
                    continue  # no terminator line after this instruction: `<<` is shell arithmetic (`$((1<<N))`) or a stray token, not a heredoc
                end = candidates[position]
                body.extend(lines[index:end])
                index = end + 1
        flags, args = _split_flags(rest.strip())
        out.append({"line": start, "kw": keyword, "flags": flags, "args": args, "text": logical + ("\n" + "\n".join(body) if body else "")})
    return out


def _expand(text: str, variables: dict[str, str]) -> tuple[str, bool]:
    """Substitute ${V}, $V and ${V:-default} from `variables`; the flag is False when something stayed unresolved, including a form this
    reader does not evaluate (`${V:+word}`, `${V#pattern}`, ...), which Docker may expand to a different value."""
    resolved = not _UNSUPPORTED_VAR.search(text)

    def replace(match: re.Match[str]) -> str:
        nonlocal resolved
        name = match.group(1) or match.group(3)
        value = variables.get(name)
        if value is _UNKNOWN:
            resolved = False  # an unknown value stays unknown under `:-` too: Docker may or may not take the default
            return match.group(0)
        if value:
            return value
        if match.group(2) is not None:
            return match.group(2)
        resolved = False
        return match.group(0)

    return _VAR.sub(replace, text), resolved


def _resolved(value: str, variables: dict[str, str]) -> str:
    """`value` with its `$NAME` and `${NAME}` read from `variables`, as Docker expands them at build time. A value that stays unresolved
    becomes `_UNKNOWN`: a later use is unresolved too, even under a `:-` default, and never a literal `$X` or a known empty value."""
    expanded, resolved = _expand(value, variables)
    return expanded if resolved else _UNKNOWN


def _bind(variables: dict[str, str], name: str, value: str) -> None:
    """Bind one ARG value in instruction order, expanded through the variables bound so far."""
    variables[name] = _resolved(value, variables)


def _stages(instructions: list[_Instruction]) -> tuple[list[_Stage], dict[str, str]]:
    stages: list[_Stage] = []
    global_args: dict[str, str] = {}
    names: dict[str, int] = {}
    for ins in instructions:
        if ins["kw"] == "FROM":
            tokens = ins["args"].split()
            base = tokens[0] if tokens else ""
            name = tokens[2].lower() if len(tokens) >= 3 and tokens[1].upper() == "AS" else ""
            parent = names.get(base.lower())
            stages.append({"line": ins["line"], "base": base, "name": name, "parent": parent, "instructions": []})
            if name:
                names[name] = len(stages) - 1
        elif not stages:
            if ins["kw"] == "ARG":
                for token in _tokens(ins["args"]):
                    key, eq, value = token.partition("=")
                    if eq:
                        _bind(global_args, key, value)
                    else:
                        global_args.setdefault(key, "")
        else:
            stages[-1]["instructions"].append(ins)
    return stages, global_args


def _last_in_chain(stages: list[_Stage], index: int, keyword: str) -> _Instruction | None:
    """Last `keyword` instruction effective in stage `index`, following `FROM <earlier stage>` inheritance."""
    seen: set[int] = set()
    current: int | None = index
    while current is not None and current not in seen:
        seen.add(current)
        for ins in reversed(stages[current]["instructions"]):
            if ins["kw"] == keyword:
                return ins
        current = stages[current]["parent"]
    return None


def _env_pairs(args: str) -> list[tuple[str, str]]:
    """(name, value) of an ENV instruction: the legacy `ENV NAME value` form or `NAME=value` words."""
    tokens = _tokens(args)
    if tokens and "=" not in tokens[0]:
        return [(tokens[0], " ".join(tokens[1:]))]
    return [(key, value) for key, _eq, value in (t.partition("=") for t in tokens)]


def _run_stage(stage: _Stage, global_args: dict[str, str], inherited: tuple[dict[str, str], dict[str, str]], before: int) -> tuple[dict[str, str], dict[str, str]]:
    """(variables, env) at position `before` of one stage. `inherited` is the (variables, env) pair its in-file parent ends with, both empty
    for an external base image: that image's ENV is not known to the scanner, see dockerfile_lint. Docker carries a parent stage's build
    arguments into a child stage through FROM as well as its ENV, so `variables` starts from the parent's build-time variables, which
    already hold the parent's ENV. Each name maps to its build-time value inside the stage: the inherited values, then the stage's own ARG
    and ENV in order. A global ARG (before the first FROM) is not visible here: Docker makes it available to a stage only when the stage
    redeclares it, and then a bare `ARG NAME` takes the global default. `env` holds the names bound by an ENV, inherited or the stage's
    own, with their values: an ENV overrides an ARG of the same name whatever the order, so no later ARG rebinds such a name. An ARG with
    no value and no default stays empty, which _expand reads as unresolved."""
    inherited_variables, inherited_env = inherited
    variables = dict(inherited_variables)
    env = dict(inherited_env)
    for ins in stage["instructions"][:before]:
        if ins["kw"] == "ARG":
            for token in _tokens(ins["args"]):
                key, eq, value = token.partition("=")
                if key in env:
                    continue
                if eq:
                    _bind(variables, key, value)
                else:
                    variables.setdefault(key, global_args.get(key, ""))
        elif ins["kw"] == "ENV":
            # Docker expands every pair of one ENV instruction against the variables from before it, then binds the values
            pairs = [(name, _resolved(value, variables)) for name, value in _env_pairs(ins["args"])]
            for name, value in pairs:
                variables[name] = value
                env[name] = value
    return variables, env


def _stage_scopes(stages: list[_Stage], global_args: dict[str, str]) -> list[tuple[dict[str, str], dict[str, str]]]:
    """(variables, env) at the end of each stage, indexed by stage: what its in-file children inherit through FROM. A parent always
    precedes its child, so one pass in stage order computes every entry from finished entries: no recursion, and each stage runs once,
    so the cost is linear in the instructions."""
    table: list[tuple[dict[str, str], dict[str, str]]] = []
    for stage in stages:
        parent = stage["parent"]
        inherited = table[parent] if parent is not None else ({}, {})
        table.append(_run_stage(stage, global_args, inherited, len(stage["instructions"])))
    return table


def _effective_user(stages: list[_Stage], table: list[tuple[dict[str, str], dict[str, str]]], global_args: dict[str, str], index: int) -> tuple[_Instruction | None, dict[str, str]]:
    """The USER instruction effective in stage `index` (inherited through FROM) and the build variables visible to it."""
    seen: set[int] = set()
    current: int | None = index
    while current is not None and current not in seen:
        seen.add(current)
        instructions = stages[current]["instructions"]
        for position in range(len(instructions) - 1, -1, -1):
            if instructions[position]["kw"] == "USER":
                parent = stages[current]["parent"]
                inherited = table[parent] if parent is not None else ({}, {})
                return instructions[position], _run_stage(stages[current], global_args, inherited, position)[0]
        current = stages[current]["parent"]
    return None, dict(global_args)



def _lint_dockerfile(content: str, require_healthcheck: bool, require_digest_pin: bool) -> tuple[JsonObject | None, list[Finding]]:
    instructions = _parse_dockerfile(content)
    stages, global_args = _stages(instructions)
    if not stages:
        return None, []
    findings: list[Finding] = []
    final = len(stages) - 1

    for stage in stages:
        base = stage["base"]
        at = f"line {stage['line']}"
        if not base:
            findings.append(_f("docker.base-invalid-reference", "major", "FROM has no image", at))
        elif base.lower() == "scratch" or stage["parent"] is not None:
            continue
        else:
            text, resolved = _expand(base, global_args)
            if not resolved:
                findings.append(_f("docker.base-unresolved-arg", "major", f"base image {_echo_value(base)!r} depends on a build argument without default; the image cannot be checked", at))
                continue
            parsed, reason = _parse_ref(text)
            if parsed is None:
                findings.append(_f("docker.base-invalid-reference", "major", f"base image {_echo_ref(text)!r} is not a valid reference: {reason}", at))
                continue
            pinning = _ref_pinning(parsed)
            if pinning == "latest":
                findings.append(_f("docker.latest-tag", "major", f"base image {text!r} resolves to the mutable :latest tag; pin a version and digest", at))
            elif pinning == "tag":
                severity = "major" if require_digest_pin else "minor"
                findings.append(_f("docker.base-not-digest-pinned", severity, f"base image {text!r} is pinned by tag only; add @sha256:<digest>", at))

    table = _stage_scopes(stages, global_args)
    user, user_variables = _effective_user(stages, table, global_args, final)
    user_text, user_resolved = _expand(user["args"], user_variables) if user else ("", True)
    user_name = user_text.split(":", 1)[0].strip()
    if user is None:
        findings.append(_f("docker.final-user-missing", "major", "final stage has no USER instruction; the container runs as root unless the base image sets a non-root user", f"line {stages[final]['line']}"))
    elif not user_resolved:
        findings.append(_f("docker.final-user-unresolved", "major", f"final stage USER {_echo_value(user['args'])} depends on a build argument without default; the user cannot be checked", f"line {user['line']}"))
    elif user_name in ("root", "0"):
        findings.append(_f("docker.final-user-root", "major", f"final stage ends with USER {_echo_value(user['args'])}; run as a non-root user", f"line {user['line']}"))

    health = _last_in_chain(stages, final, "HEALTHCHECK")
    has_health = health is not None and not health["args"].upper().startswith("NONE")
    if require_healthcheck and not has_health:
        reason = "disables the health check with HEALTHCHECK NONE" if health is not None else "has no HEALTHCHECK instruction"
        findings.append(_f("docker.no-healthcheck", "minor", f"final stage {reason} (a base image may still define one)", f"line {stages[final]['line']}"))

    toolchain_line = 0
    for stage_index, stage in enumerate(stages):
        env: dict[str, str] = dict(table[stage["parent"]][1]) if stage["parent"] is not None else {}
        for ins in stage["instructions"]:
            at = f"line {ins['line']}"
            kw, text = ins["kw"], ins["text"]
            if kw == "ENV":
                pairs = _env_pairs(ins["args"])
                for name, value in pairs:
                    env[name] = value
                    if _secret_name(name) and value and value.lower() not in _BOOLEAN_VALUES:
                        findings.append(_f("docker.secret-in-env", "blocker", f"ENV {name} stores a secret in the image configuration; use RUN --mount=type=secret", at))
                if _URL_CREDENTIALS.search(ins["args"]):
                    findings.append(_f("docker.credential-in-url", "blocker", "ENV holds a URL with embedded credentials", at))
            elif kw == "ARG":
                for token in _tokens(ins["args"]):
                    name, _, default = token.partition("=")
                    if _secret_name(name) and default.lower() not in _BOOLEAN_VALUES:
                        findings.append(_f("docker.secret-in-arg", "major", f"ARG {name} is visible in image history and provenance; use RUN --mount=type=secret", at))
                if _URL_CREDENTIALS.search(ins["args"]):
                    findings.append(_f("docker.credential-in-url", "blocker", "ARG holds a URL with embedded credentials", at))
            elif kw == "RUN":
                cached = any(flag.startswith("--mount=") and "type=cache" in flag for flag in ins["flags"])
                secret_mount = any(flag.startswith("--mount=") and "type=secret" in flag for flag in ins["flags"])
                if _PIPE_TO_SHELL.search(text):
                    findings.append(_f("docker.pipe-to-shell", "major", "RUN pipes a download into a shell without verifying it", at))
                if _URL_CREDENTIALS.search(text) and not secret_mount:
                    findings.append(_f("docker.credential-in-url", "major", "RUN uses a URL with embedded credentials (kept in layer history)", at))
                if not cached:
                    if _APT.search(text) and "/var/lib/apt/lists" not in text:
                        findings.append(_f("docker.pkg-cache-apt", "minor", "apt install without removing /var/lib/apt/lists in the same RUN", at))
                    if _APK.search(text) and "--no-cache" not in text and "/var/cache/apk" not in text:
                        findings.append(_f("docker.pkg-cache-apk", "minor", "apk add without --no-cache", at))
                    if _RPM.search(text) and "clean all" not in text and "/var/cache/yum" not in text and "/var/cache/dnf" not in text:
                        findings.append(_f("docker.pkg-cache-rpm", "minor", "yum/dnf install without `clean all` in the same RUN", at))
                    if _PIP.search(text) and "--no-cache-dir" not in text and env.get("PIP_NO_CACHE_DIR", "").lower() not in _TRUE_WORDS:
                        findings.append(_f("docker.pip-cache", "minor", "pip install without --no-cache-dir (or PIP_NO_CACHE_DIR)", at))
                if stage_index == final and not toolchain_line and _BUILD_TOOLS.search(text.lower()):
                    toolchain_line = ins["line"]
            elif kw in ("ADD", "COPY") and "<<" not in ins["args"]:
                raw = ins["args"]
                try:
                    items = json.loads(raw) if raw.startswith("[") else raw.split()
                except ValueError:
                    items = raw.split()
                sources = [s for s in items[:-1] if isinstance(s, str)]
                for source in sources:
                    base_name = source.rsplit("/", 1)[-1].lower()
                    if base_name in _SECRET_FILES:
                        findings.append(_f("docker.copy-secret-file", "major", f"{kw} copies {source}, a file that usually holds credentials", at))
                    if kw != "ADD":
                        continue
                    if source.lower().startswith(("http://", "https://")):
                        if not any(flag.startswith("--checksum=") for flag in ins["flags"]):
                            findings.append(_f("docker.add-remote-unverified", "major", f"ADD {source} downloads content without --checksum", at))
                    elif not source.lower().startswith(("git@", "git://")) and not source.lower().endswith(_ARCHIVE_SUFFIXES):
                        findings.append(_f("docker.add-instead-of-copy", "minor", f"ADD {source} has no archive to extract; use COPY", at))
    if len(stages) == 1 and toolchain_line:
        findings.append(_f("docker.single-stage-build-tools", "minor", "single-stage image runs build tooling; use a multi-stage build and copy only the artifact", f"line {toolchain_line}"))

    final_base = stages[final]["base"]
    facts = {
        "stages": len(stages),
        "instructions": len(instructions),
        "final_base": _clip(_echo_ref(final_base), 300),
        "final_user": _clip(user["args"], 300) if user else "",
        "has_healthcheck": has_health,
    }
    return facts, findings


def dockerfile_lint(payload: JsonObject) -> JsonObject:
    """Lint one Dockerfile: mutable or unpinned bases, root user, ADD misuse, package caches, pipe-to-shell, secrets.

    Scope of the ARG and ENV analysis: values come from this file only, that is its global ARG, its stage ARG and ENV, and the ENV of an
    in-file parent stage named with `FROM <alias>`. The ENV of an external base image (for example `FROM alpine`) is NOT known to the
    scanner, so a USER that only the image's own ENV sets is not analysed; the scanner does not guess it."""

    def work() -> JsonObject:
        content = payload["content"]
        facts, findings = _lint_dockerfile(content, payload.get("require_healthcheck", True), payload.get("require_digest_pin", False))
        if facts is None:
            return _invalid("content", "the Dockerfile has no FROM instruction")
        return {"state": "SUCCESS", **facts, "verdict": _verdict(findings), **_result(findings)}

    return _respond(payload, work)


# ---- base image policy ------------------------------------------------------------------------------------------------

EOL_WARNING_DAYS_DEFAULT = 90


def _registry_rule(entry: str) -> tuple[str, str] | None:
    """An allowed-registry entry `host` or `host/path-prefix` as (host, path); docker.io aliases are normalised."""
    text = entry.strip().lower().strip("/")
    if not text or any(c.isspace() for c in text):
        return None
    host, _, path = text.partition("/")
    return _REGISTRY_ALIASES.get(host, host), path


def _registry_allowed(parsed: dict[str, str], rules: list[tuple[str, str]]) -> bool:
    for host, path in rules:
        if parsed["registry"] != host:
            continue
        if not path or parsed["repository"] == path or parsed["repository"].startswith(path + "/"):
            return True
    return False


def base_image_policy(payload: JsonObject) -> JsonObject:
    """Check base image references against allowed registries, digest pinning, a maximum age and end-of-life dates.
    A date is end of life from `eol_date` itself (endoflife.date `eolFrom`: the first day without support), compared with the
    calendar date of `as_of` in its own offset; an image older than `max_age_days` x 24 h fails (exactly that old passes)."""

    def work() -> JsonObject:
        as_of = _timestamp(payload["as_of"])
        if as_of is None:
            return _invalid("as_of", "as_of must be an ISO-8601 timestamp with an explicit offset")
        rules: list[tuple[str, str]] = []
        for entry in payload["allowed_registries"]:
            rule = _registry_rule(entry)
            if rule is None:
                return _invalid("allowed_registries", f"{entry!r} is not a registry host or host/path prefix")
            rules.append(rule)
        max_age = payload.get("max_age_days")
        warn_days = payload.get("eol_warning_days", EOL_WARNING_DAYS_DEFAULT)
        require_digest = payload["require_digest"]
        findings: list[Finding] = []
        violating: set[int] = set()
        for position, entry in enumerate(payload["images"]):
            ref = entry["ref"]
            parsed, reason = _parse_ref(ref)
            if parsed is None:
                return _invalid("images", f"image reference {_echo_ref(ref)!r} is invalid: {reason}")
            created = entry.get("created_at")
            created_at = None
            if created is not None:
                created_at = _timestamp(created)
                if created_at is None:
                    return _invalid("images", f"images[{position}].created_at must be an ISO-8601 timestamp with an explicit offset")
                if created_at > as_of:
                    return _invalid("images", f"images[{position}].created_at is after as_of")
            eol = entry.get("eol_date")
            eol_day = None
            if eol is not None:
                eol_day = _calendar_date(eol)
                if eol_day is None:
                    return _invalid("images", f"images[{position}].eol_date must be YYYY-MM-DD")
            before = len(findings)
            if not _registry_allowed(parsed, rules):
                findings.append(_f("image.registry-not-allowed", "major", f"{parsed['registry']}/{parsed['repository']} is not on an allowed registry", ref))
            pinning = _ref_pinning(parsed)
            if pinning == "latest":
                findings.append(_f("image.latest-tag", "major", "reference resolves to the mutable :latest tag", ref))
            elif pinning == "tag":
                findings.append(_f("image.not-digest-pinned", "major" if require_digest else "minor", "pinned by tag only; a tag can be re-pointed", ref))
            if max_age is not None:
                if created_at is None:
                    findings.append(_f("image.age-unknown", "major", "no created_at supplied; the age limit cannot be checked", ref))
                elif (as_of - created_at).total_seconds() > max_age * 86400:
                    age_days = int(-(-(as_of - created_at).total_seconds() // 86400))  # rounded up: always above max_age here
                    findings.append(_f("image.too-old", "major", f"image is {age_days} days old (rounded up), over the {max_age} day limit", ref))
            if eol_day is not None:
                days_left = (eol_day - as_of.date()).days
                if days_left <= 0:
                    findings.append(_f("image.end-of-life", "major", f"support ended on {eol_day.isoformat()}", ref))
                elif days_left <= warn_days:
                    findings.append(_f("image.eol-soon", "minor", f"support ends on {eol_day.isoformat()} ({days_left} day{'' if days_left == 1 else 's'})", ref))
            if any(_RANK[f["severity"]] <= _RANK["major"] for f in findings[before:]):
                violating.add(position)
        return {
            "state": "SUCCESS",
            "images_checked": len(payload["images"]),
            "violating_images": len(violating),
            "verdict": _verdict(findings),
            **_result(findings),
        }

    return _respond(payload, work)


# ---- SBOM parsing, validation, diff ---------------------------------------------------------------------------------

_SPDX_DEP_RELATIONSHIPS = {
    "DEPENDS_ON", "DEPENDENCY_OF", "CONTAINS", "CONTAINED_BY", "STATIC_LINK", "DYNAMIC_LINK", "BUILD_DEPENDENCY_OF",
    "DEV_DEPENDENCY_OF", "OPTIONAL_DEPENDENCY_OF", "PROVIDED_DEPENDENCY_OF", "TEST_DEPENDENCY_OF", "RUNTIME_DEPENDENCY_OF",
}
_SPDX_ID_REFS = {"purl", "cpe22Type", "cpe23Type", "swid", "gitoid", "swh"}
_NTIA_ELEMENTS = (
    "supplier_name", "component_name", "component_version", "unique_identifier", "dependency_relationship", "sbom_author", "timestamp",
)
_NTIA_LABEL = {
    "supplier_name": "Supplier Name", "component_name": "Component Name", "component_version": "Version of the Component",
    "unique_identifier": "Other Unique Identifiers", "dependency_relationship": "Dependency Relationship",
    "sbom_author": "Author of SBOM Data", "timestamp": "Timestamp",
}
_MAX_NESTING = 64


def _clip(text: str, limit: int) -> str:
    """Truncate a document-supplied string so it fits the declared output_schema maxLength (credentials removed first)."""
    return _show(text, limit)


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _list(value: object) -> list[Any]:
    """`value` when it is a JSON array, else an empty list: SBOM documents are untrusted and may carry any JSON type."""
    return value if isinstance(value, list) else []


class _SbomLoad(TypedDict):
    kind: str
    doc: JsonObject


class _Subject(TypedDict):
    name: str
    digests: dict[str, str]


class _Component(TypedDict):
    name: str
    group: str
    version: str
    purl: str
    supplier: bool
    identified: bool
    ref: str
    license: str


class _CdxFacts(TypedDict):
    author: bool
    timestamp: bool
    root: int


class _Group(TypedDict):
    name: str
    versions: set[str]
    licenses: dict[str, str]


def _load_json(text: str, field: str, what: str) -> tuple[JsonObject | None, JsonObject | None]:
    """json.loads for a bounded document; a non-object or parse failure is an INVALID_INPUT naming `field`."""
    try:
        doc = json.loads(text)
    except (ValueError, RecursionError) as exc:
        return None, _invalid(field, f"{what} is not valid JSON: {str(exc)[:80]}")
    if not isinstance(doc, dict):
        return None, _invalid(field, f"{what} must be a JSON object")
    return doc, None


def _cdx_license(component: JsonObject) -> str:
    names: list[str] = []
    for item in _list(component.get("licenses")):
        if not isinstance(item, dict):
            continue
        if _text(item.get("expression")):
            names.append(_text(item["expression"]))
        lic = item.get("license")
        if isinstance(lic, dict):
            name = _text(lic.get("id")) or _text(lic.get("name"))
            if name:
                names.append(name)
    return ", ".join(sorted(set(names)))


def _cdx_components(doc: JsonObject) -> tuple[list[_Component], _CdxFacts]:
    """Flatten metadata.component and the nested components tree; second value holds the SBOM level facts."""
    metadata = _dict(doc.get("metadata"))
    roots = [metadata["component"]] if isinstance(metadata.get("component"), dict) else []
    found: list[_Component] = []
    top = _list(doc.get("components"))
    stack = [(item, 0) for item in reversed(top)] + [(root, 0) for root in reversed(roots)]
    ordered: list[JsonObject] = []
    while stack:
        item, depth = stack.pop()
        if not isinstance(item, dict):
            continue
        ordered.append(item)
        children = item.get("components")
        if isinstance(children, list) and depth < _MAX_NESTING:
            stack.extend((child, depth + 1) for child in reversed(children))
    for item in ordered:
        swid = item.get("swid")
        found.append({
            "name": _text(item.get("name")),
            "group": _text(item.get("group")),
            "version": _text(item.get("version")),
            "purl": _text(item.get("purl")),
            "supplier": bool(isinstance(item.get("supplier"), dict) and _text(item["supplier"].get("name"))),
            "identified": bool(_text(item.get("purl")) or _text(item.get("cpe")) or (isinstance(swid, dict) and swid)),
            "ref": _text(item.get("bom-ref")),
            "license": _cdx_license(item),
        })
    authors = metadata.get("authors")
    tools = metadata.get("tools")
    if isinstance(tools, dict):
        tools = _list(tools.get("components")) + _list(tools.get("services"))
    has_author = (
        isinstance(authors, list) and any(isinstance(a, dict) and (_text(a.get("name")) or _text(a.get("email"))) for a in authors)
    ) or (isinstance(tools, list) and any(isinstance(t, dict) and (_text(t.get("name")) or _text(t.get("vendor"))) for t in tools))
    return found, {"author": has_author, "timestamp": _timestamp(metadata.get("timestamp")) is not None, "root": len(roots)}


def _spdx_license(package: JsonObject) -> str:
    for key in ("licenseConcluded", "licenseDeclared"):
        value = _text(package.get(key))
        if value and value not in ("NOASSERTION", "NONE"):
            return value
    return ""


def _spdx_components(doc: JsonObject) -> list[_Component]:
    found: list[_Component] = []
    for package in _list(doc.get("packages")):
        if not isinstance(package, dict):
            continue
        refs = [r for r in _list(package.get("externalRefs")) if isinstance(r, dict)]
        purl = next((_text(r.get("referenceLocator")) for r in refs if r.get("referenceType") == "purl" and _text(r.get("referenceLocator"))), "")
        supplier = _text(package.get("supplier"))
        found.append({
            "name": _text(package.get("name")),
            "group": "",
            "version": "" if _text(package.get("versionInfo")).upper() == "NOASSERTION" else _text(package.get("versionInfo")),
            "purl": purl,
            "supplier": bool(supplier) and not supplier.upper().startswith("NOASSERTION"),
            "identified": any(isinstance(r.get("referenceType"), str) and r["referenceType"] in _SPDX_ID_REFS and _text(r.get("referenceLocator")) for r in refs),
            "ref": _text(package.get("SPDXID")),
            "license": _spdx_license(package),
        })
    return found


def _sbom_kind(doc: JsonObject) -> str:
    if doc.get("bomFormat") == "CycloneDX":
        return "cyclonedx"
    if isinstance(doc.get("spdxVersion"), str) and doc["spdxVersion"].startswith("SPDX-2."):
        return "spdx"
    return ""


def _load_sbom(text: str, field: str) -> tuple[_SbomLoad | None, JsonObject | None]:
    doc, error = _load_json(text, field, "the SBOM")
    if doc is None:
        return None, error
    kind = _sbom_kind(doc)
    if not kind:
        return None, _invalid(field, "not CycloneDX JSON (bomFormat) or SPDX 2.x JSON (spdxVersion); SPDX 3 and XML are not supported")
    return {"kind": kind, "doc": doc}, None


def sbom_validate(payload: JsonObject) -> JsonObject:
    """Validate a CycloneDX 1.2-1.7 or SPDX 2.2/2.3 JSON SBOM: structural integrity and the seven NTIA minimum elements of the
    2021 baseline (CISA's 2026 minimum elements replace it and add fields such as component hash and license: not checked).
    `component_count` includes the described root component/package. Author accepts a CycloneDX tool entry; supplier
    `NOASSERTION` and a missing version do not count as present."""

    def work() -> JsonObject:
        loaded, error = _load_sbom(payload["document"], "document")
        if error:
            return error
        loaded = cast(_SbomLoad, loaded)  # _load_sbom returns a document whenever it returns no error
        doc, kind = loaded["doc"], loaded["kind"]
        findings: list[Finding] = []
        if kind == "cyclonedx":
            spec_version = _text(doc.get("specVersion"))
            if spec_version not in ("1.2", "1.3", "1.4", "1.5", "1.6", "1.7"):
                findings.append(_f("sbom.unknown-spec-version", "minor", f"specVersion {_echo_value(spec_version)!r} is not one of 1.2-1.7", "specVersion"))
            components, facts = _cdx_components(doc)
            known = {c["ref"] for c in components if c["ref"]}
            seen: set[str] = set()
            for c in components:
                if c["ref"] and c["ref"] in seen:
                    findings.append(_f("sbom.duplicate-bom-ref", "major", f"bom-ref {c['ref']!r} is used by more than one component", c["ref"]))
                seen.add(c["ref"])
            deps = [d for d in _list(doc.get("dependencies")) if isinstance(d, dict)]
            dangling: set[str] = set()
            linked = False
            for d in deps:
                for ref in [d.get("ref"), *_list(d.get("dependsOn"))]:
                    if isinstance(ref, str) and ref not in known:
                        dangling.add(ref)
                if _text(d.get("ref")) and (isinstance(d.get("dependsOn"), list) and d["dependsOn"]):
                    linked = True
            subjects = components
            body_count = len(components) - facts["root"]
            relationship_ok = bool(deps) and (linked or body_count <= 1) and not dangling
            author_ok, time_ok = facts["author"], facts["timestamp"]
        else:
            spec_version = doc["spdxVersion"][len("SPDX-"):]
            if spec_version not in ("2.2", "2.3"):
                findings.append(_f("sbom.unknown-spec-version", "minor", f"spdxVersion {_echo_value(doc['spdxVersion'])!r} is not SPDX-2.2 or SPDX-2.3", "spdxVersion"))
            if doc.get("dataLicense") != "CC0-1.0":
                findings.append(_f("sbom.spdx-data-license", "major", "dataLicense must be CC0-1.0", "dataLicense"))
            if doc.get("SPDXID") != "SPDXRef-DOCUMENT":
                findings.append(_f("sbom.spdx-document-id", "major", "SPDXID of the document must be SPDXRef-DOCUMENT", "SPDXID"))
            for key in ("name", "documentNamespace"):
                if not _text(doc.get(key)):
                    findings.append(_f("sbom.spdx-missing-field", "major", f"mandatory document field {key} is missing", key))
            components = _spdx_components(doc)
            ids = [c["ref"] for c in components]
            known = {"SPDXRef-DOCUMENT", *ids}
            seen = set()
            for ref in ids:
                if ref in seen:
                    findings.append(_f("sbom.duplicate-spdxid", "major", f"SPDXID {ref!r} is used by more than one package", ref))
                seen.add(ref)
            relationships = [r for r in _list(doc.get("relationships")) if isinstance(r, dict)]
            dangling = set()
            linked = False
            describes = bool(doc.get("documentDescribes"))
            for r in relationships:
                ends = [_text(r.get("spdxElementId")), _text(r.get("relatedSpdxElement"))]
                for end in ends:
                    if end and end not in known and end not in ("NOASSERTION", "NONE"):
                        dangling.add(end)
                if r.get("relationshipType") == "DESCRIBES":
                    describes = True
                if isinstance(r.get("relationshipType"), str) and r["relationshipType"] in _SPDX_DEP_RELATIONSHIPS and all(e in known for e in ends):
                    linked = True
            if not describes:
                findings.append(_f("sbom.no-root", "major", "no DESCRIBES relationship or documentDescribes names the described package", "relationships"))
            info = _dict(doc.get("creationInfo"))
            creators = [c for c in _list(info.get("creators")) if isinstance(c, str)]
            good = [c for c in creators if c.startswith(("Person: ", "Organization: ", "Tool: ")) and len(c.split(": ", 1)[1].strip()) > 0]
            for creator in creators:
                if creator not in good:
                    findings.append(_f("sbom.spdx-bad-creator", "minor", f"creator {_show(creator, 60)!r} is not 'Person:', 'Organization:' or 'Tool:' followed by a name", "creationInfo.creators"))
            body_count = len(components)
            relationship_ok = (linked or body_count <= 1) and not dangling
            author_ok, time_ok = bool(good), _timestamp(info.get("created")) is not None
            subjects = components
        for ref in sorted(dangling):
            findings.append(_f("sbom.dangling-reference", "major", f"relationship or dependency refers to unknown identifier {_show(ref, 80)!r}", ref))
        if body_count == 0:
            findings.append(_f("sbom.no-components", "major", "the SBOM lists no components", "components"))

        coverage = {
            "supplier_name": sum(1 for c in subjects if c["supplier"]),
            "component_name": sum(1 for c in subjects if c["name"]),
            "component_version": sum(1 for c in subjects if c["version"]),
            "unique_identifier": sum(1 for c in subjects if c["identified"]),
        }
        elements = []
        for key in _NTIA_ELEMENTS:
            if key in coverage:
                covered, total = coverage[key], len(subjects)
                satisfied = total > 0 and covered == total
            else:
                covered, total = (1 if {"dependency_relationship": relationship_ok, "sbom_author": author_ok, "timestamp": time_ok}[key] else 0), 1
                satisfied = covered == 1
            elements.append({"element": key, "satisfied": satisfied, "covered": covered, "total": total})
            if not satisfied and (subjects or key not in coverage):
                if key in coverage:
                    missing = [c["name"] or c["ref"] or "(unnamed)" for c in subjects if not {
                        "supplier_name": c["supplier"], "component_name": bool(c["name"]),
                        "component_version": bool(c["version"]), "unique_identifier": c["identified"]}[key]]
                    sample = ", ".join(sorted(set(missing))[:3])
                    findings.append(_f(f"ntia.{key}", "major", f"{_NTIA_LABEL[key]} missing on {total - covered} of {total} components (e.g. {sample})", key))
                else:
                    findings.append(_f(f"ntia.{key}", "major", f"{_NTIA_LABEL[key]} is missing or malformed", key))
        return {
            "state": "SUCCESS",
            "format": kind,
            "spec_version": _clip(_echo_value(spec_version), 40),
            "component_count": len(subjects),
            "ntia_compliant": all(e["satisfied"] for e in elements),
            "ntia_elements": elements,
            "verdict": _verdict(findings),
            **_result(findings),
        }

    return _respond(payload, work)


def _split_purl_version(base: str) -> tuple[str, str]:
    """(identity, version) of a package URL without subpath or qualifiers. The version follows the last `@` that is not the scope
    marker of a namespace: `pkg:npm/@scope/name` has no version, `pkg:npm/@scope/name@1.0.0` has one."""
    at = base.rfind("@")
    if at > 0 and base[at - 1] != "/" and "/" in base[:at]:
        return base[:at], base[at + 1:]
    return base, ""


def _component_key(c: _Component) -> str:
    """Identity of a component across versions: purl without version/qualifiers, else lowercase group/name."""
    purl = c["purl"]
    if purl.startswith("pkg:"):
        return _split_purl_version(purl.split("#", 1)[0].split("?", 1)[0])[0].lower()
    return f"{c['group']}/{c['name']}".strip("/").lower()


_SEMVER = re.compile(r"[vV]?(\d+(?:\.\d+)*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?")


def _version_key(version: str) -> tuple[tuple[int, ...], tuple[object, ...]] | None:
    """Sort key of a semantic version (semver.org item 11), or None when the string is not one: a release outranks its pre-releases,
    numeric pre-release identifiers compare as numbers and rank below alphanumeric ones, build metadata is ignored, `1.0` equals
    `1.0.0`. Epochs, PEP 440 and distro revisions are not semver: no direction is claimed for them."""
    match = _SEMVER.fullmatch(version)
    if not match:
        return None
    numbers = [int(part) for part in match.group(1).split(".")]
    while len(numbers) > 1 and numbers[-1] == 0:
        numbers.pop()
    pre = match.group(2)
    pre_key = (1,) if pre is None else (0, tuple((0, int(i), "") if i.isdigit() else (1, 0, i) for i in pre.split(".")))
    return tuple(numbers), pre_key


def sbom_diff(payload: JsonObject) -> JsonObject:
    """Diff two SBOMs by component identity: added, removed, version changes (with downgrades) and license changes."""

    def work() -> JsonObject:
        sides: list[dict[str, _Group]] = []
        for field in ("before", "after"):
            loaded, error = _load_sbom(payload[field], field)
            if error:
                return error
            loaded = cast(_SbomLoad, loaded)  # _load_sbom returns a document whenever it returns no error
            comps = _cdx_components(loaded["doc"])[0] if loaded["kind"] == "cyclonedx" else _spdx_components(loaded["doc"])
            grouped: dict[str, _Group] = {}
            for c in comps:
                if not (c["name"] or c["purl"]):
                    continue
                entry = grouped.setdefault(_component_key(c), {"name": _clip(c["name"] or c["purl"], 300), "versions": set(), "licenses": {}})
                entry["versions"].add(_clip(c["version"], 200))
                entry["licenses"][_clip(c["version"], 200)] = _clip(c["license"], 300)
            sides.append(grouped)
        before, after = sides
        added: list[Finding] = []
        removed: list[Finding] = []
        changed: list[Finding] = []
        relicensed: list[Finding] = []
        findings: list[Finding] = []
        for key in sorted(set(before) | set(after)):
            if key not in before:
                added.extend({"name": after[key]["name"], "version": v} for v in sorted(after[key]["versions"]))
                continue
            old = before[key]
            if key not in after:
                removed.extend({"name": old["name"], "version": v} for v in sorted(old["versions"]))
                continue
            new = after[key]
            gone, fresh = sorted(old["versions"] - new["versions"]), sorted(new["versions"] - old["versions"])
            if len(gone) == 1 and len(fresh) == 1:
                a, b = _version_key(gone[0]), _version_key(fresh[0])
                direction = "changed" if a is None or b is None or a == b else "upgrade" if b > a else "downgrade"
                changed.append({"name": new["name"], "before": gone[0], "after": fresh[0], "direction": direction})
                if direction == "downgrade":
                    findings.append(_f("sbom.version-downgrade", "major", f"{new['name']} went from {gone[0]} to {fresh[0]}", key))
                old_license, new_license = old["licenses"][gone[0]], new["licenses"][fresh[0]]
            else:
                added.extend({"name": new["name"], "version": v} for v in fresh)
                removed.extend({"name": old["name"], "version": v} for v in gone)
                common = sorted(old["versions"] & new["versions"])
                old_license, new_license = (old["licenses"][common[0]], new["licenses"][common[0]]) if common else ("", "")
            if old_license != new_license:
                relicensed.append({"name": new["name"], "before": old_license, "after": new_license})
                findings.append(_f("sbom.license-changed", "minor", f"{new['name']} license changed from {old_license or '(none)'} to {new_license or '(none)'}", key))
        downgrades = sum(1 for c in changed if c["direction"] == "downgrade")
        attention = bool(downgrades or relicensed)
        differs = bool(added or removed or changed or relicensed)
        return {
            "state": "SUCCESS",
            "added_count": len(added),
            "removed_count": len(removed),
            "version_changed_count": len(changed),
            "license_changed_count": len(relicensed),
            "downgrade_count": downgrades,
            "added": _fit(added, 24_000),
            "removed": _fit(removed, 24_000),
            "version_changed": _fit(changed, 24_000),
            "license_changed": _fit(relicensed, 24_000),
            "verdict": "review" if attention else "changed" if differs else "unchanged",
            **_result(findings),
        }

    return _respond(payload, work)


# ---- in-toto statements: SLSA level and subject digest -----------------------------------------------------------------

_SLSA_V1 = "https://slsa.dev/provenance/v1"
_SLSA_V02 = "https://slsa.dev/provenance/v0.2"
_STATEMENT_PREFIX = "https://in-toto.io/Statement/"


def _load_statement(text: str, field: str) -> tuple[JsonObject | None, JsonObject | None]:
    """Parse an in-toto Statement from plain JSON, a DSSE envelope or a Sigstore bundle (`dsseEnvelope`)."""
    doc, error = _load_json(text, field, "the attestation")
    if doc is None:
        return None, error
    inner = doc.get("dsseEnvelope")
    envelope = inner if isinstance(inner, dict) else doc
    if isinstance(envelope.get("payload"), str) and isinstance(envelope.get("payloadType"), str):
        raw = envelope["payload"]
        decoded = None
        for decoder in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                decoded = decoder(raw + "=" * (-len(raw) % 4)).decode("utf-8")
                break
            except (binascii.Error, ValueError):
                continue
        if decoded is None:
            return None, _invalid(field, "the DSSE payload is not base64-encoded UTF-8")
        doc, error = _load_json(decoded, field, "the DSSE payload")
        if doc is None:
            return None, error
    if not isinstance(doc.get("_type"), str) or not doc["_type"].startswith(_STATEMENT_PREFIX) or not isinstance(doc.get("predicateType"), str):
        return None, _invalid(field, f"not an in-toto Statement (_type must start with {_STATEMENT_PREFIX} and predicateType must be set)")
    return doc, None


def _subjects(statement: JsonObject) -> list[_Subject]:
    """[{name, digests: {alg: hex}}] with digest values lowercased; malformed subjects are kept with empty digests."""
    out: list[_Subject] = []
    for item in _list(statement.get("subject")):
        if not isinstance(item, dict):
            continue
        digests = _dict(item.get("digest"))
        out.append({"name": _text(item.get("name")), "digests": {str(k).lower(): v.lower() for k, v in digests.items() if isinstance(v, str)}})
    return out


def _usable_digest(algorithm: str, hex_part: str) -> bool:
    return _valid_digest(f"{algorithm}:{hex_part}") is not None


def _dict(value: object) -> JsonObject:
    return value if isinstance(value, dict) else {}


def slsa_level(payload: JsonObject) -> JsonObject:
    """Assess the SLSA Build level (0..3, v1.0 levels) of a provenance statement against the verification facts the caller supplies.

    L1: well-formed provenance exists (subject with a digest, builder id, build type, v1 external parameters).
    L2: L1 plus a verified signature and a builder id on the caller's trusted hosted-builder list. SLSA asks for a hosted build
        platform and authenticated provenance; the trusted-builder list is this tool's way to decide "hosted platform" (house rule).
    L3: L2 plus an isolated build and a signing key unreachable from user-defined build steps (both attested by the platform).
        SLSA's Build L3 also wants runs that cannot influence one another; the caller's isolated_build fact must cover that.
    """

    def work() -> JsonObject:
        statement, error = _load_statement(payload["provenance"], "provenance")
        if error:
            return error
        statement = cast(JsonObject, statement)  # _load_statement returns a statement whenever it returns no error
        predicate_type = statement["predicateType"]
        if predicate_type not in (_SLSA_V1, _SLSA_V02):
            return _invalid("provenance", f"predicateType {predicate_type!r} is not SLSA provenance v1 or v0.2")
        required = payload["required_level"]
        predicate = _dict(statement.get("predicate"))
        if predicate_type == _SLSA_V1:
            version = "v1"
            builder_id = _text(_dict(_dict(predicate.get("runDetails")).get("builder")).get("id"))
            definition = _dict(predicate.get("buildDefinition"))
            build_type = _text(definition.get("buildType"))
            external = definition.get("externalParameters")
        else:
            version = "v0.2"
            builder_id = _text(_dict(predicate.get("builder")).get("id"))
            build_type = _text(predicate.get("buildType"))
            external = True  # v0.2 has no mandatory external-parameters object
        subjects = _subjects(statement)
        gaps: dict[int, list[tuple[str, str]]] = {1: [], 2: [], 3: []}
        if not any(_usable_digest(a, h) for s in subjects for a, h in s["digests"].items()):
            gaps[1].append(("subject", "no subject carries a valid sha256/sha384/sha512 digest"))
        if not builder_id:
            gaps[1].append(("builder-id", "the builder id is missing"))
        if not build_type:
            gaps[1].append(("build-type", "the build type is missing"))
        if version == "v1" and not (isinstance(external, dict) and external):
            gaps[1].append(("external-parameters", "buildDefinition.externalParameters is missing or empty"))
        if not payload["signature_verified"]:
            gaps[2].append(("signature-unverified", "the provenance signature has not been verified"))
        if builder_id and builder_id not in payload["trusted_builder_ids"]:
            gaps[2].append(("builder-untrusted", f"builder {_show(builder_id, 100)!r} is not on the trusted hosted-builder list"))
        if not payload["isolated_build"]:
            gaps[3].append(("build-not-isolated", "the platform does not attest isolated builds"))
        if not payload["signing_key_isolated"]:
            gaps[3].append(("signing-key-exposed", "the signing key is not attested unreachable from user-defined build steps"))
        level = 0
        for n in (1, 2, 3):
            if gaps[n]:
                break
            level = n
        findings = [_f(f"slsa.l{n}-{name}", "major", f"SLSA L{n}: {text}", name) for n in (1, 2, 3) if n <= required for name, text in gaps[n]]
        if version == "v0.2":
            findings.append(_f("slsa.predicate-v0.2", "info", "provenance v0.2 predates the SLSA v1.0 Build track; mapped by builder.id and buildType", "predicateType"))
        nxt = [text for _, text in gaps[level + 1]] if level < 3 else []
        return {
            "state": "SUCCESS",
            "level": level,
            "required_level": required,
            "meets_required": level >= required,
            "predicate_version": version,
            "builder_id": _clip(builder_id, 300),
            "build_type": _clip(build_type, 300),
            "subject_count": len(subjects),
            "next_level_gaps": nxt,
            "verdict": "pass" if level >= required else "fail",
            **_result(findings),
        }

    return _respond(payload, work)


def provenance_match(payload: JsonObject) -> JsonObject:
    """Check that an artifact digest is one of the provenance subjects; a same-named subject with another digest is a blocker."""

    def work() -> JsonObject:
        statement, error = _load_statement(payload["provenance"], "provenance")
        if error:
            return error
        statement = cast(JsonObject, statement)  # _load_statement returns a statement whenever it returns no error
        wanted = _valid_digest(payload["artifact_digest"])
        if wanted is None:
            return _invalid("artifact_digest", "artifact_digest must be sha256:<64 hex>, sha384:<96 hex> or sha512:<128 hex>")
        algorithm, hex_part = wanted
        artifact_name = payload.get("artifact_name", "")
        subjects = _subjects(statement)
        findings: list[Finding] = []
        if not subjects:
            findings.append(_f("provenance.no-subject", "blocker", "the statement has no subjects", "subject"))
        matching = [s for s in subjects if s["digests"].get(algorithm) == hex_part]
        matched_name = ""
        if matching:
            named = [s for s in matching if artifact_name and s["name"] == artifact_name]
            matched_name = (named or matching)[0]["name"]
            if artifact_name and not named:
                findings.append(_f("provenance.name-mismatch", "major", f"digest matches subject {matched_name!r} but not the artifact name {artifact_name!r}", matched_name))
        elif subjects:
            findings.append(_f("provenance.digest-not-in-subjects", "blocker", f"{algorithm}:{hex_part[:12]}... is not a subject of the provenance", "subject"))
            if artifact_name:
                for s in subjects:
                    if s["name"] == artifact_name and algorithm in s["digests"]:
                        findings.append(_f("provenance.digest-mismatch", "blocker", f"subject {artifact_name!r} has digest {algorithm}:{s['digests'][algorithm][:12]}... instead", s["name"]))
        matched = bool(matching) and not any(f["id"] == "provenance.name-mismatch" for f in findings)
        return {
            "state": "SUCCESS",
            "matched": matched,
            "algorithm": algorithm,
            "matched_subject": _clip(matched_name, 300),
            "subject_count": len(subjects),
            "verdict": _verdict(findings),
            **_result(findings),
        }

    return _respond(payload, work)


# ---- signature policy -----------------------------------------------------------------------------------------------


def _glob_match(pattern: str, text: str) -> bool:
    """`*` matches any run of characters (including none); everything else is literal. No regular expressions."""
    parts = pattern.split("*")
    if len(parts) == 1:
        return pattern == text
    if not text.startswith(parts[0]) or not text[len(parts[0]):].endswith(parts[-1]):
        return False
    position = len(parts[0])
    limit = len(text) - len(parts[-1])
    for middle in parts[1:-1]:
        found = text.find(middle, position, limit)
        if found < 0:
            return False
        position = found + len(middle)
    return position <= limit


def signature_policy(payload: JsonObject) -> JsonObject:
    """Evaluate verified signatures against required signer rules (issuer equality and an identity glob), a minimum
    number of distinct (issuer, signer) pairs and an optional transparency-log requirement. Keyless Sigstore: identity is the
    certificate SAN, issuer the OIDC issuer URL. House rules, not Sigstore semantics: any signature that did not verify is a
    blocker even when others verified, and `*` in an identity also matches `/`."""

    def work() -> JsonObject:
        rules = payload["rules"]
        names = [r["name"] for r in rules]
        if len(set(names)) != len(names) or any(not n.strip() for n in names):
            return _invalid("rules", "rule names must be non-empty and unique")
        if any(not r["issuer"].strip() or not r["identity"].strip() for r in rules):
            return _invalid("rules", "every rule needs a non-empty issuer and identity")
        require_log = payload["require_transparency_log"]
        findings: list[Finding] = []
        counted: list[JsonObject] = []
        unverified = 0
        for s in payload["signatures"]:
            who = _echo_value(f"{s['signer']} ({s['issuer']})")
            if not s["verified"]:
                unverified += 1
                findings.append(_f("signature.verification-failed", "blocker", f"signature by {who} did not verify", _echo_value(s["signer"])))
            elif require_log and not s["in_transparency_log"]:
                findings.append(_f("signature.not-in-transparency-log", "major", f"signature by {who} has no transparency-log entry", _echo_value(s["signer"])))
            else:
                counted.append(s)
        missing: list[str] = []
        satisfied = 0
        used: set[int] = set()
        for r in rules:
            hits = [i for i, s in enumerate(counted) if s["issuer"] == r["issuer"] and _glob_match(r["identity"], s["signer"])]
            used.update(hits)
            if hits:
                satisfied += 1
            elif r["required"]:
                missing.append(_echo_value(r["name"]))
                findings.append(_f("signature.required-signer-missing", "blocker", f"no verified signature matches rule {_echo_value(r['name'])!r} (issuer {_echo_value(r['issuer'])}, identity {_echo_value(r['identity'])})", _echo_value(r["name"])))
        unexpected = sorted({_echo_value(counted[i]["signer"]) for i in range(len(counted)) if i not in used})
        for signer in unexpected:
            findings.append(_f("signature.unexpected-signer", "minor", f"verified signer {_show(signer, 120)!r} matches no rule", signer))
        distinct = len({(s["issuer"], s["signer"]) for s in counted})
        if distinct < payload["min_valid_signatures"]:
            findings.append(_f("signature.too-few", "major", f"{distinct} distinct valid signer(s), policy needs {payload['min_valid_signatures']}", "signatures"))
        return {
            "state": "SUCCESS",
            "valid_signatures": len(counted),
            "unverified_signatures": unverified,
            "rules_satisfied": satisfied,
            "required_rules_missing": sorted(missing),
            "unexpected_signers": unexpected,
            "verdict": _verdict(findings),
            **_result(findings),
        }

    return _respond(payload, work)


# ---- VEX ------------------------------------------------------------------------------------------------------------

_VEX_JUSTIFICATIONS = {
    "component_not_present", "vulnerable_code_not_present", "vulnerable_code_cannot_be_controlled_by_adversary",
    "vulnerable_code_not_in_execute_path", "inline_mitigations_already_exist",
}
_SEVERITY_TO_FINDING = {"critical": "blocker", "high": "major", "medium": "minor", "low": "info", "unknown": "minor"}
# Conservative order when two statements about the same pair carry the same timestamp: the worse status wins.
_VEX_PRECEDENCE = {"affected": 0, "under_investigation": 1, "fixed": 2, "not_affected": 3}


_PurlParts: TypeAlias = tuple[str, str, frozenset[tuple[str, str]]]  # (identity, version, qualifiers)


def _purl_parts(product: str) -> _PurlParts:
    """(identity, version, qualifiers) of a package URL: subpath dropped, type lowercased, qualifier keys lowercased (purl spec);
    other strings are kept as is, without version or qualifiers."""
    if not product.startswith("pkg:"):
        return product, "", frozenset()
    base, _, query = product.split("#", 1)[0].partition("?")
    qualifiers = frozenset((key.lower(), value) for key, _eq, value in (item.partition("=") for item in query.split("&")) if key and value)
    head, version = _split_purl_version(base)
    scheme_type, slash, rest = head[4:].partition("/")
    return f"pkg:{scheme_type.lower()}{slash}{rest}", version, qualifiers


def _product_matches(statement_parts: _PurlParts, product_parts: _PurlParts) -> bool:
    """A statement product matches when the identities are equal, its version is absent or equal and every qualifier it names
    (arch, distro ...) is also on the vulnerable product: a statement about arm64 says nothing about amd64."""
    return (statement_parts[0] == product_parts[0] and (not statement_parts[1] or statement_parts[1] == product_parts[1])
            and statement_parts[2] <= product_parts[2])


def vex_apply(payload: JsonObject) -> JsonObject:
    """Apply OpenVEX statements to a vulnerability list. The newest valid statement per (vulnerability, product) wins
    (equal timestamps: the more conservative status, a house rule); `not_affected` needs one of the five OpenVEX justifications
    or an impact statement or the statement is ignored. Products match by package URL identity: a statement product without a
    version covers every version, and a statement product without qualifiers covers every variant (arch, distro ...), but
    qualifiers it names must be present on the vulnerable product. Product names that are not purls match exactly."""

    def work() -> JsonObject:
        findings: list[Finding] = []
        statements: list[JsonObject] = []
        ignored = 0
        for index, s in enumerate(payload["statements"]):
            moment = _timestamp(s["timestamp"])
            if moment is None:
                return _invalid("statements", f"statements[{index}].timestamp must be ISO-8601 with an explicit offset")
            justification = s.get("justification", "")
            if justification and justification not in _VEX_JUSTIFICATIONS:
                return _invalid("statements", f"statements[{index}].justification {_echo_value(justification)!r} is not an OpenVEX justification")
            if s["status"] == "not_affected" and not justification and not _text(s.get("impact_statement")):
                ignored += 1
                findings.append(_f("vex.not-affected-unjustified", "major", f"{_echo_value(s['vulnerability'])}: not_affected needs a justification or impact statement; statement ignored", _echo_value(s["vulnerability"])))
                continue
            parts = [_purl_parts(p.strip()) for p in s["products"]]
            statements.append({**s, "moment": moment, "vulnerability": s["vulnerability"].strip().upper(), "parts": parts})
        by_vulnerability: dict[str, list[tuple[int, JsonObject]]] = {}
        for index, s in enumerate(statements):
            by_vulnerability.setdefault(s["vulnerability"], []).append((index, s))
        seen: set[tuple[str, str]] = set()
        exposed: list[JsonObject] = []
        suppressed: list[JsonObject] = []
        used: set[int] = set()
        for v in payload["vulnerabilities"]:
            shown, product = v["id"].strip(), v["product"].strip()
            vid = shown.upper()
            if not vid or not product:
                return _invalid("vulnerabilities", "id and product must not be blank")
            if (vid, product) in seen:
                return _invalid("vulnerabilities", f"{shown} / {product} is listed twice")
            seen.add((vid, product))
            wanted = _purl_parts(product)
            candidates = [(i, s) for i, s in by_vulnerability.get(vid, []) if any(_product_matches(p, wanted) for p in s["parts"])]
            if not candidates:
                exposed.append({"id": _scrub(shown), "product": _scrub(product), "severity": v["severity"], "status": "unassessed"})
                continue
            index, best = min(candidates, key=lambda c: (-c[1]["moment"].timestamp(), _VEX_PRECEDENCE[c[1]["status"]], c[0]))
            used.update(i for i, _ in candidates)
            if best["status"] in ("not_affected", "fixed"):
                suppressed.append({"id": _scrub(shown), "product": _scrub(product), "status": best["status"], "justification": best.get("justification", "")})
            else:
                exposed.append({"id": _scrub(shown), "product": _scrub(product), "severity": v["severity"], "status": best["status"]})
        exposed.sort(key=lambda e: (_RANK[_SEVERITY_TO_FINDING[e["severity"]]], e["id"], e["product"]))
        suppressed.sort(key=lambda e: (e["id"], e["product"]))
        for e in exposed:
            findings.append(_f(e["id"], _SEVERITY_TO_FINDING[e["severity"]], f"{e['severity']} vulnerability in {e['product']} is exposed ({e['status']})", e["product"]))
        unmatched = len(statements) - len(used)
        return {
            "state": "SUCCESS",
            "total": len(payload["vulnerabilities"]),
            "exposed_count": len(exposed),
            "suppressed_count": len(suppressed),
            "ignored_statements": ignored,
            "unmatched_statements": unmatched,
            "exposed": _fit(exposed, 48_000),
            "suppressed": _fit(suppressed, 48_000),
            "verdict": "exposed" if exposed else "clear",
            **_result(findings),
        }

    return _respond(payload, work)


# ---- pin audit --------------------------------------------------------------------------------------------------------

_USES_LINE = re.compile(r"(?:-\s+)?uses:\s*(\S.*)")
_IMAGE_LINE = re.compile(r"(?:-\s+)?(?:image|container):\s*(\S.*)")
_SHA_REF = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_FIRST_PARTY_OWNERS = {"actions", "github"}
_NPM_EXACT = re.compile(r"[=v]?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?")
_NPM_SHORTHAND = re.compile(r"[\w.-]+/[\w.-]+(?:#.*)?")
_NPM_LATEST = {"", "*", "x", "latest", "next"}
_LOCKFILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json"}
_NPM_SECTIONS = ("dependencies", "devDependencies", "optionalDependencies")


def _cut_comment(text: str) -> str:
    """Drop a trailing `# comment` (a # that starts the text or follows whitespace); linear, no backtracking."""
    cut = len(text)
    for index, char in enumerate(text):
        if char == "#" and (index == 0 or text[index - 1] in " \t"):
            cut = index
            break
    return text[:cut]


def _uncomment(value: str) -> str:
    return _cut_comment(value).strip().strip("'\"")


def _norm_path(path: str) -> str:
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def _pin_kind(path: str) -> str:
    low = path.lower()
    base = low.rsplit("/", 1)[-1]
    if (low.startswith(".github/workflows/") or "/.github/workflows/" in low) and base.endswith((".yml", ".yaml")):
        return "github-actions"
    if "/.github/actions/" in "/" + low and base in ("action.yml", "action.yaml"):
        return "github-actions"
    if base in ("dockerfile", "containerfile") or base.startswith("dockerfile.") or base.endswith(".dockerfile"):
        return "dockerfile"
    if re.fullmatch(r"(?:requirements|constraints)[\w.-]*\.txt", base):
        return "requirements"
    if base == "package.json":
        return "package-json"
    if base in _LOCKFILES:
        return "lockfile"
    return ""


def _is_action_metadata(path: str) -> bool:
    """action.yml / action.yaml under .github/actions/: an action's metadata, whose `image:` is a docker:// reference or a Dockerfile.
    A workflow file that happens to be named action.yml is a workflow: the path decides, not the basename alone."""
    low = "/" + path.replace("\\", "/").lower()
    return "/.github/actions/" in low and low.rsplit("/", 1)[-1] in ("action.yml", "action.yaml")


def _docker_action_pin(value: str, at: str) -> list[Finding]:
    """A `docker://` reference (a `uses:` or action-metadata `image:`) must be pinned by digest: the same rule for both places."""
    ref = value[len("docker://"):]
    parsed, _reason = _parse_ref(ref)
    pinning = _ref_pinning(parsed) if parsed else "tag"
    if pinning == "digest":
        return []
    return [_f("pin.action-image-unpinned", "major" if pinning == "latest" or parsed is None else "minor", f"docker://{_echo_ref(ref)} is not pinned by digest", at)]



def _audit_workflow(path: str, content: str) -> tuple[int, list[Finding]]:
    action_metadata = _is_action_metadata(path)
    refs, findings = 0, []
    for number, line in enumerate(content.split("\n"), 1):
        at = f"{path}:{number}"
        if line.lstrip().startswith("#"):
            continue
        match = _USES_LINE.fullmatch(line.strip())
        if match:
            value = _uncomment(match.group(1))
            if value.startswith(("./", "../")) or "${{" in value:
                continue
            refs += 1
            if value.startswith("docker://"):
                findings += _docker_action_pin(value, at)
                continue
            name, at_sign, ref = value.rpartition("@")
            if not at_sign:
                findings.append(_f("pin.action-no-ref", "major", f"{value} has no @ref", at))
            elif not _SHA_REF.fullmatch(ref):
                owner = name.split("/", 1)[0].lower()
                mutable = ref.lower() in _MUTABLE_BRANCHES
                severity = "major" if mutable or owner not in _FIRST_PARTY_OWNERS else "minor"
                findings.append(_f("pin.action-not-sha", severity, f"{name}@{ref} is not pinned to a full commit SHA" + (" (mutable branch)" if mutable else ""), at))
            continue
        match = _IMAGE_LINE.fullmatch(line.strip())
        if match:
            value = _uncomment(match.group(1))
            if "${{" in value or "${" in value or value in ("{", "|", ">"):
                continue
            if action_metadata:  # action.yml: `image: docker://ref` is a registry image; any other value names a Dockerfile of the action
                if value.startswith("docker://"):
                    refs += 1
                    findings += _docker_action_pin(value, at)
                continue
            parsed, reason = _parse_ref(value)
            refs += 1
            if parsed is None:
                findings.append(_f("pin.image-invalid-reference", "major", f"image {_echo_ref(value)} is not a valid reference: {reason}", at))
                continue
            pinning = _ref_pinning(parsed)
            if pinning != "digest":
                findings.append(_f("pin.image-latest" if pinning == "latest" else "pin.image-tag-only", "major" if pinning == "latest" else "minor", f"image {value} is not pinned by digest", at))
    return refs, findings


def _audit_dockerfile(path: str, content: str) -> tuple[int, list[Finding]]:
    stages, global_args = _stages(_parse_dockerfile(content))
    refs, findings = 0, []
    for stage in stages:
        base, location = stage["base"], f"{path}:{stage['line']}"
        if base and (base.lower() == "scratch" or stage["parent"] is not None):
            continue  # scratch and earlier-stage bases are not registry references
        refs += 1
        if not base:
            findings.append(_f("pin.base-invalid-reference", "major", "FROM has no image", location))
            continue
        text, resolved = _expand(base, global_args)
        if not resolved:
            findings.append(_f("pin.base-unresolved-arg", "major", f"FROM {_echo_value(base)} depends on a build argument without default; the image cannot be checked", location))
            continue
        parsed, reason = _parse_ref(text)
        if parsed is None:
            findings.append(_f("pin.base-invalid-reference", "major", f"FROM {_echo_ref(text)} is not a valid reference: {reason}", location))
            continue
        pinning = _ref_pinning(parsed)
        if pinning != "digest":
            findings.append(_f("pin.image-latest" if pinning == "latest" else "pin.image-tag-only", "major" if pinning == "latest" else "minor",
                               f"FROM {text} is not pinned by digest", location))
    return refs, findings


def _audit_requirements(path: str, content: str) -> tuple[int, list[Finding]]:
    refs, findings, hashed = 0, [], False
    pending, start = "", 0
    logical: list[tuple[int, str]] = []
    for number, line in enumerate(content.split("\n"), 1):
        if not pending:
            start = number
        text = line.rstrip()
        if text.endswith("\\"):
            pending += text[:-1] + " "
            continue
        logical.append((start, pending + text))
        pending = ""
    if pending:
        logical.append((start, pending))
    for number, line in logical:
        line = _cut_comment(line).strip()
        if not line:
            continue
        editable = line.startswith(("-e ", "--editable"))
        if line.startswith("-") and not editable:
            continue
        tokens = line.split()
        if editable:
            tokens = tokens[1:]
        hashed = hashed or any(t.startswith("--hash") for t in tokens)
        req = " ".join(t for t in tokens if not t.startswith("--")).split(";", 1)[0].strip()
        at = f"{path}:{number}"
        if not req:
            continue
        refs += 1
        if "://" in req:
            if not (re.search(r"@[0-9a-f]{40}\b", req) or "sha256=" in req):
                findings.append(_f("pin.requirement-url-unpinned", "major", f"{_show(req, 100)} is a URL/VCS dependency without a commit SHA or hash", at))
            continue
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*(.*)", req)
        if not match:
            refs -= 1
            continue
        spec = match.group(2).replace(" ", "")
        if not re.fullmatch(r"===?[A-Za-z0-9.!+_-]+", spec) or "*" in spec:
            findings.append(_f("pin.requirement-not-exact", "minor", f"{match.group(1)} is not pinned with == ({spec or 'no version'})", at))
    if refs and not hashed:
        findings.append(_f("pin.requirements-no-hashes", "info", "no --hash entries; pip --require-hashes cannot verify downloads", path))
    return refs, findings


def _npm_pin(value: str) -> tuple[bool, str] | None:
    """(pinned, severity if not) for a package.json dependency specifier; None for workspace/file/link specifiers."""
    text = value.strip()
    if text.startswith(("workspace:", "file:", "link:", "portal:")):
        return None
    if text.startswith("npm:") and "@" in text[4:]:
        text = text.rsplit("@", 1)[1]
    if _NPM_EXACT.fullmatch(text):
        return True, ""
    if "://" in text or text.startswith(("github:", "gitlab:", "bitbucket:", "git+", "git@")) or _NPM_SHORTHAND.fullmatch(text):
        return (True, "") if re.search(r"#[0-9a-f]{40}$", text) else (False, "major")
    return (False, "major") if text.lower() in _NPM_LATEST else (False, "minor")


def _audit_package_json(path: str, content: str, has_lockfile: bool) -> tuple[int, list[Finding]]:
    try:
        doc = json.loads(content)
    except (ValueError, RecursionError):
        return 0, [_f("pin.unparseable", "major", "package.json is not valid JSON", path)]
    if not isinstance(doc, dict):
        return 0, [_f("pin.unparseable", "major", "package.json must be a JSON object", path)]
    refs, findings = 0, []
    for section in _NPM_SECTIONS:
        entries = _dict(doc.get(section))
        for name in sorted(entries):
            value = entries[name]
            if not isinstance(value, str):
                continue
            verdict = _npm_pin(value)
            if verdict is None:
                continue
            refs += 1
            if not verdict[0]:
                findings.append(_f("pin.npm-not-exact", verdict[1], f"{section}.{name} is {value!r}, not an exact version or commit", path))
    if refs and not has_lockfile:
        findings.append(_f("pin.no-lockfile", "minor", "no lockfile for this package.json was supplied", path))
    return refs, findings


def pin_audit(payload: JsonObject) -> JsonObject:
    """Audit supplied files for unpinned dependencies: GitHub Actions (`uses:` without a full commit SHA, floating
    images), Dockerfile FROM lines, requirements*.txt (`==` pins) and package.json (exact versions or commit pins).
    Supply lockfiles (content may be empty) to let the missing-lockfile check see them."""

    def work() -> JsonObject:
        files = payload["files"]
        paths = [_norm_path(f["path"]) for f in files]
        if len(set(paths)) != len(paths):
            return _invalid("files", "paths must be unique")
        if any(not p or p.startswith("/") or ".." in p.split("/") or "\x00" in p for p in paths):
            return _invalid("files", "paths must be non-empty, relative and free of '..'")
        lock_dirs = {p.rsplit("/", 1)[0] if "/" in p else "" for p in paths if p.rsplit("/", 1)[-1].lower() in _LOCKFILES}
        findings: list[Finding] = []
        by_kind: dict[str, list[int]] = {}
        skipped = 0
        for path, entry in zip(paths, files):
            kind = _pin_kind(path)
            content = entry["content"]
            if kind == "":
                skipped += 1
            if kind in ("", "lockfile"):
                continue
            if kind == "github-actions":
                refs, found = _audit_workflow(path, content)
            elif kind == "dockerfile":
                refs, found = _audit_dockerfile(path, content)
            elif kind == "requirements":
                refs, found = _audit_requirements(path, content)
            else:
                refs, found = _audit_package_json(path, content, (path.rsplit("/", 1)[0] if "/" in path else "") in lock_dirs)
            unpinned = sum(1 for f in found if f["id"] not in ("pin.requirements-no-hashes", "pin.no-lockfile", "pin.unparseable"))
            tally = by_kind.setdefault(kind, [0, 0])
            tally[0] += refs
            tally[1] += unpinned
            findings.extend(found)
        references = sum(t[0] for t in by_kind.values())
        unpinned_total = sum(t[1] for t in by_kind.values())
        pinned_pct = 100.0 if references == 0 else round(100 * (references - unpinned_total) / references, 2)
        return {
            "state": "SUCCESS",
            "files_audited": sum(1 for p in paths if _pin_kind(p) not in ("", "lockfile")),
            "files_skipped": skipped,
            "references": references,
            "unpinned": unpinned_total,
            "pinned_pct": pinned_pct,
            "by_kind": [{"kind": k, "references": by_kind[k][0], "unpinned": by_kind[k][1]} for k in sorted(by_kind)],
            "verdict": _verdict(findings),
            **_result(findings),
        }

    return _respond(payload, work)


# ---- license policy ---------------------------------------------------------------------------------------------------

# SPDX License List identifiers (lowercase) grouped by the usual FSF/SPDX copyleft strength. Deliberately small.
_LICENSE_CATEGORY = {
    **dict.fromkeys(
        ("mit", "mit-0", "apache-2.0", "bsd-2-clause", "bsd-3-clause", "isc", "0bsd", "zlib", "bsl-1.0", "cc0-1.0", "unlicense",
         "postgresql", "blueoak-1.0.0", "psf-2.0"), "permissive"),
    **dict.fromkeys(
        ("lgpl-2.1-only", "lgpl-2.1-or-later", "lgpl-3.0-only", "lgpl-3.0-or-later", "mpl-2.0", "epl-1.0", "epl-2.0", "cddl-1.0", "cddl-1.1"),
        "weak-copyleft"),
    **dict.fromkeys(("gpl-2.0-only", "gpl-2.0-or-later", "gpl-3.0-only", "gpl-3.0-or-later"), "strong-copyleft"),
    **dict.fromkeys(("agpl-3.0-only", "agpl-3.0-or-later"), "network-copyleft"),
    **dict.fromkeys(("sspl-1.0", "busl-1.1", "elastic-2.0"), "source-available"),
}
# Deprecated SPDX identifiers that still occur in package metadata, mapped to their current -only/-or-later form.
_LICENSE_ALIASES = {
    f"{base}{plus}": f"{base}-{'or-later' if plus else 'only'}"
    for base in ("gpl-2.0", "gpl-3.0", "lgpl-2.1", "lgpl-3.0", "agpl-3.0")
    for plus in ("", "+")
}
_LICENSE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+-]*(?::[A-Za-z0-9.+-]+)?")
_LICENSE_TOKEN = re.compile(r"\(|\)|[^\s()]+")
_MAX_EXPRESSION_DEPTH = 16


class _ExpressionError(ValueError):
    pass


def _license_id(text: str) -> str:
    low = text.lower()
    return _LICENSE_ALIASES.get(low, low)


_LicenseLeaf: TypeAlias = tuple[str, str, str]  # ("id", license id, exception id or "")
_LicenseTree: TypeAlias = "_LicenseLeaf | tuple[str, list[_LicenseTree]]"  # an id leaf, or ("and" | "or", operands)


def _parse_expression(expression: str) -> _LicenseTree:
    """SPDX license expression -> nested tuples. WITH binds tighter than AND, AND tighter than OR (SPDX 2.3 Annex D).
    Operators are accepted in any case."""
    tokens = _LICENSE_TOKEN.findall(expression)
    if not tokens:
        raise _ExpressionError("empty expression")
    position = 0

    def peek() -> str:
        return tokens[position] if position < len(tokens) else ""

    def operator(word: str) -> bool:
        return peek().upper() == word

    def parse_or(depth: int) -> _LicenseTree:
        nonlocal position
        if depth > _MAX_EXPRESSION_DEPTH:
            raise _ExpressionError("expression nests too deeply")
        items = [parse_and(depth)]
        while operator("OR"):
            position += 1
            items.append(parse_and(depth))
        return items[0] if len(items) == 1 else ("or", items)

    def parse_and(depth: int) -> _LicenseTree:
        nonlocal position
        items = [parse_atom(depth)]
        while operator("AND"):
            position += 1
            items.append(parse_atom(depth))
        return items[0] if len(items) == 1 else ("and", items)

    def parse_atom(depth: int) -> _LicenseTree:
        nonlocal position
        token = peek()
        if token == "(":
            position += 1
            inner = parse_or(depth + 1)
            if peek() != ")":
                raise _ExpressionError("missing closing parenthesis")
            position += 1
            return inner
        if not token or token in (")",) or token.upper() in ("AND", "OR", "WITH") or not _LICENSE_ID.fullmatch(token):
            raise _ExpressionError(f"unexpected token {token!r}" if token else "unexpected end of expression")
        position += 1
        exception = ""
        if operator("WITH"):
            position += 1
            exception = peek()
            if not exception or exception in ("(", ")") or not _LICENSE_ID.fullmatch(exception):
                raise _ExpressionError("WITH needs a license exception id")
            position += 1
        return ("id", token, exception)

    tree = parse_or(0)
    if position != len(tokens):
        raise _ExpressionError(f"unexpected token {tokens[position]!r}")
    return tree


def _atom_status(tree: _LicenseLeaf, allow: set[str], deny: set[str], deny_categories: set[str]) -> tuple[str, list[str]]:
    _kind, name, exception = tree
    base = _license_id(name)
    full = f"{base} with {exception.lower()}" if exception else base
    label = f"{name} WITH {exception}" if exception else name
    if full in deny or base in deny:
        return "denied", [label]
    if full in allow or base in allow:
        return "allowed", []
    category = _LICENSE_CATEGORY.get(base, "")
    if category and category in deny_categories:
        return "denied", [f"{label} ({category})"]
    return "review", [label]


def _evaluate(tree: _LicenseTree, allow: set[str], deny: set[str], deny_categories: set[str]) -> tuple[str, list[str]]:
    if len(tree) == 3:
        return _atom_status(tree, allow, deny, deny_categories)
    results = [_evaluate(child, allow, deny, deny_categories) for child in tree[1]]
    statuses = [r[0] for r in results]
    if tree[0] == "and":
        status = "denied" if "denied" in statuses else "review" if "review" in statuses else "allowed"
        return status, [reason for s, rs in results if s != "allowed" for reason in rs]
    status = "allowed" if "allowed" in statuses else "review" if "review" in statuses else "denied"
    return status, [] if status == "allowed" else [reason for _s, rs in results for reason in rs]


def _policy_set(entries: list[str], field: str) -> tuple[set[str] | None, JsonObject | None]:
    out: set[str] = set()
    for entry in entries:
        words = entry.split()
        if len(words) == 1:
            out.add(_license_id(words[0]))
        elif len(words) == 3 and words[1].upper() == "WITH":
            out.add(f"{_license_id(words[0])} with {words[2].lower()}")
        else:
            return None, _invalid(field, f"{entry!r} is not an SPDX license id or `<id> WITH <exception>`")
    return out, None


def license_policy(payload: JsonObject) -> JsonObject:
    """Evaluate SPDX license expressions of components against allow/deny lists and denied copyleft categories.
    OR is satisfied by any allowed alternative; AND needs every operand allowed; unlisted licenses need review. Precedence is
    WITH, AND, OR (SPDX 2.3 Annex D); operators are accepted in any case although the SPDX text says to match them case-sensitively;
    identifiers match case-insensitively; `+` is resolved only for the deprecated GPL/LGPL/AGPL ids, any other `X+` needs review."""

    def work() -> JsonObject:
        allow, error = _policy_set(payload["allow"], "allow")
        if error:
            return error
        allow = cast(set[str], allow)  # _policy_set returns a set whenever it returns no error
        deny, error = _policy_set(payload["deny"], "deny")
        if error:
            return error
        deny = cast(set[str], deny)  # the same invariant holds for deny
        both = sorted(allow & deny)
        if both:
            return _invalid("deny", f"{both[0]} is both allowed and denied")
        deny_categories = set(payload["deny_categories"])
        counts = {"allowed": 0, "denied": 0, "review": 0}
        findings: list[Finding] = []
        for c in payload["components"]:
            expression = c["license"].strip()
            name = c["name"]
            if not expression or expression.upper() in ("NOASSERTION", "NONE"):
                counts["review"] += 1
                findings.append(_f("license.missing", "minor", f"{name}: no license asserted", name))
                continue
            try:
                status, reasons = _evaluate(_parse_expression(expression), allow, deny, deny_categories)
            except _ExpressionError as exc:
                counts["review"] += 1
                findings.append(_f("license.unparseable", "minor", f"{name}: {_show(expression, 80)!r} is not a valid SPDX expression ({exc})", name))
                continue
            counts[status] += 1
            if status == "denied":
                findings.append(_f("license.denied", "major", f"{name}: {_show(expression, 80)} is denied by {_show(', '.join(sorted(set(reasons))), 120)}", name))
            elif status == "review":
                findings.append(_f("license.review", "minor", f"{name}: {_show(expression, 80)} is not covered by the policy ({_show(', '.join(sorted(set(reasons))), 120)})", name))
        return {
            "state": "SUCCESS",
            "total": len(payload["components"]),
            "allowed_count": counts["allowed"],
            "denied_count": counts["denied"],
            "review_count": counts["review"],
            "verdict": "fail" if counts["denied"] else "review" if counts["review"] else "pass",
            **_result(findings),
        }

    return _respond(payload, work)


# ---- registry retention plan -------------------------------------------------------------------------------------------

_KEEP_REASONS = ("in-use-digest", "protected-prefix", "recent-count", "min-age")


def retention_plan(payload: JsonObject) -> JsonObject:
    """Plan which registry tags to keep or delete. A tag is kept when its digest is in use, it starts with a protected
    prefix, it is one of the `keep_latest` newest tags, or it is younger than `min_age_days`; the rest are deleted.
    The verdict fails when the plan would delete more than `max_delete_pct` percent of the tags (a circuit breaker).
    Plan only: A18 never deletes anything."""

    def work() -> JsonObject:
        as_of = _timestamp(payload["as_of"])
        if as_of is None:
            return _invalid("as_of", "as_of must be an ISO-8601 timestamp with an explicit offset")
        protected_digests = set()
        for index, digest in enumerate(payload["protected_digests"]):
            parsed = _valid_digest(digest)
            if parsed is None:
                return _invalid("protected_digests", f"protected_digests[{index}] is not a valid OCI digest (algorithm:hex)")
            protected_digests.add(f"{parsed[0]}:{parsed[1]}")
        tags = []
        seen: set[str] = set()
        for index, entry in enumerate(payload["tags"]):
            name = entry["tag"]
            parsed = _valid_digest(entry["digest"])
            moment = _timestamp(entry["pushed_at"])
            if not _TAG.fullmatch(name):
                return _invalid("tags", f"tags[{index}].tag is not a valid OCI tag")
            if name in seen:
                return _invalid("tags", f"tag {name} is listed twice")
            seen.add(name)
            if parsed is None:
                return _invalid("tags", f"tag {name}: digest is not a valid OCI digest")
            if moment is None or moment > as_of:
                return _invalid("tags", f"tag {name}: pushed_at must be ISO-8601 with an offset and not after as_of")
            tags.append({"tag": name, "digest": f"{parsed[0]}:{parsed[1]}", "moment": moment})
        newest = sorted(tags, key=lambda t: (-t["moment"].timestamp(), t["tag"]))
        recent = {t["tag"] for t in newest[:payload["keep_latest"]]}
        min_age_seconds = payload["min_age_days"] * 86400
        kept: dict[str, str] = {}
        for t in tags:
            if t["digest"] in protected_digests:
                kept[t["tag"]] = "in-use-digest"
            elif any(t["tag"].startswith(p) for p in payload["protected_prefixes"]):
                kept[t["tag"]] = "protected-prefix"
            elif t["tag"] in recent:
                kept[t["tag"]] = "recent-count"
            elif (as_of - t["moment"]).total_seconds() < min_age_seconds:
                kept[t["tag"]] = "min-age"
        delete = sorted(t["tag"] for t in tags if t["tag"] not in kept)
        surviving = {t["digest"] for t in tags if t["tag"] in kept}
        orphaned = len({t["digest"] for t in tags if t["tag"] not in kept} - surviving)
        findings: list[Finding] = []
        listed = {t["digest"] for t in tags}
        for digest in sorted(protected_digests - listed):
            findings.append(_f("retention.protected-digest-unlisted", "minor", f"in-use digest {digest[:19]}... has no tag in the listing; the listing may be incomplete", digest))
        total = len(tags)
        limit = _dec(payload["max_delete_pct"])
        if total and Decimal(len(delete)) * 100 > limit * total:
            findings.append(_f("retention.delete-ratio-exceeded", "major", f"plan deletes {len(delete)} of {total} tags, over the {payload['max_delete_pct']}% limit", "delete_tags"))
        if total and not kept:
            findings.append(_f("retention.deletes-everything", "blocker", f"plan deletes all {total} tags: keep_latest, protected prefixes and protected digests keep nothing", "delete_tags"))
        shared = [t["tag"] for t in sorted(tags, key=lambda t: t["tag"]) if t["tag"] not in kept and t["digest"] in surviving]
        if shared:
            findings.append(_f("retention.shared-digest", "minor", f"{len(shared)} tag(s) to delete share a digest with a kept tag (e.g. {shared[0]}); delete by tag, never by digest, or the kept tag is lost", shared[0]))
        return {
            "state": "SUCCESS",
            "tags_total": total,
            "keep_count": len(kept),
            "delete_count": len(delete),
            "delete_pct": round(100 * len(delete) / total, 2) if total else 0.0,
            "delete_tags": delete,
            "keep_reasons": [{"reason": r, "count": sum(1 for v in kept.values() if v == r)} for r in _KEEP_REASONS],
            "orphaned_digests": orphaned,
            "verdict": _verdict(findings),
            **_result(findings),
        }

    return _respond(payload, work)


# ---- attestation bundle -----------------------------------------------------------------------------------------------

# predicateType prefixes of the public attestation formats (in-toto attestation predicates, cosign, OpenVEX, SLSA).
_PREDICATE_KINDS = (
    ("https://slsa.dev/provenance/", "provenance"),
    ("https://slsa.dev/verification_summary/", "vsa"),
    ("https://cyclonedx.org/bom", "sbom"),
    ("https://spdx.dev/Document", "sbom"),
    ("https://openvex.dev/ns", "vex"),
    ("https://cosign.sigstore.dev/attestation/vuln/", "vuln-scan"),
)


def _predicate_kind(predicate_type: str) -> str:
    for prefix, kind in _PREDICATE_KINDS:
        if prefix.endswith("/"):
            if predicate_type.startswith(prefix) and len(predicate_type) > len(prefix):
                return kind
        elif predicate_type == prefix or (predicate_type.startswith(prefix + "/") and len(predicate_type) > len(prefix) + 1):
            return kind  # an unversioned predicate type, or one followed by a path segment: never a longer name such as .../bomX
    return ""


def attestation_bundle(payload: JsonObject) -> JsonObject:
    """Check that every required attestation kind (provenance, sbom, vex, vuln-scan, vsa) exists for the artifact digest
    with a verified signature. Kinds are derived from the predicateType of each attestation."""

    def work() -> JsonObject:
        wanted = _valid_digest(payload["artifact_digest"])
        if wanted is None:
            return _invalid("artifact_digest", "artifact_digest must be sha256:<64 hex>, sha384:<96 hex> or sha512:<128 hex>")
        target = f"{wanted[0]}:{wanted[1]}"
        required = sorted(set(payload["required_kinds"]))
        records = []
        for index, a in enumerate(payload["attestations"]):
            digest = _valid_digest(a["subject_digest"])
            if digest is None:
                return _invalid("attestations", f"attestations[{index}].subject_digest is not a valid OCI digest")
            if a["signature_verified"] and not a["signed"]:
                return _invalid("attestations", f"attestations[{index}]: signature_verified needs signed")
            records.append({"kind": _predicate_kind(a["predicate_type"]), "digest": f"{digest[0]}:{digest[1]}",
                            "signed": a["signed"], "verified": a["signature_verified"], "type": a["predicate_type"]})
        findings: list[Finding] = []
        missing: list[str] = []
        for kind in required:
            of_kind = [r for r in records if r["kind"] == kind]
            bound = [r for r in of_kind if r["digest"] == target]
            if not of_kind:
                findings.append(_f("attestation.missing", "blocker", f"no {kind} attestation was supplied", kind))
            elif not bound:
                findings.append(_f("attestation.digest-mismatch", "blocker", f"{kind} attestations exist but none is bound to the artifact digest", kind))
            elif not any(r["signed"] for r in bound):
                findings.append(_f("attestation.unsigned", "major", f"the {kind} attestation for the artifact is not signed", kind))
            elif not any(r["verified"] for r in bound):
                findings.append(_f("attestation.signature-unverified", "major", f"the signature of the {kind} attestation was not verified", kind))
            else:
                continue
            missing.append(kind)
        unrecognized = [r for r in records if not r["kind"]]
        for r in sorted(unrecognized, key=lambda r: r["type"]):
            findings.append(_f("attestation.unrecognized-type", "info", f"predicateType {_show(r['type'], 100)!r} maps to no known kind", r["type"]))
        return {
            "state": "SUCCESS",
            "required_count": len(required),
            "satisfied_count": len(required) - len(missing),
            "missing_kinds": missing,
            "present_kinds": sorted({r["kind"] for r in records if r["kind"]}),
            "complete": not missing,
            "verdict": _verdict(findings),
            **_result(findings),
        }

    return _respond(payload, work)
