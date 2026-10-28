//! Typed writer errors that say whether anything was written.
//!
//! Every [`WriterError`] carries a [`WriteState`]:
//!
//! - [`WriteState::NotStarted`]: the failure happened before the first write statement of the request. The caller may let the
//!   Python body answer the request (`E-DEP` with `error.details.write_state = "not_started"`).
//! - [`WriteState::Final`]: the answer is definitive. Either the error mirrors a Python answer (`E-CONTRACT concurrent
//!   transition`), or it happened at or after the first write statement. Nothing is ever re-run after it.

use std::fmt;

use gsd_codes::ErrorCode;
use gsd_route::SeamError;

/// Whether a failed request may still be answered by the Python engine.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WriteState {
    /// Nothing was written and the request may be handed to Python.
    NotStarted,
    /// The outcome is definitive; the request must never be re-run.
    Final,
}

impl WriteState {
    /// The wire value of `error.details.write_state`.
    pub fn as_str(self) -> &'static str {
        match self {
            WriteState::NotStarted => "not_started",
            WriteState::Final => "final",
        }
    }
}

/// What went wrong, for callers that branch on the cause.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorKind {
    /// The store file is absent or could not be opened (Python creates and migrates it).
    StoreUnavailable,
    /// A required table or column is absent (Python migrates it).
    SchemaNotReady,
    /// The write lock or the connection was not available within the busy timeout.
    Busy,
    /// A stored value or a request is not one Rust can prove byte-identical to Python for.
    Unsupported,
    /// The conditional state `UPDATE` matched no row: `E-CONTRACT concurrent transition`.
    ConcurrentTransition,
    /// Any other SQLite or driver failure.
    Database,
}

/// A writer failure: cause, taxonomy code, message and whether anything was written.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WriterError {
    kind: ErrorKind,
    write_state: WriteState,
    code: ErrorCode,
    message: String,
}

impl WriterError {
    /// A refusal before the first write: `E-DEP`, [`WriteState::NotStarted`].
    pub(crate) fn not_started(kind: ErrorKind, message: impl Into<String>) -> Self {
        Self {
            kind,
            write_state: WriteState::NotStarted,
            code: ErrorCode::Dep,
            message: message.into(),
        }
    }

    /// A definitive failure: [`WriteState::Final`] with the given taxonomy code.
    pub(crate) fn final_error(
        kind: ErrorKind,
        code: ErrorCode,
        message: impl Into<String>,
    ) -> Self {
        Self {
            kind,
            write_state: WriteState::Final,
            code,
            message: message.into(),
        }
    }

    /// The cause.
    pub fn kind(&self) -> ErrorKind {
        self.kind
    }

    /// Whether the request may still be answered by Python.
    pub fn write_state(&self) -> WriteState {
        self.write_state
    }

    /// The taxonomy code (`E-DEP` for every not-started error).
    pub fn code(&self) -> ErrorCode {
        self.code
    }

    /// The message without the `CODE: ` prefix, as `SwarmError` keeps it.
    pub fn message(&self) -> &str {
        &self.message
    }

    /// The seam error with the `CODE: ` message prefix; the caller attaches `write_state` to its details.
    pub fn to_seam_error(&self) -> SeamError {
        SeamError::new(self.code, &self.message)
    }
}

impl fmt::Display for WriterError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.code.as_str(), self.message)
    }
}

impl std::error::Error for WriterError {}
