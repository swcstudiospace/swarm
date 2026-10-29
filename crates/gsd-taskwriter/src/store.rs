//! The write side of the Python-owned `tasks.db`: open without DDL, `BEGIN IMMEDIATE` transactions, conditional state
//! updates with their audit rows, and append-only verdict and artifact rows.
//!
//! The schema belongs to `swarm/taskstore.py`. This module never creates, alters or drops anything and never sets a journal
//! mode: sqlx sets none unless asked, so a WAL store (`SWARM_TASKSTORE_WAL=1`) stays WAL and a rollback-journal store stays
//! one. `transitions`, `verdicts` and `artifacts` are written by `INSERT` only; there is no `UPDATE` or `DELETE` path for them.

use std::path::PathBuf;
use std::time::Duration;

use gsd_codes::ErrorCode;
use gsd_route::{parse_json, Fields, Json};
use sqlx::sqlite::{SqliteConnectOptions, SqlitePoolOptions, SqliteRow};
use sqlx::{Column, Row, Sqlite, SqlitePool, Transaction, TypeInfo, ValueRef};

use crate::error::{ErrorKind, WriterError};

#[cfg(feature = "fault-injection")]
use fault::{Faults, PausePoint};
#[cfg(feature = "fault-injection")]
use std::sync::Arc;

/// How long a contender waits for the write lock, as `sqlite3.connect(timeout=30)` in `TaskStore.__init__`.
const DEFAULT_BUSY_TIMEOUT: Duration = Duration::from_secs(30);

/// Tables and columns of `swarm/taskstore.py::_SCHEMA` (plus the two envelope columns the `verdicts` migration adds) that
/// this writer reads or writes.
const REQUIRED_SCHEMA: [(&str, &[&str]); 4] = [
    (
        "tasks",
        &[
            "task_id",
            "correlation_id",
            "parent_id",
            "dag_depth",
            "capability",
            "agent_id",
            "title",
            "inputs",
            "outputs",
            "acceptance",
            "budget",
            "risk_class",
            "priority",
            "state",
            "attempt",
            "max_attempts",
            "rework_loops",
            "depends_on",
            "assigned_to",
            "notes",
            "created_at",
            "updated_at",
        ],
    ),
    (
        "transitions",
        &[
            "id",
            "task_id",
            "from_state",
            "to_state",
            "actor",
            "reason",
            "ts",
        ],
    ),
    (
        "verdicts",
        &[
            "id",
            "task_id",
            "gate",
            "verdict",
            "agent_id",
            "findings",
            "expires_at",
            "ts",
            "envelope_json",
            "sig",
        ],
    ),
    (
        "artifacts",
        &[
            "id", "task_id", "kind", "uri", "version", "digest", "producer", "ts",
        ],
    ),
];

/// JSON text columns of `tasks` and the text `TaskStore._row` parses when the stored value is empty.
const JSON_COLUMNS: [(&str, &str); 5] = [
    ("inputs", "[]"),
    ("outputs", "[]"),
    ("acceptance", "[]"),
    ("budget", "{}"),
    ("depends_on", "[]"),
];

/// Where the store lives and how long a contender waits for it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WriterConfig {
    /// Path of `tasks.db`. It must already exist: Python creates and migrates it.
    pub db_path: PathBuf,
    /// Wait for the write lock and for the single pooled connection (30 s by default, like Python).
    pub busy_timeout: Duration,
}

impl WriterConfig {
    /// A configuration for `db_path` with the Python busy timeout of 30 s.
    pub fn new(db_path: impl Into<PathBuf>) -> Self {
        Self {
            db_path: db_path.into(),
            busy_timeout: DEFAULT_BUSY_TIMEOUT,
        }
    }
}

/// An open Task Store with a pool of one connection.
#[derive(Debug)]
pub struct WriteStore {
    pool: SqlitePool,
    #[cfg(feature = "fault-injection")]
    faults: Arc<Faults>,
}

