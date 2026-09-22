//! The Rust-owned product catalog. Legacy imports use SQLite's online backup
//! against a read-only source and are published only after validation succeeds.

use crate::{Backend, BackendError, PythonSqliteBackend, User};
use base64::{engine::general_purpose::STANDARD, Engine};
use fernet::Fernet;
use rusqlite::{params, Connection, OpenFlags, OptionalExtension, TransactionBehavior};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use uuid::Uuid;

pub const PRODUCT_SCHEMA: &str = concat!(
    include_str!("product_store/schema.sql"),
    "\n",
    include_str!("product_store/extensions.sql")
);
const EXTENSIONS: &str = include_str!("product_store/extensions.sql");
const OWNER: &str = "forge-rust-product";
// Bump this whenever the extension catalog grows.  Existing databases are
// upgraded transactionally in `verify_owner`; the extension DDL is
// idempotent, so a partially upgraded process can safely be retried.
const VERSION: i64 = 4;

pub struct ProductStore {
    path: PathBuf,
    cipher: Option<Fernet>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ProductOutboxMessage {
    pub id: String,
    pub run_id: String,
    pub action: String,
    pub payload: serde_json::Value,
    pub attempts: i64,
}

impl ProductStore {
    pub fn open(
        path: impl AsRef<Path>,
        legacy_path: Option<&Path>,
        admin_password: Option<&str>,
    ) -> Result<Self, BackendError> {
        let requested = path.as_ref();
        let parent = requested
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .unwrap_or(Path::new("."));
        fs::create_dir_all(parent).map_err(io_error)?;
        let path = parent
            .canonicalize()
            .map_err(io_error)?
            .join(requested.file_name().ok_or(BackendError::InvalidData)?);
        let source = legacy_path
            .map(Path::canonicalize)
            .transpose()
            .map_err(io_error)?;
        if source.as_ref().is_some_and(|source| {
            source == &path || path.canonicalize().ok().as_ref() == Some(source)
        }) {
            return Err(BackendError::InvalidData);
        }
        #[cfg(unix)]
        if let (Some(source), Ok(target)) = (&source, fs::metadata(&path)) {
            use std::os::unix::fs::MetadataExt;
            let source = fs::metadata(source).map_err(io_error)?;
            if target.dev() == source.dev() && target.ino() == source.ino() {
                return Err(BackendError::InvalidData);
            }
        }
        if !path.exists() {
            initialize(&path, source.as_deref(), admin_password)?;
        }
        let store = Self {
            path,
            cipher: std::env::var("ENCRYPTION_KEY")
                .ok()
                .filter(|key| !key.is_empty())
                .map(|key| Fernet::new(&key).ok_or(BackendError::InvalidData))
                .transpose()?,
        };
        // Several desktop processes can race during the first launch.  The
        // winner publishes a complete database atomically, while the other
        // openers may briefly contend on the owner migration or WAL pragma.
        // Reopen the connection with bounded backoff instead of surfacing a
        // transient SQLITE_BUSY as an unfriendly startup failure.
        let mut ready = false;
        for attempt in 0..12_u64 {
            let result = (|| {
                let mut db = store.connect()?;
                verify_owner(&mut db)?;
                validate_schema(&db)?;
                db.pragma_update(None, "journal_mode", "WAL")
                    .map_err(db_error)?;
                Ok::<(), BackendError>(())
            })();
            match result {
                Ok(()) => {
                    ready = true;
                    break;
                }
                Err(BackendError::Unavailable) if attempt < 11 => {
                    std::thread::sleep(Duration::from_millis(15 * (attempt + 1)));
                }
                Err(error) => return Err(error),
            }
        }
        if !ready {
            return Err(BackendError::Unavailable);
        }
        Ok(store)
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn connect(&self) -> Result<Connection, BackendError> {
        let db = Connection::open_with_flags(
            &self.path,
            OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )
        .map_err(db_error)?;
        db.busy_timeout(Duration::from_secs(5)).map_err(db_error)?;
        db.pragma_update(None, "foreign_keys", true)
            .map_err(db_error)?;
        db.pragma_update(None, "trusted_schema", false)
            .map_err(db_error)?;
        Ok(db)
    }

    pub fn with_encryption_key(mut self, key: &str) -> Result<Self, BackendError> {
        self.cipher = Some(Fernet::new(key).ok_or(BackendError::InvalidData)?);
        Ok(self)
    }

    pub fn encrypt_secret(&self, secret: &str) -> Result<String, BackendError> {
        if secret.is_empty() {
            return Ok(String::new());
        }
        Ok(self
            .cipher
            .as_ref()
            .ok_or(BackendError::Unavailable)?
            .encrypt(secret.as_bytes()))
    }

    pub fn decrypt_secret(&self, encrypted: &str) -> Result<String, BackendError> {
        if encrypted.is_empty() {
            return Ok(String::new());
        }
        let bytes = self
            .cipher
            .as_ref()
            .ok_or(BackendError::Unavailable)?
            .decrypt(encrypted)
            .map_err(|_| BackendError::InvalidData)?;
        String::from_utf8(bytes).map_err(|_| BackendError::InvalidData)
    }

    pub fn hash_password(password: &str) -> Result<String, BackendError> {
        if !(12..=512).contains(&password.chars().count()) {
            return Err(BackendError::InvalidData);
        }
        let salt = Uuid::new_v4().into_bytes();
        let mut raw = [0_u8; 80];
        raw[..16].copy_from_slice(&salt);
        let params = scrypt::Params::new(14, 8, 1, 64).map_err(|_| BackendError::Unavailable)?;
        scrypt::scrypt(password.as_bytes(), &salt, &params, &mut raw[16..])
            .map_err(|_| BackendError::Unavailable)?;
        Ok(STANDARD.encode(raw))
    }

    pub fn check_credentials(
        &self,
        username: &str,
        password: &str,
    ) -> Result<Option<User>, BackendError> {
        PythonSqliteBackend::open(&self.path)?.check_credentials(username, password)
    }

    pub fn user(&self, user_id: &str) -> Result<Option<User>, BackendError> {
        PythonSqliteBackend::open(&self.path)?.user(user_id)
    }

    pub fn authorize_project(
        &self,
        user: &User,
        project_id: &str,
        write: bool,
    ) -> Result<User, BackendError> {
        PythonSqliteBackend::open(&self.path)?.authorize_project(user, project_id, write)
    }

    /// The raw bearer token is never stored. Python's ISO-8601 session expiry
    /// format is retained so imported sessions remain usable after cutover.
    pub fn create_session(
        &self,
        user_id: &str,
        token: &str,
        ttl: Duration,
    ) -> Result<(), BackendError> {
        if token.len() < 32 || token.len() > 256 || ttl.is_zero() {
            return Err(BackendError::InvalidData);
        }
        let now = epoch_seconds()?;
        let expires = now
            .checked_add(ttl.as_secs().min(43_200) as i64)
            .ok_or(BackendError::InvalidData)?;
        let mut db = self.connect()?;
        let transaction = db
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(db_error)?;
        let enabled: bool = transaction.query_row("SELECT EXISTS(SELECT 1 FROM users WHERE id=?1 AND enabled=1 AND role IN ('admin','developer','viewer'))", [user_id], |row| row.get(0)).map_err(db_error)?;
        if !enabled {
            return Err(BackendError::NotFound);
        }
        transaction.execute("DELETE FROM sessions WHERE unixepoch(expires_at) IS NULL OR unixepoch(expires_at)<=?1", [now]).map_err(db_error)?;
        let count: i64 = transaction
            .query_row("SELECT count(*) FROM sessions", [], |row| row.get(0))
            .map_err(db_error)?;
        if count >= 10_000 {
            return Err(BackendError::Unavailable);
        }
        transaction.execute("INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?1,?2,strftime('%Y-%m-%dT%H:%M:%f+00:00',?3,'unixepoch'))", params![token_hash(token),user_id,expires]).map_err(db_error)?;
        transaction.commit().map_err(db_error)
    }

    pub fn session_user(&self, token: &str) -> Result<Option<User>, BackendError> {
        if token.len() < 32 || token.len() > 256 {
            return Ok(None);
        }
        self.connect()?.query_row("SELECT u.id,u.username,u.role,u.enabled FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?1 AND unixepoch(s.expires_at)>?2 AND u.enabled=1 AND u.role IN ('admin','developer','viewer')", params![token_hash(token),epoch_seconds()?], |row| Ok(User{id:row.get(0)?,username:row.get(1)?,role:row.get(2)?,enabled:row.get(3)?})).optional().map_err(db_error)
    }

    pub fn revoke_session(&self, token: &str) -> Result<(), BackendError> {
        self.connect()?
            .execute(
                "DELETE FROM sessions WHERE token_hash=?1",
                [token_hash(token)],
            )
            .map_err(db_error)?;
        Ok(())
    }

    pub fn revoke_user_sessions(&self, user_id: &str) -> Result<(), BackendError> {
        self.connect()?
            .execute("DELETE FROM sessions WHERE user_id=?1", [user_id])
            .map_err(db_error)?;
        Ok(())
    }

    /// Return unsent messages for one run in creation order. The caller owns
    /// delivery; rows stay pending until `acknowledge_outbox` succeeds.
    pub fn pending_outbox(
        &self,
        run_id: &str,
        limit: u32,
    ) -> Result<Vec<ProductOutboxMessage>, BackendError> {
        if run_id.is_empty() || !(1..=256).contains(&limit) {
            return Err(BackendError::InvalidData);
        }
        let db = self.connect()?;
        let mut query = db
            .prepare("SELECT id,run_id,action,payload,attempts FROM outbox WHERE run_id=?1 AND sent=0 ORDER BY created_at,id LIMIT ?2")
            .map_err(db_error)?;
        let rows = query
            .query_map(params![run_id, limit], |row| {
                let payload: String = row.get(3)?;
                Ok(ProductOutboxMessage {
                    id: row.get(0)?,
                    run_id: row.get(1)?,
                    action: row.get(2)?,
                    payload: serde_json::from_str(&payload).unwrap_or(serde_json::Value::Null),
                    attempts: row.get(4)?,
                })
            })
            .map_err(db_error)?;
        rows.collect::<std::result::Result<Vec<_>, _>>()
            .map_err(db_error)
    }

    /// Record an attempted delivery while retaining the row for retry. This
    /// update is idempotent and cannot resurrect an acknowledged message.
    pub fn note_outbox_attempt(&self, id: &str, error: &str) -> Result<bool, BackendError> {
        if id.is_empty() || error.len() > 2_000 {
            return Err(BackendError::InvalidData);
        }
        let changed = self
            .connect()?
            .execute(
                "UPDATE outbox SET attempts=attempts+1,error=?1 WHERE id=?2 AND sent=0",
                params![error, id],
            )
            .map_err(db_error)?;
        Ok(changed == 1)
    }

    /// Acknowledge one delivery exactly once. Repeated acknowledgements return
    /// false so callers cannot mistake a duplicate for a new delivery.
    pub fn acknowledge_outbox(&self, id: &str) -> Result<bool, BackendError> {
        if id.is_empty() {
            return Err(BackendError::InvalidData);
        }
        let changed = self
            .connect()?
            .execute(
                "UPDATE outbox SET sent=1,error='' WHERE id=?1 AND sent=0",
                [id],
            )
            .map_err(db_error)?;
        Ok(changed == 1)
    }
}

fn token_hash(token: &str) -> String {
    format!("{:x}", Sha256::digest(token.as_bytes()))
}

fn epoch_seconds() -> Result<i64, BackendError> {
    i64::try_from(
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| BackendError::Unavailable)?
            .as_secs(),
    )
    .map_err(|_| BackendError::Unavailable)
}
fn io_error(_: std::io::Error) -> BackendError {
    BackendError::Unavailable
}
fn db_error(_: rusqlite::Error) -> BackendError {
    BackendError::Unavailable
}

fn verify_owner(db: &mut Connection) -> Result<(), BackendError> {
    let marker: Option<(String, i64)> = db
        .query_row(
            "SELECT owner,version FROM forge_product_meta WHERE id=1",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()
        .map_err(|_| BackendError::InvalidData)?;
    if marker
        .as_ref()
        .is_some_and(|(owner, version)| owner == OWNER && (1..=VERSION).contains(version))
    {
        let transaction = db
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(db_error)?;
        transaction.execute_batch(EXTENSIONS).map_err(|_| BackendError::InvalidData)?;
        migrate_model_parameter_defaults(&transaction)?;
        validate_schema(&transaction)?;
        if marker.as_ref().is_some_and(|(_, version)| *version != VERSION) {
            transaction
                .execute("UPDATE forge_product_meta SET version=?1 WHERE id=1", [VERSION])
                .map_err(db_error)?;
        }
        transaction.commit().map_err(db_error)?;
    } else if marker != Some((OWNER.to_owned(), VERSION)) {
        return Err(BackendError::InvalidData);
    }
    Ok(())
}

/// Repair model profiles imported from the legacy catalog before any runtime
/// validates them.  Older OpenAI-compatible profiles used `max_tokens`; the
/// Responses API requires `max_output_tokens`.  The migration keeps the
/// explicit numeric value, adds the protocol default when an enabled profile
/// has no output limit, and records the previous profile in the immutable
/// revision table before updating the live row.
fn migrate_model_parameter_defaults(db: &rusqlite::Transaction<'_>) -> Result<(), BackendError> {
    let mut statement = db
        .prepare(
            "SELECT m.id,m.version,m.config,r.config FROM model_profiles m \
             JOIN providers p ON p.id=json_extract(m.config,'$.provider_id') \
             JOIN provider_revisions r ON r.id=p.revision_id AND r.provider_id=p.id",
        )
        .map_err(db_error)?;
    let rows = statement
        .query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, i64>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, String>(3)?,
            ))
        })
        .map_err(db_error)?
        .collect::<rusqlite::Result<Vec<_>>>()
        .map_err(db_error)?;
    drop(statement);
    for (id, version, raw, provider_raw) in rows {
        let mut config: Value = serde_json::from_str(&raw).map_err(|_| BackendError::InvalidData)?;
        let provider: Value = serde_json::from_str(&provider_raw).map_err(|_| BackendError::InvalidData)?;
        let protocol = provider.get("protocol").and_then(Value::as_str).unwrap_or("");
        let enabled = config.get("enabled").and_then(Value::as_bool).unwrap_or(true);
        let mut changed = false;
        let Some(mut parameters) = config
            .get("parameters")
            .and_then(Value::as_object)
            .cloned()
        else {
            continue;
        };
        let Some(mut allowed) = config
            .get("allowed_parameters")
            .and_then(Value::as_array)
            .cloned()
        else {
            continue;
        };
        let output_key = match protocol {
            "openai_responses" => Some("max_output_tokens"),
            "google_gemini" => Some("maxOutputTokens"),
            "anthropic_messages" | "openai_chat" => Some("max_tokens"),
            _ => None,
        };
        if protocol == "openai_responses" {
            if !parameters.contains_key("max_output_tokens") {
                if let Some(value) = parameters.remove("max_tokens") {
                    parameters.insert("max_output_tokens".into(), value);
                    changed = true;
                } else if let Some(value) = parameters.remove("max_completion_tokens") {
                    parameters.insert("max_output_tokens".into(), value);
                    changed = true;
                }
            } else if parameters.remove("max_tokens").is_some() {
                changed = true;
            }
            if parameters.remove("max_completion_tokens").is_some() {
                changed = true;
            }
            if allowed.iter().any(|value| value.as_str() == Some("max_tokens")) {
                for value in allowed.iter_mut() {
                    if value.as_str() == Some("max_tokens") {
                        *value = json!("max_output_tokens");
                        changed = true;
                    }
                }
            }
            let allowed_len = allowed.len();
            allowed.retain(|value| value.as_str() != Some("max_completion_tokens"));
            changed |= allowed.len() != allowed_len;
        }
        if enabled {
            if let Some(key) = output_key {
                if !parameters.contains_key(key) {
                    parameters.insert(key.into(), json!(4096));
                    changed = true;
                }
                if !allowed.iter().any(|value| value.as_str() == Some(key)) {
                    allowed.push(json!(key));
                    changed = true;
                }
            }
        }
        if !changed {
            continue;
        }
        config["parameters"] = Value::Object(parameters);
        config["allowed_parameters"] = Value::Array(allowed);
        let next_version = version.checked_add(1).ok_or(BackendError::InvalidData)?;
        let timestamp: String = db
            .query_row("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')", [], |row| row.get(0))
            .map_err(db_error)?;
        db.execute(
            "INSERT OR IGNORE INTO model_asset_revisions(id,model_id,version,config,actor,created_at) VALUES(?1,?2,?3,?4,?5,?6)",
            params![format!("{id}:{version}"), id, version, raw, "system:model-parameter-migration-v4", timestamp],
        )
        .map_err(db_error)?;
        db.execute(
            "UPDATE model_profiles SET config=?1,version=?2 WHERE id=?3 AND version=?4",
            params![config.to_string(), next_version, id, version],
        )
        .map_err(db_error)?;
    }
    Ok(())
}

