use super::*;
use std::sync::{Arc, Weak};

mod office;
mod packages;
mod readiness;
#[cfg(test)]
mod tests;

pub(super) struct RuntimeView {
    path: PathBuf,
    service: Weak<crate::run_service::RunService>,
}

impl ProductApi {
    pub(crate) fn with_runtime(
        mut self,
        path: PathBuf,
        service: &Arc<crate::run_service::RunService>,
    ) -> Self {
        self.runtime = Some(RuntimeView {
            path,
            service: Arc::downgrade(service),
        });
        self
    }

    pub(super) fn execution_request(
        &self,
        c: &Connection,
        u: &User,
        m: &str,
        p: &[&str],
        q: &BTreeMap<String, String>,
        b: &Value,
    ) -> Result<ProductReply> {
        let value = match (m, p) {
            ("GET", ["office", "state"]) => self.office_state(c, u, q)?,
            ("GET", ["execution", "readiness"]) => self.readiness(c, u, q, None)?,
            ("POST", ["execution", "preflight"]) => self.readiness(c, u, q, Some(b))?,
            ("GET", ["tool-packages"]) => {
                json!({"items":[packages::package()],"policy":"Fixed offline package versions require a scoped administrator approval."})
            }
            ("GET", ["execution", "tools"]) => {
                let run = self.ledger_scope(c, u, q)?;
                let mut s = c.prepare("SELECT json_object('event_id',id,'run_id',run_id,'created_at',created_at,'data',json(data)) FROM events WHERE kind='execution.tools.discovered' AND (?1='' OR run_id=?1) ORDER BY id DESC LIMIT 50")?;
                let rows = s.query_map([run], |r| decode(r.get(0)?))?;
                let items = rows
                    .map(|row| {
                        let mut v = row?;
                        if let Some(data) = v["data"].as_object().cloned() {
                            v.as_object_mut().unwrap().extend(data);
                        }
                        v.as_object_mut().unwrap().remove("data");
                        Ok(v)
                    })
                    .collect::<std::result::Result<Vec<_>, rusqlite::Error>>()?;
                json!({"items":items,"policy":"Only recorded execution.tools.discovered evidence; no installation is performed."})
            }
            ("GET", ["execution", "permissions"]) | ("GET", ["tool-installations"]) => {
                let run = self.ledger_scope(c, u, q)?;
                let kind = if p[0] == "tool-installations" {
                    Ledger::Installation
                } else {
                    Ledger::Permission
                };
                let mut s=c.prepare(&format!("SELECT id FROM {} WHERE (?1='' OR run_id=?1) ORDER BY created_at DESC,id DESC LIMIT 100",kind.table()))?;
                let ids = s
                    .query_map([run], |r| r.get::<_, String>(0))?
                    .collect::<std::result::Result<Vec<_>, _>>()?;
                let items = ids
                    .into_iter()
                    .map(|id| self.ledger(c, kind, &id))
                    .collect::<Result<Vec<_>>>()?;
                json!({"items":items})
            }
            ("POST", ["execution", "permissions", id, "review"]) => {
                self.review(c, u, Ledger::Permission, id, b)?
            }
            ("POST", ["tool-installations", id, "review"]) => {
                self.review(c, u, Ledger::Installation, id, b)?
            }
            ("POST", ["tool-installations", id, "reuse-approved"]) => self.reuse(c, u, id, b)?,
            _ => return Err(missing()),
        };
        Ok(ProductReply::json(200, value))
    }

    fn runtime_db(&self) -> Result<Option<Connection>> {
        self.runtime
            .as_ref()
            .map(|r| {
                Connection::open_with_flags(&r.path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)
                    .map_err(ProductError::from)
            })
            .transpose()
    }

