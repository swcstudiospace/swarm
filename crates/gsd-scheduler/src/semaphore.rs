//! Named resource-class semaphores that bound concurrent work.

use std::sync::Arc;

use dashmap::DashMap;
use tokio::sync::Semaphore;

/// Permits in the global pool shared by all scheduled work.
const GLOBAL_PERMITS: usize = 30;

/// Holds the global permit pool and one permit pool per resource class.
pub struct SemaphoreManager {
    global: Semaphore,
    resource_semaphores: DashMap<String, Arc<Semaphore>>,
}

impl Default for SemaphoreManager {
    fn default() -> Self {
        Self::new()
    }
}

impl SemaphoreManager {
    /// Creates a manager with the global pool at its default size.
    pub fn new() -> Self {
        Self {
            global: Semaphore::new(GLOBAL_PERMITS),
            resource_semaphores: DashMap::new(),
        }
    }

    /// Returns the global permit pool.
    pub fn global(&self) -> &Semaphore {
        &self.global
    }

    /// Returns the semaphore for resource class `name`, creating it with `permits`
    /// permits on first use. Later calls return the existing semaphore unchanged.
    pub fn get_or_create(&self, name: &str, permits: usize) -> Arc<Semaphore> {
        let entry = self
            .resource_semaphores
            .entry(name.to_string())
            .or_insert_with(|| Arc::new(Semaphore::new(permits)));
        Arc::clone(entry.value())
    }
}
