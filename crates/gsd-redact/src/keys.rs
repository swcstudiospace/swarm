//! Credential-name classification (level 0 of the port): which names, and which name/value pairs, hold a secret.
//!
//! A port of `swarm/redact.py` lines 52-131 and 681-724. Names are `&[char]` slices of the scanned text. Nothing here
//! allocates except [`name_tokens`], which exists for callers that want the tokens themselves: the classifiers walk the name
//! with a token iterator and compare against the word sets case-insensitively on the code points.
//!
//! The Python `lru_cache` on `_name_hit` and `_key_name` is not ported; it only memoises a pure function.
//! Python's `is_sensitive_key(name: object)` returns `False` for a non-`str`; the typed slices here make that case the
//! caller's (the container walker's) business.

use crate::cls::{is_alnum, is_digit, is_lower, is_upper};

/// `_NAME_CAP` (redact.py:90): names longer than this are cut before splitting.
const NAME_CAP: usize = 256;

/// The longest word in any of the sets below. A token longer than this cannot be a member of a set.
const MAX_WORD: usize = 16;

/// `_SENSITIVE_TOKENS` (redact.py:54).
fn is_sensitive_token(w: &str) -> bool {
    matches!(
        w,
        "token"
            | "secret"
            | "password"
            | "passwd"
            | "passphrase"
            | "credential"
            | "credentials"
            | "signing"
            | "private"
            | "cookie"
            | "cookies"
            | "auth"
            | "oauth"
            | "authorization"
            | "authorisation"
            | "authentication"
            | "apikey"
            | "accesskey"
            | "secretkey"
            | "privatekey"
            | "signingkey"
            | "sshkey"
            | "masterkey"
            | "encryptionkey"
            | "bearer"
    )
}

/// `_WEAK_TOKENS` (redact.py:84).
fn is_weak_token(w: &str) -> bool {
    matches!(
        w,
        "auth"
            | "oauth"
            | "private"
            | "signing"
            | "cookie"
            | "cookies"
            | "authentication"
            | "authorization"
            | "authorisation"
            | "bearer"
    )
}

/// `_KEY_PREFIXES` (redact.py:64).
const KEY_PREFIXES: [&str; 19] = [
    "api",
    "access",
    "secret",
    "private",
    "signing",
    "ssh",
    "encryption",
    "master",
    "auth",
    "account",
    "session",
    "license",
    "licence",
    "client",
    "ed25519",
    "hmac",
    "jwt",
    "handoff",
    "signer",
];

/// `_KEY_WORDS` (redact.py:66).
const KEY_WORDS: [&str; 2] = ["key", "seed"];

/// The words of `_SENSITIVE_SUFFIXES` (redact.py:73) that are written out: the other entries are every `prefix + word` of
/// `KEY_PREFIXES` x `KEY_WORDS`, which [`ends_with_sensitive_suffix`] derives.
const SUFFIX_WORDS: [&str; 15] = [
    "password",
    "passwd",
    "passphrase",
    "token",
    "secret",
    "credential",
    "credentials",
    "apikey",
    "accesskey",
    "secretkey",
    "privatekey",
    "signingkey",
    "sshkey",
    "masterkey",
    "encryptionkey",
];

/// `_PLURAL_WORDS` (redact.py:67).
fn is_plural_word(w: &str) -> bool {
    matches!(w, "tokens" | "secrets" | "passwords" | "keys")
}

/// `_PLURAL_CONTEXT` (redact.py:68).
fn is_plural_context(w: &str) -> bool {
    matches!(
        w,
        "operator" | "api" | "access" | "bearer" | "auth" | "session"
    )
}

/// `_PATH_TOKENS` (redact.py:70).
fn is_path_token(w: &str) -> bool {
    matches!(w, "file" | "path")
}

/// `_PASS_TOKENS` (redact.py:79).
fn is_pass_token(w: &str) -> bool {
    matches!(w, "pass" | "pwd")
}

/// `_PASS_CONTEXT` (redact.py:80).
fn is_pass_context(w: &str) -> bool {
    matches!(
        w,
        "db" | "database"
            | "sql"
            | "mysql"
            | "pg"
            | "postgres"
            | "postgresql"
            | "mongo"
            | "mongodb"
            | "redis"
            | "smtp"
            | "imap"
            | "ftp"
            | "sftp"
            | "ssh"
            | "ldap"
            | "amqp"
            | "rabbitmq"
            | "user"
            | "admin"
            | "root"
            | "login"
            | "mail"
            | "auth"
            | "master"
    )
}

