"""Secret redaction for run-log payloads and telemetry strings (stdlib only).

`redact()` returns a redacted COPY; the caller's object is never mutated. Two independent layers:
  * sensitive keys (see `is_sensitive_key`: a name token that IS or ENDS WITH token, secret, password, credential,
    apikey ... (`PGPASSWORD`, `accesstoken`), a whole-word signing/private/cookie/auth(orization)/bearer, a
    `api|access|secret|private|signing|ssh|encryption|master|auth|session|license|client|ed25519|hmac|jwt|handoff|signer`
    + `key|seed` pair (`SWARM_ED25519_KEY`), or a `<db|mysql|pg|...>_pass|pwd` pair; a name ending in `file`/`path`
    (`SWARM_ED25519_KEY_FILE`) names a location, not a secret): every string beneath such a dict key is replaced.
    Numbers, booleans and None stay, so a counter such as `tokens: 1200` survives. A bare `key`, `seed`, `sort_key`,
    `cache_key`, `author` or `authority` is not a credential.
  * text patterns inside any string: GitHub, `sk-`, AWS, Slack and Bearer tokens, `Authorization: Basic`, JWTs, PEM
    private-key blocks, `NAME=value`, `NAME: value`, `--flag value`, `"NAME": "value"` / `'NAME': 'value'` /
    `\\"NAME\\": \\"value\\"` pairs with a sensitive NAME, and URL userinfo (`scheme://user:pass@host`, the password may
    contain `@`).
A benign name never hides a later assignment (`user=alice,password=x`, `--mode a;--password x`): the scan is one forward pass
that steps past a benign separator, while a sensitive name masks its whole comma-bearing value.
Identifiers (task ids, run ids, correlation ids, `sha256:<64 hex>` digests) and ordinary prose never match.

How a string is redacted (the design that keeps passes from interacting): EVERY detector (names, flags, headers, JSON/YAML pairs, URL
userinfo, PEM, token shapes, command lines) reads the ORIGINAL text and only reports `(start, end)` spans of what must disappear;
`redact_spans` unions overlapping or touching spans and `redact_text` replaces each union by ONE `[REDACTED]`. Nothing is rewritten
while detecting, so no detector ever measures a value another detector already shortened, the order of detectors is irrelevant, and a
secret hidden by any one of them is hidden. A detector that needs a boundary scans linearly to the TRUE end of the value: a value is
never cut by a length cap (a cap would leave a readable tail). Only syntax is bounded (a name <= 127 characters, a gap <= 32 blanks);
command lines are read word by word (see "command-line credentials" below), so no argument is ever skipped by length or count. Every
quantifier that can be re-entered from many start positions is bounded, or its failed scans are remembered, so adversarial input of
megabytes is scanned in time linear in its size.
Masking is idempotent on every shared vector; a mask can newly expose a word boundary (`AKIA...--password x`) and let a second pass hide
more, never less.
Best effort, not a guarantee: it covers the carriers above plus the command-line forms `curl -u user:pass`, `mysql -pSECRET`,
`sshpass -p`, `docker login -p`, `redis-cli -a`, `sqlcmd -P`, `-pass pass:X`; vendor tokens (GitHub, Stripe, Google, SendGrid, Slack, npm,
GitLab, DigitalOcean, AWS key ids, ...); `<vendor>_KEY=<long mixed-case value>`; and YAML `- name: X_PASSWORD` / `value:` pairs and
block scalars (`password: |`). Identifier fields are never free text and are not passed through it (see `swarm.runlog.emit`).
Every option form (`NAME=`, `NAME:`, `--flag value`, `-pVALUE`, `-p VALUE`, `-u user:pass`, `"name": "value"` ...) reads its value through ONE
quote-aware reader (`_Reader`): bare (a backslash keeps a blank), `'..'`, `".."`, ANSI-C `$'..'`, locale `$".."`, the escaped form a double quote
takes inside saved JSON evidence (`\\".."`, and `\\\\\\"..` when it was serialised twice), with blanks, tabs, newlines, `;&|`, unicode and escaped
quotes of its own kind inside. The quote context is computed once per string by ONE left-to-right lexer (`_Lexer`: a stack of open quotes,
backslash escapes, nested quotes of the other kind, prose apostrophes such as `it's` never open a quote; rules in the comment above it) and every
detector queries it; nothing is counted locally and no cache is shared between scans, so a later unmatched quote never changes how an earlier
value is read. A quoted value may span lines; a quote that never closes runs to the end of its line. A quoted value is always a value, even
when it holds only blanks or punctuation. The one quote that is NOT a value is a lone quote that the lexer pairs with an earlier quote of
the same kind on its line (`"mysql -p" ok`: a flag in a sentence).
"""
from __future__ import annotations

import functools
import logging
import os
import re
import threading
from array import array
from bisect import bisect_left, bisect_right
from collections.abc import Callable

from ._native_loader import load_native, pure_python_requested

REDACTED = "[REDACTED]"

_SENSITIVE_TOKENS = frozenset({
    "token", "secret", "password", "passwd", "passphrase", "credential", "credentials",
    "signing", "private", "cookie", "cookies", "auth", "oauth", "authorization", "authorisation", "authentication",
    "apikey", "accesskey", "secretkey", "privatekey", "signingkey", "sshkey", "masterkey", "encryptionkey", "bearer",
})
# Plural `tokens`/`secrets`/`passwords` are deliberately absent: they are counts and category labels in this codebase
# (`tokens: 1200`, `max_tokens`, `ux_tokens`, `runs: {"secrets": "pass"}`); the singular names hold credentials. The one
# exception is a plural right after one of `_PLURAL_CONTEXT` (`SUBSTRATE_OPERATOR_TOKENS`, `api_keys`: credential lists).
# `<prefix>_key` / `<prefix>_seed` is a credential only for these prefixes (`SWARM_ED25519_KEY`, `signing_seed`); a bare
# `key`/`seed`, `sort_key`, `cache_key`, `idempotency_key`, `random_seed` ... is an identifier and stays readable.
_KEY_PREFIXES = frozenset({"api", "access", "secret", "private", "signing", "ssh", "encryption", "master", "auth", "account",
                           "session", "license", "licence", "client", "ed25519", "hmac", "jwt", "handoff", "signer"})
_KEY_WORDS = frozenset({"key", "seed"})
_PLURAL_WORDS = frozenset({"tokens", "secrets", "passwords", "keys"})
_PLURAL_CONTEXT = frozenset({"operator", "api", "access", "bearer", "auth", "session"})
# A name whose LAST token is one of these holds a path to the secret, not the secret (`SWARM_ED25519_KEY_FILE`).
_PATH_TOKENS = frozenset({"file", "path"})
# A name token that merely ENDS with one of these is a glued compound (`PGPASSWORD`, `accesstoken`, `clientsecret`,
# `sessionkey`): sensitive too. Plural forms (`tokens`, `secrets`) do not end with them and stay readable.
_SENSITIVE_SUFFIXES = tuple(sorted({
    "password", "passwd", "passphrase", "token", "secret", "credential", "credentials", "apikey", "accesskey",
    "secretkey", "privatekey", "signingkey", "sshkey", "masterkey", "encryptionkey",
} | {p + w for p in _KEY_PREFIXES for w in _KEY_WORDS}))
# `DB_PASS`, `MYSQL_PWD`: `pass`/`pwd` are credentials only right after a service/account word (a bare `PWD` is the cwd,
# `gate_pass` is a verdict).
_PASS_TOKENS = frozenset({"pass", "pwd"})
_PASS_CONTEXT = frozenset({"db", "database", "sql", "mysql", "pg", "postgres", "postgresql", "mongo", "mongodb", "redis",
                           "smtp", "imap", "ftp", "sftp", "ssh", "ldap", "amqp", "rabbitmq", "user", "admin", "root",
                           "login", "mail", "auth", "master"})
# Whole-word names too generic for the prose-prone `name: value` and `--flag value` forms; `=` and JSON pairs keep them.
_WEAK_TOKENS = frozenset({"auth", "oauth", "private", "signing", "cookie", "cookies", "authentication", "authorization",
                          "authorisation", "bearer"})
_Add = Callable[[tuple[int, int]], None]  # the span sink every detector reports into (the spans list's append)

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+")
_NAME_CAP = 256  # names longer than this are not credential names (and are never scanned in full)
_ALNUM = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"  # ASCII only: `str.isalnum` is Unicode-aware, JS `[A-Za-z0-9]` is not


def _name_tokens(name: str) -> list[str]:
    """Lowercase tokens of `name`, split on non-alphanumerics and camelCase (`apiKey` -> api, key)."""
    return [t.lower() for part in _SPLIT_RE.split(name[:_NAME_CAP]) for t in _CAMEL_RE.split(part) if t]


def _sensitive(toks: list[str], *, strict: bool) -> bool:
    if len(toks) > 1 and toks[-1] in _PATH_TOKENS:
        return False
    for i, t in enumerate(toks):
        if t in _SENSITIVE_TOKENS:
            if not (strict and t in _WEAK_TOKENS):
                return True
        elif t.endswith(_SENSITIVE_SUFFIXES):
            return True
        elif i > 0:
            if t in _KEY_WORDS and toks[i - 1] in _KEY_PREFIXES:
                return True
            if t in _PASS_TOKENS and toks[i - 1] in _PASS_CONTEXT:
                return True
            if t in _PLURAL_WORDS and toks[i - 1] in _PLURAL_CONTEXT:
                return True
    return False


