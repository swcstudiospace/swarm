//! The chat-thread binding of `swarm._native` (Phase 14.6 CR-04): `Governor`, `ChatPool` and `ChatTicket`.
//!
//! * [`PyGovernor`] wraps a `gsd_governor::Governor`. Its wire reservation returns `granted == False` for a refusal and
//!   never raises. Its checked reservation raises with the code of [`governor_code`].
//! * [`PyChatPool`] wraps a `gsd_pool::ChatPool` built over a [`GovernorBudget`] that shares the governor object the caller
//!   passed in (DEC-1, DEC-2).
//! * [`PyChatTicket`] wraps the `gsd_pool::Ticket` of one accepted message.
//!
//! The binding runs only the built-in executor. No Python callable runs on a worker (DEC-5), and `hold_ms` is crate-private
//! in `gsd-pool`, so it is not exposed.
//!
//! Every error is a `ValueError` whose text starts with its wire code and then the operation, as `sched_run` does:
//! `"E-TIMEOUT: reserve_budget_checked failed: Budget exceeded: ..."`.
//!
//! # Wire codes
//!
//! [`chat_code`] maps each `ChatError`:
//!
//! | `ChatError` | Code |
//! |---|---|
//! | `InvalidConfig`, `UnknownThread`, `ThreadExists`, `UnknownMessage`, `InvalidBudget` | `E-INPUT` |
//! | `ThreadBusy`, `QueueFull`, `Capacity`, `ShuttingDown`, `RateLimited` | `E-CAPACITY` |
//! | `BudgetDenied` | `E-TIMEOUT` |
//! | `Governor`, `Panicked`, `WorkerSpawn` | `E-INTERNAL` |
//!
//! [`governor_code`] maps each `GovernorError`:
//!
//! | `GovernorError` | Code |
//! |---|---|
//! | `BudgetExceeded`, `CostLimitExceeded` | `E-TIMEOUT` |
//! | `InvalidBudget` | `E-INPUT` |
//! | `RateLimitExceeded`, `QuotaExceeded` | `E-CAPACITY` |
//! | `Unauthorized` | `E-POLICY` |
//! | `Other`, `UnknownReservation` | `E-INTERNAL` |
//!
//! # Lease note (DEC-4)
//!
//! A scheduler lease on a chat thread lasts until its job completes (`MatrixScheduler::complete`). This binding does not
//! reach the scheduler, so it takes no lease.

use std::future::Future;
use std::sync::Arc;
use std::task::{Context, Poll, Waker};
use std::time::Duration;

use gsd_codes::ErrorCode;
use gsd_governor::{Governor, GovernorConfig, GovernorError};
use gsd_pool::{
    BudgetPort, BudgetRefusal, BudgetReservation, CancelOutcome, ChatError, ChatMessage,
    ChatRuntimeConfig, Completed, MessageRef, SubmitOutcome, Ticket,
};
use gsd_proto::gsd::v1::{
    BudgetGrant, BudgetRequest, BudgetSnapshot, ChatThreadId, SessionId, SubagentType,
};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};

/// The message a governor call reports when it does not complete on its first poll. The governor's reserve and release
/// futures contain no await point, so this never happens; the message exists so that a broken invariant is a visible fault.
const NOT_READY: &str = "governor call did not complete on its first poll";

/// The wire code for a chat-facade error (the table in the module docs).
pub fn chat_code(error: &ChatError) -> ErrorCode {
    match error {
        ChatError::InvalidConfig(_)
        | ChatError::UnknownThread(_)
        | ChatError::ThreadExists(_)
        | ChatError::UnknownMessage { .. }
        | ChatError::InvalidBudget(_) => ErrorCode::Input,
        ChatError::ThreadBusy(_)
        | ChatError::QueueFull { .. }
        | ChatError::Capacity { .. }
        | ChatError::ShuttingDown
        | ChatError::RateLimited(_) => ErrorCode::Capacity,
        ChatError::BudgetDenied(_) => ErrorCode::Timeout,
        ChatError::Governor(_) | ChatError::Panicked(_) | ChatError::WorkerSpawn(_) => {
            ErrorCode::Internal
        }
    }
}

