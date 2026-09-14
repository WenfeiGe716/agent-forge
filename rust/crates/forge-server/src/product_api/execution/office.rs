use super::*;

impl ProductApi {
    pub(super) fn office_state(
        &self,
        c: &Connection,
        u: &User,
        q: &BTreeMap<String, String>,
    ) -> Result<Value> {
        let after = q
            .get("after")
            .map(|s| s.parse::<i64>().map_err(|_| bad("Invalid office cursor")))
            .transpose()?;
        let limit = q
            .get("limit")
            .map(|s| {
                s.parse::<usize>()
                    .map_err(|_| bad("Invalid office page size"))
            })
            .transpose()?
            .unwrap_or(200);
        if after.is_some_and(|x| x < 0) || !(1..=500).contains(&limit) {
            return Err(bad("Invalid office pagination"));
        }
        let stamp = now(c)?;
        let settings = config(c, "office_visual", office_defaults())?;
        // Two databases own independent event sequences. This durable index gives
        // the frontend a stable global cursor without rewriting source events.
        c.execute_batch("CREATE TABLE IF NOT EXISTS forge_office_events(id INTEGER PRIMARY KEY AUTOINCREMENT,source TEXT NOT NULL,source_id INTEGER NOT NULL,run_id TEXT NOT NULL,kind TEXT NOT NULL,data TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(source,source_id));CREATE INDEX IF NOT EXISTS forge_office_events_run ON forge_office_events(run_id,id);")?;
        c.execute("INSERT INTO forge_office_events(source,source_id,run_id,kind,data,created_at) SELECT 'product',id,run_id,kind,data,created_at FROM events WHERE run_id IS NOT NULL AND id>COALESCE((SELECT MAX(source_id) FROM forge_office_events WHERE source='product'),0) ORDER BY id",[])?;
        let mut ids = c
            .prepare("SELECT id FROM runs")?
            .query_map([], |r| r.get::<_, String>(0))?
            .collect::<std::result::Result<BTreeSet<_>, _>>()?;
        if let Some(db) = self.runtime_db()? {
            ids.extend(
                db.prepare("SELECT id FROM runs")?
                    .query_map([], |r| r.get::<_, String>(0))?
                    .collect::<std::result::Result<Vec<_>, _>>()?,
            );
            let cursor: i64 = c.query_row(
                "SELECT COALESCE(MAX(source_id),0) FROM forge_office_events WHERE source='rust'",
                [],
                |r| r.get(0),
            )?;
            let mut s=db.prepare("SELECT id,run_id,kind,payload_json,strftime('%Y-%m-%dT%H:%M:%fZ',created_at_ms/1000.0,'unixepoch') FROM events WHERE id>?1 ORDER BY id")?;
            for item in s.query_map([cursor], |r| {
                Ok((
                    r.get::<_, i64>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                    decode(r.get(3)?)?,
                    r.get::<_, String>(4)?,
                ))
            })? {
                let (id, rid, kind, data, at) = item?;
                let mut event = crate::Event {
                    id,
                    run_id: rid,
                    kind,
                    data,
                    created_at: at,
                };
                if let Some(service) = self.runtime.as_ref().and_then(|r| r.service.upgrade()) {
                    service.enrich_event(&mut event)?;
                }
                c.execute("INSERT INTO forge_office_events(source,source_id,run_id,kind,data,created_at) VALUES('rust',?1,?2,?3,?4,?5)",params![event.id,event.run_id,event.kind,crate::python_sqlite::redact(event.data).to_string(),event.created_at])?;
            }
        }
        let agents = configs(c, ConfigTable::Agent)?;
        let mut records = BTreeMap::new();
        for agent in agents {
            let id = string(&agent, "id", 1, 64)?.to_owned();
            records.insert(id.clone(),json!({"agent_id":id,"busy":false,"phase":"idle","label":"Idle","idle_since":settings["epoch"].as_str().unwrap_or("2026-01-01T00:00:00+00:00"),"run_id":null,"task_key":null,"evidence_ids":[],"scope":"global","tool":null,"run_count":0,"updated_at":stamp}));
        }
        let mut visible_ids = BTreeSet::new();
        for id in ids {
            let run = self.run(c, &id)?;
            let known = project(c, u, run["project_id"].as_str().unwrap_or("")).is_ok();
            if known {
                visible_ids.insert(id.clone());
            }
            let status = run["status"].as_str().unwrap_or("");
            let finished = matches!(status, "accepted" | "completed" | "cancelled");
            let members = run["snapshot"]["agents"]
                .as_object()
                .ok_or_else(|| bad("Invalid frozen office membership"))?;
            // A run-level failure/approval belongs to the task owner or the
            // specific pending task, not every teammate in the snapshot.
            // Keeping this projection task-scoped prevents one failed member
            // from making the entire office appear blocked.
            let scoped_members: Vec<&String> = if status == "needs_attention"
                || status == "outcome_unknown"
            {
                let affected: Vec<&String> = run["tasks"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .filter(|task| {
                        matches!(
                            task["status"].as_str(),
                            Some("failed" | "needs_attention" | "outcome_unknown")
                        )
                    })
                    .filter_map(|task| task["agent_id"].as_str())
                    .filter_map(|id| members.keys().find(|key| key.as_str() == id))
                    .collect();
                if affected.is_empty() {
                    run["snapshot"]["entry_agent_id"]
                        .as_str()
                        .and_then(|id| members.keys().find(|key| key.as_str() == id))
                        .into_iter()
                        .collect()
                } else {
                    affected
                }
            } else if status.starts_with("awaiting_") {
                let pending: Vec<&String> = run["tasks"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .filter(|task| {
                        matches!(
                            task["status"].as_str(),
                            Some("waiting_decision" | "awaiting_approval" | "awaiting_acceptance")
                        )
                    })
                    .filter_map(|task| task["agent_id"].as_str())
                    .filter_map(|id| members.keys().find(|key| key.as_str() == id))
                    .collect();
                if pending.is_empty() {
                    run["snapshot"]["entry_agent_id"]
                        .as_str()
                        .and_then(|id| members.keys().find(|key| key.as_str() == id))
                        .into_iter()
                        .collect()
                } else {
                    pending
                }
            } else {
                members.keys().collect()
            };
            for aid in scoped_members {
                let Some(old) = records.get_mut(aid) else {
                    continue;
                };
                if finished {
                    if known
                        && old["busy"] == false
                        && run["updated_at"].as_str() > old["idle_since"].as_str()
                    {
                        old["idle_since"] = run["updated_at"].clone();
                    }
                    continue;
                }
                let count = old["run_count"].as_u64().unwrap_or(0) + u64::from(known);
                old["run_count"] = json!(count);
                let task = run["tasks"].as_array().and_then(|tasks| {
                    tasks
                        .iter()
                        .find(|t| t["agent_id"] == *aid && t["status"] == "running")
                });
                let mut phase = if !known {
                    "reserved"
                } else if matches!(status, "needs_attention" | "outcome_unknown") {
                    "attention"
                } else if status == "paused" {
                    "paused"
                } else if status.starts_with("awaiting_")
                    || matches!(status, "submitted" | "integrated")
                    || run["pending_approval"] == true
                {
                    "approval"
                } else if task.is_some() {
                    "thinking"
                } else {
                    "waiting"
                };
                let mut evidence = Vec::<i64>::new();
                let mut tool = Value::Null;
                if known && phase == "thinking" {
                    let event:Option<(i64,String,Value)>=c.query_row("SELECT id,kind,data FROM forge_office_events WHERE run_id=?1 AND json_extract(data,'$.agent_id')=?2 AND (kind IN ('tool.started','tool.completed','task.finished') OR kind LIKE 'model.%' OR kind LIKE 'runtime.item.%') ORDER BY id DESC LIMIT 1",params![id,aid],|r|Ok((r.get(0)?,r.get(1)?,decode(r.get(2)?)?))).optional()?;
                    if let Some((eid, kind, data)) = event {
                        evidence.push(eid);
                        if kind == "tool.started" {
                            tool = data["tool"].clone();
                            phase = match tool.as_str() {
                                Some("read_file" | "list_files" | "search_files") => "reading",
                                Some("write_file" | "apply_patch") => "writing",
                                Some("run_command") => "testing",
                                Some("send_message") => "communicating",
                                _ => "thinking",
                            };
                        } else if kind == "runtime.item.started" {
                            phase = match data.pointer("/payload/item/type").and_then(Value::as_str)
                            {
                                Some("commandExecution") => "testing",
                                Some("fileChange") => "writing",
                                _ => "thinking",
                            };
                        }
                    }
                }
                let old_phase = old["phase"].as_str().unwrap_or("idle");
                if old["busy"] == false
                    || rank(phase) > rank(old_phase)
                    || rank(phase) == rank(old_phase)
                        && known
                        && id.as_str() < old["run_id"].as_str().unwrap_or("")
                {
                    let candidate = json!({"busy":true,"phase":phase,"label":label(phase),"idle_since":null,"run_id":if known{json!(id)}else{Value::Null},"task_key":if known{task.map(|t|t["key"].clone()).unwrap_or(Value::Null)}else{Value::Null},"evidence_ids":evidence,"scope":if known{"authorized"}else{"reserved"},"tool":tool,"updated_at":if known{run["updated_at"].clone()}else{json!(stamp)}});
                    old.as_object_mut()
                        .unwrap()
                        .extend(candidate.as_object().unwrap().clone());
                }
            }
        }
        let visible =
            serde_json::to_string(&visible_ids).map_err(|_| bad("Invalid office scope"))?;
        let mut events = Vec::new();
        let mut has_more = false;
        let cursor;
        if let Some(after) = after {
            let mut s=c.prepare("SELECT json_object('id',id,'run_id',run_id,'kind',kind,'data',json(data),'created_at',created_at) FROM forge_office_events WHERE id>?1 AND run_id IN (SELECT value FROM json_each(?2)) ORDER BY id LIMIT ?3")?;
            events = s
                .query_map(params![after, visible, limit + 1], |r| decode(r.get(0)?))?
                .collect::<std::result::Result<Vec<_>, _>>()?;
            has_more = events.len() > limit;
            events.truncate(limit);
            cursor = events
                .last()
                .and_then(|v| v["id"].as_i64())
                .unwrap_or(after);
        } else {
            cursor=c.query_row("SELECT COALESCE(MAX(id),0) FROM forge_office_events WHERE run_id IN (SELECT value FROM json_each(?1))",[&visible],|r|r.get::<_,i64>(0))?;
        }
        let meetings=c.prepare("SELECT json_object('id',id,'run_id',run_id,'host_id',host_id,'participants',json(participants),'subject',subject,'agenda',agenda,'status',status,'outcome',json(outcome),'created_at',created_at,'ended_at',ended_at,'source','business','speaker_ids',json('[]')) FROM meetings WHERE status='active' AND run_id IN (SELECT value FROM json_each(?1))")?.query_map([visible],|r|decode(r.get(0)?))?.collect::<std::result::Result<Vec<_>,_>>()?;
        Ok(
            json!({"meetings":meetings,"server_time":stamp,"cursor":cursor,"has_more":has_more,"events":events.into_iter().map(crate::python_sqlite::redact).collect::<Vec<_>>(),"presence":records.into_values().collect::<Vec<_>>(),"settings":settings,"complete":true,"read_only_projection":true}),
        )
    }
}
fn rank(phase: &str) -> u8 {
    match phase {
        "testing" | "writing" => 100,
        "reading" | "reviewing" => 95,
        "communicating" => 92,
        "thinking" => 90,
        "attention" => 80,
        "paused" => 75,
        "reserved" => 70,
        "approval" => 60,
        "waiting" => 20,
        _ => 0,
    }
}
fn label(phase: &str) -> &'static str {
    match phase {
        "reserved" => "Reserved",
        "attention" => "Needs attention",
        "paused" => "Paused",
        "approval" => "Awaiting approval",
        "testing" => "Executing command",
        "writing" => "Writing files",
        "reading" => "Reading files",
        "thinking" => "Working",
        _ => "Waiting",
    }
}