def is_sensitive_key(name: object) -> bool:
    """True when `name` looks like it names a credential: a token that is, or ends with, password/passwd/passphrase/
    token/secret/credential(s)/apikey/accesskey/secretkey/privatekey/... (`PGPASSWORD`, `accesstoken`, `github_token`),
    a whole-word signing/private/cookie/auth/authorization, a `key` token directly preceded by one of `_KEY_PREFIXES`
    (api_key, `ACCESS-KEY`, clientKey ...), or `pass`/`pwd` after a service word (DB_PASS, MYSQL_PWD).
    `author`/`authority` never match; neither does a bare `key`, plural `tokens`/`secrets` or `max_tokens`."""
    if not isinstance(name, str):
        return False
    return _sensitive(_name_tokens(name), strict=False)


def _strict_key(name: str) -> bool:
    """Like `is_sensitive_key` minus the generic whole words (auth, private, cookie, ...) that read as plain prose."""
    return _sensitive(_name_tokens(name), strict=True)


_PEM_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-----.*?(?:-----END [A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-----|\Z)",
    re.DOTALL)
_JWT_RE = re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*")
_TOKEN_RES = (
    re.compile(r"(?<![A-Za-z0-9_])(?:gh[oprsu]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Za-z0-9])"),
    re.compile(r"(?<![A-Za-z0-9])(?:xox[baprs]|xapp)-[A-Za-z0-9-]{10,}"),
    # the shapes scripts/sec_gate.py detects (Stripe, Google, GitHub) plus the other prefixed vendor tokens that appear in logs
    re.compile(r"(?<![A-Za-z0-9])(?:(?:[spr]k_live|whsec)_[A-Za-z0-9]{16,}|AIza[A-Za-z0-9_-]{30,}"
               r"|SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}|npm_[A-Za-z0-9]{30,}|glpat-[A-Za-z0-9_-]{16,}"
               r"|dop_v1_[A-Za-z0-9]{32,}|hf_[A-Za-z0-9]{30,}|dckr_pat_[A-Za-z0-9_-]{20,}|shpat_[A-Za-z0-9]{32,}"
               r"|lin_api_[A-Za-z0-9]{30,}|pypi-AgEIcHlwaS5vcmc[A-Za-z0-9_-]{20,})"),
)
# `re.ASCII` on every pattern that uses `\s`, `\b` or IGNORECASE: Python's `\s` / `\b` / case folding are Unicode-aware, JS's (twin in
# omp/src/swarm_bar.ts, which spells the ASCII classes out) are not; a `接続postgres://u:p@h` or a NEL / U+FEFF split must behave alike.
_BEARER_RE = re.compile(r"(?<![A-Za-z0-9])([Bb]earer\s{1,16})[A-Za-z0-9._~+/=-]{16,}", re.ASCII)
# `Authorization: Basic <base64>` (and Proxy-Authorization); a bare "basic <word>" in prose is not touched.
_BASIC_RE = re.compile(r"(?<![A-Za-z0-9])((?:Proxy-)?Authorization[ \t]{0,3}[:=][ \t]{0,3}[\"']?Basic[ \t]{1,16})[A-Za-z0-9+/_-]{4,}={0,2}",
                       re.IGNORECASE | re.ASCII)
# Any other `Cookie:` / `Set-Cookie:` / `Authorization:` header (`Authorization: Token x`, `Cookie: sid=x; theme=dark`): the rest of
# the line is the credential (Bearer/Basic keep their scheme visible, see above). The regex matches the lead only; `_header_end`
# asks the lexer for the TRUE end of the value (line end, or the quote that encloses the header): never a length cap,
# a cap would leave the tail of a long session token readable.
_HEADER_RE = re.compile(r"(?<![A-Za-z0-9_-])((?:Set-|Proxy-)?(?:Cookie|Authorization)[ \t]*:(?![ \t]*(?:Bearer|Basic)\b)[ \t]*)(?=[^\s\"'])",
                        re.IGNORECASE | re.ASCII)
# scheme://user:password@host: the user cannot contain ':' (so the split is unambiguous), the password runs to the LAST
# '@' before the first '/' or whitespace (`postgres://u:p@ss@host/db`). Neither part is length-capped: a scan stops at the next
# '/' (every `://` ends the previous scan), so the scans of different schemes never overlap.
_USERINFO_RE = re.compile(r"((?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]{0,31}://)[^/\s@:]*:[^/\s]*@", re.ASCII)
# ',' is part of an unquoted value (`TOKENS=one,two`): a credential may contain it, and a list of credentials is comma-joined.
# A VALUE is read by ONE quote-aware reader (`_Reader.read`, below) for every option form (NAME=, NAME:, --flag value, -pVALUE, -u user:pass ...);
# these patterns are only its BARE word: a backslash keeps the next blank in the word (`pa\ ss`), but never starts an escaped quote (`\"`).
_NAME_BARE_RE = re.compile(r"""(?:[^\s&;"'\\]|\\[ \t]|\\(?!"))+""", re.ASCII)
# The second lookbehind keeps a `a.b.c.d` / `a-b-c` run from re-starting a 128-char scan at every dot or dash.
# NAME=value / NAME = value (any sensitive name) and NAME: value / NAME : value (strict names only; prose-safe). The regex
# matches only `NAME<sep>`; `_scan_name_values` then consumes the value (`_VALUE_RE`) of a SENSITIVE name and merely steps
# past the separator of a benign one, so a later assignment inside its "value" (`user=alice,password=x`) is still scanned.
_NAME_SEP_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?<![A-Za-z0-9_][.-])([A-Za-z_][A-Za-z0-9_.-]{0,127})([ \t]{0,32}[=:][ \t]{0,32})")
_FLAG_BARE_RE = re.compile(r"""(?:[^\s"'\\]|\\[ \t]|\\(?!"))+""", re.ASCII)
# `--password hunter2`, `-db-password hunter2`: a sensitive flag followed by a separate value that is not another flag. Scanned
# like NAME=value: the regex matches `--flag<ws>` only, a benign flag never hides a later one (`--mode a;--password x`).
_FLAG_SEP_RE = re.compile(r"(?<![A-Za-z0-9_-])(--?[A-Za-z][A-Za-z0-9_-]{0,63})([ \t]{1,8})(?![-\s])", re.ASCII)
_EOL_RE = re.compile(r"[\r\n]")


# ---- the quote lexer ---------------------------------------------------------------------------------------------------------
# The quote/escape context of a string is computed ONCE, by ONE left-to-right lexer (`_Lexer`, twin of `Lexer` in omp/src/swarm_bar.ts), and
# every detector QUERIES it: whether a quote OPENS, CLOSES or is plain CONTENT, and where a quoted value ends, is never re-derived locally
# (no counting of same-kind quotes on a line, no cache shared by scans that restart at earlier positions): every answer is a pure function of
# (text, position). Rules, identical in both implementations:
#   * a MARK is a `"` or `'` with the backslashes before it: an odd run of `k` backslashes makes an ESCAPED mark of level `k` (the form a quote
#     takes inside saved JSON: `\"` level 1, `\\\"` level 3), any other run (none, or an even one) a PLAIN mark (level 0).
#   * the lexer keeps a stack of open quotes. A mark of the same kind and level as an open quote CLOSES it (and ends every quote that was opened
#     after it and never closed: a quote nested in a quote ends before the close of the quote that holds it). A PLAIN mark also ends the
#     escaped quotes of its own kind that are open on top of the stack (`\"x"`: the plain quote is the one that closes the JSON string).
#   * any other mark OPENS a quote, nested in the open ones (`"mysql -p'x' db"`, `'a "b c"'`, `"{\"k\": 1}"`), except a PROSE apostrophe or
#     quote: a plain `'` or `"` right after a letter or digit that is not the `-p` of a flag word (`it's`, `don't`, `users'`, `5"`) never
#     opens a quote and is plain content. (`-p'x'`, `-u'a:b'` and `--pass"x"`: the letters belong to a flag word, the quote opens.)
#   * outside quotes, a `#` that STARTS a word (the start of the text, or right after a blank, a newline, `;`, `&`, `|` or `(`, unless that
#     character is backslash-escaped) begins a COMMENT that runs to the end of its line. A comment is text, but the VALUE of a sensitive name is
#     a value wherever it sits. A quote in comment PROSE is a CONTENT mark: it opens and closes nothing, so a stray apostrophe such as `# '` or
#     `# don't` changes no later quote. The VALUE SPAN of a sensitive name in a comment is read as it is outside one: it starts where `NAME=`,
#     `NAME:` (strict names), `"NAME": ` or `--NAME ` (strict) names a sensitive name (the detectors' own test; a `//` after `NAME:` is a URL
#     scheme), and it is a shell WORD: its marks are read by the rules above, so joined segments (`"a"'b'"c"`) and multi-line quotes are one
#     value, until a blank outside quotes ends the word (a blank that a backslash escapes does not), or a `;` or `&` outside quotes when the
#     value is a `NAME=`, `NAME:` or pair value (a flag value reads through them, as `_FLAG_BARE_RE` does), or the line ends. Two readings
#     of one word (`--password :;` is a flag and also `password :`) make one value, the longer. A quote that nothing closes runs to the end
#     of its line. `a#b`, `$#`, `\#` and a `#` inside an open quote are not comments. "Outside quotes" is the lexer's own view: no quote is
#     open on its stack at that point (an unclosed quote still counts as open).
#   * a comment's quote state is bounded by its LINE (shell semantics): at the newline that ends a comment, every quote opened inside it is
#     closed without a closer and runs to that line's end; the lexer never carries an open quote out of a comment, so the next line is read from
#     a clean state (`# PASSWORD="x"'` then `TOKEN='alpha` and `s3cr3t'` read exactly as `TOKEN='alpha` and `s3cr3t'` with no comment above).
#   * quotes of any kind may span lines. A quote that nothing closes runs to the end of its LINE as a value (a truncated log line): the
#     detectors mask from the value start to the end of that line, never a part of it.
#   * a detector that sees a quote where it expects the start of a value asks the lexer: OPEN gives the value's end, a quote the lexer takes
#     for a CLOSE or for prose is read like an opener of its own kind that nothing in the lexer's view contradicts (the next quote of its
#     kind, else the end of the line): over-redacting, never leaving a suffix of the value readable.
_LEX_RE = re.compile("[\"'#]")
_COMMENT_LEAD = frozenset(" \t\r\n;&|(")
_PROSE_FLAG = "-"
_CONTENT, _OPEN, _CLOSE = 0, 1, 2
_UNCLOSED, _CLOSED, _CUT = 0, 1, 2


