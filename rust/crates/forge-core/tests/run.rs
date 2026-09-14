use forge_core::{
    Command, DomainError, Run, RunConfig, RunError, RunRuntimeEvent, RunStatus, RunTransition,
};
use serde_json::json;

fn config() -> RunConfig {
    serde_json::from_value(json!({
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
    .unwrap()
}

#[test]
fn frozen_team_capabilities_preserve_legacy_members_and_require_one_coordinator() {
    let mut config = config();
    assert_eq!(config.coordinator_id(), Some("worker"));
    assert!(config.members[0].tools.is_empty());
    assert!(config.members[0].skills.is_empty());
    config.members.push(
        serde_json::from_value(json!({
            "worker_id":"reviewer", "config_revision":"reviewer-v1", "role":"reviewer",
            "instructions":"Review changes", "tools":["read_file"],
            "skills":[{"skill_id":"rust-review","version":2,"digest":"frozen"}],
            "permission_summary":{"filesystem":"read_only"}, "is_coordinator":true,
            "communication_recipients":["worker"]
        }))
        .unwrap(),
    );
    config.validate().unwrap();
    assert_eq!(config.coordinator_id(), Some("reviewer"));
    let restored: RunConfig =
        serde_json::from_value(serde_json::to_value(&config).unwrap()).unwrap();
    assert_eq!(restored, config);
    config.members[0].is_coordinator = true;
    assert_eq!(
        config.validate(),
        Err(RunError::MultipleCoordinators.into())
    );
    config.members[0].is_coordinator = false;
    config.members[1]
        .communication_recipients
        .push("outsider".into());
    assert_eq!(config.validate(), Err(RunError::UnauthorizedWorker.into()));
}

fn begin(run: &mut Run, connection_id: &str) {
    run.transition(
        run.version,
        RunTransition::Begin {
            worker_id: "worker".into(),
            connection_id: connection_id.into(),
        },
    )
    .unwrap();
}

fn bind(run: &mut Run, turn: &str) {
    let connection = run.execution.as_ref().unwrap().connection.clone();
    run.transition(
        run.version,
        RunTransition::BindThread {
            connection: connection.clone(),
            thread_id: "thread".into(),
        },
    )
    .unwrap();
    run.transition(
        run.version,
        RunTransition::BindTurn {
            connection,
            turn_id: turn.into(),
        },
    )
    .unwrap();
}

#[test]
fn uncertain_member_session_requires_reconciliation_and_cannot_control_leader() {
    let mut team = config();
    let mut member = team.members[0].clone();
    member.worker_id = "member".into();
    team.members.push(member);
    let mut run = Run::with_config("run".into(), team).unwrap();
    begin(&mut run, "leader-connection");
    bind(&mut run, "leader-turn");
    let leader = run.execution.clone();
    run.transition(
        run.version,
        RunTransition::BeginMember {
            worker_id: "member".into(),
            connection_id: "member-1".into(),
        },
    )
    .unwrap();
    let connection = run.sessions["member"].execution.connection.clone();
    let before = run.clone();
    assert_eq!(
        run.transition(
            run.version,
            RunTransition::Pause {
                connection: connection.clone(),
                evidence_ref: "member-may-not-stop-leader".into()
            }
        ),
        Err(RunError::StaleExecution.into())
    );
    assert_eq!(run, before);
    run.transition(
        run.version,
        RunTransition::MarkMemberOutcomeUnknown {
            connection: connection.clone(),
        },
    )
    .unwrap();
    assert_eq!(
        run.transition(
            run.version,
            RunTransition::BeginMember {
                worker_id: "member".into(),
                connection_id: "member-2".into()
            }
        ),
        Err(RunError::UnresolvedMemberSessions.into())
    );
    assert_eq!(
        run.require_connection(&connection),
        Err(RunError::StaleExecution.into())
    );
    run.transition(
        run.version,
        RunTransition::ReconcileMember {
            worker_id: "member".into(),
            evidence_ref: "verified-stopped".into(),
        },
    )
    .unwrap();
    run.transition(
        run.version,
        RunTransition::BeginMember {
            worker_id: "member".into(),
            connection_id: "member-2".into(),
        },
    )
    .unwrap();
    assert_eq!(run.sessions["member"].execution.connection.generation, 2);
    assert_eq!(run.execution, leader);
}

#[test]
fn runtime_event_validation_keeps_terminal_status_and_item_scope_coherent() {
    let mut run = Run::with_config("run".into(), config()).unwrap();
    begin(&mut run, "connection");
    bind(&mut run, "turn");
    let before = run.clone();
    let event = RunRuntimeEvent {
        connection: run.execution.as_ref().unwrap().connection.clone(),
        sequence: 1,
        thread_id: Some("thread".into()),
        turn_id: Some("turn".into()),
        item_id: None,
        kind: "turn/completed".into(),
        terminal_status: Some("completed".into()),
    };
    run.require_runtime_event(&event).unwrap();
    for status in ["interrupted", "failed"] {
        let mut interrupted = event.clone();
        interrupted.terminal_status = Some(status.into());
        run.require_runtime_event(&interrupted).unwrap();
    }
    let mut invalid = event.clone();
    invalid.terminal_status = Some("untrusted runtime text".into());
    assert_eq!(
        run.require_runtime_event(&invalid),
        Err(RunError::InvalidRuntimeEvent.into())
    );
    invalid = event.clone();
    invalid.kind = "item/completed".into();
    assert_eq!(
        run.require_runtime_event(&invalid),
        Err(RunError::InvalidRuntimeEvent.into())
    );
    invalid = event.clone();
    invalid.turn_id = None;
    assert_eq!(
        run.require_runtime_event(&invalid),
        Err(RunError::InvalidRuntimeEvent.into())
    );
    invalid.terminal_status = None;
    invalid.item_id = Some("item".into());
    assert_eq!(
        run.require_runtime_event(&invalid),
        Err(RunError::InvalidRuntimeEvent.into())
    );
    invalid = event.clone();
    invalid.sequence = u64::MAX;
    assert_eq!(
        run.require_runtime_event(&invalid),
        Err(DomainError::Overflow)
    );
    invalid = event;
    invalid.kind = " ".into();
    assert_eq!(
        run.require_runtime_event(&invalid),
        Err(DomainError::EmptyIdentifier("runtime_event_kind"))
    );
    assert_eq!(run, before);
}

#[test]
fn runtime_event_command_rejects_raw_notification_content() {
    let mut command = json!({
        "type": "record_runtime_event", "run_id": "run",
        "connection": {"worker_id": "worker", "connection_id": "connection", "generation": 1},
        "sequence": 1, "thread_id": "thread", "turn_id": "turn", "item_id": "item",
        "kind": "item/completed", "terminal_status": null
    });
    assert!(serde_json::from_value::<Command>(command.clone()).is_ok());
    for field in ["payload", "token", "body"] {
        command[field] = json!("private content");
        assert!(serde_json::from_value::<Command>(command.clone()).is_err());
        command.as_object_mut().unwrap().remove(field);
    }
}

#[test]
fn interrupted_run_preserves_frozen_config_and_thread_but_fences_old_connection() {
    let frozen = config();
    let mut run = Run::with_config("run".into(), frozen.clone()).unwrap();
    begin(&mut run, "connection-1");
    bind(&mut run, "turn-1");
    let old = run.execution.clone().unwrap();
    run.transition(
        run.version,
        RunTransition::Pause {
            connection: old.connection.clone(),
            evidence_ref: "interrupt-confirmed".into(),
        },
    )
    .unwrap();
    assert!(run.require_connection(&old.connection).is_err());
    begin(&mut run, "connection-2");
    let current = run.execution.clone().unwrap();
    assert_eq!(current.connection.generation, 2);
    assert_eq!(current.thread_id, old.thread_id);
    assert_eq!(current.turn_id, None);
    assert_eq!(
        run.require_connection(&old.connection),
        Err(RunError::StaleExecution.into())
    );
    let before = run.clone();
    assert_eq!(
        run.transition(
            run.version,
            RunTransition::BindThread {
                connection: current.connection,
                thread_id: "different-thread".into()
            }
        ),
        Err(RunError::ThreadMismatch.into())
    );
    assert_eq!(run, before);
    bind(&mut run, "turn-2");
    let before = run.clone();
    assert_eq!(
        run.transition(
            run.version,
            RunTransition::Submit {
                execution: old,
                result: json!("stale result")
            }
        ),
        Err(RunError::StaleExecution.into())
    );
    assert_eq!(run, before);
    assert_eq!(run.config.as_deref(), Some(&frozen));
}

#[test]
fn lost_start_must_be_reconciled_before_another_execution() {
    let mut run = Run::with_config("run".into(), config()).unwrap();
    begin(&mut run, "first");
    assert_eq!(run.status, RunStatus::Starting);
    run.transition(
        run.version,
        RunTransition::MarkOutcomeUnknown {
            connection: run.execution.as_ref().unwrap().connection.clone(),
        },
    )
    .unwrap();
    let before = run.clone();
    assert_eq!(
        run.transition(
            run.version,
            RunTransition::Begin {
                worker_id: "worker".into(),
                connection_id: "second".into()
            }
        ),
        Err(RunError::InvalidState(RunStatus::OutcomeUnknown).into())
    );
    assert_eq!(
        run.transition(
            run.version,
            RunTransition::Reconcile {
                evidence_ref: "  ".into()
            }
        ),
        Err(DomainError::EmptyIdentifier("evidence_ref"))
    );
    assert_eq!(run, before);
    run.transition(
        run.version,
        RunTransition::Reconcile {
            evidence_ref: "worker-reaped-worktree-checked".into(),
        },
    )
    .unwrap();
    assert_eq!(run.status, RunStatus::Paused);
    assert!(run
        .transition(
            run.version,
            RunTransition::Begin {
                worker_id: "worker".into(),
                connection_id: "first".into()
            }
        )
        .is_err());
    begin(&mut run, "second");
    assert_eq!(run.execution.unwrap().connection.generation, 2);
}

#[test]
fn submission_integration_and_acceptance_are_separate_decisions() {
    let mut run = Run::with_config("run".into(), config()).unwrap();
    begin(&mut run, "connection");
    bind(&mut run, "turn");
    assert!(run
        .transition(
            run.version,
            RunTransition::Accept {
                evidence_ref: "too-early".into()
            }
        )
        .is_err());
    run.transition(
        run.version,
        RunTransition::Submit {
            execution: run.execution.clone().unwrap(),
            result: json!({"candidate": "result"}),
        },
    )
    .unwrap();
    assert_eq!(run.status, RunStatus::Submitted);
    assert!(run
        .transition(
            run.version,
            RunTransition::Accept {
                evidence_ref: "still-too-early".into()
            }
        )
        .is_err());
    run.transition(
        run.version,
        RunTransition::Integrate {
            evidence_ref: "integration-reviewed".into(),
        },
    )
    .unwrap();
    run.transition(
        run.version,
        RunTransition::Accept {
            evidence_ref: "user-acceptance".into(),
        },
    )
    .unwrap();
    assert_eq!(run.status, RunStatus::Accepted);
    assert_eq!(run.result, Some(json!({"candidate": "result"})));
    let before = run.clone();
    assert!(run
        .transition(
            run.version,
            RunTransition::Begin {
                worker_id: "worker".into(),
                connection_id: "new".into()
            }
        )
        .is_err());
    assert_eq!(run, before);
}

#[test]
fn legacy_unbound_run_decodes_but_cannot_enter_the_new_execution_path() {
    let mut run: Run = serde_json::from_value(json!({"id":"legacy", "status":"pending"})).unwrap();
    assert_eq!(run, Run::new("legacy".into()).unwrap());
    assert_eq!(
        run.transition(
            0,
            RunTransition::Begin {
                worker_id: "worker".into(),
                connection_id: "connection".into()
            }
        ),
        Err(RunError::Unbound.into())
    );
}

#[test]
fn configuration_rejects_missing_evidence_duplicate_members_and_relative_paths() {
    let mut missing = config();
    missing.route_binding.capability_evidence_ref = " ".into();
    assert!(Run::with_config("run".into(), missing).is_err());
    let mut duplicate = config();
    duplicate.members.push(duplicate.members[0].clone());
    assert_eq!(
        Run::with_config("run".into(), duplicate),
        Err(RunError::DuplicateMember.into())
    );
    let mut no_members = config();
    no_members.members.clear();
    assert_eq!(
        Run::with_config("run".into(), no_members),
        Err(RunError::EmptyMembers.into())
    );
    let mut relative = config();
    relative.runtime_home = "runtime/run".into();
    assert_eq!(
        Run::with_config("run".into(), relative),
        Err(RunError::RelativePath.into())
    );
    let mut unknown = serde_json::to_value(config()).unwrap();
    unknown["permission_profile"] = json!("unknown");
    assert!(serde_json::from_value::<RunConfig>(unknown).is_err());
    let mut secret = serde_json::to_value(config()).unwrap();
    secret["route_binding"]["api_key"] = json!("must-not-be-accepted");
    assert!(serde_json::from_value::<RunConfig>(secret).is_err());
}

#[test]
fn unauthorized_worker_stale_version_and_overflow_leave_run_unchanged() {
    let mut run = Run::with_config("run".into(), config()).unwrap();
    let before = run.clone();
    assert_eq!(
        run.transition(
            0,
            RunTransition::Begin {
                worker_id: "outside-team".into(),
                connection_id: "connection".into()
            }
        ),
        Err(RunError::UnauthorizedWorker.into())
    );
    assert_eq!(
        run.transition(
            1,
            RunTransition::Begin {
                worker_id: "worker".into(),
                connection_id: "connection".into()
            }
        ),
        Err(DomainError::VersionConflict {
            expected: 1,
            actual: 0
        })
    );
    assert_eq!(run, before);
    run.version = i64::MAX;
    let before = run.clone();
    assert_eq!(
        run.transition(
            i64::MAX,
            RunTransition::Begin {
                worker_id: "worker".into(),
                connection_id: "connection".into()
            }
        ),
        Err(DomainError::Overflow)
    );
    assert_eq!(run, before);
}
