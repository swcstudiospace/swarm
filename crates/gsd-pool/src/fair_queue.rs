//! Per-thread bounded FIFO with per-thread dedupe and a round-robin ready ring.
//!
//! Crate-private and pure: no threads, no clock, no `unsafe`. The runtime in `chat_runtime.rs`
//! owns one [`FairQueue`] under its runtime lock and is its only caller.
//!
//! A thread is *ready* when its queue is non-empty and none of its jobs is in flight. Ready
//! threads wait in a ring in the order they became ready, so each one is served in turn (GA-4).
//! The dedupe window keeps `(msg_id, seq)` pairs per thread; its index is a map so that a
//! resubmission can report the sequence number it was first accepted at.

use std::collections::hash_map::Entry;
use std::collections::{HashMap, VecDeque};

use crate::chat::ChatError;

/// Returned by [`FairQueue::open`] when the thread id is already open. The queue is unchanged
/// (DD-3).
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct QueueAlreadyOpen(pub String);

/// Outcome of an offer that was not refused.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum OfferOutcome {
    /// The message was queued at sequence number `seq`.
    Enqueued { seq: u64 },
    /// The message id is inside the thread's dedupe window. `seq` is the value recorded when it
    /// was first accepted.
    AlreadyAccepted { seq: u64 },
}

/// One queued message. `arrival` orders messages across threads in [`FairQueue::drain_queued`].
struct Queued<T> {
    arrival: u64,
    seq: u64,
    msg_id: String,
    item: T,
}

/// The message a thread has in flight.
struct InFlight {
    msg_id: String,
    /// Sequence number the message was accepted at.
    seq: u64,
}

/// State of one open thread.
struct ThreadQueue<T> {
    queued: VecDeque<Queued<T>>,
    /// Recently accepted ids, oldest first, with the sequence number each was accepted at.
    window: VecDeque<(String, u64)>,
    /// Index over `window`, for lookup by id.
    window_index: HashMap<String, u64>,
    next_seq: u64,
    /// The message this thread has in flight, or `None` when it has none.
    in_flight: Option<InFlight>,
}

impl<T> ThreadQueue<T> {
    fn new() -> Self {
        Self {
            queued: VecDeque::new(),
            window: VecDeque::new(),
            window_index: HashMap::new(),
            next_seq: 0,
            in_flight: None,
        }
    }

    /// A thread is busy while it has queued work or a job in flight.
    fn is_busy(&self) -> bool {
        !self.queued.is_empty() || self.in_flight.is_some()
    }
}

/// Bounded per-thread FIFO queues with dedupe and round-robin dispatch. Crate-private.
pub(crate) struct FairQueue<T> {
    per_thread_capacity: usize,
    dedupe_window: usize,
    next_arrival: u64,
    threads: HashMap<String, ThreadQueue<T>>,
    /// Ready thread ids, first to become ready first.
    ready: VecDeque<String>,
}

/// The error for an operation on a thread that is not open.
fn unknown(thread: &str) -> ChatError {
    ChatError::UnknownThread(thread.to_string())
}

impl<T> FairQueue<T> {
    /// Creates an empty queue set. Each thread holds at most `per_thread_capacity` queued
    /// messages and remembers at most `dedupe_window` accepted ids.
    pub(crate) fn new(per_thread_capacity: usize, dedupe_window: usize) -> Self {
        Self {
            per_thread_capacity,
            dedupe_window,
            next_arrival: 0,
            threads: HashMap::new(),
            ready: VecDeque::new(),
        }
    }

    /// Opens `thread` with an empty queue. Returns [`QueueAlreadyOpen`] and changes nothing when
    /// it is already open.
    pub(crate) fn open(&mut self, thread: &str) -> Result<(), QueueAlreadyOpen> {
        match self.threads.entry(thread.to_string()) {
            Entry::Occupied(_) => Err(QueueAlreadyOpen(thread.to_string())),
            Entry::Vacant(slot) => {
                slot.insert(ThreadQueue::new());
                Ok(())
            }
        }
    }

    /// Closes `thread`. Returns `UnknownThread` when it is not open and `ThreadBusy` when it
    /// has queued or in-flight work. A refused call changes nothing.
    pub(crate) fn close(&mut self, thread: &str) -> Result<(), ChatError> {
        let entry = self.threads.get(thread).ok_or_else(|| unknown(thread))?;
        if entry.is_busy() {
            return Err(ChatError::ThreadBusy(thread.to_string()));
        }
        self.threads.remove(thread);
        Ok(())
    }

    /// Reports whether `thread` has queued or in-flight work. `UnknownThread` when it is not open.
    pub(crate) fn is_busy(&self, thread: &str) -> Result<bool, ChatError> {
        self.threads
            .get(thread)
            .map(ThreadQueue::is_busy)
            .ok_or_else(|| unknown(thread))
    }

