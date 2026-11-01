//! Compiled scheduler entry: the existing std-only loop/pool, never a second controller.
//!
//! Frozen Python records (14.4-PLAN.md, physical producer freeze):
//! | # | Callback | Python return / record |
//! |---|---|---|
//! | 1 | `begin_pass(round: int, in_flight: list[str])` | `list[tuple[str, str \| None, int \| None, float \| None, float \| None]]`: `(task_id, priority, dag_depth, created_at, retry_after)` |
//! | 2 | `agent_for(task_id: str)` | `(agent_id: str, slug: str, max_parallel: int)` or `None` |
//! | 3 | `claims_for(task_id: str)` | `(claims: list[tuple[str, str]], None)` or `(None, error_text: str)`; claim pair `(pattern, mode)`, mode `"shared"` or `"exclusive"` from the existing Python parser, mapping to source `ClaimSpec.shared` |
//! | 4 | `block(task_id: str, reason: str)` | `None` |
//! | 5 | `lease(task_id: str, agent_id: str)` | `(dispatch: bool, status: str, holder: str \| None)` or `None` (leases off) |
//! | 6 | `observe_pass(info: dict[str, object])` | `None`; exact input keys `round`, `ready`, `submitted`, `effective_parallel`, `free` (integers), `in_flight` (submission-ordered `list[str]`), `skipped` (first-skipped-ordered `list[tuple[str, str]]` of task/reason), `unsubmitted` (selection-ordered `list[tuple[str, str]]` of granted task/agent leases not submitted after a stop or port error) |
//! | 7 | `submit(task_id: str, agent_id: str)` | tagged `("started", sid: str)` or `("failed", WorkerDoneTuple)`, never a bare SID |
//! | 8 | `worker(task_id: str, agent_id: str, sid: str)` | `WorkerDoneTuple` |
//! | 9 | `internal_failure(task_id: str, agent_id: str, text: str)` | `WorkerDoneTuple` |
//! | 10 | `complete(done: WorkerDoneTuple)` | `(failed: bool, rate_limited: float \| None, retry_attempt: int \| None, extension_down: bool, cancelled: bool)`, **not a reason-bearing tuple** |
//! | 11 | `set_retry_after(task_id: str, wall: float)` | `None` |
//! | 12 | `random_unit()` | `float` in `[-1, 1]`, one existing Python jitter sample |
//! | 13 | `cancel_state()` | `(is_set: bool, force: bool, reason: str \| None, elapsed_s: float)` |
//! | 14 | `cancel_begin(reason: str, grace_s: float, in_flight: int)` | `None` |
//! | 15 | `kill(grace_s: float, reason: str)` | `None` |
//! | 16 | `log(line: str)` | `None` |
//!
//! WorkerDoneTuple is (task_id: str, agent_id: str, outcome: str, error: str | None).
//! Exact builtin containers/scalars prevent coercion/defaults from hiding malformed host data.
//! None and zero remain distinct. Counts saturate unsigned overflow; negatives are E-INPUT. Ready-row `created_at` and `retry_after` are `None` or non-NaN reals: NaN is E-INPUT, while ±inf are kept because the oracle gives them meaning (`until > now`; a total `created_at` order).
//! A Python exception is opaque: only the two constants below enter PortError.summary. The
//! original loop PyErr (including SwarmError and traceback) is returned, never formatted.
//!
//! NativeSchedulerScope marks native entry, conversions and caught-payload disposal. Only
//! arbitrary host execution (including attribute access and Python object destruction) suspends
//! the mark. No Rust lock spans a callback. The core pool carries the submission's native mark.
//! Hook setup must be cooperative/quiescent at first entry. Later/concurrent host hook replacement
//! is unsupported and can defeat confidentiality; we never reinstall. Unavailable TLS, panic=abort,
//! arbitrary host output and abandoned host callbacks have the core producer's documented limits.
//!
//! Detached callback lifetime follows the existing core, including on a loop PyErr:
//! `Runtime::drive` closes the pool with zero wait; an in-flight job still owns its
//! `Arc<PythonPorts>`. A successful Python worker return is converted and its attachment
//! released before that job drops its Arc. On worker failure the opaque PyErr is disposed
//! after `internal_failure`; its traceback may also own the Python ports instance.
//! The last Arc disposes the sixteen callbacks in a separate attached host scope below.
//! Thus neither releasing a Python worker Event nor joining an unrelated signal sender
//! proves that the binding has released its Python references. A caller tearing down its
//! interpreter must allow that reference cleanup to finish first. The real signal consumer
//! witnesses the last bound-method owner becoming unreachable before interpreter shutdown.
//! This is not a worker join, a hard-preemption promise, or a stuck-worker exit guarantee.
//! PyO3 0.30.1 documents `Python::try_attach` as best-effort (including a finalization
//! check/attach race); replacing `attach` with it would not establish quiescence.
//!
//! Cleanup fence (14.4-NATIVE-CLEANUP-FENCE.md R1): each `run_scheduler` call takes one fresh `NativeCleanupFence`. The
//! fence becomes quiescent only after the entry is sealed, the final adapter disposal has returned, and every native error
//! ticket has been discharged after its actual restore or attached disposal returned. A held Rust `PyErr` is not a transfer.

use std::ffi::CStr;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::time::{Duration, Instant};

use gsd_sched_core::panic_guard::{with_host_callback, NativeSchedulerScope};
use gsd_sched_core::ports::{
    AgentInfo, CancelState, ClaimSpec, ClaimsVerdict, Completion, LeaseVerdict, PassInfo,
    PortError, ReadyRow, RunConfig, RunError, RunSummary, SchedulerPorts, Submission, WorkerDone,
    MAX_SECONDS,
};
use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyCFunction, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};

const LOOP_FAILED: &str = "E-INTERNAL: scheduler loop callback failed";
const WORKER_FAILED: &str = "E-INTERNAL: scheduler worker callback failed";
const NAMES: [&str; 16] = [
    "begin_pass",
    "agent_for",
    "claims_for",
    "block",
    "lease",
    "observe_pass",
    "submit",
    "worker",
    "internal_failure",
    "complete",
    "set_retry_after",
    "random_unit",
    "cancel_state",
    "cancel_begin",
    "kill",
    "log",
];

const FENCE_KEY: &str = "cleanup_fence";
const PORTS_KEY: &str = "ports";
const CONFIG_COUNT: usize = 16;
const CONFIG_KEYS: [&str; CONFIG_COUNT] = [
    "max_concurrent",
    "barrier",
    "once",
    "budget",
    "max_rounds",
    "fail_streak",
    "cooldown_s",
    "retry_cap_s",
    "backoff_base_s",
    "backoff_cap_s",
    "wait_tick_s",
    "internal_failure_limit",
    "cancel_grace_s",
    "shutdown_budget_s",
    "kill_grace_s",
    "kill_settle_s",
];
const UNEXPECTED_ARGUMENT: &str = "run_scheduler() got an unexpected argument";
const DUPLICATE_PORTS: &str = "run_scheduler() got multiple values for ports";
const MISSING_PORTS: &str = "run_scheduler() missing required argument: ports";
const MISSING_FENCE: &str = "run_scheduler() missing required keyword argument: cleanup_fence";
const MISSING_CONFIG: &str = "run_scheduler() missing a required configuration keyword argument";

fn invalid(operation: &'static str, rule: &'static str) -> PyErr {
    PyValueError::new_err(format!("E-INPUT: {operation} failed: {rule}"))
}

fn input(rule: &'static str) -> PyErr {
    invalid("run_scheduler", rule)
}

/// Argument-shape errors are fixed text: nothing from the caller's keys, values or reprs is formatted.
fn type_error(message: &'static str) -> PyErr {
    PyTypeError::new_err(message)
}

/// The real entry and worker boundaries share this catch, including payload destruction.
fn native_boundary<T, E: From<PyErr>>(work: impl FnOnce() -> Result<T, E>) -> Result<T, E> {
    let _native = NativeSchedulerScope::enter();
    match catch_unwind(AssertUnwindSafe(work)) {
        Ok(result) => result,
        Err(payload) => {
            if let Err(secondary) = catch_unwind(AssertUnwindSafe(|| drop(payload))) {
                // A second hostile destructor must not escape or recursively panic on drop.
                std::mem::forget(secondary);
            }
            Err(E::from(PyValueError::new_err(
                "E-INTERNAL: run_scheduler failed: internal panic",
            )))
        }
    }
}

