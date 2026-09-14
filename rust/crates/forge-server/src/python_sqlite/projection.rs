use super::database_error;
use crate::BackendError;
use rusqlite::{Connection, Row};
use serde_json::{json, Value};

pub(super) const RUN_COLUMNS: &str = "r.id, r.project_id, r.title, r.request, r.status,
    r.calls, r.input_tokens, r.output_tokens, r.error, r.created_at, r.updated_at,
    r.snapshot, r.plan, r.plan_digest, r.artifact_digest, r.report";

pub(super) fn json_column(row: &Row<'_>, index: usize) -> Result<Value, BackendError> {
    let raw: Option<String> = row.get(index).map_err(database_error)?;
    match raw {
        Some(raw) => serde_json::from_str(&raw).map_err(|_| BackendError::InvalidData),
        None => Ok(Value::Null),
    }
}

/// Field-based defense in depth, not a detector for secrets inside prose or
/// arbitrary tool output. Keep token counts and permission descriptions visible.
pub(crate) fn redact(mut value: Value) -> Value {
    fn visit(value: &mut Value) {
        match value {
            Value::Object(fields) => {
                for (key, value) in fields {
                    let key: String = key
                        .chars()
                        .filter(|c| c.is_ascii_alphanumeric())
                        .map(|c| c.to_ascii_lowercase())
                        .collect();
                    if sensitive(&key) {
                        *value = Value::String("[REDACTED]".into());
                    } else {
                        visit(value);
                    }
                }
            }
            Value::Array(items) => items.iter_mut().for_each(visit),
            _ => {}
        }
    }
    visit(&mut value);
    value
}

fn sensitive(key: &str) -> bool {
    matches!(
        key,
        "auth"
            | "authorization"
            | "proxyauthorization"
            | "cookie"
            | "cookies"
            | "setcookie"
            | "headers"
            | "httpheaders"
            | "envheaders"
            | "environment"
            | "environmentvariables"
            | "envvars"
            | "env"
            | "token"
            | "tokens"
            | "privatekey"
            | "claim"
            | "claims"
            | "claimtoken"
            | "credential"
            | "credentials"
    ) || key.contains("password")
        || key.contains("passwd")
        || key.contains("secret")
        || key.ends_with("apikey")
        || key.ends_with("encryptedkey")
        || key.ends_with("privatekey")
        || key.ends_with("encryptionkey")
        || key.ends_with("signingkey")
        || key.ends_with("headers")
        || key.ends_with("token")
        || key.ends_with("tokenhash")
        || key.ends_with("credentials")
        || key.ends_with("credential")
}

