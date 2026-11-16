//! Checkpoint measurement of the Rust scheduler loop (Phase 14.4, decision D-P21): what the loop itself costs.
//!
//! The loop runs against stub ports that do no work (a state vector behind one mutex, answers built from pre-made strings, a worker
//! that returns at once), so what is measured is the loop's own overhead: the pass (`begin_pass` to `observe_pass`, which includes
//! ordering, selection and one `submit` per dispatch, each of which starts a pool thread), the pool hand-off, and the completion
//! handling. The rows and answers allocate strings as the port contract requires, exactly as the Python adapter would.
//!
//! Run in release on an otherwise quiet host:
//!
//! ```text
//! cargo run --release --example loop_bench -p gsd-sched-core
//! ```
//!
//! It prints `/proc/loadavg` at the start and the end, for 50 and 500 ready tasks on 4 and 32 workers: per-pass latency (p50 and p99
//! over every pass of every run), the loop thread's CPU time per pass (from `/proc/thread-self/schedstat`, when the kernel provides
//! it) and the end-to-end time per task; then the per-call cost of the primitives the Python loop runs per pass (`order_ready`,
//! `ClaimTable`, `AimdLimiter`, `backoff_s`).

use std::hint::black_box;
use std::sync::{Arc, Mutex, PoisonError};
use std::time::{Duration, Instant};

use gsd_sched_core::aimd::AimdLimiter;
use gsd_sched_core::backoff::backoff_s;
use gsd_sched_core::claim_pattern::ClaimMode;
use gsd_sched_core::claims::ClaimTable;
use gsd_sched_core::pool::Pool;
use gsd_sched_core::ports::{
    AgentInfo, CancelState, ClaimsVerdict, Completion, LeaseVerdict, PassInfo, PortError, ReadyRow,
    RunConfig, SchedulerPorts, Submission, WorkerDone,
};
use gsd_sched_core::priority::order_ready;
use gsd_sched_core::runtime::run;

/// Task states of the stub store.
const PENDING: u8 = 0;
const ACTIVE: u8 = 1;
const DONE: u8 = 2;

/// Ports that do nothing but answer.
struct Stub {
    ids: Vec<String>,
    state: Mutex<Vec<u8>>,
    pass_started: Mutex<Option<Instant>>,
    /// `(nanoseconds from begin_pass to observe_pass, tasks the pass submitted)` per pass.
    pass_ns: Mutex<Vec<(u64, usize)>>,
}

impl Stub {
    fn new(tasks: usize) -> Self {
        Self {
            ids: (0..tasks).map(|n| format!("T{n}")).collect(),
            state: Mutex::new(vec![PENDING; tasks]),
            pass_started: Mutex::new(None),
            pass_ns: Mutex::new(Vec::new()),
        }
    }

    fn set(&self, task_id: &str, to: u8) {
        let index: usize = task_id[1..].parse().unwrap_or(0);
        self.state.lock().unwrap_or_else(PoisonError::into_inner)[index] = to;
    }
}

impl SchedulerPorts for Stub {
    fn begin_pass(&self, _round: u64, _in_flight: &[&str]) -> Result<Vec<ReadyRow>, PortError> {
        *self
            .pass_started
            .lock()
            .unwrap_or_else(PoisonError::into_inner) = Some(Instant::now());
        let state = self.state.lock().unwrap_or_else(PoisonError::into_inner);
        Ok(state
            .iter()
            .enumerate()
            .filter(|(_, state)| **state == PENDING)
            .map(|(index, _)| ReadyRow {
                task_id: self.ids[index].clone(),
                priority: Some("P2".to_owned()),
                dag_depth: Some(0),
                created_at: Some(index as f64),
                retry_after: None,
            })
            .collect())
    }

    fn agent_for(&self, _task_id: &str) -> Result<Option<AgentInfo>, PortError> {
        Ok(Some(AgentInfo {
            agent_id: "A".to_owned(),
            slug: "a".to_owned(),
            max_parallel: 1000,
        }))
    }

    fn claims_for(&self, _task_id: &str) -> Result<ClaimsVerdict, PortError> {
        Ok(ClaimsVerdict::Claims(Vec::new()))
    }

