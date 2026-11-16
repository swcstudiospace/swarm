//! The read-only `orch_status` route as a pure function over a JSON request (P5 slice 2 unit 4,
//! D-P5-11).
//!
//! The host reads the Task Store with sqlx and sends the rows in one request object. This module
//! shapes the answer exactly as `orch_status.py` does and reads no database, file or clock, so it
//! compiles for `wasm32-unknown-unknown`. The wire forms are [`orch_status_invoke`]
//! (request bytes in, reply bytes out) and [`decode_reply_wire`] (the host's side of the reply).

use std::collections::HashMap;

use gsd_codes::ErrorCode;

use crate::{
    check_safe_id, count_state, empty_status, finish, history_fields, jstr, obj_get, parse_json,
    push_latest, satisfied_ids, status_list, status_row, to_python_json, Fields, Json, LatestRow,
    Reply, SeamError, StatusRow, Task,
};

/// Verdict rows of the request, keyed by task id.
type Verdicts<'a> = HashMap<&'a str, &'a [Json]>;

/// The request fields every mode reads: the caller, the resolved correlation and the store paths.
struct Header<'a> {
    agent: &'a str,
    script: &'a str,
    correlation: Option<&'a str>,
    db_path: &'a str,
    swarm_dir: &'a str,
    events: i64,
}

/// Builds the input error for a request field that is absent or has the wrong type.
fn bad_field(name: &str) -> SeamError {
    SeamError::new(
        ErrorCode::Input,
        format!("request field {name} is missing or has the wrong type"),
    )
}

/// Builds the contract error for a reply that does not match the wire shape.
fn wire_error(message: &str) -> SeamError {
    SeamError::new(ErrorCode::Contract, message)
}

fn object_of<'a>(value: &'a Json, name: &str) -> Result<&'a [(String, Json)], SeamError> {
    match value {
        Json::Obj(fields) => Ok(fields.as_slice()),
        _ => Err(bad_field(name)),
    }
}

fn str_of<'a>(fields: &'a [(String, Json)], key: &str) -> Result<&'a str, SeamError> {
    match obj_get(fields, key) {
        Some(Json::Str(text)) => Ok(text.as_str()),
        _ => Err(bad_field(key)),
    }
}

fn int_of(fields: &[(String, Json)], key: &str) -> Result<i64, SeamError> {
    match obj_get(fields, key) {
        Some(Json::Int(number)) => Ok(*number),
        _ => Err(bad_field(key)),
    }
}

fn list_of<'a>(fields: &'a [(String, Json)], key: &str) -> Result<&'a [Json], SeamError> {
    match obj_get(fields, key) {
        Some(Json::Arr(items)) => Ok(items.as_slice()),
        _ => Err(bad_field(key)),
    }
}

fn row_fields(row: &Json, name: &str) -> Result<Fields, SeamError> {
    match row {
        Json::Obj(fields) => Ok(fields.clone()),
        _ => Err(bad_field(name)),
    }
}

/// Groups the `verdicts` entries by task id; a task id may appear once.
fn verdicts_of(entries: &[Json]) -> Result<Verdicts<'_>, SeamError> {
    let mut grouped: Verdicts<'_> = HashMap::new();
    for entry in entries {
        let fields = object_of(entry, "verdicts")?;
        let task_id = str_of(fields, "task_id")?;
        let rows = list_of(fields, "rows")?;
        if grouped.insert(task_id, rows).is_some() {
            return Err(SeamError::new(
                ErrorCode::Input,
                "request field verdicts has a duplicate task entry",
            ));
        }
    }
    Ok(grouped)
}

/// The latest verdict row per gate for `task_id`, rows taken in the order the host sent them.
fn latest_for(verdicts: &Verdicts<'_>, task_id: &str) -> Result<Vec<LatestRow>, SeamError> {
    let mut latest: Vec<LatestRow> = Vec::new();
    if let Some(rows) = verdicts.get(task_id) {
        for row in rows.iter() {
            push_latest(&mut latest, row_fields(row, "verdicts rows")?)?;
        }
    }
    Ok(latest)
}

