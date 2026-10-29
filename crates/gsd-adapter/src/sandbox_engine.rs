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

use std::cell::{Cell, OnceCell};
use std::fmt::Display;
use std::time::{Duration, Instant};

use gsd_failure::ErrorCode;
use gsd_pool::PoolError;
use gsd_route::{
    decode_reply_wire, orch_status_reply, to_python_json, Json, ReadArgs, Reply, SeamError,
};
use gsd_sandbox::wasmtime_limits::deadline_ticks_for;
use gsd_sandbox::wasmtime_module_build::{MODULE_NAME, MODULE_TARGET};
use gsd_sandbox::{
    GuestInvokeError, GuestLoader, Limits, LoadError, LoadedGuest, ModuleManifest, GUEST_MODULE,
};

use crate::route_table::is_read_only_route;
use crate::strangler_seam::{
    empty_snapshot, no_route, read_args, read_snapshot, single_thread_runtime, within_deadline,
    CoreBackend, Entrypoint, Invocation,
};

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
    loaded: OnceCell<LoadedGuest>,
    /// The code of the sandbox fault the latest read failed over on; None when it did not fail over.
    failover: Cell<Option<ErrorCode>>,
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
            loaded: OnceCell::new(),
            failover: Cell::new(None),
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

    /// The code of the sandbox fault the latest read failed over on, answered by host Rust, or None
    /// when that read was answered by the guest (its error included), timed out, or was refused.
    /// Each read resets it. It carries the wire code only, never a path or a message.
    pub fn failover(&self) -> Option<ErrorCode> {
        self.failover.get()
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
    fn loaded_guest(&self) -> Result<&LoadedGuest, PoolError> {
        if let Some(loaded) = self.loaded.get() {
            return Ok(loaded);
        }
        let loaded = self
            .load()
            .map_err(|error| fault(error.error_code(), error))?;
        Ok(self.loaded.get_or_init(|| loaded))
    }

    /// Sends `request` to the guest under `limits` and classifies what comes back.
    fn answer(&self, request: &[u8], limits: &Limits) -> GuestCall {
        match self.loaded_guest() {
            Ok(loaded) => self.run(loaded, request, limits),
            Err(error) => GuestCall::Faulted(error),
        }
    }

    /// Instantiates `loaded` under `limits` and invokes it with `request`.
    fn run(&self, loaded: &LoadedGuest, request: &[u8], limits: &Limits) -> GuestCall {
        let mut instance = match self.loader.instantiate(loaded, limits) {
            Ok(instance) => instance,
            Err(error) => return GuestCall::Faulted(fault(error.error_code(), error)),
        };
        match instance.invoke(request) {
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
        self.failover.set(None);
        let started = Instant::now();
        let request = read_snapshot(args, call).await?;
        let budget = budget_after(call.deadline, started.elapsed())?;
        let wire = to_python_json(&request).into_bytes();
        let outcome = self.answer(&wire, &make_limits(budget));
        // The guest call is synchronous: a result that lands past the deadline is not reported.
        budget_after(call.deadline, started.elapsed())?;
        self.failover.set(failover_code(&outcome));
        settle(outcome, &request)
    }
}

/// The per-call limits (OD-6): fuel, memory, and an epoch deadline from the call deadline.
fn limits(deadline: Duration) -> Limits {
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

    fn orch_plan(&self, call: &Invocation) -> Result<Reply, SeamError> {
        Err(no_route(&call.script))
    }

    fn orch_status(&self, call: &Invocation) -> Result<Reply, SeamError> {
        self.failover.set(None);
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

        let transition = ["--transition", "T-0000", "DONE", "--json"];
        let ingest = ["--ingest", "result.json", "--json"];
        for argv in [&transition[..], &ingest[..]] {
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
            Err(PoolError::Sandbox { code, .. }) => code,
            _ => return Err("the pin mismatch did not fault the guest".to_string()),
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
