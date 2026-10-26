//! The typed guest boundary: the `gsd:core@0.1.0` world as Rust types, and one `invoke` call.
//!
//! The bindings are generated from `crates/gsd-guest/wit`. An `invoke-error` the guest returns is
//! a [`GuestInvokeError::Guest`] carrying the guest's own code and message. A call that does not
//! complete (a trap, fuel or deadline exhaustion, a refused memory grow) is a
//! [`GuestInvokeError::Trap`] classified by `wasmtime_limits::classify`; the trap text is dropped,
//! so no runtime detail reaches the caller.

use gsd_failure::ErrorCode;

use crate::wasmtime_limits::{arm_call_budget, classify};
use crate::wasmtime_module_load::GuestInstance;

/// Rust types and the typed `invoke` call generated from the `gsd-core` world.
pub mod bindings {
    wasmtime::component::bindgen!({
        path: "../gsd-guest/wit",
        world: "gsd-core",
    });
}

/// A failed guest invoke.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GuestInvokeError {
    /// The guest returned an `invoke-error`: its taxonomy code and its message.
    Guest {
        /// The taxonomy code the guest reported.
        code: ErrorCode,
        /// The guest's message; it carries no secret.
        message: String,
    },
    /// The call did not complete; the code is classified from the runtime error.
    Trap(ErrorCode),
}

impl GuestInvokeError {
    /// The taxonomy code of this failure, whichever variant carries it.
    pub fn error_code(&self) -> ErrorCode {
        match self {
            Self::Guest { code, .. } => *code,
            Self::Trap(code) => *code,
        }
    }
}

/// Maps the WIT `error-code` enum to the taxonomy code; every variant has exactly one arm.
fn host_code(code: bindings::ErrorCode) -> ErrorCode {
    match code {
        bindings::ErrorCode::Input => ErrorCode::Input,
        bindings::ErrorCode::Timeout => ErrorCode::Timeout,
        bindings::ErrorCode::Dep => ErrorCode::Dep,
        bindings::ErrorCode::Capacity => ErrorCode::Capacity,
        bindings::ErrorCode::Contract => ErrorCode::Contract,
        bindings::ErrorCode::Policy => ErrorCode::Policy,
        bindings::ErrorCode::Internal => ErrorCode::Internal,
    }
}

/// Classifies a runtime error from the guest store into a [`GuestInvokeError::Trap`].
fn trap(error: &wasmtime::Error, deadline_configured: bool) -> GuestInvokeError {
    GuestInvokeError::Trap(classify(error, deadline_configured))
}

