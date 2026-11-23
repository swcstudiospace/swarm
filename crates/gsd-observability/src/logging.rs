//! Structured graph events, correlation propagation, centralized redaction,
//! and feature flags for the graph engine.
//!
//! ## Event model
//!
//! Every lifecycle moment the swarm cares about is a [`GraphEvent`]:
//! submit, validate, node transitions, dispatch, retry, timeout, cancel,
//! aggregate, and PyO3 boundary errors. An event always carries a
//! [`CorrelationContext`] (correlation / graph / node / subagent / tool
//! ids), so one request can be followed across the scheduler, the native
//! core, and the Python bridge. Call sites build events through
//! [`GraphEvent::new`] and hand them to the telemetry sink; they never
//! format log lines or scrub secrets themselves.
//!
//! ## Redaction (centralized, not call-site)
//!
//! [`redact_text`] is the single scrubber for every free-text field.
//! [`GraphEvent::with_detail`] runs it automatically, so a secret that
//! slips into a detail string is masked before it reaches tracing, the
//! metrics registry, or any recorder. Identifiers (correlation ids,
//! `sha256:<hex>` digests, node ids) are not free text and are never
//! passed through the scrubber. Mirrors `swarm/redact.py`'s two layers:
//! sensitive key names (`token`, `password`, `*_key`, ...) and token
//! shapes (Bearer, `sk-...`, JWTs, PEM blocks, URL userinfo).
//!
//! ## Tracing propagation
//!
//! [`CorrelationContext::span`] opens a `tracing` span carrying the
//! correlation fields, and [`CorrelationContext::merged`] folds a child
//! context over a parent's (non-empty child fields win). Across the PyO3
//! boundary the same string ids travel as plain arguments; the far side
//! rebuilds the context with [`CorrelationContext::new`], which is what
//! keeps a Python-side event correlated with its native dispatch.
//!
//! ## Feature flags
//!
//! [`FeatureFlags`] gates the three independently disableable paths
//! ([`Path`]): graph orchestration, PyO3 dispatch, and scheduler policy.
//! Disabling one path suppresses only its events/counters
//! (`GraphTelemetry::record` returns `false`); the other paths keep
//! working. Rollback is therefore a flag flip per path, with no data
//! migration and no code change.
//!
//! ## Hardening notes
//!
//! * Emission is best-effort and O(1) (a `tracing` call plus DashMap
//!   increments): it must never block, fail, or slow dispatch, so
//!   recording never does I/O and never returns an error to the caller.
//! * Cancellation and timeouts are enforced by the scheduler at node
//!   boundaries; this crate only records them, it does not implement
//!   preemption or deadlines.
//! * Backpressure is enforced by resource limits (see `metrics`);
//!   when a queue is at its cap the scheduler sheds load before an
//!   event is ever produced.
//!
//! ## Rollback
//!
//! Set the offending path's flag to `false` (e.g.
//! `FeatureFlags { pyo3_dispatch: false, ..Default::default() }`), or
//! remove the `logging`/`metrics` wiring from `lib.rs`. The pre-existing
//! `TracingLayer`/`MetricsSink` API is unchanged, so rollback cannot
//! strand callers.

use std::fmt;

/// Replacement written in place of every masked span.
pub const REDACTED: &str = "[REDACTED]";

/// Maximum characters kept of an event detail after redaction.
const MAX_DETAIL_CHARS: usize = 256;

// ---------------------------------------------------------------------------
// Correlation context
// ---------------------------------------------------------------------------

/// Ids that tie one request together across components.
///
/// Every field is a plain string so the context can cross the PyO3
/// boundary unchanged. Empty means "not applicable to this event".
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct CorrelationContext {
    /// End-to-end correlation id for the request (always set in practice).
    pub correlation_id: String,
    /// Graph the event belongs to.
    pub graph_id: String,
    /// Node the event is about, if any.
    pub node_id: String,
    /// Subagent executing the node, if any.
    pub subagent_id: String,
    /// Tool being invoked, if any.
    pub tool_name: String,
}

impl CorrelationContext {
    /// A context carrying only the correlation id.
    pub fn new(correlation_id: impl Into<String>) -> Self {
        Self {
            correlation_id: correlation_id.into(),
            ..Self::default()
        }
    }

    pub fn with_graph(mut self, graph_id: impl Into<String>) -> Self {
        self.graph_id = graph_id.into();
        self
    }

    pub fn with_node(mut self, node_id: impl Into<String>) -> Self {
        self.node_id = node_id.into();
        self
    }

    pub fn with_subagent(mut self, subagent_id: impl Into<String>) -> Self {
        self.subagent_id = subagent_id.into();
        self
    }

    pub fn with_tool(mut self, tool_name: impl Into<String>) -> Self {
        self.tool_name = tool_name.into();
        self
    }

