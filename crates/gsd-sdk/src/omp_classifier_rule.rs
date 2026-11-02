//! Rust mirror of the SDLC prompt classifier. This is the fourth copy; the other three are the
//! sources: `omp/src/hooks.ts` (the authority: `classifyPrompt`, `classifiedText`, and the HOOK-01
//! wiring in `swarmContext`), `swarm/signal_detector.py` (`classify`,
//! `extract_unescaped_original`) and `hooks/user_prompt_submit.py` (`classify`). Where they
//! disagree, this module follows `omp/src/hooks.ts`. Nothing here reads or edits the sources.
//!
//! Fixture: `tests/fixtures/classifier_prompts.json` (`{positive, negative}`). The tests below
//! replay it; `omp/test/hooks.test.ts` and `tests/test_hook.py` read the same file. This module
//! is fixture-based, so `VECTOR_IDS` is empty.
//!
//! Exact TS semantics, kept on purpose:
//! - Trimming is JavaScript's `trim` (`js_trim`): U+FEFF is removed; U+0085 and U+001C..U+001F
//!   stay.
//! - The regexes have no `u` flag, so `\b`, `\w` and `/i` are ASCII-only. Words are matched on
//!   bytes: a non-ASCII byte is neither a word byte nor an ASCII letter, so `fixé` is `fix`
//!   followed by a boundary.
//! - Opt-out tokens are searched in the full lowercase of the trimmed text, so U+212A KELVIN SIGN
//!   is `k`.
//! - `classifiedText` returns the unescaped ORIGINAL. Decoding makes a new string, so
//!   `classified_text` returns a `Cow`: borrowed unless the element contains `&`. This is the one
//!   signature change from the brief (`-> &str`).
//!
//! Divergences of the Python copies from `omp/src/hooks.ts`, none of them followed here:
//! - Trim: `prompt.strip()` (`swarm/signal_detector.py:48,86`, `hooks/user_prompt_submit.py:60`)
//!   removes U+0085 and U+001C..U+001F and keeps U+FEFF. TS (`omp/src/hooks.ts:43,88`) does the
//!   opposite.
//! - Uplift root: `UPLIFT_ROOT` (`swarm/signal_detector.py:19`) has no `re.A`, so it folds
//!   non-ASCII case (U+017F for `s`, U+212A for `k`) and reads `é` as a word char. TS
//!   (`omp/src/hooks.ts:52`) is ASCII-only.
//! - ORIGINAL opener: `_is_ascii_ident` (`swarm/signal_detector.py:39-40,56-57`) uses `isalnum()`,
//!   so `<originalé>` is skipped. TS (`omp/src/hooks.ts:78-80,95`) extracts it.
//! - `hooks/user_prompt_submit.py:58-68`, the fallback taken when `swarm.signal_detector` cannot be
//!   imported or raises, has no ORIGINAL extraction. Its primary path (`:54-57`) delegates to
//!   `signal_detector`, which has it.
//!
//! Not ported: the HOOK-01 session gates in `swarmContext` (identity, `SWARM_CHILD`,
//! `SWARM_AGENT`, an existing SWARM_CONTEXT part) and the `/gsd-autonomous` handoff. This module
//! decides the prompt only.

use std::borrow::Cow;

/// Fixture-based: no frozen contract record (`tests/omp_contract/vectors.json`) is replayed here.
pub const VECTOR_IDS: &[&str] = &[];

/// Opt-out tokens (`NEGATIVE`, `omp/src/hooks.ts:14`), searched in the lowercased trimmed text.
const NEGATIVE: &[&str] = &["/uplift", "/think", "explain only", "/all-in-one:"];

/// Tags the `/swarm` dispatch prompt (`DISPATCH_MARKER`, `omp/src/hooks.ts:17`).
const DISPATCH_MARKER: &str = "[agent-swarm:dispatch]";

/// Interrogative openers (`QUESTION`, `omp/src/hooks.ts:19`), each a whole word at the start.
const QUESTION_OPENERS: &[&str] = &[
    "what", "why", "how", "when", "where", "who", "which", "is", "are", "can", "could", "does",
    "do", "should", "explain", "describe",
];