/// Opens the store at `config.db_path` for writing, with no DDL.
///
/// Refuses with a not-started error when the file is absent or unreadable, when one of the four tables is missing or when a
/// column of the schema (including the `verdicts` columns `envelope_json` and `sig`) is missing, so Python migrates first.
pub async fn open_for_write(config: &WriterConfig) -> Result<WriteStore, WriterError> {
    // Python's sqlite3 leaves foreign key enforcement off; sqlx turns it on by default. The schema has no foreign keys,
    // so this only keeps the connection state identical.
    let options = SqliteConnectOptions::new()
        .filename(&config.db_path)
        .create_if_missing(false)
        .foreign_keys(false)
        .busy_timeout(config.busy_timeout);
    let pool = SqlitePoolOptions::new()
        .max_connections(1)
        .acquire_timeout(config.busy_timeout)
        .connect_with(options)
        .await
        .map_err(|error| {
            WriterError::not_started(
                ErrorKind::StoreUnavailable,
                format!(
                    "task store {} cannot be opened: {error}",
                    config.db_path.display()
                ),
            )
        })?;
    if let Some(problem) = schema_problem(&pool).await? {
        pool.close().await;
        return Err(WriterError::not_started(ErrorKind::SchemaNotReady, problem));
    }
    Ok(WriteStore {
        pool,
        #[cfg(feature = "fault-injection")]
        faults: Arc::new(Faults::default()),
    })
}

/// Describes the first missing table or columns, or none when the schema is complete.
async fn schema_problem(pool: &SqlitePool) -> Result<Option<String>, WriterError> {
    for (table, required) in REQUIRED_SCHEMA {
        let rows = sqlx::query("SELECT name FROM pragma_table_info(?)")
            .bind(table)
            .fetch_all(pool)
            .await
            .map_err(|error| {
                WriterError::not_started(
                    ErrorKind::Database,
                    format!("task store schema check of {table}: {error}"),
                )
            })?;
        if rows.is_empty() {
            return Ok(Some(format!("task store table {table} is missing")));
        }
        let mut present: Vec<String> = Vec::with_capacity(rows.len());
        for row in &rows {
            present.push(row.try_get::<String, _>(0).map_err(|error| {
                WriterError::not_started(
                    ErrorKind::Database,
                    format!("task store schema check of {table}: {error}"),
                )
            })?);
        }
        let missing: Vec<&str> = required
            .iter()
            .copied()
            .filter(|column| !present.iter().any(|name| name == column))
            .collect();
        if !missing.is_empty() {
            return Ok(Some(format!(
                "task store table {table} lacks column(s) {}",
                missing.join(", ")
            )));
        }
    }
    Ok(None)
}

impl WriteStore {
    /// Starts a write transaction with `BEGIN IMMEDIATE`, as `TaskStore.transaction()` does.
    ///
    /// Waits up to the busy timeout for the write lock; a timeout is a not-started [`ErrorKind::Busy`] error.
    pub async fn begin(&self) -> Result<Tx, WriterError> {
        let tx = self
            .pool
            .begin_with("BEGIN IMMEDIATE")
            .await
            .map_err(|error| {
                let kind = if is_busy(&error) {
                    ErrorKind::Busy
                } else {
                    ErrorKind::Database
                };
                WriterError::not_started(kind, format!("task store BEGIN IMMEDIATE: {error}"))
            })?;
        let tx = Tx {
            tx,
            wrote: false,
            #[cfg(feature = "fault-injection")]
            faults: Arc::clone(&self.faults),
        };
        #[cfg(feature = "fault-injection")]
        tx.faults.hit(PausePoint::AfterBegin).await;
        Ok(tx)
    }

    /// Closes the connection; a WAL store is checkpointed by SQLite as usual.
    pub async fn close(self) {
        self.pool.close().await;
    }

    /// The pause-point registry of this store (tests only).
    #[cfg(feature = "fault-injection")]
    pub fn faults(&self) -> &Faults {
        &self.faults
    }
}