/// `_VENDOR_KEY_SKIP` (redact.py:701).
fn is_vendor_key_skip(w: &str) -> bool {
    matches!(
        w,
        "cache"
            | "sort"
            | "idempotency"
            | "partition"
            | "primary"
            | "foreign"
            | "public"
            | "lookup"
            | "group"
            | "shard"
            | "unique"
            | "index"
            | "hash"
            | "object"
            | "route"
            | "routing"
            | "dedupe"
            | "dedup"
            | "lock"
            | "message"
    )
}

/// The lowercase of the ASCII token `tok` as a `&str` view into `buf`, or `None` when it is longer than [`MAX_WORD`] (it
/// cannot be in any word set then). Tokens only hold `[A-Za-z0-9]`, so the lowercase is ASCII.
fn lowered<'b>(tok: &[char], buf: &'b mut [u8; MAX_WORD]) -> Option<&'b str> {
    let dst = buf.get_mut(..tok.len())?;
    for (d, c) in dst.iter_mut().zip(tok) {
        *d = u8::try_from(c.to_ascii_lowercase()).ok()?;
    }
    std::str::from_utf8(dst).ok()
}

/// `tok` is a member of the word set `set`, compared in lowercase (`t in SET`).
fn in_set(tok: &[char], set: fn(&str) -> bool) -> bool {
    let mut buf = [0; MAX_WORD];
    lowered(tok, &mut buf).is_some_and(set)
}

/// `tok == word`, with `tok` compared in lowercase.
fn eq_ci(tok: &[char], word: &str) -> bool {
    tok.len() == word.len() && ends_with_ci(tok, word)
}

/// `tok.endswith(suffix)`, with `tok` compared in lowercase.
fn ends_with_ci(tok: &[char], suffix: &str) -> bool {
    let n = suffix.len();
    tok.len() >= n
        && tok
            .iter()
            .skip(tok.len() - n)
            .zip(suffix.chars())
            .all(|(c, w)| c.to_ascii_lowercase() == w)
}

/// `t.endswith(_SENSITIVE_SUFFIXES)` (redact.py:106): the 15 written words, or `prefix + key|seed`.
fn ends_with_sensitive_suffix(tok: &[char]) -> bool {
    if SUFFIX_WORDS.iter().any(|w| ends_with_ci(tok, w)) {
        return true;
    }
    KEY_WORDS.iter().any(|w| {
        ends_with_ci(tok, w)
            && tok
                .get(..tok.len() - w.len())
                .is_some_and(|rest| KEY_PREFIXES.iter().any(|p| ends_with_ci(rest, p)))
    })
}

/// The zero-width split of `_CAMEL_RE` (redact.py:88) between `prev` and `cur`, `next` being the character after `cur`:
/// `(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])`.
fn camel_break(prev: char, cur: char, next: Option<char>) -> bool {
    (is_upper(cur) && (is_lower(prev) || is_digit(prev)))
        || (is_upper(prev) && is_upper(cur) && next.is_some_and(is_lower))
}

/// The tokens of `_name_tokens` (redact.py:94) as slices of the name, in their original case: the alphanumeric runs of
/// `name[:256]`, cut at camel-case boundaries. Every code point outside `[A-Za-z0-9]` is a separator.
struct Tokens<'a> {
    name: &'a [char],
    pos: usize,
}

impl<'a> Tokens<'a> {
    fn new(name: &'a [char]) -> Self {
        Tokens {
            name: name.get(..NAME_CAP).unwrap_or(name),
            pos: 0,
        }
    }
}

impl<'a> Iterator for Tokens<'a> {
    type Item = &'a [char];

    fn next(&mut self) -> Option<&'a [char]> {
        let name = self.name;
        let start = self.pos
            + name
                .iter()
                .skip(self.pos)
                .take_while(|&&c| !is_alnum(c))
                .count();
        let mut end = start + 1;
        while let (Some(&prev), Some(&cur)) = (name.get(end - 1), name.get(end)) {
            if !is_alnum(cur) || camel_break(prev, cur, name.get(end + 1).copied()) {
                break;
            }
            end += 1;
        }
        self.pos = end;
        name.get(start..end)
    }
}