def _prose_quote(s: str, p: int) -> bool:
    """Whether the plain quote at s[p] sits right after a letter or digit that is not part of a flag word (`it's`, `users'`, not `-p'x'`)."""
    if p == 0 or s[p - 1] not in _ALNUM:
        return False
    i = p - 1
    while i > 0 and s[i - 1] in _ALNUM:
        i -= 1
    return not (i > 0 and s[i - 1] == _PROSE_FLAG)


def _mate(s: str, p: int, stop: int) -> int:
    """The next PLAIN quote of s[p]'s kind before `stop` (the end of its comment line), or -1: the mate that closes a prose quote's segment."""
    c = s[p]
    q = s.find(c, p + 1, stop)
    while q >= 0:
        r = q
        while r > 0 and s[r - 1] == "\\":
            r -= 1
        if (q - r) % 2 == 0:
            return q
        q = s.find(c, q + 1, stop)
    return -1


def _comment_at(s: str, p: int) -> bool:
    """Whether the `#` at s[p] starts a word, so that it begins a shell comment (`echo x #note`, `#` at the start of a line); not for `a#b`."""
    if p == 0:
        return True
    if s[p - 1] not in _COMMENT_LEAD:
        return False
    r = p - 1  # an escaped blank or joiner (`\ #`, `\;#`) does not start a word
    while r > 0 and s[r - 1] == "\\":
        r -= 1
    return (p - 1 - r) % 2 == 0


_WORD_BREAK_RE = re.compile(r"[ \t;&]")
_BLANK_RE = re.compile(r"[ \t]")


def _value_starts(line: str) -> list[tuple[int, bool]]:
    """(offset, ends at `;`/`&` too) for every value of a SENSITIVE name in `line` (one comment, from its `#`), ascending. The offset is the end
    of `NAME<sep>` (`=` is tested loosely, `:` strictly, as in `_scan`; a `//` after `NAME:` is a URL scheme, not a value), of a
    `"NAME": ` / `\\"NAME\\": ` pair (as in `_pair_spans`), or of `--flag<blank>` (strict). A NAME or pair value stops at `;` and `&` too
    (`_NAME_BARE_RE`); a flag value does not (`_FLAG_BARE_RE`)."""
    out: list[tuple[int, bool]] = []
    for m in _NAME_SEP_RE.finditer(line):
        eq = "=" in m.group(2)
        if not eq and line.startswith("//", m.end()):
            continue
        if _name_hit(m.group(1), eq):
            out.append((m.end(), True))
    for m in _FLAG_SEP_RE.finditer(line):
        if _name_hit(m.group(1), False):
            out.append((m.end(), False))
    for m in _PAIR_RE.finditer(line):
        if is_sensitive_key(m.group("name")):
            out.append((m.end(), True))
    for m in _ESC_PAIR_RE.finditer(line):
        if len(m.group("bs")) % 2 == 1 and is_sensitive_key(m.group("name")):
            out.append((m.end(), True))
    out.sort()
    return out


def _word_break(s: str, a: int, b: int, joiners: bool) -> bool:
    """Whether s[a:b], outside quotes, ends a value word: a blank that no backslash escapes, or (`joiners`) a `;` or `&`."""
    pat = _WORD_BREAK_RE if joiners else _BLANK_RE
    k = a
    while (m := pat.search(s, k, b)) is not None:
        k = m.start()
        if s[k] in ";&" or k == 0 or s[k - 1] != "\\":
            return True
        k += 1
    return False


class _Lexer:
    """Every quote mark of one string with its role: parallel arrays indexed by the mark's number (ascending position). `role` is content,
    open or close; an OPEN mark has `how` (unclosed, closed, or ended by an enclosing close) and `link` (the mark that ended it; an unclosed
    quote that an escaped-quote rule dropped from the stack keeps `how` unclosed but names the mark that dropped it); a CLOSE mark's `link` is
    the mark it closes. `top[m]` (the innermost quote open after mark m, -1 for none), `par[m]` (the quote an OPEN mark is nested in) and
    `lost` (the ascending numbers of the quotes that nothing closes) are derived from those on first use."""
    __slots__ = ("pos", "lvl", "role", "link", "how", "_top", "_par", "_lost", "nx0", "nxl")
    pos: array[int]
    lvl: array[int]
    role: bytearray
    link: array[int]
    how: bytearray
    _top: array[int] | None
    _par: array[int] | None
    _lost: list[int] | None
    nx0: array[int] | None
    nxl: array[int] | None

    def __init__(self, s: str) -> None:
        pos, lvl, link = array("i"), array("i"), array("i")
        role, how = bytearray(), bytearray()
        pos_add, lvl_add, link_add, role_add, how_add = pos.append, lvl.append, link.append, role.append, how.append
        stack: list[int] = []  # the marks of the open quotes ...
        kstack: list[int] = []  # ... and their keys (`level << 1 | is_apostrophe`)
        cnt: dict[int, int] = {}
        i = 0
        skip_to = -1
        starts: list[tuple[int, bool]] = []  # the value spans of the comment in progress: where each starts and whether it also ends at `;`/`&`, ascending
        si = 0
        vact = False  # a value word of that comment is open ...
        vscan = 0  # ... and the text before here is checked for the word's end (no break in it); the text from here on is not yet
        vjoin = True  # whether that word ends at a `;` or `&` as well as at a blank
        vseg = -1  # the mate of a prose quote that opened a quoted segment of that word: the blanks up to it are inside the quotes
        comment_open = False  # a comment began and no mark has come after its line yet: the first mark after that line closes the quotes it opened
        for mm in _LEX_RE.finditer(s):
            p = mm.start()
            if comment_open and p >= skip_to:  # rule (a): the comment's line has ended; no quote it opened outlives that line
                comment_open = False
                for o in stack:
                    link[o] = i  # the mark that ends it; `how` stays UNCLOSED, so the value it opened runs to the end of its line
                stack.clear()
                kstack.clear()
                cnt.clear()
            if s[p] == "#":  # a `#` inside a comment, or inside a quote, is plain text
                if p >= skip_to and not stack and _comment_at(s, p):
                    eol = _EOL_RE.search(s, p)
                    skip_to = eol.start() if eol else len(s)
                    starts = [(p + k, j) for k, j in _value_starts(s[p:skip_to])]
                    si, vact = 0, False
                    comment_open = True
                continue
            r = p
            while r and s[r - 1] == "\\":
                r -= 1
            q1 = s[p] == "'"
            level = (p - r) if (p - r) & 1 else 0
            key = (level << 1) | q1
            pos_add(p)
            lvl_add(level)
            role_add(_CONTENT)
            link_add(-1)
            how_add(_UNCLOSED)
            in_comment = p < skip_to
            if in_comment:  # a quote in a comment is prose unless it lies in the value span of a sensitive name (the rules above)
                while si < len(starts) and starts[si][0] <= p:
                    st, joins = starts[si]
                    if not stack:
                        if vact and st > vscan:  # only the text past the cursor is scanned: every character of the comment is scanned once
                            vact = not _word_break(s, vscan, st, vjoin)
                            vscan = st
                        if vact:  # a second reading of the same word (`--password :;` is also `password :`): the detectors take the union, the longer word
                            vjoin = vjoin and joins
                        else:
                            vact, vscan, vjoin = True, st, joins
                    si += 1
                if vact and not stack and p > vscan:
                    vact = not _word_break(s, vscan, p, vjoin)
                    vscan = p
                if not vact:  # comment prose: a CONTENT mark, it opens and closes nothing
                    i += 1
                    continue
                if not stack and level == 0 and p > vseg and _prose_quote(s, p):
                    # `ab'c d'ef` in a comment: the prose quote opens a quoted segment that its mate on the line closes; the blanks in between are
                    # inside the quotes, so the word goes on past them (the reader's rule for the same word outside a comment)
                    mate = _mate(s, p, skip_to)
                    if mate >= 0:
                        vseg = vscan = mate
            if level == 0:  # a plain quote ends the escaped quotes of its kind on top of the stack
                while kstack:
                    k = kstack[-1]
                    if k < 2 or (k & 1) != q1:
                        break
                    kstack.pop()
                    o = stack.pop()
                    cnt[k] -= 1
                    link[o] = i
                    if cnt.get(key):  # the plain quote closes a quote that holds the escaped one (`"mysql -p\\"x"`); with none it ends nothing
                        how[o] = _CUT
            if cnt.get(key):
                while True:
                    k = kstack.pop()
                    o = stack.pop()
                    cnt[k] -= 1
                    link[o] = i
                    if k == key:
                        how[o] = _CLOSED
                        role[i] = _CLOSE
                        link[i] = o
                        break
                    how[o] = _CUT
            elif level or not _prose_quote(s, p):
                role[i] = _OPEN
                stack.append(i)
                kstack.append(key)
                cnt[key] = cnt.get(key, 0) + 1
            if in_comment:
                vscan = max(vscan, p + 1)
            i += 1
        self.pos, self.lvl, self.role, self.link, self.how = pos, lvl, role, link, how
        self._top = self._par = self._lost = self.nx0 = self.nxl = None

    def _derive(self) -> tuple[array[int], array[int]]:
        """`top` and `par` for every mark (one pass over the roles and links)."""
        n = len(self.pos)
        top, par = array("i", [-1]) * n, array("i", [-1]) * n
        role, link = self.role, self.link
        stack: list[int] = []
        for i in range(n):
            while stack and link[stack[-1]] == i:  # the quotes that end at mark i
                stack.pop()
            if role[i] == _OPEN:
                if stack:
                    par[i] = stack[-1]
                stack.append(i)
            top[i] = stack[-1] if stack else -1
        self._top, self._par = top, par
        return top, par

    @property
    def top(self) -> array[int]:
        top = self._top
        if top is None:
            top, _ = self._derive()
        return top

    @property
    def par(self) -> array[int]:
        par = self._par
        if par is None:
            _, par = self._derive()
        return par

    @property
    def lost(self) -> list[int]:
        if self._lost is None:
            role, how = self.role, self.how
            self._lost = [i for i in range(len(role)) if role[i] == _OPEN and how[i] == _UNCLOSED]
        return self._lost

    def index(self, p: int) -> int:
        """The number of the mark at s[p], or -1."""
        i = bisect_left(self.pos, p)
        return i if i < len(self.pos) and self.pos[i] == p else -1

    def next_marks(self, s: str) -> tuple[array[int], array[int]]:
        """For every mark the next PLAIN mark of its kind, and the next mark of its kind and level (built on first use)."""
        if self.nx0 is None or self.nxl is None:
            n = len(self.pos)
            nx0, nxl = array("i", [-1]) * n, array("i", [-1]) * n
            last0 = [-1, -1]
            lastk: dict[int, int] = {}
            pos, lvl = self.pos, self.lvl
            for i in range(n - 1, -1, -1):
                k = (lvl[i] << 1) | (1 if s[pos[i]] == "'" else 0)
                nx0[i] = last0[k & 1]
                nxl[i] = lastk.get(k, -1)
                if k < 2:
                    last0[k & 1] = i
                else:
                    lastk[k] = i
            self.nx0, self.nxl = nx0, nxl
        return self.nx0, self.nxl


