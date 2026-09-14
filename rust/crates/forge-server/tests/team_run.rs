use forge_server::{router_with_product_store, ProductStore, RustOwnedBackend, ServerConfig};
use reqwest::{Client, StatusCode};
use serde_json::{json, Value};
use std::{
    collections::BTreeSet,
    fs,
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};
use tokio::net::TcpListener;
use uuid::Uuid;

#[test]
fn coordinator_next_turn_requires_durable_completion_of_the_current_turn() {
    use forge_core::{Command, CommandResult, DomainError, RunConfig, RunError, RunTransition};
    use forge_store::{Store, StoreError};

    let temp = Temp::new();
    let path = temp.0.join("turn-evidence.sqlite");
    let config: RunConfig = serde_json::from_value(json!({
        "project_id":"project", "owner_id":"owner", "permission_profile":"auto",
        "working_directory":temp.0.join("workspace"), "runtime_home":temp.0.join("runtime"),
        "runtime_version":"fixture", "members":[{"worker_id":"leader","config_revision":"v1","is_coordinator":true}],
        "route_binding":{
            "provider_id":"provider", "provider_revision_id":"revision", "model_id":"model",
            "upstream_protocol":"openai_responses", "gateway_route_id":"route",
            "responses_base_url":"http://127.0.0.1:9000/v1", "config_digest":"digest",
            "credential_ref":"credential", "capability_evidence_ref":"evidence", "gateway_version":"fixture"
        }
    })).unwrap();
    let mut store = Store::open(&path).unwrap();
    store
        .execute(
            "create",
            Command::CreateBoundRun {
                run_id: "run".into(),
                config: Box::new(config),
            },
        )
        .unwrap();
    let transition = |expected_version, transition| Command::TransitionRun {
        run_id: "run".into(),
        expected_version,
        transition,
    };
    let begin = store
        .execute(
            "begin",
            transition(
                0,
                RunTransition::Begin {
                    worker_id: "leader".into(),
                    connection_id: "connection".into(),
                },
            ),
        )
        .unwrap();
    let CommandResult::Run(started) = begin.result else {
        panic!("run receipt required")
    };
    let connection = started.execution.unwrap().connection;
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
            "turn-one",
            transition(
                2,
                RunTransition::BindTurn {
                    connection: connection.clone(),
                    turn_id: "turn-one".into(),
                },
            ),
        )
        .unwrap();
    let next = transition(
        3,
        RunTransition::BindTurn {
            connection: connection.clone(),
            turn_id: "turn-two".into(),
        },
    );
    assert!(matches!(
        store.execute("premature-next", next.clone()),
        Err(StoreError::Domain(DomainError::Run(
            RunError::StaleExecution
        )))
    ));
    store
        .execute(
            "item-done",
            Command::RecordRuntimeEvent {
                run_id: "run".into(),
                connection: connection.clone(),
                sequence: 1,
                thread_id: Some("thread".into()),
                turn_id: Some("turn-one".into()),
                item_id: Some("item".into()),
                kind: "item/completed".into(),
                terminal_status: None,
            },
        )
        .unwrap();
    assert!(matches!(
        store.execute("item-is-not-turn-completion", next.clone()),
        Err(StoreError::Domain(DomainError::Run(
            RunError::StaleExecution
        )))
    ));
    store
        .execute(
            "turn-done",
            Command::RecordRuntimeEvent {
                run_id: "run".into(),
                connection: connection.clone(),
                sequence: 2,
                thread_id: Some("thread".into()),
                turn_id: Some("turn-one".into()),
                item_id: None,
                kind: "turn/completed".into(),
                terminal_status: Some("completed".into()),
            },
        )
        .unwrap();
    drop(store);
    let mut reopened = Store::open(&path).unwrap();
    reopened
        .execute("next-after-durable-completion", next)
        .unwrap();
    let current = reopened.get_run("run").unwrap().unwrap();
    assert_eq!(
        current.execution.unwrap().turn_id.as_deref(),
        Some("turn-two")
    );
    assert!(matches!(
        reopened.execute(
            "old-completion-cannot-unlock-next",
            transition(
                4,
                RunTransition::BindTurn {
                    connection,
                    turn_id: "turn-three".into(),
                }
            )
        ),
        Err(StoreError::Domain(DomainError::Run(
            RunError::StaleExecution
        )))
    ));
}