/// True for `SQLITE_BUSY` and `SQLITE_LOCKED` (including extended codes) and for a pool that timed out waiting for its connection.
fn is_busy(error: &sqlx::Error) -> bool {
    match error {
        sqlx::Error::PoolTimedOut => true,
        sqlx::Error::Database(database) => database
            .code()
            .and_then(|code| code.parse::<i32>().ok())
            .is_some_and(|code| matches!(code & 0xff, 5 | 6)),
        _ => false,
    }
}

/// A `tasks` column a state change may set besides `state` and `updated_at`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PlanColumn<'a> {
    /// `attempt`
    Attempt(i64),
    /// `rework_loops`
    ReworkLoops(i64),
    /// `notes`, the JSON text of the notes object.
    Notes(&'a str),
}

impl PlanColumn<'_> {
    /// The SQL column name; the only identifiers that ever reach the `UPDATE` text.
    fn column(&self) -> &'static str {
        match self {
            PlanColumn::Attempt(_) => "attempt",
            PlanColumn::ReworkLoops(_) => "rework_loops",
            PlanColumn::Notes(_) => "notes",
        }
    }
}

/// One audited state change, as `TaskStore._apply` performs it.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct StateChange<'a> {
    /// The task whose row changes.
    pub task_id: &'a str,
    /// The state the row must be in; the `UPDATE` is conditional on it.
    pub from: &'a str,
    /// The new state.
    pub to: &'a str,
    /// Further columns to set, each at most once.
    pub columns: &'a [PlanColumn<'a>],
    /// `actor` of the audit row.
    pub actor: &'a str,
    /// `reason` of the audit row.
    pub reason: &'a str,
    /// `updated_at` of the task row (Python `time.time()` at the `UPDATE`).
    pub updated_at: f64,
    /// `ts` of the audit row (Python `time.time()` at the `INSERT`).
    pub ts: f64,
}

/// A gate verdict row to append (`TaskStore.record_verdict`).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct NewVerdict<'a> {
    /// `task_id`
    pub task_id: &'a str,
    /// `gate`
    pub gate: &'a str,
    /// `verdict`
    pub verdict: &'a str,
    /// `agent_id`, the envelope's `source`.
    pub agent_id: &'a str,
    /// `findings`, the JSON text of the findings list.
    pub findings: &'a str,
    /// `expires_at`
    pub expires_at: f64,
    /// `ts`
    pub ts: f64,
    /// `envelope_json`, the JSON text of the signed envelope.
    pub envelope_json: &'a str,
    /// `sig`
    pub sig: &'a str,
}

/// An artifact row to append (`TaskStore.add_artifact`).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct NewArtifact<'a> {
    /// `task_id`
    pub task_id: &'a str,
    /// `kind`
    pub kind: &'a str,
    /// `uri`
    pub uri: &'a str,
    /// `version`
    pub version: &'a str,
    /// `digest`
    pub digest: &'a str,
    /// `producer`
    pub producer: &'a str,
    /// `ts`
    pub ts: f64,
}

/// A text column of `tasks` that an op rewrites as a whole, as `TaskStore.update` does.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TextColumn {
    /// `notes`, the JSON text of the notes object.
    Notes,
    /// `outputs`, the JSON text of the outputs list.
    Outputs,
    /// `depends_on`, the JSON text of the dependency list.
    DependsOn,
}

impl TextColumn {
    /// The SQL column name; the only identifiers that ever reach the `UPDATE` text.
    fn column(self) -> &'static str {
        match self {
            TextColumn::Notes => "notes",
            TextColumn::Outputs => "outputs",
            TextColumn::DependsOn => "depends_on",
        }
    }
}

