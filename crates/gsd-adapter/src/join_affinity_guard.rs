//! No mixed-engine joins (G-no-mixed-joins).
//!
//! One session join (a matrix fan-out or a nexus barrier) executes all of its member tasks on one
//! engine. [`JoinAffinityGuard`] pins the engine when the join is created and checks every later
//! step against it. A step on the other engine is rejected with `E-CONTRACT`, and so is a session
//! whose steps straddle the two engines. Engine reuse across sessions is allowed; a guard covers one
//! join and is never shared between joins.

use gsd_failure::{ErrorCode, FailureError};

/// Engine a call or join step runs on, selected per invocation by `GSD_ENGINE`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Engine {
    /// The legacy Python engine. The default.
    Python,
    /// The Rust engine.
    Rust,
}

impl Engine {
    /// Returns the `GSD_ENGINE` selector value for this engine.
    pub fn as_str(&self) -> &'static str {
        match self {
            Engine::Python => "python",
            Engine::Rust => "rust",
        }
    }
}

/// Pins one session join to the engine it was created on.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct JoinAffinityGuard {
    join_id: String,
    engine: Engine,
}

impl JoinAffinityGuard {
    /// Creates the guard for `join_id`, pinning the join to `engine`.
    pub fn new(join_id: impl Into<String>, engine: Engine) -> Self {
        Self {
            join_id: join_id.into(),
            engine,
        }
    }

    /// Returns the id of the session join this guard covers.
    pub fn join_id(&self) -> &str {
        &self.join_id
    }

    /// Returns the engine the join was pinned to at creation.
    pub fn engine(&self) -> Engine {
        self.engine
    }

    /// Checks one step against the pinned engine. Returns `E-CONTRACT` when the step would run on the
    /// other engine.
    pub fn check_step(&self, step_engine: Engine) -> Result<(), FailureError> {
        if step_engine == self.engine {
            return Ok(());
        }
        Err(FailureError::new(
            ErrorCode::Contract,
            format!(
                "join {} is pinned to engine {} but a step runs on {} (G-no-mixed-joins)",
                self.join_id,
                self.engine.as_str(),
                step_engine.as_str(),
            ),
        ))
    }

    /// Checks every step of a session join. The first step on the other engine rejects the whole
    /// session with `E-CONTRACT`.
    pub fn check_steps<I>(&self, steps: I) -> Result<(), FailureError>
    where
        I: IntoIterator<Item = Engine>,
    {
        steps.into_iter().try_for_each(|step| self.check_step(step))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn code_of(result: Result<(), FailureError>) -> Option<ErrorCode> {
        result.err().map(|e| e.code())
    }

    #[test]
    fn straddling_step_is_rejected_with_e_contract() {
        let guard = JoinAffinityGuard::new("join-1", Engine::Python);
        assert_eq!(
            code_of(guard.check_step(Engine::Rust)),
            Some(ErrorCode::Contract)
        );
    }

    #[test]
    fn straddling_session_is_rejected_with_e_contract() {
        let guard = JoinAffinityGuard::new("join-2", Engine::Rust);
        let steps = [Engine::Rust, Engine::Rust, Engine::Python];
        assert_eq!(code_of(guard.check_steps(steps)), Some(ErrorCode::Contract));
    }

    #[test]
    fn same_engine_session_is_accepted() {
        let guard = JoinAffinityGuard::new("join-3", Engine::Rust);
        let steps = [Engine::Rust, Engine::Rust, Engine::Rust];
        assert!(guard.check_steps(steps).is_ok());
        assert!(guard.check_steps(std::iter::empty()).is_ok());
    }

    #[test]
    fn guard_keeps_creation_engine_and_join_id() {
        let guard = JoinAffinityGuard::new(String::from("join-4"), Engine::Python);
        assert_eq!(guard.join_id(), "join-4");
        assert_eq!(guard.engine(), Engine::Python);
        assert!(guard.check_step(Engine::Python).is_ok());
    }

    #[test]
    fn engine_selector_strings_match_gsd_engine_values() {
        assert_eq!(Engine::Python.as_str(), "python");
        assert_eq!(Engine::Rust.as_str(), "rust");
    }

    #[test]
    fn rejection_message_names_join_and_engines() {
        let guard = JoinAffinityGuard::new("join-5", Engine::Python);
        let message = guard
            .check_step(Engine::Rust)
            .err()
            .map(|e| e.message().to_owned())
            .unwrap_or_default();
        assert!(message.contains("join-5"));
        assert!(message.contains("python"));
        assert!(message.contains("rust"));
    }
}