class _Reader:
    """The ONE reader of a value that follows a secret-taking name or option. It starts where the value starts and understands every way a
    shell, a JSON string or a log line quotes it: bare (a backslash keeps a blank, `pa\\ ss`), `'..'`, `".."`, ANSI-C `$'..'`, locale `$".."`,
    and the ESCAPED form a quote takes inside saved JSON evidence (`\\".."`, `\\\\\\"..\\\\\\"` when the JSON was serialised twice). A quoted value
    is whatever the lexer (above) says it is: it runs through its closing quote honouring backslash escapes (a quote preceded by an odd run
    of backslashes is content, as is a blank, a tab, a newline, `;&|`, `#`, unicode ...), across lines; an escaped-form value closes at a
    quote preceded by exactly the opening run and ends before a plain quote (the closing quote of the JSON string that holds it); a quote
    nested in another ends before the quote that closes the outer one; a quote that never closes runs to the end of its line. A quoted value
    always is a value, even when it holds only blanks or punctuation. One instance reads one string and keeps no state that depends on the
    order of the questions: the lexer is immutable once built (lazily, on the first question), the end of a line is a pure memo."""
    __slots__ = ("s", "_lex", "eol_from", "eol_to", "_eols")

    def __init__(self, s: str) -> None:
        self.s = s
        self._lex: _Lexer | None = None
        self.eol_from, self.eol_to = 1, 0
        self._eols: list[int] | None = None

    @property
    def lex(self) -> _Lexer:
        lex = self._lex
        if lex is None:
            lex = self._lex = _Lexer(self.s)
        return lex

    def opener(self, i: int) -> tuple[str, int, int] | None:
        """`(quote, length of the opener, backslashes before the quote)` when a quote opens at s[i] (`"`, `'`, `$'`, `$"`, `\\"`, `$\\"` ...), else None."""
        s = self.s
        c = s[i:i + 1]
        j = i
        if c == "$":
            j += 1
            c = s[j:j + 1]
        if c == '"' or c == "'":
            return c, j + 1 - i, 0
        if c != "\\":
            return None
        r = j
        while s[r:r + 1] == "\\":
            r += 1
        run = r - j
        q = s[r:r + 1]
        if run % 2 == 1 and (q == '"' or q == "'"):
            return q, r + 1 - i, run
        return None

    def line_end(self, i: int) -> int:
        if self.eol_from <= i <= self.eol_to:
            return self.eol_to
        m = _EOL_RE.search(self.s, i)
        end = m.start() if m else len(self.s)
        self.eol_from, self.eol_to = i, end
        return end

    def quoted(self, i: int, q: str, oplen: int, run: int) -> tuple[int, int]:
        """`(end, length of the closing quote)` of the quoted value that opens at s[i]; the closing length is 0 for a value that ends at the end of
        its line or before the quote that closes an enclosing one."""
        lex = self.lex
        m = lex.index(i + oplen - 1)
        if m < 0:
            return self.line_end(i), 0
        if lex.role[m] == _OPEN and lex.lvl[m] == run:
            how = lex.how[m]
            if how == _UNCLOSED:
                return self.line_end(i), 0
            t = lex.link[m]
            if how == _CLOSED:
                return lex.pos[t] + 1, run + 1 if run else 1
            end = lex.pos[t] - lex.lvl[t]
            if run:
                # an escaped quote ends before the plain quote that closes the JSON string holding it (`"mysql -p\\"x"`); any other escaped quote
                # that was cut by an enclosing one is ambiguous (`OPTS='sshpass -p "x'y' z'`): its value runs at least to the end of its line
                return (end if self.s[lex.pos[t]] == q else max(end, self.line_end(i))), 0
            # a plain quote cut by an enclosing close (`echo a 'x` ... `P="x 'y` ... `s3cr3t"`: the stray `'` closes the earlier one) is a value
            # that runs to its matching quote, the next plain quote of its kind, which the fallback below finds; a multi-line value stays whole
        # the lexer takes this quote for a closer or for prose: read it as an opener of its own kind (the next quote that ends it)
        nx0, nxl = lex.next_marks(self.s)
        t0 = nx0[m]
        t1 = nxl[m] if run and lex.lvl[m] == run else -1
        if t1 >= 0 and (t0 < 0 or t1 < t0):
            return lex.pos[t1] + 1, run + 1
        if t0 >= 0:
            return (lex.pos[t0] + 1, 1) if not run else (lex.pos[t0], 0)
        return self.line_end(i), 0

    def read(self, i: int, bare_re: re.Pattern[str]) -> tuple[int, int]:
        """`(end, closing quote length)` of the value at s[i]; `end == i` when there is none (no bare character, or a quote with nothing after it).
        The value is a shell WORD: segments that touch the one just read belong to it (`-p'a b'c`, `-p"x"'y'`, `x=ab'c'`): a quote the lexer opens
        (or one it takes for prose) continues it, a quote it closes ends it (the closing quote of an enclosing string), and after a quoted
        segment a bare character continues it unless it is a closer of prose or Markdown or a joiner (`/`, `?`, `&`, `|`)."""
        s = self.s
        op = self.opener(i)
        if op is not None:
            end, close = self.quoted(i, *op)
            if not (close or end > i + op[1]):
                return i, 0
        else:
            m = bare_re.match(s, i)
            if not m:
                return i, 0
            end, close = m.end(), 0
        quoted = close > 0
        n = len(s)
        lex = None
        nxt = -1  # the number of the mark that follows the last segment, when that segment was a closed quote
        while end < n:
            if nxt >= 0:  # the next quote starts exactly here and the lexer closes it: one step, no re-reading (`\\"a\\"\\"b\\"...`)
                lex = self.lex
                if nxt < len(lex.pos) and lex.pos[nxt] - lex.lvl[nxt] == end and lex.role[nxt] == _OPEN and lex.how[nxt] == _CLOSED:
                    t = lex.link[nxt]
                    end, close, nxt = lex.pos[t] + 1, (lex.lvl[nxt] + 1 if lex.lvl[nxt] else 1), t + 1
                    continue
            c = s[end]
            if c == '"' or c == "'" or c == "\\" or c == "$":
                op = self.opener(end)
                if op is None:
                    m = bare_re.match(s, end)
                    if not m:
                        break
                    end, quoted, nxt = m.end(), False, -1
                    continue
                if lex is None:
                    lex = self.lex
                qpos = end + op[1] - 1
                k = nxt if 0 <= nxt < len(lex.pos) and lex.pos[nxt] == qpos else lex.index(qpos)
                role = lex.role[k] if k >= 0 else _CONTENT
                if role == _CLOSE:
                    break
                if role == _OPEN and lex.lvl[k] == op[2]:
                    if lex.how[k] == _CLOSED:
                        t = lex.link[k]
                        end, close, nxt = lex.pos[t] + 1, op[2] + 1 if op[2] else 1, t + 1
                    else:
                        end, close = self.quoted(end, *op)
                        nxt = -1
                else:  # a quote that is prose (`it's`): part of the word, unless its mate on the line closes a quoted segment (`x'a b'`)
                    end, close = self.prose_end(qpos) if op[2] == 0 else (qpos + 1, 0)
                    nxt = -1
                quoted = True
            elif quoted and c not in _GLUE_STOPS and (m := bare_re.match(s, end)):
                end, quoted, nxt = m.end(), False, -1
            else:
                break
        return end, close

    def prose_end(self, qpos: int) -> tuple[int, int]:
        """`(end, closing quote length)` of a PLAIN quote the lexer takes for prose (`x'a b'`, `ab'c d'ef`): it opens a quoted segment when its mate,
        the next plain quote of its kind, lies on the same line, and the blanks between them are inside the quotes; else it is part of the word,
        `(qpos + 1, 0)`. Outside a comment this and `_mate` (the same rule in a comment's value) read a word the same way."""
        lex = self.lex
        m = lex.index(qpos)
        t = lex.next_marks(self.s)[0][m] if m >= 0 else -1
        if t >= 0 and lex.pos[t] < self.line_end(qpos):
            return lex.pos[t] + 1, 1
        return qpos + 1, 0

    def closes(self, i: int, oplen: int, run: int) -> bool:
        """Whether the quote of the opener at s[i] CLOSES a quote that opened earlier on the same line (`"mysql -p" ok`: a prose quote, not the
        opener of a password). Two things make that reading unsafe, and the quote is then taken for an opener (over-redacting): the lexer pairs
        it with a quote on an earlier line, or a quote that nothing closes follows it on its line (`'x && mysql -p'***' db`: the quoting of the
        line is unbalanced, so the pairing may be an accident of a stray quote)."""
        lex = self.lex
        m = lex.index(i + oplen - 1)
        if m < 0 or lex.role[m] != _CLOSE or lex.lvl[m] != run:
            return False
        eols = self._eols
        if eols is None:
            eols = self._eols = [x.start() for x in _EOL_RE.finditer(self.s)]
        at = lex.pos[m]
        if bisect_left(eols, lex.pos[lex.link[m]]) != bisect_left(eols, at):
            return False
        j = bisect_right(lex.lost, m)
        return not (j < len(lex.lost) and lex.pos[lex.lost[j]] < self.line_end(at))

    def enclosing_end(self, p: int) -> int:
        """Where the quote that encloses s[p] ends (before its closing quote), or -1 when none does or it never closes. A closing quote right
        after `=` is not taken for the close of the enclosing quote (`Cookie: a=1; session="x"`: it opens a cookie value): the next enclosing
        quote out is tried (at most 4 levels, then -1: the caller reads to the end of the line)."""
        lex, s = self.lex, self.s
        m = bisect_left(lex.pos, p) - 1
        o = lex.top[m] if m >= 0 else -1
        for _ in range(4):
            if o < 0 or lex.how[o] == _UNCLOSED:
                return -1
            t = lex.link[o]
            end = lex.pos[t] - lex.lvl[t]
            if end == 0 or s[end - 1] != "=":
                return end
            o = lex.par[o]
        return -1


