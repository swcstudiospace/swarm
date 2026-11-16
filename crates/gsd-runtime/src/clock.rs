use std::future::Future;
use std::pin::Pin;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime};

/// Trait for clock implementations
pub trait Clock: Send + Sync {
    fn now(&self) -> Instant;
    fn now_system(&self) -> SystemTime;
    fn sleep(&self, duration: Duration) -> Pin<Box<dyn Future<Output = ()> + Send + '_>>;
    fn now_nanos(&self) -> u64;
    fn elapsed_since(&self, start: Instant) -> Duration;
}

/// Real clock using system time
#[derive(Debug, Clone)]
pub struct RealClock;

impl RealClock {
    pub fn new() -> Self {
        Self
    }
}

impl Default for RealClock {
    fn default() -> Self {
        Self::new()
    }
}

impl Clock for RealClock {
    fn now(&self) -> Instant {
        Instant::now()
    }

    fn now_system(&self) -> SystemTime {
        SystemTime::now()
    }

    fn sleep(&self, duration: Duration) -> Pin<Box<dyn Future<Output = ()> + Send + '_>> {
        Box::pin(tokio::time::sleep(duration))
    }

    fn now_nanos(&self) -> u64 {
        SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos() as u64
    }

    fn elapsed_since(&self, start: Instant) -> Duration {
        start.elapsed()
    }
}

/// Deterministic clock for simulation/replay mode
#[derive(Debug, Clone)]
pub struct SimClock {
    /// Current virtual time in nanoseconds
    virtual_time_ns: Arc<std::sync::atomic::AtomicU64>,
    /// Whether we're in simulation mode
    sim_mode: Arc<std::sync::atomic::AtomicBool>,
    /// Waiters for time advancement
    waiters: Arc<parking_lot::Mutex<Vec<tokio::sync::oneshot::Sender<()>>>>,
}

impl SimClock {
    pub fn new() -> Self {
        Self {
            virtual_time_ns: Arc::new(std::sync::atomic::AtomicU64::new(0)),
            sim_mode: Arc::new(std::sync::atomic::AtomicBool::new(true)),
            waiters: Arc::new(parking_lot::Mutex::new(Vec::new())),
        }
    }

    /// Create a new SimClock starting at a specific virtual time
    pub fn with_start_time(nanos: u64) -> Self {
        let clock = Self::new();
        clock
            .virtual_time_ns
            .store(nanos, std::sync::atomic::Ordering::SeqCst);
        clock
    }

    /// Enable or disable simulation mode
    pub fn set_sim_mode(&self, enabled: bool) {
        self.sim_mode
            .store(enabled, std::sync::atomic::Ordering::SeqCst);
    }

    /// Check if in simulation mode
    pub fn is_sim_mode(&self) -> bool {
        self.sim_mode.load(std::sync::atomic::Ordering::SeqCst)
    }

    /// Get current virtual time in nanoseconds
    pub fn virtual_time_ns(&self) -> u64 {
        self.virtual_time_ns
            .load(std::sync::atomic::Ordering::SeqCst)
    }

    /// Advance virtual time by the given duration
    pub fn advance(&self, duration: Duration) {
        let nanos = duration.as_nanos() as u64;
        self.virtual_time_ns
            .fetch_add(nanos, std::sync::atomic::Ordering::SeqCst);
        self.notify_waiters();
    }

    /// Set virtual time to a specific value
    pub fn set_time(&self, nanos: u64) {
        self.virtual_time_ns
            .store(nanos, std::sync::atomic::Ordering::SeqCst);
        self.notify_waiters();
    }

    /// Get current virtual time as Instant (for compatibility)
    pub fn virtual_instant(&self) -> Instant {
        // Create a fake instant - this is only for comparison purposes
        // In sim mode, all time operations use virtual_time_ns directly
        Instant::now()
    }

    fn notify_waiters(&self) {
        let mut waiters = self.waiters.lock();
        for waiter in waiters.drain(..) {
            let _ = waiter.send(());
        }
    }

