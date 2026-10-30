//! The read route on the sandbox: the verified GSD-Core guest answers `orch_status` (U12.5).
//!
//! The host reads the Task Store into one snapshot object. The guest takes that whole object as its
//! input, so the 100 000-byte `--input` cap does not apply to it (GA2). Each call instantiates the
//! verified guest with an empty linker (OD-6), invokes it and decodes the reply. A sandbox fault
//! (identity, instantiation, or a trap other than a timeout) is answered in-process by the host Rust
//! logic on the same snapshot (GA3, OD-5), and the engine records that it did and with which wire
//! code ([`SandboxEngine::failover`], [`failover_notice`]) so the binary can say so on stderr. A guest
//! answer, including the error it reports, is final, and so is a timeout. Every other entrypoint
//! refuses with `E-DEP`.
//!
//! The write route `orch_status --transition` (phase 14.5, unit U3.3b) runs on the write-enabled guest behind the `sandbox-write`
//! feature: [`SandboxDecider`] is the decide step of the host's [`TaskStoreWriter`](gsd_taskwriter::TaskStoreWriter), inside the
//! write transaction. The guest is a verified shadow of the host decision (D-P29): the host computes its own decision on the same
//! facts and uses the guest's only when it is exactly equal; any difference or fault before the apply step is answered by the host
//! decision (and recorded, so the binary can say so on stderr). The module is loaded before the transaction opens; the guest gets
//! the budget less a reserve for apply and commit; only a spent deadline is `E-TIMEOUT`, with the transaction rolled back and no
//! failover; after the commit nothing is decided again. Without the feature the call is refused before any write, naming the
//! feature.

use std::fmt::Display;
use std::sync::{Mutex, OnceLock, PoisonError};
use std::time::{Duration, Instant};

use gsd_failure::ErrorCode;
use gsd_pool::PoolError;
use gsd_route::transition_args::{parse_transition_args, TransitionArgs};
use gsd_route::transition_plan::{Facts, Request, TransitionError, TransitionPlan, WriteState};
use gsd_route::transition_wire::{decode_reply, encode_request, Decision};
use gsd_route::{
    decode_reply_wire, orch_status_reply, to_python_json, Json, ReadArgs, Reply, SeamError,
};
use gsd_sandbox::wasmtime_limits::deadline_ticks_for;
use gsd_sandbox::wasmtime_module_build::{MODULE_NAME, MODULE_TARGET};
use gsd_sandbox::{
    GuestInvokeError, GuestLoader, Limits, LoadError, LoadedGuest, ModuleManifest, GUEST_MODULE,
};
use gsd_taskwriter::{ApprovalVerifier, Decider, HostDecider};

use crate::route_table::is_read_only_route;
use crate::strangler_seam::{
    empty_snapshot, no_route, read_args, read_snapshot, single_thread_runtime, within_deadline,
    CoreBackend, Entrypoint, Invocation,
};
#[cfg(feature = "sandbox-write-2")]
use crate::write_route::ingest::ingest_reply_checked;
#[cfg(feature = "sandbox-write-2")]
use crate::write_route::shadow_approval::{PreparedGuest, SandboxApproval};
#[cfg(feature = "sandbox-write-2")]
#[doc(hidden)]
pub use crate::write_route::shadow_ingest::ShadowIngest;
#[cfg(feature = "sandbox-write-2")]
use crate::write_route::shadow_plan::PlanShadow;
use crate::write_route::transition_reply_with;
#[cfg(feature = "sandbox-write-2")]
use gsd_route::ingest_args::{parse_ingest_args, IngestArgs};

/// Guest fuel per call (OD-6).
const FUEL: u64 = 1_000_000_000;
/// Guest linear memory cap per call: 16 MiB (OD-6).
const MEMORY_BYTES: usize = 16 * 1024 * 1024;
/// The semver the manifest of the built guest carries, as the sandbox tests verify it.
const GUEST_SEMVER: &str = "0.1.0";

/// The sandbox engine: serves the read-only `orch_status` route from the verified guest.
pub struct SandboxEngine {
    runtime: tokio::runtime::Runtime,
    loader: GuestLoader,
    module: &'static [u8],
    pin: &'static str,
    /// The module after its first successful identity check and compile; later calls reuse it.
    loaded: OnceLock<LoadedGuest>,
    /// The code of the sandbox fault the latest call failed over on; None when it did not fail over.
    failover: Mutex<Option<ErrorCode>>,
}

/// One guest call: what the guest or the wire answered, or a fault of the sandbox itself.
enum GuestCall {
    Answered(Result<Reply, PoolError>),
    Faulted(PoolError),
}

impl SandboxEngine {
    /// Builds the engine over the guest this crate was built with and its committed pin.
    pub fn new() -> Result<Self, SeamError> {
        let (module, pin) = GUEST_MODULE;
        Self::with_module(module, pin)
    }

    /// Builds the engine over `module`, which must match `pin` (lowercase hex SHA-256) on load.
    fn with_module(module: &'static [u8], pin: &'static str) -> Result<Self, SeamError> {
        let loader = GuestLoader::new()
            .map_err(|error| SeamError::new(error.error_code(), error.to_string()))?;
        Ok(Self {
            runtime: single_thread_runtime()?,
            loader,
            module,
            pin,
            loaded: OnceLock::new(),
            failover: Mutex::new(None),
        })
    }

    /// The guest's health: the module matches the pin and compiles, it instantiates, and one invoke
    /// on an empty snapshot answers. Every failure is `E-DEP`; each stage has its own fixed message.
    pub fn health(&self, deadline: Duration) -> Result<(), SeamError> {
        let loaded = self.load().map_err(|_| {
            SeamError::new(
                ErrorCode::Dep,
                "guest module failed its identity check or did not compile",
            )
        })?;
        let mut instance = self
            .loader
            .instantiate(&loaded, &limits(deadline))
            .map_err(|_| SeamError::new(ErrorCode::Dep, "guest module did not instantiate"))?;
        let probe = to_python_json(&empty_snapshot()).into_bytes();
        let bytes = instance
            .invoke(&probe)
            .map_err(|_| SeamError::new(ErrorCode::Dep, "guest probe did not answer"))?;
        match decode_reply_wire(&bytes) {
            Ok(reply) if reply.exit_code != 2 => Ok(()),
            _ => Err(SeamError::new(
                ErrorCode::Dep,
                "guest probe reply is not a success",
            )),
        }
    }

