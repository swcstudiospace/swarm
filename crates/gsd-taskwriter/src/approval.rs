//! The facts a transition to `APPROVED` needs, read by the HOST inside the writer's own transaction (phase 14.5, unit U3.4d).
//!
//! # What Python reads for an APPROVED target (`swarm/taskstore.py`, `TaskStore.transition`, lines 279 to 294)
//!
//! In this order, inside the `BEGIN IMMEDIATE` of the write, after the legality check, the `CHANGES_REQUESTED` bookkeeping and the
//! `max_attempts` check, and before `_apply`:
//!
//! 1. `os.environ["SWARM_REQUIRE_KEY"] == "1"` and `real_key_configured()` is false: `E-POLICY`
//!    `fail-closed: SWARM_REQUIRE_KEY=1 but no signing key configured`;
//! 2. `real_key_configured()` is false and `insecure_dev_key()` is `None`: `E-POLICY` `fail-closed: no signing key configured`;
//! 3. `missing_gates(task_id)`: `_gates_of(task)` (`notes.gates` when present, else `GATES_BY_RISK[risk_class]`); the rows of the task
//!    with `ts >= notes.verdicts_since` in id order (`_latest_rows`, `SELECT * FROM verdicts WHERE task_id=? AND ts>=? ORDER BY
//!    id`), the highest id per gate; for each required gate `_check_row` (`absent`, `unsigned`, `bad-sig` after `validate_verdict`
//!    verified the signature, `mismatch`, `stale`, `dry-run`, `expired`, `fail`, `ok`). A gate that is not `ok` is missing, and
//!    the refusal is `E-POLICY` `fail-closed: <task> lacks passing gates [<repr of the names>]`;
//! 4. `unaccepted_gates(task_id)`: the required gates that are `ok` whose current row names a `gate_task` (in the signed payload)
//!    whose task is not `IN_REVIEW`, `APPROVED` or `DONE`, or does not exist, or whose envelope cannot be read: `E-POLICY`
//!    `fail-closed: <task> has unaccepted gates [<repr of the sorted names>]`.
//!
//! The first two are the key policy and read no row; the last two read rows and verify signatures. The only keyed step is
//! `verify_envelope` inside `validate_verdict`. It re-reads `os.environ` on every call; this module reads the environment ONCE per
//! call ([`ApprovalSource::keys`]) and uses that snapshot for the policy and for every row.
//!
//! # What `real_key` and `dev_key` mean
//!
//! `real_key` is `real_key_configured()`: a loadable Ed25519 seed or a non-dev `SWARM_SIGNING_KEY` (never "an HMAC secret is
//! present": it can be the public dev key). `dev_key` is `insecure_dev_key() is not None`: `SWARM_ALLOW_INSECURE_DEV_KEY=1`, no
//! Ed25519 signer, no Ed25519 loading error and `SWARM_REQUIRE_KEY` not `1`. Both come from `gsd_envelope::KeyMaterial`
//! ([`KeyMaterial::real_key_configured`], [`KeyMaterial::dev_key_active`]) and nowhere else.
//!
//! # `security.dev_key`
//!
//! Python emits `security.dev_key` from `_warn_dev_key`, which only `sign_envelope` calls (`swarm/envelope.py:395`, the unset-or-empty
//! `SWARM_SIGNING_KEY` opt-in). `verify_envelope`, `real_key_configured`, `insecure_dev_key` and `TaskStore.transition` never emit
//! it, so a transition to APPROVED on the dev-key path emits NO `security.dev_key` event in Python and none here; the parity
//! cells compare the whole effect list. The audit is a fact of the signing side (`gsd_envelope::SignOutcome::dev_key_audit`, the gate
//! issue tail), not of this module.
//!
//! # How the writer gets keys and rows
//!
//! * Keys: an [`ApprovalSource`] passed to [`TaskStoreWriter::with_approval`](crate::TaskStoreWriter::with_approval). The writer asks
//!   it for [`ApprovalKeys`] exactly once per call, only when the target is `APPROVED`, BEFORE `BEGIN IMMEDIATE`. A
//!   [`Declined`] (a key-file reference, an unsupported shape) refuses there as not-started, so Python answers and the store is
//!   untouched (D-P24 (2)). [`EnvApprovalSource`] reads the process environment (production); [`FixedApprovalSource`] reads a
//!   given snapshot (tests). [`TaskStoreWriter::new`](crate::TaskStoreWriter::new) has no source: a legal APPROVED stays a
//!   not-started refusal, as before this unit, so every existing caller works unchanged.
//! * Rows: [`gather`] runs on the writer's own [`Tx`], after the task row was read and before the decider is called, so the task
//!   row, the verdict rows and the issuer tasks are one snapshot under one write lock; no row is read before the lock. Python's
//!   `_latest_rows` runs `json.loads(findings)` on every row of the task it reads, whatever its gate: [`Tx::verdict_rows`] makes the
//!   same check on the same rows (a `NULL` or invalid `findings` declines the call, as not started, so Python refuses with its own
//!   text).
//! * Clock: the expiry of a verdict is compared with [`ApprovalSource::clock`], read inside the transaction after the rows, because
//!   Python reads `time.time()` inside its lock. The call's own `now` (read before the lock was awaited) stays the `updated_at` and
//!   the audit `ts`; it is only the lower bound of the clock. A verdict that expired while the writer waited for the lock is expired.
//!
//! # The key boundary
//!
//! Pure facts (`gsd_route::gate_facts`) do everything that needs no key. This module adds the signature answer per row. A
//! [`Declined`] on a row the answer is needed for (an envelope text outside what Python's `json` and this crate agree on) declines
//! the WHOLE call; it never becomes `sig_ok = false` ("skip the block"). A row whose envelope is not even a valid `gate.verdict`
//! shape is `bad-sig` whatever its signature says, so it is not verified. The result is an [`ApprovalFacts`]: two booleans for the
//! key policy, `require_key`, and gate names. No key, signature or envelope leaves this module, and none is in any error text.