/// A `tasks` row to insert, as `TaskStore.create` binds it. The JSON columns are already the text Python's `json.dumps` prints.
/// `state` is `CREATED`; `outputs`, `attempt`, `rework_loops` and `assigned_to` keep their column defaults.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct NewTaskRow<'a> {
    /// `task_id`
    pub task_id: &'a str,
    /// `correlation_id`
    pub correlation_id: &'a str,
    /// `parent_id`
    pub parent_id: Option<&'a str>,
    /// `dag_depth`
    pub dag_depth: i64,
    /// `capability`
    pub capability: &'a str,
    /// `agent_id`
    pub agent_id: Option<&'a str>,
    /// `title`
    pub title: &'a str,
    /// `inputs`, JSON text.
    pub inputs: &'a str,
    /// `acceptance`, JSON text.
    pub acceptance: &'a str,
    /// `budget`, JSON text.
    pub budget: &'a str,
    /// `risk_class`
    pub risk_class: &'a str,
    /// `priority`
    pub priority: &'a str,
    /// `depends_on`, JSON text.
    pub depends_on: &'a str,
    /// `max_attempts`
    pub max_attempts: i64,
    /// `notes`, JSON text.
    pub notes: &'a str,
    /// `created_at`, `updated_at` and the `ts` of the `NULL -> CREATED` audit row.
    pub now: f64,
}

/// A `BEGIN IMMEDIATE` transaction. Dropping it without [`Tx::commit`] rolls it back.
#[derive(Debug)]
pub struct Tx {
    tx: Transaction<'static, Sqlite>,
    wrote: bool,
    #[cfg(feature = "fault-injection")]
    pub(crate) faults: Arc<Faults>,
}

/// A failure of a statement: not started until the transaction issued its first write, final from then on.
fn failure(wrote: bool, kind: ErrorKind, message: String) -> WriterError {
    if wrote {
        WriterError::final_error(kind, ErrorCode::Internal, message)
    } else {
        WriterError::not_started(kind, message)
    }
}

impl Tx {
    /// Reads the task row as `TaskStore.get` returns it: columns in table order, the JSON columns parsed in place,
    /// `notes_json` appended. `None` when the task does not exist.
    ///
    /// A stored value Rust cannot prove Python reads identically (a JSON column that is not text or that this crate's
    /// parser rejects, such as `NaN` or an integer outside i64) is an [`ErrorKind::Unsupported`] error, so Python answers.
    pub async fn task(&mut self, task_id: &str) -> Result<Option<Fields>, WriterError> {
        let wrote = self.wrote;
        let row = sqlx::query("SELECT * FROM tasks WHERE task_id=?")
            .bind(task_id)
            .fetch_optional(&mut *self.tx)
            .await
            .map_err(|error| {
                failure(
                    wrote,
                    ErrorKind::Database,
                    format!("task store read of {task_id}: {error}"),
                )
            })?;
        let Some(row) = row else {
            return Ok(None);
        };
        let fields = row_fields(&row).and_then(python_dict).map_err(|problem| {
            failure(
                wrote,
                ErrorKind::Unsupported,
                format!("task {task_id}: {problem}"),
            )
        })?;
        Ok(Some(fields))
    }

    /// `TaskStore._apply`: the state `UPDATE` conditional on `change.from`, then the audit `INSERT`.
    ///
    /// `rowcount != 1` is `E-CONTRACT concurrent transition on <task>: expected <from>` (final). The caller rolls back
    /// by dropping or calling [`Tx::rollback`]. Legality of the change is the caller's business.
    pub async fn apply_state(&mut self, change: &StateChange<'_>) -> Result<(), WriterError> {
        for (index, column) in change.columns.iter().enumerate() {
            if change.columns[..index]
                .iter()
                .any(|earlier| earlier.column() == column.column())
            {
                return Err(failure(
                    self.wrote,
                    ErrorKind::Unsupported,
                    format!("column {} is set twice", column.column()),
                ));
            }
        }
        let mut sql = String::from("UPDATE tasks SET state=?");
        for column in change.columns {
            sql.push_str(", ");
            sql.push_str(column.column());
            sql.push_str("=?");
        }
        sql.push_str(", updated_at=? WHERE task_id=? AND state=?");
        let mut update = sqlx::query(&sql).bind(change.to);
        for column in change.columns {
            update = match column {
                PlanColumn::Attempt(value) | PlanColumn::ReworkLoops(value) => update.bind(*value),
                PlanColumn::Notes(text) => update.bind(*text),
            };
        }
        self.wrote = true;
        let done = update
            .bind(change.updated_at)
            .bind(change.task_id)
            .bind(change.from)
            .execute(&mut *self.tx)
            .await
            .map_err(|error| {
                failure(
                    true,
                    ErrorKind::Database,
                    format!("task store state update: {error}"),
                )
            })?;
        if done.rows_affected() != 1 {
            return Err(WriterError::final_error(
                ErrorKind::ConcurrentTransition,
                ErrorCode::Contract,
                format!(
                    "concurrent transition on {}: expected {}",
                    change.task_id, change.from
                ),
            ));
        }
        #[cfg(feature = "fault-injection")]
        self.faults.hit(PausePoint::AfterStateUpdate).await;
        sqlx::query(
            "INSERT INTO transitions (task_id, from_state, to_state, actor, reason, ts) VALUES (?,?,?,?,?,?)",
        )
        .bind(change.task_id)
        .bind(change.from)
        .bind(change.to)
        .bind(change.actor)
        .bind(change.reason)
        .bind(change.ts)
        .execute(&mut *self.tx)
        .await
        .map_err(|error| failure(true, ErrorKind::Database, format!("task store audit insert: {error}")))?;
        Ok(())
    }