pub(super) fn run_view(
    db: &Connection,
    row: &Row<'_>,
    detailed: bool,
) -> Result<Value, BackendError> {
    let snapshot = json_column(row, 11)?;
    if !snapshot.is_null() && !snapshot.is_object() {
        return Err(BackendError::InvalidData);
    }
    let strategy = snapshot
        .get("execution_strategy")
        .and_then(Value::as_str)
        .unwrap_or("workflow");
    let agents = snapshot.get("agents");
    if agents.is_some_and(|agents| !agents.is_object()) {
        return Err(BackendError::InvalidData);
    }
    // SQLite retains object insertion order, matching Python list(snapshot.agents).
    let raw_snapshot: Option<String> = row.get(11).map_err(database_error)?;
    let mut agent_query = db
        .prepare("SELECT key FROM json_each(?1, '$.agents') ORDER BY id")
        .map_err(database_error)?;
    let agent_ids = agent_query
        .query_map([raw_snapshot.as_deref().unwrap_or("null")], |row| {
            row.get::<_, String>(0)
        })
        .map_err(database_error)?
        .collect::<rusqlite::Result<Vec<_>>>()
        .map_err(database_error)?;
    let id: String = row.get(0).map_err(database_error)?;
    let mut output = json!({
        "id": id,
        "project_id": row.get::<_, String>(1).map_err(database_error)?,
        "title": row.get::<_, String>(2).map_err(database_error)?,
        "request": row.get::<_, String>(3).map_err(database_error)?,
        "status": row.get::<_, String>(4).map_err(database_error)?,
        "calls": row.get::<_, i64>(5).map_err(database_error)?,
        "input_tokens": row.get::<_, i64>(6).map_err(database_error)?,
        "output_tokens": row.get::<_, i64>(7).map_err(database_error)?,
        "error": row.get::<_, String>(8).map_err(database_error)?,
        "created_at": row.get::<_, String>(9).map_err(database_error)?,
        "updated_at": row.get::<_, String>(10).map_err(database_error)?,
        "agent_ids": agent_ids,
        "mode": if strategy == "autonomous" { json!("autonomous") }
                else { snapshot.get("orchestration").and_then(|o| o.get("mode")).cloned().unwrap_or(json!("development")) },
        "execution_strategy": strategy,
        "execution_engine": snapshot.get("execution_engine").cloned().unwrap_or(json!("legacy")),
        "run_project_checks": snapshot.get("run_project_checks").cloned().unwrap_or(json!(strategy == "workflow")),
        "entry_agent_id": snapshot.get("entry_agent_id"),
        "planner_id": snapshot.get("planner_id"),
        "reviewer_id": snapshot.get("reviewer_id"),
    });
    if detailed {
        output["snapshot"] = snapshot;
        output["plan"] = json_column(row, 12)?;
        output["plan_digest"] = row.get::<_, String>(13).map_err(database_error)?.into();
        output["artifact_digest"] = row.get::<_, String>(14).map_err(database_error)?.into();
        output["report"] = json_column(row, 15)?;
        output["tasks"] = tasks(db, &id)?.into();
        output["commands"] = commands(db, &id)?.into();
        output["event_cursor"] = db
            .query_row(
                "SELECT COALESCE(MAX(id), 0) FROM events WHERE run_id = ?1",
                [&id],
                |row| row.get::<_, i64>(0),
            )
            .map_err(database_error)?
            .into();
    }
    // Filter after projection too: legacy snapshot fields may contain objects
    // even where current Python input validation expects a scalar.
    Ok(redact(output))
}

fn tasks(db: &Connection, run_id: &str) -> Result<Vec<Value>, BackendError> {
    let mut statement = db
        .prepare(
            "SELECT id, key, agent_id, kind, title, status, spec, result
                                    FROM tasks WHERE run_id = ?1 ORDER BY rowid",
        )
        .map_err(database_error)?;
    let mut rows = statement.query([run_id]).map_err(database_error)?;
    let mut tasks = Vec::new();
    while let Some(row) = rows.next().map_err(database_error)? {
        tasks.push(json!({
            "id": row.get::<_, String>(0).map_err(database_error)?,
            "key": row.get::<_, String>(1).map_err(database_error)?,
            "agent_id": row.get::<_, String>(2).map_err(database_error)?,
            "kind": row.get::<_, String>(3).map_err(database_error)?,
            "title": row.get::<_, String>(4).map_err(database_error)?,
            "status": row.get::<_, String>(5).map_err(database_error)?,
            "spec": json_column(row, 6)?,
            "result": json_column(row, 7)?,
        }));
    }
    Ok(tasks)
}

fn commands(db: &Connection, run_id: &str) -> Result<Vec<Value>, BackendError> {
    let mut statement = db
        .prepare(
            "SELECT id, action, payload, sent, error
                                    FROM outbox WHERE run_id = ?1 ORDER BY rowid",
        )
        .map_err(database_error)?;
    let mut rows = statement.query([run_id]).map_err(database_error)?;
    let mut commands = Vec::new();
    while let Some(row) = rows.next().map_err(database_error)? {
        commands.push(json!({
            "id": row.get::<_, String>(0).map_err(database_error)?,
            "action": row.get::<_, String>(1).map_err(database_error)?,
            "payload": json_column(row, 2)?,
            "sent": row.get::<_, i64>(3).map_err(database_error)? == 1,
            "error": row.get::<_, String>(4).map_err(database_error)?,
        }));
    }
    Ok(commands)
}