fn initialize(
    path: &Path,
    source: Option<&Path>,
    admin_password: Option<&str>,
) -> Result<(), BackendError> {
    let temporary = path.with_extension(format!("import-{}", Uuid::new_v4().simple()));
    let result = (|| {
        let mut options = fs::OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        options.open(&temporary).map_err(io_error)?;
        let mut db = Connection::open(&temporary).map_err(db_error)?;
        if let Some(source) = source {
            let legacy = Connection::open_with_flags(
                source,
                OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
            )
            .map_err(db_error)?;
            legacy
                .busy_timeout(Duration::from_secs(5))
                .map_err(db_error)?;
            legacy
                .pragma_update(None, "query_only", true)
                .map_err(db_error)?;
            legacy
                .pragma_update(None, "trusted_schema", false)
                .map_err(db_error)?;
            let backup = rusqlite::backup::Backup::new(&legacy, &mut db).map_err(db_error)?;
            backup
                .run_to_completion(256, Duration::from_millis(10), None)
                .map_err(db_error)?;
        } else {
            db.execute_batch(PRODUCT_SCHEMA).map_err(db_error)?;
        }
        db.pragma_update(None, "journal_mode", "DELETE")
            .map_err(db_error)?;
        db.pragma_update(None, "trusted_schema", false)
            .map_err(db_error)?;
        db.execute_batch(EXTENSIONS)
            .map_err(|_| BackendError::InvalidData)?;
        validate_schema(&db)?;
        validate_data(&db)?;
        let transaction = db
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(db_error)?;
        transaction.execute_batch("CREATE TABLE forge_product_meta(id INTEGER PRIMARY KEY CHECK(id=1),owner TEXT NOT NULL,version INTEGER NOT NULL,source_path TEXT,imported_at TEXT NOT NULL);").map_err(|_| BackendError::InvalidData)?;
        transaction.execute("INSERT INTO forge_product_meta VALUES(1,?1,?2,?3,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",params![OWNER,VERSION,source.map(|p|p.to_string_lossy().into_owned())]).map_err(db_error)?;
        let count: i64 = transaction
            .query_row("SELECT count(*) FROM users", [], |row| row.get(0))
            .map_err(db_error)?;
        if count == 0 {
            let encoded =
                ProductStore::hash_password(admin_password.ok_or(BackendError::InvalidData)?)?;
            transaction.execute("INSERT INTO users(id,username,password_hash,role,enabled) VALUES(?1,'admin',?2,'admin',1)",params![Uuid::new_v4().simple().to_string(),encoded]).map_err(db_error)?;
        }
        transaction.commit().map_err(db_error)?;
        db.close().map_err(|_| BackendError::Unavailable)?;
        fs::File::open(&temporary)
            .map_err(io_error)?
            .sync_all()
            .map_err(io_error)?;
        // A hard link atomically publishes the complete database and cannot
        // overwrite another process's concurrent successful initialization.
        match fs::hard_link(&temporary, path) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
            Err(error) => return Err(io_error(error)),
        }
        #[cfg(unix)]
        fs::File::open(path.parent().ok_or(BackendError::InvalidData)?)
            .map_err(io_error)?
            .sync_all()
            .map_err(io_error)?;
        Ok(())
    })();
    let _ = fs::remove_file(&temporary);
    result
}

