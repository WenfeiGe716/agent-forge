//! Read-only projections of the Python product database. Never use forge-store
//! to open this database: Python remains the sole owner of its records.

mod projection;

use crate::{Backend, BackendError, Event, EventPage, User};
use base64::{engine::general_purpose::STANDARD, Engine};
use projection::{json_column, run_view, RUN_COLUMNS};
use rusqlite::{params, Connection, OpenFlags, OptionalExtension, Row};
use serde_json::Value;
use std::{
    path::{Path, PathBuf},
    time::Duration,
};
use subtle::ConstantTimeEq;

/// An explicitly selected existing SQLite file, with fresh read transactions
/// per call. Sessions remain owned by the Rust HTTP host, not Python's table.
pub struct PythonSqliteBackend {
    path: PathBuf,
}

// CASE protects JSON functions from malformed legacy ACLs. Only an array of
// string IDs grants membership; a string/object containing the ID never does.
const ACCESS: &str = "(
    ?2 = 'admin' OR p.owner_id = ?1 OR
    CASE WHEN json_valid(p.config) THEN
        CASE WHEN json_type(p.config, '$.members') = 'array' THEN
            NOT EXISTS (SELECT 1 FROM json_each(p.config, '$.members') m WHERE m.type != 'text')
            AND EXISTS (SELECT 1 FROM json_each(p.config, '$.members') m
                    WHERE m.type = 'text' AND m.value = ?1)
        ELSE 0 END
    ELSE 0 END
)";

impl PythonSqliteBackend {
    /// Rust-owned runs reuse live product account and project access, without
    /// manufacturing a legacy Run or writing to the Python database.
    pub fn authorize_project(
        &self,
        user: &User,
        project_id: &str,
        write: bool,
    ) -> Result<User, BackendError> {
        self.read(|db| {
            let user = current_user(db, user)?;
            if write && !matches!(user.role.as_str(), "admin" | "developer") {
                return Err(BackendError::Forbidden);
            }
            let allowed = db
                .query_row(
                    &format!("SELECT 1 FROM projects p WHERE p.id = ?3 AND {ACCESS}"),
                    params![user.id, user.role, project_id],
                    |_| Ok(()),
                )
                .optional()
                .map_err(database_error)?;
            allowed.ok_or(BackendError::NotFound)?;
            Ok(user)
        })
    }

    pub fn open(path: impl AsRef<Path>) -> Result<Self, BackendError> {
        let path = path
            .as_ref()
            .canonicalize()
            .map_err(|_| BackendError::Unavailable)?;
        if !path.is_file() {
            return Err(BackendError::InvalidData);
        }
        let backend = Self { path };
        backend.read(|db| {
            // Require real product tables and the columns actually consumed.
            // No version stamp, migrations, CREATE or bootstrap writes.
            for (table, columns) in [
                ("users", "id, username, password_hash, role, enabled"),
                ("projects", "id, owner_id, config"),
                ("runs", RUN_COLUMNS),
                (
                    "tasks",
                    "id, run_id, key, agent_id, kind, title, status, spec, result",
                ),
                ("outbox", "id, run_id, action, payload, sent, error"),
                ("events", "id, run_id, kind, data, created_at"),
            ] {
                let kind: Option<String> = db
                    .query_row(
                        "SELECT type FROM sqlite_schema WHERE name = ?1",
                        [table],
                        |row| row.get(0),
                    )
                    .optional()
                    .map_err(database_error)?;
                if kind.as_deref() != Some("table") {
                    return Err(BackendError::InvalidData);
                }
                db.prepare(&format!("SELECT {columns} FROM {table} r LIMIT 0"))
                    .map_err(|_| BackendError::InvalidData)?;
            }
            Ok(())
        })?;
        Ok(backend)
    }

