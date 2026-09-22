//! Product run creation and live Codex ownership. A product intent is retained
//! before kernel provisioning; after an interrupted start it is never replayed.
use crate::product_api::{ProductDownload, ProductError, ProductReply};
use crate::{
    BackendError, ControlAction, ProductStore, RunControl, RunController, RunDecisionInput, User,
};
use base64::Engine;
use forge_core::{
    ApprovalKind, ApprovalRequest, ApprovalStatus, Command, CommandResult, PermissionProfile,
    ProviderModelSnapshot, ProviderSnapshot, RouteBinding, Run, RunConfig, RunMember, RunStatus,
    RunTransition,
};
use forge_runtime::{CodexClient, RuntimeConfig, TurnTerminalStatus};
use forge_store::Store;
use rusqlite::{params, OptionalExtension, TransactionBehavior};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet, HashMap},
    fs,
    io::{Read, Write},
    path::{Component, Path, PathBuf},
    sync::{Arc, Mutex, Weak},
    time::Duration,
};
use tokio::sync::{mpsc, oneshot};
use uuid::Uuid;
mod configuration;
mod model_parameters;
mod queries;
mod team_decisions;
mod team_execution;

type Result<T> = std::result::Result<T, BackendError>;
fn db(_: rusqlite::Error) -> BackendError {
    BackendError::Unavailable
}
fn io(_: std::io::Error) -> BackendError {
    BackendError::Unavailable
}
fn kernel(_: forge_store::StoreError) -> BackendError {
    BackendError::Conflict
}
fn hash(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

/// Gateway requests use a provider namespace exactly once. Frozen route
/// bindings already carry that namespace, while raw fallback candidates do
/// not; preserve model names containing slashes and only avoid duplicating
/// this exact provider prefix.
pub(super) fn gateway_model_id(provider_id: &str, model_id: &str) -> String {
    let prefix = format!("{provider_id}/");
    if model_id.starts_with(&prefix) {
        model_id.to_owned()
    } else {
        format!("{prefix}{model_id}")
    }
}

fn materialize_skills(runtime_home: &Path, skills: &[FrozenSkill]) -> Result<()> {
    fn ensure_directory(path: &Path, root: &Path) -> Result<()> {
        for parent in path.ancestors() {
            if parent.starts_with(root)
                && fs::symlink_metadata(parent).is_ok_and(|meta| meta.file_type().is_symlink())
            {
                return Err(BackendError::Conflict);
            }
        }
        fs::create_dir_all(path).map_err(io)
    }
    let root = runtime_home.join("skills");
    // Materialization precedes Codex startup, so reject redirected ancestors here
    // as well as in the process environment validator.
    for parent in root
        .ancestors()
        .take_while(|path| path.starts_with(runtime_home))
    {
        if fs::symlink_metadata(parent).is_ok_and(|meta| meta.file_type().is_symlink()) {
            return Err(BackendError::Conflict);
        }
    }
    ensure_directory(&root, &root)?;
    for skill in skills {
        let name = Path::new(&skill.name)
            .file_name()
            .and_then(|v| v.to_str())
            .filter(|v| !v.is_empty() && *v != "." && *v != "..")
            .ok_or(BackendError::InvalidData)?;
        let directory = root.join(name);
        ensure_directory(&directory, &root)?;
        for (relative, content) in &skill.files {
            let script = relative.starts_with("scripts/")
                || Path::new(relative)
                    .extension()
                    .and_then(|value| value.to_str())
                    .is_some_and(|extension| ["py", "sh", "js", "ts"].contains(&extension));
            if script && !skill.allow_scripts {
                continue;
            }
            let path = directory.join(relative);
            if !path
                .parent()
                .is_some_and(|parent| parent.starts_with(&directory))
            {
                return Err(BackendError::InvalidData);
            }
            if let Some(parent) = path.parent() {
                ensure_directory(parent, &root)?;
            }
            match fs::symlink_metadata(&path) {
                Ok(meta) if meta.file_type().is_symlink() => return Err(BackendError::Conflict),
                Ok(_) => {
                    let existing = fs::read_to_string(&path).map_err(io)?;
                    if existing != *content {
                        return Err(BackendError::Conflict);
                    }
                }
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                    let mut file = fs::OpenOptions::new()
                        .write(true)
                        .create_new(true)
                        .open(&path)
                        .map_err(io)?;
                    file.write_all(content.as_bytes()).map_err(io)?;
                    file.flush().map_err(io)?;
                }
                Err(_) => return Err(BackendError::Unavailable),
            }
        }
    }
    Ok(())
}

fn materialize_input_attachments(workspace: &Path, attachments: &[RunAttachment]) -> Result<()> {
    if attachments.is_empty() {
        return Ok(());
    }
    let root = workspace.join(".forge-attachments");
    fs::create_dir_all(&root).map_err(io)?;
    for attachment in attachments {
        let bytes = base64::engine::general_purpose::STANDARD
            .decode(&attachment.content_base64)
            .map_err(|_| BackendError::InvalidData)?;
        let target = root.join(&attachment.filename);
        let mut output = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(target)
            .map_err(io)?;
        output.write_all(&bytes).map_err(io)?;
        output.flush().map_err(io)?;
    }
    Ok(())
}
fn text<'a>(v: &'a Value, key: &str) -> Result<&'a str> {
    v[key]
        .as_str()
        .filter(|s| !s.trim().is_empty())
        .ok_or(BackendError::InvalidData)
}

fn effective_tools(agent: &Value) -> Result<Vec<String>> {
    let role = agent["role"].as_str().ok_or(BackendError::InvalidData)?;
    let allowed = crate::product_api::role_tools(role);
    let tools = match agent.get("tool_permissions") {
        None | Some(Value::Null) => allowed.iter().map(|s| (*s).to_owned()).collect(),
        Some(value) => value
            .as_array()
            .ok_or(BackendError::InvalidData)?
            .iter()
            .map(|item| {
                item.as_str()
                    .filter(|s| !s.trim().is_empty())
                    .map(str::to_owned)
                    .ok_or(BackendError::InvalidData)
            })
            .collect::<Result<Vec<_>>>()?,
    };
    if tools.is_empty()
        || tools.len() > 20
        || !tools.iter().any(|tool| tool == "finish")
        || tools.iter().any(|tool| !allowed.contains(&tool.as_str()))
        || tools.iter().collect::<BTreeSet<_>>().len() != tools.len()
    {
        return Err(BackendError::Forbidden);
    }
    Ok(tools)
}

fn freeze_skills(tx: &rusqlite::Transaction<'_>, agent: &Value) -> Result<Vec<FrozenSkill>> {
    let bindings = agent
        .get("skills")
        .and_then(Value::as_array)
        .ok_or(BackendError::InvalidData)?;
    let mut frozen = Vec::new();
    let mut seen = BTreeSet::new();
    if bindings.len() > 12 {
        return Err(BackendError::InvalidData);
    }
    for binding in bindings {
        if binding["enabled"] == false {
            continue;
        }
        let skill_id = binding["skill_id"]
            .as_str()
            .filter(|s| !s.is_empty())
            .ok_or(BackendError::InvalidData)?;
        if !seen.insert(skill_id) {
            return Err(BackendError::InvalidData);
        }
        let version = binding["version"]
            .as_i64()
            .filter(|v| *v > 0)
            .ok_or(BackendError::InvalidData)?;
        let mode = binding["mode"].as_str().unwrap_or("on_demand");
        let parameters = binding
            .get("parameters")
            .cloned()
            .unwrap_or_else(|| json!({}));
        if !["on_demand", "always"].contains(&mode)
            || !parameters.is_object()
            || parameters.to_string().len() > 4000
        {
            return Err(BackendError::InvalidData);
        }
        if binding["allow_scripts"] == true
            && !effective_tools(agent)?
                .iter()
                .any(|tool| tool == "run_command")
        {
            return Err(BackendError::Forbidden);
        }
        let (name, digest, status, enabled, files_raw, description): (String, String, String, bool, String, String) = tx
            .query_row(
                "SELECT s.name,r.digest,r.status,s.enabled,r.files,COALESCE(json_extract(r.manifest,'$.description'),'') FROM skills s JOIN skill_revisions r ON r.skill_id=s.id AND r.version=?2 WHERE s.id=?1",
                params![skill_id, version],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?, row.get(4)?, row.get(5)?)),
            )
            .optional()
            .map_err(db)?
            .ok_or(BackendError::NotFound)?;
        if !enabled || status != "approved" {
            return Err(BackendError::Forbidden);
        }
        if name.len() > 64
            || name.split('-').any(|part| {
                part.is_empty()
                    || !part
                        .bytes()
                        .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit())
            })
        {
            return Err(BackendError::InvalidData);
        }
        let files: Value =
            serde_json::from_str(&files_raw).map_err(|_| BackendError::InvalidData)?;
        if crate::product_api::digest(&files) != digest {
            return Err(BackendError::Conflict);
        }
        let object = files.as_object().ok_or(BackendError::InvalidData)?;
        if object.is_empty() || object.len() > 80 || files_raw.len() > 2_000_000 {
            return Err(BackendError::InvalidData);
        }
        let mut clean = BTreeMap::new();
        let mut total = 0;
        for (path, content) in object {
            let content = content.as_str().ok_or(BackendError::InvalidData)?;
            total += content.len();
            if path.is_empty()
                || path.len() > 200
                || path.starts_with('/')
                || path.contains(['\\', ':', '\0'])
                || path
                    .split('/')
                    .any(|part| part.is_empty() || part == "." || part == "..")
                || content.contains('\0')
                || content.len() > 200_000
                || total > 1_500_000
            {
                return Err(BackendError::InvalidData);
            }
            clean.insert(path.clone(), content.to_owned());
        }
        if !clean.contains_key("SKILL.md") {
            return Err(BackendError::InvalidData);
        }
        frozen.push(FrozenSkill {
            skill_id: skill_id.to_owned(),
            version,
            digest,
            name,
            description,
            allow_scripts: binding["allow_scripts"] == true,
            mode: mode.to_owned(),
            parameters,
            files: clean,
        });
    }
    Ok(frozen)
}

