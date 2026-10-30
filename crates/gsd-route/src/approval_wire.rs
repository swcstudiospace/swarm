//! The wire form of the second-batch guest mode `approval` (Phase 14.5 pin P2): the gate facts of a transition to `APPROVED`.
//!
//! The host reads the task row, the verdict rows and the gate tasks inside its own write transaction (`gsd_taskwriter::approval`),
//! verifies each row's envelope with its own keys, and encodes what it read with [`encode_request`]. The pure logic is [`answer`]:
//! the host runs it on the facts it read, the guest runs it on the decoded request, and [`encode_outcome`] renders the answer so the
//! verified shadow (`gsd_adapter::sandbox_shadow`) can compare the two byte for byte. Nothing here reads a key, a clock or the
//! environment. Each row's stored envelope text travels in the request as `envelope_json`: its `sig` field is a signature, not
//! key material, and the guest parses the text's fields but verifies no signature. A row's signature check is the boolean
//! `sig_ok` the host computed, and `now` is a request field. No key value, key file path or environment value is carried.
//!
//! # Request
//!
//! ```text
//! {"mode": "approval", "now": <float>,
//!  "policy": {"require_key": <bool>, "real_key": <bool>, "dev_key": <bool>},
//!  "task": {"task_id": .., "correlation_id": .., "risk_class": .. | null, "notes": .. | null},
//!  "rows": [{"id": <int>, "task_id": .., "gate": .., "verdict": .. | null, "envelope_json": .. | null, "ts": <float>,
//!            "sig_ok": <bool>}],
//!  "issuers": [{"task_id": .., "state": ..}]}
//! ```
//!
//! The gate names are not a field: they are `notes.gates` or `GATES_BY_RISK[risk_class]`, which [`TaskFacts::from_row`] reads from
//! `task` exactly as the host does. `rows` are the verdict rows of the task at or after `notes.verdicts_since`; a row whose signature
//! the host did not verify (no current row of a required gate) carries `sig_ok: false`, and no answer depends on such a row. `issuers`
//! are the states of the gate tasks the passing current rows name.
//!
//! Every object holds exactly the keys above. A missing, extra or mistyped key, or text the guest cannot read, is `E-INPUT` naming the
//! field, never its value.
//!
//! # Reply
//!
//! ```text
//! {"missing": [{"gate": .., "outcome": ..}], "unaccepted": [.. gate names ..], "refusal": .. | null}
//! ```
//!
//! `missing` is `missing_gate_reasons` in required order; `unaccepted` is `unaccepted_gates`; `refusal` is the `fail-closed:` text
//! Python's `TaskStore.transition` raises for these facts (key policy first, then missing, then unaccepted), or `null` when the transition
//! may go on. A facts set outside what this module reproduces is `E-INPUT` in the guest and `Unsupported` on the host, so the host lets
//! Python answer.
//!
//! # Determinism
//!
//! Both directions render with [`to_python_dumps`] and read with [`parse_python_json`], which refuses what [`Json`] cannot hold
//! byte-exactly. Equal answers give equal bytes.

use gsd_codes::ErrorCode;

use crate::gate_facts::{
    missing_gate_reasons, unaccepted_gates, CheckOutcome, IssuerState, TaskFacts, Unsupported,
    VerdictFacts, VerdictRow,
};
use crate::pydumps::{parse_python_json, to_python_dumps};
use crate::{jstr, obj_get, Json, SeamError};

/// The `mode` of an approval request.
pub const MODE: &str = "approval";

const REQUEST_KEYS: [&str; 6] = ["mode", "now", "policy", "task", "rows", "issuers"];
const POLICY_KEYS: [&str; 3] = ["require_key", "real_key", "dev_key"];
const TASK_KEYS: [&str; 4] = ["task_id", "correlation_id", "risk_class", "notes"];
const ROW_KEYS: [&str; 7] = [
    "id",
    "task_id",
    "gate",
    "verdict",
    "envelope_json",
    "ts",
    "sig_ok",
];
const ISSUER_KEYS: [&str; 2] = ["task_id", "state"];

/// `check_approval`'s refusals, in its order: the key policy, then the missing gates, then the unaccepted ones.
const NO_KEY_REQUIRED: &str = "fail-closed: SWARM_REQUIRE_KEY=1 but no signing key configured";
const NO_KEY: &str = "fail-closed: no signing key configured";

