//! Metrics registry, resource limits, and the telemetry bundle for the
//! graph engine.
//!
//! ## Metrics registry
//!
//! [`MetricsRegistry`] holds the counters and gauges the scheduler
//! publishes: graphs submitted, nodes by state, queue depth,
//! concurrency, tool outcomes, retries/timeouts, and PyO3 conversion
//! errors. Names are stable (`gsd_*`); label values come from closed
//! vocabularies ([`ToolOutcome`], canonical node states) so a caller
//! can never explode cardinality with a free-form string — an
//! unrecognized state is counted under `unknown`, not under its raw
//! value. Hit paths use pre-interned names and DashMap `get` first,
//! so recording on the dispatch path does not allocate.
//!
//! [`MetricsRegistry::record_event`] is the single mapping from an
//! [`GraphEvent`] to counters, keeping the event vocabulary and the
//! metric vocabulary in one place.
//!
//! ## Resource limits
//!
//! [`ResourceLimits`] are the conservative caps the scheduler enforces
//! before work is admitted: graph size, depth, fan-out, runtime,
//! memory, and the Python-invocation bound (count and per-call
//! timeout). Every `check_*` returns a typed [`LimitViolation`]
//! naming the resource and both values, so a rejection is itself
//! loggable as an event. These are design constants, not operator
//! policy: a host may lower them, never silently raise them.
//!
//! ## Backpressure, cancellation, timeout (hardening notes)
//!
//! * Backpressure: [`ResourceLimits::admit_queue`] admits only while
//!   the queue is below its cap. The scheduler is expected to shed or
//!   defer before dispatch; recording a shed as an event is optional
//!   and never required for correctness.
//! * Cancellation: nodes observe cancellation at transition
//!   boundaries, never mid-Python-call. The Python invocation bound
//!   caps how long a single call can hold the GIL, so a stuck call
//!   cannot stall cancellation of the rest of the graph.
//! * Timeouts: the per-invocation timeout and the graph runtime cap
//!   are enforced by the scheduler's deadlines; this crate only
//!   records the resulting `timeout` events and counts them.
//! * Recording is best-effort (see `logging`): a metrics failure can
//!   never fail a dispatch, because recording never returns an error.
//!
//! ## Rollback
//!
//! Flip a [`FeatureFlags`] switch to suppress one path's telemetry, or
//! drop [`GraphTelemetry`] and keep using the pre-existing
//! `MetricsSink`. Limits are enforced by their callers, so removing
//! this module only removes enforcement, never correctness of the
//! graph model itself.

use std::fmt;
use std::sync::atomic::{AtomicU64, Ordering};

use dashmap::DashMap;

use crate::logging::{path_for_kind, EventKind, EventRecorder, FeatureFlags, GraphEvent};

// ---------------------------------------------------------------------------
// Tool outcomes
// ---------------------------------------------------------------------------

/// Closed vocabulary for tool-call outcomes. Adding a variant is a
/// metric-schema change; free-form outcomes are not accepted.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ToolOutcome {
    Success,
    Failure,
    Timeout,
    Cancelled,
    Error,
}

impl ToolOutcome {
    pub fn as_str(self) -> &'static str {
        match self {
            ToolOutcome::Success => "success",
            ToolOutcome::Failure => "failure",
            ToolOutcome::Timeout => "timeout",
            ToolOutcome::Cancelled => "cancelled",
            ToolOutcome::Error => "error",
        }
    }
}

/// Canonical node states (mirrors the scheduler FSM); anything else
/// collapses to `unknown` so metric cardinality stays bounded.
pub const NODE_STATES: &[&str] = &[
    "queued",
    "ready",
    "running",
    "paused",
    "succeeded",
    "failed",
    "cancelled",
    "timed_out",
    "skipped",
];

fn canonical_state(state: &str) -> &'static str {
    match state {
        "queued" => "queued",
        "ready" => "ready",
        "running" => "running",
        "paused" => "paused",
        "succeeded" => "succeeded",
        "failed" => "failed",
        "cancelled" => "cancelled",
        "timed_out" => "timed_out",
        "skipped" => "skipped",
        _ => "unknown",
    }
}

// ---------------------------------------------------------------------------
// Metrics registry
// ---------------------------------------------------------------------------

/// Counter/gauge registry for graph telemetry. Thread-safe; every
/// method is O(1) and infallible by design.
#[derive(Debug, Default)]
pub struct MetricsRegistry {
    counters: DashMap<String, AtomicU64>,
    gauges: DashMap<String, AtomicU64>,
}

