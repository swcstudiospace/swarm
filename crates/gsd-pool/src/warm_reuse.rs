//! Warm pool of parked chat-thread handles, keyed per `SubagentType`.
//!
//! Crate-private machinery (D-1). A parked handle is reused by `pop` until its grace period
//! elapses. Time is passed in explicitly so eviction is deterministic under test.

use std::collections::HashMap;
use std::time::{Duration, Instant};

use gsd_proto::gsd::v1::SubagentType;

use crate::handle_registry::ChatThreadHandle;

/// Checkpoint callback run on each handle as it is parked in the pool.
pub(crate) type CheckpointHook = Box<dyn Fn(&ChatThreadHandle) + Send + Sync>;

/// A parked handle and the instant it was parked.
struct PooledEntry {
    handle: ChatThreadHandle,
    parked_at: Instant,
}

/// Result of [`WarmPool::push`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct PushOutcome {
    /// `true` when the handle was parked. `false` when the pool stayed full.
    pub parked: bool,
}

/// Bounded pool of idle handles, keyed by `SubagentType::value`.
///
/// Capacity (`warm_pool_size`) is shared across all subagent types. Entries parked
/// longer than the eviction grace period are never returned by `pop`; `push` reclaims
/// them when the pool is full, and `evict_expired` sweeps them on demand.
pub(crate) struct WarmPool {
    capacity: usize,
    eviction_grace: Duration,
    slots: HashMap<String, Vec<PooledEntry>>,
    checkpoint: Option<CheckpointHook>,
    hits: u64,
}

impl WarmPool {
    /// Creates an empty pool holding at most `warm_pool_size` handles.
    pub(crate) fn new(warm_pool_size: usize, eviction_grace: Duration) -> Self {
        Self {
            capacity: warm_pool_size,
            eviction_grace,
            slots: HashMap::new(),
            checkpoint: None,
            hits: 0,
        }
    }

    /// Installs the checkpoint hook, run on each handle as it is parked.
    #[cfg(test)]
    pub(crate) fn with_checkpoint(mut self, hook: CheckpointHook) -> Self {
        self.checkpoint = Some(hook);
        self
    }

    /// Takes the most recently parked live handle for `subagent_type`.
    ///
    /// Stale entries of that type are dropped first. A successful pop counts as a hit.
    pub(crate) fn pop(
        &mut self,
        subagent_type: &SubagentType,
        now: Instant,
    ) -> Option<ChatThreadHandle> {
        let grace = self.eviction_grace;
        let key = subagent_type.value.as_str();
        let entries = self.slots.get_mut(key)?;
        entries.retain(|entry| !is_stale(entry, now, grace));
        let reused = entries.pop().map(|entry| entry.handle);
        if entries.is_empty() {
            self.slots.remove(key);
        }
        if reused.is_some() {
            self.hits += 1;
        }
        reused
    }

    /// Parks `handle` under `subagent_type`, running the checkpoint hook first.
    ///
    /// When the pool is full, stale entries are reclaimed first. If the pool is still full,
    /// `parked` is `false`, the handle is not parked and the hook does not run.
    pub(crate) fn push(
        &mut self,
        subagent_type: &SubagentType,
        handle: ChatThreadHandle,
        now: Instant,
    ) -> PushOutcome {
        if self.len() >= self.capacity {
            self.evict_expired(now);
        }
        if self.len() >= self.capacity {
            return PushOutcome { parked: false };
        }
        if let Some(checkpoint) = &self.checkpoint {
            checkpoint(&handle);
        }
        self.slots
            .entry(subagent_type.value.clone())
            .or_default()
            .push(PooledEntry {
                handle,
                parked_at: now,
            });
        PushOutcome { parked: true }
    }

    /// Drops every entry parked for longer than the grace period.
    pub(crate) fn evict_expired(&mut self, now: Instant) {
        let grace = self.eviction_grace;
        self.slots.retain(|_, entries| {
            entries.retain(|entry| !is_stale(entry, now, grace));
            !entries.is_empty()
        });
    }

    /// Removes the warm entry whose handle has thread id `id`. Returns `true` when one was
    /// removed. Used when a parked id is opened as a fresh thread (DD-13).
    pub(crate) fn remove_id(&mut self, id: &str) -> bool {
        let mut removed = false;
        self.slots.retain(|_, entries| {
            let before = entries.len();
            entries.retain(|entry| entry.handle.id().value != id);
            removed |= entries.len() != before;
            !entries.is_empty()
        });
        removed
    }

    /// Number of parked handles across all subagent types.
    pub(crate) fn len(&self) -> usize {
        self.slots.values().map(Vec::len).sum()
    }

    /// Returns `true` when no handle is parked.
    #[cfg(test)]
    pub(crate) fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// Number of successful pops (warm reuses) since creation.
    #[cfg(test)]
    pub(crate) fn hits(&self) -> u64 {
        self.hits
    }
}