    /// The code of the sandbox fault the latest call failed over on, answered by host Rust, or None
    /// when that call was answered by the guest (its error included), timed out, or was refused.
    /// Each call resets it. It carries the wire code only, never a path or a message. A write that
    /// failed over before its apply step reports it the same way.
    pub fn failover(&self) -> Option<ErrorCode> {
        *self.failover.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Replaces the failover record: the code of the fault a call is answered on, or None.
    pub(crate) fn record_failover(&self, code: Option<ErrorCode>) {
        *self.failover.lock().unwrap_or_else(PoisonError::into_inner) = code;
    }

    /// Verifies the module bytes against the pin and compiles them.
    fn load(&self) -> Result<LoadedGuest, LoadError> {
        let manifest = ModuleManifest {
            name: MODULE_NAME.to_owned(),
            semver: GUEST_SEMVER.to_owned(),
            target: MODULE_TARGET.to_owned(),
            sha256: self.pin.trim().to_owned(),
        };
        self.loader
            .load_verified(self.module, &manifest, GUEST_SEMVER)
    }

    /// The verified guest, loaded on first use. A failed load is not cached, so it is retried.
    fn loaded_guest(&self) -> Result<&LoadedGuest, LoadError> {
        if let Some(loaded) = self.loaded.get() {
            return Ok(loaded);
        }
        let loaded = self.load()?;
        Ok(self.loaded.get_or_init(|| loaded))
    }

    /// Sends `request` to the guest under `limits` and classifies what comes back.
    fn answer(&self, request: &[u8], limits: &Limits) -> GuestCall {
        match self.loaded_guest() {
            Ok(loaded) => self.run(loaded, request, limits),
            Err(error) => GuestCall::Faulted(fault(error.error_code(), error)),
        }
    }

    /// Instantiates `loaded` under `limits` and invokes it with `request`: the reply bytes, or how the call failed. An instantiation
    /// failure keeps the guest from answering as a trap does, so it is reported as one, with its code.
    fn invoke(
        &self,
        loaded: &LoadedGuest,
        request: &[u8],
        limits: &Limits,
    ) -> Result<Vec<u8>, GuestInvokeError> {
        let mut instance = self
            .loader
            .instantiate(loaded, limits)
            .map_err(|error| GuestInvokeError::Trap(error.error_code()))?;
        instance.invoke(request)
    }

    /// Invokes `loaded` with `request` under `limits` and classifies what comes back.
    fn run(&self, loaded: &LoadedGuest, request: &[u8], limits: &Limits) -> GuestCall {
        match self.invoke(loaded, request, limits) {
            Ok(bytes) => GuestCall::Answered(decode_reply_wire(&bytes).map_err(sandbox_answer)),
            Err(GuestInvokeError::Guest { code, message }) => {
                GuestCall::Answered(Err(PoolError::Sandbox { code, message }))
            }
            Err(GuestInvokeError::Trap(code)) => {
                GuestCall::Faulted(fault(code, "guest call did not complete"))
            }
        }
    }

    /// Reads the snapshot, answers from the guest, and fails over to the host logic on a fault.
    async fn guest_or_failover(
        &self,
        args: &ReadArgs,
        call: &Invocation,
    ) -> Result<Reply, SeamError> {
        self.guest_or_failover_with(args, call, limits).await
    }

    /// `guest_or_failover` with its guest limits made from the budget left after the snapshot.
    /// Production passes [`limits`]; the tests pass other fuel and memory caps.
    async fn guest_or_failover_with(
        &self,
        args: &ReadArgs,
        call: &Invocation,
        make_limits: impl Fn(Duration) -> Limits,
    ) -> Result<Reply, SeamError> {
        let started = Instant::now();
        let request = read_snapshot(args, call).await?;
        let budget = budget_after(call.deadline, started.elapsed())?;
        let wire = to_python_json(&request).into_bytes();
        let outcome = self.answer(&wire, &make_limits(budget));
        // The guest call is synchronous: a result that lands past the deadline is not reported.
        budget_after(call.deadline, started.elapsed())?;
        self.record_failover(failover_code(&outcome));
        settle(outcome, &request)
    }
}

/// The per-call limits (OD-6): fuel, memory, and an epoch deadline from the call deadline.
pub(crate) fn limits(deadline: Duration) -> Limits {
    Limits {
        fuel: FUEL,
        memory_bytes: MEMORY_BYTES,
        deadline_ticks: Some(deadline_ticks_for(deadline)),
    }
}

/// The budget left of `total` once `elapsed` is spent. None left is `E-TIMEOUT`: the guest is
/// not called without a budget, and a call that ends with none has run past its deadline.
fn budget_after(total: Duration, elapsed: Duration) -> Result<Duration, SeamError> {
    match total.checked_sub(elapsed) {
        Some(remaining) if !remaining.is_zero() => Ok(remaining),
        _ => Err(SeamError::new(
            ErrorCode::Timeout,
            format!("guest call exceeded {} s", total.as_secs()),
        )),
    }
}

/// The read's answer from the guest's outcome. A guest answer, its error included, is final. A
/// timeout fails. Any other sandbox fault fails over to the host logic on `request` (GA3).
fn settle(outcome: GuestCall, request: &Json) -> Result<Reply, SeamError> {
    match outcome {
        GuestCall::Answered(answer) => answer.map_err(into_seam),
        GuestCall::Faulted(error) if is_timeout(&error) => Err(into_seam(error)),
        GuestCall::Faulted(_) => orch_status_reply(request),
    }
}

/// The code of the sandbox fault that `settle` answers from host Rust, or None when it does not
/// fail over: a guest answer, its error included, and a timeout are final.
fn failover_code(outcome: &GuestCall) -> Option<ErrorCode> {
    match outcome {
        GuestCall::Faulted(PoolError::Sandbox { code, .. }) if *code != ErrorCode::Timeout => {
            Some(*code)
        }
        GuestCall::Faulted(error) if !is_timeout(error) => Some(ErrorCode::Internal),
        GuestCall::Answered(_) | GuestCall::Faulted(_) => None,
    }
}

/// The one line a failover prints to stderr. It is built from the wire code only: no path, message
/// or environment value can reach it.
pub fn failover_notice(code: ErrorCode) -> String {
    format!(
        "gsd_engine: sandbox fault {}; answered by host Rust",
        code.as_str()
    )
}

/// Builds a sandbox fault in the seam's `CODE: text` form.
fn fault(code: ErrorCode, reason: impl Display) -> PoolError {
    PoolError::Sandbox {
        code,
        message: format!("{}: {reason}", code.as_str()),
    }
}

/// Carries a reply the guest wrote but the wire rejected as an answer, keeping its code.
fn sandbox_answer(error: SeamError) -> PoolError {
    PoolError::Sandbox {
        code: error.code(),
        message: error.message().to_owned(),
    }
}

/// True for the sandbox's deadline fault, which never fails over (OD-5).
fn is_timeout(error: &PoolError) -> bool {
    matches!(
        error,
        PoolError::Sandbox {
            code: ErrorCode::Timeout,
            ..
        }
    )
}

/// Maps a sandbox error to the seam error, keeping its code and message.
fn into_seam(error: PoolError) -> SeamError {
    match error {
        PoolError::Sandbox { code, message } => SeamError::unprefixed(code, message),
        other => SeamError::new(ErrorCode::Internal, other.to_string()),
    }
}

impl CoreBackend for SandboxEngine {
    fn swarm_run(&self, call: &Invocation) -> Result<Reply, SeamError> {
        Err(no_route(&call.script))
    }

    #[cfg(feature = "sandbox-write-2")]
    fn orch_plan(&self, call: &Invocation) -> Result<Reply, SeamError> {
        if !crate::route_table::is_plan_route(Entrypoint::OrchPlan, &call.argv) {
            self.record_failover(None);
            return Err(no_route(&call.script));
        }
        self.plan_via(self, limits, call)
    }

    /// Without `sandbox-write-2` the embedded guest has no plan mode: every `orch_plan` call is refused before any write.
    #[cfg(not(feature = "sandbox-write-2"))]
    fn orch_plan(&self, call: &Invocation) -> Result<Reply, SeamError> {
        Err(no_route(&call.script))
    }

    fn orch_status(&self, call: &Invocation) -> Result<Reply, SeamError> {
        self.record_failover(None);
        #[cfg(feature = "sandbox-write-2")]
        if let Some(args) = parse_ingest_args(&call.argv) {
            return self.orch_status_ingest(&args, call);
        }
        if let Some(args) = parse_transition_args(&call.argv) {
            return self.orch_status_transition(&args, call);
        }
        if !is_read_only_route(Entrypoint::OrchStatus, &call.argv) {
            return Err(no_route(&call.script));
        }
        let args = read_args(call)?;
        self.runtime.block_on(within_deadline(
            call.deadline,
            &call.script,
            self.guest_or_failover(&args, call),
        ))
    }

    fn orch_assign(&self, call: &Invocation) -> Result<Reply, SeamError> {
        Err(no_route(&call.script))
    }

    fn qa_gate(&self, call: &Invocation) -> Result<Reply, SeamError> {
        Err(no_route(&call.script))
    }

    fn rev_gate(&self, call: &Invocation) -> Result<Reply, SeamError> {
        Err(no_route(&call.script))
    }

    fn sec_gate(&self, call: &Invocation) -> Result<Reply, SeamError> {
        Err(no_route(&call.script))
    }

    fn rel_plan(&self, call: &Invocation) -> Result<Reply, SeamError> {
        Err(no_route(&call.script))
    }

    fn swarm_tool(&self, call: &Invocation) -> Result<Reply, SeamError> {
        Err(no_route(&call.script))
    }

    fn lane_verb(&self, call: &Invocation) -> Result<Reply, SeamError> {
        Err(no_route(&call.script))
    }
}

/// Largest reply the host reads back from the write guest. A plan is a few hundred bytes plus the task's `notes`; a larger reply is a
/// fault of the guest, answered by the host decision, which gives the plan the guest should have given. Public for the boundary test
/// only.
#[doc(hidden)]
pub const WRITE_REPLY_CAP_BYTES: usize = 1024 * 1024;

/// The most of a write's budget [`SandboxDecider`] keeps back from the guest for what follows the decision: the apply step, the
/// `COMMIT`, closing the store and the manifest. That is milliseconds of work on a healthy host and seconds on a saturated one.
const WRITE_RESERVE_CAP: Duration = Duration::from_secs(2);

/// The reserve is one tenth of the deadline when that is below [`WRITE_RESERVE_CAP`], so a short deadline keeps nine tenths of its
/// budget for the guest and a long one gives up two seconds.
const WRITE_RESERVE_DIVISOR: u32 = 10;

/// The write guest as [`SandboxDecider`] sees it: one request in, reply bytes or a fault out.
///
/// [`SandboxEngine`] is the production guest: it checks the module against its pin, instantiates it with an empty linker under the
/// call's [`Limits`] and invokes it. A test stands in a scripted guest to show how the decider treats one that misbehaves.
pub trait WriteGuest: Sync {
    /// Makes the guest ready to be called: for [`SandboxEngine`], the identity check against the pin and the compile of the module.
    /// [`SandboxDecider::new`] calls it once, before the write transaction opens, so that work never runs under the write lock. An
    /// `Err` is remembered and the decider answers from the host without calling the guest; `E-TIMEOUT` is not a code of a load and
    /// is read as `E-INTERNAL`. A guest with nothing to prepare keeps the default.
    fn prepare(&self) -> Result<(), ErrorCode> {
        Ok(())
    }