impl MetricsRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    fn inc(&self, name: &'static str, value: u64) {
        if let Some(counter) = self.counters.get(name) {
            counter.fetch_add(value, Ordering::Relaxed);
            return;
        }
        self.counters
            .entry(name.to_string())
            .or_insert_with(|| AtomicU64::new(0))
            .fetch_add(value, Ordering::Relaxed);
    }

    fn set(&self, name: &'static str, value: f64) {
        if let Some(gauge) = self.gauges.get(name) {
            gauge.store(value.to_bits(), Ordering::Relaxed);
            return;
        }
        self.gauges
            .entry(name.to_string())
            .or_insert_with(|| AtomicU64::new(0))
            .store(value.to_bits(), Ordering::Relaxed);
    }

    /// Current counter value (0 when never incremented).
    pub fn counter(&self, name: &str) -> u64 {
        self.counters
            .get(name)
            .map(|counter| counter.load(Ordering::Relaxed))
            .unwrap_or(0)
    }

    /// Current gauge value (0.0 when never set).
    pub fn gauge(&self, name: &str) -> f64 {
        self.gauges
            .get(name)
            .map(|gauge| f64::from_bits(gauge.load(Ordering::Relaxed)))
            .unwrap_or(0.0)
    }

    /// Every counter as `(name, value)`, sorted by name for stable
    /// assertions and scrapes.
    pub fn snapshot(&self) -> Vec<(String, u64)> {
        let mut rows: Vec<(String, u64)> = self
            .counters
            .iter()
            .map(|row| (row.key().clone(), row.value().load(Ordering::Relaxed)))
            .collect();
        rows.sort_by(|a, b| a.0.cmp(&b.0));
        rows
    }

    pub fn inc_graph_submitted(&self) {
        self.inc("gsd_graphs_submitted_total", 1);
    }

    /// Counts a node entering `state`. Unknown states collapse to
    /// `unknown` (bounded cardinality, never the caller's raw string).
    pub fn inc_node_state(&self, state: &str) {
        let name = match canonical_state(state) {
            "queued" => "gsd_nodes_by_state_queued",
            "ready" => "gsd_nodes_by_state_ready",
            "running" => "gsd_nodes_by_state_running",
            "paused" => "gsd_nodes_by_state_paused",
            "succeeded" => "gsd_nodes_by_state_succeeded",
            "failed" => "gsd_nodes_by_state_failed",
            "cancelled" => "gsd_nodes_by_state_cancelled",
            "timed_out" => "gsd_nodes_by_state_timed_out",
            "skipped" => "gsd_nodes_by_state_skipped",
            _ => "gsd_nodes_by_state_unknown",
        };
        self.inc(name, 1);
    }

    /// Current scheduler queue depth (gauge).
    pub fn set_queue_depth(&self, depth: u64) {
        self.set("gsd_queue_depth", depth as f64);
    }

    /// Currently executing nodes (gauge).
    pub fn set_concurrency(&self, running: u64) {
        self.set("gsd_concurrency", running as f64);
    }

    pub fn inc_tool_outcome(&self, outcome: ToolOutcome) {
        let name = match outcome {
            ToolOutcome::Success => "gsd_tool_outcomes_success",
            ToolOutcome::Failure => "gsd_tool_outcomes_failure",
            ToolOutcome::Timeout => "gsd_tool_outcomes_timeout",
            ToolOutcome::Cancelled => "gsd_tool_outcomes_cancelled",
            ToolOutcome::Error => "gsd_tool_outcomes_error",
        };
        self.inc(name, 1);
    }

    pub fn inc_dispatch(&self) {
        self.inc("gsd_dispatch_total", 1);
    }

    pub fn inc_retry(&self) {
        self.inc("gsd_retries_total", 1);
    }

    pub fn inc_timeout(&self) {
        self.inc("gsd_timeouts_total", 1);
    }

    pub fn inc_cancel(&self) {
        self.inc("gsd_cancels_total", 1);
    }

    pub fn inc_aggregate(&self) {
        self.inc("gsd_aggregates_total", 1);
    }

    pub fn inc_conversion_error(&self) {
        self.inc("gsd_conversion_errors_total", 1);
    }

    /// The single event-to-counter mapping. A transition counts the
    /// node under its destination state; a PyO3 boundary error counts
    /// as a conversion error.
    pub fn record_event(&self, event: &GraphEvent) {
        match event.kind() {
            EventKind::Submit => self.inc_graph_submitted(),
            EventKind::Validate => self.inc("gsd_validations_total", 1),
            EventKind::Transition => {
                if let Some(to) = event.to_state() {
                    self.inc_node_state(to);
                }
            }
            EventKind::Dispatch => self.inc_dispatch(),
            EventKind::Retry => self.inc_retry(),
            EventKind::Timeout => self.inc_timeout(),
            EventKind::Cancel => self.inc_cancel(),
            EventKind::Aggregate => self.inc_aggregate(),
            EventKind::PyO3Error => self.inc_conversion_error(),
        }
    }
}

