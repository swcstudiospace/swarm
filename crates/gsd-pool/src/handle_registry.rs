//! Chat-thread handle issuance and the capacity-bounded handle registry.
//!
//! Crate-private machinery (D-1). `HandleRegistry` is the sole admission registry for live
//! threads. A parked thread leaves it, so warm entries never count against `max_threads`.

use std::sync::atomic::{AtomicUsize, Ordering};

use dashmap::mapref::entry::Entry;
use dashmap::DashMap;
use gsd_proto::gsd::v1::{ChatThreadId, SubagentType};

use crate::{PoolError, PoolResult};

/// Handle to a chat thread owned by the pool. Crate-private (DEC-7).
#[derive(Debug, Clone)]
pub(crate) struct ChatThreadHandle {
    id: ChatThreadId,
    subagent_type: SubagentType,
}

impl ChatThreadHandle {
    /// Creates a handle for thread `id` running `subagent_type`.
    pub(crate) fn new(id: ChatThreadId, subagent_type: SubagentType) -> Self {
        Self { id, subagent_type }
    }

    /// Returns the thread id.
    pub(crate) fn id(&self) -> &ChatThreadId {
        &self.id
    }

    /// Returns the subagent type.
    pub(crate) fn subagent_type(&self) -> &SubagentType {
        &self.subagent_type
    }
}

/// `DashMap`-backed registry of live chat-thread handles, bounded by a fixed capacity.
///
/// Slot accounting uses an atomic reservation, so concurrent registrations never admit more
/// than `capacity` handles.
#[derive(Debug)]
pub(crate) struct HandleRegistry {
    handles: DashMap<String, ChatThreadHandle>,
    /// Slots claimed by successful reservations (includes in-flight ones).
    reserved: AtomicUsize,
    capacity: usize,
}

impl HandleRegistry {
    /// Creates an empty registry that holds at most `capacity` handles.
    pub(crate) fn new(capacity: usize) -> Self {
        Self {
            handles: DashMap::new(),
            reserved: AtomicUsize::new(0),
            capacity,
        }
    }

    /// Maximum number of simultaneously registered handles.
    #[cfg(test)]
    pub(crate) fn capacity(&self) -> usize {
        self.capacity
    }

    /// Number of currently registered handles.
    #[cfg(test)]
    pub(crate) fn len(&self) -> usize {
        self.handles.len()
    }

    /// Returns `true` when no handle is registered.
    #[cfg(test)]
    pub(crate) fn is_empty(&self) -> bool {
        self.handles.is_empty()
    }

    /// Reserves one slot and registers a handle for thread `id` in it.
    ///
    /// Returns `PoolError::Exhausted` when the registry is at capacity and
    /// `PoolError::AlreadyExists` when `id` is already registered. Neither failure consumes a slot.
    #[cfg(test)]
    pub(crate) fn register(
        &self,
        id: ChatThreadId,
        subagent_type: SubagentType,
    ) -> PoolResult<ChatThreadHandle> {
        self.reserve()?;
        self.register_reserved(id, subagent_type)
    }

    /// Registers a handle for `id` in a slot already claimed by [`HandleRegistry::reserve`].
    ///
    /// The slot is consumed on success. When `id` is already registered the slot is returned
    /// to the registry and `PoolError::AlreadyExists` is returned.
    pub(crate) fn register_reserved(
        &self,
        id: ChatThreadId,
        subagent_type: SubagentType,
    ) -> PoolResult<ChatThreadHandle> {
        match self.handles.entry(id.value.clone()) {
            Entry::Occupied(_) => {
                self.reserved.fetch_sub(1, Ordering::AcqRel);
                Err(PoolError::AlreadyExists(id.value))
            }
            Entry::Vacant(slot) => {
                let handle = ChatThreadHandle::new(id, subagent_type);
                slot.insert(handle.clone());
                Ok(handle)
            }
        }
    }

    /// Removes the handle for thread `id`, freeing its slot.
    ///
    /// Returns `PoolError::NotFound` when no such handle is registered.
    pub(crate) fn release(&self, id: &str) -> PoolResult<ChatThreadHandle> {
        match self.handles.remove(id) {
            Some((_, handle)) => {
                self.reserved.fetch_sub(1, Ordering::AcqRel);
                Ok(handle)
            }
            None => Err(PoolError::NotFound(id.to_string())),
        }
    }

    /// Returns a clone of the handle for thread `id`, if registered.
    pub(crate) fn get(&self, id: &str) -> Option<ChatThreadHandle> {
        self.handles.get(id).map(|entry| entry.value().clone())
    }

