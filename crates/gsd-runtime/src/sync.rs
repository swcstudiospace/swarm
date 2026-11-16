//! Synchronization primitives for GSD-Core
//!
//! Provides async synchronization primitives optimized for the orchestration engine:
//! - Lock-free queues for high-throughput message passing
//! - Semaphore with priority support
//! - Wait-for graph for deadlock detection
//! - Async barriers with timeout support

use crate::clock::Clock;
use crossbeam::channel::{bounded, unbounded, Receiver, Sender};
use crossbeam::queue::SegQueue;
use parking_lot::RwLock;
use std::collections::{HashMap, VecDeque};
use std::future::Future;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::{oneshot, Barrier, Notify, OwnedSemaphorePermit, Semaphore};

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
    _marker: std::marker::PhantomData<T>,
}

impl<T> LockFreeChannel<T> {
    pub fn bounded(capacity: usize) -> (Sender<T>, Receiver<T>) {
        bounded(capacity)
    }

    pub fn unbounded() -> (Sender<T>, Receiver<T>) {
        unbounded()
    }
}

/// Priority-aware semaphore that supports priority-based acquisition.
///
/// All permit accounting happens under one mutex: the available count and the
/// ordered waiter queue. A release hands the permit directly to the highest
/// priority live waiter (FIFO among equal priorities), so `available` only grows
/// when no queued waiter can take the permit.
#[derive(Debug)]
pub struct PrioritySemaphore {
    shared: Arc<Shared>,
}

#[derive(Debug)]
struct Shared {
    max_permits: usize,
    state: parking_lot::Mutex<SemState>,
}

#[derive(Debug)]
struct SemState {
    available: usize,
    waiters: VecDeque<Waiter>,
    next_seq: u64,
}

#[derive(Debug)]
struct Waiter {
    priority: i32,
    seq: u64,
    tx: oneshot::Sender<()>,
}

impl PrioritySemaphore {
    /// Creates a semaphore with `permits` permits.
    pub fn new(permits: usize) -> Self {
        Self {
            shared: Arc::new(Shared {
                max_permits: permits,
                state: parking_lot::Mutex::new(SemState {
                    available: permits,
                    waiters: VecDeque::new(),
                    next_seq: 0,
                }),
            }),
        }
    }

    /// Acquires a permit, queuing behind higher-priority waiters when none is free.
    ///
    /// The `Err` path is unreachable in this design. The oneshot sender lives only in
    /// the waiter queue. It is either consumed by a hand-off, which sends a value, or
    /// removed by the waiter's drop guard, which runs only after this future is gone.
    /// So the receiver always observes a value.
    pub async fn acquire_with_priority(
        &self,
        priority: i32,
    ) -> Result<PriorityPermit, oneshot::error::RecvError> {
        let (seq, rx) = {
            let mut state = self.shared.state.lock();
            // Invariant: `available > 0` implies the queue is empty, so this never
            // barges ahead of queued waiters.
            if state.available > 0 {
                state.available -= 1;
                return Ok(PriorityPermit {
                    shared: self.shared.clone(),
                });
            }
            let (tx, rx) = oneshot::channel();
            let seq = state.next_seq;
            state.next_seq = state.next_seq.wrapping_add(1);
            state.waiters.push_back(Waiter { priority, seq, tx });
            (seq, rx)
        };

        let mut guard = WaitGuard {
            shared: self.shared.clone(),
            seq,
            rx,
            armed: true,
        };
        (&mut guard.rx).await?;
        guard.armed = false;
        Ok(PriorityPermit {
            shared: self.shared.clone(),
        })
    }

    /// Acquires a permit without waiting. Returns `None` when none is free.
    pub fn try_acquire(&self) -> Option<PriorityPermit> {
        let mut state = self.shared.state.lock();
        if state.available == 0 {
            return None;
        }
        state.available -= 1;
        Some(PriorityPermit {
            shared: self.shared.clone(),
        })
    }

    /// Returns the number of currently free permits.
    pub fn available_permits(&self) -> usize {
        self.shared.state.lock().available
    }

    /// Returns the total number of permits this semaphore was created with.
    pub fn max_permits(&self) -> usize {
        self.shared.max_permits
    }

