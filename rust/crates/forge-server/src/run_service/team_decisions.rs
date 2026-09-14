//! Durable bridge between one Codex user-input callback and product questions.
//! The event loop owns transport writes; only a successful write may be acked.
use super::*;
use crate::product_api::team::TeamActor;
use forge_core::RunExecution;
use forge_runtime::{RequestId, RuntimeEvent};

#[derive(Clone, Debug)]
pub(super) struct PendingDecision {
    pub id: String,
    pub request_id: RequestId,
    binding: Value,
}

fn binding(actor: &TeamActor, execution: &RunExecution, callback: &RequestId) -> Value {
    json!({"run_id":actor.run_id,"task_key":actor.task_key,"agent_id":actor.agent_id,
        "claim_digest":hash(actor.claim_token.as_bytes()),"attempt_generation":actor.attempt_generation,
        "execution":execution,"callback":callback})
}

fn authority(
    c: &rusqlite::Connection,
    actor: &TeamActor,
    execution: &RunExecution,
) -> Result<String> {
    if execution.connection.worker_id != actor.agent_id
        || execution.thread_id.as_deref().is_none_or(str::is_empty)
        || execution.turn_id.as_deref().is_none_or(str::is_empty)
    {
        return Err(BackendError::Forbidden);
    }
    let row: Option<(String, String)> = c.query_row(
        "SELECT COALESCE(json_extract(i.frozen,'$.config.project_id'),r.project_id),f.snapshot FROM forge_team_runs f LEFT JOIN forge_run_intents i ON i.id=f.run_id LEFT JOIN runs r ON r.id=f.run_id WHERE f.run_id=?1",
        [&actor.run_id], |r| Ok((r.get(0)?, r.get(1)?)),
    ).optional().map_err(db)?;
    let (project, raw) = row.ok_or(BackendError::Conflict)?;
    let snapshot: Value = serde_json::from_str(&raw).map_err(|_| BackendError::InvalidData)?;
    crate::product_api::team::validate_actor(c, &snapshot, actor)?;
    if !snapshot["agents"][&actor.agent_id]["tools"]
        .as_array()
        .is_some_and(|tools| tools.iter().any(|tool| tool == "request_decision"))
    {
        return Err(BackendError::Forbidden);
    }
    Ok(project)
}

fn question(value: &Value, group: &str) -> Result<Value> {
    let bounded = |key: &str, max: usize| -> Result<&str> {
        text(value, key).and_then(|s| {
            if s.chars().count() <= max {
                Ok(s)
            } else {
                Err(BackendError::InvalidData)
            }
        })
    };
    let original_id = bounded("id", 200)?;
    let header = bounded("header", 200)?;
    let prompt = bounded("question", 8000)?;
    let options = match value.get("options") {
        None | Some(Value::Null) => Vec::new(),
        Some(Value::Array(options)) if options.len() <= 20 => options.clone(),
        _ => return Err(BackendError::InvalidData),
    };
    let mut mapped = Vec::new();
    for (index, option) in options.iter().enumerate() {
        let label = text(option, "label")?;
        let description = option["description"]
            .as_str()
            .ok_or(BackendError::InvalidData)?;
        if label.chars().count() > 200 || description.chars().count() > 2000 {
            return Err(BackendError::InvalidData);
        }
        mapped.push(
            json!({"id":format!("option_{}", index + 1),"label":label,"description":description}),
        );
    }
    let flag = |key: &str| -> Result<bool> {
        value
            .get(key)
            .map_or(Ok(false), |v| v.as_bool().ok_or(BackendError::InvalidData))
    };
    Ok(json!({"question":prompt,"header":header,"options":mapped,
        "allow_text":options.is_empty() || flag("isOther")?,"is_secret":flag("isSecret")?,
        "codex":{"callback_id":group,"question_id":original_id}}))
}