    /// Atomically claims one slot, failing when the capacity is reached.
    ///
    /// The claim must be paired with [`HandleRegistry::register_reserved`]; a claim that is
    /// never registered is a leak of one slot.
    pub(crate) fn reserve(&self) -> PoolResult<()> {
        let mut current = self.reserved.load(Ordering::Acquire);
        loop {
            if current >= self.capacity {
                return Err(PoolError::Exhausted(format!(
                    "handle registry full: {current} of {} slots in use",
                    self.capacity
                )));
            }
            match self.reserved.compare_exchange_weak(
                current,
                current + 1,
                Ordering::AcqRel,
                Ordering::Acquire,
            ) {
                Ok(_) => return Ok(()),
                Err(actual) => current = actual,
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::PoolConfig;

    fn chat_id(value: &str) -> ChatThreadId {
        ChatThreadId {
            value: value.to_string(),
        }
    }

    fn subagent(value: &str) -> SubagentType {
        SubagentType {
            value: value.to_string(),
        }
    }

    #[test]
    fn default_capacity_is_pool_max_threads_thirty() {
        let registry = HandleRegistry::new(PoolConfig::default().max_threads);
        assert_eq!(registry.capacity(), 30);
        assert!(registry.is_empty());
    }

    #[test]
    fn thirty_first_registration_is_exhausted() -> PoolResult<()> {
        let registry = HandleRegistry::new(PoolConfig::default().max_threads);
        for i in 0..30 {
            registry.register(chat_id(&format!("t{i}")), subagent("worker"))?;
        }
        assert_eq!(registry.len(), 30);
        let refused = registry.register(chat_id("t30"), subagent("worker"));
        assert!(matches!(refused, Err(PoolError::Exhausted(_))));
        assert_eq!(registry.len(), 30);
        Ok(())
    }

    #[test]
    fn release_frees_a_slot_for_reuse() -> PoolResult<()> {
        let registry = HandleRegistry::new(1);
        registry.register(chat_id("a"), subagent("worker"))?;
        assert!(matches!(
            registry.register(chat_id("b"), subagent("worker")),
            Err(PoolError::Exhausted(_))
        ));
        let released = registry.release("a")?;
        assert_eq!(released.id().value, "a");
        registry.register(chat_id("b"), subagent("worker"))?;
        assert!(registry.get("b").is_some());
        assert!(registry.get("a").is_none());
        Ok(())
    }

    #[test]
    fn duplicate_id_is_rejected_without_consuming_a_slot() -> PoolResult<()> {
        let registry = HandleRegistry::new(2);
        registry.register(chat_id("a"), subagent("worker"))?;
        assert!(matches!(
            registry.register(chat_id("a"), subagent("worker")),
            Err(PoolError::AlreadyExists(_))
        ));
        registry.register(chat_id("b"), subagent("worker"))?;
        assert_eq!(registry.len(), 2);
        Ok(())
    }

    #[test]
    fn reserved_slot_is_returned_when_its_id_is_taken() -> PoolResult<()> {
        let registry = HandleRegistry::new(2);
        registry.register(chat_id("a"), subagent("worker"))?;
        registry.reserve()?;
        assert!(matches!(
            registry.register_reserved(chat_id("a"), subagent("worker")),
            Err(PoolError::AlreadyExists(_))
        ));
        // The failed registration returned its slot, so one more reservation fits.
        registry.reserve()?;
        registry.register_reserved(chat_id("b"), subagent("worker"))?;
        assert_eq!(registry.len(), 2);
        assert!(matches!(registry.reserve(), Err(PoolError::Exhausted(_))));
        Ok(())
    }

    #[test]
    fn release_of_unknown_id_is_not_found() {
        let registry = HandleRegistry::new(1);
        assert!(matches!(
            registry.release("missing"),
            Err(PoolError::NotFound(_))
        ));
    }

    #[test]
    fn concurrent_registration_never_exceeds_capacity() -> Result<(), Box<dyn std::error::Error>> {
        const THREADS: usize = 8;
        const ATTEMPTS_PER_THREAD: usize = 10;
        let registry = HandleRegistry::new(PoolConfig::default().max_threads);
        let shared = &registry;
        let accepted: Result<usize, &str> = std::thread::scope(|scope| {
            let workers: Vec<_> = (0..THREADS)
                .map(|t| {
                    scope.spawn(move || {
                        (0..ATTEMPTS_PER_THREAD)
                            .filter(|i| {
                                shared
                                    .register(chat_id(&format!("t{t}-{i}")), subagent("worker"))
                                    .is_ok()
                            })
                            .count()
                    })
                })
                .collect();
            let mut total = 0;
            for worker in workers {
                total += worker.join().map_err(|_| "registration thread failed")?;
            }
            Ok(total)
        });
        let accepted = accepted?;
        assert_eq!(accepted, 30);
        assert_eq!(registry.len(), 30);
        Ok(())
    }
}