    fn block(&self, _task_id: &str, _reason: &str) -> Result<(), PortError> {
        Ok(())
    }

    fn lease(&self, _task_id: &str, _agent_id: &str) -> Result<Option<LeaseVerdict>, PortError> {
        Ok(None)
    }

    fn observe_pass(&self, info: &PassInfo<'_>) -> Result<(), PortError> {
        let started = self
            .pass_started
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .take();
        if let Some(started) = started {
            let ns = u64::try_from(started.elapsed().as_nanos()).unwrap_or(u64::MAX);
            self.pass_ns
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .push((ns, info.submitted));
        }
        Ok(())
    }

    fn submit(&self, task_id: &str, _agent_id: &str) -> Result<Submission, PortError> {
        self.set(task_id, ACTIVE);
        Ok(Submission::Started {
            sid: format!("S-{task_id}"),
        })
    }

    fn worker(&self, task_id: &str, agent_id: &str, _sid: &str) -> Result<WorkerDone, PortError> {
        Ok(WorkerDone {
            task_id: task_id.to_owned(),
            agent_id: agent_id.to_owned(),
            outcome: "DONE".to_owned(),
            error: None,
        })
    }

    fn internal_failure(&self, _: &str, _: &str, text: &str) -> Result<WorkerDone, PortError> {
        Err(PortError::new(text, Box::new(())))
    }

    fn complete(&self, done: &WorkerDone) -> Result<Completion, PortError> {
        self.set(&done.task_id, DONE);
        Ok(Completion::default())
    }

    fn set_retry_after(&self, _: &str, _: f64) -> Result<(), PortError> {
        Ok(())
    }

    fn random_unit(&self) -> Result<f64, PortError> {
        Ok(0.0)
    }

    fn cancel_state(&self) -> Result<CancelState, PortError> {
        Ok(CancelState::default())
    }

    fn cancel_begin(&self, _: &str, _: f64, _: usize) -> Result<(), PortError> {
        Ok(())
    }

    fn kill(&self, _: f64, _: &str) -> Result<(), PortError> {
        Ok(())
    }

    fn log(&self, _: &str) -> Result<(), PortError> {
        Ok(())
    }
}

/// The CPU time the calling thread has used, in nanoseconds, from `/proc/thread-self/schedstat` (Linux with schedstats).
fn thread_cpu_ns() -> Option<u64> {
    std::fs::read_to_string("/proc/thread-self/schedstat")
        .ok()?
        .split_whitespace()
        .next()?
        .parse()
        .ok()
}

fn loadavg() -> String {
    std::fs::read_to_string("/proc/loadavg")
        .map_or_else(|_| "unavailable".to_owned(), |text| text.trim().to_owned())
}

/// The value at quantile `q` (0 to 1) of an ascending-sorted slice.
fn quantile(sorted: &[u64], q: f64) -> u64 {
    if sorted.is_empty() {
        return 0;
    }
    let index = ((sorted.len() - 1) as f64 * q).round() as usize;
    sorted[index]
}