fn boolean(value: &Bound<'_, PyAny>) -> PyResult<bool> {
    if !value.is_exact_instance_of::<PyBool>() {
        return Err(input("expected bool"));
    }
    value.extract()
}

fn text(value: &Bound<'_, PyAny>) -> PyResult<String> {
    if !value.is_exact_instance_of::<PyString>() {
        return Err(input("expected str"));
    }
    value.extract()
}

fn optional<T>(
    value: &Bound<'_, PyAny>,
    decode: impl FnOnce(&Bound<'_, PyAny>) -> PyResult<T>,
) -> PyResult<Option<T>> {
    if value.is_none() {
        Ok(None)
    } else {
        decode(value).map(Some)
    }
}

fn number(value: &Bound<'_, PyAny>) -> PyResult<f64> {
    if !value.is_exact_instance_of::<PyFloat>() && !value.is_exact_instance_of::<PyInt>() {
        return Err(input("expected real number"));
    }
    value.extract().map_err(|_| input("number out of range"))
}

/// A ready-row time (`created_at`, `retry_after`). NaN is rejected with E-INPUT. For `created_at` NaN has no ordering meaning. For
/// `retry_after` the oracle would not hold a task back on NaN (`until > now` is false), but the binding refuses it so that a host
/// never sends one by accident. Infinities are kept: `+inf` holds a task back for good (continuous mode then waits until cancel),
/// `-inf` never does, and both order totally.
fn ready_time(value: &Bound<'_, PyAny>) -> PyResult<f64> {
    let result = number(value)?;
    if result.is_nan() {
        return Err(input("ready-row time must not be NaN"));
    }
    Ok(result)
}

fn seconds(value: &Bound<'_, PyAny>) -> PyResult<f64> {
    let result = number(value)?;
    if !(0.0..=MAX_SECONDS).contains(&result) {
        return Err(input(
            "seconds must be finite and between zero and 1000000000",
        ));
    }
    Ok(result)
}

fn count(value: &Bound<'_, PyAny>, max: u64) -> PyResult<u64> {
    if !value.is_exact_instance_of::<PyInt>() {
        return Err(input("expected integer count"));
    }
    // Exact ints only: this comparison cannot invoke a user-defined rich comparison.
    if value.lt(0)? {
        return Err(input("count must be nonnegative"));
    }
    Ok(value.extract::<u64>().unwrap_or(u64::MAX).min(max))
}

fn count32(value: &Bound<'_, PyAny>) -> PyResult<u32> {
    count(value, u64::from(u32::MAX)).map(|value| value as u32)
}

fn depth(value: &Bound<'_, PyAny>) -> PyResult<i64> {
    if !value.is_exact_instance_of::<PyInt>() {
        return Err(input("expected integer depth"));
    }
    value.extract().map_err(|_| input("depth out of range"))
}

fn tuple<'py>(value: &Bound<'py, PyAny>, len: usize) -> PyResult<Bound<'py, PyTuple>> {
    if !value.is_exact_instance_of::<PyTuple>() {
        return Err(input("expected tuple"));
    }
    let result = value.cast::<PyTuple>()?;
    if result.len() != len {
        return Err(input("wrong tuple arity"));
    }
    Ok(result.clone())
}

fn list<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyList>> {
    if !value.is_exact_instance_of::<PyList>() {
        return Err(input("expected list"));
    }
    Ok(value.cast::<PyList>()?.clone())
}

fn nothing(value: &Bound<'_, PyAny>) -> PyResult<()> {
    if value.is_none() {
        Ok(())
    } else {
        Err(input("callback must return None"))
    }
}

fn done(value: &Bound<'_, PyAny>, task: &str, agent: &str) -> PyResult<WorkerDone> {
    let record = tuple(value, 4)?;
    let result = WorkerDone {
        task_id: text(&record.get_item(0)?)?,
        agent_id: text(&record.get_item(1)?)?,
        outcome: text(&record.get_item(2)?)?,
        error: optional(&record.get_item(3)?, text)?,
    };
    if result.task_id != task || result.agent_id != agent {
        return Err(input("worker result identity mismatch"));
    }
    Ok(result)
}

/// Pure Rust state of one run's cleanup fence, shared by the fence object, the entry owner, the adapter and every ticket.
/// It holds no Python reference, ID or backreference. `claimed` is the single-use boundary. Quiescence needs the entry sealed,
/// the final adapter disposal returned, and every native error ticket discharged after its actual restore or attached disposal
/// returned. Poisoned accounting is never quiescent.
#[derive(Default)]
struct FenceState {
    claimed: bool,
    entry_sealed: bool,
    disposed: bool,
    tickets: usize,
    poisoned: bool,
}

struct FenceShared {
    state: Mutex<FenceState>,
    changed: Condvar,
}

/// Returned by `wait` on a fence that run_scheduler has not claimed.
#[derive(Debug, PartialEq, Eq)]
struct Fresh;

impl FenceShared {
    fn fresh() -> Arc<Self> {
        Arc::new(Self {
            state: Mutex::new(FenceState::default()),
            changed: Condvar::new(),
        })
    }

    fn guard(&self) -> MutexGuard<'_, FenceState> {
        self.state.lock().unwrap_or_else(|poison| {
            let mut state = poison.into_inner();
            state.poisoned = true;
            state
        })
    }

    fn claim(self: &Arc<Self>) -> Option<EntryOwner> {
        let mut state = self.guard();
        if state.claimed {
            return None;
        }
        state.claimed = true;
        drop(state);
        Some(EntryOwner(Arc::clone(self)))
    }

    fn add_ticket(&self) {
        let mut state = self.guard();
        match state.tickets.checked_add(1) {
            Some(count) => state.tickets = count,
            None => state.poisoned = true,
        }
    }

    fn ack_ticket(&self) {
        {
            let mut state = self.guard();
            match state.tickets.checked_sub(1) {
                Some(count) => state.tickets = count,
                None => state.poisoned = true,
            }
        }
        self.changed.notify_all();
    }

    fn seal_entry(&self) {
        self.guard().entry_sealed = true;
        self.changed.notify_all();
    }

    fn mark_disposed(&self) {
        self.guard().disposed = true;
        self.changed.notify_all();
    }

    /// Checks at most once before the deadline. Spurious wakeups consume the same deadline. The guard is dropped before the
    /// caller reacquires the GIL, and no Python code runs under it.
    fn wait_until(&self, deadline: Instant) -> Result<bool, Fresh> {
        let mut state = self.guard();
        loop {
            if !state.claimed {
                return Err(Fresh);
            }
            if state.entry_sealed && state.disposed && state.tickets == 0 && !state.poisoned {
                return Ok(true);
            }
            let now = Instant::now();
            if now >= deadline {
                return Ok(false);
            }
            state = match self.changed.wait_timeout(state, deadline - now) {
                Ok((guard, _)) => guard,
                Err(poison) => {
                    let (mut guard, _) = poison.into_inner();
                    guard.poisoned = true;
                    guard
                }
            };
        }
    }
}

/// The claimed entry. It lives outside the fallible body and is sealed only by an explicit finish. Dropping it never seals.
struct EntryOwner(Arc<FenceShared>);

impl EntryOwner {
    fn shared(&self) -> Arc<FenceShared> {
        Arc::clone(&self.0)
    }

    fn seal(self) {
        self.0.seal_entry();
    }
}

/// One native-held error reference, counted until its restore or attached disposal has returned. Dropping a ticket without
/// discharging it keeps the fence pending.
struct Ticket(Arc<FenceShared>);

impl Ticket {
    fn register(shared: &Arc<FenceShared>) -> Self {
        shared.add_ticket();
        Self(Arc::clone(shared))
    }

    fn discharge(self) {
        self.0.ack_ticket();
    }
}

/// A Python error held in Rust. The outgoing carrier and any native worker share the same ticket.
/// The core may drop a worker error without the GIL, so disposal attaches and unmarks.
struct OpaquePyErr {
    error: Option<PyErr>,
    ticket: Option<Ticket>,
}

impl OpaquePyErr {
    fn new(error: PyErr, cleanup: Option<&Arc<FenceShared>>) -> Self {
        Self {
            error: Some(error),
            ticket: cleanup.map(Ticket::register),
        }
    }

