use forge_core::{
    Approval, ApprovalError, ApprovalKind, ApprovalRequest, ApprovalRequestId, ApprovalStatus,
    Command, CommandResult, DomainError, PermissionProfile, ProviderModelSnapshot,
    ProviderSnapshot, RouteBinding, RunConfig, RunExecution, RunMember, RunTransition,
};
use forge_store::{Store, StoreError};
use rusqlite::Connection;
use serde_json::json;
use std::{
    path::PathBuf,
    sync::{Arc, Barrier},
    thread,
};
use uuid::Uuid;

struct Database(PathBuf);

impl Database {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!("forge-provisioning-{}", Uuid::new_v4()));
        std::fs::create_dir(&path).unwrap();
        Self(path)
    }

    fn path(&self) -> PathBuf {
        self.0.join("store.db")
    }

    fn config(&self) -> RunConfig {
        RunConfig {
            project_id: "project".into(),
            owner_id: "user".into(),
            members: vec![RunMember {
                worker_id: "worker".into(),
                config_revision: "member-v1".into(),
                role: String::new(),
                instructions: String::new(),
                tools: vec![],
                skills: vec![],
                permission_summary: None,
                is_coordinator: true,
                communication_recipients: vec![],
            }],
            permission_profile: PermissionProfile::Auto,
            working_directory: self.0.join("workspace"),
            runtime_home: self.0.join("runtime"),
            runtime_version: "runtime-v1".into(),
            route_binding: RouteBinding {
                provider_id: "provider".into(),
                provider_revision_id: "revision-v1".into(),
                model_id: "model".into(),
                upstream_protocol: "anthropic_messages".into(),
                gateway_route_id: "route-private".into(),
                responses_base_url: "http://127.0.0.1:10100/v1".into(),
                config_digest: "digest-private".into(),
                credential_ref: "credential-private".into(),
                capability_evidence_ref: "probe-v1".into(),
                gateway_version: "gateway-v1".into(),
            },
        }
    }

    fn provider(&self) -> ProviderSnapshot {
        ProviderSnapshot {
            provider_id: "provider".into(),
            provider_revision_id: "revision-v1".into(),
            upstream_base_url: "https://upstream.example/v1".into(),
            credential_ref: "credential-private".into(),
            config_digest: "digest-private".into(),
            models: vec![ProviderModelSnapshot {
                model_id: "model".into(),
                upstream_protocol: "anthropic_messages".into(),
                capability_evidence_ref: "probe-v1".into(),
            }],
        }
    }

    fn create(&self, run_id: &str) -> Command {
        Command::CreateProvisionedRun {
            run_id: run_id.into(),
            config: Box::new(self.config()),
            provider_snapshot: Box::new(self.provider()),
        }
    }

    fn running(&self, store: &mut Store) -> RunExecution {
        self.running_with_peer(store, false)
    }

    fn running_with_peer(&self, store: &mut Store, peer: bool) -> RunExecution {
        let mut create = self.create("run");
        if peer {
            if let Command::CreateProvisionedRun { config, .. } = &mut create {
                let mut member = config.members[0].clone();
                member.worker_id = "peer".into();
                member.is_coordinator = false;
                config.members.push(member);
            }
        }
        store.execute("create", create).unwrap();
        store
            .execute(
                "begin",
                transition(
                    0,
                    RunTransition::Begin {
                        worker_id: "worker".into(),
                        connection_id: "connection".into(),
                    },
                ),
            )
            .unwrap();
        let connection = store
            .get_run("run")
            .unwrap()
            .unwrap()
            .execution
            .unwrap()
            .connection;
        store
            .execute(
                "thread",
                transition(
                    1,
                    RunTransition::BindThread {
                        connection: connection.clone(),
                        thread_id: "thread".into(),
                    },
                ),
            )
            .unwrap();
        store
            .execute(
                "turn",
                transition(
                    2,
                    RunTransition::BindTurn {
                        connection,
                        turn_id: "turn".into(),
                    },
                ),
            )
            .unwrap();
        store.get_run("run").unwrap().unwrap().execution.unwrap()
    }
}