/// Register all questions atomically and retain the precise native callback.
/// Repeating the same event is safe; changing its questions is a conflict.
pub(super) fn register(
    product: &ProductStore,
    actor: &TeamActor,
    execution: &RunExecution,
    event: &RuntimeEvent,
) -> Result<PendingDecision> {
    if event.kind != "item/tool/requestUserInput"
        || event.thread_id != execution.thread_id
        || event.turn_id != execution.turn_id
        || event.payload["threadId"].as_str() != execution.thread_id.as_deref()
        || event.payload["turnId"].as_str() != execution.turn_id.as_deref()
        || event.item_id.as_deref() != event.payload["itemId"].as_str()
        || event.item_id.as_deref().is_none_or(str::is_empty)
    {
        return Err(BackendError::Forbidden);
    }
    let request_id = event.request_id.clone().ok_or(BackendError::InvalidData)?;
    let binding = binding(actor, execution, &request_id);
    let id = hash(binding.to_string().as_bytes());
    let pending = PendingDecision {
        id: id.clone(),
        request_id,
        binding,
    };
    let payload_digest = hash(event.payload.to_string().as_bytes());
    let source = event.payload["questions"]
        .as_array()
        .filter(|q| !q.is_empty() && q.len() <= 3)
        .ok_or(BackendError::InvalidData)?;
    let mut questions = Vec::new();
    let mut seen = BTreeSet::new();
    for native in source {
        let mapped = question(native, &id)?;
        let original_id = text(&mapped["codex"], "question_id")?;
        if !seen.insert(original_id.to_owned()) {
            return Err(BackendError::InvalidData);
        }
        let question_id = hash(json!([id, original_id]).to_string().as_bytes());
        questions.push((question_id, mapped));
    }
    let mut c = product.connect()?;
    let tx = c
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(db)?;
    let project = authority(&tx, actor, execution)?;
    let existing: Option<(String, String)> = tx
        .query_row(
            "SELECT binding,payload_digest FROM forge_team_decision_callbacks WHERE id=?1",
            [&id],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()
        .map_err(db)?;
    if let Some((stored_binding, digest)) = existing {
        if serde_json::from_str::<Value>(&stored_binding).map_err(|_| BackendError::InvalidData)?
            != pending.binding
            || digest != payload_digest
        {
            return Err(BackendError::Conflict);
        }
        tx.commit().map_err(db)?;
        return Ok(pending);
    }
    let stamp = crate::product_api::now(&tx)?;
    let ids: Vec<_> = questions.iter().map(|(id, _)| id).collect();
    tx.execute("INSERT INTO forge_team_decision_callbacks(id,run_id,task_key,agent_id,project_id,binding,payload_digest,question_ids,status,response,created_at,updated_at) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,'pending','{}',?9,?9)",
        params![id,actor.run_id,actor.task_key,actor.agent_id,project,pending.binding.to_string(),payload_digest,json!(ids).to_string(),stamp]).map_err(db)?;
    for (request_id, mapped) in questions {
        let digest = crate::product_api::digest(&mapped);
        tx.execute("INSERT INTO decision_requests(id,run_id,task_key,agent_id,digest,question,status,answer,answered_by,created_at,answered_at) VALUES(?1,?2,?3,?4,?5,?6,'pending','{}','',?7,'')",
            params![request_id,actor.run_id,actor.task_key,actor.agent_id,digest,mapped.to_string(),stamp]).map_err(db)?;
        tx.execute("INSERT INTO events(run_id,kind,data,created_at) VALUES(?1,'human.decision.requested',?2,?3)",
            params![actor.run_id,json!({"request_id":request_id,"task_key":actor.task_key,"agent_id":actor.agent_id,"digest":digest,"question":mapped}).to_string(),stamp]).map_err(db)?;
    }
    tx.commit().map_err(db)?;
    Ok(pending)
}

struct StoredCallback {
    project: String,
    questions: Vec<String>,
    status: String,
    response: Value,
}

fn load(
    c: &rusqlite::Connection,
    actor: &TeamActor,
    execution: &RunExecution,
    pending: &PendingDecision,
) -> Result<StoredCallback> {
    if pending.binding != binding(actor, execution, &pending.request_id) {
        return Err(BackendError::Forbidden);
    }
    let row: Option<(String,String,String,String,String)> = c.query_row(
        "SELECT project_id,binding,question_ids,status,response FROM forge_team_decision_callbacks WHERE id=?1 AND run_id=?2 AND task_key=?3 AND agent_id=?4",
        params![pending.id,actor.run_id,actor.task_key,actor.agent_id],
        |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?)),
    ).optional().map_err(db)?;
    let (project, stored_binding, questions, status, response) =
        row.ok_or(BackendError::Conflict)?;
    if serde_json::from_str::<Value>(&stored_binding).map_err(|_| BackendError::InvalidData)?
        != pending.binding
    {
        return Err(BackendError::Forbidden);
    }
    Ok(StoredCallback {
        project,
        questions: serde_json::from_str(&questions).map_err(|_| BackendError::InvalidData)?,
        status,
        response: serde_json::from_str(&response).map_err(|_| BackendError::InvalidData)?,
    })
}

