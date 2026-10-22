//! Staged fan-out and fan-in over Nexus, with a barrier at the end of each stage.
//!
//! [`run_nexus_barrier_join`] is generic over [`NexusPublisher`] and [`NexusSubscriber`] from
//! `gsd-nexus`, which carry [`NexusMessage`] envelopes. Those traits only publish and subscribe, so
//! replies are read through [`NexusInbox`], the receive side this join needs.
//!
//! A stage publishes one request per participant on `<join>/stage/<n>/request`, correlated by task
//! id. Participants answer on `<join>/stage/<n>/reply` with a [`ParticipantReply`]. The barrier waits
//! until every participant has answered or the stage deadline passes. A participant still silent at
//! the deadline becomes a named partial-join timeout, whose session terminal is `CANCELLED_TIMED_OUT`.
//! A failed participant becomes a failed input.
//! Both are values in the stage log, so neither can stop the join. The stage is merged in
//! [`CompletionKey`] order, the same log shape as [`crate::matrix_join`], and the next stage is
//! released only after that merge.
//!
//! The join family is fixed by [`select_join_kind`] before dispatch. [`run_nexus_barrier_join`]
//! refuses any kind other than [`JoinKind::Nexus`], so one run never mixes families.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::time::Duration;

use gsd_failure::{ErrorCode, FailureError};
use gsd_nexus::{NexusMessage, NexusPublisher, NexusSubscriber};
use gsd_proto::gsd::v1::{SchedulingPolicy, Task, WorkflowDef};
use tokio::time::{sleep, Instant};

use crate::force_parallel_dispatch::TaskOutcome;
use crate::matrix_join::{
    CompletionKey, CompletionLog, CompletionRecord, Provenance, VerdictEnvelope,
};
use crate::policy_config::policy_for;
use crate::{SchedulerError, SchedulerResult};

/// Join family a run executes under. Fixed by [`select_join_kind`] before dispatch.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum JoinKind {
    /// Flat fan-out with no barrier; see [`crate::matrix_join`].
    Matrix,
    /// Staged fan-out and fan-in with a barrier at the end of each stage.
    Nexus,
}

/// Receive side of a Nexus subscription, used to read replies.
pub trait NexusInbox {
    /// Removes and returns the next message delivered to `topic`, or `None` when none is waiting.
    fn receive(&self, topic: &str) -> Option<NexusMessage>;
}

/// Reply a participant publishes for one request.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ParticipantReply {
    /// The participant finished and produced this output.
    Completed(Vec<u8>),
    /// The participant failed with this message.
    Failed(String),
}

const TAG_COMPLETED: u8 = 0;
const TAG_FAILED: u8 = 1;

impl ParticipantReply {
    /// Encodes the reply as a message payload: a one-byte tag followed by the body.
    pub fn encode(&self) -> Vec<u8> {
        match self {
            Self::Completed(output) => {
                let mut bytes = Vec::with_capacity(output.len() + 1);
                bytes.push(TAG_COMPLETED);
                bytes.extend_from_slice(output);
                bytes
            }
            Self::Failed(message) => {
                let mut bytes = Vec::with_capacity(message.len() + 1);
                bytes.push(TAG_FAILED);
                bytes.extend_from_slice(message.as_bytes());
                bytes
            }
        }
    }

    /// Decodes a payload written by [`ParticipantReply::encode`].
    ///
    /// Returns an error for an empty payload, an unknown tag, or a failure body that is not UTF-8.
    pub fn decode(payload: &[u8]) -> Result<Self, String> {
        match payload.split_first() {
            Some((&TAG_COMPLETED, body)) => Ok(Self::Completed(body.to_vec())),
            Some((&TAG_FAILED, body)) => std::str::from_utf8(body)
                .map(|message| Self::Failed(message.to_string()))
                .map_err(|error| format!("failure body is not UTF-8: {error}")),
            Some((tag, _)) => Err(format!("unknown reply tag {tag}")),
            None => Err("empty reply payload".to_string()),
        }
    }
}