/// Answers an `orch_status` request object with the Python-shaped reply.
///
/// Request schema (keys in any order; every key is required unless noted):
/// - `mode`: `"list"` or `"history"`; any other value is an input error.
/// - `agent`, `script`, `db_path`, `swarm_dir`: strings.
/// - `correlation`: a string or null. A non-empty string must pass [`check_safe_id`] in list mode;
///   history mode ignores it.
/// - `events`: integer, the run-log count the host computed (list mode; `0` in history mode).
/// - `tasks`: list of task rows in SQL order (list mode; ignored in history mode).
/// - `verdicts`: list of `{"task_id": string, "rows": [row, ...]}` with the rows already filtered by
///   `ts >= since` and ordered by id. Each task the reply needs has one entry.
/// - `history`: `{"task_id": string, "task": row or null, "transitions": [row, ...]}`, required in
///   history mode and ignored in list mode.
///
/// A row is a JSON object keyed by the Task Store column names. Its `task_id` must pass
/// [`check_safe_id`] in history mode; an absent task row is an `E-INPUT` `unknown task` error.
pub fn orch_status_reply(request: &Json) -> Result<Reply, SeamError> {
    let fields = object_of(request, "request")?;
    let mode = str_of(fields, "mode")?;
    let header = Header {
        agent: str_of(fields, "agent")?,
        script: str_of(fields, "script")?,
        correlation: match obj_get(fields, "correlation") {
            Some(Json::Str(text)) => Some(text.as_str()),
            Some(Json::Null) => None,
            _ => return Err(bad_field("correlation")),
        },
        db_path: str_of(fields, "db_path")?,
        swarm_dir: str_of(fields, "swarm_dir")?,
        events: int_of(fields, "events")?,
    };
    let verdicts = verdicts_of(list_of(fields, "verdicts")?)?;
    match mode {
        "list" => list_reply(fields, &verdicts, &header),
        "history" => history_reply(fields, &verdicts, &header),
        _ => Err(SeamError::new(
            ErrorCode::Input,
            "request field mode must be \"list\" or \"history\"",
        )),
    }
}

/// `orch_status` with no `--history`: the task table for one correlation, or every task.
fn list_reply(
    fields: &[(String, Json)],
    verdicts: &Verdicts<'_>,
    header: &Header<'_>,
) -> Result<Reply, SeamError> {
    if let Some(id) = header.correlation.filter(|id| !id.is_empty()) {
        check_safe_id(id, "correlation id")?;
    }
    let mut tasks: Vec<Task> = Vec::new();
    for row in list_of(fields, "tasks")? {
        tasks.push(Task::from_row(row_fields(row, "tasks")?)?);
    }
    if tasks.is_empty() {
        let answer = empty_status(header.correlation, header.db_path, header.swarm_dir);
        return Ok(finish(answer, header.agent, header.script));
    }
    let satisfied = satisfied_ids(&tasks);
    let mut counts: Vec<(String, i64)> = Vec::new();
    let mut rows: Vec<StatusRow> = Vec::with_capacity(tasks.len());
    for task in &tasks {
        count_state(&mut counts, &task.state);
        let latest = latest_for(verdicts, &task.task_id)?;
        rows.push(status_row(task, &latest, &satisfied)?);
    }
    let answer = status_list(
        header.correlation,
        &rows,
        &counts,
        header.events,
        header.db_path,
        header.swarm_dir,
    );
    Ok(finish(answer, header.agent, header.script))
}

/// `orch_status --history TASK`: transitions, latest verdicts and missing gate reasons of one task.
fn history_reply(
    fields: &[(String, Json)],
    verdicts: &Verdicts<'_>,
    header: &Header<'_>,
) -> Result<Reply, SeamError> {
    let history = match obj_get(fields, "history") {
        Some(Json::Obj(history)) => history.as_slice(),
        _ => return Err(bad_field("history")),
    };
    let task_id = str_of(history, "task_id")?;
    check_safe_id(task_id, "task id")?;
    let task_row = match obj_get(history, "task") {
        Some(Json::Null) => None,
        Some(Json::Obj(row)) => Some(row.clone()),
        _ => return Err(bad_field("history.task")),
    };
    let row = task_row
        .ok_or_else(|| SeamError::new(ErrorCode::Input, "unknown task for history.task_id"))?;
    let task = Task::from_row(row)?;
    let mut transitions: Vec<Json> = Vec::new();
    for row in list_of(history, "transitions")? {
        transitions.push(Json::Obj(row_fields(row, "history.transitions")?));
    }
    let latest = latest_for(verdicts, task_id)?;
    let answer = history_fields(task_id, transitions, latest, &task)?;
    Ok(finish(answer, header.agent, header.script))
}

/// Encodes a reply as the wire object `{"exit_code": n, "stdout": "..."}`.
fn encode_reply(reply: &Reply) -> String {
    to_python_json(&Json::Obj(vec![
        (
            "exit_code".to_string(),
            Json::Int(i64::from(reply.exit_code)),
        ),
        ("stdout".to_string(), jstr(&reply.stdout)),
    ]))
}

