use forge_core::{
    Approval, ApprovalDecision, ApprovalError, ApprovalKind, ApprovalRequest, ApprovalRequestId,
    ApprovalStatus, DomainError, Run, RunConfig, RunStatus, RunTransition,
};
use serde_json::{json, Value};

fn running() -> Run {
    let config: RunConfig = serde_json::from_value(json!({
        "project_id": "project", "owner_id": "owner",
        "members": [{"worker_id": "worker", "config_revision": "member-v1"}],
        "permission_profile": "ask", "working_directory": "/forge/worktree",
        "runtime_home": "/forge/runtime/run", "runtime_version": "codex-pinned",
        "route_binding": {
            "provider_id": "provider", "provider_revision_id": "provider-v1",
            "model_id": "model", "upstream_protocol": "anthropic_messages",
            "gateway_route_id": "route-v1", "responses_base_url": "http://127.0.0.1:8787/v1",
            "config_digest": "sha256:config", "credential_ref": "vault:provider-v1",
            "capability_evidence_ref": "evidence:tools", "gateway_version": "gateway-pinned"
        }
    }))
    .unwrap();
    let mut run = Run::with_config("run".into(), config).unwrap();
    run.transition(
        0,
        RunTransition::Begin {
            worker_id: "worker".into(),
            connection_id: "connection".into(),
        },
    )
    .unwrap();
    let connection = run.execution.as_ref().unwrap().connection.clone();
    run.transition(
        1,
        RunTransition::BindThread {
            connection: connection.clone(),
            thread_id: "thread".into(),
        },
    )
    .unwrap();
    run.transition(
        2,
        RunTransition::BindTurn {
            connection,
            turn_id: "turn".into(),
        },
    )
    .unwrap();
    run
}

fn request(run: &Run, kind: ApprovalKind, payload: Value) -> ApprovalRequest {
    ApprovalRequest {
        run_id: run.id.clone(),
        execution: run.execution.clone().unwrap(),
        request_id: ApprovalRequestId::Integer(17),
        item_id: "item".into(),
        kind,
        payload,
    }
}

fn approval(run: &Run) -> Approval {
    Approval::new("approval".into(), request(run, ApprovalKind::CommandExecution,
        json!({"command": "cargo test", "threadId": "thread", "turnId": "turn", "itemId": "item"})),
        run, 100).unwrap()
}

fn decision(approved: bool) -> ApprovalDecision {
    ApprovalDecision {
        approved,
        actor_id: "reviewer".into(),
        comment: "reviewed".into(),
    }
}

fn queue(approval: &mut Approval, run: &Run, approved: bool) {
    approval
        .decide(
            approval.version,
            &approval.digest.clone(),
            decision(approved),
            run,
            101,
        )
        .unwrap();
}

#[test]
fn decision_delivery_is_one_shot_and_does_not_complete_the_run() {
    for approved in [true, false] {
        let run = running();
        let mut record = approval(&run);
        assert!(record.response().is_err());
        queue(&mut record, &run, approved);
        assert_eq!(record.status, ApprovalStatus::Queued);
        assert_eq!(record.decision, Some(decision(approved)));
        assert_eq!(
            record.response().unwrap(),
            json!({"decision": if approved { "accept" } else { "decline" }})
        );
        let execution = run.execution.as_ref().unwrap();
        record.begin_dispatch(1, execution, &run, 102).unwrap();
        assert_eq!(record.status, ApprovalStatus::Dispatching);
        record.finish_dispatch(2, true, 103).unwrap();
        assert_eq!(record.status, ApprovalStatus::Delivered);
        assert_eq!(record.version, 3);
        assert_eq!(record.created_at_ms, 100);
        assert_eq!(record.updated_at_ms, 103);
        assert_eq!(run.status, RunStatus::Running);
        assert_eq!(run.result, None);
        assert!(record.response().is_err());
        let before = record.clone();
        assert!(record.begin_dispatch(3, execution, &run, 104).is_err());
        assert!(record.finish_dispatch(3, true, 104).is_err());
        assert_eq!(record, before);
    }
}