fn response(
    product: &ProductStore,
    c: &rusqlite::Connection,
    actor: &TeamActor,
    callback: &StoredCallback,
) -> Result<Option<Value>> {
    let mut answers = serde_json::Map::new();
    let mut complete = true;
    for id in &callback.questions {
        let (status, raw_question, raw_answer, user_id, stored_digest): (String,String,String,String,String) = c.query_row(
            "SELECT status,question,answer,answered_by,digest FROM decision_requests WHERE id=?1 AND run_id=?2 AND task_key=?3 AND agent_id=?4",
            params![id,actor.run_id,actor.task_key,actor.agent_id], |r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?)),
        ).map_err(db)?;
        if status == "pending" {
            complete = false;
            continue;
        }
        if !["answered", "rejected"].contains(&status.as_str()) {
            return Err(BackendError::Conflict);
        }
        let user = product
            .user(&user_id)?
            .filter(|user| user.enabled)
            .ok_or(BackendError::Forbidden)?;
        product.authorize_project(&user, &callback.project, true)?;
        let question: Value =
            serde_json::from_str(&raw_question).map_err(|_| BackendError::InvalidData)?;
        let answer: Value =
            serde_json::from_str(&raw_answer).map_err(|_| BackendError::InvalidData)?;
        if crate::product_api::digest(&question) != stored_digest {
            return Err(BackendError::Conflict);
        }
        let mut selected = Vec::new();
        if status == "answered" {
            let choice = answer["choice_id"].as_str().unwrap_or("");
            if !choice.is_empty() {
                let option = question["options"]
                    .as_array()
                    .and_then(|options| options.iter().find(|o| o["id"] == choice))
                    .ok_or(BackendError::InvalidData)?;
                selected.push(text(option, "label")?.to_owned());
            }
            let freeform = answer["text"].as_str().unwrap_or("");
            if !freeform.is_empty() {
                if question["allow_text"] != true {
                    return Err(BackendError::InvalidData);
                }
                selected.push(freeform.to_owned());
            }
            if selected.is_empty() {
                return Err(BackendError::InvalidData);
            }
        }
        answers.insert(
            text(&question["codex"], "question_id")?.to_owned(),
            json!({"answers":selected}),
        );
    }
    Ok(complete.then(|| json!({"answers":answers})))
}

/// Return None until every question is answered. Responses are persisted before
/// returning, and every attempt revalidates the answering users and project ACL.
pub(super) fn poll_response(
    product: &ProductStore,
    actor: &TeamActor,
    execution: &RunExecution,
    pending: &PendingDecision,
) -> Result<Option<Value>> {
    let mut c = product.connect()?;
    let tx = c
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(db)?;
    let callback = load(&tx, actor, execution, pending)?;
    if matches!(callback.status.as_str(), "delivered" | "abandoned") {
        return Ok(None);
    }
    if authority(&tx, actor, execution)? != callback.project {
        return Err(BackendError::Conflict);
    }
    let Some(response) = response(product, &tx, actor, &callback)? else {
        return Ok(None);
    };
    if callback.status == "queued" && callback.response != response {
        return Err(BackendError::Conflict);
    }
    tx.execute("UPDATE forge_team_decision_callbacks SET status='queued',response=?1,updated_at=?2 WHERE id=?3 AND status IN ('pending','queued')",
        params![response.to_string(),crate::product_api::now(&tx)?,pending.id]).map_err(db)?;
    tx.commit().map_err(db)?;
    Ok(Some(response))
}

