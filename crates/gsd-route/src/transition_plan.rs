//! Pure decision of `TaskStore.transition` (`swarm/taskstore.py`) and of `check_manual_transition` (`swarm/results.py`), as a plan.
//!
//! [`decide_transition`] reads one task row and the host-supplied facts, and returns the plan the host writes: the columns to change,
//! the audit rows to insert and the observer effects to run after the commit. It opens no database and reads no clock (`now` is a
//! parameter), no environment and no key. The host applies the plan in one `BEGIN IMMEDIATE` transaction: one conditional
//! `UPDATE tasks SET <columns>, updated_at = now WHERE task_id = ? AND state = plan.from`, one `INSERT INTO transitions` per audit row
//! with `ts = now`, then `COMMIT`.
//!
//! Python answers every case Rust cannot prove byte-identical. Those cases return a [`TransitionError`] whose
//! [`WriteState::NotStarted`] marker tells the route nothing was written and the Python body must answer (Design 1 of the Phase 14.5 plan):
//! a state name that is not one of the fourteen (Python raises `ValueError` and prints a traceback), a task row Python could not read,
//! `notes` text that is not valid JSON or that Rust cannot represent (an integer beyond `i64`, nesting beyond 128), a column of the wrong
//! type that the decision needs, gate names whose `repr` is not plain ASCII, and a legal APPROVED without [`ApprovalFacts`]. Every other
//! refusal is [`WriteState::Final`] and carries Python's code, exact message and `details`.
//!
//! Python reads the clock several times per transition (`updated_at`, every `ts`, `notes.verdicts_since`); the plan uses the single `now`
//! for all of them and `now + 0.001` for `verdicts_since`, so a parity test masks exactly those three.

use gsd_codes::ErrorCode;

use crate::{jstr, obj_get, parse_json, Fields, Json, Task};

/// The fourteen task states in `TaskState` declaration order.
pub const STATES: [&str; 14] = [
    "CREATED",
    "VALIDATED",
    "PLANNED",
    "CLAIMED",
    "IN_PROGRESS",
    "IN_REVIEW",
    "CHANGES_REQUESTED",
    "APPROVED",
    "DONE",
    "BLOCKED",
    "FAILED",
    "RETRY",
    "ESCALATED",
    "CANCELLED",
];

/// `MAX_REWORK_LOOPS`: the rework loop after which `CHANGES_REQUESTED` becomes `ESCALATED`.
pub const MAX_REWORK_LOOPS: i64 = 2;

/// Python adds this to the clock for `notes.verdicts_since`, so a verdict row written in the same instant counts as after the rework.
const VERDICTS_SINCE_OFFSET: f64 = 0.001;

/// The states reachable from `from` (`LEGAL_TRANSITIONS`), or `None` when `from` is not a state name.
pub fn legal_targets(from: &str) -> Option<&'static [&'static str]> {
    Some(match from {
        "CREATED" => &["VALIDATED", "CANCELLED"],
        "VALIDATED" => &["PLANNED", "BLOCKED", "CANCELLED"],
        "PLANNED" => &["CLAIMED", "BLOCKED", "FAILED", "CANCELLED"],
        "CLAIMED" => &["IN_PROGRESS", "FAILED", "RETRY", "BLOCKED", "CANCELLED"],
        "IN_PROGRESS" => &["IN_REVIEW", "FAILED", "RETRY", "BLOCKED", "CANCELLED"],
        "IN_REVIEW" => &["APPROVED", "CHANGES_REQUESTED", "FAILED", "CANCELLED"],
        "CHANGES_REQUESTED" => &["IN_PROGRESS", "ESCALATED", "CANCELLED"],
        "APPROVED" => &["DONE", "CANCELLED"],
        "BLOCKED" => &["PLANNED", "CLAIMED", "CANCELLED", "ESCALATED"],
        "FAILED" => &["RETRY", "ESCALATED", "CANCELLED"],
        "RETRY" => &["PLANNED", "CLAIMED", "ESCALATED"],
        "ESCALATED" => &["PLANNED", "CANCELLED"],
        "DONE" | "CANCELLED" => &[],
        _ => return None,
    })
}