    /// Child-over-parent merge: a non-empty child field wins, an empty
    /// child field inherits the parent's. This is the propagation step
    /// when a dispatch spawns a child node or a tool call.
    pub fn merged(&self, child: &CorrelationContext) -> CorrelationContext {
        fn pick(child: &str, parent: &str) -> String {
            if child.is_empty() {
                parent.to_string()
            } else {
                child.to_string()
            }
        }
        CorrelationContext {
            correlation_id: pick(&child.correlation_id, &self.correlation_id),
            graph_id: pick(&child.graph_id, &self.graph_id),
            node_id: pick(&child.node_id, &self.node_id),
            subagent_id: pick(&child.subagent_id, &self.subagent_id),
            tool_name: pick(&child.tool_name, &self.tool_name),
        }
    }

    /// Opens a `tracing` span carrying the correlation fields, so any
    /// log line emitted inside the span is automatically correlated.
    pub fn span(&self) -> tracing::Span {
        tracing::info_span!(
            "graph.operation",
            correlation_id = %self.correlation_id,
            graph_id = %self.graph_id,
            node_id = %self.node_id,
            subagent_id = %self.subagent_id,
            tool_name = %self.tool_name,
        )
    }

    /// Stable `key=value` rendering used in summaries and tests.
    pub fn render(&self) -> String {
        let mut out = format!("correlation_id={}", self.correlation_id);
        if !self.graph_id.is_empty() {
            out.push_str(&format!(" graph_id={}", self.graph_id));
        }
        if !self.node_id.is_empty() {
            out.push_str(&format!(" node_id={}", self.node_id));
        }
        if !self.subagent_id.is_empty() {
            out.push_str(&format!(" subagent_id={}", self.subagent_id));
        }
        if !self.tool_name.is_empty() {
            out.push_str(&format!(" tool={}", self.tool_name));
        }
        out
    }
}

// ---------------------------------------------------------------------------
// Events
// ---------------------------------------------------------------------------

/// The lifecycle moments a graph event can describe.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum EventKind {
    Submit,
    Validate,
    Transition,
    Dispatch,
    Retry,
    Timeout,
    Cancel,
    Aggregate,
    PyO3Error,
}

impl EventKind {
    /// Stable wire name; never rename these, log scrapers key on them.
    pub fn as_str(self) -> &'static str {
        match self {
            EventKind::Submit => "graph.submit",
            EventKind::Validate => "graph.validate",
            EventKind::Transition => "node.transition",
            EventKind::Dispatch => "node.dispatch",
            EventKind::Retry => "node.retry",
            EventKind::Timeout => "node.timeout",
            EventKind::Cancel => "node.cancel",
            EventKind::Aggregate => "graph.aggregate",
            EventKind::PyO3Error => "pyo3.error",
        }
    }
}

impl fmt::Display for EventKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// One structured lifecycle event.
///
/// Built at call sites, scrubbed in [`GraphEvent::with_detail`], and
/// consumed by the tracing emitter and the metrics registry. It owns
/// its strings (`Send + Sync`), so it can be handed across the PyO3
/// boundary or to a recorder without borrowing.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GraphEvent {
    kind: EventKind,
    correlation: CorrelationContext,
    from_state: Option<String>,
    to_state: Option<String>,
    attempt: Option<u32>,
    duration_ms: Option<u64>,
    outcome: Option<String>,
    error_code: Option<String>,
    detail: String,
}

impl GraphEvent {
    pub fn new(kind: EventKind, correlation: CorrelationContext) -> Self {
        Self {
            kind,
            correlation,
            from_state: None,
            to_state: None,
            attempt: None,
            duration_ms: None,
            outcome: None,
            error_code: None,
            detail: String::new(),
        }
    }

    pub fn kind(&self) -> EventKind {
        self.kind
    }

    pub fn correlation(&self) -> &CorrelationContext {
        &self.correlation
    }

    pub fn from_state(&self) -> Option<&str> {
        self.from_state.as_deref()
    }

    pub fn to_state(&self) -> Option<&str> {
        self.to_state.as_deref()
    }

    pub fn attempt(&self) -> Option<u32> {
        self.attempt
    }

    pub fn duration_ms(&self) -> Option<u64> {
        self.duration_ms
    }

    pub fn outcome(&self) -> Option<&str> {
        self.outcome.as_deref()
    }

    pub fn error_code(&self) -> Option<&str> {
        self.error_code.as_deref()
    }

    /// The (already redacted) detail text.
    pub fn detail(&self) -> &str {
        &self.detail
    }

    pub fn with_from(mut self, state: impl Into<String>) -> Self {
        self.from_state = Some(state.into());
        self
    }

    pub fn with_to(mut self, state: impl Into<String>) -> Self {
        self.to_state = Some(state.into());
        self
    }

    pub fn with_attempt(mut self, attempt: u32) -> Self {
        self.attempt = Some(attempt);
        self
    }

    pub fn with_duration_ms(mut self, ms: u64) -> Self {
        self.duration_ms = Some(ms);
        self
    }

