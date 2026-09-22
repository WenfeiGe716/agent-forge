//! Product projection and write boundary for Rust-owned runs. The legacy
//! product database supplies live identity/ACLs; only forge-store writes runs.

use crate::{
    python_sqlite::redact, ApprovalDecisionInput, Backend, BackendError, Event, EventPage,
    PythonSqliteBackend, RunControl, RunDecisionInput, User,
};
use forge_core::{Approval, ApprovalStatus, Command, CommandResult, Run, RunExecution, RunStatus};
use forge_store::{Store, StoreError};
use rusqlite::{params, Connection, OpenFlags, OptionalExtension};
use serde_json::{json, Value};
use std::{
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
};

/// An embedding host must apply the action to the actual live process and
/// durably record its observed result. A queued control is not a stopped run.
pub trait RunController: Send + Sync + 'static {
    fn control(&self, actor: &User, run: &Run, input: RunControl) -> Result<Value, BackendError>;
}

pub struct RustOwnedBackend {
    product: PythonSqliteBackend,
    path: PathBuf,
    store: Mutex<Store>,
    controller: Option<Arc<dyn RunController>>,
    service: Option<Arc<crate::run_service::RunService>>,
    assets: Option<crate::product_api::ProductApi>,
}

fn store_error(error: StoreError) -> BackendError {
    match error {
        StoreError::NotFound { .. } => BackendError::NotFound,
        StoreError::Domain(_)
        | StoreError::AlreadyExists { .. }
        | StoreError::CommandConflict(_)
        | StoreError::ConcurrentMutation
        | StoreError::RuntimeEventSequenceConflict { .. } => BackendError::Conflict,
        _ => BackendError::Unavailable,
    }
}

fn db_error(_: rusqlite::Error) -> BackendError {
    BackendError::Unavailable
}

impl RustOwnedBackend {
    pub fn open(
        product_path: impl AsRef<Path>,
        rust_path: impl AsRef<Path>,
    ) -> Result<Self, BackendError> {
        let product = PythonSqliteBackend::open(product_path)?;
        let store = Store::open(rust_path.as_ref()).map_err(store_error)?;
        let path = rust_path
            .as_ref()
            .canonicalize()
            .map_err(|_| BackendError::Unavailable)?;
        Ok(Self {
            product,
            path,
            store: Mutex::new(store),
            controller: None,
            service: None,
            assets: None,
        })
    }

    pub fn from_product(
        product: Arc<crate::ProductStore>,
        rust_path: impl AsRef<Path>,
        data_root: impl AsRef<Path>,
        codex: Option<PathBuf>,
    ) -> Result<Self, BackendError> {
        let mut backend = Self::open(product.path(), rust_path)?;
        let service = crate::run_service::RunService::new(
            product.clone(),
            backend.path.clone(),
            data_root.as_ref().to_owned(),
            codex,
        )?;
        backend.assets = Some(
            crate::product_api::ProductApi::new(
                product,
                data_root.as_ref(),
                std::env::var_os("IMPORT_ROOT")
                    .map(PathBuf::from)
                    .unwrap_or_else(|| data_root.as_ref().join("imports")),
            )
            .with_runtime(backend.path.clone(), &service),
        );
        backend.controller = Some(service.clone());
        backend.service = Some(service);
        Ok(backend)
    }

    pub fn with_controller(mut self, controller: Arc<dyn RunController>) -> Self {
        self.controller = Some(controller);
        self
    }

    pub fn with_directories(mut self, tickets: Arc<crate::desktop::DirectoryTickets>) -> Self {
        self.assets = self.assets.take().map(|api| api.with_directories(tickets));
        self
    }

    fn read<T>(
        &self,
        operation: impl FnOnce(&Connection) -> Result<T, BackendError>,
    ) -> Result<T, BackendError> {
        let mut db = Connection::open_with_flags(
            &self.path,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )
        .map_err(db_error)?;
        db.busy_timeout(std::time::Duration::from_secs(2))
            .map_err(db_error)?;
        db.pragma_update(None, "query_only", true)
            .map_err(db_error)?;
        let transaction = db.transaction().map_err(db_error)?;
        operation(&transaction)
    }

