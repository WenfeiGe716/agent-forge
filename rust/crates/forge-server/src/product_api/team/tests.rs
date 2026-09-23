use super::*;

struct Fixture {
    c: Connection,
    snapshot: Value,
    lead: TeamActor,
}
impl Fixture {
    fn new() -> Self {
        let mut c = Connection::open_in_memory().unwrap();
        c.execute_batch(crate::product_store::PRODUCT_SCHEMA)
            .unwrap();
        let snapshot = json!({"entry_agent_id":"lead","runtime":{"max_tasks":5,"max_agent_messages":5},"agents":{
            "lead":{"name":"Lead","role":"planner","instructions":"Coordinate useful work","tools":["finish"],"skills":[]},
            "dev":{"name":"Developer","role":"developer","instructions":"Implement code","tools":["finish","read_file"],"skills":[{"name":"rust","version":2,"description":"Rust development"}]},
            "qa":{"name":"QA","role":"tester","instructions":"Verify changes","tools":["finish"],"skills":[]}
        }});
        initialize_run(&mut c, "run", &snapshot, "Build the requested feature").unwrap();
        let lead = claim_task(&mut c, &snapshot, "run", "codex", "lead", 1, 600_000).unwrap();
        Self { c, snapshot, lead }
    }
    fn call(&mut self, actor: &TeamActor, op: &str, name: &str, args: Value) -> Result<Value> {
        team_call(&mut self.c, &self.snapshot, actor, op, name, &args)
    }
    fn delegate(&mut self, recipient: &str, op: &str) -> Value {
        self.call(&self.lead.clone(),op,"assign_work",json!({"recipient_id":recipient,"title":"Complete specific work","instruction":"Provide implementation and evidence","acceptance":"All scoped checks pass"})).unwrap()
    }
    fn worker(&mut self, recipient: &str, op: &str) -> TeamActor {
        let work = self.delegate(recipient, op);
        claim_task(
            &mut self.c,
            &self.snapshot,
            "run",
            work["task_key"].as_str().unwrap(),
            recipient,
            work["version"].as_i64().unwrap(),
            600_000,
        )
        .unwrap()
    }
    fn count(&self, table: &str) -> i64 {
        self.c
            .query_row(&format!("SELECT count(*) FROM {table}"), [], |r| r.get(0))
            .unwrap()
    }
}