/// Timing bounds for a staged join.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct NexusJoinConfig {
    /// Time a stage waits for its participants, measured from its fan-out. Must be non-zero.
    pub stage_deadline: Duration,
    /// Pause between reply checks while a stage waits. Must be non-zero.
    pub poll_interval: Duration,
}

impl Default for NexusJoinConfig {
    fn default() -> Self {
        Self {
            stage_deadline: Duration::from_secs(30),
            poll_interval: Duration::from_millis(10),
        }
    }
}

/// A staged join: `stages` run in order, each behind its own barrier.
#[derive(Debug, Clone, Default)]
pub struct NexusPlan {
    /// Name that scopes the stage topics. Must be non-empty.
    pub join_id: String,
    /// Participants of each stage in declared key order. Task ids are unique within a stage.
    pub stages: Vec<Vec<Task>>,
}

/// The Nexus endpoints a join runs over.
pub struct NexusTransport<'a, P, S, R> {
    /// Publishes stage requests.
    pub publisher: &'a P,
    /// Subscribes each stage's reply topic before its fan-out.
    pub subscriber: &'a S,
    /// Reads replies from the subscribed reply topic.
    pub inbox: &'a R,
}

/// Merged result of one stage.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StageMerge {
    /// Stage index, starting at 0.
    pub stage: u32,
    /// Every participant's record in declared key order, with provenance.
    pub log: CompletionLog,
    /// Output bytes of each completed participant, keyed by task id.
    pub outputs: BTreeMap<String, Vec<u8>>,
}

/// Terminal state of a staged join.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum JoinTerminal {
    /// Every participant completed.
    Complete,
    /// At least one participant failed or timed out. The other inputs still merged.
    PartialSuccess,
}

/// Result of one staged join.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NexusJoinReport {
    /// Merged stages in release order.
    pub stages: Vec<StageMerge>,
    /// Task ids that missed a stage deadline, in merge order. Each is a named partial-join timeout that
    /// ends `CANCELLED_TIMED_OUT` (`timed_out`) in the session join.
    pub partial_timeouts: Vec<String>,
    /// Task ids whose participant failed, in merge order.
    pub failed: Vec<String>,
    /// Terminal state of the join.
    pub terminal: JoinTerminal,
}

/// Selects the join family for `workflow` from its `policy`.
///
/// Call this before dispatch and pass the result to [`run_nexus_barrier_join`]. Returns `E-INPUT`
/// for an unknown policy value or a policy with no implementation. The wire `SchedulingPolicy` has
/// no staged variant yet, so every implemented policy selects [`JoinKind::Matrix`]. A staged run is
/// requested by passing [`JoinKind::Nexus`] explicitly.
pub fn select_join_kind(workflow: &WorkflowDef) -> SchedulerResult<JoinKind> {
    let policy = SchedulingPolicy::try_from(workflow.policy).map_err(|_| {
        FailureError::new(
            ErrorCode::Input,
            format!("unknown scheduling policy value {}", workflow.policy),
        )
    })?;
    policy_for(policy)?;
    Ok(JoinKind::Matrix)
}

/// Default request payload: the task's `input_payload` value bytes, or empty when unset.
pub fn task_input_bytes(task: &Task) -> Vec<u8> {
    task.input_payload
        .as_ref()
        .map_or_else(Vec::new, |payload| payload.value.clone())
}