# "name": "value", 'name': 'value', "name": 'value' (JSON, Python repr, JS).
_PAIR_RE = re.compile(r"""((?P<q>["'])(?P<name>[^"'\\\n]{1,80})(?P=q)[ \t]{0,8}:[ \t]{0,8})(?=["'])""")
# JSON that was serialised again into a string: {\"password\": \"x\"}, and again: {\\\"password\\\": \\\"x\\\"} (the quotes carry an odd run of backslashes)
_ESC_PAIR_RE = re.compile(r"""(?<!\\)(?P<bs>\\{1,31})"(?P<name>[^"'\\\n]{1,80})(?P=bs)"[ \t]{0,8}:[ \t]{0,8}(?=(?P=bs)")""")


@functools.lru_cache(maxsize=4096)
def _name_hit(name: str, eq: bool) -> bool:
    return is_sensitive_key(name) if eq else _strict_key(name)


# A masked value can swallow the NAME of a further sensitive assignment (`password=a,token: b`, `key=a,token = b`,
# `key=a;--password b`) and stop right before that assignment's own value (a quote, or whitespace around the `:`/`=`/flag):
# the value goes on. A quote right after any other value is not taken as a continuation: it is as likely the closing quote
# of an enclosing string.
_TAIL_NAME_RE = re.compile(r"(?<![A-Za-z0-9_])(?<![A-Za-z0-9_][.-])([A-Za-z_][A-Za-z0-9_.-]{0,127})([=:]?)")
_TAIL_FLAG_RE = re.compile(r"(?<![A-Za-z0-9_-])--?[A-Za-z][A-Za-z0-9_-]{0,63}")
_AFTER_NAME_RE = re.compile(r"[ \t]{0,32}([=:])[ \t]{0,32}")
_AFTER_SEP_RE = re.compile(r"[ \t]{0,32}")
_FLAG_GAP_RE = re.compile(r"[ \t]{1,8}(?![-\s])", re.ASCII)
_TAIL_WINDOW = 200
_TAIL_DELIMS = (",", ";", "&", "/", "?")


# `<vendor>_KEY=<long mixed-case value>` (`STRIPE_KEY`, `SUPABASE_SERVICE_ROLE_KEY`): a bare `*_key` name is too common to mask on its
# name alone (`sort_key=name`), so it is masked when the VALUE looks like a credential (>= 20 token-alphabet characters, upper and
# lower case and a digit: not a hash, uuid, path or kebab-case id) and the word before `key` is not a lookup-style qualifier.
_VENDOR_KEY_SKIP = frozenset({"cache", "sort", "idempotency", "partition", "primary", "foreign", "public", "lookup", "group", "shard",
                              "unique", "index", "hash", "object", "route", "routing", "dedupe", "dedup", "lock", "message"})
_LONG_SECRET_RE = re.compile(r"[A-Za-z0-9_+/=~.-]{20,}")
_DIGIT_RE, _LOWER_RE, _UPPER_RE = re.compile("[0-9]"), re.compile("[a-z]"), re.compile("[A-Z]")
# the value of a `*_key` name inside running text: the WHOLE token (never length-capped: a cap leaves the tail readable), optionally
# quoted. `_scan` remembers where a failed check ended, so a long run of `STRIPE_KEY=` repeats is not re-read at every name.
_KEY_VALUE_RE = re.compile(r"""(\\?["']?)([A-Za-z0-9_+/=~.-]{20,})""")


@functools.lru_cache(maxsize=4096)
def _key_name(name: str) -> bool:
    toks = _name_tokens(name)
    return len(toks) > 1 and toks[-1] == "key" and toks[-2] not in _VENDOR_KEY_SKIP


def _long_secret(value: str) -> bool:
    v = value.strip("\"'\\")
    return (_LONG_SECRET_RE.fullmatch(v) is not None and _DIGIT_RE.search(v) is not None
            and _LOWER_RE.search(v) is not None and _UPPER_RE.search(v) is not None)


def _credential_pair(name: object, value: object) -> bool:
    """True when `name` holds a credential: a sensitive name, or a `*_key` name with a credential-shaped string value."""
    return is_sensitive_key(name) or (isinstance(name, str) and isinstance(value, str) and _key_name(name) and _long_secret(value))


def _key_value_end(s: str, pos: int) -> tuple[int, int]:
    """(end of the credential-shaped token and its closing quote that starts at s[pos], or -1; end of the token run that was checked,
    or 0 when there is no run). A failed check fails for every later start inside the same run: a suffix has no more classes."""
    m = _KEY_VALUE_RE.match(s, pos)
    if m is None:
        return -1, 0
    if not _long_secret(m.group(2)):
        return -1, m.end()
    end, q = m.end(), m.group(1)
    return (end + len(q) if q and s.startswith(q, end) else end), m.end()


