//! Opt-in panic-output control for native scheduler work, not a process-wide privacy policy.
//!
//! The first explicit [`NativeSchedulerScope::enter`] installs one process-global hook, synchronously and before marking the
//! calling thread. It captures the prior hook once. Marked threads produce no hook output; unmarked threads delegate to that
//! prior hook unchanged. Merely importing this module, constructing a pool or submitting an ordinary job installs nothing.
//!
//! # Embedding host contract and limits
//!
//! The host must finish hook setup cooperatively, with hook mutation quiescent at the first native scheduler entry. Later or
//! concurrent host `set_hook`/`take_hook` invalidates this support contract: Rust cannot prevent replacement. We never detect,
//! fight or reinstall over a replacement. If thread-local storage is unavailable (for example during thread teardown), the hook
//! delegates to the prior hook; no privacy guarantee applies there. This is not a new guarantee for `panic=abort`, arbitrary
//! host/Python callbacks, or output produced outside the hook. Sanitizing a returned `E-INTERNAL` error is a separate boundary.
//!
//! Binding integration must keep a scope alive through catch/unwind **and disposal of caught panic payloads**, and use
//! [`with_host_callback`] only around arbitrary host/Python execution. Explicit native reentry from such a callback may enter
//! a fresh scope. Pool submissions inherit only the submitting thread's current native mark, per job; a callback's ordinary
//! submissions do not inherit the suspended mark. No Python flag or debug callable is exposed here.

use std::cell::Cell;
use std::marker::PhantomData;
use std::rc::Rc;
use std::sync::Once;

thread_local! {
    static NATIVE_DEPTH: Cell<usize> = const { Cell::new(0) };
}

static INSTALL: Once = Once::new();

/// A thread-bound, nested opt-in mark for native scheduler work.
///
/// This guard is neither `Send` nor `Sync`. Keep it on the entering thread and drop it before leaving that native boundary.
/// It allocates nothing; only the first entry installs a hook. Do not first enter from a panicking thread or a panic hook
/// (`std::panic::set_hook` rejects that); host hook setup must obey the [module contract](self).
#[must_use = "keep the scope alive through catch/unwind and caught panic-payload disposal"]
pub struct NativeSchedulerScope {
    entered: bool,
    _thread_bound: PhantomData<Rc<()>>,
}

impl NativeSchedulerScope {
    /// Installs the hook once if necessary, then marks only this thread until the returned guard is dropped.
    ///
    /// Nesting is balanced on success, returned errors and unwind. Unavailable TLS preserves the prior hook's behavior instead
    /// of panicking or claiming suppression. No hook is swapped or restored at scope exit.
    pub fn enter() -> Self {
        INSTALL.call_once(|| {
            let prior = std::panic::take_hook();
            std::panic::set_hook(Box::new(move |info| {
                // No payload inspection, formatting, allocation, application lock or callback on the marked fast path.
                if !native_context() {
                    prior(info);
                }
            }));
        });
        let entered = NATIVE_DEPTH
            .try_with(|depth| match depth.get().checked_add(1) {
                Some(next) => {
                    depth.set(next);
                    true
                }
                None => false,
            })
            .unwrap_or(false);
        Self {
            entered,
            _thread_bound: PhantomData,
        }
    }
}

impl Drop for NativeSchedulerScope {
    fn drop(&mut self) {
        if self.entered {
            let _ = NATIVE_DEPTH.try_with(|depth| depth.set(depth.get().saturating_sub(1)));
        }
    }
}

/// Runs arbitrary host/Python callback code without inheriting the enclosing native scheduler mark.
///
/// Rust-only binding integration helper, not a public Python switch. Restores the exact enclosing nesting depth on return or
/// unwind, including nested callbacks and explicit native reentry. Catch and dispose callback-owned panic payloads inside the
/// callback if their destructors must also run unmarked. Native reentry guards must be dropped within their reentry boundary.
/// This changes TLS only: it never installs, removes or restores a process hook.
pub fn with_host_callback<T>(callback: impl FnOnce() -> T) -> T {
    let restore = RestoreDepth(NATIVE_DEPTH.try_with(|depth| depth.replace(0)).ok());
    let result = callback();
    drop(restore);
    result
}

struct RestoreDepth(Option<usize>);

impl Drop for RestoreDepth {
    fn drop(&mut self) {
        if let Some(saved) = self.0 {
            let _ = NATIVE_DEPTH.try_with(|depth| depth.set(saved));
        }
    }
}

