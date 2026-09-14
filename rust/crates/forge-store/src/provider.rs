use crate::{Connection, StoreError};
use forge_core::{ExecutionPolicy, ProviderSnapshot, RouteBinding, Run};
use rusqlite::{params, OptionalExtension};

pub(super) fn load_snapshot(
    connection: &Connection,
    provider_id: &str,
    revision_id: &str,
) -> Result<Option<ProviderSnapshot>, StoreError> {
    let raw: Option<String> = connection.query_row(
        "SELECT snapshot_json FROM provider_snapshots WHERE provider_id=?1 AND provider_revision_id=?2",
        params![provider_id, revision_id], |row| row.get(0),
    ).optional()?;
    raw.map(|value| serde_json::from_str(&value).map_err(Into::into))
        .transpose()
}

pub(super) fn load_run_snapshot(
    connection: &Connection,
    run_id: &str,
) -> Result<Option<ProviderSnapshot>, StoreError> {
    let raw: Option<String> = connection.query_row(
        "SELECT p.snapshot_json FROM provider_snapshots p JOIN run_provider_snapshots r USING(provider_id, provider_revision_id) WHERE r.run_id=?1",
        [run_id], |row| row.get(0),
    ).optional()?;
    raw.map(|value| serde_json::from_str(&value).map_err(Into::into))
        .transpose()
}

pub(super) fn retain_snapshot(
    connection: &Connection,
    snapshot: &ProviderSnapshot,
) -> Result<(), StoreError> {
    snapshot.validate()?;
    if let Some(existing) = load_snapshot(
        connection,
        &snapshot.provider_id,
        &snapshot.provider_revision_id,
    )? {
        if existing != *snapshot {
            return Err(StoreError::AlreadyExists {
                kind: "provider revision",
                id: format!("{}/{}", snapshot.provider_id, snapshot.provider_revision_id),
            });
        }
        return Ok(());
    }
    connection.execute(
        "INSERT INTO provider_snapshots(provider_id, provider_revision_id, snapshot_json) VALUES (?1, ?2, ?3)",
        params![snapshot.provider_id, snapshot.provider_revision_id, serde_json::to_string(snapshot)?],
    )?;
    Ok(())
}

pub(super) fn bind_snapshot(
    connection: &Connection,
    run_id: &str,
    snapshot: &ProviderSnapshot,
) -> Result<(), StoreError> {
    connection.execute(
        "INSERT INTO run_provider_snapshots(run_id, provider_id, provider_revision_id) VALUES (?1, ?2, ?3)",
        params![run_id, snapshot.provider_id, snapshot.provider_revision_id],
    )?;
    Ok(())
}

pub(super) fn retain_run_config(connection: &Connection, run: &Run) -> Result<(), StoreError> {
    if let Some(config) = &run.config {
        connection.execute(
            "INSERT INTO run_routes(run_id, snapshot_json) VALUES (?1, ?2)",
            params![run.id, serde_json::to_string(&config.route_binding)?],
        )?;
        connection.execute(
            "INSERT INTO run_permissions(run_id, snapshot_json) VALUES (?1, ?2)",
            params![
                run.id,
                serde_json::to_string(&config.permission_profile.execution_policy())?
            ],
        )?;
    }
    Ok(())
}

pub(super) fn load_route(
    connection: &Connection,
    run_id: &str,
) -> Result<Option<RouteBinding>, StoreError> {
    let raw: Option<String> = connection
        .query_row(
            "SELECT snapshot_json FROM run_routes WHERE run_id=?1",
            [run_id],
            |row| row.get(0),
        )
        .optional()?;
    raw.map(|value| serde_json::from_str(&value).map_err(Into::into))
        .transpose()
}

pub(super) fn load_policy(
    connection: &Connection,
    run_id: &str,
) -> Result<Option<ExecutionPolicy>, StoreError> {
    let raw: Option<String> = connection
        .query_row(
            "SELECT snapshot_json FROM run_permissions WHERE run_id=?1",
            [run_id],
            |row| row.get(0),
        )
        .optional()?;
    raw.map(|value| serde_json::from_str(&value).map_err(Into::into))
        .transpose()
}
