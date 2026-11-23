//! Graph observability bench (Wave5, D3: wall-clock, no SLO assertions).
//!
//! Representative decomposed workload: N=500 nodes of a DAG
//! (root + fan-out layers), each with a tool call. Same workload runs
//! twice: BASELINE (plain `summary()` strings in a Vec, no metrics)
//! vs TELEMETRY (`GraphTelemetry::record` with duration/outcome).
//! INVENTORY baseline: no prior graph_bench numbers existed in
//! INVENTORY.md (bench dir absent); the BASELINE arm here is the
//! before/after anchor — rerun on the same host/workload to compare.
//! Recorded run 2026-11-23 (this host): BASELINE 0.420 ms/run, TELEMETRY
//! 0.957 ms/run, delta +0.538 ms/run (+128.2%), dump 98274 bytes.
//!
//! Run: `cargo bench -p gsd-observability --bench graph_bench` (release).

use std::hint::black_box;
use std::time::Instant;

use gsd_observability::{
    CorrelationContext, EventKind, FeatureFlags, GraphEvent, GraphTelemetry, ResourceLimits,
};

/// Builds `nodes` events for one decomposed graph workload.
fn build_workload(nodes: usize, root: &str) -> Vec<GraphEvent> {
    (0..nodes)
        .map(|i| {
            let node = format!("n-{i}");
            let corr = CorrelationContext::new(root)
                .with_graph("g-bench")
                .with_node(&node)
                .with_subagent("sub-bench")
                .with_tool("swarm_tool");
            match i % 10 {
                0 => GraphEvent::new(EventKind::Submit, corr).with_detail("submit"),
                5 => GraphEvent::new(EventKind::Retry, corr)
                    .with_attempt(2)
                    .with_duration_ms(5),
                _ => GraphEvent::new(EventKind::Dispatch, corr)
                    .with_from("ready")
                    .with_to("running")
                    .with_attempt(1)
                    .with_duration_ms(12)
                    .with_outcome("success")
                    .with_detail("tool ok"),
            }
        })
        .collect()
}

/// Runs `runs` iterations of the workload; returns median wall-clock ms.
fn median_ms<F>(mut run: F, runs: usize) -> f64
where
    F: FnMut(),
{
    let mut times: Vec<f64> = (0..runs)
        .map(|_| {
            let t = Instant::now();
            run();
            t.elapsed().as_secs_f64() * 1000.0
        })
        .collect();
    times.sort_by(f64::total_cmp);
    times[times.len() / 2]
}

fn main() {
    let nodes = 500usize;
    let runs = 20usize;
    let events = build_workload(nodes, "root-bench");

    // BASELINE: format summaries into a Vec (no counters/recorder).
    let baseline_ms = median_ms(
        || {
            let mut sink = Vec::with_capacity(events.len());
            for e in &events {
                sink.push(black_box(e.summary()));
            }
            black_box(sink);
        },
        runs,
    );

    // TELEMETRY: full GraphTelemetry record path (tracing+counters+recorder).
    let telemetry = GraphTelemetry::new(FeatureFlags::default(), ResourceLimits::default());
    let telemetry_ms = median_ms(
        || {
            for e in &events {
                black_box(telemetry.record(e));
            }
            telemetry.registry().record_latency_ms(12.0);
            telemetry.registry().record_queue_wait_ms(3.0);
            telemetry.registry().set_parallelism(16);
            black_box(telemetry.graph_dump_json(500));
        },
        runs,
    );

    let dump_len = telemetry.graph_dump_json(500).len();
    println!("graph_bench: nodes={nodes} runs={runs} workload=decomposed DAG + tool call");
    println!("  BASELINE  (summary only)  median {baseline_ms:.3} ms/run");
    println!("  TELEMETRY (full record)   median {telemetry_ms:.3} ms/run");
    println!(
        "  delta  {:+.3} ms/run ({:+.1}%)  dump_bytes={dump_len} events={}",
        telemetry_ms - baseline_ms,
        (telemetry_ms - baseline_ms) / baseline_ms * 100.0,
        telemetry.recorder().len(),
    );
    println!("  counters: {}", telemetry.registry().snapshot().len());
    for (name, value) in telemetry.registry().snapshot() {
        println!("    {name} = {value}");
    }
}
