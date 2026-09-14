use forge_core::{
    ApprovalKind, ApprovalRequest, ApprovalRequestId, ApprovalStatus, Command, CommandResult,
    DomainError, PermissionProfile, Receipt, RouteBinding, Run, RunConfig, RunConnection, RunError,
    RunMember, RunOwner, RunStatus, RunTransition,
};
use forge_store::{Store, StoreError};
use rusqlite::{params, Connection};
use serde_json::json;
use std::{path::PathBuf, sync::Arc, sync::Barrier, thread};
use uuid::Uuid;

struct TestDatabase(PathBuf);

impl TestDatabase {
    fn new() -> Self {
        let directory = std::env::temp_dir().join(format!("forge-run-contract-{}", Uuid::new_v4()));
        std::fs::create_dir(&directory).unwrap();
        Self(directory)
    }

    fn path(&self) -> PathBuf {
        self.0.join("store.sqlite3")
    }

    fn config(&self) -> RunConfig {
        RunConfig {
            project_id: "project".into(),
            owner_id: "user".into(),
            members: vec![
                RunMember {
                    worker_id: "worker".into(),
                    config_revision: "member-revision-1".into(),
                    role: String::new(),
                    instructions: String::new(),
                    tools: vec![],
                    skills: vec![],
                    permission_summary: None,
                    is_coordinator: true,
                    communication_recipients: vec![],
                },
                RunMember {
                    worker_id: "other-member".into(),
                    config_revision: "member-revision-2".into(),
                    role: String::new(),
                    instructions: String::new(),
                    tools: vec![],
                    skills: vec![],
                    permission_summary: None,
                    is_coordinator: false,
                    communication_recipients: vec![],
                },
            ],
            permission_profile: PermissionProfile::Auto,
            working_directory: self.0.join("workspace"),
            runtime_home: self.0.join("runtime"),
            runtime_version: "codex-fixture-1".into(),
            route_binding: RouteBinding {
                provider_id: "provider".into(),
                provider_revision_id: "provider-revision-1".into(),
                model_id: "upstream-model".into(),
                upstream_protocol: "anthropic_messages".into(),
                gateway_route_id: "immutable-route-1".into(),
                responses_base_url: "http://127.0.0.1:10100/v1".into(),
                config_digest: "a".repeat(64),
                credential_ref: "credential-revision-1".into(),
                capability_evidence_ref: "tool-probe-1".into(),
                gateway_version: "gateway-fixture-1".into(),
            },
        }
    }
}

