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
            let exporter = match SpanExporter::builder()
                .with_tonic()
                .with_endpoint(endpoint.clone())
                .build()
            {
                Ok(exporter) => Some(exporter),
                Err(err) => {
                    tracing::warn!(
                        endpoint = %endpoint,
                        error = %err,
                        "OTLP span exporter failed to build; spans will not be exported"
                    );
                    None
                }
            };

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

        if let Err(err) = tracing::subscriber::set_global_default(subscriber) {
            tracing::warn!(error = %err, "logging subscriber already set; keeping the existing one");
        }
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

    /// Flushes buffered spans to the exporter and shuts the pipeline down.
    /// A no-op when no tracer provider was installed.
    pub fn shutdown(&self) {
        if let Some(provider) = &self.tracer_provider {
            if let Err(err) = provider.shutdown() {
                tracing::warn!(error = %err, "span pipeline shutdown failed; buffered spans may be lost");
            }
        }
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
        if let Some(counter) = self.counters.get(name) {
            counter.fetch_add(value, Ordering::Relaxed);
            return;
        }
        self.counters
            .entry(name.to_string())
            .or_insert_with(|| AtomicU64::new(0))
            .fetch_add(value, Ordering::Relaxed);
    }

    pub fn set_gauge(&self, name: &str, value: f64) {
        if let Some(gauge) = self.gauges.get(name) {
            gauge.store(value.to_bits(), Ordering::Relaxed);
            return;
        }
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicUsize;
    use tracing_subscriber::layer::{Context as LayerContext, Layer};

    #[derive(Clone, Default)]
    struct WarnCounter(Arc<AtomicUsize>);

    impl<S: tracing::Subscriber> Layer<S> for WarnCounter {
        fn on_event(&self, event: &tracing::Event<'_>, _ctx: LayerContext<'_, S>) {
            if *event.metadata().level() == tracing::Level::WARN {
                self.0.fetch_add(1, Ordering::SeqCst);
            }
        }
    }

    #[test]
    fn failed_exporter_build_warns_and_disables_tracing() {
        let warnings = WarnCounter::default();
        let subscriber = Registry::default().with(warnings.clone());
        let config = ObservabilityConfig {
            // "[::1" is not a valid URI, so the OTLP endpoint cannot be built.
            otlp_endpoint: Some("http://[::1".to_string()),
            ..ObservabilityConfig::default()
        };

        let layer = tracing::subscriber::with_default(subscriber, || TracingLayer::new(config));

        assert_eq!(warnings.0.load(Ordering::SeqCst), 1);
        assert!(layer.tracer_provider.is_none());
    }

    #[derive(Debug, Clone, Default)]
    struct RecordingExporter(Arc<std::sync::Mutex<Vec<opentelemetry_sdk::trace::SpanData>>>);

    impl opentelemetry_sdk::trace::SpanExporter for RecordingExporter {
        async fn export(
            &self,
            batch: Vec<opentelemetry_sdk::trace::SpanData>,
        ) -> opentelemetry_sdk::error::OTelSdkResult {
            self.0.lock().unwrap().extend(batch);
            Ok(())
        }
    }

    #[test]
    fn shutdown_flushes_spans_recorded_before_it() {
        use opentelemetry::trace::{Span as _, Tracer as _};

        let exported = RecordingExporter::default();
        let layer = TracingLayer {
            tracer_provider: Some(
                opentelemetry_sdk::trace::SdkTracerProvider::builder()
                    .with_batch_exporter(exported.clone())
                    .build(),
            ),
            metrics_sink: Arc::new(MetricsSink::new()),
        };

        let tracer = layer.tracer();
        let mut span = tracer.start("before-shutdown");
        span.end();

        layer.shutdown();

        let names: Vec<String> = exported
            .0
            .lock()
            .unwrap()
            .iter()
            .map(|span| span.name.to_string())
            .collect();
        assert_eq!(names, vec!["before-shutdown".to_string()]);
    }

    #[test]
    fn rejected_logging_init_emits_warning() {
        let warnings = WarnCounter::default();
        let layer = TracingLayer::new(ObservabilityConfig::default());

        tracing::subscriber::with_default(Registry::default().with(warnings.clone()), || {
            // The first call installs the global subscriber; the second is rejected.
            layer.init_logging();
            layer.init_logging();
        });

        assert_eq!(warnings.0.load(Ordering::SeqCst), 1);
    }

    mod alloc_count {
        use std::alloc::{GlobalAlloc, Layout, System};
        use std::cell::Cell;

        thread_local! {
            static ALLOCATIONS: Cell<usize> = const { Cell::new(0) };
        }

        pub struct CountingAlloc;

        unsafe impl GlobalAlloc for CountingAlloc {
            unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
                ALLOCATIONS.with(|count| count.set(count.get() + 1));
                System.alloc(layout)
            }

            unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
                System.dealloc(ptr, layout)
            }

            unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
                ALLOCATIONS.with(|count| count.set(count.get() + 1));
                System.realloc(ptr, layout, new_size)
            }
        }

        /// Runs `f` and returns its result with the number of heap allocations
        /// made on this thread while it ran.
        pub fn allocations_during<R>(f: impl FnOnce() -> R) -> (R, usize) {
            let before = ALLOCATIONS.with(Cell::get);
            let out = f();
            (out, ALLOCATIONS.with(Cell::get) - before)
        }
    }

    #[global_allocator]
    static GLOBAL: alloc_count::CountingAlloc = alloc_count::CountingAlloc;

    #[test]
    fn metrics_hit_path_does_not_allocate() {
        let sink = MetricsSink::new();
        sink.increment("requests", 1);
        sink.set_gauge("queue_depth", 1.0);

        let ((), allocations) = alloc_count::allocations_during(|| {
            sink.increment("requests", 1);
            sink.set_gauge("queue_depth", 2.0);
        });

        assert_eq!(allocations, 0);
    }
}