/// Runs `plan` as a staged fan-out and fan-in and returns the merged stages.
///
/// `kind` must be [`JoinKind::Nexus`]; any other kind is refused before anything is published.
/// `build_request` builds each participant's request payload from the task and the stages merged so
/// far. Every stage waits behind a barrier bounded by `config.stage_deadline`. A straggler becomes a
/// named partial-join timeout and a failed participant becomes a failed input. Neither stops the
/// join, and the next stage is released after the merge.
pub async fn run_nexus_barrier_join<P, S, R, B>(
    kind: JoinKind,
    plan: &NexusPlan,
    config: NexusJoinConfig,
    transport: NexusTransport<'_, P, S, R>,
    build_request: B,
) -> SchedulerResult<NexusJoinReport>
where
    P: NexusPublisher,
    S: NexusSubscriber,
    R: NexusInbox,
    B: Fn(&Task, &[StageMerge]) -> Vec<u8>,
{
    if kind != JoinKind::Nexus {
        return Err(invalid(
            "the nexus barrier join refuses a run selected as matrix; the join family is fixed before dispatch",
        ));
    }
    validate(plan, config)?;

    let mut stages: Vec<StageMerge> = Vec::with_capacity(plan.stages.len());
    for (index, tasks) in plan.stages.iter().enumerate() {
        let stage = u32::try_from(index)
            .map_err(|_| invalid(format!("stage index {index} does not fit in u32")))?;
        let contributions = run_stage(
            stage,
            tasks,
            &plan.join_id,
            config,
            &transport,
            &build_request,
            &stages,
        )
        .await;
        stages.push(merge_stage(stage, contributions));
    }

    let timeout = ErrorCode::Timeout.as_str();
    let mut partial_timeouts = Vec::new();
    let mut failed = Vec::new();
    for merge in &stages {
        for record in merge.log.records() {
            match &record.outcome {
                TaskOutcome::Failed { code, .. } if *code == timeout => {
                    partial_timeouts.push(record.key.task_id.clone());
                }
                TaskOutcome::Failed { .. } => failed.push(record.key.task_id.clone()),
                TaskOutcome::Completed | TaskOutcome::RateLimited { .. } => {}
            }
        }
    }
    let terminal = if partial_timeouts.is_empty() && failed.is_empty() {
        JoinTerminal::Complete
    } else {
        JoinTerminal::PartialSuccess
    };
    Ok(NexusJoinReport {
        stages,
        partial_timeouts,
        failed,
        terminal,
    })
}

/// One participant's contribution to a stage: its record and the output it produced, if any.
struct Contribution {
    record: CompletionRecord,
    output: Option<Vec<u8>>,
}

/// Fans out one stage, waits behind its barrier, and returns one contribution per participant.
async fn run_stage<P, S, R, B>(
    stage: u32,
    tasks: &[Task],
    join_id: &str,
    config: NexusJoinConfig,
    transport: &NexusTransport<'_, P, S, R>,
    build_request: &B,
    merged: &[StageMerge],
) -> Vec<Contribution>
where
    P: NexusPublisher,
    S: NexusSubscriber,
    R: NexusInbox,
    B: Fn(&Task, &[StageMerge]) -> Vec<u8>,
{
    let request_topic = stage_request_topic(join_id, stage);
    let reply_topic = stage_reply_topic(join_id, stage);
    let mut contributions = Vec::with_capacity(tasks.len());
    let mut pending: HashMap<String, (usize, &Task)> = HashMap::with_capacity(tasks.len());

    // Subscribe before fan-out so that no reply can be published unseen.
    let subscribed = transport.subscriber.subscribe(&reply_topic);
    for (creation, task) in tasks.iter().enumerate() {
        if let Err(error) = &subscribed {
            contributions.push(failure(
                stage,
                creation,
                task,
                ErrorCode::Internal.as_str(),
                format!("reply topic {reply_topic} is unavailable: {error}"),
            ));
            continue;
        }
        let id = task_id_of(task);
        let message = NexusMessage {
            topic: request_topic.clone(),
            correlation_id: id.to_string(),
            payload: build_request(task, merged),
        };
        match transport.publisher.publish(message) {
            Ok(()) => {
                pending.insert(id.to_string(), (creation, task));
            }
            Err(error) => contributions.push(failure(
                stage,
                creation,
                task,
                ErrorCode::Internal.as_str(),
                format!("request publish failed: {error}"),
            )),
        }
    }

    // Fan-in. The deadline is checked on every pass, so a flood of replies cannot hold the barrier.
    let deadline = Instant::now() + config.stage_deadline;
    loop {
        if pending.is_empty() {
            break;
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            break;
        }
        match transport.inbox.receive(&reply_topic) {
            Some(message) => {
                if let Some((creation, task)) = pending.remove(&message.correlation_id) {
                    contributions.push(from_reply(stage, creation, task, &message.payload));
                }
            }
            None => sleep(remaining.min(config.poll_interval)).await,
        }
    }

    for (id, (creation, task)) in pending {
        contributions.push(failure(
            stage,
            creation,
            task,
            ErrorCode::Timeout.as_str(),
            format!(
                "partial-join timeout: task {id} missed the stage {stage} barrier deadline of {:?}",
                config.stage_deadline
            ),
        ));
    }
    contributions
}