/// The wire code for a governor error (the table in the module docs).
pub fn governor_code(error: &GovernorError) -> ErrorCode {
    match error {
        GovernorError::BudgetExceeded(_) | GovernorError::CostLimitExceeded(_) => {
            ErrorCode::Timeout
        }
        GovernorError::InvalidBudget(_) => ErrorCode::Input,
        GovernorError::RateLimitExceeded(_) | GovernorError::QuotaExceeded(_) => {
            ErrorCode::Capacity
        }
        GovernorError::Unauthorized(_) => ErrorCode::Policy,
        GovernorError::Other(_) | GovernorError::UnknownReservation(_) => ErrorCode::Internal,
    }
}

/// The budget-port refusal for a governor error: `Exceeded`, `RateLimited`, `Invalid` or `Other`.
pub fn refusal_of(error: GovernorError) -> BudgetRefusal {
    match error {
        GovernorError::BudgetExceeded(m)
        | GovernorError::QuotaExceeded(m)
        | GovernorError::CostLimitExceeded(m) => BudgetRefusal::Exceeded(m),
        GovernorError::RateLimitExceeded(m) => BudgetRefusal::RateLimited(m),
        GovernorError::InvalidBudget(m) => BudgetRefusal::Invalid(m),
        GovernorError::Unauthorized(m)
        | GovernorError::Other(m)
        | GovernorError::UnknownReservation(m) => BudgetRefusal::Other(m),
    }
}

/// What `ChatTicket.wait` reports for one outcome.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TicketView {
    /// The message is still pending at the timeout.
    Pending,
    /// The executor returned this payload.
    Ok(Vec<u8>),
    /// The message failed; the wire code of its error.
    Failed(ErrorCode),
    /// The message was cancelled.
    Cancelled,
}

/// The view of one `wait` result. `None` (still pending) maps to [`TicketView::Pending`].
pub fn ticket_view(outcome: Option<Completed>) -> TicketView {
    match outcome {
        None => TicketView::Pending,
        Some(Completed::Ok(payload)) => TicketView::Ok(payload),
        Some(Completed::Failed(error)) => TicketView::Failed(chat_code(&error)),
        Some(Completed::Cancelled) => TicketView::Cancelled,
    }
}

/// Converts a reservation's wall time to the governor's signed milliseconds. A value beyond `i64` is `Invalid`.
fn to_grant_ms(wall_time_ms: u64) -> Result<i64, BudgetRefusal> {
    i64::try_from(wall_time_ms)
        .map_err(|_| BudgetRefusal::Invalid(format!("wall_time_ms {wall_time_ms} exceeds i64")))
}

/// The protocol request for one reservation or release.
fn budget_request(
    session_id: &str,
    thread_id: &str,
    tokens: i64,
    wall_time_ms: i64,
    tool_calls: i32,
    cost_usd: f64,
) -> BudgetRequest {
    BudgetRequest {
        session_id: Some(SessionId {
            value: session_id.to_string(),
        }),
        thread_id: Some(ChatThreadId {
            value: thread_id.to_string(),
        }),
        tokens,
        wall_time_ms,
        tool_calls,
        cost_usd,
    }
}

/// Polls a governor future once and returns its output, or `None` when it is not ready.
///
/// The governor's reserve and release futures contain no await point, so they are ready on the first poll. A loop on
/// `Pending` would spin forever on a future that waits for a waker nothing will wake, so the single poll is the whole
/// driver, and `None` becomes a typed fault at the call site.
fn poll_once<F: Future>(future: F) -> Option<F::Output> {
    let mut context = Context::from_waker(Waker::noop());
    let mut future = std::pin::pin!(future);
    match future.as_mut().poll(&mut context) {
        Poll::Ready(output) => Some(output),
        Poll::Pending => None,
    }
}

/// The budget port over a governor shared with the Python `Governor` object (DEC-1).
///
/// Reservations carry wall time only. Tokens, tool calls and cost are reserved as zero, as in `gsd-scheduler`. A reservation
/// the governor grants is `Ok`. A refusal is a [`BudgetRefusal`] built by [`refusal_of`]. A grant with `granted == false`
/// is `Other`.
pub struct GovernorBudget {
    governor: Arc<Governor>,
}

impl GovernorBudget {
    /// Wraps `governor` as a budget port.
    pub fn new(governor: Arc<Governor>) -> Self {
        Self { governor }
    }
}