/// `TRIVIAL` (`omp/src/hooks.ts:20`): `typo` or `typos` as a whole word anywhere.
const TYPO_WORDS: &[&str] = &["typo", "typos"];

/// The word of `TRIVIAL`'s `^rename\b` alternative: `rename` at the start.
const RENAME_WORD: &str = "rename";

/// SDLC verbs (`SDLC`, `omp/src/hooks.ts:21-22`), each a whole word. `write tests?` is its two
/// spellings, and `set up` keeps its single space.
const SDLC_PHRASES: &[&str] = &[
    "build",
    "implement",
    "add",
    "create",
    "fix",
    "refactor",
    "migrate",
    "deploy",
    "release",
    "ship",
    "write test",
    "write tests",
    "set up",
    "setup",
    "integrate",
    "scaffold",
    "upgrade",
    "orchestrate",
];

/// Root names of an Ultrathink or Prompt-Uplift XML (`UPLIFT_ROOT`, `omp/src/hooks.ts:52`).
const UPLIFT_ROOTS: &[&str] = &[
    "BUILD_PROMPT",
    "FIX_PROMPT",
    "RESEARCH_PROMPT",
    "CHANGE_PROMPT",
    "UPLIFTED_PROMPT",
    "uplifted",
    "ultrathink",
];

/// Opener and closer of the ORIGINAL element (`omp/src/hooks.ts:54-55`).
const ORIGINAL_OPEN: &str = "<original";
const ORIGINAL_CLOSE: &str = "</original>";

/// System-note prefix: `startsWith` at `omp/src/hooks.ts:45`, case-sensitive.
const SYSTEM_REMINDER_PREFIX: &str = "<system-reminder";

/// The text the classifier judges: `classifiedText` (`omp/src/hooks.ts:87-112`).
///
/// A prompt whose trimmed text starts with an uplift root (`UPLIFT_ROOTS`, ASCII case-insensitive,
/// followed by a non-word char) yields the raw text between its first ORIGINAL element and that
/// element's `</ORIGINAL>`, with the five entities decoded in TS order (`&amp;` last). Any other
/// prompt, and an uplift root with no complete ORIGINAL element, yields the prompt itself,
/// untrimmed.
///
/// Borrowed unless the element contains `&`, the only character an entity starts with. The brief's
/// `&str` return cannot hold decoded text, so this returns `Cow<'_, str>`, which derefs to `&str`.
pub fn classified_text(prompt: &str) -> Cow<'_, str> {
    let t = js_trim(prompt);
    if !is_uplift_root(t) {
        return Cow::Borrowed(prompt);
    }
    match original_element(t) {
        Some(raw) => unescape_entities(raw),
        None => Cow::Borrowed(prompt),
    }
}

/// Whether `prompt` is SDLC-shaped: `classifyPrompt(classifiedText(prompt))`, the HOOK-01 decision
/// (`omp/src/hooks.ts:42-49,123-150`). An uplift XML is judged by its ORIGINAL; every other prompt
/// is judged by its own text.
pub fn is_sdlc_prompt(prompt: &str) -> bool {
    is_sdlc_text(&classified_text(prompt))
}

/// `classifyPrompt` (`omp/src/hooks.ts:42-49`): false for empty text, slash commands, system notes,
/// opt-out tokens, questions and trivial edits; otherwise true when an SDLC verb is a whole word.
fn is_sdlc_text(text: &str) -> bool {
    let t = js_trim(text);
    if t.is_empty() || t.starts_with('/') || t.starts_with(SYSTEM_REMINDER_PREFIX) {
        return false;
    }
    if contains_lowercased(t, DISPATCH_MARKER)
        || NEGATIVE.iter().any(|token| contains_lowercased(t, token))
    {
        return false;
    }
    if t.ends_with('?')
        || QUESTION_OPENERS
            .iter()
            .any(|word| starts_with_word(t, word))
        || is_trivial_edit(t)
    {
        return false;
    }
    SDLC_PHRASES
        .iter()
        .any(|phrase| contains_whole_word(t, phrase))
}

/// `TRIVIAL` (`omp/src/hooks.ts:20`): `\btypos?\b` anywhere, or `^rename\b`.
fn is_trivial_edit(t: &str) -> bool {
    starts_with_word(t, RENAME_WORD) || TYPO_WORDS.iter().any(|word| contains_whole_word(t, word))
}

