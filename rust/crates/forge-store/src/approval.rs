use crate::{load_run, Command, CommandResult, Connection, StoreError};
use forge_core::{Approval, ApprovalDecision, ApprovalError, DomainError, RunConnection};
use rusqlite::{params, OptionalExtension};
use serde_json::{json, Value};

type ApprovalChange = (CommandResult, String, &'static str, Value);

pub(super) fn load(connection: &Connection, id: &str) -> Result<Option<Approval>, StoreError> {
    let raw: Option<String> = connection
        .query_row(
            "SELECT snapshot_json FROM runtime_approvals WHERE id=?1",
            [id],
            |row| row.get(0),
        )
        .optional()?;
    raw.map(|value| serde_json::from_str(&value).map_err(Into::into))
        .transpose()
}

pub(super) fn list(
    connection: &Connection,
    run_id: &str,
    limit: u32,
) -> Result<Vec<Approval>, StoreError> {
    let mut statement = connection.prepare(
        "SELECT snapshot_json FROM runtime_approvals WHERE run_id=?1 ORDER BY status IN ('pending', 'queued', 'dispatching') DESC, rowid DESC LIMIT ?2",
    )?;
    let rows = statement.query_map(params![run_id, limit.min(1000)], |row| {
        row.get::<_, String>(0)
    })?;
    rows.map(|row| Ok(serde_json::from_str(&row?)?)).collect()
}

pub(super) fn execute(
    connection: &Connection,
    command: Command,
    now_ms: i64,
) -> Result<ApprovalChange, StoreError> {
    match command {
        Command::RecordApproval {
            approval_id,
            request,
        } => {
            if load(connection, &approval_id)?.is_some() {
                return Err(StoreError::AlreadyExists {
                    kind: "approval",
                    id: approval_id,
                });
            }
            let run = require_run(connection, &request.run_id)?;
            let approval = Approval::new(approval_id, *request, &run, now_ms)?;
            let execution = &approval.request.execution.connection;
            let request_json = serde_json::to_string(&approval.request.request_id)?;
            let duplicate: bool = connection.query_row(
                "SELECT EXISTS(SELECT 1 FROM runtime_approvals WHERE run_id=?1 AND connection_id=?2 AND connection_generation=?3 AND request_id_json=?4)",
                params![run.id, execution.connection_id, execution.generation, request_json], |row| row.get(0),
            )?;
            if duplicate {
                return Err(StoreError::AlreadyExists {
                    kind: "approval callback",
                    id: approval.id,
                });
            }
            connection.execute(
                "INSERT INTO runtime_approvals(id, run_id, connection_id, connection_generation, request_id_json, version, status, snapshot_json) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
                params![approval.id, run.id, execution.connection_id, execution.generation, request_json,
                    approval.version, status(&approval)?, serde_json::to_string(&approval)?],
            )?;
            let payload = event(&approval);
            Ok((
                CommandResult::Approval(Box::new(approval)),
                run.id,
                "approval_requested",
                payload,
            ))
        }
        Command::ExpireApprovals {
            run_id,
            connection: execution,
        } => {
            require_run(connection, &run_id)?;
            let approvals = invalidate(connection, &run_id, Some(&execution), now_ms)?;
            let payload = json!({"run_id": run_id, "approvals": approvals.iter().map(event).collect::<Vec<_>>()});
            Ok((
                CommandResult::Approvals(approvals),
                run_id,
                "approvals_expired",
                payload,
            ))
        }
        command => {
            let id = match &command {
                Command::DecideApproval { approval_id, .. }
                | Command::BeginApprovalDispatch { approval_id, .. }
                | Command::FinishApprovalDispatch { approval_id, .. } => approval_id,
                _ => unreachable!(),
            };
            let mut approval = load(connection, id)?.ok_or_else(|| StoreError::NotFound {
                kind: "approval",
                id: id.clone(),
            })?;
            let old_version = approval.version;
            let run_id = approval.request.run_id.clone();
            let kind = match command {
                Command::DecideApproval {
                    expected_version,
                    digest,
                    approved,
                    actor_id,
                    comment,
                    ..
                } => {
                    let run = require_run(connection, &run_id)?;
                    approval.decide(
                        expected_version,
                        &digest,
                        ApprovalDecision {
                            approved,
                            actor_id,
                            comment,
                        },
                        &run,
                        now_ms,
                    )?;
                    "approval_decision_queued"
                }
                Command::BeginApprovalDispatch {
                    expected_version,
                    execution,
                    ..
                } => {
                    let run = require_run(connection, &run_id)?;
                    approval.begin_dispatch(expected_version, &execution, &run, now_ms)?;
                    "approval_dispatching"
                }
                Command::FinishApprovalDispatch {
                    expected_version,
                    execution,
                    delivered,
                    ..
                } => {
                    if execution != approval.request.execution {
                        return Err(DomainError::from(ApprovalError::StaleExecution).into());
                    }
                    approval.finish_dispatch(expected_version, delivered, now_ms)?;
                    if delivered {
                        "approval_delivered"
                    } else {
                        "approval_delivery_unknown"
                    }
                }
                _ => unreachable!(),
            };
            persist(connection, &approval, old_version)?;
            let payload = event(&approval);
            Ok((
                CommandResult::Approval(Box::new(approval)),
                run_id,
                kind,
                payload,
            ))
        }
    }
}

pub(super) fn require_resolved(connection: &Connection, run_id: &str) -> Result<(), StoreError> {
    let unresolved: bool = connection.query_row(
        "SELECT EXISTS(SELECT 1 FROM runtime_approvals WHERE run_id=?1 AND status IN ('pending', 'queued', 'dispatching'))",
        [run_id], |row| row.get(0),
    )?;
    if unresolved {
        return Err(DomainError::from(ApprovalError::UnresolvedApprovals).into());
    }
    Ok(())
}

pub(super) fn require_connection_resolved(
    connection: &Connection,
    run_id: &str,
    execution: &RunConnection,
) -> Result<(), StoreError> {
    let unresolved: bool = connection.query_row(
        "SELECT EXISTS(SELECT 1 FROM runtime_approvals WHERE run_id=?1 AND connection_id=?2 AND connection_generation=?3 AND status IN ('pending', 'queued', 'dispatching'))",
        params![run_id, execution.connection_id, execution.generation], |row| row.get(0),
    )?;
    if unresolved {
        return Err(DomainError::from(ApprovalError::UnresolvedApprovals).into());
    }
    Ok(())
}

pub(super) fn invalidate_run(
    connection: &Connection,
    run_id: &str,
    now_ms: i64,
) -> Result<Vec<String>, StoreError> {
    Ok(invalidate(connection, run_id, None, now_ms)?
        .into_iter()
        .map(|approval| approval.id)
        .collect())
}

pub(super) fn invalidate_connection(
    connection: &Connection,
    run_id: &str,
    execution: &RunConnection,
    now_ms: i64,
) -> Result<Vec<String>, StoreError> {
    Ok(invalidate(connection, run_id, Some(execution), now_ms)?
        .into_iter()
        .map(|approval| approval.id)
        .collect())
}

fn invalidate(
    connection: &Connection,
    run_id: &str,
    execution: Option<&RunConnection>,
    now_ms: i64,
) -> Result<Vec<Approval>, StoreError> {
    let mut statement = connection.prepare(
        "SELECT snapshot_json FROM runtime_approvals WHERE run_id=?1 AND status IN ('pending', 'queued', 'dispatching') ORDER BY id",
    )?;
    let rows = statement.query_map([run_id], |row| row.get::<_, String>(0))?;
    let mut changed = Vec::new();
    for row in rows {
        let mut approval: Approval = serde_json::from_str(&row?)?;
        if execution.is_some_and(|identity| identity != &approval.request.execution.connection) {
            continue;
        }
        let old_version = approval.version;
        if approval.invalidate(now_ms)? {
            changed.push((approval, old_version));
        }
    }
    // Finish reading before mutating the rows visited by the query.
    drop(statement);
    for (approval, old_version) in &changed {
        persist(connection, approval, *old_version)?;
    }
    Ok(changed.into_iter().map(|(approval, _)| approval).collect())
}

fn persist(
    connection: &Connection,
    approval: &Approval,
    old_version: i64,
) -> Result<(), StoreError> {
    let updated = connection.execute(
        "UPDATE runtime_approvals SET version=?1, status=?2, snapshot_json=?3 WHERE id=?4 AND version=?5",
        params![approval.version, status(approval)?, serde_json::to_string(approval)?, approval.id, old_version],
    )?;
    if updated != 1 {
        return Err(StoreError::ConcurrentMutation);
    }
    Ok(())
}

fn status(approval: &Approval) -> Result<String, StoreError> {
    Ok(serde_json::from_value(serde_json::to_value(
        approval.status,
    )?)?)
}

fn require_run(connection: &Connection, run_id: &str) -> Result<forge_core::Run, StoreError> {
    load_run(connection, run_id)?.ok_or_else(|| StoreError::NotFound {
        kind: "run",
        id: run_id.into(),
    })
}

fn event(approval: &Approval) -> Value {
    // Callback IDs, request bodies, comments and decisions stay in protected records.
    json!({"approval_id": approval.id, "run_id": approval.request.run_id,
        "status": approval.status, "version": approval.version, "execution_completed": false})
}