impl BudgetPort for GovernorBudget {
    fn reserve(&self, reservation: &BudgetReservation) -> Result<(), BudgetRefusal> {
        let request = budget_request(
            &reservation.session_id,
            &reservation.thread_id,
            0,
            to_grant_ms(reservation.wall_time_ms)?,
            0,
            0.0,
        );
        match poll_once(self.governor.reserve_budget_checked(request)) {
            Some(Ok(grant)) if grant.granted => Ok(()),
            Some(Ok(_)) => Err(BudgetRefusal::Other(
                "governor returned no grant".to_string(),
            )),
            Some(Err(error)) => Err(refusal_of(error)),
            None => Err(BudgetRefusal::Other(NOT_READY.to_string())),
        }
    }

    fn release(&self, reservation: &BudgetReservation) -> Result<(), BudgetRefusal> {
        let wall_time_ms = to_grant_ms(reservation.wall_time_ms)?;
        match poll_once(self.governor.release_budget(
            &reservation.session_id,
            &reservation.thread_id,
            0,
            wall_time_ms,
            0,
            0.0,
        )) {
            Some(Ok(())) => Ok(()),
            Some(Err(error)) => Err(refusal_of(error)),
            None => Err(BudgetRefusal::Other(NOT_READY.to_string())),
        }
    }
}

/// A `ValueError` whose text starts with the wire code: `"<code>: <operation> failed: <detail>"`.
fn raise(code: ErrorCode, operation: &str, detail: impl std::fmt::Display) -> PyErr {
    PyValueError::new_err(format!("{}: {operation} failed: {detail}", code.as_str()))
}

/// The Python error for a chat-facade failure in `operation`.
fn chat_err(operation: &str, error: ChatError) -> PyErr {
    raise(chat_code(&error), operation, error)
}

/// The Python error for a governor failure in `operation`.
fn governor_err(operation: &str, error: GovernorError) -> PyErr {
    raise(governor_code(&error), operation, error)
}

/// A dict of the wire grant fields. The `valid_for` duration is not exposed.
fn grant_dict<'py>(py: Python<'py>, grant: &BudgetGrant) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    dict.set_item("granted", grant.granted)?;
    dict.set_item("tokens_granted", grant.tokens_granted)?;
    dict.set_item("wall_time_ms_granted", grant.wall_time_ms_granted)?;
    dict.set_item("tool_calls_granted", grant.tool_calls_granted)?;
    dict.set_item("cost_usd_granted", grant.cost_usd_granted)?;
    Ok(dict)
}

/// A dict of the used and remaining amounts of one session budget.
fn snapshot_dict<'py>(py: Python<'py>, snapshot: &BudgetSnapshot) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    dict.set_item("tokens_used", snapshot.tokens_used)?;
    dict.set_item("wall_time_ms_used", snapshot.wall_time_ms_used)?;
    dict.set_item("tool_calls_used", snapshot.tool_calls_used)?;
    dict.set_item("cost_usd_used", snapshot.cost_usd_used)?;
    dict.set_item("tokens_remaining", snapshot.tokens_remaining)?;
    dict.set_item("wall_time_ms_remaining", snapshot.wall_time_ms_remaining)?;
    dict.set_item("tool_calls_remaining", snapshot.tool_calls_remaining)?;
    dict.set_item("cost_usd_remaining", snapshot.cost_usd_remaining)?;
    Ok(dict)
}

/// A dict of the `ChatTicket.wait` result: `status`, `payload` and `code`.
fn ticket_dict<'py>(py: Python<'py>, view: TicketView) -> PyResult<Bound<'py, PyDict>> {
    let (status, payload, code) = match view {
        TicketView::Pending => ("pending", None, None),
        TicketView::Ok(payload) => ("ok", Some(PyBytes::new(py, &payload)), None),
        TicketView::Failed(code) => ("failed", None, Some(code.as_str())),
        TicketView::Cancelled => ("cancelled", None, None),
    };
    let dict = PyDict::new(py);
    dict.set_item("status", status)?;
    dict.set_item("payload", payload)?;
    dict.set_item("code", code)?;
    Ok(dict)
}