    pub fn with_outcome(mut self, outcome: impl Into<String>) -> Self {
        self.outcome = Some(outcome.into());
        self
    }

    pub fn with_error_code(mut self, code: impl Into<String>) -> Self {
        self.error_code = Some(code.into());
        self
    }

    /// Sets the detail text, running it through the centralized
    /// redactor and truncating it so a hostile payload cannot flood
    /// the log pipeline. This is the only way detail text enters an
    /// event; call sites never scrub (or forget to scrub) themselves.
    pub fn with_detail(mut self, detail: impl AsRef<str>) -> Self {
        self.detail = truncate(&redact_text(detail.as_ref()), MAX_DETAIL_CHARS);
        self
    }

    /// Stable one-line rendering: kind, correlation ids, transition
    /// endpoints, attempt, error code, and the redacted detail.
    pub fn summary(&self) -> String {
        let mut out = format!("{} {}", self.kind, self.correlation.render());
        if let Some(from) = &self.from_state {
            out.push_str(&format!(" from={from}"));
        }
        if let Some(to) = &self.to_state {
            out.push_str(&format!(" to={to}"));
        }
        if let Some(attempt) = self.attempt {
            out.push_str(&format!(" attempt={attempt}"));
        }
        if let Some(ms) = self.duration_ms {
            out.push_str(&format!(" duration_ms={ms}"));
        }
        if let Some(outcome) = &self.outcome {
            out.push_str(&format!(" outcome={outcome}"));
        }
        if let Some(code) = &self.error_code {
            out.push_str(&format!(" error_code={code}"));
        }
        if !self.detail.is_empty() {
            out.push_str(&format!(" detail={}", self.detail));
        }
        out
    }

    /// Emits the event as one structured `tracing` record carrying the
    /// correlation fields. Never fails and never does I/O beyond the
    /// subscriber's own handling, so it is safe on the dispatch path.
    pub fn emit(&self) {
        tracing::info!(
            event = %self.kind,
            correlation_id = %self.correlation.correlation_id,
            graph_id = %self.correlation.graph_id,
            node_id = %self.correlation.node_id,
            subagent_id = %self.correlation.subagent_id,
            tool_name = %self.correlation.tool_name,
            from_state = ?self.from_state,
            to_state = ?self.to_state,
            attempt = ?self.attempt,
            duration_ms = ?self.duration_ms,
            outcome = ?self.outcome,
            error_code = ?self.error_code,
            detail = %self.detail,
            "graph event"
        );
    }

    /// Structured JSON for one event (root/node/subagent/tool/attempt/duration/outcome).
    pub fn to_json(&self) -> String {
        let mut out = format!(
            "{{\"event\":\"{}\",\"root\":\"{}\",\"node\":\"{}\",\"subagent\":\"{}\",\"tool\":\"{}\"",
            self.kind,
            json_escape(&self.correlation.correlation_id),
            json_escape(&self.correlation.node_id),
            json_escape(&self.correlation.subagent_id),
            json_escape(&self.correlation.tool_name)
        );
        if let Some(v) = &self.from_state {
            out.push_str(&format!(",\"from_state\":\"{}\"", json_escape(v)));
        }
        if let Some(v) = &self.to_state {
            out.push_str(&format!(",\"to_state\":\"{}\"", json_escape(v)));
        }
        if let Some(a) = self.attempt {
            out.push_str(&format!(",\"attempt\":{a}"));
        }
        if let Some(ms) = self.duration_ms {
            out.push_str(&format!(",\"duration_ms\":{ms}"));
        }
        if let Some(v) = &self.outcome {
            out.push_str(&format!(",\"outcome\":\"{}\"", json_escape(v)));
        }
        if let Some(v) = &self.error_code {
            out.push_str(&format!(",\"error_code\":\"{}\"", json_escape(v)));
        }
        if !self.detail.is_empty() {
            out.push_str(&format!(",\"detail\":\"{}\"", json_escape(&self.detail)));
        }
        out.push('}');
        out
    }
}

/// Escapes a string for inclusion in JSON output.
pub fn json_escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out
}

/// In-memory event recorder for tests and short-lived diagnostics.
/// Bounded by the caller: every recorded event is a clone, so a
/// long-lived recorder grows with the run and must be drained.
#[derive(Debug, Default)]
pub struct EventRecorder {
    events: std::sync::Mutex<Vec<GraphEvent>>,
}

impl EventRecorder {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn record(&self, event: &GraphEvent) {
        self.events
            .lock()
            .expect("event recorder lock")
            .push(event.clone());
    }

    pub fn snapshot(&self) -> Vec<GraphEvent> {
        self.events.lock().expect("event recorder lock").clone()
    }