fn columns(db: &Connection, table: &str) -> Result<BTreeMap<String, (String, bool)>, BackendError> {
    let mut statement = db
        .prepare("SELECT name,type,\"notnull\" FROM pragma_table_info(?1)")
        .map_err(db_error)?;
    let result = statement
        .query_map([table], |row| Ok((row.get(0)?, (row.get(1)?, row.get(2)?))))
        .map_err(db_error)?
        .collect::<Result<BTreeMap<_, _>, _>>()
        .map_err(db_error);
    result
}

fn validate_schema(db: &Connection) -> Result<(), BackendError> {
    let reference = Connection::open_in_memory().map_err(db_error)?;
    reference.execute_batch(PRODUCT_SCHEMA).map_err(db_error)?;
    let mut statement = reference
        .prepare("SELECT name FROM sqlite_schema WHERE type='table'")
        .map_err(db_error)?;
    let tables = statement
        .query_map([], |row| row.get::<_, String>(0))
        .map_err(db_error)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(db_error)?;
    for table in tables {
        let kind: Option<String> = db
            .query_row(
                "SELECT type FROM sqlite_schema WHERE name=?1",
                [&table],
                |row| row.get(0),
            )
            .optional()
            .map_err(db_error)?;
        if kind.as_deref() != Some("table") {
            return Err(BackendError::InvalidData);
        }
        let actual = columns(db, &table)?;
        for (column, expected) in columns(&reference, &table)? {
            if actual.get(&column) != Some(&expected) {
                return Err(BackendError::InvalidData);
            }
        }
    }
    let integrity: String = db
        .query_row("PRAGMA quick_check", [], |row| row.get(0))
        .map_err(db_error)?;
    if integrity != "ok" {
        return Err(BackendError::InvalidData);
    }
    Ok(())
}

