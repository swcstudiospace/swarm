//! The wire form of the guest `catalog` mode (Phase 14.5, pin P2): the pure `swarm_tool catalog` of [`crate::catalog`] as bytes in and
//! bytes out.
//!
//! No host route serves `swarm_tool` (the sandbox engine answers `no_route`), so no host calls this module yet. A route would need
//! three host-side functions: [`encode_request`] for the request, [`answer`] for the decision, and [`encode_outcome`] for the bytes the
//! guest must reproduce. The guest runs [`invoke`], which is [`answer`] on the decoded request.
//!
//! # Request
//!
//! ```text
//! {"mode": "catalog",
//!  "args": {"json": <bool>, "dry_run": <bool>, "caller": <text|null>, "kind": "tools" | "skills" | "all", "query": <text|null>,
//!           "task_id": <text|null>, "correlation_id": <text|null>},
//!  "registry": {"agents": [{"id", "slug"}],
//!               "tools": [{"name", "purpose", "side_effect_tier", "timeout_s", "permitted_callers"}]},
//!  "skills": [{"name", "description", "owner", "tools": [<text>]}]}
//! ```
//!
//! Every object holds exactly the keys above. A missing, extra or mistyped key is `E-INPUT` and the message names the field, never its
//! value. The registry values (`id`, `slug`, `name`, `purpose`, ...) are any JSON value, as the host reads them: the catalog judges
//! their shape and declines what it cannot print, so the wire does not.
//!
//! # Reply
//!
//! ```text
//! {"reply": {"exit_code": <int>, "stdout": <text>}}
//! {"not_started": <text>}
//! ```
//!
//! `reply` is the [`Reply`] of a decided call, a refusal of the command included (exit code 2, as the Python script prints it).
//! `not_started` is a decline: Python answers, and the text names its class before the first colon.
//!
//! # Size
//!
//! The write guest's reply cap (`WRITE_REPLY_CAP_BYTES` of `gsd-adapter`, 1 MiB) applies to the encoded reply, not to `stdout`. The
//! reply is JSON, so every `"` and newline of `stdout` takes two bytes on the wire, and a `stdout` of nearly 1 MiB can encode to more
//! than the cap. [`answer`] therefore declines a reply whose encoded form is longer than [`MAX_WIRE_REPLY_BYTES`], with the
//! `reply-too-large` class. Both sides call [`answer`]: the guest never returns bytes the cap refuses, and the host never returns an
//! answer the guest cannot reproduce.
//!
//! # Limits
//!
//! A registry value that is a non-finite float (`1e400` reads as infinity) is printed as `Infinity`, which
//! [`parse_python_json`] refuses. The guest then fails with `E-DEP` and the host answer stands (a recorded fault), so no wrong answer
//! is given; a route would decline before the shadow for such input.
//!
//! # Determinism
//!
//! Both directions render with [`to_python_dumps`] (keys in the order written) and read with [`parse_python_json`]. A reply is a pure
//! function of its request, so equal requests give equal bytes.

use gsd_codes::ErrorCode;

use crate::catalog::{
    catalog, CatalogArgs, Kind, NotStarted, Registry, RegistryAgent, RegistryTool, SkillRow,
};
use crate::pydumps::{parse_python_json, to_python_dumps};
use crate::{jstr, obj_get, Fields, Json, Reply, SeamError};

/// The request `mode` string of this mode.
pub const MODE: &str = "catalog";

/// The largest encoded reply the wire carries: the write guest's reply cap, `WRITE_REPLY_CAP_BYTES` of `gsd-adapter`. A fault test
/// asserts the two are equal.
pub const MAX_WIRE_REPLY_BYTES: usize = 1 << 20;

const REQUEST_KEYS: [&str; 4] = ["mode", "args", "registry", "skills"];
const ARGS_KEYS: [&str; 7] = [
    "json",
    "dry_run",
    "caller",
    "kind",
    "query",
    "task_id",
    "correlation_id",
];
const REGISTRY_KEYS: [&str; 2] = ["agents", "tools"];
const AGENT_KEYS: [&str; 2] = ["id", "slug"];
const TOOL_KEYS: [&str; 5] = [
    "name",
    "purpose",
    "side_effect_tier",
    "timeout_s",
    "permitted_callers",
];
const SKILL_KEYS: [&str; 4] = ["name", "description", "owner", "tools"];
const REPLY_KEYS: [&str; 2] = ["exit_code", "stdout"];
const REPLY: &str = "reply";
const NOT_STARTED: &str = "not_started";