    /// Returns the number of live waiters currently queued.
    #[cfg(test)]
    fn queued_waiters(&self) -> usize {
        self.shared.state.lock().waiters.len()
    }
}

/// A permit from [`PrioritySemaphore`]; dropping it releases the permit.
#[derive(Debug)]
pub struct PriorityPermit {
    shared: Arc<Shared>,
}

impl Drop for PriorityPermit {
    fn drop(&mut self) {
        release(&self.shared);
    }
}

/// Drop guard for a queued waiter. Removes its queue entry on cancellation, or
/// returns a permit that was handed over before the future was dropped.
struct WaitGuard {
    shared: Arc<Shared>,
    seq: u64,
    rx: oneshot::Receiver<()>,
    armed: bool,
}

impl Drop for WaitGuard {
    fn drop(&mut self) {
        if !self.armed {
            return;
        }
        // The lock is released before `release` is called below.
        let still_queued = {
            let mut state = self.shared.state.lock();
            match state.waiters.iter().position(|w| w.seq == self.seq) {
                Some(idx) => state.waiters.remove(idx).is_some(),
                None => false,
            }
        };
        if !still_queued && self.rx.try_recv().is_ok() {
            release(&self.shared);
        }
    }
}

/// Returns one permit: hands it to the best live waiter, or increments `available`.
fn release(shared: &Shared) {
    let mut state = shared.state.lock();
    while let Some(idx) = next_waiter_index(&state.waiters) {
        if let Some(waiter) = state.waiters.remove(idx) {
            if waiter.tx.send(()).is_ok() {
                return;
            }
        }
    }
    state.available += 1;
}

/// Index of the highest-priority waiter; ties keep the earliest queued entry.
fn next_waiter_index(waiters: &VecDeque<Waiter>) -> Option<usize> {
    let mut best: Option<(usize, i32)> = None;
    for (idx, waiter) in waiters.iter().enumerate() {
        match best {
            Some((_, priority)) if waiter.priority <= priority => {}
            _ => best = Some((idx, waiter.priority)),
        }
    }
    best.map(|(idx, _)| idx)
}

/// Reusable async barrier with timeout support.
///
/// Waiters are released in generations of `count`, with one leader per generation.
///
/// # Cancellation safety
///
/// Like [`tokio::sync::Barrier`], waiting is not cancellation safe. Dropping a wait
/// (including when `wait_timeout` expires) does not roll back an arrival already
/// counted toward the current generation.
#[derive(Clone)]
pub struct AsyncBarrier {
    inner: Arc<Barrier>,
}

impl AsyncBarrier {
    pub fn new(count: usize) -> Self {
        assert!(count > 0, "AsyncBarrier needs at least one participant");
        Self {
            inner: Arc::new(Barrier::new(count)),
        }
    }