/// Facts about a gated APPROVED the host reads from the store and its key policy; Rust never sees a key or an envelope.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ApprovalFacts {
    /// `SWARM_REQUIRE_KEY == "1"` in the host's environment.
    pub require_key: bool,
    /// `real_key_configured()`: a non-dev signing key is configured.
    pub real_key: bool,
    /// `insecure_dev_key() is not None`.
    pub dev_key: bool,
    /// `TaskStore.missing_gates(task_id)`: required gates without a verified, current, passing verdict, in required order.
    pub missing_gates: Vec<String>,
    /// The gates of `TaskStore.unaccepted_gates(task_id)` (any order; the decision sorts them as Python's `sorted` does).
    pub unaccepted_gates: Vec<String>,
}

/// Everything the host reads before it decides.
#[derive(Debug, Clone)]
pub struct Facts {
    /// The `tasks` row as `dict(sqlite3.Row)` (JSON columns still text, as [`Task::from_row`] takes it); `None` when the task does not exist.
    pub task: Option<Fields>,
    /// Facts for an APPROVED target; `None` until the host can supply them, which makes a legal APPROVED `not_started`.
    pub approval: Option<ApprovalFacts>,
}

/// The request of one transition call.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Request {
    /// The task the call names (the `unknown task` message and every `details.task_id` carry it).
    pub task_id: String,
    /// The target state name exactly as the caller wrote it.
    pub to: String,
    /// The `actor` of the audit rows.
    pub actor: String,
    /// The `reason` of the audit row (Python's default is the empty string).
    pub reason: String,
    /// Run `check_manual_transition` first, as `orch_status --transition` and the omp transition tool do; ingest and reconcile do not.
    pub manual: bool,
}

/// Work to run after the commit, in order.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PlanEffect {
    /// `TaskStore._notify_committed`: refresh `live.json` (best effort).
    LiveRefresh,
}

/// One row of `transitions`, before the host adds `id` and `ts`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AuditRow {
    /// `from_state`.
    pub from: String,
    /// `to_state`.
    pub to: String,
    /// `actor`.
    pub actor: String,
    /// `reason`.
    pub reason: String,
}

/// What the host writes for one accepted transition.
#[derive(Debug, Clone, PartialEq)]
pub struct TransitionPlan {
    /// The row's state when it was read: the `state = ?` guard of the conditional `UPDATE`.
    pub from: String,
    /// The final state (an auto-escalation ends in `ESCALATED` whatever was asked).
    pub to: String,
    /// Columns to set, in the order `state`, `attempt`, `rework_loops`, `notes`; `notes` is a JSON object, `state` is [`Self::to`].
    /// `updated_at` is not here: the host sets it to `now`.
    pub columns: Fields,
    /// One or two audit rows that chain from [`Self::from`] to [`Self::to`], in insertion order.
    pub audit: Vec<AuditRow>,
    /// Observer effects to run after the commit.
    pub effects: Vec<PlanEffect>,
}

/// Whether Python must answer the call.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WriteState {
    /// Nothing was decided or written: the route refuses with `error.details.write_state = "not_started"` and Python answers.
    NotStarted,
    /// The refusal is Python's own answer (code, message, details) and is printed as is.
    Final,
}

/// A refusal: Python's `SwarmError` (code, message, details) or a `not_started` marker.
#[derive(Debug, Clone, PartialEq)]
pub struct TransitionError {
    /// The taxonomy code; `E-DEP` for a `not_started` refusal.
    pub code: ErrorCode,
    /// The message as `str(SwarmError)` prints it: `CODE: text`.
    pub message: String,
    /// `SwarmError.details`: `task_id` for a refusal on an existing task, empty for `unknown task`. A `not_started` refusal has none;
    /// the route adds `write_state` when it renders one.
    pub details: Fields,
    /// Whether Python must answer.
    pub write_state: WriteState,
}

impl TransitionError {
    fn python(code: ErrorCode, message: &str, task_id: Option<&str>) -> Self {
        Self {
            code,
            message: format!("{}: {message}", code.as_str()),
            details: task_id
                .map(|id| vec![("task_id".to_string(), jstr(id))])
                .unwrap_or_default(),
            write_state: WriteState::Final,
        }
    }