/// The class of the decline of a reply too long for the wire. It is one of the classes of [`crate::catalog`].
const TOO_LARGE: &str =
    "reply-too-large: the encoded reply is longer than the write guest's reply cap";

fn entry(key: &str, value: Json) -> (String, Json) {
    (key.to_string(), value)
}

fn object(entries: Vec<(String, Json)>) -> Json {
    Json::Obj(entries)
}

fn optional(text: &Option<String>) -> Json {
    text.as_deref().map_or(Json::Null, jstr)
}

fn kind_name(kind: Kind) -> &'static str {
    match kind {
        Kind::Tools => "tools",
        Kind::Skills => "skills",
        Kind::All => "all",
    }
}

/// Renders one catalog request for the guest, byte for byte what [`decode_request`] reads back.
pub fn encode_request(args: &CatalogArgs, registry: &Registry, skills: &[SkillRow]) -> String {
    let args = object(vec![
        entry("json", Json::Bool(args.json)),
        entry("dry_run", Json::Bool(args.dry_run)),
        entry("caller", optional(&args.caller)),
        entry("kind", jstr(kind_name(args.kind))),
        entry("query", optional(&args.query)),
        entry("task_id", optional(&args.task_id)),
        entry("correlation_id", optional(&args.correlation_id)),
    ]);
    let agents = registry
        .agents
        .iter()
        .map(|agent| {
            object(vec![
                entry("id", agent.id.clone()),
                entry("slug", agent.slug.clone()),
            ])
        })
        .collect();
    let tools = registry
        .tools
        .iter()
        .map(|tool| {
            object(vec![
                entry("name", tool.name.clone()),
                entry("purpose", tool.purpose.clone()),
                entry("side_effect_tier", tool.side_effect_tier.clone()),
                entry("timeout_s", tool.timeout_s.clone()),
                entry("permitted_callers", tool.permitted_callers.clone()),
            ])
        })
        .collect();
    let skills = skills
        .iter()
        .map(|skill| {
            object(vec![
                entry("name", jstr(&skill.name)),
                entry("description", jstr(&skill.description)),
                entry("owner", jstr(&skill.owner)),
                entry(
                    "tools",
                    Json::Arr(skill.tools.iter().map(|tool| jstr(tool)).collect()),
                ),
            ])
        })
        .collect();
    to_python_dumps(&object(vec![
        entry("mode", jstr(MODE)),
        entry("args", args),
        entry(
            "registry",
            object(vec![
                entry("agents", Json::Arr(agents)),
                entry("tools", Json::Arr(tools)),
            ]),
        ),
        entry("skills", Json::Arr(skills)),
    ]))
}

/// The decision of one call, as the guest and the host both compute it: [`catalog`], then the wire size check of the module
/// documentation. A refusal of the command is an answer (`Ok` with exit code 2); a decline is `Err`.
pub fn answer(
    args: &CatalogArgs,
    registry: &Registry,
    skills: &[SkillRow],
) -> Result<Reply, NotStarted> {
    let reply = catalog(args, registry, skills)?;
    if reply_text(&reply).len() > MAX_WIRE_REPLY_BYTES {
        return Err(NotStarted(TOO_LARGE.to_string()));
    }
    Ok(reply)
}

fn reply_text(reply: &Reply) -> String {
    to_python_dumps(&object(vec![entry(
        REPLY,
        object(vec![
            entry("exit_code", Json::Int(i64::from(reply.exit_code))),
            entry("stdout", jstr(&reply.stdout)),
        ]),
    )]))
}

/// Renders the answer of one call: `{"reply": ..}` or `{"not_started": ..}`.
pub fn encode_outcome(outcome: &Result<Reply, NotStarted>) -> String {
    match outcome {
        Ok(reply) => reply_text(reply),
        Err(NotStarted(why)) => to_python_dumps(&object(vec![entry(NOT_STARTED, jstr(why))])),
    }
}

/// A shape problem, as the description of the field at fault. The public readers turn it into `E-INPUT`.
type Shape<T> = Result<T, String>;

fn bad(path: &str) -> String {
    format!("field {path} is missing or has the wrong type")
}

