use forge_core::{ApprovalStatus, Command as StoreCommand, RunOwner, RunStatus, TaskStatus};
use forge_store::Store;
use serde_json::{json, Value};
use std::{
    fs,
    path::PathBuf,
    process::{Command, Output},
};
use tempfile::TempDir;

struct Fixture {
    root: TempDir,
    input: Value,
}

impl Fixture {
    fn new(mode: &str) -> Self {
        let root = TempDir::new().unwrap();
        let path = root.path().canonicalize().unwrap();
        fs::create_dir(path.join("workspace")).unwrap();
        let input = json!({
            "schema_version": 1, "run_id": "owned-run", "task_id": "owned-task", "worker_id": "worker",
            "config": {
                "project_id": "project", "owner_id": "actor",
                "members": [{"worker_id": "worker", "config_revision": "agent:v1:fixture"}],
                "permission_profile": "ask", "working_directory": path.join("workspace"),
                "runtime_home": path.join("runtime"), "runtime_version": "fixture-v1",
                "route_binding": {
                    "provider_id": "provider", "provider_revision_id": "revision-1", "model_id": "fixture-model",
                    "upstream_protocol": "openai_responses", "gateway_route_id": "fixture-route-1",
                    "responses_base_url": "http://127.0.0.1:12345/v1", "config_digest": "computed-by-rust",
                    "credential_ref": "env:FORGE_RUN_API_KEY", "capability_evidence_ref": "fixture:tool-contract",
                    "gateway_version": "fixture-v1"
                }
            },
            "provider_snapshot": {
                "provider_id": "provider",
                "provider_revision_id": "revision-1",
                "upstream_base_url": "http://127.0.0.1:12345/v1",
                "credential_ref": "env:FORGE_RUN_API_KEY",
                "config_digest": "pending-rust-freeze",
                "models": [{
                    "model_id": "fixture-model",
                    "upstream_protocol": "openai_responses",
                    "capability_evidence_ref": "fixture:tool-contract"
                }]
            },
            "runtime_executable": PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../forge-runtime/tests/fixtures/execution_app_server.py").canonicalize().unwrap(),
            "prompt": "Write fixture-result.txt exactly once.", "fixture_mode": mode,
            "acceptance": {"path": "fixture-result.txt", "expected_content": "fixture-turn-1\n"},
            "approved_command": "printf 'fixture-turn-1\\n' >> fixture-result.txt"
        });
        Self { root, input }
    }

    fn db(&self) -> PathBuf {
        self.root.path().join("kernel.sqlite3")
    }
    fn effect(&self) -> PathBuf {
        self.root.path().join("workspace/fixture-result.txt")
    }
    fn store(&self) -> Store {
        Store::open(self.db()).unwrap()
    }
    fn execute(&self) -> Output {
        let input = self.root.path().join("input.json");
        fs::write(&input, serde_json::to_vec(&self.input).unwrap()).unwrap();
        Command::new(env!("CARGO_BIN_EXE_forge-run"))
            .args(["execute", "--db"])
            .arg(self.db())
            .arg("--input")
            .arg(input)
            .env("FORGE_RUN_API_KEY", "fixture-secret-never-persist")
            .output()
            .unwrap()
    }
    fn accept(&self) -> Output {
        Command::new(env!("CARGO_BIN_EXE_forge-run"))
            .args(["accept", "--db"])
            .arg(self.db())
            .args(["--run", "owned-run"])
            .output()
            .unwrap()
    }
}