fn validate_data(db: &Connection) -> Result<(), BackendError> {
    let reference = Connection::open_in_memory().map_err(db_error)?;
    reference.execute_batch(PRODUCT_SCHEMA).map_err(db_error)?;
    let mut statement = reference
        .prepare("SELECT name FROM sqlite_schema WHERE type='table'")
        .map_err(db_error)?;
    let tables = statement
        .query_map([], |row| row.get::<_, String>(0))
        .map_err(db_error)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(db_error)?;
    for table in tables {
        for (column, (kind, _)) in columns(&reference, &table)? {
            if kind == "JSON" {
                // Identifiers come only from the built-in schema.
                let invalid: bool = db.query_row(&format!("SELECT EXISTS(SELECT 1 FROM \"{table}\" WHERE \"{column}\" IS NOT NULL AND NOT json_valid(\"{column}\"))"),[],|row|row.get(0)).map_err(db_error)?;
                if invalid {
                    return Err(BackendError::InvalidData);
                }
            }
        }
    }
    for query in [
        "SELECT EXISTS(SELECT 1 FROM users WHERE enabled NOT IN (0,1) OR role NOT IN ('admin','developer','viewer'))",
        "SELECT EXISTS(SELECT 1 FROM providers p LEFT JOIN provider_revisions r ON r.id=p.revision_id AND r.provider_id=p.id WHERE r.id IS NULL)",
        "SELECT EXISTS(SELECT 1 FROM projects p LEFT JOIN users u ON u.id=p.owner_id WHERE u.id IS NULL)",
    ] {
        if db.query_row(query,[],|row|row.get::<_,bool>(0)).map_err(db_error)? { return Err(BackendError::InvalidData); }
    }
    Ok(())
}