    pub fn len(&self) -> usize {
        self.events.lock().expect("event recorder lock").len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// How many recorded events have this kind.
    pub fn count(&self, kind: EventKind) -> usize {
        self.events
            .lock()
            .expect("event recorder lock")
            .iter()
            .filter(|event| event.kind() == kind)
            .count()
    }

    /// Structured graph dump (no UI): JSON array of up to `max_events`
    /// events. Signals (burn/latency) are derivable from counters, not
    /// embedded here. Secrets already redacted at build time.
    pub fn dump_json(&self, max_events: usize) -> String {
        let events = self.events.lock().expect("event recorder lock");
        let total = events.len();
        let take = total.min(max_events);
        let mut out = format!("{{\"total\":{total},\"shown\":{take},\"events\":[");
        for (i, e) in events[..take].iter().enumerate() {
            if i > 0 {
                out.push(',');
            }
            out.push_str(&e.to_json());
        }
        out.push_str("]}");
        out
    }
}

// ---------------------------------------------------------------------------
// Feature flags
// ---------------------------------------------------------------------------

/// The three independently disableable paths.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Path {
    /// Graph submission, validation, and aggregation.
    GraphOrchestration,
    /// Native-to-Python dispatch and its error surface.
    PyO3Dispatch,
    /// Node scheduling: transitions, retries, timeouts, cancels.
    SchedulerPolicy,
}

impl Path {
    pub fn as_str(self) -> &'static str {
        match self {
            Path::GraphOrchestration => "graph_orchestration",
            Path::PyO3Dispatch => "pyo3_dispatch",
            Path::SchedulerPolicy => "scheduler_policy",
        }
    }
}

impl fmt::Display for Path {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Which event kind belongs to which flag-gated path.
pub fn path_for_kind(kind: EventKind) -> Path {
    match kind {
        EventKind::Submit | EventKind::Validate | EventKind::Aggregate => Path::GraphOrchestration,
        EventKind::Transition | EventKind::Retry | EventKind::Timeout | EventKind::Cancel => {
            Path::SchedulerPolicy
        }
        EventKind::Dispatch | EventKind::PyO3Error => Path::PyO3Dispatch,
    }
}

/// Why a path was refused.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PathDisabled(pub Path);

impl fmt::Display for PathDisabled {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "path {} is disabled", self.0)
    }
}

impl std::error::Error for PathDisabled {}

/// Independent on/off switches for the three paths.
///
/// Defaults are all-on. Each flag gates only its own path, so a
/// misbehaving Python bridge can be switched off without losing
/// scheduler or orchestration telemetry (and vice versa).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FeatureFlags {
    pub graph_orchestration: bool,
    pub pyo3_dispatch: bool,
    pub scheduler_policy: bool,
}

impl Default for FeatureFlags {
    fn default() -> Self {
        Self {
            graph_orchestration: true,
            pyo3_dispatch: true,
            scheduler_policy: true,
        }
    }
}

impl FeatureFlags {
    pub fn all_enabled() -> Self {
        Self::default()
    }

    pub fn all_disabled() -> Self {
        Self {
            graph_orchestration: false,
            pyo3_dispatch: false,
            scheduler_policy: false,
        }
    }

    /// Whether `path` currently emits.
    pub fn allows(&self, path: Path) -> bool {
        match path {
            Path::GraphOrchestration => self.graph_orchestration,
            Path::PyO3Dispatch => self.pyo3_dispatch,
            Path::SchedulerPolicy => self.scheduler_policy,
        }
    }

    /// [`PathDisabled`] when the path is off; the error names the path
    /// so a caller can log which switch to flip for rollback.
    pub fn check(&self, path: Path) -> Result<(), PathDisabled> {
        if self.allows(path) {
            Ok(())
        } else {
            Err(PathDisabled(path))
        }
    }
}

// ---------------------------------------------------------------------------
// Centralized redaction (mirrors swarm/redact.py, std-only)
// ---------------------------------------------------------------------------

fn is_sensitive_token(token: &str) -> bool {
    matches!(
        token,
        "token"
            | "secret"
            | "password"
            | "passwd"
            | "passphrase"
            | "credential"
            | "credentials"
            | "signing"
            | "private"
            | "cookie"
            | "cookies"
            | "auth"
            | "oauth"
            | "authorization"
            | "authorisation"
            | "authentication"
            | "apikey"
            | "accesskey"
            | "secretkey"
            | "privatekey"
            | "signingkey"
            | "sshkey"
            | "masterkey"
            | "encryptionkey"
            | "bearer"
    )
}

/// Splits a key name into lowercase tokens on non-alphanumerics and
/// camelCase boundaries (`apiKey` -> `["api", "key"]`).
fn name_tokens(name: &str) -> Vec<String> {
    let mut tokens = Vec::new();
    let mut current = String::new();
    let mut prev_lower = false;
    let mut prev_upper = false;
    for ch in name.chars() {
        if ch.is_ascii_alphanumeric() {
            let lower = ch.is_ascii_lowercase() || ch.is_ascii_digit();
            let upper = ch.is_ascii_uppercase();
            if !current.is_empty()
                && ((prev_lower && upper) || (prev_upper && upper && ch.is_ascii_lowercase()))
            {
                tokens.push(std::mem::take(&mut current).to_lowercase());
            }
            current.push(ch);
            prev_lower = lower;
            prev_upper = upper;
        } else if !current.is_empty() {
            tokens.push(std::mem::take(&mut current).to_lowercase());
            prev_lower = false;
            prev_upper = false;
        }
    }
    if !current.is_empty() {
        tokens.push(current.to_lowercase());
    }
    tokens
}