/// Captured at submission time, not when a worker happens to dequeue the job. No initialization or hook mutation here.
pub(crate) fn native_context() -> bool {
    NATIVE_DEPTH
        .try_with(|depth| depth.get() != 0)
        .unwrap_or(false)
}

#[cfg(all(test, not(loom)))]
mod tests {
    use super::{native_context, with_host_callback, NativeSchedulerScope};
    use crate::pool::{Outcome, Pool};
    use std::io::Read;
    use std::panic::{catch_unwind, panic_any, AssertUnwindSafe};
    use std::process::{Command, Stdio};
    use std::sync::{mpsc, Arc, Barrier};
    use std::thread;
    use std::time::{Duration, Instant};

    const CHILD_ENV: &str = "GSD_SCHED_CORE_PANIC_GUARD_CHILD";
    const WAIT: Duration = Duration::from_secs(20);
    const PRIOR: &str = "SCHED_PRIOR_HOOK";

    // Every hook mutation and actual panic runs only in a disposable, exact-filtered child. A watchdog bounds deadlocks,
    // and concurrent pipe readers retain the entire stderr rather than a truncated/prefix-only canary check.
    fn captured_child(name: &str, body: impl FnOnce()) -> Option<String> {
        if std::env::var(CHILD_ENV).as_deref() == Ok(name) {
            body();
            return None;
        }
        let mut child = Command::new(std::env::current_exe().expect("test executable"))
            .args([
                "--exact",
                &format!("panic_guard::tests::{name}"),
                "--nocapture",
                "--test-threads=1",
            ])
            .env(CHILD_ENV, name)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .expect("disposable panic child starts");
        let mut stdout = child.stdout.take().unwrap();
        let mut stderr = child.stderr.take().unwrap();
        let out = thread::spawn(move || {
            let mut bytes = Vec::new();
            stdout.read_to_end(&mut bytes).unwrap();
            bytes
        });
        let err = thread::spawn(move || {
            let mut bytes = Vec::new();
            stderr.read_to_end(&mut bytes).unwrap();
            bytes
        });
        let deadline = Instant::now() + WAIT;
        let (status, timed_out) = loop {
            if let Some(status) = child.try_wait().expect("child status") {
                break (status, false);
            }
            if Instant::now() >= deadline {
                child.kill().expect("kill timed-out child");
                break (child.wait().expect("reap timed-out child"), true);
            }
            thread::sleep(Duration::from_millis(10));
        };
        let stdout = String::from_utf8(out.join().unwrap()).expect("child stdout UTF-8");
        let stderr = String::from_utf8(err.join().unwrap()).expect("child stderr UTF-8");
        assert!(
            !timed_out && status.success(),
            "{name}: {status}, timeout={timed_out}\n{stdout}\n{stderr}"
        );
        assert!(
            stdout.contains("running 1 test") && stdout.contains("1 passed"),
            "child must execute exactly one case: {stdout}"
        );
        Some(stderr)
    }

    fn prior_hook() {
        std::panic::set_hook(Box::new(|info| eprintln!("{PRIOR}: {info}")));
    }

    fn caught_panic(canary: &'static str) {
        let payload = catch_unwind(|| panic!("{canary}")).expect_err("panic was caught");
        drop(payload);
    }

    fn next(pool: &Pool<usize>) -> (String, Outcome<usize>) {
        let done = pool.next_completion(WAIT).expect("worker completes");
        (done.task_id, done.outcome)
    }

    #[test]
    fn scheduler_marked_panic_stderr_is_canary_free() {
        const CANARY: &str = "INERT_NATIVE_SCOPE_CANARY";
        if let Some(stderr) = captured_child("scheduler_marked_panic_stderr_is_canary_free", || {
            // Keep the real default hook: a catch_unwind without this scope would still print CANARY.
            assert!(!super::INSTALL.is_completed());
            let ordinary = Pool::<usize>::new(1);
            ordinary
                .submit("no-opt-in", || {
                    assert!(!native_context());
                    1
                })
                .unwrap();
            assert_eq!(next(&ordinary), ("no-opt-in".to_owned(), Outcome::Done(1)));
            assert!(ordinary.shutdown(WAIT).abandoned.is_empty());
            assert!(
                !super::INSTALL.is_completed(),
                "ordinary pool work cannot install a hook"
            );
            {
                let _native = NativeSchedulerScope::enter();
                caught_panic(CANARY);
                assert!(super::INSTALL.is_completed());
                assert!(native_context());
            }
            assert!(!native_context());
            let error = {
                let _native = NativeSchedulerScope::enter();
                Err::<(), _>(7)
            };
            assert_eq!(error, Err(7));
            assert!(
                !native_context(),
                "returned errors must balance their native scope"
            );
            let unwind = catch_unwind(|| {
                let _native = NativeSchedulerScope::enter();
                panic!("{CANARY}");
            });
            drop(unwind.expect_err("scope unwinds"));
            assert!(!native_context());
        }) {
            assert!(
                !stderr.contains(CANARY),
                "marked default-hook output: {stderr}"
            );
            assert!(stderr.is_empty(), "marked path must emit nothing: {stderr}");
        }
    }