    fn authorize(&self, user: &User, run: &Run, write: bool) -> Result<User, BackendError> {
        let config = run.config.as_ref().ok_or(BackendError::NotFound)?;
        self.product
            .authorize_project(user, &config.project_id, write)
    }

    /// Call only while the exact callback is present in the live transport.
    /// Historical receipts are never returned as a reason to resend a response.
    pub fn begin_approval_dispatch(
        &self,
        approval_id: &str,
        execution: RunExecution,
    ) -> Result<Approval, BackendError> {
        let mut store = self.store.lock().map_err(|_| BackendError::Unavailable)?;
        let approval = store
            .get_approval(approval_id)
            .map_err(store_error)?
            .ok_or(BackendError::NotFound)?;
        if approval.status != ApprovalStatus::Queued || approval.request.execution != execution {
            return Err(BackendError::Conflict);
        }
        let decision = approval.decision.as_ref().ok_or(BackendError::Conflict)?;
        let user = self
            .product
            .user(&decision.actor_id)?
            .ok_or(BackendError::NotFound)?;
        let run = store
            .get_run(&approval.request.run_id)
            .map_err(store_error)?
            .ok_or(BackendError::NotFound)?;
        self.authorize(&user, &run, true)?;
        let receipt = store
            .execute(
                &format!("http:approval:{approval_id}:dispatch"),
                Command::BeginApprovalDispatch {
                    approval_id: approval_id.into(),
                    expected_version: approval.version,
                    execution,
                },
            )
            .map_err(store_error)?;
        match receipt.result {
            CommandResult::Approval(value) => Ok(*value),
            _ => Err(BackendError::Unavailable),
        }
    }

    /// This records the already attempted transport write even if access was
    /// revoked during I/O. Revocation must never hide delivery uncertainty.
    pub fn finish_approval_dispatch(
        &self,
        approval_id: &str,
        execution: RunExecution,
        delivered: bool,
    ) -> Result<Approval, BackendError> {
        let mut store = self.store.lock().map_err(|_| BackendError::Unavailable)?;
        let approval = store
            .get_approval(approval_id)
            .map_err(store_error)?
            .ok_or(BackendError::NotFound)?;
        if approval.status != ApprovalStatus::Dispatching {
            return Err(BackendError::Conflict);
        }
        let receipt = store
            .execute(
                &format!("http:approval:{approval_id}:finish"),
                Command::FinishApprovalDispatch {
                    approval_id: approval_id.into(),
                    expected_version: approval.version,
                    execution,
                    delivered,
                },
            )
            .map_err(store_error)?;
        match receipt.result {
            CommandResult::Approval(value) => Ok(*value),
            _ => Err(BackendError::Unavailable),
        }
    }

    /// Exposes the durable store only to trusted integration tests/hosts; the
    /// HTTP adapter itself never accepts raw `Command` values from clients.
    pub fn store_path(&self) -> &Path {
        &self.path
    }
}

fn load_run(db: &Connection, id: &str) -> Result<Option<Run>, BackendError> {
    let raw: Option<String> = db
        .query_row("SELECT snapshot_json FROM runs WHERE id=?1", [id], |r| {
            r.get(0)
        })
        .optional()
        .map_err(db_error)?;
    raw.map(|raw| serde_json::from_str(&raw).map_err(|_| BackendError::InvalidData))
        .transpose()
}

fn status(run: &Run) -> &'static str {
    match run.status {
        RunStatus::Pending => "queued",
        RunStatus::Starting | RunStatus::Running => "running",
        RunStatus::Paused => "paused",
        RunStatus::OutcomeUnknown => "needs_attention",
        RunStatus::Submitted | RunStatus::Integrated => "awaiting_acceptance",
        RunStatus::Accepted => "completed",
        RunStatus::Cancelled => "cancelled",
    }
}