/// Prefixes that make a following `key`/`seed` token a credential
/// (mirrors `swarm.redact._KEY_PREFIXES`).
const KEY_PREFIXES: &[&str] = &[
    "api",
    "access",
    "secret",
    "private",
    "signing",
    "ssh",
    "encryption",
    "master",
    "auth",
    "account",
    "session",
    "license",
    "licence",
    "client",
    "ed25519",
    "hmac",
    "jwt",
    "handoff",
    "signer",
];

/// Context words that make a following `pass`/`pwd` token a credential
/// (mirrors `swarm.redact._PASS_CONTEXT`).
const PASS_CONTEXT: &[&str] = &[
    "db",
    "database",
    "sql",
    "mysql",
    "pg",
    "postgres",
    "postgresql",
    "mongo",
    "mongodb",
    "redis",
    "smtp",
    "imap",
    "ftp",
    "sftp",
    "ssh",
    "ldap",
    "amqp",
    "rabbitmq",
    "user",
    "admin",
    "root",
    "login",
    "mail",
    "auth",
    "master",
];

/// Whether a field/key name denotes a credential (mirrors
/// `swarm.redact.is_sensitive_key`). A trailing `file`/`path` token
/// means the value is a location, not the secret itself. Counts and
/// labels such as `tokens: 1200` or `sort_key` stay readable.
pub fn is_sensitive_key(name: &str) -> bool {
    let tokens = name_tokens(name);
    if tokens.len() > 1 && matches!(tokens[tokens.len() - 1].as_str(), "file" | "path") {
        return false;
    }
    for (i, token) in tokens.iter().enumerate() {
        if is_sensitive_token(token) {
            return true;
        }
        // Glued compounds: `PGPASSWORD`, `accesstoken`, `clientsecret`.
        if token.len() > 3
            && [
                "password",
                "passwd",
                "passphrase",
                "token",
                "secret",
                "credential",
                "credentials",
                "apikey",
            ]
            .iter()
            .any(|suffix| token.ends_with(suffix))
        {
            return true;
        }
        if i > 0 {
            let prev = &tokens[i - 1];
            if matches!(token.as_str(), "key" | "seed") && KEY_PREFIXES.contains(&prev.as_str()) {
                return true;
            }
            if matches!(token.as_str(), "pass" | "pwd") && PASS_CONTEXT.contains(&prev.as_str()) {
                return true;
            }
        }
    }
    false
}

fn is_name_byte(b: u8) -> bool {
    b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-' | b'.')
}

fn is_token_byte(b: u8) -> bool {
    b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b'=')
}

/// End of the value starting at `start`: a quoted value runs to its
/// closing quote, a bare value to whitespace or a separator.
fn value_end(bytes: &[u8], start: usize) -> usize {
    let mut i = start;
    if i < bytes.len() && matches!(bytes[i], b'"' | b'\'') {
        let quote = bytes[i];
        i += 1;
        while i < bytes.len() && bytes[i] != quote {
            i += 1;
        }
        return i; // the closing quote itself stays visible
    }
    while i < bytes.len()
        && !matches!(
            bytes[i],
            b' ' | b'\t' | b'\n' | b'\r' | b',' | b';' | b'}' | b']'
        )
    {
        i += 1;
    }
    i
}

/// Span of the value at `start`, excluding surrounding quotes so the
/// quotes stay visible (e.g. JSON `"token": "secret"` keeps its quotes).
fn value_span(bytes: &[u8], start: usize) -> (usize, usize) {
    let end = value_end(bytes, start);
    if start < bytes.len() && matches!(bytes[start], b'"' | b'\'') && end > start + 1 {
        (start + 1, end)
    } else {
        (start, end)
    }
}

/// Whether the value at `start` is already the redaction placeholder.
fn is_already_redacted(bytes: &[u8], start: usize) -> bool {
    bytes[start..].starts_with(REDACTED.as_bytes())
}

fn push_span(spans: &mut Vec<(usize, usize)>, start: usize, end: usize) {
    if end > start {
        spans.push((start, end));
    }
}