    /// Restores the ORIGINAL exception inside an unmarked host scope. `checkpoint` runs after the restore returns and
    /// before the ticket is discharged.
    fn restore_with(mut self, py: Python<'_>, checkpoint: impl FnOnce()) {
        if let Some(error) = self.error.take() {
            with_host_callback(|| error.restore(py));
            checkpoint();
            if let Some(ticket) = self.ticket.take() {
                ticket.discharge();
            }
        }
    }
}

impl Drop for OpaquePyErr {
    fn drop(&mut self) {
        // An empty shell performed no hand-off, so it discharges nothing.
        if let Some(error) = self.error.take() {
            with_host_callback(|| Python::attach(|_| drop(error)));
            #[cfg(test)]
            witness("opaque-attach-returned");
            if let Some(ticket) = self.ticket.take() {
                ticket.discharge();
            }
        }
    }
}

/// An error leaving the native entry. A ticketed port error keeps its original object and ticket together.
enum Failure {
    Plain(PyErr),
    Opaque(OpaquePyErr),
}

impl From<PyErr> for Failure {
    fn from(error: PyErr) -> Self {
        Self::Plain(error)
    }
}

impl Failure {
    fn restore(self, py: Python<'_>) {
        self.restore_with(py, || {});
    }

    fn restore_with(self, py: Python<'_>, checkpoint: impl FnOnce()) {
        match self {
            Self::Plain(error) => {
                with_host_callback(|| error.restore(py));
                checkpoint();
            }
            Self::Opaque(error) => error.restore_with(py, checkpoint),
        }
    }
}

struct HostReturn<'py>(Bound<'py, PyAny>);

impl Drop for HostReturn<'_> {
    fn drop(&mut self) {
        // Replace with None so automatic field destruction is harmless, including on unwind.
        let py = self.0.py();
        let value = std::mem::replace(&mut self.0, py.None().into_bound(py));
        with_host_callback(|| drop(value));
    }
}

struct PythonPorts {
    callbacks: [Option<Py<PyAny>>; NAMES.len()],
    /// Set only after the run claims its fence. Capture failures before the claim are not run witnesses.
    cleanup: Option<Arc<FenceShared>>,
    #[cfg(test)]
    fault: TestFault,
}

impl PythonPorts {
    fn new(ports: &Bound<'_, PyAny>) -> PyResult<Self> {
        let mut result = Self {
            callbacks: std::array::from_fn(|_| None),
            cleanup: None,
            #[cfg(test)]
            fault: TEST_FAULT.with(|fault| fault.replace(TestFault::None)),
        };
        for (index, name) in NAMES.iter().enumerate() {
            let callback = with_host_callback(|| ports.getattr(*name))?;
            if !callback.is_callable() {
                with_host_callback(|| drop(callback));
                return Err(input("all sixteen ports must be callable"));
            }
            result.callbacks[index] = Some(callback.unbind());
        }
        Ok(result)
    }

    fn invoke<T>(
        &self,
        index: usize,
        work: impl for<'py> FnOnce(Python<'py>, &Bound<'py, PyAny>) -> PyResult<T>,
    ) -> Result<T, PortError> {
        let summary = if index == 7 {
            WORKER_FAILED
        } else {
            LOOP_FAILED
        };
        native_boundary::<_, PyErr>(|| {
            with_host_callback(|| {
                Python::attach(|py| {
                    native_boundary::<_, PyErr>(|| {
                        let callback = self.callbacks[index].as_ref().ok_or_else(|| {
                            PyValueError::new_err(
                                "E-INTERNAL: run_scheduler failed: unresolved callback",
                            )
                        })?;
                        work(py, callback.bind(py))
                    })
                })
            })
        })
        .map_err(|error| {
            PortError::new(
                summary,
                Box::new(OpaquePyErr::new(error, self.cleanup.as_ref())),
            )
        })
    }

    /// Host return ownership remains here until conversion ends; even rejected objects are
    /// destroyed while unmarked. Converters operate only on exact builtins, not host protocols.
    fn returned<'py, T>(
        call: impl FnOnce() -> PyResult<Bound<'py, PyAny>>,
        decode: impl FnOnce(&Bound<'py, PyAny>) -> PyResult<T>,
    ) -> PyResult<T> {
        let value = HostReturn(with_host_callback(call)?);
        decode(&value.0)
    }
}

impl Drop for PythonPorts {
    fn drop(&mut self) {
        with_host_callback(|| {
            Python::attach(|_| {
                for callback in &mut self.callbacks {
                    drop(callback.take());
                }
            })
        });
        #[cfg(test)]
        witness("adapter-attach-returned");
        // Acknowledged only after the whole attached disposal above has returned.
        if let Some(cleanup) = self.cleanup.take() {
            cleanup.mark_disposed();
        }
    }
}

impl SchedulerPorts for PythonPorts {
    fn begin_pass(&self, round: u64, in_flight: &[&str]) -> Result<Vec<ReadyRow>, PortError> {
        self.invoke(0, |py, callback| {
            let rows = Self::returned(
                || {
                    // Python container allocation can itself run host GC finalizers.
                    let ids = PyList::new(py, in_flight)?;
                    callback.call1((round, ids))
                },
                |value| {
                    list(value)?
                        .iter()
                        .map(|row| {
                            let record = tuple(&row, 5)?;
                            Ok(ReadyRow {
                                task_id: text(&record.get_item(0)?)?,
                                priority: optional(&record.get_item(1)?, text)?,
                                dag_depth: optional(&record.get_item(2)?, depth)?,
                                created_at: optional(&record.get_item(3)?, ready_time)?,
                                retry_after: optional(&record.get_item(4)?, ready_time)?,
                            })
                        })
                        .collect::<PyResult<Vec<_>>>()
                },
            )?;
            #[cfg(test)]
            if self.fault == TestFault::Loop {
                panic_payload("INERT_BINDING_LOOP_NATIVE");
            }
            Ok(rows)
        })
    }

    fn agent_for(&self, task_id: &str) -> Result<Option<AgentInfo>, PortError> {
        self.invoke(1, |_, callback| {
            Self::returned(
                || callback.call1((task_id,)),
                |value| {
                    optional(value, |value| {
                        let record = tuple(value, 3)?;
                        Ok(AgentInfo {
                            agent_id: text(&record.get_item(0)?)?,
                            slug: text(&record.get_item(1)?)?,
                            max_parallel: count32(&record.get_item(2)?)?,
                        })
                    })
                },
            )
        })
    }

    fn claims_for(&self, task_id: &str) -> Result<ClaimsVerdict, PortError> {
        self.invoke(2, |_, callback| {
            Self::returned(
                || callback.call1((task_id,)),
                |value| {
                    let record = tuple(value, 2)?;
                    let claims = record.get_item(0)?;
                    let error = record.get_item(1)?;
                    if claims.is_none() {
                        return text(&error).map(ClaimsVerdict::Invalid);
                    }
                    if !error.is_none() {
                        return Err(input("claims must have exactly one branch"));
                    }
                    let claims = list(&claims)?
                        .iter()
                        .map(|claim| {
                            let pair = tuple(&claim, 2)?;
                            let mode = text(&pair.get_item(1)?)?;
                            let shared = match mode.as_str() {
                                "shared" => true,
                                "exclusive" => false,
                                _ => return Err(input("claim mode must be shared or exclusive")),
                            };
                            Ok(ClaimSpec {
                                pattern: text(&pair.get_item(0)?)?,
                                shared,
                            })
                        })
                        .collect::<PyResult<Vec<_>>>()?;
                    Ok(ClaimsVerdict::Claims(claims))
                },
            )
        })
    }

    fn block(&self, task_id: &str, reason: &str) -> Result<(), PortError> {
        self.invoke(3, |_, callback| {
            Self::returned(|| callback.call1((task_id, reason)), nothing)
        })
    }

    fn lease(&self, task_id: &str, agent_id: &str) -> Result<Option<LeaseVerdict>, PortError> {
        self.invoke(4, |_, callback| {
            Self::returned(
                || callback.call1((task_id, agent_id)),
                |value| {
                    optional(value, |value| {
                        let record = tuple(value, 3)?;
                        Ok(LeaseVerdict {
                            dispatch: boolean(&record.get_item(0)?)?,
                            status: text(&record.get_item(1)?)?,
                            holder: optional(&record.get_item(2)?, text)?,
                        })
                    })
                },
            )
        })
    }