fn view(db: &Connection, run: &Run, detailed: bool) -> Result<Value, BackendError> {
    let config = run.config.as_ref().ok_or(BackendError::NotFound)?;
    let (created, updated, cursor): (String, String, i64) = db.query_row(
        "SELECT COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ',min(created_at_ms)/1000.0,'unixepoch'),''), COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ',max(created_at_ms)/1000.0,'unixepoch'),''), COALESCE(max(id),0) FROM events WHERE run_id=?1",
        [&run.id], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))).map_err(db_error)?;
    let agents: Vec<_> = config.members.iter().map(|m| m.worker_id.clone()).collect();
    let mut value = json!({"id": run.id, "project_id": config.project_id, "title": run.id,
        "request": "", "status": status(run), "calls": 0, "input_tokens": 0, "output_tokens": 0,
        "error": "", "created_at": created, "updated_at": updated, "agent_ids": agents,
        "mode": "autonomous", "execution_strategy": "autonomous", "execution_engine": "codex",
        "run_project_checks": false, "entry_agent_id": agents.first(), "planner_id": agents.first(),
        "reviewer_id": null, "owner": "rust", "version": run.version, "kernel_status": run.status});
    if detailed {
        let mut tasks = db
            .prepare("SELECT snapshot_json FROM tasks WHERE run_id=?1 ORDER BY id")
            .map_err(db_error)?;
        let tasks: Result<Vec<Value>, BackendError> = tasks.query_map([&run.id], |r| r.get::<_, String>(0)).map_err(db_error)?
            .map(|raw| {
                let task: forge_core::Task = serde_json::from_str(&raw.map_err(db_error)?).map_err(|_| BackendError::InvalidData)?;
                Ok(json!({"id": task.id, "key": task.id, "agent_id": task.claim.as_ref().map(|c| &c.worker_id),
                    "kind": "autonomous", "title": task.id, "status": task.status, "spec": {},
                    "result": redact(task.result.unwrap_or_else(|| json!({})))}))
            }).collect();
        let mut commands = db.prepare("SELECT e.command_id, e.kind, e.payload_json, o.delivered_at_ms IS NOT NULL FROM events e JOIN outbox o ON o.event_id=e.id WHERE e.run_id=?1 ORDER BY e.id").map_err(db_error)?;
        let commands: Result<Vec<Value>, BackendError> = commands.query_map([&run.id], |r| Ok((r.get::<_, String>(0)?,r.get::<_, String>(1)?,r.get::<_, String>(2)?,r.get::<_, bool>(3)?))).map_err(db_error)?
            .map(|row| { let (id, action, raw, sent) = row.map_err(db_error)?; let payload: Value = serde_json::from_str(&raw).map_err(|_| BackendError::InvalidData)?;
                Ok(json!({"id": id, "action": action, "payload": redact(payload), "sent": sent, "error": ""})) }).collect();
        let result = run.result.clone().unwrap_or_else(|| json!({}));
        value.as_object_mut().unwrap().extend(json!({"snapshot": {
            "execution_engine": "codex", "execution_strategy": "autonomous", "owner": "rust",
            "route_binding": {"provider_id": config.route_binding.provider_id,
                "provider_revision_id": config.route_binding.provider_revision_id,
                "model_id": config.route_binding.model_id, "config_digest": config.route_binding.config_digest},
            "permission_profile": config.permission_profile,
            "execution_policy": config.permission_profile.execution_policy()},
            "plan": null, "plan_digest": "", "artifact_digest": result.get("artifact_sha256").and_then(Value::as_str).unwrap_or(""),
            "report": redact(result), "tasks": tasks?, "commands": commands?, "event_cursor": cursor
        }).as_object().unwrap().clone());
    }
    Ok(value)
}

fn approval_view(approval: Approval) -> Value {
    json!({"id": approval.id, "run_id": approval.request.run_id, "task_key": "codex",
        "agent_id": approval.request.execution.connection.worker_id, "kind": approval.request.kind,
        "thread_id": approval.request.execution.thread_id, "turn_id": approval.request.execution.turn_id,
        "item_id": approval.request.item_id, "status": approval.status, "digest": approval.digest,
        "payload": redact(approval.request.payload), "decision": approval.decision.map(|d| json!({
            "approved": d.approved, "actor": d.actor_id, "comment": d.comment})), "error": "",
        "created_at": approval.created_at_ms})
}

