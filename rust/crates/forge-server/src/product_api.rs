//! Native product configuration API. All mutations share one immediate SQLite
//! transaction with their audit event; runtime state lives in the kernel store.
use crate::product_store::ProductStore;
use crate::{BackendError, User};
use base64::Engine;
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};
use uuid::Uuid;

mod catalog;
mod commands;
mod design;
mod execution;
mod network;
mod organization;
mod policies;
pub(crate) mod protocols;
mod skill_store;
mod skills;
pub(crate) mod team;
pub(crate) mod workspace;

#[derive(Debug)]
pub struct ProductReply {
    pub status: u16,
    pub body: Value,
    pub download: Option<ProductDownload>,
}

#[derive(Debug)]
pub struct ProductDownload {
    pub bytes: Vec<u8>,
    pub filename: String,
    pub content_type: &'static str,
}

impl ProductReply {
    pub fn json(status: u16, body: Value) -> Self {
        Self {
            status,
            body,
            download: None,
        }
    }
}

#[derive(Debug)]
pub struct ProductError {
    pub status: u16,
    pub detail: String,
}
impl From<rusqlite::Error> for ProductError {
    fn from(error: rusqlite::Error) -> Self {
        let conflict = matches!(&error, rusqlite::Error::SqliteFailure(e, _) if e.code == rusqlite::ErrorCode::ConstraintViolation);
        Self {
            status: if conflict { 409 } else { 503 },
            detail: if conflict {
                "Configuration conflicts with existing data"
            } else {
                "Product database unavailable"
            }
            .into(),
        }
    }
}
impl From<BackendError> for ProductError {
    fn from(error: BackendError) -> Self {
        Self {
            status: match error {
                BackendError::NotFound => 404,
                BackendError::Forbidden => 403,
                BackendError::Conflict
                | BackendError::Unsupported
                | BackendError::UnsupportedConfiguration(_) => 409,
                BackendError::InvalidData => 422,
                _ => 503,
            },
            detail: error.to_string(),
        }
    }
}
pub(super) type Result<T> = std::result::Result<T, ProductError>;
pub(super) fn bad(message: impl Into<String>) -> ProductError {
    ProductError {
        status: 422,
        detail: message.into(),
    }
}
pub(super) fn missing() -> ProductError {
    ProductError {
        status: 404,
        detail: "Resource not found".into(),
    }
}
pub(super) fn conflict() -> ProductError {
    ProductError {
        status: 409,
        detail: "Configuration version or digest changed; refresh before retrying".into(),
    }
}
pub(super) fn uid() -> String {
    Uuid::new_v4().simple().to_string()
}
pub(super) fn now(c: &Connection) -> Result<String> {
    Ok(c.query_row(
        "SELECT strftime('%Y-%m-%dT%H:%M:%f+00:00','now')",
        [],
        |r| r.get(0),
    )?)
}
pub(super) fn digest(v: &Value) -> String {
    format!(
        "{:x}",
        Sha256::digest(serde_json::to_vec(v).expect("JSON serializes"))
    )
}
pub(super) fn object(v: &Value) -> Result<&Map<String, Value>> {
    v.as_object().ok_or_else(|| bad("Expected JSON object"))
}
pub(super) fn string<'a>(v: &'a Value, k: &str, min: usize, max: usize) -> Result<&'a str> {
    let s = v[k]
        .as_str()
        .ok_or_else(|| bad(format!("{k} must be a string")))?;
    if s.chars().count() < min || s.chars().count() > max || s.contains('\0') {
        return Err(bad(format!("Invalid {k} length")));
    }
    Ok(s)
}
pub(super) fn fields(v: &Value, allowed: &[&str]) -> Result<()> {
    if object(v)?.keys().any(|k| !allowed.contains(&k.as_str())) {
        return Err(bad("Unknown configuration field"));
    }
    Ok(())
}
pub(super) fn defaults(v: &Value, d: Value) -> Result<Value> {
    let mut output = object(&d)?.clone();
    output.extend(object(v)?.clone());
    Ok(Value::Object(output))
}
pub(super) fn encode(v: &Value) -> String {
    v.to_string()
}
pub(super) fn decode(v: String) -> rusqlite::Result<Value> {
    serde_json::from_str(&v).map_err(|e| {
        rusqlite::Error::FromSqlConversionFailure(0, rusqlite::types::Type::Text, Box::new(e))
    })
}
pub(super) fn emit(c: &Connection, u: &User, kind: &str, mut data: Value) -> Result<()> {
    data["actor"] = json!(u.id);
    c.execute(
        "INSERT INTO events(run_id,kind,data,created_at) VALUES(NULL,?1,?2,?3)",
        params![kind, encode(&data), now(c)?],
    )?;
    Ok(())
}
pub(super) fn config(c: &Connection, id: &str, default: Value) -> Result<Value> {
    Ok(
        c.query_row("SELECT value FROM platform_config WHERE id=?1", [id], |r| {
            decode(r.get(0)?)
        })
        .optional()?
        .unwrap_or(default),
    )
}
pub(super) fn put_config(c: &Connection, id: &str, v: &Value) -> Result<()> {
    c.execute("INSERT INTO platform_config(id,value) VALUES(?1,?2) ON CONFLICT(id) DO UPDATE SET value=excluded.value",params![id,encode(v)])?;
    Ok(())
}

#[derive(Clone, Copy)]
pub(super) enum ConfigTable {
    Model,
    Agent,
    Orchestration,
}
impl ConfigTable {
    fn table(self) -> &'static str {
        match self {
            Self::Model => "model_profiles",
            Self::Agent => "agents",
            Self::Orchestration => "orchestration_templates",
        }
    }
    fn kind(self) -> &'static str {
        match self {
            Self::Model => "model",
            Self::Agent => "agent",
            Self::Orchestration => "orchestration",
        }
    }
}
pub(super) fn configs(c: &Connection, table: ConfigTable) -> Result<Vec<Value>> {
    let mut q = c.prepare(&format!(
        "SELECT id,version,config FROM {} ORDER BY id",
        table.table()
    ))?;
    let rows = q.query_map([], |r| {
        let mut v = decode(r.get(2)?)?;
        v["id"] = r.get::<_, String>(0)?.into();
        v["version"] = r.get::<_, u64>(1)?.into();
        Ok(v)
    })?;
    Ok(rows.collect::<std::result::Result<_, _>>()?)
}
pub(super) fn get_config(c: &Connection, table: ConfigTable, id: &str) -> Result<Value> {
    c.query_row(
        &format!("SELECT config,version FROM {} WHERE id=?1", table.table()),
        [id],
        |r| {
            let mut v = decode(r.get(0)?)?;
            v["id"] = json!(id);
            v["version"] = r.get::<_, u64>(1)?.into();
            Ok(v)
        },
    )
    .optional()?
    .ok_or_else(missing)
}
pub(super) fn save_config(
    c: &Connection,
    u: &User,
    table: ConfigTable,
    id: &str,
    mut v: Value,
    previous: Option<u64>,
) -> Result<Value> {
    object(&v)?;
    v.as_object_mut().unwrap().remove("id");
    v.as_object_mut().unwrap().remove("version");
    v.as_object_mut().unwrap().remove("expected_version");
    let version = previous.unwrap_or(0) + 1;
    if matches!(table, ConfigTable::Model) && previous.is_some() {
        c.execute("INSERT OR IGNORE INTO model_asset_revisions(id,model_id,version,config,actor,created_at) SELECT id||':'||version,id,version,config,?1,?2 FROM model_profiles WHERE id=?3",params![u.id,now(c)?,id])?;
    }
    let n = if let Some(old) = previous {
        c.execute(
            &format!(
                "UPDATE {} SET config=?1,version=?2 WHERE id=?3 AND version=?4",
                table.table()
            ),
            params![encode(&v), version, id, old],
        )?
    } else {
        c.execute(
            &format!(
                "INSERT INTO {}(id,config,version) VALUES(?1,?2,1)",
                table.table()
            ),
            params![id, encode(&v)],
        )?
    };
    if n != 1 {
        return Err(conflict());
    }
    if matches!(table, ConfigTable::Model) {
        c.execute("INSERT OR IGNORE INTO model_asset_revisions(id,model_id,version,config,actor,created_at) VALUES(?1,?2,?3,?4,?5,?6)",params![format!("{id}:{version}"),id,version,encode(&v),u.id,now(c)?])?;
    }
    emit(
        c,
        u,
        &format!("{}.updated", table.kind()),
        json!({"id":id,"version":version}),
    )?;
    v["id"] = json!(id);
    v["version"] = json!(version);
    Ok(v)
}