    fn observe_pass(&self, info: &PassInfo<'_>) -> Result<(), PortError> {
        self.invoke(5, |py, callback| {
            Self::returned(
                || {
                    let record = PyDict::new(py);
                    record.set_item("round", info.round)?;
                    record.set_item("ready", info.ready)?;
                    record.set_item("submitted", info.submitted)?;
                    record.set_item("effective_parallel", info.effective_parallel)?;
                    record.set_item("free", info.free)?;
                    record.set_item("in_flight", PyList::new(py, info.in_flight)?)?;
                    record.set_item(
                        "skipped",
                        PyList::new(
                            py,
                            info.skipped.iter().map(|row| (&row.task_id, &row.reason)),
                        )?,
                    )?;
                    record.set_item(
                        "unsubmitted",
                        PyList::new(
                            py,
                            info.unsubmitted
                                .iter()
                                .map(|(task_id, agent_id)| (task_id, agent_id)),
                        )?,
                    )?;
                    callback.call1((record,))
                },
                nothing,
            )
        })
    }

    fn submit(&self, task_id: &str, agent_id: &str) -> Result<Submission, PortError> {
        self.invoke(6, |_, callback| {
            Self::returned(
                || callback.call1((task_id, agent_id)),
                |value| {
                    let record = tuple(value, 2)?;
                    match text(&record.get_item(0)?)?.as_str() {
                        "started" => Ok(Submission::Started {
                            sid: text(&record.get_item(1)?)?,
                        }),
                        "failed" => {
                            done(&record.get_item(1)?, task_id, agent_id).map(Submission::Failed)
                        }
                        _ => Err(input("submission must be tagged started or failed")),
                    }
                },
            )
        })
    }

    fn worker(&self, task_id: &str, agent_id: &str, sid: &str) -> Result<WorkerDone, PortError> {
        self.invoke(7, |_, callback| {
            let result = Self::returned(
                || callback.call1((task_id, agent_id, sid)),
                |value| done(value, task_id, agent_id),
            )?;
            #[cfg(test)]
            if self.fault == TestFault::Worker && task_id == "fault" {
                panic_payload("INERT_BINDING_WORKER_NATIVE");
            }
            Ok(result)
        })
    }

    fn internal_failure(
        &self,
        task_id: &str,
        agent_id: &str,
        _text: &str,
    ) -> Result<WorkerDone, PortError> {
        // Core prefixes PortError.summary for historical adapters. Do not forward that decorated
        // string or a panic-specific variant: the Python persistence boundary gets this allowlist.
        self.invoke(8, |_, callback| {
            Self::returned(
                || callback.call1((task_id, agent_id, WORKER_FAILED)),
                |value| done(value, task_id, agent_id),
            )
        })
    }

    fn complete(&self, done: &WorkerDone) -> Result<Completion, PortError> {
        self.invoke(9, |_, callback| {
            Self::returned(
                || callback.call1(((&done.task_id, &done.agent_id, &done.outcome, &done.error),)),
                |value| {
                    let record = tuple(value, 5)?;
                    Ok(Completion {
                        failed: boolean(&record.get_item(0)?)?,
                        rate_limited: optional(&record.get_item(1)?, seconds)?,
                        retry_attempt: optional(&record.get_item(2)?, count32)?,
                        extension_down: boolean(&record.get_item(3)?)?,
                        cancelled: boolean(&record.get_item(4)?)?,
                    })
                },
            )
        })
    }

    fn set_retry_after(&self, task_id: &str, wall: f64) -> Result<(), PortError> {
        self.invoke(10, |_, callback| {
            Self::returned(|| callback.call1((task_id, wall)), nothing)
        })
    }

    fn random_unit(&self) -> Result<f64, PortError> {
        self.invoke(11, |_, callback| {
            Self::returned(
                || callback.call0(),
                |value| {
                    let sample = number(value)?;
                    if !(-1.0..=1.0).contains(&sample) {
                        return Err(input("random sample must be in [-1, 1]"));
                    }
                    Ok(sample)
                },
            )
        })
    }

    fn cancel_state(&self) -> Result<CancelState, PortError> {
        self.invoke(12, |py, callback| {
            with_host_callback(|| py.check_signals())?;
            Self::returned(
                || callback.call0(),
                |value| {
                    let record = tuple(value, 4)?;
                    Ok(CancelState {
                        is_set: boolean(&record.get_item(0)?)?,
                        force: boolean(&record.get_item(1)?)?,
                        reason: optional(&record.get_item(2)?, text)?,
                        elapsed_s: seconds(&record.get_item(3)?)?,
                    })
                },
            )
        })
    }

    fn cancel_begin(&self, reason: &str, grace_s: f64, in_flight: usize) -> Result<(), PortError> {
        self.invoke(13, |_, callback| {
            Self::returned(|| callback.call1((reason, grace_s, in_flight)), nothing)
        })
    }

    fn kill(&self, grace_s: f64, reason: &str) -> Result<(), PortError> {
        self.invoke(14, |_, callback| {
            Self::returned(|| callback.call1((grace_s, reason)), nothing)
        })
    }

    fn log(&self, line: &str) -> Result<(), PortError> {
        self.invoke(15, |_, callback| {
            Self::returned(|| callback.call1((line,)), nothing)
        })
    }
}

/// The original port exception, carried with its ticket. Any other payload becomes a fixed internal error.
fn port_failure(error: PortError) -> Failure {
    match error.into_payload().downcast::<OpaquePyErr>() {
        Ok(opaque) => Failure::Opaque(*opaque),
        Err(payload) => {
            drop(payload);
            Failure::Plain(PyValueError::new_err(
                "E-INTERNAL: run_scheduler failed: invalid port payload",
            ))
        }
    }
}

fn summary<'py>(py: Python<'py>, result: RunSummary) -> PyResult<Bound<'py, PyDict>> {
    // Outbound Python allocation is host execution too (it may trigger GC/finalizers).
    with_host_callback(|| {
        let dict = PyDict::new(py);
        dict.set_item("rounds", result.rounds)?;
        dict.set_item("dispatched", result.dispatched)?;
        dict.set_item("effective_parallel", result.effective_parallel)?;
        dict.set_item("closed", result.closed)?;
        dict.set_item("extension_down", result.extension_down)?;
        dict.set_item("abandoned", result.abandoned)?;
        dict.set_item("stopped", result.stopped.as_str())?;
        dict.set_item("waiting", result.waiting)?;
        Ok(dict)
    })
}

/// Single-use cleanup observer for one `run_scheduler` call. Pure Rust state with no Python reference, ID or backreference.
/// `wait` is pending (`False`) until the run's native references are released. `True` is terminal and repeatable, and a
/// claimed fence is never reused. A fresh fence raises `ValueError` (`E-INPUT`).
#[pyclass(name = "NativeCleanupFence", frozen, module = "swarm._native")]
pub(crate) struct NativeCleanupFence {
    shared: Arc<FenceShared>,
}

#[pymethods]
impl NativeCleanupFence {
    #[new]
    fn new() -> Self {
        Self {
            shared: FenceShared::fresh(),
        }
    }

    /// Waits at most `timeout_s` seconds (zero polls for 0). Returns `True` only when the run is quiescent. The GIL is
    /// released while waiting. `ValueError` (`E-INPUT`) for a fresh fence or an invalid timeout.
    fn wait(&self, py: Python<'_>, timeout_s: &Bound<'_, PyAny>) -> PyResult<bool> {
        let timeout = wait_seconds(timeout_s)?;
        let deadline = Instant::now()
            .checked_add(Duration::from_secs_f64(timeout))
            .ok_or_else(|| invalid("NativeCleanupFence.wait", "timeout is not representable"))?;
        let shared = Arc::clone(&self.shared);
        py.detach(move || shared.wait_until(deadline))
            .map_err(|Fresh| {
                invalid(
                    "NativeCleanupFence.wait",
                    "handle was not claimed by run_scheduler",
                )
            })
    }
}

