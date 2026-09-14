use super::*;
mod answers;
pub(super) mod artifacts;
use crate::product_api::team::projection as team_projection;

impl RunService {
    pub(super) fn access_id(&self, user: &User, id: &str, write: bool) -> Result<()> {
        let project = if self.owns(id)? {
            self.frozen(id)?.config.project_id
        } else {
            self.product
                .connect()?
                .query_row("SELECT project_id FROM runs WHERE id=?1", [id], |r| {
                    r.get::<_, String>(0)
                })
                .optional()
                .map_err(db)?
                .ok_or(BackendError::NotFound)?
        };
        self.product.authorize_project(user, &project, write)?;
        Ok(())
    }
    pub(super) fn query_request(
        &self,
        user: &User,
        method: &str,
        path: &str,
        q: &BTreeMap<String, String>,
        body: &[u8],
    ) -> std::result::Result<Option<ProductReply>, ProductError> {
        let p: Vec<_> = path.trim_matches('/').split('/').collect();
        let reply = match (method, p.as_slice()) {
            ("GET", ["api", "runs", id, "team"]) => {
                self.access_id(user, id, false)?;
                let frozen = self.frozen(id)?;
                ProductReply::json(
                    200,
                    team_projection::directory(
                        &self.product.connect()?,
                        &frozen.team_snapshot(),
                        id,
                        q.get("member_id").map(String::as_str),
                    )?,
                )
            }
            ("GET", ["api", "runs", id, "tasks"]) if self.owns(id)? => {
                self.access_id(user, id, false)?;
                ProductReply::json(200, json!(self.sessions(id)?))
            }
            ("POST", ["api", "runs", _id, "tasks"])
            | ("POST", ["api", "runs", _id, "tasks", ..]) => {
                return Err(crate::product_api::ProductError {
                    status: 403,
                    detail:
                        "Team mutations are available only to the active Rust-owned Codex session"
                            .into(),
                });
            }
            ("GET", ["api", "runs", id, "artifacts", filename]) => {
                self.access_id(user, id, false)?;
                let bytes =
                    artifacts::read(&self.root.join("runs").join(id).join("artifacts"), filename)?;
                ProductReply {
                    status: 200,
                    body: Value::Null,
                    download: Some(ProductDownload {
                        bytes,
                        filename: filename.to_string(),
                        content_type: if *filename == "source.zip" {
                            "application/zip"
                        } else {
                            "application/octet-stream"
                        },
                    }),
                }
            }
            ("GET", ["api", "runs", id, "collaboration"]) => {
                self.access_id(user, id, false)?;
                let c = self.product.connect()?;
                let decisions=rows(&c,"SELECT json_object('id',id,'run_id',run_id,'task_key',task_key,'agent_id',agent_id,'digest',digest,'question',json(question),'status',status,'answer',json(answer),'answered_by',answered_by,'created_at',created_at,'kind','question','resource_id',id) FROM decision_requests WHERE run_id=?1 ORDER BY created_at",id)?;
                let messages=rows(&c,"SELECT json_object('id',id,'sender_id',sender_id,'recipient_id',recipient_id,'body',body,'created_at',created_at,'delivered_at',delivered_at) FROM agent_messages WHERE run_id=?1 ORDER BY created_at",id)?;
                let instructions=rows(&c,"SELECT json_object('id',id,'body',body,'recipients',json(recipients),'author_id',author_id,'created_at',created_at) FROM human_instructions WHERE run_id=?1 ORDER BY created_at",id)?;
                let meetings=rows(&c,"SELECT json_object('id',id,'host_id',host_id,'participants',json(participants),'subject',subject,'agenda',agenda,'status',status,'outcome',json(outcome),'created_at',created_at,'ended_at',ended_at,'speaker_ids',json('[]'),'source','business') FROM meetings WHERE run_id=?1 ORDER BY created_at",id)?;
                let actions=rows(&c,"SELECT json_object('id',id,'meeting_id',meeting_id,'owner_id',owner_id,'task_key',task_key,'specification',json(specification),'status',status,'origin',json(origin),'created_at',created_at,'result',json('{}')) FROM action_items WHERE run_id=?1 ORDER BY created_at",id)?;
                ProductReply::json(
                    200,
                    json!({"run_id":id,"decisions":decisions,"messages":messages,"instructions":instructions,"meetings":meetings,"actions":actions}),
                )
            }
            ("GET", ["api", "runs", id, "sessions"]) => {
                self.access_id(user, id, false)?;
                let mut items = self.sessions(id)?;
                if let Some(a) = q.get("agent_id").filter(|s| !s.is_empty()) {
                    items.retain(|v| v["agent_id"] == **a)
                }
                if let Some(search) = q.get("q").filter(|s| !s.is_empty()) {
                    let search = search.to_lowercase();
                    items.retain(|v| v.to_string().to_lowercase().contains(&search));
                }
                let (offset, limit) = page(q, 60)?;
                let total = items.len();
                let items: Vec<_> = items.into_iter().skip(offset).take(limit).collect();
                ProductReply::json(
                    200,
                    json!({"next_offset":offset+items.len(),"items":items,"total":total,"has_more":total>offset+limit}),
                )
            }
            ("GET", ["api", "runs", id, "sessions", key]) => {
                self.access_id(user, id, false)?;
                let session = self
                    .sessions(id)?
                    .into_iter()
                    .find(|v| v["key"] == *key)
                    .ok_or(BackendError::NotFound)?;
                let items = if self.owns(id)? && !self.frozen(id)?.team.is_empty() {
                    team_projection::messages(&self.product.connect()?, id, key)?
                } else if self.owns(id)? {
                    self.single_session_messages(id)?
                } else {
                    let raw: String = self
                        .product
                        .connect()?
                        .query_row(
                            "SELECT messages FROM tasks WHERE run_id=?1 AND key=?2",
                            params![id, key],
                            |r| r.get(0),
                        )
                        .map_err(db)?;
                    let values: Vec<Value> =
                        serde_json::from_str(&raw).map_err(|_| BackendError::InvalidData)?;
                    values.into_iter().enumerate().filter(|(_,v)|matches!(v["role"].as_str(),Some("assistant"|"tool"))).map(|(index,v)|json!({"index":index,"role":v["role"],"content":v["content"],"calls":v["calls"]})).collect()
                };
                let (offset, limit) = page(q, 50)?;
                let total = items.len();
                let items: Vec<_> = items
                    .into_iter()
                    .skip(offset)
                    .take(limit)
                    .map(crate::python_sqlite::redact)
                    .collect();
                ProductReply::json(
                    200,
                    crate::python_sqlite::redact(
                        json!({"task_key":key,"agent_id":session["agent_id"],"status":session["status"],"result":session["result"],"spec":session.get("spec").cloned().unwrap_or(json!({})),"next_offset":offset+items.len(),"items":items,"total":total,"has_more":total>offset+limit}),
                    ),
                )
            }
            ("POST", ["api", "runs", id, "instructions"]) => {
                self.access_id(user, id, true)?;
                let input: Value =
                    serde_json::from_slice(body).map_err(|_| BackendError::InvalidData)?;
                let message = text(&input, "body")?;
                if message.len() > 30000 {
                    return Err(BackendError::InvalidData.into());
                }
                let f = self.frozen(id)?;
                let recipients = input
                    .get("recipients")
                    .cloned()
                    .unwrap_or_else(|| json!([]));
                let recipients = recipients.as_array().ok_or(BackendError::InvalidData)?;
                if recipients
                    .iter()
                    .any(|r| r.as_str().is_none_or(|s| !f.config.permits_worker(s)))
                {
                    return Err(BackendError::InvalidData.into());
                }
                let unique: std::collections::BTreeSet<_> = recipients
                    .iter()
                    .map(|v| v.as_str().unwrap_or(""))
                    .collect();
                if unique.len() != recipients.len() {
                    return Err(BackendError::InvalidData.into());
                }
                let run = Store::open(&self.path)
                    .map_err(kernel)?
                    .get_run(id)
                    .map_err(kernel)?
                    .ok_or(BackendError::NotFound)?;
                if !matches!(
                    run.status,
                    RunStatus::Running | RunStatus::Paused | RunStatus::Starting
                ) {
                    return Err(BackendError::Conflict.into());
                }
                let mid = q
                    .get("__idempotency_key")
                    .map(|key| hash(format!("{id}:{}:{key}", user.id).as_bytes()))
                    .unwrap_or_else(|| Uuid::new_v4().simple().to_string());
                let mut c = self.product.connect()?;
                let tx = c
                    .transaction_with_behavior(TransactionBehavior::Immediate)
                    .map_err(db)?;
                let existing: Option<(String, String)> = tx
                    .query_row(
                        "SELECT body,recipients FROM human_instructions WHERE id=?1",
                        [&mid],
                        |r| Ok((r.get(0)?, r.get(1)?)),
                    )
                    .optional()
                    .map_err(db)?;
                if let Some((saved, saved_recipients)) = existing {
                    let saved_recipients: Value = serde_json::from_str(&saved_recipients)
                        .map_err(|_| BackendError::InvalidData)?;
                    if saved != message || saved_recipients != json!(recipients) {
                        return Err(BackendError::Conflict.into());
                    }
                    let delivered:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM instruction_receipts WHERE instruction_id=?1)",[&mid],|r|r.get(0)).map_err(db)?;
                    return Ok(Some(ProductReply::json(
                        202,
                        json!({"id":mid,"status":if delivered{"delivered"}else{"persisted"}}),
                    )));
                }
                tx.execute("INSERT INTO human_instructions VALUES(?1,?2,?3,?4,?5,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",params![mid,id,user.id,serde_json::to_string(recipients).map_err(|_|BackendError::InvalidData)?,message]).map_err(db)?;
                tx.execute("INSERT INTO events(run_id,kind,data,created_at) VALUES(?1,'human.instruction',?2,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",params![id,json!({"instruction_id":mid,"actor":user.id,"body":message,"recipients":recipients}).to_string()]).map_err(db)?;
                tx.commit().map_err(db)?;
                let delivered = if run.status == RunStatus::Running {
                    self.control(
                        user,
                        &run,
                        RunControl {
                            action: ControlAction::Steer,
                            task_key: None,
                            agent_id: None,
                            reason: String::new(),
                            prompt: Some(message.into()),
                        },
                    )
                    .is_ok()
                } else {
                    false
                };
                if delivered {
                    self.product.connect()?.execute("INSERT INTO instruction_receipts VALUES(?1,?2,?3,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",params![format!("{mid}:codex"),mid,format!("{id}:codex")]).map_err(db)?;
                } else {
                    self.product.connect()?.execute("INSERT INTO outbox(id,run_id,action,payload,sent,error,attempts,created_at) VALUES(?1,?2,'answer',?3,0,'',0,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",params![Uuid::new_v4().simple().to_string(),id,json!({"instruction_id":mid,"text":message}).to_string()]).map_err(db)?;
                }
                ProductReply::json(
                    202,
                    json!({"id":mid,"status":if delivered{"delivered"}else{"persisted"}}),
                )
            }
            ("POST", ["api", "runs", id, "questions", request, "answer"]) => {
                self.answer_question(user, id, request, body)?
            }
            _ => return Ok(None),
        };
        Ok(Some(reply))
    }
    fn sessions(&self, id: &str) -> Result<Vec<Value>> {
        if self.owns(id)? {
            let f = self.frozen(id)?;
            if !f.team.is_empty() {
                let c = self.product.connect()?;
                let mut tasks = team_projection::tasks(&c, id).map_err(BackendError::from)?;
                for task in &mut tasks {
                    let key = task["key"].as_str().ok_or(BackendError::InvalidData)?;
                    task["message_count"] = json!(team_projection::messages(&c, id, key)
                        .map_err(BackendError::from)?
                        .len());
                }
                return Ok(tasks);
            }
            let c = rusqlite::Connection::open(&self.path).map_err(db)?;
            let mut s = c
                .prepare("SELECT snapshot_json FROM tasks WHERE run_id=?1 ORDER BY rowid")
                .map_err(db)?;
            let values = s.query_map([id], |r| r.get::<_, String>(0)).map_err(db)?;
            let mut out = Vec::new();
            for raw in values {
                let task: forge_core::Task = serde_json::from_str(&raw.map_err(db)?)
                    .map_err(|_| BackendError::InvalidData)?;
                out.push(json!({"key":task.id.strip_prefix(&format!("{id}:")),"agent_id":task.claim.as_ref().map(|c|c.worker_id.clone()).unwrap_or_else(||f.config.members[0].worker_id.clone()),"title":f.title,"kind":"autonomous","status":task.status,"message_count":0,"summary":task.result.as_ref().and_then(|v|v["summary"].as_str()).unwrap_or(""),"result":task.result}));
            }
            Ok(out)
        } else {
            rows(&self.product.connect()?,"SELECT json_object('key',key,'agent_id',agent_id,'title',title,'kind',kind,'status',status,'meeting_id',json_extract(spec,'$.meeting_id'),'message_count',json_array_length(messages),'summary',json_extract(result,'$.summary'),'result',json(result)) FROM tasks WHERE run_id=?1 ORDER BY started_at,key",id)
        }
    }

    /// Final read projection after the compatibility enrichment: team tasks
    /// are product-owned, while the independent kernel owns the aggregate run.
    pub(crate) fn enrich_team_projection(&self, value: &mut Value) -> Result<()> {
        let Some(id) = value["id"].as_str().map(str::to_owned) else {
            return Ok(());
        };
        if value.get("tasks").is_none() || !self.owns(&id)? {
            return Ok(());
        }
        let frozen = self.frozen(&id)?;
        if frozen.team.is_empty() {
            return Ok(());
        }
        value["tasks"] = json!(self.sessions(&id)?);
        *value = crate::python_sqlite::redact(value.take());
        Ok(())
    }

    fn single_session_messages(&self, id: &str) -> Result<Vec<Value>> {
        let c = rusqlite::Connection::open(&self.path).map_err(db)?;
        // Historical single-member sessions may reconnect. Only connections
        // recorded for this frozen worker and this run are eligible.
        let worker = self.frozen(id)?.config.members[0].worker_id.clone();
        let mut s=c.prepare("SELECT DISTINCT json_extract(payload_json,'$.connection.connection_id') FROM events WHERE run_id=?1 AND kind='runtime_event' AND json_extract(payload_json,'$.connection.worker_id')=?2").map_err(db)?;
        let connections = s
            .query_map(params![id, worker], |row| row.get::<_, String>(0))
            .map_err(db)?
            .collect::<std::result::Result<Vec<_>, _>>()
            .map_err(db)?;
        let c = self.product.connect()?;
        let mut s=c.prepare("SELECT json_object('role','assistant','content',json_extract(payload,'$.item.text'),'calls',json('[]'),'index',rowid) FROM forge_runtime_messages WHERE run_id=?1 AND connection_id IN (SELECT value FROM json_each(?2)) AND kind='item/completed' AND json_extract(payload,'$.item.type')='agentMessage' ORDER BY rowid").map_err(db)?;
        let rows = s
            .query_map(params![id, json!(connections).to_string()], |r| {
                r.get::<_, String>(0)
            })
            .map_err(db)?;
        rows.map(|r| {
            serde_json::from_str(&r.map_err(db)?)
                .map(crate::python_sqlite::redact)
                .map_err(|_| BackendError::InvalidData)
        })
        .collect()
    }
}
fn rows(c: &rusqlite::Connection, sql: &str, id: &str) -> Result<Vec<Value>> {
    let mut s = c.prepare(sql).map_err(db)?;
    let rows = s.query_map([id], |r| r.get::<_, String>(0)).map_err(db)?;
    rows.map(|r| serde_json::from_str(&r.map_err(db)?).map_err(|_| BackendError::InvalidData))
        .collect()
}
fn page(q: &BTreeMap<String, String>, default: usize) -> Result<(usize, usize)> {
    let offset = q
        .get("offset")
        .map(|s| s.parse::<usize>().map_err(|_| BackendError::InvalidData))
        .transpose()?
        .unwrap_or(0);
    let limit = q
        .get("limit")
        .map(|s| s.parse::<usize>().map_err(|_| BackendError::InvalidData))
        .transpose()?
        .unwrap_or(default);
    if !(1..=100).contains(&limit) {
        return Err(BackendError::InvalidData);
    }
    Ok((offset, limit))
}
