//! Chat-thread handle issuance and the capacity-bounded handle registry.

use std::sync::atomic::{AtomicUsize, Ordering};

use dashmap::mapref::entry::Entry;
use dashmap::DashMap;
use gsd_proto::gsd::v1::{ChatThreadId, SubagentType};

use crate::{PoolConfig, PoolError, PoolResult};

/// Handle to a chat thread owned by the pool.
#[derive(Debug, Clone)]
pub struct ChatThreadHandle {
    id: ChatThreadId,
    subagent_type: SubagentType,
}

impl ChatThreadHandle {
    /// Creates a handle for thread `id` running `subagent_type`.
    pub fn new(id: ChatThreadId, subagent_type: SubagentType) -> Self {
        Self { id, subagent_type }
    }

    /// Returns the thread id.
    pub fn id(&self) -> &ChatThreadId {
        &self.id
    }

    /// Returns the subagent type.
    pub fn subagent_type(&self) -> &SubagentType {
        &self.subagent_type
    }
}

/// `DashMap`-backed registry of live chat-thread handles, bounded by a fixed capacity.
///
/// Slot accounting uses an atomic reservation, so concurrent `register` calls never
/// admit more than `capacity` handles.
#[derive(Debug)]
pub struct HandleRegistry {
    handles: DashMap<String, ChatThreadHandle>,
    /// Slots claimed by successful registrations (includes in-flight ones).
    reserved: AtomicUsize,
    capacity: usize,
}

impl HandleRegistry {
    /// Creates an empty registry that holds at most `capacity` handles.
    pub fn new(capacity: usize) -> Self {
        Self {
            handles: DashMap::new(),
            reserved: AtomicUsize::new(0),
            capacity,
        }
    }

    /// Creates an empty registry sized by `config.max_threads`.
    pub fn from_config(config: &PoolConfig) -> Self {
        Self::new(config.max_threads)
    }

    /// Maximum number of simultaneously registered handles.
    pub fn capacity(&self) -> usize {
        self.capacity
    }

    /// Number of currently registered handles.
    pub fn len(&self) -> usize {
        self.handles.len()
    }

    /// Returns `true` when no handle is registered.
    pub fn is_empty(&self) -> bool {
        self.handles.is_empty()
    }

    /// Registers a new handle for thread `id`.
    ///
    /// Returns `PoolError::Exhausted` when the registry is at capacity (E-CAPACITY) and
    /// `PoolError::AlreadyExists` when `id` is already registered.
    pub fn register(
        &self,
        id: ChatThreadId,
        subagent_type: SubagentType,
    ) -> PoolResult<ChatThreadHandle> {
        self.reserve()?;
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
    pub fn release(&self, id: &str) -> PoolResult<ChatThreadHandle> {
        match self.handles.remove(id) {
            Some((_, handle)) => {
                self.reserved.fetch_sub(1, Ordering::AcqRel);
                Ok(handle)
            }
            None => Err(PoolError::NotFound(id.to_string())),
        }
    }

    /// Returns a clone of the handle for thread `id`, if registered.
    pub fn get(&self, id: &str) -> Option<ChatThreadHandle> {
        self.handles.get(id).map(|entry| entry.value().clone())
    }

    /// Atomically claims one slot, failing when the capacity is reached.
    fn reserve(&self) -> PoolResult<()> {
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
        let registry = HandleRegistry::from_config(&PoolConfig::default());
        assert_eq!(registry.capacity(), 30);
        assert!(registry.is_empty());
    }

    #[test]
    fn thirty_first_registration_is_exhausted() -> PoolResult<()> {
        let registry = HandleRegistry::from_config(&PoolConfig::default());
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
        let registry = HandleRegistry::from_config(&PoolConfig::default());
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