fn wait_seconds(value: &Bound<'_, PyAny>) -> PyResult<f64> {
    const OPERATION: &str = "NativeCleanupFence.wait";
    if !value.is_exact_instance_of::<PyFloat>() && !value.is_exact_instance_of::<PyInt>() {
        return Err(invalid(OPERATION, "expected real number"));
    }
    let seconds: f64 = value
        .extract()
        .map_err(|_| invalid(OPERATION, "number out of range"))?;
    if !(0.0..=MAX_SECONDS).contains(&seconds) {
        return Err(invalid(
            OPERATION,
            "timeout must be finite and between zero and 1000000000",
        ));
    }
    Ok(seconds)
}

/// The entry callback body shared by production and the embedded test module. Every failure is restored as the original
/// exception object. A successful summary is returned once. The entry is sealed only after that hand-off.
fn run_scheduler_entry(
    args: &Bound<'_, PyTuple>,
    kwargs: Option<&Bound<'_, PyDict>>,
) -> *mut pyo3::ffi::PyObject {
    let py = args.py();
    let mut entry = None;
    let outcome = native_boundary(|| call(py, args, kwargs, &mut entry));
    let pointer = match outcome {
        Ok(summary) => summary.into_ptr(),
        Err(failure) => {
            failure.restore(py);
            std::ptr::null_mut()
        }
    };
    if let Some(entry) = entry {
        entry.seal();
    }
    pointer
}

/// Binds, converts and captures everything before the atomic single-use claim. Nothing is dispatched or claimed on error.
fn call<'py>(
    py: Python<'py>,
    args: &Bound<'py, PyTuple>,
    kwargs: Option<&Bound<'py, PyDict>>,
    entry: &mut Option<EntryOwner>,
) -> Result<Bound<'py, PyDict>, Failure> {
    let bound = bind(args, kwargs)?;
    let handle = bound
        .fence
        .cast::<NativeCleanupFence>()
        .map_err(|_| type_error("cleanup_fence must be a NativeCleanupFence"))?;
    let shared = Arc::clone(&handle.get().shared);
    let config = run_config(bound.config)?;
    #[cfg(test)]
    if TEST_FAULT.with(|fault| fault.get() == TestFault::Entry) {
        TEST_FAULT.with(|fault| fault.set(TestFault::None));
        panic_payload("INERT_BINDING_ENTRY_NATIVE");
    }
    let mut ports = PythonPorts::new(&bound.ports)?;
    let owner = shared
        .claim()
        .ok_or_else(|| invalid("run_scheduler", "cleanup_fence was already used"))?;
    ports.cleanup = Some(owner.shared());
    *entry = Some(owner);
    let ports: Arc<dyn SchedulerPorts> = Arc::new(ports);
    let outcome = py.detach(|| gsd_sched_core::runtime::run(ports, &config));
    match outcome {
        Ok(result) => Ok(summary(py, result)?),
        Err(RunError::Sched(error)) => Err(Failure::Plain(PyValueError::new_err(
            error.wire_message("run_scheduler"),
        ))),
        Err(RunError::Port(error)) => Err(port_failure(error)),
    }
}

/// The arguments by name: `ports` positional-or-keyword (zero or one positional), then `cleanup_fence` and the sixteen
/// configuration values keyword-only. Unknown, duplicate, missing and extra positional arguments are `TypeError`.
struct Arguments<'py> {
    ports: Bound<'py, PyAny>,
    fence: Bound<'py, PyAny>,
    config: [Option<Bound<'py, PyAny>>; CONFIG_COUNT],
}

fn bind<'py>(
    args: &Bound<'py, PyTuple>,
    kwargs: Option<&Bound<'py, PyDict>>,
) -> PyResult<Arguments<'py>> {
    if args.len() > 1 {
        return Err(type_error(
            "run_scheduler() takes at most one positional argument",
        ));
    }
    let mut ports = if args.len() == 1 {
        Some(args.get_item(0)?)
    } else {
        None
    };
    let mut fence = None;
    let mut config: [Option<Bound<'py, PyAny>>; CONFIG_COUNT] = std::array::from_fn(|_| None);
    if let Some(kwargs) = kwargs {
        for (key, value) in kwargs.iter() {
            let name = key
                .cast::<PyString>()
                .map_err(|_| type_error(UNEXPECTED_ARGUMENT))?
                .to_str()
                .map_err(|_| type_error(UNEXPECTED_ARGUMENT))?;
            if name == FENCE_KEY {
                fence = Some(value);
            } else if name == PORTS_KEY {
                if ports.is_some() {
                    return Err(type_error(DUPLICATE_PORTS));
                }
                ports = Some(value);
            } else if let Some(index) = CONFIG_KEYS.iter().position(|field| *field == name) {
                config[index] = Some(value);
            } else {
                return Err(type_error(UNEXPECTED_ARGUMENT));
            }
        }
    }
    Ok(Arguments {
        ports: ports.ok_or_else(|| type_error(MISSING_PORTS))?,
        fence: fence.ok_or_else(|| type_error(MISSING_FENCE))?,
        config,
    })
}