/// `_name_tokens(name)` (redact.py:94): the lowercase tokens of `name`, split on non-alphanumerics and camel case
/// (`apiKey` gives `api`, `key`). The name is cut to 256 code points first.
///
/// The classifiers below do not call this; they iterate the tokens without allocating.
pub fn name_tokens(name: &[char]) -> Vec<String> {
    Tokens::new(name)
        .map(|t| t.iter().map(char::to_ascii_lowercase).collect())
        .collect()
}

/// `_sensitive(toks, strict=...)` (redact.py:99) over the tokens of `name`.
///
/// The first branch is an `if`/`elif` chain: a whole-word match that strict mode skips (`auth`) falls out of the chain without
/// trying the suffix and previous-token rules (redact.py:103-105), and this keeps that.
fn sensitive(name: &[char], strict: bool) -> bool {
    let (mut count, mut last) = (0usize, None);
    for t in Tokens::new(name) {
        count += 1;
        last = Some(t);
    }
    if count > 1 && last.is_some_and(|t| in_set(t, is_path_token)) {
        return false;
    }
    let mut prev: Option<&[char]> = None;
    for t in Tokens::new(name) {
        if in_set(t, is_sensitive_token) {
            if !(strict && in_set(t, is_weak_token)) {
                return true;
            }
        } else if ends_with_sensitive_suffix(t) {
            return true;
        } else if let Some(p) = prev {
            let key_word = KEY_WORDS.iter().any(|w| eq_ci(t, w));
            if (key_word && KEY_PREFIXES.iter().any(|w| eq_ci(p, w)))
                || (in_set(t, is_pass_token) && in_set(p, is_pass_context))
                || (in_set(t, is_plural_word) && in_set(p, is_plural_context))
            {
                return true;
            }
        }
        prev = Some(t);
    }
    false
}

/// `is_sensitive_key(name)` (redact.py:118), the public Python function: the name looks like it names a credential.
///
/// Serves `_value_starts` (redact.py:277, 280), `_yaml_spans`, `_pair_spans` (redact.py:1056) and `_credential_pair`, and is the
/// `eq` branch of [`name_hit`].
pub fn is_sensitive_key(name: &[char]) -> bool {
    sensitive(name, false)
}

/// `_strict_key(name)` (redact.py:129): [`is_sensitive_key`] minus the generic whole words (`auth`, `private`, `cookie` ...).
///
/// Only reached through [`name_hit`] in Python.
pub fn strict_key(name: &[char]) -> bool {
    sensitive(name, true)
}

/// `_name_hit(name, eq)` (redact.py:681): [`is_sensitive_key`] when `eq`, else [`strict_key`].
///
/// Serves `_scan` (redact.py:1014), `_continuation` and `_value_starts` (redact.py:271, 274).
pub fn name_hit(name: &[char], eq: bool) -> bool {
    if eq {
        is_sensitive_key(name)
    } else {
        strict_key(name)
    }
}

/// `_key_name(name)` (redact.py:711): the name has at least two tokens, the last is `key` and the one before it is not one
/// of the lookup-style qualifiers of `_VENDOR_KEY_SKIP` (`STRIPE_KEY` yes, `sort_key` no).
///
/// Serves `_scan` (redact.py:1015) and `_credential_pair`.
pub fn key_name(name: &[char]) -> bool {
    let (mut before, mut last): (Option<&[char]>, Option<&[char]>) = (None, None);
    for t in Tokens::new(name) {
        before = last;
        last = Some(t);
    }
    match (before, last) {
        (Some(b), Some(l)) => eq_ci(l, "key") && !in_set(b, is_vendor_key_skip),
        _ => false,
    }
}

/// `[A-Za-z0-9_+/=~.-]`, the class of `_LONG_SECRET_RE` (redact.py:703).
fn is_secret_char(c: char) -> bool {
    is_alnum(c) || matches!(c, '_' | '+' | '/' | '=' | '~' | '.' | '-')
}