// ---------------------------------------------------------------------------
// Resource limits
// ---------------------------------------------------------------------------

/// Conservative caps enforced before work is admitted.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ResourceLimits {
    /// Maximum nodes in one graph.
    pub max_nodes: usize,
    /// Maximum dependency depth (edges) in one graph.
    pub max_depth: usize,
    /// Maximum dependents of a single node.
    pub max_fan_out: usize,
    /// Maximum wall-clock runtime of one graph, milliseconds.
    pub max_runtime_ms: u64,
    /// Maximum memory budget of one graph, bytes.
    pub max_memory_bytes: u64,
    /// Maximum Python invocations one graph may perform.
    pub max_python_invocations: usize,
    /// Maximum duration of a single Python invocation, milliseconds.
    pub python_invocation_timeout_ms: u64,
    /// Queue depth at which admission is refused (backpressure).
    pub max_queue_depth: u64,
    /// Maximum concurrently executing nodes.
    pub max_concurrency: usize,
}

impl Default for ResourceLimits {
    fn default() -> Self {
        Self {
            max_nodes: 1024,
            max_depth: 64,
            max_fan_out: 64,
            max_runtime_ms: 300_000,
            max_memory_bytes: 512 * 1024 * 1024,
            max_python_invocations: 1024,
            python_invocation_timeout_ms: 60_000,
            max_queue_depth: 4096,
            max_concurrency: 64,
        }
    }
}

/// A rejected admission, naming the resource and both values so the
/// rejection is itself loggable as an event.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LimitViolation {
    Nodes { requested: usize, limit: usize },
    Depth { requested: usize, limit: usize },
    FanOut { requested: usize, limit: usize },
    RuntimeMs { requested: u64, limit: u64 },
    MemoryBytes { requested: u64, limit: u64 },
    PythonInvocations { requested: usize, limit: usize },
    QueueDepth { requested: u64, limit: u64 },
}

impl fmt::Display for LimitViolation {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match *self {
            LimitViolation::Nodes { requested, limit } => {
                write!(f, "graph nodes {requested} exceed limit {limit}")
            }
            LimitViolation::Depth { requested, limit } => {
                write!(f, "graph depth {requested} exceed limit {limit}")
            }
            LimitViolation::FanOut { requested, limit } => {
                write!(f, "fan-out {requested} exceed limit {limit}")
            }
            LimitViolation::RuntimeMs { requested, limit } => {
                write!(f, "runtime {requested}ms exceed limit {limit}ms")
            }
            LimitViolation::MemoryBytes { requested, limit } => {
                write!(f, "memory {requested} bytes exceed limit {limit} bytes")
            }
            LimitViolation::PythonInvocations { requested, limit } => {
                write!(f, "python invocations {requested} exceed limit {limit}")
            }
            LimitViolation::QueueDepth { requested, limit } => {
                write!(f, "queue depth {requested} exceed limit {limit}")
            }
        }
    }
}

impl std::error::Error for LimitViolation {}

impl ResourceLimits {
    pub fn check_graph(
        &self,
        nodes: usize,
        depth: usize,
        fan_out: usize,
    ) -> Result<(), LimitViolation> {
        if nodes > self.max_nodes {
            return Err(LimitViolation::Nodes {
                requested: nodes,
                limit: self.max_nodes,
            });
        }
        if depth > self.max_depth {
            return Err(LimitViolation::Depth {
                requested: depth,
                limit: self.max_depth,
            });
        }
        if fan_out > self.max_fan_out {
            return Err(LimitViolation::FanOut {
                requested: fan_out,
                limit: self.max_fan_out,
            });
        }
        Ok(())
    }

    pub fn check_runtime(&self, elapsed_ms: u64) -> Result<(), LimitViolation> {
        if elapsed_ms > self.max_runtime_ms {
            return Err(LimitViolation::RuntimeMs {
                requested: elapsed_ms,
                limit: self.max_runtime_ms,
            });
        }
        Ok(())
    }

    pub fn check_memory(&self, bytes: u64) -> Result<(), LimitViolation> {
        if bytes > self.max_memory_bytes {
            return Err(LimitViolation::MemoryBytes {
                requested: bytes,
                limit: self.max_memory_bytes,
            });
        }
        Ok(())
    }