    fn run(&self, c: &Connection, id: &str) -> Result<Value> {
        if let Some(db) = self.runtime_db()? {
            let run: Option<Value> = db
                .query_row("SELECT snapshot_json FROM runs WHERE id=?1", [id], |r| {
                    decode(r.get(0)?)
                })
                .optional()?;
            if let Some(run) = run {
                let frozen: Option<Value> = c
                    .query_row(
                        "SELECT frozen FROM forge_run_intents WHERE id=?1",
                        [id],
                        |r| decode(r.get(0)?),
                    )
                    .optional()?;
                let frozen = frozen.ok_or_else(|| bad("Missing immutable Rust run intent"))?;
                if let Some(team) = frozen["team"].as_object().filter(|team| !team.is_empty()) {
                    let mut agents = Map::new();
                    let mut policies = Map::new();
                    for (aid, member) in team {
                        let mut agent = member["agent"].clone();
                        agent["id"] = json!(aid);
                        agent["tools"] = member["tools"].clone();
                        agents.insert(aid.clone(), agent);
                        policies.insert(
                            aid.clone(),
                            json!({"permission_profile":member["config"]["permission_profile"]}),
                        );
                    }
                    let tasks = super::team::projection::tasks(c, id)?;
                    let stamp:String=db.query_row("SELECT COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ',max(created_at_ms)/1000.0,'unixepoch'),'') FROM events WHERE run_id=?1",[id],|r|r.get(0))?;
                    let pending:bool=db.query_row("SELECT EXISTS(SELECT 1 FROM runtime_approvals WHERE run_id=?1 AND status IN ('pending','queued','dispatching'))",[id],|r|r.get(0))?;
                    return Ok(crate::python_sqlite::redact(
                        json!({"id":id,"project_id":frozen["config"]["project_id"],"status":run["status"],"snapshot":{"project":frozen["project"],"agents":agents,"execution_policy":{"members":policies}},"tasks":tasks,"updated_at":stamp,"rust_owned":true,"pending_approval":pending}),
                    ));
                }
                let mut tasks = db
                    .prepare("SELECT snapshot_json FROM tasks WHERE run_id=?1")?
                    .query_map([id], |r| decode(r.get(0)?))?
                    .collect::<std::result::Result<Vec<_>, _>>()?;
                for task in &mut tasks {
                    task["key"] = json!(task["id"]
                        .as_str()
                        .and_then(|s| s.strip_prefix(&format!("{id}:")))
                        .unwrap_or(""));
                    task["agent_id"] = task["claim"]["worker_id"]
                        .as_str()
                        .map(Value::from)
                        .unwrap_or_else(|| frozen["config"]["members"][0]["worker_id"].clone());
                    task["kind"] = json!("autonomous");
                }
                let stamp:String=db.query_row("SELECT COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ',max(created_at_ms)/1000.0,'unixepoch'),'') FROM events WHERE run_id=?1",[id],|r|r.get(0))?;
                let aid = frozen["config"]["members"][0]["worker_id"]
                    .as_str()
                    .ok_or_else(|| bad("Invalid frozen run membership"))?;
                let pending:bool=db.query_row("SELECT EXISTS(SELECT 1 FROM runtime_approvals WHERE run_id=?1 AND status IN ('pending','queued','dispatching'))",[id],|r|r.get(0))?;
                return Ok(
                    json!({"id":id,"project_id":frozen["config"]["project_id"],"status":run["status"],"snapshot":{"project":frozen["project"],"agents":{aid:frozen["agent"]},"execution_policy":{"members":{aid:{"permission_profile":frozen["config"]["permission_profile"]}}}},"tasks":tasks,"updated_at":stamp,"rust_owned":true,"pending_approval":pending}),
                );
            }
        }
        let mut v:Value=c.query_row("SELECT json_object('id',id,'project_id',project_id,'status',status,'snapshot',json(snapshot),'updated_at',updated_at,'rust_owned',json('false')) FROM runs WHERE id=?1",[id],|r|decode(r.get(0)?)).optional()?.ok_or_else(missing)?;
        v["tasks"]=json!(c.prepare("SELECT json_object('id',id,'key',key,'kind',kind,'agent_id',agent_id,'status',status,'result',json(result)) FROM tasks WHERE run_id=?1 ORDER BY started_at,key")?.query_map([id],|r|decode(r.get(0)?))?.collect::<std::result::Result<Vec<_>,_>>()?);
        Ok(v)
    }