#[test]
fn runtime_members_delegate_message_ack_and_submit_with_atomic_receipts() {
    let mut f = Fixture::new();
    let lead = f.lead.clone();
    let directory = f.call(&lead, "inspect", "inspect_team", json!({})).unwrap();
    assert_eq!(directory["members"].as_array().unwrap().len(), 3);
    assert!(directory.to_string().contains("Rust development"));
    assert!(!directory.to_string().contains("claim_token"));
    let dev = f.worker("dev", "assign-dev");
    let qa = f.worker("qa", "assign-qa");
    let args = json!({"recipient_id":"qa","body":"Please verify the implemented behavior","request_reply":false});
    let sent = f
        .call(&dev, "message", "send_team_message", args.clone())
        .unwrap();
    let counts = (
        f.count("agent_messages"),
        f.count("events"),
        f.count("outbox"),
    );
    assert_eq!(
        f.call(&dev, "message", "send_team_message", args).unwrap(),
        sent
    );
    assert_eq!(
        counts,
        (
            f.count("agent_messages"),
            f.count("events"),
            f.count("outbox")
        )
    );
    let inbox = f.call(&qa, "inbox", "read_inbox", json!({})).unwrap();
    assert_eq!(inbox["messages"][0]["sender_id"], "dev");
    f.call(
        &qa,
        "ack",
        "mark_included",
        json!({"message_ids":[sent["message_id"]]}),
    )
    .unwrap();
    assert!(
        f.call(&qa, "inbox2", "read_inbox", json!({})).unwrap()["messages"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    let result = json!({"outcome":"done","summary":"Verification complete","notes":"Scoped integration checks passed with recorded evidence.","deliverables":[],"evidence_task_keys":[]});
    let submitted = f
        .call(&qa, "result", "submit_result", result.clone())
        .unwrap();
    assert_eq!(submitted["status"], "done");
    assert_eq!(
        submitted,
        f.call(&qa, "result", "submit_result", result).unwrap()
    );
    assert_eq!(
        task(&f.c, "run", &qa.task_key).unwrap().state.status,
        TaskStatus::Submitted
    );
}

#[test]
fn authenticated_user_message_targets_frozen_member_and_is_durable() {
    let f = Fixture::new();
    let user = User {
        id: "human-1".into(),
        username: "operator".into(),
        role: "developer".into(),
        enabled: true,
    };
    let sent = send_user_message(
        &f.c,
        &f.snapshot,
        &user,
        "run",
        "dev",
        "Please report the current implementation state",
    )
    .unwrap();
    assert_eq!(sent["sender_id"], "human-1");
    assert_eq!(sent["recipient_id"], "dev");
    assert_eq!(sent["delivered"], false);
    let row: (String, String, String, String) =
        f.c.query_row(
            "SELECT task_key,sender_id,recipient_id,body FROM agent_messages WHERE id=?1",
            [&sent["message_id"].as_str().unwrap()],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        )
        .unwrap();
    assert_eq!(
        row,
        (
            "human".into(),
            "human-1".into(),
            "dev".into(),
            "Please report the current implementation state".into()
        )
    );
    let event: String = f
        .c
        .query_row(
            "SELECT data FROM events WHERE run_id='run' AND kind='team.message.sent' ORDER BY id DESC LIMIT 1",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(decode(event).unwrap()["sender_kind"], "user");
    let viewer = User {
        role: "viewer".into(),
        ..user
    };
    assert_eq!(
        send_user_message(&f.c, &f.snapshot, &viewer, "run", "dev", "blocked")
            .unwrap_err()
            .status,
        403
    );
    assert_eq!(
        send_user_message(
            &f.c,
            &f.snapshot,
            &User {
                id: "human-1".into(),
                username: "operator".into(),
                role: "developer".into(),
                enabled: true,
            },
            "run",
            "missing",
            "blocked"
        )
        .unwrap_err()
        .status,
        404
    );
}

#[test]
fn actor_forgery_conflicting_operations_and_non_coordinator_delegation_fail() {
    let mut f = Fixture::new();
    let dev = f.worker("dev", "assign-dev");
    assert_eq!(
        f.call(
            &dev,
            "forged",
            "send_team_message",
            json!({"sender_id":"lead","recipient_id":"qa","body":"Forged"})
        )
        .unwrap_err()
        .status,
        422
    );
    assert_eq!(f.call(&dev,"delegate","assign_work",json!({"recipient_id":"qa","title":"Work","instruction":"Do work","acceptance":"Evidence"})).unwrap_err().status,403);
    let first = f.delegate("qa", "same-operation");
    let before = f.count("tasks");
    let conflict=f.call(&f.lead.clone(),"same-operation","assign_work",json!({"recipient_id":"dev","title":"Other work","instruction":"Different","acceptance":"Evidence"})).unwrap_err();
    assert_eq!(conflict.status, 409);
    assert_eq!(f.count("tasks"), before);
    assert_eq!(first["recipient_id"], "qa");
    let mut forged = dev.clone();
    forged.agent_id = "qa".into();
    assert_eq!(
        f.call(&forged, "fake", "inspect_team", json!({}))
            .unwrap_err()
            .status,
        409
    );
}

#[test]
fn coordinator_can_create_bounded_subagent_and_reclamation_is_idempotent() {
    let mut f = Fixture::new();
    let child = f
        .call(
            &f.lead.clone(),
            "create-child",
            "create_subagent",
            json!({
                "recipient_id":"dev",
                "title":"Bounded implementation",
                "instruction":"Implement only the requested change",
                "acceptance":"Record the changed files and verification"
            }),
        )
    .unwrap();
    assert_eq!(child["recipient_id"], "dev");
    let key = child["task_key"].as_str().unwrap();
    let spec: String = f
        .c
        .query_row("SELECT spec FROM tasks WHERE run_id='run' AND key=?1", [key], |r| r.get(0))
        .unwrap();
    assert_eq!(decode(spec).unwrap()["assignment_type"], "subagent");
    let spec: String = f
        .c
        .query_row(
            "SELECT spec FROM tasks WHERE run_id='run' AND key=?1",
            [key],
            |r| r.get(0),
        )
        .unwrap();
    let spec: Value = serde_json::from_str(&spec).unwrap();
    assert_eq!(spec["assignment_type"], "subagent");
    record_activation_reclaimed(
        &mut f.c,
        "run",
        key,
        "dev",
        "activation-1",
        "uncertain",
        "test-runtime-closed",
    )
    .unwrap();
    let before: i64 = f
        .c
        .query_row(
            "SELECT count(*) FROM events WHERE kind='team.activation.reclaimed'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    record_activation_reclaimed(
        &mut f.c,
        "run",
        key,
        "dev",
        "activation-1",
        "uncertain",
        "duplicate-receipt",
    )
    .unwrap();
    let after: i64 = f
        .c
        .query_row(
            "SELECT count(*) FROM events WHERE kind='team.activation.reclaimed'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(before, 1);
    assert_eq!(after, before);
}

#[test]
fn claims_reject_wrong_member_stale_version_expiry_and_generation() {
    let mut f = Fixture::new();
    let pending = f.delegate("dev", "delegation");
    let key = pending["task_key"].as_str().unwrap();
    assert_eq!(
        claim_task(&mut f.c, &f.snapshot, "run", key, "qa", 1, 1000)
            .unwrap_err()
            .status,
        403
    );
    assert_eq!(
        claim_task(&mut f.c, &f.snapshot, "run", key, "dev", 0, 1000)
            .unwrap_err()
            .status,
        409
    );
    let actor = claim_task(&mut f.c, &f.snapshot, "run", key, "dev", 1, 600_000).unwrap();
    assert_eq!(
        claim_task(&mut f.c, &f.snapshot, "run", key, "dev", 2, 1000)
            .unwrap_err()
            .status,
        409
    );
    let mut stale = actor.clone();
    stale.attempt_generation += 1;
    assert_eq!(
        f.call(&stale, "stale", "inspect_team", json!({}))
            .unwrap_err()
            .status,
        409
    );
    let mut expired = task(&f.c, "run", key).unwrap();
    expired.state.claim.as_mut().unwrap().expires_at_ms = 1;
    save_task(&f.c, &mut expired, "running").unwrap();
    assert_eq!(f.call(&actor,"expired","submit_result",json!({"outcome":"done","summary":"Done","notes":"Long enough evidence description for a real work report"})).unwrap_err().status,409);
    assert_eq!(
        task(&f.c, "run", key).unwrap().state.status,
        TaskStatus::Running
    );
}

#[test]
fn delivery_receipts_are_recipient_bound_and_batch_is_atomic() {
    let mut f = Fixture::new();
    let lead = f.lead.clone();
    let dev = f.worker("dev", "dev");
    let qa = f.worker("qa", "qa");
    let a = f
        .call(
            &lead,
            "m1",
            "send_team_message",
            json!({"recipient_id":"dev","body":"Dev work"}),
        )
        .unwrap();
    let b = f
        .call(
            &lead,
            "m2",
            "send_team_message",
            json!({"recipient_id":"qa","body":"QA work"}),
        )
        .unwrap();
    assert_eq!(
        f.call(
            &dev,
            "ack",
            "mark_included",
            json!({"message_ids":[a["message_id"],b["message_id"]]})
        )
        .unwrap_err()
        .status,
        403
    );
    assert_eq!(
        f.call(&dev, "inbox", "read_inbox", json!({})).unwrap()["messages"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    assert_eq!(
        f.call(&qa, "inbox", "read_inbox", json!({})).unwrap()["messages"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    let acknowledged = |c: &Connection| -> i64 {
        c.query_row(
            "SELECT count(*) FROM outbox WHERE action='team_message' AND sent=1",
            [],
            |r| r.get(0),
        )
        .unwrap()
    };
    assert_eq!(
        acknowledged(&f.c),
        0,
        "Rejected batch must acknowledge no outbox items"
    );
    f.call(
        &dev,
        "valid-ack",
        "mark_message_delivered",
        json!({"message_ids":[a["message_id"]]}),
    )
    .unwrap();
    assert_eq!(acknowledged(&f.c), 1);
    f.call(
        &dev,
        "repeated-ack",
        "mark_message_delivered",
        json!({"message_ids":[a["message_id"]]}),
    )
    .unwrap();
    assert_eq!(acknowledged(&f.c), 1);
    let attempts: i64 =
        f.c.query_row(
            "SELECT sum(attempts) FROM outbox WHERE action='team_message'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(
        attempts, 1,
        "A replay must not manufacture delivery attempts"
    );
    assert_eq!(
        f.call(&qa, "still-pending", "read_inbox", json!({}))
            .unwrap()["messages"][0]["id"],
        b["message_id"]
    );
}

#[test]
fn budgets_communication_and_terminal_run_prevent_new_side_effects() {
    let mut f = Fixture::new();
    f.snapshot["agents"]["dev"]["communication"] = json!({"scope":"custom","recipients":["lead"]});
    f.snapshot["runtime"]["max_tasks"] = json!(1);
    f.c.execute(
        "UPDATE forge_team_runs SET snapshot=?1,snapshot_digest=?2",
        params![encode(&f.snapshot), digest(&f.snapshot)],
    )
    .unwrap();
    let dev = f.worker("dev", "dev");
    assert_eq!(
        f.call(
            &dev,
            "blocked-peer",
            "send_team_message",
            json!({"recipient_id":"qa","body":"No authorization"})
        )
        .unwrap_err()
        .status,
        403
    );
    assert_eq!(f.call(&f.lead.clone(),"over-budget","assign_work",json!({"recipient_id":"qa","title":"Extra work","instruction":"Work","acceptance":"Evidence"})).unwrap_err().status,409);
    let before = (f.count("events"), f.count("outbox"), f.count("steps"));
    set_run_status(&mut f.c, "run", "completed").unwrap();
    assert_eq!(
        f.call(
            &dev,
            "late-message",
            "send_team_message",
            json!({"recipient_id":"lead","body":"After finish"})
        )
        .unwrap_err()
        .status,
        409
    );
    assert_eq!(
        (f.count("events"), f.count("outbox"), f.count("steps")),
        (before.0 + 1, before.1, before.2)
    );
}

#[test]
fn failed_result_does_not_clear_claim_or_create_receipt() {
    let mut f = Fixture::new();
    let dev = f.worker("dev", "dev");
    let before = (f.count("events"), f.count("outbox"), f.count("steps"));
    assert_eq!(
        f.call(
            &dev,
            "unsafe-path",
            "submit_result",
            json!({"outcome":"done","summary":"Done","deliverables":["../outside"]})
        )
        .unwrap_err()
        .status,
        422
    );
    assert_eq!(
        task(&f.c, "run", &dev.task_key).unwrap().state.status,
        TaskStatus::Running
    );
    assert_eq!(
        before,
        (f.count("events"), f.count("outbox"), f.count("steps"))
    );
}

#[test]
fn a_waiter_resumes_once_after_its_frozen_replies_finish_and_fences_old_claims() {
    let mut f = Fixture::new();
    let dev = f.worker("dev", "delegate-dev");
    let waiting = json!({"outcome":"waiting","summary":"Need teammate evidence"});
    assert_eq!(
        f.call(&dev, "premature-wait", "submit_result", waiting.clone())
            .unwrap_err()
            .status,
        422
    );
    assert_eq!(task(&f.c, "run", &dev.task_key).unwrap().status, "running");
    let qa = f
        .call(
            &dev,
            "ask-qa",
            "send_team_message",
            json!({"recipient_id":"qa","body":"Verify the behavior","request_reply":true}),
        )
        .unwrap();
    let lead=f.call(&dev,"ask-lead","send_team_message",json!({"recipient_id":"lead","body":"Clarify the acceptance evidence","request_reply":true})).unwrap();
    let waited = f
        .call(&dev, "wait", "submit_result", waiting.clone())
        .unwrap();
    assert_eq!(waited["status"], "waiting");
    let stored = task(&f.c, "run", &dev.task_key).unwrap();
    let replies = stored.spec["reply_wait"]["task_keys"].as_array().unwrap();
    assert_eq!(replies.len(), 2);
    assert!(replies.contains(&qa["task_key"]));
    assert!(replies.contains(&lead["task_key"]));
    assert_eq!(
        resume_ready_waiters(&mut f.c, &f.snapshot, "run").unwrap(),
        0
    );
    let qa_actor = claim_task(
        &mut f.c,
        &f.snapshot,
        "run",
        qa["task_key"].as_str().unwrap(),
        "qa",
        1,
        600_000,
    )
    .unwrap();
    f.call(
        &qa_actor,
        "qa-failed",
        "submit_result",
        json!({"outcome":"blocked","summary":"Need an unavailable external dependency"}),
    )
    .unwrap();
    assert_eq!(
        resume_ready_waiters(&mut f.c, &f.snapshot, "run").unwrap(),
        0
    );
    let lead_actor = claim_task(
        &mut f.c,
        &f.snapshot,
        "run",
        lead["task_key"].as_str().unwrap(),
        "lead",
        1,
        600_000,
    )
    .unwrap();
    assert_eq!(f.call(&lead_actor,"invalid-ready","submit_result",json!({"outcome":"ready","summary":"This is only a helper reply","notes":"This helper reply is not the final coordinated delivery."})).unwrap_err().status,403);
    f.call(&lead_actor,"lead-done","submit_result",json!({"outcome":"done","summary":"Acceptance clarified","notes":"Checked and clarified all expected acceptance evidence for the task."})).unwrap();
    let binding = json!({"connection_id":"reply-connection","thread_id":"reply-thread","turn_id":"reply-turn"});
    f.c.execute("UPDATE tasks SET spec=json_set(spec,'$.runtime_sessions',json(?1)) WHERE run_id='run' AND key=?2",params![json!([binding]).to_string(),lead_actor.task_key]).unwrap();
    assert_eq!(
        resume_ready_waiters(&mut f.c, &f.snapshot, "run").unwrap(),
        0,
        "submission must wait for runtime closure and workspace integration"
    );
    f.c.execute("UPDATE tasks SET spec=json_set(spec,'$.runtime_completion',json(?1)) WHERE run_id='run' AND key=?2",params![json!({"session":{"connection_id":"old-connection","thread_id":"reply-thread","turn_id":"reply-turn"}}).to_string(),lead_actor.task_key]).unwrap();
    assert_eq!(
        resume_ready_waiters(&mut f.c, &f.snapshot, "run").unwrap(),
        0,
        "an earlier activation receipt cannot release the waiter"
    );
    f.c.execute("UPDATE tasks SET spec=json_set(spec,'$.runtime_completion',json(?1)) WHERE run_id='run' AND key=?2",params![json!({"session":binding,"workspace_checked":true}).to_string(),lead_actor.task_key]).unwrap();
    let before = (f.count("events"), f.count("outbox"));
    assert_eq!(
        resume_ready_waiters(&mut f.c, &f.snapshot, "run").unwrap(),
        1
    );
    assert_eq!(
        resume_ready_waiters(&mut f.c, &f.snapshot, "run").unwrap(),
        0
    );
    assert_eq!(
        (f.count("events"), f.count("outbox")),
        (before.0 + 1, before.1 + 1)
    );
    let resumed = task(&f.c, "run", &dev.task_key).unwrap();
    assert_eq!(resumed.status, "pending");
    assert_eq!(resumed.spec["reply_wait"]["consumed"], true);
    assert_eq!(
        resumed.spec["consumed_reply_task_keys"]
            .as_array()
            .unwrap()
            .len(),
        2
    );
    let activation = pending_tasks(&f.c, "run")
        .unwrap()
        .into_iter()
        .find(|task| task["task_key"] == dev.task_key)
        .unwrap();
    assert!(activation["instruction"]
        .as_str()
        .unwrap()
        .contains("Acceptance clarified"));
    assert!(activation["instruction"]
        .as_str()
        .unwrap()
        .contains("unavailable external dependency"));
    assert_eq!(
        f.call(&dev, "stale-after-resume", "inspect_team", json!({}))
            .unwrap_err()
            .status,
        409
    );
    let fresh = claim_task(
        &mut f.c,
        &f.snapshot,
        "run",
        &dev.task_key,
        "dev",
        resumed.state.version,
        600_000,
    )
    .unwrap();
    assert_eq!(fresh.attempt_generation, dev.attempt_generation + 1);
    assert_ne!(fresh.claim_token, dev.claim_token);
    assert_eq!(
        f.call(
            &dev,
            "old-token",
            "submit_result",
            json!({"outcome":"failed","summary":"Stale worker cannot overwrite"})
        )
        .unwrap_err()
        .status,
        409
    );
    assert_eq!(
        f.call(&fresh, "wait-on-consumed-replies", "submit_result", waiting)
            .unwrap_err()
            .status,
        422
    );
    assert_eq!(
        resume_ready_waiters(&mut f.c, &f.snapshot, "run").unwrap(),
        0
    );
}

#[test]
fn stopping_a_run_stops_live_task_projections_and_old_authority_stays_fenced() {
    for status in ["cancelled", "needs_attention", "failed"] {
        let mut f = Fixture::new();
        let dev = f.worker("dev", "dev");
        let done = f.worker("qa", "qa");
        f.call(&done,"completed","submit_result",json!({"outcome":"done","summary":"Completed QA","notes":"Actual verification completed before the run was stopped."})).unwrap();
        let reply=f.call(&dev,"ask","send_team_message",json!({"recipient_id":"qa","body":"Check one additional detail","request_reply":true})).unwrap();
        f.call(
            &dev,
            "wait",
            "submit_result",
            json!({"outcome":"waiting","summary":"Awaiting additional detail"}),
        )
        .unwrap();
        set_run_status(&mut f.c, "run", status).unwrap();
        let events = f.count("events");
        set_run_status(&mut f.c, "run", status).unwrap();
        assert_eq!(f.count("events"), events);
        let views = projection::tasks(&f.c, "run").unwrap();
        for key in [
            "codex",
            dev.task_key.as_str(),
            reply["task_key"].as_str().unwrap(),
        ] {
            assert_eq!(
                views.iter().find(|t| t["key"] == key).unwrap()["status"],
                status
            );
        }
        assert_eq!(
            views.iter().find(|t| t["key"] == done.task_key).unwrap()["status"],
            "done"
        );
        let stopped: i64 =
            f.c.query_row(
                "SELECT count(*) FROM events WHERE run_id='run' AND kind='team.work.stopped'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(stopped, 3);
        assert!(pending_tasks(&f.c, "run").unwrap().is_empty());
        assert_eq!(
            resume_ready_waiters(&mut f.c, &f.snapshot, "run")
                .unwrap_err()
                .status,
            409
        );
        if status == "needs_attention" {
            set_run_status(&mut f.c, "run", "running").unwrap();
            assert_eq!(
                f.call(
                    &f.lead.clone(),
                    "stale-after-restart",
                    "inspect_team",
                    json!({})
                )
                .unwrap_err()
                .status,
                409
            );
            let key = reply["task_key"].as_str().unwrap();
            assert_eq!(
                claim_task(&mut f.c, &f.snapshot, "run", key, "qa", 1, 600_000)
                    .unwrap_err()
                    .status,
                409
            );
        }
    }
}