    fn not_started(reason: &str) -> Self {
        Self {
            code: ErrorCode::Dep,
            message: format!("{}: not started: {reason}", ErrorCode::Dep.as_str()),
            details: Vec::new(),
            write_state: WriteState::NotStarted,
        }
    }
}

/// Python truthiness of a stored JSON value.
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

/// Sets `key` in an ordered object: an existing key keeps its position (Python `dict` assignment), a new one is appended.
fn set_key(fields: &mut Fields, key: &str, value: Json) {
    match fields.iter_mut().find(|(name, _)| name == key) {
        Some(entry) => entry.1 = value,
        None => fields.push((key.to_string(), value)),
    }
}

/// `TaskStore._row`'s `notes_json`: the parsed object, `{}` for an absent, empty or non-object value.
/// Text Rust cannot parse is refused, not read as `{}`: Python may parse it (a big integer, deep nesting).
fn notes_of(row: &Fields) -> Result<Fields, TransitionError> {
    match obj_get(row, "notes") {
        None | Some(Json::Null) => Ok(Vec::new()),
        Some(Json::Str(text)) if text.is_empty() => Ok(Vec::new()),
        Some(Json::Str(text)) => match parse_json(text) {
            Ok(Json::Obj(fields)) => Ok(fields),
            Ok(_) => Ok(Vec::new()),
            Err(_) => Err(TransitionError::not_started(
                "notes is not JSON that Rust can read",
            )),
        },
        Some(_) => Err(TransitionError::not_started("notes is not text")),
    }
}

/// An integer column as the arithmetic of the decision reads it; widened so that `+ 1` cannot overflow.
fn int_column(row: &Fields, name: &str) -> Result<i128, TransitionError> {
    match obj_get(row, name) {
        Some(Json::Int(number)) => Ok(i128::from(*number)),
        _ => Err(TransitionError::not_started(&format!(
            "{name} is not an integer"
        ))),
    }
}

/// Python's `repr` of a list of gate names, for names whose `repr` is plain: printable ASCII without a quote or a backslash.
fn py_repr_names(names: &[String]) -> Result<String, TransitionError> {
    let plain = |name: &String| {
        name.chars()
            .all(|ch| (' '..='~').contains(&ch) && ch != '\'' && ch != '\\')
    };
    if !names.iter().all(plain) {
        return Err(TransitionError::not_started(
            "a gate name needs an escaped repr",
        ));
    }
    let quoted: Vec<String> = names.iter().map(|name| format!("'{name}'")).collect();
    Ok(format!("[{}]", quoted.join(", ")))
}

/// The `to_state is S.APPROVED` block of `TaskStore.transition`, in Python's order.
fn check_approval(facts: Option<&ApprovalFacts>, task_id: &str) -> Result<(), TransitionError> {
    let Some(facts) = facts else {
        return Err(TransitionError::not_started("approval facts not supplied"));
    };
    let policy =
        |message: String| TransitionError::python(ErrorCode::Policy, &message, Some(task_id));
    if facts.require_key && !facts.real_key {
        return Err(policy(
            "fail-closed: SWARM_REQUIRE_KEY=1 but no signing key configured".to_string(),
        ));
    }
    if !facts.real_key && !facts.dev_key {
        return Err(policy("fail-closed: no signing key configured".to_string()));
    }
    if !facts.missing_gates.is_empty() {
        let missing = py_repr_names(&facts.missing_gates)?;
        return Err(policy(format!(
            "fail-closed: {task_id} lacks passing gates {missing}"
        )));
    }
    let mut unaccepted = facts.unaccepted_gates.clone();
    unaccepted.sort();
    unaccepted.dedup();
    if !unaccepted.is_empty() {
        let names = py_repr_names(&unaccepted)?;
        return Err(policy(format!(
            "fail-closed: {task_id} has unaccepted gates {names}"
        )));
    }
    Ok(())
}