/// A list of `(thread_id, msg_id)` pairs, in the order given.
fn message_refs(refs: Vec<MessageRef>) -> Vec<(String, String)> {
    refs.into_iter()
        .map(|message| (message.thread_id, message.msg_id))
        .collect()
}

/// The governor that reserves and releases budget for every `ChatPool` built over it.
///
/// Thread-safe: the governor serialises its own state. Its calls hold only short internal locks and do not await, so they
/// run with the GIL held. `reserve_budget` is the wire call: a refusal returns `granted == False` and never raises.
/// `reserve_budget_checked` raises on a refusal, with the code in the module docs.
#[pyclass(name = "Governor", frozen, module = "swarm._native")]
pub(crate) struct PyGovernor {
    inner: Arc<Governor>,
}

#[pymethods]
impl PyGovernor {
    /// Creates a governor with the default configuration (`GovernorConfig::default`).
    #[new]
    fn new() -> Self {
        Self {
            inner: Arc::new(Governor::new(GovernorConfig::default())),
        }
    }

    /// Wire reservation. Returns the grant as a dict; a refusal returns `granted` False with zero amounts and never raises.
    ///
    /// A refusal never raises. `ValueError` (`E-INTERNAL`) is raised only if the governor call does not complete on its first
    /// poll, which the governor's reserve path never does. Use `reserve_budget_checked` to learn why a reservation was refused.
    #[allow(clippy::too_many_arguments)] // reason: the Python signature is flat by design, one parameter per amount.
    #[pyo3(signature = (session_id, thread_id, tokens, wall_time_ms, tool_calls, cost_usd))]
    fn reserve_budget<'py>(
        &self,
        py: Python<'py>,
        session_id: &str,
        thread_id: &str,
        tokens: i64,
        wall_time_ms: i64,
        tool_calls: i32,
        cost_usd: f64,
    ) -> PyResult<Bound<'py, PyDict>> {
        let request = budget_request(
            session_id,
            thread_id,
            tokens,
            wall_time_ms,
            tool_calls,
            cost_usd,
        );
        match poll_once(self.inner.reserve_budget(request)) {
            Some(grant) => grant_dict(py, &grant),
            None => Err(raise(ErrorCode::Internal, "reserve_budget", NOT_READY)),
        }
    }

    /// Checked reservation. Returns the grant as a dict on success.
    ///
    /// Raises `ValueError` with the code of the refusal and no change to the budget: `E-INPUT` for a missing id or a negative
    /// or non-finite amount, `E-TIMEOUT` when a session, thread or global cap would be exceeded, `E-CAPACITY` for a rate-limit
    /// refusal.
    #[allow(clippy::too_many_arguments)] // reason: the Python signature is flat by design, one parameter per amount.
    #[pyo3(signature = (session_id, thread_id, tokens, wall_time_ms, tool_calls, cost_usd))]
    fn reserve_budget_checked<'py>(
        &self,
        py: Python<'py>,
        session_id: &str,
        thread_id: &str,
        tokens: i64,
        wall_time_ms: i64,
        tool_calls: i32,
        cost_usd: f64,
    ) -> PyResult<Bound<'py, PyDict>> {
        let request = budget_request(
            session_id,
            thread_id,
            tokens,
            wall_time_ms,
            tool_calls,
            cost_usd,
        );
        match poll_once(self.inner.reserve_budget_checked(request)) {
            Some(Ok(grant)) => grant_dict(py, &grant),
            Some(Err(error)) => Err(governor_err("reserve_budget_checked", error)),
            None => Err(raise(
                ErrorCode::Internal,
                "reserve_budget_checked",
                NOT_READY,
            )),
        }
    }

    /// Returns the amounts of an earlier reservation to the session and thread budgets and to the global totals.
    ///
    /// Raises `ValueError`: `E-INPUT` for a negative or non-finite amount, and `E-INTERNAL` when the session and thread pair
    /// holds no reservation. A refused release changes nothing.
    #[pyo3(signature = (session_id, thread_id, tokens, wall_time_ms, tool_calls, cost_usd))]
    fn release_budget(
        &self,
        session_id: &str,
        thread_id: &str,
        tokens: i64,
        wall_time_ms: i64,
        tool_calls: i32,
        cost_usd: f64,
    ) -> PyResult<()> {
        match poll_once(self.inner.release_budget(
            session_id,
            thread_id,
            tokens,
            wall_time_ms,
            tool_calls,
            cost_usd,
        )) {
            Some(result) => result.map_err(|error| governor_err("release_budget", error)),
            None => Err(raise(ErrorCode::Internal, "release_budget", NOT_READY)),
        }
    }

    /// The used and remaining amounts of session `session_id` as a dict.
    ///
    /// Raises `ValueError` (`E-INPUT`) when the session has no budget yet, which is the case before its first reservation.
    fn budget_snapshot<'py>(
        &self,
        py: Python<'py>,
        session_id: &str,
    ) -> PyResult<Bound<'py, PyDict>> {
        let snapshot = self
            .inner
            .get_budget_snapshot(session_id)
            .map_err(|error| governor_err("budget_snapshot", error))?;
        snapshot_dict(py, &snapshot)
    }
}