/// Runs the loop `runs` times over `tasks` ready tasks on `workers` workers and prints the figures.
fn bench_loop(tasks: usize, workers: u32, runs: usize) {
    let config = RunConfig {
        max_concurrent: workers,
        budget: u64::MAX,
        ..RunConfig::default()
    };
    let mut all_ns: Vec<u64> = Vec::new();
    let mut dispatching_ns: Vec<u64> = Vec::new();
    let mut per_dispatch_ns: Vec<u64> = Vec::new();
    let mut per_task_ns: Vec<u64> = Vec::new();
    let (mut passes_total, mut cpu_total, mut cpu_runs) = (0_u64, 0_u64, 0_u64);
    for round in 0..runs + 5 {
        let stub = Arc::new(Stub::new(tasks));
        let cpu_before = thread_cpu_ns();
        let started = Instant::now();
        let summary = match run(Arc::clone(&stub) as Arc<dyn SchedulerPorts>, &config) {
            Ok(summary) => summary,
            Err(error) => {
                eprintln!("the loop failed: {error}");
                return;
            }
        };
        let wall = started.elapsed();
        let cpu_after = thread_cpu_ns();
        if round < 5 {
            continue; // warm-up
        }
        assert_eq!(summary.dispatched, tasks as u64);
        per_task_ns.push(u64::try_from(wall.as_nanos() / tasks as u128).unwrap_or(u64::MAX));
        for &(ns, submitted) in stub
            .pass_ns
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .iter()
        {
            all_ns.push(ns);
            if submitted > 0 {
                dispatching_ns.push(ns);
                per_dispatch_ns.push(ns / submitted as u64);
            }
        }
        passes_total += summary.rounds;
        if let (Some(before), Some(after)) = (cpu_before, cpu_after) {
            cpu_total += after - before;
            cpu_runs += 1;
        }
    }
    for list in [
        &mut all_ns,
        &mut dispatching_ns,
        &mut per_dispatch_ns,
        &mut per_task_ns,
    ] {
        list.sort_unstable();
    }
    let cpu_per_pass = if cpu_runs > 0 && passes_total > 0 {
        format!(
            "{:.1}",
            cpu_total as f64 / 1000.0 / passes_total as f64 * (runs as f64 / cpu_runs as f64)
        )
    } else {
        "n/a".to_owned()
    };
    let us = |sorted: &[u64], q: f64| quantile(sorted, q) as f64 / 1000.0;
    println!(
        "{tasks:>4} tasks {workers:>2} workers | passes/run {:>5.1} (dispatching {:>5.1}) | all passes p50 {:>7.1} p99 {:>8.1} us | dispatching passes p50 {:>7.1} p99 {:>8.1} us | per dispatched task p50 {:>6.1} p99 {:>7.1} us | loop cpu/pass {:>6} us | end-to-end per task p50 {:>7.1} p99 {:>7.1} us",
        passes_total as f64 / runs as f64,
        dispatching_ns.len() as f64 / runs as f64,
        us(&all_ns, 0.5),
        us(&all_ns, 0.99),
        us(&dispatching_ns, 0.5),
        us(&dispatching_ns, 0.99),
        us(&per_dispatch_ns, 0.5),
        us(&per_dispatch_ns, 0.99),
        cpu_per_pass,
        us(&per_task_ns, 0.5),
        us(&per_task_ns, 0.99),
    );
}

/// The cost of the pool alone: `jobs` immediate jobs submitted on `workers` workers and their completions read back, per job.
fn bench_pool(workers: usize, jobs: usize) {
    let mut per_job: Vec<u64> = (0..50)
        .map(|_| {
            let pool: Pool<u32> = Pool::new(workers);
            let started = Instant::now();
            for n in 0..jobs {
                if pool.submit(format!("J{n}"), || 0).is_err() {
                    return u64::MAX;
                }
            }
            let mut seen = 0;
            while seen < jobs {
                if pool.next_completion(Duration::from_secs(5)).is_none() {
                    return u64::MAX;
                }
                seen += 1;
            }
            u64::try_from(started.elapsed().as_nanos() / jobs as u128).unwrap_or(u64::MAX)
        })
        .collect();
    per_job.sort_unstable();
    println!(
        "  Pool: {jobs:>3} immediate jobs on {workers:>2} workers   per job p50 {:>7.1} us p99 {:>7.1} us (submit with a thread start + completion)",
        quantile(&per_job, 0.5) as f64 / 1000.0,
        quantile(&per_job, 0.99) as f64 / 1000.0,
    );
}

/// The median over `samples` of the time of one call, measured as `iters` calls per sample, in nanoseconds.
fn per_call_ns(iters: usize, samples: usize, mut call: impl FnMut()) -> f64 {
    let mut times: Vec<f64> = (0..samples)
        .map(|_| {
            let started = Instant::now();
            for _ in 0..iters {
                call();
            }
            started.elapsed().as_nanos() as f64 / iters as f64
        })
        .collect();
    times.sort_by(f64::total_cmp);
    times[times.len() / 2]
}