impl Drop for TestDatabase {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn create(config: RunConfig) -> Command {
    Command::CreateBoundRun {
        run_id: "run".into(),
        config: Box::new(config),
    }
}

fn transition(expected_version: i64, transition: RunTransition) -> Command {
    Command::TransitionRun {
        run_id: "run".into(),
        expected_version,
        transition,
    }
}

fn begin(connection_id: &str) -> Command {
    transition(
        0,
        RunTransition::Begin {
            worker_id: "worker".into(),
            connection_id: connection_id.into(),
        },
    )
}

fn run_from(receipt: Receipt) -> Run {
    let CommandResult::Run(run) = receipt.result else {
        panic!("expected run receipt");
    };
    run
}

fn make_running(store: &mut Store) -> RunConnection {
    let started = run_from(store.execute("begin", begin("connection-1")).unwrap());
    let connection = started.execution.unwrap().connection;
    store
        .execute(
            "thread",
            transition(
                1,
                RunTransition::BindThread {
                    connection: connection.clone(),
                    thread_id: "thread-1".into(),
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
                    connection: connection.clone(),
                    turn_id: "turn-1".into(),
                },
            ),
        )
        .unwrap();
    connection
}

fn make_member_running(store: &mut Store, connection_id: &str) -> RunConnection {
    let version = store.get_run("run").unwrap().unwrap().version;
    let started = run_from(
        store
            .execute(
                &format!("{connection_id}:begin"),
                transition(
                    version,
                    RunTransition::BeginMember {
                        worker_id: "other-member".into(),
                        connection_id: connection_id.into(),
                    },
                ),
            )
            .unwrap(),
    );
    let connection = started.sessions["other-member"]
        .execution
        .connection
        .clone();
    store
        .execute(
            &format!("{connection_id}:thread"),
            transition(
                version + 1,
                RunTransition::BindMemberThread {
                    connection: connection.clone(),
                    thread_id: "member-thread".into(),
                },
            ),
        )
        .unwrap();
    store
        .execute(
            &format!("{connection_id}:turn"),
            transition(
                version + 2,
                RunTransition::BindMemberTurn {
                    connection: connection.clone(),
                    turn_id: format!("{connection_id}:turn"),
                },
            ),
        )
        .unwrap();
    connection
}

fn runtime_event(connection: RunConnection, sequence: u64) -> Command {
    Command::RecordRuntimeEvent {
        run_id: "run".into(),
        connection,
        sequence,
        thread_id: Some("thread-1".into()),
        turn_id: Some("turn-1".into()),
        item_id: Some("item-1".into()),
        kind: "item/completed".into(),
        terminal_status: None,
    }
}

#[test]
fn runtime_metadata_is_durable_idempotent_and_does_not_advance_run_version() {
    let database = TestDatabase::new();
    let mut store = Store::open(database.path()).unwrap();
    store.execute("create", create(database.config())).unwrap();
    let connection = make_running(&mut store);
    let running = store.get_run("run").unwrap().unwrap();
    let command = runtime_event(connection.clone(), 1);
    let receipt = store.execute("runtime-1", command.clone()).unwrap();
    assert_eq!(run_from(receipt.clone()), running);
    assert_eq!(store.get_run("run").unwrap(), Some(running.clone()));
    let events = store.runtime_events_after("run", 0, 100).unwrap();
    assert_eq!(events.len(), 1);
    assert_eq!(events[0].id, receipt.event_id);
    assert_eq!(events[0].kind, "runtime_event");
    assert_eq!(
        events[0].payload,
        json!({
            "run_id": "run", "owner": "rust", "status": "running", "version": 3,
            "connection": connection, "sequence": 1,
            "thread_id": "thread-1", "turn_id": "turn-1", "item_id": "item-1",
            "kind": "item/completed", "terminal_status": null
        })
    );
    assert_eq!(
        store.pending_outbox(100).unwrap().last().unwrap().event,
        events[0]
    );
    assert!(store
        .runtime_events_after("another-run", 0, 100)
        .unwrap()
        .is_empty());
    assert!(store.runtime_events_after("run", 0, 0).unwrap().is_empty());
    let second = store
        .execute("runtime-2", runtime_event(connection, 2))
        .unwrap();
    assert_eq!(
        store
            .runtime_events_after("run", receipt.event_id, 1)
            .unwrap()[0]
            .id,
        second.event_id
    );
    drop(store);

    let mut reopened = Store::open(database.path()).unwrap();
    assert_eq!(
        reopened.execute("runtime-1", command.clone()).unwrap(),
        receipt
    );
    assert_eq!(reopened.get_receipt("runtime-1").unwrap(), Some(receipt));
    assert_eq!(reopened.get_run("run").unwrap(), Some(running));
    let mut changed = command.clone();
    if let Command::RecordRuntimeEvent { item_id, .. } = &mut changed {
        *item_id = Some("different-item".into());
    }
    assert!(matches!(
        reopened.execute("runtime-1", changed),
        Err(StoreError::CommandConflict(_))
    ));
    assert!(matches!(
        reopened.execute("same-sequence", command),
        Err(StoreError::RuntimeEventSequenceConflict { sequence: 1, .. })
    ));
    assert!(reopened.get_receipt("same-sequence").unwrap().is_none());
    assert_eq!(
        reopened.runtime_events_after("run", 0, 100).unwrap().len(),
        2
    );
    assert_eq!(reopened.events_after(0, 100).unwrap().len(), 6);
    assert_eq!(reopened.pending_outbox(100).unwrap().len(), 6);
}

#[test]
fn runtime_metadata_rejects_stale_connection_and_wrong_execution_scope_atomically() {
    let database = TestDatabase::new();
    let mut store = Store::open(database.path()).unwrap();
    store.execute("create", create(database.config())).unwrap();
    let connection = make_running(&mut store);
    let before = store.get_run("run").unwrap();
    for (index, field) in [
        "worker",
        "connection",
        "generation",
        "thread",
        "turn",
        "global-turn",
    ]
    .iter()
    .enumerate()
    {
        let mut command = runtime_event(connection.clone(), 1);
        if let Command::RecordRuntimeEvent {
            connection,
            thread_id,
            turn_id,
            ..
        } = &mut command
        {
            match *field {
                "worker" => connection.worker_id = "other-member".into(),
                "connection" => connection.connection_id = "another-connection".into(),
                "generation" => connection.generation += 1,
                "thread" => *thread_id = Some("another-thread".into()),
                "turn" => *turn_id = Some("another-turn".into()),
                "global-turn" => *thread_id = None,
                _ => unreachable!(),
            }
        }
        let command_id = format!("invalid-runtime-{index}");
        let result = store.execute(&command_id, command);
        if *field == "thread" {
            assert!(matches!(
                result,
                Err(StoreError::Domain(DomainError::Run(
                    RunError::ThreadMismatch
                )))
            ));
        } else {
            assert!(matches!(
                result,
                Err(StoreError::Domain(DomainError::Run(
                    RunError::StaleExecution
                )))
            ));
        }
        assert!(store.get_receipt(&command_id).unwrap().is_none());
    }
    assert_eq!(store.get_run("run").unwrap(), before);
    assert!(store
        .runtime_events_after("run", 0, 100)
        .unwrap()
        .is_empty());
    assert_eq!(store.events_after(0, 100).unwrap().len(), 4);
    assert_eq!(store.pending_outbox(100).unwrap().len(), 4);
}

#[test]
fn runtime_metadata_accepts_global_startup_events_and_fences_resume_generations() {
    let database = TestDatabase::new();
    let mut store = Store::open(database.path()).unwrap();
    store.execute("create", create(database.config())).unwrap();
    let started = run_from(store.execute("begin", begin("connection-1")).unwrap());
    let old_connection = started.execution.unwrap().connection;
    let global = |connection| Command::RecordRuntimeEvent {
        run_id: "run".into(),
        connection,
        sequence: 0,
        thread_id: None,
        turn_id: None,
        item_id: None,
        kind: "account/updated".into(),
        terminal_status: None,
    };
    let original = store
        .execute("global-first", global(old_connection.clone()))
        .unwrap();
    assert!(matches!(
        store.execute("unbound-scope", runtime_event(old_connection.clone(), 1)),
        Err(StoreError::Domain(DomainError::Run(
            RunError::ThreadMismatch
        )))
    ));
    let paused = run_from(
        store
            .execute(
                "pause",
                transition(
                    1,
                    RunTransition::Pause {
                        connection: old_connection.clone(),
                        evidence_ref: "stopped-before-thread".into(),
                    },
                ),
            )
            .unwrap(),
    );
    assert!(matches!(
        store.execute("late-global", global(old_connection.clone())),
        Err(StoreError::Domain(DomainError::Run(
            RunError::InvalidState(RunStatus::Paused)
        )))
    ));
    assert_eq!(
        store
            .execute("global-first", global(old_connection.clone()))
            .unwrap(),
        original
    );
    let resumed = run_from(
        store
            .execute(
                "resume",
                transition(
                    paused.version,
                    RunTransition::Begin {
                        worker_id: "worker".into(),
                        connection_id: "connection-2".into(),
                    },
                ),
            )
            .unwrap(),
    );
    let new_connection = resumed.execution.unwrap().connection;
    store
        .execute("global-second", global(new_connection))
        .unwrap();
    assert!(matches!(
        store.execute("stale-global", global(old_connection)),
        Err(StoreError::Domain(DomainError::Run(
            RunError::StaleExecution
        )))
    ));
    let events = store.runtime_events_after("run", 0, 100).unwrap();
    assert_eq!(events.len(), 2);
    assert_eq!(events[0].payload["sequence"], 0);
    assert_eq!(events[1].payload["sequence"], 0);
    assert_eq!(events[0].payload["connection"]["generation"], 1);
    assert_eq!(events[1].payload["connection"]["generation"], 2);
}

#[test]
fn runtime_metadata_outbox_failure_rolls_back_and_can_be_retried() {
    let database = TestDatabase::new();
    let mut store = Store::open(database.path()).unwrap();
    store.execute("create", create(database.config())).unwrap();
    let connection = make_running(&mut store);
    let before = store.get_run("run").unwrap();
    let database_connection = Connection::open(database.path()).unwrap();
    database_connection.execute_batch(
        "CREATE TRIGGER fail_runtime_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT, 'injected outbox failure'); END;"
    ).unwrap();
    let command = runtime_event(connection, 1);
    assert!(matches!(
        store.execute("runtime", command.clone()),
        Err(StoreError::Sql(_))
    ));
    assert!(store.get_receipt("runtime").unwrap().is_none());
    assert!(store
        .runtime_events_after("run", 0, 100)
        .unwrap()
        .is_empty());
    assert_eq!(store.get_run("run").unwrap(), before);
    assert_eq!(store.events_after(0, 100).unwrap().len(), 4);
    assert_eq!(store.pending_outbox(100).unwrap().len(), 4);
    database_connection
        .execute_batch("DROP TRIGGER fail_runtime_outbox")
        .unwrap();
    store.execute("runtime", command).unwrap();
    assert_eq!(store.runtime_events_after("run", 0, 100).unwrap().len(), 1);
    assert_eq!(store.pending_outbox(100).unwrap().len(), 5);
}

#[test]
fn concurrent_runtime_events_cannot_reuse_a_connection_sequence() {
    let database = TestDatabase::new();
    let mut store = Store::open(database.path()).unwrap();
    store.execute("create", create(database.config())).unwrap();
    let connection = make_running(&mut store);
    let before = store.get_run("run").unwrap();
    let barrier = Arc::new(Barrier::new(2));
    let workers: Vec<_> = (0..2)
        .map(|index| {
            let path = database.path();
            let barrier = barrier.clone();
            let connection = connection.clone();
            thread::spawn(move || {
                let mut contender = Store::open(path).unwrap();
                barrier.wait();
                contender.execute(&format!("contender-{index}"), runtime_event(connection, 11))
            })
        })
        .collect();
    let results: Vec<_> = workers
        .into_iter()
        .map(|worker| worker.join().unwrap())
        .collect();
    assert_eq!(results.iter().filter(|result| result.is_ok()).count(), 1);
    assert_eq!(
        results
            .iter()
            .filter(|result| matches!(
                result,
                Err(StoreError::RuntimeEventSequenceConflict { sequence: 11, .. })
            ))
            .count(),
        1
    );
    assert_eq!(store.get_run("run").unwrap(), before);
    assert_eq!(store.runtime_events_after("run", 0, 100).unwrap().len(), 1);
    assert_eq!(store.pending_outbox(100).unwrap().len(), 5);
}

#[test]
fn frozen_config_route_and_receipts_survive_reopen_without_restarting_execution() {
    let database = TestDatabase::new();
    let config = database.config();
    let command = create(config.clone());
    let mut store = Store::open(database.path()).unwrap();
    let created = store.execute("create", command.clone()).unwrap();
    make_running(&mut store);
    let started = store.get_receipt("begin").unwrap().unwrap();
    let running = store.get_run("run").unwrap().unwrap();
    assert_eq!(running.status, RunStatus::Running);
    assert_eq!(running.owner, RunOwner::Rust);
    assert_eq!(running.config.as_deref(), Some(&config));
    drop(store);

    let mut reopened = Store::open(database.path()).unwrap();
    assert_eq!(reopened.get_run("run").unwrap(), Some(running.clone()));
    assert_eq!(
        reopened.get_route_binding("run").unwrap(),
        Some(config.route_binding.clone())
    );
    assert_eq!(reopened.get_route_binding("missing").unwrap(), None);
    assert_eq!(
        reopened.get_receipt("create").unwrap(),
        Some(created.clone())
    );
    assert_eq!(reopened.execute("create", command).unwrap(), created);
    assert_eq!(
        reopened.execute("begin", begin("connection-1")).unwrap(),
        started
    );
    assert_eq!(reopened.get_run("run").unwrap(), Some(running.clone()));

    let mut changed = config.clone();
    changed.route_binding.provider_revision_id = "replacement-revision".into();
    assert!(matches!(
        reopened.execute("create", create(changed.clone())),
        Err(StoreError::CommandConflict(_))
    ));
    assert!(matches!(
        reopened.execute("replace-route", create(changed)),
        Err(StoreError::AlreadyExists { kind: "run", .. })
    ));
    assert_eq!(reopened.get_run("run").unwrap(), Some(running));
    assert!(reopened.get_receipt("replace-route").unwrap().is_none());
    let events = reopened.events_after(0, 100).unwrap();
    assert_eq!(events.len(), 4);
    assert_eq!(reopened.pending_outbox(100).unwrap().len(), 4);
    let public_events = serde_json::to_string(&events).unwrap();
    assert!(!public_events.contains(&config.route_binding.credential_ref));
    assert!(!public_events.contains(&config.route_binding.gateway_route_id));
    assert!(!public_events.contains(config.runtime_home.to_str().unwrap()));
}

#[test]
fn v1_snapshots_and_receipts_migrate_without_rewriting_historical_data() {
    let database = TestDatabase::new();
    let legacy_snapshot = json!({"id": "legacy", "status": "pending"}).to_string();
    let legacy_request = json!({"type": "create_run", "run_id": "legacy"}).to_string();
    let legacy_receipt = json!({
        "command_id": "legacy-create", "event_id": 1,
        "result": {"type": "run", "value": {"id": "legacy", "status": "pending"}}
    })
    .to_string();
    {
        let connection = Connection::open(database.path()).unwrap();
        connection
            .execute_batch(include_str!("../src/schema.sql"))
            .unwrap();
        connection
            .pragma_update(None, "application_id", 0x46475253_i64)
            .unwrap();
        connection
            .pragma_update(None, "user_version", 1_i64)
            .unwrap();
        connection
            .execute(
                "INSERT INTO runs(id, snapshot_json) VALUES ('legacy', ?1)",
                [&legacy_snapshot],
            )
            .unwrap();
        connection
            .execute(
                "INSERT INTO commands(id, request_json, receipt_json) VALUES ('legacy-create', ?1, ?2)",
                params![legacy_request, legacy_receipt],
            )
            .unwrap();
        connection
            .execute(
                "INSERT INTO events(id, command_id, run_id, kind, payload_json, created_at_ms) VALUES (1, 'legacy-create', 'legacy', 'run_created', ?1, 10)",
                [json!({"run_id": "legacy", "status": "pending"}).to_string()],
            )
            .unwrap();
        connection
            .execute("INSERT INTO outbox(event_id) VALUES (1)", [])
            .unwrap();
    }

    let mut store = Store::open(database.path()).unwrap();
    let legacy = store.get_run("legacy").unwrap().unwrap();
    assert_eq!(legacy, Run::new("legacy".into()).unwrap());
    assert_eq!(store.get_route_binding("legacy").unwrap(), None);
    let expected: Receipt = serde_json::from_str(&legacy_receipt).unwrap();
    assert_eq!(
        store.get_receipt("legacy-create").unwrap(),
        Some(expected.clone())
    );
    assert_eq!(
        store
            .execute(
                "legacy-create",
                Command::CreateRun {
                    run_id: "legacy".into()
                }
            )
            .unwrap(),
        expected
    );
    assert!(matches!(
        store.execute(
            "legacy-begin",
            Command::TransitionRun {
                run_id: "legacy".into(),
                expected_version: 0,
                transition: RunTransition::Begin {
                    worker_id: "worker".into(),
                    connection_id: "old".into()
                }
            }
        ),
        Err(StoreError::Domain(DomainError::Run(RunError::Unbound)))
    ));
    assert_eq!(store.events_after(0, 100).unwrap().len(), 1);
    assert_eq!(store.pending_outbox(100).unwrap().len(), 1);
    store
        .execute("new-bound", create(database.config()))
        .unwrap();
    drop(store);

    // A second open must not reapply ALTER TABLE or reinterpret old receipts.
    let reopened = Store::open(database.path()).unwrap();
    assert_eq!(reopened.get_run("legacy").unwrap(), Some(legacy));
    assert_eq!(
        reopened.get_receipt("legacy-create").unwrap(),
        Some(expected)
    );
    assert!(reopened.get_route_binding("run").unwrap().is_some());
    let connection = Connection::open(database.path()).unwrap();
    let version: i64 = connection
        .pragma_query_value(None, "user_version", |row| row.get(0))
        .unwrap();
    assert_eq!(version, 3);
    let stored_snapshot: String = connection
        .query_row(
            "SELECT snapshot_json FROM runs WHERE id='legacy'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    let stored_receipt: String = connection
        .query_row(
            "SELECT receipt_json FROM commands WHERE id='legacy-create'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(stored_snapshot, legacy_snapshot);
    assert_eq!(stored_receipt, legacy_receipt);
}

#[test]
fn concurrent_begin_commands_have_one_owner_and_one_durable_event() {
    let database = TestDatabase::new();
    let mut first = Store::open(database.path()).unwrap();
    first.execute("create", create(database.config())).unwrap();
    let second = Store::open(database.path()).unwrap();
    let barrier = Arc::new(Barrier::new(2));
    let handles: Vec<_> = [first, second]
        .into_iter()
        .enumerate()
        .map(|(index, mut store)| {
            let barrier = barrier.clone();
            thread::spawn(move || {
                let command_id = format!("begin-{index}");
                barrier.wait();
                let result = store.execute(&command_id, begin(&format!("connection-{index}")));
                (command_id, result)
            })
        })
        .collect();
    let results: Vec<_> = handles
        .into_iter()
        .map(|handle| handle.join().unwrap())
        .collect();
    assert_eq!(
        results.iter().filter(|(_, result)| result.is_ok()).count(),
        1
    );
    let reopened = Store::open(database.path()).unwrap();
    for (command_id, result) in results {
        match result {
            Ok(receipt) => {
                assert_eq!(
                    reopened.get_receipt(&command_id).unwrap(),
                    Some(receipt.clone())
                );
                assert_eq!(reopened.get_run("run").unwrap(), Some(run_from(receipt)));
            }
            Err(error) => {
                assert!(matches!(
                    error,
                    StoreError::Domain(DomainError::VersionConflict {
                        expected: 0,
                        actual: 1
                    })
                ));
                assert!(reopened.get_receipt(&command_id).unwrap().is_none());
            }
        }
    }
    assert_eq!(reopened.events_after(0, 100).unwrap().len(), 2);
    assert_eq!(reopened.pending_outbox(100).unwrap().len(), 2);
}

#[test]
fn outbox_failure_rolls_back_bound_creation_and_run_transition() {
    let database = TestDatabase::new();
    let mut store = Store::open(database.path()).unwrap();
    let injector = Connection::open(database.path()).unwrap();
    let fail_outbox = "CREATE TRIGGER fail_run_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT, 'injected outbox failure'); END;";
    injector.execute_batch(fail_outbox).unwrap();
    let create_command = create(database.config());
    assert!(matches!(
        store.execute("create", create_command.clone()),
        Err(StoreError::Sql(_))
    ));
    assert!(store.get_run("run").unwrap().is_none());
    assert!(store.get_route_binding("run").unwrap().is_none());
    assert!(store.get_receipt("create").unwrap().is_none());
    assert!(store.events_after(0, 100).unwrap().is_empty());
    assert!(store.pending_outbox(100).unwrap().is_empty());

    injector
        .execute_batch("DROP TRIGGER fail_run_outbox")
        .unwrap();
    store.execute("create", create_command).unwrap();
    let before = store.get_run("run").unwrap().unwrap();
    injector.execute_batch(fail_outbox).unwrap();
    assert!(matches!(
        store.execute("begin", begin("connection-1")),
        Err(StoreError::Sql(_))
    ));
    assert_eq!(store.get_run("run").unwrap(), Some(before));
    assert!(store.get_receipt("begin").unwrap().is_none());
    assert_eq!(store.events_after(0, 100).unwrap().len(), 1);
    assert_eq!(store.pending_outbox(100).unwrap().len(), 1);

    injector
        .execute_batch("DROP TRIGGER fail_run_outbox")
        .unwrap();
    let started = run_from(store.execute("begin", begin("connection-1")).unwrap());
    assert_eq!(started.version, 1);
    assert_eq!(started.execution.unwrap().connection.generation, 1);
    assert_eq!(store.events_after(0, 100).unwrap().len(), 2);
    assert_eq!(store.pending_outbox(100).unwrap().len(), 2);
}

#[test]
fn tasks_require_the_active_frozen_member_and_cannot_execute_while_paused() {
    let database = TestDatabase::new();
    let mut store = Store::open(database.path()).unwrap();
    store.execute("create", create(database.config())).unwrap();
    let connection = make_running(&mut store);
    for task_id in ["task", "ready-task"] {
        store
            .execute(
                &format!("create-{task_id}"),
                Command::CreateTask {
                    run_id: "run".into(),
                    task_id: task_id.into(),
                },
            )
            .unwrap();
    }
    let ready = store.get_task("task").unwrap().unwrap();
    let claim = |task_id: &str, worker_id: &str| Command::ClaimTask {
        task_id: task_id.into(),
        worker_id: worker_id.into(),
        expected_version: 0,
        lease_duration_ms: 900_000,
    };
    for worker in ["outsider", "other-member"] {
        assert!(matches!(
            store.execute(worker, claim("task", worker)),
            Err(StoreError::Domain(DomainError::Run(
                RunError::UnauthorizedWorker
            )))
        ));
        assert!(store.get_receipt(worker).unwrap().is_none());
        assert_eq!(store.get_task("task").unwrap(), Some(ready.clone()));
    }
    store.execute("claim", claim("task", "worker")).unwrap();
    let running_task = store.get_task("task").unwrap().unwrap();
    let held = running_task.claim.as_ref().unwrap();
    store
        .execute(
            "pause",
            transition(
                3,
                RunTransition::Pause {
                    connection,
                    evidence_ref: "runtime-interrupted".into(),
                },
            ),
        )
        .unwrap();
    let paused = store.get_run("run").unwrap().unwrap();
    let event_count = store.events_after(0, 100).unwrap().len();
    for (id, command) in [
        ("paused-claim", claim("ready-task", "worker")),
        (
            "paused-renew",
            Command::RenewClaim {
                task_id: "task".into(),
                claim_token: held.token.clone(),
                attempt_generation: held.generation,
                lease_duration_ms: 900_000,
            },
        ),
        (
            "paused-submit",
            Command::SubmitTask {
                task_id: "task".into(),
                claim_token: held.token.clone(),
                attempt_generation: held.generation,
                result: json!({"candidate": true}),
            },
        ),
    ] {
        assert!(matches!(
            store.execute(id, command),
            Err(StoreError::Domain(DomainError::Run(
                RunError::InvalidState(RunStatus::Paused)
            )))
        ));
        assert!(store.get_receipt(id).unwrap().is_none());
    }
    assert_eq!(store.get_task("task").unwrap(), Some(running_task.clone()));
    assert_eq!(store.get_run("run").unwrap(), Some(paused.clone()));
    assert_eq!(store.events_after(0, 100).unwrap().len(), event_count);
    let resume = transition(
        paused.version,
        RunTransition::Begin {
            worker_id: "worker".into(),
            connection_id: "connection-2".into(),
        },
    );
    assert!(matches!(
        store.execute("resume", resume.clone()),
        Err(StoreError::Domain(DomainError::Run(
            RunError::UnresolvedTasks
        )))
    ));
    assert_eq!(store.get_run("run").unwrap(), Some(paused));
    assert!(store.get_receipt("resume").unwrap().is_none());
    store
        .execute(
            "release",
            Command::ReleaseTask {
                task_id: "task".into(),
                claim_token: held.token.clone(),
                attempt_generation: held.generation,
            },
        )
        .unwrap();
    let resumed = run_from(store.execute("resume", resume).unwrap());
    assert_eq!(resumed.status, RunStatus::Starting);
    let execution = resumed.execution.unwrap();
    assert_eq!(execution.thread_id.as_deref(), Some("thread-1"));
    assert_eq!(execution.connection.generation, 2);
    assert!(execution.turn_id.is_none());
}

#[test]
fn coordinator_can_assign_ready_task_to_frozen_member_and_claim_is_fenced() {
    let database = TestDatabase::new();
    let mut store = Store::open(database.path()).unwrap();
    store.execute("create", create(database.config())).unwrap();
    store
        .execute(
            "task",
            Command::CreateTask {
                run_id: "run".into(),
                task_id: "delegated".into(),
            },
        )
        .unwrap();

    let unauthorized = store.execute(
        "bad-assign",
        Command::AssignTask {
            task_id: "delegated".into(),
            coordinator_id: "other-member".into(),
            worker_id: "worker".into(),
            expected_version: 0,
        },
    );
    assert!(matches!(
        unauthorized,
        Err(StoreError::Domain(DomainError::Run(
            RunError::UnauthorizedWorker
        )))
    ));
    store
        .execute(
            "assign",
            Command::AssignTask {
                task_id: "delegated".into(),
                coordinator_id: "worker".into(),
                worker_id: "other-member".into(),
                expected_version: 0,
            },
        )
        .unwrap();
    let assigned = store.get_task("delegated").unwrap().unwrap();
    assert_eq!(assigned.assigned_worker_id.as_deref(), Some("other-member"));
    assert_eq!(assigned.assigned_by.as_deref(), Some("worker"));

    let connection = make_running(&mut store);
    make_member_running(&mut store, "member-connection");
    let claim = store
        .execute(
            "claim-other",
            Command::ClaimTask {
                task_id: "delegated".into(),
                worker_id: "other-member".into(),
                expected_version: 1,
                lease_duration_ms: 900_000,
            },
        )
        .unwrap();
    let task = match claim.result {
        CommandResult::Task(task) => task,
        _ => panic!("expected task"),
    };
    assert_eq!(
        task.claim.as_ref().map(|claim| claim.worker_id.as_str()),
        Some("other-member")
    );
    assert_eq!(connection.worker_id, "worker");

    let wrong = store.execute(
        "claim-coordinator",
        Command::ClaimTask {
            task_id: "delegated".into(),
            worker_id: "worker".into(),
            expected_version: 1,
            lease_duration_ms: 900_000,
        },
    );
    assert!(matches!(
        wrong,
        Err(StoreError::Domain(DomainError::Run(
            RunError::UnauthorizedWorker
        )))
    ));
}

#[test]
fn member_connections_and_approvals_are_independently_fenced_and_durable() {
    let database = TestDatabase::new();
    let mut store = Store::open(database.path()).unwrap();
    store.execute("create", create(database.config())).unwrap();
    let leader = make_running(&mut store);
    let member = make_member_running(&mut store, "member-1");
    let run = store.get_run("run").unwrap().unwrap();
    let leader_execution = run.execution.clone().unwrap();
    let member_execution = run.sessions["other-member"].execution.clone();
    for (id, execution) in [
        ("leader-approval", leader_execution.clone()),
        ("member-approval", member_execution.clone()),
    ] {
        store
            .execute(
                id,
                Command::RecordApproval {
                    approval_id: id.into(),
                    request: Box::new(ApprovalRequest {
                        run_id: "run".into(),
                        execution,
                        request_id: ApprovalRequestId::Integer(17),
                        item_id: "item".into(),
                        kind: ApprovalKind::CommandExecution,
                        payload: json!({"command":"cargo test"}),
                    }),
                },
            )
            .unwrap();
    }
    assert!(matches!(
        store.execute(
            "premature-submit",
            transition(
                run.version,
                RunTransition::Submit {
                    execution: leader_execution.clone(),
                    result: json!({})
                }
            )
        ),
        Err(StoreError::Domain(DomainError::Run(
            RunError::UnresolvedMemberSessions
        )))
    ));
    let stopped = run_from(
        store
            .execute(
                "stop-member",
                transition(
                    run.version,
                    RunTransition::EndMember {
                        connection: member.clone(),
                        evidence_ref: "observed-stop".into(),
                    },
                ),
            )
            .unwrap(),
    );
    assert_eq!(stopped.execution, Some(leader_execution));
    assert_eq!(
        store
            .get_approval("member-approval")
            .unwrap()
            .unwrap()
            .status,
        ApprovalStatus::Expired
    );
    assert_eq!(
        store
            .get_approval("leader-approval")
            .unwrap()
            .unwrap()
            .status,
        ApprovalStatus::Pending
    );
    assert!(matches!(
        store.execute(
            "old-member-event",
            Command::RecordRuntimeEvent {
                run_id: "run".into(),
                connection: member.clone(),
                sequence: 1,
                thread_id: Some("member-thread".into()),
                turn_id: Some("member-1:turn".into()),
                item_id: None,
                kind: "turn/completed".into(),
                terminal_status: Some("completed".into())
            }
        ),
        Err(StoreError::Domain(DomainError::Run(
            RunError::StaleExecution
        )))
    ));
    store
        .execute("leader-still-live", runtime_event(leader, 1))
        .unwrap();
    let next_member = make_member_running(&mut store, "member-2");
    assert_eq!(next_member.generation, member.generation + 1);
    let restarted = store.get_run("run").unwrap().unwrap();
    assert_eq!(
        restarted.sessions["other-member"]
            .execution
            .thread_id
            .as_deref(),
        Some("member-thread")
    );
    assert!(matches!(
        store.execute(
            "old-member-approval",
            Command::RecordApproval {
                approval_id: "stale".into(),
                request: Box::new(ApprovalRequest {
                    run_id: "run".into(),
                    execution: member_execution,
                    request_id: ApprovalRequestId::Integer(18),
                    item_id: "item".into(),
                    kind: ApprovalKind::CommandExecution,
                    payload: json!({"command":"cargo test"})
                }),
            }
        ),
        Err(StoreError::Domain(DomainError::Approval(
            forge_core::ApprovalError::StaleExecution
        )))
    ));
    drop(store);
    let restored = Store::open(database.path()).unwrap();
    assert_eq!(restored.get_run("run").unwrap(), Some(restarted));
    assert!(restored
        .get_receipt("old-member-approval")
        .unwrap()
        .is_none());
}

#[test]
fn task_creation_and_assignment_commit_as_one_idempotent_command() {
    let database = TestDatabase::new();
    let mut store = Store::open(database.path()).unwrap();
    store.execute("create", create(database.config())).unwrap();
    let command = Command::CreateAssignedTask {
        run_id: "run".into(),
        task_id: "assigned".into(),
        coordinator_id: "worker".into(),
        worker_id: "other-member".into(),
    };
    let injector = Connection::open(database.path()).unwrap();
    injector.execute_batch("CREATE TRIGGER fail_assignment_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT, 'injected failure'); END;").unwrap();
    assert!(store.execute("assignment", command.clone()).is_err());
    assert!(store.get_task("assigned").unwrap().is_none());
    assert!(store.get_receipt("assignment").unwrap().is_none());
    injector
        .execute_batch("DROP TRIGGER fail_assignment_outbox")
        .unwrap();
    let receipt = store.execute("assignment", command.clone()).unwrap();
    assert_eq!(store.execute("assignment", command).unwrap(), receipt);
    let task = store.get_task("assigned").unwrap().unwrap();
    assert_eq!(task.version, 1);
    assert_eq!(task.assigned_worker_id.as_deref(), Some("other-member"));
    let events = store.events_after(0, 100).unwrap();
    assert_eq!(events.len(), 2);
    assert_eq!(events[1].payload["assigned_by"], "worker");
}
