use crate::{load_run, Command, CommandResult, Connection, StoreError, Task};
use forge_core::{Run, RunConfig, RunError, RunRuntimeEvent, RunStatus, RunTransition};
use rusqlite::params;
use serde_json::{json, Value};

type RunChange = (CommandResult, String, &'static str, Value);

pub(super) fn create(
    connection: &Connection,
    run_id: String,
    config: RunConfig,
) -> Result<RunChange, StoreError> {
    if load_run(connection, &run_id)?.is_some() {
        return Err(StoreError::AlreadyExists {
            kind: "run",
            id: run_id,
        });
    }
    let run = Run::with_config(run_id.clone(), config)?;
    connection.execute(
        "INSERT INTO runs(id, version, snapshot_json) VALUES (?1, ?2, ?3)",
        params![run_id, run.version, serde_json::to_string(&run)?],
    )?;
    crate::provider::retain_run_config(connection, &run)?;
    let payload = event(&run);
    Ok((CommandResult::Run(run), run_id, "run_bound", payload))
}

pub(super) fn transition(
    connection: &Connection,
    run_id: String,
    expected_version: i64,
    transition: RunTransition,
    now_ms: i64,
) -> Result<RunChange, StoreError> {
    let mut run = require_run(connection, &run_id)?;
    let old_version = run.version;
    if matches!(transition, RunTransition::BindTurn { .. }) && run.status == RunStatus::Running {
        let execution = run
            .execution
            .as_ref()
            .ok_or(forge_core::RunError::StaleExecution)
            .map_err(forge_core::DomainError::from)?;
        let complete: bool = connection.query_row(
            "SELECT EXISTS(SELECT 1 FROM events WHERE run_id=?1 AND kind='runtime_event' AND json_extract(payload_json,'$.connection.connection_id')=?2 AND json_extract(payload_json,'$.turn_id')=?3 AND json_extract(payload_json,'$.terminal_status')='completed')",
            rusqlite::params![run_id, execution.connection.connection_id, execution.turn_id], |row| row.get(0))?;
        if !complete {
            return Err(forge_core::DomainError::from(forge_core::RunError::StaleExecution).into());
        }
        crate::approval::require_connection_resolved(connection, &run_id, &execution.connection)?;
    }
    let (kind, evidence_ref) = match &transition {
        RunTransition::Begin { .. } => ("run_starting", None),
        RunTransition::BindThread { .. } => ("run_thread_bound", None),
        RunTransition::BindTurn { .. } => ("run_running", None),
        RunTransition::BeginMember { .. } => ("member_starting", None),
        RunTransition::BindMemberThread { .. } => ("member_thread_bound", None),
        RunTransition::BindMemberTurn { .. } => ("member_running", None),
        RunTransition::EndMember { evidence_ref, .. } => {
            ("member_stopped", Some(evidence_ref.clone()))
        }
        RunTransition::MarkMemberOutcomeUnknown { .. } => ("member_outcome_unknown", None),
        RunTransition::ReconcileMember { evidence_ref, .. } => {
            ("member_reconciled", Some(evidence_ref.clone()))
        }
        RunTransition::Pause { evidence_ref, .. } => ("run_paused", Some(evidence_ref.clone())),
        RunTransition::MarkOutcomeUnknown { .. } => ("run_outcome_unknown", None),
        RunTransition::Reconcile { evidence_ref } => ("run_reconciled", Some(evidence_ref.clone())),
        RunTransition::Submit { .. } => ("run_submitted", None),
        RunTransition::Integrate { evidence_ref } => ("run_integrated", Some(evidence_ref.clone())),
        RunTransition::Accept { evidence_ref } => ("run_accepted", Some(evidence_ref.clone())),
        RunTransition::Cancel { evidence_ref } => ("run_cancelled", Some(evidence_ref.clone())),
        RunTransition::Reopen { evidence_ref } => ("run_reopened", Some(evidence_ref.clone())),
    };
    // The task lease and the run session are different facts. Neither silently
    // clears the other; the coordinator must resolve outstanding task attempts.
    let unresolved_query = match &transition {
        RunTransition::Begin { .. } => Some("SELECT EXISTS(SELECT 1 FROM tasks WHERE run_id=?1 AND json_extract(snapshot_json, '$.status') IN ('running', 'outcome_unknown'))"),
        RunTransition::Submit { .. } => Some("SELECT EXISTS(SELECT 1 FROM tasks WHERE run_id=?1 AND json_extract(snapshot_json, '$.status') != 'submitted')"),
        _ => None,
    };
    let submitting = matches!(&transition, RunTransition::Submit { .. });
    let beginning_member = match &transition {
        RunTransition::BeginMember { worker_id, .. } => Some(worker_id.clone()),
        _ => None,
    };
    let member_id = match &transition {
        RunTransition::BeginMember { worker_id, .. }
        | RunTransition::ReconcileMember { worker_id, .. } => Some(worker_id.clone()),
        RunTransition::BindMemberThread { connection, .. }
        | RunTransition::BindMemberTurn { connection, .. }
        | RunTransition::EndMember { connection, .. }
        | RunTransition::MarkMemberOutcomeUnknown { connection } => {
            Some(connection.worker_id.clone())
        }
        _ => None,
    };
    let member_invalidation = match &transition {
        RunTransition::BeginMember { worker_id, .. } => run
            .sessions
            .get(worker_id)
            .map(|session| session.execution.connection.clone()),
        RunTransition::EndMember { connection, .. }
        | RunTransition::MarkMemberOutcomeUnknown { connection } => Some(connection.clone()),
        _ => None,
    };
    let invalidates_approvals = matches!(
        &transition,
        RunTransition::Pause { .. }
            | RunTransition::MarkOutcomeUnknown { .. }
            | RunTransition::Reconcile { .. }
            | RunTransition::Begin { .. }
            | RunTransition::Cancel { .. }
            | RunTransition::Reopen { .. }
    );
    run.transition(expected_version, transition)?;
    if submitting {
        crate::approval::require_resolved(connection, &run_id)?;
    }
    if let Some(worker_id) = beginning_member {
        let unresolved: bool = connection.query_row(
            "SELECT EXISTS(SELECT 1 FROM tasks WHERE run_id=?1 AND json_extract(snapshot_json, '$.status') IN ('running','outcome_unknown') AND (json_extract(snapshot_json, '$.assigned_worker_id')=?2 OR json_extract(snapshot_json, '$.claim.worker_id')=?2))",
            params![run_id, worker_id], |row| row.get(0),
        )?;
        if unresolved {
            return Err(forge_core::DomainError::from(RunError::UnresolvedTasks).into());
        }
    }
    if let Some(query) = unresolved_query {
        let unresolved: bool = connection.query_row(query, [&run_id], |row| row.get(0))?;
        if unresolved {
            return Err(forge_core::DomainError::from(RunError::UnresolvedTasks).into());
        }
    }
    let updated = connection.execute(
        "UPDATE runs SET version=?1, snapshot_json=?2 WHERE id=?3 AND version=?4",
        params![
            run.version,
            serde_json::to_string(&run)?,
            run_id,
            old_version
        ],
    )?;
    if updated != 1 {
        return Err(StoreError::ConcurrentMutation);
    }
    let invalidated = if invalidates_approvals {
        crate::approval::invalidate_run(connection, &run_id, now_ms)?
    } else if let Some(member_connection) = member_invalidation {
        crate::approval::invalidate_connection(connection, &run_id, &member_connection, now_ms)?
    } else {
        Vec::new()
    };
    let mut payload = event(&run);
    if let Some(member_id) = member_id {
        payload["worker_id"] = json!(member_id);
        payload["member_session"] = serde_json::to_value(run.sessions.get(&member_id))?;
    }
    if !invalidated.is_empty() {
        payload["invalidated_approval_ids"] = serde_json::to_value(invalidated)?;
    }
    if let Some(evidence_ref) = evidence_ref {
        payload["evidence_ref"] = Value::String(evidence_ref);
    }
    Ok((CommandResult::Run(run), run_id, kind, payload))
}

pub(super) fn record_runtime_event(
    connection: &Connection,
    run_id: String,
    metadata: RunRuntimeEvent,
) -> Result<RunChange, StoreError> {
    let run = require_run(connection, &run_id)?;
    run.require_runtime_event(&metadata)?;
    // execute holds the SQLite writer lock, so validation and deduplication are
    // atomic with the receipt/event/outbox insert without rewriting run state.
    let duplicate: bool = connection.query_row(
        "SELECT EXISTS(SELECT 1 FROM events WHERE run_id=?1 AND kind='runtime_event' AND json_extract(payload_json, '$.connection.worker_id')=?2 AND json_extract(payload_json, '$.connection.connection_id')=?3 AND json_extract(payload_json, '$.connection.generation')=?4 AND json_extract(payload_json, '$.sequence')=?5)",
        params![
            run_id,
            metadata.connection.worker_id,
            metadata.connection.connection_id,
            metadata.connection.generation,
            metadata.sequence as i64,
        ],
        |row| row.get(0),
    )?;
    if duplicate {
        return Err(StoreError::RuntimeEventSequenceConflict {
            run_id,
            sequence: metadata.sequence,
        });
    }
    let mut payload = serde_json::to_value(metadata)?;
    payload["run_id"] = json!(run.id);
    payload["owner"] = json!(run.owner);
    payload["status"] = json!(run.status);
    payload["version"] = json!(run.version);
    Ok((CommandResult::Run(run), run_id, "runtime_event", payload))
}

fn require_run(connection: &Connection, run_id: &str) -> Result<Run, StoreError> {
    load_run(connection, run_id)?.ok_or_else(|| StoreError::NotFound {
        kind: "run",
        id: run_id.into(),
    })
}

pub(super) fn require_task_creation(
    connection: &Connection,
    run_id: &str,
) -> Result<(), StoreError> {
    let run = require_run(connection, run_id)?;
    if run.config.is_some()
        && matches!(
            run.status,
            RunStatus::Submitted | RunStatus::Integrated | RunStatus::Accepted
        )
    {
        return Err(forge_core::DomainError::from(RunError::InvalidState(run.status)).into());
    }
    Ok(())
}

pub(super) fn require_task_command(
    connection: &Connection,
    task: &Task,
    command: &Command,
) -> Result<(), StoreError> {
    let run = require_run(connection, &task.run_id)?;
    let Some(config) = &run.config else {
        if matches!(command, Command::AssignTask { .. }) {
            return Err(forge_core::DomainError::from(RunError::Unbound).into());
        }
        return Ok(());
    };
    if matches!(command, Command::ReopenTask { .. }) && run.status != RunStatus::Paused {
        return Err(forge_core::DomainError::from(RunError::InvalidState(run.status)).into());
    }
    let worker_id = match command {
        Command::ClaimTask { worker_id, .. } => Some(worker_id.as_str()),
        Command::RenewClaim { .. } | Command::SubmitTask { .. } => {
            task.claim.as_ref().map(|claim| claim.worker_id.as_str())
        }
        // Assignment itself is authorized below; recovery/release still uses
        // task version, lease and generation fences.
        Command::AssignTask {
            coordinator_id,
            worker_id,
            ..
        } => {
            if config.coordinator_id() != Some(coordinator_id.as_str())
                || !config.permits_worker(worker_id)
            {
                return Err(forge_core::DomainError::from(RunError::UnauthorizedWorker).into());
            }
            return Ok(());
        }
        _ => return Ok(()),
    };
    if run.status != RunStatus::Running {
        return Err(forge_core::DomainError::from(RunError::InvalidState(run.status)).into());
    }
    if let Some(worker_id) = worker_id {
        let assigned = task.assigned_worker_id.as_deref();
        let active = run
            .execution_for(worker_id)
            .filter(|execution| execution.turn_id.is_some())
            .map(|execution| execution.connection.worker_id.as_str());
        if !config.permits_worker(worker_id)
            || assigned.is_some_and(|owner| owner != worker_id)
            || active != Some(worker_id)
        {
            return Err(forge_core::DomainError::from(RunError::UnauthorizedWorker).into());
        }
    }
    Ok(())
}

fn event(run: &Run) -> Value {
    // Keep route/credential references, local paths and submitted content in
    // protected state and receipts. The shared outbox contains execution facts.
    json!({"run_id": run.id, "owner": run.owner, "status": run.status, "version": run.version, "execution": run.execution})
}