    /// Appends one `verdicts` row.
    pub async fn insert_verdict(&mut self, verdict: &NewVerdict<'_>) -> Result<(), WriterError> {
        self.wrote = true;
        sqlx::query(
            "INSERT INTO verdicts (task_id, gate, verdict, agent_id, findings, expires_at, ts, envelope_json, sig) \
             VALUES (?,?,?,?,?,?,?,?,?)",
        )
        .bind(verdict.task_id)
        .bind(verdict.gate)
        .bind(verdict.verdict)
        .bind(verdict.agent_id)
        .bind(verdict.findings)
        .bind(verdict.expires_at)
        .bind(verdict.ts)
        .bind(verdict.envelope_json)
        .bind(verdict.sig)
        .execute(&mut *self.tx)
        .await
        .map_err(|error| failure(true, ErrorKind::Database, format!("task store verdict insert: {error}")))?;
        Ok(())
    }

    /// Appends one `artifacts` row.
    pub async fn insert_artifact(&mut self, artifact: &NewArtifact<'_>) -> Result<(), WriterError> {
        self.wrote = true;
        sqlx::query(
            "INSERT INTO artifacts (task_id, kind, uri, version, digest, producer, ts) VALUES (?,?,?,?,?,?,?)",
        )
        .bind(artifact.task_id)
        .bind(artifact.kind)
        .bind(artifact.uri)
        .bind(artifact.version)
        .bind(artifact.digest)
        .bind(artifact.producer)
        .bind(artifact.ts)
        .execute(&mut *self.tx)
        .await
        .map_err(|error| failure(true, ErrorKind::Database, format!("task store artifact insert: {error}")))?;
        Ok(())
    }

    /// `TaskStore.update` of one text column: `UPDATE tasks SET <column>=?, updated_at=? WHERE task_id=?`.
    ///
    /// The row must exist (the caller checked inside this transaction); a statement that matches no row is a final error.
    pub async fn update_text(
        &mut self,
        task_id: &str,
        column: TextColumn,
        text: &str,
        updated_at: f64,
    ) -> Result<(), WriterError> {
        self.wrote = true;
        let sql = format!(
            "UPDATE tasks SET {}=?, updated_at=? WHERE task_id=?",
            column.column()
        );
        let done = sqlx::query(&sql)
            .bind(text)
            .bind(updated_at)
            .bind(task_id)
            .execute(&mut *self.tx)
            .await
            .map_err(|error| {
                failure(
                    true,
                    ErrorKind::Database,
                    format!("task store update of {}: {error}", column.column()),
                )
            })?;
        if done.rows_affected() != 1 {
            return Err(WriterError::final_error(
                ErrorKind::Database,
                ErrorCode::Internal,
                format!("task {task_id} vanished inside its own transaction"),
            ));
        }
        Ok(())
    }

