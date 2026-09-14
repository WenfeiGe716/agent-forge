//! Staged desktop workspace preparation. The original workspace is never opened
//! for writing; credentials are returned only on the private stdout protocol.

use crate::ProductStore;
use base64::{engine::general_purpose::URL_SAFE, Engine};
use serde::Deserialize;
use serde_json::{json, Value};
use std::{
    fs,
    io::{Read, Write},
    path::{Component, Path, PathBuf},
};
use uuid::Uuid;

const SECRET_KEYS: [&str; 3] = ["ENCRYPTION_KEY", "ADMIN_PASSWORD", "SANDBOX_TOKEN"];

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DesktopInput {
    pub action: String,
    pub data_dir: PathBuf,
    pub import_from: Option<PathBuf>,
    pub port: Option<u16>,
    pub desktop_token: Option<String>,
    pub control_token: Option<String>,
    pub secrets: Option<Value>,
}

pub fn read_input() -> Result<DesktopInput, &'static str> {
    let mut bytes = Vec::new();
    std::io::stdin()
        .take(262_145)
        .read_to_end(&mut bytes)
        .map_err(|_| "cannot read desktop launch input")?;
    if bytes.len() > 262_144 {
        return Err("desktop launch input is too large");
    }
    serde_json::from_slice(&bytes).map_err(|_| "invalid desktop launch input")
}

fn plain_path(path: &Path) -> Result<PathBuf, &'static str> {
    if !path.is_absolute()
        || path.components().any(|c| matches!(c, Component::ParentDir))
        || fs::symlink_metadata(path).is_ok_and(|m| m.file_type().is_symlink())
    {
        return Err("desktop data path must be an explicit absolute path without symlinks");
    }
    if path.exists() {
        return path
            .canonicalize()
            .map_err(|_| "cannot resolve desktop data path");
    }
    let parent = path.parent().ok_or("invalid desktop data directory")?;
    let parent = parent
        .canonicalize()
        .map_err(|_| "desktop data parent must exist")?;
    Ok(parent.join(path.file_name().ok_or("invalid desktop data directory")?))
}

pub fn private_dir(path: &Path) -> Result<(), &'static str> {
    fs::create_dir_all(path).map_err(|_| "cannot create private data directory")?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))
            .map_err(|_| "cannot secure private data directory")?;
    }
    Ok(())
}

fn write_json(path: &Path, value: &Value) -> Result<(), &'static str> {
    let mut options = fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options
        .open(path)
        .map_err(|_| "cannot create private metadata")?;
    file.write_all(
        serde_json::to_string_pretty(value)
            .map_err(|_| "invalid metadata")?
            .as_bytes(),
    )
    .map_err(|_| "cannot write private metadata")?;
    file.sync_all().map_err(|_| "cannot sync private metadata")
}

fn random_secret() -> String {
    format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple())
}

fn validate_secrets(value: &Value) -> Result<(), &'static str> {
    if SECRET_KEYS.iter().any(|k| {
        !value[*k]
            .as_str()
            .is_some_and(|v| !v.is_empty() && v.len() <= 16_384)
    }) || fernet::Fernet::new(value["ENCRYPTION_KEY"].as_str().unwrap_or("")).is_none()
    {
        return Err("workspace credentials are missing or invalid");
    }
    Ok(())
}

pub fn prepare(input: &DesktopInput) -> Result<Value, &'static str> {
    let destination = plain_path(&input.data_dir)?;
    if destination.exists() {
        return Err("desktop workspace already exists; it will not be overwritten");
    }
    let source = input
        .import_from
        .as_ref()
        .map(|p| plain_path(p))
        .transpose()?;
    if source
        .as_ref()
        .is_some_and(|p| p.starts_with(&destination) || destination.starts_with(p))
    {
        return Err("source and destination workspaces cannot contain each other");
    }
    let staging = destination
        .parent()
        .ok_or("invalid destination")?
        .join(format!(".forge-prepare-{}", Uuid::new_v4().simple()));
    private_dir(&staging)?;
    let result = prepare_into(&staging, source.as_deref());
    if result.is_err() {
        let _ = fs::remove_dir_all(&staging);
    }
    result.map(|(secrets, counts)| json!({"ok":true,"staging_dir":staging,"secrets":secrets,"migrated":source.is_some(),"counts":counts}))
}

