//! Contract fixture for a Rust-owned task and a simulated Codex App Server.
//! Only forge-store writes task state. The fixture's history models remote
//! evidence; neither a transport reply nor that history authorizes a retry.

use forge_core::{Claim, Command, CommandResult, DomainError, RunStatus, Task, TaskStatus};
use forge_runtime::{CodexClient, RuntimeConfig, RuntimeError, RuntimeEvent, TurnTerminalStatus};
use forge_store::{Clock, Store, StoreError};
use serde_json::{json, Value};
use std::{
    path::PathBuf,
    sync::{
        atomic::{AtomicI64, Ordering},
        Arc,
    },
    time::Duration,
};
use tempfile::TempDir;

const DEADLINE: Duration = Duration::from_secs(5);
const RUN: &str = "fixture-run";
const TASK: &str = "fixture-task";

struct TestClock(AtomicI64);

impl Clock for TestClock {
    fn now_ms(&self) -> i64 {
        self.0.load(Ordering::SeqCst)
    }
}

struct Fixture {
    root: TempDir,
    clock: Arc<TestClock>,
}

impl Fixture {
    fn new() -> Self {
        Self {
            root: TempDir::new().unwrap(),
            clock: Arc::new(TestClock(AtomicI64::new(1_000))),
        }
    }

    fn cwd(&self) -> PathBuf {
        self.root.path().canonicalize().unwrap()
    }

    fn store(&self) -> Store {
        Store::open_with_clock(self.root.path().join("kernel.sqlite3"), self.clock.clone()).unwrap()
    }

    fn seed(&self) -> (Store, Claim) {
        let mut store = self.store();
        store
            .execute("create-run", Command::CreateRun { run_id: RUN.into() })
            .unwrap();
        store
            .execute(
                "create-task",
                Command::CreateTask {
                    run_id: RUN.into(),
                    task_id: TASK.into(),
                },
            )
            .unwrap();
        let receipt = store.execute("claim-first", claim_command(0)).unwrap();
        let CommandResult::Task(task) = receipt.result else {
            panic!("expected a claimed task");
        };
        (store, task.claim.unwrap())
    }

    async fn start(&self, mode: &str) -> CodexClient {
        let mut config = RuntimeConfig::new(
            PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join("tests/fixtures/execution_app_server.py"),
            self.cwd(),
            self.root.path().join("runtime"),
        );
        config
            .forge_env
            .insert("FORGE_EXECUTION_FIXTURE_MODE".into(), mode.into());
        CodexClient::start(config, DEADLINE).await.unwrap().0
    }

    fn expire(&self, claim: &Claim) {
        self.clock.0.store(claim.expires_at_ms, Ordering::SeqCst);
    }

    fn effect(&self) -> PathBuf {
        self.cwd().join("fixture-result.txt")
    }
}

fn task(store: &Store) -> Task {
    store.get_task(TASK).unwrap().unwrap()
}

fn claim_command(expected_version: i64) -> Command {
    Command::ClaimTask {
        task_id: TASK.into(),
        worker_id: "fixture-member".into(),
        expected_version,
        lease_duration_ms: 100,
    }
}

fn submit_command(claim: &Claim, result: Value) -> Command {
    Command::SubmitTask {
        task_id: TASK.into(),
        claim_token: claim.token.clone(),
        attempt_generation: claim.generation,
        result,
    }
}

fn mark_unknown(fixture: &Fixture, store: &mut Store, claim: &Claim) {
    let command = Command::MarkOutcomeUnknown {
        task_id: TASK.into(),
        expected_version: task(store).version,
    };
    assert!(matches!(
        store.execute("unknown", command.clone()),
        Err(StoreError::Domain(DomainError::LeaseActive))
    ));
    fixture.expire(claim);
    store.execute("unknown", command).unwrap();
    assert_eq!(task(store).status, TaskStatus::OutcomeUnknown);
    assert!(matches!(
        store.execute("automatic-reclaim", claim_command(task(store).version)),
        Err(StoreError::Domain(DomainError::InvalidState(
            TaskStatus::OutcomeUnknown
        )))
    ));
    assert!(store.get_receipt("automatic-reclaim").unwrap().is_none());
}

async fn start_thread(fixture: &Fixture, client: &CodexClient) -> String {
    let response = client
        .request(
            "thread/start",
            json!({
                "cwd": fixture.cwd(),
                "approvalPolicy": "on-request",
                "sandbox": "workspace-write"
            }),
            DEADLINE,
        )
        .await
        .unwrap();
    assert_eq!(response["thread"]["cwd"], json!(fixture.cwd()));
    response["thread"]["id"].as_str().unwrap().into()
}