fn revalidate_skills(connection: &rusqlite::Connection, skills: &[FrozenSkill]) -> Result<()> {
    for skill in skills {
        let current: Option<(bool, String, String)> = connection
            .query_row(
                "SELECT s.enabled,r.status,r.digest FROM skills s JOIN skill_revisions r ON r.skill_id=s.id AND r.version=?2 WHERE s.id=?1",
                params![skill.skill_id, skill.version],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .optional()
            .map_err(db)?;
        if !current.is_some_and(|(enabled, status, digest)| {
            enabled && status == "approved" && digest == skill.digest
        }) {
            return Err(BackendError::Forbidden);
        }
    }
    Ok(())
}

/// Keep the approved binding metadata identical for single and team sessions.
fn skill_instructions(runtime_home: &Path, skills: &[FrozenSkill]) -> String {
    if skills.is_empty() {
        return String::new();
    }
    let catalog = skills.iter().map(|skill| {
        json!({"name":skill.name,"path":runtime_home.join("skills").join(&skill.name).join("SKILL.md"),"version":skill.version,"mode":skill.mode,"allow_scripts":skill.allow_scripts,"parameters":skill.parameters})
    }).collect::<Vec<_>>();
    format!(" Approved skill bindings: {}. Always-mode skills are explicitly selected for this turn; apply their instructions. Read on-demand skills when relevant. Skill metadata never grants additional tools. Packaged scripts are available only where allow_scripts is true.", json!(catalog))
}

/// An explicit Skill input makes Codex load always-mode skills through its own
/// catalog and selection path, instead of relying on a model to remember a hint.
fn turn_inputs(runtime_home: &Path, skills: &[FrozenSkill], prompt: &str) -> Value {
    let mut input = vec![json!({"type":"text","text":prompt,"text_elements":[]})];
    input.extend(skills.iter().filter(|skill| skill.mode == "always").map(|skill| {
        json!({"type":"skill","name":skill.name,"path":runtime_home.join("skills").join(&skill.name).join("SKILL.md")})
    }));
    json!(input)
}

#[derive(Clone, Serialize, Deserialize)]
struct FrozenModel {
    profile_id: String,
    profile_version: i64,
    model_id: String,
    parameters: Value,
    provider_id: String,
    provider_revision_id: String,
    protocol: String,
    base_url: String,
}

#[derive(Clone, Serialize, Deserialize)]
struct FrozenSkill {
    skill_id: String,
    version: i64,
    digest: String,
    name: String,
    #[serde(default)]
    description: String,
    #[serde(default)]
    allow_scripts: bool,
    #[serde(default)]
    mode: String,
    #[serde(default)]
    parameters: Value,
    files: BTreeMap<String, String>,
}

#[derive(Clone, Serialize, Deserialize)]
struct RunAttachment {
    filename: String,
    #[serde(default)]
    media_type: String,
    content_base64: String,
}

/// The applied office scope selected for a run. This is provenance for the
/// run, not an execution permission grant; the member/model/policy snapshots
/// remain the authoritative execution configuration.
#[derive(Clone, Serialize, Deserialize)]
struct FrozenOfficeSelection {
    id: String,
    name: String,
    floor: i64,
    floor_name: String,
    department: String,
    layout_ref: Value,
    member_ids: Vec<String>,
    selected_agent_ids: Vec<String>,
}

#[derive(Clone, Serialize, Deserialize)]
struct Frozen {
    id: String,
    title: String,
    request: String,
    config: RunConfig,
    provider: ProviderSnapshot,
    agent: Value,
    /// Effective, role-validated Forge tools frozen at run creation.
    #[serde(default)]
    tools: Vec<String>,
    /// Approved skill revisions copied into the isolated Codex home.
    #[serde(default)]
    skills: Vec<FrozenSkill>,
    project: Value,
    model: Value,
    /// Primary model followed by the reviewed fallback chain. All entries
    /// are immutable snapshots and share the provider revision used by the
    /// frozen route, so switching never changes credentials or endpoint.
    #[serde(default)]
    models: Vec<FrozenModel>,
    #[serde(default)]
    runtime: configuration::RuntimeLimits,
    encrypted_key: String,
    /// Validated non-secret headers frozen with the provider revision.
    #[serde(default)]
    provider_headers: BTreeMap<String, String>,
    #[serde(default)]
    gateway: Option<crate::gateway::GatewayRoute>,
    #[serde(default)]
    team: BTreeMap<String, Frozen>,
    /// Attachments supplied with the initial request, frozen before the
    /// runtime starts so the model can read them from its run workspace.
    #[serde(default)]
    attachments: Vec<RunAttachment>,
    #[serde(default)]
    office: Option<FrozenOfficeSelection>,
}

impl Frozen {
    fn team_snapshot(&self) -> Value {
        let mut agents = serde_json::Map::new();
        let views: Vec<(&str, &Frozen)> = if self.team.is_empty() {
            vec![(self.config.members[0].worker_id.as_str(), self)]
        } else {
            self.team
                .iter()
                .map(|(id, member)| (id.as_str(), member))
                .collect()
        };
        for (id, member) in views {
            let mut agent = member.agent.clone();
            agent["id"] = json!(id);
            agent["tools"] = json!(member.tools);
            agent["model"] = json!({"id": member.config.route_binding.model_id, "provider_id": member.provider.provider_id});
            agent["skills"] = json!(member.skills.iter().map(|skill| json!({"skill_id":skill.skill_id,"name":skill.name,"description":skill.description,"version":skill.version,"digest":skill.digest,"mode":skill.mode,"allow_scripts":skill.allow_scripts})).collect::<Vec<_>>());
            agents.insert(id.into(), agent);
        }
        let mut snapshot = json!({"execution_engine":"codex","execution_strategy":"autonomous","project":self.project,"agents":agents,"entry_agent_id":self.config.coordinator_id(),"runtime":self.runtime,"owner":"rust"});
        if let Some(office) = &self.office {
            snapshot["design"] = json!({"organization_team": office});
        }
        snapshot
    }
}
struct LiveCommand {
    actor: User,
    input: RunControl,
    reply: oneshot::Sender<Result<Value>>,
}

pub(crate) struct RunService {
    product: Arc<ProductStore>,
    path: PathBuf,
    root: PathBuf,
    codex: Option<PathBuf>,
    runtime: tokio::runtime::Handle,
    slots: tokio::sync::Semaphore,
    live: Mutex<HashMap<String, mpsc::Sender<LiveCommand>>>,
    creation: Mutex<()>,
    events_changed: tokio::sync::watch::Sender<u64>,
    this: Weak<Self>,
    _lock: fs::File,
}

impl RunService {
    pub(crate) fn new(
        product: Arc<ProductStore>,
        path: PathBuf,
        root: PathBuf,
        codex: Option<PathBuf>,
    ) -> Result<Arc<Self>> {
        fs::create_dir_all(&root).map_err(io)?;
        let lock = fs::OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(false)
            .open(path.with_extension("host.lock"))
            .map_err(io)?;
        #[cfg(unix)]
        {
            use std::os::fd::AsRawFd;
            if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
                return Err(BackendError::Conflict);
            }
        }
        let mut store = Store::open(&path).map_err(kernel)?;
        let c = rusqlite::Connection::open(&path).map_err(db)?;
        let mut statement=c.prepare("SELECT snapshot_json FROM runs WHERE json_extract(snapshot_json,'$.status') IN ('starting','running')").map_err(db)?;
        let stale = statement
            .query_map([], |r| r.get::<_, String>(0))
            .map_err(db)?
            .collect::<std::result::Result<Vec<_>, _>>()
            .map_err(db)?;
        for raw in stale {
            let run: Run = serde_json::from_str(&raw).map_err(|_| BackendError::InvalidData)?;
            if let Some(execution) = run.execution {
                change(
                    &mut store,
                    &run.id,
                    RunTransition::MarkOutcomeUnknown {
                        connection: execution.connection,
                    },
                )?;
            }
        }
        product.connect()?.execute_batch("CREATE TABLE IF NOT EXISTS forge_run_intents(id TEXT PRIMARY KEY,actor_id TEXT NOT NULL,request_hash TEXT NOT NULL,frozen TEXT NOT NULL,created_at TEXT NOT NULL);").map_err(db)?;
        product.connect()?.execute_batch("CREATE TABLE IF NOT EXISTS forge_runtime_messages(run_id TEXT NOT NULL,connection_id TEXT NOT NULL,sequence INTEGER NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(run_id,connection_id,sequence));").map_err(db)?;
        // Repair catalog/kernel provisioning only; recovery never starts a model turn.
        let catalog = product.connect()?;
        let mut intents = catalog
            .prepare("SELECT frozen FROM forge_run_intents")
            .map_err(db)?;
        for raw in intents
            .query_map([], |row| row.get::<_, String>(0))
            .map_err(db)?
        {
            let frozen: Frozen =
                serde_json::from_str(&raw.map_err(db)?).map_err(|_| BackendError::InvalidData)?;
            provision(&mut store, &frozen)?;
        }
        let root = root.canonicalize().map_err(io)?;
        let runtime =
            tokio::runtime::Handle::try_current().map_err(|_| BackendError::Unavailable)?;
        Ok(Arc::new_cyclic(|this| Self {
            product,
            path,
            root,
            codex,
            runtime,
            slots: tokio::sync::Semaphore::new(
                std::env::var("MAX_ACTIVE_RUNS")
                    .ok()
                    .and_then(|s| s.parse::<usize>().ok())
                    .filter(|n| (1..=32).contains(n))
                    .unwrap_or(2),
            ),
            live: Mutex::new(HashMap::new()),
            creation: Mutex::new(()),
            events_changed: tokio::sync::watch::channel(0).0,
            this: this.clone(),
            _lock: lock,
        }))
    }
    pub(crate) fn enabled(&self) -> bool {
        self.codex.as_ref().is_some_and(|p| p.is_file())
    }
    pub(crate) fn owns(&self, id: &str) -> Result<bool> {
        self.product
            .connect()?
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM forge_run_intents WHERE id=?1)",
                [id],
                |r| r.get(0),
            )
            .map_err(db)
    }
    fn frozen(&self, id: &str) -> Result<Frozen> {
        let raw: String = self
            .product
            .connect()?
            .query_row(
                "SELECT frozen FROM forge_run_intents WHERE id=?1",
                [id],
                |r| r.get(0),
            )
            .optional()
            .map_err(db)?
            .ok_or(BackendError::NotFound)?;
        serde_json::from_str(&raw).map_err(|_| BackendError::InvalidData)
    }
    pub(crate) fn enrich(&self, value: &mut Value) -> Result<()> {
        let Some(id) = value["id"].as_str().map(str::to_owned) else {
            return Ok(());
        };
        if !self.owns(&id)? {
            return Ok(());
        }
        let f = self.frozen(&id)?;
        value["title"] = json!(f.title);
        value["request"] = json!(f.request);
        if value.get("snapshot").is_some() {
            value["snapshot"]["agents"] = f.team_snapshot()["agents"].clone();
            value["snapshot"]["project"] = f.project;
            value["snapshot"]["runtime"] = json!(f.runtime);
            value["snapshot"]["execution_engine"] = json!("codex");
            value["snapshot"]["entry_agent_id"] = json!(f.config.members[0].worker_id);
            if let Some(office) = &f.office {
                value["snapshot"]["design"] = json!({"organization_team": office});
            }
            value["commands"] = json!([]);
            if let Some(tasks) = value["tasks"].as_array_mut() {
                for task in tasks {
                    if let Some(id) = task["id"].as_str() {
                        task["key"] = json!(id.split_once(':').map(|(_, key)| key).unwrap_or(id));
                    }
                    task["agent_id"] = task
                        .get("assigned_worker_id")
                        .filter(|id| !id.is_null())
                        .cloned()
                        .unwrap_or_else(|| json!(f.config.members[0].worker_id));
                    task["status"] = json!(match task["status"].as_str() {
                        Some("submitted") => "done",
                        Some("outcome_unknown") => "needs_attention",
                        Some(s) => s,
                        None => "pending",
                    });
                }
            }
        }
        Ok(())
    }
    pub(crate) fn subscribe_events(&self) -> tokio::sync::watch::Receiver<u64> {
        self.events_changed.subscribe()
    }
    fn notify_events(&self) {
        self.events_changed.send_modify(|version| *version = version.wrapping_add(1));
    }

    fn apply_enriched_event(event: &mut crate::Event, payload: Value) {
        let kind = event.data["kind"].as_str().unwrap_or("unknown").to_owned();
        event.data["agent_id"] = event
            .data
            .pointer("/connection/worker_id")
            .cloned()
            .unwrap_or(Value::Null);
        if let Some(task_key) = payload.get("_forge_task_key").and_then(Value::as_str) {
            event.data["task_key"] = json!(task_key);
        }
        event.data["payload"] = payload;
        event.data["runtime_kind"] = json!(kind);
        event.kind = format!("runtime.{}", kind.replace('/', "."));
    }

    /// Enrich a page with one query per run instead of one query per event.
    /// Runtime events are persisted in a normalized message table, so doing
    /// the lookup inside the SSE loop caused an N+1 query for every burst of
    /// deltas. Missing payloads remain valid runtime events with an empty body.
    pub(crate) fn enrich_events(&self, events: &mut [crate::Event]) -> Result<()> {
        let keys: Vec<Value> = events.iter().filter(|event| event.kind == "runtime_event").map(|event| {
            json!([event.run_id, event.data.pointer("/connection/connection_id").and_then(Value::as_str).unwrap_or(""), event.data["sequence"]])
        }).collect();
        if keys.is_empty() { return Ok(()); }
        let connection = self.product.connect()?;
        let mut payloads: HashMap<(String, String, i64), Value> = HashMap::new();
        // json_each avoids SQLite's parameter limit. Each requested identity
        // uses the (run, connection, sequence) primary index; old history is
        // never scanned again when a new delta arrives.
        for chunk in keys.chunks(1000) {
            let mut statement = connection.prepare(
                "SELECT m.run_id,m.connection_id,m.sequence,m.payload FROM json_each(?1) k JOIN forge_runtime_messages m ON m.run_id=json_extract(k.value,'$[0]') AND m.connection_id=json_extract(k.value,'$[1]') AND m.sequence=json_extract(k.value,'$[2]')"
            ).map_err(db)?;
            let rows = statement.query_map([json!(chunk).to_string()], |row| Ok((row.get::<_,String>(0)?,row.get::<_,String>(1)?,row.get::<_,i64>(2)?,row.get::<_,String>(3)?))).map_err(db)?;
            for row in rows {
                let (run, connection, sequence, raw) = row.map_err(db)?;
                payloads.insert((run, connection, sequence), serde_json::from_str(&raw).map_err(|_| BackendError::InvalidData)?);
            }
        }
        for event in events.iter_mut().filter(|event| event.kind == "runtime_event") {
            let connection = event
                .data
                .pointer("/connection/connection_id")
                .and_then(Value::as_str)
                .unwrap_or("");
            let sequence = event.data["sequence"].as_i64().unwrap_or(0);
            let payload = payloads
                .remove(&(event.run_id.clone(), connection.to_owned(), sequence))
                .unwrap_or_else(|| json!({}));
            Self::apply_enriched_event(event, payload);
        }
        Ok(())
    }

    fn prepare(&self, user: &User, input: &Value, id: &str) -> Result<Frozen> {
        let ids = input["agent_ids"]
            .as_array()
            .ok_or(BackendError::InvalidData)?;
        if ids.is_empty() || ids.len() > 16 {
            return Err(BackendError::InvalidData);
        }
        let ids = ids
            .iter()
            .map(|id| id.as_str().ok_or(BackendError::InvalidData))
            .collect::<Result<Vec<_>>>()?;
        if ids.iter().collect::<BTreeSet<_>>().len() != ids.len() {
            return Err(BackendError::InvalidData);
        }
        // Resolve the selected office team against the currently applied
        // organization snapshot before splitting a multi-member run. The
        // result is copied into every frozen member and the run itself.
        let selected_ids = ids.iter().map(|id| (*id).to_owned()).collect::<Vec<_>>();
        let office = self.resolve_office_selection(input, &selected_ids)?;
        if ids.len() == 1 {
            let mut frozen = self.prepare_one(user, input, id)?;
            frozen.office = office;
            return Ok(frozen);
        }
        let coordinator = input["entry_agent_id"].as_str().unwrap_or(ids[0]);
        if !ids.contains(&coordinator) {
            return Err(BackendError::InvalidData);
        }
        let mut team = BTreeMap::new();
        for aid in &ids {
            let mut one = input.clone();
            one["agent_ids"] = json!([aid]);
            one["entry_agent_id"] = json!(aid);
            let mut member = self.prepare_one(user, &one, &format!("{id}-{aid}"))?;
            member.office = office.clone();
            member.id = id.into();
            member.config.working_directory = self.root.join("runs").join(id).join("workspace");
            member.config.runtime_home = self.root.join("runtime").join(id).join(aid);
            member.config.members[0].is_coordinator = *aid == coordinator;
            member.config.members[0].tools = member.tools.clone();
            member.config.members[0].skills = member.agent["skills"]
                .as_array()
                .cloned()
                .unwrap_or_default();
            team.insert((*aid).to_owned(), member);
        }
        let mut frozen = team
            .get(coordinator)
            .ok_or(BackendError::InvalidData)?
            .clone();
        frozen.config.members = std::iter::once(coordinator)
            .chain(ids.iter().copied().filter(|aid| *aid != coordinator))
            .map(|aid| team[aid].config.members[0].clone())
            .collect();
        frozen
            .config
            .validate()
            .map_err(|_| BackendError::InvalidData)?;
        frozen.team = team;
        frozen.office = office;
        Ok(frozen)
    }

    /// Validate an explicit office-team selection against the human-applied
    /// organization layout. Never silently falls back to all members or a
    /// different team: stale/malformed selections are surfaced as a concrete
    /// configuration error for the caller to fix.
    fn resolve_office_selection(
        &self,
        input: &Value,
        selected_ids: &[String],
    ) -> Result<Option<FrozenOfficeSelection>> {
        let team_value = input.get("office_team_id");
        let layout_value = input.get("office_layout_ref");
        let team_id = match team_value {
            None | Some(Value::Null) => None,
            Some(value) => Some(
                value
                    .as_str()
                    .filter(|id| !id.trim().is_empty())
                    .ok_or(BackendError::InvalidData)?,
            ),
        };
        if team_id.is_none() {
            if !matches!(layout_value, None | Some(Value::Null)) {
                return Err(BackendError::UnsupportedConfiguration(
                    "办公团队与布局版本必须同时提供",
                ));
            }
            return Ok(None);
        }
        let team_id = team_id.ok_or(BackendError::InvalidData)?;
        let supplied_ref = layout_value.filter(|value| !value.is_null()).ok_or(
            BackendError::UnsupportedConfiguration("选择办公团队时必须同时提交已应用的布局版本"),
        )?;
        let c = self.product.connect()?;
        let raw: Option<String> = c
            .query_row(
                "SELECT value FROM platform_config WHERE id='organization_layout'",
                [],
                |row| row.get(0),
            )
            .optional()
            .map_err(db)?;
        let layout: Value = raw
            .map(|value| serde_json::from_str(&value).map_err(|_| BackendError::InvalidData))
            .transpose()?
            .unwrap_or_else(|| json!({"ref":null,"config":{}}));
        let current_ref = layout.get("ref").filter(|value| !value.is_null()).ok_or(
            BackendError::UnsupportedConfiguration(
                "尚未应用组织布局，请先发布并应用包含该办公团队的布局",
            ),
        )?;
        if current_ref != supplied_ref {
            return Err(BackendError::UnsupportedConfiguration(
                "组织布局已更新，办公团队或成员名单已过期；请刷新后重新选择",
            ));
        }
        let config = layout.get("config").and_then(Value::as_object).ok_or(
            BackendError::UnsupportedConfiguration("当前组织布局数据无效，请重新发布布局"),
        )?;
        let teams = config.get("teams").and_then(Value::as_array).ok_or(
            BackendError::UnsupportedConfiguration(
                "当前组织布局未配置办公团队，请先完善并应用布局",
            ),
        )?;
        let team = teams
            .iter()
            .find(|team| team.get("id").and_then(Value::as_str) == Some(team_id))
            .ok_or(BackendError::UnsupportedConfiguration(
                "所选办公团队不在当前已应用布局中，请重新选择团队",
            ))?;
        let member_ids = team
            .get("member_ids")
            .and_then(Value::as_array)
            .ok_or(BackendError::UnsupportedConfiguration(
                "所选办公团队成员名单无效，请重新配置布局",
            ))?
            .iter()
            .map(|id| {
                id.as_str()
                    .map(str::to_owned)
                    .ok_or(BackendError::InvalidData)
            })
            .collect::<Result<Vec<_>>>()?;
        if member_ids.is_empty() {
            return Err(BackendError::UnsupportedConfiguration(
                "所选办公团队尚未配置成员；请先安排有工位的成员",
            ));
        }
        if selected_ids
            .iter()
            .any(|id| !member_ids.iter().any(|member| member == id))
        {
            return Err(BackendError::UnsupportedConfiguration(
                "所选成员不完全属于该办公团队，请按当前团队名单重新选择",
            ));
        }
        let floor_number = team.get("floor").and_then(Value::as_i64).ok_or(
            BackendError::UnsupportedConfiguration("办公团队缺少有效楼层，请重新配置布局"),
        )?;
        let floor = config
            .get("floors")
            .and_then(Value::as_array)
            .and_then(|floors| {
                floors
                    .iter()
                    .find(|floor| floor.get("floor").and_then(Value::as_i64) == Some(floor_number))
            })
            .ok_or(BackendError::UnsupportedConfiguration(
                "办公团队所属楼层不存在，请重新应用有效布局",
            ))?;
        let desks = config.get("desks").and_then(Value::as_array).ok_or(
            BackendError::UnsupportedConfiguration(
                "当前布局未配置正式办公桌，请先为团队成员安排工位",
            ),
        )?;
        for aid in selected_ids {
            let matches = desks
                .iter()
                .filter(|desk| {
                    desk.get("agent_id").and_then(Value::as_str) == Some(aid)
                        && desk.get("team_id").and_then(Value::as_str) == Some(team_id)
                        && desk.get("floor").and_then(Value::as_i64) == Some(floor_number)
                })
                .count();
            if matches != 1 {
                return Err(BackendError::UnsupportedConfiguration(
                    "所选成员必须在当前办公团队拥有唯一有效工位",
                ));
            }
        }
        Ok(Some(FrozenOfficeSelection {
            id: team_id.to_owned(),
            name: team
                .get("name")
                .and_then(Value::as_str)
                .unwrap_or("未命名团队")
                .to_owned(),
            floor: floor_number,
            floor_name: floor
                .get("name")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned(),
            department: floor
                .get("department")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned(),
            layout_ref: current_ref.clone(),
            member_ids,
            selected_agent_ids: selected_ids.to_vec(),
        }))
    }
    fn prepare_one(&self, user: &User, input: &Value, id: &str) -> Result<Frozen> {
        let fields = input.as_object().ok_or(BackendError::InvalidData)?;
        if fields.keys().any(|key| {
            ![
                "execution_strategy",
                "run_project_checks",
                "routing_ref",
                "team_ref",
                "office_team_id",
                "office_layout_ref",
                "workflow_ref",
                "routing_overrides",
                "entry_agent_id",
                "collaboration_flow",
                "orchestration",
                "template_id",
                "template_version",
                "project_id",
                "title",
                "request",
                "agent_ids",
                "runtime",
                "parent_run_id",
                "attachments",
            ]
            .contains(&key.as_str())
        }) {
            return Err(BackendError::InvalidData);
        }
        if input
            .get("execution_strategy")
            .is_some_and(|v| v != "autonomous")
            || input["run_project_checks"] == true
            || input["collaboration_flow"] == true
        {
            return Err(BackendError::Unsupported);
        }
        if let Some(value) = input.get("routing_overrides") {
            if !value
                .as_object()
                .ok_or(BackendError::InvalidData)?
                .is_empty()
            {
                return Err(BackendError::Unsupported);
            }
        }
        if serde_json::to_vec(input)
            .map_err(|_| BackendError::InvalidData)?
            .len()
            > 64 * 1024
        {
            return Err(BackendError::InvalidData);
        }
        let attachments = input
            .get("attachments")
            .map(|value| {
                let list = value.as_array().ok_or(BackendError::InvalidData)?;
                if list.len() > 8 {
                    return Err(BackendError::InvalidData);
                }
                let mut total = 0usize;
                let mut names = BTreeSet::new();
                let mut output = Vec::with_capacity(list.len());
                for item in list {
                    let filename = text(item, "filename")?.to_owned();
                    if filename == "."
                        || filename == ".."
                        || filename.starts_with('.')
                        || filename.contains(['/', '\\', '\0'])
                        || filename.chars().count() > 240
                        || !names.insert(filename.clone())
                    {
                        return Err(BackendError::InvalidData);
                    }
                    let encoded = text(item, "content_base64")?;
                    let bytes = base64::engine::general_purpose::STANDARD
                        .decode(encoded)
                        .map_err(|_| BackendError::InvalidData)?;
                    if bytes.is_empty() || bytes.len() > 8 * 1024 * 1024 {
                        return Err(BackendError::InvalidData);
                    }
                    total = total.saturating_add(bytes.len());
                    if total > 20 * 1024 * 1024 {
                        return Err(BackendError::InvalidData);
                    }
                    let media_type = item
                        .get("media_type")
                        .and_then(Value::as_str)
                        .unwrap_or("application/octet-stream")
                        .to_owned();
                    output.push(RunAttachment {
                        filename,
                        media_type,
                        content_base64: encoded.to_owned(),
                    });
                }
                Ok(output)
            })
            .transpose()?
            .unwrap_or_default();
        let project_id = text(input, "project_id")?;
        let actor = self.product.authorize_project(user, project_id, true)?;
        let title = text(input, "title")?;
        let request = text(input, "request")?;
        if title.chars().count() > 180 || request.len() > 100_000 {
            return Err(BackendError::InvalidData);
        }
        let ids = input["agent_ids"]
            .as_array()
            .ok_or(BackendError::InvalidData)?;
        if ids.len() != 1 {
            return Err(BackendError::Unsupported);
        }
        let aid = ids[0].as_str().ok_or(BackendError::InvalidData)?;
        if input
            .get("entry_agent_id")
            .is_some_and(|v| !v.is_null() && v != aid)
        {
            return Err(BackendError::InvalidData);
        }
        for key in [
            "orchestration",
            "template_id",
            "workflow_ref",
            "routing_ref",
            "team_ref",
            "parent_run_id",
        ] {
            if input.get(key).is_some_and(|v| !v.is_null()) {
                return Err(BackendError::Unsupported);
            }
        }
        let mut c = self.product.connect()?;
        let tx = c.transaction().map_err(db)?;
        let raw: String = tx
            .query_row(
                "SELECT config FROM projects WHERE id=?1",
                [project_id],
                |r| r.get(0),
            )
            .map_err(db)?;
        let project: Value = serde_json::from_str(&raw).map_err(|_| BackendError::InvalidData)?;
        if project["archived"] == true {
            return Err(BackendError::Conflict);
        }
        if project["execution_mode"].as_str().unwrap_or("container") != "local" {
            return Err(BackendError::Unsupported);
        }
        let (raw, version): (String, i64) = tx
            .query_row(
                "SELECT config,version FROM agents WHERE id=?1",
                [aid],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()
            .map_err(db)?
            .ok_or(BackendError::NotFound)?;
        let agent: Value = serde_json::from_str(&raw).map_err(|_| BackendError::InvalidData)?;
        if agent["enabled"] == false {
            return Err(BackendError::Conflict);
        }
        let tools = effective_tools(&agent)?;
        let skills = freeze_skills(&tx, &agent)?;
        let model_id = text(&agent, "model_profile_id")?;
        let (raw, mversion): (String, i64) = tx
            .query_row(
                "SELECT config,version FROM model_profiles WHERE id=?1",
                [model_id],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .map_err(db)?;
        let model: Value = serde_json::from_str(&raw).map_err(|_| BackendError::InvalidData)?;
        if model["enabled"] == false {
            return Err(BackendError::Conflict);
        }
        let pid = text(&model, "provider_id")?;
        let (rid,raw,encrypted):(String,String,String)=tx.query_row("SELECT r.id,r.config,r.encrypted_key FROM providers p JOIN provider_revisions r ON r.id=p.revision_id AND r.provider_id=p.id WHERE p.id=?1 AND p.enabled=1",[pid],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).optional().map_err(db)?.ok_or(BackendError::NotFound)?;
        let connection: Value =
            serde_json::from_str(&raw).map_err(|_| BackendError::InvalidData)?;
        configuration::validate(&tx, &project, &agent, &model, &connection)?;
        let runtime = configuration::resolve_runtime(&tx, &project, &agent, Some(input))?;
        if connection["external"] == true && project["allow_external_models"] != true {
            return Err(BackendError::Forbidden);
        }
        let protocol = text(&connection, "protocol")?;
        if protocol != "openai_responses" && !crate::gateway::GatewayRoute::available() {
            return Err(BackendError::Unsupported);
        }
        let base = text(&connection, "base_url")?;
        let model_name = text(&model, "model").or_else(|_| text(&model, "model_id"))?;
        let headers = crate::product_api::protocols::static_headers(&connection["protocol_config"])
            .map_err(|_| BackendError::UnsupportedConfiguration("Provider headers are invalid"))?;
        // Freeze the complete candidate chain. Parameters are the profile
        // defaults merged with the immutable per-agent overrides, and every
        // candidate must resolve to the same provider revision as the route.
        let overrides = agent
            .get("model_parameters")
            .cloned()
            .unwrap_or_else(|| json!({}));
        let mut candidates = Vec::new();
        let fallback_ids = agent
            .get("fallback_model_ids")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        if !fallback_ids.is_empty() {
            return Err(BackendError::UnsupportedConfiguration(
                "Automatic fallback requires a classified provider rejection",
            ));
        }
        let mut candidate_ids = vec![model_id.to_owned()];
        candidate_ids.extend(
            fallback_ids
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_owned),
        );
        for (index, profile_id) in candidate_ids.iter().enumerate() {
            let (candidate_raw, candidate_version): (String, i64) = tx
                .query_row(
                    "SELECT config,version FROM model_profiles WHERE id=?1",
                    [profile_id],
                    |r| Ok((r.get(0)?, r.get(1)?)),
                )
                .optional()
                .map_err(db)?
                .ok_or(BackendError::NotFound)?;
            let candidate: Value =
                serde_json::from_str(&candidate_raw).map_err(|_| BackendError::InvalidData)?;
            if candidate["enabled"] == false {
                return Err(BackendError::Conflict);
            }
            let candidate_provider = text(&candidate, "provider_id")?;
            if candidate_provider != pid {
                return Err(BackendError::UnsupportedConfiguration(
                    "Fallback models must use the same provider revision",
                ));
            }
            let mut parameters = candidate
                .get("parameters")
                .cloned()
                .unwrap_or_else(|| json!({}));
            let object = parameters
                .as_object_mut()
                .ok_or(BackendError::InvalidData)?;
            for (key, value) in overrides.as_object().ok_or(BackendError::InvalidData)? {
                object.insert(key.clone(), value.clone());
            }
            let allowed = candidate
                .get("allowed_parameters")
                .cloned()
                .unwrap_or_else(|| json!([]));
            if crate::product_api::validate_parameters(protocol, &allowed, &parameters).is_err() {
                return Err(BackendError::UnsupportedConfiguration(
                    "Model parameters are invalid for the selected protocol",
                ));
            }
            model_parameters::apply_thread(&mut json!({"config": {}}), &parameters)?;
            model_parameters::apply_turn(&mut json!({}), &parameters)?;
            let candidate_name =
                text(&candidate, "model").or_else(|_| text(&candidate, "model_id"))?;
            if index > 0 && candidate_name == model_name {
                return Err(BackendError::InvalidData);
            }
            candidates.push(FrozenModel {
                profile_id: profile_id.clone(),
                profile_version: candidate_version,
                model_id: candidate_name.to_owned(),
                parameters,
                provider_id: pid.to_owned(),
                provider_revision_id: rid.clone(),
                protocol: protocol.to_owned(),
                base_url: base.to_owned(),
            });
        }
        let gateway = if crate::gateway::GatewayRoute::available() {
            Some(crate::gateway::GatewayRoute::new(
                self.root.join("gateway").join(id),
                pid,
                model_name,
                protocol,
                headers.clone(),
            )?)
        } else {
            None
        };
        let routed_model = if gateway.is_some() {
            format!("{pid}/{model_name}")
        } else {
            model_name.to_owned()
        };
        let profile = resolve_policy(&tx, &project, aid)?;
        let evidence = format!("model:{model_id}:v{mversion}");
        let credential = format!("provider-revision:{rid}");
        let snapshot_models = candidates
            .iter()
            .map(|candidate| ProviderModelSnapshot {
                model_id: if gateway.is_some() {
                    format!("{pid}/{}", candidate.model_id)
                } else {
                    candidate.model_id.clone()
                },
                upstream_protocol: candidate.protocol.clone(),
                capability_evidence_ref: format!(
                    "model:{}:v{}",
                    candidate.profile_id, candidate.profile_version
                ),
            })
            .collect();
        let mut provider = ProviderSnapshot {
            provider_id: pid.into(),
            provider_revision_id: rid.clone(),
            upstream_base_url: base.into(),
            credential_ref: credential.clone(),
            config_digest: String::new(),
            models: snapshot_models,
        };
        // Include the validated static header map in the frozen digest so a
        // route can never silently change upstream request metadata.
        provider.config_digest = hash(
            &serde_json::to_vec(&json!({
                "provider": provider,
                "headers": headers,
            }))
            .map_err(|_| BackendError::InvalidData)?,
        );
        let route = RouteBinding {
            provider_id: pid.into(),
            provider_revision_id: rid,
            model_id: routed_model,
            upstream_protocol: protocol.into(),
            gateway_route_id: format!("run-{id}"),
            responses_base_url: gateway
                .as_ref()
                .map(|g| format!("http://127.0.0.1:{}/v1", g.port))
                .unwrap_or_else(|| base.into()),
            config_digest: provider.config_digest.clone(),
            credential_ref: credential,
            capability_evidence_ref: evidence,
            gateway_version: if gateway.is_some() {
                "opencodex-pinned"
            } else {
                "direct-responses-v1"
            }
            .into(),
        };
        provider
            .validate_route(&route)
            .map_err(|_| BackendError::InvalidData)?;
        let config = RunConfig {
            project_id: project_id.into(),
            owner_id: actor.id,
            members: vec![RunMember {
                worker_id: aid.into(),
                config_revision: format!("agent:{aid}:v{version}"),
                role: agent["role"].as_str().unwrap_or_default().into(),
                instructions: agent["instructions"].as_str().unwrap_or_default().into(),
                tools: agent
                    .get("tools")
                    .or_else(|| agent.get("tool_permissions"))
                    .and_then(Value::as_array)
                    .map(|v| {
                        v.iter()
                            .filter_map(Value::as_str)
                            .map(str::to_owned)
                            .collect()
                    })
                    .unwrap_or_default(),
                skills: agent["skills"].as_array().cloned().unwrap_or_default(),
                permission_summary: agent.get("tool_permissions").cloned(),
                is_coordinator: true,
                communication_recipients: Vec::new(),
            }],
            permission_profile: profile,
            working_directory: self.root.join("runs").join(id).join("workspace"),
            runtime_home: self.root.join("runtime").join(id),
            runtime_version: "app-server-v2".into(),
            route_binding: route,
        };
        config.validate().map_err(|_| BackendError::InvalidData)?;
        Ok(Frozen {
            id: id.into(),
            title: title.into(),
            request: request.into(),
            config,
            provider,
            agent,
            tools,
            skills,
            project,
            model,
            models: candidates,
            runtime,
            encrypted_key: encrypted,
            provider_headers: headers,
            gateway,
            team: BTreeMap::new(),
            attachments,
            office: None,
        })
    }
    pub(crate) fn preview(&self, user: &User, input: Value) -> Result<Value> {
        let f = self.prepare(user, &input, "preview")?;
        let mut snapshot = f.team_snapshot();
        snapshot["route_binding"] = json!(f.config.route_binding);
        snapshot["permission_profile"] = json!(f.config.permission_profile);
        Ok(snapshot)
    }
    pub(crate) fn create(
        self: &Arc<Self>,
        user: &User,
        input: Value,
        key: Option<String>,
    ) -> Result<String> {
        if !self.enabled() {
            return Err(BackendError::Unsupported);
        }
        let _guard = self
            .creation
            .lock()
            .map_err(|_| BackendError::Unavailable)?;
        let checksum = hash(&serde_json::to_vec(&input).map_err(|_| BackendError::InvalidData)?);
        let id = key
            .map(|key| hash(format!("{}:{key}", user.id).as_bytes())[..32].to_owned())
            .unwrap_or_else(|| Uuid::new_v4().simple().to_string());
        let mut c = self.product.connect()?;
        let tx = c
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(db)?;
        if let Some(old) = tx
            .query_row(
                "SELECT request_hash FROM forge_run_intents WHERE id=?1",
                [&id],
                |r| r.get::<_, String>(0),
            )
            .optional()
            .map_err(db)?
        {
            let f = self.frozen(&id)?;
            self.product
                .authorize_project(user, &f.config.project_id, true)?;
            if old != checksum {
                return Err(BackendError::Conflict);
            }
            drop(tx);
            provision(&mut Store::open(&self.path).map_err(kernel)?, &f)?;
            return Ok(id);
        }
        let f = self.prepare(user, &input, &id)?;
        tx.execute("INSERT INTO forge_run_intents VALUES(?1,?2,?3,?4,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",params![id,user.id,checksum,serde_json::to_string(&f).map_err(|_|BackendError::InvalidData)?]).map_err(db)?;
        tx.commit().map_err(db)?;
        let mut store = Store::open(&self.path).map_err(kernel)?;
        provision(&mut store, &f)?;
        let (sender, receiver) = mpsc::channel(32);
        self.live
            .lock()
            .map_err(|_| BackendError::Unavailable)?
            .insert(id.clone(), sender);
        let service = self.clone();
        self.runtime.spawn(async move {
            service.execute(f, receiver).await;
        });
        Ok(id)
    }

    async fn execute(self: Arc<Self>, f: Frozen, receiver: mpsc::Receiver<LiveCommand>) {
        let Ok(_slot) = self.slots.acquire().await else {
            return;
        };
        if Store::open(&self.path)
            .ok()
            .and_then(|s| s.get_run(&f.id).ok().flatten())
            .is_some_and(|r| r.status == RunStatus::Cancelled)
        {
            if let Ok(mut live) = self.live.lock() {
                live.remove(&f.id);
            }
            return;
        }
        let outcome = self.execute_inner(&f, receiver).await;
        if let Err(error) = &outcome {
            eprintln!("forge run {} failed: {error}", f.id);
        }
        if outcome.is_err() {
            if let Ok(mut store) = Store::open(&self.path) {
                if let Ok(Some(run)) = store.get_run(&f.id) {
                    if matches!(run.status, RunStatus::Starting | RunStatus::Running) {
                        if let Some(e) = run.execution {
                            let _ = change(
                                &mut store,
                                &f.id,
                                RunTransition::MarkOutcomeUnknown {
                                    connection: e.connection,
                                },
                            );
                        }
                    }
                }
            }
        }
        if let Ok(mut live) = self.live.lock() {
            live.remove(&f.id);
        }
    }
    async fn execute_inner(
        &self,
        f: &Frozen,
        mut receiver: mpsc::Receiver<LiveCommand>,
    ) -> Result<()> {
        if !f.team.is_empty() {
            return self.execute_team(f, receiver).await;
        }
        let mut store = Store::open(&self.path).map_err(kernel)?;
        let previous = store
            .get_run(&f.id)
            .map_err(kernel)?
            .ok_or(BackendError::NotFound)?;
        let resuming = previous
            .execution
            .as_ref()
            .and_then(|e| e.thread_id.clone());
        let run = change(
            &mut store,
            &f.id,
            RunTransition::Begin {
                worker_id: f.config.members[0].worker_id.clone(),
                connection_id: Uuid::new_v4().to_string(),
            },
        )?;
        let connection = run.execution.ok_or(BackendError::Conflict)?.connection;
        fs::create_dir_all(
            f.config
                .working_directory
                .parent()
                .ok_or(BackendError::InvalidData)?,
        )
        .map_err(io)?;
        let source = self.root.join("projects").join(&f.config.project_id);
        if resuming.is_none() {
            crate::product_api::workspace::snapshot(&source, &f.config.working_directory)
                .map_err(|_| BackendError::InvalidData)?;
            for args in [
                vec!["init", "--quiet"],
                vec!["add", "--all"],
                vec![
                    "-c",
                    "user.name=Forge",
                    "-c",
                    "user.email=forge@localhost",
                    "-c",
                    "commit.gpgsign=false",
                    "commit",
                    "--allow-empty",
                    "--quiet",
                    "-m",
                    "Run baseline",
                ],
            ] {
                git(&f.config.working_directory, &args)?;
            }
        }
        // Initial attachments are copied into the immutable run workspace
        // before the first model turn. They are never written to the source
        // project directory and remain available on resumed runs.
        materialize_input_attachments(&f.config.working_directory, &f.attachments)?;
        fs::create_dir_all(
            f.config
                .runtime_home
                .parent()
                .ok_or(BackendError::InvalidData)?,
        )
        .map_err(io)?;
        materialize_skills(&f.config.runtime_home, &f.skills)?;
        let mut runtime = RuntimeConfig::new(
            self.codex.clone().ok_or(BackendError::Unsupported)?,
            f.config.working_directory.clone(),
            f.config.runtime_home.clone(),
        );
        let key = self.product.decrypt_secret(&f.encrypted_key)?;
        let gateway = if let Some(route) = &f.gateway {
            Some(route.start(&f.provider.upstream_base_url, &key).await?)
        } else {
            None
        };
        if let Some(gateway) = &gateway {
            runtime
                .forge_env
                .insert("FORGE_GATEWAY_TOKEN".into(), gateway.token.clone());
        } else {
            runtime.forge_env.insert("FORGE_RUN_API_KEY".into(), key);
        }
        let (client, _) =
            CodexClient::start(runtime, Duration::from_secs(f.runtime.call_timeout_s))
                .await
                .map_err(|_| BackendError::Unavailable)?;
        let result = self
            .drive(&mut store, f, &client, &connection, &mut receiver, resuming)
            .await;
        client.close().await;
        if let Some(gateway) = gateway {
            gateway.close().await;
        }
        result
    }
    async fn drive(
        &self,
        store: &mut Store,
        f: &Frozen,
        client: &CodexClient,
        connection: &forge_core::RunConnection,
        receiver: &mut mpsc::Receiver<LiveCommand>,
        resume_thread: Option<String>,
    ) -> Result<()> {
        let deadline = Duration::from_secs(f.runtime.call_timeout_s);
        revalidate_skills(&self.product.connect()?, &f.skills)?;
        let policy = f.config.permission_profile.execution_policy();
        let tools = if f.tools.is_empty() {
            effective_tools(&f.agent)?
        } else {
            f.tools.clone()
        };
        let mut params = json!({"cwd":f.config.working_directory,"model":f.config.route_binding.model_id,"modelProvider":"forge","approvalPolicy":policy.approval_policy,"approvalsReviewer":policy.approvals_reviewer,"sandbox":policy.sandbox,"config":{"model_providers.forge":{"name":"Forge","base_url":f.config.route_binding.responses_base_url,"env_key":"FORGE_RUN_API_KEY","wire_api":"responses","requires_openai_auth":false,"supports_websockets":false},"analytics.enabled":false,"allow_login_shell":false,"agents.enabled":false,"features.multi_agent_v2":false}});
        let mut developer_instructions = format!(
            "This is a Forge-owned run. You are agent {} ({}). Authorized Forge tools: {}. Use only these team capabilities; never claim access to tools not listed.",
            f.agent["name"]
                .as_str()
                .unwrap_or(f.config.members[0].worker_id.as_str()),
            f.agent["role"].as_str().unwrap_or("agent"),
            tools.join(", "),
        );
        developer_instructions.push_str(&skill_instructions(&f.config.runtime_home, &f.skills));
        params["developerInstructions"] = json!(developer_instructions);
        if f.gateway.is_none() && !f.provider_headers.is_empty() {
            params["config"]["model_providers.forge"]["http_headers"] = json!(f.provider_headers);
        }
        let candidates = if f.models.is_empty() {
            vec![FrozenModel {
                profile_id: String::new(),
                profile_version: 0,
                model_id: f.config.route_binding.model_id.clone(),
                parameters: f
                    .model
                    .get("parameters")
                    .cloned()
                    .unwrap_or_else(|| json!({})),
                provider_id: f.provider.provider_id.clone(),
                provider_revision_id: f.provider.provider_revision_id.clone(),
                protocol: f.config.route_binding.upstream_protocol.clone(),
                base_url: f.provider.upstream_base_url.clone(),
            }]
        } else {
            f.models.clone()
        };
        if candidates.len() != 1 {
            return Err(BackendError::UnsupportedConfiguration(
                "Automatic fallback requires a classified provider rejection",
            ));
        }
        let can_modify_workspace = tools
            .iter()
            .any(|tool| ["write_file", "delete_file", "run_command"].contains(&tool.as_str()));
        if matches!(f.agent["role"].as_str(), Some("planner" | "reviewer")) || !can_modify_workspace
        {
            params["sandbox"] = json!("read-only");
            params["approvalPolicy"] = json!("never");
            params["config"]["web_search"] = json!("disabled");
        }
        if f.gateway.is_some() {
            let provider = &mut params["config"]["model_providers.forge"];
            provider["env_key"] = json!("FORGE_GATEWAY_TOKEN");
            provider["env_http_headers"] = json!({"x-opencodex-api-key":"FORGE_GATEWAY_TOKEN"});
        }
        if let Some(id) = &resume_thread {
            params["threadId"] = json!(id);
        }
        let previous_calls: u64 = self
            .product
            .connect()?
            .query_row(
                "SELECT count(*) FROM forge_runtime_messages WHERE run_id=?1 AND kind='turn/started'",
                [&f.id],
                |r| r.get(0),
            )
            .map_err(db)?;
        if previous_calls >= f.runtime.max_model_calls {
            return Err(BackendError::UnsupportedConfiguration(
                "Rust run model call budget exhausted",
            ));
        }
        let method = if resume_thread.is_some() {
            "thread/resume"
        } else {
            "thread/start"
        };
        let candidate = &candidates[0];
        params["model"] = json!(if f.gateway.is_some() {
            gateway_model_id(&candidate.provider_id, &candidate.model_id)
        } else {
            candidate.model_id.clone()
        });
        model_parameters::apply_thread(&mut params, &candidate.parameters)?;
        let thread = client
            .request(method, params, deadline)
            .await
            .map_err(|_| BackendError::Unavailable)?;
        let thread_id = thread
            .pointer("/thread/id")
            .and_then(Value::as_str)
            .ok_or(BackendError::InvalidData)?
            .to_owned();
        change(
            store,
            &f.id,
            RunTransition::BindThread {
                connection: connection.clone(),
                thread_id: thread_id.clone(),
            },
        )?;
        let mut prompt = format!(
            "{}\n\n{}\n\n{}",
            f.project["instructions"].as_str().unwrap_or(""),
            f.agent["instructions"].as_str().unwrap_or(""),
            f.request
        );
        let pending = {
            let c = self.product.connect()?;
            let mut q = c.prepare("SELECT i.id,i.body FROM human_instructions i WHERE i.run_id=?1 AND NOT EXISTS(SELECT 1 FROM instruction_receipts r WHERE r.instruction_id=i.id) AND NOT EXISTS(SELECT 1 FROM outbox o WHERE o.run_id=i.run_id AND o.sent=0 AND o.attempts>0 AND json_extract(o.payload,'$.instruction_id')=i.id) ORDER BY i.created_at,i.id").map_err(db)?;
            let items = q
                .query_map([&f.id], |r| {
                    Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
                })
                .map_err(db)?
                .collect::<std::result::Result<Vec<_>, _>>()
                .map_err(db)?;
            items
        };
        if resume_thread.is_some() {
            prompt = "Continue this task from the existing workspace and thread. Inspect the current state before repeating any operation.".into();
        }
        for (_, body) in &pending {
            prompt.push_str("\n\nHuman instruction:\n");
            prompt.push_str(body);
        }
        if prompt.chars().count() > f.runtime.max_context_chars as usize {
            return Err(BackendError::UnsupportedConfiguration(
                "Run prompt exceeds the frozen context limit",
            ));
        }
        let mut request = json!({
            "threadId": thread_id,
            "input": turn_inputs(&f.config.runtime_home, &f.skills, &prompt),
            "model": if f.gateway.is_some() {
                gateway_model_id(&candidate.provider_id, &candidate.model_id)
            } else {
                candidate.model_id.clone()
            },
        });
        model_parameters::apply_turn(&mut request, &candidate.parameters)?;
        let turn = client
            .request("turn/start", request, deadline)
            .await
            .map_err(|_| BackendError::Unavailable)?;
        let turn_id = turn
            .pointer("/turn/id")
            .and_then(Value::as_str)
            .ok_or(BackendError::InvalidData)?
            .to_owned();
        for (id, _) in pending {
            self.product.connect()?.execute("INSERT OR IGNORE INTO instruction_receipts VALUES(?1,?2,?3,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",params![format!("{id}:codex"),&id,format!("{}:codex",f.id)]).map_err(db)?;
            self.product.connect()?.execute("UPDATE outbox SET sent=1,error='' WHERE run_id=?1 AND sent=0 AND json_extract(payload,'$.instruction_id')=?2", params![&f.id, &id]).map_err(db)?;
        }
        let run = change(
            store,
            &f.id,
            RunTransition::BindTurn {
                connection: connection.clone(),
                turn_id: turn_id.clone(),
            },
        )?;
        let execution = run.execution.ok_or(BackendError::Conflict)?;
        let task_id = format!("{}:codex", f.id);
        let task = store
            .get_task(&task_id)
            .map_err(kernel)?
            .ok_or(BackendError::NotFound)?;
        let claim = store
            .execute(
                &format!("{}:claim:{}", f.id, connection.generation),
                Command::ClaimTask {
                    task_id: format!("{}:codex", f.id),
                    worker_id: connection.worker_id.clone(),
                    expected_version: task.version,
                    lease_duration_ms: 120_000,
                },
            )
            .map_err(kernel)?;
        let claim = match claim.result {
            CommandResult::Task(task) => task.claim.ok_or(BackendError::Conflict)?,
            _ => return Err(BackendError::Conflict),
        };
        let mut last_renewal = tokio::time::Instant::now();
        let mut sequence = 0;
        let mut answer = String::new();
        let mut pending_control: Option<LiveCommand> = None;
        let end = tokio::time::Instant::now() + Duration::from_secs(f.runtime.task_timeout_s);
        let mut active_commands = HashMap::new();
        loop {
            if tokio::time::Instant::now() > end
                || active_commands
                    .values()
                    .any(|deadline| tokio::time::Instant::now() > *deadline)
            {
                return Err(BackendError::Unavailable);
            }
            if last_renewal.elapsed() > Duration::from_secs(30) {
                sequence += 1;
                store
                    .execute(
                        &format!("{}:renew:{}:{sequence}", f.id, connection.generation),
                        Command::RenewClaim {
                            task_id: format!("{}:codex", f.id),
                            claim_token: claim.token.clone(),
                            attempt_generation: claim.generation,
                            lease_duration_ms: 120_000,
                        },
                    )
                    .map_err(kernel)?;
                last_renewal = tokio::time::Instant::now();
            }
            if let Ok(command) = receiver.try_recv() {
                if self
                    .product
                    .authorize_project(&command.actor, &f.config.project_id, true)
                    .is_err()
                {
                    let _ = command.reply.send(Err(BackendError::Forbidden));
                    continue;
                }
                match command.input.action {
                    ControlAction::Steer => {
                        let result=client.request("turn/steer",json!({"threadId":thread_id,"expectedTurnId":turn_id,"input":[{"type":"text","text":command.input.prompt,"text_elements":[]}]}),deadline).await.map(|_|json!({"accepted":true,"status":"running"})).map_err(|_|BackendError::Conflict);
                        let _ = command.reply.send(result);
                    }
                    ControlAction::Pause | ControlAction::Cancel => {
                        if pending_control.is_some() {
                            let _ = command.reply.send(Err(BackendError::Conflict));
                        } else if client
                            .request(
                                "turn/interrupt",
                                json!({"threadId":thread_id,"turnId":turn_id}),
                                deadline,
                            )
                            .await
                            .is_ok()
                        {
                            pending_control = Some(command);
                        } else {
                            let _ = command.reply.send(Err(BackendError::Conflict));
                        }
                    }
                    _ => {
                        let _ = command.reply.send(Err(BackendError::Unsupported));
                    }
                }
            }
            // Product decisions and ordinary answers are durable outbox rows.
            // Consume them from the live turn and acknowledge only after the
            // Codex transport confirms the response. Attempted writes remain
            // durable for reconciliation and are not automatically replayed.
            self.consume_product_outbox(f, client, &thread_id, &turn_id, deadline)
                .await?;
            let event = match client.next_event(Duration::from_millis(100)).await {
                Ok(e) => e,
                Err(forge_runtime::RuntimeError::EventTimeout) => continue,
                Err(_) => return Err(BackendError::Unavailable),
            };
            if event.thread_id.as_ref().is_some_and(|id| id != &thread_id)
                || event.turn_id.as_ref().is_some_and(|id| id != &turn_id)
            {
                return Err(BackendError::Conflict);
            }
            if event.payload.pointer("/item/type").and_then(Value::as_str)
                == Some("commandExecution")
            {
                if let Some(item_id) = &event.item_id {
                    if event.kind == "item/started" {
                        active_commands.insert(
                            item_id.clone(),
                            tokio::time::Instant::now()
                                + Duration::from_secs(f.runtime.command_timeout_s),
                        );
                    } else if event.kind == "item/completed" {
                        active_commands.remove(item_id);
                    }
                }
            }
            if matches!(
                event.kind.as_str(),
                "item/agentMessage/delta"
                    | "item/commandExecution/outputDelta"
                    | "item/plan/delta"
                    | "item/reasoning/summaryTextDelta"
                    | "item/completed"
                    | "item/started"
                    | "turn/completed"
                    | "turn/started"
            ) {
                let mut payload = crate::python_sqlite::redact(event.payload.clone());
                if let Some(object) = payload.as_object_mut() {
                    object.insert("_forge_task_key".into(), json!(task_id));
                }
                self.product.connect()?.execute("INSERT INTO forge_runtime_messages VALUES(?1,?2,?3,?4,?5,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",params![f.id,connection.connection_id,event.sequence,event.kind,payload.to_string()]).map_err(db)?;
            }
            store
                .execute(
                    &format!(
                        "{}:event:{}:{}",
                        f.id, connection.generation, event.sequence
                    ),
                    Command::RecordRuntimeEvent {
                        run_id: f.id.clone(),
                        connection: connection.clone(),
                        sequence: event.sequence,
                        thread_id: event.thread_id.clone(),
                        turn_id: event.turn_id.clone(),
                        item_id: event.item_id.clone(),
                        kind: event.kind.clone(),
                        terminal_status: event.terminal_status().map(|s| {
                            match s {
                                TurnTerminalStatus::Completed => "completed",
                                TurnTerminalStatus::Interrupted => "interrupted",
                                TurnTerminalStatus::Failed => "failed",
                            }
                            .into()
                        }),
                    },
                )
                .map_err(kernel)?;
            self.notify_events();
            if let Some(request_id) = &event.request_id {
                if let Some(item_id) = &event.item_id {
                    active_commands.remove(item_id);
                }
                let kind = match event.kind.as_str() {
                    "item/commandExecution/requestApproval" => ApprovalKind::CommandExecution,
                    "item/fileChange/requestApproval" => ApprovalKind::FileChange,
                    "item/permissions/requestApproval" => ApprovalKind::Permissions,
                    _ => return Err(BackendError::Unsupported),
                };
                let id = format!(
                    "{}:approval:{}:{}",
                    f.id, connection.generation, event.sequence
                );
                store
                    .execute(
                        &format!("{id}:record"),
                        Command::RecordApproval {
                            approval_id: id.clone(),
                            request: Box::new(ApprovalRequest {
                                run_id: f.id.clone(),
                                execution: execution.clone(),
                                request_id: serde_json::from_value(
                                    serde_json::to_value(request_id)
                                        .map_err(|_| BackendError::InvalidData)?,
                                )
                                .map_err(|_| BackendError::InvalidData)?,
                                item_id: event.item_id.clone().ok_or(BackendError::InvalidData)?,
                                kind,
                                payload: event.payload.clone(),
                            }),
                        },
                    )
                    .map_err(kernel)?;
                let queued = loop {
                    let approval = store
                        .get_approval(&id)
                        .map_err(kernel)?
                        .ok_or(BackendError::Conflict)?;
                    if approval.status == ApprovalStatus::Queued {
                        break approval;
                    }
                    if approval.status != ApprovalStatus::Pending
                        || tokio::time::Instant::now() > end
                    {
                        return Err(BackendError::Conflict);
                    }
                    if last_renewal.elapsed() > Duration::from_secs(30) {
                        sequence += 1;
                        store
                            .execute(
                                &format!("{}:renew:{}:{sequence}", f.id, connection.generation),
                                Command::RenewClaim {
                                    task_id: task_id.clone(),
                                    claim_token: claim.token.clone(),
                                    attempt_generation: claim.generation,
                                    lease_duration_ms: 120_000,
                                },
                            )
                            .map_err(kernel)?;
                        last_renewal = tokio::time::Instant::now();
                    }
                    if let Ok(command) = receiver.try_recv() {
                        if self
                            .product
                            .authorize_project(&command.actor, &f.config.project_id, true)
                            .is_err()
                        {
                            let _ = command.reply.send(Err(BackendError::Forbidden));
                            continue;
                        }
                        if matches!(
                            command.input.action,
                            ControlAction::Pause | ControlAction::Cancel
                        ) {
                            let acknowledged = client
                                .request(
                                    "turn/interrupt",
                                    json!({"threadId":thread_id,"turnId":turn_id}),
                                    deadline,
                                )
                                .await
                                .is_ok();
                            let interrupted = acknowledged
                                && tokio::time::timeout(deadline, async {
                                    loop {
                                        let event = client
                                            .next_event(deadline)
                                            .await
                                            .map_err(|_| BackendError::Unavailable)?;
                                        if event.thread_id.as_deref() == Some(&thread_id)
                                            && event.turn_id.as_deref() == Some(&turn_id)
                                        {
                                            if let Some(status) = event.terminal_status() {
                                                return Ok::<_, BackendError>(
                                                    status == TurnTerminalStatus::Interrupted,
                                                );
                                            }
                                        }
                                    }
                                })
                                .await
                                .is_ok_and(|result| matches!(result, Ok(true)));
                            client.close().await;
                            if interrupted {
                                let cancel = matches!(command.input.action, ControlAction::Cancel);
                                self.stop_run(store, &f.id, connection, &claim, cancel)?;
                                let _=command.reply.send(Ok(json!({"accepted":true,"status":if cancel{"cancelled"}else{"paused"}})));
                                return Ok(());
                            }
                            let _ = command.reply.send(Err(BackendError::Unavailable));
                            return Err(BackendError::Unavailable);
                        }
                        let _ = command.reply.send(Err(BackendError::Conflict));
                    }
                    tokio::time::sleep(Duration::from_millis(100)).await;
                };
                let actor = self
                    .product
                    .user(
                        &queued
                            .decision
                            .as_ref()
                            .ok_or(BackendError::Conflict)?
                            .actor_id,
                    )?
                    .ok_or(BackendError::Forbidden)?;
                self.product
                    .authorize_project(&actor, &f.config.project_id, true)?;
                store
                    .execute(
                        &format!("{id}:dispatch"),
                        Command::BeginApprovalDispatch {
                            approval_id: id.clone(),
                            expected_version: queued.version,
                            execution: execution.clone(),
                        },
                    )
                    .map_err(kernel)?;
                let dispatch = store
                    .get_approval(&id)
                    .map_err(kernel)?
                    .ok_or(BackendError::Conflict)?;
                let delivered = client
                    .respond(
                        request_id,
                        dispatch.response().map_err(|_| BackendError::Conflict)?,
                        deadline,
                    )
                    .await
                    .is_ok();
                store
                    .execute(
                        &format!("{id}:finish"),
                        Command::FinishApprovalDispatch {
                            approval_id: id,
                            expected_version: dispatch.version,
                            execution: execution.clone(),
                            delivered,
                        },
                    )
                    .map_err(kernel)?;
                if !delivered {
                    return Err(BackendError::Unavailable);
                }
                if event.kind == "item/commandExecution/requestApproval" {
                    if let Some(item_id) = &event.item_id {
                        active_commands.insert(
                            item_id.clone(),
                            tokio::time::Instant::now()
                                + Duration::from_secs(f.runtime.command_timeout_s),
                        );
                    }
                }
            }
            if event.kind == "item/agentMessage/delta" {
                if let Some(delta) = event.payload["delta"].as_str() {
                    if answer.len() + delta.len() > f.runtime.max_file_bytes as usize {
                        return Err(BackendError::InvalidData);
                    }
                    answer.push_str(delta);
                }
            }
            if event.kind == "item/completed"
                && event.payload.pointer("/item/type").and_then(Value::as_str)
                    == Some("agentMessage")
            {
                if let Some(full) = event.payload.pointer("/item/text").and_then(Value::as_str) {
                    if full.len() > f.runtime.max_file_bytes as usize {
                        return Err(BackendError::InvalidData);
                    }
                    answer = full.into();
                }
            }
            if let Some(status) = event.terminal_status() {
                if let Some(command) = pending_control.take() {
                    if status == TurnTerminalStatus::Interrupted {
                        client.close().await;
                        let cancel = matches!(command.input.action, ControlAction::Cancel);
                        self.stop_run(store, &f.id, connection, &claim, cancel)?;
                        let _ = command.reply.send(Ok(
                            json!({"accepted":true,"status":if cancel{"cancelled"}else{"paused"}}),
                        ));
                        return Ok(());
                    }
                    let _ = command.reply.send(Err(BackendError::Conflict));
                }
                if status == TurnTerminalStatus::Interrupted {
                    client.close().await;
                    self.stop_run(store, &f.id, connection, &claim, false)?;
                    return Ok(());
                }
                if status != TurnTerminalStatus::Completed {
                    return Err(BackendError::Conflict);
                }
                break;
            }
        }
        let artifacts = self.root.join("runs").join(&f.id).join("artifacts");
        fs::create_dir_all(&artifacts).map_err(io)?;
        let report = format!("# {}\n\n{}\n", f.title, answer);
        git(
            &f.config.working_directory,
            &["add", "--intent-to-add", "--all"],
        )?;
        let patch = git(
            &f.config.working_directory,
            &["diff", "--no-ext-diff", "--no-textconv", "--binary", "HEAD"],
        )?;
        let source = archive_workspace(&f.config.working_directory)?;
        if [report.len(), patch.len(), source.len()]
            .into_iter()
            .any(|len| len as u64 > queries::artifacts::MAX_BYTES)
        {
            return Err(BackendError::InvalidData);
        }
        fs::write(artifacts.join("discussion.md"), report.as_bytes()).map_err(io)?;
        fs::write(artifacts.join("changes.patch"), &patch).map_err(io)?;
        fs::write(artifacts.join("source.zip"), &source).map_err(io)?;
        let manifest = json!({"discussion.md":hash(report.as_bytes()),"changes.patch":hash(&patch),"source.zip":hash(&source)});
        let result = json!({"summary":answer,"mode":"autonomous","checks":{"verification":"not_requested"},"artifacts":manifest,"artifact":"discussion.md","artifact_sha256":hash(&serde_json::to_vec(&manifest).map_err(|_|BackendError::InvalidData)?),"thread_id":thread_id,"turn_id":turn_id,"runtime":"codex"});
        let result_bytes =
            serde_json::to_vec_pretty(&result).map_err(|_| BackendError::InvalidData)?;
        if result_bytes.len() as u64 > queries::artifacts::MAX_BYTES {
            return Err(BackendError::InvalidData);
        }
        fs::write(artifacts.join("report.json"), &result_bytes).map_err(io)?;
        fs::write(artifacts.join("discussion.json"), &result_bytes).map_err(io)?;
        store
            .execute(
                &format!("{}:submit-task:{}", f.id, connection.generation),
                Command::SubmitTask {
                    task_id: format!("{}:codex", f.id),
                    claim_token: claim.token,
                    attempt_generation: claim.generation,
                    result: result.clone(),
                },
            )
            .map_err(kernel)?;
        change(store, &f.id, RunTransition::Submit { execution, result })?;
        Ok(())
    }

    async fn consume_product_outbox(
        &self,
        f: &Frozen,
        client: &CodexClient,
        thread_id: &str,
        turn_id: &str,
        deadline: Duration,
    ) -> Result<()> {
        let messages = self.product.pending_outbox(&f.id, 32)?;
        for message in messages {
            let payload = message.payload.clone();
            let result = match message.action.as_str() {
                // `respond` belongs to the legacy approval dispatcher. Rust
                // Codex approvals use the kernel approval ledger directly;
                // leave those rows for their owning consumer.
                "respond" => continue,
                // Some product callers use an explicit answer action for an
                // ordinary assistant reply. Deliver it as a steer on the
                // active turn; this keeps the answer visible in the same
                // Codex thread and does not create a second model turn.
                "answer" | "assistant.answer" => {
                    if message.attempts > 0 {
                        continue;
                    }
                    let Some(text) = payload
                        .get("text")
                        .or_else(|| payload.get("content"))
                        .and_then(Value::as_str)
                        .filter(|text| !text.trim().is_empty())
                    else {
                        self.product
                            .note_outbox_attempt(&message.id, "answer payload missing text")?;
                        continue;
                    };
                    self.product
                        .note_outbox_attempt(&message.id, "delivery_in_progress")?;
                    client
                        .request(
                            "turn/steer",
                            json!({
                                "threadId": thread_id,
                                "expectedTurnId": turn_id,
                                "input": [{"type":"text","text":text,"text_elements":[]}]
                            }),
                            deadline,
                        )
                        .await
                        .map(|_| ())
                }
                _ => {
                    self.product
                        .note_outbox_attempt(&message.id, "unsupported product outbox action")?;
                    continue;
                }
            };
            match result {
                Ok(()) => {
                    let mut product = self.product.connect()?;
                    let tx = product
                        .transaction_with_behavior(TransactionBehavior::Immediate)
                        .map_err(db)?;
                    if message.action == "answer" || message.action == "assistant.answer" {
                        if let Some(instruction_id) = message.payload["instruction_id"].as_str() {
                            tx.execute("INSERT OR IGNORE INTO instruction_receipts VALUES(?1,?2,?3,strftime('%Y-%m-%dT%H:%M:%fZ','now'))", params![format!("{instruction_id}:codex"), instruction_id, format!("{}:codex", f.id)]).map_err(db)?;
                        }
                    }
                    tx.execute(
                        "UPDATE outbox SET sent=1,error='' WHERE id=?1 AND sent=0",
                        [&message.id],
                    )
                    .map_err(db)?;
                    tx.commit().map_err(db)?;
                }
                Err(error) => {
                    self.product
                        .note_outbox_attempt(&message.id, &format!("{error}"))?;
                    if message.action == "answer" || message.action == "assistant.answer" {
                        continue;
                    }
                    // Unknown callbacks are permanent for this turn. Keep the
                    // row durable and let recovery/reporting surface it.
                    if matches!(error, forge_runtime::RuntimeError::UnknownServerRequest) {
                        continue;
                    }
                    return Err(BackendError::Unavailable);
                }
            }
        }
        Ok(())
    }
    fn stop_run(
        &self,
        store: &mut Store,
        id: &str,
        connection: &forge_core::RunConnection,
        claim: &forge_core::Claim,
        cancel: bool,
    ) -> Result<()> {
        store
            .execute(
                &format!("{id}:release:{}", connection.generation),
                Command::ReleaseTask {
                    task_id: format!("{id}:codex"),
                    claim_token: claim.token.clone(),
                    attempt_generation: claim.generation,
                },
            )
            .map_err(kernel)?;
        change(
            store,
            id,
            RunTransition::Pause {
                connection: connection.clone(),
                evidence_ref: "codex-interrupted-and-closed".into(),
            },
        )?;
        if cancel {
            change(
                store,
                id,
                RunTransition::Cancel {
                    evidence_ref: "human-cancel-after-runtime-stop".into(),
                },
            )?;
        }
        Ok(())
    }
    pub(crate) fn decide(
        &self,
        user: &User,
        id: &str,
        phase: &str,
        input: RunDecisionInput,
    ) -> Result<Value> {
        let f = self.frozen(id)?;
        self.product
            .authorize_project(user, &f.config.project_id, true)?;
        if !f.team.is_empty() && !input.approved {
            return Err(BackendError::UnsupportedConfiguration(
                "Returning a team delivery requires explicit team recovery, which is not yet supported",
            ));
        }
        if phase != "acceptance" {
            return Err(BackendError::Conflict);
        }
        let mut store = Store::open(&self.path).map_err(kernel)?;
        let run = store
            .get_run(id)
            .map_err(kernel)?
            .ok_or(BackendError::NotFound)?;
        if run
            .result
            .as_ref()
            .and_then(|r| r["artifact_sha256"].as_str())
            != Some(&input.digest)
        {
            return Err(BackendError::Conflict);
        }
        let manifest = run
            .result
            .as_ref()
            .and_then(|r| r["artifacts"].as_object())
            .ok_or(BackendError::Conflict)?;
        for (name, expected) in manifest {
            let bytes =
                queries::artifacts::read(&self.root.join("runs").join(id).join("artifacts"), name)?;
            if expected.as_str() != Some(&hash(&bytes)) {
                return Err(BackendError::Conflict);
            }
        }
        let evidence = format!("human:{}:{}", user.id, input.digest);
        if !input.approved {
            if !matches!(run.status, RunStatus::Submitted | RunStatus::Integrated) {
                return Err(BackendError::Conflict);
            }
            change(
                &mut store,
                id,
                RunTransition::Reopen {
                    evidence_ref: evidence.clone(),
                },
            )?;
            let task_id = format!("{id}:codex");
            let task = store
                .get_task(&task_id)
                .map_err(kernel)?
                .ok_or(BackendError::NotFound)?;
            store
                .execute(
                    &format!("{id}:reopen-task:{}", task.version),
                    Command::ReopenTask {
                        task_id,
                        expected_version: task.version,
                        evidence_ref: evidence,
                    },
                )
                .map_err(kernel)?;
            if !input.note.trim().is_empty() {
                self.product.connect()?.execute("INSERT INTO human_instructions VALUES(?1,?2,?3,'[]',?4,strftime('%Y-%m-%dT%H:%M:%fZ','now'))", params![Uuid::new_v4().simple().to_string(),id,user.id,input.note]).map_err(db)?;
            }
            return Ok(json!({"accepted":true,"run_id":id,"status":"paused"}));
        }
        if run.status == RunStatus::Submitted {
            change(
                &mut store,
                id,
                RunTransition::Integrate {
                    evidence_ref: evidence.clone(),
                },
            )?;
        }
        if run.status != RunStatus::Accepted {
            change(
                &mut store,
                id,
                RunTransition::Accept {
                    evidence_ref: evidence,
                },
            )?;
        }
        Ok(json!({"accepted":true,"run_id":id,"status":"completed"}))
    }
    pub(crate) fn product_request(
        &self,
        user: &User,
        method: &str,
        path: &str,
        _query: &BTreeMap<String, String>,
        _body: &[u8],
    ) -> std::result::Result<Option<ProductReply>, ProductError> {
        if let Some(reply) = self.query_request(user, method, path, _query, _body)? {
            return Ok(Some(reply));
        }
        let parts: Vec<_> = path.trim_matches('/').split('/').collect();
        if let ["api", "runs", id, "artifacts", filename] = parts.as_slice() {
            if method != "GET" {
                return Err(ProductError {
                    status: 405,
                    detail: "Method Not Allowed".into(),
                });
            }
            self.access_id(user, id, false)?;
            let bytes = queries::artifacts::read(
                &self.root.join("runs").join(id).join("artifacts"),
                filename,
            )?;
            return Ok(Some(ProductReply {
                status: 200,
                body: Value::Null,
                download: Some(ProductDownload {
                    bytes,
                    filename: filename.to_string(),
                    content_type: "application/octet-stream",
                }),
            }));
        }
        if path == "/api/runtime/status" && method == "GET" {
            return Ok(Some(ProductReply::json(
                200,
                json!({"execution_engine":"codex","owner":"rust","available":self.enabled(),"task_execution_enabled":self.enabled(),"default_scheduler_enabled":self.enabled(),"active_runs":self.live.lock().map_err(|_|BackendError::Unavailable)?.len()}),
            )));
        }
        Ok(None)
    }
}
impl RunController for RunService {
    fn control(&self, _actor: &User, run: &Run, input: RunControl) -> Result<Value> {
        if matches!(input.action, ControlAction::Cancel)
            && matches!(run.status, RunStatus::Pending | RunStatus::Paused)
        {
            let mut store = Store::open(&self.path).map_err(kernel)?;
            change(
                &mut store,
                &run.id,
                RunTransition::Cancel {
                    evidence_ref: format!("human:{}", _actor.id),
                },
            )?;
            return Ok(json!({"accepted":true,"status":"cancelled"}));
        }
        let team_steer = matches!(input.action, ControlAction::Steer)
            && run
                .config
                .as_ref()
                .is_some_and(|config| config.members.len() > 1);
        if team_steer {
            if input.agent_id.as_ref().is_some_and(|id| {
                run.config
                    .as_ref()
                    .is_none_or(|config| !config.permits_worker(id))
            }) {
                return Err(BackendError::NotFound);
            }
            if let Some(key) = &input.task_key {
                let assigned: Option<String> = self
                    .product
                    .connect()?
                    .query_row(
                        "SELECT agent_id FROM tasks WHERE run_id=?1 AND key=?2",
                        params![run.id, key],
                        |row| row.get(0),
                    )
                    .optional()
                    .map_err(db)?;
                let assigned = assigned.ok_or(BackendError::NotFound)?;
                if input.agent_id.as_ref().is_some_and(|id| id != &assigned) {
                    return Err(BackendError::Conflict);
                }
            }
        }
        if !team_steer && input.task_key.as_ref().is_some_and(|key| key != "codex") {
            return Err(BackendError::NotFound);
        }
        if !team_steer
            && input.agent_id.as_ref().is_some_and(|agent| {
                run.config
                    .as_ref()
                    .is_none_or(|c| c.members.first().is_none_or(|m| m.worker_id != *agent))
            })
        {
            return Err(BackendError::Unsupported);
        }
        if matches!(
            input.action,
            ControlAction::Resume
                | ControlAction::Continue
                | ControlAction::Retry
                | ControlAction::Reassign
        ) {
            if !matches!(run.status, RunStatus::Pending | RunStatus::Paused) {
                return Err(BackendError::Conflict);
            }
            let f = self.frozen(&run.id)?;
            let mut live = self.live.lock().map_err(|_| BackendError::Unavailable)?;
            if live.contains_key(&run.id) {
                return Err(BackendError::Conflict);
            }
            let mut store = Store::open(&self.path).map_err(kernel)?;
            let task_id = format!("{}:codex", run.id);
            let task = store
                .get_task(&task_id)
                .map_err(kernel)?
                .ok_or(BackendError::NotFound)?;
            if task.status == forge_core::TaskStatus::Submitted {
                store
                    .execute(
                        &format!("{}:reopen-task:{}", run.id, task.version),
                        Command::ReopenTask {
                            task_id,
                            expected_version: task.version,
                            evidence_ref: format!("human:{}:resume-after-return", _actor.id),
                        },
                    )
                    .map_err(kernel)?;
            }
            if !input.reason.trim().is_empty() {
                self.product.connect()?.execute("INSERT INTO human_instructions VALUES(?1,?2,?3,'[]',?4,strftime('%Y-%m-%dT%H:%M:%fZ','now'))", params![Uuid::new_v4().simple().to_string(),run.id,_actor.id,input.reason]).map_err(db)?;
            }
            let (sender, receiver) = mpsc::channel(32);
            live.insert(run.id.clone(), sender);
            let service = self.this.upgrade().ok_or(BackendError::Unavailable)?;
            self.runtime.spawn(async move {
                service.execute(f, receiver).await;
            });
            return Ok(json!({"accepted":true,"status":"queued"}));
        }
        if matches!(input.action, ControlAction::Steer)
            && input.prompt.as_ref().is_none_or(|p| p.trim().is_empty())
        {
            return Err(BackendError::InvalidData);
        }
        let sender = self
            .live
            .lock()
            .map_err(|_| BackendError::Unavailable)?
            .get(&run.id)
            .cloned()
            .ok_or(BackendError::Conflict)?;
        let (reply, receive) = oneshot::channel();
        sender
            .try_send(LiveCommand {
                actor: _actor.clone(),
                input,
                reply,
            })
            .map_err(|_| BackendError::Conflict)?;
        let control_timeout = self
            .frozen(&run.id)
            .map(|f| f.runtime.call_timeout_s)
            .unwrap_or(50)
            .clamp(5, 1_800);
        self.runtime.block_on(async {
            tokio::time::timeout(Duration::from_secs(control_timeout), async {
                let result = receive.await.map_err(|_| BackendError::Unavailable)??;
                if matches!(result["status"].as_str(), Some("paused" | "cancelled")) {
                    loop {
                        let attached = self
                            .live
                            .lock()
                            .map_err(|_| BackendError::Unavailable)?
                            .get(&run.id)
                            .is_some_and(|current| current.same_channel(&sender));
                        if !attached {
                            break;
                        }
                        tokio::time::sleep(Duration::from_millis(10)).await;
                    }
                }
                Ok(result)
            })
            .await
            .map_err(|_| BackendError::Unavailable)?
        })
    }
}
fn change(store: &mut Store, id: &str, transition: RunTransition) -> Result<Run> {
    let run = store
        .get_run(id)
        .map_err(kernel)?
        .ok_or(BackendError::NotFound)?;
    let receipt = store
        .execute(
            &format!("{id}:transition:{}", run.version),
            Command::TransitionRun {
                run_id: id.into(),
                expected_version: run.version,
                transition,
            },
        )
        .map_err(kernel)?;
    match receipt.result {
        CommandResult::Run(run) => Ok(run),
        _ => Err(BackendError::Conflict),
    }
}
fn provision(store: &mut Store, f: &Frozen) -> Result<()> {
    if store.get_run(&f.id).map_err(kernel)?.is_none() {
        store
            .execute(
                &format!("{}:create", f.id),
                Command::CreateProvisionedRun {
                    run_id: f.id.clone(),
                    config: Box::new(f.config.clone()),
                    provider_snapshot: Box::new(f.provider.clone()),
                },
            )
            .map_err(kernel)?;
    }
    if !f.team.is_empty() {
        return Ok(());
    }
    let task_id = format!("{}:codex", f.id);
    if store.get_task(&task_id).map_err(kernel)?.is_none() {
        store
            .execute(
                &format!("{}:task", f.id),
                Command::CreateTask {
                    run_id: f.id.clone(),
                    task_id,
                },
            )
            .map_err(kernel)?;
    }
    Ok(())
}
fn resolve_policy(
    c: &rusqlite::Connection,
    project: &Value,
    aid: &str,
) -> Result<PermissionProfile> {
    let read = |id: &str| -> Result<Value> {
        let raw: Option<String> = c
            .query_row("SELECT value FROM platform_config WHERE id=?1", [id], |r| {
                r.get(0)
            })
            .optional()
            .map_err(db)?;
        raw.map(|s| serde_json::from_str(&s).map_err(|_| BackendError::InvalidData))
            .transpose()
            .map(|v| v.unwrap_or_else(|| json!({})))
    };
    let ledger = read("member_execution_policies")?;
    let layout = read("organization_layout")?;
    let mut profile = project["permission_profile"].as_str().unwrap_or("ask");
    if let Some(teams) = layout.pointer("/config/teams").and_then(Value::as_array) {
        for team in teams {
            if team["member_ids"]
                .as_array()
                .is_some_and(|ids| ids.iter().any(|id| id == aid))
            {
                let entry =
                    &ledger["teams"][team["id"].as_str().ok_or(BackendError::InvalidData)?];
                if entry["layout_id"] == layout["ref"]["id"] {
                    if let Some(p) = entry["profile"].as_str() {
                        profile = p;
                    }
                }
            }
        }
    }
    if let Some(p) = ledger["agents"][aid]["profile"].as_str() {
        profile = p;
    }
    match profile {
        "ask" => Ok(PermissionProfile::Ask),
        "auto" => Ok(PermissionProfile::Auto),
        "full" => Ok(PermissionProfile::Full),
        _ => Err(BackendError::InvalidData),
    }
}
fn safe_file(root: &Path, name: &str) -> Result<Vec<u8>> {
    let relative = Path::new(name);
    if relative.components().count() != 1
        || !matches!(relative.components().next(), Some(Component::Normal(_)))
    {
        return Err(BackendError::NotFound);
    }
    #[cfg(unix)]
    let file = {
        use std::os::fd::{AsRawFd, FromRawFd};
        let directory = crate::product_api::workspace::open_directory(root)
            .map_err(|_| BackendError::NotFound)?;
        let name = std::ffi::CString::new(name).map_err(|_| BackendError::NotFound)?;
        let fd = unsafe {
            libc::openat(
                directory.as_raw_fd(),
                name.as_ptr(),
                libc::O_RDONLY | libc::O_CLOEXEC | libc::O_NOFOLLOW | libc::O_NONBLOCK,
            )
        };
        if fd < 0 {
            return Err(BackendError::NotFound);
        }
        unsafe { fs::File::from_raw_fd(fd) }
    };
    #[cfg(not(unix))]
    let file = {
        let path = root.join(relative);
        let meta = path
            .symlink_metadata()
            .map_err(|_| BackendError::NotFound)?;
        if !meta.is_file() || meta.file_type().is_symlink() {
            return Err(BackendError::NotFound);
        }
        fs::File::open(path).map_err(io)?
    };
    let actual = file.metadata().map_err(io)?;
    if !actual.is_file() || actual.len() > 20 * 1024 * 1024 {
        return Err(BackendError::NotFound);
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if actual.nlink() != 1 {
            return Err(BackendError::NotFound);
        }
    }
    let mut bytes = Vec::new();
    file.take(20 * 1024 * 1024 + 1)
        .read_to_end(&mut bytes)
        .map_err(io)?;
    if bytes.len() > 20 * 1024 * 1024 {
        return Err(BackendError::InvalidData);
    }
    Ok(bytes)
}