fn scan_assignments(bytes: &[u8], spans: &mut Vec<(usize, usize)>) {
    let mut i = 0;
    while i < bytes.len() {
        if !is_name_byte(bytes[i]) {
            i += 1;
            continue;
        }
        let name_start = i;
        while i < bytes.len() && is_name_byte(bytes[i]) {
            i += 1;
        }
        let name_end = i;
        // Skip blanks and an optional closing quote of a quoted key
        // (`"token": ...`), then require `=` or `:`.
        let mut j = i;
        while j < bytes.len() && matches!(bytes[j], b' ' | b'\t' | b'"' | b'\'') {
            j += 1;
        }
        if j >= bytes.len() || !matches!(bytes[j], b'=' | b':') {
            continue;
        }
        let name = std::str::from_utf8(&bytes[name_start..name_end]).unwrap_or("");
        if !is_sensitive_key(name) {
            continue;
        }
        let mut value_start = j + 1;
        while value_start < bytes.len() && matches!(bytes[value_start], b' ' | b'\t') {
            value_start += 1;
        }
        if is_already_redacted(bytes, value_start)
            || (value_start < bytes.len()
                && matches!(bytes[value_start], b'"' | b'\'')
                && is_already_redacted(bytes, value_start + 1))
        {
            continue;
        }
        if name.eq_ignore_ascii_case("authorization") || name.eq_ignore_ascii_case("auth") {
            // Header form (`Authorization: Bearer <token>`): mask the
            // whole remainder of the value, scheme included.
            let mut end = value_start;
            while end < bytes.len() && !matches!(bytes[end], b'\n' | b'\r' | b'"') {
                end += 1;
            }
            push_span(spans, value_start, end);
        } else {
            let (s, e) = value_span(bytes, value_start);
            push_span(spans, s, e);
        }
    }
}

fn scan_bearer(bytes: &[u8], spans: &mut Vec<(usize, usize)>) {
    let needle = b"bearer";
    let mut start = 0;
    while start + needle.len() <= bytes.len() {
        if bytes[start..start + needle.len()].eq_ignore_ascii_case(needle) {
            let after = start + needle.len();
            let boundary_ok = start == 0 || !bytes[start - 1].is_ascii_alphanumeric();
            let spaced = after < bytes.len() && matches!(bytes[after], b' ' | b'\t');
            if boundary_ok && spaced {
                let mut value_start = after;
                while value_start < bytes.len() && matches!(bytes[value_start], b' ' | b'\t') {
                    value_start += 1;
                }
                if !is_already_redacted(bytes, value_start) {
                    let (s, e) = value_span(bytes, value_start);
                    push_span(spans, s, e);
                }
                start = value_start.max(after);
                continue;
            }
        }
        start += 1;
    }
}

/// Byte-window search for an ASCII needle (indices stay byte-exact
/// even for non-ASCII input, which the lossy-text version could not
/// guarantee).
fn find_bytes(haystack: &[u8], needle: &[u8], from: usize) -> Option<usize> {
    if needle.is_empty() || from > haystack.len() {
        return None;
    }
    haystack[from..]
        .windows(needle.len())
        .position(|w| w == needle)
        .map(|rel| from + rel)
}

fn scan_token_shapes(bytes: &[u8], spans: &mut Vec<(usize, usize)>) {
    let prefixes: &[&[u8]] = &[
        b"sk-",
        b"ghp_",
        b"gho_",
        b"ghu_",
        b"ghs_",
        b"ghr_",
        b"github_pat_",
    ];
    let mut i = 0;
    while i < bytes.len() {
        let rest = &bytes[i..];
        // PEM private-key blocks.
        if rest.starts_with(b"-----BEGIN") {
            if let Some(end_rel) = find_bytes(bytes, b"-----END", i) {
                let mut end = end_rel + "-----END".len();
                while end < bytes.len() && bytes[end] != b'\n' {
                    end += 1;
                }
                push_span(spans, i, end);
                i = end;
                continue;
            }
        }
        if prefixes.iter().any(|p| rest.starts_with(p)) {
            let mut end = i;
            while end < bytes.len() && is_token_byte(bytes[end]) {
                end += 1;
            }
            if end - i >= 12 {
                push_span(spans, i, end);
            }
            i = end.max(i + 1);
            continue;
        }
        if rest.starts_with(b"AKIA") {
            let run = rest
                .iter()
                .take_while(|b| b.is_ascii_uppercase() || b.is_ascii_digit())
                .count();
            if run >= 20 {
                push_span(spans, i, i + 20);
                i += 20;
                continue;
            }
        }
        if rest.starts_with(b"xox")
            && rest.len() > 4
            && matches!(rest[3], b'b' | b'a' | b'p' | b'r' | b's')
        {
            let mut end = i;
            while end < bytes.len() && is_token_byte(bytes[end]) {
                end += 1;
            }
            if end - i >= 10 {
                push_span(spans, i, end);
            }
            i = end.max(i + 1);
            continue;
        }
        // JWT: three base64url segments, header segment starts `eyJ`.
        if rest.starts_with(b"eyJ") {
            let mut end = i;
            let mut dots = 0;
            while end < bytes.len() {
                let b = bytes[end];
                if b == b'.' {
                    dots += 1;
                } else if !(b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_')) {
                    break;
                }
                end += 1;
            }
            if dots == 2 && end - i >= 20 {
                push_span(spans, i, end);
                i = end;
                continue;
            }
        }
        i += 1;
    }
}

