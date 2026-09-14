use super::*;
use crate::product_api::{bad, conflict, decode, fields, now, string};

impl RunService {
    pub(super) fn answer_question(
        &self,
        user: &User,
        id: &str,
        request: &str,
        body: &[u8],
    ) -> std::result::Result<ProductReply, ProductError> {
        let input: Value = serde_json::from_slice(body).map_err(|_| bad("Invalid answer body"))?;
        let answer = Answer::parse(&input)?;
        let mut product = self.product.connect()?;
        let tx = product.transaction_with_behavior(TransactionBehavior::Immediate)?;
        self.access_id(user, id, true)?;
        // Serialize the product decision and Rust lifecycle in a consistent
        // product-then-kernel lock order; a concurrent cancel cannot pass between
        // the terminal-state check and the committed answer.
        let mut kernel = rusqlite::Connection::open(&self.path)?;
        kernel.busy_timeout(Duration::from_secs(5))?;
        let kernel_tx = kernel.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let value = apply_answer(&tx, &kernel_tx, user, id, request, &answer)?;
        tx.commit()?;
        kernel_tx.commit()?;
        Ok(ProductReply::json(200, value))
    }
}

struct Answer {
    digest: String,
    choice: String,
    text: String,
    reject: bool,
}
impl Answer {
    fn parse(v: &Value) -> std::result::Result<Self, ProductError> {
        fields(v, &["digest", "choice_id", "text", "reject"])?;
        let digest = string(v, "digest", 64, 64)?;
        if !digest
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err(bad("Expected SHA256 question digest"));
        }
        let choice = if v.get("choice_id").is_some() {
            string(v, "choice_id", 0, 80)?
        } else {
            ""
        };
        let text = if v.get("text").is_some() {
            string(v, "text", 0, 8000)?
        } else {
            ""
        };
        let reject = v
            .get("reject")
            .map(|v| v.as_bool().ok_or_else(|| bad("reject must be boolean")))
            .transpose()?
            .unwrap_or(false);
        Ok(Self {
            digest: digest.into(),
            choice: choice.into(),
            text: text.trim().into(),
            reject,
        })
    }
    fn value(&self) -> Value {
        json!({"choice_id":self.choice,"text":self.text,"reject":self.reject})
    }
}

fn decision(
    c: &rusqlite::Connection,
    id: &str,
    request: &str,
) -> std::result::Result<Value, ProductError> {
    c.query_row("SELECT json_object('id',id,'request_id',id,'run_id',run_id,'task_key',task_key,'agent_id',agent_id,'digest',digest,'status',status,'question',json(question),'answer',json(answer),'answered_by',answered_by,'created_at',created_at,'answered_at',answered_at) FROM decision_requests WHERE id=?1 AND run_id=?2",params![request,id],|r|decode(r.get(0)?)).optional()?.ok_or_else(||BackendError::NotFound.into())
}