    #[test]
    fn scheduler_unmarked_panic_chains_prior_hook() {
        const BEFORE: &str = "INERT_UNMARKED_BEFORE_CANARY";
        const AFTER: &str = "INERT_UNMARKED_AFTER_CANARY";
        if let Some(stderr) = captured_child("scheduler_unmarked_panic_chains_prior_hook", || {
            prior_hook();
            caught_panic(BEFORE);
            {
                let _native = NativeSchedulerScope::enter();
                caught_panic("INERT_MARKED_CHAIN_CANARY");
            }
            caught_panic(AFTER);
        }) {
            assert!(
                stderr.contains(BEFORE) && stderr.contains(AFTER),
                "prior payload output lost: {stderr}"
            );
            assert_eq!(
                stderr.matches(PRIOR).count(),
                2,
                "prior hook called only for unmarked work"
            );
            assert!(!stderr.contains("INERT_MARKED_CHAIN_CANARY"));
        }
    }

    #[test]
    fn scheduler_scope_is_thread_local_and_nested() {
        const MARKED: &str = "INERT_CONCURRENT_MARKED_CANARY";
        const UNMARKED: &str = "INERT_CONCURRENT_UNMARKED_CANARY";
        if let Some(stderr) = captured_child("scheduler_scope_is_thread_local_and_nested", || {
            prior_hook();
            // Race the very first explicit entries, not repeated take/set cycles. Both marked threads remain live while an
            // unrelated thread panics. Repeated nested exits must preserve each outer mark independently.
            let gate = Arc::new(Barrier::new(3));
            let mut threads = Vec::new();
            for _ in 0..2 {
                let gate = Arc::clone(&gate);
                threads.push(thread::spawn(move || {
                    let _outer = NativeSchedulerScope::enter();
                    gate.wait();
                    for _ in 0..8 {
                        {
                            let _inner = NativeSchedulerScope::enter();
                            caught_panic(MARKED);
                        }
                        assert!(native_context());
                        caught_panic(MARKED);
                    }
                    gate.wait();
                }));
            }
            assert!(!native_context());
            gate.wait();
            caught_panic(UNMARKED);
            gate.wait();
            for handle in threads {
                handle.join().unwrap();
            }
            assert!(!native_context());
        }) {
            assert!(
                !stderr.contains(MARKED),
                "concurrent native output: {stderr}"
            );
            assert!(stderr.contains(UNMARKED));
            assert_eq!(
                stderr.matches(PRIOR).count(),
                1,
                "unrelated thread retains prior hook exactly once"
            );
        }
    }

    #[test]
    fn scheduler_callback_suspension_restores_scope() {
        const HOST: &str = "INERT_HOST_CALLBACK_CANARY";
        const NATIVE: &str = "INERT_CALLBACK_REENTRY_NATIVE_CANARY";
        if let Some(stderr) = captured_child("scheduler_callback_suspension_restores_scope", || {
            prior_hook();
            let outer = NativeSchedulerScope::enter();
            let inner = NativeSchedulerScope::enter();
            let value = with_host_callback(|| {
                assert!(!native_context());
                caught_panic(HOST);
                {
                    let _reentry = NativeSchedulerScope::enter();
                    caught_panic(NATIVE);
                    with_host_callback(|| {
                        assert!(!native_context());
                        caught_panic(HOST);
                    });
                    assert!(native_context());
                }
                assert!(!native_context());
                with_host_callback(|| assert!(!native_context()));
                Err::<(), _>(7)
            });
            assert_eq!(value, Err(7));
            assert!(native_context());
            // A callback unwinding out of the suspension restores the outer native depth without suppressing its hook.
            let payload =
                catch_unwind(AssertUnwindSafe(|| with_host_callback(|| panic!("{HOST}"))));
            drop(payload.expect_err("callback panicked"));
            assert!(native_context());
            drop(inner);
            caught_panic(NATIVE);
            drop(outer);
            assert!(!native_context());
            with_host_callback(|| caught_panic(HOST));
            assert!(!native_context());
        }) {
            assert!(
                !stderr.contains(NATIVE),
                "reentry or restored scope lost: {stderr}"
            );
            assert_eq!(
                stderr.matches(HOST).count(),
                4,
                "all host callbacks remain unmarked: {stderr}"
            );
            assert_eq!(stderr.matches(PRIOR).count(), 4);
        }
    }