/// The fields of the object `value`, which must hold exactly `keys`.
fn exact<'a>(value: &'a Json, path: &str, keys: &[&str]) -> Shape<&'a Fields> {
    match value {
        Json::Obj(fields)
            if fields.len() == keys.len()
                && keys.iter().all(|key| obj_get(fields, key).is_some()) =>
        {
            Ok(fields)
        }
        _ => Err(bad(path)),
    }
}

fn member<'a>(fields: &'a [(String, Json)], scope: &str, key: &str) -> Shape<&'a Json> {
    obj_get(fields, key).ok_or_else(|| bad(&format!("{scope}.{key}")))
}

fn text<'a>(fields: &'a [(String, Json)], scope: &str, key: &str) -> Shape<&'a str> {
    match member(fields, scope, key)? {
        Json::Str(text) => Ok(text),
        _ => Err(bad(&format!("{scope}.{key}"))),
    }
}

fn optional_text(fields: &[(String, Json)], scope: &str, key: &str) -> Shape<Option<String>> {
    match member(fields, scope, key)? {
        Json::Null => Ok(None),
        Json::Str(text) => Ok(Some(text.clone())),
        _ => Err(bad(&format!("{scope}.{key}"))),
    }
}

fn flag(fields: &[(String, Json)], scope: &str, key: &str) -> Shape<bool> {
    match member(fields, scope, key)? {
        Json::Bool(flag) => Ok(*flag),
        _ => Err(bad(&format!("{scope}.{key}"))),
    }
}

fn items<'a>(fields: &'a [(String, Json)], scope: &str, key: &str) -> Shape<&'a [Json]> {
    match member(fields, scope, key)? {
        Json::Arr(items) => Ok(items),
        _ => Err(bad(&format!("{scope}.{key}"))),
    }
}

fn texts(fields: &[(String, Json)], scope: &str, key: &str) -> Shape<Vec<String>> {
    items(fields, scope, key)?
        .iter()
        .map(|item| match item {
            Json::Str(text) => Ok(text.clone()),
            _ => Err(bad(&format!("{scope}.{key}"))),
        })
        .collect()
}

fn input(problem: String) -> SeamError {
    SeamError::new(ErrorCode::Input, problem)
}

fn args_of(value: &Json) -> Shape<CatalogArgs> {
    let fields = exact(value, "args", &ARGS_KEYS)?;
    let kind = match text(fields, "args", "kind")? {
        "tools" => Kind::Tools,
        "skills" => Kind::Skills,
        "all" => Kind::All,
        _ => return Err(bad("args.kind")),
    };
    Ok(CatalogArgs {
        json: flag(fields, "args", "json")?,
        dry_run: flag(fields, "args", "dry_run")?,
        caller: optional_text(fields, "args", "caller")?,
        kind,
        query: optional_text(fields, "args", "query")?,
        task_id: optional_text(fields, "args", "task_id")?,
        correlation_id: optional_text(fields, "args", "correlation_id")?,
    })
}

fn agent_of(value: &Json) -> Shape<RegistryAgent> {
    let fields = exact(value, "registry.agents", &AGENT_KEYS)?;
    Ok(RegistryAgent {
        id: member(fields, "registry.agents", "id")?.clone(),
        slug: member(fields, "registry.agents", "slug")?.clone(),
    })
}

fn tool_of(value: &Json) -> Shape<RegistryTool> {
    let fields = exact(value, "registry.tools", &TOOL_KEYS)?;
    let get = |key: &str| member(fields, "registry.tools", key).cloned();
    Ok(RegistryTool {
        name: get("name")?,
        purpose: get("purpose")?,
        side_effect_tier: get("side_effect_tier")?,
        timeout_s: get("timeout_s")?,
        permitted_callers: get("permitted_callers")?,
    })
}

fn registry_of(value: &Json) -> Shape<Registry> {
    let fields = exact(value, "registry", &REGISTRY_KEYS)?;
    Ok(Registry {
        agents: items(fields, "registry", "agents")?
            .iter()
            .map(agent_of)
            .collect::<Shape<_>>()?,
        tools: items(fields, "registry", "tools")?
            .iter()
            .map(tool_of)
            .collect::<Shape<_>>()?,
    })
}