    /// Offers one message to `thread`.
    ///
    /// Checked in this order, and a refusal records nothing: `UnknownThread` when the thread is
    /// not open; `AlreadyAccepted` with the in-flight sequence number when `msg_id` is the id of
    /// the message the thread has in flight; `AlreadyAccepted` with the queued sequence number
    /// when `msg_id` is queued on the thread; `AlreadyAccepted` with the recorded sequence number
    /// when `msg_id` is inside the dedupe window; `QueueFull` when the queue is at capacity.
    /// A live id (in flight or queued) is refused whatever the window holds, so eviction from
    /// the window cannot let a second copy in. The window covers the ids that are no longer
    /// live. Otherwise the message is queued and its id enters the window, which evicts its
    /// oldest id when full.
    pub(crate) fn offer(
        &mut self,
        thread: &str,
        msg_id: &str,
        item: T,
    ) -> Result<OfferOutcome, ChatError> {
        let capacity = self.per_thread_capacity;
        let window_len = self.dedupe_window;
        let arrival = self.next_arrival;
        let entry = self
            .threads
            .get_mut(thread)
            .ok_or_else(|| unknown(thread))?;
        if let Some(job) = &entry.in_flight {
            if job.msg_id == msg_id {
                return Ok(OfferOutcome::AlreadyAccepted { seq: job.seq });
            }
        }
        // A queued id is live. It is refused here whatever the window holds, so window eviction
        // cannot let a second copy of it into the queue.
        if let Some(queued) = entry.queued.iter().find(|queued| queued.msg_id == msg_id) {
            return Ok(OfferOutcome::AlreadyAccepted { seq: queued.seq });
        }
        if let Some(&seq) = entry.window_index.get(msg_id) {
            return Ok(OfferOutcome::AlreadyAccepted { seq });
        }
        if entry.queued.len() >= capacity {
            return Err(ChatError::QueueFull {
                thread_id: thread.to_string(),
                capacity,
            });
        }
        let seq = entry.next_seq;
        entry.next_seq += 1;
        self.next_arrival += 1;
        if window_len > 0 {
            if entry.window.len() == window_len {
                if let Some((evicted, _)) = entry.window.pop_front() {
                    entry.window_index.remove(&evicted);
                }
            }
            entry.window.push_back((msg_id.to_string(), seq));
            entry.window_index.insert(msg_id.to_string(), seq);
        }
        let becomes_ready = entry.queued.is_empty() && entry.in_flight.is_none();
        entry.queued.push_back(Queued {
            arrival,
            seq,
            msg_id: msg_id.to_string(),
            item,
        });
        if becomes_ready {
            self.ready.push_back(thread.to_string());
        }
        Ok(OfferOutcome::Enqueued { seq })
    }

    /// Takes the head message of the thread at the front of the ready ring and marks that
    /// thread in flight. Returns `(thread, msg_id, item)`.
    pub(crate) fn take_next(&mut self) -> Option<(String, String, T)> {
        while let Some(thread) = self.ready.pop_front() {
            let Some(entry) = self.threads.get_mut(&thread) else {
                continue;
            };
            let Some(head) = entry.queued.pop_front() else {
                continue;
            };
            let Queued {
                msg_id, seq, item, ..
            } = head;
            entry.in_flight = Some(InFlight {
                msg_id: msg_id.clone(),
                seq,
            });
            return Some((thread, msg_id, item));
        }
        None
    }

    /// Clears the in-flight job of `thread`. The thread rejoins the back of the ring when it has
    /// queued work. A thread with no job in flight is left unchanged.
    pub(crate) fn complete(&mut self, thread: &str) {
        let Some(entry) = self.threads.get_mut(thread) else {
            return;
        };
        if entry.in_flight.is_none() {
            return;
        }
        entry.in_flight = None;
        if !entry.queued.is_empty() {
            self.ready.push_back(thread.to_string());
        }
    }

    /// Returns the id of the message `thread` has in flight, or `None` when it has none.
    /// `UnknownThread` when `thread` is not open.
    pub(crate) fn in_flight_msg(&self, thread: &str) -> Result<Option<&str>, ChatError> {
        self.threads
            .get(thread)
            .map(|entry| entry.in_flight.as_ref().map(|job| job.msg_id.as_str()))
            .ok_or_else(|| unknown(thread))
    }

    /// Removes one queued (not in-flight) message. The dedupe window is unchanged, so the id is
    /// no longer live but stays in the window until it is evicted.
    pub(crate) fn remove(&mut self, thread: &str, msg_id: &str) -> Option<T> {
        let entry = self.threads.get_mut(thread)?;
        let position = entry
            .queued
            .iter()
            .position(|queued| queued.msg_id == msg_id)?;
        let removed = entry.queued.remove(position)?;
        if entry.queued.is_empty() {
            self.ready.retain(|ready| ready.as_str() != thread);
        }
        Some(removed.item)
    }