fn bench_primitives() {
    println!(
        "\nper-call cost of the primitives the Python loop runs per pass (median of at least 50 samples):"
    );
    for rows in [50_usize, 500] {
        let data: Vec<(Option<String>, Option<i64>, Option<f64>)> = (0..rows)
            .map(|i| {
                (
                    Some(["P0", "P1", "P2", "P3"][(i * 7) % 4].to_owned()),
                    Some((i % 7) as i64),
                    Some(((i * 37) % 101) as f64),
                )
            })
            .collect();
        let ns = per_call_ns(200, 200, || {
            black_box(order_ready(data.iter().map(
                |(priority, depth, created)| (priority.as_deref(), *depth, *created),
            )));
        });
        println!(
            "  order_ready over {rows:>3} rows        {:>10.1} us",
            ns / 1000.0
        );
    }
    let owners: Vec<String> = (0..50).map(|i| format!("T{i}")).collect();
    let patterns: Vec<String> = (0..50).map(|i| format!("src/mod{i}/**")).collect();
    let ns = per_call_ns(4, 50, || {
        let mut table = ClaimTable::new();
        for (owner, pattern) in owners.iter().zip(&patterns) {
            let _ = black_box(table.acquire(owner, &[(pattern.as_str(), ClaimMode::Exclusive)]));
        }
    });
    println!(
        "  ClaimTable: 50 acquires, 1 claim each, into a fresh table  {:>10.1} us total, {:>8.1} us per acquire (grows with the table)",
        ns / 1000.0,
        ns / 50.0 / 1000.0
    );
    let ns = per_call_ns(4, 50, || {
        let mut table = ClaimTable::new();
        let all: Vec<(&str, ClaimMode)> = patterns
            .iter()
            .map(|pattern| (pattern.as_str(), ClaimMode::Exclusive))
            .collect();
        let _ = black_box(table.acquire("T0", &all));
    });
    println!(
        "  ClaimTable: one acquire of 50 claims            {:>10.1} us",
        ns / 1000.0
    );
    let ns = per_call_ns(4, 50, || {
        let mut table = ClaimTable::new();
        for (owner, pattern) in owners.iter().zip(&patterns) {
            let _ = table.acquire(owner, &[(pattern.as_str(), ClaimMode::Exclusive)]);
        }
        for owner in &owners {
            black_box(table.release(owner));
        }
    });
    println!(
        "  ClaimTable: 50 acquires then 50 releases        {:>10.1} us total",
        ns / 1000.0
    );
    let now = Instant::now();
    let ids: Vec<String> = (0..64).map(|i| format!("T{i}")).collect();
    let fresh = || {
        AimdLimiter::new(32, 1, Duration::from_secs(60))
            .map(|limiter| limiter.with_streak_threshold(3))
    };
    if let Ok(template) = fresh() {
        let mut limiter = template.clone();
        println!(
            "  AimdLimiter::on_success                         {:>10.1} ns",
            per_call_ns(100_000, 200, || {
                black_box(limiter.on_success(now));
            })
        );
        let mut limiter = template.clone();
        println!(
            "  AimdLimiter::on_rate_limit                      {:>10.1} ns",
            per_call_ns(100_000, 200, || {
                black_box(limiter.on_rate_limit(now, None));
            })
        );
        let mut limiter = template;
        let mut next = 0_usize;
        println!(
            "  AimdLimiter::on_failure (distinct task ids)     {:>10.1} ns",
            per_call_ns(100_000, 200, || {
                next = (next + 1) % ids.len();
                black_box(limiter.on_failure(now, &ids[next]));
            })
        );
    }
    let mut attempt = 0_i64;
    println!(
        "  backoff_s                                       {:>10.1} ns",
        per_call_ns(1_000_000, 200, || {
            attempt = attempt % 8 + 1;
            let _ = black_box(backoff_s(attempt, 0.1, 5.0, 0.25, 0.5));
        })
    );
}

fn main() {
    println!("loadavg at start: {}", loadavg());
    println!("pool alone (thread start per job, std::thread):");
    for (workers, jobs) in [(4, 50), (32, 50), (4, 500), (32, 500)] {
        bench_pool(workers, jobs);
    }
    println!("loop with stub ports (zero-cost closures), release build expected:");
    for (tasks, workers, runs) in [(50, 4, 200), (50, 32, 200), (500, 4, 40), (500, 32, 40)] {
        bench_loop(tasks, workers, runs);
    }
    bench_primitives();
    println!("\nloadavg at end: {}", loadavg());
}