/// `_long_secret(value)` (redact.py:716): after stripping `"`, `'` and `\` from both ends, the value is at least 20
/// characters of `[A-Za-z0-9_+/=~.-]` and holds a digit, a lowercase and an uppercase letter.
///
/// Serves `_key_value_end` (redact.py:733) and `_credential_pair`.
pub fn long_secret(value: &[char]) -> bool {
    let strip = |c: &char| matches!(c, '"' | '\'' | '\\');
    let start = value.iter().take_while(|&c| strip(c)).count();
    let v = value.get(start..).unwrap_or_default();
    let end = v.len() - v.iter().rev().take_while(|&c| strip(c)).count();
    let v = v.get(..end).unwrap_or_default();
    v.len() >= 20
        && v.iter().all(|&c| is_secret_char(c))
        && v.iter().any(|&c| is_digit(c))
        && v.iter().any(|&c| is_lower(c))
        && v.iter().any(|&c| is_upper(c))
}

/// `_credential_pair(name, value)` (redact.py:722): a sensitive name, or a `*_key` name with a credential-shaped value.
///
/// Serves `_pair_spans` (redact.py:1046); the container walker `_walk` calls it too, after checking both are `str`.
pub fn credential_pair(name: &[char], value: &[char]) -> bool {
    is_sensitive_key(name) || (key_name(name) && long_secret(value))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cs(s: &str) -> Vec<char> {
        s.chars().collect()
    }

    // Every expected value below was produced by running `swarm.redact` (the oracle) on the input.

    #[test]
    fn tokens_split_on_non_ascii_and_camel_case() {
        let table: &[(&str, &[&str])] = &[
            ("apiKey", &["api", "key"]),
            ("APIKey", &["api", "key"]),
            ("HTTPServer", &["http", "server"]),
            ("XMLHttpRequest", &["xml", "http", "request"]),
            ("getHTTPResponseCode", &["get", "http", "response", "code"]),
            ("ed25519Key", &["ed25519", "key"]),
            ("a1B2c3", &["a1", "b2c3"]),
            ("ABCdef", &["ab", "cdef"]),
            ("__a__b__", &["a", "b"]),
            ("", &[]),
            ("---", &[]),
            ("\u{e9}tokenX", &["token", "x"]),
            ("tok\u{e9}n", &["tok", "n"]),
            ("SWARM_ED25519_KEY", &["swarm", "ed25519", "key"]),
            ("api_keys", &["api", "keys"]),
            ("isOK", &["is", "ok"]),
            ("iPhone", &["i", "phone"]),
            ("AB", &["ab"]),
            ("aB", &["a", "b"]),
            ("Ab1Cd", &["ab1", "cd"]),
            ("a1B", &["a1", "b"]),
            ("A1b", &["a1b"]),
            ("ABc", &["a", "bc"]),
            ("aBC", &["a", "bc"]),
            ("aBCd", &["a", "b", "cd"]),
        ];
        for (name, want) in table {
            assert_eq!(name_tokens(&cs(name)), *want, "{name:?}");
        }
    }

    #[test]
    fn sensitive_and_strict_keys() {
        let table: &[(&str, bool, bool)] = &[
            ("password", true, true),
            ("PGPASSWORD", true, true),
            ("accesstoken", true, true),
            ("github_token", true, true),
            ("api_key", true, true),
            ("ACCESS-KEY", true, true),
            ("clientKey", true, true),
            ("signing_seed", true, true),
            ("key", false, false),
            ("seed", false, false),
            ("sort_key", false, false),
            ("cache_key", false, false),
            ("random_seed", false, false),
            ("tokens", false, false),
            ("max_tokens", false, false),
            ("ux_tokens", false, false),
            ("secrets", false, false),
            ("passwords", false, false),
            ("SUBSTRATE_OPERATOR_TOKENS", true, true),
            ("api_keys", true, true),
            ("OPERATOR_KEYS", true, true),
            ("session_secrets", true, true),
            ("author", false, false),
            ("authority", false, false),
            ("auth", true, false),
            ("oauth", true, false),
            ("Authorization", true, false),
            ("authentication", true, false),
            ("authorisation", true, false),
            ("bearer", true, false),
            ("private", true, false),
            ("signing", true, false),
            ("cookie", true, false),
            ("cookies", true, false),
            ("Set-Cookie", true, false),
            ("DB_PASS", true, true),
            ("MYSQL_PWD", true, true),
            ("PWD", false, false),
            ("gate_pass", false, false),
            ("pass", false, false),
            ("pwd", false, false),
            ("user_pass", true, true),
            ("sshpass", false, false),
            ("SWARM_ED25519_KEY", true, true),
            ("SWARM_ED25519_KEY_FILE", false, false),
            ("api_key_file", false, false),
            ("api_key_path", false, false),
            ("key_file", false, false),
            ("token_file", false, false),
            ("file", false, false),
            ("path", false, false),
            ("token_path_x", true, true),
            ("private_key", true, true),
            ("privateKey", true, true),
            ("ssh_key", true, true),
            ("sshkey", true, true),
            ("ssh_seed", true, true),
            ("handoff_seed", true, true),
            ("signer_key", true, true),
            ("signerseed", true, true),
            ("hmac_key", true, true),
            ("jwt_seed", true, true),
            ("license_key", true, true),
            ("licence-key", true, true),
            ("LicenseSeed", true, true),
            ("master.key", true, true),
            ("account_key", true, true),
            ("accountkey", true, true),
            ("x_apikey", true, true),
            ("apikey", true, true),
            ("myapikey", true, true),
            ("my_secretkey", true, true),
            ("credentials", true, true),
            ("credential_id", true, true),
            ("passphrase", true, true),
            ("passwd", true, true),
            ("password_hash", true, true),
            ("tokenizer", false, false),
            ("author_token", true, true),
            ("e", false, false),
            ("", false, false),
            ("\u{e9}token", true, true),
            ("token\u{e9}", true, true),
            ("to\u{e9}ken", false, false),
            ("api\u{e9}key", true, true),
            ("api\u{212a}ey", false, false),
            ("apI_Key", false, false),
            ("keys", false, false),
            ("api_seed", true, true),
            ("api_seeds", false, false),
            ("auth_key", true, true),
            ("auth_keys", true, true),
            ("bearer_tokens", true, true),
            ("Bearer", true, false),
            ("x_auth", true, false),
            ("access_tokens_x", true, true),
            ("encryptionKey", true, true),
            ("ENCRYPTION_SEED", true, true),
            ("enc_key", false, false),
            ("postgresql_pass", true, true),
            ("amqp_pwd", true, true),
            ("master_pass", true, true),
        ];
        for &(name, sens, strict) in table {
            let n = cs(name);
            assert_eq!(is_sensitive_key(&n), sens, "is_sensitive_key({name:?})");
            assert_eq!(strict_key(&n), strict, "strict_key({name:?})");
            assert_eq!(name_hit(&n, true), sens, "name_hit({name:?}, eq)");
            assert_eq!(name_hit(&n, false), strict, "name_hit({name:?}, !eq)");
        }
    }

    #[test]
    fn vendor_key_names() {
        let table: &[(&str, bool)] = &[
            ("STRIPE_KEY", true),
            ("SUPABASE_SERVICE_ROLE_KEY", true),
            ("sort_key", false),
            ("cache_key", false),
            ("key", false),
            ("_key", false),
            ("x.key", true),
            ("Message_Key", false),
            ("messageKey", false),
            ("stripeKey", true),
            ("a_key_file", false),
            ("a_key_", true),
            ("api_keys", false),
            ("x_Key", true),
            ("x_KEY1", false),
            ("public-key", false),
            ("publicKey", false),
            ("lock_key", false),
            ("dedup_key", false),
            ("dedupe_key", false),
            ("hash_key", false),
            ("index_key", false),
            ("routing_key", false),
            ("route_key", false),
            ("object_key", false),
            ("unique_key", false),
            ("shard_key", false),
            ("group_key", false),
            ("lookup_key", false),
            ("foreign_key", false),
            ("primary_key", false),
            ("partition_key", false),
            ("idempotency_key", false),
            ("cache.key", false),
            ("x\u{e9}key", true),
            ("\u{e9}key", false),
            ("a\u{e9}\u{e9}key", true),
            ("AKey", true),
            ("aKEY", true),
            ("XMLKey", true),
            ("HTTPKey", true),
        ];
        for &(name, want) in table {
            assert_eq!(key_name(&cs(name)), want, "{name:?}");
        }
    }

    #[test]
    fn long_secret_shapes() {
        let table: &[(&str, bool)] = &[
            ("aB3aB3aB3aB3aB3aB3aB3", true),
            ("aB3aB3aB3aB3aB3aB3aB", true),
            ("aB3aB3aB3aB3aB3aB3aB3", true),
            ("\"aB3aB3aB3aB3aB3aB3aB3\"", true),
            ("\\'aB3aB3aB3aB3aB3aB3aB3'\\", true),
            ("aB3aB3aB3aB3aB3aB3aB3!", false),
            ("abcdefghijklmnopqrstuvwxyz", false),
            ("ABCDEFGHIJ0123456789", false),
            ("ABCDEFGHIJabcdefghij", false),
            ("abcdefghij0123456789", false),
            ("Ab1.................", true),
            ("Ab1................", false),
            ("\"\"\"\"Ab1.................", true),
            ("Ab1.Ab1.Ab1.Ab1.Ab1.\"x\"", false),
            (
                "\"\"\"\"\"\"\"\"\"\"\"\"\"\"\"\"\"\"\"\"\"\"\"\"\"\"\"\"\"\"",
                false,
            ),
            ("", false),
            ("\\\"\\\"Ab1+/=~._-xxxxxxxxxxxxx\\\"", true),
            ("Ab1+/=~._-xxxxxxxxxxxxx\u{e9}", false),
            ("\u{e9}Ab1+/=~._-xxxxxxxxxxxxx", false),
            ("Ab1 Ab1 Ab1 Ab1 Ab1 Ab1 Ab1", false),
            ("Ab1\nAb1xxxxxxxxxxxxxxxxxx", false),
            ("Ab1xxxxxxxxxxxxxxxxx\\", true),
            ("a\"bAb1Ab1Ab1Ab1Ab1Ab1Ab1", false),
            ("\"Ab1'Ab1\\Ab1Ab1Ab1Ab1Ab1\"", false),
        ];
        for &(value, want) in table {
            assert_eq!(long_secret(&cs(value)), want, "{value:?}");
        }
    }

    #[test]
    fn credential_pairs() {
        let table: &[(&str, &str, bool)] = &[
            ("STRIPE_KEY", "aB3aB3aB3aB3aB3aB3aB3", true),
            ("STRIPE_KEY", "short", false),
            ("sort_key", "aB3aB3aB3aB3aB3aB3aB3", false),
            ("password", "x", true),
            ("x_token", "", true),
            ("STRIPE_KEY", "\"aB3aB3aB3aB3aB3aB3aB3\"", true),
            ("key", "aB3aB3aB3aB3aB3aB3aB3", false),
            ("a_key", "", false),
            ("api_secret", "\\\"", true),
            ("a_key", "Ab1.................", true),
        ];
        for &(name, value, want) in table {
            assert_eq!(
                credential_pair(&cs(name), &cs(value)),
                want,
                "{name:?} = {value:?}"
            );
        }
    }

    #[test]
    fn name_is_cut_to_256_code_points_before_splitting() {
        let a = |n: usize| "a".repeat(n);
        // (name, is_sensitive_key, last token); oracle values.
        let table: Vec<(String, bool, String)> = vec![
            (a(250) + "_token", true, "token".into()),
            (a(251) + "_token", false, "toke".into()),
            (a(255) + "B", false, "b".into()),
            (a(256) + "B", false, a(256)),
            (a(254) + "BCd", false, "bc".into()),
            (a(255) + "BCd", false, "b".into()),
        ];
        for (name, sens, last) in table {
            let toks = name_tokens(&cs(&name));
            assert_eq!(
                toks.last(),
                Some(&last),
                "last token of a name of {} characters",
                name.len()
            );
            assert_eq!(is_sensitive_key(&cs(&name)), sens);
        }
    }

    #[test]
    fn long_inputs_are_handled() {
        // A 600k-character name (cut to 256, so its `_key` tail is gone) and a 300k-character value: no panic, no blow-up.
        assert!(!key_name(&cs(&format!("{}_key", "a".repeat(600_000)))));
        assert!(key_name(&cs(&format!("{}_key", "a".repeat(100)))));
        assert!(long_secret(&cs(&"aB3".repeat(100_000))));
        assert!(!is_sensitive_key(&cs(&"x".repeat(600_000))));
    }

    #[test]
    fn empty_inputs() {
        assert!(name_tokens(&[]).is_empty());
        assert!(
            !is_sensitive_key(&[])
                && !strict_key(&[])
                && !key_name(&[])
                && !long_secret(&[])
                && !credential_pair(&[], &[])
        );
    }
}