/// Call only after client.respond succeeds. The exact response and callback must
/// match the queued durable response before marking those answer outboxes sent.
pub(super) fn ack(
    product: &ProductStore,
    actor: &TeamActor,
    execution: &RunExecution,
    pending: &PendingDecision,
    delivered_response: &Value,
) -> Result<()> {
    let mut c = product.connect()?;
    let tx = c
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(db)?;
    let callback = load(&tx, actor, execution, pending)?;
    if callback.status == "delivered" && callback.response == *delivered_response {
        return Ok(());
    }
    if callback.status != "queued" || callback.response != *delivered_response {
        return Err(BackendError::Conflict);
    }
    let stamp = crate::product_api::now(&tx)?;
    for id in &callback.questions {
        tx.execute("UPDATE outbox SET sent=1,error='' WHERE run_id=?1 AND action='answer' AND sent=0 AND json_extract(payload,'$.request_id')=?2 AND json_extract(payload,'$.digest')=(SELECT digest FROM decision_requests WHERE id=?2)",
            params![actor.run_id,id]).map_err(db)?;
    }
    tx.execute("UPDATE forge_team_decision_callbacks SET status='delivered',updated_at=?1 WHERE id=?2 AND status='queued'",params![stamp,pending.id]).map_err(db)?;
    tx.execute("INSERT INTO events(run_id,kind,data,created_at) VALUES(?1,'human.decision.delivered',?2,?3)",
        params![actor.run_id,json!({"callback_id":pending.id,"task_key":actor.task_key,"agent_id":actor.agent_id,"connection_id":execution.connection.connection_id,"thread_id":execution.thread_id,"turn_id":execution.turn_id}).to_string(),stamp]).map_err(db)?;
    tx.commit().map_err(db)?;
    Ok(())
}