#[cfg(test)]
mod outbox_tests {
    use super::*;

    #[test]
    fn imported_responses_profiles_are_repaired_before_runtime_use() {
        let root = std::env::temp_dir().join(format!("forge-product-model-migration-{}", Uuid::new_v4()));
        fs::create_dir_all(&root).unwrap();
        let path = root.join("product.db");
        let db = Connection::open(&path).unwrap();
        db.execute_batch(PRODUCT_SCHEMA).unwrap();
        db.execute("CREATE TABLE forge_product_meta(id INTEGER PRIMARY KEY CHECK(id=1),owner TEXT NOT NULL,version INTEGER NOT NULL,source_path TEXT,imported_at TEXT NOT NULL)", []).unwrap();
        db.execute("INSERT INTO forge_product_meta VALUES(1,?1,3,NULL,'2026-01-01')", [OWNER]).unwrap();
        db.execute("INSERT INTO providers(id,name,revision_id,enabled) VALUES('p','OpenAI','r',1)", []).unwrap();
        db.execute("INSERT INTO provider_revisions(id,provider_id,config,encrypted_key,created_at) VALUES('r','p',?1,'','2026-01-01')", [json!({"protocol":"openai_responses","base_url":"http://127.0.0.1:1/v1","external":false,"protocol_config":{}}).to_string()]).unwrap();
        db.execute("INSERT INTO model_profiles(id,config,version) VALUES('m',?1,1)", [json!({"enabled":true,"name":"Legacy","provider_id":"p","model_id":"gpt","allowed_parameters":["max_tokens"],"parameters":{"max_tokens":3072},"supports_tools":true}).to_string()]).unwrap();
        drop(db);
        let store = ProductStore::open(&path, None, None).unwrap();
        let config: String = store.connect().unwrap().query_row("SELECT config FROM model_profiles WHERE id='m'", [], |row| row.get(0)).unwrap();
        let config: Value = serde_json::from_str(&config).unwrap();
        assert_eq!(config["parameters"], json!({"max_output_tokens":3072}));
        assert_eq!(config["allowed_parameters"], json!(["max_output_tokens"]));
        let version: i64 = store.connect().unwrap().query_row("SELECT version FROM model_profiles WHERE id='m'", [], |row| row.get(0)).unwrap();
        assert_eq!(version, 2);
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn outbox_delivery_is_retryable_and_idempotent() {
        let root = std::env::temp_dir().join(format!("forge-product-outbox-{}", Uuid::new_v4()));
        let store = ProductStore::open(root.join("product.db"), None, Some("outbox-test-password"))
            .unwrap();
        store
            .connect()
            .unwrap()
            .execute(
                "INSERT INTO outbox(id,run_id,action,payload,sent,error,attempts,created_at) VALUES('m1','run1','answer',?1,0,'',0,'2026-01-01')",
                [serde_json::json!({"text":"hello"}).to_string()],
            )
            .unwrap();
        let pending = store.pending_outbox("run1", 10).unwrap();
        assert_eq!(pending.len(), 1);
        assert_eq!(pending[0].payload["text"], "hello");
        assert!(store.note_outbox_attempt("m1", "temporary").unwrap());
        assert_eq!(store.pending_outbox("run1", 10).unwrap()[0].attempts, 1);
        assert!(store.acknowledge_outbox("m1").unwrap());
        assert!(!store.acknowledge_outbox("m1").unwrap());
        assert!(store.pending_outbox("run1", 10).unwrap().is_empty());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn outbox_rejects_invalid_limits_and_preserves_bad_payload_for_audit() {
        let root = std::env::temp_dir().join(format!("forge-product-outbox-{}", Uuid::new_v4()));
        let store = ProductStore::open(root.join("product.db"), None, Some("outbox-test-password"))
            .unwrap();
        assert!(matches!(
            store.pending_outbox("run1", 0),
            Err(BackendError::InvalidData)
        ));
        store
            .connect()
            .unwrap()
            .execute(
                "INSERT INTO outbox(id,run_id,action,payload,sent,error,attempts,created_at) VALUES('m2','run1','answer','not-json',0,'',0,'2026-01-01')",
                [],
            )
            .unwrap();
        let message = &store.pending_outbox("run1", 1).unwrap()[0];
        assert_eq!(message.payload, serde_json::Value::Null);
        assert!(store.note_outbox_attempt("m2", "invalid payload").unwrap());
        assert!(!store.acknowledge_outbox("missing").unwrap());
        let _ = fs::remove_dir_all(root);
    }
}