/// The CHANGES_REQUESTED bookkeeping: stashes this attempt's `result` and `meta` into `notes.rework_evidence` (one entry per rework
/// loop). Returns whether anything was stashed.
fn stash_rework_evidence(notes: &mut Fields) -> bool {
    let prior: Fields = ["result", "meta"]
        .into_iter()
        .filter_map(|key| {
            obj_get(notes, key)
                .filter(|v| truthy(v))
                .map(|v| (key.to_string(), v.clone()))
        })
        .collect();
    if prior.is_empty() {
        return false;
    }
    match notes.iter_mut().find(|(name, _)| name == "rework_evidence") {
        Some((_, Json::Arr(history))) => history.push(Json::Obj(prior)),
        _ => set_key(notes, "rework_evidence", Json::Arr(vec![Json::Obj(prior)])),
    }
    true
}

/// Decides one transition: the plan to write, or the refusal Python would answer with.
///
/// Python's order is kept: with `request.manual`, an unknown task (`E-INPUT`) and then the gate-task rule (`E-POLICY`) come first;
/// then the target name, the row's own state, legality (`E-CONTRACT`), the rework bookkeeping, the attempt cap (`E-TIMEOUT`) and the
/// APPROVED policy (`E-POLICY`). `now` is the host's single clock reading.
pub fn decide_transition(
    facts: &Facts,
    request: &Request,
    now: f64,
) -> Result<TransitionPlan, TransitionError> {
    let task_id = request.task_id.as_str();
    let target_known = STATES.contains(&request.to.as_str());
    if !request.manual && !target_known {
        return Err(TransitionError::not_started("not a task state name"));
    }
    let Some(row) = facts.task.as_ref() else {
        return Err(TransitionError::python(
            ErrorCode::Input,
            &format!("unknown task {task_id}"),
            None,
        ));
    };
    let shaped = Task::from_row(row.clone())
        .map_err(|_| TransitionError::not_started("task row is not readable"))?;
    if shaped.task_id != task_id {
        return Err(TransitionError::not_started("row is another task"));
    }
    let mut notes = notes_of(row)?;
    if request.manual && request.to == "IN_REVIEW" && obj_get(&notes, "gate").is_some_and(truthy) {
        return Err(TransitionError::python(
            ErrorCode::Policy,
            &format!(
                "{task_id} is a gate task: only a task.result ingest may move it to IN_REVIEW (it checks that the \
                 gate script ran during this lease and that the agent's verdicts match the recorded rows)"
            ),
            Some(task_id),
        ));
    }
    if !target_known {
        return Err(TransitionError::not_started("not a task state name"));
    }
    let from = shaped.state;
    let Some(legal) = legal_targets(&from) else {
        return Err(TransitionError::not_started(
            "row state is not a task state",
        ));
    };
    if !legal.contains(&request.to.as_str()) {
        return Err(TransitionError::python(
            ErrorCode::Contract,
            &format!("illegal transition {from} → {} for {task_id}", request.to),
            Some(task_id),
        ));
    }

    let mut to = request.to.clone();
    let mut reason = request.reason.clone();
    let mut audit_from = from.clone();
    let mut audit = Vec::new();
    let (mut attempt, mut rework_loops, mut notes_changed) = (None, None, false);
    if to == "CHANGES_REQUESTED" {
        let loops = int_column(row, "rework_loops")? + 1;
        let stashed = stash_rework_evidence(&mut notes);
        if loops > i128::from(MAX_REWORK_LOOPS) {
            to = "ESCALATED".to_string();
            reason = format!("rework loops exhausted ({}); {}", loops - 1, request.reason);
            notes_changed = stashed;
            if !legal.contains(&"ESCALATED") {
                audit.push(AuditRow {
                    from: from.clone(),
                    to: "CHANGES_REQUESTED".to_string(),
                    actor: request.actor.clone(),
                    reason: "rework cap reached".to_string(),
                });
                audit_from = "CHANGES_REQUESTED".to_string();
            }
        } else {
            rework_loops = Some(loops);
            set_key(
                &mut notes,
                "verdicts_since",
                Json::Float(now + VERDICTS_SINCE_OFFSET),
            );
            notes_changed = true;
        }
    }
    if to == "CLAIMED" && matches!(from.as_str(), "PLANNED" | "RETRY" | "BLOCKED") {
        let next = int_column(row, "attempt")? + 1;
        if next > int_column(row, "max_attempts")? {
            return Err(TransitionError::python(
                ErrorCode::Timeout,
                &format!("max_attempts exceeded for {task_id}"),
                Some(task_id),
            ));
        }
        attempt = Some(next);
    }
    if to == "APPROVED" {
        check_approval(facts.approval.as_ref(), task_id)?;
    }
    audit.push(AuditRow {
        from: audit_from,
        to: to.clone(),
        actor: request.actor.clone(),
        reason,
    });

    let mut columns: Fields = vec![("state".to_string(), jstr(&to))];
    for (name, value) in [("attempt", attempt), ("rework_loops", rework_loops)] {
        if let Some(number) = value {
            let stored = i64::try_from(number)
                .map_err(|_| TransitionError::not_started("column value beyond i64"))?;
            columns.push((name.to_string(), Json::Int(stored)));
        }
    }
    if notes_changed {
        columns.push(("notes".to_string(), Json::Obj(notes)));
    }
    Ok(TransitionPlan {
        from,
        to,
        columns,
        audit,
        effects: vec![PlanEffect::LiveRefresh],
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const VECTORS: &str = include_str!("../tests/vectors/transition.json");

    fn member<'a>(value: &'a Json, key: &str) -> &'a Json {
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

    fn fields(value: &Json) -> Fields {
        match value {
            Json::Obj(fields) => fields.clone(),
            other => panic!("not an object: {other:?}"),
        }
    }

    fn number(value: &Json) -> f64 {
        match value {
            Json::Float(number) => *number,
            Json::Int(number) => *number as f64,
            other => panic!("not a number: {other:?}"),
        }
    }

    fn names(value: &Json) -> Vec<String> {
        items(value)
            .iter()
            .map(|name| text(name).to_string())
            .collect()
    }

    fn vectors() -> Json {
        parse_json(VECTORS).expect("vector file parses")
    }

    fn approval_of(value: &Json) -> Option<ApprovalFacts> {
        match value {
            Json::Null => None,
            facts => Some(ApprovalFacts {
                require_key: flag(member(facts, "require_key")),
                real_key: flag(member(facts, "real_key")),
                dev_key: flag(member(facts, "dev_key")),
                missing_gates: names(member(facts, "missing_gates")),
                unaccepted_gates: names(member(facts, "unaccepted_gates")),
            }),
        }
    }

    /// The row as the host would hold it after the plan: the plan's columns and `updated_at = now` over the row read.
    fn apply(row: &Fields, plan: &TransitionPlan, now: f64) -> Fields {
        let mut after = row.clone();
        let updated: Fields = plan
            .columns
            .iter()
            .cloned()
            .chain([("updated_at".to_string(), Json::Float(now))])
            .collect();
        for (name, value) in updated {
            set_key(&mut after, &name, value);
        }
        after
    }

    /// `notes` as an object, so the text Python wrote and the object the plan holds compare as values (key order included).
    fn normalised(mut row: Fields) -> Fields {
        if !matches!(obj_get(&row, "notes"), Some(Json::Obj(_))) {
            let notes = Json::Obj(notes_of(&row).expect("notes readable"));
            set_key(&mut row, "notes", notes);
        }
        row.sort_by(|a, b| a.0.cmp(&b.0));
        row
    }

    fn request_of(case: &Json) -> Request {
        let request = member(case, "request");
        Request {
            task_id: text(member(request, "task_id")).to_string(),
            to: text(member(request, "to")).to_string(),
            actor: text(member(request, "actor")).to_string(),
            reason: text(member(request, "reason")).to_string(),
            manual: flag(member(request, "manual")),
        }
    }

    fn facts_of(case: &Json) -> Facts {
        Facts {
            task: match member(case, "task") {
                Json::Null => None,
                row => Some(fields(row)),
            },
            approval: approval_of(member(case, "approval")),
        }
    }

    /// Checks that the plan is something the host's validator can accept: guarded by the read state, only allowed columns, at most two
    /// chained audit rows and a final state the legal table reaches.
    fn assert_plan_is_valid(plan: &TransitionPlan, row: &Fields) {
        assert_eq!(Some(&Json::Str(plan.from.clone())), obj_get(row, "state"));
        assert!(plan.columns.iter().all(
            |(name, _)| ["state", "attempt", "rework_loops", "notes"].contains(&name.as_str())
        ));
        assert!((1..=2).contains(&plan.audit.len()));
        let mut at = plan.from.as_str();
        for step in &plan.audit {
            assert_eq!(step.from, at);
            assert!(
                legal_targets(at).is_some_and(|targets| targets.contains(&step.to.as_str())),
                "{at} -> {}",
                step.to
            );
            at = &step.to;
        }
        assert_eq!(at, plan.to);
        assert_eq!(
            obj_get(&plan.columns, "state"),
            Some(&Json::Str(plan.to.clone()))
        );
    }

    fn replay(case: &Json) {
        let id = text(member(case, "id"));
        let now = number(member(case, "now"));
        let facts = facts_of(case);
        let request = request_of(case);
        let expect = member(case, "expect");
        let outcome = text(member(expect, "outcome"));
        let decided = decide_transition(&facts, &request, now);
        if flag(member(case, "rust_not_started")) || outcome == "exception" {
            let err = decided.expect_err(id);
            assert_eq!(err.write_state, WriteState::NotStarted, "{id}: {err:?}");
            assert_eq!(err.code, ErrorCode::Dep, "{id}");
            return;
        }
        match outcome {
            "ok" => {
                let plan = decided.unwrap_or_else(|err| panic!("{id}: {err:?}"));
                let row = facts.task.as_ref().expect("an ok case has a task");
                assert_plan_is_valid(&plan, row);
                let mut expected = row.clone();
                for (name, value) in fields(member(expect, "row_diff")) {
                    set_key(&mut expected, &name, value);
                }
                assert_eq!(
                    normalised(apply(row, &plan, now)),
                    normalised(expected),
                    "{id}: row"
                );
                let rows: Vec<(String, String, String, String, f64)> =
                    items(member(expect, "audit"))
                        .iter()
                        .map(|r| {
                            (
                                text(member(r, "from_state")).to_string(),
                                text(member(r, "to_state")).to_string(),
                                text(member(r, "actor")).to_string(),
                                text(member(r, "reason")).to_string(),
                                number(member(r, "ts")),
                            )
                        })
                        .collect();
                let planned: Vec<_> = plan
                    .audit
                    .iter()
                    .map(|r| {
                        (
                            r.from.clone(),
                            r.to.clone(),
                            r.actor.clone(),
                            r.reason.clone(),
                            now,
                        )
                    })
                    .collect();
                assert_eq!(planned, rows, "{id}: audit");
                assert_eq!(plan.effects, vec![PlanEffect::LiveRefresh], "{id}: effects");
            }
            "error" => {
                let err = decided.expect_err(id);
                let wanted = member(expect, "error");
                assert_eq!(err.write_state, WriteState::Final, "{id}");
                assert_eq!(
                    err.code.as_str(),
                    text(member(wanted, "code")),
                    "{id}: code"
                );
                assert_eq!(
                    err.message,
                    text(member(wanted, "message")),
                    "{id}: message"
                );
                assert_eq!(
                    Json::Obj(err.details),
                    member(wanted, "details").clone(),
                    "{id}: details"
                );
            }
            other => panic!("{id}: unknown outcome {other}"),
        }
    }

    /// Replays every case of one group; `count` is the number of cases the committed file holds for it, so a truncated file fails.
    fn replay_group(group: &str, count: usize) {
        let doc = vectors();
        let cases: Vec<&Json> = items(member(&doc, "cases"))
            .iter()
            .filter(|case| text(member(case, "group")) == group)
            .collect();
        assert_eq!(cases.len(), count, "cases in group {group}");
        for case in cases {
            replay(case);
        }
    }

    #[test]
    fn matrix_replays_all_196_state_pairs() {
        replay_group("matrix", 196);
    }

    #[test]
    fn rework_stash_cap_and_double_row_replay() {
        replay_group("rework", 41);
    }

    #[test]
    fn claim_attempt_and_cap_replay() {
        replay_group("claim", 34);
    }

    #[test]
    fn gate_task_manual_rule_replays() {
        replay_group("gate", 19);
    }

    #[test]
    fn state_names_and_unknown_tasks_replay() {
        replay_group("state-name", 12);
        replay_group("unknown-task", 5);
    }

    #[test]
    fn approval_facts_replay() {
        replay_group("approval", 27);
    }

    #[test]
    fn notes_and_row_shapes_replay() {
        replay_group("notes", 6);
        replay_group("row", 5);
        replay_group("rust-refuses", 6);
    }

    #[test]
    fn no_case_is_left_outside_a_replayed_group() {
        let doc = vectors();
        let total = items(member(&doc, "cases")).len();
        assert_eq!(total, 196 + 41 + 34 + 19 + 12 + 5 + 27 + 6 + 5 + 6);
    }

    #[test]
    fn matrix_covers_all_196_state_pairs() {
        let doc = vectors();
        let seen: Vec<(&str, &str)> = items(member(&doc, "cases"))
            .iter()
            .filter(|case| text(member(case, "group")) == "matrix")
            .map(|case| {
                let to = text(member(member(case, "request"), "to"));
                (text(member(member(case, "task"), "state")), to)
            })
            .collect();
        let want: Vec<(&str, &str)> = STATES
            .iter()
            .flat_map(|from| STATES.iter().map(move |to| (*from, *to)))
            .collect();
        assert_eq!(seen, want);
    }

    #[test]
    fn constants_match_the_python_tables() {
        let doc = vectors();
        let constants = member(&doc, "constants");
        assert_eq!(names(member(constants, "states")), STATES.map(String::from));
        assert_eq!(
            member(constants, "max_rework_loops"),
            &Json::Int(MAX_REWORK_LOOPS)
        );
        let Json::Obj(legal) = member(constants, "legal") else {
            panic!("legal is an object");
        };
        assert_eq!(legal.len(), STATES.len());
        for (from, targets) in legal {
            let mut ours: Vec<&str> = legal_targets(from).expect("a state").to_vec();
            ours.sort_unstable();
            let theirs = names(targets);
            assert_eq!(
                ours,
                theirs.iter().map(String::as_str).collect::<Vec<_>>(),
                "{from}"
            );
        }
        assert_eq!(legal_targets("NOT_A_STATE"), None);
    }

    fn case_with(id: &str) -> (Facts, Request, f64) {
        let doc = vectors();
        let case = items(member(&doc, "cases"))
            .iter()
            .find(|case| text(member(case, "id")) == id)
            .unwrap_or_else(|| panic!("no case {id}"))
            .clone();
        (
            facts_of(&case),
            request_of(&case),
            number(member(&case, "now")),
        )
    }

    #[test]
    fn a_legal_approved_without_facts_is_not_started_but_an_illegal_one_is_python_s_answer() {
        let (mut facts, request, now) = case_with("matrix-IN_REVIEW-APPROVED");
        assert!(facts.approval.is_some());
        facts.approval = None;
        let err = decide_transition(&facts, &request, now).expect_err("not started");
        assert_eq!(err.write_state, WriteState::NotStarted);

        let (facts, request, now) = case_with("matrix-IN_PROGRESS-APPROVED");
        assert!(facts.approval.is_none());
        let err = decide_transition(&facts, &request, now).expect_err("illegal");
        assert_eq!(err.write_state, WriteState::Final);
        assert_eq!(err.code, ErrorCode::Contract);
    }

    #[test]
    fn a_row_of_another_task_is_not_started() {
        let (facts, mut request, now) = case_with("matrix-CREATED-VALIDATED");
        request.task_id = "T-someone-else".to_string();
        let err = decide_transition(&facts, &request, now).expect_err("mismatch");
        assert_eq!(err.write_state, WriteState::NotStarted);
    }
}
