//! Pure `swarm_tool catalog` (`scripts/swarm_tool.py`): the tools and skills an agent may use, as the bytes Python prints and the exit status.
//!
//! [`catalog`] reads the facts of one call (the arguments, a slice of the registry, the skill rows) and returns what `AgentScript.main` of
//! `swarm_tool.py` prints and exits with for the `catalog` command. It opens no file, process or clock, draws no random number and reads no
//! environment. The host reads `agents.json` and `prompts/skills` and builds the slice; this module decides and renders.
//!
//! # Inventory of `scripts/swarm_tool.py` (150 lines), `swarm/tool_registry.py` and `swarm/skill_registry.py`
//!
//! ## Inputs
//!
//! * The arguments: the positional `command` (`catalog` or `call`, default `catalog`), `--caller`, `--kind` (`tools`, `skills`, `all`, default
//!   `all`), `--query`, and the options `AgentScript.parser` adds: `--json`, `--dry-run`, `--root`, `--task-id`, `--correlation-id`. `call` and
//!   its options (`--name`, `--input`, `--plan-mode`) are not this module's: [`parse_catalog_argv`] returns `None` for them. `--root` only
//!   places the run log, so it is read and dropped. The parser has `allow_abbrev=False`.
//! * `--task-id` and `--correlation-id` default to `SWARM_TASK_ID` and `SWARM_CORRELATION_ID` when the flag is absent (an empty variable is
//!   unset). The host applies that default; [`CatalogArgs`] carries the resolved values.
//! * The registry, `tool_registry.load_document()`: the file `SWARM_AGENTS_FILE` or the repository's `agents.json`, read as UTF-8 JSON. Only
//!   `agents[].{id, slug}` and, through `tool_registry.iter_entries`, the tool entries of every agent's `registered_tools` followed by the
//!   top-level `shared_tools` are read, and of each tool only `name`, `purpose`, `side_effect_tier` (`tool_registry.tool_tier`),
//!   `timeout_s` and `permitted_callers`. See "Slice" below.
//! * The skills, `skill_registry.skills_for(slug)` then `skills_for("shared")`: every `prompts/skills/<owner>/*.md`, parsed tolerantly
//!   (`load_skills`, sorted by owner directory then file name), kept when `owner_dir` equals the owner asked for. Only `kind` `skills` and
//!   `all` read them. A parse problem never raises; an unreadable directory (`OSError`, `ValueError`) is `skills_unavailable` (below).
//! * No clock, no random number, no environment beyond the two defaults above. `ctx.emit` appends a run-log record (a host side effect that
//!   `AgentScript.record_external_reply` replays for a reply another engine made); it never changes the bytes printed.
//!
//! ## Order of `AgentScript.main` for `catalog`
//!
//! 1. `--task-id` then `--correlation-id`, when present, go through `check_task_id` (`invalid task id: N characters, RULE; expected ...`).
//! 2. `run()`: with `--dry-run` and no `--caller` the canned reply `{status, dry_run, command, summary}`.
//! 3. `tool_registry.load_document()`; `OSError` or `JSONDecodeError` is `E-DEP` `registry unreadable: {exc}` (host: a decline).
//! 4. `resolve_caller`: no `--caller` or an empty one after `str.strip()` is `E-INPUT` `--caller is required: an agent id (A05) or slug
//!    (a05-backend)`; no dict agent whose `id` or `slug` equals the stripped text is `E-INPUT` `unknown caller {text!r}; use one of {ids} or
//!    an agent slug`, `ids` the `str(agent.get("id"))` of every dict agent joined with `", "`.
//! 5. `catalog()`: `kind` `tools` or `all`: each tool entry that is a dict and lists the caller's id in `permitted_callers` (`or []`) and
//!    matches the query becomes `{name, purpose, tier, timeout_s}`, sorted by `name` (stable). `skills` or `all`: the caller's own skills
//!    then the shared ones, matching rows become `{name, description, owner, tools}`, sorted by `name` (stable). `summary` is
//!    `"{id} ({slug}): N tools, M skills"` over the kinds present. A query matches when every word of `query.lower().split()` is a substring
//!    of `" ".join(texts).lower()`, texts being name and purpose (tools) or name and description (skills); no query or an empty one matches
//!    everything. `--dry-run` appends `dry_run: true` after `summary`.
//! 6. `run` returns `{"status": "ok", **result}`; `main` adds `agent: "shared"` and `script: "swarm_tool"` when absent, so they come last.
//!
//! ## Reply bytes and exit paths
//!
//! * `--json`: `json.dumps(reply, indent=2, default=str)` plus a newline, `ensure_ascii` on, keys in the order above. Exit 0.
//! * No `--json`: `[shared/swarm_tool] OK`, then the `summary` line. The lists are not printed. Exit 0.
//! * A `SwarmError` (steps 1 and 4): `{agent, script, status: "error", error: {code, message, details: {}, handling}}`, with `--json`; without it
//!   `[shared/swarm_tool] ERROR` and `  error: ` followed by the `error` object as `json.dumps(indent=2)`. Exit 2. Every refusal text of this
//!   module is one of: the `check_task_id` text, the `--caller is required` text, the `unknown caller` text. All carry the `E-INPUT` prefix.
//! * Any other exception is caught by `main`'s generic handler and printed with a traceback (exit 2): a decline here.
//! * `argparse` rejects (exit 2, usage on stderr) an unknown option, a bad `--kind`, a second positional, an abbreviation, a missing value:
//!   a decline here ([`parse_catalog_argv`] returns `None`).
//!
//! `skills_unavailable` (`skill sources unreadable: {exc}`) embeds the text of an operating-system error, so the host declines instead of
//! rendering it; the key is never produced here.
//!
//! # Size, measured before the design
//!
//! Measured on this checkout (`agents.json`, 30 agents, 394 tool entries, 25 of them shared). The heap figures come from a counting
//! allocator around `parse_json`, `Registry::from_json` and `catalog` in a throwaway test on x86-64 (8-byte pointers, so a bound for wasm32).
//!
//! | what | bytes |
//! |---|---|
//! | `agents.json` as text | 2 594 104 (2.47 MiB), 114 132 JSON nodes |
//! | heap of `parse_json(agents.json)` | 6 194 000 retained, 6 195 801 at its peak (with the 7 integers below replaced) |
//! | `agents.json` escaped into one request string | 2 848 724, 6 787 391 at its peak |
//! | slice of `agents.json` (`id`, `slug`, the five tool keys) as text | 98 781 (125 765 with `indent=2`) |
//! | every skill row (376 rows) as text | 154 450 (179 548 with `indent=2`) |
//! | heap of parsing both slices and decoding [`Registry`] and [`SkillRow`] | 1 191 121 |
//! | reply, `--json`, `--kind all`, one caller | 18 556 (A13) to 22 233 (A30) |
//! | heap peak of `catalog` itself for those replies | 104 026 and 116 891, about 5.3 times the reply |
//!
//! The guest memory cap is 16 MiB (OD-6, `MEMORY_BYTES` of `gsd-adapter`; `gsd-sandbox::wasmtime_limits::Limits::memory_bytes`). The whole
//! manifest would hold its text (2.5 MiB), the escaped request copy (2.7 MiB) and the tree (5.9 MiB) at once, about 11 of 16 MiB before the
//! reply and the module's own memory, and it grows with the manifest (its handler entries run past line 30 000). Worse, it cannot be read
//! at all: `parse_json` refuses it, because seven `input_schema` bounds of two tools (`agents[26]` and `agents[28]`, `maximum` of
//! `initial_shares` and others, `value_wei`, `balance_wei`) are integers beyond `i64`, which [`Json::Int`] cannot hold. The catalog needs
//! four percent of the text and none of those fields.
//!
//! **Design note: the function takes a host-prefiltered slice, not the manifest.** [`Registry`] holds, in file order, the dict agents
//! (`id`, `slug`) and the dict tool entries (`name`, `purpose`, `side_effect_tier`, `timeout_s`, `permitted_callers`; a key the entry lacks
//! is [`Json::Null`], which every Python read of it (`.get`) cannot tell from absent). [`Registry::from_json`] is the wire shape. The
//! slice of this checkout is 98 781 bytes, under one percent of the cap. The skill rows are the same kind of slice: [`SkillRow`], every
//! row `load_skills` returns, in its order (154 450 bytes for 376 rows); the function keeps the owners it needs. The host declines a
//! manifest whose `agents` is not a list; a non-list `registered_tools` or `shared_tools` is skipped by `iter_entries`, so it adds no entry.
//! The host builds the slice by `registry_slice` of `tests/parity/gen_catalog_vectors.py`, which the vectors and a fresh test hold equal to
//! the whole document for every caller, kind and a set of queries.
//!
//! **Size check: [`MAX_REPLY_BYTES`] (1 MiB).** The reply is built as a [`Json`] tree, rendered into one `String` and handed on in a wire
//! reply: the function peaks near 5.3 times the reply, and the wire holds the reply again (escaped) beside it. At the cap that is about
//! 5.3 MiB in the function plus two or three MiB of copies and the 1.2 MiB of input, well inside 16 MiB, and fifty times the largest real
//! reply. A reply longer than that is a decline, not a trap with `E-CAPACITY`. The check is strict: exactly the cap is answered.
//!
//! # Declines
//!
//! A decline is [`NotStarted`]: nothing was decided and Python answers. Each names its class before the first colon.
//!
//! * `argv-subset`: [`parse_catalog_argv`] returned `None` (see above). Not a [`NotStarted`], the parser has no error value.
//! * `non-ascii-repr`: an unknown caller that is not ASCII. `repr` prints a character escaped or raw by Unicode data Rust has no copy of.
//! * `non-ascii-query`: a query with a non-ASCII character. `str.lower()` and `str.split()` of it are Unicode data. (A non-ASCII *text* in
//!   the registry is fine: the words are ASCII, and only U+212A and U+0130 lower-case to an ASCII letter, in both languages.)
//! * `agent-shape`: the matched agent's `id` or `slug` is not text, or an `id` printed in the unknown-caller text is a container.
//! * `tool-shape`: a tool the caller may call whose `name` is not text, or a `permitted_callers` that is neither a list nor falsy (Python
//!   would test a substring or a key).
//! * `str-of-value`: a name or purpose that is not text, null, a boolean or an integer, when the query needs `str()` of it.
//! * `reply-too-large`: the rendered reply is longer than [`MAX_REPLY_BYTES`].
//! * `slice-shape`: [`Registry::from_json`] or [`SkillRow::from_json`] was given something else than the documented shape.
//!
//! The host never calls the function, and Python answers, for: `call`, an unreadable or unparsable registry, unreadable skill sources, a
//! manifest whose shape the slice cannot carry, an integer beyond `i64`, a lone surrogate, and anything Python's generic handler would print
//! with a traceback (for example an agent that matches but has no `id`).

