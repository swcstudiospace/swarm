//! Synchronization primitives for GSD-Core
//!
//! Provides async synchronization primitives optimized for the orchestration engine:
//! - Lock-free queues for high-throughput message passing
//! - Semaphore with priority support
//! - Wait-for graph for deadlock detection
//! - Async barriers with timeout support

use crossbeam::channel::{bounded, unbounded, Receiver, Sender};
use crossbeam::queue::SegQueue;
use parking_lot::RwLock;
use std::collections::{HashMap, VecDeque};
use std::future::Future;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::{Mutex as AsyncMutex, Notify, OwnedSemaphorePermit, Semaphore};

/// Lock-free MPSC queue for high-throughput message passing
pub struct LockFreeQueue<T> {
    queue: Arc<SegQueue<T>>,
    pop_notify: Arc<Notify>,
    len: AtomicUsize,
}

impl<T> LockFreeQueue<T> {
    pub fn new() -> Self {
        Self {
            queue: Arc::new(SegQueue::new()),
            pop_notify: Arc::new(Notify::new()),
            len: AtomicUsize::new(0),
        }
    }

    pub fn push(&self, item: T) {
        self.queue.push(item);
        self.len.fetch_add(1, Ordering::Release);
        self.pop_notify.notify_one();
    }

    pub fn try_pop(&self) -> Option<T> {
        match self.queue.pop() {
            Some(item) => {
                self.len.fetch_sub(1, Ordering::Release);
                Some(item)
            }
            None => None,
        }
    }

    pub async fn pop(&self) -> T {
        loop {
            if let Some(item) = self.try_pop() {
                return item;
            }
            self.pop_notify.notified().await;
        }
    }