/// The key policy of one call: the three booleans `check_approval` reads before any gate. The host copies them from its key material;
/// no key is in this type.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Policy {
    /// `SWARM_REQUIRE_KEY == "1"`.
    pub require_key: bool,
    /// `real_key_configured()`.
    pub real_key: bool,
    /// `insecure_dev_key() is not None`.
    pub dev_key: bool,
}

/// The columns of the task row the gate decision reads.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TaskColumns {
    /// `task_id`.
    pub task_id: String,
    /// `correlation_id`.
    pub correlation_id: String,
    /// `risk_class` (`NULL` is `None`).
    pub risk_class: Option<String>,
    /// `notes`, the stored text (`NULL` is `None`).
    pub notes: Option<String>,
}

impl TaskColumns {
    /// The gate facts of this row: `_gates_of`, `_verdicts_since` and `notes.dry_run`, read by [`TaskFacts::from_row`].
    ///
    /// # Errors
    ///
    /// [`Unsupported`] when the row is outside what [`TaskFacts::from_row`] reproduces.
    pub fn facts(&self) -> Result<TaskFacts, Unsupported> {
        TaskFacts::from_row(
            &self.task_id,
            &self.correlation_id,
            self.risk_class.as_deref(),
            self.notes.as_deref(),
        )
    }
}

/// Everything one APPROVED call reads inside its transaction, as the pure logic takes it.
#[derive(Debug, Clone, PartialEq)]
pub struct Inputs {
    /// The clock reading the expiry of a verdict is compared with.
    pub now: f64,
    /// The key policy of the call.
    pub policy: Policy,
    /// The task row.
    pub task: TaskColumns,
    /// The verdict rows of the task at or after its last rework, each with the host's signature answer.
    pub rows: Vec<VerdictFacts>,
    /// The state of each gate task a passing current row names.
    pub issuers: Vec<IssuerState>,
}

/// The answer of the approval logic for one call.
#[derive(Debug, Clone, PartialEq)]
pub struct Answer {
    /// `missing_gate_reasons`: the required gates without a verified, current, passing verdict, with the reason, in required order.
    pub missing: Vec<(String, CheckOutcome)>,
    /// `unaccepted_gates`: the passing gates whose issuer is not an accepted state, in required order.
    pub unaccepted: Vec<String>,
    /// The `fail-closed:` refusal for these facts, `None` when the transition may go on.
    pub refusal: Option<String>,
}

/// The approval answer for `inputs`: the same function the host and the guest both run.
///
/// # Errors
///
/// [`Unsupported`] when the facts are outside what this module reproduces (see the module documentation). The host then lets Python
/// answer; the guest refuses the request.
pub fn answer(inputs: &Inputs) -> Result<Answer, Unsupported> {
    let task = inputs.task.facts()?;
    let missing = missing_gate_reasons(&task, &inputs.rows, inputs.now)?;
    let unaccepted = unaccepted_gates(&task, &inputs.rows, &missing, &inputs.issuers)?;
    let refusal = refusal(inputs.policy, &task.task_id, &missing, &unaccepted)?;
    Ok(Answer {
        missing,
        unaccepted,
        refusal,
    })
}

/// `check_approval` of `transition_plan`, for the facts of one call: the key policy, the missing gates, then the unaccepted gates.
///
/// The unaccepted names are sorted and de-duplicated as Python's `sorted` of the set does; the names of both lists must have a plain
/// `repr`, or the message cannot be reproduced.
fn refusal(
    policy: Policy,
    task_id: &str,
    missing: &[(String, CheckOutcome)],
    unaccepted: &[String],
) -> Result<Option<String>, Unsupported> {
    if policy.require_key && !policy.real_key {
        return Ok(Some(NO_KEY_REQUIRED.to_string()));
    }
    if !policy.real_key && !policy.dev_key {
        return Ok(Some(NO_KEY.to_string()));
    }
    if !missing.is_empty() {
        let names: Vec<String> = missing.iter().map(|(gate, _)| gate.clone()).collect();
        return Ok(Some(format!(
            "fail-closed: {task_id} lacks passing gates {}",
            repr_names(&names)?
        )));
    }
    let mut unaccepted = unaccepted.to_vec();
    unaccepted.sort();
    unaccepted.dedup();
    if unaccepted.is_empty() {
        return Ok(None);
    }
    Ok(Some(format!(
        "fail-closed: {task_id} has unaccepted gates {}",
        repr_names(&unaccepted)?
    )))
}