    pub(super) fn authorize_run(&self, c: &Connection, u: &User, id: &str) -> Result<Value> {
        let run = self.run(c, id)?;
        let pid = run["project_id"].as_str().ok_or_else(missing)?;
        project(c, u, pid)?;
        Ok(run)
    }
    fn ledger_scope(
        &self,
        c: &Connection,
        u: &User,
        q: &BTreeMap<String, String>,
    ) -> Result<String> {
        if let Some(id) = q.get("run_id").filter(|s| !s.is_empty()) {
            self.authorize_run(c, u, id)?;
            Ok(id.clone())
        } else if u.role == "admin" {
            Ok(String::new())
        } else {
            Err(ProductError {
                status: 403,
                detail: "A permitted run_id is required".into(),
            })
        }
    }
    fn ledger(&self, c: &Connection, kind: Ledger, id: &str) -> Result<Value> {
        let common="'id',id,'run_id',run_id,'task_key',task_key,'agent_id',agent_id,'decision_id',decision_id,'digest',digest,'reason',reason,'status',status,'reviewed_by',reviewed_by,'review_comment',review_comment,'result',json(result),'created_at',created_at,'updated_at',updated_at";
        let extra=match kind{Ledger::Permission=>"'command',command,'network',json(CASE WHEN network THEN 'true' ELSE 'false' END),'paths',json(paths),'review_kind',review_kind",Ledger::Installation=>"'manifest',json(manifest)"};
        let mut v: Value = c
            .query_row(
                &format!(
                    "SELECT json_object({common},{extra}) FROM {} WHERE id=?1",
                    kind.table()
                ),
                [id],
                |r| decode(r.get(0)?),
            )
            .optional()?
            .ok_or_else(missing)?;
        let rid = string(&v, "run_id", 1, 64)?;
        let key = string(&v, "task_key", 1, 100)?;
        if !safe_component(rid) || !safe_component(key) {
            return Err(bad("Invalid stored execution scope"));
        }
        v["cwd"] = json!(self
            .data_root
            .join("runs")
            .join(rid)
            .join("tasks")
            .join(key));
        if kind == Ledger::Permission {
            v["scope"] = json!("one exact command in one run/task; no permanent policy change");
            v["constraints"] = constraints();
        } else {
            let run = self.run(c, v["run_id"].as_str().unwrap())?;
            let policy = frozen_policy(&run, v["agent_id"].as_str().unwrap_or(""))?;
            v["execution_mode"] = policy["execution_mode"].clone();
            v["permission_profile"] = policy["permission_profile"].clone();
            v["scope"] = json!("one run/task/agent only");
        }
        Ok(v)
    }
    fn authorize_task(&self, c: &Connection, u: &User, row: &Value, kind: Ledger) -> Result<()> {
        let run = self.authorize_run(c, u, string(row, "run_id", 1, 64)?)?;
        if matches!(
            run["status"].as_str(),
            Some("completed" | "cancelled" | "needs_attention" | "outcome_unknown" | "accepted")
        ) {
            return Err(conflict());
        }
        let aid = string(row, "agent_id", 1, 64)?;
        let task = run["tasks"]
            .as_array()
            .and_then(|a| {
                a.iter()
                    .find(|t| t["key"] == row["task_key"] && t["agent_id"] == aid)
            })
            .ok_or_else(conflict)?;
        if !matches!(task["kind"].as_str(), Some("develop" | "autonomous"))
            || !run["snapshot"]["agents"][aid]["tools"]
                .as_array()
                .is_some_and(|tools| tools.iter().any(|t| t == "run_command"))
        {
            return Err(conflict());
        }
        let policy = frozen_policy(&run, aid)?;
        if kind == Ledger::Permission
            && (policy["execution_mode"] != "local" || policy["permission_profile"] == "full")
        {
            return Err(conflict());
        }
        Ok(())
    }
    fn review(&self, c: &Connection, u: &User, kind: Ledger, id: &str, b: &Value) -> Result<Value> {
        fields(b, &["digest", "approved", "comment"])?;
        hex(b, "digest")?;
        let approved = b["approved"]
            .as_bool()
            .ok_or_else(|| bad("approved must be boolean"))?;
        let comment = if b.get("comment").is_some() {
            string(b, "comment", 0, 2000)?
        } else {
            ""
        };
        let row = self.ledger(c, kind, id)?;
        self.authorize_run(c, u, string(&row, "run_id", 1, 64)?)?;
        if row["digest"] != b["digest"] {
            return Err(conflict());
        }
        if row["status"] != "pending" {
            let accepted = match kind {
                Ledger::Permission => ["approved", "running", "completed"],
                Ledger::Installation => ["approved", "installing", "installed"],
            };
            return if approved && accepted.contains(&row["status"].as_str().unwrap_or(""))
                || !approved && row["status"] == "rejected"
            {
                Ok(row)
            } else {
                Err(conflict())
            };
        }
        self.authorize_task(c, u, &row, kind)?;
        if kind == Ledger::Permission {
            self.validate_grants(&row["paths"])?;
        } else if row["manifest"] != packages::package() {
            return Err(conflict());
        }
        answer(c, u, &row, approved)?;
        let status = if approved { "approved" } else { "rejected" };
        let extra = if kind == Ledger::Permission {
            ",review_kind='human'"
        } else {
            ""
        };
        if c.execute(&format!("UPDATE {} SET status=?1,reviewed_by=?2,review_comment=?3,updated_at=?4{extra} WHERE id=?5 AND status='pending' AND digest=?6",kind.table()),params![status,u.id,comment,now(c)?,id,string(b,"digest",64,64)?])?!=1{return Err(conflict());}
        let name = if kind == Ledger::Permission {
            "permission_id"
        } else {
            "installation_id"
        };
        run_event(
            c,
            &row,
            if kind == Ledger::Permission {
                "execution.permission.reviewed"
            } else {
                "tool.installation.reviewed"
            },
            json!({name:id,"task_key":row["task_key"],"agent_id":row["agent_id"],"actor":u.id,"approved":approved,"digest":row["digest"],"comment":comment,"review_kind":"human"}),
        )?;
        self.ledger(c, kind, id)
    }
    fn reuse(&self, c: &Connection, u: &User, id: &str, b: &Value) -> Result<Value> {
        fields(b, &["digest", "installation_id"])?;
        hex(b, "digest")?;
        let original_id = hex(b, "installation_id")?;
        let row = self.ledger(c, Ledger::Installation, id)?;
        self.authorize_run(c, u, string(&row, "run_id", 1, 64)?)?;
        if row["digest"] != b["digest"] {
            return Err(conflict());
        }
        if row["status"] == "superseded" && row["result"]["reused_installation_id"] == original_id {
            return Ok(row);
        }
        let original = self
            .ledger(c, Ledger::Installation, original_id)
            .map_err(|_| conflict())?;
        if row["status"] != "pending"
            || id == original_id
            || ["run_id", "task_key", "agent_id", "manifest"]
                .iter()
                .any(|key| row[key] != original[key])
            || !matches!(original["status"].as_str(), Some("approved" | "installed"))
            || original["reviewed_by"].as_str().is_none_or(str::is_empty)
            || row["manifest"] != packages::package()
        {
            return Err(conflict());
        }
        self.authorize_task(c, u, &row, Ledger::Installation)?;
        let decision = decision(c, &row)?;
        let prior = self::decision(c, &original)?;
        if decision["status"] != "pending"
            || prior["status"] != "answered"
            || prior["answer"]["choice_id"] != "approve"
            || prior["answer"]["reject"] == true
        {
            return Err(conflict());
        }
        c.execute(
            "UPDATE tool_installations SET status='superseded',result=?1,updated_at=?2 WHERE id=?3",
            params![
                json!({"reused_installation_id":original_id}).to_string(),
                now(c)?,
                id
            ],
        )?;
        c.execute(
            "UPDATE decision_requests SET status='superseded' WHERE id=?1",
            [string(&row, "decision_id", 1, 64)?],
        )?;
        outbox(
            c,
            &row,
            json!({"request_id":decision["id"],"digest":decision["digest"],"rejected":false,"reused_installation_id":original_id}),
        )?;
        let audit = json!({"installation_id":id,"reused_installation_id":original_id,"request_id":decision["id"],"original_request_id":prior["id"],"task_key":row["task_key"],"agent_id":row["agent_id"],"actor":u.id,"digest":row["digest"],"source_sha256":row["manifest"]["sha256"]});
        run_event(c, &row, "tool.installation.reused", audit.clone())?;
        run_event(c, &row, "human.decision.superseded", audit)?;
        self.ledger(c, Ledger::Installation, id)
    }
    fn validate_grants(&self, v: &Value) -> Result<()> {
        let items = v
            .as_array()
            .filter(|v| v.len() <= 12)
            .ok_or_else(|| bad("At most 12 exact file grants are permitted"))?;
        let mut seen = BTreeSet::new();
        for item in items {
            fields(item, &["path", "access"])?;
            let raw = string(item, "path", 1, 1000)?;
            let access = string(item, "access", 4, 5)?;
            if !["read", "write"].contains(&access) || raw.chars().any(char::is_control) {
                return Err(bad("Invalid exact file grant"));
            }
            let path = Path::new(raw);
            if !path.is_absolute()
                || path.components().any(|p| {
                    matches!(
                        p,
                        std::path::Component::ParentDir | std::path::Component::CurDir
                    )
                })
                || path.components().any(|p| {
                    p.as_os_str().to_str().is_some_and(|s| {
                        [
                            ".ssh", ".aws", ".azure", ".gcloud", ".codex", ".config", ".local",
                        ]
                        .contains(&s)
                            || s.starts_with(".env")
                            || s.contains("credentials")
                            || [".npmrc", ".pypirc", ".netrc"].contains(&s)
                            || ["pem", "key", "p12", "pfx"].contains(
                                &Path::new(s)
                                    .extension()
                                    .and_then(|x| x.to_str())
                                    .unwrap_or(""),
                            )
                    })
                })
            {
                return Err(bad("Credential and configuration paths cannot be granted"));
            }
            let mut cursor = PathBuf::new();
            for part in path.components() {
                cursor.push(part);
                if std::fs::symlink_metadata(&cursor).is_ok_and(|m| m.file_type().is_symlink()) {
                    return Err(bad("Grant paths cannot contain symlinks"));
                }
            }
            let parent = path
                .parent()
                .filter(|p| p.is_dir())
                .ok_or_else(|| bad("Grant parent directory is unavailable"))?
                .canonicalize()
                .map_err(|_| bad("Invalid grant parent"))?;
            let canonical =
                parent.join(path.file_name().ok_or_else(|| bad("Exact file required"))?);
            let data = self
                .data_root
                .canonicalize()
                .map_err(|_| bad("Data root unavailable"))?;
            let source = Path::new(env!("CARGO_MANIFEST_DIR"))
                .ancestors()
                .nth(3)
                .unwrap_or(Path::new("/"));
            if canonical.starts_with(data)
                || canonical.starts_with(source)
                || !seen.insert(canonical)
            {
                return Err(bad("Platform files and duplicate paths cannot be granted"));
            }
            match std::fs::symlink_metadata(path) {
                Ok(meta) => {
                    if !meta.is_file() {
                        return Err(bad("Only regular files may be granted"));
                    }
                    #[cfg(unix)]
                    {
                        use std::os::unix::fs::MetadataExt;
                        if meta.nlink() != 1 {
                            return Err(bad("Hardlinked files cannot be granted"));
                        }
                    }
                }
                Err(e) if access == "write" && e.kind() == std::io::ErrorKind::NotFound => (),
                Err(_) => return Err(bad("Read grant requires an existing file")),
            }
        }
        Ok(())
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Ledger {
    Permission,
    Installation,
}
impl Ledger {
    fn table(self) -> &'static str {
        match self {
            Self::Permission => "execution_permissions",
            Self::Installation => "tool_installations",
        }
    }
}
fn safe_component(s: &str) -> bool {
    !s.is_empty()
        && s != "."
        && s != ".."
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_-.".contains(&b))
}
fn hex<'a>(v: &'a Value, k: &str) -> Result<&'a str> {
    let s = string(v, k, 64, 64)?;
    if !s
        .bytes()
        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err(bad("Expected SHA256 digest"));
    }
    Ok(s)
}
fn constraints() -> Value {
    json!({"policy_version":1,"child_processes":false,"path_scope":"exact_files","network_scope":"outbound_ip_only","message":"Temporary grants prohibit child processes and do not change the frozen project policy."})
}
fn project(c: &Connection, u: &User, id: &str) -> Result<Value> {
    c.query_row(
        "SELECT config,owner_id FROM projects WHERE id=?1",
        [id],
        |r| {
            let mut v = decode(r.get(0)?)?;
            v["id"] = json!(id);
            v["owner_id"] = json!(r.get::<_, String>(1)?);
            Ok(v)
        },
    )
    .optional()?
    .filter(|v| visible(v, u))
    .ok_or_else(missing)
}
fn frozen_policy(run: &Value, aid: &str) -> Result<Value> {
    let mut p = run["snapshot"]["project"].clone();
    let policy = &run["snapshot"]["execution_policy"]["members"][aid];
    let legacy = &run["snapshot"]["agents"][aid]["execution_policy"];
    if !p.is_object()
        || !policy.is_null() && (!policy.is_object() || !policy["permission_profile"].is_string())
        || !legacy.is_null() && (!legacy.is_object() || !legacy["profile"].is_string())
    {
        return Err(conflict());
    }
    if let Some(profile) = policy["permission_profile"]
        .as_str()
        .or_else(|| run["snapshot"]["agents"][aid]["execution_policy"]["profile"].as_str())
    {
        p["permission_profile"] = json!(profile);
    }
    if p["execution_mode"].is_null() {
        p["execution_mode"] = json!("container");
    }
    if p["permission_profile"].is_null() {
        p["permission_profile"] = json!("ask");
    }
    if !matches!(
        p["permission_profile"].as_str(),
        Some("ask" | "auto" | "full")
    ) || !matches!(p["execution_mode"].as_str(), Some("local" | "container"))
        || p["permission_profile"] == "full" && p["execution_mode"] != "local"
    {
        return Err(conflict());
    }
    Ok(p)
}
fn decision(c: &Connection, row: &Value) -> Result<Value> {
    c.query_row("SELECT json_object('id',id,'digest',digest,'status',status,'answer',json(answer),'question',json(question)) FROM decision_requests WHERE id=?1 AND run_id=?2 AND task_key=?3 AND agent_id=?4",params![string(row,"decision_id",1,64)?,string(row,"run_id",1,64)?,string(row,"task_key",1,100)?,string(row,"agent_id",1,64)?],|r|decode(r.get(0)?)).optional()?.ok_or_else(conflict)
}
fn answer(c: &Connection, u: &User, row: &Value, approved: bool) -> Result<()> {
    let d = decision(c, row)?;
    let choice = if approved { "approve" } else { "reject" };
    if d["status"] != "pending"
        || approved
            && !d["question"]["options"]
                .as_array()
                .is_some_and(|v| v.iter().any(|v| v["id"] == choice))
    {
        return Err(conflict());
    }
    let answer = json!({"choice_id":choice,"text":"","reject":!approved});
    c.execute("UPDATE decision_requests SET status=?1,answer=?2,answered_by=?3,answered_at=?4 WHERE id=?5",params![if approved{"answered"}else{"rejected"},answer.to_string(),u.id,now(c)?,string(&d,"id",1,64)?])?;
    outbox(
        c,
        row,
        json!({"request_id":d["id"],"digest":d["digest"],"rejected":!approved}),
    )?;
    run_event(
        c,
        row,
        "human.decision.answered",
        json!({"request_id":d["id"],"task_key":row["task_key"],"agent_id":row["agent_id"],"actor":u.id,"digest":d["digest"],"answer":answer,"choice_label":d["question"]["options"].as_array().and_then(|v|v.iter().find(|v|v["id"]==choice)).map(|v|v["label"].clone()).unwrap_or(json!(""))}),
    )
}
fn outbox(c: &Connection, row: &Value, payload: Value) -> Result<()> {
    c.execute("INSERT INTO outbox(id,run_id,action,payload,sent,error,attempts,created_at) VALUES(?1,?2,'respond',?3,0,'',0,?4)",params![uid(),string(row,"run_id",1,64)?,payload.to_string(),now(c)?])?;
    Ok(())
}
fn run_event(c: &Connection, row: &Value, kind: &str, data: Value) -> Result<()> {
    c.execute(
        "INSERT INTO events(run_id,kind,data,created_at) VALUES(?1,?2,?3,?4)",
        params![
            string(row, "run_id", 1, 64)?,
            kind,
            data.to_string(),
            now(c)?
        ],
    )?;
    Ok(())
}