    /// The Python-invocation bound: how many invocations one graph may
    /// still perform. A caller passes the count so far.
    pub fn check_python_invocations(&self, count: usize) -> Result<(), LimitViolation> {
        if count > self.max_python_invocations {
            return Err(LimitViolation::PythonInvocations {
                requested: count,
                limit: self.max_python_invocations,
            });
        }
        Ok(())
    }

    /// Backpressure gate: admit only while the queue is below its cap.
    pub fn admit_queue(&self, current_depth: u64) -> bool {
        current_depth < self.max_queue_depth
    }

    /// Caps a requested concurrency at the configured maximum; the
    /// scheduler runs `min(requested, cap)` workers.
    pub fn cap_concurrency(&self, requested: usize) -> usize {
        requested.min(self.max_concurrency)
    }
}

// ---------------------------------------------------------------------------
// Telemetry bundle
// ---------------------------------------------------------------------------

/// The single wiring point for graph telemetry: feature flags gate
/// which events are recorded, the registry counts them, and the
/// recorder keeps them for tests/diagnostics. Recording is
/// best-effort and never fails a dispatch.
#[derive(Debug, Default)]
pub struct GraphTelemetry {
    flags: FeatureFlags,
    limits: ResourceLimits,
    registry: MetricsRegistry,
    recorder: EventRecorder,
}

impl GraphTelemetry {
    pub fn new(flags: FeatureFlags, limits: ResourceLimits) -> Self {
        Self {
            flags,
            limits,
            registry: MetricsRegistry::new(),
            recorder: EventRecorder::new(),
        }
    }

    pub fn flags(&self) -> &FeatureFlags {
        &self.flags
    }

    pub fn limits(&self) -> &ResourceLimits {
        &self.limits
    }

    pub fn registry(&self) -> &MetricsRegistry {
        &self.registry
    }

    pub fn recorder(&self) -> &EventRecorder {
        &self.recorder
    }

    /// Records `event` (tracing + counter + recorder) when its path is
    /// enabled. Returns `false` when the flag gate suppressed it, so a
    /// caller can tell suppression from recording without re-checking.
    pub fn record(&self, event: &GraphEvent) -> bool {
        if !self.flags.allows(path_for_kind(event.kind())) {
            return false;
        }
        event.emit();
        self.registry.record_event(event);
        self.recorder.record(event);
        true
    }