fn skill_of(value: &Json) -> Shape<SkillRow> {
    let fields = exact(value, "skills", &SKILL_KEYS)?;
    Ok(SkillRow {
        name: text(fields, "skills", "name")?.to_string(),
        description: text(fields, "skills", "description")?.to_string(),
        owner: text(fields, "skills", "owner")?.to_string(),
        tools: texts(fields, "skills", "tools")?,
    })
}

fn request_of(value: &Json) -> Shape<(CatalogArgs, Registry, Vec<SkillRow>)> {
    let top = exact(value, "request", &REQUEST_KEYS)?;
    if text(top, "request", "mode")? != MODE {
        return Err(bad("request.mode"));
    }
    let args = args_of(member(top, "request", "args")?)?;
    let registry = registry_of(member(top, "request", "registry")?)?;
    let skills = items(top, "request", "skills")?
        .iter()
        .map(skill_of)
        .collect::<Shape<_>>()?;
    Ok((args, registry, skills))
}

/// Reads one request value into what [`catalog`] takes. A shape problem is `E-INPUT`, naming the field.
pub fn decode_request(value: &Json) -> Result<(CatalogArgs, Registry, Vec<SkillRow>), SeamError> {
    request_of(value).map_err(input)
}

fn contract(problem: String) -> SeamError {
    SeamError::new(ErrorCode::Contract, format!("reply {problem}"))
}

fn outcome_of(value: &Json) -> Shape<Result<Reply, NotStarted>> {
    let Json::Obj(top) = value else {
        return Err(bad("reply"));
    };
    match top.as_slice() {
        [(key, body)] if key == REPLY => {
            let fields = exact(body, REPLY, &REPLY_KEYS)?;
            let exit_code = match member(fields, REPLY, "exit_code")? {
                Json::Int(code) => i32::try_from(*code).map_err(|_| bad("reply.exit_code"))?,
                _ => return Err(bad("reply.exit_code")),
            };
            Ok(Ok(Reply {
                exit_code,
                stdout: text(fields, REPLY, "stdout")?.to_string(),
            }))
        }
        [(key, why)] if key == NOT_STARTED => match why {
            Json::Str(why) => Ok(Err(NotStarted(why.clone()))),
            _ => Err(bad(NOT_STARTED)),
        },
        _ => Err(bad("reply or not_started")),
    }
}

/// Reads the guest's reply bytes into the decision it made. Anything but one of the two shapes of the module documentation is
/// `E-CONTRACT`; text that is not JSON, or holds what [`Json`] cannot carry, is `E-DEP` (see [`parse_python_json`]).
pub fn decode_outcome(bytes: &[u8]) -> Result<Result<Reply, NotStarted>, SeamError> {
    let text = std::str::from_utf8(bytes).map_err(|_| contract("is not valid UTF-8".into()))?;
    outcome_of(&parse_python_json(text)?).map_err(contract)
}