pub struct ProductApi {
    store: std::sync::Arc<ProductStore>,
    data_root: PathBuf,
    import_root: PathBuf,
    directories: Option<std::sync::Arc<crate::desktop::DirectoryTickets>>,
    runtime: Option<execution::RuntimeView>,
}

/// Copy an uploaded run attachment into the run's canonical workspace and
/// into any member workspaces that are already active.  Runtime agents are
/// sandboxed to those directories, so returning a workspace-relative path is
/// both useful to the model and safer than exposing the product data root.
fn materialize_run_attachment(
    data_root: &Path,
    run_id: &str,
    attachment_id: &str,
    filename: &str,
    bytes: &[u8],
) -> Result<String> {
    let run_root = data_root.join("runs").join(run_id);
    let canonical = run_root.join("workspace");
    let mut workspaces = vec![canonical];
    let members = run_root.join("members");
    if let Ok(entries) = fs::read_dir(&members) {
        for entry in entries.flatten() {
            let workspace = entry.path().join("workspace");
            if workspace.is_dir() {
                workspaces.push(workspace);
            }
        }
    }
    let relative_path = format!(".forge-attachments/{attachment_id}-{filename}");
    for workspace in workspaces {
        // Never follow a redirected workspace (including an ancestor
        // symlink); attachments are user-provided bytes and must stay inside
        // this run's storage root.
        for parent in workspace.ancestors() {
            if !parent.starts_with(&run_root) {
                break;
            }
            if let Ok(meta) = fs::symlink_metadata(parent) {
                if meta.file_type().is_symlink() {
                    return Err(ProductError {
                        status: 409,
                        detail: "Attachment workspace is redirected".into(),
                    });
                }
            }
        }
        let attachments = workspace.join(".forge-attachments");
        if let Ok(meta) = fs::symlink_metadata(&attachments) {
            if meta.file_type().is_symlink() {
                return Err(ProductError {
                    status: 409,
                    detail: "Attachment workspace is redirected".into(),
                });
            }
        }
        fs::create_dir_all(&attachments).map_err(|_| ProductError {
            status: 503,
            detail: "Attachment workspace unavailable".into(),
        })?;
        let target = workspace.join(&relative_path);
        let mut output = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&target)
            .map_err(|_| ProductError {
                status: 503,
                detail: "Attachment workspace unavailable".into(),
            })?;
        use std::io::Write;
        output.write_all(bytes).map_err(|_| ProductError {
            status: 503,
            detail: "Attachment workspace unavailable".into(),
        })?;
    }
    Ok(relative_path)
}