use std::ffi::OsString;
use std::fmt;
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

use gsd_envelope::{
    conventional_key_file_presence, parse_envelope as parse_signed, verify_envelope, Declined,
    KeyFilePresence, KeyMaterial,
};
use gsd_route::gate_facts::{
    latest_rows, missing_gate_reasons, unaccepted_gates, EnvelopeState, GateTask, IssuerState,
    TaskFacts, Unsupported, VerdictFacts,
};
use gsd_route::transition_plan::ApprovalFacts;
use gsd_route::{obj_get, Fields, Json};

use crate::error::{ErrorKind, WriterError};
use crate::store::Tx;

/// The signing keys of one call and the policy switch Python reads beside them. Not `Clone`, no `Serialize`, and `Debug` names
/// only presence flags.
pub struct ApprovalKeys {
    material: KeyMaterial,
    require_key: bool,
}

impl ApprovalKeys {
    /// Resolves the keys from an arbitrary environment, as [`KeyMaterial::from_lookup`], and reads `SWARM_REQUIRE_KEY` from the same
    /// snapshot. `presence` classifies the conventional key-file path.
    ///
    /// # Errors
    ///
    /// [`Declined`] when Python must answer (see `gsd_envelope`).
    pub fn from_lookup(
        get: impl Fn(&str) -> Option<OsString>,
        presence: impl Fn(&Path) -> KeyFilePresence,
    ) -> Result<Self, Declined> {
        let require_key = get("SWARM_REQUIRE_KEY").is_some_and(|value| value == "1");
        let material = KeyMaterial::from_lookup(&get, presence)?;
        Ok(Self {
            material,
            require_key,
        })
    }

    /// Resolves the keys from the process environment and the real file system.
    ///
    /// # Errors
    ///
    /// [`Declined`] when Python must answer.
    pub fn from_env() -> Result<Self, Declined> {
        Self::from_lookup(
            |name| std::env::var_os(name),
            conventional_key_file_presence,
        )
    }

    /// `SWARM_REQUIRE_KEY == "1"`.
    pub fn require_key(&self) -> bool {
        self.require_key
    }

    /// `real_key_configured()`.
    pub fn real_key(&self) -> bool {
        self.material.real_key_configured()
    }

    /// `insecure_dev_key() is not None`.
    pub fn dev_key(&self) -> bool {
        self.material.dev_key_active()
    }

    /// `verify_envelope` of the stored envelope `text`.
    ///
    /// # Errors
    ///
    /// [`Declined`] when the text is one Python's `json` reads but this crate cannot represent exactly.
    fn verify_text(&self, text: &str) -> Result<bool, Declined> {
        Ok(verify_envelope(&parse_signed(text)?, &self.material))
    }