/// A chat-thread pool over a governor, with a bounded queue and a worker pool per pool object.
///
/// `workers` threads run each message on the built-in executor, which holds for no time and returns the payload. No Python
/// callable runs on a worker (DEC-5). Each thread has one message in flight at a time, so its messages run in FIFO order.
/// Message ids are unique per thread (DEC-6). A queued or in-flight id is always refused as already
/// accepted, and a finished id is refused while it is inside `dedupe_window`. A message id reused on
/// another thread is a new message.
/// Warm parked threads do not count against `max_threads`.
///
/// Errors are `ValueError`s with a wire code (see the module docs). Dropping the last reference stops the pool: it waits at
/// most `drop_grace_ms` (default 2000) for its workers, then detaches any still running. Call `shutdown` to get a report instead.
#[pyclass(name = "ChatPool", frozen, module = "swarm._native")]
pub(crate) struct PyChatPool {
    inner: gsd_pool::ChatPool,
    /// The grace that `shutdown` waits for when a `with` block exits. It is the `drop_grace_ms` the constructor received.
    drop_grace_ms: u64,
}

#[pymethods]
impl PyChatPool {
    /// Creates a pool over `governor` and starts `workers` worker threads.
    ///
    /// `workers` is at most 256. `dedupe_window` must be greater than `per_thread_queue`. `drop_grace_ms` bounds how long
    /// dropping the pool waits for its workers, in milliseconds. It defaults to 2000. Raises `ValueError` with `E-INPUT` when a
    /// count or `drop_grace_ms` is zero, when `workers` is above 256, or when `dedupe_window` is not greater than
    /// `per_thread_queue`; and with `E-INTERNAL` when the OS refuses a worker thread.
    #[new]
    #[pyo3(signature = (
        governor,
        workers = 4,
        per_thread_queue = 64,
        dedupe_window = 256,
        max_threads = 30,
        warm_pool_size = 5,
        drop_grace_ms = 2000,
    ))]
    fn new(
        governor: PyRef<'_, PyGovernor>,
        workers: usize,
        per_thread_queue: usize,
        dedupe_window: usize,
        max_threads: usize,
        warm_pool_size: usize,
        drop_grace_ms: u64,
    ) -> PyResult<Self> {
        let config = ChatRuntimeConfig {
            workers,
            per_thread_queue,
            dedupe_window,
            max_threads,
            warm_pool_size,
            drop_grace_ms,
        };
        let budget: Arc<dyn BudgetPort> =
            Arc::new(GovernorBudget::new(Arc::clone(&governor.inner)));
        let inner =
            gsd_pool::ChatPool::new(config, budget).map_err(|error| chat_err("ChatPool", error))?;
        Ok(Self {
            inner,
            drop_grace_ms,
        })
    }

    /// Opens live thread `thread_id` with the subagent type `subagent_type`.
    ///
    /// Raises `ValueError`: `E-INPUT` when a live thread already holds the id, and `E-CAPACITY` when `max_threads` live threads
    /// exist. A failed call changes nothing.
    fn open_thread(&self, thread_id: &str, subagent_type: &str) -> PyResult<()> {
        self.inner
            .open_thread(
                thread_id,
                SubagentType {
                    value: subagent_type.to_string(),
                },
            )
            .map_err(|error| chat_err("open_thread", error))
    }

    /// Closes live thread `thread_id`.
    ///
    /// Raises `ValueError`: `E-INPUT` for an id that is not live, and `E-CAPACITY` while the thread has queued or running work.
    fn close_thread(&self, thread_id: &str) -> PyResult<()> {
        self.inner
            .close_thread(thread_id)
            .map_err(|error| chat_err("close_thread", error))
    }

    /// Queues one message and returns `(outcome, seq, ticket)`.
    ///
    /// `outcome` is `"enqueued"`, with a `ChatTicket` for the message, or `"already_accepted"` when `msg_id`
    /// is queued or in flight on the thread, or inside its dedupe window, with `None` for the ticket.
    /// `seq` is the per-thread sequence number of the message.
    ///
    /// Raises `ValueError`: `E-CAPACITY` when the queue is full (nothing is enqueued), when the pool is shutting down, or
    /// `E-INPUT` when the thread is not live.
    fn submit(
        &self,
        thread_id: &str,
        msg_id: &str,
        session_id: &str,
        payload: &[u8],
    ) -> PyResult<(&'static str, u64, Option<PyChatTicket>)> {
        let submitted = self
            .inner
            .submit(ChatMessage::new(
                msg_id,
                thread_id,
                session_id,
                payload.to_vec(),
            ))
            .map_err(|error| chat_err("submit", error))?;
        let (outcome, seq) = match submitted.outcome {
            SubmitOutcome::Enqueued { seq } => ("enqueued", seq),
            SubmitOutcome::AlreadyAccepted { seq } => ("already_accepted", seq),
        };
        Ok((
            outcome,
            seq,
            submitted.ticket.map(|inner| PyChatTicket { inner }),
        ))
    }

    /// Cancels message `msg_id` on thread `thread_id`.
    ///
    /// Returns `"removed_queued"` when the message was queued: it is removed and never runs. Returns `"signalled_in_flight"`
    /// when a worker is running it: the message's ticket completes `cancelled` when the worker finishes, and the executor's
    /// value is discarded.
    ///
    /// Raises `ValueError`: `E-INPUT` for a thread that is not live, or a message that is neither queued nor running (this
    /// includes a message that already completed).
    fn cancel_message(&self, thread_id: &str, msg_id: &str) -> PyResult<&'static str> {
        match self
            .inner
            .cancel_message(thread_id, msg_id)
            .map_err(|error| chat_err("cancel_message", error))?
        {
            CancelOutcome::RemovedQueued => Ok("removed_queued"),
            CancelOutcome::SignalledInFlight => Ok("signalled_in_flight"),
        }
    }

    /// Cancels every message of thread `thread_id`. Returns `{"removed": [(thread_id, msg_id), ...], "signalled": [...]}`.
    ///
    /// Raises `ValueError` (`E-INPUT`) for a thread that is not live.
    fn cancel_thread<'py>(&self, py: Python<'py>, thread_id: &str) -> PyResult<Bound<'py, PyDict>> {
        let report = self
            .inner
            .cancel_thread(thread_id)
            .map_err(|error| chat_err("cancel_thread", error))?;
        let dict = PyDict::new(py);
        dict.set_item("removed", message_refs(report.removed))?;
        dict.set_item("signalled", message_refs(report.signalled))?;
        Ok(dict)
    }

    /// Stops accepting work and reports every message queued or in flight at the call.
    ///
    /// Returns `{"flushed": [...], "dropped": [...], "abandoned": [...], "cancelled": [...]}`, each a list of
    /// `(thread_id, msg_id)` pairs. Messages that finish within `grace_ms` are `flushed`. Queued messages still waiting at
    /// the deadline are `dropped`, and their tickets complete `failed` with `E-CAPACITY`. Running messages still running at
    /// the deadline are `abandoned`, with the same ticket outcome. `cancelled` lists the queued messages cancelled while
    /// shutdown waited. Messages that completed before the call are not listed. The GIL is released while the pool waits.
    fn shutdown<'py>(&self, py: Python<'py>, grace_ms: u64) -> PyResult<Bound<'py, PyDict>> {
        let report = py.detach(|| self.inner.shutdown(Duration::from_millis(grace_ms)));
        let dict = PyDict::new(py);
        dict.set_item("flushed", message_refs(report.flushed))?;
        dict.set_item("dropped", message_refs(report.dropped))?;
        dict.set_item("abandoned", message_refs(report.abandoned))?;
        dict.set_item("cancelled", message_refs(report.cancelled))?;
        Ok(dict)
    }

    /// Returns the pool itself, so that `with ChatPool(...) as pool:` binds it.
    fn __enter__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    /// Shuts the pool down when a `with` block exits, as `shutdown` does, and waits at most `drop_grace_ms` for its workers.
    /// Returns `False`, so an exception raised in the block propagates.
    fn __exit__(
        &self,
        py: Python<'_>,
        _exc_type: Option<Bound<'_, PyAny>>,
        _exc_value: Option<Bound<'_, PyAny>>,
        _traceback: Option<Bound<'_, PyAny>>,
    ) -> PyResult<bool> {
        self.shutdown(py, self.drop_grace_ms)?;
        Ok(false)
    }
}