impl ProductApi {
    pub fn new(
        store: std::sync::Arc<ProductStore>,
        data_root: impl Into<PathBuf>,
        import_root: impl Into<PathBuf>,
    ) -> Self {
        Self {
            store,
            data_root: data_root.into(),
            import_root: import_root.into(),
            directories: None,
            runtime: None,
        }
    }
    pub fn with_directories(
        mut self,
        directories: std::sync::Arc<crate::desktop::DirectoryTickets>,
    ) -> Self {
        self.directories = Some(directories);
        self
    }
    pub fn request(
        &self,
        user: &User,
        method: &str,
        path: &str,
        query: &BTreeMap<String, String>,
        body: &[u8],
    ) -> Result<ProductReply> {
        if let Some(result) = self.network_request(user, method, path, query, body) {
            return result;
        }
        let mut c = self.store.connect()?;
        let tx =
            c.transaction_with_behavior(if method == "GET" && path != "/api/office/state" {
                TransactionBehavior::Deferred
            } else {
                TransactionBehavior::Immediate
            })?;
        let current = tx
            .query_row(
                "SELECT id,username,role,enabled FROM users WHERE id=?1",
                [&user.id],
                |r| {
                    Ok(User {
                        id: r.get(0)?,
                        username: r.get(1)?,
                        role: r.get(2)?,
                        enabled: r.get(3)?,
                    })
                },
            )
            .optional()?
            .filter(|u| u.enabled)
            .ok_or_else(|| ProductError {
                status: 401,
                detail: "Session account is unavailable".into(),
            })?;
        let parts: Vec<_> = path.trim_matches('/').split('/').collect();
        if parts.first() != Some(&"api") {
            return Err(missing());
        }
        let team_message_post = method == "POST"
            && parts.len() == 4
            && parts[0] == "api"
            && parts[1] == "runs"
            && parts[3] == "messages";
        let team_attachment_post = method == "POST"
            && parts.len() == 4
            && parts[0] == "api"
            && parts[1] == "runs"
            && parts[3] == "attachments";
        if method != "GET"
            && current.role != "admin"
            && !(["/api/execution/preflight", "/api/commands/resolve"].contains(&path)
                && current.role == "developer")
            && !(team_message_post && current.role == "developer")
            && !(team_attachment_post && current.role == "developer")
        {
            return Err(ProductError {
                status: 403,
                detail: "Administrator required".into(),
            });
        }
        let binary = path == "/api/skills/import" || path.ends_with("/archive");
        let input = if body.is_empty() || binary {
            json!({})
        } else {
            serde_json::from_slice(body).map_err(|_| bad("Invalid JSON body"))?
        };
        let result = self.dispatch(&tx, &current, method, &parts[1..], query, &input, body)?;
        tx.commit()?;
        Ok(result)
    }
    #[allow(clippy::too_many_arguments)]
    fn dispatch(
        &self,
        c: &Connection,
        u: &User,
        m: &str,
        p: &[&str],
        q: &BTreeMap<String, String>,
        b: &Value,
        raw: &[u8],
    ) -> Result<ProductReply> {
        let (status, value) = match (m, p) {
            ("POST", ["runs", run_id, "messages"]) => {
                // Cookie users may send a message only through the explicit
                // recipient/body contract; attempts to impersonate a runtime
                // actor are rejected before run lookup to avoid an oracle.
                if b.get("sender_id").is_some() || b.get("agent_id").is_some() {
                    return Err(ProductError {
                        status: 403,
                        detail: "Runtime actor identity cannot be supplied by HTTP clients".into(),
                    });
                }
                let run = self.authorize_run(c, u, run_id)?;
                let snapshot = c
                    .query_row(
                        "SELECT snapshot FROM forge_team_runs WHERE run_id=?1",
                        [run_id],
                        |r| decode(r.get(0)?),
                    )
                    .optional()?
                    .unwrap_or_else(|| run["snapshot"].clone());
                fields(b, &["recipient_id", "body"])?;
                let recipient = string(b, "recipient_id", 1, 64)?;
                let body = string(b, "body", 1, 20_000)?;
                (
                    202,
                    team::send_user_message(c, &snapshot, u, run_id, recipient, body)?,
                )
            }
            ("POST", ["runs", run_id, "attachments"]) => {
                let run = self.authorize_run(c, u, run_id)?;
                let snapshot = c
                    .query_row(
                        "SELECT snapshot FROM forge_team_runs WHERE run_id=?1",
                        [run_id],
                        |r| decode(r.get(0)?),
                    )
                    .optional()?
                    .unwrap_or_else(|| run["snapshot"].clone());
                fields(b, &["filename", "content_base64", "recipient_id"])?;
                let filename = string(b, "filename", 1, 180)?;
                let recipient = b
                    .get("recipient_id")
                    .and_then(Value::as_str)
                    .filter(|value| !value.is_empty());
                if filename.contains(['/', '\\'])
                    || filename == "."
                    || filename == ".."
                    || filename.starts_with('.')
                {
                    return Err(bad("Invalid attachment filename"));
                }
                let encoded = string(b, "content_base64", 1, 30_000_000)?;
                let bytes = base64::engine::general_purpose::STANDARD
                    .decode(encoded)
                    .map_err(|_| bad("Invalid attachment encoding"))?;
                if bytes.is_empty() || bytes.len() > 20_000_000 {
                    return Err(bad("Attachment exceeds the 20 MB limit"));
                }
                team::require_active(c, &snapshot, run_id)?;
                if let Some(recipient) = recipient {
                    if !team::members(&snapshot)?.contains_key(recipient) {
                        return Err(ProductError {
                            status: 404,
                            detail: "Attachment recipient is not in this run".into(),
                        });
                    }
                }
                team::quota(c, &snapshot, run_id, true)?;
                let dir = self.data_root.join("runs").join(run_id).join("messages");
                fs::create_dir_all(&dir).map_err(|_| ProductError {
                    status: 503,
                    detail: "Attachment storage unavailable".into(),
                })?;
                let id = uid();
                let path = dir.join(format!("{id}-{filename}"));
                fs::write(&path, &bytes).map_err(|_| ProductError {
                    status: 503,
                    detail: "Attachment storage unavailable".into(),
                })?;
                let relative_path =
                    materialize_run_attachment(&self.data_root, run_id, &id, filename, &bytes)?;
                let digest = format!("{:x}", Sha256::digest(&bytes));
                let value = json!({"attachment_id":id,"run_id":run_id,"filename":filename,"size":bytes.len(),"sha256":digest,"workspace_path":relative_path,"recipient_id":recipient});
                emit(c, u, "team.attachment.uploaded", value.clone())?;
                (201, value)
            }
            (_, ["runs", _, "team" | "tasks" | "messages", ..]) => {
                return team::http_request(self, c, u, m, p, q);
            }
            ("POST", ["commands", "resolve"]) => (200, commands::resolve(c, b)?),
            ("GET", ["users"]) => {
                if u.role != "admin" {
                    return Err(ProductError {
                        status: 403,
                        detail: "Administrator required".into(),
                    });
                }
                let mut s =
                    c.prepare("SELECT id,username,role,enabled FROM users ORDER BY username")?;
                let rows=s.query_map([],|r|Ok(json!({"id":r.get::<_,String>(0)?,"username":r.get::<_,String>(1)?,"role":r.get::<_,String>(2)?,"enabled":r.get::<_,bool>(3)?})))?;
                (
                    200,
                    Value::Array(rows.collect::<std::result::Result<_, _>>()?),
                )
            }
            ("POST", ["users"]) => {
                fields(b, &["username", "password", "role"])?;
                let name = string(b, "username", 2, 80)?;
                if !name
                    .bytes()
                    .all(|x| x.is_ascii_alphanumeric() || b"_.-".contains(&x))
                {
                    return Err(bad("Invalid username"));
                }
                let password = string(b, "password", 12, 512)?;
                let role = b["role"].as_str().unwrap_or("developer");
                if !["admin", "developer", "viewer"].contains(&role) {
                    return Err(bad("Invalid role"));
                }
                let id = uid();
                let hash = ProductStore::hash_password(password)?;
                c.execute("INSERT INTO users(id,username,password_hash,role,enabled) VALUES(?1,?2,?3,?4,1)",params![id,name,hash,role])?;
                emit(c, u, "user.created", json!({"user_id":id,"role":role}))?;
                (
                    201,
                    json!({"id":id,"username":name,"role":role,"enabled":true}),
                )
            }
            ("POST", ["users", id, "disable"]) => {
                if *id == u.id {
                    return Err(conflict());
                }
                if c.execute("UPDATE users SET enabled=0 WHERE id=?1", [id])? != 1 {
                    return Err(missing());
                }
                c.execute("DELETE FROM sessions WHERE user_id=?1", [id])?;
                emit(c, u, "user.disabled", json!({"user_id":id}))?;
                (200, json!({"ok":true}))
            }
            ("GET", ["providers"]) => (200, self.providers(c)?),
            ("POST", ["providers"]) => (201, self.save_provider(c, u, None, b)?),
            ("PUT", ["providers", id]) => (200, self.save_provider(c, u, Some(id), b)?),
            ("GET", [kind @ ("models" | "agents" | "orchestrations")]) => {
                (200, json!(configs(c, table(kind))?))
            }
            ("POST", [kind @ ("models" | "agents" | "orchestrations")]) => {
                (201, self.save_asset(c, u, table(kind), None, b)?)
            }
            ("PUT", [kind @ ("models" | "agents" | "orchestrations"), id]) => {
                (200, self.save_asset(c, u, table(kind), Some(id), b)?)
            }
            ("GET", ["projects"]) => (200, self.projects(c, u)?),
            ("POST", ["projects"]) => (201, self.save_project(c, u, None, b)?),
            ("PUT", ["projects", id]) => (200, self.save_project(c, u, Some(id), b)?),
            ("GET", ["settings"]) => (
                200,
                json!({"runtime":config(c,"runtime",runtime_defaults())?,"roles":config(c,"roles",json!({"planner":{"max_steps":8},"developer":{},"tester":{},"reviewer":{"max_steps":12}}))?,"validation_mode":false,"orchestrator":"rust","installation":{"max_active_runs":4,"max_active_activities":12,"max_project_mb":100},"dispatcher":"running"}),
            ),
            ("PUT", ["settings"]) => {
                fields(b, &["runtime", "roles"])?;
                if let Some(r) = b.get("runtime") {
                    validate_runtime(r, false)?;
                    put_config(c, "runtime", &defaults(r, runtime_defaults())?)?
                }
                if let Some(r) = b.get("roles") {
                    fields(r, &["planner", "developer", "tester", "reviewer"])?;
                    for v in object(r)?.values() {
                        validate_runtime(v, true)?
                    }
                    put_config(c, "roles", r)?
                }
                emit(
                    c,
                    u,
                    "settings.updated",
                    json!({"groups":object(b)?.keys().collect::<Vec<_>>()}),
                )?;
                (200, json!({"ok":true}))
            }
            ("GET", ["office", "settings"]) => {
                (200, config(c, "office_visual", office_defaults())?)
            }
            ("PUT", ["office", "settings"]) => {
                fields(
                    b,
                    &[
                        "idle_enabled",
                        "idle_grace_s",
                        "idle_stagger_s",
                        "idle_cooldown_s",
                        "walk_speed",
                        "activities",
                        "durations_s",
                    ],
                )?;
                let old = config(c, "office_visual", office_defaults())?;
                let mut v = defaults(b, old.clone())?;
                if !v["idle_enabled"].is_boolean() {
                    return Err(bad("idle_enabled must be boolean"));
                }
                for (k, l, h) in [
                    ("idle_grace_s", 0., 3600.),
                    ("idle_stagger_s", 0., 600.),
                    ("idle_cooldown_s", 0., 3600.),
                    ("walk_speed", 0.3, 5.),
                ] {
                    number(&v, k, l, h)?;
                }
                let allowed = ["drink", "phone", "restroom", "nap", "stretch"];
                let actions = strings(&v, "activities", 5)?;
                if actions.iter().any(|x| !allowed.contains(&x.as_str())) {
                    return Err(bad("Unknown office activity"));
                }
                fields(&v["durations_s"], &allowed)?;
                for k in allowed {
                    number(&v["durations_s"], k, 2., 300.)?;
                }
                v["revision"] = json!(old["revision"].as_u64().unwrap_or(0) + 1);
                put_config(c, "office_visual", &v)?;
                emit(
                    c,
                    u,
                    "office.settings.updated",
                    json!({"revision":v["revision"]}),
                )?;
                (200, v)
            }
            ("GET", ["organization"]) => (
                200,
                config(
                    c,
                    "organization_layout",
                    json!({"ref":null,"config":{"positions":{},"floors":[],"description":""},"applied_at":null}),
                )?,
            ),
            ("GET", ["design", "enterprise-policy"]) => {
                (200, config(c, "enterprise_policy", json!({"ref":null}))?)
            }
            ("PUT", ["design", "enterprise-policy"]) => {
                fields(b, &["ref"])?;
                if !b["ref"].is_null() {
                    let r = design::resolve(c, &b["ref"])?;
                    if r["kind"] != "routing" {
                        return Err(bad("Enterprise policy must reference published routing"));
                    }
                }
                let v = json!({"ref":b["ref"],"actor":u.id});
                put_config(c, "enterprise_policy", &v)?;
                emit(c, u, "enterprise.policy.selected", v.clone())?;
                (200, v)
            }
            ("GET", ["setup", "status"]) => (200, self.setup_status(c, u)?),
            ("GET", ["audit"]) => {
                if u.role != "admin" {
                    return Err(ProductError {
                        status: 403,
                        detail: "Administrator required".into(),
                    });
                }
                let mut s=c.prepare("SELECT id,kind,data,created_at FROM events WHERE run_id IS NULL ORDER BY id DESC LIMIT 500")?;
                let rows=s.query_map([],|r|Ok(json!({"id":r.get::<_,i64>(0)?,"run_id":null,"kind":r.get::<_,String>(1)?,"data":decode(r.get(2)?)?,"created_at":r.get::<_,String>(3)?})))?;
                (
                    200,
                    json!(rows.collect::<std::result::Result<Vec<_>, _>>()?),
                )
            }
            (_, ["design", "assets", ..]) => return design::request(c, u, m, p, q, b),
            (_, ["skills", ..]) => return skills::request(c, u, m, p, q, b, raw),
            (_, ["skill-store", ..]) | (_, ["role-templates", ..]) => {
                return skill_store::request(c, u, m, p, q, b, raw)
            }
            (_, ["providers", _, "catalog", ..])
            | (_, ["providers", _, "models", "bulk-state", ..])
            | (_, ["model-hub", ..]) => return catalog::request(c, u, m, p, b),
            (_, ["execution", "policies", ..]) => return policies::request(c, u, m, p, q, b),
            (_, ["execution", ..])
            | (_, ["tool-installations", ..])
            | (_, ["tool-packages"])
            | (_, ["office", "state"]) => return self.execution_request(c, u, m, p, q, b),
            _ => return Err(missing()),
        };
        Ok(ProductReply::json(status, value))
    }
    fn providers(&self, c: &Connection) -> Result<Value> {
        let mut q=c.prepare("SELECT p.id,p.name,p.enabled,p.revision_id,r.config,r.encrypted_key FROM providers p JOIN provider_revisions r ON r.id=p.revision_id ORDER BY p.id")?;
        let rows = q.query_map([], |r| {
            let mut v = decode(r.get(4)?)?;
            v["id"] = r.get::<_, String>(0)?.into();
            v["name"] = r.get::<_, String>(1)?.into();
            v["enabled"] = r.get::<_, bool>(2)?.into();
            v["revision_id"] = r.get::<_, String>(3)?.into();
            v["has_key"] = json!(!r.get::<_, String>(5)?.is_empty());
            Ok(v)
        })?;
        Ok(json!(rows.collect::<std::result::Result<Vec<_>, _>>()?))
    }
    fn save_provider(
        &self,
        c: &Connection,
        u: &User,
        id: Option<&str>,
        b: &Value,
    ) -> Result<Value> {
        fields(
            b,
            &[
                "name",
                "protocol",
                "protocol_config",
                "base_url",
                "api_key",
                "external",
                "enabled",
            ],
        )?;
        let name = string(b, "name", 1, 100)?;
        let protocol = string(b, "protocol", 1, 50)?;
        if ![
            "openai_chat",
            "openai_responses",
            "anthropic_messages",
            "google_gemini",
            "custom_http",
        ]
        .contains(&protocol)
        {
            return Err(bad("Unknown provider protocol"));
        }
        let raw = string(b, "base_url", 1, 1500)?;
        let url = url::Url::parse(raw).map_err(|_| bad("Invalid provider URL"))?;
        let external = bool_or(b, "external", true)?;
        if !["http", "https"].contains(&url.scheme())
            || url.host_str().is_none()
            || !url.username().is_empty()
            || url.password().is_some()
            || url.query().is_some()
            || url.fragment().is_some()
        {
            return Err(bad(
                "Provider URL must use HTTP(S), without credentials, query, or fragment",
            ));
        }
        if external && url.scheme() != "https" {
            return Err(bad("External provider requires HTTPS"));
        }
        let prior = if let Some(id) = id {
            Some(c.query_row("SELECT r.config,r.encrypted_key FROM providers p JOIN provider_revisions r ON r.id=p.revision_id WHERE p.id=?1",[id],|r|Ok((decode(r.get(0)?)?,r.get::<_,String>(1)?))).optional()?.ok_or_else(missing)?)
        } else {
            None
        };
        let mut pc = b.get("protocol_config").cloned().unwrap_or(json!({}));
        object(&pc)?;
        if protocol != "custom_http" {
            // Native protocols may opt into bounded, non-secret static
            // headers. Credentials and auth headers are always managed by
            // the selected protocol adapter.
            fields(&pc, &["headers"])?;
            protocols::static_headers(&pc)?;
        }
        if protocol == "custom_http" {
            pc = protocols::normalize(&pc)?;
            protocols::validate(&pc)?;
        }
        if let Some((old, _)) = &prior {
            if old["protocol"] != protocol || old["protocol_config"] != pc {
                for model in configs(c, ConfigTable::Model)? {
                    if model["provider_id"] == id.unwrap_or("") {
                        let checked = if protocol == "custom_http" {
                            protocols::parameters(
                                protocol,
                                &model["allowed_parameters"],
                                &model["parameters"],
                                &pc,
                            )
                        } else {
                            validate_parameters(
                                protocol,
                                &model["allowed_parameters"],
                                &model["parameters"],
                            )
                        };
                        if let Err(error) = checked {
                            return Err(ProductError{status:error.status,detail:format!("Model '{}' ({}) profile {} is incompatible with the new provider protocol: {}",model["name"].as_str().unwrap_or("model"),model["model_id"].as_str().unwrap_or(""),model["id"].as_str().unwrap_or(""),error.detail)});
                        }
                    }
                }
            }
        }
        let key = match b.get("api_key") {
            Some(Value::String(v)) => self.store.encrypt_secret(v)?,
            Some(Value::Null) | None => prior.map(|x| x.1).unwrap_or_default(),
            _ => return Err(bad("api_key must be a string or null")),
        };
        let pid = id.map(str::to_owned).unwrap_or_else(uid);
        let rid = uid();
        let value = json!({"protocol":protocol,"base_url":raw.trim_end_matches('/'),"external":external,"protocol_config":pc});
        c.execute("INSERT INTO provider_revisions(id,provider_id,config,encrypted_key,created_at) VALUES(?1,?2,?3,?4,?5)",params![rid,pid,encode(&value),key,now(c)?])?;
        c.execute("INSERT INTO providers(id,name,revision_id,enabled) VALUES(?1,?2,?3,?4) ON CONFLICT(id) DO UPDATE SET name=excluded.name,revision_id=excluded.revision_id,enabled=excluded.enabled",params![pid,name,rid,bool_or(b,"enabled",true)?])?;
        emit(
            c,
            u,
            "provider.updated",
            json!({"provider_id":pid,"revision_id":rid}),
        )?;
        Ok(json!({"id":pid,"revision_id":rid}))
    }
    fn save_asset(
        &self,
        c: &Connection,
        u: &User,
        t: ConfigTable,
        id: Option<&str>,
        b: &Value,
    ) -> Result<Value> {
        let prior = id.map(|id| get_config(c, t, id)).transpose()?;
        let value = match t {
            ConfigTable::Model => validate_model(c, b)?,
            ConfigTable::Agent => validate_agent(c, id, b, prior.as_ref())?,
            ConfigTable::Orchestration => {
                fields(
                    b,
                    &[
                        "name",
                        "enabled",
                        "graph",
                        "expected_version",
                        "description",
                    ],
                )?;
                string(b, "name", 1, 120)?;
                validate_graph(c, &b["graph"])?;
                if prior
                    .as_ref()
                    .is_some_and(|v| v["version"] != b["expected_version"])
                {
                    return Err(conflict());
                }
                defaults(b, json!({"enabled":true,"description":""}))?
            }
        };
        save_config(
            c,
            u,
            t,
            &id.map(str::to_owned).unwrap_or_else(uid),
            value,
            prior.as_ref().and_then(|v| v["version"].as_u64()),
        )
    }
    fn projects(&self, c: &Connection, u: &User) -> Result<Value> {
        let mut q=c.prepare("SELECT id,owner_id,config,base_commit,created_at FROM projects ORDER BY created_at DESC")?;
        let rows = q.query_map([], |r| {
            let mut v = decode(r.get(2)?)?;
            v["id"] = r.get::<_, String>(0)?.into();
            v["owner_id"] = r.get::<_, String>(1)?.into();
            v["base_commit"] = r.get::<_, String>(3)?.into();
            v["created_at"] = r.get::<_, String>(4)?.into();
            Ok(v)
        })?;
        Ok(json!(rows
            .collect::<std::result::Result<Vec<Value>, _>>()?
            .into_iter()
            .filter(|p| visible(p, u))
            .collect::<Vec<_>>()))
    }
    fn save_project(&self, c: &Connection, u: &User, id: Option<&str>, b: &Value) -> Result<Value> {
        let mut v = validate_project(c, b)?;
        let pid = id.map(str::to_owned).unwrap_or_else(uid);
        let (base, created, owner) = if let Some(id) = id {
            let old = self
                .projects(c, u)?
                .as_array()
                .unwrap()
                .iter()
                .find(|x| x["id"] == id)
                .cloned()
                .ok_or_else(missing)?;
            if old["import_path"] != v["import_path"] {
                return Err(bad("Import path is immutable; import as a new project"));
            }
            for (k, legacy) in [
                ("execution_mode", json!("container")),
                ("permission_profile", json!("ask")),
                ("allow_external_models", json!(false)),
            ] {
                if b.get(k).is_none() {
                    v[k] = old.get(k).cloned().unwrap_or(legacy)
                }
            }
            if old["blank"] == true {
                v["blank"] = json!(true)
            }
            if let Some(source) = old.get("desktop_source_path") {
                v["desktop_source_path"] = source.clone()
            }
            (
                old["base_commit"].as_str().unwrap_or("").to_owned(),
                old["created_at"].as_str().unwrap_or("").to_owned(),
                old["owner_id"].as_str().unwrap_or("").to_owned(),
            )
        } else {
            let base = if let Some(token) = b["desktop_directory_token"].as_str() {
                if v["blank"] == true {
                    return Err(bad("Blank projects cannot consume a directory grant"));
                }
                let grant = self
                    .directories
                    .as_ref()
                    .ok_or_else(|| bad("Desktop directory authorization unavailable"))?
                    .consume(token)?;
                v["desktop_source_path"] = json!(grant.path);
                workspace::import_authorized(&self.data_root, &pid, &grant)?
            } else {
                workspace::import(&self.data_root, &self.import_root, &pid, &v)?
            };
            (base, now(c)?, u.id.clone())
        };
        if v["permission_profile"] == "full"
            && (v["execution_mode"] != "local" || b["full_access_confirmed"] != true)
        {
            return Err(bad(
                "Full access requires explicit confirmation and local execution",
            ));
        }
        v.as_object_mut().unwrap().remove("full_access_confirmed");
        v.as_object_mut().unwrap().remove("desktop_directory_token");
        c.execute("INSERT INTO projects(id,owner_id,config,base_commit,created_at) VALUES(?1,?2,?3,?4,?5) ON CONFLICT(id) DO UPDATE SET config=excluded.config",params![pid,u.id,encode(&v),base,created])?;
        emit(
            c,
            u,
            if id.is_some() {
                "project.updated"
            } else {
                "project.imported"
            },
            json!({"project_id":pid,"base_commit":base}),
        )?;
        v["id"] = json!(pid);
        v["owner_id"] = json!(owner);
        v["base_commit"] = json!(base);
        v["created_at"] = json!(created);
        Ok(v)
    }
    fn setup_status(&self, c: &Connection, u: &User) -> Result<Value> {
        let models = configs(c, ConfigTable::Model)?;
        let providers = self.providers(c)?;
        let usable: Vec<_> = models
            .iter()
            .filter(|m| {
                m["enabled"] != false
                    && m["supports_tools"] == true
                    && providers
                        .as_array()
                        .unwrap()
                        .iter()
                        .any(|p| p["id"] == m["provider_id"] && p["enabled"] == true)
            })
            .collect();
        let agents: Vec<_> = configs(c, ConfigTable::Agent)?
            .into_iter()
            .filter(|a| a["enabled"] != false)
            .collect();
        let bound = agents
            .iter()
            .filter(|a| usable.iter().any(|m| m["id"] == a["model_profile_id"]))
            .count();
        let mut checked = 0;
        for model in &usable {
            let probe = config(
                c,
                &format!("probe:{}", model["id"].as_str().unwrap()),
                Value::Null,
            )?;
            let provider = providers
                .as_array()
                .unwrap()
                .iter()
                .find(|p| p["id"] == model["provider_id"])
                .unwrap();
            if probe["model_version"] == model["version"]
                && probe["revision_id"] == provider["revision_id"]
            {
                checked += 1;
            }
        }
        Ok(
            json!({"mode":"production","has_usable_model":!usable.is_empty(),"team_ready":!agents.is_empty()&&bound==agents.len(),"needs_connection":usable.is_empty(),"agent_count":agents.len(),"bound_agent_count":bound,"checked_model_count":checked,"workspace_accessible":true,"provider_count":providers.as_array().unwrap().len(),"can_configure":u.role=="admin","onboarding_completed":!config(c,"onboarding",Value::Null)?.is_null()}),
        )
    }
}