    /// Removes every queued message of `thread`, in queue order. The thread stays open.
    pub(crate) fn drain_thread(&mut self, thread: &str) -> Vec<(String, T)> {
        let Some(entry) = self.threads.get_mut(thread) else {
            return Vec::new();
        };
        let drained = entry
            .queued
            .drain(..)
            .map(|queued| (queued.msg_id, queued.item))
            .collect();
        self.ready.retain(|ready| ready.as_str() != thread);
        drained
    }

    /// Removes every queued message of every thread and returns `(thread, msg_id, item)` in
    /// arrival order. Used by shutdown.
    pub(crate) fn drain_queued(&mut self) -> Vec<(String, String, T)> {
        let mut drained: Vec<(u64, String, String, T)> = Vec::new();
        for (thread, entry) in self.threads.iter_mut() {
            for queued in entry.queued.drain(..) {
                drained.push((queued.arrival, thread.clone(), queued.msg_id, queued.item));
            }
        }
        self.ready.clear();
        drained.sort_by_key(|row| row.0);
        drained
            .into_iter()
            .map(|(_, thread, msg_id, item)| (thread, msg_id, item))
            .collect()
    }

    /// Lists every queued `(thread, msg_id)` in arrival order without removing anything.
    pub(crate) fn queued_refs(&self) -> Vec<(String, String)> {
        let mut rows: Vec<(u64, String, String)> = self
            .threads
            .iter()
            .flat_map(|(thread, entry)| {
                entry
                    .queued
                    .iter()
                    .map(move |queued| (queued.arrival, thread.clone(), queued.msg_id.clone()))
            })
            .collect();
        rows.sort_by_key(|row| row.0);
        rows.into_iter()
            .map(|(_, thread, msg_id)| (thread, msg_id))
            .collect()
    }

    /// Number of queued messages across all threads.
    #[cfg(test)]
    pub(crate) fn queued_count(&self) -> usize {
        self.threads.values().map(|entry| entry.queued.len()).sum()
    }