async fn start_turn(client: &CodexClient, thread: &str) -> String {
    let response = client.request("turn/start", json!({
        "threadId": thread,
        "input": [{"type": "text", "text": "Write the fixture result.", "text_elements": []}]
    }), DEADLINE).await.unwrap();
    assert_eq!(response["turn"]["status"], "inProgress");
    response["turn"]["id"].as_str().unwrap().into()
}

async fn until(client: &CodexClient, events: &mut Vec<RuntimeEvent>, kind: &str) -> RuntimeEvent {
    for _ in 0..20 {
        let event = client.next_event(DEADLINE).await.unwrap();
        let found = event.kind == kind;
        events.push(event.clone());
        if found {
            return event;
        }
    }
    panic!("fixture did not emit {kind}");
}

async fn approval(
    client: &CodexClient,
    events: &mut Vec<RuntimeEvent>,
    thread: &str,
    turn: &str,
) -> RuntimeEvent {
    let event = until(client, events, "item/commandExecution/requestApproval").await;
    assert_eq!(event.thread_id.as_deref(), Some(thread));
    assert_eq!(event.turn_id.as_deref(), Some(turn));
    assert!(event.item_id.is_some());
    assert!(event.request_id.is_some());
    event
}

async fn accept(client: &CodexClient, event: &RuntimeEvent) {
    client
        .respond(
            event.request_id.as_ref().unwrap(),
            json!({"decision": "accept"}),
            DEADLINE,
        )
        .await
        .unwrap();
}

async fn history(client: &CodexClient, thread: &str) -> Value {
    client
        .request(
            "thread/read",
            json!({"threadId": thread, "includeTurns": true}),
            DEADLINE,
        )
        .await
        .unwrap()["thread"]
        .clone()
}

async fn stats(client: &CodexClient) -> Value {
    client
        .request("fixture/stats", json!({}), DEADLINE)
        .await
        .unwrap()
}

fn assert_event_identity(events: &[RuntimeEvent], thread: &str, turn: &str) {
    for (index, event) in events.iter().enumerate() {
        assert_eq!(event.sequence, index as u64 + 1);
        assert_eq!(event.thread_id.as_deref(), Some(thread));
        if let Some(event_turn) = &event.turn_id {
            assert_eq!(event_turn, turn);
        }
    }
}

#[tokio::test]
async fn thread_turn_tool_events_and_durable_submission_form_one_execution() {
    let fixture = Fixture::new();
    let (mut store, claim) = fixture.seed();
    let client = fixture.start("normal").await;
    let thread = start_thread(&fixture, &client).await;
    let turn = start_turn(&client, &thread).await;
    let mut events = Vec::new();
    let request = approval(&client, &mut events, &thread, &turn).await;
    assert_eq!(request.payload["cwd"], json!(fixture.cwd()));
    assert!(!fixture.effect().exists());
    assert_eq!(stats(&client).await["effects"], 0);
    assert_eq!(
        client.next_event(Duration::from_millis(20)).await,
        Err(RuntimeError::EventTimeout)
    );

    accept(&client, &request).await;
    let terminal = until(&client, &mut events, "turn/completed").await;
    assert_eq!(
        terminal.terminal_status(),
        Some(TurnTerminalStatus::Completed)
    );
    assert_event_identity(&events, &thread, &turn);
    assert!(events
        .iter()
        .any(|event| event.kind == "item/agentMessage/delta"));
    assert!(events
        .iter()
        .any(|event| event.kind == "item/completed" && event.item_id == request.item_id));
    assert_eq!(
        std::fs::read_to_string(fixture.effect())
            .unwrap()
            .lines()
            .count(),
        1
    );
    assert_eq!(task(&store).status, TaskStatus::Running);
    assert!(task(&store).result.is_none());

    let candidate = json!({"thread_id": thread, "turn_id": turn, "artifact": fixture.effect()});
    let command = submit_command(&claim, candidate.clone());
    let receipt = store.execute("submit-result", command.clone()).unwrap();
    assert_eq!(task(&store).status, TaskStatus::Submitted);
    assert_eq!(task(&store).result, Some(candidate));
    assert_eq!(
        store.get_run(RUN).unwrap().unwrap().status,
        RunStatus::Pending
    );
    let durable_events = store.events_after(0, 100).unwrap();
    let outbox = store.pending_outbox(100).unwrap();
    drop(store);

    // A lost host response replays the SQLite receipt, never the model turn.
    let mut reopened = fixture.store();
    assert_eq!(reopened.execute("submit-result", command).unwrap(), receipt);
    assert_eq!(reopened.events_after(0, 100).unwrap(), durable_events);
    assert_eq!(reopened.pending_outbox(100).unwrap(), outbox);
    assert_eq!(stats(&client).await["turn_starts"], 1);
    assert_eq!(stats(&client).await["effects"], 1);
    client.close().await;
}