    fn read<T>(
        &self,
        operation: impl FnOnce(&Connection) -> Result<T, BackendError>,
    ) -> Result<T, BackendError> {
        // Do not enable URI interpretation or immutable mode: committed WAL
        // events and ACL changes must remain visible to subsequent polls.
        let mut db = Connection::open_with_flags(
            &self.path,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )
        .map_err(database_error)?;
        db.busy_timeout(Duration::from_secs(2))
            .map_err(database_error)?;
        db.pragma_update(None, "query_only", true)
            .map_err(database_error)?;
        db.pragma_update(None, "trusted_schema", false)
            .map_err(database_error)?;
        let transaction = db.transaction().map_err(database_error)?;
        operation(&transaction)
    }
}

pub(crate) use projection::redact;

fn database_error(error: rusqlite::Error) -> BackendError {
    use rusqlite::Error::*;
    match error {
        InvalidColumnType(..) | IntegralValueOutOfRange(..) | FromSqlConversionFailure(..) => {
            BackendError::InvalidData
        }
        _ => BackendError::Unavailable,
    }
}

fn user_row(row: &Row<'_>) -> rusqlite::Result<User> {
    Ok(User {
        id: row.get(0)?,
        username: row.get(1)?,
        role: row.get(2)?,
        enabled: row.get::<_, i64>(3)? == 1,
    })
}

fn load_user(db: &Connection, id: &str) -> Result<Option<User>, BackendError> {
    db.query_row(
        "SELECT id, username, role, enabled FROM users WHERE id = ?1",
        [id],
        user_row,
    )
    .optional()
    .map_err(database_error)
}

fn current_user(db: &Connection, user: &User) -> Result<User, BackendError> {
    load_user(db, &user.id)?
        .filter(|user| {
            user.enabled && matches!(user.role.as_str(), "admin" | "developer" | "viewer")
        })
        .ok_or(BackendError::NotFound)
}

fn authorize_run(db: &Connection, user: &User, run_id: &str) -> Result<(), BackendError> {
    let allowed = db
        .query_row(
            &format!(
                "SELECT 1 FROM runs r JOIN projects p ON p.id = r.project_id
                  WHERE r.id = ?3 AND {ACCESS}"
            ),
            params![user.id, user.role, run_id],
            |_| Ok(()),
        )
        .optional()
        .map_err(database_error)?;
    allowed.ok_or(BackendError::NotFound)
}

fn password_matches(password: &str, encoded: Option<&str>) -> bool {
    // Python hashlib.scrypt defaults to dklen=64, while PHC scrypt hashes in
    // MemoryBackend use a different encoding and output length.
    let decoded = encoded
        .filter(|value| value.len() == 108)
        .and_then(|value| STANDARD.decode(value).ok())
        .filter(|raw| raw.len() == 80);
    let dummy = [0_u8; 80];
    let raw = decoded.as_deref().unwrap_or(&dummy);
    let Ok(params) = scrypt::Params::new(14, 8, 1, 64) else {
        return false;
    };
    let mut digest = [0_u8; 64];
    let computed = scrypt::scrypt(password.as_bytes(), &raw[..16], &params, &mut digest).is_ok();
    let matches = bool::from(digest.ct_eq(&raw[16..]));
    computed && decoded.is_some() && matches
}

impl Backend for PythonSqliteBackend {
    fn check_credentials(
        &self,
        username: &str,
        password: &str,
    ) -> Result<Option<User>, BackendError> {
        if username.is_empty()
            || username.chars().count() > 80
            || password.is_empty()
            || password.chars().count() > 512
        {
            return Ok(None);
        }
        let candidate = self.read(|db| {
            db.query_row(
                "SELECT id, username, role, enabled, password_hash FROM users WHERE username = ?1",
                [username],
                |row| Ok((user_row(row)?, row.get::<_, String>(4)?)),
            )
            .optional()
            .map_err(database_error)
        })?;
        // Do the same bounded KDF work for unknown, disabled and malformed
        // accounts. No password hash or underlying DB error enters a DTO/log.
        let valid = password_matches(password, candidate.as_ref().map(|(_, hash)| hash.as_str()));
        if !valid {
            return Ok(None);
        }
        let Some((candidate, encoded)) = candidate else {
            return Ok(None);
        };
        self.read(|db| {
            let current = db
                .query_row(
                    "SELECT id, username, role, enabled, password_hash FROM users WHERE id = ?1",
                    [&candidate.id],
                    |row| Ok((user_row(row)?, row.get::<_, String>(4)?)),
                )
                .optional()
                .map_err(database_error)?;
            Ok(current
                .filter(|(user, hash)| {
                    user.enabled
                        && user.username == username
                        && matches!(user.role.as_str(), "admin" | "developer" | "viewer")
                        && *hash == encoded
                })
                .map(|(user, _)| user))
        })
    }