/// `UPLIFT_ROOT` (`omp/src/hooks.ts:52`): `<`, then a root name, then a non-word char or the end.
fn is_uplift_root(t: &str) -> bool {
    t.strip_prefix('<')
        .is_some_and(|rest| UPLIFT_ROOTS.iter().any(|root| starts_with_word(rest, root)))
}

/// The raw text between the first real ORIGINAL element of `t` and its closer
/// (`omp/src/hooks.ts:91-108`).
///
/// An opener followed by an identifier char (`<originals>`) is not an element, and the search moves
/// past it. No `>` after the opener, or no closer after that, means there is no element.
fn original_element(t: &str) -> Option<&str> {
    let mut from = 0;
    loop {
        let open = find_ascii_ci(t, ORIGINAL_OPEN, from)?;
        let after_open = open + ORIGINAL_OPEN.len();
        let next = t.get(after_open..).and_then(|rest| rest.chars().next());
        if !next.is_some_and(lowercases_to_ident_char) {
            let gt = open + t.get(open..)?.find('>')?;
            let close = find_ascii_ci(t, ORIGINAL_CLOSE, gt + 1)?;
            return t.get(gt + 1..close);
        }
        from = after_open;
    }
}

/// Decodes the five entities in `classifiedText`'s order, so `&amp;lt;` becomes `&lt;`.
/// Borrowed when there is no `&`, because then no entity can be present.
fn unescape_entities(raw: &str) -> Cow<'_, str> {
    if !raw.contains('&') {
        return Cow::Borrowed(raw);
    }
    Cow::Owned(
        raw.replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", "\"")
            .replace("&apos;", "'")
            .replace("&amp;", "&"),
    )
}

/// `s.trim()` in JavaScript: WhiteSpace and LineTerminator. Rust's `trim` differs: it removes
/// U+0085 and keeps U+FEFF.
fn js_trim(text: &str) -> &str {
    text.trim_matches(is_js_whitespace)
}

/// The code points JavaScript's `trim` removes: TAB to CR, the Zs spaces, U+FEFF, U+2028, U+2029.
fn is_js_whitespace(c: char) -> bool {
    matches!(
        c,
        '\u{0009}'..='\u{000D}'
            | '\u{0020}'
            | '\u{00A0}'
            | '\u{1680}'
            | '\u{2000}'..='\u{200A}'
            | '\u{2028}'..='\u{2029}'
            | '\u{202F}'
            | '\u{205F}'
            | '\u{3000}'
            | '\u{FEFF}'
    )
}

/// A `\w` byte without the `u` flag: `[A-Za-z0-9_]`. Bytes of non-ASCII chars are not word bytes.
fn is_word_byte(b: &u8) -> bool {
    b.is_ascii_alphanumeric() || *b == b'_'
}

/// True when `word` matches ASCII case-insensitively at `start` and no word byte follows it.
fn word_at(bytes: &[u8], start: usize, word: &str) -> bool {
    let end = start + word.len();
    bytes
        .get(start..end)
        .is_some_and(|hit| hit.eq_ignore_ascii_case(word.as_bytes()))
        && !bytes.get(end).is_some_and(is_word_byte)
}

/// `^word\b`, ASCII case-insensitive.
fn starts_with_word(text: &str, word: &str) -> bool {
    word_at(text.as_bytes(), 0, word)
}

/// `\bword\b`, ASCII case-insensitive. Every word here begins and ends with an ASCII letter, so
/// each boundary is a non-word byte (or the edge of the text) just outside the word.
fn contains_whole_word(text: &str, word: &str) -> bool {
    let bytes = text.as_bytes();
    (0..bytes.len()).any(|start| {
        let boundary_before =
            start == 0 || bytes.get(start - 1).is_some_and(|prev| !is_word_byte(prev));
        boundary_before && word_at(bytes, start, word)
    })
}

