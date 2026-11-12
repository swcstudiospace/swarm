//! Failure text for a worker task that panicked or was cancelled.
//!
//! Shared by the force-parallel dispatcher and the matrix join, so the same panic gives the same
//! failure message on both paths.

use tokio::task::JoinError;

use crate::force_parallel_dispatch::WORKER_FAILURE_CODE;

/// Failure text for a worker that panicked or was cancelled.
///
/// Built only from the panic payload. `JoinError`'s display embeds tokio's process-wide task id,
/// which differs between runs, so it must never reach a completion record.
pub(crate) fn panic_message(join_error: JoinError) -> String {
    let payload = match join_error.try_into_panic() {
        Ok(payload) => payload,
        Err(_) => return format!("{WORKER_FAILURE_CODE}: worker task was cancelled"),
    };
    let detail = payload
        .downcast_ref::<&str>()
        .map(|text| (*text).to_string())
        .or_else(|| payload.downcast_ref::<String>().cloned())
        .unwrap_or_else(|| "non-string panic payload".to_string());
    format!("{WORKER_FAILURE_CODE}: worker panicked: {detail}")
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Runs `body` in a spawned task that panics, and returns the message the dispatcher reports.
    async fn message_of_panic<F>(body: F) -> String
    where
        F: FnOnce() + Send + 'static,
    {
        let join_error = tokio::spawn(async move { body() })
            .await
            .expect_err("task should panic");
        panic_message(join_error)
    }

    #[tokio::test]
    async fn str_payload_maps_to_its_text() {
        let text = message_of_panic(|| std::panic::panic_any("boom")).await;
        assert_eq!(text, "E-INTERNAL: worker panicked: boom");
    }

    #[tokio::test]
    async fn string_payload_maps_to_the_same_text() {
        let text = message_of_panic(|| std::panic::panic_any(String::from("boom"))).await;
        assert_eq!(text, "E-INTERNAL: worker panicked: boom");
    }

    #[tokio::test]
    async fn opaque_payload_maps_to_the_non_string_text() {
        let text = message_of_panic(|| std::panic::panic_any(42_u32)).await;
        assert_eq!(
            text,
            "E-INTERNAL: worker panicked: non-string panic payload"
        );
    }

    #[tokio::test]
    async fn cancelled_task_maps_to_the_cancellation_text() {
        let handle = tokio::spawn(std::future::pending::<()>());
        handle.abort();
        let join_error = handle.await.expect_err("aborted task should not finish");
        assert_eq!(
            panic_message(join_error),
            "E-INTERNAL: worker task was cancelled"
        );
    }
}