impl Drop for Database {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn transition(expected_version: i64, transition: RunTransition) -> Command {
    Command::TransitionRun {
        run_id: "run".into(),
        expected_version,
        transition,
    }
}

fn record(id: &str, execution: &RunExecution, request_id: ApprovalRequestId) -> Command {
    Command::RecordApproval {
        approval_id: id.into(),
        request: Box::new(ApprovalRequest {
            run_id: "run".into(),
            execution: execution.clone(),
            request_id,
            item_id: "item".into(),
            kind: ApprovalKind::CommandExecution,
            payload: json!({"command":"private command", "availableDecisions":["accept","decline"]}),
        }),
    }
}

fn decide(approval: &Approval, approved: bool) -> Command {
    Command::DecideApproval {
        approval_id: approval.id.clone(),
        expected_version: approval.version,
        digest: approval.digest.clone(),
        approved,
        actor_id: "user".into(),
        comment: "private comment".into(),
    }
}

fn approval(store: &Store, id: &str) -> Approval {
    store.get_approval(id).unwrap().unwrap()
}

fn begin_dispatch(id: &str, execution: &RunExecution) -> Command {
    Command::BeginApprovalDispatch {
        approval_id: id.into(),
        expected_version: 1,
        execution: execution.clone(),
    }
}

#[test]
fn provider_revisions_routes_and_policies_are_immutable_and_survive_reopen() {
    let db = Database::new();
    let mut store = Store::open(db.path()).unwrap();
    let command = db.create("run");
    let receipt = store.execute("create", command.clone()).unwrap();
    store.execute("same-revision", db.create("other")).unwrap();
    let mut different = db.provider();
    different.upstream_base_url = "https://changed.example".into();
    assert!(matches!(
        store.execute(
            "replace",
            Command::CreateProvisionedRun {
                run_id: "third".into(),
                config: Box::new(db.config()),
                provider_snapshot: Box::new(different)
            }
        ),
        Err(StoreError::AlreadyExists {
            kind: "provider revision",
            ..
        })
    ));
    assert!(store.get_run("third").unwrap().is_none());

    let mut next = db.provider();
    next.provider_revision_id = "revision-v2".into();
    next.credential_ref = "credential-v2".into();
    let mut config = db.config();
    config.route_binding.provider_revision_id = next.provider_revision_id.clone();
    config.route_binding.credential_ref = next.credential_ref.clone();
    config.permission_profile = PermissionProfile::Full;
    store
        .execute(
            "new-revision",
            Command::CreateProvisionedRun {
                run_id: "new".into(),
                config: Box::new(config.clone()),
                provider_snapshot: Box::new(next.clone()),
            },
        )
        .unwrap();
    drop(store);
    let mut store = Store::open(db.path()).unwrap();
    assert_eq!(store.execute("create", command).unwrap(), receipt);
    assert_eq!(
        store.get_run_provider_snapshot("run").unwrap(),
        Some(db.provider())
    );
    assert_eq!(
        store
            .get_provider_snapshot("provider", "revision-v2")
            .unwrap(),
        Some(next)
    );
    assert_eq!(
        store.get_route_binding("run").unwrap(),
        Some(db.config().route_binding)
    );
    assert_eq!(
        store.get_execution_policy("run").unwrap(),
        Some(PermissionProfile::Auto.execution_policy())
    );
    assert_eq!(
        store.get_execution_policy("new").unwrap(),
        Some(PermissionProfile::Full.execution_policy())
    );
    assert_eq!(
        store.get_route_binding("new").unwrap(),
        Some(config.route_binding)
    );
    assert!(store
        .get_run_provider_snapshot("missing")
        .unwrap()
        .is_none());
    let events = serde_json::to_string(&store.events_after(0, 100).unwrap()).unwrap();
    for secret in [
        "credential-private",
        "digest-private",
        "route-private",
        "upstream.example",
    ] {
        assert!(!events.contains(secret));
    }
}

#[test]
fn mismatched_provisioning_and_outbox_failure_leave_no_partial_state() {
    let db = Database::new();
    let mut store = Store::open(db.path()).unwrap();
    let mut config = db.config();
    config.route_binding.credential_ref = "wrong".into();
    assert!(store
        .execute(
            "bad",
            Command::CreateProvisionedRun {
                run_id: "run".into(),
                config: Box::new(config),
                provider_snapshot: Box::new(db.provider())
            }
        )
        .is_err());
    let injector = Connection::open(db.path()).unwrap();
    injector.execute_batch("CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT, 'injected'); END;").unwrap();
    assert!(store.execute("create", db.create("run")).is_err());
    assert!(store.get_run("run").unwrap().is_none());
    assert!(store.get_route_binding("run").unwrap().is_none());
    assert!(store.get_execution_policy("run").unwrap().is_none());
    assert!(store
        .get_provider_snapshot("provider", "revision-v1")
        .unwrap()
        .is_none());
    assert!(store.get_receipt("create").unwrap().is_none());
    assert!(store.events_after(0, 100).unwrap().is_empty());
    injector.execute_batch("DROP TRIGGER fail_outbox").unwrap();
    store.execute("create", db.create("run")).unwrap();
}

#[test]
fn approval_receipts_and_delivery_survive_restart_without_repeating_dispatch() {
    let db = Database::new();
    let mut store = Store::open(db.path()).unwrap();
    let execution = db.running(&mut store);
    let request = record("approval", &execution, ApprovalRequestId::Integer(7));
    let created = store.execute("approval", request.clone()).unwrap();
    let decision = decide(&approval(&store, "approval"), true);
    let queued = store.execute("decision", decision.clone()).unwrap();
    let dispatch = begin_dispatch("approval", &execution);
    let dispatched = store.execute("dispatch", dispatch.clone()).unwrap();
    assert_eq!(
        approval(&store, "approval").response().unwrap(),
        json!({"decision":"accept"})
    );
    drop(store);
    let mut store = Store::open(db.path()).unwrap();
    assert_eq!(
        store.execute("dispatch", dispatch.clone()).unwrap(),
        dispatched
    );
    assert!(store.execute("duplicate-dispatch", dispatch).is_err());
    store
        .execute(
            "delivered",
            Command::FinishApprovalDispatch {
                approval_id: "approval".into(),
                expected_version: 2,
                execution,
                delivered: true,
            },
        )
        .unwrap();
    assert_eq!(
        approval(&store, "approval").status,
        ApprovalStatus::Delivered
    );
    assert!(approval(&store, "approval").response().is_err());
    assert_eq!(store.execute("approval", request).unwrap(), created);
    assert_eq!(store.execute("decision", decision).unwrap(), queued);
    assert_eq!(
        approval(&store, "approval").status,
        ApprovalStatus::Delivered
    );
    let events = serde_json::to_string(&store.events_after(0, 100).unwrap()).unwrap();
    assert!(!events.contains("private command"));
    assert!(!events.contains("private comment"));
    assert!(!events.contains("request_id"));
}

#[test]
fn typed_callback_uniqueness_digest_and_execution_fences_are_enforced() {
    let db = Database::new();
    let mut store = Store::open(db.path()).unwrap();
    let execution = db.running(&mut store);
    store
        .execute("a", record("a", &execution, ApprovalRequestId::Integer(1)))
        .unwrap();
    store
        .execute(
            "b",
            record("b", &execution, ApprovalRequestId::String("1".into())),
        )
        .unwrap();
    let mut reused = record("c", &execution, ApprovalRequestId::Integer(1));
    if let Command::RecordApproval { request, .. } = &mut reused {
        request.payload["command"] = json!("other command");
    }
    assert!(matches!(
        store.execute("reuse", reused),
        Err(StoreError::AlreadyExists {
            kind: "approval callback",
            ..
        })
    ));
    assert!(store.get_approval("c").unwrap().is_none());
    let mut stale = execution.clone();
    stale.connection.generation += 1;
    assert!(store
        .execute(
            "stale",
            record("stale", &stale, ApprovalRequestId::Integer(5))
        )
        .is_err());
    let mut wrong_digest = decide(&approval(&store, "a"), true);
    if let Command::DecideApproval { digest, .. } = &mut wrong_digest {
        *digest = "wrong".into();
    }
    assert!(matches!(
        store.execute("wrong-digest", wrong_digest),
        Err(StoreError::Domain(DomainError::Approval(
            ApprovalError::DigestMismatch
        )))
    ));
    store
        .execute("decision", decide(&approval(&store, "a"), false))
        .unwrap();
    assert!(store
        .execute("wrong-dispatch", begin_dispatch("a", &stale))
        .is_err());
    assert_eq!(approval(&store, "a").status, ApprovalStatus::Queued);
    assert_eq!(store.list_approvals("run", 1).unwrap().len(), 1);
}

#[test]
fn stop_atomically_expires_callbacks_and_fences_old_connections_after_resume() {
    let db = Database::new();
    let mut store = Store::open(db.path()).unwrap();
    let execution = db.running(&mut store);
    for (i, id) in ["pending", "queued", "dispatching"].iter().enumerate() {
        store
            .execute(
                &format!("record-{id}"),
                record(id, &execution, ApprovalRequestId::Integer(i as i64)),
            )
            .unwrap();
    }
    for id in ["queued", "dispatching"] {
        store
            .execute(&format!("decide-{id}"), decide(&approval(&store, id), true))
            .unwrap();
    }
    store
        .execute("dispatch", begin_dispatch("dispatching", &execution))
        .unwrap();
    assert!(matches!(
        store.execute(
            "submit",
            transition(
                3,
                RunTransition::Submit {
                    execution: execution.clone(),
                    result: json!({})
                }
            )
        ),
        Err(StoreError::Domain(DomainError::Approval(
            ApprovalError::UnresolvedApprovals
        )))
    ));
    let pause = transition(
        3,
        RunTransition::Pause {
            connection: execution.connection.clone(),
            evidence_ref: "stopped".into(),
        },
    );
    let injector = Connection::open(db.path()).unwrap();
    injector.execute_batch("CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT, 'injected'); END;").unwrap();
    assert!(store.execute("pause", pause.clone()).is_err());
    assert_eq!(approval(&store, "pending").status, ApprovalStatus::Pending);
    assert_eq!(
        approval(&store, "dispatching").status,
        ApprovalStatus::Dispatching
    );
    injector.execute_batch("DROP TRIGGER fail_outbox").unwrap();
    store.execute("pause", pause).unwrap();
    assert_eq!(approval(&store, "pending").status, ApprovalStatus::Expired);
    assert_eq!(approval(&store, "queued").status, ApprovalStatus::Expired);
    assert_eq!(
        approval(&store, "dispatching").status,
        ApprovalStatus::DeliveryUnknown
    );
    store
        .execute(
            "resume",
            transition(
                4,
                RunTransition::Begin {
                    worker_id: "worker".into(),
                    connection_id: "connection-2".into(),
                },
            ),
        )
        .unwrap();
    let connection = store
        .get_run("run")
        .unwrap()
        .unwrap()
        .execution
        .unwrap()
        .connection;
    store
        .execute(
            "resumed-turn",
            transition(
                5,
                RunTransition::BindTurn {
                    connection,
                    turn_id: "turn-2".into(),
                },
            ),
        )
        .unwrap();
    let current = store.get_run("run").unwrap().unwrap().execution.unwrap();
    store
        .execute(
            "new",
            record("new", &current, ApprovalRequestId::Integer(0)),
        )
        .unwrap();
    store
        .execute(
            "old-expiration",
            Command::ExpireApprovals {
                run_id: "run".into(),
                connection: execution.connection,
            },
        )
        .unwrap();
    assert_eq!(approval(&store, "new").status, ApprovalStatus::Pending);
    assert!(store
        .execute("old-decision", decide(&approval(&store, "pending"), true))
        .is_err());
    assert!(store
        .execute("old-dispatch", begin_dispatch("queued", &current))
        .is_err());
}

#[test]
fn concurrent_decisions_have_one_winner_and_one_receipt() {
    let db = Database::new();
    let mut first = Store::open(db.path()).unwrap();
    let execution = db.running(&mut first);
    first
        .execute(
            "record",
            record("approval", &execution, ApprovalRequestId::Integer(1)),
        )
        .unwrap();
    let pending = approval(&first, "approval");
    let second = Store::open(db.path()).unwrap();
    let barrier = Arc::new(Barrier::new(2));
    let handles: Vec<_> = [first, second]
        .into_iter()
        .enumerate()
        .map(|(index, mut store)| {
            let barrier = barrier.clone();
            let pending = pending.clone();
            thread::spawn(move || {
                barrier.wait();
                store.execute(&format!("decision-{index}"), decide(&pending, index == 0))
            })
        })
        .collect();
    let results: Vec<_> = handles
        .into_iter()
        .map(|handle| handle.join().unwrap())
        .collect();
    assert_eq!(results.iter().filter(|result| result.is_ok()).count(), 1);
    assert!(results.iter().any(|result| matches!(
        result,
        Err(StoreError::Domain(DomainError::VersionConflict { .. }))
    )));
    let store = Store::open(db.path()).unwrap();
    assert_eq!(store.events_after(0, 100).unwrap().len(), 6);
    assert_eq!(approval(&store, "approval").version, 1);
    assert_eq!(
        ["decision-0", "decision-1"]
            .iter()
            .filter(|id| store.get_receipt(id).unwrap().is_some())
            .count(),
        1
    );
}

#[test]
fn approval_writes_roll_back_when_event_delivery_record_cannot_be_created() {
    let db = Database::new();
    let mut store = Store::open(db.path()).unwrap();
    let execution = db.running(&mut store);
    let injector = Connection::open(db.path()).unwrap();
    let fail = "CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT, 'injected'); END;";
    injector.execute_batch(fail).unwrap();
    let request = record("a", &execution, ApprovalRequestId::Integer(1));
    assert!(store.execute("record", request.clone()).is_err());
    assert!(store.get_approval("a").unwrap().is_none());
    assert!(store.get_receipt("record").unwrap().is_none());
    injector.execute_batch("DROP TRIGGER fail_outbox").unwrap();
    let receipt = store.execute("record", request).unwrap();
    let CommandResult::Approval(before) = receipt.result else {
        panic!("expected approval")
    };
    injector.execute_batch(fail).unwrap();
    assert!(store.execute("decide", decide(&before, true)).is_err());
    assert_eq!(approval(&store, "a"), *before);
    assert!(store.get_receipt("decide").unwrap().is_none());
    assert_eq!(store.events_after(0, 100).unwrap().len(), 5);
    assert_eq!(store.pending_outbox(100).unwrap().len(), 5);
}

#[test]
fn version_two_migration_backfills_routes_and_policies_without_rewriting_receipts() {
    let db = Database::new();
    let connection = Connection::open(db.path()).unwrap();
    connection
        .execute_batch(include_str!("../src/schema.sql"))
        .unwrap();
    connection
        .execute_batch(include_str!("../src/migration_002.sql"))
        .unwrap();
    connection
        .pragma_update(None, "application_id", 0x46475253_i64)
        .unwrap();
    connection.pragma_update(None, "user_version", 2).unwrap();
    let snapshot =
        serde_json::to_string(&forge_core::Run::with_config("run".into(), db.config()).unwrap())
            .unwrap();
    connection
        .execute(
            "INSERT INTO runs(id, snapshot_json) VALUES ('run', ?1)",
            [&snapshot],
        )
        .unwrap();
    drop(connection);
    let store = Store::open(db.path()).unwrap();
    assert_eq!(
        store.get_route_binding("run").unwrap(),
        Some(db.config().route_binding)
    );
    assert_eq!(
        store.get_execution_policy("run").unwrap(),
        Some(PermissionProfile::Auto.execution_policy())
    );
    assert!(store.get_run_provider_snapshot("run").unwrap().is_none());
    assert!(store.list_approvals("run", 100).unwrap().is_empty());
    drop(store);
    Store::open(db.path()).unwrap();
    let connection = Connection::open(db.path()).unwrap();
    let stored: String = connection
        .query_row("SELECT snapshot_json FROM runs WHERE id='run'", [], |row| {
            row.get(0)
        })
        .unwrap();
    assert_eq!(stored, snapshot);
    assert_eq!(
        connection
            .pragma_query_value(None, "user_version", |row| row.get::<_, i64>(0))
            .unwrap(),
        3
    );
}

#[test]
fn coordinator_next_turn_is_fenced_by_its_own_approvals_not_a_parallel_members() {
    let db = Database::new();
    let mut store = Store::open(db.path()).unwrap();
    let lead = db.running_with_peer(&mut store, true);
    store
        .execute(
            "peer-begin",
            transition(
                3,
                RunTransition::BeginMember {
                    worker_id: "peer".into(),
                    connection_id: "peer-connection".into(),
                },
            ),
        )
        .unwrap();
    let peer = store
        .get_run("run")
        .unwrap()
        .unwrap()
        .execution_for("peer")
        .unwrap()
        .connection
        .clone();
    store
        .execute(
            "peer-thread",
            transition(
                4,
                RunTransition::BindMemberThread {
                    connection: peer.clone(),
                    thread_id: "peer-thread".into(),
                },
            ),
        )
        .unwrap();
    store
        .execute(
            "peer-turn",
            transition(
                5,
                RunTransition::BindMemberTurn {
                    connection: peer,
                    turn_id: "peer-turn".into(),
                },
            ),
        )
        .unwrap();
    let peer = store
        .get_run("run")
        .unwrap()
        .unwrap()
        .execution_for("peer")
        .unwrap()
        .clone();
    store
        .execute(
            "peer-approval",
            record("peer-approval", &peer, ApprovalRequestId::Integer(1)),
        )
        .unwrap();
    let completed = |execution: &RunExecution, sequence| Command::RecordRuntimeEvent {
        run_id: "run".into(),
        connection: execution.connection.clone(),
        sequence,
        thread_id: execution.thread_id.clone(),
        turn_id: execution.turn_id.clone(),
        item_id: None,
        kind: "turn/completed".into(),
        terminal_status: Some("completed".into()),
    };
    store
        .execute("lead-completed", completed(&lead, 1))
        .unwrap();
    store
        .execute(
            "lead-next",
            transition(
                6,
                RunTransition::BindTurn {
                    connection: lead.connection.clone(),
                    turn_id: "lead-second".into(),
                },
            ),
        )
        .unwrap();
    assert_eq!(
        approval(&store, "peer-approval").status,
        ApprovalStatus::Pending
    );
    let lead = store.get_run("run").unwrap().unwrap().execution.unwrap();
    store
        .execute(
            "lead-approval",
            record("lead-approval", &lead, ApprovalRequestId::Integer(1)),
        )
        .unwrap();
    store
        .execute("lead-second-completed", completed(&lead, 2))
        .unwrap();
    assert!(matches!(
        store.execute(
            "lead-third",
            transition(
                7,
                RunTransition::BindTurn {
                    connection: lead.connection,
                    turn_id: "lead-third".into()
                }
            )
        ),
        Err(StoreError::Domain(DomainError::Approval(
            ApprovalError::UnresolvedApprovals
        )))
    ));
}