struct Temp(PathBuf);
impl Temp {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!("forge-team-http-{}", Uuid::new_v4()));
        fs::create_dir_all(&path).unwrap();
        Self(path)
    }
}
impl Drop for Temp {
    fn drop(&mut self) {
        if std::thread::panicking() {
            eprintln!("Team fixture evidence retained at {}", self.0.display());
        } else {
            let _ = fs::remove_dir_all(&self.0);
        }
    }
}

async fn post(client: &Client, base: &str, cookie: &str, path: &str, body: Value) -> Value {
    let response = client
        .post(format!("{base}/api{path}"))
        .header("x-forge-request", "1")
        .header("cookie", cookie)
        .json(&body)
        .send()
        .await
        .unwrap();
    let status = response.status();
    let value: Value = response.json().await.unwrap();
    assert!(status.is_success(), "{path}: {status} {value}");
    value
}
async fn get(client: &Client, base: &str, cookie: &str, path: &str) -> Value {
    let response = client
        .get(format!("{base}/api{path}"))
        .header("cookie", cookie)
        .send()
        .await
        .unwrap();
    let status = response.status();
    let value: Value = response.json().await.unwrap();
    assert!(status.is_success(), "{path}: {status} {value}");
    value
}

async fn wait_submitted(client: &Client, base: &str, cookie: &str, id: &str) -> Value {
    for _ in 0..300 {
        let run = get(client, base, cookie, &format!("/runs/{id}")).await;
        if run["kernel_status"] == "submitted" || run["status"] == "awaiting_acceptance" {
            return run;
        }
        assert_ne!(run["status"], "needs_attention", "team run failed: {run}");
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    let final_run = get(client, base, cookie, &format!("/runs/{id}")).await;
    panic!("team run did not reach submitted: {final_run}");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 6)]