fn table(name: &str) -> ConfigTable {
    match name {
        "models" => ConfigTable::Model,
        "agents" => ConfigTable::Agent,
        _ => ConfigTable::Orchestration,
    }
}
pub(super) fn bool_or(v: &Value, k: &str, d: bool) -> Result<bool> {
    match v.get(k) {
        None => Ok(d),
        Some(Value::Bool(v)) => Ok(*v),
        _ => Err(bad(format!("{k} must be boolean"))),
    }
}
pub(super) fn strings(v: &Value, k: &str, max: usize) -> Result<Vec<String>> {
    let arr = v[k]
        .as_array()
        .ok_or_else(|| bad(format!("{k} must be an array")))?;
    if arr.len() > max {
        return Err(bad(format!("Too many {k}")));
    }
    let mut seen = BTreeSet::new();
    let mut out = Vec::new();
    for x in arr {
        let s = x
            .as_str()
            .ok_or_else(|| bad(format!("{k} must contain strings")))?;
        if !seen.insert(s) {
            return Err(bad(format!("Duplicate {k}")));
        }
        out.push(s.into())
    }
    Ok(out)
}
pub(super) fn number(v: &Value, k: &str, min: f64, max: f64) -> Result<()> {
    let n = v[k]
        .as_f64()
        .ok_or_else(|| bad(format!("{k} must be numeric")))?;
    if !n.is_finite() || n < min || n > max {
        return Err(bad(format!("{k} out of range")));
    }
    Ok(())
}
pub(super) fn runtime_defaults() -> Value {
    json!({"max_steps":24,"max_agent_messages":200,"max_model_calls":100,"task_timeout_s":3600,"call_timeout_s":120,"command_timeout_s":120,"max_parallel":3,"max_tasks":20,"max_rework_rounds":2,"cpu":1,"memory_mb":1024,"max_file_bytes":200000,"max_context_chars":120000})
}
pub(super) fn validate_runtime(v: &Value, agent: bool) -> Result<()> {
    let limits = [
        ("max_steps", 1., 500.),
        ("max_agent_messages", 0., 5000.),
        ("max_model_calls", 1., 10000.),
        ("task_timeout_s", 30., 86400.),
        ("call_timeout_s", 5., 1800.),
        ("command_timeout_s", 1., 3600.),
        ("max_parallel", 1., 32.),
        ("max_tasks", 1., 100.),
        ("max_rework_rounds", 0., 10.),
        ("cpu", 0.1, 32.),
        ("memory_mb", 128., 65536.),
        ("max_file_bytes", 1024., 5000000.),
        ("max_context_chars", 4096., 1000000.),
    ];
    for k in object(v)?.keys() {
        let (_, l, h) = limits
            .iter()
            .find(|(name, _, _)| name == k)
            .ok_or_else(|| bad("Unknown runtime field"))?;
        if agent
            && [
                "max_agent_messages",
                "max_model_calls",
                "max_parallel",
                "max_tasks",
                "max_rework_rounds",
            ]
            .contains(&k.as_str())
        {
            return Err(bad("Run budget cannot be overridden by an agent"));
        }
        number(v, k, *l, *h)?
    }
    Ok(())
}
fn office_defaults() -> Value {
    json!({"idle_enabled":true,"idle_grace_s":35,"idle_stagger_s":25,"idle_cooldown_s":30,"walk_speed":2.3,"activities":["drink","phone","restroom","nap","stretch"],"durations_s":{"drink":9,"phone":13,"restroom":14,"nap":16,"stretch":7},"revision":0,"epoch":"2026-01-01T00:00:00+00:00"})
}
pub(super) fn visible(p: &Value, u: &User) -> bool {
    u.role == "admin"
        || p["owner_id"] == u.id
        || p["members"]
            .as_array()
            .is_some_and(|v| v.contains(&json!(u.id)))
}
pub(crate) fn validate_parameters(protocol: &str, allowed: &Value, p: &Value) -> Result<()> {
    let allowed = allowed
        .as_array()
        .ok_or_else(|| bad("allowed_parameters must be an array"))?;
    let supported: &[&str] = match protocol {
        "openai_chat" => &[
            "temperature",
            "top_p",
            "max_tokens",
            "max_completion_tokens",
            "presence_penalty",
            "frequency_penalty",
            "seed",
            "reasoning_effort",
            "stop",
            "thinking",
        ],
        "openai_responses" => &[
            "temperature",
            "top_p",
            "max_output_tokens",
            "reasoning",
            "text",
            "parallel_tool_calls",
        ],
        "anthropic_messages" => &[
            "max_tokens",
            "temperature",
            "top_p",
            "top_k",
            "stop_sequences",
            "thinking",
            "output_config",
        ],
        "google_gemini" => &[
            "temperature",
            "topP",
            "topK",
            "maxOutputTokens",
            "stopSequences",
            "thinkingConfig",
            "responseMimeType",
            "responseSchema",
            "seed",
        ],
        _ => return Err(bad("Unsupported model protocol")),
    };
    let unsupported = allowed
        .iter()
        .filter(|x| x.as_str().is_none_or(|s| !supported.contains(&s)))
        .map(|v| v.as_str().unwrap_or("<non-string>"))
        .collect::<Vec<_>>();
    if !unsupported.is_empty() {
        return Err(bad(format!(
            "Parameters {} are not supported by {protocol}. {}",
            unsupported.join(", "),
            if protocol == "openai_responses" {
                "Use max_output_tokens instead of max_tokens; update the model profile before changing the provider protocol."
            } else {
                "Update the model's declared and configured parameters before changing protocol."
            }
        )));
    }
    for k in object(p)?.keys() {
        if !allowed.contains(&json!(k)) {
            return Err(bad("Model parameter is not allowed"));
        }
    }
    for key in [
        "max_tokens",
        "max_completion_tokens",
        "max_output_tokens",
        "maxOutputTokens",
        "topK",
    ] {
        if let Some(v) = p.get(key) {
            if v.as_u64().is_none_or(|n| n == 0) {
                return Err(bad(format!("{key} must be a positive integer")));
            }
        }
    }
    if protocol == "openai_chat"
        && p.get("max_tokens").is_some()
        && p.get("max_completion_tokens").is_some()
    {
        return Err(bad("Choose max_tokens or max_completion_tokens, not both"));
    }
    if protocol == "anthropic_messages" && p.get("max_tokens").is_none() {
        return Err(bad("Anthropic Messages requires max_tokens"));
    }
    for (key, high) in [
        (
            "temperature",
            if protocol == "anthropic_messages" {
                1.
            } else {
                2.
            },
        ),
        ("top_p", 1.),
        ("topP", 1.),
    ] {
        if p.get(key).is_some() {
            number(p, key, 0., high)?;
        }
    }
    Ok(())
}
pub(super) fn validate_model(c: &Connection, b: &Value) -> Result<Value> {
    fields(
        b,
        &[
            "enabled",
            "name",
            "provider_id",
            "model_id",
            "allowed_parameters",
            "parameters",
            "supports_tools",
            "input_cost_per_million",
            "output_cost_per_million",
            "context_window",
        ],
    )?;
    string(b, "name", 1, 120)?;
    string(b, "model_id", 1, 200)?;
    let pid = string(b, "provider_id", 1, 32)?;
    let connection=c.query_row("SELECT r.config FROM providers p JOIN provider_revisions r ON r.id=p.revision_id WHERE p.id=?1",[pid],|r|decode(r.get(0)?)).optional()?.ok_or_else(||bad("Provider does not exist"))?;
    let v = defaults(
        b,
        json!({"enabled":true,"allowed_parameters":["temperature","top_p","max_tokens"],"parameters":{"max_tokens":4096},"supports_tools":true,"input_cost_per_million":null,"output_cost_per_million":null,"context_window":null}),
    )?;
    bool_or(&v, "enabled", true)?;
    bool_or(&v, "supports_tools", true)?;
    for k in ["input_cost_per_million", "output_cost_per_million"] {
        if !v[k].is_null() {
            number(&v, k, 0., 1e12)?
        }
    }
    if !v["context_window"].is_null() {
        number(&v, "context_window", 1., 1e9)?
    }
    if connection["protocol"] == "custom_http" {
        protocols::parameters(
            connection["protocol"].as_str().unwrap(),
            &v["allowed_parameters"],
            &v["parameters"],
            &connection["protocol_config"],
        )?;
    } else {
        validate_parameters(
            connection["protocol"].as_str().unwrap_or(""),
            &v["allowed_parameters"],
            &v["parameters"],
        )?;
    }
    Ok(v)
}
pub(super) fn validate_agent(
    c: &Connection,
    id: Option<&str>,
    b: &Value,
    prior: Option<&Value>,
) -> Result<Value> {
    fields(
        b,
        &[
            "job_title",
            "office_position",
            "tool_permissions",
            "design_ref",
            "role_template_ref",
            "department",
            "communication",
            "skills",
            "enabled",
            "reports_to",
            "peer_reception",
            "name",
            "role",
            "model_profile_id",
            "fallback_model_ids",
            "instructions",
            "runtime",
            "model_parameters",
        ],
    )?;
    string(b, "name", 1, 80)?;
    let role = string(b, "role", 1, 30)?;
    if !["planner", "developer", "tester", "reviewer"].contains(&role) {
        return Err(bad("Invalid agent role"));
    }
    let mut v = defaults(
        b,
        json!({"job_title":"","office_position":null,"tool_permissions":null,"design_ref":null,"role_template_ref":null,"department":"","communication":{"scope":"run","recipients":[]},"skills":[],"enabled":true,"reports_to":null,"peer_reception":"sit","model_profile_id":null,"fallback_model_ids":[],"instructions":"","runtime":{},"model_parameters":{}}),
    )?;
    if let Some(old) = prior {
        for k in ["office_position", "tool_permissions", "role_template_ref"] {
            if b.get(k).is_none() {
                v[k] = old[k].clone()
            }
        }
    }
    if !v["design_ref"].is_null() && prior.is_none_or(|x| x["design_ref"] != v["design_ref"]) {
        return Err(bad(
            "Design provenance can only be created through instantiation",
        ));
    }
    validate_runtime(&v["runtime"], true)?;
    string(&v, "instructions", 0, 20000)?;
    bool_or(&v, "enabled", true)?;
    let mut models = strings(&v, "fallback_model_ids", 5)?;
    if let Some(mid) = v["model_profile_id"].as_str() {
        models.push(mid.into())
    }
    for mid in models {
        let model = get_config(c, ConfigTable::Model, &mid)?;
        let connection=c.query_row("SELECT r.config FROM providers p JOIN provider_revisions r ON r.id=p.revision_id WHERE p.id=?1",[model["provider_id"].as_str().unwrap_or("")],|r|decode(r.get(0)?))?;
        let effective = defaults(&v["model_parameters"], model["parameters"].clone())?;
        if connection["protocol"] == "custom_http" {
            protocols::parameters(
                connection["protocol"].as_str().unwrap(),
                &model["allowed_parameters"],
                &effective,
                &connection["protocol_config"],
            )?;
        } else {
            validate_parameters(
                connection["protocol"].as_str().unwrap_or(""),
                &model["allowed_parameters"],
                &effective,
            )?;
        }
    }
    let allowed = role_tools(role);
    if !v["tool_permissions"].is_null() {
        let permissions = strings(&v, "tool_permissions", 20)?;
        if !permissions.contains(&"finish".to_owned())
            || permissions.iter().any(|x| !allowed.contains(&x.as_str()))
        {
            return Err(bad("Agent tool permissions exceed role authority"));
        }
    }
    let mut seen = BTreeSet::new();
    if let Some(id) = id {
        seen.insert(id.to_owned());
    }
    let mut leader = v["reports_to"].as_str().map(str::to_owned);
    while let Some(aid) = leader {
        if !seen.insert(aid.clone()) {
            return Err(bad("Reporting relationship is cyclic"));
        }
        leader = get_config(c, ConfigTable::Agent, &aid)?["reports_to"]
            .as_str()
            .map(str::to_owned);
    }
    let recipients = v["communication"]
        .get("recipients")
        .cloned()
        .unwrap_or(json!([]));
    for aid in strings(&json!({"recipients":recipients}), "recipients", 100)? {
        get_config(c, ConfigTable::Agent, &aid)?;
    }
    skills::validate_bindings(c, &v["skills"])?;
    if !v["role_template_ref"].is_null() {
        skill_store::validate_role_ref(c, &v["role_template_ref"])?;
    }
    Ok(v)
}
pub(super) fn role_tools(role: &str) -> Vec<&'static str> {
    let mut tools = vec![
        "list_files",
        "read_file",
        "send_message",
        "finish",
        "request_decision",
    ];
    if ["developer", "tester"].contains(&role) {
        tools.extend(["write_file", "delete_file", "run_command"])
    }
    tools
}
pub(super) fn validate_graph(c: &Connection, v: &Value) -> Result<()> {
    fields(
        v,
        &[
            "mode",
            "participants",
            "strategy",
            "stop_when",
            "prompt",
            "criteria",
            "assessor_id",
            "stagnation_checks",
            "legacy_stop_migrated",
        ],
    )?;
    let normalized = defaults(
        v,
        json!({"mode":"development", "participants":[], "strategy":"sequential", "stop_when":"goal_satisfied", "prompt":"Deliver a verified solution with explicit risks and acceptance evidence.", "criteria":[{"id":"solution","description":"Deliver an actionable solution"},{"id":"risks","description":"Identify risks and verification evidence"}], "assessor_id":null, "stagnation_checks":3,"legacy_stop_migrated":false}),
    )?;
    let v = &normalized;
    let members = strings(v, "participants", 30)?;
    if !["development", "discussion"].contains(&v["mode"].as_str().unwrap_or(""))
        || !["sequential", "parallel"].contains(&v["strategy"].as_str().unwrap_or(""))
        || v["stop_when"] != "goal_satisfied"
    {
        return Err(bad(
            "Invalid orchestration mode, strategy, or stopping condition",
        ));
    }
    if (v["mode"] == "discussion" || !members.is_empty()) && members.len() < 2 {
        return Err(bad("Orchestration requires at least two participants"));
    }
    string(v, "prompt", 1, 8000)?;
    number(v, "stagnation_checks", 2., 20.)?;
    bool_or(v, "legacy_stop_migrated", false)?;
    let criteria = v["criteria"]
        .as_array()
        .ok_or_else(|| bad("Orchestration criteria must be an array"))?;
    if criteria.is_empty() || criteria.len() > 20 {
        return Err(bad("Orchestration requires 1 to 20 criteria"));
    }
    let mut ids = BTreeSet::new();
    for criterion in criteria {
        fields(criterion, &["id", "description", "evidence_type"])?;
        let id = string(criterion, "id", 1, 48)?;
        if !id.as_bytes()[0].is_ascii_alphabetic()
            || !id
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
            || !ids.insert(id)
        {
            return Err(bad("Acceptance criterion IDs must be valid and unique"));
        }
        string(criterion, "description", 3, 2000)?;
        if criterion
            .get("evidence_type")
            .is_some_and(|v| !["decision", "code_reference"].contains(&v.as_str().unwrap_or("")))
        {
            return Err(bad("Invalid acceptance evidence type"));
        }
    }
    if let Some(assessor) = v.get("assessor_id").filter(|v| !v.is_null()) {
        if !members.iter().any(|id| assessor == id) {
            return Err(bad("Assessor must belong to the participant team"));
        }
    }
    for aid in &members {
        if get_config(c, ConfigTable::Agent, aid)?["enabled"] == false {
            return Err(bad("Orchestration member is disabled"));
        }
    }
    Ok(())
}
fn validate_project(c: &Connection, b: &Value) -> Result<Value> {
    fields(
        b,
        &[
            "execution_mode",
            "permission_profile",
            "full_access_confirmed",
            "archived",
            "name",
            "import_path",
            "blank",
            "desktop_directory_token",
            "instructions",
            "sandbox_image",
            "check_commands",
            "allow_external_models",
            "runtime",
            "members",
        ],
    )?;
    string(b, "name", 1, 120)?;
    let v = defaults(
        b,
        json!({"execution_mode":"local","permission_profile":"ask","archived":false,"import_path":"blank","blank":false,"instructions":"","sandbox_image":"agent-forge/sandbox:local","check_commands":[],"allow_external_models":true,"runtime":{},"members":[]}),
    )?;
    if !["local", "container"].contains(&v["execution_mode"].as_str().unwrap_or(""))
        || !["ask", "auto", "full"].contains(&v["permission_profile"].as_str().unwrap_or(""))
    {
        return Err(bad("Invalid execution policy"));
    }
    if v["permission_profile"] == "full"
        && (v["execution_mode"] != "local" || b["full_access_confirmed"] != true)
    {
        return Err(bad("Full access requires explicit confirmation"));
    }
    for k in ["archived", "blank", "allow_external_models"] {
        bool_or(&v, k, false)?;
    }
    string(&v, "instructions", 0, 20000)?;
    string(&v, "import_path", 1, 1000)?;
    validate_runtime(&v["runtime"], false)?;
    for command in strings(&v, "check_commands", 20)? {
        if command.trim().is_empty() || command.len() > 4000 {
            return Err(bad("Invalid check command"));
        }
    }
    for member in strings(&v, "members", 1000)? {
        if c.query_row("SELECT enabled FROM users WHERE id=?1", [member], |r| {
            r.get::<_, bool>(0)
        })
        .optional()?
            != Some(true)
        {
            return Err(bad("Project member does not exist or is disabled"));
        }
    }
    Ok(v)
}