    /// Register a waiter for the next time advancement
    fn register_waiter(&self) -> tokio::sync::oneshot::Receiver<()> {
        let (tx, rx) = tokio::sync::oneshot::channel();
        self.waiters.lock().push(tx);
        rx
    }
}

impl Default for SimClock {
    fn default() -> Self {
        Self::new()
    }
}

impl Clock for SimClock {
    fn now(&self) -> Instant {
        if self.sim_mode.load(Ordering::SeqCst) {
            // In sim mode, return a fake instant based on virtual time
            // This is only for API compatibility - actual time operations use virtual time
            Instant::now()
        } else {
            Instant::now()
        }
    }

    fn now_system(&self) -> std::time::SystemTime {
        if self.sim_mode.load(Ordering::SeqCst) {
            // Convert virtual time to SystemTime
            std::time::UNIX_EPOCH
                + std::time::Duration::from_nanos(self.virtual_time_ns.load(Ordering::SeqCst))
        } else {
            std::time::SystemTime::now()
        }
    }

    fn sleep(&self, duration: Duration) -> Pin<Box<dyn Future<Output = ()> + Send + '_>> {
        if self.sim_mode.load(Ordering::SeqCst) {
            // In sim mode, advance virtual time and wait for next advancement
            let self_clone = self.clone();

            Box::pin(async move {
                let target_time =
                    self_clone.virtual_time_ns.load(Ordering::SeqCst) + duration.as_nanos() as u64;

                loop {
                    if self_clone.virtual_time_ns.load(Ordering::SeqCst) >= target_time {
                        break;
                    }
                    // Register before the re-check: an advance that lands after the re-check finds
                    // this waiter, and one that landed before registration is caught by the re-check.
                    let woken = self_clone.register_waiter();
                    if self_clone.virtual_time_ns.load(Ordering::SeqCst) >= target_time {
                        break;
                    }
                    let _ = woken.await;
                }
            })
        } else {
            Box::pin(tokio::time::sleep(duration))
        }
    }

    fn now_nanos(&self) -> u64 {
        if self.sim_mode.load(Ordering::SeqCst) {
            self.virtual_time_ns.load(Ordering::SeqCst)
        } else {
            SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_nanos() as u64
        }
    }

    fn elapsed_since(&self, start: Instant) -> Duration {
        if self.sim_mode.load(Ordering::SeqCst) {
            // In sim mode, we can't use real Instant.elapsed()
            // This is a limitation - callers should use virtual time directly
            Duration::ZERO
        } else {
            start.elapsed()
        }
    }
}

/// Clock provider that can switch between real and simulated time
#[derive(Debug, Clone)]
pub struct ClockProvider {
    real_clock: RealClock,
    sim_clock: SimClock,
    use_sim: Arc<std::sync::atomic::AtomicBool>,
}

impl Default for ClockProvider {
    fn default() -> Self {
        Self::new()
    }
}

impl ClockProvider {
    pub fn new() -> Self {
        Self {
            real_clock: RealClock::new(),
            sim_clock: SimClock::new(),
            use_sim: Arc::new(std::sync::atomic::AtomicBool::new(false)),
        }
    }

    /// Create a new provider in simulation mode
    pub fn sim() -> Self {
        let provider = Self::new();
        provider.enable_sim();
        provider
    }

    /// Enable simulation mode
    pub fn enable_sim(&self) {
        self.use_sim.store(true, Ordering::SeqCst);
        self.sim_clock.set_sim_mode(true);
    }

    /// Disable simulation mode
    pub fn disable_sim(&self) {
        self.use_sim.store(false, Ordering::SeqCst);
        self.sim_clock.set_sim_mode(false);
    }

    /// Get the current clock
    pub fn clock(&self) -> Arc<dyn Clock> {
        if self.use_sim.load(Ordering::SeqCst) {
            Arc::new(self.sim_clock.clone())
        } else {
            Arc::new(self.real_clock.clone())
        }
    }