fn prepare_into(staging: &Path, source: Option<&Path>) -> Result<(Value, Value), &'static str> {
    let mut key = Vec::new();
    key.extend_from_slice(Uuid::new_v4().as_bytes());
    key.extend_from_slice(Uuid::new_v4().as_bytes());
    let mut secrets = json!({"ENCRYPTION_KEY":URL_SAFE.encode(key),"ADMIN_PASSWORD":random_secret(),"SANDBOX_TOKEN":random_secret()});
    let mut counts = json!({});
    if let Some(source) = source {
        let config_path = source.join(".local/config.json");
        let values: Value = serde_json::from_slice(&read_regular(&config_path)?)
            .map_err(|_| "invalid legacy workspace configuration")?;
        validate_secrets(&values)?;
        secrets = json!({"ENCRYPTION_KEY":values["ENCRYPTION_KEY"],"ADMIN_PASSWORD":values["ADMIN_PASSWORD"],"SANDBOX_TOKEN":values["SANDBOX_TOKEN"]});
        let raw = values["DATABASE_URL"]
            .as_str()
            .ok_or("missing legacy database URL")?;
        let database = raw.strip_prefix("sqlite:///").filter(|p| p.starts_with('/') && !p.contains('?')).ok_or("import a SQLite export first; PostgreSQL is preserved and requires explicit migration")?;
        let db_path = contained(source, Path::new(database))?;
        let data = contained(
            source,
            Path::new(
                values["DATA_ROOT"]
                    .as_str()
                    .ok_or("missing legacy task data")?,
            ),
        )?;
        let imports = contained(
            source,
            Path::new(
                values["IMPORT_ROOT"]
                    .as_str()
                    .ok_or("missing legacy imports")?,
            ),
        )?;
        for tree in [&data, &imports] {
            if db_path.starts_with(tree) || config_path.starts_with(tree) {
                return Err("project data must not contain credential configuration or databases");
            }
        }
        let snapshot = staging.join(".database");
        private_dir(&snapshot)?;
        let db_copy = snapshot.join("legacy.sqlite3");
        let db_bytes = read_regular(&db_path)?;
        write_bytes(&db_copy, &db_bytes)?;
        let wal_path = PathBuf::from(format!("{}-wal", db_path.display()));
        let wal_bytes = if wal_path.exists() {
            Some(read_regular(&wal_path)?)
        } else {
            None
        };
        if PathBuf::from(format!("{}-journal", db_path.display())).exists() {
            return Err(
                "legacy database has an unfinished journal; stop its owner before exporting",
            );
        }
        if let Some(bytes) = &wal_bytes {
            write_bytes(&PathBuf::from(format!("{}-wal", db_copy.display())), bytes)?;
        }
        let check = rusqlite::Connection::open(&db_copy)
            .map_err(|_| "cannot open private database snapshot")?;
        let active: i64 = check.query_row("SELECT count(*) FROM runs WHERE status NOT IN ('completed','cancelled','needs_attention')", [], |r| r.get(0)).map_err(|_| "legacy database is incompatible")?;
        if active != 0 {
            return Err(
                "legacy workspace contains active runs; finish or stop them before importing",
            );
        }
        drop(check);
        let product = ProductStore::open(staging.join("product.sqlite3"), Some(&db_copy), None)
            .map_err(|_| "cannot migrate legacy product database")?
            .with_encryption_key(secrets["ENCRYPTION_KEY"].as_str().unwrap())
            .map_err(|_| "invalid encryption key")?;
        let db = product
            .connect()
            .map_err(|_| "cannot inspect copied product database")?;
        let mut revisions = db
            .prepare("SELECT encrypted_key FROM provider_revisions WHERE encrypted_key!=''")
            .map_err(|_| "cannot inspect copied provider revisions")?;
        for value in revisions
            .query_map([], |r| r.get::<_, String>(0))
            .map_err(|_| "cannot inspect copied credentials")?
        {
            product
                .decrypt_secret(&value.map_err(|_| "invalid provider credential")?)
                .map_err(|_| "encryption key does not match copied credentials")?;
        }
        for table in [
            "agents",
            "providers",
            "model_profiles",
            "projects",
            "runs",
            "skills",
        ] {
            let n: i64 = db
                .query_row(&format!("SELECT count(*) FROM {table}"), [], |r| r.get(0))
                .map_err(|_| "incompatible product data")?;
            counts[table] = n.into();
        }
        copy_private_tree(&data, &staging.join("data"))?;
        copy_private_tree(&imports, &staging.join("imports"))?;
        if read_regular(&db_path)? != db_bytes
            || wal_bytes.as_deref()
                != if wal_path.exists() {
                    Some(read_regular(&wal_path)?)
                } else {
                    None
                }
                .as_deref()
        {
            return Err("legacy database changed while importing; retry after stopping its owner");
        }
        drop(revisions);
        drop(db);
        drop(product);
        fs::remove_dir_all(snapshot).map_err(|_| "cannot finalize private database snapshot")?;
    } else {
        private_dir(&staging.join("data"))?;
        private_dir(&staging.join("imports"))?;
        ProductStore::open(
            staging.join("product.sqlite3"),
            None,
            secrets["ADMIN_PASSWORD"].as_str(),
        )
        .map_err(|_| "cannot initialize Rust product database")?;
    }
    write_json(
        &staging.join("config.json"),
        &json!({"desktop_schema":2,"backend":"rust","migrated":source.is_some()}),
    )?;
    write_json(
        &staging.join("migration.json"),
        &json!({"schema":2,"backend":"rust","migrated":source.is_some(),"counts":counts,"source_untouched":true}),
    )?;
    Ok((secrets, counts))
}