use std::fmt;

use crate::{check_safe_id, finish, jstr, to_python_json, Json, Reply, SeamError};
use gsd_codes::ErrorCode;

/// The longest reply this module renders, in bytes. See "Size check" in the module documentation.
pub const MAX_REPLY_BYTES: usize = 1 << 20;

/// `agent` of every reply: `AgentScript("shared", "swarm_tool", ...)`.
const AGENT: &str = "shared";
/// `script` of every reply.
const SCRIPT: &str = "swarm_tool";
/// The owner directory of the skills every agent may use (`skill_registry.SHARED_OWNER`).
const SHARED_OWNER: &str = "shared";

/// Which lists `--kind` asks for.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    /// `tools`.
    Tools,
    /// `skills`.
    Skills,
    /// `all`, the default.
    All,
}

impl Kind {
    fn as_str(self) -> &'static str {
        match self {
            Kind::Tools => "tools",
            Kind::Skills => "skills",
            Kind::All => "all",
        }
    }
}

/// The arguments of one `catalog` call, as argparse would have produced them.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CatalogArgs {
    /// `--json`.
    pub json: bool,
    /// `--dry-run`.
    pub dry_run: bool,
    /// `--caller`.
    pub caller: Option<String>,
    /// `--kind`.
    pub kind: Kind,
    /// `--query`.
    pub query: Option<String>,
    /// `--task-id`, after the host applied `SWARM_TASK_ID` when the flag is absent.
    pub task_id: Option<String>,
    /// `--correlation-id`, after the host applied `SWARM_CORRELATION_ID` when the flag is absent.
    pub correlation_id: Option<String>,
}