    /// The host's signature answer for one verdict row, by the rule [`gather`] applies to the rows it reads: a row with no envelope
    /// text, or whose envelope is not a valid `gate.verdict` shape, is `false` without a verification; any other row is verified
    /// with this call's keys. Used by the ingest snapshot, which verifies the rows the planner may read (`TaskStoreWriter::ingest`).
    ///
    /// # Errors
    ///
    /// A not-started [`WriterError`] when the envelope text is one Python's `json` reads but this crate cannot represent exactly
    /// ([`Declined`]): the whole call is declined, never read as `false`.
    pub(crate) fn row_sig_ok(&self, entry: &VerdictFacts) -> Result<bool, WriterError> {
        match (&entry.envelope, entry.row.envelope_json.as_deref()) {
            (EnvelopeState::Invalid, _) | (_, None | Some("")) => Ok(false),
            (_, Some(text)) => self.verify_text(text).map_err(declined),
        }
    }
}

impl fmt::Debug for ApprovalKeys {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ApprovalKeys")
            .field("material", &self.material)
            .field("require_key", &self.require_key)
            .finish()
    }
}

/// Where the writer gets the keys and the clock of one APPROVED call. Implementations hand out a fresh snapshot per call.
pub trait ApprovalSource: Send + Sync + fmt::Debug {
    /// The keys for this call.
    ///
    /// # Errors
    ///
    /// [`Declined`] when Python must answer; the writer refuses before it opens its transaction.
    fn keys(&self) -> Result<ApprovalKeys, Declined>;

    /// The clock reading the expiry of a verdict is compared with (`issued_at + expires_s > now` of `_check_row`), in seconds since the
    /// epoch. The writer calls it ONCE per call, INSIDE the transaction, after the write lock was taken and the verdict rows were
    /// read: Python reads `time.time()` inside its lock (`missing_gate_reasons`), so a verdict that expired while the call waited for
    /// the lock is expired. `requested_now` is the call's own reading, taken before the wait; it stays the `updated_at` and the audit
    /// `ts` of the write and is only a lower bound here. There is no default: an implementation that returned `requested_now` would
    /// accept a verdict that expired during the wait.
    fn clock(&self, requested_now: f64) -> f64;
}

/// The production source: the process environment and the wall clock, read once per call.
#[derive(Debug, Clone, Copy, Default)]
pub struct EnvApprovalSource;

impl ApprovalSource for EnvApprovalSource {
    fn keys(&self) -> Result<ApprovalKeys, Declined> {
        ApprovalKeys::from_env()
    }

    /// The wall clock now, never earlier than `requested_now` (a clock that stepped back must not make a verdict younger than the
    /// call's own reading says).
    fn clock(&self, requested_now: f64) -> f64 {
        let wall = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(requested_now, |elapsed| elapsed.as_secs_f64());
        wall.max(requested_now)
    }
}

/// A source over a fixed environment snapshot (name, value pairs), for tests and for callers that must not read the process
/// environment. The conventional key file is still classified on the real file system, from the snapshot's `HOME` and
/// `XDG_CONFIG_HOME`.
#[derive(Clone, Default)]
pub struct FixedApprovalSource {
    vars: Vec<(String, String)>,
}

impl FixedApprovalSource {
    /// A source whose environment is exactly `vars`.
    pub fn new(vars: Vec<(String, String)>) -> Self {
        Self { vars }
    }
}

impl fmt::Debug for FixedApprovalSource {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "FixedApprovalSource({} variables)", self.vars.len())
    }
}

impl ApprovalSource for FixedApprovalSource {
    fn keys(&self) -> Result<ApprovalKeys, Declined> {
        ApprovalKeys::from_lookup(
            |name| {
                self.vars
                    .iter()
                    .find(|(key, _)| key == name)
                    .map(|(_, value)| OsString::from(value))
            },
            conventional_key_file_presence,
        )
    }

    /// The call's own reading: a fixed source has no wall clock, so a test decides the time by the `now` it passes.
    fn clock(&self, requested_now: f64) -> f64 {
        requested_now
    }
}

/// The refusal before any write for a [`Declined`] or an [`Unsupported`] input: not started, Python answers. The text is fixed; it
/// names the kind of input and never a value.
pub(crate) fn declined(error: Declined) -> WriterError {
    WriterError::not_started(
        ErrorKind::Unsupported,
        format!("approval: Python answers this call ({error:?})"),
    )
}

fn unsupported(error: Unsupported) -> WriterError {
    WriterError::not_started(
        ErrorKind::Unsupported,
        format!("approval: Python answers this call ({error})"),
    )
}

/// The text of a string column of `task`, `None` for `NULL`, and `Err` for any other type.
fn text_column<'a>(task: &'a Fields, name: &str) -> Result<Option<&'a str>, WriterError> {
    match obj_get(task, name) {
        Some(Json::Str(text)) => Ok(Some(text)),
        Some(Json::Null) | None => Ok(None),
        Some(_) => Err(WriterError::not_started(
            ErrorKind::Unsupported,
            format!("approval: task column {name} is not text"),
        )),
    }
}