/// Abandon a callback whose turn or connection ended. Answers remain audited,
/// but are never acknowledged as delivered or sent into a replacement thread.
pub(super) fn abandon(
    product: &ProductStore,
    actor: &TeamActor,
    execution: &RunExecution,
    pending: &PendingDecision,
    reason: &str,
) -> Result<()> {
    if reason.trim().is_empty() || reason.len() > 500 {
        return Err(BackendError::InvalidData);
    }
    let mut c = product.connect()?;
    let tx = c
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(db)?;
    let callback = load(&tx, actor, execution, pending)?;
    if matches!(callback.status.as_str(), "delivered" | "abandoned") {
        return Ok(());
    }
    let stamp = crate::product_api::now(&tx)?;
    for request in &callback.questions {
        tx.execute("UPDATE decision_requests SET status='expired' WHERE id=?1 AND run_id=?2 AND status='pending'", params![request, actor.run_id]).map_err(db)?;
        tx.execute("UPDATE outbox SET error=?1 WHERE run_id=?2 AND action='answer' AND sent=0 AND json_extract(payload,'$.request_id')=?3", params![reason, actor.run_id, request]).map_err(db)?;
    }
    tx.execute(
        "UPDATE forge_team_decision_callbacks SET status='abandoned',updated_at=?1 WHERE id=?2",
        params![stamp, pending.id],
    )
    .map_err(db)?;
    tx.execute("INSERT INTO events(run_id,kind,data,created_at) VALUES(?1,'human.decision.expired',?2,?3)", params![actor.run_id,json!({"callback_id":pending.id,"task_key":actor.task_key,"agent_id":actor.agent_id,"reason":reason}).to_string(),stamp]).map_err(db)?;
    tx.commit().map_err(db)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::product_api::team;
    use crate::{Backend, RustOwnedBackend};
    use forge_core::RunConnection;

    struct Fixture {
        root: PathBuf,
        product: Arc<ProductStore>,
        backend: RustOwnedBackend,
        user: User,
        actor: TeamActor,
        execution: RunExecution,
    }
    impl Fixture {
        fn new() -> Self {
            let root =
                std::env::temp_dir().join(format!("forge-team-decisions-{}", Uuid::new_v4()));
            fs::create_dir_all(&root).unwrap();
            let product = Arc::new(
                ProductStore::open(
                    root.join("product.db"),
                    None,
                    Some("correct-decision-password"),
                )
                .unwrap(),
            );
            let user = product
                .check_credentials("admin", "correct-decision-password")
                .unwrap()
                .unwrap();
            let backend = RustOwnedBackend::from_product(
                product.clone(),
                root.join("kernel.db"),
                root.join("data"),
                None,
            )
            .unwrap();
            let mut c = product.connect().unwrap();
            c.execute(
                "INSERT INTO projects VALUES('project',?1,'{}','','2026-01-01')",
                [&user.id],
            )
            .unwrap();
            c.execute("INSERT INTO runs VALUES('run','project',?1,'Run','Request','running','{}',NULL,'','','{}',0,0,0,'','2026-01-01','2026-01-01')", [&user.id]).unwrap();
            let snapshot = json!({"entry_agent_id":"lead","agents":{"lead":{"tools":["finish","request_decision"]}},"runtime":{"max_tasks":5}});
            team::initialize_run(&mut c, "run", &snapshot, "Deliver the requested work").unwrap();
            let actor =
                team::claim_task(&mut c, &snapshot, "run", "codex", "lead", 1, 600_000).unwrap();
            let execution = RunExecution {
                connection: RunConnection {
                    worker_id: "lead".into(),
                    connection_id: "lead-connection".into(),
                    generation: 1,
                },
                thread_id: Some("lead-thread".into()),
                turn_id: Some("lead-turn".into()),
            };
            Self {
                root,
                product,
                backend,
                user,
                actor,
                execution,
            }
        }
        fn rust_owned_without_product_run(&self) {
            let config: RunConfig = serde_json::from_value(json!({
                "project_id":"project","owner_id":self.user.id,
                "members":[{"worker_id":"lead","config_revision":"lead-v1","is_coordinator":true}],
                "permission_profile":"ask","working_directory":self.root.join("workspace"),"runtime_home":self.root.join("runtime"),"runtime_version":"test",
                "route_binding":{"provider_id":"provider","provider_revision_id":"revision","model_id":"model","upstream_protocol":"openai_responses","gateway_route_id":"route","responses_base_url":"https://example.invalid/v1","config_digest":"digest","credential_ref":"credential","capability_evidence_ref":"evidence","gateway_version":"test"}
            })).unwrap();
            let frozen: Frozen = serde_json::from_value(json!({
                "id":"run","title":"Run","request":"Request","config":config,
                "provider":{"provider_id":"provider","provider_revision_id":"revision","upstream_base_url":"https://example.invalid/v1","credential_ref":"credential","config_digest":"digest","models":[]},
                "agent":{"id":"lead","role":"planner"},"project":{"id":"project"},"model":{},"encrypted_key":""
            })).unwrap();
            self.product
                .connect()
                .unwrap()
                .execute(
                    "INSERT INTO forge_run_intents VALUES('run',?1,'digest',?2,'2026-01-01')",
                    params![self.user.id, serde_json::to_string(&frozen).unwrap()],
                )
                .unwrap();
            Store::open(self.root.join("kernel.db"))
                .unwrap()
                .execute(
                    "create-native-question-run",
                    Command::CreateBoundRun {
                        run_id: "run".into(),
                        config: Box::new(config),
                    },
                )
                .unwrap();
            self.product
                .connect()
                .unwrap()
                .execute("DELETE FROM runs WHERE id='run'", [])
                .unwrap();
        }
        fn event(&self, callback: i64) -> RuntimeEvent {
            RuntimeEvent {
                kind: "item/tool/requestUserInput".into(),
                thread_id: self.execution.thread_id.clone(),
                turn_id: self.execution.turn_id.clone(),
                item_id: Some(format!("item-{callback}")),
                sequence: callback as u64,
                request_id: Some(RequestId::Integer(callback)),
                payload: json!({"threadId":self.execution.thread_id,"turnId":self.execution.turn_id,"itemId":format!("item-{callback}"),"isBlocking":false,"questions":[
                    {"id":"implementation","header":"Approach","question":"Which implementation should be used?","isOther":true,"options":[{"label":"Rust","description":"Use the Rust backend"},{"label":"Python","description":"Use the legacy backend"}]},
                    {"id":"constraint","header":"Constraint","question":"What requirement must be preserved?","options":null,"isOther":false}
                ]}),
            }
        }
        fn ids(&self, pending: &PendingDecision) -> Vec<String> {
            let raw: String = self
                .product
                .connect()
                .unwrap()
                .query_row(
                    "SELECT question_ids FROM forge_team_decision_callbacks WHERE id=?1",
                    [&pending.id],
                    |r| r.get(0),
                )
                .unwrap();
            serde_json::from_str(&raw).unwrap()
        }
        fn answer(&self, request: &str, mut value: Value) {
            let digest: String = self
                .product
                .connect()
                .unwrap()
                .query_row(
                    "SELECT digest FROM decision_requests WHERE id=?1",
                    [request],
                    |r| r.get(0),
                )
                .unwrap();
            value["digest"] = json!(digest);
            self.backend
                .product_request(
                    &self.user,
                    "POST",
                    &format!("/api/runs/run/questions/{request}/answer"),
                    &BTreeMap::new(),
                    &serde_json::to_vec(&value).unwrap(),
                )
                .unwrap();
        }
        fn sent(&self) -> i64 {
            self.product
                .connect()
                .unwrap()
                .query_row(
                    "SELECT COUNT(*) FROM outbox WHERE action='answer' AND sent=1",
                    [],
                    |r| r.get(0),
                )
                .unwrap()
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.root);
        }
    }

    #[tokio::test]
    async fn persists_multi_question_callback_and_only_acks_after_exact_response_delivery() {
        let f = Fixture::new();
        f.rust_owned_without_product_run();
        let event = f.event(20);
        let pending = register(&f.product, &f.actor, &f.execution, &event).unwrap();
        let same = register(&f.product, &f.actor, &f.execution, &event).unwrap();
        assert_eq!(same.id, pending.id);
        let ids = f.ids(&pending);
        assert_eq!(ids.len(), 2);
        assert_eq!(
            f.product
                .connect()
                .unwrap()
                .query_row("SELECT COUNT(*) FROM decision_requests", [], |r| r
                    .get::<_, i64>(0))
                .unwrap(),
            2
        );
        assert!(poll_response(&f.product, &f.actor, &f.execution, &pending)
            .unwrap()
            .is_none());
        f.answer(
            &ids[0],
            json!({"choice_id":"option_1","text":"Keep compatible HTTP routes"}),
        );
        assert!(poll_response(&f.product, &f.actor, &f.execution, &pending)
            .unwrap()
            .is_none());
        f.answer(&ids[1], json!({"text":"Do not modify Codex"}));
        let response = poll_response(&f.product, &f.actor, &f.execution, &pending)
            .unwrap()
            .unwrap();
        assert_eq!(
            response,
            json!({"answers":{"implementation":{"answers":["Rust","Keep compatible HTTP routes"]},"constraint":{"answers":["Do not modify Codex"]}}})
        );
        assert_eq!(f.sent(), 0);
        assert!(ack(
            &f.product,
            &f.actor,
            &f.execution,
            &pending,
            &json!({"answers":{}})
        )
        .is_err());
        assert_eq!(f.sent(), 0);
        // A newly opened product store sees the durable response after a failed
        // transport write; polling neither consumes nor acknowledges answers.
        let reopened = ProductStore::open(f.product.path(), None, None).unwrap();
        assert_eq!(
            poll_response(&reopened, &f.actor, &f.execution, &pending).unwrap(),
            Some(response.clone())
        );
        let unrelated = register(&f.product, &f.actor, &f.execution, &f.event(21)).unwrap();
        for request in f.ids(&unrelated) {
            f.answer(&request, json!({"reject":true}));
        }
        ack(&reopened, &f.actor, &f.execution, &pending, &response).unwrap();
        assert_eq!(f.sent(), 2);
        ack(&reopened, &f.actor, &f.execution, &pending, &response).unwrap();
        assert_eq!(f.sent(), 2);
        assert!(poll_response(&reopened, &f.actor, &f.execution, &pending)
            .unwrap()
            .is_none());
        assert_eq!(
            poll_response(&reopened, &f.actor, &f.execution, &unrelated).unwrap(),
            Some(json!({"answers":{"implementation":{"answers":[]},"constraint":{"answers":[]}}}))
        );
    }

    #[tokio::test]
    async fn binds_member_claim_execution_and_live_answerer_authority() {
        let f = Fixture::new();
        let event = f.event(22);
        let pending = register(&f.product, &f.actor, &f.execution, &event).unwrap();
        let mut modified = event.clone();
        modified.payload["questions"][0]["question"] = json!("Changed after publication");
        assert!(matches!(
            register(&f.product, &f.actor, &f.execution, &modified),
            Err(BackendError::Conflict)
        ));
        let mut other = f.execution.clone();
        other.thread_id = Some("other-member-thread".into());
        assert!(matches!(
            poll_response(&f.product, &f.actor, &other, &pending),
            Err(BackendError::Forbidden)
        ));
        let mut impostor = f.actor.clone();
        impostor.agent_id = "other-member".into();
        assert!(matches!(
            poll_response(&f.product, &impostor, &f.execution, &pending),
            Err(BackendError::Forbidden)
        ));
        let mut renewed = f.actor.clone();
        renewed.attempt_generation += 1;
        assert!(matches!(
            poll_response(&f.product, &renewed, &f.execution, &pending),
            Err(BackendError::Forbidden)
        ));
        for request in f.ids(&pending) {
            f.answer(&request, json!({"reject":true}));
        }
        let queued = poll_response(&f.product, &f.actor, &f.execution, &pending)
            .unwrap()
            .unwrap();
        // Revalidate every time, including replay of an already queued answer.
        f.product
            .connect()
            .unwrap()
            .execute("UPDATE users SET enabled=0 WHERE id=?1", [&f.user.id])
            .unwrap();
        assert!(matches!(
            poll_response(&f.product, &f.actor, &f.execution, &pending),
            Err(BackendError::Forbidden)
        ));
        assert_eq!(f.sent(), 0);
        f.product
            .connect()
            .unwrap()
            .execute(
                "UPDATE users SET enabled=1,role='viewer' WHERE id=?1",
                [&f.user.id],
            )
            .unwrap();
        assert!(matches!(
            poll_response(&f.product, &f.actor, &f.execution, &pending),
            Err(BackendError::Forbidden)
        ));
        f.product
            .connect()
            .unwrap()
            .execute(
                "UPDATE users SET role='developer' WHERE id=?1",
                [&f.user.id],
            )
            .unwrap();
        f.product
            .connect()
            .unwrap()
            .execute(
                "UPDATE projects SET owner_id='other' WHERE id='project'",
                [],
            )
            .unwrap();
        assert!(poll_response(&f.product, &f.actor, &f.execution, &pending).is_err());
        assert_eq!(f.sent(), 0);
        // ACK represents a write that already succeeded. It records that fact
        // without retroactively losing a delivery if the user was just revoked.
        ack(&f.product, &f.actor, &f.execution, &pending, &queued).unwrap();
        assert_eq!(f.sent(), 2);
    }

    #[tokio::test]
    async fn abandoned_callback_expires_questions_without_acknowledging_answers() {
        let f = Fixture::new();
        f.rust_owned_without_product_run();
        let pending = register(&f.product, &f.actor, &f.execution, &f.event(24)).unwrap();
        let ids = f.ids(&pending);
        f.answer(&ids[0], json!({"choice_id":"option_1"}));
        abandon(
            &f.product,
            &f.actor,
            &f.execution,
            &pending,
            "Turn completed before an answer was delivered",
        )
        .unwrap();
        abandon(
            &f.product,
            &f.actor,
            &f.execution,
            &pending,
            "Already closed",
        )
        .unwrap();
        assert_eq!(f.sent(), 0);
        assert!(poll_response(&f.product, &f.actor, &f.execution, &pending)
            .unwrap()
            .is_none());
        assert!(ack(
            &f.product,
            &f.actor,
            &f.execution,
            &pending,
            &json!({"answers":{}})
        )
        .is_err());
        let c = f.product.connect().unwrap();
        assert_eq!(
            c.query_row(
                "SELECT status FROM decision_requests WHERE id=?1",
                [&ids[1]],
                |r| r.get::<_, String>(0)
            )
            .unwrap(),
            "expired"
        );
        assert_eq!(
            c.query_row(
                "SELECT COUNT(*) FROM events WHERE kind='human.decision.expired'",
                [],
                |r| r.get::<_, i64>(0)
            )
            .unwrap(),
            1
        );
        assert!(c
            .query_row("SELECT error FROM outbox WHERE action='answer'", [], |r| {
                r.get::<_, String>(0)
            })
            .unwrap()
            .contains("before an answer"));
    }

    #[tokio::test]
    async fn registration_rolls_back_whole_question_group_on_storage_failure() {
        let f = Fixture::new();
        f.product.connect().unwrap().execute_batch("CREATE TRIGGER reject_second_question BEFORE INSERT ON decision_requests WHEN json_extract(NEW.question,'$.codex.question_id')='constraint' BEGIN SELECT RAISE(ABORT,'storage failure'); END;").unwrap();
        assert!(register(&f.product, &f.actor, &f.execution, &f.event(23)).is_err());
        let c = f.product.connect().unwrap();
        for table in ["decision_requests", "forge_team_decision_callbacks"] {
            assert_eq!(
                c.query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |r| r
                    .get::<_, i64>(0))
                    .unwrap(),
                0
            );
        }
        assert_eq!(
            c.query_row(
                "SELECT COUNT(*) FROM events WHERE kind='human.decision.requested'",
                [],
                |r| r.get::<_, i64>(0)
            )
            .unwrap(),
            0
        );
    }
}