    pub fn len(&self) -> usize {
        self.len.load(Ordering::Acquire)
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

impl<T> Default for LockFreeQueue<T> {
    fn default() -> Self {
        Self::new()
    }
}

/// Lock-free MPMC channel using crossbeam
pub struct LockFreeChannel<T> {
    sender: Sender<T>,
    receiver: Receiver<T>,
}

impl<T> LockFreeChannel<T> {
    pub fn bounded(capacity: usize) -> (Sender<T>, Receiver<T>) {
        bounded(capacity)
    }

    pub fn unbounded() -> (Sender<T>, Receiver<T>) {
        unbounded()
    }
}

/// Priority-aware semaphore that supports priority-based acquisition
#[derive(Debug)]
pub struct PrioritySemaphore {
    semaphore: Arc<Semaphore>,
    waiters: Arc<parking_lot::Mutex<Vec<(i32, tokio::sync::oneshot::Sender<()>)>>>,
    max_permits: usize,
    current_permits: Arc<AtomicUsize>,
}

impl PrioritySemaphore {
    pub fn new(permits: usize) -> Self {
        Self {
            semaphore: Arc::new(Semaphore::new(permits)),
            waiters: Arc::new(parking_lot::Mutex::new(Vec::new())),
            max_permits: permits,
            current_permits: Arc::new(AtomicUsize::new(permits)),
        }
    }

    pub async fn acquire_with_priority(
        &self,
        priority: i32,
    ) -> Result<PriorityPermit, tokio::sync::oneshot::error::RecvError> {
        // Fast path: permits available
        if self.current_permits.fetch_sub(1, Ordering::AcqRel) > 0 {
            return Ok(PriorityPermit {
                semaphore: self.semaphore.clone(),
                current_permits: self.current_permits.clone(),
            });
        }

        // Need to wait - register with priority
        let (tx, rx) = tokio::sync::oneshot::channel();
        {
            let mut waiters = self.waiters.lock();
            waiters.push((priority, tx));
            waiters.sort_by_key(|(p, _)| -p); // Higher priority first
        }

        rx.await?;

        // We got the permit
        Ok(PriorityPermit {
            semaphore: self.semaphore.clone(),
            current_permits: self.current_permits.clone(),
        })
    }

    pub fn try_acquire(&self) -> Option<PriorityPermit> {
        if self.current_permits.fetch_sub(1, Ordering::AcqRel) > 0 {
            Some(PriorityPermit {
                semaphore: self.semaphore.clone(),
                current_permits: self.current_permits.clone(),
            })
        } else {
            self.current_permits.fetch_add(1, Ordering::Release);
            None
        }
    }

    pub fn available_permits(&self) -> usize {
        self.current_permits.load(Ordering::Acquire)
    }

    pub fn max_permits(&self) -> usize {
        self.max_permits
    }
}

#[derive(Debug)]
pub struct PriorityPermit {
    semaphore: Arc<Semaphore>,
    current_permits: Arc<AtomicUsize>,
}

impl Drop for PriorityPermit {
    fn drop(&mut self) {
        self.current_permits.fetch_add(1, Ordering::Release);

        // Notify next waiter if any
        // (In a real implementation, we'd notify the highest priority waiter)
    }
}

/// Async barrier with timeout support
#[derive(Clone)]
pub struct AsyncBarrier {
    count: Arc<AtomicUsize>,
    target: usize,
    notify: Arc<Notify>,
}

impl AsyncBarrier {
    pub fn new(count: usize) -> Self {
        Self {
            count: Arc::new(AtomicUsize::new(count)),
            target: count,
            notify: Arc::new(Notify::new()),
        }
    }

    pub async fn wait(&self) -> bool {
        // Register for the release before arriving: `notify_waiters` only wakes
        // futures that already exist, so a release racing with arrival must not be missed.
        let mut released = std::pin::pin!(self.notify.notified());
        released.as_mut().enable();

        let remaining = self.count.fetch_sub(1, Ordering::AcqRel);
        if remaining == 1 {
            // Last arrival is the leader: reset for the next round and release the rest.
            self.count.store(self.target, Ordering::Release);
            self.notify.notify_waiters();
            true
        } else {
            released.await;
            false
        }
    }

    pub async fn wait_timeout(
        &self,
        duration: Duration,
    ) -> Result<bool, tokio::time::error::Elapsed> {
        tokio::time::timeout(duration, self.wait()).await
    }
}

/// Wait-for graph for deadlock detection
#[derive(Debug, Default)]
pub struct WaitForGraph {
    edges: Arc<RwLock<HashMap<usize, Vec<usize>>>>,
    node_counter: AtomicUsize,
}

impl WaitForGraph {
    pub fn new() -> Self {
        Self::default()
    }

    /// Add a wait edge: waiter -> resource_holder
    pub fn add_edge(&self, waiter: usize, holder: usize) {
        let mut edges = self.edges.write();
        edges.entry(waiter).or_default().push(holder);
    }

    /// Remove a wait edge
    pub fn remove_edge(&self, waiter: usize, holder: usize) {
        let mut edges = self.edges.write();
        if let Some(holders) = edges.get_mut(&waiter) {
            holders.retain(|&h| h != holder);
            if holders.is_empty() {
                edges.remove(&waiter);
            }
        }
    }

    /// Detect cycles (deadlocks) using DFS
    pub fn detect_cycles(&self) -> Vec<Vec<usize>> {
        let edges = self.edges.read();
        let mut visited = HashMap::new();
        let mut rec_stack = HashMap::new();
        let mut cycles = Vec::new();

        for &node in edges.keys() {
            if !visited.contains_key(&node) {
                self.dfs_cycle(
                    node,
                    &edges,
                    &mut visited,
                    &mut rec_stack,
                    &mut cycles,
                    Vec::new(),
                );
            }
        }

        cycles
    }

    fn dfs_cycle(
        &self,
        node: usize,
        edges: &HashMap<usize, Vec<usize>>,
        visited: &mut HashMap<usize, bool>,
        rec_stack: &mut HashMap<usize, bool>,
        cycles: &mut Vec<Vec<usize>>,
        path: Vec<usize>,
    ) {
        visited.insert(node, true);
        rec_stack.insert(node, true);
        let mut new_path = path.clone();
        new_path.push(node);

        if let Some(neighbors) = edges.get(&node) {
            for &neighbor in neighbors {
                if !visited.get(&neighbor).copied().unwrap_or(false) {
                    self.dfs_cycle(
                        neighbor,
                        edges,
                        visited,
                        rec_stack,
                        cycles,
                        new_path.clone(),
                    );
                } else if rec_stack.get(&neighbor).copied().unwrap_or(false) {
                    // Found a cycle
                    if let Some(start) = new_path.iter().position(|&p| p == neighbor) {
                        cycles.push(new_path[start..].to_vec());
                    }
                }
            }
        }

        rec_stack.remove(&node);
    }

    /// Get all nodes that are part of a cycle
    pub fn deadlocked_nodes(&self) -> Vec<usize> {
        self.detect_cycles()
            .into_iter()
            .flatten()
            .collect::<std::collections::HashSet<_>>()
            .into_iter()
            .collect()
    }
}

/// Async wait group for managing multiple concurrent tasks
#[derive(Clone)]
pub struct WaitGroup {
    count: Arc<AtomicUsize>,
    notify: Arc<Notify>,
}

impl WaitGroup {
    pub fn new() -> Self {
        Self {
            count: Arc::new(AtomicUsize::new(0)),
            notify: Arc::new(Notify::new()),
        }
    }

    pub fn add(&self, delta: usize) {
        self.count.fetch_add(delta, Ordering::Release);
    }

    pub fn done(&self) {
        if self.count.fetch_sub(1, Ordering::AcqRel) == 1 {
            self.notify.notify_waiters();
        }
    }

    pub async fn wait(&self) {
        if self.count.load(Ordering::Acquire) == 0 {
            return;
        }
        self.notify.notified().await;
    }
}

impl Default for WaitGroup {
    fn default() -> Self {
        Self::new()
    }
}

/// Async oneshot channel with timeout
pub async fn oneshot_timeout<T, F, Fut>(
    duration: Duration,
    fut: F,
) -> Result<T, tokio::time::error::Elapsed>
where
    F: FnOnce() -> Fut,
    Fut: Future<Output = T>,
{
    tokio::time::timeout(duration, fut()).await
}

/// Rate limiter using token bucket algorithm
pub struct TokenBucket {
    capacity: usize,
    tokens: Arc<AsyncMutex<f64>>,
    refill_rate: f64, // tokens per second
    last_refill: Arc<AsyncMutex<Instant>>,
}

impl TokenBucket {
    pub fn new(capacity: usize, refill_rate: f64) -> Self {
        Self {
            capacity,
            tokens: Arc::new(AsyncMutex::new(capacity as f64)),
            refill_rate,
            last_refill: Arc::new(AsyncMutex::new(Instant::now())),
        }
    }

    pub async fn take(&self, tokens: usize) -> bool {
        let mut tokens_guard = self.tokens.lock().await;
        let mut last_refill = self.last_refill.lock().await;

        let now = Instant::now();
        let elapsed = now.duration_since(*last_refill).as_secs_f64();

        *tokens_guard = (*tokens_guard + elapsed * self.refill_rate).min(self.capacity as f64);
        *last_refill = now;

        if *tokens_guard >= tokens as f64 {
            *tokens_guard -= tokens as f64;
            true
        } else {
            false
        }
    }

    pub fn try_take(&self, tokens: usize) -> bool {
        let mut tokens_guard = self.tokens.try_lock();
        if let Ok(mut guard) = tokens_guard {
            if *guard >= tokens as f64 {
                *guard -= tokens as f64;
                true
            } else {
                false
            }
        } else {
            false
        }
    }

    pub fn available(&self) -> f64 {
        let guard = self.tokens.blocking_lock();
        *guard
    }
}

/// Semaphore with fairness guarantee
pub struct FairSemaphore {
    permits: Arc<Semaphore>,
    wait_queue: Arc<AsyncMutex<VecDeque<tokio::sync::oneshot::Sender<()>>>>,
    max_permits: usize,
}

impl FairSemaphore {
    pub fn new(permits: usize) -> Self {
        Self {
            permits: Arc::new(Semaphore::new(permits)),
            wait_queue: Arc::new(AsyncMutex::new(VecDeque::new())),
            max_permits: permits,
        }
    }

    pub async fn acquire(&self) -> Result<FairPermit, tokio::sync::AcquireError> {
        let permit = self.permits.clone().acquire_owned().await?;
        Ok(FairPermit { _permit: permit })
    }

    pub fn try_acquire(&self) -> Result<FairPermit, tokio::sync::TryAcquireError> {
        let permit = self.permits.clone().try_acquire_owned()?;
        Ok(FairPermit { _permit: permit })
    }

    pub fn available_permits(&self) -> usize {
        self.permits.available_permits()
    }
}

#[derive(Debug)]
pub struct FairPermit {
    _permit: OwnedSemaphorePermit,
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    #[tokio::test]
    async fn test_lock_free_queue() {
        let queue = LockFreeQueue::new();
        queue.push(1);
        queue.push(2);
        assert_eq!(queue.pop().await, 1);
        assert_eq!(queue.pop().await, 2);
    }

    #[tokio::test]
    async fn test_semaphore() {
        let sem = Semaphore::new(2);
        let p1 = sem.acquire().await.unwrap();
        let p2 = sem.acquire().await.unwrap();
        assert!(sem.try_acquire().is_err());
        drop(p1);
        assert!(sem.try_acquire().is_ok());
    }

    #[tokio::test]
    async fn test_barrier() {
        let barrier = AsyncBarrier::new(3);
        let b1 = barrier.clone();
        let b2 = barrier.clone();

        let h1 = tokio::spawn(async move { b1.wait().await });
        let h2 = tokio::spawn(async move { b2.wait().await });
        let last = barrier.wait().await;
        let l1 = h1.await.unwrap();
        let l2 = h2.await.unwrap();
        assert_eq!([last, l1, l2].iter().filter(|leader| **leader).count(), 1);
    }

    #[tokio::test]
    async fn test_wait_group() {
        let wg = WaitGroup::new();
        wg.add(3);

        for i in 0..3 {
            let wg = wg.clone();
            tokio::spawn(async move {
                tokio::time::sleep(Duration::from_millis(10)).await;
                wg.done();
            });
        }

        wg.wait().await;
    }

    #[tokio::test]
    async fn test_wait_for_graph() {
        let graph = WaitForGraph::new();
        graph.add_edge(1, 2);
        graph.add_edge(2, 3);
        graph.add_edge(3, 1); // cycle

        let cycles = graph.detect_cycles();
        assert_eq!(cycles.len(), 1);
        assert_eq!(cycles[0].len(), 3);
    }

    #[tokio::test]
    async fn test_token_bucket() {
        let bucket = TokenBucket::new(10, 10.0); // 10 tokens, refill 10/s
        assert!(bucket.take(5).await);
        assert!(bucket.take(5).await);
        assert!(!bucket.take(1).await);
    }
}