impl GuestInstance {
    /// Calls the guest's `invoke` export with `input` (UTF-8 request bytes) and returns the reply
    /// bytes the guest wrote.
    ///
    /// The export is looked up by its WIT name on every call; a missing or mistyped export is a
    /// trap classified as `E-INTERNAL`.
    ///
    /// An instance is single-use after a trap. A trap poisons the store, so a later call on the
    /// same instance fails with `CannotEnterComponent` before it reaches the guest, and that
    /// failure is classified as `E-INTERNAL`. Build a new instance for each call that may trap.
    pub fn invoke(&mut self, input: &[u8]) -> Result<Vec<u8>, GuestInvokeError> {
        let deadline_configured = self.deadline_configured();
        let limits = self.limits();
        let (store, instance) = self.parts();
        arm_call_budget(store, &limits).map_err(|error| trap(&error, deadline_configured))?;
        let guest = bindings::GsdCore::new(&mut *store, &instance)
            .map_err(|error| trap(&error, deadline_configured))?;
        match guest.call_invoke(&mut *store, input) {
            Ok(Ok(reply)) => Ok(reply),
            Ok(Err(failure)) => Err(GuestInvokeError::Guest {
                code: host_code(failure.code),
                message: failure.message,
            }),
            Err(error) => Err(trap(&error, deadline_configured)),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::wasmtime_limits::Limits;
    use crate::wasmtime_module_build::{describe, MODULE_NAME};
    use crate::wasmtime_module_load::GuestLoader;
    use wasmtime::component::types::ComponentItem;

    const GUEST: &[u8] = include_bytes!(env!("GSD_GUEST_WASM"));
    const SEMVER: &str = "0.1.0";

    const LIMITS: Limits = Limits {
        fuel: 1_000_000_000,
        memory_bytes: 16 * 1024 * 1024,
        deadline_ticks: None,
    };

    /// A list request: two tasks, the second with one verdict row (review, unsigned).
    const LIST_REQUEST: &str = r#"{"mode":"list","agent":"A01","script":"orch_status","correlation":null,"db_path":"/x/tasks.db","swarm_dir":"/x","events":3,"tasks":[{"task_id":"T-A","state":"DONE","agent_id":"A05","dag_depth":0,"attempt":0,"rework_loops":0,"risk_class":"low","depends_on":"[]","notes":"{}"},{"task_id":"T-B","state":"IN_REVIEW","agent_id":"A05","dag_depth":1,"attempt":0,"rework_loops":0,"risk_class":"low","depends_on":"[\"T-A\"]","notes":"{}"}],"verdicts":[{"task_id":"T-B","rows":[{"id":1,"task_id":"T-B","gate":"review","verdict":"pass","findings":"[]","ts":1.5,"envelope_json":null}]}],"history":null}"#;

    /// A history request for a task that is absent from the Task Store.
    const HISTORY_UNKNOWN: &str = r#"{"mode":"history","agent":"A01","script":"orch_status","correlation":null,"db_path":"/x/tasks.db","swarm_dir":"/x","events":0,"tasks":[],"verdicts":[],"history":{"task_id":"T-X","task":null,"transitions":[]}}"#;

    /// A list request whose correlation id breaks the id rule.
    const TRAVERSAL_CORRELATION: &str = r#"{"mode":"list","agent":"A01","script":"orch_status","correlation":"../x","db_path":"/x/tasks.db","swarm_dir":"/x","events":0,"tasks":[],"verdicts":[],"history":null}"#;

    fn start() -> GuestInstance {
        let loader = GuestLoader::new().expect("loader builds");
        let manifest = describe(GUEST, MODULE_NAME, SEMVER);
        let guest = loader
            .load_verified(GUEST, &manifest, SEMVER)
            .expect("the built guest verifies and compiles");
        loader
            .instantiate(&guest, &LIMITS)
            .expect("the guest instantiates with no imports")
    }

    #[test]
    fn the_component_imports_nothing_and_exports_only_invoke() {
        let loader = GuestLoader::new().expect("loader builds");
        let manifest = describe(GUEST, MODULE_NAME, SEMVER);
        let guest = loader
            .load_verified(GUEST, &manifest, SEMVER)
            .expect("the built guest verifies and compiles");
        let component_type = guest.component().component_type();
        assert_eq!(component_type.imports(loader.engine()).count(), 0);
        let exports: Vec<(&str, bool)> = component_type
            .exports(loader.engine())
            .map(|(name, item)| (name, matches!(item.ty, ComponentItem::ComponentFunc(_))))
            .collect();
        assert_eq!(exports, vec![("invoke", true)]);
    }

    #[test]
    fn a_list_request_returns_the_bytes_the_native_route_returns() {
        let mut instance = start();
        let guest_reply = instance
            .invoke(LIST_REQUEST.as_bytes())
            .expect("the list request invokes the guest");
        let native = gsd_route::orch_status_invoke(LIST_REQUEST.as_bytes())
            .expect("the native route answers the list request");
        assert_eq!(guest_reply, native);
        let reply = gsd_route::decode_reply_wire(&guest_reply).expect("the reply decodes");
        assert_eq!(reply.exit_code, 0);
        assert!(reply.stdout.contains("\"T-A\""), "{}", reply.stdout);
        assert!(reply.stdout.contains("\"T-B\""), "{}", reply.stdout);
    }

    #[test]
    fn an_unknown_history_task_is_an_invoke_error_not_a_trap() {
        let mut instance = start();
        let native = gsd_route::orch_status_invoke(HISTORY_UNKNOWN.as_bytes())
            .err()
            .expect("the native route refuses an unknown task");
        match instance.invoke(HISTORY_UNKNOWN.as_bytes()) {
            Err(GuestInvokeError::Guest { code, message }) => {
                assert_eq!(code, ErrorCode::Input);
                assert_eq!(message, native.message());
                assert!(
                    message.ends_with("unknown task for history.task_id"),
                    "{message}"
                );
                assert!(!message.contains("T-X"), "{message}");
            }
            Err(GuestInvokeError::Trap(code)) => {
                panic!("an unknown task must be an invoke error, got a trap ({code})")
            }
            Ok(reply) => panic!("an unknown task must be refused, got {} bytes", reply.len()),
        }
    }

    #[test]
    fn malformed_input_is_an_input_invoke_error() {
        let mut instance = start();
        match instance.invoke(b"{") {
            Err(GuestInvokeError::Guest { code, .. }) => assert_eq!(code, ErrorCode::Input),
            other => panic!("malformed JSON must be an input invoke error, got {other:?}"),
        }
    }

    #[test]
    fn a_traversal_correlation_is_an_input_invoke_error() {
        let mut instance = start();
        match instance.invoke(TRAVERSAL_CORRELATION.as_bytes()) {
            Err(GuestInvokeError::Guest { code, .. }) => assert_eq!(code, ErrorCode::Input),
            other => panic!("a `..` correlation must be an input invoke error, got {other:?}"),
        }
    }

    #[test]
    fn every_call_gets_the_full_fuel_budget() {
        let loader = GuestLoader::new().expect("loader builds");
        let manifest = describe(GUEST, MODULE_NAME, SEMVER);
        let guest = loader
            .load_verified(GUEST, &manifest, SEMVER)
            .expect("the built guest verifies and compiles");
        let generous = Limits {
            fuel: 10_000_000_000,
            memory_bytes: 16 * 1024 * 1024,
            deadline_ticks: None,
        };
        let mut probe = loader.instantiate(&guest, &generous).expect("instantiates");
        probe
            .invoke(LIST_REQUEST.as_bytes())
            .expect("the probe call answers");
        let (store, _) = probe.parts();
        let per_call = 10_000_000_000u64 - store.get_fuel().expect("fuel is readable");
        let tight = Limits {
            fuel: per_call * 3 / 2,
            memory_bytes: 16 * 1024 * 1024,
            deadline_ticks: None,
        };
        let mut instance = loader
            .instantiate(&guest, &tight)
            .expect("instantiates under a tight budget");
        for call in 0..5 {
            instance
                .invoke(LIST_REQUEST.as_bytes())
                .unwrap_or_else(|error| panic!("call {call} must get a fresh budget: {error:?}"));
        }
    }
}