fn contained(root: &Path, value: &Path) -> Result<PathBuf, &'static str> {
    let path = plain_path(value)?;
    if !path.starts_with(root) || path == root {
        Err("legacy assets must be contained inside the selected workspace")
    } else {
        Ok(path)
    }
}

fn read_regular(path: &Path) -> Result<Vec<u8>, &'static str> {
    let before = fs::symlink_metadata(path).map_err(|_| "missing legacy file")?;
    if !before.is_file() || before.file_type().is_symlink() || before.len() > 1024 * 1024 * 1024 {
        return Err("legacy copy requires bounded regular files without links");
    }
    let mut options = fs::OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
        if before.nlink() != 1 {
            return Err("legacy hardlinks cannot be imported");
        }
        options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    }
    let mut file = options.open(path).map_err(|_| "cannot open legacy file")?;
    let opened = file.metadata().map_err(|_| "cannot inspect legacy file")?;
    if !same_file(&before, &opened) {
        return Err("legacy file changed before copying");
    }
    let mut bytes = Vec::new();
    Read::by_ref(&mut file)
        .take(before.len() + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| "cannot read legacy file")?;
    if bytes.len() as u64 != before.len()
        || !same_file(
            &before,
            &file.metadata().map_err(|_| "cannot inspect copied file")?,
        )
        || !same_file(
            &before,
            &fs::symlink_metadata(path).map_err(|_| "legacy file disappeared")?,
        )
    {
        return Err("legacy file changed while copying");
    }
    Ok(bytes)
}

fn same_file(a: &fs::Metadata, b: &fs::Metadata) -> bool {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        a.dev() == b.dev()
            && a.ino() == b.ino()
            && a.len() == b.len()
            && a.mtime_nsec() == b.mtime_nsec()
            && a.mtime() == b.mtime()
            && a.ctime() == b.ctime()
            && a.ctime_nsec() == b.ctime_nsec()
            && b.nlink() == 1
    }
    #[cfg(not(unix))]
    {
        a.len() == b.len() && a.modified().ok() == b.modified().ok()
    }
}

fn write_bytes(path: &Path, bytes: &[u8]) -> Result<(), &'static str> {
    let mut options = fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options.open(path).map_err(|_| "cannot copy private file")?;
    file.write_all(bytes)
        .map_err(|_| "cannot copy private file")?;
    file.sync_all().map_err(|_| "cannot sync private file")
}

fn copy_private_tree(source: &Path, dest: &Path) -> Result<(), &'static str> {
    let info = fs::symlink_metadata(source).map_err(|_| "missing legacy directory")?;
    if !info.is_dir() || info.file_type().is_symlink() {
        return Err("legacy directory symlinks cannot be imported");
    }
    let original_path = source
        .canonicalize()
        .map_err(|_| "cannot resolve legacy directory")?;
    private_dir(dest)?;
    for entry in fs::read_dir(source).map_err(|_| "cannot read legacy directory")? {
        let entry = entry.map_err(|_| "cannot read legacy directory")?;
        let target = dest.join(entry.file_name());
        let metadata = entry
            .file_type()
            .map_err(|_| "cannot inspect legacy entry")?;
        if metadata.is_dir() {
            copy_private_tree(&entry.path(), &target)?;
        } else if metadata.is_file() {
            write_bytes(&target, &read_regular(&entry.path())?)?;
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                let mode = fs::symlink_metadata(entry.path())
                    .map_err(|_| "legacy file disappeared")?
                    .permissions()
                    .mode();
                if mode & 0o111 != 0 {
                    fs::set_permissions(&target, fs::Permissions::from_mode(0o700))
                        .map_err(|_| "cannot preserve executable file")?;
                }
            }
        } else {
            return Err("legacy copy cannot include symlinks or special files");
        }
    }
    let after = fs::symlink_metadata(source).map_err(|_| "legacy directory disappeared")?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if info.dev() != after.dev()
            || info.ino() != after.ino()
            || info.mtime() != after.mtime()
            || info.mtime_nsec() != after.mtime_nsec()
        {
            return Err("legacy directory changed during import");
        }
    }
    if !after.is_dir()
        || after.file_type().is_symlink()
        || source.canonicalize().ok().as_ref() != Some(&original_path)
    {
        return Err("legacy directory changed during import");
    }
    Ok(())
}

pub fn serve_data(input: &DesktopInput) -> Result<(PathBuf, Value), &'static str> {
    let root = plain_path(&input.data_dir)?;
    let sentinel = root.join("secrets.enc");
    if !fs::symlink_metadata(sentinel).is_ok_and(|m| m.is_file() && !m.file_type().is_symlink()) {
        return Err("workspace keychain protection is incomplete");
    }
    let secrets = input
        .secrets
        .as_ref()
        .ok_or("missing desktop credentials")?;
    validate_secrets(secrets)?;
    if input.port.is_none_or(|p| p < 1024)
        || [&input.desktop_token, &input.control_token]
            .iter()
            .any(|v| !v.as_ref().is_some_and(|v| v.len() >= 32 && v.len() <= 4096))
    {
        return Err("invalid desktop process identity or port");
    }
    Ok((root, secrets.clone()))
}