/// Merges one stage's contributions into a log in declared key order.
fn merge_stage(stage: u32, contributions: Vec<Contribution>) -> StageMerge {
    let mut outputs = BTreeMap::new();
    let mut records = Vec::with_capacity(contributions.len());
    for Contribution { record, output } in contributions {
        if let Some(bytes) = output {
            outputs.insert(record.key.task_id.clone(), bytes);
        }
        records.push(record);
    }
    StageMerge {
        stage,
        log: CompletionLog::from_records(records),
        outputs,
    }
}

/// Builds the contribution for a reply, treating a malformed reply as a failure.
fn from_reply(stage: u32, creation: usize, task: &Task, payload: &[u8]) -> Contribution {
    match ParticipantReply::decode(payload) {
        Ok(ParticipantReply::Completed(output)) => {
            contribution(stage, creation, task, TaskOutcome::Completed, Some(output))
        }
        Ok(ParticipantReply::Failed(message)) => {
            failure(stage, creation, task, ErrorCode::Internal.as_str(), message)
        }
        Err(detail) => failure(
            stage,
            creation,
            task,
            ErrorCode::Internal.as_str(),
            format!("malformed reply: {detail}"),
        ),
    }
}

/// Builds a contribution that ended with `code` and `message`.
fn failure(
    stage: u32,
    creation: usize,
    task: &Task,
    code: &'static str,
    message: String,
) -> Contribution {
    contribution(
        stage,
        creation,
        task,
        TaskOutcome::Failed { code, message },
        None,
    )
}

/// Builds a contribution with its key and provenance, as the matrix log does. Stage participants
/// are a flat batch, so their depth is 0 and the creation index orders them.
fn contribution(
    stage: u32,
    creation: usize,
    task: &Task,
    outcome: TaskOutcome,
    output: Option<Vec<u8>>,
) -> Contribution {
    let verdict = VerdictEnvelope::from_outcome(&outcome);
    Contribution {
        record: CompletionRecord {
            key: CompletionKey {
                stage,
                depth: 0,
                creation,
                task_id: task_id_of(task).to_string(),
            },
            outcome,
            provenance: Provenance {
                producer: agent_of(task).to_string(),
                attempt: 1,
                verdict,
            },
        },
        output,
    }
}

/// Checks the plan and timing before anything is published.
fn validate(plan: &NexusPlan, config: NexusJoinConfig) -> SchedulerResult<()> {
    if plan.join_id.is_empty() {
        return Err(invalid("nexus join id must be non-empty"));
    }
    if config.stage_deadline.is_zero() || config.poll_interval.is_zero() {
        return Err(invalid(
            "stage deadline and poll interval must be non-zero; a barrier without a deadline is refused",
        ));
    }
    for (index, tasks) in plan.stages.iter().enumerate() {
        let mut ids = HashSet::with_capacity(tasks.len());
        for task in tasks {
            let id = task_id_of(task);
            if id.is_empty() {
                return Err(invalid(format!("a task in stage {index} has no id")));
            }
            if !ids.insert(id) {
                return Err(invalid(format!(
                    "task id {id} appears twice in stage {index}"
                )));
            }
            if agent_of(task).is_empty() {
                return Err(invalid(format!(
                    "task {id} has no subagent type, so it has no producer"
                )));
            }
        }
    }
    Ok(())
}

fn stage_request_topic(join_id: &str, stage: u32) -> String {
    format!("{join_id}/stage/{stage}/request")
}

/// Reply topic of `stage`. Participants publish their [`ParticipantReply`] here.
pub fn stage_reply_topic(join_id: &str, stage: u32) -> String {
    format!("{join_id}/stage/{stage}/reply")
}

