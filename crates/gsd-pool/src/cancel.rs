//! Cancellation for the chat-thread runtime (CR-03, GA-9).
//!
//! A queued message is removed from its thread's queue and its ticket completes
//! [`Completed::Cancelled`](crate::Completed::Cancelled). An in-flight message gets a cooperative
//! [`CancelToken`]: the executor checks it, and the ticket completes `Cancelled` when its worker
//! finishes, even when the executor returns a value. A shutdown that abandons the message first
//! completes the ticket with `Failed(ShuttingDown)` instead. No thread is ever killed (GA-9).
//!
//! The decision to set a token lives in `chat_runtime.rs`, under the runtime lock. This module
//! holds the token type and the public result types.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use crate::chat_runtime::MessageRef;

/// Cooperative cancellation flag shared by a worker and the executor it runs. Crate-private.
///
/// Every clone observes the same flag. Setting it is one-way: a token is never reset.
#[derive(Clone, Debug, Default)]
pub(crate) struct CancelToken(Arc<AtomicBool>);

impl CancelToken {
    /// Sets the flag. Every clone of this token then reads it as cancelled.
    pub(crate) fn cancel(&self) {
        self.0.store(true, Ordering::Release);
    }

    /// Reports whether the flag has been set.
    pub(crate) fn is_cancelled(&self) -> bool {
        self.0.load(Ordering::Acquire)
    }
}

/// How [`crate::ChatPool::cancel_message`] handled a message it found.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CancelOutcome {
    /// The message was queued. It was removed from the queue and its ticket completed
    /// `Cancelled`. The executor never ran it.
    RemovedQueued,
    /// The message was running on a worker. Its cancel token is set, and its ticket completes
    /// `Cancelled` when the worker finishes, unless a shutdown abandons it first. A value the
    /// executor returns is discarded.
    SignalledInFlight,
}

/// What [`crate::ChatPool::cancel_thread`] did to one thread.
///
/// Every message named here is accounted for: each `removed` ticket completed `Cancelled`, and
/// each `signalled` ticket completes `Cancelled` when its worker finishes the message, unless a
/// shutdown abandons it first.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct CancelReport {
    /// Queued messages removed from the thread, in queue order.
    pub removed: Vec<MessageRef>,
    /// Messages in flight on the thread whose tokens were set. A thread has at most one.
    pub signalled: Vec<MessageRef>,
}

#[cfg(test)]
mod tests {
    use std::error::Error;
    use std::time::Duration;

    use gsd_proto::gsd::v1::SubagentType;

    use super::*;
    use crate::chat::{ChatError, ChatPool};
    use crate::chat_runtime::tests::{
        enqueue, msg_ref, open, settle, start, Probe, ScriptedBudget, WAIT,
    };
    use crate::chat_runtime::Completed;

    #[test]
    fn builtin_executor_stops_after_one_slice_when_its_token_is_set() {
        use crate::chat_runtime::{builtin_executor, ChatMessage};
        // A message that would hold for a minute stops at its first cooperative check.
        let mut msg = ChatMessage::new("m1", "a", "s1", b"m1".to_vec());
        msg.hold_ms = 60_000;
        let token = CancelToken::default();
        token.cancel();
        let executor = builtin_executor();
        assert_eq!(executor(&msg, &token), Err("cancelled".to_string()));
    }

    #[test]
    fn token_is_shared_by_clones_and_is_one_way() {
        let token = CancelToken::default();
        let clone = token.clone();
        assert!(!clone.is_cancelled());
        token.cancel();
        assert!(clone.is_cancelled());
        token.cancel();
        assert!(token.is_cancelled());
    }

    #[test]
    fn cancel_of_a_queued_message_removes_it_and_it_never_runs() -> Result<(), Box<dyn Error>> {
        let probe = Probe::new(false);
        let runtime = start(1, 64, Arc::new(ScriptedBudget::default()), &probe)?;
        open(&runtime, "a");
        let a1 = enqueue(&runtime, "a", "a1")?;
        probe.wait_started(1);
        let a2 = enqueue(&runtime, "a", "a2")?;
        let a3 = enqueue(&runtime, "a", "a3")?;
        assert_eq!(
            runtime.cancel_message("a", "a2")?,
            CancelOutcome::RemovedQueued
        );
        assert_eq!(settle(&a2), Completed::Cancelled);
        probe.open_gate();
        assert!(matches!(settle(&a1), Completed::Ok(_)));
        assert_eq!(settle(&a3), Completed::Ok(b"a3".to_vec()));
        assert!(!probe.started().iter().any(|id| id == "a2"));
        assert_eq!(probe.finished(), ["a1", "a3"]);
        runtime.shutdown(WAIT);
        Ok(())
    }

    #[test]
    fn cancel_of_an_in_flight_message_is_observed_by_the_executor_and_the_worker_serves_on(
    ) -> Result<(), Box<dyn Error>> {
        let probe = Probe::new(false);
        let runtime = start(1, 64, Arc::new(ScriptedBudget::default()), &probe)?;
        open(&runtime, "a");
        let a1 = enqueue(&runtime, "a", "a1")?;
        probe.wait_started(1);
        let a2 = enqueue(&runtime, "a", "a2")?;
        assert_eq!(
            runtime.cancel_message("a", "a1")?,
            CancelOutcome::SignalledInFlight
        );
        // The executor returns a payload after it sees the token. The ticket still completes
        // Cancelled, and the payload is discarded. The gate stayed closed the whole time, so the
        // executor saw the token before the gate opened: a gate-ordering fact, not a timing one.
        assert_eq!(settle(&a1), Completed::Cancelled);
        assert!(probe.cancelled_at_closed_gate().iter().any(|id| id == "a1"));
        probe.open_gate();
        assert_eq!(settle(&a2), Completed::Ok(b"a2".to_vec()));
        runtime.shutdown(WAIT);
        Ok(())
    }