    /// Admission check for a candidate graph against the limits.
    pub fn check_graph(
        &self,
        nodes: usize,
        depth: usize,
        fan_out: usize,
    ) -> Result<(), LimitViolation> {
        self.limits.check_graph(nodes, depth, fan_out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::logging::CorrelationContext;

    fn corr() -> CorrelationContext {
        CorrelationContext::new("corr-1")
            .with_graph("g-1")
            .with_node("n-1")
    }

    #[test]
    fn registry_counters_increment() {
        let registry = MetricsRegistry::new();
        registry.inc_graph_submitted();
        registry.inc_graph_submitted();
        assert_eq!(registry.counter("gsd_graphs_submitted_total"), 2);

        registry.inc_node_state("running");
        registry.inc_node_state("running");
        registry.inc_node_state("succeeded");
        assert_eq!(registry.counter("gsd_nodes_by_state_running"), 2);
        assert_eq!(registry.counter("gsd_nodes_by_state_succeeded"), 1);

        registry.set_queue_depth(7);
        registry.set_concurrency(3);
        assert_eq!(registry.gauge("gsd_queue_depth"), 7.0);
        assert_eq!(registry.gauge("gsd_concurrency"), 3.0);

        registry.inc_tool_outcome(ToolOutcome::Success);
        registry.inc_tool_outcome(ToolOutcome::Timeout);
        assert_eq!(registry.counter("gsd_tool_outcomes_success"), 1);
        assert_eq!(registry.counter("gsd_tool_outcomes_timeout"), 1);

        registry.inc_retry();
        registry.inc_timeout();
        registry.inc_cancel();
        registry.inc_dispatch();
        registry.inc_aggregate();
        registry.inc_conversion_error();
        assert_eq!(registry.counter("gsd_retries_total"), 1);
        assert_eq!(registry.counter("gsd_timeouts_total"), 1);
        assert_eq!(registry.counter("gsd_cancels_total"), 1);
        assert_eq!(registry.counter("gsd_dispatch_total"), 1);
        assert_eq!(registry.counter("gsd_aggregates_total"), 1);
        assert_eq!(registry.counter("gsd_conversion_errors_total"), 1);

        let snapshot = registry.snapshot();
        assert!(snapshot
            .iter()
            .any(|(name, value)| name == "gsd_graphs_submitted_total" && *value == 2));
        assert_eq!(registry.counter("never_seen"), 0);
    }

    #[test]
    fn unknown_state_collapses_to_bounded_label() {
        let registry = MetricsRegistry::new();
        registry.inc_node_state("user_supplied_garbage");
        registry.inc_node_state("");
        assert_eq!(registry.counter("gsd_nodes_by_state_unknown"), 2);
        assert!(registry
            .snapshot()
            .iter()
            .all(|(name, _)| !name.contains("garbage")));
    }

    #[test]
    fn record_event_maps_kinds_to_counters() {
        let registry = MetricsRegistry::new();
        registry.record_event(&GraphEvent::new(EventKind::Submit, corr()));
        registry.record_event(
            &GraphEvent::new(EventKind::Transition, corr())
                .with_from("ready")
                .with_to("running"),
        );
        registry.record_event(&GraphEvent::new(EventKind::Retry, corr()).with_attempt(2));
        registry.record_event(&GraphEvent::new(EventKind::Timeout, corr()));
        registry.record_event(
            &GraphEvent::new(EventKind::PyO3Error, corr()).with_error_code("E-INTERNAL"),
        );
        assert_eq!(registry.counter("gsd_graphs_submitted_total"), 1);
        assert_eq!(registry.counter("gsd_nodes_by_state_running"), 1);
        assert_eq!(registry.counter("gsd_retries_total"), 1);
        assert_eq!(registry.counter("gsd_timeouts_total"), 1);
        assert_eq!(registry.counter("gsd_conversion_errors_total"), 1);
    }

    #[test]
    fn limits_reject_oversize_admissions() {
        let limits = ResourceLimits::default();
        assert!(limits.check_graph(10, 5, 3).is_ok());
        assert_eq!(
            limits.check_graph(2000, 5, 3),
            Err(LimitViolation::Nodes {
                requested: 2000,
                limit: 1024
            })
        );
        assert!(matches!(
            limits.check_graph(1, 65, 1),
            Err(LimitViolation::Depth { .. })
        ));
        assert!(matches!(
            limits.check_graph(1, 1, 65),
            Err(LimitViolation::FanOut { .. })
        ));
        assert!(limits.check_runtime(1000).is_ok());
        assert!(matches!(
            limits.check_runtime(300_001),
            Err(LimitViolation::RuntimeMs { .. })
        ));
        assert!(matches!(
            limits.check_memory(limits.max_memory_bytes + 1),
            Err(LimitViolation::MemoryBytes { .. })
        ));
        assert!(limits.check_python_invocations(1024).is_ok());
        assert!(matches!(
            limits.check_python_invocations(1025),
            Err(LimitViolation::PythonInvocations { .. })
        ));
    }

    #[test]
    fn backpressure_and_concurrency_cap() {
        let limits = ResourceLimits::default();
        assert!(limits.admit_queue(limits.max_queue_depth - 1));
        assert!(!limits.admit_queue(limits.max_queue_depth));
        assert_eq!(limits.cap_concurrency(10), 10);
        assert_eq!(limits.cap_concurrency(1000), limits.max_concurrency);
    }

    #[test]
    fn flags_gate_telemetry_paths_independently() {
        let telemetry = GraphTelemetry::new(
            FeatureFlags {
                pyo3_dispatch: false,
                ..FeatureFlags::default()
            },
            ResourceLimits::default(),
        );
        // Orchestration and scheduler paths still record.
        assert!(telemetry.record(&GraphEvent::new(EventKind::Submit, corr())));
        assert!(telemetry.record(&GraphEvent::new(EventKind::Transition, corr()).with_to("ready")));
        // PyO3 dispatch is suppressed: no record, no counter.
        assert!(!telemetry.record(&GraphEvent::new(EventKind::Dispatch, corr())));
        assert!(!telemetry.record(&GraphEvent::new(EventKind::PyO3Error, corr())));
        assert_eq!(
            telemetry.registry().counter("gsd_graphs_submitted_total"),
            1
        );
        assert_eq!(telemetry.registry().counter("gsd_dispatch_total"), 0);
        assert_eq!(
            telemetry.registry().counter("gsd_conversion_errors_total"),
            0
        );
        assert_eq!(telemetry.recorder().count(EventKind::Submit), 1);
        assert_eq!(telemetry.recorder().count(EventKind::Dispatch), 0);
    }

    #[test]
    fn telemetry_bundle_delegates_limit_checks() {
        let telemetry = GraphTelemetry::default();
        assert!(telemetry.check_graph(1, 1, 1).is_ok());
        assert!(telemetry.check_graph(9999, 1, 1).is_err());
    }
}