/// Python's `repr` of a list of gate names, for names whose `repr` is plain: printable ASCII without a quote or a backslash.
fn repr_names(names: &[String]) -> Result<String, Unsupported> {
    let plain = |name: &String| {
        name.chars()
            .all(|ch| (' '..='~').contains(&ch) && ch != '\'' && ch != '\\')
    };
    if !names.iter().all(plain) {
        return Err(Unsupported("a gate name needs an escaped repr"));
    }
    let quoted: Vec<String> = names.iter().map(|name| format!("'{name}'")).collect();
    Ok(format!("[{}]", quoted.join(", ")))
}

fn entry(key: &str, value: Json) -> (String, Json) {
    (key.to_string(), value)
}

fn object(entries: Vec<(String, Json)>) -> Json {
    Json::Obj(entries)
}

fn opt_text(value: Option<&str>) -> Json {
    value.map_or(Json::Null, jstr)
}

fn row_json(facts: &VerdictFacts) -> Json {
    let row = &facts.row;
    object(vec![
        entry("id", Json::Int(row.id)),
        entry("task_id", jstr(&row.task_id)),
        entry("gate", jstr(&row.gate)),
        entry("verdict", opt_text(row.verdict.as_deref())),
        entry("envelope_json", opt_text(row.envelope_json.as_deref())),
        entry("ts", Json::Float(row.ts)),
        entry("sig_ok", Json::Bool(facts.sig_ok)),
    ])
}

/// Renders one approval request for the guest, byte for byte what [`decode_request`] reads back.
pub fn encode_request(inputs: &Inputs) -> String {
    let policy = inputs.policy;
    let task = &inputs.task;
    to_python_dumps(&object(vec![
        entry("mode", jstr(MODE)),
        entry("now", Json::Float(inputs.now)),
        entry(
            "policy",
            object(vec![
                entry("require_key", Json::Bool(policy.require_key)),
                entry("real_key", Json::Bool(policy.real_key)),
                entry("dev_key", Json::Bool(policy.dev_key)),
            ]),
        ),
        entry(
            "task",
            object(vec![
                entry("task_id", jstr(&task.task_id)),
                entry("correlation_id", jstr(&task.correlation_id)),
                entry("risk_class", opt_text(task.risk_class.as_deref())),
                entry("notes", opt_text(task.notes.as_deref())),
            ]),
        ),
        entry(
            "rows",
            Json::Arr(inputs.rows.iter().map(row_json).collect()),
        ),
        entry(
            "issuers",
            Json::Arr(
                inputs
                    .issuers
                    .iter()
                    .map(|issuer| {
                        object(vec![
                            entry("task_id", jstr(&issuer.task_id)),
                            entry("state", jstr(&issuer.state)),
                        ])
                    })
                    .collect(),
            ),
        ),
    ]))
}

/// Renders the answer of one call, the bytes the verified shadow compares against the guest's reply.
pub fn encode_outcome(answer: &Answer) -> String {
    let missing = Json::Arr(
        answer
            .missing
            .iter()
            .map(|(gate, outcome)| {
                object(vec![
                    entry("gate", jstr(gate)),
                    entry("outcome", jstr(outcome.as_str())),
                ])
            })
            .collect(),
    );
    let unaccepted = Json::Arr(answer.unaccepted.iter().map(|gate| jstr(gate)).collect());
    to_python_dumps(&object(vec![
        entry("missing", missing),
        entry("unaccepted", unaccepted),
        entry("refusal", opt_text(answer.refusal.as_deref())),
    ]))
}

/// A shape problem, as the description of the field at fault. The readers turn it into `E-INPUT`.
type Shape<T> = Result<T, String>;

fn bad(path: &str) -> String {
    format!("field {path} is missing or has the wrong type")
}

/// The fields of the object `value`, which must hold exactly `keys`.
fn exact<'a>(value: &'a Json, path: &str, keys: &[&str]) -> Shape<&'a [(String, Json)]> {
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

fn opt_text_of(fields: &[(String, Json)], scope: &str, key: &str) -> Shape<Option<String>> {
    match member(fields, scope, key)? {
        Json::Str(text) => Ok(Some(text.clone())),
        Json::Null => Ok(None),
        _ => Err(bad(&format!("{scope}.{key}"))),
    }
}

fn flag(fields: &[(String, Json)], scope: &str, key: &str) -> Shape<bool> {
    match member(fields, scope, key)? {
        Json::Bool(flag) => Ok(*flag),
        _ => Err(bad(&format!("{scope}.{key}"))),
    }
}