    #[test]
    fn scheduler_worker_panic_stderr_is_canary_free() {
        const CANARY: &str = "INERT_NATIVE_WORKER_CANARY";
        if let Some(stderr) = captured_child("scheduler_worker_panic_stderr_is_canary_free", || {
            prior_hook();
            let pool = Pool::<usize>::new(1);
            {
                let _native = NativeSchedulerScope::enter();
                pool.submit("native-panic", || panic!("{CANARY}")).unwrap();
                pool.submit("native-ok", || 7).unwrap();
                pool.submit("native-error", || {
                    assert!(native_context());
                    let error: Result<(), ()> = Err(());
                    assert!(error.is_err());
                    8
                })
                .unwrap();
            }
            assert_eq!(next(&pool), ("native-panic".to_owned(), Outcome::Crashed));
            assert_eq!(next(&pool), ("native-ok".to_owned(), Outcome::Done(7)));
            assert_eq!(next(&pool), ("native-error".to_owned(), Outcome::Done(8)));
            assert_eq!((pool.in_flight(), pool.queued()), (0, 0));
            assert!(pool.shutdown(WAIT).abandoned.is_empty());
        }) {
            assert!(
                !stderr.contains(CANARY),
                "worker panic hook leaked: {stderr}"
            );
            assert!(
                stderr.is_empty(),
                "marked workers must not call prior hook: {stderr}"
            );
        }
    }

    #[test]
    fn scheduler_concurrent_workers_keep_individual_context() {
        const MARKED: &str = "INERT_PARALLEL_NATIVE_WORKER_CANARY";
        const HOST: &str = "INERT_PARALLEL_ORDINARY_WORKER_CANARY";
        if let Some(stderr) = captured_child(
            "scheduler_concurrent_workers_keep_individual_context",
            || {
                prior_hook();
                let pool = Pool::<usize>::new(2);
                let gate = Arc::new(Barrier::new(2));
                let _native = NativeSchedulerScope::enter();
                let native_gate = Arc::clone(&gate);
                pool.submit("parallel-native", move || {
                    assert!(native_context());
                    native_gate.wait();
                    panic!("{MARKED}");
                })
                .unwrap();
                with_host_callback(|| {
                    pool.submit("parallel-ordinary", move || {
                        assert!(!native_context());
                        gate.wait();
                        panic!("{HOST}");
                    })
                    .unwrap();
                });
                let mut got = vec![next(&pool), next(&pool)];
                got.sort_by(|a, b| a.0.cmp(&b.0));
                assert_eq!(
                    got,
                    vec![
                        ("parallel-native".to_owned(), Outcome::Crashed),
                        ("parallel-ordinary".to_owned(), Outcome::Crashed),
                    ]
                );
                assert_eq!((pool.in_flight(), pool.queued()), (0, 0));
                assert!(pool.shutdown(WAIT).abandoned.is_empty());
            },
        ) {
            assert!(
                !stderr.contains(MARKED),
                "concurrent marked worker output: {stderr}"
            );
            assert!(
                stderr.contains(HOST),
                "ordinary worker retains prior hook: {stderr}"
            );
            assert_eq!(stderr.matches(PRIOR).count(), 1);
        }
    }