#[test]
fn request_ids_preserve_integer_string_distinction_and_reject_other_json_types() {
    let run = running();
    let integer = request(&run, ApprovalKind::CommandExecution, json!({}));
    let mut string = integer.clone();
    string.request_id = ApprovalRequestId::String("17".into());
    assert_ne!(integer.digest().unwrap(), string.digest().unwrap());
    assert_ne!(integer.request_id, string.request_id);
    assert_eq!(
        serde_json::to_value(&integer.request_id).unwrap(),
        json!(17)
    );
    assert_eq!(
        serde_json::to_value(&string.request_id).unwrap(),
        json!("17")
    );
    for invalid in [
        json!(true),
        json!(17.1),
        json!(null),
        json!({"id": 17}),
        json!(u64::MAX),
    ] {
        assert!(serde_json::from_value::<ApprovalRequestId>(invalid).is_err());
    }
    string.request_id = ApprovalRequestId::String("  ".into());
    assert!(Approval::new("approval".into(), string, &run, 100).is_err());
}

#[test]
fn unsupported_approval_kinds_and_decision_fields_are_rejected() {
    assert!(serde_json::from_value::<ApprovalKind>(json!("other/requestApproval")).is_err());
    assert!(serde_json::from_value::<ApprovalDecision>(json!({
        "approved": "true", "actor_id": "reviewer", "comment": ""
    }))
    .is_err());
    assert!(serde_json::from_value::<ApprovalDecision>(json!({
        "approved": true, "actor_id": "reviewer", "comment": "", "scope": "session"
    }))
    .is_err());
}