fn apply_answer(
    c: &rusqlite::Connection,
    kernel: &rusqlite::Connection,
    user: &User,
    id: &str,
    request: &str,
    answer: &Answer,
) -> std::result::Result<Value, ProductError> {
    let row = decision(c, id, request)?;
    let blocked:bool=c.query_row("SELECT EXISTS(SELECT 1 FROM tool_installations WHERE decision_id=?1) OR EXISTS(SELECT 1 FROM execution_permissions WHERE decision_id=?1)",[request],|r|r.get(0))?;
    if blocked || row["digest"] != answer.digest {
        return Err(conflict());
    }
    let value = answer.value();
    if row["status"] != "pending" {
        return if row["answer"] == value {
            Ok(row)
        } else {
            Err(conflict())
        };
    }
    let status: Option<String> = kernel
        .query_row(
            "SELECT json_extract(snapshot_json,'$.status') FROM runs WHERE id=?1",
            [id],
            |r| r.get(0),
        )
        .optional()?;
    let status = match status {
        Some(s) => s,
        None => c
            .query_row("SELECT status FROM runs WHERE id=?1", [id], |r| r.get(0))
            .optional()?
            .ok_or_else(|| ProductError::from(BackendError::NotFound))?,
    };
    if matches!(
        status.as_str(),
        "completed" | "cancelled" | "needs_attention" | "accepted" | "outcome_unknown"
    ) {
        return Err(conflict());
    }
    let choice = row["question"]["options"]
        .as_array()
        .and_then(|options| options.iter().find(|v| v["id"] == answer.choice));
    if !answer.reject
        && ((answer.choice.is_empty() && answer.text.is_empty())
            || (!answer.text.is_empty() && row["question"]["allow_text"] != true)
            || (!answer.choice.is_empty() && choice.is_none()))
    {
        return Err(bad("Answer does not match the permitted question choices"));
    }
    let at = now(c)?;
    if c.execute("UPDATE decision_requests SET status=?1,answer=?2,answered_by=?3,answered_at=?4 WHERE id=?5 AND run_id=?6 AND digest=?7 AND status='pending'",params![if answer.reject{"rejected"}else{"answered"},value.to_string(),user.id,at,request,id,answer.digest])?!=1{return Err(conflict());}
    c.execute("INSERT INTO outbox(id,run_id,action,payload,sent,error,attempts,created_at) VALUES(?1,?2,'answer',?3,0,'',0,?4)",params![Uuid::new_v4().simple().to_string(),id,json!({"request_id":request,"digest":answer.digest,"rejected":answer.reject,"answer":value,"text":format!("Human answer to question {request}: {value}")}).to_string(),at])?;
    c.execute("INSERT INTO events(run_id,kind,data,created_at) VALUES(?1,'human.decision.answered',?2,?3)",params![id,json!({"request_id":request,"task_key":row["task_key"],"agent_id":row["agent_id"],"actor":user.id,"digest":answer.digest,"answer":value,"choice_label":choice.map(|v|v["label"].clone()).unwrap_or(json!(""))}).to_string(),at])?;
    decision(c, id, request)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{Backend, RustOwnedBackend};

    #[tokio::test]
    async fn answers_enforce_lifecycle_replay_and_atomic_audit_through_product_dispatch() {
        let root = std::env::temp_dir().join(format!("forge-answer-{}", Uuid::new_v4()));
        fs::create_dir_all(&root).unwrap();
        let store = Arc::new(
            ProductStore::open(
                root.join("product.db"),
                None,
                Some("correct-answer-test-password"),
            )
            .unwrap(),
        );
        let user = store
            .check_credentials("admin", "correct-answer-test-password")
            .unwrap()
            .unwrap();
        let backend = RustOwnedBackend::from_product(
            store.clone(),
            root.join("runs.db"),
            root.join("data"),
            None,
        )
        .unwrap();
        let c = store.connect().unwrap();
        c.execute(
            "INSERT INTO projects VALUES('project',?1,'{}','','2026-01-01')",
            [&user.id],
        )
        .unwrap();
        c.execute("INSERT INTO runs VALUES('run','project',?1,'Run','Request','running','{}',NULL,'','','{}',0,0,0,'','2026-01-01','2026-01-01')",[&user.id]).unwrap();
        let seed = |id: &str| {
            c.execute("INSERT INTO decision_requests VALUES(?1,'run','task','agent',?2,?3,'pending','{}','','2026-01-01','')",params![id,"a".repeat(64),json!({"question":"Choose","options":[{"id":"approve","label":"Proceed"},{"id":"other","label":"Alternative"}],"allow_text":true}).to_string()]).unwrap();
        };
        let call = |request: &str, input: Value| {
            backend.product_request(
                &user,
                "POST",
                &format!("/api/runs/run/questions/{request}/answer"),
                &BTreeMap::new(),
                &serde_json::to_vec(&input).unwrap(),
            )
        };
        let approved =
            json!({"digest":"a".repeat(64),"choice_id":"approve","text":"  Explanation  "});
        seed("first");
        let answer = call("first", approved.clone()).unwrap().body;
        assert_eq!(answer["status"], "answered");
        assert_eq!(answer["answer"]["text"], "Explanation");
        assert_eq!(answer["request_id"], "first");
        assert_eq!(answer["task_key"], "task");
        assert_eq!(answer["answered_by"], user.id);
        assert!(!answer["answered_at"].as_str().unwrap().is_empty());
        c.execute("UPDATE runs SET status='completed' WHERE id='run'", [])
            .unwrap();
        assert_eq!(call("first", approved.clone()).unwrap().body, answer);
        assert_eq!(
            call(
                "first",
                json!({"digest":"a".repeat(64),"choice_id":"other"})
            )
            .unwrap_err()
            .status,
            409
        );
        seed("terminal");
        assert_eq!(call("terminal", approved.clone()).unwrap_err().status, 409);
        assert_eq!(
            c.query_row("SELECT COUNT(*) FROM outbox", [], |r| r.get::<_, i64>(0))
                .unwrap(),
            1
        );
        let event: Value = c
            .query_row(
                "SELECT data FROM events WHERE kind='human.decision.answered'",
                [],
                |r| decode(r.get(0)?),
            )
            .unwrap();
        assert_eq!(event["choice_label"], "Proceed");
        assert_eq!(event["answer"], answer["answer"]);
        c.execute("UPDATE runs SET status='running' WHERE id='run'", [])
            .unwrap();
        seed("fault");
        c.execute_batch("CREATE TRIGGER fail_answer_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT,'injected storage failure'); END;").unwrap();
        assert!(call("fault", approved.clone()).is_err());
        assert_eq!(
            c.query_row(
                "SELECT status FROM decision_requests WHERE id='fault'",
                [],
                |r| r.get::<_, String>(0)
            )
            .unwrap(),
            "pending"
        );
        c.execute_batch("DROP TRIGGER fail_answer_outbox").unwrap();
        seed("scoped");
        c.execute("INSERT INTO execution_permissions VALUES('grant','run','task','agent','scoped',?1,'echo test',0,'[]','Reason','pending','','','','{}','','')",["b".repeat(64)]).unwrap();
        assert_eq!(call("scoped", approved.clone()).unwrap_err().status, 409);
        assert_eq!(
            call("terminal", json!({"digest":"bad","reject":true}))
                .unwrap_err()
                .status,
            422
        );
        assert_eq!(
            call("terminal", json!({"digest":"a".repeat(64),"reject":"true"}))
                .unwrap_err()
                .status,
            422
        );
        assert_eq!(
            call(
                "terminal",
                json!({"digest":"a".repeat(64),"choice_id":"missing"})
            )
            .unwrap_err()
            .status,
            422
        );
        let mut kernel = Store::open(root.join("runs.db")).unwrap();
        let config = RunConfig {
            project_id: "project".into(),
            owner_id: user.id.clone(),
            members: vec![RunMember {
                worker_id: "agent".into(),
                config_revision: "agent-v1".into(),
                role: String::new(),
                instructions: String::new(),
                tools: Vec::new(),
                skills: Vec::new(),
                permission_summary: None,
                is_coordinator: true,
                communication_recipients: Vec::new(),
            }],
            permission_profile: PermissionProfile::Ask,
            working_directory: root.join("workspace"),
            runtime_home: root.join("runtime"),
            runtime_version: "test".into(),
            route_binding: RouteBinding {
                provider_id: "provider".into(),
                provider_revision_id: "revision".into(),
                model_id: "model".into(),
                upstream_protocol: "openai_responses".into(),
                gateway_route_id: "route".into(),
                responses_base_url: "https://example.invalid/v1".into(),
                config_digest: "digest".into(),
                credential_ref: "credential".into(),
                capability_evidence_ref: "evidence".into(),
                gateway_version: "test".into(),
            },
        };
        kernel
            .execute(
                "create",
                Command::CreateBoundRun {
                    run_id: "run".into(),
                    config: Box::new(config),
                },
            )
            .unwrap();
        let run = kernel.get_run("run").unwrap().unwrap();
        kernel
            .execute(
                "cancel",
                Command::TransitionRun {
                    run_id: "run".into(),
                    expected_version: run.version,
                    transition: RunTransition::Cancel {
                        evidence_ref: "test lifecycle".into(),
                    },
                },
            )
            .unwrap();
        assert_eq!(call("terminal", approved).unwrap_err().status, 409);
        assert_eq!(
            c.query_row("SELECT COUNT(*) FROM events", [], |r| r.get::<_, i64>(0))
                .unwrap(),
            1
        );
        drop(backend);
        drop(c);
        drop(store);
        fs::remove_dir_all(root).unwrap();
    }
}