/// `text.toLowerCase().includes(needle)` for an ASCII lowercase `needle`, without building the
/// lowered text.
///
/// Every start is tried, and each char is lowercased in full as JavaScript does, so U+212A KELVIN
/// SIGN matches `k`. No full lowercase mapping has an ASCII char after its first char (U+0130 maps
/// to `i` then U+0307), so starting only at original chars misses no match.
fn contains_lowercased(text: &str, needle: &str) -> bool {
    text.char_indices().any(|(start, _)| {
        text.get(start..).is_some_and(|suffix| {
            let mut lowered = suffix.chars().flat_map(char::to_lowercase);
            needle.chars().all(|want| lowered.next() == Some(want))
        })
    })
}

/// `indexOfAscii` (`omp/src/hooks.ts:62-75`): the byte index of the first ASCII case-insensitive
/// match of `needle` at or after `from`. A match is ASCII, so the index is a char boundary.
fn find_ascii_ci(text: &str, needle: &str, from: usize) -> Option<usize> {
    let hay = text.as_bytes().get(from..)?;
    hay.windows(needle.len())
        .position(|window| window.eq_ignore_ascii_case(needle.as_bytes()))
        .map(|offset| from + offset)
}

/// `isAsciiIdentChar(c.toLowerCase())` (`omp/src/hooks.ts:78-80,95`): the lowercase of `c` is one
/// ASCII identifier char. U+212A KELVIN SIGN qualifies (it lowercases to `k`); U+0130 does not (it
/// lowercases to two chars).
fn lowercases_to_ident_char(c: char) -> bool {
    let mut lowered = c.to_lowercase();
    match (lowered.next(), lowered.next()) {
        (Some(only), None) => only.is_ascii_lowercase() || only.is_ascii_digit() || only == '_',
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const FIXTURE: &str = include_str!("../../../tests/fixtures/classifier_prompts.json");

    /// The prompts under `key` in the shared fixture.
    fn fixture_prompts(key: &str) -> Vec<String> {
        let fixture: serde_json::Value = serde_json::from_str(FIXTURE).expect("fixture is JSON");
        fixture[key]
            .as_array()
            .expect("fixture key holds an array")
            .iter()
            .map(|prompt| {
                prompt
                    .as_str()
                    .expect("fixture entry is a string")
                    .to_owned()
            })
            .collect()
    }

    #[test]
    fn fixture_positives_are_sdlc() {
        let positives = fixture_prompts("positive");
        assert!(!positives.is_empty(), "the fixture has positives");
        for (i, prompt) in positives.iter().enumerate() {
            assert!(
                is_sdlc_prompt(prompt),
                "positive[{i}] must be SDLC: {prompt:?}"
            );
        }
    }

    #[test]
    fn fixture_negatives_are_not_sdlc() {
        let negatives = fixture_prompts("negative");
        assert!(!negatives.is_empty(), "the fixture has negatives");
        for (i, prompt) in negatives.iter().enumerate() {
            assert!(
                !is_sdlc_prompt(prompt),
                "negative[{i}] must not be SDLC: {prompt:?}"
            );
        }
    }

    #[test]
    fn interrogative_opener_silences_an_sdlc_verb() {
        assert!(!is_sdlc_prompt("Should we build the importer"));
        assert!(!is_sdlc_prompt("explain how the build pipeline works"));
        // The opener needs a word boundary: "whatever" is not "what".
        assert!(is_sdlc_prompt("whatever you choose, build the importer"));
    }

    #[test]
    fn trailing_question_mark_silences_the_prompt() {
        assert!(!is_sdlc_prompt("build the importer?"));
        assert!(!is_sdlc_prompt("build the importer?  \n\t"));
        // Only the trimmed end counts.
        assert!(is_sdlc_prompt("build the importer? then ship it"));
    }

    #[test]
    fn leading_slash_marks_a_command() {
        assert!(!is_sdlc_prompt("/swarm add a /health endpoint"));
        assert!(!is_sdlc_prompt("  /build the importer"));
        assert!(is_sdlc_prompt("build the /health endpoint"));
    }

    #[test]
    fn leading_system_reminder_silences_the_prompt() {
        let note = "<system-reminder>implement the billing feature</system-reminder>";
        assert!(!is_sdlc_prompt(note));
        // The check is a case-sensitive startsWith, so an upper-case tag is ordinary text.
        assert!(is_sdlc_prompt(
            "<SYSTEM-REMINDER>implement the billing feature"
        ));
    }

    #[test]
    fn slash_uplift_and_slash_think_opt_out_case_insensitively() {
        assert!(!is_sdlc_prompt(
            "add a billing feature, then run /UPLIFT on it"
        ));
        assert!(!is_sdlc_prompt("add a billing feature /think"));
        // The token includes its slash: the bare word does not opt out.
        assert!(is_sdlc_prompt("add a billing feature and uplift it"));
    }

    #[test]
    fn explain_only_opts_out_only_as_the_exact_phrase() {
        assert!(!is_sdlc_prompt(
            "implement the billing module; EXPLAIN ONLY"
        ));
        assert!(is_sdlc_prompt("implement the billing module; explain-only"));
        assert!(is_sdlc_prompt(
            "implement the billing module; explain  only"
        ));
    }

    #[test]
    fn dispatch_marker_opts_out_case_insensitively() {
        assert!(!is_sdlc_prompt(
            "Run [AGENT-SWARM:DISPATCH] to implement the plan"
        ));
        assert!(is_sdlc_prompt(
            "Use the [agent-swarm: dispatch] tag to implement the plan"
        ));
    }

    #[test]
    fn trivial_edits_silence_the_prompt_as_whole_words() {
        assert!(!is_sdlc_prompt("fix the typo in README"));
        assert!(!is_sdlc_prompt("Rename getUser to fetchUser"));
        assert!(is_sdlc_prompt("fix the typography of the banner"));
        assert!(is_sdlc_prompt(
            "renames the API surface, then implement the shim"
        ));
        // "rename" counts only at the start of the prompt.
        assert!(is_sdlc_prompt(
            "implement the rename of getUser to fetchUser"
        ));
    }

    #[test]
    fn uplift_root_is_judged_by_its_original_not_its_wrapper() {
        // The wrapper says build; the user's own words are a question.
        let question = "<BUILD_PROMPT><ORIGINAL>why?</ORIGINAL><TASK>build</TASK></BUILD_PROMPT>";
        assert!(!is_sdlc_prompt(question));
        // The root name is ASCII case-insensitive.
        let lower_root = "<build_prompt><ORIGINAL>what is it?</ORIGINAL> build it</build_prompt>";
        assert!(!is_sdlc_prompt(lower_root));
        // The plugin's closing tag holds "/uplift"; the user's own words are SDLC and decide.
        let fallback = "<UPLIFTED_PROMPT>\n<ORIGINAL>implement the importer</ORIGINAL>\n\
                        </UPLIFTED_PROMPT>";
        assert!(is_sdlc_prompt(fallback));
    }

    #[test]
    fn original_entities_decode_in_ts_order_before_the_rules_apply() {
        let prompt = "<BUILD_PROMPT><ORIGINAL>a &lt;b&gt; &amp;lt; &quot;c&quot; &apos;d&apos;\
                      </ORIGINAL></BUILD_PROMPT>";
        assert_eq!(classified_text(prompt), "a <b> &lt; \"c\" 'd'");
        // The decoded "<system-reminder>" is the user's own text, so the leading-tag rule applies.
        let escaped = "<FIX_PROMPT><ORIGINAL>&lt;system-reminder&gt;implement the importer\
                       </ORIGINAL></FIX_PROMPT>";
        assert!(!is_sdlc_prompt(escaped));
    }

    #[test]
    fn amp_is_decoded_last_so_an_escaped_entity_stays_text() {
        let prompt = "<FIX_PROMPT><ORIGINAL>&amp;lt;system-reminder&gt;implement the importer\
                      </ORIGINAL></FIX_PROMPT>";
        assert_eq!(
            classified_text(prompt),
            "&lt;system-reminder>implement the importer"
        );
        assert!(is_sdlc_prompt(prompt));
    }

    #[test]
    fn only_a_whole_root_name_and_a_whole_original_opener_count() {
        // "<ultrathinks>" is not the "ultrathink" root: the whole prompt decides, and "build" wins.
        let not_root = "<ultrathinks><ORIGINAL>why?</ORIGINAL> build it</ultrathinks>";
        assert!(is_sdlc_prompt(not_root));
        // "<originals>" is not an ORIGINAL opener: no element, so the whole prompt decides.
        let not_opener = "<BUILD_PROMPT><originals>why?</originals> build it</BUILD_PROMPT>";
        assert!(is_sdlc_prompt(not_opener));
        // A non-ASCII letter after "<original" is not an identifier char: the element is extracted.
        assert_eq!(
            classified_text("<BUILD_PROMPT><originalé>x</original>"),
            "x"
        );
        // A plain prompt that quotes an ORIGINAL element is not an uplift XML.
        let quoted = "implement a parser for <ORIGINAL>what is a monad</ORIGINAL> tags";
        assert!(is_sdlc_prompt(quoted));
    }

    #[test]
    fn a_root_without_a_closed_element_is_judged_whole() {
        // No ORIGINAL: the whole prompt, untrimmed, is returned and judged.
        let bare = "  <BUILD_PROMPT><TASK>implement the importer</TASK></BUILD_PROMPT>  ";
        assert_eq!(classified_text(bare), bare);
        assert!(is_sdlc_prompt(bare));
        // The plugin's bare fallback closes with "</UPLIFTED_PROMPT>", which holds "/uplift".
        let bare_fallback = "<UPLIFTED_PROMPT><TASK>implement it</TASK>\
                             </UPLIFTED_PROMPT>";
        assert!(!is_sdlc_prompt(bare_fallback));
        // An opener that never closes: the whole prompt again.
        let unclosed = "<FIX_PROMPT><ORIGINAL>fix the login</FIX_PROMPT>";
        assert_eq!(classified_text(unclosed), unclosed);
    }

    #[test]
    fn plain_original_is_borrowed_and_keeps_its_whitespace() {
        let prompt = "<FIX_PROMPT>\n<ORIGINAL id=\"1\">  fix the login  </ORIGINAL>\n\
                      </FIX_PROMPT>";
        let text = classified_text(prompt);
        assert_eq!(text, "  fix the login  ");
        assert!(
            matches!(text, Cow::Borrowed(_)),
            "an entity-free element is borrowed"
        );
    }

    #[test]
    fn a_dotted_capital_i_in_the_original_does_not_move_the_closer() {
        let prompt = "<RESEARCH_PROMPT><ORIGINAL>Deploy the \u{130}stanbul app?</ORIGINAL>\
                      </RESEARCH_PROMPT>";
        assert_eq!(classified_text(prompt), "Deploy the \u{130}stanbul app?");
        assert!(!is_sdlc_prompt(prompt));
    }

    #[test]
    fn opt_out_tokens_match_the_full_lowercase_of_the_text() {
        // U+212A KELVIN SIGN lowercases to "k", so "/thinK" is "/think" to toLowerCase().
        assert!(!is_sdlc_prompt("add a billing feature /thin\u{212A}"));
    }

    #[test]
    fn a_run_of_skipped_openers_ends_without_an_element() {
        let prompt = format!("<BUILD_PROMPT>{}", "<originalx".repeat(20_000));
        assert_eq!(classified_text(&prompt), prompt.as_str());
    }

    #[test]
    fn sdlc_verbs_are_whole_ascii_words() {
        assert!(!is_sdlc_prompt("prefix the logs with the service name"));
        assert!(!is_sdlc_prompt("rebuilding the importer is a chore"));
        assert!(!is_sdlc_prompt("shipping notes for the review"));
        // "é" is not an ASCII word char, so "fixé" holds the whole word "fix".
        assert!(is_sdlc_prompt("Le code fixé hier doit être revu"));
    }

    #[test]
    fn multi_word_verbs_need_their_exact_spelling() {
        assert!(is_sdlc_prompt("set up CI for the monorepo"));
        assert!(is_sdlc_prompt("setup CI for the monorepo"));
        assert!(!is_sdlc_prompt("set  up CI for the monorepo"));
        assert!(is_sdlc_prompt("write test for the parser"));
        assert!(!is_sdlc_prompt("write testing notes for the parser"));
    }

    #[test]
    fn trimming_follows_javascript_whitespace() {
        // U+FEFF is trimmed, so the "/" leads and the prompt is a command.
        assert!(!is_sdlc_prompt("\u{FEFF}/build the importer"));
        // U+0085 is not trimmed, so the prompt does not start with "/".
        assert!(is_sdlc_prompt("\u{85}/build the importer"));
    }
}
