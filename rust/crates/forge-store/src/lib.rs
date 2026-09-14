//! A single SQLite transaction owns a command, state, event and outbox record.
//! This store is opt-in and refuses existing databases owned by the Python app.

use forge_core::{
    validate_identifier, Approval, Command, CommandResult, DomainError, ExecutionPolicy,
    ProviderSnapshot, Receipt, RouteBinding, Run, RunRuntimeEvent, Task,
};
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    path::Path,
    sync::Arc,
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use thiserror::Error;
use uuid::Uuid;

const APPLICATION_ID: i64 = 0x46475253;
const SCHEMA_VERSION: i64 = 3;

mod approval;
mod provider;
mod run;

/// Trusted server time, not a field accepted from a command caller.
pub trait Clock: Send + Sync {
    fn now_ms(&self) -> i64;
}

pub struct SystemClock;

impl Clock for SystemClock {
    fn now_ms(&self) -> i64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis()
            .try_into()
            .unwrap_or(i64::MAX)
    }
}

#[derive(Debug, Error)]
pub enum StoreError {
    #[error(transparent)]
    Domain(#[from] DomainError),
    #[error(transparent)]
    Sql(#[from] rusqlite::Error),
    #[error(transparent)]
    Json(#[from] serde_json::Error),
    #[error("{kind} not found: {id}")]
    NotFound { kind: &'static str, id: String },
    #[error("{kind} already exists: {id}")]
    AlreadyExists { kind: &'static str, id: String },
    #[error("command id was already used with a different payload: {0}")]
    CommandConflict(String),
    #[error("database belongs to another application or has an unsupported schema")]
    IncompatibleDatabase,
    #[error("state changed concurrently")]
    ConcurrentMutation,
    #[error("runtime event sequence {sequence} was already recorded for run {run_id}")]
    RuntimeEventSequenceConflict { run_id: String, sequence: u64 },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Event {
    pub id: i64,
    pub command_id: String,
    pub run_id: String,
    pub kind: String,
    pub payload: Value,
    pub created_at_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OutboxMessage {
    pub event: Event,
}

pub struct Store {
    connection: Connection,
    clock: Arc<dyn Clock>,
}

impl Store {
    pub fn open(path: impl AsRef<Path>) -> Result<Self, StoreError> {
        Self::open_with_clock(path, Arc::new(SystemClock))
    }

    pub fn open_in_memory() -> Result<Self, StoreError> {
        Self::initialize(Connection::open_in_memory()?, Arc::new(SystemClock))
    }

    /// Dependency injection for trusted hosts/tests. Never expose clock control
    /// through an HTTP or CLI command.
    pub fn open_with_clock(
        path: impl AsRef<Path>,
        clock: Arc<dyn Clock>,
    ) -> Result<Self, StoreError> {
        Self::initialize(Connection::open(path)?, clock)
    }

    fn initialize(mut connection: Connection, clock: Arc<dyn Clock>) -> Result<Self, StoreError> {
        connection.busy_timeout(Duration::from_secs(5))?;
        connection.pragma_update(None, "foreign_keys", "ON")?;
        {
            let transaction =
                connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
            let application_id: i64 =
                transaction.pragma_query_value(None, "application_id", |r| r.get(0))?;
            let version: i64 =
                transaction.pragma_query_value(None, "user_version", |r| r.get(0))?;
            let table_count: i64 = transaction.query_row("SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'", [], |r| r.get(0))?;
            if !((application_id == 0 && version == 0 && table_count == 0)
                || (application_id == APPLICATION_ID && (1..=SCHEMA_VERSION).contains(&version)))
            {
                return Err(StoreError::IncompatibleDatabase);
            }
            if application_id == 0 {
                transaction.execute_batch(include_str!("schema.sql"))?;
                transaction.pragma_update(None, "application_id", APPLICATION_ID)?;
                transaction.pragma_update(None, "user_version", 1)?;
            }
            if version < 2 {
                transaction.execute_batch(include_str!("migration_002.sql"))?;
                transaction.pragma_update(None, "user_version", 2)?;
            }
            if version < 3 {
                transaction.execute_batch(include_str!("migration_003.sql"))?;
                transaction.pragma_update(None, "user_version", 3)?;
            }
            transaction.commit()?;
        }
        connection.pragma_update(None, "journal_mode", "WAL")?;
        connection.pragma_update(None, "synchronous", "FULL")?;
        Ok(Self { connection, clock })
    }

    pub fn get_run(&self, run_id: &str) -> Result<Option<Run>, StoreError> {
        load_run(&self.connection, run_id)
    }

    pub fn get_task(&self, task_id: &str) -> Result<Option<Task>, StoreError> {
        load_task(&self.connection, task_id)
    }

    /// The route is retained at run creation, never a live catalog lookup.
    pub fn get_route_binding(&self, run_id: &str) -> Result<Option<RouteBinding>, StoreError> {
        provider::load_route(&self.connection, run_id)
    }

    pub fn get_execution_policy(
        &self,
        run_id: &str,
    ) -> Result<Option<ExecutionPolicy>, StoreError> {
        provider::load_policy(&self.connection, run_id)
    }

    pub fn get_provider_snapshot(
        &self,
        provider_id: &str,
        provider_revision_id: &str,
    ) -> Result<Option<ProviderSnapshot>, StoreError> {
        provider::load_snapshot(&self.connection, provider_id, provider_revision_id)
    }

    pub fn get_run_provider_snapshot(
        &self,
        run_id: &str,
    ) -> Result<Option<ProviderSnapshot>, StoreError> {
        provider::load_run_snapshot(&self.connection, run_id)
    }

    pub fn get_approval(&self, approval_id: &str) -> Result<Option<Approval>, StoreError> {
        approval::load(&self.connection, approval_id)
    }

    /// Pending decisions precede history, so old approvals cannot hide live ones.
    pub fn list_approvals(&self, run_id: &str, limit: u32) -> Result<Vec<Approval>, StoreError> {
        approval::list(&self.connection, run_id, limit)
    }

    pub fn get_receipt(&self, command_id: &str) -> Result<Option<Receipt>, StoreError> {
        let raw: Option<String> = self
            .connection
            .query_row(
                "SELECT receipt_json FROM commands WHERE id=?1",
                [command_id],
                |r| r.get(0),
            )
            .optional()?;
        raw.map(|value| serde_json::from_str(&value).map_err(StoreError::from))
            .transpose()
    }

    /// Idempotency is scoped to successful commands. A replay returns the exact
    /// durable receipt (including the original lease), without executing again.
    pub fn execute(&mut self, command_id: &str, command: Command) -> Result<Receipt, StoreError> {
        validate_identifier(command_id, "command_id")?;
        let request_json = serde_json::to_string(&command)?;
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let previous: Option<(String, String)> = transaction
            .query_row(
                "SELECT request_json, receipt_json FROM commands WHERE id=?1",
                [command_id],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()?;
        if let Some((previous_request, receipt)) = previous {
            if serde_json::from_str::<Value>(&previous_request)?
                != serde_json::from_str::<Value>(&request_json)?
            {
                return Err(StoreError::CommandConflict(command_id.to_owned()));
            }
            return Ok(serde_json::from_str(&receipt)?);
        }
        // Read after acquiring the writer lock; lock waits cannot prolong a lease.
        let now_ms = self.clock.now_ms();
        let (result, run_id, kind, payload) = match command {
            Command::CreateRun { run_id } => {
                validate_identifier(&run_id, "run_id")?;
                if load_run(&transaction, &run_id)?.is_some() {
                    return Err(StoreError::AlreadyExists {
                        kind: "run",
                        id: run_id,
                    });
                }
                let run = Run::new(run_id.clone())?;
                transaction.execute(
                    "INSERT INTO runs(id, snapshot_json) VALUES (?1, ?2)",
                    params![run_id, serde_json::to_string(&run)?],
                )?;
                let payload = json!({"run_id": run_id, "status": run.status});
                (CommandResult::Run(run), run_id, "run_created", payload)
            }
            Command::CreateBoundRun { run_id, config } => {
                run::create(&transaction, run_id, *config)?
            }
            Command::CreateProvisionedRun {
                run_id,
                config,
                provider_snapshot,
            } => {
                provider_snapshot.validate_route(&config.route_binding)?;
                provider::retain_snapshot(&transaction, &provider_snapshot)?;
                let change = run::create(&transaction, run_id.clone(), *config)?;
                provider::bind_snapshot(&transaction, &run_id, &provider_snapshot)?;
                change
            }
            command @ (Command::RecordApproval { .. }
            | Command::DecideApproval { .. }
            | Command::BeginApprovalDispatch { .. }
            | Command::FinishApprovalDispatch { .. }
            | Command::ExpireApprovals { .. }) => approval::execute(&transaction, command, now_ms)?,
            Command::TransitionRun {
                run_id,
                expected_version,
                transition,
            } => run::transition(&transaction, run_id, expected_version, transition, now_ms)?,
            Command::RecordRuntimeEvent {
                run_id,
                connection,
                sequence,
                thread_id,
                turn_id,
                item_id,
                kind,
                terminal_status,
            } => run::record_runtime_event(
                &transaction,
                run_id,
                RunRuntimeEvent {
                    connection,
                    sequence,
                    thread_id,
                    turn_id,
                    item_id,
                    kind,
                    terminal_status,
                },
            )?,
            Command::CreateTask { run_id, task_id } => {
                if load_run(&transaction, &run_id)?.is_none() {
                    return Err(StoreError::NotFound {
                        kind: "run",
                        id: run_id,
                    });
                }
                if load_task(&transaction, &task_id)?.is_some() {
                    return Err(StoreError::AlreadyExists {
                        kind: "task",
                        id: task_id,
                    });
                }
                run::require_task_creation(&transaction, &run_id)?;
                let task = Task::new(task_id, run_id.clone())?;
                transaction.execute(
                    "INSERT INTO tasks(id, run_id, version, snapshot_json) VALUES (?1, ?2, ?3, ?4)",
                    params![task.id, run_id, task.version, serde_json::to_string(&task)?],
                )?;
                let payload = task_event(&task);
                (CommandResult::Task(task), run_id, "task_created", payload)
            }
            Command::AssignTask {
                task_id,
                coordinator_id,
                worker_id,
                expected_version,
            } => {
                let mut task =
                    load_task(&transaction, &task_id)?.ok_or_else(|| StoreError::NotFound {
                        kind: "task",
                        id: task_id.clone(),
                    })?;
                let old_version = task.version;
                run::require_task_command(
                    &transaction,
                    &task,
                    &Command::AssignTask {
                        task_id: task_id.clone(),
                        coordinator_id: coordinator_id.clone(),
                        worker_id: worker_id.clone(),
                        expected_version,
                    },
                )?;
                task.assign(coordinator_id, worker_id, expected_version)?;
                let updated = transaction.execute(
                    "UPDATE tasks SET version=?1, snapshot_json=?2 WHERE id=?3 AND version=?4",
                    params![
                        task.version,
                        serde_json::to_string(&task)?,
                        task.id,
                        old_version
                    ],
                )?;
                if updated != 1 {
                    return Err(StoreError::ConcurrentMutation);
                }
                let run_id = task.run_id.clone();
                let payload = task_event(&task);
                (CommandResult::Task(task), run_id, "task_assigned", payload)
            }
            Command::CreateAssignedTask {
                run_id,
                task_id,
                coordinator_id,
                worker_id,
            } => {
                run::require_task_creation(&transaction, &run_id)?;
                if load_task(&transaction, &task_id)?.is_some() {
                    return Err(StoreError::AlreadyExists {
                        kind: "task",
                        id: task_id,
                    });
                }
                let mut task = Task::new(task_id.clone(), run_id.clone())?;
                run::require_task_command(
                    &transaction,
                    &task,
                    &Command::AssignTask {
                        task_id,
                        coordinator_id: coordinator_id.clone(),
                        worker_id: worker_id.clone(),
                        expected_version: 0,
                    },
                )?;
                task.assign(coordinator_id, worker_id, 0)?;
                transaction.execute(
                    "INSERT INTO tasks(id, run_id, version, snapshot_json) VALUES (?1, ?2, ?3, ?4)",
                    params![task.id, run_id, task.version, serde_json::to_string(&task)?],
                )?;
                let payload = task_event(&task);
                (CommandResult::Task(task), run_id, "task_assigned", payload)
            }
            command => {
                let task_id = match &command {
                    Command::ClaimTask { task_id, .. }
                    | Command::AssignTask { task_id, .. }
                    | Command::RenewClaim { task_id, .. }
                    | Command::ReleaseTask { task_id, .. }
                    | Command::SubmitTask { task_id, .. }
                    | Command::MarkOutcomeUnknown { task_id, .. }
                    | Command::ReconcileTask { task_id, .. }
                    | Command::ReopenTask { task_id, .. } => task_id,
                    _ => unreachable!(),
                };
                let mut task =
                    load_task(&transaction, task_id)?.ok_or_else(|| StoreError::NotFound {
                        kind: "task",
                        id: task_id.clone(),
                    })?;
                let old_version = task.version;
                run::require_task_command(&transaction, &task, &command)?;
                let mut reconciliation_evidence = None;
                let kind = match command {
                    Command::ClaimTask {
                        worker_id,
                        expected_version,
                        lease_duration_ms,
                        ..
                    } => {
                        task.claim(
                            worker_id,
                            Uuid::new_v4().to_string(),
                            expected_version,
                            now_ms,
                            lease_duration_ms,
                        )?;
                        let claim = task.claim.as_ref().expect("successful claim");
                        transaction.execute("INSERT INTO attempts(task_id, generation, token, worker_id, expires_at_ms, status) VALUES (?1, ?2, ?3, ?4, ?5, 'running')", params![task.id, claim.generation, claim.token, claim.worker_id, claim.expires_at_ms])?;
                        "task_claimed"
                    }
                    Command::RenewClaim {
                        claim_token,
                        attempt_generation,
                        lease_duration_ms,
                        ..
                    } => {
                        task.renew_claim(
                            &claim_token,
                            attempt_generation,
                            now_ms,
                            lease_duration_ms,
                        )?;
                        let claim = task.claim.as_ref().expect("successful renewal");
                        let updated = transaction.execute(
                            "UPDATE attempts SET expires_at_ms=?1 WHERE task_id=?2 AND generation=?3 AND token=?4 AND status='running'",
                            params![claim.expires_at_ms, task.id, claim.generation, claim.token],
                        )?;
                        if updated != 1 {
                            return Err(StoreError::ConcurrentMutation);
                        }
                        "claim_renewed"
                    }
                    Command::ReleaseTask {
                        claim_token,
                        attempt_generation,
                        ..
                    } => {
                        task.release(&claim_token, attempt_generation, now_ms)?;
                        update_attempt(&transaction, &task, "released")?;
                        "task_released"
                    }
                    Command::SubmitTask {
                        claim_token,
                        attempt_generation,
                        result,
                        ..
                    } => {
                        task.submit(&claim_token, attempt_generation, now_ms, result)?;
                        update_attempt(&transaction, &task, "submitted")?;
                        "task_submitted"
                    }
                    Command::MarkOutcomeUnknown {
                        expected_version, ..
                    } => {
                        task.mark_outcome_unknown(expected_version, now_ms)?;
                        update_attempt(&transaction, &task, "outcome_unknown")?;
                        "task_outcome_unknown"
                    }
                    Command::ReconcileTask {
                        expected_version,
                        evidence_ref,
                        ..
                    } => {
                        task.reconcile(expected_version, &evidence_ref)?;
                        reconciliation_evidence = Some(evidence_ref);
                        "task_reconciled"
                    }
                    Command::ReopenTask {
                        expected_version,
                        evidence_ref,
                        ..
                    } => {
                        task.reopen(expected_version, &evidence_ref)?;
                        reconciliation_evidence = Some(evidence_ref);
                        "task_reopened"
                    }
                    _ => unreachable!(),
                };
                let updated = transaction.execute(
                    "UPDATE tasks SET version=?1, snapshot_json=?2 WHERE id=?3 AND version=?4",
                    params![
                        task.version,
                        serde_json::to_string(&task)?,
                        task.id,
                        old_version
                    ],
                )?;
                if updated != 1 {
                    return Err(StoreError::ConcurrentMutation);
                }
                let run_id = task.run_id.clone();
                let mut payload = task_event(&task);
                if let Some(evidence_ref) = reconciliation_evidence {
                    payload["evidence_ref"] = Value::String(evidence_ref);
                }
                (CommandResult::Task(task), run_id, kind, payload)
            }
        };
        transaction.execute("INSERT INTO events(command_id, run_id, kind, payload_json, created_at_ms) VALUES (?1, ?2, ?3, ?4, ?5)", params![command_id, run_id, kind, serde_json::to_string(&payload)?, now_ms])?;
        let event_id = transaction.last_insert_rowid();
        transaction.execute("INSERT INTO outbox(event_id) VALUES (?1)", [event_id])?;
        let receipt = Receipt {
            command_id: command_id.to_owned(),
            event_id,
            result,
        };
        transaction.execute(
            "INSERT INTO commands(id, request_json, receipt_json) VALUES (?1, ?2, ?3)",
            params![command_id, request_json, serde_json::to_string(&receipt)?],
        )?;
        transaction.commit()?;
        Ok(receipt)
    }

    /// Cursor is exclusive and durable across connection/process restarts.
    pub fn events_after(&self, after: i64, limit: u32) -> Result<Vec<Event>, StoreError> {
        let mut statement = self.connection.prepare("SELECT id, command_id, run_id, kind, payload_json, created_at_ms FROM events WHERE id>?1 ORDER BY id LIMIT ?2")?;
        let rows = statement.query_map(params![after, limit.min(1000)], read_event_row)?;
        rows.map(|row| decode_event(row?)).collect()
    }

    /// Reads persisted runtime metadata for one run using the global event cursor.
    pub fn runtime_events_after(
        &self,
        run_id: &str,
        after: i64,
        limit: u32,
    ) -> Result<Vec<Event>, StoreError> {
        let mut statement = self.connection.prepare("SELECT id, command_id, run_id, kind, payload_json, created_at_ms FROM events WHERE run_id=?1 AND kind='runtime_event' AND id>?2 ORDER BY id LIMIT ?3")?;
        let rows = statement.query_map(params![run_id, after, limit.min(1000)], read_event_row)?;
        rows.map(|row| decode_event(row?)).collect()
    }

    pub fn pending_outbox(&self, limit: u32) -> Result<Vec<OutboxMessage>, StoreError> {
        let mut statement = self.connection.prepare("SELECT e.id, e.command_id, e.run_id, e.kind, e.payload_json, e.created_at_ms FROM outbox o JOIN events e ON e.id=o.event_id WHERE o.delivered_at_ms IS NULL ORDER BY e.id LIMIT ?1")?;
        let rows = statement.query_map([limit.min(1000)], read_event_row)?;
        rows.map(|row| {
            Ok(OutboxMessage {
                event: decode_event(row?)?,
            })
        })
        .collect()
    }

    /// Acknowledge only after the destination durably accepts this event ID.
    /// Delivery is at least once; consumers must deduplicate on that ID.
    pub fn acknowledge_outbox(&mut self, event_id: i64) -> Result<bool, StoreError> {
        let updated = self.connection.execute(
            "UPDATE outbox SET delivered_at_ms=?1 WHERE event_id=?2 AND delivered_at_ms IS NULL",
            params![self.clock.now_ms(), event_id],
        )?;
        Ok(updated == 1)
    }
}

fn load_run(connection: &Connection, id: &str) -> Result<Option<Run>, StoreError> {
    let raw: Option<String> = connection
        .query_row("SELECT snapshot_json FROM runs WHERE id=?1", [id], |r| {
            r.get(0)
        })
        .optional()?;
    raw.map(|value| serde_json::from_str(&value).map_err(StoreError::from))
        .transpose()
}

fn load_task(connection: &Connection, id: &str) -> Result<Option<Task>, StoreError> {
    let raw: Option<String> = connection
        .query_row("SELECT snapshot_json FROM tasks WHERE id=?1", [id], |r| {
            r.get(0)
        })
        .optional()?;
    raw.map(|value| serde_json::from_str(&value).map_err(StoreError::from))
        .transpose()
}

fn update_attempt(connection: &Connection, task: &Task, status: &str) -> Result<(), StoreError> {
    let updated = connection.execute(
        "UPDATE attempts SET status=?1 WHERE task_id=?2 AND generation=?3 AND status='running'",
        params![status, task.id, task.attempt_generation],
    )?;
    if updated != 1 {
        return Err(StoreError::ConcurrentMutation);
    }
    Ok(())
}

fn task_event(task: &Task) -> Value {
    // Claim tokens and submitted content stay in protected command/state records.
    json!({"task_id": task.id, "run_id": task.run_id, "status": task.status, "version": task.version, "attempt_generation": task.attempt_generation, "assigned_worker_id": task.assigned_worker_id, "assigned_by": task.assigned_by})
}

type EventRow = (i64, String, String, String, String, i64);
fn read_event_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<EventRow> {
    Ok((
        row.get(0)?,
        row.get(1)?,
        row.get(2)?,
        row.get(3)?,
        row.get(4)?,
        row.get(5)?,
    ))
}
fn decode_event(row: EventRow) -> Result<Event, StoreError> {
    Ok(Event {
        id: row.0,
        command_id: row.1,
        run_id: row.2,
        kind: row.3,
        payload: serde_json::from_str(&row.4)?,
        created_at_ms: row.5,
    })
}

#[cfg(test)]
mod file_tests;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn replay_preserves_receipt_and_emits_one_event() {
        let mut store = Store::open_in_memory().unwrap();
        let command = Command::CreateRun {
            run_id: "run".into(),
        };
        let first = store.execute("create", command.clone()).unwrap();
        assert_eq!(store.execute("create", command).unwrap(), first);
        assert_eq!(store.events_after(0, 100).unwrap().len(), 1);
        assert_eq!(store.pending_outbox(100).unwrap().len(), 1);
        assert!(matches!(
            store.execute(
                "create",
                Command::CreateRun {
                    run_id: "different".into()
                }
            ),
            Err(StoreError::CommandConflict(_))
        ));
        assert!(store.get_run("different").unwrap().is_none());
        assert!(store.acknowledge_outbox(first.event_id).unwrap());
        assert!(!store.acknowledge_outbox(first.event_id).unwrap());
        assert!(store.pending_outbox(100).unwrap().is_empty());
    }

    #[test]
    fn outbox_failure_rolls_back_all_state_and_receipt() {
        let mut store = Store::open_in_memory().unwrap();
        store.connection.execute_batch("CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT, 'injected outbox failure'); END;").unwrap();
        let command = Command::CreateRun {
            run_id: "run".into(),
        };
        assert!(store.execute("create", command.clone()).is_err());
        assert!(store.get_run("run").unwrap().is_none());
        assert!(store.get_receipt("create").unwrap().is_none());
        assert!(store.events_after(0, 100).unwrap().is_empty());
        store
            .connection
            .execute_batch("DROP TRIGGER fail_outbox")
            .unwrap();
        store.execute("create", command).unwrap();
        assert_eq!(store.pending_outbox(100).unwrap().len(), 1);
    }

    #[test]
    fn events_do_not_disclose_claim_token_or_result_content() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .execute(
                "r",
                Command::CreateRun {
                    run_id: "run".into(),
                },
            )
            .unwrap();
        store
            .execute(
                "t",
                Command::CreateTask {
                    run_id: "run".into(),
                    task_id: "task".into(),
                },
            )
            .unwrap();
        let receipt = store
            .execute(
                "claim",
                Command::ClaimTask {
                    task_id: "task".into(),
                    worker_id: "worker".into(),
                    expected_version: 0,
                    lease_duration_ms: 100_000,
                },
            )
            .unwrap();
        let CommandResult::Task(task) = receipt.result else {
            panic!("expected task");
        };
        let claim = task.claim.unwrap();
        store
            .execute(
                "submit",
                Command::SubmitTask {
                    task_id: "task".into(),
                    claim_token: claim.token.clone(),
                    attempt_generation: claim.generation,
                    result: json!({"secret": "result-private-content"}),
                },
            )
            .unwrap();
        let events = serde_json::to_string(&store.events_after(0, 100).unwrap()).unwrap();
        assert!(!events.contains(&claim.token));
        assert!(!events.contains("result-private-content"));
    }
}