    fn user(&self, user_id: &str) -> Result<Option<User>, BackendError> {
        self.read(|db| load_user(db, user_id))
    }

    fn runs(&self, user: &User, limit: usize) -> Result<Vec<Value>, BackendError> {
        if !(1..=500).contains(&limit) {
            return Err(BackendError::InvalidData);
        }
        self.read(|db| {
            let user = current_user(db, user)?;
            let mut statement = db
                .prepare(&format!(
                    "SELECT {RUN_COLUMNS} FROM runs r JOIN projects p ON p.id = r.project_id
                 WHERE {ACCESS} ORDER BY r.created_at DESC, r.id ASC LIMIT ?3"
                ))
                .map_err(database_error)?;
            let mut rows = statement
                .query(params![user.id, user.role, limit as i64])
                .map_err(database_error)?;
            let mut values = Vec::new();
            while let Some(row) = rows.next().map_err(database_error)? {
                values.push(run_view(db, row, false)?);
            }
            Ok(values)
        })
    }

    fn run_statuses(
        &self,
        user: &User,
    ) -> Result<std::collections::BTreeMap<String, String>, BackendError> {
        self.read(|db| {
            let user = current_user(db, user)?;
            let mut statement = db
                .prepare(&format!(
                    "SELECT r.id, r.status FROM runs r JOIN projects p ON p.id = r.project_id WHERE {ACCESS}"
                ))
                .map_err(database_error)?;
            let rows = statement
                .query_map(params![user.id, user.role], |r| Ok((r.get(0)?, r.get(1)?)))
                .map_err(database_error)?;
            rows.collect::<Result<_, _>>().map_err(database_error)
        })
    }

    fn run(&self, user: &User, run_id: &str) -> Result<Value, BackendError> {
        self.read(|db| {
            let user = current_user(db, user)?;
            authorize_run(db, &user, run_id)?;
            let mut statement = db
                .prepare(&format!("SELECT {RUN_COLUMNS} FROM runs r WHERE r.id = ?1"))
                .map_err(database_error)?;
            let mut rows = statement.query([run_id]).map_err(database_error)?;
            run_view(
                db,
                rows.next()
                    .map_err(database_error)?
                    .ok_or(BackendError::NotFound)?,
                true,
            )
        })
    }

    fn events(
        &self,
        user: &User,
        run_id: &str,
        after: i64,
        limit: usize,
    ) -> Result<EventPage, BackendError> {
        if after < 0 || !(1..=1000).contains(&limit) {
            return Err(BackendError::InvalidData);
        }
        self.read(|db| {
            let user = current_user(db, user)?;
            authorize_run(db, &user, run_id)?;
            let mut statement = db
                .prepare(
                    "SELECT id, run_id, kind, data, created_at FROM events
                 WHERE run_id = ?1 AND id > ?2 ORDER BY id ASC LIMIT ?3",
                )
                .map_err(database_error)?;
            let mut rows = statement
                .query(params![run_id, after, (limit + 1) as i64])
                .map_err(database_error)?;
            let mut events = Vec::new();
            let mut has_more = false;
            while let Some(row) = rows.next().map_err(database_error)? {
                if events.len() == limit {
                    has_more = true;
                    break;
                }
                events.push(Event {
                    id: row.get(0).map_err(database_error)?,
                    run_id: row.get(1).map_err(database_error)?,
                    kind: row.get(2).map_err(database_error)?,
                    data: redact(json_column(row, 3)?),
                    created_at: row.get(4).map_err(database_error)?,
                });
            }
            Ok(EventPage { events, has_more })
        })
    }
}