    #[test]
    fn cancel_thread_removes_queued_and_signals_the_in_flight_message() -> Result<(), Box<dyn Error>>
    {
        let probe = Probe::new(false);
        let runtime = start(1, 64, Arc::new(ScriptedBudget::default()), &probe)?;
        open(&runtime, "a");
        let a1 = enqueue(&runtime, "a", "a1")?;
        probe.wait_started(1);
        let a2 = enqueue(&runtime, "a", "a2")?;
        let a3 = enqueue(&runtime, "a", "a3")?;
        let a4 = enqueue(&runtime, "a", "a4")?;
        let report = runtime.cancel_thread("a")?;
        assert_eq!(
            report.removed,
            [msg_ref("a", "a2"), msg_ref("a", "a3"), msg_ref("a", "a4")]
        );
        assert_eq!(report.signalled, [msg_ref("a", "a1")]);
        for ticket in [&a2, &a3, &a4] {
            assert_eq!(settle(ticket), Completed::Cancelled);
        }
        assert_eq!(settle(&a1), Completed::Cancelled);
        assert_eq!(probe.started(), ["a1"]);
        runtime.shutdown(WAIT);
        Ok(())
    }

    #[test]
    fn unknown_message_on_an_open_thread_changes_nothing() -> Result<(), Box<dyn Error>> {
        let probe = Probe::new(false);
        let runtime = start(1, 64, Arc::new(ScriptedBudget::default()), &probe)?;
        open(&runtime, "a");
        let a1 = enqueue(&runtime, "a", "a1")?;
        probe.wait_started(1);
        let a2 = enqueue(&runtime, "a", "a2")?;
        assert_eq!(
            runtime.cancel_message("a", "nope"),
            Err(ChatError::UnknownMessage {
                thread_id: "a".to_string(),
                msg_id: "nope".to_string(),
            })
        );
        assert_eq!(runtime.state().queue.queued_count(), 1);
        probe.open_gate();
        assert!(matches!(settle(&a1), Completed::Ok(_)));
        assert_eq!(settle(&a2), Completed::Ok(b"a2".to_vec()));
        runtime.shutdown(WAIT);
        Ok(())
    }

    #[test]
    fn cancel_after_completion_is_unknown_and_the_ticket_keeps_its_outcome(
    ) -> Result<(), Box<dyn Error>> {
        let probe = Probe::new(true);
        let runtime = start(1, 64, Arc::new(ScriptedBudget::default()), &probe)?;
        open(&runtime, "a");
        let a1 = enqueue(&runtime, "a", "a1")?;
        assert_eq!(settle(&a1), Completed::Ok(b"a1".to_vec()));
        assert_eq!(
            runtime.cancel_message("a", "a1"),
            Err(ChatError::UnknownMessage {
                thread_id: "a".to_string(),
                msg_id: "a1".to_string(),
            })
        );
        assert_eq!(settle(&a1), Completed::Ok(b"a1".to_vec()));
        runtime.shutdown(WAIT);
        Ok(())
    }

    #[test]
    fn shutdown_sets_the_token_of_an_abandoned_message() -> Result<(), Box<dyn Error>> {
        let probe = Probe::new(false);
        let runtime = start(1, 64, Arc::new(ScriptedBudget::default()), &probe)?;
        open(&runtime, "a");
        let a1 = enqueue(&runtime, "a", "a1")?;
        probe.wait_started(1);
        let a2 = enqueue(&runtime, "a", "a2")?;
        let a3 = enqueue(&runtime, "a", "a3")?;
        let report = runtime.shutdown(Duration::from_millis(50));
        assert_eq!(report.abandoned, [msg_ref("a", "a1")]);
        assert_eq!(report.dropped, [msg_ref("a", "a2"), msg_ref("a", "a3")]);
        // The token is set before the worker is detached, so it reads cancelled now.
        assert!(probe.token("a1").is_some_and(|token| token.is_cancelled()));
        for ticket in [&a1, &a2, &a3] {
            assert_eq!(settle(ticket), Completed::Failed(ChatError::ShuttingDown));
        }
        // The executor returns after it sees the token. Its late result is ignored.
        probe.wait_finished(1);
        assert_eq!(settle(&a1), Completed::Failed(ChatError::ShuttingDown));
        Ok(())
    }

    #[test]
    fn unknown_thread_is_refused_by_both_facade_methods() -> Result<(), Box<dyn Error>> {
        let probe = Probe::new(true);
        let pool = ChatPool::assemble(
            crate::chat_runtime::tests::config(1, 64),
            Arc::new(ScriptedBudget::default()),
            probe.executor(),
        )?;
        assert_eq!(
            pool.cancel_message("nope", "x"),
            Err(ChatError::UnknownThread("nope".to_string()))
        );
        assert_eq!(
            pool.cancel_thread("nope"),
            Err(ChatError::UnknownThread("nope".to_string()))
        );
        // An open thread with nothing queued or running reports nothing and is not an error.
        let kind = SubagentType {
            value: "w".to_string(),
        };
        pool.open_thread("a", kind)?;
        assert_eq!(pool.cancel_thread("a")?, CancelReport::default());
        pool.shutdown(WAIT);
        Ok(())
    }
}