fn assert_success(output: Output) {
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

#[test]
fn frozen_route_runtime_events_submission_and_separate_acceptance_survive_processes() {
    let mut fixture = Fixture::new("normal");
    assert_success(fixture.execute());
    let store = fixture.store();
    let submitted = store.get_run("owned-run").unwrap().unwrap();
    assert_eq!(submitted.owner, RunOwner::Rust);
    assert_eq!(submitted.status, RunStatus::Submitted);
    assert_eq!(
        store.get_task("owned-task").unwrap().unwrap().status,
        TaskStatus::Submitted
    );
    let route = store.get_route_binding("owned-run").unwrap().unwrap();
    assert_eq!(route.config_digest.len(), 64);
    let events = store.runtime_events_after("owned-run", 0, 100).unwrap();
    assert!(events
        .iter()
        .any(|event| event.payload["kind"] == "item/commandExecution/requestApproval"));
    assert!(events
        .iter()
        .any(|event| event.payload["terminal_status"] == "completed"));
    let all_events = store.events_after(0, 100).unwrap();
    assert_eq!(all_events.len(), store.pending_outbox(100).unwrap().len());
    let public = serde_json::to_string(&all_events).unwrap();
    assert!(!public.contains("fixture-secret-never-persist"));
    assert!(!public.contains("printf"));
    assert!(!public.contains("fixture-result.txt"));
    assert_eq!(
        all_events
            .iter()
            .filter(|event| event.kind == "run_submitted")
            .count(),
        1
    );
    drop(store);

    fixture.input["config"]["route_binding"]["model_id"] = json!("replaced-model");
    assert!(!fixture.execute().status.success());
    assert_eq!(
        fs::read_to_string(fixture.effect()).unwrap(),
        "fixture-turn-1\n"
    );
    assert_eq!(
        fixture
            .store()
            .get_route_binding("owned-run")
            .unwrap()
            .unwrap(),
        route
    );
    assert_success(fixture.accept());
    let count = fixture.store().events_after(0, 100).unwrap().len();
    assert_success(fixture.accept());
    assert_eq!(fixture.store().events_after(0, 100).unwrap().len(), count);
    assert_eq!(
        fixture
            .store()
            .get_run("owned-run")
            .unwrap()
            .unwrap()
            .status,
        RunStatus::Accepted
    );
}

#[test]
fn completed_turn_cannot_pass_a_wrong_acceptance_contract_or_modified_artifact() {
    let mut fixture = Fixture::new("normal");
    fixture.input["acceptance"]["expected_content"] = json!("different\n");
    assert_success(fixture.execute());
    assert!(!fixture.accept().status.success());
    assert_eq!(
        fixture
            .store()
            .get_run("owned-run")
            .unwrap()
            .unwrap()
            .status,
        RunStatus::Submitted
    );
    fs::write(fixture.effect(), "different\n").unwrap();
    assert!(!fixture.accept().status.success());
    assert_eq!(
        fixture
            .store()
            .get_run("owned-run")
            .unwrap()
            .unwrap()
            .status,
        RunStatus::Submitted
    );
}

#[test]
fn uncertain_external_effect_is_never_automatically_replayed() {
    for mode in ["exit_before_turn_reply", "exit_after_effect"] {
        let fixture = Fixture::new(mode);
        assert!(!fixture.execute().status.success());
        let run = fixture.store().get_run("owned-run").unwrap().unwrap();
        assert_eq!(run.status, RunStatus::OutcomeUnknown);
        assert!(run.result.is_none());
        assert!(!fixture.accept().status.success());
        assert!(!fixture.execute().status.success());
        if mode == "exit_after_effect" {
            assert_eq!(
                fs::read_to_string(fixture.effect()).unwrap(),
                "fixture-turn-1\n"
            );
        } else {
            assert!(!fixture.effect().exists());
        }
        let history: Value = serde_json::from_slice(
            &fs::read(fixture.root.path().join("runtime/fixture-history.json")).unwrap(),
        )
        .unwrap();
        assert_eq!(history["turn_starts"], 1);
    }
}

#[test]
fn unapproved_command_and_unsafe_mapping_fail_closed() {
    let mut fixture = Fixture::new("normal");
    fixture.input["approved_command"] = Value::Null;
    assert!(!fixture.execute().status.success());
    assert!(!fixture.effect().exists());
    assert_eq!(
        fixture
            .store()
            .get_run("owned-run")
            .unwrap()
            .unwrap()
            .status,
        RunStatus::OutcomeUnknown
    );
    let mut unsafe_input = Fixture::new("normal");
    unsafe_input.input["config"]["route_binding"]["responses_base_url"] =
        json!("https://example.com/v1");
    assert!(!unsafe_input.execute().status.success());
    assert!(unsafe_input.store().get_run("owned-run").unwrap().is_none());
    assert!(!unsafe_input.root.path().join("runtime").exists());
}

#[test]
fn external_decision_is_persisted_before_one_live_callback_dispatch() {
    for approved in [true, false] {
        let mut fixture = Fixture::new("normal");
        fixture.input["approved_command"] = Value::Null;
        fixture.input["external_approvals"] = json!(true);
        let db = fixture.db();
        let effect = fixture.effect();
        // Keep the fixture alive in the execution thread until the child exits.
        let runner = std::thread::spawn(move || (fixture.execute(), fixture));
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(8);
        let approval = loop {
            if db.exists() {
                if let Ok(store) = Store::open(&db) {
                    if let Some(approval) = store.list_approvals("owned-run", 10).unwrap().first() {
                        break approval.clone();
                    }
                }
            }
            assert!(
                std::time::Instant::now() < deadline,
                "approval never became durable"
            );
            std::thread::sleep(std::time::Duration::from_millis(20));
        };
        assert_eq!(approval.status, ApprovalStatus::Pending);
        assert!(!effect.exists());
        let mut store = Store::open(&db).unwrap();
        let decision = StoreCommand::DecideApproval {
            approval_id: approval.id.clone(),
            expected_version: approval.version,
            digest: approval.digest.clone(),
            approved,
            actor_id: "authenticated-user".into(),
            comment: "Reviewed through the product host".into(),
        };
        store.execute("http-decision", decision.clone()).unwrap();
        let (output, fixture) = runner.join().unwrap();
        assert_eq!(
            output.status.success(),
            approved,
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        assert_eq!(effect.exists(), approved);
        assert_eq!(
            store.get_approval(&approval.id).unwrap().unwrap().status,
            ApprovalStatus::Delivered
        );
        // Historical command receipts must not result in another transport write.
        store.execute("http-decision", decision).unwrap();
        let history: Value = serde_json::from_slice(
            &fs::read(fixture.root.path().join("runtime/fixture-history.json")).unwrap(),
        )
        .unwrap();
        assert_eq!(history["effects"], if approved { 1 } else { 0 });
    }
}

#[test]
fn provider_revision_digest_is_stable_across_independent_routes() {
    use forge_cli::owned_run::RunInput;
    let fixture = Fixture::new("normal");
    let mut first: RunInput = serde_json::from_value(fixture.input.clone()).unwrap();
    let mut second = first.clone();
    second.config.route_binding.gateway_route_id = "another-route".into();
    second.config.route_binding.responses_base_url = "http://127.0.0.1:23456/v1".into();
    first.freeze().unwrap();
    second.freeze().unwrap();
    assert_eq!(first.provider_snapshot, second.provider_snapshot);
    assert_eq!(
        first.config.route_binding.config_digest,
        second.config.route_binding.config_digest
    );
    first.freeze().unwrap();
    assert_eq!(first.provider_snapshot, second.provider_snapshot);
}