    /// Sends `request` under `limits`. `Err` carries the taxonomy code of the fault that kept the guest from answering. `E-TIMEOUT`
    /// means the sandbox stopped the guest: its epoch deadline, or its fuel under a deadline. The decider tells the two apart by the
    /// host clock (the epoch deadline cannot trap before the budget it was given is spent). Any other code is a fault the decider
    /// answers from the host.
    fn call(&self, request: &[u8], limits: &Limits) -> Result<Vec<u8>, ErrorCode>;
}

impl WriteGuest for SandboxEngine {
    fn prepare(&self) -> Result<(), ErrorCode> {
        self.loaded_guest()
            .map(drop)
            .map_err(|error| error.error_code())
    }

    fn call(&self, request: &[u8], limits: &Limits) -> Result<Vec<u8>, ErrorCode> {
        let loaded = self.loaded_guest().map_err(|error| error.error_code())?;
        self.invoke(loaded, request, limits).map_err(fault_of)
    }
}

/// The code of a failed guest invoke. The deadline is the sandbox's to declare: a guest that claims `E-TIMEOUT` in an error of its
/// own is an ordinary fault, answered by the host decision, and never a reason to end the call.
fn fault_of(error: GuestInvokeError) -> ErrorCode {
    match error {
        GuestInvokeError::Trap(code) => code,
        GuestInvokeError::Guest {
            code: ErrorCode::Timeout,
            ..
        } => ErrorCode::Internal,
        GuestInvokeError::Guest { code, .. } => code,
    }
}

/// The decide step of a write on the sandbox: the guest as a VERIFIED SHADOW of the host decision (the GA3 amendment of phase 14.5,
/// decision D-P29).
///
/// [`TaskStoreWriter::transition`](gsd_taskwriter::TaskStoreWriter::transition) calls [`Decider::decide`] once, inside its
/// `BEGIN IMMEDIATE` transaction, on the row it read there, and validates, applies and commits what comes back without ever calling
/// a decider again. The failover is therefore reachable only here, before the first write, and never after a commit: there is no
/// second decision to make once the first has been written. The plan validator checks the shape and the legality of a plan, not its
/// values, so a well-formed plan that is wrong (an approval without the facts, a gate task moved by hand, a skipped `max_attempts`,
/// any counter or `notes`, any `reason`, extra effects) would be written. The guest is therefore never taken at its word. In this
/// order:
///
/// 0. [`Self::new`] asks the guest to prepare (for the engine, the pin check and the compile), outside the transaction. A failure
///    is remembered; step 3 answers it;
/// 1. the host computes its own decision on the same facts, request and `now` with [`HostDecider`] (pure and cheap);
/// 2. the budget left before the guest's deadline (the call deadline less a reserve, see [`WRITE_RESERVE_CAP`]) is spent:
///    `E-TIMEOUT`, nothing written, no failover (OD-5: a guest that burns the deadline would burn the host's);
/// 3. the guest could not be prepared: failover, the guest is not called;
/// 4. the request is encoded with [`encode_request`] and the guest invoked under fuel, memory and an epoch deadline derived from
///    the budget of step 2; the reply is read back with [`decode_reply`];
/// 5. a call that ends past the call deadline, or whose guest the sandbox stopped (`E-TIMEOUT`) when the host clock shows the guest's
///    budget spent, ends the call as `E-TIMEOUT`, no failover. An `E-TIMEOUT` while budget remains can only be the fuel (the epoch
///    deadline cannot trap before its budget is spent, see `deadline_ticks_for`): it is a fault of the guest, step 6;
/// 6. any other fault answers with the host decision, after recording its code ([`Self::failover`]): a trap, fuel or memory
///    exhausted, a pin or instantiation failure, an `invoke-error`, a reply that is too large or not a reply, and any reply whose
///    plan or refusal is not EXACTLY the host decision (`E-CONTRACT`);
/// 7. a reply equal to the host decision is the answer, and what is returned is the host's own value. Python's refusals (illegal
///    transition, the manual-transition `E-POLICY`, `max_attempts`, `not_started`) are decisions like any other.
///
/// The writer then validates, applies and commits the host's decision, so nothing the guest says is ever written or printed.
pub struct SandboxDecider<'g> {
    guest: &'g dyn WriteGuest,
    started: Instant,
    /// The call deadline: nothing the guest returns after it is used.
    deadline: Duration,
    /// `deadline` less the reserve, counted from `started`: the guest's epoch deadline is made from the time left before it.
    guest_deadline: Duration,
    make_limits: fn(Duration) -> Limits,
    /// What [`WriteGuest::prepare`] answered.
    prepared: Result<(), ErrorCode>,
    failover: Mutex<Option<ErrorCode>>,
}

impl<'g> SandboxDecider<'g> {
    /// A decider over `guest` for a call that started at `started` and must end within `deadline`, under the production limits
    /// (OD-6: fuel 1e9, 16 MiB, an epoch deadline from the budget left less the reserve for apply and commit). Prepares the guest
    /// now: call it before the write transaction opens.
    pub fn new(guest: &'g dyn WriteGuest, started: Instant, deadline: Duration) -> Self {
        let reserve = (deadline / WRITE_RESERVE_DIVISOR).min(WRITE_RESERVE_CAP);
        let prepared = match guest.prepare() {
            Err(ErrorCode::Timeout) => Err(ErrorCode::Internal),
            other => other,
        };
        Self {
            guest,
            started,
            deadline,
            guest_deadline: deadline - reserve,
            make_limits: limits,
            prepared,
            failover: Mutex::new(None),
        }
    }

    /// The code of the guest fault the decision was answered on, by the host decision; None when the guest answered, the deadline
    /// was spent, or nothing was decided.
    pub fn failover(&self) -> Option<ErrorCode> {
        *self.failover.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Asks the guest to decide `facts` and holds its answer against `host`, the host's own decision on the same facts: `Ok` only
    /// when the guest answered with exactly that decision, else the code of the fault that kept it from deciding (`E-TIMEOUT`
    /// when the deadline is spent, see the type documentation).
    fn verify_guest(
        &self,
        facts: &Facts,
        request: &Request,
        now: f64,
        host: &Decision,
    ) -> Result<(), ErrorCode> {
        let spent = |_| ErrorCode::Timeout;
        let budget = budget_after(self.guest_deadline, self.started.elapsed()).map_err(spent)?;
        self.prepared?;
        let wire = encode_request(facts, request, now);
        let outcome = self
            .guest
            .call(wire.as_bytes(), &(self.make_limits)(budget));
        let elapsed = self.started.elapsed();
        // The guest call is synchronous: whatever it returns after the deadline is not used.
        budget_after(self.deadline, elapsed).map_err(spent)?;
        let bytes = match outcome {
            // The epoch deadline cannot trap before the budget it was made from is spent, so an `E-TIMEOUT` before that is fuel.
            Err(ErrorCode::Timeout) if elapsed < self.guest_deadline => {
                return Err(ErrorCode::Internal);
            }
            other => other?,
        };
        if bytes.len() > WRITE_REPLY_CAP_BYTES {
            return Err(ErrorCode::Capacity);
        }
        let decision = decode_reply(&bytes).map_err(|error| error.code())?;
        if decision == *host {
            Ok(())
        } else {
            Err(ErrorCode::Contract)
        }
    }
}

impl Decider for SandboxDecider<'_> {
    fn decide(
        &self,
        facts: &Facts,
        request: &Request,
        now: f64,
    ) -> Result<TransitionPlan, TransitionError> {
        let host = HostDecider.decide(facts, request, now);
        match self.verify_guest(facts, request, now, &host) {
            Ok(()) => host,
            Err(ErrorCode::Timeout) => Err(TransitionError {
                code: ErrorCode::Timeout,
                message: format!(
                    "{}: guest call exceeded {} s",
                    ErrorCode::Timeout.as_str(),
                    self.deadline.as_secs()
                ),
                details: Vec::new(),
                write_state: WriteState::Final,
            }),
            Err(code) => {
                *self.failover.lock().unwrap_or_else(PoisonError::into_inner) = Some(code);
                host
            }
        }
    }
}