/// The outcome handle of one accepted message. It cannot be built from Python; `ChatPool.submit` returns it.
#[pyclass(name = "ChatTicket", frozen, module = "swarm._native")]
pub(crate) struct PyChatTicket {
    inner: Ticket,
}

#[pymethods]
impl PyChatTicket {
    /// Waits up to `timeout_ms` for the message to finish and returns `{"status", "payload", "code"}`.
    ///
    /// `status` is `"ok"` (with `payload` as bytes and `code` None), `"failed"` (with `payload` None and `code` the wire code),
    /// `"cancelled"`, or `"pending"` when the timeout elapsed first. A wait timeout does not cancel the message. The GIL is
    /// released while the call waits.
    fn wait<'py>(&self, py: Python<'py>, timeout_ms: u64) -> PyResult<Bound<'py, PyDict>> {
        let outcome = py.detach(|| self.inner.wait(Duration::from_millis(timeout_ms)));
        ticket_dict(py, ticket_view(outcome))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use gsd_governor::GovernorError;
    use gsd_pool::ChatError;

    #[test]
    fn every_chat_error_has_its_table_code() {
        let cases = [
            (ChatError::InvalidConfig("x".into()), ErrorCode::Input),
            (ChatError::UnknownThread("t".into()), ErrorCode::Input),
            (ChatError::ThreadExists("t".into()), ErrorCode::Input),
            (
                ChatError::UnknownMessage {
                    thread_id: "t".into(),
                    msg_id: "m".into(),
                },
                ErrorCode::Input,
            ),
            (ChatError::InvalidBudget("x".into()), ErrorCode::Input),
            (ChatError::ThreadBusy("t".into()), ErrorCode::Capacity),
            (
                ChatError::QueueFull {
                    thread_id: "t".into(),
                    capacity: 1,
                },
                ErrorCode::Capacity,
            ),
            (ChatError::Capacity { max_threads: 1 }, ErrorCode::Capacity),
            (ChatError::ShuttingDown, ErrorCode::Capacity),
            (ChatError::RateLimited("x".into()), ErrorCode::Capacity),
            (ChatError::BudgetDenied("x".into()), ErrorCode::Timeout),
            (ChatError::Governor("x".into()), ErrorCode::Internal),
            (ChatError::Panicked("x".into()), ErrorCode::Internal),
            (ChatError::WorkerSpawn("x".into()), ErrorCode::Internal),
        ];
        for (error, code) in cases {
            assert_eq!(chat_code(&error), code, "{error}");
        }
    }

    #[test]
    fn every_governor_error_has_its_table_code() {
        let cases = [
            (
                GovernorError::BudgetExceeded("x".into()),
                ErrorCode::Timeout,
            ),
            (
                GovernorError::CostLimitExceeded("x".into()),
                ErrorCode::Timeout,
            ),
            (GovernorError::InvalidBudget("x".into()), ErrorCode::Input),
            (
                GovernorError::RateLimitExceeded("x".into()),
                ErrorCode::Capacity,
            ),
            (
                GovernorError::QuotaExceeded("x".into()),
                ErrorCode::Capacity,
            ),
            (GovernorError::Unauthorized("x".into()), ErrorCode::Policy),
            (GovernorError::Other("x".into()), ErrorCode::Internal),
            (
                GovernorError::UnknownReservation("x".into()),
                ErrorCode::Internal,
            ),
        ];
        for (error, code) in cases {
            assert_eq!(governor_code(&error), code, "{error}");
        }
    }

    #[test]
    fn governor_refusals_map_to_the_budget_port_kinds() {
        assert!(matches!(
            refusal_of(GovernorError::BudgetExceeded("x".into())),
            BudgetRefusal::Exceeded(_)
        ));
        assert!(matches!(
            refusal_of(GovernorError::QuotaExceeded("x".into())),
            BudgetRefusal::Exceeded(_)
        ));
        assert!(matches!(
            refusal_of(GovernorError::CostLimitExceeded("x".into())),
            BudgetRefusal::Exceeded(_)
        ));
        assert!(matches!(
            refusal_of(GovernorError::RateLimitExceeded("x".into())),
            BudgetRefusal::RateLimited(_)
        ));
        assert!(matches!(
            refusal_of(GovernorError::InvalidBudget("x".into())),
            BudgetRefusal::Invalid(_)
        ));
        assert!(matches!(
            refusal_of(GovernorError::Unauthorized("x".into())),
            BudgetRefusal::Other(_)
        ));
        assert!(matches!(
            refusal_of(GovernorError::Other("x".into())),
            BudgetRefusal::Other(_)
        ));
        assert!(matches!(
            refusal_of(GovernorError::UnknownReservation("x".into())),
            BudgetRefusal::Other(_)
        ));
    }

    #[test]
    fn ticket_outcomes_map_to_their_status_and_code() {
        assert_eq!(ticket_view(None), TicketView::Pending);
        assert_eq!(
            ticket_view(Some(Completed::Ok(b"p".to_vec()))),
            TicketView::Ok(b"p".to_vec())
        );
        assert_eq!(
            ticket_view(Some(Completed::Failed(ChatError::ShuttingDown))),
            TicketView::Failed(ErrorCode::Capacity)
        );
        assert_eq!(
            ticket_view(Some(Completed::Failed(ChatError::Panicked("x".into())))),
            TicketView::Failed(ErrorCode::Internal)
        );
        assert_eq!(
            ticket_view(Some(Completed::Cancelled)),
            TicketView::Cancelled
        );
    }

    #[test]
    fn poll_once_returns_a_ready_output_and_refuses_to_spin_on_pending() {
        assert_eq!(poll_once(async { 7_u8 }), Some(7));
        // A future that is never ready must return to the caller, not loop.
        assert_eq!(poll_once(std::future::pending::<u8>()), None);
    }

    #[test]
    fn a_non_negative_wall_time_converts_and_an_oversized_one_is_invalid() {
        assert_eq!(to_grant_ms(5), Ok(5));
        assert!(matches!(
            to_grant_ms(u64::MAX),
            Err(BudgetRefusal::Invalid(_))
        ));
    }

    #[test]
    fn the_python_defaults_are_the_pool_defaults() {
        // The literals in the `ChatPool` signature above. Stubtest checks the stub against them, because pyo3 renders only
        // literal defaults in the runtime signature.
        let config = ChatRuntimeConfig::default();
        assert_eq!(
            (
                config.workers,
                config.per_thread_queue,
                config.dedupe_window,
                config.max_threads,
                config.warm_pool_size,
                config.drop_grace_ms,
            ),
            (4, 64, 256, 30, 5, 2000)
        );
    }

    #[test]
    fn the_adapter_grants_within_budget_and_refuses_over_it_without_mutation() -> Result<(), String>
    {
        let governor = Arc::new(Governor::new(GovernorConfig::default()));
        let port = GovernorBudget::new(Arc::clone(&governor));
        let within = BudgetReservation {
            session_id: "s1".into(),
            thread_id: "t1".into(),
            wall_time_ms: 1,
        };
        port.reserve(&within)
            .map_err(|refusal| format!("{refusal:?}"))?;
        let over = BudgetReservation {
            session_id: "s1".into(),
            thread_id: "t1".into(),
            wall_time_ms: 10_000_000,
        };
        match port.reserve(&over) {
            Err(BudgetRefusal::Exceeded(_)) => {}
            other => return Err(format!("expected Exceeded, got {other:?}")),
        }
        port.release(&within)
            .map_err(|refusal| format!("{refusal:?}"))
    }
}
