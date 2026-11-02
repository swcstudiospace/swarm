//! Lifecycle event dispatch to notify-only observers.
//!
//! Observers receive a shared reference to each event and return nothing, so they have
//! no path to mutate pool state. Dispatch snapshots the observer list under the read lock
//! and releases it before any observer runs.

use std::sync::Arc;

use gsd_proto::gsd::v1::ChatThreadId;
use parking_lot::RwLock;

/// A lifecycle transition for one chat thread.
#[derive(Debug, Clone)]
// Dead-code: only create, resume and terminate are dispatched today. Lint: the `On` prefix is the
// event vocabulary that the lifecycle contract names, so the variants keep it.
#[expect(
    dead_code,
    clippy::enum_variant_names,
    reason = "the On prefix is the lifecycle contract's vocabulary; not every variant is dispatched yet"
)]
pub(crate) enum LifecycleEvent {
    /// Thread was created.
    OnCreate(ChatThreadId),
    /// Thread was assigned work.
    OnAssign(ChatThreadId),
    /// Thread was suspended.
    OnSuspend(ChatThreadId),
    /// Thread was resumed.
    OnResume(ChatThreadId),
    /// Thread state was checkpointed.
    OnCheckpoint(ChatThreadId),
    /// Thread completed successfully.
    OnComplete(ChatThreadId),
    /// Thread failed.
    OnFailure(ChatThreadId),
    /// Thread was terminated.
    OnTerminate(ChatThreadId),
}

impl LifecycleEvent {
    /// Returns the chat thread this transition applies to.
    pub fn thread_id(&self) -> &ChatThreadId {
        match self {
            Self::OnCreate(id)
            | Self::OnAssign(id)
            | Self::OnSuspend(id)
            | Self::OnResume(id)
            | Self::OnCheckpoint(id)
            | Self::OnComplete(id)
            | Self::OnFailure(id)
            | Self::OnTerminate(id) => id,
        }
    }
}

/// Receives lifecycle events.
///
/// The only input is `&LifecycleEvent` and the return type is `()`, so an observer cannot
/// return a value into the pool or obtain a mutable reference to pool state.
pub(crate) trait LifecycleObserver: Send + Sync {
    /// Handles one event. Called once per dispatch, in registration order.
    fn on_event(&self, event: &LifecycleEvent);
}

/// Registry of observers and the dispatch point for lifecycle events.
#[derive(Default)]
pub(crate) struct LifecycleDispatcher {
    observers: RwLock<Vec<Arc<dyn LifecycleObserver>>>,
}

impl LifecycleDispatcher {
    /// Appends `observer`; it receives every event dispatched after this call returns.
    #[cfg(test)]
    pub fn register(&self, observer: Arc<dyn LifecycleObserver>) {
        self.observers.write().push(observer);
    }

    /// Delivers `event` to every observer registered when dispatch began, in registration
    /// order. No lock is held while an observer runs.
    pub fn dispatch(&self, event: &LifecycleEvent) {
        tracing::trace!(thread_id = %event.thread_id().value, "chat thread lifecycle event");
        // The temporary read guard is dropped at the end of this statement.
        let snapshot = self.observers.read().clone();
        for observer in &snapshot {
            observer.on_event(event);
        }
    }
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::{Arc, Weak};

    use gsd_proto::gsd::v1::ChatThreadId;
    use parking_lot::Mutex;

    use super::{LifecycleDispatcher, LifecycleEvent, LifecycleObserver};

    fn thread(value: &str) -> ChatThreadId {
        ChatThreadId {
            value: value.to_string(),
        }
    }

    /// Records `label:thread_id` for every event it sees.
    struct Recorder {
        label: &'static str,
        log: Arc<Mutex<Vec<String>>>,
    }

    impl LifecycleObserver for Recorder {
        fn on_event(&self, event: &LifecycleEvent) {
            self.log
                .lock()
                .push(format!("{}:{}", self.label, event.thread_id().value));
        }
    }

    /// On its first event, registers a late `Recorder` through the dispatcher it holds.
    /// Registering takes the write lock, so this completes only if dispatch holds no lock
    /// while observers run.
    struct RegistersOnFirstEvent {
        dispatcher: Weak<LifecycleDispatcher>,
        log: Arc<Mutex<Vec<String>>>,
        fired: AtomicBool,
    }

    impl LifecycleObserver for RegistersOnFirstEvent {
        fn on_event(&self, _event: &LifecycleEvent) {
            if self.fired.swap(true, Ordering::SeqCst) {
                return;
            }
            if let Some(dispatcher) = self.dispatcher.upgrade() {
                dispatcher.register(Arc::new(Recorder {
                    label: "late",
                    log: Arc::clone(&self.log),
                }));
            }
        }
    }

    #[test]
    fn observer_sees_each_event_exactly_once() {
        let log = Arc::new(Mutex::new(Vec::new()));
        let dispatcher = LifecycleDispatcher::default();
        dispatcher.register(Arc::new(Recorder {
            label: "obs",
            log: Arc::clone(&log),
        }));

        dispatcher.dispatch(&LifecycleEvent::OnCreate(thread("a")));
        dispatcher.dispatch(&LifecycleEvent::OnAssign(thread("a")));
        dispatcher.dispatch(&LifecycleEvent::OnComplete(thread("b")));

        assert_eq!(*log.lock(), vec!["obs:a", "obs:a", "obs:b"]);
    }

    #[test]
    fn dispatch_order_is_registration_order() {
        let log = Arc::new(Mutex::new(Vec::new()));
        let dispatcher = LifecycleDispatcher::default();
        for label in ["first", "second", "third"] {
            dispatcher.register(Arc::new(Recorder {
                label,
                log: Arc::clone(&log),
            }));
        }

        dispatcher.dispatch(&LifecycleEvent::OnResume(thread("t")));

        assert_eq!(*log.lock(), vec!["first:t", "second:t", "third:t"]);
    }

    #[test]
    fn dispatch_holds_no_lock_across_observer_call() {
        let log = Arc::new(Mutex::new(Vec::new()));
        let dispatcher = Arc::new(LifecycleDispatcher::default());
        dispatcher.register(Arc::new(RegistersOnFirstEvent {
            dispatcher: Arc::downgrade(&dispatcher),
            log: Arc::clone(&log),
            fired: AtomicBool::new(false),
        }));

        dispatcher.dispatch(&LifecycleEvent::OnCreate(thread("first")));
        // The late observer joined during the first dispatch, so it misses that event.
        assert!(log.lock().is_empty());

        dispatcher.dispatch(&LifecycleEvent::OnCreate(thread("second")));
        assert_eq!(*log.lock(), vec!["late:second"]);
    }
}