/// Reads the approval facts of `task` inside `tx`, the transaction that will write the transition.
///
/// `task` is the row [`Tx::task`] returned in this transaction; `requested_now` is the call's own clock reading, taken before the
/// write lock was awaited. The expiry (Python's `_check_row`: `issued_at + expires_s > now`) is compared with
/// [`ApprovalSource::clock`], read here once the rows are in hand and so after the lock was taken, as Python reads `time.time()` inside
/// its lock. When the key policy refuses (steps 1 and 2 of the module documentation) no row is read: the decision refuses with the
/// policy text first, as Python does, and the refusal does not depend on the rows.
///
/// # Errors
///
/// A not-started [`WriterError`] when Python must answer: a [`Declined`] row, an [`Unsupported`] input, a column of the wrong
/// type, or a store read that failed.
pub(crate) async fn gather(
    tx: &mut Tx,
    keys: &ApprovalKeys,
    source: &dyn ApprovalSource,
    task: &Fields,
    requested_now: f64,
) -> Result<ApprovalFacts, WriterError> {
    let (require_key, real_key, dev_key) = (keys.require_key(), keys.real_key(), keys.dev_key());
    let mut facts = ApprovalFacts {
        require_key,
        real_key,
        dev_key,
        missing_gates: Vec::new(),
        unaccepted_gates: Vec::new(),
    };
    if (require_key && !real_key) || (!real_key && !dev_key) {
        return Ok(facts);
    }

    let (Some(task_id), Some(correlation_id)) = (
        text_column(task, "task_id")?,
        text_column(task, "correlation_id")?,
    ) else {
        return Err(WriterError::not_started(
            ErrorKind::Unsupported,
            "approval: the task row has no task_id or correlation_id",
        ));
    };
    let target = TaskFacts::from_row(
        task_id,
        correlation_id,
        text_column(task, "risk_class")?,
        text_column(task, "notes")?,
    )
    .map_err(unsupported)?;

    let rows = tx.verdict_rows(task_id, target.verdicts_since).await?;
    let mut rows: Vec<VerdictFacts> = rows
        .into_iter()
        .map(|row| VerdictFacts::new(row, false))
        .collect();
    // Only the rows `_check_row` can reach are verified: the highest id of each required gate.
    let current: Vec<i64> = {
        let latest = latest_rows(&target, &rows, false);
        target
            .required_gates
            .iter()
            .filter_map(|gate| {
                latest
                    .iter()
                    .find(|(name, _)| name == gate)
                    .map(|(_, facts)| facts.row.id)
            })
            .collect()
    };
    for entry in rows
        .iter_mut()
        .filter(|entry| current.contains(&entry.row.id))
    {
        entry.sig_ok = match (&entry.envelope, entry.row.envelope_json.as_deref()) {
            (EnvelopeState::Invalid, _) | (_, None | Some("")) => false,
            (_, Some(text)) => keys.verify_text(text).map_err(declined)?,
        };
    }

    let now = source.clock(requested_now);
    let reasons = missing_gate_reasons(&target, &rows, now).map_err(unsupported)?;

    // The issuer rule reads the state of each gate task a passing current row names, in this same transaction.
    let mut issuers: Vec<IssuerState> = Vec::new();
    for (gate, entry) in latest_rows(&target, &rows, false) {
        let required = target.required_gates.iter().any(|name| name == gate);
        let reported = reasons.iter().any(|(name, _)| name == gate);
        if !required || reported || !entry.sig_ok {
            continue;
        }
        let EnvelopeState::Valid(envelope) = &entry.envelope else {
            continue;
        };
        let GateTask::Task(issuer) = &envelope.gate_task else {
            continue;
        };
        if issuers.iter().any(|known| known.task_id == *issuer) {
            continue;
        }
        if let Some(row) = tx.task(issuer).await? {
            if let Some(state) = text_column(&row, "state")? {
                issuers.push(IssuerState {
                    task_id: issuer.clone(),
                    state: state.to_string(),
                });
            }
        }
    }
    let unaccepted = unaccepted_gates(&target, &rows, &reasons, &issuers).map_err(unsupported)?;

    facts.missing_gates = reasons.into_iter().map(|(gate, _)| gate).collect();
    facts.unaccepted_gates = unaccepted;
    Ok(facts)
}
