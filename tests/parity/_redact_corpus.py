"""The deterministic corpus of the Rust-vs-Python redaction span parity test (Phase 14.1.1).

`build_corpus()` returns `(source_label, text)` pairs: every vector the Python redaction suite and the shared TypeScript fixtures hold, the
generators of `tests/test_redact.py`, a hand-written set of edge inputs and a small-scale family of adversarial floods. Nothing here is a
secret-shaped literal: the fixtures carry `{{tok:KIND:INDEX}}` placeholders, and this module expands them in memory with the helpers of
`tests/test_redact.py` (loaded from its file path and never copied, so the corpus cannot drift from the suite). Do not write an expanded
corpus into a committed file (GitHub push protection flags secret-shaped literals); `.planning/research/redact-oracle/dump_oracle.py`
writes dumps under /tmp only.

The corpus is a pure function of the repository files: same checkout, same list, same order. Each text is at most `MAX_TEXT_CHARS`
characters (a longer one is dropped and counted in `Corpus.dropped`) and appears once, under the label of the source that produced it
first, so a generator that re-draws a fixture case adds only what the fixture lacks.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
TESTS = REPO / "tests"
CASES_FIXTURE = TESTS / "fixtures" / "redact_cases.json"
GENERATED_FIXTURE = TESTS / "fixtures" / "redact_generated.json"

MAX_TEXT_CHARS = 16 * 1024

# The labels `build_corpus` emits, in emission order (a label whose texts were all produced by an earlier source has no entry).
SOURCES: tuple[str, ...] = (
    "fixture.cases",
    "fixture.generated.cases",
    "fixture.generated.wild",
    "fixture.generated.quoting",
    "fixture.generated.property",
    "fixture.generated.noise",
    "fixture.generated.random",
    "fixture.generated.tokens",
    "fixture.property-templates",
    "fixture.expected",
    "gen.option",
    "gen.wild",
    "gen.quoting",
    "gen.property",
    "gen.noise",
    "gen.random",
    "edge",
    "flood",
)

# Extra seeds of the generators (the fixtures pin the default seeds). Chosen once; changing one changes the corpus.
_OPTION_SEEDS = (1, 2, 3, 7)
_WILD_SEED = 11
_PROPERTY_EXTRA_SEED, _PROPERTY_EXTRA_COUNT = 77, 1500
_NOISE_EXTRA_SEEDS = ((31, 1200), (32, 1200))
_RANDOM_EXTRA_SEED, _RANDOM_EXTRA_COUNT = 20241099, 20000


@lru_cache(maxsize=1)
def helpers() -> Any:
    """`tests/test_redact.py` as a module (`expand_tokens`, the `generate_*` functions, the xorshift generator). It is imported by file
    path under a private name, so it neither shadows nor re-runs the copy pytest collects. The checkout root must be importable."""
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    spec = importlib.util.spec_from_file_location("_redact_suite_helpers", TESTS / "test_redact.py")
    if spec is None or spec.loader is None:
        raise ImportError("cannot load tests/test_redact.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[spec.name]
        raise
    return module


@dataclass
class Corpus:
    """The pairs in emission order, how many each source contributed, and how many texts were dropped as too long."""

    pairs: list[tuple[str, str]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    dropped: dict[str, int] = field(default_factory=dict)
    _seen: set[str] = field(default_factory=set, repr=False)

    def add(self, source: str, text: str) -> None:
        """Append `text` under `source` unless it is a duplicate or longer than `MAX_TEXT_CHARS`."""
        if len(text) > MAX_TEXT_CHARS:
            self.dropped[source] = self.dropped.get(source, 0) + 1
            return
        if text in self._seen:
            return
        self._seen.add(text)
        self.pairs.append((source, text))
        self.counts[source] = self.counts.get(source, 0) + 1


def _load(path: Path) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return document


def build_corpus() -> list[tuple[str, str]]:
    """The corpus as `(source_label, text)` pairs."""
    return build_corpus_report().pairs


def build_corpus_report() -> Corpus:
    """The corpus with its per-source counts and drop counts."""
    h = helpers()
    corpus = Corpus()
    shared = _load(CASES_FIXTURE)
    generated = _load(GENERATED_FIXTURE)
    expand: Callable[[str], str] = h.expand_tokens
    expected_outputs: list[str] = []

    # redact_cases.json: `input` is one string or a list of pieces, joined (and token-expanded) at test time
    for case in shared["cases"]:
        corpus.add("fixture.cases", _join(h, case["input"]))
        if "expected" in case:
            expected_outputs.append(case["expected"])
        expected_outputs.extend(_join(h, needle) for needle in case.get("must_contain", ()))

    # redact_generated.json
    for name in ("cases", "wild", "quoting"):
        for case in generated[name]:
            corpus.add(f"fixture.generated.{name}", expand(case["input"]))
            expected_outputs.append(case["expected"])
    for name in ("property", "noise"):
        for case in generated[name]:
            corpus.add(f"fixture.generated.{name}", expand(case["input"]))
    corpus_random = generated["random"]
    if list(h._RANDOM_FRAGMENTS) != corpus_random["fragments"]:
        raise RuntimeError("tests/test_redact.py _RANDOM_FRAGMENTS differ from tests/fixtures/redact_generated.json: regenerate the fixture")
    random_inputs: list[str] = h.generate_random_strings(corpus_random["seed"], corpus_random["count"])
    if hashlib.sha256("\u0000".join(random_inputs).encode("utf-8")).hexdigest()[:16] != corpus_random["inputs"]:
        raise RuntimeError("the seeded random strings differ from the digest in tests/fixtures/redact_generated.json")
    for text in random_inputs:
        corpus.add("fixture.generated.random", text)
    for text in _golden_token_texts(h, generated["tokens"]):
        corpus.add("fixture.generated.tokens", text)

    # the seeded `property*` snippet sections of redact_cases.json
    for key in sorted(k for k in shared if k.startswith("property")):
        for snippet in h._property_snippets(shared[key]):
            corpus.add("fixture.property-templates", expand(snippet))

    # masked outputs: `[REDACTED]` inside text must itself scan identically
    for text in expected_outputs:
        corpus.add("fixture.expected", text)

    # the generators of tests/test_redact.py, beyond what the fixtures already hold
    for case in h.generate_option_cases(h._GENERATED_SEED, h._GENERATED_COUNT):
        corpus.add("gen.option", case["input"])
    for seed in _OPTION_SEEDS:
        for case in h.generate_option_cases(seed, 500):
            corpus.add("gen.option", case["input"])
    for case in h.generate_wild_cases(h._WILD_SEED, h._WILD_COUNT) + h.generate_wild_cases(_WILD_SEED, 3000):
        corpus.add("gen.wild", case["input"])
    for case in h.generate_quoting_cases():
        corpus.add("gen.quoting", case["input"])
    for case in h.generate_property_cases(h._PROPERTY_SEED, h._PROPERTY_EXTRA) + h.generate_property_cases(_PROPERTY_EXTRA_SEED, _PROPERTY_EXTRA_COUNT):
        corpus.add("gen.property", expand(case["input"]))
    for case in h.generate_noise_cases(h._NOISE_SEED, h._NOISE_COUNT):
        corpus.add("gen.noise", expand(case["input"]))
    for seed, count in _NOISE_EXTRA_SEEDS:
        for case in h.generate_noise_cases(seed, count):
            corpus.add("gen.noise", expand(case["input"]))
    for text in h.generate_random_strings(_RANDOM_EXTRA_SEED, _RANDOM_EXTRA_COUNT):
        corpus.add("gen.random", text)

    for text in _edge_texts(expand):
        corpus.add("edge", text)
    for text in _flood_texts(expand):
        corpus.add("flood", text)
    return corpus


def _join(h: Any, value: str | list[str]) -> str:
    text: str = h.expand_tokens(value if isinstance(value, str) else "".join(value))
    return text


def _golden_token_texts(h: Any, golden: list[list[str]]) -> Iterator[str]:
    """Every golden token of the fixture, expanded (its digest checked), bare and in four carriers."""
    for ref, digest in golden:
        token: str = h.expand_tokens(ref)
        if hashlib.sha256(token.encode("utf-8")).hexdigest()[:16] != digest:
            raise RuntimeError(f"token {ref} no longer expands to the digest pinned in tests/fixtures/redact_generated.json")
        yield token
        yield f"see {token} here"
        yield f"api_token={token}"
        yield f"Authorization: Bearer {token}"
        yield f"{{\"x\": \"{token}\", \"n\": 1}}"


# ---- hand-written edge inputs ------------------------------------------------------------------------------------------------------


def _edge_texts(expand: Callable[[str], str]) -> Iterator[str]:
    ghp = expand("{{tok:ghp:7:40}}")
    sk = expand("{{tok:sk:7:51}}")
    jwt = expand("{{tok:jwt:7:80}}")
    akia = expand("{{tok:aws_key:7}}")
    plain = expand("{{tok:plain:7:24}}")

    # empty, shorter than 6 characters (`redact_spans` answers [] below 6), and the first 6-character inputs
    yield from ("", " ", "a", "ab", "abc", "abcd", "abcde", "abcdef", "\n", "\r\n", "\x00", "\x00" * 6, "=", "==", "a=b", "pw=x", "-p", "-p ab", "a:b@",
                "token", "token=", "token=1", "tokens", "pass=x", "-pab", "u:p@h", "\"\"", "'", "''", "#", "# a", "ab\ud800")

    # lone surrogates cannot cross into Rust (UnicodeEncodeError): the native leg reports them `unencodable`
    for lone in ("\ud800", "\udbff", "\udc00", "\udfff"):
        yield from (lone, lone * 3, "\ud83d\ude00" if lone == "\ud800" else lone + lone, f"password={lone}abc", f"password=abc{lone}def",
                    f"x {lone} password=hunter2 {lone}", f"token={ghp}{lone}", f"{lone}{ghp}", f"{ghp[:20]}{lone}{ghp[20:]}", f'"password": "{lone}"',
                    f"Authorization: Bearer {jwt}{lone}", f"-----BEGIN PRIVATE KEY-----\n{lone}\n-----END PRIVATE KEY-----", f"curl -u me:{lone}x https://h",
                    f"mysql -p{lone}", f"{lone}password=hunter2", f"password=hunter2{lone}", f"it's {lone} don't", f"# {lone}\npassword=hunter2")

    # astral code points before and inside secrets: offsets are code points, not UTF-16 units
    emoji, math_a, last = "\U0001f600", "\U0001d538", "\U0010ffff"
    for lead in ("", emoji, emoji * 5, emoji * 40, math_a + emoji, "\uffff", "\ufffe\uffff", last):
        yield from (f"{lead}password=hunter2", f"{lead} token={ghp}", f"{lead}{ghp}", f"{lead}{sk} x", f"{lead}Bearer {jwt}", f"{lead}{akia}",
                    f'{lead}{{"password": "a {emoji} b"}}', f"{lead}\npassword=\"{emoji * 3}\"", f"{lead}mysql -u root -p{emoji}x db",
                    f"{lead}curl -u me:{emoji}{plain} https://h", f"{lead}Cookie: sid={emoji}abc; theme=dark", f"{lead}postgres://u:p{emoji}w@h/db")
    yield from (f"password={emoji}", f"password={emoji * 30}", f"token={ghp}{emoji}", f"{ghp[:10]}{emoji}{ghp[10:]}", f"api_key: {emoji}{emoji}x",
                f"{emoji}=hunter2 password=x", f"--password {emoji} next", f"-----BEGIN PRIVATE KEY-----\n{emoji}\n-----END PRIVATE KEY-----", f"{math_a}token=x",
                f"x{emoji}token=abc12345", f"{emoji}.password=x", f"{emoji}-password=x")

    # unicode blanks and line terminators that Python's `\s` (str) and `re.ASCII` treat differently
    for ws in ("\u00a0", "\u2028", "\u2029", "\u0085", "\u3000", "\u2003", "\u200b", "\ufeff", "\x0b", "\x0c", "\x1c", "\x1f", "\u180e", "\ufe0f"):
        yield from (f"password={ws}hunter2", f"password:{ws}hunter2", f"password{ws}=hunter2", f"--password{ws}hunter2", f"Authorization:{ws}Bearer {jwt}",
                    f"Bearer{ws}{sk}", f"a{ws}=b{ws}token={ghp}{ws}end", f"Cookie:{ws}sid=abc1234567890", f"mysql -u root{ws}-pxxxxxxxxx",
                    f"- name: DB_PASSWORD\n  value:{ws}abc", f"line1{ws}line2 secret=abc123def", f"password=ab{ws}cd ef", f'password="ab{ws}cd" ef',
                    f"curl -u{ws}me:{plain} https://h", f"# note{ws}PASSWORD=a'b c'", f"secret: |\n  a{ws}b\n  c\nnext: 1", f"x{ws}-p{plain}",
                    f"{ws}password=hunter2{ws}", f"token: abc{ws}def{ws}ghi", f"sshpass -p{ws}{plain} ssh h", f"postgres://u:p{ws}w@h/db")

    # names around every length cap of the scanners (name cap 256; pair names 80; flags 64; tail and YAML names 128; userinfo scheme 32)
    for n in (1, 2, 3, 31, 32, 33, 62, 63, 64, 65, 66, 78, 79, 80, 81, 82, 126, 127, 128, 129, 130, 200, 254, 255, 256, 257, 258, 300, 500):
        name = "a" * n
        yield from (f"{name}=x", f"{name}: value", f"{name}.{name}=x", f"--{name} value", f'"{name}": "v"', f"'{name}': 'v'", f'\\"{name}\\": \\"v\\"',
                    f"{name}_password=hunter2", f"password_{name}=hunter2", f"{name}token=abc12345", f"{name}_key={'v' * 30}", f"- name: {name}_token\n  value: abc",
                    f"{name}: |\n  abc\n  def\nnext: 1", f"{'a' * max(1, n - 8)}password=hunter2", f"token{name}=hunter2",
                    f"{name}://u:p@h/x", f"{name}://:{plain}@h/x", f"{name}-p value", f"{name} -u me:{plain}", f"Cookie: {name}=abc", f"x={name}", f"password={name}",
                    f"password={name},{name}", f"password='{name}'", f'--password "{name}"', f"mysql -p{name}", f"Bearer {name}", f"{name}\npassword=hunter2")
    # a name that is exactly at, one below and one above the token-split cap, ending in a credential word
    for n in (248, 249, 250, 255, 256, 257):
        yield from (f"{'x' * (n - 8)}_password=v", f"{'x' * (n - 5)}token=v", f"{'x' * (n - 6)}Secret=v", f"{'X' * (n - 9)}_PASSWORD=v")
    # sensitive names glued, camel, kebab, dotted, plural and path-like in text
    for name in ("PGPASSWORD", "accesstoken", "authToken", "clientSecret", "github-token", "api.key", "apiKeys", "API_KEYS", "password_file", "key_path",
                 "SSH_KEY", "ssh_key_file", "auth", "author", "authority", "monkey", "keys", "tokens", "max_tokens", "DB_PASS", "MYSQL_PWD", "pass", "STRIPE_KEY",
                 "cache_key", "idempotency_key", "sort_key", "AWS_SECRET_ACCESS_KEY", "SWARM_ED25519_KEY", "ed25519.seed", "x-api-key", "X_API_KEY"):
        yield from (f"{name}=hunter2", f"{name}: hunter2", f'"{name}": "hunter2"', f"--{name} hunter2", f"export {name}={plain}", f"{{'{name}': '{plain}'}}")

    # backslash runs of every length around the 31-run bound of the escaped-pair pattern, in every position of an escaped pair
    for n in (1, 2, 3, 5, 15, 16, 17, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 63, 64, 65):
        bs = "\\" * n
        yield from (f'{bs}"password{bs}": {bs}"hunter2{bs}"', f'{bs}"password{bs}": {bs}"hunter2{bs}" , "ok": 1', f'{bs}"api_token{bs}":{bs}"{plain}{bs}"',
                    f'DB_PASSWORD={bs}"abc def{bs}" tail', f'DB_PASSWORD={bs}"{bs}"', f'x{bs}"', f'"a": "{bs}', f'"a": "b{bs}"', f'--password {bs}"abc', f"--password {bs}abc",
                    f"a{bs} b", f"{bs}\npassword=hunter2", f"password={bs}", f"password=a{bs}b c", f'password="{bs}"', f"password='{bs}'", f'mysql -p{bs}"abc def{bs}" db',
                    f'sshpass -p {bs}"{plain}{bs}" ssh h', f'curl -u me:{bs}"a b{bs}"', f'{bs}"password": {bs}"x', f'"password": "{bs}"{bs}"', f"password:{bs}\nsecret=x")

    # CRLF and bare CR line ends
    yield from ("password=abc\r\nuser=x", "a\rpassword=x", "Cookie: sid=abc\r\nHost: h\r\n\r\n", "token: abc\r\n", "- name: DB_PASSWORD\r\n  value: abc\r\n",
                "secret: |\r\n  abc\r\n  def\r\nnext: 1", "# comment 'x\r\nPASSWORD='abc'", "\r" * 10 + "password=x", 'password="abc\r\ndef"', "mysql -u root\r\n-pxx",
                "key: >-\r\n  abc\r\nnext: 1", "token:\r\n  abc", "password=abc\r", "password=\r\nabc", f"Authorization: Bearer\r\n{jwt}", "-----BEGIN PRIVATE KEY-----\r\nabc\r\n-----END PRIVATE KEY-----\r\n",
                "docker login -u me\r\n-p abc", "redis-cli -a\r\nabc ping", "a=b\r\nPASSWORD=\r\n", "\r\n\r\npassword=hunter2\r\n\r\n", "Set-Cookie: a=b\r\n c=d\r\n")

    # the boundaries of every token shape: one character below, at and above the minimum, and the characters that may glue to it
    for n in (18, 19, 20, 21, 22, 35, 36, 37):
        yield from (f"ghp_{'a' * n}", f"x ghp_{'a' * n} y", f"_ghp_{'a' * n}", f"1ghp_{'a' * n}", f"github_pat_{'a' * n}", f"gho_{'A1' * (n // 2)}")
    for n in (14, 15, 16, 17, 18):
        yield from (f"sk-{'a' * n}", f"asksk-{'a' * n}", f"-sk-{'a' * n}", f"sk-{'a-' * (n // 2)}", f"sk_live_{'a' * n}", f"whsec_{'a' * n}", f"rk_live_{'a' * n}")
    for n in (14, 15, 16, 17, 18):
        yield from (f"AKIA{'A' * n}", f"AKIA{'A' * n}A", f"AKIA{'A' * n}a", f"AKIA{'A' * n}_", f"xAKIA{'A' * n}", f"ASIA{'0' * n}", f"AKIA{'a' * n}")
    for n in (8, 9, 10, 11, 12):
        yield from (f"xoxb-{'1' * n}", f"xoxa-{'1-' * (n // 2)}", f"xapp-{'1' * n}", f"xoxz-{'1' * n}")
    for n in (28, 29, 30, 31, 32):
        yield from (f"AIza{'a' * n}", f"AIza{'a-_' * (n // 3)}")
    for a, b, c in ((4, 5, 5), (5, 4, 5), (5, 5, 0), (5, 5, 1), (6, 6, 20), (5, 5, 5)):
        yield from (f"eyJ{'a' * a}.{'b' * b}.{'c' * c}", f"_eyJ{'a' * a}.{'b' * b}.{'c' * c}", f"-eyJ{'a' * a}.{'b' * b}.{'c' * c}", f"x eyJ{'a' * a}.{'b' * b}.{'c' * c}.")
    for gap in (1, 2, 15, 16, 17, 18):
        for n in (15, 16, 17):
            yield from (f"Bearer{' ' * gap}{'a' * n}", f"bearer{chr(9) * gap}{'a' * n}", f"Authorization: Bearer{' ' * gap}{'a' * n}", f"xBearer {'a' * n}",
                        f"Basic{' ' * gap}{'a' * n}", f"Authorization: Basic{' ' * gap}{'Zm9v' * (n // 4 + 1)}==", f"Authorization:Basic{' ' * gap}ab==",
                        f"Proxy-Authorization = \"Basic{' ' * gap}{'a' * n}")
    for label in ("", "RSA ", "EC ", "OPENSSH ", "ENCRYPTED ", "A" * 40 + " ", "A" * 41 + " ", "a b ", "0 9 "):
        yield from (f"-----BEGIN {label}PRIVATE KEY-----\nabc\n-----END {label}PRIVATE KEY-----", f"-----BEGIN {label}PRIVATE KEY-----\nabc", f"-----BEGIN {label}PRIVATE KEY BLOCK-----\nabc\n-----END {label}PRIVATE KEY BLOCK-----",
                    f"x-----BEGIN {label}PRIVATE KEY-----\n-----END {label}PRIVATE KEY-----x", f"-----BEGIN {label}PRIVATE KEY-----", f"-----END {label}PRIVATE KEY-----")
    for scheme in (1, 2, 31, 32, 33):
        yield from (f"{'a' * scheme}://user:{plain}@h/p", f"1{'a' * scheme}://user:{plain}@h/p", f"{'a' * scheme}+x.y-z://u:pw@h", f"{'a' * scheme}://user:p@w@h/p", f"{'a' * scheme}://u@h:80/p",
                    f"{'a' * scheme}://:@h", f"{'a' * scheme}://u:\n@h", f"{'a' * scheme}://u:p/q@h", f"{'a' * scheme}://u:p w@h")

    # quote noise and comments around a sensitive name
    for text in ("# 'x\nPASSWORD='abc def'", "x#'y\npassword=abc", "a#b 'x' y\n# 'z\n'w'", "# it's 'x' PASSWORD=\"a\"", "# PASSWORD=ab'c d' x'", "it's a 'password=abc def' x",
                 "'a' \"b\" 'c' \"d\" password='x y'", "\"it's\" password=abc", "'\"' password=abc", "$'a\\'b' password=abc", "$\"a\\\"b\" password=abc",
                 "password=\"a\\\"b c\"", "password='a\\'b c'", "password=$'a\\'b c' d", "echo 'a' && mysql -p'x y' db", "echo \"it's\"; mysql -p'***' db"):
        yield text
        yield text + "\n" + text
        yield f"{text}\n# trailing '\n{text}"


# ---- adversarial floods at small scale ---------------------------------------------------------------------------------------------

_FLOOD_SIZES = (2048, 8192)
_TAIL = "\npassword=hunter2"

# the units of the megabyte, new-carrier, quoting, quote-noise and command-line flood tests of tests/test_redact.py
_UNITS = (
    "a://b:b:b:", "--password ", "mode=fast ", "'a':'", "Authorization: Basic ", "-----BEGIN PRIVATE KEY-----", "Bearer ", "a.b-", "SWARM_ED25519_KEY=",
    "SWARM_ED25519_KEY_FILE=", "a,", "k=a,", "--ed25519-key ", "ed25519.seed.", "Cookie: ", "Authorization:x", "--mode a;", "-v ", "password=a,token = ",
    "token=a,--password ", 'Cookie: a="', "x=-u=", '-u "', "A_KEY=", "STRIPE_KEY=A_KEY=", "a://u:", 'password="', "-u=a:", 'Cookie: a="b" ', "-u a:",
    "--user=-u=", "a=", "a=a=", "user=a,", "user=a;", "x=1&", "mysql ", "docker login ", "sshpass -p ", "-u a:b ", "- name: PASSWORD\n  value: ",
    "password: |\n  ", "STRIPE_KEY=", "redis-cli ", "- name: ", "name: PASSWORD\n", "password:  ", 'mysql -p" ', "mysql -p\\\" ", "-u me:'", "x --password $'",
    'DB_PASSWORD="', "mysql -p\\\\\\\" ", '"password": "', '\\"password\\": \\"', "a\\", "'", '"', "\\'", '\\"', "a'", "-p'", '-p"', "it's ", "x' ", "$'",
    "'\\'", "\"'", "mysql -p'a' ", "echo \"it's\"; ", "mysql -u a ", "docker login x ", "`--user a:", "docker login -p ", "redis-cli -a ", "-passin pass:",
    "--cmd=mysql ", "-u=-u=", "x -u ", "-u  ", "mysql -p'", 'sshpass -p "', "docker login -u a: -p ", "sqlcmd -P ", "ssh-keygen -N ", "__-u a:", "(-u a:",
    "PASSWORD=", "x=y ", 'PASSWORD="ab cd" ', "a.", "a-", "a+", "eyJ-", "bearer ", "-----BEGIN ", 'a="', "a=b;", "token: ", "\\", '"\\', 'a=\\"',
)
_LEADS = ("", "DB_PASSWORD=", "mysql -p", "Cookie: ", "curl -u me:", "# ", "password: ", "--token ")
# the 200 KB adversarial shapes of `test_adversarial_200kb_inputs_scan_in_bounded_time`, as (name, unit-or-builder)
_SHAPED_FLOODS: tuple[tuple[str, Callable[[int], str]], ...] = (
    ("scheme_colons", lambda n: "x://" + ":" * n),
    ("dashes", lambda n: "-" * n),
    ("userinfo", lambda n: ("a://" + "u" * 200 + ":") * max(1, n // 205)),
    ("backslashes", lambda n: '"a":"' + "\\" * n),
    ("backslash_pairs", lambda n: "\\" * n + '"password": "x"'),
    ("escaped_quotes", lambda n: 'DB_PASSWORD=' + '\\"' * (n // 2)),
    ("quote_backslash", lambda n: "--password " + '"\\' * (n // 2)),
    ("assignment_escaped", lambda n: "DB_PASSWORD=" + 'a=\\"' * (n // 4)),
    ("long_name", lambda n: "a" * n + "=password=x"),
    ("long_name_tail", lambda n: "a" * n + "_password=hunter2"),
    ("long_value", lambda n: "password=" + "a" * n),
    ("comma_value", lambda n: "API_TOKEN=" + "q," * (n // 2) + " done"),
    ("many_blanks", lambda n: "password=" + " " * n + "x"),
    ("many_newlines", lambda n: "token:" + "\n" * n + "abc"),
    ("many_crlf", lambda n: "password=abc" + "\r\n" * (n // 2) + "x"),
    ("yaml_block", lambda n: "secret: |\n" + "  line\n" * (n // 7)),
    ("yaml_pairs", lambda n: "- name: PASSWORD\n  value: x\n" * (n // 28)),
    ("pem_open", lambda n: "-----BEGIN PRIVATE KEY-----\n" + "A" * n),
    ("pem_repeat", lambda n: "-----BEGIN " * (n // 11)),
    ("long_base64", lambda n: "Authorization: Basic " + "Zm9v" * (n // 4)),
    ("long_bearer", lambda n: "Bearer " + "a" * n),
    ("many_flags", lambda n: " ".join(f"--opt{i}=v" for i in range(n // 12)) + " -p pw"),
    ("mysql_arguments", lambda n: "mysql " + " ".join(f"--a{i}={'v' * 40}" for i in range(n // 50)) + " -u root -pxx db"),
)


def _flood_texts(expand: Callable[[str], str]) -> Iterator[str]:
    for size in _FLOOD_SIZES:
        for unit in _UNITS:
            yield unit * (size // len(unit)) + _TAIL
            yield unit * (size // len(unit))
        for unit in _UNITS[:60:3]:
            for lead in _LEADS:
                yield lead + unit * (size // len(unit)) + _TAIL
        for _, build in _SHAPED_FLOODS:
            text = build(size)
            yield text
            yield text + _TAIL
        secret = expand("{{tok:plain:9:24}}")
        yield "token=" + "a," * (size // 2) + secret
        yield "x " * (size // 2) + f"password={secret}"