impl Backend for RustOwnedBackend {
    fn product_request(
        &self,
        user: &User,
        method: &str,
        path: &str,
        query: &std::collections::BTreeMap<String, String>,
        body: &[u8],
    ) -> Result<crate::product_api::ProductReply, crate::product_api::ProductError> {
        let parts: Vec<_> = path.trim_matches('/').split('/').collect();
        if let ("GET", ["api", "runs", id, "trace"]) = (method, parts.as_slice()) {
            let run = self.run(user, id)?;
            let after = query
                .get("after")
                .map(|s| s.parse::<i64>().map_err(|_| BackendError::InvalidData))
                .transpose()?
                .unwrap_or(0);
            let limit = query
                .get("limit")
                .map(|s| s.parse::<usize>().map_err(|_| BackendError::InvalidData))
                .transpose()?
                .unwrap_or(100);
            let search = query.get("q").map(|s| s.to_lowercase()).unwrap_or_default();
            if after < 0 || !(1..=300).contains(&limit) || search.chars().count() > 200 {
                return Err(BackendError::InvalidData.into());
            }
            let agent = query.get("agent_id").filter(|s| !s.is_empty());
            let meeting = query.get("meeting_id").filter(|s| !s.is_empty());
            let communication = query
                .get("communication_only")
                .map(|s| match s.as_str() {
                    "true" | "1" => Ok(true),
                    "false" | "0" => Ok(false),
                    _ => Err(BackendError::InvalidData),
                })
                .transpose()?
                .unwrap_or(false);
            let tasks: Vec<_> = run["tasks"]
                .as_array()
                .into_iter()
                .flatten()
                .filter(|t| {
                    agent.is_none_or(|a| t["agent_id"] == **a)
                        && meeting.is_none_or(|m| t["spec"]["meeting_id"] == **m)
                })
                .filter_map(|t| t["key"].as_str())
                .collect();
            let page = self.events(user, id, after, 1000)?;
            let mut cursor = after;
            let mut items = Vec::new();
            for event in page.events {
                cursor = event.id;
                let data = &event.data;
                if communication
                    && !matches!(
                        event.kind.as_str(),
                        "agent.message"
                            | "agent.message.delivered"
                            | "team.attachment.uploaded"
                            | "task.handoff"
                            | "task.delegated"
                            | "orchestration.handoff"
                    )
                {
                    continue;
                }
                let task_matches = data["task_key"]
                    .as_str()
                    .is_some_and(|key| tasks.contains(&key));
                if agent.is_some_and(|a| {
                    ![
                        "agent_id",
                        "from_agent_id",
                        "to_agent_id",
                        "recipient_id",
                        "host_id",
                    ]
                    .iter()
                    .any(|key| data[key] == **a)
                        && !["participants", "recipients"].iter().any(|key| {
                            data[key].as_array().is_some_and(|items| {
                                items.iter().any(|v| v.as_str() == Some(a.as_str()))
                            })
                        })
                        && !task_matches
                }) {
                    continue;
                }
                if meeting.is_some_and(|m| data["meeting_id"] != **m && !task_matches) {
                    continue;
                }
                if !search.is_empty()
                    && !format!("{} {}", event.kind, event.data)
                        .to_lowercase()
                        .contains(&search)
                {
                    continue;
                }
                items.push(event);
                if items.len() == limit {
                    break;
                }
            }
            let more = !self.events(user, id, cursor, 1)?.events.is_empty();
            return Ok(crate::product_api::ProductReply::json(
                200,
                json!({"items":items,"next_cursor":cursor,"has_more":more}),
            ));
        }
        if let Some(service) = &self.service {
            if let Some(reply) = service.product_request(user, method, path, query, body)? {
                return Ok(reply);
            }
        }
        self.assets
            .as_ref()
            .ok_or(crate::product_api::ProductError {
                status: 404,
                detail: "Not Found".into(),
            })?
            .request(user, method, path, query, body)
    }
    fn preview_run(&self, user: &User, input: Value) -> Result<Value, BackendError> {
        self.service
            .as_ref()
            .ok_or(BackendError::Unsupported)?
            .preview(user, input)
    }
    fn create_run(
        &self,
        user: &User,
        input: Value,
        key: Option<String>,
    ) -> Result<Value, BackendError> {
        let id = self
            .service
            .as_ref()
            .ok_or(BackendError::Unsupported)?
            .create(user, input, key)?;
        self.run(user, &id)
    }
    fn capabilities(&self) -> Value {
        json!({"api_version": 1, "read": {"runs": true,"events": true,"stream": true,"artifacts": self.service.is_some()},
            "write": {"create_run": self.service.as_ref().is_some_and(|s| s.enabled()), "product_assets":self.assets.is_some(), "control_run": self.controller.is_some(), "decisions": true,"approvals": true},
            "durability": {"sessions": if self.service.is_some(){"sqlite"}else{"process"}, "product_database":if self.assets.is_some(){"rust_owned"}else{"read_only"}, "rust_database": "sole_run_owner"}})
    }
    fn check_credentials(
        &self,
        username: &str,
        password: &str,
    ) -> Result<Option<User>, BackendError> {
        self.product.check_credentials(username, password)
    }
    fn user(&self, id: &str) -> Result<Option<User>, BackendError> {
        self.product.user(id)
    }