#[tokio::test]
async fn lost_turn_start_reply_requires_history_lookup_without_replay() {
    let fixture = Fixture::new();
    let (mut store, claim) = fixture.seed();
    let client = fixture.start("exit_before_turn_reply").await;
    let thread = start_thread(&fixture, &client).await;
    assert_eq!(
        client
            .request(
                "turn/start",
                json!({
                    "threadId": thread,
                    "input": [{"type": "text", "text": "Write once.", "text_elements": []}]
                }),
                DEADLINE
            )
            .await,
        Err(RuntimeError::Closed)
    );
    client.close().await;
    mark_unknown(&fixture, &mut store, &claim);
    drop(store);

    let recovered = fixture.start("normal").await;
    let remote = history(&recovered, &thread).await;
    assert_eq!(remote["id"], thread);
    assert_eq!(remote["cwd"], json!(fixture.cwd()));
    assert_eq!(remote["turns"].as_array().unwrap().len(), 1);
    assert_eq!(remote["turns"][0]["status"], "inProgress");
    assert!(!remote["turns"][0]["id"].as_str().unwrap().is_empty());
    assert!(!fixture.effect().exists());
    let counts = stats(&recovered).await;
    assert_eq!(counts["thread_starts"], 1);
    assert_eq!(counts["turn_starts"], 1);
    assert_eq!(counts["effects"], 0);
    assert_eq!(
        fixture.store().get_task(TASK).unwrap().unwrap().status,
        TaskStatus::OutcomeUnknown
    );
    assert_eq!(
        recovered.next_event(Duration::from_millis(20)).await,
        Err(RuntimeError::EventTimeout)
    );
    recovered.close().await;
}

#[tokio::test]
async fn tool_effect_survives_lost_completion_and_is_not_automatically_retried() {
    let fixture = Fixture::new();
    let (mut store, claim) = fixture.seed();
    let client = fixture.start("exit_after_effect").await;
    let thread = start_thread(&fixture, &client).await;
    let turn = start_turn(&client, &thread).await;
    let mut events = Vec::new();
    let request = approval(&client, &mut events, &thread, &turn).await;
    accept(&client, &request).await;
    assert_eq!(client.next_event(DEADLINE).await, Err(RuntimeError::Closed));
    assert!(events.iter().all(|event| event.terminal_status().is_none()));
    client.close().await;
    let effect = std::fs::read_to_string(fixture.effect()).unwrap();
    assert_eq!(effect.lines().count(), 1);
    mark_unknown(&fixture, &mut store, &claim);
    drop(store);

    let recovered = fixture.start("normal").await;
    let remote = history(&recovered, &thread).await;
    assert_eq!(remote["id"], thread);
    assert_eq!(remote["cwd"], json!(fixture.cwd()));
    assert_eq!(remote["turns"][0]["id"], turn);
    assert_eq!(remote["turns"][0]["status"], "completed");
    assert_eq!(remote["turns"].as_array().unwrap().len(), 1);
    assert_eq!(stats(&recovered).await["turn_starts"], 1);
    assert_eq!(stats(&recovered).await["effects"], 1);
    assert_eq!(std::fs::read_to_string(fixture.effect()).unwrap(), effect);
    assert_eq!(
        recovered
            .respond(
                request.request_id.as_ref().unwrap(),
                json!({"decision": "accept"}),
                DEADLINE
            )
            .await,
        Err(RuntimeError::UnknownServerRequest)
    );

    // Current recovery only authorizes a safe retry. Observing completed history
    // must not reopen the task or submit using the expired attempt's credentials.
    let mut reopened = fixture.store();
    assert_eq!(task(&reopened).status, TaskStatus::OutcomeUnknown);
    assert!(reopened
        .execute(
            "late-submit",
            submit_command(&claim, json!({"artifact": fixture.effect()}))
        )
        .is_err());
    assert!(reopened.get_receipt("late-submit").unwrap().is_none());
    assert!(task(&reopened).result.is_none());
    recovered.close().await;
}