impl SandboxEngine {
    /// The `--transition` route with `guest` standing in for the engine's own guest and `make_limits` giving the guest's limits
    /// from the budget left of the call deadline. [`CoreBackend::orch_status`] calls it with the engine itself and the production
    /// limits; a test calls it with a scripted guest. The guest is prepared here, before the writer opens its transaction. Records
    /// the failover code for [`Self::failover`]. Public for the integration tests only.
    #[doc(hidden)]
    pub fn transition_via(
        &self,
        guest: &dyn WriteGuest,
        make_limits: fn(Duration) -> Limits,
        args: &TransitionArgs,
        call: &Invocation,
    ) -> Result<Reply, SeamError> {
        let started = Instant::now();
        let mut decider = SandboxDecider::new(guest, started, call.deadline);
        decider.make_limits = make_limits;
        // The approval facts of an APPROVED target are verified by the guest as well (pin P2): the check runs inside the write
        // transaction, before the decider, on the sandbox-write-2 build only. It shares the decider's prepare, so the guest is prepared
        // once per call.
        #[cfg(feature = "sandbox-write-2")]
        let prepared_guest = PreparedGuest::new(guest, decider.prepared);
        #[cfg(feature = "sandbox-write-2")]
        let approval = (args.state == "APPROVED")
            .then(|| SandboxApproval::new(&prepared_guest, make_limits, started, call.deadline));
        #[cfg(feature = "sandbox-write-2")]
        let verifier: Option<&dyn ApprovalVerifier> = approval.as_ref().map(|v| v as _);
        #[cfg(not(feature = "sandbox-write-2"))]
        let verifier: Option<&dyn ApprovalVerifier> = None;
        let reply = self.runtime.block_on(within_deadline(
            call.deadline,
            &call.script,
            transition_reply_with(args, call, &decider, verifier),
        ));
        // The record names the first fault of the call: the approval check runs before the decider.
        #[cfg(feature = "sandbox-write-2")]
        let fault = approval
            .as_ref()
            .and_then(SandboxApproval::failover)
            .or_else(|| decider.failover());
        #[cfg(not(feature = "sandbox-write-2"))]
        let fault = decider.failover();
        self.record_failover(fault);
        reply
    }

    /// The `--ingest` route with `guest` standing in for the engine's own guest, as [`Self::transition_via`]. The host plans the call
    /// and the reconcile stage; the guest verifies both inside the write transaction, before the first write, and the host's answer
    /// is what the call returns. A spent deadline is `E-TIMEOUT` with nothing written. Records the failover code for
    /// [`Self::failover`]. Public for the integration tests only.
    #[cfg(feature = "sandbox-write-2")]
    #[doc(hidden)]
    pub fn ingest_via(
        &self,
        guest: &dyn WriteGuest,
        make_limits: fn(Duration) -> Limits,
        args: &IngestArgs,
        call: &Invocation,
    ) -> Result<Reply, SeamError> {
        let check = ShadowIngest::new(guest, make_limits, Instant::now(), call.deadline);
        let reply = self.runtime.block_on(within_deadline(
            call.deadline,
            &call.script,
            ingest_reply_checked(args, call, &check),
        ));
        self.record_failover(check.failover());
        reply
    }

    /// The `--ingest` route on the write guest the binary embeds (`sandbox-write-2`).
    #[cfg(feature = "sandbox-write-2")]
    fn orch_status_ingest(&self, args: &IngestArgs, call: &Invocation) -> Result<Reply, SeamError> {
        self.ingest_via(self, limits, args, call)
    }

    /// The `--transition` route on the write guest the binary embeds.
    #[cfg(feature = "sandbox-write")]
    fn orch_status_transition(
        &self,
        args: &TransitionArgs,
        call: &Invocation,
    ) -> Result<Reply, SeamError> {
        self.transition_via(self, limits, args, call)
    }

    /// Without the `sandbox-write` feature the embedded guest has no write mode: the call is refused before any write, naming the
    /// missing feature, and the caller falls back.
    #[cfg(not(feature = "sandbox-write"))]
    fn orch_status_transition(
        &self,
        _args: &TransitionArgs,
        call: &Invocation,
    ) -> Result<Reply, SeamError> {
        Err(crate::strangler_seam::before_any_write(SeamError::new(
            ErrorCode::Dep,
            format!(
                "the rust engine has no route for {}: the sandbox backend has no write guest in this build \
                 (rebuild gsd-adapter with the sandbox-write feature, or use GSD_RUST_BACKEND=host); use GSD_ENGINE=python",
                call.script
            ),
        )))
    }
}