fn scan_url_userinfo(bytes: &[u8], spans: &mut Vec<(usize, usize)>) {
    let mut from = 0;
    while let Some(rel) = bytes[from..].windows(3).position(|w| w == b"://") {
        let userinfo_start = from + rel + 3;
        let mut end = userinfo_start;
        while end < bytes.len() && !matches!(bytes[end], b'/' | b'?' | b'#' | b' ' | b'\t' | b'\n')
        {
            if bytes[end] == b'@' {
                if let Some(colon) = bytes[userinfo_start..end].iter().position(|b| *b == b':') {
                    push_span(spans, userinfo_start + colon + 1, end);
                }
                break;
            }
            end += 1;
        }
        from = userinfo_start;
    }
}

/// Returns a copy of `input` with every credential masked by
/// `[REDACTED]`. Detectors read the original text and only report
/// spans, so masking is idempotent and order-independent. Benign
/// prose and identifiers pass through unchanged.
pub fn redact_text(input: &str) -> String {
    let bytes = input.as_bytes();
    let mut spans: Vec<(usize, usize)> = Vec::new();
    scan_assignments(bytes, &mut spans);
    scan_bearer(bytes, &mut spans);
    scan_token_shapes(bytes, &mut spans);
    scan_url_userinfo(bytes, &mut spans);
    if spans.is_empty() {
        return input.to_string();
    }
    spans.sort_unstable();
    // Merge overlapping spans (e.g. an Authorization header span and a
    // Bearer span over the same token) so they yield one placeholder,
    // and drop spans already equal to the placeholder (idempotence).
    let mut merged: Vec<(usize, usize)> = Vec::with_capacity(spans.len());
    for (start, end) in spans {
        if let Some(last) = merged.last_mut() {
            if start <= last.1 {
                last.1 = last.1.max(end);
                continue;
            }
        }
        merged.push((start, end));
    }
    let mut out = String::with_capacity(input.len());
    let mut cursor = 0;
    for (start, end) in merged {
        if &input[start..end] == REDACTED {
            continue;
        }
        if start > cursor {
            out.push_str(&input[cursor..start]);
        }
        out.push_str(REDACTED);
        cursor = end;
    }
    out.push_str(&input[cursor..]);
    out
}