/// The guest entry: UTF-8 request bytes in, encoded reply bytes out.
///
/// Invalid UTF-8, invalid JSON and any request that breaks the schema of [`orch_status_reply`]
/// are `E-INPUT`. Messages name the field and never echo the input value.
pub fn orch_status_invoke(input: &[u8]) -> Result<Vec<u8>, SeamError> {
    let text = std::str::from_utf8(input)
        .map_err(|_| SeamError::new(ErrorCode::Input, "request is not valid UTF-8"))?;
    let request = parse_json(text)?;
    let reply = orch_status_reply(&request)?;
    Ok(encode_reply(&reply).into_bytes())
}

/// Decodes the reply wire object written by [`orch_status_invoke`].
///
/// Anything other than an object with exactly an integer `exit_code` and a string `stdout` is
/// `E-CONTRACT`.
pub fn decode_reply_wire(bytes: &[u8]) -> Result<Reply, SeamError> {
    let text = std::str::from_utf8(bytes).map_err(|_| wire_error("reply is not valid UTF-8"))?;
    let value = parse_json(text).map_err(|_| wire_error("reply is not valid JSON"))?;
    let Json::Obj(fields) = &value else {
        return Err(wire_error("reply must be a JSON object"));
    };
    if fields.len() != 2 {
        return Err(wire_error("reply must hold exactly exit_code and stdout"));
    }
    let exit = match obj_get(fields, "exit_code") {
        Some(Json::Int(number)) => *number,
        _ => return Err(wire_error("reply exit_code must be an integer")),
    };
    let exit_code =
        i32::try_from(exit).map_err(|_| wire_error("reply exit_code is out of range"))?;
    let stdout = match obj_get(fields, "stdout") {
        Some(Json::Str(text)) => text.clone(),
        _ => return Err(wire_error("reply stdout must be a string")),
    };
    Ok(Reply { exit_code, stdout })
}

#[cfg(test)]
mod tests {
    use super::*;

    type TestResult = Result<(), String>;

    fn message(error: SeamError) -> String {
        error.message().to_string()
    }

    fn cols(pairs: &[(&str, Json)]) -> Fields {
        pairs
            .iter()
            .map(|(key, value)| (key.to_string(), value.clone()))
            .collect()
    }

    fn task_json(id: &str, state: &str, depends_on: &str) -> Json {
        Json::Obj(cols(&[
            ("task_id", jstr(id)),
            ("state", jstr(state)),
            ("agent_id", jstr("A05")),
            ("dag_depth", Json::Int(1)),
            ("attempt", Json::Int(0)),
            ("rework_loops", Json::Int(0)),
            ("risk_class", jstr("low")),
            ("depends_on", jstr(depends_on)),
            ("notes", jstr("{}")),
            ("outputs", jstr("[]")),
        ]))
    }

    fn verdict_json(id: &str) -> Json {
        Json::Obj(cols(&[
            ("task_id", jstr(id)),
            ("gate", jstr("review")),
            ("verdict", jstr("pass")),
            ("findings", jstr("[]")),
            ("envelope_json", Json::Null),
            ("ts", Json::Float(2.0)),
        ]))
    }

    fn request(mode: &str, tasks: Vec<Json>, verdicts: Vec<Json>, history: Json) -> Json {
        Json::Obj(cols(&[
            ("mode", jstr(mode)),
            ("agent", jstr("A01")),
            ("script", jstr("orch_status")),
            ("correlation", jstr("C1")),
            ("db_path", jstr("/x/tasks.db")),
            ("swarm_dir", jstr("/x")),
            ("events", Json::Int(3)),
            ("tasks", Json::Arr(tasks)),
            ("verdicts", Json::Arr(verdicts)),
            ("history", history),
        ]))
    }

    fn verdict_entry(id: &str) -> Json {
        Json::Obj(cols(&[
            ("task_id", jstr(id)),
            ("rows", Json::Arr(vec![verdict_json(id)])),
        ]))
    }

    /// The reply's stdout as ordered top-level fields.
    fn answer(reply: &Reply) -> Result<Fields, String> {
        match parse_json(reply.stdout.trim_end()).map_err(message)? {
            Json::Obj(fields) => Ok(fields),
            _ => Err("reply stdout is not an object".to_string()),
        }
    }

    fn get(fields: &[(String, Json)], key: &str) -> Result<Json, String> {
        obj_get(fields, key)
            .cloned()
            .ok_or_else(|| format!("missing key {key}"))
    }