#[cfg(feature = "sandbox-write-2")]
impl SandboxEngine {
    /// The `orch_plan` route with `guest` standing in for the engine's own guest and `make_limits` giving the guest's limits from the
    /// budget left of the call deadline. [`CoreBackend::orch_plan`] calls it with the engine itself and the production limits; a test
    /// calls it with a scripted guest. The verified shadow is prepared before the call reads the store, so the identity check and the
    /// compile never run under the write lock. The shadow enforces the call deadline at its check, before the first write, so no
    /// wrapper ends the call after a commit: the host route has none either. Records the failover code for [`Self::failover`]. Public for
    /// the integration tests only.
    #[doc(hidden)]
    pub fn plan_via(
        &self,
        guest: &dyn WriteGuest,
        make_limits: fn(Duration) -> Limits,
        call: &Invocation,
    ) -> Result<Reply, SeamError> {
        let shadow = PlanShadow::new(guest, make_limits, Instant::now(), call.deadline);
        let reply = self
            .runtime
            .block_on(crate::write_route::plan::answer_checked(call, &shadow));
        self.record_failover(shadow.failover());
        reply
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::strangler_seam::{invoke, open_store, RustEngine, INPUT_CAP_BYTES};
    use sqlx::sqlite::SqlitePool;
    use std::fs;
    use std::future::Future;
    use std::path::Path;
    use tempfile::TempDir;

    type TestResult = Result<(), String>;

    /// A pin that no built guest matches.
    const WRONG_PIN: &str = "0000000000000000000000000000000000000000000000000000000000000000";

    fn message(error: SeamError) -> String {
        error.message().to_string()
    }

    fn block<F: Future>(future: F) -> Result<F::Output, String> {
        let runtime = single_thread_runtime().map_err(message)?;
        Ok(runtime.block_on(future))
    }

    fn engine() -> Result<SandboxEngine, String> {
        SandboxEngine::new().map_err(message)
    }

    fn host() -> Result<RustEngine, String> {
        RustEngine::new().map_err(message)
    }

    fn call(root: &Path, swarm: &Path, argv: &[&str]) -> Invocation {
        Invocation {
            agent: "A01".to_string(),
            script: "orch_status".to_string(),
            argv: argv.iter().map(|s| s.to_string()).collect(),
            root: root.to_path_buf(),
            env: vec![("SWARM_DIR".to_string(), swarm.display().to_string())],
            deadline: Duration::from_secs(30),
            effects_path: None,
        }
    }

    /// The wire snapshot of a read, exactly as the sandbox path encodes it.
    fn snapshot(engine: &SandboxEngine, call: &Invocation) -> Result<Vec<u8>, String> {
        let args = read_args(call).map_err(message)?;
        let request = engine
            .runtime
            .block_on(read_snapshot(&args, call))
            .map_err(message)?;
        Ok(to_python_json(&request).into_bytes())
    }

    /// Answers a read through the guest alone, with no failover.
    fn guest_call(engine: &SandboxEngine, call: &Invocation) -> Result<GuestCall, String> {
        let wire = snapshot(engine, call)?;
        Ok(engine.answer(&wire, &limits(call.deadline)))
    }

    /// The reply the guest gives, or the reason it did not give one.
    fn guest_reply(engine: &SandboxEngine, call: &Invocation) -> Result<Reply, String> {
        match guest_call(engine, call)? {
            GuestCall::Answered(Ok(reply)) => Ok(reply),
            GuestCall::Answered(Err(error)) => Err(format!("guest refused: {error}")),
            GuestCall::Faulted(error) => Err(format!("guest faulted: {error}")),
        }
    }

    async fn insert_task(
        pool: &SqlitePool,
        id: &str,
        state: &str,
        depth: i64,
        created: f64,
        deps: &str,
    ) -> Result<(), sqlx::Error> {
        sqlx::query(
            "INSERT INTO tasks (task_id, correlation_id, dag_depth, capability, agent_id, title, risk_class, state, depends_on, notes, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        )
        .bind(id)
        .bind("C1")
        .bind(depth)
        .bind("cap")
        .bind("A05")
        .bind("title")
        .bind("low")
        .bind(state)
        .bind(deps)
        .bind("{}")
        .bind(created)
        .bind(created)
        .execute(pool)
        .await?;
        Ok(())
    }

    /// Writes a chain of `count` tasks (at least one) under correlation `C1`: `T-0000` is the root
    /// and each task depends on the one before it. Even tasks are DONE with one passing review.
    /// `T-0000` has two transitions. Two run-log lines are written.
    async fn seed(swarm: &Path, count: usize) -> Result<(), String> {
        let pool = open_store(swarm).await.map_err(message)?;
        for index in 0..count {
            let id = format!("T-{index:04}");
            let done = index % 2 == 0;
            let state = if done { "DONE" } else { "PLANNED" };
            let deps = if index == 0 {
                "[]".to_string()
            } else {
                format!("[\"T-{:04}\"]", index - 1)
            };
            insert_task(&pool, &id, state, (index % 7) as i64, index as f64, &deps)
                .await
                .map_err(|e| e.to_string())?;
            if done {
                sqlx::query(
                    "INSERT INTO verdicts (task_id, gate, verdict, agent_id, findings, expires_at, ts) VALUES (?, 'review', 'pass', 'A09', '[]', 9.0, ?)",
                )
                .bind(id.as_str())
                .bind(index as f64)
                .execute(&pool)
                .await
                .map_err(|e| e.to_string())?;
            }
        }
        sqlx::query(
            "INSERT INTO transitions (task_id, from_state, to_state, actor, reason, ts) VALUES ('T-0000', NULL, 'CREATED', 'system', 'created', 1.0), ('T-0000', 'CREATED', 'DONE', 'A05', 'done', 2.0)",
        )
        .execute(&pool)
        .await
        .map_err(|e| e.to_string())?;
        pool.close().await;
        fs::write(
            swarm.join("events.jsonl"),
            "{\"type\":\"a\",\"correlation_id\":\"C1\"}\n{\"type\":\"b\",\"correlation_id\":\"C2\"}\n",
        )
        .map_err(|e| e.to_string())
    }

    #[test]
    fn list_and_history_of_the_500_task_snapshot_match_host_rust() -> TestResult {
        let root = TempDir::new().map_err(|e| e.to_string())?;
        let swarm = root.path().join(".swarm");
        block(seed(&swarm, 500))??;
        let sandbox = engine()?;
        let host = host()?;

        let list = call(root.path(), &swarm, &["--json"]);
        assert!(snapshot(&sandbox, &list)?.len() > INPUT_CAP_BYTES);
        let host_list = host.orch_status(&list).map_err(message)?;
        assert_eq!(host_list.exit_code, 0);
        assert!(host_list.stdout.contains("T-0499"));
        assert_eq!(guest_reply(&sandbox, &list)?, host_list);

        let history = call(root.path(), &swarm, &["--history", "T-0000", "--json"]);
        let host_history = host.orch_status(&history).map_err(message)?;
        assert!(host_history.stdout.contains("T-0000"));
        assert_eq!(guest_reply(&sandbox, &history)?, host_history);
        Ok(())
    }

    #[test]
    fn unknown_task_is_answered_by_the_guest_with_host_parity() -> TestResult {
        let root = TempDir::new().map_err(|e| e.to_string())?;
        let swarm = root.path().join(".swarm");
        block(seed(&swarm, 1))??;
        let sandbox = engine()?;
        let unknown = call(root.path(), &swarm, &["--history", "T-9999", "--json"]);

        let host_error = host()?
            .orch_status(&unknown)
            .err()
            .ok_or("host answered an unknown task")?;
        assert!(matches!(
            guest_call(&sandbox, &unknown)?,
            GuestCall::Answered(Err(_))
        ));
        assert_eq!(sandbox.orch_status(&unknown).err(), Some(host_error));
        Ok(())
    }

    #[test]
    fn empty_store_matches_host_rust() -> TestResult {
        let root = TempDir::new().map_err(|e| e.to_string())?;
        let swarm = root.path().join(".swarm");
        block(open_store(&swarm))?.map_err(message)?;
        let sandbox = engine()?;
        let host = host()?;

        let list = call(root.path(), &swarm, &["--json"]);
        let host_list = host.orch_status(&list).map_err(message)?;
        assert_eq!(guest_reply(&sandbox, &list)?, host_list);
        sandbox.health(Duration::from_secs(30)).map_err(message)
    }

    #[test]
    fn one_task_store_matches_host_rust() -> TestResult {
        let root = TempDir::new().map_err(|e| e.to_string())?;
        let swarm = root.path().join(".swarm");
        block(seed(&swarm, 1))??;
        let sandbox = engine()?;
        let host = host()?;

        let list = call(root.path(), &swarm, &["--json"]);
        let host_list = host.orch_status(&list).map_err(message)?;
        assert_eq!(guest_reply(&sandbox, &list)?, host_list);

        let history = call(root.path(), &swarm, &["--history", "T-0000", "--json"]);
        let host_history = host.orch_status(&history).map_err(message)?;
        assert_eq!(guest_reply(&sandbox, &history)?, host_history);
        Ok(())
    }

    #[test]
    fn mutating_argv_is_refused_with_dep() -> TestResult {
        let root = TempDir::new().map_err(|e| e.to_string())?;
        let swarm = root.path().join(".swarm");
        block(seed(&swarm, 1))??;
        let sandbox = engine()?;

        let ingest = ["--ingest", "result.json", "--json"];
        // With the `sandbox-write` feature the write guest serves a transition: that path is tested in
        // tests/write_route_sandbox_faults.rs and tests/write_route.rs.
        #[cfg(not(feature = "sandbox-write"))]
        let transition = ["--transition", "T-0000", "DONE", "--json"];
        #[cfg(not(feature = "sandbox-write"))]
        let mutating_argvs = [&transition[..], &ingest[..]];
        #[cfg(feature = "sandbox-write")]
        let mutating_argvs = [&ingest[..]];
        for argv in mutating_argvs {
            let mutating = call(root.path(), &swarm, argv);
            assert!(!is_read_only_route(Entrypoint::OrchStatus, &mutating.argv));
            let refusal = sandbox
                .orch_status(&mutating)
                .err()
                .ok_or("mutating argv answered")?;
            assert_eq!(refusal.code(), ErrorCode::Dep);
            assert_eq!(invoke(&sandbox, &mutating).exit_code, 2);
        }
        Ok(())
    }

    #[test]
    fn sandbox_fault_fails_over_to_host_rust_on_reads_only() -> TestResult {
        let root = TempDir::new().map_err(|e| e.to_string())?;
        let swarm = root.path().join(".swarm");
        block(seed(&swarm, 3))??;
        let broken = SandboxEngine::with_module(GUEST_MODULE.0, WRONG_PIN).map_err(message)?;
        let host = host()?;

        let read = call(root.path(), &swarm, &["--json"]);
        assert!(matches!(guest_call(&broken, &read)?, GuestCall::Faulted(_)));
        assert_eq!(broken.orch_status(&read), host.orch_status(&read));

        // Without the `sandbox-write` feature a write on the sandbox backend is refused before any write; with it the write guest
        // answers, and a faulted guest is answered by the host decision (tests/write_route_sandbox_faults.rs).
        #[cfg(not(feature = "sandbox-write"))]
        {
            let mutating = call(
                root.path(),
                &swarm,
                &["--transition", "T-0000", "DONE", "--json"],
            );
            let refusal = broken
                .orch_status(&mutating)
                .err()
                .ok_or("mutating argv answered")?;
            assert_eq!(refusal.code(), ErrorCode::Dep);
            assert!(refusal.message().contains("no route"));
        }
        Ok(())
    }

    #[test]
    fn pin_mismatch_fails_health_at_the_identity_check() -> TestResult {
        let broken = SandboxEngine::with_module(GUEST_MODULE.0, WRONG_PIN).map_err(message)?;
        match broken.health(Duration::from_secs(30)) {
            Ok(()) => Err("a pin mismatch passed health".to_string()),
            Err(error) => {
                assert_eq!(error.code(), ErrorCode::Dep);
                assert!(
                    error.message().contains("identity check"),
                    "{}",
                    error.message()
                );
                Ok(())
            }
        }
    }

    #[test]
    fn verified_guest_passes_health() -> TestResult {
        engine()?.health(Duration::from_secs(30)).map_err(message)
    }

    #[test]
    fn a_guest_cannot_claim_the_deadline_in_an_error_of_its_own() {
        let guest = |code| GuestInvokeError::Guest {
            code,
            message: "from the guest".to_string(),
        };
        let cases = [
            (
                GuestInvokeError::Trap(ErrorCode::Timeout),
                ErrorCode::Timeout,
            ),
            (
                GuestInvokeError::Trap(ErrorCode::Capacity),
                ErrorCode::Capacity,
            ),
            (
                GuestInvokeError::Trap(ErrorCode::Internal),
                ErrorCode::Internal,
            ),
            (guest(ErrorCode::Timeout), ErrorCode::Internal),
            (guest(ErrorCode::Input), ErrorCode::Input),
            (guest(ErrorCode::Dep), ErrorCode::Dep),
        ];
        for (error, wanted) in cases {
            assert_eq!(fault_of(error.clone()), wanted, "{error:?}");
        }
    }

    #[test]
    fn a_pin_mismatch_is_a_dep_fault_of_the_write_guest_before_it_runs() -> TestResult {
        let broken = SandboxEngine::with_module(GUEST_MODULE.0, WRONG_PIN).map_err(message)?;
        assert_eq!(
            WriteGuest::call(&broken, b"{}", &limits(Duration::from_secs(30))),
            Err(ErrorCode::Dep)
        );
        Ok(())
    }

    /// The engine prepares the write guest with the identity check and the compile: a pin mismatch is `E-DEP` before anything runs.
    #[test]
    fn a_pin_mismatch_is_a_dep_failure_to_prepare_the_write_guest() -> TestResult {
        let broken = SandboxEngine::with_module(GUEST_MODULE.0, WRONG_PIN).map_err(message)?;
        assert_eq!(WriteGuest::prepare(&broken), Err(ErrorCode::Dep));
        Ok(())
    }

    /// A write on an engine whose module fails its pin is answered by the host decision, here the host's own refusal of an illegal
    /// transition (T-0000 is DONE), byte for byte as the host backend answers on a twin store, with the pin failure recorded.
    #[test]
    fn a_pin_mismatch_on_a_write_is_answered_by_the_host_and_recorded() -> TestResult {
        let argv = ["--transition", "T-0000", "CREATED", "--json"];
        let (sandbox_root, host_root) = (
            TempDir::new().map_err(|e| e.to_string())?,
            TempDir::new().map_err(|e| e.to_string())?,
        );
        let mut answers = Vec::new();
        for root in [&sandbox_root, &host_root] {
            let swarm = root.path().join(".swarm");
            block(seed(&swarm, 1))??;
            let mut write = call(root.path(), &swarm, &argv);
            write.effects_path = Some(root.path().join("effects.json"));
            answers.push(write);
        }
        let broken = SandboxEngine::with_module(GUEST_MODULE.0, WRONG_PIN).map_err(message)?;
        let args = parse_transition_args(&answers[0].argv).ok_or("not a transition argv")?;
        let sandbox = broken.transition_via(&broken, limits, &args, &answers[0]);
        let host = host()?.orch_status(&answers[1]);
        assert_eq!(sandbox, host);
        let refusal = sandbox.err().ok_or("an illegal transition was answered")?;
        assert_eq!(refusal.code(), ErrorCode::Contract);
        assert_eq!(broken.failover(), Some(ErrorCode::Dep));
        Ok(())
    }

    /// The guest's deadline is the call deadline less a reserve for apply and commit: a tenth of it (whole nanoseconds, rounded down),
    /// at most two seconds. Checked at zero, below ten nanoseconds, and each side of the point where the cap starts.
    #[test]
    fn the_reserve_is_a_tenth_of_the_deadline_and_at_most_two_seconds() {
        struct Idle;
        impl WriteGuest for Idle {
            fn call(&self, _: &[u8], _: &Limits) -> Result<Vec<u8>, ErrorCode> {
                Err(ErrorCode::Internal)
            }
        }
        let nanos = Duration::from_nanos;
        let millis = Duration::from_millis;
        let secs = Duration::from_secs;
        let cases = [
            (Duration::ZERO, Duration::ZERO),
            (nanos(9), Duration::ZERO),
            (nanos(10), nanos(1)),
            (millis(100), millis(10)),
            (secs(1), millis(100)),
            (millis(19_990), millis(1_999)),
            (secs(20), secs(2)),
            (secs(20) + nanos(10), secs(2)),
            (secs(600), secs(2)),
        ];
        for (deadline, reserve) in cases {
            let decider = SandboxDecider::new(&Idle, Instant::now(), deadline);
            assert_eq!(
                deadline - decider.guest_deadline,
                reserve,
                "the reserve of {deadline:?}"
            );
        }
    }

    #[test]
    fn other_entrypoints_refuse_with_dep() -> TestResult {
        let root = TempDir::new().map_err(|e| e.to_string())?;
        let swarm = root.path().join(".swarm");
        let sandbox = engine()?;
        let c = call(root.path(), &swarm, &["--json"]);
        let refusals = [
            sandbox.swarm_run(&c),
            sandbox.orch_plan(&c),
            sandbox.orch_assign(&c),
            sandbox.qa_gate(&c),
            sandbox.rev_gate(&c),
            sandbox.sec_gate(&c),
            sandbox.rel_plan(&c),
            sandbox.swarm_tool(&c),
            sandbox.lane_verb(&c),
        ];
        for refusal in refusals {
            assert_eq!(refusal.err().map(|e| e.code()), Some(ErrorCode::Dep));
        }
        Ok(())
    }

    #[test]
    fn faults_keep_their_code_and_message() {
        let codes = [
            ErrorCode::Input,
            ErrorCode::Timeout,
            ErrorCode::Dep,
            ErrorCode::Capacity,
            ErrorCode::Contract,
            ErrorCode::Policy,
            ErrorCode::Internal,
        ];
        for code in codes {
            let seam = into_seam(fault(code, "probe"));
            assert_eq!(seam.code(), code);
            assert_eq!(seam.message(), format!("{}: probe", code.as_str()));
            assert_eq!(
                is_timeout(&fault(code, "probe")),
                code == ErrorCode::Timeout
            );
        }
    }

    #[test]
    fn a_spent_budget_is_e_timeout() -> TestResult {
        let total = Duration::from_secs(30);
        let left = budget_after(total, Duration::from_secs(10)).map_err(message)?;
        assert_eq!(left, Duration::from_secs(20));
        for elapsed in [total, total + Duration::from_millis(1)] {
            let error = budget_after(total, elapsed)
                .err()
                .ok_or("a spent budget was granted")?;
            assert_eq!(error.code(), ErrorCode::Timeout);
        }
        Ok(())
    }

    /// Every wire code, so a new variant is not forgotten by the notice tests.
    const ALL_CODES: [ErrorCode; 7] = [
        ErrorCode::Input,
        ErrorCode::Timeout,
        ErrorCode::Dep,
        ErrorCode::Capacity,
        ErrorCode::Contract,
        ErrorCode::Policy,
        ErrorCode::Internal,
    ];

    /// The code of a line that the harness's `FAILOVER_NOTICE` accepts, else None. The pattern
    /// `^gsd_engine: sandbox fault (E-[A-Z]+); answered by host Rust$` (`tests/parity/harness.py`) is
    /// spelled out by hand here, not imported, so a change to either side fails this test.
    fn harness_notice_code(line: &str) -> Option<&str> {
        let rest = line.strip_prefix("gsd_engine: sandbox fault ")?;
        let code = rest.strip_suffix("; answered by host Rust")?;
        let letters = code.strip_prefix("E-")?;
        let upper = !letters.is_empty() && letters.bytes().all(|b| b.is_ascii_uppercase());
        upper.then_some(code)
    }

    #[test]
    fn failover_notice_text_is_fixed_for_every_code() {
        for code in ALL_CODES {
            let line = failover_notice(code);
            assert_eq!(
                line,
                format!(
                    "gsd_engine: sandbox fault {}; answered by host Rust",
                    code.as_str()
                )
            );
            assert_eq!(harness_notice_code(&line), Some(code.as_str()));
            assert!(!line.contains('\n'), "the notice is one line");
        }
        assert_eq!(
            failover_notice(ErrorCode::Dep),
            "gsd_engine: sandbox fault E-DEP; answered by host Rust"
        );
        assert_eq!(
            failover_notice(ErrorCode::Internal),
            "gsd_engine: sandbox fault E-INTERNAL; answered by host Rust"
        );
        assert_eq!(harness_notice_code("gsd_engine: sandbox fault E-DEP"), None);
        assert_eq!(
            harness_notice_code("gsd_engine: sandbox fault e-dep; answered by host Rust"),
            None
        );
    }

    #[test]
    fn failover_code_is_the_fault_code_and_none_for_final_outcomes() -> TestResult {
        for code in ALL_CODES {
            let expected = (code != ErrorCode::Timeout).then_some(code);
            assert_eq!(
                failover_code(&GuestCall::Faulted(fault(code, "reason"))),
                expected,
                "{code:?}"
            );
            let guest_error = PoolError::Sandbox {
                code,
                message: "guest said so".to_string(),
            };
            assert_eq!(failover_code(&GuestCall::Answered(Err(guest_error))), None);
        }
        let reply = decode_reply_wire(br#"{"exit_code": 0, "stdout": ""}"#).map_err(message)?;
        assert_eq!(failover_code(&GuestCall::Answered(Ok(reply))), None);
        // A fault that is not a sandbox error is answered by host Rust and reported as internal.
        let other = GuestCall::Faulted(PoolError::Other("x".to_string()));
        assert_eq!(failover_code(&other), Some(ErrorCode::Internal));
        Ok(())
    }

    #[test]
    fn a_failover_is_recorded_with_its_code_and_the_answer_is_the_host_answer() -> TestResult {
        let root = TempDir::new().map_err(|e| e.to_string())?;
        let swarm = root.path().join(".swarm");
        block(seed(&swarm, 3))??;
        let broken = SandboxEngine::with_module(GUEST_MODULE.0, WRONG_PIN).map_err(message)?;
        let fault_code = match broken.loaded_guest() {
            Err(error) => error.error_code(),
            Ok(_) => return Err("the pin mismatch did not fault the guest".to_string()),
        };
        let read = call(root.path(), &swarm, &["--json"]);
        assert_eq!(broken.failover(), None, "nothing is recorded before a read");

        let answer = broken.orch_status(&read).map_err(message)?;
        let host_answer = host()?.orch_status(&read).map_err(message)?;
        // The notice goes to stderr only: what the caller reads is exactly the host backend's reply.
        assert_eq!(answer.stdout.as_bytes(), host_answer.stdout.as_bytes());
        assert_eq!(answer.exit_code, host_answer.exit_code);

        assert_eq!(broken.failover(), Some(fault_code));
        let line = failover_notice(fault_code);
        assert_eq!(harness_notice_code(&line), Some(fault_code.as_str()));
        assert!(!line.contains(&root.path().display().to_string()));
        Ok(())
    }

    #[test]
    fn a_guest_answer_records_no_failover() -> TestResult {
        let root = TempDir::new().map_err(|e| e.to_string())?;
        let swarm = root.path().join(".swarm");
        block(seed(&swarm, 3))??;
        let sandbox = engine()?;
        let list = call(root.path(), &swarm, &["--json"]);
        assert!(matches!(
            guest_call(&sandbox, &list)?,
            GuestCall::Answered(_)
        ));
        sandbox.orch_status(&list).map_err(message)?;
        assert_eq!(sandbox.failover(), None);

        // An error the guest reports is its answer too, and is not failed over.
        let unknown = call(root.path(), &swarm, &["--history", "T-9999", "--json"]);
        let _ = sandbox.orch_status(&unknown);
        assert_eq!(sandbox.failover(), None);
        Ok(())
    }

    #[test]
    fn a_later_refused_call_clears_the_failover_record() -> TestResult {
        let root = TempDir::new().map_err(|e| e.to_string())?;
        let swarm = root.path().join(".swarm");
        block(seed(&swarm, 1))??;
        let broken = SandboxEngine::with_module(GUEST_MODULE.0, WRONG_PIN).map_err(message)?;
        broken
            .orch_status(&call(root.path(), &swarm, &["--json"]))
            .map_err(message)?;
        assert!(broken.failover().is_some());
        let refused = call(
            root.path(),
            &swarm,
            &["--transition", "T-0000", "DONE", "--json"],
        );
        assert!(broken.orch_status(&refused).is_err());
        assert_eq!(broken.failover(), None);
        Ok(())
    }
}

#[cfg(test)]
mod read_route_faults {
    use super::*;
    use crate::strangler_seam::{open_store, RustEngine};
    use std::fs;
    use std::future::Future;
    use std::path::Path;
    use tempfile::TempDir;

    type TestResult = Result<(), String>;

    /// The deadline of the test calls.
    const CALL_DEADLINE: Duration = Duration::from_secs(30);
    /// The cap that the A10 condition 4 measurement refused for the 500-task request.
    const FOUR_MIB: usize = 4 * 1024 * 1024;
    /// A pin that no built guest matches.
    const WRONG_PIN: &str = "0000000000000000000000000000000000000000000000000000000000000000";

    fn message(error: SeamError) -> String {
        error.message().to_string()
    }

    fn block<F: Future>(future: F) -> Result<F::Output, String> {
        let runtime = single_thread_runtime().map_err(message)?;
        Ok(runtime.block_on(future))
    }

    fn engine() -> Result<SandboxEngine, String> {
        SandboxEngine::new().map_err(message)
    }

    fn call(root: &Path, swarm: &Path) -> Invocation {
        Invocation {
            agent: "A01".to_string(),
            script: "orch_status".to_string(),
            argv: vec!["--json".to_string()],
            root: root.to_path_buf(),
            env: vec![("SWARM_DIR".to_string(), swarm.display().to_string())],
            deadline: CALL_DEADLINE,
            effects_path: None,
        }
    }

    /// Writes the 500-task store of the A10 measurement: even tasks are DONE with a passing review,
    /// each task depends on the one before it, and `T-0000` has two transitions.
    async fn seed_500(swarm: &Path) -> Result<(), String> {
        let pool = open_store(swarm).await.map_err(message)?;
        for index in 0..500usize {
            let id = format!("T-{index:04}");
            let done = index % 2 == 0;
            let state = if done { "DONE" } else { "PLANNED" };
            let deps = if index == 0 {
                "[]".to_string()
            } else {
                format!("[\"T-{:04}\"]", index - 1)
            };
            sqlx::query(
                "INSERT INTO tasks (task_id, correlation_id, dag_depth, capability, agent_id, title, risk_class, state, depends_on, notes, created_at, updated_at) VALUES (?, 'C1', ?, 'cap', 'A05', 'title', 'low', ?, ?, '{}', ?, ?)",
            )
            .bind(id.as_str())
            .bind((index % 7) as i64)
            .bind(state)
            .bind(deps)
            .bind(index as f64)
            .bind(index as f64)
            .execute(&pool)
            .await
            .map_err(|e| e.to_string())?;
            if done {
                sqlx::query(
                    "INSERT INTO verdicts (task_id, gate, verdict, agent_id, findings, expires_at, ts) VALUES (?, 'review', 'pass', 'A09', '[]', 9.0, ?)",
                )
                .bind(id.as_str())
                .bind(index as f64)
                .execute(&pool)
                .await
                .map_err(|e| e.to_string())?;
            }
        }
        sqlx::query(
            "INSERT INTO transitions (task_id, from_state, to_state, actor, reason, ts) VALUES ('T-0000', NULL, 'CREATED', 'system', 'created', 1.0), ('T-0000', 'CREATED', 'DONE', 'A05', 'done', 2.0)",
        )
        .execute(&pool)
        .await
        .map_err(|e| e.to_string())?;
        pool.close().await;
        fs::write(
            swarm.join("events.jsonl"),
            "{\"type\":\"a\",\"correlation_id\":\"C1\"}\n",
        )
        .map_err(|e| e.to_string())
    }

    /// The wire snapshot of a read, exactly as the sandbox path encodes it.
    fn snapshot(sandbox: &SandboxEngine, call: &Invocation) -> Result<Vec<u8>, String> {
        let args = read_args(call).map_err(message)?;
        let request = sandbox
            .runtime
            .block_on(read_snapshot(&args, call))
            .map_err(message)?;
        Ok(to_python_json(&request).into_bytes())
    }

    /// A 500-task store under `root`, its engine, the list call and that call's wire snapshot.
    fn seeded(root: &TempDir) -> Result<(SandboxEngine, Invocation, Vec<u8>), String> {
        let swarm = root.path().join(".swarm");
        block(seed_500(&swarm))??;
        let sandbox = engine()?;
        let list = call(root.path(), &swarm);
        let wire = snapshot(&sandbox, &list)?;
        Ok((sandbox, list, wire))
    }

    /// Limits with the given fuel, memory cap and optional call deadline.
    fn custom_limits(fuel: u64, memory_bytes: usize, deadline: Option<Duration>) -> Limits {
        Limits {
            fuel,
            memory_bytes,
            deadline_ticks: deadline.map(deadline_ticks_for),
        }
    }

    /// One instantiate and invoke of the verified guest under `limits`. This is what
    /// `SandboxEngine::run` does, with the limits given, because the production path fixes them.
    fn invoke_under(
        sandbox: &SandboxEngine,
        limits: &Limits,
        wire: &[u8],
    ) -> Result<Result<Vec<u8>, GuestInvokeError>, String> {
        let loaded = sandbox.loaded_guest().map_err(|error| error.to_string())?;
        let mut instance = sandbox
            .loader
            .instantiate(loaded, limits)
            .map_err(|error| error.to_string())?;
        Ok(instance.invoke(wire))
    }

    #[test]
    fn fuel_of_one_million_is_internal_without_a_deadline() -> TestResult {
        let root = TempDir::new().map_err(|e| e.to_string())?;
        let (sandbox, _, wire) = seeded(&root)?;
        let starved = custom_limits(1_000_000, MEMORY_BYTES, None);
        assert_eq!(
            invoke_under(&sandbox, &starved, &wire)?.err(),
            Some(GuestInvokeError::Trap(ErrorCode::Internal))
        );
        let fed = custom_limits(FUEL, MEMORY_BYTES, None);
        assert!(
            invoke_under(&sandbox, &fed, &wire)?.is_ok(),
            "the request must answer under the engine's fuel"
        );
        Ok(())
    }

    #[test]
    fn fuel_of_one_million_is_timeout_with_a_deadline() -> TestResult {
        let root = TempDir::new().map_err(|e| e.to_string())?;
        let (sandbox, _, wire) = seeded(&root)?;
        let starved = custom_limits(1_000_000, MEMORY_BYTES, Some(CALL_DEADLINE));
        assert_eq!(
            invoke_under(&sandbox, &starved, &wire)?.err(),
            Some(GuestInvokeError::Trap(ErrorCode::Timeout))
        );
        Ok(())
    }

    #[test]
    fn four_mib_cap_refuses_the_500_task_request_as_capacity() -> TestResult {
        let root = TempDir::new().map_err(|e| e.to_string())?;
        let (sandbox, _, wire) = seeded(&root)?;
        let capped = custom_limits(FUEL, FOUR_MIB, Some(CALL_DEADLINE));
        assert_eq!(
            invoke_under(&sandbox, &capped, &wire)?.err(),
            Some(GuestInvokeError::Trap(ErrorCode::Capacity))
        );
        let roomy = custom_limits(FUEL, MEMORY_BYTES, Some(CALL_DEADLINE));
        assert!(
            invoke_under(&sandbox, &roomy, &wire)?.is_ok(),
            "the request must answer under the engine's memory cap"
        );
        Ok(())
    }

    /// The seam's answer to the list call under the limits `make_limits` gives the guest.
    fn seam_answer(
        sandbox: &SandboxEngine,
        call: &Invocation,
        make_limits: impl Fn(Duration) -> Limits,
    ) -> Result<Result<Reply, SeamError>, String> {
        let args = read_args(call).map_err(message)?;
        block(sandbox.guest_or_failover_with(&args, call, make_limits))
    }

    #[test]
    fn fuel_with_a_deadline_is_e_timeout_and_does_not_fail_over() -> TestResult {
        let root = TempDir::new().map_err(|e| e.to_string())?;
        let (sandbox, list, _) = seeded(&root)?;
        let starved = |budget: Duration| custom_limits(1_000_000, MEMORY_BYTES, Some(budget));
        let error = seam_answer(&sandbox, &list, starved)?
            .err()
            .ok_or("a starved guest with a deadline was answered")?;
        assert_eq!(error.code(), ErrorCode::Timeout);
        Ok(())
    }

    #[test]
    fn fuel_without_a_deadline_fails_over_to_the_host_answer() -> TestResult {
        let root = TempDir::new().map_err(|e| e.to_string())?;
        let (sandbox, list, wire) = seeded(&root)?;
        let starved = |_: Duration| custom_limits(1_000_000, MEMORY_BYTES, None);
        // The guest faults under these limits, so the answer below can only be the host's.
        assert_eq!(
            invoke_under(&sandbox, &starved(CALL_DEADLINE), &wire)?.err(),
            Some(GuestInvokeError::Trap(ErrorCode::Internal))
        );
        let host_answer = RustEngine::new().map_err(message)?.orch_status(&list);
        assert_eq!(seam_answer(&sandbox, &list, starved)?, host_answer);
        Ok(())
    }

    #[test]
    fn a_pin_mismatch_fails_over_to_the_host_answer() -> TestResult {
        let root = TempDir::new().map_err(|e| e.to_string())?;
        let (_, list, _) = seeded(&root)?;
        let broken = SandboxEngine::with_module(GUEST_MODULE.0, WRONG_PIN).map_err(message)?;
        assert!(
            broken.loaded_guest().is_err(),
            "the pin mismatch must fault the guest"
        );
        let host_answer = RustEngine::new().map_err(message)?.orch_status(&list);
        assert_eq!(seam_answer(&broken, &list, limits)?, host_answer);
        Ok(())
    }

    #[test]
    fn a_capacity_fault_on_a_read_fails_over_to_the_host_answer() -> TestResult {
        let root = TempDir::new().map_err(|e| e.to_string())?;
        let (sandbox, list, wire) = seeded(&root)?;
        let capped = |budget: Duration| custom_limits(FUEL, FOUR_MIB, Some(budget));
        assert_eq!(
            invoke_under(&sandbox, &capped(CALL_DEADLINE), &wire)?.err(),
            Some(GuestInvokeError::Trap(ErrorCode::Capacity))
        );
        let host_answer = RustEngine::new().map_err(message)?.orch_status(&list);
        assert_eq!(seam_answer(&sandbox, &list, capped)?, host_answer);
        Ok(())
    }

    #[test]
    fn a_guest_invoke_error_is_the_answer_and_not_a_host_answer() -> TestResult {
        let root = TempDir::new().map_err(|e| e.to_string())?;
        let (sandbox, list, _) = seeded(&root)?;
        let args = read_args(&list).map_err(message)?;
        let request = block(read_snapshot(&args, &list))?.map_err(message)?;
        assert!(
            orch_status_reply(&request).is_ok(),
            "the host answers this snapshot"
        );
        let error = match sandbox.answer(b"{", &limits(CALL_DEADLINE)) {
            GuestCall::Answered(Err(error)) => error,
            _ => return Err("the guest did not answer malformed input".to_string()),
        };
        // The host answers this snapshot, so the guest's E-INPUT survives only without failover.
        let answer = settle(GuestCall::Answered(Err(error)), &request);
        assert_eq!(answer.err().map(|e| e.code()), Some(ErrorCode::Input));
        Ok(())
    }

    #[test]
    fn malformed_input_is_the_guest_input_answer_and_is_not_retried() -> TestResult {
        let sandbox = engine()?;
        for wire in [&b"{"[..], &b"\xff\xfe"[..]] {
            match sandbox.answer(wire, &limits(CALL_DEADLINE)) {
                GuestCall::Answered(Err(error)) => {
                    assert_eq!(into_seam(error).code(), ErrorCode::Input);
                }
                _ => return Err("malformed input was not answered by the guest".to_string()),
            }
        }
        Ok(())
    }

    #[test]
    fn truncated_reply_bytes_map_to_contract() -> TestResult {
        let control = decode_reply_wire(br#"{"exit_code": 0, "stdout": ""}"#);
        assert!(control.is_ok(), "a whole reply must decode");
        let wires: [&[u8]; 4] = [
            b"",
            br#"{"exit_code": 0, "stdout""#,
            br#"{"exit_code": 0}"#,
            br#"{"exit_code": "0", "stdout": ""}"#,
        ];
        for wire in wires {
            let error = decode_reply_wire(wire)
                .err()
                .ok_or("a truncated reply decoded")?;
            assert_eq!(into_seam(sandbox_answer(error)).code(), ErrorCode::Contract);
        }
        Ok(())
    }

    /// The host backend's answer to the list call.
    fn host_reply(list: &Invocation) -> Result<Reply, String> {
        RustEngine::new()
            .map_err(message)?
            .orch_status(list)
            .map_err(message)
    }

    #[test]
    fn a_timeout_records_no_failover() -> TestResult {
        let root = TempDir::new().map_err(|e| e.to_string())?;
        let (sandbox, list, _) = seeded(&root)?;
        let starved = |budget: Duration| custom_limits(1_000_000, MEMORY_BYTES, Some(budget));
        let error = seam_answer(&sandbox, &list, starved)?
            .err()
            .ok_or("a starved guest with a deadline was answered")?;
        assert_eq!(error.code(), ErrorCode::Timeout);
        assert_eq!(sandbox.failover(), None);
        Ok(())
    }

    #[test]
    fn a_fault_records_its_code_and_leaves_the_host_stdout_unchanged() -> TestResult {
        let root = TempDir::new().map_err(|e| e.to_string())?;
        let (sandbox, list, _) = seeded(&root)?;
        let host_answer = host_reply(&list)?;

        let starved = |_: Duration| custom_limits(1_000_000, MEMORY_BYTES, None);
        let answer = seam_answer(&sandbox, &list, starved)?.map_err(message)?;
        assert_eq!(answer.stdout.as_bytes(), host_answer.stdout.as_bytes());
        assert_eq!(answer.exit_code, host_answer.exit_code);
        assert_eq!(sandbox.failover(), Some(ErrorCode::Internal));
        assert_eq!(
            failover_notice(ErrorCode::Internal),
            "gsd_engine: sandbox fault E-INTERNAL; answered by host Rust"
        );

        let capped = |budget: Duration| custom_limits(FUEL, FOUR_MIB, Some(budget));
        let answer = seam_answer(&sandbox, &list, capped)?.map_err(message)?;
        assert_eq!(answer.stdout.as_bytes(), host_answer.stdout.as_bytes());
        assert_eq!(sandbox.failover(), Some(ErrorCode::Capacity));
        assert_eq!(
            failover_notice(ErrorCode::Capacity),
            "gsd_engine: sandbox fault E-CAPACITY; answered by host Rust"
        );
        Ok(())
    }

    #[test]
    fn a_guest_answer_under_the_production_limits_records_no_failover() -> TestResult {
        let root = TempDir::new().map_err(|e| e.to_string())?;
        let (sandbox, list, _) = seeded(&root)?;
        let answer = seam_answer(&sandbox, &list, limits)?.map_err(message)?;
        assert_eq!(
            answer.stdout.as_bytes(),
            host_reply(&list)?.stdout.as_bytes()
        );
        assert_eq!(sandbox.failover(), None);
        Ok(())
    }
}