fn present(slot: Option<Bound<'_, PyAny>>) -> PyResult<Bound<'_, PyAny>> {
    slot.ok_or_else(|| type_error(MISSING_CONFIG))
}

fn run_config(values: [Option<Bound<'_, PyAny>>; CONFIG_COUNT]) -> PyResult<RunConfig> {
    let [max_concurrent, barrier, once, budget, max_rounds, fail_streak, cooldown_s, retry_cap_s, backoff_base_s, backoff_cap_s, wait_tick_s, internal_failure_limit, cancel_grace_s, shutdown_budget_s, kill_grace_s, kill_settle_s] =
        values;
    let config = RunConfig {
        max_concurrent: count32(&present(max_concurrent)?)?,
        barrier: boolean(&present(barrier)?)?,
        once: boolean(&present(once)?)?,
        budget: count(&present(budget)?, u64::MAX)?,
        max_rounds: count(&present(max_rounds)?, u64::MAX)?,
        fail_streak: count32(&present(fail_streak)?)?,
        cooldown_s: number(&present(cooldown_s)?)?,
        retry_cap_s: number(&present(retry_cap_s)?)?,
        backoff_base_s: number(&present(backoff_base_s)?)?,
        backoff_cap_s: number(&present(backoff_cap_s)?)?,
        wait_tick_s: number(&present(wait_tick_s)?)?,
        internal_failure_limit: count32(&present(internal_failure_limit)?)?,
        cancel_grace_s: number(&present(cancel_grace_s)?)?,
        shutdown_budget_s: number(&present(shutdown_budget_s)?)?,
        kill_grace_s: number(&present(kill_grace_s)?)?,
        kill_settle_s: number(&present(kill_settle_s)?)?,
    };
    config
        .validate()
        .map_err(|error| PyValueError::new_err(error.wire_message("run_scheduler")))?;
    Ok(config)
}

const RUN_SCHEDULER_DOC: &CStr = c"run_scheduler(ports, *, cleanup_fence, max_concurrent, barrier, once, budget, max_rounds, fail_streak, cooldown_s, retry_cap_s, backoff_base_s, backoff_cap_s, wait_tick_s, internal_failure_limit, cancel_grace_s, shutdown_budget_s, kill_grace_s, kill_settle_s)\n--\n\nRun the existing native loop/pool with sixteen host callbacks, sixteen required options and one fresh NativeCleanupFence. Returns exactly rounds, dispatched, effective_parallel, closed, extension_down, abandoned, stopped and waiting. Loop exceptions are re-raised as the original object; worker and native faults use fixed E-INTERNAL summaries. The fence is single use and becomes quiescent only after native references are released.";

/// The `run_scheduler` function object: one stateless closure with no captured Python reference, used by the production
/// module and the embedded test module alike.
pub(crate) fn run_scheduler_function(py: Python<'_>) -> PyResult<Bound<'_, PyCFunction>> {
    PyCFunction::new_closure(
        py,
        Some(c"run_scheduler"),
        Some(RUN_SCHEDULER_DOC),
        |args: &Bound<'_, PyTuple>, kwargs: Option<&Bound<'_, PyDict>>| {
            run_scheduler_entry(args, kwargs)
        },
    )
}

#[cfg(test)]
#[derive(Clone, Copy, PartialEq, Eq)]
enum TestFault {
    None,
    Entry,
    Loop,
    Worker,
}

#[cfg(test)]
thread_local! {
    static TEST_FAULT: std::cell::Cell<TestFault> = const { std::cell::Cell::new(TestFault::None) };
}

#[cfg(test)]
struct TestPayload(&'static str);

#[cfg(test)]
impl Drop for TestPayload {
    fn drop(&mut self) {
        panic!("INERT_BINDING_PAYLOAD_DROP {}", self.0);
    }
}

#[cfg(test)]
fn panic_payload(canary: &'static str) -> ! {
    std::panic::panic_any(TestPayload(canary));
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::ffi::CString;
    use std::io::Read;
    use std::process::{Command, Stdio};
    use std::thread;
    use std::time::{Duration, Instant};

    const CHILD_ENV: &str = "GSD_PYTHON_SCHED_WRAPPER_CHILD";
    const WAIT: Duration = Duration::from_secs(20);
    const PRIOR: &str = "BINDING_PRIOR_HOOK";

    fn prior_hook() {
        std::panic::set_hook(Box::new(|info| {
            if let Some(payload) = info.payload().downcast_ref::<TestPayload>() {
                eprintln!("{PRIOR}: {}", payload.0);
            } else {
                eprintln!("{PRIOR}: {info}");
            }
        }));
    }

    // All additions to this module are test-only: no injector, probe, control, hook setter or
    // debug function is registered by the shipped extension.
    #[pyfunction]
    fn host_probe() {
        let payload = catch_unwind(|| panic!("INERT_BINDING_HOST")).expect_err("host panic");
        drop(payload);
    }

    #[pyfunction]
    fn unrelated_probe() {
        thread::spawn(|| {
            let payload =
                catch_unwind(|| panic!("INERT_BINDING_UNRELATED")).expect_err("unrelated panic");
            drop(payload);
        })
        .join()
        .unwrap();
    }

    #[pyfunction]
    fn arm_loop() {
        TEST_FAULT.with(|fault| fault.set(TestFault::Loop));
    }

    const FIXTURE: &str = r#"
import threading
SAFE = "E-INTERNAL: scheduler worker callback failed"
class Ports:
    def __init__(self, tasks=("fault", "sibling")):
        self.tasks = tasks
        self.finished = []
        self.failures = []
        self.workers = []
    def begin_pass(self, round, in_flight):
        return [(t, None, None, None, None) for t in self.tasks]
    def agent_for(self, task): return ("agent", "slug", 2)
    def claims_for(self, task): return ([], None)
    def block(self, *args): pass
    def lease(self, *args): return None
    def observe_pass(self, info):
        assert list(info) == ["round", "ready", "submitted", "effective_parallel", "free", "in_flight", "skipped", "unsubmitted"]
    def submit(self, task, agent): return ("started", "sid-" + task)
    def worker(self, task, agent, sid):
        self.workers.append(task)
        return (task, agent, "DONE", None)
    def internal_failure(self, task, agent, text):
        assert text == SAFE
        self.failures.append(text)
        return (task, agent, "FAILED", text)
    def complete(self, done):
        self.finished.append(done)
        return (done[2] == "FAILED", None, None, False, False)
    def set_retry_after(self, *args): pass
    def random_unit(self): return 0.0
    def cancel_state(self): return (False, False, None, 0.0)
    def cancel_begin(self, *args): pass
    def kill(self, *args): pass
    def log(self, *args): pass
def run(ports):
    return binding.run_scheduler(ports, cleanup_fence=binding.NativeCleanupFence(), max_concurrent=2, barrier=False, once=True,
        budget=2, max_rounds=1, fail_streak=0, cooldown_s=0., retry_cap_s=1.,
        backoff_base_s=.001, backoff_cap_s=.01, wait_tick_s=.001, internal_failure_limit=1,
        cancel_grace_s=0., shutdown_budget_s=.1, kill_grace_s=0., kill_settle_s=0.)
"#;

    fn embedded(code: &str) {
        Python::initialize();
        Python::attach(|py| {
            let module = PyModule::new(py, "_binding_under_test").unwrap();
            module
                .add_function(run_scheduler_function(py).unwrap())
                .unwrap();
            module.add_class::<NativeCleanupFence>().unwrap();
            module
                .add_function(wrap_pyfunction!(host_probe, &module).unwrap())
                .unwrap();
            module
                .add_function(wrap_pyfunction!(unrelated_probe, &module).unwrap())
                .unwrap();
            module
                .add_function(wrap_pyfunction!(arm_loop, &module).unwrap())
                .unwrap();
            let globals = PyDict::new(py);
            globals.set_item("binding", module).unwrap();
            py.run(
                CString::new(FIXTURE).unwrap().as_c_str(),
                Some(&globals),
                None,
            )
            .unwrap();
            py.run(CString::new(code).unwrap().as_c_str(), Some(&globals), None)
                .unwrap();
        });
    }

    /// Concurrent readers retain FULL stdout/stderr. Exact filter and nonzero executed count
    /// prevent a false green from an empty child. Every deliberate panic and hook mutation is
    /// child-only; the shared cargo-test parent never touches a hook or embedded interpreter.
    fn captured(test: &str, case: &str) -> String {
        let mut child = Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                &format!("sched_run::tests::{test}"),
                "--nocapture",
                "--test-threads=1",
            ])
            .env(CHILD_ENV, case)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let mut out = child.stdout.take().unwrap();
        let mut err = child.stderr.take().unwrap();
        let stdout = thread::spawn(move || {
            let mut bytes = Vec::new();
            out.read_to_end(&mut bytes).unwrap();
            bytes
        });
        let stderr = thread::spawn(move || {
            let mut bytes = Vec::new();
            err.read_to_end(&mut bytes).unwrap();
            bytes
        });
        let deadline = Instant::now() + WAIT;
        let (status, timed_out) = loop {
            if let Some(status) = child.try_wait().unwrap() {
                break (status, false);
            }
            if Instant::now() >= deadline {
                child.kill().unwrap();
                break (child.wait().unwrap(), true);
            }
            thread::sleep(Duration::from_millis(10));
        };
        let stdout = String::from_utf8(stdout.join().unwrap()).unwrap();
        let stderr = String::from_utf8(stderr.join().unwrap()).unwrap();
        assert!(
            !timed_out && status.success(),
            "{case}: {status}, timeout={timed_out}\n{stdout}\n{stderr}"
        );
        assert!(
            stdout.contains("running 1 test") && stdout.contains("1 passed"),
            "no child proof: {stdout}\n{stderr}"
        );
        assert!(
            stdout.contains("WRAPPER_PROOF_OK"),
            "missing actual wrapper witness: {stdout}\n{stderr}"
        );
        stderr
    }

    #[test]
    fn wrapper_panic_stderr_is_scoped_and_safe() {
        if let Ok(case) = std::env::var(CHILD_ENV) {
            prior_hook();
            match case.as_str() {
                "entry" | "loop" => {
                    TEST_FAULT.with(|fault| {
                        fault.set(if case == "entry" {
                            TestFault::Entry
                        } else {
                            TestFault::Loop
                        })
                    });
                    embedded(
                        r#"
try:
    run(Ports(()))
except ValueError as error:
    assert str(error) == "E-INTERNAL: run_scheduler failed: internal panic"
    assert type(error) is ValueError
else:
    raise AssertionError("actual binding loop panic did not reach the boundary")
binding.host_probe()
"#,
                    );
                }
                "worker" => {
                    TEST_FAULT.with(|fault| fault.set(TestFault::Worker));
                    embedded(
                        r#"
p = Ports()
result = run(p)
assert result["dispatched"] == 2 and result["abandoned"] == []
assert p.failures == [SAFE]
assert sorted(p.workers) == ["fault", "sibling"]
assert sorted(p.finished) == [("fault", "agent", "FAILED", SAFE), ("sibling", "agent", "DONE", None)]
binding.host_probe()
"#,
                    );
                }
                _ => panic!("unknown wrapper child"),
            }
            println!("WRAPPER_PROOF_OK {case}");
            return;
        }
        for case in ["entry", "loop", "worker"] {
            let stderr = captured("wrapper_panic_stderr_is_scoped_and_safe", case);
            for canary in [
                "INERT_BINDING_ENTRY_NATIVE",
                "INERT_BINDING_LOOP_NATIVE",
                "INERT_BINDING_WORKER_NATIVE",
                "INERT_BINDING_PAYLOAD_DROP",
            ] {
                assert!(
                    !stderr.contains(canary),
                    "{case} leaked native payload: {stderr}"
                );
            }
            assert!(
                stderr.contains("INERT_BINDING_HOST"),
                "scope must restore after wrapper: {stderr}"
            );
            assert_eq!(
                stderr.matches(PRIOR).count(),
                1,
                "only the unmarked host probe reaches the prior hook: {stderr}"
            );
        }
    }

    #[test]
    fn wrapper_callbacks_suspend_reenter_and_preserve_unrelated_threads() {
        if std::env::var(CHILD_ENV).as_deref() == Ok("callbacks") {
            prior_hook();
            embedded(
                r#"
def nested():
    binding.host_probe()
    binding.arm_loop()
    try:
        run(Ports(()))
    except ValueError as error:
        assert str(error) == "E-INTERNAL: run_scheduler failed: internal panic"
    else:
        raise AssertionError("nested actual wrapper panic not caught")
    binding.host_probe()
class CallbackPorts(Ports):
    def __init__(self):
        super().__init__()
        self.gate = threading.Barrier(2, timeout=3)
    def begin_pass(self, round, in_flight):
        nested()
        binding.unrelated_probe()
        return super().begin_pass(round, in_flight)
    def worker(self, task, agent, sid):
        self.gate.wait()
        nested()
        return super().worker(task, agent, sid)
p = CallbackPorts()
assert run(p)["dispatched"] == 2
assert len(p.finished) == 2 and p.failures == []
class Finalizer:
    def __del__(self): binding.host_probe()
q = Ports(())
q.begin_pass = lambda *args: Finalizer()
try:
    run(q)
except ValueError as error:
    assert str(error).startswith("E-INPUT:")
else:
    raise AssertionError("malformed return not rejected")
binding.host_probe()
"#,
            );
            println!("WRAPPER_PROOF_OK callbacks");
            return;
        }
        let stderr = captured(
            "wrapper_callbacks_suspend_reenter_and_preserve_unrelated_threads",
            "callbacks",
        );
        assert!(!stderr.contains("INERT_BINDING_LOOP_NATIVE"), "{stderr}");
        assert!(!stderr.contains("INERT_BINDING_PAYLOAD_DROP"), "{stderr}");
        assert_eq!(
            stderr.matches("INERT_BINDING_HOST").count(),
            8,
            "callback/reentry/finalizer return must restore host state: {stderr}"
        );
        assert_eq!(
            stderr.matches("INERT_BINDING_UNRELATED").count(),
            1,
            "unrelated thread must remain unmarked: {stderr}"
        );
        assert_eq!(stderr.matches(PRIOR).count(), 9, "{stderr}");
    }

    #[test]
    fn wrapper_host_hook_replacement_is_unsupported() {
        if std::env::var(CHILD_ENV).as_deref() == Ok("replacement") {
            embedded("assert run(Ports(()))[\"stopped\"] == \"nothing-ready\"");
            // This deliberately violates the documented cooperative-host contract. We prove
            // the limit, not an unsupported promise to detect/reinstall over the host.
            std::panic::set_hook(Box::new(|info| {
                eprintln!("BINDING_HOST_REPLACEMENT: {info}")
            }));
            TEST_FAULT.with(|fault| fault.set(TestFault::Loop));
            embedded(
                r#"
try:
    run(Ports(()))
except ValueError as error:
    assert str(error) == "E-INTERNAL: run_scheduler failed: internal panic"
else:
    raise AssertionError("actual wrapper must still catch the replaced-hook panic")
"#,
            );
            println!("WRAPPER_PROOF_OK replacement");
            return;
        }
        let stderr = captured(
            "wrapper_host_hook_replacement_is_unsupported",
            "replacement",
        );
        assert!(stderr.contains("BINDING_HOST_REPLACEMENT"), "{stderr}");
        assert!(
            stderr.contains("INERT_BINDING_PAYLOAD_DROP"),
            "host replacement can defeat output privacy: {stderr}"
        );
    }

    #[test]
    fn native_porterror_summaries_are_fixed_and_payload_opaque() {
        if std::env::var(CHILD_ENV).as_deref() != Ok("opaque") {
            let stderr = captured(
                "native_porterror_summaries_are_fixed_and_payload_opaque",
                "opaque",
            );
            assert!(
                stderr.is_empty(),
                "opaque errors must not produce hook output: {stderr}"
            );
            return;
        }
        Python::initialize();
        Python::attach(|py| {
            let globals = PyDict::new(py);
            py.run(
                pyo3::ffi::c_str!(
                    r#"
class OpaqueFailure(BaseException):
    def __str__(self): raise AssertionError("must not stringify")
    def __repr__(self): raise AssertionError("must not repr")
error = OpaqueFailure("INERT_BINDING_PYERR_CANARY")
def raising_callback(*args): raise error
class Ports: pass
ports = Ports()
"#
                ),
                Some(&globals),
                None,
            )
            .unwrap();
            let object = globals.get_item("ports").unwrap().unwrap();
            let callback = globals.get_item("raising_callback").unwrap().unwrap();
            for name in NAMES {
                object.setattr(name, &callback).unwrap();
            }
            let adapter = PythonPorts::new(&object).unwrap();
            let original = globals.get_item("error").unwrap().unwrap();
            for index in 0..NAMES.len() {
                let error: PortError = adapter
                    .invoke(index, |_, callback| {
                        PythonPorts::returned(|| callback.call0(), nothing)
                    })
                    .unwrap_err();
                assert_eq!(
                    error.summary(),
                    if index == 7 {
                        WORKER_FAILED
                    } else {
                        LOOP_FAILED
                    }
                );
                let raised = port_failure(error);
                raised.restore(py);
                let fetched = PyErr::fetch(py);
                assert!(fetched.value(py).is(&original));
                assert!(fetched.traceback(py).is_some());
            }
            // NC07a: a real claimed entry plus a ticketed adapter error. The carrier stays Rust-held, and the
            // fence is not quiescent until the actual restore, its ticket discharge and the entry seal have returned.
            let shared = FenceShared::fresh();
            let owner = shared.claim().expect("fresh fence claims once");
            let mut adapter = PythonPorts::new(&object).unwrap();
            adapter.cleanup = Some(owner.shared());
            let error = adapter
                .invoke(0, |_, callback| {
                    PythonPorts::returned(|| callback.call0(), nothing)
                })
                .unwrap_err();
            drop(adapter);
            let carrier = port_failure(error);
            let now = Instant::now();
            assert_eq!(
                shared.wait_until(now),
                Ok(false),
                "held carrier is not quiescent"
            );
            let moved = carrier;
            assert_eq!(
                shared.wait_until(now),
                Ok(false),
                "moving the carrier transfers nothing"
            );
            moved.restore_with(py, || {
                assert_eq!(
                    shared.wait_until(Instant::now()),
                    Ok(false),
                    "restored but undischarged"
                );
            });
            assert_eq!(
                shared.wait_until(Instant::now()),
                Ok(false),
                "entry still active after restore"
            );
            owner.seal();
            assert_eq!(
                shared.wait_until(Instant::now() + Duration::from_secs(1)),
                Ok(true)
            );
            assert_eq!(
                shared.wait_until(Instant::now()),
                Ok(true),
                "terminal and repeatable"
            );
            assert!(PyErr::fetch(py).value(py).is(&original));
        });
        println!("WRAPPER_PROOF_OK opaque");
    }

    // NC05/NC06 witnesses. Each case drives the production permit and disposal path with real Python objects and samples
    // the fence at the cfg(test) witness points inside the drops. No sleep, counter, public hook or dependency change is used.
    const CLEANUP_FIXTURE: &str = r#"
NAMES = ("begin_pass", "agent_for", "claims_for", "block", "lease", "observe_pass", "submit", "worker",
         "internal_failure", "complete", "set_retry_after", "random_unit", "cancel_state", "cancel_begin", "kill", "log")
class Ports:
    pass
class Opaque(BaseException):
    def __del__(self):
        witness_probe("opaque-finalizer")
def raiser(*args):
    raise Opaque("INERT_WITNESS_CANARY")
def raising_ports():
    ports = Ports()
    for name in NAMES:
        setattr(ports, name, raiser)
    return ports
def worker_ports():
    ports = raising_ports()
    ports.internal_failure = lambda task, agent, text: (task, agent, "FAILED", text)
    return ports
class Probe:
    def __del__(self):
        witness_probe("final-decref")
    def cb(self, *args):
        return None
def probed_ports():
    probe = Probe()
    ports = Ports()
    for name in NAMES:
        setattr(ports, name, probe.cb)
    return ports
"#;

    fn quiescent(shared: &FenceShared) -> bool {
        shared.wait_until(Instant::now()) == Ok(true)
    }

    type Log = std::rc::Rc<std::cell::RefCell<Vec<(String, bool)>>>;

    /// Records each witness point with whether the fence was quiescent at that instant.
    fn observe(shared: &Arc<FenceShared>) -> Log {
        let log = Log::default();
        let (sink, shared) = (std::rc::Rc::clone(&log), Arc::clone(shared));
        set_witness(std::rc::Rc::new(move |point: &str| {
            sink.borrow_mut()
                .push((point.to_owned(), quiescent(&shared)));
        }));
        log
    }

    fn sealed_fence() -> Arc<FenceShared> {
        let shared = FenceShared::fresh();
        shared.claim().expect("fresh fence claims once").seal();
        shared
    }

    /// A sealed fence, an adapter bound to it, and one ticketed port error raised by the callback at `index`.
    fn ticket(
        ports: &Bound<'_, PyAny>,
        index: usize,
    ) -> (Arc<FenceShared>, PythonPorts, PortError, Log) {
        let shared = sealed_fence();
        let log = observe(&shared);
        let mut adapter = PythonPorts::new(ports).unwrap();
        adapter.cleanup = Some(Arc::clone(&shared));
        let error = adapter
            .invoke(index, |_, callback| {
                PythonPorts::returned(|| callback.call0(), nothing)
            })
            .unwrap_err();
        (shared, adapter, error, log)
    }

    fn assert_disposed(case: &str, shared: &FenceShared, log: &Log) {
        let entries = log.borrow().clone();
        assert!(
            entries.contains(&("opaque-finalizer".to_owned(), false)),
            "{case}: the finalizer must observe the fence still pending: {entries:?}"
        );
        assert!(
            entries.contains(&("opaque-attach-returned".to_owned(), false)),
            "{case}: the ticket was acknowledged before attached disposal returned: {entries:?}"
        );
        assert!(
            quiescent(shared),
            "{case}: quiescent only after disposal returned"
        );
    }

    fn refuse(payload: PortError) -> Result<(), PortError> {
        Err(payload)
    }

    /// NC05: the adapter's final callback decref runs inside its attachment, and the fence stays pending until that
    /// attachment has returned and the adapter acknowledges disposal.
    fn nc05_adapter_attach_returns_before_acknowledgement(make: &Bound<'_, PyAny>) {
        let shared = sealed_fence();
        let log = observe(&shared);
        let ports = make.call0().unwrap();
        let mut adapter = PythonPorts::new(&ports).unwrap();
        adapter.cleanup = Some(Arc::clone(&shared));
        drop(ports);
        assert!(!quiescent(&shared), "the adapter still owns its callbacks");
        drop(adapter);
        assert_eq!(
            *log.borrow(),
            vec![
                ("final-decref".to_owned(), false),
                ("adapter-attach-returned".to_owned(), false),
            ],
            "the final decref and the attach return must both precede acknowledgement"
        );
        assert!(
            quiescent(&shared),
            "quiescent only after the whole attachment returned"
        );
    }

    /// NC06: a worker opaque held across internal_failure, unread completion, queueing, refusal, unrun spawn and panic
    /// unwinding. In each the ticket stays pending while the payload is held and is discharged only after its attached
    /// disposal returns.
    fn nc06_held_payloads(raising: &Bound<'_, PyAny>, worker: &Bound<'_, PyAny>) {
        let (shared, adapter, error, log) = ticket(worker, 7);
        let done = adapter
            .internal_failure("task", "agent", error.summary())
            .expect("internal_failure returns a record");
        assert_eq!(
            (done.task_id.as_str(), done.outcome.as_str()),
            ("task", "FAILED")
        );
        drop(adapter);
        assert!(!quiescent(&shared), "held across internal_failure");
        drop(error);
        assert_disposed("internal_failure", &shared, &log);

        let (shared, adapter, error, log) = ticket(raising, 7);
        let unread = vec![error];
        drop(adapter);
        assert!(!quiescent(&shared), "an unread completion holds the ticket");
        drop(unread);
        assert_disposed("unread completion", &shared, &log);

        let (shared, adapter, error, log) = ticket(raising, 7);
        let (sender, queue) = std::sync::mpsc::channel::<PortError>();
        sender.send(error).expect("the queue is open");
        drop(sender);
        drop(adapter);
        assert!(!quiescent(&shared), "a queued payload holds the ticket");
        drop(queue);
        assert_disposed("queued payload", &shared, &log);

        let (shared, adapter, error, log) = ticket(raising, 7);
        let refused = refuse(error).unwrap_err();
        drop(adapter);
        assert!(!quiescent(&shared), "a refused payload holds the ticket");
        drop(refused);
        assert_disposed("refusal", &shared, &log);

        let (shared, adapter, error, log) = ticket(raising, 7);
        let job = move || drop(error);
        drop(adapter);
        assert!(!quiescent(&shared), "an unrun job holds the payload");
        // The spawn was refused, so the job is dropped without running.
        drop(job);
        assert_disposed("refused spawn", &shared, &log);

        let (shared, adapter, error, log) = ticket(raising, 7);
        drop(adapter);
        let pending_in_unwind = std::cell::Cell::new(None);
        let unwound = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let _held = error;
            pending_in_unwind.set(Some(!quiescent(&shared)));
            std::panic::resume_unwind(Box::new("INERT_WITNESS_PANIC"));
        }));
        assert_eq!(
            pending_in_unwind.get(),
            Some(true),
            "the payload holds the ticket while unwinding"
        );
        assert!(unwound.is_err(), "the unwinding closure must panic");
        drop(unwound);
        assert_disposed("panic unwind", &shared, &log);
    }

    #[pyfunction]
    fn witness_probe(point: &str) {
        witness(point);
    }

    #[test]
    fn native_cleanup_permits_and_disposal_witnesses() {
        if std::env::var(CHILD_ENV).as_deref() != Ok("cleanup") {
            captured("native_cleanup_permits_and_disposal_witnesses", "cleanup");
            return;
        }
        Python::initialize();
        Python::attach(|py| {
            let module = PyModule::new(py, "_witness_under_test").unwrap();
            module
                .add_function(wrap_pyfunction!(witness_probe, &module).unwrap())
                .unwrap();
            let globals = PyDict::new(py);
            globals
                .set_item("witness_probe", module.getattr("witness_probe").unwrap())
                .unwrap();
            py.run(
                CString::new(CLEANUP_FIXTURE).unwrap().as_c_str(),
                Some(&globals),
                None,
            )
            .unwrap();
            let get = |name: &str| globals.get_item(name).unwrap().unwrap();
            nc05_adapter_attach_returns_before_acknowledgement(&get("probed_ports"));
            let raising = get("raising_ports").call0().unwrap();
            let worker = get("worker_ports").call0().unwrap();
            nc06_held_payloads(&raising, &worker);
        });
        println!("WRAPPER_PROOF_OK cleanup");
    }
}

// Test-only observation point for the drop paths above. Compiled out of every non-test build, so production behaviour is
// unchanged. The observer is a pure Rust thread-local that samples fence state; no product injector or hook exists.
#[cfg(test)]
type WitnessObserver = std::rc::Rc<dyn Fn(&str)>;

#[cfg(test)]
thread_local! {
    static WITNESS: std::cell::RefCell<Option<WitnessObserver>> =
        const { std::cell::RefCell::new(None) };
}

#[cfg(test)]
fn witness(point: &str) {
    let observer = WITNESS.with(|slot| slot.borrow().clone());
    if let Some(observer) = observer {
        observer(point);
    }
}

#[cfg(test)]
fn set_witness(observer: std::rc::Rc<dyn Fn(&str)>) {
    WITNESS.with(|slot| *slot.borrow_mut() = Some(observer));
}