    fn get_of(value: &Json, key: &str) -> Result<Json, String> {
        match value {
            Json::Obj(fields) => get(fields, key),
            _ => Err(format!("{key} parent is not an object")),
        }
    }

    fn input_error(result: Result<Reply, SeamError>) -> Result<String, String> {
        match result {
            Ok(_) => Err("expected an error".to_string()),
            Err(error) => {
                assert!(matches!(error.code(), ErrorCode::Input));
                Ok(message(error))
            }
        }
    }

    #[test]
    fn empty_list_gives_the_empty_status_reply_through_the_wire() -> TestResult {
        let bytes = to_python_json(&request("list", Vec::new(), Vec::new(), Json::Null));
        let out = orch_status_invoke(bytes.as_bytes()).map_err(message)?;
        let reply = decode_reply_wire(&out).map_err(message)?;
        let expected = "{\n  \"status\": \"ok\",\n  \"correlation_id\": \"C1\",\n  \"tasks\": [],\n  \"db\": \"/x/tasks.db\",\n  \"swarm_dir\": \"/x\",\n  \"summary\": \"no tasks (run orch_plan.py first)\",\n  \"agent\": \"A01\",\n  \"script\": \"orch_status\"\n}\n";
        assert_eq!(reply.exit_code, 0);
        assert_eq!(reply.stdout, expected);
        Ok(())
    }

    #[test]
    fn two_tasks_give_rows_counts_and_summary() -> TestResult {
        let req = request(
            "list",
            vec![
                task_json("T-A", "IN_REVIEW", "[]"),
                task_json("T-B", "PLANNED", "[\"T-A\"]"),
            ],
            vec![verdict_entry("T-A")],
            Json::Null,
        );
        let fields = answer(&orch_status_reply(&req).map_err(message)?)?;
        assert_eq!(
            get(&fields, "counts")?,
            Json::Obj(cols(&[
                ("IN_REVIEW", Json::Int(1)),
                ("PLANNED", Json::Int(1))
            ]))
        );
        assert_eq!(get(&fields, "events")?, Json::Int(3));
        assert_eq!(get(&fields, "complete")?, Json::Bool(false));
        assert_eq!(get(&fields, "escalated")?, Json::Arr(Vec::new()));
        assert_eq!(get(&fields, "correlation_id")?, jstr("C1"));
        let Json::Str(summary) = get(&fields, "summary")? else {
            return Err("summary is not text".to_string());
        };
        assert!(summary.contains(
            "counts: {'IN_REVIEW': 1, 'PLANNED': 1}   escalated: none   complete: False"
        ));

        let Json::Arr(rows) = get(&fields, "tasks")? else {
            return Err("tasks is not a list".to_string());
        };
        assert_eq!(rows.len(), 2);
        assert_eq!(get_of(&rows[0], "ready")?, Json::Bool(false));
        assert_eq!(
            get_of(&rows[0], "gates_required")?,
            Json::Arr(vec![jstr("review")])
        );
        assert_eq!(
            get_of(&rows[0], "missing_gates")?,
            Json::Arr(vec![jstr("review")])
        );
        assert_eq!(
            get_of(&rows[0], "verdicts")?,
            Json::Obj(cols(&[("review", jstr("pass"))]))
        );
        assert_eq!(get_of(&rows[1], "ready")?, Json::Bool(true));
        assert_eq!(get_of(&rows[1], "missing_gates")?, Json::Arr(Vec::new()));
        assert_eq!(get_of(&rows[1], "verdicts")?, Json::Obj(Vec::new()));
        assert_eq!(
            get_of(&rows[1], "depends_on")?,
            Json::Arr(vec![jstr("T-A")])
        );
        Ok(())
    }

    #[test]
    fn known_history_task_gives_transitions_and_missing_reasons() -> TestResult {
        let task = task_json("T-A", "DONE", "[]");
        let transition = Json::Obj(cols(&[
            ("to_state", jstr("CREATED")),
            ("from_state", Json::Null),
        ]));
        let history = Json::Obj(cols(&[
            ("task_id", jstr("T-A")),
            ("task", task),
            ("transitions", Json::Arr(vec![transition.clone()])),
        ]));
        let req = request("history", Vec::new(), vec![verdict_entry("T-A")], history);
        let reply = orch_status_reply(&req).map_err(message)?;
        assert_eq!(reply.exit_code, 0);
        let fields = answer(&reply)?;
        assert_eq!(get(&fields, "task_id")?, jstr("T-A"));
        assert_eq!(get(&fields, "history")?, Json::Arr(vec![transition]));
        assert_eq!(
            get(&fields, "missing_gate_reasons")?,
            Json::Obj(cols(&[("review", jstr("unsigned"))]))
        );
        assert_eq!(
            get_of(&get(&fields, "verdicts")?, "review").and_then(|row| get_of(&row, "verdict"))?,
            jstr("pass")
        );
        assert_eq!(get(&fields, "summary")?, jstr("history for T-A"));
        assert_eq!(
            fields.last(),
            Some(&("script".to_string(), jstr("orch_status")))
        );
        Ok(())
    }