    /// Get the sim clock directly for time manipulation
    pub fn sim_clock(&self) -> &SimClock {
        &self.sim_clock
    }

    /// Get the real clock
    pub fn real_clock(&self) -> &RealClock {
        &self.real_clock
    }

    /// Check if in simulation mode
    pub fn is_sim(&self) -> bool {
        self.use_sim.load(Ordering::SeqCst)
    }
}

/// Global clock provider instance
static GLOBAL_CLOCK: std::sync::OnceLock<ClockProvider> = std::sync::OnceLock::new();

/// Get the global clock provider
pub fn global_clock() -> &'static ClockProvider {
    GLOBAL_CLOCK.get_or_init(ClockProvider::new)
}

/// Initialize the global clock provider
pub fn init_global_clock(provider: ClockProvider) {
    GLOBAL_CLOCK.set(provider).ok();
}

/// Get the current clock (real or sim depending on mode)
pub fn current_clock() -> Arc<dyn Clock> {
    global_clock().clock()
}

/// Enable simulation mode globally
pub fn enable_sim_mode() {
    global_clock().enable_sim();
}

/// Disable simulation mode globally
pub fn disable_sim_mode() {
    global_clock().disable_sim();
}

/// Check if in simulation mode
pub fn is_sim_mode() -> bool {
    global_clock().is_sim()
}

/// Get the sim clock for time manipulation
pub fn sim_clock() -> SimClock {
    global_clock().sim_clock().clone()
}

/// Sleep for the given duration using the current clock
pub async fn sleep(duration: Duration) {
    let clock = current_clock();
    clock.sleep(duration).await;
}

/// Get current time using the active clock
pub fn now() -> Instant {
    current_clock().now()
}

/// Get current system time using the active clock
pub fn now_system() -> SystemTime {
    current_clock().now_system()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn test_real_clock() {
        let clock = RealClock::new();
        let start = clock.now();
        clock.sleep(Duration::from_millis(10)).await;
        assert!(clock.elapsed_since(start) >= Duration::from_millis(10));
    }

    #[tokio::test]
    async fn test_sim_clock_advance() {
        let clock = SimClock::new();
        clock.set_sim_mode(true);

        let start = clock.virtual_time_ns();
        clock.advance(std::time::Duration::from_millis(100));
        let elapsed = clock.virtual_time_ns() - start;

        assert_eq!(elapsed, 100_000_000); // 100ms in nanoseconds
    }

    #[tokio::test]
    async fn test_sim_clock_sleep() {
        let clock = SimClock::new();
        clock.set_sim_mode(true);

        // Spawn a task that advances time
        let clock_clone = clock.clone();
        tokio::spawn(async move {
            tokio::time::sleep(Duration::from_millis(10)).await;
            clock_clone.advance(Duration::from_millis(50));
        });

        clock.sleep(Duration::from_millis(30)).await;
        // Reaching this point means the sleep completed once the spawned task advanced virtual time.
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn test_sim_sleep_is_not_lost_to_concurrent_advance() {
        for _ in 0..1_000 {
            let clock = SimClock::new();
            let advancer = clock.clone();
            let finished = Arc::new(std::sync::atomic::AtomicBool::new(false));
            let flag = Arc::clone(&finished);
            let ticking = tokio::spawn(async move {
                while !flag.load(Ordering::SeqCst) {
                    advancer.advance(Duration::from_millis(1));
                    tokio::task::yield_now().await;
                }
            });
            tokio::time::timeout(
                Duration::from_secs(2),
                clock.sleep(Duration::from_millis(1)),
            )
            .await
            .expect("sim sleep was never woken by advance()");
            finished.store(true, Ordering::SeqCst);
            ticking.await.unwrap();
        }
    }

    #[test]
    fn test_clock_provider() {
        let provider = ClockProvider::new();
        assert!(!provider.is_sim());

        provider.enable_sim();
        assert!(provider.is_sim());

        provider.disable_sim();
        assert!(!provider.is_sim());
    }
}