    /// `TaskStore.create`: the `tasks` `INSERT` and its `NULL -> CREATED` audit row (`system`, `created`).
    ///
    /// A duplicate `task_id` is the caller's to refuse beforehand; the primary key makes it a final database error here.
    pub async fn insert_task(&mut self, row: &NewTaskRow<'_>) -> Result<(), WriterError> {
        self.wrote = true;
        sqlx::query(
            "INSERT INTO tasks (task_id, correlation_id, parent_id, dag_depth, capability, agent_id, title, \
             inputs, acceptance, budget, risk_class, priority, state, depends_on, max_attempts, \
             notes, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        )
        .bind(row.task_id)
        .bind(row.correlation_id)
        .bind(row.parent_id)
        .bind(row.dag_depth)
        .bind(row.capability)
        .bind(row.agent_id)
        .bind(row.title)
        .bind(row.inputs)
        .bind(row.acceptance)
        .bind(row.budget)
        .bind(row.risk_class)
        .bind(row.priority)
        .bind("CREATED")
        .bind(row.depends_on)
        .bind(row.max_attempts)
        .bind(row.notes)
        .bind(row.now)
        .bind(row.now)
        .execute(&mut *self.tx)
        .await
        .map_err(|error| failure(true, ErrorKind::Database, format!("task store task insert: {error}")))?;
        sqlx::query(
            "INSERT INTO transitions (task_id, from_state, to_state, actor, reason, ts) VALUES (?,?,?,?,?,?)",
        )
        .bind(row.task_id)
        .bind(None::<&str>)
        .bind("CREATED")
        .bind("system")
        .bind("created")
        .bind(row.now)
        .execute(&mut *self.tx)
        .await
        .map_err(|error| failure(true, ErrorKind::Database, format!("task store audit insert: {error}")))?;
        Ok(())
    }

    /// Commits. A failure here is final: whether the commit landed is not known, so nothing is re-run.
    pub async fn commit(self) -> Result<(), WriterError> {
        #[cfg(feature = "fault-injection")]
        self.faults.hit(PausePoint::BeforeCommit).await;
        self.tx.commit().await.map_err(|error| {
            WriterError::final_error(
                ErrorKind::Database,
                ErrorCode::Internal,
                format!("task store commit failed: {error}"),
            )
        })
    }

    /// Rolls back every statement of the transaction and releases the write lock.
    pub async fn rollback(self) -> Result<(), WriterError> {
        self.tx.rollback().await.map_err(|error| {
            WriterError::final_error(
                ErrorKind::Database,
                ErrorCode::Internal,
                format!("task store rollback failed: {error}"),
            )
        })
    }
}

/// One SQLite row as ordered fields typed by the stored value (Python `dict(row)`).
fn row_fields(row: &SqliteRow) -> Result<Fields, String> {
    let mut fields = Vec::with_capacity(row.columns().len());
    for (index, column) in row.columns().iter().enumerate() {
        let raw = row.try_get_raw(index).map_err(|error| error.to_string())?;
        let value = if raw.is_null() {
            Json::Null
        } else {
            match raw.type_info().name() {
                "INTEGER" | "BOOLEAN" => Json::Int(
                    row.try_get::<i64, _>(index)
                        .map_err(|error| error.to_string())?,
                ),
                "REAL" | "NUMERIC" => Json::Float(
                    row.try_get::<f64, _>(index)
                        .map_err(|error| error.to_string())?,
                ),
                "TEXT" => Json::Str(
                    row.try_get::<String, _>(index)
                        .map_err(|error| error.to_string())?,
                ),
                other => {
                    return Err(format!(
                        "column {} has unsupported type {other}",
                        column.name()
                    ))
                }
            }
        };
        fields.push((column.name().to_string(), value));
    }
    Ok(fields)
}

/// `TaskStore._row`: parses the JSON columns in place and appends `notes_json` (an object, `{}` for anything else).
fn python_dict(mut fields: Fields) -> Result<Fields, String> {
    for (column, default) in JSON_COLUMNS {
        let slot = fields
            .iter_mut()
            .find(|(name, _)| name == column)
            .ok_or_else(|| format!("row has no column {column}"))?;
        let text = match &slot.1 {
            Json::Null => default,
            Json::Str(text) if text.is_empty() => default,
            Json::Str(text) => text.as_str(),
            _ => return Err(format!("{column} is not text")),
        };
        let parsed = parse_json(text).map_err(|_| {
            format!("{column} is not JSON that Rust can prove Python parses the same way")
        })?;
        slot.1 = parsed;
    }
    let notes_json = match fields
        .iter()
        .find(|(name, _)| name == "notes")
        .map(|(_, value)| value)
    {
        None | Some(Json::Null) => Json::Obj(Vec::new()),
        Some(Json::Str(text)) if text.is_empty() => Json::Obj(Vec::new()),
        Some(Json::Str(text)) => match parse_json(text) {
            Ok(object @ Json::Obj(_)) => object,
            Ok(_) => Json::Obj(Vec::new()),
            Err(_) => {
                return Err(
                    "notes is not JSON that Rust can prove Python parses the same way".to_string(),
                )
            }
        },
        Some(_) => return Err("notes is not text".to_string()),
    };
    fields.push(("notes_json".to_string(), notes_json));
    Ok(fields)
}

/// Named pause points inside a write transaction, for fault tests. Compiled only with the `fault-injection` feature, which
/// is never enabled by default and never in the shipped binary.
#[cfg(feature = "fault-injection")]
pub mod fault {
    use std::sync::{Mutex, PoisonError};