# ---- command-line credentials ---------------------------------------------------------------------------------------------
# `curl -u user:pass`, `--user user:pass`, `--proxy-user a:b` (a docker `-u 1000:1000` uid:gid pair is not a credential), `mysql -pSECRET`,
# `docker login -p`, `sshpass -p`, `redis-cli -a`, `sqlcmd -P`, `ssh-keygen -N`, `openssl -passin pass:X`.
# A command line is read WORD by word, never through a regex that has to SKIP arguments: a regex needs a bound on the length and the
# number of the arguments it skips, and an argument above the bound (`docker login -u admin:<300 characters> -p pw`) hid the password after
# it. A word ends at a blank (` \t\r\n\f\v`) or at `;&|`; a segment (the arguments of one command) ends at a newline or at `;&|`. An option
# is a word that starts with `-` after any run of delimiters (backtick, quote, `(`, `[`, `{`, `<`, Markdown emphasis ...); its value is the
# next word (or the `=value` / `-pVALUE` attached form), taken whole: a quoted value to its closing quote, an unterminated one to the end
# of its line, any other to the next blank, `;&|` or quote. Nothing is capped, nothing reads text another detector rewrote, and every
# scan moves forward only: a value that failed a check is remembered (`run`, `colon` below), so `x=-u=-u=-u=...` stays linear.
_CLI_BREAKS = "\r\n\f\v;&|"
_CLI_STOPS = " \t" + _CLI_BREAKS
_CLI_WORD_RE = re.compile(r"[^ \t\r\n\f\v;&|]+")
_CLI_BLANKS_RE = re.compile(r"[ \t]*")
_CLI_UNQUOTED_RE = re.compile(r"""(?:[^ \t\r\n\f\v;&|"'\\]|\\[ \t]|\\(?!"))+""")
_CLI_UID_GID_RE = re.compile(r"[0-9]{1,10}:[0-9]{1,10}")
# `-u`/`--user`/`--proxy-user` value, and the openssl `-pass`/`-passin`/`-passout` pass:X form; the option may follow ANY non-name character
# (a backtick, a quote, a bracket, `=`, `_` of Markdown emphasis ...), not a letter, digit or `-`.
_CLI_OPTION_RE = re.compile(r"(?<![A-Za-z0-9-])(?:(?:--user|--proxy-user|-u)(=|(?=[ \t]))|(-pass(?:in|out)?)(?=[ \t]))")
_CLI_CORE_RE = re.compile(r"[A-Za-z0-9_./-]+")
_CLI_NOT_LEAD = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-")
_CLI_NAME_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.-")  # no `_`: no command name has one, `__mysql` is emphasis
_CLI_CLOSERS = frozenset("`\"')]}>*_.,;:")  # Markdown/prose closers: a word `-p` followed by only these is a flag in prose, not `-pPASSWORD`
# the commands whose flags are checked (kind bits), the first words of a two-word command, the flag letter of each kind
_CLI_MYSQL, _CLI_LOGIN, _CLI_REDIS, _CLI_SQLCMD, _CLI_KEYGEN = 1, 2, 4, 8, 16
_CLI_COMMANDS = {
    **dict.fromkeys(("mysql", "mysqladmin", "mysqldump", "mysqlcheck", "mysqlimport", "mysqlshow", "mysqlpump", "mysqlsh", "mariadb",
                     "mariadb-dump", "mariadb-admin"), _CLI_MYSQL),
    **dict.fromkeys(("sshpass", "mongo", "mongosh", "mongodump", "mongorestore", "mongoexport", "mongoimport", "mongostat", "mongotop"), _CLI_LOGIN),
    "redis-cli": _CLI_REDIS, "sqlcmd": _CLI_SQLCMD, "bcp": _CLI_SQLCMD, "ssh-keygen": _CLI_KEYGEN}
_CLI_TWO_WORD = {**dict.fromkeys(("docker", "podman", "buildah", "skopeo", "crane", "oras", "nerdctl", "az"), 1), "helm": 2}  # 1: `login`; 2: `registry login`
_CLI_FLAG_KINDS = {"p": _CLI_MYSQL | _CLI_LOGIN, "a": _CLI_REDIS, "P": _CLI_SQLCMD, "N": _CLI_KEYGEN}
# after a quoted segment a bare character continues the word unless it is a closer of prose or Markdown or one of the joiners (`/`, `?`, `&`, `|`)
# that end a value in URLs, option lists and pipelines (`password='x'?id=1`)
# A value glued to a quoted segment ends at a separator or a closing mark. `_`, `.` and `:` are word characters to the shell
# (`"a"_SECRET` is one word), so they continue the value; `,` and `;` separate options.
_GLUE_STOPS = frozenset("`\"')]}>*,;/?&|")
_CLI_COMMAND_HINTS = ("mysql", "mariadb", "sshpass", "login", "mongo", "redis-cli", "sqlcmd", "bcp", "ssh-keygen")


def _cli_attached(rd: _Reader, at: int, end: int) -> bool:
    """Whether the flag at s[at:at + 2] (`-p`) of the word that ends at `end` carries its value attached: `-pVALUE`, `-p'VALUE'`, `-p$'VALUE'`, `-p\\"VALUE\\"`.
    A quote that opens the value (one that the lexer does not take for the CLOSE of an earlier quote of the line: `"mysql -p" ok` and `"-p"` are prose) makes everything
    after it the value, even when it is only punctuation or blanks (`-p" x"`, `-p\\" x\\"`); an unquoted value is one unless it is made
    only of closers (`-p`, `(-p)`, `**-p**`) that end the word."""
    v = at + 2
    if v >= end:
        return False
    op = rd.opener(v)
    if op is not None:
        if not rd.closes(v, op[1], op[2]):
            return True
        v += op[1]  # the quote CLOSES an earlier one: only what follows it can be a value (`"mysql -p" ok`, `\\"mysql -p\\" ok`)
    return any(ch not in _CLI_CLOSERS for ch in rd.s[v:end])


def _cli_option_spans(s: str, add: _Add, rd: _Reader) -> None:
    """The values of `-u user:pass` (and `--user`, `--proxy-user`, also `=user:pass`) and of `-passin pass:X`."""
    if "-u" not in s and "-pass" not in s:
        return
    n = len(s)
    run_from = run_to = -1  # [run_from, run_to) holds no blank, `;&|` or quote: the unquoted run every start inside it ends at
    colon_from, colon_at = 0, -1  # no ':' in [colon_from, colon_at), a ':' at colon_at (n when there is none)
    pushed_end = -1  # end of the last value reported: a later start (starts only grow) inside that run is covered
    for m in _CLI_OPTION_RE.finditer(s):
        blanks = _CLI_BLANKS_RE.match(s, m.end())  # `[ \t]*` matches at every position
        assert blanks is not None
        start = m.end() if m.group(1) == "=" else blanks.end()
        if start < pushed_end or start >= n or s[start] in _CLI_STOPS:
            continue  # (a start inside a reported run is covered)
        op = rd.opener(start)
        if m.group(2) is not None:  # openssl: `-passin pass:SECRET`, `-passin "pass:SECRET"`, `-passin pass:"a b"`
            if op is not None:
                end, close = rd.quoted(start, *op)
                inner = start + op[1]
                if s.startswith("pass:", inner) and end - close > inner + 5:
                    add((inner + 5, end - close))
            elif s.startswith("pass:", start):
                end = rd.read(start + 5, _CLI_UNQUOTED_RE)[0]
                if end > start + 5:
                    add((start + 5, end))
            continue
        if op is not None:  # the whole `user:pass` is quoted
            end = rd.quoted(start, *op)[0]
            if s.find(":", start + op[1], end) >= 0:
                add((start, end))
                pushed_end = end
            continue
        if not run_from <= start < run_to:
            word = _CLI_UNQUOTED_RE.match(s, start)
            assert word is not None  # the opener check above excludes every quote and quote-escaping backslash, so a word starts here
            run_from, run_to = start, word.end()
        if not colon_from <= start <= colon_at:
            colon_from = start
            colon_at = s.find(":", start)
            if colon_at < 0:
                colon_at = n
        if start >= colon_at or colon_at >= run_to:
            continue  # no user, or no `:` inside the word
        # a user, `:`, a password (both non-empty): bare, or quoted in any form (`curl -u me:'a b'`); a docker uid:gid pair is not a credential
        p = colon_at + 1
        if rd.opener(p) is not None:
            end = rd.read(p, _CLI_UNQUOTED_RE)[0]
        elif p < run_to and not (colon_at - start <= 10 and run_to - colon_at <= 11 and _CLI_UID_GID_RE.fullmatch(s, start, run_to)):
            end = run_to
        else:
            continue
        if end > p:
            add((start, end))
            pushed_end = end