/// Redacts, then truncates to `max` characters on a char boundary.
fn truncate(s: &str, max: usize) -> String {
    if s.chars().count() <= max {
        return s.to_string();
    }
    s.chars().take(max).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn redacts_assignments_and_headers() {
        assert_eq!(
            redact_text("api_key=supersecret1 ok"),
            "api_key=[REDACTED] ok"
        );
        assert_eq!(redact_text("password: hunter2"), "password: [REDACTED]");
        assert_eq!(
            redact_text("Authorization: Bearer abc.def.ghi"),
            "Authorization: [REDACTED]"
        );
        assert_eq!(
            redact_text("{\"token\": \"xyz123\"}"),
            "{\"token\": \"[REDACTED]\"}"
        );
    }

    #[test]
    fn redacts_token_shapes() {
        assert_eq!(
            redact_text("key sk-1234567890abcdef end"),
            "key [REDACTED] end"
        );
        assert_eq!(redact_text("ghp_1234567890abcdef"), "[REDACTED]");
        assert_eq!(redact_text("AKIAIOSFODNN7EXAMPLE"), "[REDACTED]");
        let jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signaturepart";
        assert_eq!(redact_text(jwt), "[REDACTED]");
        assert_eq!(
            redact_text("-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----"),
            "[REDACTED]"
        );
        assert_eq!(
            redact_text("see Bearer abc123token"),
            "see Bearer [REDACTED]"
        );
    }

    #[test]
    fn redacts_url_password_only() {
        assert_eq!(
            redact_text("https://user:p4ssw0rd@host.example/x"),
            "https://user:[REDACTED]@host.example/x"
        );
    }

    #[test]
    fn keeps_benign_prose_and_identifiers() {
        assert_eq!(redact_text("tokens: 1200 ok"), "tokens: 1200 ok");
        assert_eq!(redact_text("sort_key = graph-42"), "sort_key = graph-42");
        assert_eq!(
            redact_text("correlation_id=corr-123 graph_id=g-1"),
            "correlation_id=corr-123 graph_id=g-1"
        );
        assert_eq!(
            redact_text(
                "digest sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
            ),
            "digest sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        );
    }

    #[test]
    fn redaction_is_idempotent() {
        let once = redact_text("api_key=supersecret1");
        assert_eq!(redact_text(&once), once);
    }

    #[test]
    fn event_carries_correlation_id() {
        let event = GraphEvent::new(
            EventKind::Dispatch,
            CorrelationContext::new("corr-1")
                .with_graph("g-1")
                .with_node("n-7")
                .with_subagent("sub-3")
                .with_tool("swarm_tool"),
        )
        .with_from("ready")
        .with_to("running")
        .with_attempt(2);
        let summary = event.summary();
        assert!(summary.contains("node.dispatch"), "{summary}");
        assert!(summary.contains("correlation_id=corr-1"), "{summary}");
        assert!(summary.contains("graph_id=g-1"), "{summary}");
        assert!(summary.contains("node_id=n-7"), "{summary}");
        assert!(summary.contains("subagent_id=sub-3"), "{summary}");
        assert!(summary.contains("tool=swarm_tool"), "{summary}");
        assert!(summary.contains("from=ready to=running"), "{summary}");
        assert!(summary.contains("attempt=2"), "{summary}");
    }

    #[test]
    fn event_detail_is_redacted_automatically() {
        let event = GraphEvent::new(EventKind::PyO3Error, CorrelationContext::new("corr-9"))
            .with_error_code("E-INTERNAL")
            .with_detail("bridge failed: api_key=supersecret1 while calling");
        let summary = event.summary();
        assert!(!summary.contains("supersecret1"), "{summary}");
        assert!(summary.contains(REDACTED), "{summary}");
        assert!(summary.contains("error_code=E-INTERNAL"), "{summary}");
    }

    #[test]
    fn correlation_merge_propagates_parent_ids() {
        let parent = CorrelationContext::new("corr-1").with_graph("g-1");
        let child = CorrelationContext::new("").with_node("n-2");
        let merged = parent.merged(&child);
        assert_eq!(merged.correlation_id, "corr-1");
        assert_eq!(merged.graph_id, "g-1");
        assert_eq!(merged.node_id, "n-2");
        // A non-empty child field overrides the parent's.
        let overriding = CorrelationContext::new("corr-2");
        assert_eq!(parent.merged(&overriding).correlation_id, "corr-2");
    }

    #[test]
    fn flags_gate_paths_independently() {
        let flags = FeatureFlags {
            pyo3_dispatch: false,
            ..FeatureFlags::default()
        };
        assert!(flags.check(Path::GraphOrchestration).is_ok());
        assert!(flags.check(Path::SchedulerPolicy).is_ok());
        assert_eq!(
            flags.check(Path::PyO3Dispatch),
            Err(PathDisabled(Path::PyO3Dispatch))
        );
        assert_eq!(path_for_kind(EventKind::Dispatch), Path::PyO3Dispatch);
        assert_eq!(path_for_kind(EventKind::Submit), Path::GraphOrchestration);
        assert_eq!(path_for_kind(EventKind::Retry), Path::SchedulerPolicy);
    }

    #[test]
    fn recorder_counts_by_kind() {
        let recorder = EventRecorder::new();
        let corr = CorrelationContext::new("corr-1");
        recorder.record(&GraphEvent::new(EventKind::Submit, corr.clone()));
        recorder.record(&GraphEvent::new(EventKind::Submit, corr.clone()));
        recorder.record(&GraphEvent::new(EventKind::Retry, corr));
        assert_eq!(recorder.len(), 3);
        assert_eq!(recorder.count(EventKind::Submit), 2);
        assert_eq!(recorder.count(EventKind::Retry), 1);
        assert!(!recorder.is_empty());
    }

    #[test]
    fn detail_truncates_after_redaction() {
        let long = "x".repeat(1000);
        let event =
            GraphEvent::new(EventKind::Validate, CorrelationContext::new("c")).with_detail(long);
        assert!(event.detail().chars().count() <= MAX_DETAIL_CHARS);
    }

    #[test]
    fn event_carries_duration_outcome_and_json_dump() {
        let event = GraphEvent::new(
            EventKind::Dispatch,
            CorrelationContext::new("root-1")
                .with_node("n-1")
                .with_subagent("sub-1")
                .with_tool("swarm_tool"),
        )
        .with_attempt(2)
        .with_duration_ms(42)
        .with_outcome("success")
        .with_detail("ok api_key=supersecret1");
        let summary = event.summary();
        assert!(summary.contains("duration_ms=42"), "{summary}");
        assert!(summary.contains("outcome=success"), "{summary}");
        let json = event.to_json();
        assert!(json.contains("\"root\":\"root-1\""), "{json}");
        assert!(json.contains("\"attempt\":2"), "{json}");
        assert!(json.contains("\"duration_ms\":42"), "{json}");
        assert!(json.contains("\"outcome\":\"success\""), "{json}");
        assert!(!json.contains("supersecret1"), "{json}");
        assert!(json.contains(REDACTED), "{json}");
        let recorder = EventRecorder::new();
        recorder.record(&event);
        let dump = recorder.dump_json(10);
        assert!(dump.contains("\"total\":1"), "{dump}");
        assert!(dump.contains("\"root\":\"root-1\""), "{dump}");
        assert!(!dump.contains("supersecret1"), "{dump}");
    }
}