    fn run_statuses(
        &self,
        user: &User,
    ) -> Result<std::collections::BTreeMap<String, String>, BackendError> {
        let mut values = self.product.run_statuses(user)?;
        self.read(|db| {
            let mut statement = db
                .prepare("SELECT snapshot_json FROM runs")
                .map_err(db_error)?;
            for raw in statement
                .query_map([], |r| r.get::<_, String>(0))
                .map_err(db_error)?
            {
                let run: Run = serde_json::from_str(&raw.map_err(db_error)?)
                    .map_err(|_| BackendError::InvalidData)?;
                match self.authorize(user, &run, false) {
                    Ok(_) => {
                        values.insert(run.id.clone(), status(&run).into());
                    }
                    Err(BackendError::NotFound) => {
                        values.remove(&run.id);
                    }
                    Err(error) => return Err(error),
                }
            }
            Ok(())
        })?;
        Ok(values)
    }

    fn runs(&self, user: &User, limit: usize) -> Result<Vec<Value>, BackendError> {
        let mut values = self.product.runs(user, limit)?;
        self.read(|db| {
            let mut statement = db
                .prepare("SELECT snapshot_json FROM runs ORDER BY rowid DESC")
                .map_err(db_error)?;
            for raw in statement
                .query_map([], |r| r.get::<_, String>(0))
                .map_err(db_error)?
            {
                let run: Run = serde_json::from_str(&raw.map_err(db_error)?)
                    .map_err(|_| BackendError::InvalidData)?;
                match self.authorize(user, &run, false) {
                    Ok(_) => {
                        values.retain(|v| v["id"].as_str() != Some(&run.id));
                        values.push(view(db, &run, false)?);
                    }
                    Err(BackendError::NotFound) => {}
                    Err(error) => return Err(error),
                }
            }
            Ok(())
        })?;
        values.sort_by(|a, b| b["created_at"].as_str().cmp(&a["created_at"].as_str()));
        values.truncate(limit);
        if let Some(service) = &self.service {
            for value in &mut values {
                service.enrich(value)?;
            }
        }
        Ok(values)
    }
    fn run(&self, user: &User, id: &str) -> Result<Value, BackendError> {
        let mut value = self.read(|db| match load_run(db, id)? {
            Some(run) => {
                self.authorize(user, &run, false)?;
                view(db, &run, true)
            }
            None => self.product.run(user, id),
        })?;
        if let Some(service) = &self.service {
            service.enrich(&mut value)?;
            service.enrich_team_projection(&mut value)?;
        }
        Ok(value)
    }
    fn event_notifications(&self) -> Option<tokio::sync::watch::Receiver<u64>> {
        self.service.as_ref().map(|service| service.subscribe_events())
    }
    fn events(
        &self,
        user: &User,
        id: &str,
        after: i64,
        limit: usize,
    ) -> Result<EventPage, BackendError> {
        if after < 0 || !(1..=1000).contains(&limit) {
            return Err(BackendError::InvalidData);
        }
        let mut page = self.read(|db| {
            let Some(run) = load_run(db, id)? else { return self.product.events(user, id, after, limit); };
            self.authorize(user, &run, false)?;
            let mut statement = db.prepare("SELECT id,kind,payload_json,strftime('%Y-%m-%dT%H:%M:%fZ',created_at_ms/1000.0,'unixepoch') FROM events WHERE run_id=?1 AND id>?2 ORDER BY id LIMIT ?3").map_err(db_error)?;
            let rows = statement.query_map(params![id,after,(limit+1) as i64], |r| Ok((r.get(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?,r.get(3)?))).map_err(db_error)?;
            let mut events = Vec::new();
            for row in rows { let (event_id, kind, raw, created_at) = row.map_err(db_error)?; events.push(Event { id: event_id, run_id: id.into(), kind,
                data: redact(serde_json::from_str(&raw).map_err(|_| BackendError::InvalidData)?), created_at }); }
            let has_more = events.len() > limit; events.truncate(limit); Ok(EventPage { events, has_more })
        })?;
        if let Some(service) = &self.service {
            service.enrich_events(&mut page.events)?;
        }
        Ok(page)
    }
    fn approvals(&self, user: &User, id: &str, limit: usize) -> Result<Value, BackendError> {
        let store = self.store.lock().map_err(|_| BackendError::Unavailable)?;
        let run = store
            .get_run(id)
            .map_err(store_error)?
            .ok_or(BackendError::NotFound)?;
        self.authorize(user, &run, false)?;
        Ok(
            json!({"items": store.list_approvals(id, limit.min(500) as u32).map_err(store_error)?.into_iter().map(approval_view).collect::<Vec<_>>()}),
        )
    }
    fn decide_approval(
        &self,
        user: &User,
        id: &str,
        approval_id: &str,
        input: ApprovalDecisionInput,
    ) -> Result<Value, BackendError> {
        let mut store = self.store.lock().map_err(|_| BackendError::Unavailable)?;
        let run = store
            .get_run(id)
            .map_err(store_error)?
            .ok_or(BackendError::NotFound)?;
        let actor = self.authorize(user, &run, true)?;
        let approval = store
            .get_approval(approval_id)
            .map_err(store_error)?
            .filter(|a| a.request.run_id == id)
            .ok_or(BackendError::NotFound)?;
        if approval.digest != input.digest {
            return Err(BackendError::Conflict);
        }
        if approval.status != ApprovalStatus::Pending {
            if matches!(
                approval.status,
                ApprovalStatus::Queued | ApprovalStatus::Dispatching | ApprovalStatus::Delivered
            ) && approval
                .decision
                .as_ref()
                .is_some_and(|d| d.approved == input.approved)
            {
                return Ok(
                    json!({"accepted": true,"status": approval.status,"approval_id": approval.id}),
                );
            }
            return Err(BackendError::Conflict);
        }
        store
            .execute(
                &format!("http:approval:{approval_id}:decision"),
                Command::DecideApproval {
                    approval_id: approval_id.into(),
                    expected_version: approval.version,
                    digest: input.digest,
                    approved: input.approved,
                    actor_id: actor.id,
                    comment: input.comment,
                },
            )
            .map_err(store_error)?;
        Ok(json!({"accepted": true,"status": "queued","approval_id": approval_id}))
    }
    fn control_run(&self, user: &User, id: &str, input: RunControl) -> Result<Value, BackendError> {
        let run = self
            .store
            .lock()
            .map_err(|_| BackendError::Unavailable)?
            .get_run(id)
            .map_err(store_error)?
            .ok_or(BackendError::NotFound)?;
        let actor = self.authorize(user, &run, true)?;
        self.controller
            .as_ref()
            .ok_or(BackendError::Unsupported)?
            .control(&actor, &run, input)
    }
    fn decide_run(
        &self,
        user: &User,
        id: &str,
        phase: &str,
        input: RunDecisionInput,
    ) -> Result<Value, BackendError> {
        if let Some(service) = &self.service {
            if service.owns(id)? {
                return service.decide(user, id, phase, input);
            }
        }
        let mut store = self.store.lock().map_err(|_| BackendError::Unavailable)?;
        let run = store
            .get_run(id)
            .map_err(store_error)?
            .ok_or(BackendError::NotFound)?;
        self.authorize(user, &run, true)?;
        if phase != "acceptance"
            || !input.approved
            || run
                .result
                .as_ref()
                .and_then(|v| v.get("artifact_sha256"))
                .and_then(Value::as_str)
                != Some(&input.digest)
        {
            return Err(BackendError::Conflict);
        }
        let accepted =
            forge_cli::owned_run::accept(&mut store, id).map_err(|_| BackendError::Conflict)?;
        Ok(
            json!({"accepted": true, "run_id": id, "status": status(&accepted), "command_id": format!("{id}:accept")}),
        )
    }
}