#[cfg(test)]
mod attachment_tests {
    use super::materialize_run_attachment;
    use std::fs;
    use uuid::Uuid;

    #[test]
    fn attachment_is_materialized_for_canonical_and_active_member_workspaces() {
        let root =
            std::env::temp_dir().join(format!("forge-attachment-{}", Uuid::new_v4().simple()));
        let run = root.join("runs/run-1");
        fs::create_dir_all(run.join("workspace")).unwrap();
        fs::create_dir_all(run.join("members/agent-a/workspace")).unwrap();
        let relative =
            materialize_run_attachment(&root, "run-1", "att-1", "notes.txt", b"hello").unwrap();
        assert_eq!(relative, ".forge-attachments/att-1-notes.txt");
        for workspace in [run.join("workspace"), run.join("members/agent-a/workspace")] {
            assert_eq!(fs::read(workspace.join(&relative)).unwrap(), b"hello");
        }
        fs::remove_dir_all(root).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn redirected_attachment_directory_is_rejected() {
        use std::os::unix::fs::symlink;
        let root =
            std::env::temp_dir().join(format!("forge-attachment-{}", Uuid::new_v4().simple()));
        let run = root.join("runs/run-1");
        fs::create_dir_all(&run).unwrap();
        let outside = root.join("outside");
        fs::create_dir_all(&outside).unwrap();
        symlink(&outside, run.join("workspace")).unwrap();
        let error =
            materialize_run_attachment(&root, "run-1", "att-1", "notes.txt", b"hello").unwrap_err();
        assert_eq!(error.status, 409);
        assert!(!outside.join(".forge-attachments/att-1-notes.txt").exists());
        fs::remove_dir_all(root).unwrap();
    }
}