fn task_id_of(task: &Task) -> &str {
    task.id.as_ref().map_or("", |id| id.value.as_str())
}

fn agent_of(task: &Task) -> &str {
    task.subagent_type
        .as_ref()
        .map_or("", |subagent| subagent.value.as_str())
}

fn invalid(message: impl Into<String>) -> SchedulerError {
    SchedulerError::Other(message.into())
}

#[cfg(test)]
mod tests {
    use super::*;
    use gsd_nexus::NexusError;
    use gsd_proto::gsd::v1::{SubagentType, TaskId};
    use parking_lot::Mutex;
    use std::collections::VecDeque;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;

    /// How a fake participant answers its request.
    #[derive(Clone)]
    enum Behaviour {
        /// Replies after `delay`.
        Reply {
            delay: Duration,
            reply: ParticipantReply,
        },
        /// Never replies: a straggler.
        Silent,
    }

    fn ok(delay_ms: u64, output: &str) -> Behaviour {
        Behaviour::Reply {
            delay: Duration::from_millis(delay_ms),
            reply: ParticipantReply::Completed(output.as_bytes().to_vec()),
        }
    }

    fn fail(delay_ms: u64, message: &str) -> Behaviour {
        Behaviour::Reply {
            delay: Duration::from_millis(delay_ms),
            reply: ParticipantReply::Failed(message.to_string()),
        }
    }

    #[derive(Default)]
    struct Shared {
        /// Task id to (reply topic, behaviour).
        behaviours: HashMap<String, (String, Behaviour)>,
        fail_subscribe: bool,
        subscribed: Mutex<HashSet<String>>,
        inbox: Mutex<HashMap<String, VecDeque<NexusMessage>>>,
        arrivals: Mutex<Vec<String>>,
        published: AtomicUsize,
    }

    /// In-memory Nexus. Each participant answers on its own reply topic after its delay.
    #[derive(Clone, Default)]
    struct FakeNexus {
        shared: Arc<Shared>,
    }

    impl FakeNexus {
        fn with(behaviours: Vec<(&str, String, Behaviour)>, fail_subscribe: bool) -> Self {
            let behaviours = behaviours
                .into_iter()
                .map(|(id, topic, behaviour)| (id.to_string(), (topic, behaviour)))
                .collect();
            Self {
                shared: Arc::new(Shared {
                    behaviours,
                    fail_subscribe,
                    ..Default::default()
                }),
            }
        }

        fn published(&self) -> usize {
            self.shared.published.load(Ordering::SeqCst)
        }

        fn arrivals(&self) -> Vec<String> {
            self.shared.arrivals.lock().clone()
        }
    }

    impl NexusPublisher for FakeNexus {
        fn publish(&self, message: NexusMessage) -> Result<(), NexusError> {
            self.shared.published.fetch_add(1, Ordering::SeqCst);
            let Some((reply_topic, behaviour)) =
                self.shared.behaviours.get(&message.correlation_id).cloned()
            else {
                // No participant is registered for this task: it stays silent.
                return Ok(());
            };
            if let Behaviour::Reply { delay, reply } = behaviour {
                let shared = Arc::clone(&self.shared);
                let correlation_id = message.correlation_id;
                tokio::spawn(async move {
                    sleep(delay).await;
                    if !shared.subscribed.lock().contains(&reply_topic) {
                        return;
                    }
                    shared.arrivals.lock().push(correlation_id.clone());
                    shared
                        .inbox
                        .lock()
                        .entry(reply_topic.clone())
                        .or_default()
                        .push_back(NexusMessage {
                            topic: reply_topic,
                            correlation_id,
                            payload: reply.encode(),
                        });
                });
            }
            Ok(())
        }
    }

    impl NexusSubscriber for FakeNexus {
        fn subscribe(&self, topic: &str) -> Result<(), NexusError> {
            if self.shared.fail_subscribe {
                return Err(NexusError::UnknownTopic(topic.to_string()));
            }
            self.shared.subscribed.lock().insert(topic.to_string());
            Ok(())
        }
    }