    /// Number of threads with a job in flight.
    #[cfg(test)]
    pub(crate) fn in_flight_count(&self) -> usize {
        self.threads
            .values()
            .filter(|entry| entry.in_flight.is_some())
            .count()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;
    use proptest::test_runner::TestCaseError;

    fn open(queue: &mut FairQueue<String>, id: &str) {
        assert!(queue.open(id).is_ok(), "{id} should open");
    }

    /// Offers `id` with its own id as the payload.
    fn offer(
        queue: &mut FairQueue<String>,
        thread: &str,
        id: &str,
    ) -> Result<OfferOutcome, ChatError> {
        queue.offer(thread, id, id.to_string())
    }

    /// Takes one message and completes its thread, returning the message id.
    fn serve_one(queue: &mut FairQueue<String>) -> Option<String> {
        let (thread, msg_id, _) = queue.take_next()?;
        queue.complete(&thread);
        Some(msg_id)
    }

    #[test]
    fn in_flight_msg_names_the_running_message_until_complete() -> Result<(), ChatError> {
        let mut queue = FairQueue::new(8, 8);
        open(&mut queue, "a");
        assert_eq!(
            queue.in_flight_msg("nope"),
            Err(ChatError::UnknownThread("nope".to_string()))
        );
        assert_eq!(queue.in_flight_msg("a"), Ok(None));
        offer(&mut queue, "a", "a1")?;
        offer(&mut queue, "a", "a2")?;
        let Some((thread, msg_id, _)) = queue.take_next() else {
            return Err(ChatError::UnknownThread("a: nothing was ready".to_string()));
        };
        assert_eq!(queue.in_flight_msg("a"), Ok(Some("a1")));
        assert_eq!(msg_id, "a1");
        // The thread stays busy while a1 runs, so a2 waits behind it.
        assert_eq!(queue.is_busy("a"), Ok(true));
        queue.complete(&thread);
        assert_eq!(queue.in_flight_msg("a"), Ok(None));
        Ok(())
    }

    #[test]
    fn per_thread_order_is_enqueue_order() -> Result<(), ChatError> {
        let mut queue = FairQueue::new(8, 8);
        open(&mut queue, "a");
        for id in ["m1", "m2", "m3"] {
            offer(&mut queue, "a", id)?;
        }
        let mut served = Vec::new();
        while let Some(id) = serve_one(&mut queue) {
            served.push(id);
        }
        assert_eq!(served, ["m1", "m2", "m3"]);
        Ok(())
    }

    #[test]
    fn ready_threads_are_served_round_robin() -> Result<(), ChatError> {
        let mut queue = FairQueue::new(8, 8);
        open(&mut queue, "a");
        open(&mut queue, "b");
        offer(&mut queue, "a", "a1")?;
        offer(&mut queue, "a", "a2")?;
        offer(&mut queue, "a", "a3")?;
        offer(&mut queue, "b", "b1")?;
        // `a` became ready first and `b` second, so the ring serves a, b, then a again.
        let mut served = Vec::new();
        while let Some(id) = serve_one(&mut queue) {
            served.push(id);
        }
        assert_eq!(served, ["a1", "b1", "a2", "a3"]);
        Ok(())
    }

    #[test]
    fn queue_full_is_refused_and_records_nothing() -> Result<(), ChatError> {
        let mut queue = FairQueue::new(2, 8);
        open(&mut queue, "a");
        offer(&mut queue, "a", "m1")?;
        offer(&mut queue, "a", "m2")?;
        assert_eq!(
            offer(&mut queue, "a", "m3"),
            Err(ChatError::QueueFull {
                thread_id: "a".to_string(),
                capacity: 2,
            })
        );
        assert_eq!(queue.queued_count(), 2);
        // The refused id is not in the window and took no sequence number: the next accepted
        // message after the queue drains gets seq 2.
        assert_eq!(serve_one(&mut queue).as_deref(), Some("m1"));
        assert_eq!(
            offer(&mut queue, "a", "m3")?,
            OfferOutcome::Enqueued { seq: 2 }
        );
        Ok(())
    }

    #[test]
    fn resubmission_inside_the_window_is_already_accepted() -> Result<(), ChatError> {
        let mut queue = FairQueue::new(8, 8);
        open(&mut queue, "a");
        assert_eq!(
            offer(&mut queue, "a", "m1")?,
            OfferOutcome::Enqueued { seq: 0 }
        );
        assert_eq!(
            offer(&mut queue, "a", "m2")?,
            OfferOutcome::Enqueued { seq: 1 }
        );
        assert_eq!(
            offer(&mut queue, "a", "m1")?,
            OfferOutcome::AlreadyAccepted { seq: 0 }
        );
        assert_eq!(queue.queued_count(), 2);
        Ok(())
    }

    #[test]
    fn in_flight_id_stays_in_the_window_while_the_queue_is_full() -> Result<(), ChatError> {
        // The window is one larger than the queue, so the in-flight id (the oldest) is not
        // evicted while the queue is full, and its resubmission is AlreadyAccepted.
        let mut queue = FairQueue::new(2, 3);
        open(&mut queue, "a");
        assert_eq!(
            offer(&mut queue, "a", "x")?,
            OfferOutcome::Enqueued { seq: 0 }
        );
        let Some((thread, msg_id, _)) = queue.take_next() else {
            panic!("x should be ready");
        };
        assert_eq!((thread.as_str(), msg_id.as_str()), ("a", "x"));
        assert_eq!(
            offer(&mut queue, "a", "y")?,
            OfferOutcome::Enqueued { seq: 1 }
        );
        assert_eq!(
            offer(&mut queue, "a", "z")?,
            OfferOutcome::Enqueued { seq: 2 }
        );
        assert_eq!(queue.queued_count(), 2);
        assert_eq!(
            offer(&mut queue, "a", "x")?,
            OfferOutcome::AlreadyAccepted { seq: 0 }
        );
        Ok(())
    }

    #[test]
    fn resubmitting_the_in_flight_id_is_already_accepted_after_cancels_evict_it(
    ) -> Result<(), ChatError> {
        // The window is one larger than the queue. Cancelling the queued y and z frees queue
        // room but keeps the window, and accepting w then evicts x from the window. The
        // in-flight id must still be refused.
        let mut queue = FairQueue::new(2, 3);
        open(&mut queue, "a");
        assert_eq!(
            offer(&mut queue, "a", "x")?,
            OfferOutcome::Enqueued { seq: 0 }
        );
        let Some((thread, msg_id, _)) = queue.take_next() else {
            panic!("x should be ready");
        };
        assert_eq!((thread.as_str(), msg_id.as_str()), ("a", "x"));
        assert_eq!(
            offer(&mut queue, "a", "y")?,
            OfferOutcome::Enqueued { seq: 1 }
        );
        assert_eq!(
            offer(&mut queue, "a", "z")?,
            OfferOutcome::Enqueued { seq: 2 }
        );
        assert_eq!(queue.remove("a", "y"), Some("y".to_string()));
        assert_eq!(
            offer(&mut queue, "a", "w")?,
            OfferOutcome::Enqueued { seq: 3 }
        );
        let window = &queue.threads["a"].window_index;
        assert!(!window.contains_key("x"), "accepting w should evict x");
        assert_eq!(queue.remove("a", "z"), Some("z".to_string()));
        assert_eq!(queue.queued_count(), 1);
        assert_eq!(
            offer(&mut queue, "a", "x")?,
            OfferOutcome::AlreadyAccepted { seq: 0 }
        );
        assert_eq!(queue.queued_count(), 1);
        assert_eq!(queue.in_flight_msg("a"), Ok(Some("x")));
        Ok(())
    }

    #[test]
    fn resubmitting_a_queued_id_is_already_accepted_after_the_window_evicts_it(
    ) -> Result<(), ChatError> {
        // The window is one larger than the queue. x is in flight and q waits behind it. Each
        // r is accepted and then cancelled: a cancel frees queue room but keeps the window, so
        // the window fills with r ids and evicts q. q is still queued, so it must be refused.
        let mut queue = FairQueue::new(2, 3);
        open(&mut queue, "a");
        assert_eq!(
            offer(&mut queue, "a", "x")?,
            OfferOutcome::Enqueued { seq: 0 }
        );
        let Some((thread, msg_id, _)) = queue.take_next() else {
            panic!("x should be ready");
        };
        assert_eq!((thread.as_str(), msg_id.as_str()), ("a", "x"));
        assert_eq!(
            offer(&mut queue, "a", "q")?,
            OfferOutcome::Enqueued { seq: 1 }
        );
        for (r, seq) in [("r1", 2), ("r2", 3), ("r3", 4)] {
            assert_eq!(offer(&mut queue, "a", r)?, OfferOutcome::Enqueued { seq });
            assert_eq!(queue.remove("a", r), Some(r.to_string()));
        }
        let window = &queue.threads["a"].window_index;
        assert!(!window.contains_key("q"), "accepting r3 should evict q");
        assert_eq!(queue.queued_count(), 1);
        assert_eq!(
            offer(&mut queue, "a", "q")?,
            OfferOutcome::AlreadyAccepted { seq: 1 }
        );
        assert_eq!(queue.queued_count(), 1);
        Ok(())
    }

    #[test]
    fn resubmission_of_a_finished_id_outside_the_window_is_new() -> Result<(), ChatError> {
        // m1 runs and completes, so it is no longer live. Two later ids push it out of the
        // window of two, and its resubmission is then a new message.
        let mut queue = FairQueue::new(8, 2);
        open(&mut queue, "a");
        offer(&mut queue, "a", "m1")?;
        let Some((thread, msg_id, _)) = queue.take_next() else {
            panic!("m1 should be ready");
        };
        assert_eq!((thread.as_str(), msg_id.as_str()), ("a", "m1"));
        queue.complete("a");
        offer(&mut queue, "a", "m2")?;
        offer(&mut queue, "a", "m3")?;
        // The window holds m2 and m3 now, so m1 is new again.
        assert_eq!(
            offer(&mut queue, "a", "m1")?,
            OfferOutcome::Enqueued { seq: 3 }
        );
        Ok(())
    }

    #[test]
    fn same_id_on_another_thread_is_a_new_message() -> Result<(), ChatError> {
        let mut queue = FairQueue::new(8, 8);
        open(&mut queue, "a");
        open(&mut queue, "b");
        assert_eq!(
            offer(&mut queue, "a", "m1")?,
            OfferOutcome::Enqueued { seq: 0 }
        );
        assert_eq!(
            offer(&mut queue, "b", "m1")?,
            OfferOutcome::Enqueued { seq: 0 }
        );
        Ok(())
    }

    #[test]
    fn busy_thread_refuses_close_and_reports_busy() -> Result<(), ChatError> {
        let mut queue = FairQueue::new(8, 8);
        open(&mut queue, "a");
        assert_eq!(queue.is_busy("a"), Ok(false));
        offer(&mut queue, "a", "m1")?;
        assert_eq!(queue.is_busy("a"), Ok(true));
        assert_eq!(
            queue.close("a"),
            Err(ChatError::ThreadBusy("a".to_string()))
        );
        // In flight counts as busy even with an empty queue.
        let (thread, _, _) = queue
            .take_next()
            .ok_or(ChatError::UnknownThread("a".into()))?;
        assert_eq!(queue.is_busy("a"), Ok(true));
        assert_eq!(
            queue.close("a"),
            Err(ChatError::ThreadBusy("a".to_string()))
        );
        queue.complete(&thread);
        assert_eq!(queue.is_busy("a"), Ok(false));
        queue.close("a")?;
        assert_eq!(
            offer(&mut queue, "a", "m2"),
            Err(ChatError::UnknownThread("a".to_string()))
        );
        assert_eq!(
            queue.is_busy("a"),
            Err(ChatError::UnknownThread("a".to_string()))
        );
        assert_eq!(
            queue.close("a"),
            Err(ChatError::UnknownThread("a".to_string()))
        );
        Ok(())
    }

    #[test]
    fn duplicate_open_is_refused_and_changes_nothing() -> Result<(), ChatError> {
        let mut queue = FairQueue::new(8, 8);
        open(&mut queue, "a");
        offer(&mut queue, "a", "m1")?;
        assert_eq!(queue.open("a"), Err(QueueAlreadyOpen("a".to_string())));
        assert_eq!(queue.queued_count(), 1);
        assert_eq!(
            offer(&mut queue, "a", "m1")?,
            OfferOutcome::AlreadyAccepted { seq: 0 }
        );
        Ok(())
    }

    #[test]
    fn unknown_thread_is_refused_everywhere() {
        let mut queue: FairQueue<String> = FairQueue::new(8, 8);
        let refused = || ChatError::UnknownThread("a".to_string());
        assert_eq!(offer(&mut queue, "a", "m1"), Err(refused()));
        assert_eq!(queue.is_busy("a"), Err(refused()));
        assert_eq!(queue.close("a"), Err(refused()));
        queue.complete("a");
        assert!(queue.take_next().is_none());
    }

    #[test]
    fn remove_takes_a_queued_message_and_keeps_the_window() -> Result<(), ChatError> {
        let mut queue = FairQueue::new(8, 8);
        open(&mut queue, "a");
        offer(&mut queue, "a", "m1")?;
        offer(&mut queue, "a", "m2")?;
        offer(&mut queue, "a", "m3")?;
        assert_eq!(queue.remove("a", "m2"), Some("m2".to_string()));
        assert_eq!(queue.remove("a", "m2"), None);
        assert_eq!(queue.queued_count(), 2);
        // m2 left the queue but is still inside the dedupe window.
        assert_eq!(
            offer(&mut queue, "a", "m2")?,
            OfferOutcome::AlreadyAccepted { seq: 1 }
        );
        assert_eq!(serve_one(&mut queue).as_deref(), Some("m1"));
        assert_eq!(serve_one(&mut queue).as_deref(), Some("m3"));
        Ok(())
    }

    #[test]
    fn remove_of_the_last_queued_message_clears_readiness() -> Result<(), ChatError> {
        let mut queue = FairQueue::new(8, 8);
        open(&mut queue, "a");
        offer(&mut queue, "a", "m1")?;
        assert_eq!(queue.remove("a", "m1"), Some("m1".to_string()));
        assert!(queue.take_next().is_none());
        Ok(())
    }

    #[test]
    fn drain_thread_returns_queue_order_and_leaves_the_ring() -> Result<(), ChatError> {
        let mut queue = FairQueue::new(8, 8);
        open(&mut queue, "a");
        open(&mut queue, "b");
        offer(&mut queue, "a", "a1")?;
        offer(&mut queue, "a", "a2")?;
        offer(&mut queue, "b", "b1")?;
        let drained = queue.drain_thread("a");
        assert_eq!(
            drained,
            vec![
                ("a1".to_string(), "a1".to_string()),
                ("a2".to_string(), "a2".to_string()),
            ]
        );
        // Only `b` is left in the ring.
        assert_eq!(serve_one(&mut queue).as_deref(), Some("b1"));
        assert!(queue.take_next().is_none());
        // `a` stays open after a drain.
        assert_eq!(queue.is_busy("a"), Ok(false));
        Ok(())
    }

    #[test]
    fn drain_queued_returns_arrival_order_across_threads() -> Result<(), ChatError> {
        let mut queue = FairQueue::new(8, 8);
        open(&mut queue, "a");
        open(&mut queue, "b");
        offer(&mut queue, "a", "m1")?;
        offer(&mut queue, "b", "m2")?;
        offer(&mut queue, "a", "m3")?;
        assert_eq!(
            queue.queued_refs(),
            vec![
                ("a".to_string(), "m1".to_string()),
                ("b".to_string(), "m2".to_string()),
                ("a".to_string(), "m3".to_string()),
            ]
        );
        let drained = queue.drain_queued();
        assert_eq!(
            drained,
            vec![
                ("a".to_string(), "m1".to_string(), "m1".to_string()),
                ("b".to_string(), "m2".to_string(), "m2".to_string()),
                ("a".to_string(), "m3".to_string(), "m3".to_string()),
            ]
        );
        assert_eq!(queue.queued_count(), 0);
        assert!(queue.take_next().is_none());
        Ok(())
    }

    /// Number of threads the property model opens.
    const THREADS: usize = 8;

    fn name(index: usize) -> String {
        format!("t{index}")
    }

    #[derive(Debug, Clone)]
    enum Op {
        Offer { thread: usize, msg: usize },
        Take,
        Complete { thread: usize },
        Remove { thread: usize, msg: usize },
    }

    fn op_strategy() -> impl Strategy<Value = Op> {
        prop_oneof![
            5 => (0..THREADS, 0..24usize).prop_map(|(thread, msg)| Op::Offer { thread, msg }),
            2 => Just(Op::Take),
            2 => (0..THREADS).prop_map(|thread| Op::Complete { thread }),
            2 => (0..THREADS, 0..24usize).prop_map(|(thread, msg)| Op::Remove { thread, msg }),
        ]
    }

    /// The expected result of one offer.
    #[derive(Debug, Clone, Copy)]
    enum Expect {
        Enqueued(u64),
        Already(u64),
        Full,
    }

    /// Ready-since record for one ready thread. `order` is the ready-transition counter,
    /// `takes_at` the number of takes when it became ready, and `ready_count` the number of
    /// ready threads at that moment, including itself (GA-4 bound).
    #[derive(Debug, Clone, Copy)]
    struct Stamp {
        order: u64,
        takes_at: u64,
        ready_count: usize,
    }

    #[derive(Default)]
    struct ModelThread {
        queued: VecDeque<(String, u64)>,
        in_flight: bool,
        /// The message in flight and its sequence number, or `None` when there is none.
        running: Option<(String, u64)>,
        window: VecDeque<(String, u64)>,
        next_seq: u64,
        stamp: Option<Stamp>,
    }

    /// Independent model of [`FairQueue`]: exact round-robin by ready order, plus the dedupe
    /// window and the per-thread capacity.
    struct Model {
        cap: usize,
        window_cap: usize,
        threads: Vec<ModelThread>,
        stamps: u64,
        takes: u64,
        served: Vec<usize>,
        accepted: usize,
        taken: usize,
        removed: usize,
    }

    impl Model {
        fn new(cap: usize, window_cap: usize) -> Self {
            Self {
                cap,
                window_cap,
                threads: (0..THREADS).map(|_| ModelThread::default()).collect(),
                stamps: 0,
                takes: 0,
                served: Vec::new(),
                accepted: 0,
                taken: 0,
                removed: 0,
            }
        }

        fn ready(thread: &ModelThread) -> bool {
            !thread.queued.is_empty() && !thread.in_flight
        }

        /// Re-derives ready stamps after one operation. A thread keeps its stamp while it stays
        /// ready, so its position in the ring is the stamp order.
        fn refresh(&mut self) {
            let ready_count = self.threads.iter().filter(|th| Self::ready(th)).count();
            for th in &mut self.threads {
                if !Self::ready(th) {
                    th.stamp = None;
                } else if th.stamp.is_none() {
                    th.stamp = Some(Stamp {
                        order: self.stamps,
                        takes_at: self.takes,
                        ready_count,
                    });
                    self.stamps += 1;
                }
            }
        }

        fn offer(&mut self, thread: usize, id: &str) -> Expect {
            let cap = self.cap;
            let window_cap = self.window_cap;
            let th = &mut self.threads[thread];
            if let Some((known, seq)) = &th.running {
                if known.as_str() == id {
                    return Expect::Already(*seq);
                }
            }
            if let Some((_, seq)) = th.queued.iter().find(|(known, _)| known.as_str() == id) {
                return Expect::Already(*seq);
            }
            if let Some((_, seq)) = th.window.iter().find(|(known, _)| known.as_str() == id) {
                return Expect::Already(*seq);
            }
            if th.queued.len() >= cap {
                return Expect::Full;
            }
            let seq = th.next_seq;
            th.next_seq += 1;
            th.window.push_back((id.to_string(), seq));
            while th.window.len() > window_cap {
                th.window.pop_front();
            }
            th.queued.push_back((id.to_string(), seq));
            self.accepted += 1;
            self.refresh();
            Expect::Enqueued(seq)
        }

        /// The ready thread that has waited longest, if any.
        fn next_ready(&self) -> Option<usize> {
            self.threads
                .iter()
                .enumerate()
                .filter(|(_, th)| Self::ready(th))
                .min_by_key(|(_, th)| th.stamp.map_or(u64::MAX, |stamp| stamp.order))
                .map(|(index, _)| index)
        }

        /// Checks the GA-4 bound for the thread about to be taken: it waited at most
        /// `ready_count - 1` takes, and the takes in that window served distinct threads.
        fn check_take(&self, thread: usize) -> Result<(), TestCaseError> {
            let stamp = self.threads[thread]
                .stamp
                .ok_or_else(|| TestCaseError::fail("a ready thread has no stamp"))?;
            let waited = self.takes - stamp.takes_at;
            prop_assert!(
                waited <= stamp.ready_count.saturating_sub(1) as u64,
                "thread {thread} waited {waited} takes with {} ready",
                stamp.ready_count
            );
            let window = &self.served[stamp.takes_at as usize..];
            let mut unique = window.to_vec();
            unique.sort_unstable();
            unique.dedup();
            prop_assert_eq!(
                unique.len(),
                window.len(),
                "a wait window served a thread twice"
            );
            Ok(())
        }

        fn head(&self, thread: usize) -> Option<String> {
            self.threads[thread]
                .queued
                .front()
                .map(|(id, _)| id.clone())
        }

        fn apply_take(&mut self, thread: usize) {
            let th = &mut self.threads[thread];
            th.running = th.queued.pop_front();
            th.in_flight = true;
            self.takes += 1;
            self.served.push(thread);
            self.taken += 1;
            self.refresh();
        }

        fn complete(&mut self, thread: usize) {
            if self.threads[thread].in_flight {
                self.threads[thread].in_flight = false;
                self.threads[thread].running = None;
                self.refresh();
            }
        }

        /// Removes the queued message `id` of `thread`, when it is queued. The window is unchanged.
        fn remove(&mut self, thread: usize, id: &str) -> bool {
            let th = &mut self.threads[thread];
            let Some(position) = th.queued.iter().position(|(known, _)| known.as_str() == id)
            else {
                return false;
            };
            th.queued.remove(position);
            self.removed += 1;
            self.refresh();
            true
        }

        fn queued_total(&self) -> usize {
            self.threads.iter().map(|th| th.queued.len()).sum()
        }

        fn in_flight_total(&self) -> usize {
            self.threads.iter().filter(|th| th.in_flight).count()
        }

        fn queued_pairs(&self) -> Vec<(String, String)> {
            self.threads
                .iter()
                .enumerate()
                .flat_map(|(index, th)| {
                    th.queued
                        .iter()
                        .map(move |(id, _)| (name(index), id.clone()))
                })
                .collect()
        }
    }

    /// Runs `ops` against a [`FairQueue`] and the [`Model`] in lockstep.
    fn run_model(cap: usize, window: usize, ops: &[Op]) -> Result<(), TestCaseError> {
        let mut queue: FairQueue<String> = FairQueue::new(cap, window);
        let mut model = Model::new(cap, window);
        for index in 0..THREADS {
            prop_assert!(queue.open(&name(index)).is_ok());
        }
        for op in ops {
            match op {
                Op::Offer { thread, msg } => {
                    let id = format!("m{msg}");
                    let expected = model.offer(*thread, &id);
                    let actual = queue.offer(&name(*thread), &id, id.clone());
                    match (expected, actual) {
                        (Expect::Enqueued(want), Ok(OfferOutcome::Enqueued { seq })) => {
                            prop_assert_eq!(seq, want);
                        }
                        (Expect::Already(want), Ok(OfferOutcome::AlreadyAccepted { seq })) => {
                            prop_assert_eq!(seq, want);
                        }
                        (Expect::Full, Err(ChatError::QueueFull { .. })) => {}
                        (want, got) => {
                            return Err(TestCaseError::fail(format!(
                                "offer: expected {want:?}, got {got:?}"
                            )));
                        }
                    }
                }
                Op::Take => match (model.next_ready(), queue.take_next()) {
                    (None, None) => {}
                    (Some(thread), Some((got_thread, msg_id, item))) => {
                        prop_assert_eq!(&got_thread, &name(thread));
                        let head = model.head(thread);
                        prop_assert_eq!(head.as_deref(), Some(msg_id.as_str()));
                        prop_assert_eq!(&item, &msg_id);
                        model.check_take(thread)?;
                        model.apply_take(thread);
                    }
                    (want, got) => {
                        return Err(TestCaseError::fail(format!(
                            "take: expected {want:?}, got {got:?}"
                        )));
                    }
                },
                Op::Complete { thread } => {
                    queue.complete(&name(*thread));
                    model.complete(*thread);
                }
                Op::Remove { thread, msg } => {
                    let id = format!("m{msg}");
                    let want = model.remove(*thread, &id);
                    let got = queue.remove(&name(*thread), &id).is_some();
                    prop_assert_eq!(got, want);
                }
            }
            prop_assert_eq!(queue.queued_count(), model.queued_total());
            prop_assert_eq!(queue.in_flight_count(), model.in_flight_total());
        }
        // Conservation: every accepted message is taken, still queued, or drained now.
        let drained = queue.drain_queued();
        prop_assert!(drained.iter().all(|(_, msg_id, item)| msg_id == item));
        prop_assert_eq!(model.accepted, model.taken + model.removed + drained.len());
        let mut got: Vec<(String, String)> = drained
            .into_iter()
            .map(|(thread, msg_id, _)| (thread, msg_id))
            .collect();
        let mut want = model.queued_pairs();
        got.sort();
        want.sort();
        prop_assert_eq!(got, want);
        Ok(())
    }

    proptest! {
        #[test]
        fn fair_queue_agrees_with_the_model(
            cap in 1usize..=16,
            window in 1usize..=32,
            ops in prop::collection::vec(op_strategy(), 1..200),
        ) {
            run_model(cap, window, &ops)?;
        }
    }
}