#[tokio::test]
async fn interrupted_turn_resumes_same_run_only_after_explicit_reconciliation() {
    let fixture = Fixture::new();
    let (mut store, first_claim) = fixture.seed();
    let client = fixture.start("normal").await;
    let thread = start_thread(&fixture, &client).await;
    let first_turn = start_turn(&client, &thread).await;
    let mut events = Vec::new();
    let old_approval = approval(&client, &mut events, &thread, &first_turn).await;
    client
        .request(
            "turn/interrupt",
            json!({"threadId": thread, "turnId": first_turn}),
            DEADLINE,
        )
        .await
        .unwrap();
    let terminal = until(&client, &mut events, "turn/completed").await;
    assert_eq!(
        terminal.terminal_status(),
        Some(TurnTerminalStatus::Interrupted)
    );
    assert_eq!(task(&store).status, TaskStatus::Running);
    assert!(!fixture.effect().exists());
    client.close().await;
    mark_unknown(&fixture, &mut store, &first_claim);
    drop(store);

    let recovered = fixture.start("normal").await;
    let remote = history(&recovered, &thread).await;
    assert_eq!(remote["id"], thread);
    assert_eq!(remote["cwd"], json!(fixture.cwd()));
    assert_eq!(remote["turns"][0]["id"], first_turn);
    assert_eq!(remote["turns"][0]["status"], "interrupted");
    assert_eq!(stats(&recovered).await["effects"], 0);
    assert!(!fixture.effect().exists());
    let mut reopened = fixture.store();
    let unknown = task(&reopened);
    assert_eq!(unknown.status, TaskStatus::OutcomeUnknown);

    // This fixture's only effect is a synchronous local write. Matching an
    // interrupted turn, a reaped process, and no receipt permits this test's retry.
    let evidence_path = fixture.cwd().join("reconciliation.json");
    std::fs::write(
        &evidence_path,
        serde_json::to_vec(&json!({
            "run_id": RUN, "task_id": TASK, "thread_id": thread, "turn_id": first_turn,
            "cwd": fixture.cwd(), "status": "interrupted", "effects": 0
        }))
        .unwrap(),
    )
    .unwrap();
    reopened
        .execute(
            "reconcile",
            Command::ReconcileTask {
                task_id: TASK.into(),
                expected_version: unknown.version,
                evidence_ref: evidence_path.to_str().unwrap().into(),
            },
        )
        .unwrap();
    reopened
        .execute("claim-resumed", claim_command(task(&reopened).version))
        .unwrap();
    let second_claim = task(&reopened).claim.unwrap();
    assert_eq!(second_claim.generation, first_claim.generation + 1);
    assert_ne!(second_claim.token, first_claim.token);
    assert_eq!(task(&reopened).run_id, RUN);
    assert!(matches!(
        reopened.execute("stale-submit", submit_command(&first_claim, json!({}))),
        Err(StoreError::Domain(DomainError::StaleClaim))
    ));
    assert_eq!(
        recovered
            .respond(
                old_approval.request_id.as_ref().unwrap(),
                json!({"decision": "accept"}),
                DEADLINE
            )
            .await,
        Err(RuntimeError::UnknownServerRequest)
    );

    let resumed = recovered
        .request(
            "thread/resume",
            json!({"threadId": thread, "cwd": fixture.cwd()}),
            DEADLINE,
        )
        .await
        .unwrap();
    assert_eq!(resumed["thread"]["id"], thread);
    let second_turn = start_turn(&recovered, &thread).await;
    assert_ne!(first_turn, second_turn);
    let mut resumed_events = Vec::new();
    let fresh_approval = approval(&recovered, &mut resumed_events, &thread, &second_turn).await;
    assert_ne!(fresh_approval.request_id, old_approval.request_id);
    accept(&recovered, &fresh_approval).await;
    let completed = until(&recovered, &mut resumed_events, "turn/completed").await;
    assert_eq!(
        completed.terminal_status(),
        Some(TurnTerminalStatus::Completed)
    );
    assert_event_identity(&resumed_events, &thread, &second_turn);
    assert_eq!(task(&reopened).status, TaskStatus::Running);
    reopened
        .execute(
            "submit-resumed",
            submit_command(
                &second_claim,
                json!({
                    "thread_id": thread, "turn_id": second_turn, "artifact": fixture.effect()
                }),
            ),
        )
        .unwrap();
    assert_eq!(task(&reopened).status, TaskStatus::Submitted);
    let counts = stats(&recovered).await;
    assert_eq!(counts["thread_starts"], 1);
    assert_eq!(counts["turn_starts"], 2);
    assert_eq!(counts["effects"], 1);
    assert_eq!(counts["resumes"], 1);
    recovered.close().await;
}