/// The guest entry of the `catalog` mode: UTF-8 request bytes in, encoded answer bytes out.
///
/// A request that is not UTF-8 or breaks the schema is `E-INPUT`; one holding what [`Json`] cannot carry is `E-DEP`. A request that
/// reads is always answered: a decline is a reply (see the module documentation).
pub fn invoke(input_bytes: &[u8]) -> Result<Vec<u8>, SeamError> {
    let text = std::str::from_utf8(input_bytes)
        .map_err(|_| SeamError::new(ErrorCode::Input, "request is not valid UTF-8"))?;
    let (args, registry, skills) = decode_request(&parse_python_json(text)?)?;
    Ok(encode_outcome(&answer(&args, &registry, &skills)).into_bytes())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::catalog::{parse_catalog_argv, MAX_REPLY_BYTES};
    use crate::parse_json;

    const VECTORS: &str = include_str!("../tests/vectors/catalog.json");

    fn doc() -> Json {
        parse_json(VECTORS).expect("the vector file parses")
    }

    fn get<'a>(value: &'a Json, key: &str) -> &'a Json {
        match value {
            Json::Obj(fields) => obj_get(fields, key).unwrap_or_else(|| panic!("no key {key}")),
            other => panic!("not an object looking for {key}: {other:?}"),
        }
    }

    fn text_of(value: &Json) -> &str {
        match value {
            Json::Str(text) => text,
            other => panic!("not text: {other:?}"),
        }
    }

    fn items_of(value: &Json) -> &[Json] {
        match value {
            Json::Arr(items) => items,
            other => panic!("not a list: {other:?}"),
        }
    }

    fn strings_of(value: &Json) -> Vec<String> {
        items_of(value)
            .iter()
            .map(|item| text_of(item).to_string())
            .collect()
    }

    /// The request of one vector case, with the slice the case names, and the host's answer to it.
    fn each_case(mut check: impl FnMut(&str, &CatalogArgs, &Registry, &[SkillRow], &Json)) {
        let document = doc();
        let registry = Registry::from_json(get(&document, "registry")).expect("real slice");
        let skills = SkillRow::from_json(get(&document, "skills")).expect("real skills");
        for case in items_of(get(&document, "cases")) {
            let name = text_of(get(case, "name"));
            let argv = strings_of(get(case, "argv"));
            let args = parse_catalog_argv(&argv)
                .unwrap_or_else(|| panic!("{name}: the argv is in the accepted subset"));
            match case {
                Json::Obj(fields) if obj_get(fields, "registry").is_some() => {
                    let own = Registry::from_json(get(case, "registry")).expect("inline slice");
                    let own_skills = SkillRow::from_json(get(case, "skills")).expect("inline");
                    check(name, &args, &own, &own_skills[..], case);
                }
                _ => check(name, &args, &registry, &skills, case),
            }
        }
    }

    #[test]
    fn every_vector_case_gives_the_same_bytes_on_both_sides_and_what_python_printed() {
        let mut checked = 0;
        each_case(|name, args, registry, skills, case| {
            let request = encode_request(args, registry, skills);
            let decoded = decode_request(&parse_python_json(&request).expect("request reads"))
                .unwrap_or_else(|error| panic!("{name}: {error:?}"));
            assert_eq!(
                decoded,
                (args.clone(), registry.clone(), skills.to_vec()),
                "{name}"
            );
            let host = answer(args, registry, skills);
            let guest =
                invoke(request.as_bytes()).unwrap_or_else(|error| panic!("{name}: {error:?}"));
            assert_eq!(
                String::from_utf8(guest.clone()).expect("utf-8"),
                encode_outcome(&host),
                "{name}: the guest and the host disagree"
            );
            let decoded = decode_outcome(&guest).expect("the guest reply reads");
            assert_eq!(decoded, host, "{name}");
            match text_of(get(case, "rust")) {
                "reply" => {
                    let expect = get(case, "expect");
                    let reply = decoded.unwrap_or_else(|why| panic!("{name}: declined {why}"));
                    let Json::Int(exit) = get(expect, "exit") else {
                        panic!("{name}: exit is not an integer")
                    };
                    assert_eq!(i64::from(reply.exit_code), *exit, "{name}");
                    assert_eq!(reply.stdout, text_of(get(expect, "stdout")), "{name}");
                }
                _ => {
                    let why = decoded.expect_err(name).0;
                    let class = text_of(get(case, "why"));
                    assert!(why.starts_with(&format!("{class}:")), "{name}: {why}");
                }
            }
            checked += 1;
        });
        assert!(checked > 500, "only {checked} cases");
    }

    #[test]
    fn the_strict_decode_names_the_field_and_never_the_value() {
        let document = doc();
        let registry = Registry::from_json(get(&document, "registry")).expect("real slice");
        let skills = SkillRow::from_json(get(&document, "skills")).expect("real skills");
        let args = CatalogArgs {
            json: true,
            dry_run: false,
            caller: Some("A01".to_string()),
            kind: Kind::Tools,
            query: None,
            task_id: None,
            correlation_id: None,
        };
        let good = encode_request(&args, &registry, &skills);
        let damage: [(&str, &str, &str); 5] = [
            (
                "\"kind\": \"tools\"",
                "\"kind\": \"bogus-value\"",
                "args.kind",
            ),
            ("\"dry_run\": false", "\"dry_run\": 0", "args.dry_run"),
            ("\"caller\": \"A01\"", "\"caller\": 1", "args.caller"),
            ("\"json\": true", "\"json\": true, \"extra\": 1", "args"),
            (
                "\"mode\": \"catalog\"",
                "\"mode\": \"transition\"",
                "request.mode",
            ),
        ];
        for (from, to, field) in damage {
            assert!(good.contains(from), "{from} is in the request");
            let damaged = good.replacen(from, to, 1);
            let error = invoke(damaged.as_bytes()).expect_err(&damaged[..40]);
            assert_eq!(error.code(), ErrorCode::Input);
            assert!(
                error.message().contains(field),
                "{field}: {}",
                error.message()
            );
            assert!(
                !error.message().contains("bogus-value"),
                "{}",
                error.message()
            );
        }
        let error = invoke(br#"{"mode": "catalog"}"#).expect_err("missing keys");
        assert_eq!(error.code(), ErrorCode::Input);
        let error = invoke(&[0xff, 0xfe]).expect_err("not utf-8");
        assert_eq!(error.code(), ErrorCode::Input);
    }

    #[test]
    fn a_reply_above_the_cap_is_the_same_refusal_on_both_sides() {
        // A purpose of quotes: each `"` is two bytes of `stdout` and four on the wire. Enough of them push `stdout` past the cap.
        let registry = Registry {
            agents: vec![RegistryAgent {
                id: jstr("A01"),
                slug: jstr("a01-x"),
            }],
            tools: vec![RegistryTool {
                name: jstr("t"),
                purpose: jstr(&"\"".repeat(MAX_REPLY_BYTES)),
                side_effect_tier: Json::Null,
                timeout_s: Json::Int(5),
                permitted_callers: Json::Arr(vec![jstr("A01")]),
            }],
        };
        let args = CatalogArgs {
            json: true,
            dry_run: false,
            caller: Some("A01".to_string()),
            kind: Kind::Tools,
            query: None,
            task_id: None,
            correlation_id: None,
        };
        let why = catalog(&args, &registry, &[])
            .expect_err("the stdout is over the cap")
            .0;
        assert!(why.starts_with("reply-too-large:"), "{why}");
        let request = encode_request(&args, &registry, &[]);
        let guest = invoke(request.as_bytes()).expect("the guest answers");
        assert_eq!(
            String::from_utf8(guest).expect("utf-8"),
            encode_outcome(&answer(&args, &registry, &[]))
        );
    }

    #[test]
    fn a_stdout_under_the_cap_whose_wire_form_is_over_it_is_declined_on_both_sides() {
        // `stdout` stays under MAX_REPLY_BYTES, the wire form of it does not.
        let quotes = 300_000;
        let registry = Registry {
            agents: vec![RegistryAgent {
                id: jstr("A01"),
                slug: jstr("a01-x"),
            }],
            tools: vec![RegistryTool {
                name: jstr("t"),
                purpose: jstr(&"\"".repeat(quotes)),
                side_effect_tier: Json::Null,
                timeout_s: Json::Int(5),
                permitted_callers: Json::Arr(vec![jstr("A01")]),
            }],
        };
        let args = CatalogArgs {
            json: true,
            dry_run: false,
            caller: Some("A01".to_string()),
            kind: Kind::Tools,
            query: None,
            task_id: None,
            correlation_id: None,
        };
        let reply = catalog(&args, &registry, &[]).expect("the stdout is under the cap");
        assert!(reply.stdout.len() <= MAX_REPLY_BYTES);
        assert!(reply_text(&reply).len() > MAX_WIRE_REPLY_BYTES);
        let host = answer(&args, &registry, &[]);
        let why = host.expect_err("the wire form is over the cap").0;
        assert!(why.starts_with("reply-too-large:"), "{why}");
        let guest = invoke(encode_request(&args, &registry, &[]).as_bytes()).expect("answers");
        assert_eq!(
            String::from_utf8(guest).expect("utf-8"),
            encode_outcome(&Err(NotStarted(why)))
        );
    }

    #[test]
    fn the_outcome_round_trips() {
        let reply = Reply {
            exit_code: 2,
            stdout: "[shared/swarm_tool] ERROR\n  error: \"x\" \u{e9} \u{1f600}\n".to_string(),
        };
        for outcome in [Ok(reply), Err(NotStarted("class: text".to_string()))] {
            let text = encode_outcome(&outcome);
            assert_eq!(decode_outcome(text.as_bytes()).expect("reads"), outcome);
        }
        assert_eq!(
            decode_outcome(br#"{"reply": {"exit_code": 0}}"#)
                .expect_err("a missing field")
                .code(),
            ErrorCode::Contract
        );
    }
}