    pub async fn wait(&self) -> bool {
        self.inner.wait().await.is_leader()
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
        // Register before re-checking the count: `notify_waiters` wakes only futures that
        // already exist, so a `done()` racing with this check must not be missed.
        let mut released = std::pin::pin!(self.notify.notified());
        released.as_mut().enable();
        if self.count.load(Ordering::Acquire) == 0 {
            return;
        }
        released.await;
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

/// Rate limiter using token bucket algorithm. Refill is measured on the injected clock, so a
/// simulation clock drives it deterministically; a real clock refills by wall time.
pub struct TokenBucket {
    capacity: usize,
    refill_rate: f64, // tokens per second
    clock: Arc<dyn Clock>,
    state: std::sync::Mutex<BucketState>,
}

struct BucketState {
    tokens: f64,
    last_refill_ns: u64,
}

impl TokenBucket {
    pub fn new(capacity: usize, refill_rate: f64, clock: Arc<dyn Clock>) -> Self {
        let last_refill_ns = clock.now_nanos();
        Self {
            capacity,
            refill_rate,
            clock,
            state: std::sync::Mutex::new(BucketState {
                tokens: capacity as f64,
                last_refill_ns,
            }),
        }
    }

    /// Refills for the time elapsed since the last refill, then takes `tokens` if enough remain.
    /// `take` and `try_take` share this path so both refill identically.
    fn take_now(&self, tokens: usize) -> bool {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let now = self.clock.now_nanos();
        let elapsed = Duration::from_nanos(now.saturating_sub(state.last_refill_ns)).as_secs_f64();
        state.tokens = (state.tokens + elapsed * self.refill_rate).min(self.capacity as f64);
        state.last_refill_ns = now;

        if state.tokens >= tokens as f64 {
            state.tokens -= tokens as f64;
            true
        } else {
            false
        }
    }

    pub async fn take(&self, tokens: usize) -> bool {
        self.take_now(tokens)
    }

    pub fn try_take(&self, tokens: usize) -> bool {
        self.take_now(tokens)
    }

    pub fn available(&self) -> f64 {
        let state = self
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        state.tokens
    }
}

/// Semaphore with fairness guarantee
pub struct FairSemaphore {
    permits: Arc<Semaphore>,
}

impl FairSemaphore {
    pub fn new(permits: usize) -> Self {
        Self {
            permits: Arc::new(Semaphore::new(permits)),
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
    use crate::clock::{RealClock, SimClock};
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
        let _p2 = sem.acquire().await.unwrap();
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

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn test_barrier_rounds_survive_arrivals_during_reset() {
        let barrier = AsyncBarrier::new(3);
        for _ in 0..500 {
            let handles: Vec<_> = (0..3u32)
                .map(|spins| {
                    let barrier = barrier.clone();
                    tokio::spawn(async move {
                        for _ in 0..spins {
                            tokio::task::yield_now().await;
                        }
                        barrier.wait_timeout(Duration::from_secs(1)).await
                    })
                })
                .collect();
            let mut leaders = 0;
            for handle in handles {
                let led = handle.await.unwrap().expect("barrier round hung");
                leaders += usize::from(led);
            }
            assert_eq!(leaders, 1);
        }
    }

    #[tokio::test]
    async fn test_wait_group() {
        let wg = WaitGroup::new();
        wg.add(3);

        for _ in 0..3 {
            let wg = wg.clone();
            tokio::spawn(async move {
                tokio::time::sleep(Duration::from_millis(10)).await;
                wg.done();
            });
        }

        wg.wait().await;
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn test_wait_group_wait_is_not_lost_to_done_race() {
        for _ in 0..10_000 {
            let wg = WaitGroup::new();
            wg.add(1);
            let finisher = wg.clone();
            let handle = tokio::spawn(async move { finisher.done() });
            tokio::time::timeout(Duration::from_secs(1), wg.wait())
                .await
                .expect("wait() hung after done() raced it");
            handle.await.unwrap();
        }
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
        let bucket = TokenBucket::new(10, 10.0, Arc::new(RealClock::new())); // 10 tokens, refill 10/s
        assert!(bucket.take(5).await);
        assert!(bucket.take(5).await);
        assert!(!bucket.take(1).await);
    }

    #[tokio::test]
    async fn test_try_take_refills_after_drain() {
        let bucket = TokenBucket::new(1, 1000.0, Arc::new(RealClock::new()));
        assert!(bucket.try_take(1));
        tokio::time::sleep(Duration::from_millis(10)).await;
        assert!(bucket.try_take(1), "try_take must refill like take does");
    }

    #[tokio::test]
    async fn test_token_bucket_refills_on_injected_clock() {
        let clock = Arc::new(SimClock::new());
        let bucket = TokenBucket::new(10, 10.0, clock.clone());
        assert!(bucket.take(5).await);
        assert!(bucket.take(5).await);
        assert!(!bucket.take(1).await);
        clock.advance(Duration::from_millis(100));
        assert!(
            bucket.take(1).await,
            "100 ms at 10 tokens/s refills one token"
        );
    }

    async fn wait_for_queued(sem: &PrioritySemaphore, count: usize) {
        tokio::time::timeout(Duration::from_secs(2), async {
            while sem.queued_waiters() < count {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("waiters were not queued in time");
    }

    #[tokio::test]
    async fn priority_semaphore_wakes_waiter_on_release() {
        let sem = Arc::new(PrioritySemaphore::new(1));
        let holder = sem.acquire_with_priority(0).await.unwrap();
        let waiter = {
            let sem = sem.clone();
            tokio::spawn(async move { sem.acquire_with_priority(0).await.map(|_| ()) })
        };
        wait_for_queued(&sem, 1).await;

        drop(holder);

        let result = tokio::time::timeout(Duration::from_secs(2), waiter)
            .await
            .expect("waiter hung after release")
            .expect("waiter task panicked");
        assert!(result.is_ok());
        assert_eq!(sem.available_permits(), 1);
    }

    #[tokio::test]
    async fn priority_semaphore_serves_highest_priority_first() {
        let sem = Arc::new(PrioritySemaphore::new(1));
        let holder = sem.acquire_with_priority(0).await.unwrap();
        let (order_tx, mut order_rx) = tokio::sync::mpsc::unbounded_channel::<&'static str>();

        let mut handles = Vec::new();
        for (label, priority) in [("low", 0), ("high", 5)] {
            let waiter_sem = sem.clone();
            let order_tx = order_tx.clone();
            handles.push(tokio::spawn(async move {
                let permit = waiter_sem.acquire_with_priority(priority).await.unwrap();
                let _ = order_tx.send(label);
                drop(permit);
            }));
            wait_for_queued(&sem, handles.len()).await;
        }
        drop(order_tx);

        drop(holder);
        let first = tokio::time::timeout(Duration::from_secs(2), order_rx.recv())
            .await
            .expect("first waiter hung");
        let second = tokio::time::timeout(Duration::from_secs(2), order_rx.recv())
            .await
            .expect("second waiter hung");
        assert_eq!(first, Some("high"));
        assert_eq!(second, Some("low"));

        for handle in handles {
            tokio::time::timeout(Duration::from_secs(2), handle)
                .await
                .expect("waiter task hung")
                .expect("waiter task panicked");
        }
        assert_eq!(sem.available_permits(), 1);
    }

    #[tokio::test]
    async fn priority_semaphore_never_exceeds_max_permits() {
        let sem = PrioritySemaphore::new(2);
        let p1 = sem.try_acquire().expect("first permit");
        let p2 = sem.try_acquire().expect("second permit");
        assert!(sem.try_acquire().is_none());
        assert_eq!(sem.available_permits(), 0);

        drop(p1);
        drop(p2);
        assert_eq!(sem.available_permits(), 2);
        assert_eq!(sem.max_permits(), 2);

        let p3 = sem.try_acquire().expect("permit after release");
        let p4 = sem.try_acquire().expect("second permit after release");
        assert!(sem.try_acquire().is_none());
        drop(p3);
        drop(p4);
        assert_eq!(sem.available_permits(), 2);

        // A waiter queued while full must not let try_acquire admit a second holder:
        // the wrapped counter of the old slow path did exactly that.
        let limited = Arc::new(PrioritySemaphore::new(1));
        let holder = limited.acquire_with_priority(0).await.expect("holder");
        let queued = {
            let limited = limited.clone();
            tokio::spawn(async move { limited.acquire_with_priority(0).await.map(|_| ()) })
        };
        wait_for_queued(&limited, 1).await;
        assert!(
            limited.try_acquire().is_none(),
            "try_acquire admitted a second holder"
        );
        assert_eq!(limited.available_permits(), 0);
        drop(holder);
        let admitted = tokio::time::timeout(Duration::from_secs(2), queued)
            .await
            .expect("queued waiter hung")
            .expect("queued waiter panicked");
        assert!(admitted.is_ok());
        assert_eq!(limited.available_permits(), 1);
    }

    #[tokio::test]
    async fn priority_semaphore_cancelled_waiter_does_not_leak_permit() {
        let sem = Arc::new(PrioritySemaphore::new(1));
        let holder = sem.acquire_with_priority(0).await.unwrap();
        let waiter = {
            let sem = sem.clone();
            tokio::spawn(async move { sem.acquire_with_priority(0).await.map(|_| ()) })
        };
        wait_for_queued(&sem, 1).await;

        waiter.abort();
        let _ = tokio::time::timeout(Duration::from_secs(2), waiter)
            .await
            .expect("aborted waiter did not finish");
        assert_eq!(sem.queued_waiters(), 0);

        drop(holder);
        assert_eq!(sem.available_permits(), 1);

        let permit = tokio::time::timeout(Duration::from_secs(2), sem.acquire_with_priority(0))
            .await
            .expect("acquire hung after cancelled waiter")
            .unwrap();
        drop(permit);
        assert_eq!(sem.available_permits(), 1);
    }
}