/// A JSON float: the only form [`encode_request`] writes for a number that Python reads as a float.
fn float(fields: &[(String, Json)], scope: &str, key: &str) -> Shape<f64> {
    match member(fields, scope, key)? {
        Json::Float(number) => Ok(*number),
        _ => Err(bad(&format!("{scope}.{key}"))),
    }
}

fn int(fields: &[(String, Json)], scope: &str, key: &str) -> Shape<i64> {
    match member(fields, scope, key)? {
        Json::Int(number) => Ok(*number),
        _ => Err(bad(&format!("{scope}.{key}"))),
    }
}

fn items<'a>(fields: &'a [(String, Json)], scope: &str, key: &str) -> Shape<&'a [Json]> {
    match member(fields, scope, key)? {
        Json::Arr(items) => Ok(items),
        _ => Err(bad(&format!("{scope}.{key}"))),
    }
}

fn row_of(value: &Json) -> Shape<VerdictFacts> {
    let fields = exact(value, "rows[]", &ROW_KEYS)?;
    let row = VerdictRow {
        id: int(fields, "rows[]", "id")?,
        task_id: text(fields, "rows[]", "task_id")?.to_string(),
        gate: text(fields, "rows[]", "gate")?.to_string(),
        verdict: opt_text_of(fields, "rows[]", "verdict")?,
        envelope_json: opt_text_of(fields, "rows[]", "envelope_json")?,
        ts: float(fields, "rows[]", "ts")?,
    };
    Ok(VerdictFacts::new(row, flag(fields, "rows[]", "sig_ok")?))
}

fn issuer_of(value: &Json) -> Shape<IssuerState> {
    let fields = exact(value, "issuers[]", &ISSUER_KEYS)?;
    Ok(IssuerState {
        task_id: text(fields, "issuers[]", "task_id")?.to_string(),
        state: text(fields, "issuers[]", "state")?.to_string(),
    })
}

fn inputs_of(value: &Json) -> Shape<Inputs> {
    let top = exact(value, "request", &REQUEST_KEYS)?;
    if text(top, "request", "mode")? != MODE {
        return Err(bad("mode"));
    }
    let policy = exact(member(top, "request", "policy")?, "policy", &POLICY_KEYS)?;
    let task = exact(member(top, "request", "task")?, "task", &TASK_KEYS)?;
    let rows = items(top, "request", "rows")?
        .iter()
        .map(row_of)
        .collect::<Shape<Vec<_>>>()?;
    let issuers = items(top, "request", "issuers")?
        .iter()
        .map(issuer_of)
        .collect::<Shape<Vec<_>>>()?;
    Ok(Inputs {
        now: float(top, "request", "now")?,
        policy: Policy {
            require_key: flag(policy, "policy", "require_key")?,
            real_key: flag(policy, "policy", "real_key")?,
            dev_key: flag(policy, "policy", "dev_key")?,
        },
        task: TaskColumns {
            task_id: text(task, "task", "task_id")?.to_string(),
            correlation_id: text(task, "task", "correlation_id")?.to_string(),
            risk_class: opt_text_of(task, "task", "risk_class")?,
            notes: opt_text_of(task, "task", "notes")?,
        },
        rows,
        issuers,
    })
}

fn input(problem: String) -> SeamError {
    SeamError::new(ErrorCode::Input, problem)
}

/// Reads one request: the text of [`encode_request`]. Text that is not JSON or holds what [`Json`] cannot carry is `E-DEP` (the refusal
/// of [`parse_python_json`]); a shape problem is `E-INPUT` naming the field.
pub fn decode_request(text: &str) -> Result<Inputs, SeamError> {
    let value = parse_python_json(text)?;
    inputs_of(&value).map_err(input)
}

/// The guest entry of the `approval` mode: UTF-8 request bytes in, the encoded outcome out.
///
/// # Errors
///
/// `E-INPUT` for a request that is not UTF-8, not a well-formed approval request, or whose facts are outside what this module
/// reproduces.
pub fn invoke(input_bytes: &[u8]) -> Result<Vec<u8>, SeamError> {
    let text = std::str::from_utf8(input_bytes)
        .map_err(|_| SeamError::new(ErrorCode::Input, "request is not valid UTF-8"))?;
    let inputs = decode_request(text)?;
    let reply = answer(&inputs)
        .map_err(|Unsupported(why)| SeamError::new(ErrorCode::Input, format!("approval: {why}")))?;
    Ok(encode_outcome(&reply).into_bytes())
}