    #[test]
    fn scheduler_worker_payload_drop_stays_marked() {
        const DROP: &str = "INERT_PANIC_PAYLOAD_DROP_CANARY";
        struct Payload;
        impl Drop for Payload {
            fn drop(&mut self) {
                assert!(native_context(), "payload disposal must retain native mark");
                panic!("{DROP}");
            }
        }
        if let Some(stderr) = captured_child("scheduler_worker_payload_drop_stays_marked", || {
            prior_hook();
            let pool = Pool::<usize>::new(1);
            {
                let _native = NativeSchedulerScope::enter();
                pool.submit("payload", || panic_any(Payload)).unwrap();
            }
            assert_eq!(next(&pool), ("payload".to_owned(), Outcome::Crashed));
            pool.submit("ordinary-after-drop", || {
                assert!(!native_context());
                9
            })
            .unwrap();
            assert_eq!(
                next(&pool),
                ("ordinary-after-drop".to_owned(), Outcome::Done(9))
            );
            assert!(pool.shutdown(WAIT).abandoned.is_empty());
        }) {
            assert!(
                !stderr.contains(DROP),
                "caught payload destructor leaked: {stderr}"
            );
            assert!(
                stderr.is_empty(),
                "payload and its drop must both stay marked: {stderr}"
            );
        }
    }

    #[test]
    fn scheduler_worker_queue_context_does_not_bleed() {
        const MARKED: &str = "INERT_QUEUED_NATIVE_CANARY";
        const HOST: &str = "INERT_QUEUED_ORDINARY_CANARY";
        if let Some(stderr) =
            captured_child("scheduler_worker_queue_context_does_not_bleed", || {
                prior_hook();
                let pool = Pool::<usize>::new(1);
                let (release, released) = mpsc::channel();
                let (started, running) = mpsc::channel();
                {
                    let _native = NativeSchedulerScope::enter();
                    pool.submit("native-blocker", move || {
                        assert!(native_context());
                        started.send(thread::current().id()).unwrap();
                        released.recv_timeout(WAIT).unwrap();
                        1
                    })
                    .unwrap();
                    let worker = running.recv_timeout(WAIT).unwrap();
                    pool.submit("queued-native", move || {
                        assert_eq!(thread::current().id(), worker);
                        assert!(native_context());
                        panic!("{MARKED}");
                    })
                    .unwrap();
                    with_host_callback(|| {
                        pool.submit("queued-host", move || {
                            assert_eq!(thread::current().id(), worker);
                            assert!(!native_context());
                            panic!("{HOST}");
                        })
                        .unwrap();
                    });
                }
                pool.submit("ordinary-success", || {
                    assert!(!native_context());
                    2
                })
                .unwrap();
                assert_eq!(
                    pool.queued(),
                    3,
                    "all jobs capture their own context while first worker is blocked"
                );
                release.send(()).unwrap();
                assert_eq!(next(&pool), ("native-blocker".to_owned(), Outcome::Done(1)));
                assert_eq!(next(&pool), ("queued-native".to_owned(), Outcome::Crashed));
                assert_eq!(next(&pool), ("queued-host".to_owned(), Outcome::Crashed));
                assert_eq!(
                    next(&pool),
                    ("ordinary-success".to_owned(), Outcome::Done(2))
                );
                assert_eq!((pool.in_flight(), pool.queued()), (0, 0));
                assert!(pool.shutdown(WAIT).abandoned.is_empty());
            })
        {
            assert!(
                !stderr.contains(MARKED),
                "queued native context lost: {stderr}"
            );
            assert!(
                stderr.contains(HOST),
                "ordinary job must retain prior hook: {stderr}"
            );
            assert_eq!(
                stderr.matches(PRIOR).count(),
                1,
                "native mark cannot bleed to the next job"
            );
        }
    }

    #[test]
    fn scheduler_host_replacement_limit_is_explicit() {
        const BEFORE: &str = "INERT_BEFORE_HOST_REPLACEMENT_CANARY";
        const AFTER: &str = "INERT_AFTER_HOST_REPLACEMENT_CANARY";
        if let Some(stderr) = captured_child("scheduler_host_replacement_limit_is_explicit", || {
            prior_hook();
            {
                let _native = NativeSchedulerScope::enter();
                caught_panic(BEFORE);
            }
            // Outside the supported host arrangement. Even a fresh explicit entry must NOT fight or reinstall over this.
            std::panic::set_hook(Box::new(|info| eprintln!("SCHED_REPLACEMENT_HOOK: {info}")));
            let _native = NativeSchedulerScope::enter();
            caught_panic(AFTER);
        }) {
            assert!(!stderr.contains(BEFORE));
            assert!(
                stderr.contains(AFTER) && stderr.contains("SCHED_REPLACEMENT_HOOK"),
                "host owns later replacement: {stderr}"
            );
            assert!(
                !stderr.contains(PRIOR),
                "the captured prior hook must not be reinstalled"
            );
        }
    }
}