/// Reads the argv of `swarm_tool.py` for the `catalog` command, or `None` when Python must answer (see "Declines").
///
/// Accepted: `catalog` at most once, in any position, and `--caller`, `--kind`, `--query`, `--task-id`, `--correlation-id`, `--root` with a
/// value (`--flag value` or `--flag=value`; a separate value that starts with `-` is not read), `--json`, `--dry-run`. A repeated flag keeps
/// its last value, as argparse does. Absent `--task-id` and `--correlation-id` stay `None`: the environment default is the host's.
pub fn parse_catalog_argv(argv: &[String]) -> Option<CatalogArgs> {
    let mut out = CatalogArgs {
        json: false,
        dry_run: false,
        caller: None,
        kind: Kind::All,
        query: None,
        task_id: None,
        correlation_id: None,
    };
    let mut command = false;
    let mut index = 0;
    while index < argv.len() {
        let token = argv[index].as_str();
        index += 1;
        if !token.starts_with('-') {
            if token != "catalog" || command {
                return None;
            }
            command = true;
            continue;
        }
        let (name, inline) = match token.split_once('=') {
            Some((head, value)) if head.starts_with("--") => (head, Some(value.to_string())),
            _ => (token, None),
        };
        if matches!(name, "--json" | "--dry-run") {
            if inline.is_some() {
                return None;
            }
            if name == "--json" {
                out.json = true;
            } else {
                out.dry_run = true;
            }
            continue;
        }
        if !matches!(
            name,
            "--caller" | "--kind" | "--query" | "--task-id" | "--correlation-id" | "--root"
        ) {
            return None;
        }
        let value = match inline {
            Some(value) => value,
            None => {
                let next = argv.get(index)?;
                if next.starts_with('-') {
                    return None;
                }
                index += 1;
                next.clone()
            }
        };
        match name {
            "--caller" => out.caller = Some(value),
            "--kind" => {
                out.kind = match value.as_str() {
                    "tools" => Kind::Tools,
                    "skills" => Kind::Skills,
                    "all" => Kind::All,
                    _ => return None,
                }
            }
            "--query" => out.query = Some(value),
            "--task-id" => out.task_id = Some(value),
            "--correlation-id" => out.correlation_id = Some(value),
            _ => {}
        }
    }
    Some(out)
}

/// One dict agent of `agents.json`.
#[derive(Debug, Clone, PartialEq)]
pub struct RegistryAgent {
    /// `agent.get("id")`; [`Json::Null`] when absent.
    pub id: Json,
    /// `agent.get("slug")`; [`Json::Null`] when absent.
    pub slug: Json,
}

/// One dict tool entry, in `tool_registry.iter_entries` order.
#[derive(Debug, Clone, PartialEq)]
pub struct RegistryTool {
    /// `tool.get("name")`; [`Json::Null`] when absent.
    pub name: Json,
    /// `tool.get("purpose")`.
    pub purpose: Json,
    /// `tool.get("side_effect_tier")`.
    pub side_effect_tier: Json,
    /// `tool.get("timeout_s")`.
    pub timeout_s: Json,
    /// `tool.get("permitted_callers")`.
    pub permitted_callers: Json,
}