#[test]
fn digest_covers_request_scope_kind_and_payload_with_canonical_object_order() {
    let run = running();
    let original = request(
        &run,
        ApprovalKind::CommandExecution,
        json!({"a": 1, "z": {"a": 2, "z": 3}}),
    );
    let mut reordered = original.clone();
    reordered.payload = serde_json::from_str(r#"{"z":{"z":3,"a":2},"a":1}"#).unwrap();
    assert_eq!(original.digest().unwrap(), reordered.digest().unwrap());
    let mut variants = vec![original.clone(); 7];
    variants[0].run_id = "other-run".into();
    variants[1].execution.connection.generation += 1;
    variants[2].execution.connection.connection_id = "other-connection".into();
    variants[3].execution.thread_id = Some("other-thread".into());
    variants[4].execution.turn_id = Some("other-turn".into());
    variants[5].item_id = "other-item".into();
    variants[6].payload["a"] = json!(2);
    for variant in variants {
        assert_ne!(original.digest().unwrap(), variant.digest().unwrap());
    }
    let mut different_kind = original.clone();
    different_kind.kind = ApprovalKind::FileChange;
    assert_ne!(original.digest().unwrap(), different_kind.digest().unwrap());
}

#[test]
fn requests_require_current_run_generation_worker_thread_and_turn() {
    let run = running();
    let original = request(&run, ApprovalKind::CommandExecution, json!({}));
    let mut variants = vec![original.clone(); 7];
    variants[0].run_id = "other".into();
    variants[1].execution.connection.generation += 1;
    variants[2].execution.connection.worker_id = "other".into();
    variants[3].execution.connection.connection_id = "other".into();
    variants[4].execution.thread_id = Some("other".into());
    variants[5].execution.turn_id = Some("other".into());
    variants[6].execution.turn_id = None;
    for variant in variants {
        assert_eq!(
            Approval::new("approval".into(), variant, &run, 100),
            Err(ApprovalError::StaleExecution.into())
        );
    }
    let mut paused = run.clone();
    paused.status = RunStatus::Paused;
    assert!(Approval::new("approval".into(), original.clone(), &paused, 100).is_err());
    let mut unbound = run.clone();
    unbound.config = None;
    assert!(Approval::new("approval".into(), original, &unbound, 100).is_err());
}

#[test]
fn payload_identity_cannot_disagree_with_recorded_scope() {
    let run = running();
    for field in [
        "threadId",
        "thread_id",
        "turnId",
        "turn_id",
        "itemId",
        "item_id",
    ] {
        for value in [json!("other"), Value::Null] {
            let mut payload = json!({});
            payload[field] = value;
            let record = request(&run, ApprovalKind::CommandExecution, payload);
            assert_eq!(
                Approval::new("approval".into(), record, &run, 100),
                Err(ApprovalError::PayloadScopeMismatch.into())
            );
        }
    }
    let record = request(
        &run,
        ApprovalKind::FileChange,
        json!({"item": {"id": "other"}}),
    );
    assert_eq!(
        Approval::new("approval".into(), record, &run, 100),
        Err(ApprovalError::PayloadScopeMismatch.into())
    );
    assert!(Approval::new(
        "approval".into(),
        request(
            &run,
            ApprovalKind::CommandExecution,
            json!(["not an object"])
        ),
        &run,
        100
    )
    .is_err());
}

#[test]
fn failed_decisions_leave_state_unchanged() {
    let run = running();
    let mut record = approval(&run);
    let before = record.clone();
    assert_eq!(
        record.decide(1, &before.digest, decision(true), &run, 101),
        Err(DomainError::VersionConflict {
            expected: 1,
            actual: 0
        })
    );
    assert_eq!(
        record.decide(0, "stale", decision(true), &run, 101),
        Err(ApprovalError::DigestMismatch.into())
    );
    assert_eq!(
        record.decide(0, &before.digest, decision(true), &run, 99),
        Err(ApprovalError::InvalidTimestamp.into())
    );
    let mut anonymous = decision(true);
    anonymous.actor_id = " ".into();
    assert!(record
        .decide(0, &before.digest, anonymous, &run, 101)
        .is_err());
    let mut too_long = decision(true);
    too_long.comment = "x".repeat(4001);
    assert_eq!(
        record.decide(0, &before.digest, too_long, &run, 101),
        Err(ApprovalError::CommentTooLong.into())
    );
    assert_eq!(record, before);
    record.version = i64::MAX;
    let overflowing = record.clone();
    assert_eq!(
        record.decide(i64::MAX, &before.digest, decision(true), &run, 101),
        Err(DomainError::Overflow)
    );
    assert_eq!(record, overflowing);
}

#[test]
fn modified_payload_is_detected_before_deciding_or_dispatching() {
    let run = running();
    let mut record = approval(&run);
    record.request.payload["command"] = json!("different operation");
    let digest = record.digest.clone();
    let before = record.clone();
    assert_eq!(
        record.decide(0, &digest, decision(true), &run, 101),
        Err(ApprovalError::DigestMismatch.into())
    );
    assert_eq!(record, before);
    let mut queued = approval(&run);
    queue(&mut queued, &run, true);
    queued.request.payload["command"] = json!("different operation");
    assert_eq!(queued.response(), Err(ApprovalError::DigestMismatch.into()));
}

#[test]
fn queued_decisions_cannot_be_replaced_or_sent_to_a_different_execution() {
    let run = running();
    let mut record = approval(&run);
    queue(&mut record, &run, true);
    let before = record.clone();
    assert!(record
        .decide(1, &before.digest, decision(false), &run, 102)
        .is_err());
    let mut wrong_execution = run.execution.clone().unwrap();
    wrong_execution.connection.generation += 1;
    assert_eq!(
        record.begin_dispatch(1, &wrong_execution, &run, 102),
        Err(ApprovalError::StaleExecution.into())
    );
    let mut stopped = run.clone();
    stopped.status = RunStatus::Paused;
    assert_eq!(
        record.begin_dispatch(1, run.execution.as_ref().unwrap(), &stopped, 102),
        Err(ApprovalError::StaleExecution.into())
    );
    assert_eq!(record, before);
}

#[test]
fn command_approvals_never_silently_grant_session_access() {
    let run = running();
    for available in [
        json!(["acceptForSession", "decline"]),
        json!("accept"),
        json!([]),
        Value::Null,
    ] {
        let mut record = Approval::new(
            "approval".into(),
            request(
                &run,
                ApprovalKind::CommandExecution,
                json!({"availableDecisions": available}),
            ),
            &run,
            100,
        )
        .unwrap();
        let before = record.clone();
        assert_eq!(
            record.decide(0, &before.digest, decision(true), &run, 101),
            Err(ApprovalError::UnsupportedGrant.into())
        );
        assert_eq!(record, before);
        queue(&mut record, &run, false);
        assert_eq!(record.response().unwrap(), json!({"decision": "decline"}));
    }
}

#[test]
fn file_grants_require_the_exact_reviewable_item_and_changes() {
    let run = running();
    for payload in [
        json!({}),
        json!({"item": {"id": "item", "type": "fileChange", "changes": []}}),
        json!({"item": {"id": "item", "type": "fileChange", "changes": [{"path": "file"}]}}),
    ] {
        let mut record = Approval::new(
            "approval".into(),
            request(&run, ApprovalKind::FileChange, payload),
            &run,
            100,
        )
        .unwrap();
        let before = record.clone();
        assert_eq!(
            record.decide(0, &before.digest, decision(true), &run, 101),
            Err(ApprovalError::MissingFileChanges.into())
        );
        assert_eq!(record, before);
        queue(&mut record, &run, false);
    }
    // Empty-file additions have a complete, empty diff.
    let mut record = Approval::new(
        "approval".into(),
        request(
            &run,
            ApprovalKind::FileChange,
            json!({"item": {"id": "item", "type": "fileChange", "changes": [
            {"path": "/forge/new-file", "kind": {"type": "add"}, "diff": ""}
        ]}, "availableDecisions": ["accept", "decline"]}),
        ),
        &run,
        100,
    )
    .unwrap();
    queue(&mut record, &run, true);
    assert_eq!(record.response().unwrap(), json!({"decision": "accept"}));
}

#[test]
fn permission_grants_preserve_requested_scope_and_never_exceed_one_turn() {
    let run = running();
    let requested =
        json!({"network": {"enabled": true}, "fileSystem": {"read": ["/forge/worktree"]}});
    let mut record = Approval::new(
        "approval".into(),
        request(
            &run,
            ApprovalKind::Permissions,
            json!({"permissions": requested}),
        ),
        &run,
        100,
    )
    .unwrap();
    queue(&mut record, &run, true);
    assert_eq!(
        record.response().unwrap(),
        json!({"permissions": requested, "scope": "turn"})
    );
    for requested in [
        json!({"unknown": {}}),
        json!({"network": true}),
        json!("all"),
        Value::Null,
    ] {
        let mut record = Approval::new(
            "approval".into(),
            request(
                &run,
                ApprovalKind::Permissions,
                json!({"permissions": requested}),
            ),
            &run,
            100,
        )
        .unwrap();
        let before = record.clone();
        assert_eq!(
            record.decide(0, &before.digest, decision(true), &run, 101),
            Err(ApprovalError::InvalidPermissions.into())
        );
        assert_eq!(record, before);
        queue(&mut record, &run, false);
        assert_eq!(
            record.response().unwrap(),
            json!({"permissions": {}, "scope": "turn"})
        );
    }
}

#[test]
fn invalidation_distinguishes_unsent_and_uncertain_decisions() {
    let run = running();
    for status in [
        ApprovalStatus::Pending,
        ApprovalStatus::Queued,
        ApprovalStatus::Dispatching,
    ] {
        let mut record = approval(&run);
        if status != ApprovalStatus::Pending {
            queue(&mut record, &run, true);
        }
        if status == ApprovalStatus::Dispatching {
            record
                .begin_dispatch(1, run.execution.as_ref().unwrap(), &run, 102)
                .unwrap();
        }
        let old_version = record.version;
        assert!(record.status.is_unresolved());
        assert!(record.invalidate(103).unwrap());
        assert_eq!(record.version, old_version + 1);
        assert_eq!(
            record.status,
            if status == ApprovalStatus::Dispatching {
                ApprovalStatus::DeliveryUnknown
            } else {
                ApprovalStatus::Expired
            }
        );
        assert!(!record.status.is_unresolved());
        let terminal = record.clone();
        assert!(!record.invalidate(104).unwrap());
        assert_eq!(record, terminal);
        assert!(record.response().is_err());
        assert!(record
            .begin_dispatch(record.version, run.execution.as_ref().unwrap(), &run, 104)
            .is_err());
        assert!(record.finish_dispatch(record.version, true, 104).is_err());
        assert_eq!(record, terminal);
    }
}

#[test]
fn uncertain_write_is_terminal_even_when_original_callback_still_appears_live() {
    let run = running();
    let mut record = approval(&run);
    queue(&mut record, &run, true);
    record
        .begin_dispatch(1, run.execution.as_ref().unwrap(), &run, 102)
        .unwrap();
    record.finish_dispatch(2, false, 103).unwrap();
    assert_eq!(record.status, ApprovalStatus::DeliveryUnknown);
    let before = record.clone();
    assert!(record
        .begin_dispatch(3, run.execution.as_ref().unwrap(), &run, 104)
        .is_err());
    assert!(record
        .decide(3, &before.digest, decision(true), &run, 104)
        .is_err());
    assert_eq!(record, before);
}
