//! Observability Stack for GSD-Core
//!
//! Structured logging, distributed tracing, metrics, replay/debugging.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use dashmap::DashMap;
use opentelemetry::global;
use opentelemetry::trace::TracerProvider;
use opentelemetry_otlp::{SpanExporter, WithExportConfig};
use opentelemetry_sdk::trace::{Sampler, SdkTracerProvider};
use tracing_subscriber::layer::SubscriberExt;
use tracing_subscriber::{fmt, EnvFilter, Registry};

/// Observability configuration
#[derive(Debug, Clone)]
pub struct ObservabilityConfig {
    pub log_level: String,
    pub json_logs: bool,
    pub otlp_endpoint: Option<String>,
    pub enable_tracing: bool,
    pub enable_metrics: bool,
    pub enable_replay: bool,
    pub sampling_rate: f64,
    pub metrics_port: u16,
}

impl Default for ObservabilityConfig {
    fn default() -> Self {
        Self {
            log_level: "info".to_string(),
            json_logs: true,
            otlp_endpoint: None,
            enable_tracing: true,
            enable_metrics: true,
            enable_replay: true,
            sampling_rate: 0.1,
            metrics_port: 9090,
        }
    }
}

/// Tracing layer for structured logging
pub struct TracingLayer {
    tracer_provider: Option<SdkTracerProvider>,
    metrics_sink: Arc<MetricsSink>,
}

impl TracingLayer {
    pub fn new(config: ObservabilityConfig) -> Self {
        let metrics_sink = Arc::new(MetricsSink::new());

        let tracer_provider = if let Some(endpoint) = &config.otlp_endpoint {
            let exporter = SpanExporter::builder()
                .with_tonic()
                .with_endpoint(endpoint.clone())
                .build()
                .ok();

            if let Some(exporter) = exporter {
                let provider = SdkTracerProvider::builder()
                    .with_batch_exporter(exporter)
                    .with_sampler(Sampler::ParentBased(Box::new(Sampler::TraceIdRatioBased(
                        0.1,
                    ))))
                    .build();

                global::set_tracer_provider(provider.clone());
                Some(provider)
            } else {
                None
            }
        } else {
            None
        };

        Self {
            tracer_provider,
            metrics_sink,
        }
    }

    pub fn init_logging(&self) {
        let filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));

        let subscriber = Registry::default()
            .with(
                fmt::layer()
                    .json()
                    .with_current_span(true)
                    .with_span_list(true),
            )
            .with(filter);

        tracing::subscriber::set_global_default(subscriber).ok();
    }

    pub fn tracer(&self) -> global::BoxedTracer {
        match &self.tracer_provider {
            Some(provider) => global::BoxedTracer::new(Box::new(provider.tracer("gsd-core"))),
            None => global::tracer("gsd-core"),
        }
    }

    pub fn metrics(&self) -> Arc<MetricsSink> {
        self.metrics_sink.clone()
    }
}

/// Metrics sink for Prometheus export
pub struct MetricsSink {
    counters: DashMap<String, AtomicU64>,
    /// Gauge values stored as `f64` bit patterns.
    gauges: DashMap<String, AtomicU64>,
    histograms: DashMap<String, Vec<f64>>,
}

impl MetricsSink {
    fn new() -> Self {
        Self {
            counters: DashMap::new(),
            gauges: DashMap::new(),
            histograms: DashMap::new(),
        }
    }

    pub fn increment(&self, name: &str, value: u64) {
        self.counters
            .entry(name.to_string())
            .or_insert_with(|| AtomicU64::new(0))
            .fetch_add(value, Ordering::Relaxed);
    }

    pub fn set_gauge(&self, name: &str, value: f64) {
        self.gauges
            .entry(name.to_string())
            .or_insert_with(|| AtomicU64::new(0))
            .store(value.to_bits(), Ordering::Relaxed);
    }

    pub fn record_histogram(&self, name: &str, value: f64) {
        self.histograms
            .entry(name.to_string())
            .or_default()
            .push(value);
    }

    /// The protobuf `Metric` carries a single `f64` value and no kind, so it is
    /// recorded as a gauge holding the most recent value.
    pub fn record_metric(&self, metric: gsd_proto::gsd::v1::Metric) {
        self.set_gauge(&metric.name, metric.value);
    }
}

impl Default for MetricsSink {
    fn default() -> Self {
        Self::new()
    }
}

/// Initialize observability
pub fn init_observability(config: ObservabilityConfig) -> Arc<TracingLayer> {
    let layer = Arc::new(TracingLayer::new(config));
    layer.init_logging();
    layer
}

/// Get global tracing layer
static GLOBAL_TRACING: std::sync::OnceLock<Arc<TracingLayer>> = std::sync::OnceLock::new();

pub fn global_tracing() -> Arc<TracingLayer> {
    GLOBAL_TRACING
        .get_or_init(|| Arc::new(TracingLayer::new(ObservabilityConfig::default())))
        .clone()
}

pub fn init_default_observability() {
    let layer = TracingLayer::new(ObservabilityConfig::default());
    layer.init_logging();
    GLOBAL_TRACING.set(Arc::new(layer)).ok();
}