/// The part of `agents.json` the catalog reads. See "Design note" in the module documentation.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Registry {
    /// The dict agents, in file order.
    pub agents: Vec<RegistryAgent>,
    /// The dict tool entries of every agent's `registered_tools`, then of `shared_tools`.
    pub tools: Vec<RegistryTool>,
}

/// One parsed skill source, as `skill_registry.load_skills` returns it, reduced to what the catalog prints.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SkillRow {
    /// `Skill.name`.
    pub name: String,
    /// `Skill.description`.
    pub description: String,
    /// `Skill.owner_dir`, the printed `owner`.
    pub owner: String,
    /// `Skill.tools`.
    pub tools: Vec<String>,
}

/// Why Rust gives no answer: Python answers. The text names the decline class before its first colon.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NotStarted(pub String);

impl fmt::Display for NotStarted {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "catalog not started: {}", self.0)
    }
}

impl std::error::Error for NotStarted {}

type Decided<T> = Result<T, NotStarted>;

fn not_started<T>(why: &str) -> Decided<T> {
    Err(NotStarted(why.to_string()))
}

fn slice_shape<T>() -> Decided<T> {
    not_started("slice-shape: the registry slice or skill rows are not the documented shape")
}

fn member<'a>(value: &'a Json, key: &str) -> Option<&'a Json> {
    match value {
        Json::Obj(fields) => crate::obj_get(fields, key),
        _ => None,
    }
}

impl Registry {
    /// Reads the wire shape `{"agents": [{"id", "slug"}], "tools": [{"name", "purpose", "side_effect_tier", "timeout_s", "permitted_callers"}]}`.
    /// A missing key of an entry is null.
    pub fn from_json(value: &Json) -> Decided<Registry> {
        let (Some(Json::Arr(agents)), Some(Json::Arr(tools))) =
            (member(value, "agents"), member(value, "tools"))
        else {
            return slice_shape();
        };
        let get = |entry: &Json, key: &str| member(entry, key).cloned().unwrap_or(Json::Null);
        let mut registry = Registry::default();
        for agent in agents {
            if !matches!(agent, Json::Obj(_)) {
                return slice_shape();
            }
            registry.agents.push(RegistryAgent {
                id: get(agent, "id"),
                slug: get(agent, "slug"),
            });
        }
        for tool in tools {
            if !matches!(tool, Json::Obj(_)) {
                return slice_shape();
            }
            registry.tools.push(RegistryTool {
                name: get(tool, "name"),
                purpose: get(tool, "purpose"),
                side_effect_tier: get(tool, "side_effect_tier"),
                timeout_s: get(tool, "timeout_s"),
                permitted_callers: get(tool, "permitted_callers"),
            });
        }
        Ok(registry)
    }
}

impl SkillRow {
    /// Reads the wire shape `[{"name", "description", "owner", "tools": [text]}]`, every member required.
    pub fn from_json(value: &Json) -> Decided<Vec<SkillRow>> {
        let Json::Arr(rows) = value else {
            return slice_shape();
        };
        rows.iter()
            .map(|row| {
                let text = |key: &str| match member(row, key) {
                    Some(Json::Str(text)) => Some(text.clone()),
                    _ => None,
                };
                let tools: Option<Vec<String>> = match member(row, "tools") {
                    Some(Json::Arr(items)) => items
                        .iter()
                        .map(|item| match item {
                            Json::Str(text) => Some(text.clone()),
                            _ => None,
                        })
                        .collect(),
                    _ => None,
                };
                match (text("name"), text("description"), text("owner"), tools) {
                    (Some(name), Some(description), Some(owner), Some(tools)) => Ok(SkillRow {
                        name,
                        description,
                        owner,
                        tools,
                    }),
                    _ => slice_shape(),
                }
            })
            .collect()
    }
}

/// Python's `str.isspace` for one character (what `strip()` and `split()` use): the Unicode `White_Space` set plus U+001C to U+001F.
fn py_isspace(ch: char) -> bool {
    ch.is_whitespace() || matches!(ch, '\u{1c}'..='\u{1f}')
}