    impl NexusInbox for FakeNexus {
        fn receive(&self, topic: &str) -> Option<NexusMessage> {
            let mut inbox = self.shared.inbox.lock();
            inbox.get_mut(topic).and_then(VecDeque::pop_front)
        }
    }

    fn task(id: &str, agent: &str) -> Task {
        Task {
            id: Some(TaskId {
                value: id.to_string(),
            }),
            subagent_type: Some(SubagentType {
                value: agent.to_string(),
            }),
            ..Default::default()
        }
    }

    fn config(deadline_ms: u64) -> NexusJoinConfig {
        NexusJoinConfig {
            stage_deadline: Duration::from_millis(deadline_ms),
            poll_interval: Duration::from_millis(2),
        }
    }

    fn plan(stages: Vec<Vec<Task>>) -> NexusPlan {
        NexusPlan {
            join_id: "join".to_string(),
            stages,
        }
    }

    async fn run(
        fake: &FakeNexus,
        plan: &NexusPlan,
        config: NexusJoinConfig,
    ) -> SchedulerResult<NexusJoinReport> {
        run_nexus_barrier_join(
            JoinKind::Nexus,
            plan,
            config,
            NexusTransport {
                publisher: fake,
                subscriber: fake,
                inbox: fake,
            },
            |task: &Task, _: &[StageMerge]| task_input_bytes(task),
        )
        .await
    }