async fn rust_team_run_uses_real_member_contexts_tools_messages_results_and_skills() {
    let temp = Temp::new();
    let product = Arc::new(
        ProductStore::open(temp.0.join("product.sqlite"), None, Some("team-password"))
            .unwrap()
            .with_encryption_key("YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE=")
            .unwrap(),
    );
    let fixture = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures/team_app_server.py")
        .canonicalize()
        .unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mut mode = fs::metadata(&fixture).unwrap().permissions();
        mode.set_mode(0o755);
        fs::set_permissions(&fixture, mode).unwrap();
    }
    let backend = Arc::new(
        RustOwnedBackend::from_product(
            product.clone(),
            temp.0.join("runs.sqlite"),
            temp.0.join("data"),
            Some(fixture),
        )
        .unwrap(),
    );
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let app = router_with_product_store(backend, ServerConfig::default(), product.clone());
    let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    let client = Client::builder()
        .timeout(Duration::from_secs(10))
        .build()
        .unwrap();
    let login = client
        .post(format!("{base}/api/auth/login"))
        .header("x-forge-request", "1")
        .json(&json!({"username":"admin","password":"team-password"}))
        .send()
        .await
        .unwrap();
    assert_eq!(login.status(), StatusCode::OK);
    let cookie = login.headers()["set-cookie"]
        .to_str()
        .unwrap()
        .split(';')
        .next()
        .unwrap()
        .to_owned();

    let provider = post(&client, &base, &cookie, "/providers", json!({"name":"Team fixture provider","protocol":"openai_responses","base_url":"http://127.0.0.1:9999/v1","api_key":"team-fixture-key","external":false})).await;
    let model = post(&client, &base, &cookie, "/models", json!({"name":"Team fixture model","provider_id":provider["id"],"model_id":"team-fixture","parameters":{},"allowed_parameters":[],"supports_tools":true})).await;
    let skill = post(&client, &base, &cookie, "/skills", json!({"files":{"SKILL.md":"---\nname: team-evidence\ndescription: Team evidence fixture\n---\nAlways record concrete evidence. FROZEN_TEAM_EVIDENCE\n","references/evidence.md":"FROZEN_TEAM_EVIDENCE: verify the reviewed revision.\n","scripts/forbidden.sh":"echo must-not-run\n"}})).await;
    post(
        &client,
        &base,
        &cookie,
        &format!("/skills/{}/review", skill["id"].as_str().unwrap()),
        json!({"version":1,"digest":skill["digest"],"action":"approve"}),
    )
    .await;
    let binding = json!([{"skill_id":skill["id"],"version":1,"mode":"always","allow_scripts":false,"parameters":{}}]);
    let coordinator = post(&client, &base, &cookie, "/agents", json!({"name":"Coordinator","role":"planner","model_profile_id":model["id"],"instructions":"Coordinate the team and review evidence.","skills":binding})).await;
    let developer = post(&client, &base, &cookie, "/agents", json!({"name":"Developer","role":"developer","model_profile_id":model["id"],"instructions":"Implement the assigned evidence task.","skills":binding})).await;
    let tester = post(&client, &base, &cookie, "/agents", json!({"name":"Tester","role":"tester","model_profile_id":model["id"],"instructions":"Verify the assigned evidence task.","skills":binding})).await;
    let project = post(&client, &base, &cookie, "/projects", json!({"name":"Team run project","blank":true,"execution_mode":"local","permission_profile":"ask","runtime":{"task_timeout_s":40,"call_timeout_s":10,"max_model_calls":30}})).await;
    // Simulate the same human-applied office layout selected by the desktop
    // composer. The run must freeze this scope rather than reject it as an
    // unsupported backend operation.
    let layout_ref = json!({"id":"layout-team-run","version":1,"digest":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"});
    let c_id = coordinator["id"].as_str().unwrap();
    let d_id = developer["id"].as_str().unwrap();
    let t_id = tester["id"].as_str().unwrap();
    let positions = json!({
        c_id: {"x":-4,"z":0,"floor":1,"zone":"dev-zone"},
        d_id: {"x":0,"z":0,"floor":1,"zone":"dev-zone"},
        t_id: {"x":4,"z":0,"floor":1,"zone":"dev-zone"}
    });
    let layout = json!({"ref":layout_ref,"config":{"layout_version":1,"floors":[{"floor":1,"name":"研发楼层","department":"研发部","zones":[{"id":"dev-zone","name":"开发区","kind":"work","x":0,"z":0,"width":12,"depth":8}]}],"teams":[{"id":"team-rust-run","name":"Rust 验证团队","floor":1,"zone_ids":["dev-zone"],"headcount":3,"member_ids":[c_id,d_id,t_id]}],"desks":[{"id":"desk-c","name":"协调桌","floor":1,"zone":"dev-zone","x":-4,"z":0,"team_id":"team-rust-run","agent_id":c_id},{"id":"desk-d","name":"开发桌","floor":1,"zone":"dev-zone","x":0,"z":0,"team_id":"team-rust-run","agent_id":d_id},{"id":"desk-t","name":"测试桌","floor":1,"zone":"dev-zone","x":4,"z":0,"team_id":"team-rust-run","agent_id":t_id}],"positions":positions}});
    product.connect().unwrap().execute(
        "INSERT INTO platform_config(id,value) VALUES('organization_layout',?1) ON CONFLICT(id) DO UPDATE SET value=excluded.value",
        rusqlite::params![layout.to_string()],
    ).unwrap();
    let input = json!({"project_id":project["id"],"title":"Real Rust team run","request":"Coordinate two bounded tasks, exchange one peer message, and review both results.","agent_ids":[coordinator["id"],developer["id"],tester["id"]],"entry_agent_id":coordinator["id"],"office_team_id":"team-rust-run","office_layout_ref":layout_ref});
    let preflight = client
        .post(format!("{base}/api/execution/preflight"))
        .header("x-forge-request", "1")
        .header("cookie", &cookie)
        .json(&input)
        .send()
        .await
        .unwrap();
    let preflight_status = preflight.status();
    let preflight_body: Value = preflight.json().await.unwrap();
    assert_eq!(
        preflight_status,
        StatusCode::OK,
        "preflight: {preflight_body}"
    );
    assert!(
        preflight_body["ready"].as_bool().unwrap_or(false),
        "preflight not ready: {preflight_body}"
    );
    let mut stale = input.clone();
    stale["office_layout_ref"]["version"] = json!(2);
    let stale_response = client
        .post(format!("{base}/api/runs/preview-config"))
        .header("x-forge-request", "1")
        .header("cookie", &cookie)
        .json(&stale)
        .send()
        .await
        .unwrap();
    let stale_status = stale_response.status();
    let stale_body: Value = stale_response.json().await.unwrap();
    assert_eq!(
        stale_status,
        StatusCode::CONFLICT,
        "stale layout should be rejected: {stale_body}"
    );
    assert!(
        stale_body["detail"]
            .as_str()
            .unwrap_or("")
            .contains("布局已更新"),
        "stale error should explain refresh: {stale_body}"
    );
    let mut single = input.clone();
    single["agent_ids"] = json!([c_id]);
    single["entry_agent_id"] = json!(c_id);
    let single_preview = client
        .post(format!("{base}/api/runs/preview-config"))
        .header("x-forge-request", "1")
        .header("cookie", &cookie)
        .json(&single)
        .send()
        .await
        .unwrap();
    let single_status = single_preview.status();
    let single_body: Value = single_preview.json().await.unwrap();
    assert_eq!(
        single_status,
        StatusCode::OK,
        "single member office preview: {single_body}"
    );
    let mut missing_desk_layout = layout.clone();
    missing_desk_layout["config"]["desks"] = json!([
        {"id":"desk-c","name":"协调桌","floor":1,"zone":"dev-zone","x":-4,"z":0,"team_id":"team-rust-run","agent_id":c_id},
        {"id":"desk-t","name":"测试桌","floor":1,"zone":"dev-zone","x":4,"z":0,"team_id":"team-rust-run","agent_id":t_id}
    ]);
    product
        .connect()
        .unwrap()
        .execute(
            "UPDATE platform_config SET value=?1 WHERE id='organization_layout'",
            rusqlite::params![missing_desk_layout.to_string()],
        )
        .unwrap();
    let missing_response = client
        .post(format!("{base}/api/runs/preview-config"))
        .header("x-forge-request", "1")
        .header("cookie", &cookie)
        .json(&input)
        .send()
        .await
        .unwrap();
    let missing_status = missing_response.status();
    let missing_body: Value = missing_response.json().await.unwrap();
    assert_eq!(
        missing_status,
        StatusCode::CONFLICT,
        "missing desk should be rejected: {missing_body}"
    );
    assert!(
        missing_body["detail"]
            .as_str()
            .unwrap_or("")
            .contains("工位"),
        "missing desk error should explain seating: {missing_body}"
    );
    product
        .connect()
        .unwrap()
        .execute(
            "UPDATE platform_config SET value=?1 WHERE id='organization_layout'",
            rusqlite::params![layout.to_string()],
        )
        .unwrap();
    let mut duplicate_desk_layout = layout.clone();
    duplicate_desk_layout["config"]["desks"].as_array_mut().unwrap().push(json!({"id":"desk-d-duplicate","name":"开发备用桌","floor":1,"zone":"dev-zone","x":0,"z":3,"team_id":"team-rust-run","agent_id":d_id}));
    product
        .connect()
        .unwrap()
        .execute(
            "UPDATE platform_config SET value=?1 WHERE id='organization_layout'",
            rusqlite::params![duplicate_desk_layout.to_string()],
        )
        .unwrap();
    let duplicate_response = client
        .post(format!("{base}/api/runs/preview-config"))
        .header("x-forge-request", "1")
        .header("cookie", &cookie)
        .json(&input)
        .send()
        .await
        .unwrap();
    let duplicate_status = duplicate_response.status();
    let duplicate_body: Value = duplicate_response.json().await.unwrap();
    assert_eq!(
        duplicate_status,
        StatusCode::CONFLICT,
        "duplicate desk should be rejected: {duplicate_body}"
    );
    assert!(
        duplicate_body["detail"]
            .as_str()
            .unwrap_or("")
            .contains("工位"),
        "duplicate desk error should explain seating: {duplicate_body}"
    );
    product
        .connect()
        .unwrap()
        .execute(
            "UPDATE platform_config SET value=?1 WHERE id='organization_layout'",
            rusqlite::params![layout.to_string()],
        )
        .unwrap();
    let mut narrow_layout = layout.clone();
    narrow_layout["config"]["teams"][0]["member_ids"] = json!([c_id]);
    narrow_layout["config"]["teams"][0]["headcount"] = json!(1);
    narrow_layout["config"]["desks"] = json!([{"id":"desk-c","name":"协调桌","floor":1,"zone":"dev-zone","x":-4,"z":0,"team_id":"team-rust-run","agent_id":c_id}]);
    narrow_layout["config"]["positions"] = json!({c_id:{"x":-4,"z":0,"floor":1,"zone":"dev-zone"}});
    product
        .connect()
        .unwrap()
        .execute(
            "UPDATE platform_config SET value=?1 WHERE id='organization_layout'",
            rusqlite::params![narrow_layout.to_string()],
        )
        .unwrap();
    let cross_response = client
        .post(format!("{base}/api/runs/preview-config"))
        .header("x-forge-request", "1")
        .header("cookie", &cookie)
        .json(&input)
        .send()
        .await
        .unwrap();
    let cross_status = cross_response.status();
    let cross_body: Value = cross_response.json().await.unwrap();
    assert_eq!(
        cross_status,
        StatusCode::CONFLICT,
        "cross-team members should be rejected: {cross_body}"
    );
    assert!(
        cross_body["detail"]
            .as_str()
            .unwrap_or("")
            .contains("不完全属于"),
        "cross-team error should identify membership: {cross_body}"
    );
    product
        .connect()
        .unwrap()
        .execute(
            "UPDATE platform_config SET value=?1 WHERE id='organization_layout'",
            rusqlite::params![layout.to_string()],
        )
        .unwrap();
    let create_response = client
        .post(format!("{base}/api/runs"))
        .header("x-forge-request", "1")
        .header("cookie", &cookie)
        .header("idempotency-key", "team-run-once")
        .json(&input)
        .send()
        .await
        .unwrap();
    assert_eq!(create_response.status(), StatusCode::ACCEPTED);
    let created: Value = create_response.json().await.unwrap();
    let id = created["id"].as_str().unwrap().to_owned();
    let duplicate_response = client
        .post(format!("{base}/api/runs"))
        .header("x-forge-request", "1")
        .header("cookie", &cookie)
        .header("idempotency-key", "team-run-once")
        .json(&input)
        .send()
        .await
        .unwrap();
    assert_eq!(duplicate_response.status(), StatusCode::ACCEPTED);
    let duplicate: Value = duplicate_response.json().await.unwrap();
    assert_eq!(duplicate["id"], id);
    let run = wait_submitted(&client, &base, &cookie, &id).await;
    let detail = get(&client, &base, &cookie, &format!("/runs/{id}")).await;
    assert_eq!(
        detail["snapshot"]["design"]["organization_team"]["id"],
        "team-rust-run"
    );
    assert_eq!(
        detail["snapshot"]["design"]["organization_team"]["layout_ref"],
        layout_ref
    );
    let agents = detail["snapshot"]["agents"].as_object().unwrap();
    assert_eq!(agents.len(), 3);
    let mut processes = BTreeSet::new();
    let mut thread_ids = BTreeSet::new();
    let mut observed_turns = 0;
    let mut member_intervals = Vec::new();
    let mut workspaces = BTreeSet::new();
    let mut live_messages = 0;
    for agent in agents.values() {
        assert_eq!(agent["skills"][0]["name"], "team-evidence");
        let worker_id = agent["id"].as_str().unwrap();
        let skill_path = temp
            .0
            .join("data/runtime")
            .join(&id)
            .join(worker_id)
            .join("skills/team-evidence/SKILL.md");
        assert!(
            skill_path.is_file(),
            "missing materialized frozen skill at {}",
            skill_path.display()
        );
        assert!(fs::read_to_string(skill_path)
            .unwrap()
            .contains("Always record concrete evidence"));
        let history_path = temp
            .0
            .join("data/runtime")
            .join(&id)
            .join(worker_id)
            .join("team-fixture.json");
        let history: Value = serde_json::from_slice(&fs::read(history_path).unwrap()).unwrap();
        assert_eq!(history["errors"], json!([]));
        assert_eq!(history["skill_reads"].as_array().unwrap().len(), 2);
        assert!(history["skill_reads"]
            .as_array()
            .unwrap()
            .iter()
            .all(|read| read["content"]
                .as_str()
                .unwrap()
                .contains("FROZEN_TEAM_EVIDENCE")));
        assert!(history["responses"]
            .as_array()
            .unwrap()
            .iter()
            .all(|response| response["success"] == true));
        processes.insert(history["pid"].as_u64().unwrap());
        workspaces.insert(history["cwd"].as_str().unwrap().to_owned());
        thread_ids.insert(history["thread"]["id"].as_str().unwrap().to_owned());
        observed_turns += history["thread"]["turns"].as_array().unwrap().len();
        if history["coordinator"] == true {
            assert_eq!(history["thread"]["turns"].as_array().unwrap().len(), 2);
            assert_eq!(
                history["calls"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .filter(|call| call["tool"] == "assign_work")
                    .count(),
                4
            );
        } else {
            member_intervals.push((
                history["started_ns"].as_u64().unwrap(),
                history["finished_ns"].as_u64().unwrap(),
            ));
            live_messages += history["live_messages"].as_u64().unwrap_or_default();
            assert!(!history["dynamic_tools"]
                .as_array()
                .unwrap()
                .iter()
                .any(|tool| tool == "assign_work"));
        }
    }
    assert_eq!(processes.len(), 3);
    assert_eq!(thread_ids.len(), 3);
    assert_eq!(observed_turns, 4);
    assert_eq!(
        workspaces.len(),
        3,
        "each member needs an independent working copy"
    );
    assert_eq!(member_intervals.len(), 2);
    assert!(
        member_intervals.iter().map(|v| v.0).max().unwrap()
            < member_intervals.iter().map(|v| v.1).min().unwrap(),
        "member turns must overlap: {member_intervals:?}"
    );
    assert!(
        live_messages >= 1,
        "peer evidence must be delivered into an already running turn"
    );
    for role in ["developer", "tester"] {
        let integrated = temp
            .0
            .join("data/runs")
            .join(&id)
            .join("workspace")
            .join(format!("{role}.txt"));
        assert_eq!(
            fs::read_to_string(integrated).unwrap(),
            format!("FROZEN_TEAM_EVIDENCE: {role}\n")
        );
    }
    let tasks = detail["tasks"].as_array().unwrap();
    assert!(tasks
        .iter()
        .filter(|task| task["key"] != "codex")
        .all(|task| task["result"]["integration"]["status"] == "integrated"));
    let patch = fs::read_to_string(
        temp.0
            .join("data/runs")
            .join(&id)
            .join("artifacts/changes.patch"),
    )
    .unwrap();
    assert!(patch.contains("developer.txt") && patch.contains("tester.txt"));
    assert!(
        tasks
            .iter()
            .filter(|task| task["key"].as_str() != Some("codex"))
            .count()
            >= 2
    );
    assert!(tasks.iter().any(|task| task["result"]["summary"]
        .as_str()
        .is_some_and(|s| s.contains("completed"))));
    let connection_count: i64 = product
        .connect()
        .unwrap()
        .query_row(
            "SELECT count(DISTINCT connection_id) FROM forge_runtime_messages WHERE run_id=?1",
            [&id],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(connection_count, 3);
    let thread_count: i64 = product.connect().unwrap().query_row("SELECT count(DISTINCT json_extract(payload,'$.threadId')) FROM forge_runtime_messages WHERE run_id=?1 AND kind='turn/started'", [&id], |row| row.get(0)).unwrap();
    assert_eq!(thread_count, 3);
    let messages: i64 = product
        .connect()
        .unwrap()
        .query_row(
            "SELECT count(*) FROM agent_messages WHERE run_id=?1",
            [&id],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(messages, 2);
    let delivered: i64 = product
        .connect()
        .unwrap()
        .query_row(
            "SELECT count(*) FROM agent_messages WHERE run_id=?1 AND delivered_at<>''",
            [&id],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(delivered, 2);
    let assignment_events: i64 = product
        .connect()
        .unwrap()
        .query_row(
            "SELECT count(*) FROM events WHERE run_id=?1 AND kind='team.work.queued'",
            [&id],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(assignment_events, 3);
    let sessions = get(&client, &base, &cookie, &format!("/runs/{id}/sessions")).await;
    assert_eq!(sessions["total"], 3);
    for task in tasks {
        let task_key = task["key"].as_str().unwrap();
        let session = sessions["items"]
            .as_array()
            .unwrap()
            .iter()
            .find(|s| s["key"] == task_key)
            .unwrap();
        assert_eq!(session["agent_id"], task["agent_id"]);
        assert_eq!(session["status"], task["status"]);
        assert_eq!(session["result"], task["result"]);
        assert!(task["spec"].get("claim_state").is_none());
        assert!(task["spec"].get("runtime_sessions").is_none());
        let page = get(
            &client,
            &base,
            &cookie,
            &format!("/runs/{id}/sessions/{task_key}"),
        )
        .await;
        assert_eq!(page["agent_id"], task["agent_id"]);
        assert_eq!(page["result"], task["result"]);
        assert!(!page.to_string().contains("claim_token"));
    }
    let member_sessions = get(
        &client,
        &base,
        &cookie,
        &format!(
            "/runs/{id}/sessions?agent_id={}",
            developer["id"].as_str().unwrap()
        ),
    )
    .await;
    assert_eq!(member_sessions["total"], 1);
    assert_eq!(member_sessions["items"][0]["agent_id"], developer["id"]);
    let directory = get(&client, &base, &cookie, &format!("/runs/{id}/team")).await;
    assert_eq!(directory["members"].as_array().unwrap().len(), 3);
    assert!(directory.get("project").is_none());
    assert!(!directory.to_string().contains("team-fixture-key"));
    assert!(!detail.to_string().contains("team-fixture-key"));
    assert_eq!(run["kernel_status"], "submitted");

    let serial_project=post(&client,&base,&cookie,"/projects",json!({"name":"One slot team","blank":true,"execution_mode":"local","permission_profile":"ask","runtime":{"task_timeout_s":40,"call_timeout_s":10,"max_model_calls":30,"max_parallel":1}})).await;
    let mut serial_input = input.clone();
    serial_input["project_id"] = serial_project["id"].clone();
    serial_input["request"] =
        json!("TEAM_SERIAL: verify max_parallel limits member activations to one");
    let serial = post(&client, &base, &cookie, "/runs", serial_input).await;
    let serial_id = serial["id"].as_str().unwrap();
    wait_submitted(&client, &base, &cookie, serial_id).await;
    let mut intervals = Vec::new();
    for member in [&developer, &tester] {
        let history: Value = serde_json::from_slice(
            &fs::read(
                temp.0
                    .join("data/runtime")
                    .join(serial_id)
                    .join(member["id"].as_str().unwrap())
                    .join("team-fixture.json"),
            )
            .unwrap(),
        )
        .unwrap();
        assert_eq!(history["errors"], json!([]));
        intervals.push((
            history["started_ns"].as_u64().unwrap(),
            history["finished_ns"].as_u64().unwrap(),
        ));
    }
    intervals.sort();
    assert!(
        intervals[0].1 <= intervals[1].0,
        "max_parallel=1 must prevent overlapping activations: {intervals:?}"
    );

    let mut steer_input = input.clone();
    steer_input["request"] =
        json!("TEAM_STEER: both members wait for separately addressed human instructions");
    let steer_run = post(&client, &base, &cookie, "/runs", steer_input).await;
    let steer_id = steer_run["id"].as_str().unwrap();
    let history_path = |member: &Value| {
        temp.0
            .join("data/runtime")
            .join(steer_id)
            .join(member["id"].as_str().unwrap())
            .join("team-fixture.json")
    };
    let mut waiting = false;
    for _ in 0..100 {
        waiting = [&developer, &tester].iter().all(|member| {
            fs::read(history_path(member))
                .ok()
                .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
                .is_some_and(|history| history["waiting_host"] == true)
        });
        if waiting {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    assert!(
        waiting,
        "both running members must be ready for targeted steer"
    );
    let detail = get(&client, &base, &cookie, &format!("/runs/{steer_id}")).await;
    let tester_key = detail["tasks"]
        .as_array()
        .unwrap()
        .iter()
        .find(|task| task["agent_id"] == tester["id"])
        .unwrap()["key"]
        .clone();
    let mismatched=client.post(format!("{base}/api/runs/{steer_id}/control")).header("x-forge-request","1").header("cookie",&cookie).json(&json!({"action":"steer","agent_id":developer["id"],"task_key":tester_key,"prompt":"MUST_NOT_DELIVER"})).send().await.unwrap();
    assert_eq!(mismatched.status(), StatusCode::CONFLICT);
    for member in [&developer, &tester] {
        let instruction = format!("HOST_STEER:{}", member["id"].as_str().unwrap());
        let response = post(
            &client,
            &base,
            &cookie,
            &format!("/runs/{steer_id}/control"),
            json!({"action":"steer","agent_id":member["id"],"prompt":instruction}),
        )
        .await;
        assert_eq!(response["accepted"], true);
    }
    wait_submitted(&client, &base, &cookie, steer_id).await;
    for (member, other) in [(&developer, &tester), (&tester, &developer)] {
        let history = fs::read_to_string(history_path(member)).unwrap();
        assert!(history.contains(&format!("HOST_STEER:{}", member["id"].as_str().unwrap())));
        assert!(!history.contains(&format!("HOST_STEER:{}", other["id"].as_str().unwrap())));
        assert!(!history.contains("MUST_NOT_DELIVER"));
    }

    let mut parallel_cancel_input = input.clone();
    parallel_cancel_input["request"] =
        json!("CANCEL_MEMBERS: wait for two independent native approval callbacks");
    let parallel_cancel = post(&client, &base, &cookie, "/runs", parallel_cancel_input).await;
    let parallel_cancel_id = parallel_cancel["id"].as_str().unwrap();
    let mut parallel_approvals = Vec::new();
    for _ in 0..100 {
        let page = get(
            &client,
            &base,
            &cookie,
            &format!("/runs/{parallel_cancel_id}/runtime-approvals"),
        )
        .await;
        parallel_approvals = page["items"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|a| a["status"] == "pending")
            .cloned()
            .collect();
        if parallel_approvals.len() == 2 {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    assert_eq!(
        parallel_approvals.len(),
        2,
        "both member turns must reach approval concurrently"
    );
    assert_ne!(
        parallel_approvals[0]["agent_id"],
        parallel_approvals[1]["agent_id"]
    );
    let cancelled = post(
        &client,
        &base,
        &cookie,
        &format!("/runs/{parallel_cancel_id}/control"),
        json!({"action":"cancel"}),
    )
    .await;
    assert_eq!(cancelled["status"], "cancelled");
    let stopped = get(
        &client,
        &base,
        &cookie,
        &format!("/runs/{parallel_cancel_id}"),
    )
    .await;
    assert_eq!(stopped["kernel_status"], "cancelled");
    let page = get(
        &client,
        &base,
        &cookie,
        &format!("/runs/{parallel_cancel_id}/runtime-approvals"),
    )
    .await;
    assert_eq!(page["items"].as_array().unwrap().len(), 2);
    assert!(page["items"]
        .as_array()
        .unwrap()
        .iter()
        .all(|a| a["status"] == "delivered"));
    for member in [&developer, &tester] {
        let history: Value = serde_json::from_slice(
            &fs::read(
                temp.0
                    .join("data/runtime")
                    .join(parallel_cancel_id)
                    .join(member["id"].as_str().unwrap())
                    .join("team-fixture.json"),
            )
            .unwrap(),
        )
        .unwrap();
        assert_eq!(history["errors"], json!([]));
        assert_eq!(history["cancel_declined"], true);
        assert_eq!(history["thread"]["turns"][0]["status"], "interrupted");
    }
    let mut cancel_input = input.clone();
    cancel_input["request"] = json!("CANCEL_TEAM_APPROVAL");
    let cancel_run = post(&client, &base, &cookie, "/runs", cancel_input).await;
    let cancel_id = cancel_run["id"].as_str().unwrap();
    let mut pending_approval = None;
    for _ in 0..100 {
        let page = get(
            &client,
            &base,
            &cookie,
            &format!("/runs/{cancel_id}/runtime-approvals"),
        )
        .await;
        pending_approval = page["items"]
            .as_array()
            .unwrap()
            .iter()
            .find(|a| a["status"] == "pending")
            .cloned();
        if pending_approval.is_some() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    assert!(
        pending_approval.is_some(),
        "team approval was not persisted"
    );
    let cancelled = post(
        &client,
        &base,
        &cookie,
        &format!("/runs/{cancel_id}/control"),
        json!({"action":"cancel"}),
    )
    .await;
    assert_eq!(cancelled["status"], "cancelled");
    let stopped = get(&client, &base, &cookie, &format!("/runs/{cancel_id}")).await;
    assert_eq!(stopped["kernel_status"], "cancelled");
    assert!(stopped["artifact_digest"].as_str().unwrap().is_empty());
    let approvals = get(
        &client,
        &base,
        &cookie,
        &format!("/runs/{cancel_id}/runtime-approvals"),
    )
    .await;
    assert_eq!(approvals["items"][0]["status"], "delivered");
    let mut question_input = input.clone();
    question_input["request"] = json!("TEAM_NATIVE_QUESTION");
    let question_run = post(&client, &base, &cookie, "/runs", question_input).await;
    let question_run_id = question_run["id"].as_str().unwrap();
    let mut question = None;
    for _ in 0..100 {
        let page = get(
            &client,
            &base,
            &cookie,
            &format!("/runs/{question_run_id}/collaboration"),
        )
        .await;
        question = page["decisions"]
            .as_array()
            .unwrap()
            .iter()
            .find(|q| q["status"] == "pending")
            .cloned();
        if question.is_some() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    let question = question.expect("Codex native question must be persisted");
    assert_eq!(question["agent_id"], coordinator["id"]);
    post(
        &client,
        &base,
        &cookie,
        &format!(
            "/runs/{question_run_id}/questions/{}/answer",
            question["id"].as_str().unwrap()
        ),
        json!({"digest":question["digest"],"choice_id":"option_1"}),
    )
    .await;
    wait_submitted(&client, &base, &cookie, question_run_id).await;
    let callback: String = product
        .connect()
        .unwrap()
        .query_row(
            "SELECT status FROM forge_team_decision_callbacks WHERE run_id=?1",
            [question_run_id],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(callback, "delivered");
    server.abort();
}