    #[test]
    fn unknown_history_task_is_an_input_error() -> TestResult {
        let history = Json::Obj(cols(&[
            ("task_id", jstr("T-X")),
            ("task", Json::Null),
            ("transitions", Json::Arr(Vec::new())),
        ]));
        let req = request("history", Vec::new(), Vec::new(), history);
        assert_eq!(
            input_error(orch_status_reply(&req))?,
            "E-INPUT: unknown task for history.task_id"
        );
        Ok(())
    }

    #[test]
    fn oversized_risk_class_is_not_echoed_in_the_error() -> TestResult {
        let risk = "Q".repeat(1_000_000);
        let Json::Obj(mut row) = task_json("T-A", "DONE", "[]") else {
            return Err("task row is not an object".to_string());
        };
        for (key, value) in row.iter_mut() {
            if key.as_str() == "risk_class" {
                *value = jstr(&risk);
            }
        }
        let history = Json::Obj(cols(&[
            ("task_id", jstr("T-A")),
            ("task", Json::Obj(row)),
            ("transitions", Json::Arr(Vec::new())),
        ]));
        let req = request("history", Vec::new(), vec![verdict_entry("T-A")], history);
        let text = input_error(orch_status_reply(&req))?;
        assert!(!text.contains('Q'));
        assert!(text.len() < 200, "{text}");
        Ok(())
    }

    #[test]
    fn wrong_shape_and_unknown_mode_are_input_errors() -> TestResult {
        assert!(input_error(orch_status_reply(&Json::Arr(Vec::new()))).is_ok());
        let no_mode = Json::Obj(cols(&[("agent", jstr("A01"))]));
        assert!(input_error(orch_status_reply(&no_mode)).is_ok());
        let bogus = request("bogus", Vec::new(), Vec::new(), Json::Null);
        let text = input_error(orch_status_reply(&bogus))?;
        assert!(!text.contains("bogus"));
        let bad_id = Json::Obj(cols(&[
            ("mode", jstr("list")),
            ("agent", jstr("A01")),
            ("script", jstr("orch_status")),
            ("correlation", jstr("bad id!")),
            ("db_path", jstr("/x/tasks.db")),
            ("swarm_dir", jstr("/x")),
            ("events", Json::Int(0)),
            ("tasks", Json::Arr(Vec::new())),
            ("verdicts", Json::Arr(Vec::new())),
            ("history", Json::Null),
        ]));
        assert!(
            input_error(orch_status_reply(&bad_id))?.starts_with("E-INPUT: invalid correlation id")
        );
        Ok(())
    }

    #[test]
    fn invalid_utf8_and_invalid_json_are_input_errors() -> TestResult {
        let utf8 = orch_status_invoke(&[0x7b, 0xff, 0xfe])
            .err()
            .map(|e| e.code());
        assert_eq!(utf8, Some(ErrorCode::Input));
        let json = orch_status_invoke(b"{").err().map(|e| e.code());
        assert_eq!(json, Some(ErrorCode::Input));
        Ok(())
    }

    #[test]
    fn reply_round_trips_newlines_and_non_ascii() -> TestResult {
        let reply = Reply {
            exit_code: 2,
            stdout: "first line\nnaïve \"quoted\" é\n".to_string(),
        };
        let bytes = encode_reply(&reply);
        assert_eq!(decode_reply_wire(bytes.as_bytes()), Ok(reply));
        Ok(())
    }

    #[test]
    fn malformed_reply_wire_is_a_contract_error() -> TestResult {
        let shape = decode_reply_wire(b"{\"exit_code\": 0}")
            .err()
            .map(|e| e.code());
        assert_eq!(shape, Some(ErrorCode::Contract));
        let text = decode_reply_wire(b"{\"exit_code\": 0, \"stdout\": 1}")
            .err()
            .map(|e| e.code());
        assert_eq!(text, Some(ErrorCode::Contract));
        Ok(())
    }
}