    use tokio::sync::oneshot;

    /// Where a transaction can be held.
    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    pub enum PausePoint {
        /// Right after `BEGIN IMMEDIATE` succeeded: the write lock is held, nothing is written.
        AfterBegin,
        /// After the conditional state `UPDATE` and before the audit `INSERT` of [`crate::Tx::apply_state`].
        AfterStateUpdate,
        /// At the top of [`crate::Tx::commit`], before `COMMIT`.
        BeforeCommit,
        /// Between two ops of [`crate::TaskStoreWriter::apply_ops`]: the earlier ops are written, the later ones are not, and
        /// nothing is committed.
        BetweenOps,
    }

    #[derive(Debug)]
    struct Armed {
        point: PausePoint,
        reached: oneshot::Sender<()>,
        release: oneshot::Receiver<()>,
    }

    /// The pause points armed on one store. Each arming holds the next transaction that reaches the point, once.
    #[derive(Debug, Default)]
    pub struct Faults {
        armed: Mutex<Vec<Armed>>,
    }

    /// The test's end of one armed pause point.
    #[derive(Debug)]
    pub struct PauseGate {
        reached: Option<oneshot::Receiver<()>>,
        release: oneshot::Sender<()>,
    }

    impl Faults {
        /// Arms `point`: the next transaction on this store that reaches it stops there until [`PauseGate::release`].
        pub fn arm(&self, point: PausePoint) -> PauseGate {
            let (reached_tx, reached_rx) = oneshot::channel();
            let (release_tx, release_rx) = oneshot::channel();
            self.armed
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .push(Armed {
                    point,
                    reached: reached_tx,
                    release: release_rx,
                });
            PauseGate {
                reached: Some(reached_rx),
                release: release_tx,
            }
        }

        /// Called by the store at a pause point: stops here if the point is armed, otherwise returns at once.
        pub(crate) async fn hit(&self, point: PausePoint) {
            let armed = {
                let mut all = self.armed.lock().unwrap_or_else(PoisonError::into_inner);
                all.iter()
                    .position(|entry| entry.point == point)
                    .map(|index| all.remove(index))
            };
            if let Some(armed) = armed {
                // A dropped gate counts as a release, so an abandoned test never hangs a transaction.
                let _ = armed.reached.send(());
                let _ = armed.release.await;
            }
        }
    }

    impl PauseGate {
        /// Resolves once a transaction is held at the point (immediately if it already is).
        pub async fn wait_reached(&mut self) {
            if let Some(reached) = self.reached.take() {
                let _ = reached.await;
            }
        }

        /// Lets the held transaction continue.
        pub fn release(self) {
            let _ = self.release.send(());
        }
    }
}