def _cli_command_spans(s: str, add: _Add, rd: _Reader) -> None:
    """The password flag of `mysql -pSECRET`, `docker login -p SECRET`, `sshpass -p`, `redis-cli -a`, `sqlcmd -P`, `ssh-keygen -N`: once
    a command is seen in a segment its flag is checked in EVERY later word of that segment, however long or numerous the arguments."""
    if not any(h in s for h in _CLI_COMMAND_HINTS):
        return
    active = pending = want = prev_end = 0  # command kinds seen in this segment; `login` expected; a flag's value expected
    for m in _CLI_WORD_RE.finditer(s):
        start, end = m.span()
        if (start != prev_end + 1 or s[prev_end] not in " \t") and s[prev_end:start].strip(" \t"):
            active = pending = want = 0  # a newline or `;&|`: the next command
        prev_end = end
        if want:  # the value is taken whole, and its words are still read: a value can hold the next command (`redis-cli -a $(sshpass -p x)`)
            want = 0
            if s[start] != "-":
                value_end = rd.read(start, _CLI_UNQUOTED_RE)[0]
                if value_end > start:
                    add((start, value_end))
        at = start
        while at < end and s[at] not in _CLI_NOT_LEAD:  # `, ", (, [, <, ** ... may precede an option
            at += 1
        if at < end and s[at] == "-":
            pending = 0
            kinds = active & _CLI_FLAG_KINDS.get(s[at + 1:at + 2], 0) if active else 0
            if kinds:
                if _cli_attached(rd, at, end):  # `-pSECRET`: the value touches the flag
                    value_end = rd.read(at + 2, _CLI_UNQUOTED_RE)[0]
                    if value_end > at + 2:
                        add((at + 2, value_end))
                    continue
                if kinds != _CLI_MYSQL:  # a bare `-p` is a prompt for mysql, not for the others: the value is the next word
                    want = 1
        if pending:
            core = _CLI_CORE_RE.search(s, start, end) if end - start <= 24 else None
            if pending == 2:
                pending = 3 if core and core[0] == "registry" else 0
            else:
                if core and core[0] == "login":
                    active |= _CLI_LOGIN
                pending = 0
        base_start = end
        floor = max(start, end - 41)
        while base_start > floor and s[base_start - 1] in _CLI_NAME_CHARS:
            base_start -= 1
        if base_start < end and end - base_start <= 40:  # the command name is the last run of name characters (`/usr/bin/mysql`, `$(mysql`)
            base = s[base_start:end]
            kind = _CLI_COMMANDS.get(base)
            if kind:
                active |= kind
            elif base in _CLI_TWO_WORD:
                pending = _CLI_TWO_WORD[base]


def _cli_spans(s: str, add: _Add, rd: _Reader) -> None:
    _cli_option_spans(s, add, rd)
    _cli_command_spans(s, add, rd)


# ---- YAML: `- name: DB_PASSWORD` / `value: x` pairs (Kubernetes env) and block scalars (`password: |`) ------------------------
_YAML_NAME_RE = re.compile(
    r"""(?<![^\n])[ \t]*(?:-[ \t]+)?name[ \t]*:[ \t]*(["']?)([A-Za-z_][A-Za-z0-9_.-]{0,127})\1[ \t]*(?:#[^\r\n]*)?\r?\n([ \t]*)value[ \t]*:[ \t]*(?=[^ \t\r\n#])""")
_YAML_BLOCK_KEY_RE = re.compile(r"""(?<![^\n])([ \t]*)(?:-[ \t]+)?["']?([A-Za-z_][A-Za-z0-9_.-]{0,127})["']?[ \t]*:[ \t]*(?=[|>])""")
_YAML_INDICATOR_RE = re.compile(r"[|>][+-]?[0-9]?[+-]?[ \t]*(?:#[^\r\n]*)?(?=\r?\n|\Z)")


def _yaml_value_end(s: str, start: int, indent: int) -> int:
    """End of the scalar that begins at s[start]: a block scalar (`|`, `>-`, ...) runs through the following lines indented deeper than
    `indent` (blank lines inside it included); any other value is the rest of its line."""
    ind = _YAML_INDICATOR_RE.match(s, start)
    if ind is None:
        eol = s.find("\n", start)
        eol = len(s) if eol < 0 else eol
        return eol - 1 if s[eol - 1:eol] == "\r" else eol
    end = ind.end()
    pos = s.find("\n", end)
    while pos >= 0:
        ls = pos + 1
        le = s.find("\n", ls)
        le = len(s) if le < 0 else le
        line = s[ls:le]
        if line.strip(" \t\r"):
            if len(line) - len(line.lstrip(" \t")) <= indent:
                break
            end = le - 1 if line.endswith("\r") else le
        pos = le if le < len(s) else -1
    return end


def _yaml_spans(s: str, add: _Add, rd: _Reader) -> None:
    if "\n" not in s or ":" not in s:
        return
    for rx, indent_group in ((_YAML_NAME_RE, 3), (_YAML_BLOCK_KEY_RE, 1)):
        last = 0
        for m in rx.finditer(s):
            if m.start() < last or not is_sensitive_key(m.group(2)):
                continue
            start = m.end()
            end = _yaml_value_end(s, start, len(m.group(indent_group)))
            if s[start] in "\"'":
                end = max(end, rd.quoted(start, s[start], 1, 0)[0])  # a quoted scalar runs to its closing quote, across lines
            if end > start:
                add((start, end))
                last = end


def _continuation(s: str, pos: int, end: int, bare_re: re.Pattern[str], rd: _Reader) -> int:
    """End of the next value segment that still belongs to the secret whose value spans s[pos:end], or -1."""
    c = s[end:end + 1]
    if c != " " and c != "\t" and c != '"' and c != "'" and not s.startswith('\\"', end):
        return -1
    lo = max(pos, end - _TAIL_WINDOW)
    cut = max(s.rfind(d, lo, end) for d in _TAIL_DELIMS)
    if cut >= 0:
        lo = cut + 1
    elif end - pos > _TAIL_WINDOW:
        return -1
    colon = False  # `NAME://...` is a URL, not `NAME: value` (see _scan)
    if (a := _TAIL_NAME_RE.fullmatch(s, lo, end)) is not None:
        if a.group(2):
            if not _name_hit(a.group(1), a.group(2) == "="):
                return -1
            gap, colon = _AFTER_SEP_RE.match(s, end), a.group(2) == ":"
        else:
            gap = _AFTER_NAME_RE.match(s, end)
            if gap is None or not _name_hit(a.group(1), gap.group(1) == "="):
                return -1
            colon = gap.group(1) == ":"
    elif _TAIL_FLAG_RE.fullmatch(s, lo, end) is not None and _name_hit(s[lo:end], False):
        gap = _FLAG_GAP_RE.match(s, end)
        if gap is None:
            return -1
    else:
        return -1
    if gap is None:  # unreachable: every branch above returns when gap is None; this narrows the type for the checker
        return -1
    if colon and s.startswith("//", gap.end()):
        return -1
    value_end = rd.read(gap.end(), bare_re)[0]
    return -1 if value_end == gap.end() else value_end


_NAME_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-")


def _tail_name_start(s: str, floor: int, end: int) -> int:
    """Where the name (and its `=`/`:`) that a value may have swallowed at its end starts: at most 130 characters back, never before `floor`."""
    i = end
    if i > floor and s[i - 1] in "=:":
        i -= 1
    low = max(floor, i - 130)
    while i > low and s[i - 1] in _NAME_CHARS:
        i -= 1
    return i


def _scan(s: str, sep_re: re.Pattern[str], bare_re: re.Pattern[str], eq_sep: bool, add: _Add, rd: _Reader) -> None:
    """Report the value that follows every sensitive `NAME<sep>` match of `sep_re`: one forward pass with a cursor (no
    recursion, no rescans). A benign name only moves the cursor past its separator, so what looks like its value is scanned too."""
    pos = key_fail = 0
    while (m := sep_re.search(s, pos)) is not None:
        pos = m.end()
        if eq_sep and "=" not in m.group(2) and s.startswith("//", pos):
            continue  # `auth://host`, `token://x`: a URL scheme, not `NAME: value` (`PASSWORD=//x` is still a secret)
        name = m.group(1)
        hit = _name_hit(name, eq_sep and "=" in m.group(2))
        if not hit and not _key_name(name):
            continue
        start = pos
        if hit:
            end = rd.read(pos, bare_re)[0]
            if end == pos:
                continue
            if s.startswith(REDACTED, start) and end > start + len(REDACTED) and s[start + len(REDACTED)] not in _ALNUM:
                end = start + len(REDACTED)  # an already-masked value (`[REDACTED],x`) ends where the mask does, like a closing quote
        else:
            if pos < key_fail:
                continue  # inside a token run whose tail was already found not to look like a credential
            end, run_end = _key_value_end(s, pos)
            if end < 0:
                key_fail = max(key_fail, run_end)
                continue
        while (more := _continuation(s, pos, end, bare_re, rd)) >= 0:
            end = more
        add((start, end))
        # a value can end with the NAME of the next assignment (`--api-key = {--password=\"x\"`): scanning resumes where that name starts
        pos = _tail_name_start(s, pos, end)