    fn record<'a>(stage: &'a StageMerge, id: &str) -> &'a CompletionRecord {
        stage
            .log
            .records()
            .iter()
            .find(|record| record.key.task_id == id)
            .expect("record for task present in stage")
    }

    #[tokio::test]
    async fn barrier_waits_for_every_participant_before_releasing_next_stage() {
        let join = "join";
        let stage0 = stage_reply_topic(join, 0);
        let stage1 = stage_reply_topic(join, 1);
        let fake = FakeNexus::with(
            vec![
                ("a", stage0.clone(), ok(40, "a-out")),
                ("b", stage0.clone(), ok(5, "b-out")),
                ("c", stage0.clone(), ok(20, "c-out")),
                ("d", stage1, ok(1, "d-out")),
            ],
            false,
        );
        let plan = plan(vec![
            vec![task("a", "x"), task("b", "x"), task("c", "x")],
            vec![task("d", "x")],
        ]);

        // Records visible to the request builder: zero for every stage-0 call, then three for
        // stage 1 only if the barrier waited for all three stage-0 replies.
        let seen = Arc::new(Mutex::new(Vec::<usize>::new()));
        let seen_in = Arc::clone(&seen);
        let report = run_nexus_barrier_join(
            JoinKind::Nexus,
            &plan,
            config(2_000),
            NexusTransport {
                publisher: &fake,
                subscriber: &fake,
                inbox: &fake,
            },
            move |task: &Task, merged: &[StageMerge]| {
                seen_in
                    .lock()
                    .push(merged.last().map_or(0, |stage| stage.log.records().len()));
                task_input_bytes(task)
            },
        )
        .await
        .expect("join runs");

        assert_eq!(seen.lock().last().copied(), Some(3));
        assert_eq!(report.terminal, JoinTerminal::Complete);
        assert!(report.partial_timeouts.is_empty());
        assert!(report.failed.is_empty());
        assert_eq!(report.stages.len(), 2);
        let ids: Vec<&str> = report.stages[0]
            .log
            .records()
            .iter()
            .map(|record| record.key.task_id.as_str())
            .collect();
        assert_eq!(ids, vec!["a", "b", "c"]);
        assert_eq!(report.stages[0].outputs.len(), 3);
        assert_eq!(
            report.stages[1].outputs.get("d").map(Vec::as_slice),
            Some(b"d-out".as_slice())
        );
    }

    #[tokio::test]
    async fn straggler_past_deadline_is_named_partial_timeout() {
        let join = "join";
        let stage0 = stage_reply_topic(join, 0);
        let stage1 = stage_reply_topic(join, 1);
        let fake = FakeNexus::with(
            vec![
                ("a", stage0.clone(), ok(5, "a-out")),
                ("b", stage0, Behaviour::Silent),
                ("c", stage1, ok(1, "c-out")),
            ],
            false,
        );
        let plan = plan(vec![
            vec![task("a", "x"), task("b", "x")],
            vec![task("c", "x")],
        ]);

        let started = std::time::Instant::now();
        let report = run(&fake, &plan, config(150)).await.expect("join runs");

        assert!(
            started.elapsed() < Duration::from_secs(5),
            "barrier must not hang"
        );
        assert_eq!(report.partial_timeouts, vec!["b".to_string()]);
        assert!(report.failed.is_empty());
        assert_eq!(report.terminal, JoinTerminal::PartialSuccess);
        match &record(&report.stages[0], "b").outcome {
            TaskOutcome::Failed { code, message } => {
                assert_eq!(*code, "E-TIMEOUT");
                assert!(message.contains("partial-join timeout: task b"));
            }
            other => panic!("straggler must be a timeout, got {other:?}"),
        }
        // The timeout is a value: the next stage is still released after the merge.
        assert_eq!(report.stages.len(), 2);
        assert_eq!(
            report.stages[1].outputs.get("c").map(Vec::as_slice),
            Some(b"c-out".as_slice())
        );
    }

    #[tokio::test]
    async fn failed_participant_yields_a_value_and_no_hang() {
        let join = "join";
        let stage0 = stage_reply_topic(join, 0);
        let stage1 = stage_reply_topic(join, 1);
        let fake = FakeNexus::with(
            vec![
                ("ok", stage0.clone(), ok(5, "fine")),
                ("bad", stage0, fail(1, "boom")),
                ("after", stage1, ok(1, "after-out")),
            ],
            false,
        );
        let plan = plan(vec![
            vec![task("ok", "x"), task("bad", "x")],
            vec![task("after", "x")],
        ]);

        // A five-second deadline proves the failure is not waited out.
        let started = std::time::Instant::now();
        let report = run(&fake, &plan, config(5_000)).await.expect("join runs");

        assert!(
            started.elapsed() < Duration::from_secs(2),
            "failure must not hang"
        );
        assert_eq!(report.failed, vec!["bad".to_string()]);
        assert!(report.partial_timeouts.is_empty());
        assert_eq!(report.terminal, JoinTerminal::PartialSuccess);

        let bad = record(&report.stages[0], "bad");
        assert_eq!(
            bad.outcome,
            TaskOutcome::Failed {
                code: "E-INTERNAL",
                message: "boom".to_string(),
            }
        );
        assert_eq!(bad.provenance.verdict.verdict, "failed");
        assert_eq!(bad.provenance.producer, "x");

        // The sibling is DONE, with its output merged.
        let ok_record = record(&report.stages[0], "ok");
        assert_eq!(ok_record.outcome, TaskOutcome::Completed);
        assert_eq!(
            report.stages[0].outputs.get("ok").map(Vec::as_slice),
            Some(b"fine".as_slice())
        );
        assert_eq!(report.stages.len(), 2);
    }

    #[tokio::test]
    async fn merge_order_is_declared_key_order_regardless_of_arrival() {
        async fn ordered_run(delays_ms: [u64; 5]) -> (NexusJoinReport, Vec<String>) {
            let ids = ["t0", "t1", "t2", "t3", "t4"];
            let topic = stage_reply_topic("join", 0);
            let behaviours: Vec<(&str, String, Behaviour)> = ids
                .iter()
                .zip(delays_ms)
                .map(|(id, delay)| (*id, topic.clone(), ok(delay, &format!("out-{id}"))))
                .collect();
            let fake = FakeNexus::with(behaviours, false);
            let plan = plan(vec![ids.iter().map(|id| task(id, "x")).collect()]);
            let report = run(&fake, &plan, config(5_000)).await.expect("join runs");
            (report, fake.arrivals())
        }

        // Spaced 20 ms apart so the arrival order follows the delays.
        let (forward, forward_arrivals) = ordered_run([0, 20, 40, 60, 80]).await;
        let (reversed, reversed_arrivals) = ordered_run([80, 60, 40, 20, 0]).await;
        let (shuffled, shuffled_arrivals) = ordered_run([40, 80, 0, 60, 20]).await;

        // The arrivals really differ, so the test exercises reordering.
        assert_ne!(forward_arrivals, reversed_arrivals);
        assert_ne!(forward_arrivals, shuffled_arrivals);

        let declared = vec!["t0", "t1", "t2", "t3", "t4"];
        for report in [&forward, &reversed, &shuffled] {
            let order: Vec<&str> = report.stages[0]
                .log
                .records()
                .iter()
                .map(|record| record.key.task_id.as_str())
                .collect();
            assert_eq!(order, declared);
        }
        assert_eq!(forward, reversed);
        assert_eq!(forward, shuffled);
    }

    #[tokio::test]
    async fn selection_is_fixed_before_dispatch_and_nexus_path_refuses_matrix() {
        let workflow = WorkflowDef {
            policy: SchedulingPolicy::CriticalPathThroughputHybrid as i32,
            ..Default::default()
        };
        let kind = select_join_kind(&workflow).expect("implemented policy selects a join");
        assert_eq!(kind, JoinKind::Matrix);

        let unknown = WorkflowDef {
            policy: 999,
            ..Default::default()
        };
        let error = select_join_kind(&unknown).expect_err("unknown policy value is rejected");
        assert!(error.to_string().contains("E-INPUT"));

        let unimplemented = WorkflowDef {
            policy: SchedulingPolicy::MaxThroughput as i32,
            ..Default::default()
        };
        assert!(select_join_kind(&unimplemented).is_err());

        // A run selected as matrix never reaches the transport.
        let fake = FakeNexus::with(vec![("a", stage_reply_topic("join", 0), ok(1, "a"))], false);
        let plan = plan(vec![vec![task("a", "x")]]);
        let result = run_nexus_barrier_join(
            kind,
            &plan,
            config(1_000),
            NexusTransport {
                publisher: &fake,
                subscriber: &fake,
                inbox: &fake,
            },
            |task: &Task, _: &[StageMerge]| task_input_bytes(task),
        )
        .await;
        assert!(result.is_err());
        assert_eq!(fake.published(), 0);
    }

    #[tokio::test]
    async fn zero_deadline_is_refused_before_any_publish() {
        let fake = FakeNexus::with(vec![("a", stage_reply_topic("join", 0), ok(1, "a"))], false);
        let plan = plan(vec![vec![task("a", "x")]]);
        let refused = NexusJoinConfig {
            stage_deadline: Duration::ZERO,
            poll_interval: Duration::from_millis(2),
        };
        assert!(run(&fake, &plan, refused).await.is_err());
        assert_eq!(fake.published(), 0);
    }

    #[tokio::test]
    async fn unavailable_reply_topic_fails_every_participant_without_hang() {
        let fake = FakeNexus::with(vec![("a", stage_reply_topic("join", 0), ok(1, "a"))], true);
        let plan = plan(vec![vec![task("a", "x")]]);
        let started = std::time::Instant::now();
        let report = run(&fake, &plan, config(5_000)).await.expect("join runs");
        assert!(started.elapsed() < Duration::from_secs(2));
        assert_eq!(report.failed, vec!["a".to_string()]);
        assert_eq!(fake.published(), 0);
    }

    #[test]
    fn participant_reply_round_trips_and_rejects_malformed_payloads() {
        for reply in [
            ParticipantReply::Completed(b"out".to_vec()),
            ParticipantReply::Completed(Vec::new()),
            ParticipantReply::Failed("boom".to_string()),
        ] {
            assert_eq!(ParticipantReply::decode(&reply.encode()), Ok(reply));
        }
        assert!(ParticipantReply::decode(&[]).is_err());
        assert!(ParticipantReply::decode(&[9, 1]).is_err());
        assert!(ParticipantReply::decode(&[TAG_FAILED, 0xff]).is_err());
    }
}