/// Returns `true` when `entry` has been parked strictly longer than `grace`.
fn is_stale(entry: &PooledEntry, now: Instant, grace: Duration) -> bool {
    now.saturating_duration_since(entry.parked_at) > grace
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;
    use std::time::{Duration, Instant};

    use gsd_proto::gsd::v1::{ChatThreadId, SubagentType};

    use super::{CheckpointHook, WarmPool};
    use crate::handle_registry::ChatThreadHandle;

    fn handle(thread: &str, subagent: &str) -> ChatThreadHandle {
        ChatThreadHandle::new(
            ChatThreadId {
                value: thread.to_string(),
            },
            kind(subagent),
        )
    }

    fn kind(value: &str) -> SubagentType {
        SubagentType {
            value: value.to_string(),
        }
    }

    #[test]
    fn reuse_hit_returns_pooled_entry() {
        let t0 = Instant::now();
        let later = t0 + Duration::from_secs(1);
        let mut pool = WarmPool::new(4, Duration::from_secs(30));
        assert!(pool.push(&kind("coder"), handle("t1", "coder"), t0).parked);
        assert!(pool.push(&kind("coder"), handle("t2", "coder"), t0).parked);

        let reused = pool.pop(&kind("coder"), later);
        assert_eq!(reused.as_ref().map(|h| h.id().value.as_str()), Some("t2"));
        assert_eq!(pool.hits(), 1);
        assert_eq!(pool.len(), 1);

        let second = pool.pop(&kind("coder"), later);
        assert_eq!(second.as_ref().map(|h| h.id().value.as_str()), Some("t1"));
        assert_eq!(pool.hits(), 2);

        assert!(pool.pop(&kind("coder"), later).is_none());
        assert_eq!(pool.hits(), 2);
    }

    #[test]
    fn pop_is_keyed_per_subagent_type() {
        let t0 = Instant::now();
        let mut pool = WarmPool::new(4, Duration::from_secs(30));
        assert!(pool.push(&kind("coder"), handle("t1", "coder"), t0).parked);
        assert!(pool.pop(&kind("planner"), t0).is_none());
        assert_eq!(pool.hits(), 0);
        assert!(pool.pop(&kind("coder"), t0).is_some());
    }

    #[test]
    fn eviction_removes_entries_older_than_grace() {
        let grace = Duration::from_secs(10);
        let t0 = Instant::now();
        let mut pool = WarmPool::new(4, grace);
        assert!(pool.push(&kind("coder"), handle("old", "coder"), t0).parked);
        assert!(
            pool.push(
                &kind("planner"),
                handle("young", "planner"),
                t0 + Duration::from_secs(8)
            )
            .parked
        );

        // Exactly at the grace boundary the entry is still live.
        pool.evict_expired(t0 + grace);
        assert_eq!(pool.len(), 2);
        // Past the boundary only the entry parked at t0 is stale.
        pool.evict_expired(t0 + Duration::from_secs(11));
        assert_eq!(pool.len(), 1);
        assert!(pool
            .pop(&kind("coder"), t0 + Duration::from_secs(11))
            .is_none());
        assert!(pool
            .pop(&kind("planner"), t0 + Duration::from_secs(11))
            .is_some());
    }

    #[test]
    fn pop_never_returns_stale_entry() {
        let t0 = Instant::now();
        let mut pool = WarmPool::new(4, Duration::from_secs(10));
        assert!(pool.push(&kind("coder"), handle("t1", "coder"), t0).parked);
        assert!(pool
            .pop(&kind("coder"), t0 + Duration::from_secs(11))
            .is_none());
        assert_eq!(pool.hits(), 0);
        assert!(pool.is_empty());
    }

    #[test]
    fn push_rejects_when_full_and_reclaims_stale_slots() {
        let t0 = Instant::now();
        let mut pool = WarmPool::new(1, Duration::from_secs(10));
        assert!(pool.push(&kind("coder"), handle("t1", "coder"), t0).parked);
        assert!(!pool.push(&kind("coder"), handle("t2", "coder"), t0).parked);
        let outcome = pool.push(
            &kind("coder"),
            handle("t3", "coder"),
            t0 + Duration::from_secs(11),
        );
        assert!(outcome.parked);
        assert_eq!(pool.len(), 1);
    }

    #[test]
    fn rejected_push_leaves_the_pool_unchanged() {
        let t0 = Instant::now();
        let mut pool = WarmPool::new(1, Duration::from_secs(10));
        assert!(pool.push(&kind("coder"), handle("t1", "coder"), t0).parked);
        let outcome = pool.push(&kind("coder"), handle("t2", "coder"), t0);
        assert!(!outcome.parked);
        assert_eq!(pool.len(), 1);
    }

    #[test]
    fn remove_id_drops_only_the_named_entry() {
        let t0 = Instant::now();
        let mut pool = WarmPool::new(4, Duration::from_secs(30));
        assert!(pool.push(&kind("coder"), handle("t1", "coder"), t0).parked);
        assert!(pool.push(&kind("coder"), handle("t2", "coder"), t0).parked);
        assert!(!pool.remove_id("missing"));
        assert!(pool.remove_id("t1"));
        assert!(!pool.remove_id("t1"));
        assert_eq!(pool.len(), 1);
        let left = pool.pop(&kind("coder"), t0);
        assert_eq!(left.as_ref().map(|h| h.id().value.as_str()), Some("t2"));
    }

    #[test]
    fn checkpoint_runs_once_per_parked_handle() {
        let calls = Arc::new(AtomicUsize::new(0));
        let hook_calls = Arc::clone(&calls);
        let hook: CheckpointHook = Box::new(move |_handle: &ChatThreadHandle| {
            hook_calls.fetch_add(1, Ordering::SeqCst);
        });
        let t0 = Instant::now();
        let mut pool = WarmPool::new(1, Duration::from_secs(10)).with_checkpoint(hook);
        assert!(pool.push(&kind("coder"), handle("t1", "coder"), t0).parked);
        // A rejected push parks nothing, so the hook must not run for it.
        assert!(!pool.push(&kind("coder"), handle("t2", "coder"), t0).parked);
        assert_eq!(calls.load(Ordering::SeqCst), 1);
    }
}