def _pair_spans(s: str, add: _Add, rd: _Reader) -> None:
    """`"name": "value"` / `'name': 'value'` and the twice-serialised `\\"name\\": \\"value\\"` (any odd number of backslashes before the quotes). The
    value of a credential name is read to its closing quote (to the end of the line when it never closes); a benign pair does not hide a
    pair INSIDE its value (`{"message": "login 'password': 'x'"}`): the scan goes on inside it."""
    pos = 0
    while (m := _PAIR_RE.search(s, pos)) is not None:
        p = m.end()
        end, close = rd.quoted(p, s[p], 1, 0)
        if end - close > p + 1 and _credential_pair(m.group("name"), s[p + 1:end - close]):
            add((p + 1, end - close))
            pos = end
        else:
            pos = p + 1
    pos = 0
    while (m := _ESC_PAIR_RE.search(s, pos)) is not None:
        p = m.end()
        run = len(m.group("bs"))
        pos = p + run + 1
        if run % 2 == 0 or not is_sensitive_key(m.group("name")):
            continue
        end, close = rd.quoted(p, '"', run + 1, run)
        if end - close > p + run + 1:
            add((p + run + 1, end - close))
            pos = end


def _header_end(rd: _Reader, value_start: int) -> int:
    """End of the header value that begins at `value_start` (the first non-blank, non-quote character): the end of its line, or the quote
    that closes the quote it sits in (`-H "Cookie: a=b"`), whichever comes first. A quote inside the value (`a="b c"; d=e`, a stray
    apostrophe) is part of it: the lexer, not a local scan for the next quote, says which quote encloses the header."""
    end = rd.line_end(value_start)
    close = rd.enclosing_end(value_start)
    return close if value_start < close < end else end  # a header that starts at the closing quote of an enclosing one is ambiguous: its line


def _header_spans(s: str, add: _Add, rd: _Reader) -> None:
    pos = 0
    while (m := _HEADER_RE.search(s, pos)) is not None:
        pos = _header_end(rd, m.end())
        add((m.end(), pos))


def _spans(s: str) -> list[tuple[int, int]]:
    """Every `(start, end)` of `s` that must disappear. EVERY detector reads the original text and only reports: nothing is rewritten
    while detecting, so no detector can measure a value another detector has already shortened, and the union below does not depend
    on the order in which they run."""
    spans: list[tuple[int, int]] = []
    add = spans.append
    for m in _PEM_RE.finditer(s):
        add((m.start(), m.end()))
    for rx in (_BEARER_RE, _BASIC_RE):
        for m in rx.finditer(s):
            add((m.end(1), m.end()))
    for m in _USERINFO_RE.finditer(s):
        add((m.end(1), m.end() - 1))  # the '@' stays
    rd = _Reader(s)
    _header_spans(s, add, rd)
    _cli_spans(s, add, rd)
    _yaml_spans(s, add, rd)
    _scan(s, _NAME_SEP_RE, _NAME_BARE_RE, True, add, rd)
    _scan(s, _FLAG_SEP_RE, _FLAG_BARE_RE, False, add, rd)
    for rx in (_JWT_RE, *_TOKEN_RES):
        for m in rx.finditer(s):
            add((m.start(), m.end()))
    _pair_spans(s, add, rd)
    return spans


# Opt-in Rust span scanner (`swarm._native.redact_spans`, crate gsd-redact). The pure-Python detectors above are the oracle and the
# default; `SWARM_REDACT_ENGINE=rust` only swaps who computes the spans, and every failure of the Rust path falls back to Python for that
# call: redaction never fails because of a configuration typo, a missing or stale extension, or a scanner error.
_ENGINE_ENV = "SWARM_REDACT_ENGINE"
_engine_log = logging.getLogger("swarm.redact")
# Reasons already reported this process. The two extension reasons double as a negative cache (no import retry per call); tests reset
# it with `monkeypatch.setattr(redact, "_ENGINE_WARNED", set())`.
_ENGINE_WARNED: set[str] = set()
_engine_warn_lock = threading.Lock()


def _engine_warn_once(reason: str, message: str) -> None:
    with _engine_warn_lock:
        if reason in _ENGINE_WARNED:
            return
        _ENGINE_WARNED.add(reason)
    _engine_log.warning("%s: %s; using the pure-Python redactor", _ENGINE_ENV, message)


def _rust_engine_selected() -> bool:
    """True when `SWARM_REDACT_ENGINE=rust` is set and not overridden. Unset, empty or `python` is the default; `SWARM_PURE_PYTHON=1`
    always wins over `rust`; any other value warns once and selects Python."""
    engine = os.environ.get(_ENGINE_ENV, "")
    if engine in ("", "python"):
        return False
    if engine != "rust":
        _engine_warn_once("unknown-engine", f"unknown value {engine!r} (expected 'python' or 'rust')")
        return False
    if pure_python_requested():
        _engine_warn_once("pure-python", "'rust' requested but SWARM_PURE_PYTHON=1 is set")
        return False
    return True


def _native_spans(s: str) -> list[tuple[int, int]] | None:
    """The spans from the Rust scanner, or None when this call must use the Python path."""
    if "missing-extension" in _ENGINE_WARNED or "stale-extension" in _ENGINE_WARNED:
        return None
    try:
        native = load_native()
    except ImportError as exc:
        _engine_warn_once("missing-extension", f"swarm._native is unavailable ({exc})")
        return None
    fn = getattr(native, "redact_spans", None)
    if fn is None:
        _engine_warn_once("stale-extension", "swarm._native has no redact_spans (stale build: reinstall with `pip install .`)")
        return None
    try:
        return [(start, end) for start, end in fn(s)]
    except UnicodeEncodeError:
        return None  # a lone surrogate cannot be encoded for Rust: a normal input, answered by Python without a warning
    except Exception as exc:  # noqa: BLE001 - any native failure must degrade to the oracle, never propagate
        _engine_warn_once("native-error", f"swarm._native.redact_spans failed ({type(exc).__name__}: {str(exc)[:200]})")
        return None


def redact_spans(s: str) -> list[tuple[int, int]]:
    """The disjoint, ascending `(start, end)` ranges of `s` that `redact_text` replaces: the union of every detector's spans, where
    overlapping or touching spans become one range."""
    if not isinstance(s, str) or len(s) < 6:
        return []
    if _rust_engine_selected():
        native_spans = _native_spans(s)
        if native_spans is not None:
            return native_spans
    spans = [sp for sp in _spans(s) if sp[0] < sp[1]]  # an empty span hides nothing (the TypeScript union drops it too)
    if not spans:
        return []
    spans.sort()
    merged: list[tuple[int, int]] = []
    cur_start, cur_end = spans[0]
    for start, end in spans:
        if start > cur_end:
            merged.append((cur_start, cur_end))
            cur_start, cur_end = start, end
        elif end > cur_end:
            cur_end = end
    merged.append((cur_start, cur_end))
    return merged


def redact_text(s: str) -> str:
    """Replace every secret-looking span of `s` with `[REDACTED]`; everything else is returned unchanged. Overlapping or touching
    spans of different detectors become ONE `[REDACTED]`."""
    merged = redact_spans(s)
    if not merged:
        return s
    out: list[str] = []
    last = 0
    for start, end in merged:
        out.append(s[last:start])
        out.append(REDACTED)
        last = end
    out.append(s[last:])
    return "".join(out)


_BARE_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")


def _bare_credential_key(name: object) -> bool:
    """Whether `name` is a credential name as a whole (`api_key`), not a key redacted for a token inside its text (`Authorization: Bearer x`)."""
    return isinstance(name, str) and _BARE_NAME_RE.fullmatch(name) is not None and is_sensitive_key(name)


def _dict_value(name: object, value: object, level: int, max_depth: int, sensitive: bool) -> object:
    """A dict entry's value. A number or bool is a credential only when its key is a credential name as a whole (`{"api_key": 4821937}`),
    so a key redacted only because it embeds a token (`"Authorization: Bearer x": 1`) keeps its number or bool; None stays None.
    Strings and containers go through `_walk`, so list items keep its rules."""
    credential = sensitive or _credential_pair(name, value)
    if isinstance(value, (int, float)) and (sensitive or _bare_credential_key(name)):
        return REDACTED
    return _walk(value, level + 1, max_depth, credential)


def _walk(value: object, level: int, max_depth: int, sensitive: bool) -> object:
    if isinstance(value, str):
        return REDACTED if sensitive and value else redact_text(value)
    if isinstance(value, (dict, list, tuple)):
        if level > max_depth:
            return REDACTED  # depth cap: never recurse unbounded (cycles included), never pass an unexamined subtree
        if isinstance(value, dict):
            return {(redact_text(k) if isinstance(k, str) else k): _dict_value(k, v, level, max_depth, sensitive)
                    for k, v in value.items()}
        return [_walk(v, level + 1, max_depth, sensitive) for v in value]
    return value


def redact(value: object, *, max_depth: int = 12) -> object:
    """A redacted copy of `value` (dict/list/tuple/str recursed, tuples become lists; a number or bool that is a dict value under a credential name becomes `[REDACTED]`; other scalars unchanged).
    Containers nested more than `max_depth` levels deep are replaced by `[REDACTED]`."""
    return _walk(value, 1, max_depth, False)