fn git(cwd: &Path, args: &[&str]) -> Result<Vec<u8>> {
    let output = std::process::Command::new("git")
        .args(["-c", "core.hooksPath=/dev/null"])
        .arg("-C")
        .arg(cwd)
        .args(args)
        .env_remove("GIT_DIR")
        .env_remove("GIT_WORK_TREE")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .output()
        .map_err(io)?;
    if !output.status.success() {
        return Err(BackendError::Unavailable);
    }
    Ok(output.stdout)
}
fn archive_workspace(root: &Path) -> Result<Vec<u8>> {
    let mut archive = zip::ZipWriter::new(std::io::Cursor::new(Vec::new()));
    let mut todo = vec![root.to_owned()];
    let mut total = 0u64;
    let mut count = 0usize;
    while let Some(dir) = todo.pop() {
        for entry in fs::read_dir(&dir).map_err(io)? {
            let entry = entry.map_err(io)?;
            if entry.file_name() == ".git" {
                continue;
            }
            let path = entry.path();
            let metadata = path.symlink_metadata().map_err(io)?;
            if metadata.file_type().is_symlink() {
                return Err(BackendError::InvalidData);
            }
            if metadata.is_dir() {
                todo.push(path);
                continue;
            }
            if !metadata.is_file() {
                return Err(BackendError::InvalidData);
            }
            total += metadata.len();
            count += 1;
            if total > 100 * 1024 * 1024 || count > 30000 {
                return Err(BackendError::InvalidData);
            }
            let name = path
                .strip_prefix(root)
                .map_err(|_| BackendError::InvalidData)?
                .to_str()
                .ok_or(BackendError::InvalidData)?;
            archive
                .start_file(
                    name,
                    zip::write::SimpleFileOptions::default()
                        .compression_method(zip::CompressionMethod::Deflated),
                )
                .map_err(|_| BackendError::Unavailable)?;
            archive
                .write_all(&safe_file(&dir, &entry.file_name().to_string_lossy())?)
                .map_err(io)?;
        }
    }
    Ok(archive
        .finish()
        .map_err(|_| BackendError::Unavailable)?
        .into_inner())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn gateway_model_id_namespaces_exactly_once() {
        assert_eq!(
            gateway_model_id("provider-a", "gpt-5.6-terra:qlyj"),
            "provider-a/gpt-5.6-terra:qlyj"
        );
        assert_eq!(
            gateway_model_id("provider-a", "provider-a/gpt-5.6-terra:qlyj"),
            "provider-a/gpt-5.6-terra:qlyj"
        );
        // A slash in the upstream model name is meaningful and must not be
        // stripped or rewritten when it is not our provider namespace.
        assert_eq!(
            gateway_model_id("provider-a", "vendor/gpt-custom"),
            "provider-a/vendor/gpt-custom"
        );
    }

    #[test]
    fn effective_tools_defaults_and_enforces_role_authority() {
        let default = json!({"role":"developer","tool_permissions":null});
        assert!(effective_tools(&default)
            .unwrap()
            .contains(&"run_command".to_owned()));
        let restricted = json!({"role":"developer","tool_permissions":["finish","read_file"]});
        assert_eq!(
            effective_tools(&restricted).unwrap(),
            vec!["finish", "read_file"]
        );
        let escalation = json!({"role":"reviewer","tool_permissions":["finish","run_command"]});
        assert!(matches!(
            effective_tools(&escalation),
            Err(BackendError::Forbidden)
        ));
        let duplicate = json!({"role":"developer","tool_permissions":["finish","finish"]});
        assert!(effective_tools(&duplicate).is_err());
    }

    #[test]
    fn freezes_only_approved_matching_skill_revisions() {
        let mut connection = rusqlite::Connection::open_in_memory().unwrap();
        connection
            .execute_batch(crate::product_store::PRODUCT_SCHEMA)
            .unwrap();
        let files = json!({"SKILL.md":"---\nname: reviewed\ndescription: Review work\n---\nCheck results."});
        connection
            .execute("INSERT INTO skills VALUES('skill','reviewed',1,1)", [])
            .unwrap();
        connection
            .execute(
                "INSERT INTO skill_revisions VALUES('skill:1','skill',1,?1,'{\"description\":\"Review work\"}',?2,'approved','reviewer','now')",
                params![crate::product_api::digest(&files), files.to_string()],
            )
            .unwrap();
        let agent =
            json!({"role":"developer","skills":[{"skill_id":"skill","version":1,"mode":"always"}]});
        let tx = connection.transaction().unwrap();
        let frozen = freeze_skills(&tx, &agent).unwrap();
        assert_eq!(frozen.len(), 1);
        assert_eq!(frozen[0].mode, "always");
        assert_eq!(frozen[0].description, "Review work");
        assert!(!frozen[0].allow_scripts);
        assert_eq!(frozen[0].files["SKILL.md"], files["SKILL.md"]);
        tx.execute("UPDATE skill_revisions SET digest='changed'", [])
            .unwrap();
        assert!(matches!(
            freeze_skills(&tx, &agent),
            Err(BackendError::Conflict)
        ));
        tx.execute("UPDATE skill_revisions SET status='revoked'", [])
            .unwrap();
        assert!(matches!(
            freeze_skills(&tx, &agent),
            Err(BackendError::Forbidden)
        ));
    }

    #[test]
    fn skill_inputs_select_always_bindings_and_preserve_binding_parameters() {
        let root = Path::new("/isolated/member-home");
        let always = FrozenSkill {
            skill_id: "always-id".into(),
            version: 3,
            digest: "approved-digest".into(),
            name: "verify-evidence".into(),
            description: "Verify recorded evidence".into(),
            allow_scripts: true,
            mode: "always".into(),
            parameters: json!({"target":"unit-tests"}),
            files: BTreeMap::new(),
        };
        let mut on_demand = always.clone();
        on_demand.skill_id = "on-demand-id".into();
        on_demand.name = "review-evidence".into();
        on_demand.mode = "on_demand".into();
        let skills = vec![always, on_demand];
        let input = turn_inputs(root, &skills, "Verify this task");
        assert_eq!(input.as_array().unwrap().len(), 2);
        assert_eq!(input[0]["text"], "Verify this task");
        assert_eq!(
            input[1],
            json!({"type":"skill","name":"verify-evidence","path":"/isolated/member-home/skills/verify-evidence/SKILL.md"})
        );
        let instructions = skill_instructions(root, &skills);
        assert!(instructions.contains("review-evidence"));
        assert!(instructions.contains("unit-tests"));
        assert!(instructions.contains("\"allow_scripts\":true"));
        assert!(instructions.contains("\"version\":3"));
    }

    #[cfg(unix)]
    #[test]
    fn skill_materialization_rejects_redirected_runtime_home() {
        let root = std::env::temp_dir().join(format!("forge-linked-skill-{}", Uuid::new_v4()));
        fs::create_dir_all(root.join("target")).unwrap();
        std::os::unix::fs::symlink(root.join("target"), root.join("runtime")).unwrap();
        assert!(matches!(
            materialize_skills(&root.join("runtime"), &[]),
            Err(BackendError::Conflict)
        ));
        assert!(!root.join("target/skills").exists());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn materialize_skills_is_immutable_and_isolated() {
        let root = std::env::temp_dir().join(format!("forge-skill-{}", Uuid::new_v4()));
        let skill = FrozenSkill {
            skill_id: "s1".into(),
            version: 1,
            digest: "d".into(),
            name: "review".into(),
            description: "Review evidence".into(),
            allow_scripts: false,
            mode: "on_demand".into(),
            parameters: json!({}),
            files: BTreeMap::from([
                (
                    String::from("SKILL.md"),
                    String::from("---\nname: review\n---\nCheck."),
                ),
                (String::from("scripts/check.sh"), String::from("exit 0\n")),
            ]),
        };
        materialize_skills(&root, std::slice::from_ref(&skill)).unwrap();
        let path = root.join("skills/review/SKILL.md");
        assert_eq!(fs::read_to_string(&path).unwrap(), skill.files["SKILL.md"]);
        assert!(!root.join("skills/review/scripts/check.sh").exists());
        materialize_skills(&root, std::slice::from_ref(&skill)).unwrap();
        fs::write(&path, "modified").unwrap();
        assert!(matches!(
            materialize_skills(&root, &[skill]),
            Err(BackendError::Conflict)
        ));
        fs::remove_dir_all(root).unwrap();
    }
}