/// Python's `repr` of a string, for ASCII text; `None` for anything else (printability is Unicode data).
fn repr_str(text: &str) -> Option<String> {
    if !text.is_ascii() {
        return None;
    }
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(text.len() + 2);
    out.push(quote);
    for ch in text.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '\t' => out.push_str("\\t"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if u32::from(c) < 0x20 || u32::from(c) == 0x7f => {
                out.push_str(&format!("\\x{:02x}", u32::from(c)));
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    Some(out)
}

/// `str(value)` for a text, null, boolean or integer; `None` for a float or a container, whose `repr` this module does not reproduce.
fn str_scalar(value: &Json) -> Option<String> {
    match value {
        Json::Str(text) => Some(text.clone()),
        Json::Null => Some("None".to_string()),
        Json::Bool(true) => Some("True".to_string()),
        Json::Bool(false) => Some("False".to_string()),
        Json::Int(number) => Some(number.to_string()),
        _ => None,
    }
}

/// Python truthiness of a value (`x or []`).
fn truthy(value: &Json) -> bool {
    match value {
        Json::Null => false,
        Json::Bool(flag) => *flag,
        Json::Int(number) => *number != 0,
        Json::Float(number) => *number != 0.0,
        Json::Str(text) => !text.is_empty(),
        Json::Arr(items) => !items.is_empty(),
        Json::Obj(fields) => !fields.is_empty(),
    }
}

/// `_matches`: every word of the query is in the lower-cased texts. The query is ASCII, checked by the caller.
struct Matcher {
    words: Vec<String>,
}

impl Matcher {
    fn new(query: Option<&str>) -> Decided<Matcher> {
        let Some(query) = query.filter(|query| !query.is_empty()) else {
            return Ok(Matcher { words: Vec::new() });
        };
        if !query.is_ascii() {
            return not_started("non-ascii-query: str.lower() and str.split() of a non-ASCII query are Unicode data");
        }
        Ok(Matcher {
            words: query
                .to_ascii_lowercase()
                .split(py_isspace)
                .filter(|word| !word.is_empty())
                .map(str::to_string)
                .collect(),
        })
    }

    fn matches(&self, first: &str, second: &str) -> bool {
        if self.words.is_empty() {
            return true;
        }
        let haystack = format!("{first} {second}").to_lowercase();
        self.words
            .iter()
            .all(|word| haystack.contains(word.as_str()))
    }
}

/// The reply of a refusal: `error` is the `SwarmError` shape. JSON mode prints the whole record, text mode the header and the object.
fn refusal(error: &SeamError, json: bool) -> Reply {
    let stdout = if json {
        let record = Json::Obj(vec![
            ("agent".to_string(), jstr(AGENT)),
            ("script".to_string(), jstr(SCRIPT)),
            ("status".to_string(), jstr("error")),
            ("error".to_string(), error.to_json()),
        ]);
        format!("{}\n", to_python_json(&record))
    } else {
        format!(
            "[{AGENT}/{SCRIPT}] ERROR\n  error: {}\n",
            to_python_json(&error.to_json())
        )
    };
    Reply {
        exit_code: 2,
        stdout,
    }
}

/// The reply of a success: `fields` is `run()`'s dict, which starts with `status`.
fn success(fields: Vec<(String, Json)>, summary: &str, json: bool) -> Reply {
    if json {
        return finish(fields, AGENT, SCRIPT);
    }
    Reply {
        exit_code: 0,
        stdout: format!("[{AGENT}/{SCRIPT}] OK\n{summary}\n"),
    }
}

/// What `swarm_tool.py catalog` prints and exits with, or the decline.
///
/// `registry` is only read when the call gets past the dry-run reply and the id checks; `skills` only for `kind` `skills` and `all`. See the
/// module documentation for the order, the texts and the declines.
pub fn catalog(args: &CatalogArgs, registry: &Registry, skills: &[SkillRow]) -> Decided<Reply> {
    let reply = decide(args, registry, skills)?;
    if reply.stdout.len() > MAX_REPLY_BYTES {
        return not_started("reply-too-large: the reply is longer than MAX_REPLY_BYTES");
    }
    Ok(reply)
}

fn decide(args: &CatalogArgs, registry: &Registry, skills: &[SkillRow]) -> Decided<Reply> {
    for (id, what) in [
        (&args.task_id, "task id"),
        (&args.correlation_id, "correlation id"),
    ] {
        if let Some(id) = id {
            if let Err(error) = check_safe_id(id, what) {
                return Ok(refusal(&error, args.json));
            }
        }
    }
    if args.dry_run && args.caller.is_none() {
        let summary = "dry-run: swarm_tool catalog";
        let fields = vec![
            ("status".to_string(), jstr("ok")),
            ("dry_run".to_string(), Json::Bool(true)),
            ("command".to_string(), jstr("catalog")),
            ("summary".to_string(), jstr(summary)),
        ];
        return Ok(success(fields, summary, args.json));
    }
    let wanted = args
        .caller
        .as_deref()
        .map(|text| text.trim_matches(py_isspace))
        .unwrap_or("");
    if wanted.is_empty() {
        let error = SeamError::new(
            ErrorCode::Input,
            "--caller is required: an agent id (A05) or slug (a05-backend)",
        );
        return Ok(refusal(&error, args.json));
    }
    let Some(agent) = registry.agents.iter().find(|agent| {
        matches!(&agent.id, Json::Str(id) if id == wanted)
            || matches!(&agent.slug, Json::Str(slug) if slug == wanted)
    }) else {
        let mut known = Vec::with_capacity(registry.agents.len());
        for agent in &registry.agents {
            match str_scalar(&agent.id) {
                Some(id) => known.push(id),
                None => {
                    return not_started(
                        "agent-shape: an agent id that is not text prints as a repr",
                    )
                }
            }
        }
        let Some(shown) = repr_str(wanted) else {
            return not_started(
                "non-ascii-repr: the unknown-caller message embeds the repr of a non-ASCII text",
            );
        };
        let error = SeamError::new(
            ErrorCode::Input,
            format!(
                "unknown caller {shown}; use one of {} or an agent slug",
                known.join(", ")
            ),
        );
        return Ok(refusal(&error, args.json));
    };
    let (Json::Str(id), Json::Str(slug)) = (&agent.id, &agent.slug) else {
        return not_started("agent-shape: the matched agent has an id or slug that is not text");
    };
    let matcher = Matcher::new(args.query.as_deref())?;
    let mut fields = vec![
        ("status".to_string(), jstr("ok")),
        ("caller".to_string(), jstr(id)),
        ("caller_slug".to_string(), jstr(slug)),
        ("kind".to_string(), jstr(args.kind.as_str())),
    ];
    let mut parts = Vec::with_capacity(2);
    if matches!(args.kind, Kind::Tools | Kind::All) {
        let rows = tool_rows(registry, id, &matcher)?;
        parts.push(format!("{} tools", rows.len()));
        fields.push(("tools".to_string(), Json::Arr(rows)));
    }
    if matches!(args.kind, Kind::Skills | Kind::All) {
        let rows = skill_rows(skills, slug, &matcher);
        parts.push(format!("{} skills", rows.len()));
        fields.push(("skills".to_string(), Json::Arr(rows)));
    }
    let summary = format!("{id} ({slug}): {}", parts.join(", "));
    fields.push(("summary".to_string(), jstr(&summary)));
    if args.dry_run {
        fields.push(("dry_run".to_string(), Json::Bool(true)));
    }
    Ok(success(fields, &summary, args.json))
}

/// The `tools` rows: the tools the caller may call that match, sorted by name.
fn tool_rows(registry: &Registry, caller: &str, matcher: &Matcher) -> Decided<Vec<Json>> {
    let mut rows: Vec<(&str, Json)> = Vec::new();
    for tool in &registry.tools {
        let permitted = match &tool.permitted_callers {
            Json::Arr(items) => items
                .iter()
                .any(|item| matches!(item, Json::Str(text) if text == caller)),
            other if !truthy(other) => false,
            _ => return not_started("tool-shape: permitted_callers is neither a list nor empty"),
        };
        if !permitted {
            continue;
        }
        let Json::Str(name) = &tool.name else {
            return not_started(
                "tool-shape: a tool the caller may call has a name that is not text",
            );
        };
        if !matcher.words.is_empty() {
            let (Some(name_text), Some(purpose_text)) =
                (str_scalar(&tool.name), str_scalar(&tool.purpose))
            else {
                return not_started(
                    "str-of-value: the query needs str() of a name or purpose that is not a scalar",
                );
            };
            if !matcher.matches(&name_text, &purpose_text) {
                continue;
            }
        }
        let tier = match &tool.side_effect_tier {
            Json::Str(tier) => tier.as_str(),
            _ => "READ",
        };
        rows.push((
            name,
            Json::Obj(vec![
                ("name".to_string(), jstr(name)),
                ("purpose".to_string(), tool.purpose.clone()),
                ("tier".to_string(), jstr(tier)),
                ("timeout_s".to_string(), tool.timeout_s.clone()),
            ]),
        ));
    }
    rows.sort_by(|left, right| left.0.cmp(right.0));
    Ok(rows.into_iter().map(|(_, row)| row).collect())
}

/// The `skills` rows: the caller's own skills, then the shared ones, that match, sorted by name.
fn skill_rows(skills: &[SkillRow], slug: &str, matcher: &Matcher) -> Vec<Json> {
    let mut rows: Vec<&SkillRow> = Vec::new();
    for owner in [slug, SHARED_OWNER] {
        rows.extend(skills.iter().filter(|skill| {
            skill.owner == owner && matcher.matches(&skill.name, &skill.description)
        }));
    }
    rows.sort_by(|left, right| left.name.cmp(&right.name));
    rows.into_iter()
        .map(|skill| {
            Json::Obj(vec![
                ("name".to_string(), jstr(&skill.name)),
                ("description".to_string(), jstr(&skill.description)),
                ("owner".to_string(), jstr(&skill.owner)),
                (
                    "tools".to_string(),
                    Json::Arr(skill.tools.iter().map(|tool| jstr(tool)).collect()),
                ),
            ])
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{obj_get, parse_json};

    const VECTORS: &str = include_str!("../tests/vectors/catalog.json");

    /// Every decline class of the module documentation that a function of this module returns, with what it covers. A case of the vectors
    /// may only name one of these.
    const DECLINES: [&str; 6] = [
        "non-ascii-repr",
        "non-ascii-query",
        "agent-shape",
        "tool-shape",
        "str-of-value",
        "reply-too-large",
    ];

    fn doc() -> Json {
        parse_json(VECTORS).expect("the vector file parses")
    }

    fn get<'a>(value: &'a Json, key: &str) -> &'a Json {
        match value {
            Json::Obj(fields) => obj_get(fields, key).unwrap_or_else(|| panic!("no key {key}")),
            other => panic!("not an object looking for {key}: {other:?}"),
        }
    }

    fn text(value: &Json) -> &str {
        match value {
            Json::Str(text) => text,
            other => panic!("not text: {other:?}"),
        }
    }

    fn flag(value: &Json) -> bool {
        match value {
            Json::Bool(flag) => *flag,
            other => panic!("not a bool: {other:?}"),
        }
    }

    fn items(value: &Json) -> &[Json] {
        match value {
            Json::Arr(items) => items,
            other => panic!("not a list: {other:?}"),
        }
    }

    fn strings(value: &Json) -> Vec<String> {
        items(value)
            .iter()
            .map(|item| text(item).to_string())
            .collect()
    }

    fn optional_text(value: &Json) -> Option<String> {
        match value {
            Json::Null => None,
            other => Some(text(other).to_string()),
        }
    }

    fn real_registry(document: &Json) -> Registry {
        Registry::from_json(get(document, "registry"))
            .expect("the real slice is the documented shape")
    }

    fn real_skills(document: &Json) -> Vec<SkillRow> {
        SkillRow::from_json(get(document, "skills"))
            .expect("the real skill rows are the documented shape")
    }

    fn args(caller: Option<&str>, kind: Kind, query: Option<&str>) -> CatalogArgs {
        CatalogArgs {
            json: true,
            dry_run: false,
            caller: caller.map(str::to_string),
            kind,
            query: query.map(str::to_string),
            task_id: None,
            correlation_id: None,
        }
    }

    fn tool(name: &str, purpose: &str, callers: &[&str]) -> RegistryTool {
        RegistryTool {
            name: jstr(name),
            purpose: jstr(purpose),
            side_effect_tier: Json::Null,
            timeout_s: Json::Int(5),
            permitted_callers: Json::Arr(callers.iter().map(|caller| jstr(caller)).collect()),
        }
    }

    fn small_registry(tools: Vec<RegistryTool>) -> Registry {
        Registry {
            agents: vec![RegistryAgent {
                id: jstr("A01"),
                slug: jstr("a01-x"),
            }],
            tools,
        }
    }

    fn declined(result: Decided<Reply>) -> String {
        match result {
            Err(NotStarted(why)) => why,
            Ok(reply) => panic!("expected a decline, got {reply:?}"),
        }
    }

    #[test]
    fn every_vector_case_prints_what_python_printed() {
        let document = doc();
        assert_eq!(
            text(get(&document, "schema")),
            "gsd-route.catalog-vectors.v1"
        );
        let registry = real_registry(&document);
        let skills = real_skills(&document);
        let cases = items(get(&document, "cases"));
        let (mut replies, mut declines) = (0, 0);
        for case in cases {
            let name = text(get(case, "name"));
            let argv = strings(get(case, "argv"));
            let parsed = parse_catalog_argv(&argv)
                .unwrap_or_else(|| panic!("{name}: the argv is in the accepted subset"));
            let (own_registry, own_skills);
            let (registry, skills) = match case {
                Json::Obj(fields) if obj_get(fields, "registry").is_some() => {
                    own_registry =
                        Registry::from_json(get(case, "registry")).expect("inline slice");
                    own_skills = SkillRow::from_json(get(case, "skills")).expect("inline skills");
                    (&own_registry, &own_skills[..])
                }
                _ => (&registry, &skills[..]),
            };
            let result = catalog(&parsed, registry, skills);
            match text(get(case, "rust")) {
                "reply" => {
                    let expect = get(case, "expect");
                    let reply =
                        result.unwrap_or_else(|decline| panic!("{name}: declined: {decline}"));
                    let Json::Int(exit) = get(expect, "exit") else {
                        panic!("{name}: exit is not an integer")
                    };
                    assert_eq!(i64::from(reply.exit_code), *exit, "{name}: exit status");
                    assert_eq!(reply.stdout, text(get(expect, "stdout")), "{name}: stdout");
                    replies += 1;
                }
                "not_started" => {
                    let why = declined(result);
                    let class = text(get(case, "why"));
                    assert!(
                        DECLINES.contains(&class),
                        "{name}: {class} is not a documented decline class"
                    );
                    assert!(
                        why.starts_with(&format!("{class}:")),
                        "{name}: declined as {why:?}, vector says {class}"
                    );
                    declines += 1;
                }
                other => panic!("{name}: unknown rust answer {other}"),
            }
        }
        assert!(
            replies > 500 && declines > 20,
            "corpus too small: {replies} replies, {declines} declines"
        );
    }

    #[test]
    fn the_real_corpus_covers_every_agent_and_kind() {
        let document = doc();
        let registry = real_registry(&document);
        assert_eq!(registry.agents.len(), 30);
        let names: Vec<&str> = items(get(&document, "cases"))
            .iter()
            .map(|case| text(get(case, "name")))
            .collect();
        for agent in &registry.agents {
            let Json::Str(id) = &agent.id else {
                panic!("agent id is not text")
            };
            for what in ["tools", "skills", "all", "text", "by-slug"] {
                assert!(
                    names.contains(&format!("real/{id}/{what}").as_str()),
                    "no case {id} {what}"
                );
            }
            for query in ["route", "review gate", "zzzz-no-such-word"] {
                assert!(
                    names.contains(&format!("real/{id}/query/{query}").as_str()),
                    "no case {id} query {query}"
                );
            }
        }
    }

    #[test]
    fn every_documented_decline_class_is_in_the_vectors_or_tested_here() {
        let document = doc();
        let named: Vec<&str> = items(get(&document, "cases"))
            .iter()
            .filter_map(|case| match get(case, "why") {
                Json::Str(class) => Some(class.as_str()),
                _ => None,
            })
            .collect();
        for class in DECLINES {
            if class != "reply-too-large" {
                assert!(named.contains(&class), "no vector declines as {class}");
            }
        }
    }

    #[test]
    fn the_constants_are_the_ones_python_was_run_with() {
        let document = doc();
        let constants = get(&document, "constants");
        assert_eq!(
            get(constants, "max_reply_bytes"),
            &Json::Int(i64::try_from(MAX_REPLY_BYTES).expect("fits"))
        );
        assert_eq!(text(get(constants, "agent")), AGENT);
        assert_eq!(text(get(constants, "script")), SCRIPT);
        let kinds = strings(get(constants, "kinds"));
        assert_eq!(
            kinds,
            [Kind::Tools, Kind::Skills, Kind::All].map(|kind| kind.as_str().to_string())
        );
    }

    #[test]
    fn the_argument_parser_accepts_what_python_reads_as_catalog_and_declines_the_rest() {
        let document = doc();
        let rows = items(get(get(&document, "units"), "argv"));
        assert!(rows.len() > 50);
        let mut accepted = 0;
        for row in rows {
            let argv = strings(get(row, "argv"));
            let parsed = parse_catalog_argv(&argv);
            if !flag(get(row, "subset")) {
                assert_eq!(parsed, None, "{argv:?} is left to Python");
                continue;
            }
            let expect = get(row, "args");
            let kind = match text(get(expect, "kind")) {
                "tools" => Kind::Tools,
                "skills" => Kind::Skills,
                _ => Kind::All,
            };
            let want = CatalogArgs {
                json: flag(get(expect, "json")),
                dry_run: flag(get(expect, "dry_run")),
                caller: optional_text(get(expect, "caller")),
                kind,
                query: optional_text(get(expect, "query")),
                task_id: optional_text(get(expect, "task_id")),
                correlation_id: optional_text(get(expect, "correlation_id")),
            };
            assert_eq!(parsed, Some(want), "{argv:?}");
            accepted += 1;
        }
        assert!(accepted > 20, "only {accepted} accepted lists");
    }

    #[test]
    fn the_query_match_is_python_s_lower_and_substring_over_the_vector_table() {
        let document = doc();
        let rows = items(get(get(&document, "units"), "lower_contains"));
        assert!(rows.len() > 300);
        for row in rows {
            let (first, second, word) = (
                text(get(row, "first")),
                text(get(row, "second")),
                text(get(row, "word")),
            );
            let matcher = Matcher::new(Some(word)).expect("an ASCII word");
            assert_eq!(
                matcher.matches(first, second),
                flag(get(row, "contains")),
                "{first:?} {second:?} {word:?}"
            );
        }
    }

    #[test]
    fn a_reply_of_exactly_the_cap_is_answered_and_one_byte_more_is_declined() {
        let query = Some("pad");
        let base_args = args(Some("A01"), Kind::Tools, query);
        let measure = |padding: usize| {
            let registry = small_registry(vec![tool(
                "t",
                &format!("pad{}", "x".repeat(padding)),
                &["A01"],
            )]);
            catalog(&base_args, &registry, &[])
        };
        let empty = measure(0).expect("a small reply").stdout.len();
        let fitting = measure(MAX_REPLY_BYTES - empty).expect("exactly the cap is answered");
        assert_eq!(fitting.stdout.len(), MAX_REPLY_BYTES);
        let why = declined(measure(MAX_REPLY_BYTES - empty + 1));
        assert!(why.starts_with("reply-too-large:"), "{why}");
    }

    #[test]
    fn a_refusal_is_not_held_to_the_cap_of_a_reply_it_does_not_carry() {
        let registry = small_registry(vec![tool("t", &"x".repeat(MAX_REPLY_BYTES), &["A01"])]);
        let reply = catalog(&args(Some("A99"), Kind::All, None), &registry, &[])
            .expect("a refusal is small");
        assert_eq!(reply.exit_code, 2);
        assert!(reply
            .stdout
            .contains("unknown caller 'A99'; use one of A01 or an agent slug"));
    }

    #[test]
    fn the_wire_shapes_refuse_anything_else() {
        for text in [
            r#"[]"#,
            r#"{"agents": [], "tools": 3}"#,
            r#"{"agents": [1], "tools": []}"#,
            r#"{"agents": [], "tools": ["x"]}"#,
            r#"{"agents": []}"#,
        ] {
            let why = Registry::from_json(&parse_json(text).expect("json"))
                .expect_err(text)
                .0;
            assert!(why.starts_with("slice-shape:"), "{why}");
        }
        let good = r#"{"agents": [{"id": "A01"}], "tools": [{"name": "t"}]}"#;
        let registry = Registry::from_json(&parse_json(good).expect("json")).expect("shape");
        assert_eq!(registry.agents[0].slug, Json::Null);
        assert_eq!(registry.tools[0].purpose, Json::Null);
        for text in [
            r#"{}"#,
            r#"[{"name": "a", "description": "b", "owner": "c"}]"#,
            r#"[{"name": "a", "description": "b", "owner": "c", "tools": [1]}]"#,
            r#"[{"name": 1, "description": "b", "owner": "c", "tools": []}]"#,
            r#"[3]"#,
        ] {
            let why = SkillRow::from_json(&parse_json(text).expect("json"))
                .expect_err(text)
                .0;
            assert!(why.starts_with("slice-shape:"), "{why}");
        }
    }

    #[test]
    fn the_skills_are_not_read_for_kind_tools_and_the_tools_are_not_read_for_kind_skills() {
        let broken = RegistryTool {
            name: Json::Int(5),
            ..tool("x", "p", &["A01"])
        };
        let registry = small_registry(vec![broken]);
        assert!(catalog(&args(Some("A01"), Kind::Skills, None), &registry, &[]).is_ok());
        let why = declined(catalog(
            &args(Some("A01"), Kind::Tools, None),
            &registry,
            &[],
        ));
        assert!(why.starts_with("tool-shape:"), "{why}");
    }
}
