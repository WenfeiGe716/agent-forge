use forge_server::{router_with_product_store, ProductStore, RustOwnedBackend, ServerConfig};
use reqwest::{Client, StatusCode};
use serde_json::{json, Value};
use std::{
    fs,
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};
use tokio::net::TcpListener;
use uuid::Uuid;

struct Temp(PathBuf);
impl Temp {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!("forge-http-run-{}", Uuid::new_v4()));
        fs::create_dir(&path).unwrap();
        Self(path)
    }
}
impl Drop for Temp {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

async fn post(client: &Client, base: &str, cookie: &str, path: &str, input: Value) -> Value {
    let response = client
        .post(format!("{base}/api{path}"))
        .header("x-forge-request", "1")
        .header("cookie", cookie)
        .json(&input)
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

async fn pending(client: &Client, base: &str, cookie: &str, id: &str) -> Value {
    for _ in 0..150 {
        let page = get(
            client,
            base,
            cookie,
            &format!("/runs/{id}/runtime-approvals"),
        )
        .await;
        if let Some(found) = page["items"]
            .as_array()
            .unwrap()
            .iter()
            .find(|a| a["status"] == "pending")
        {
            return found.clone();
        }
        let run = get(client, base, cookie, &format!("/runs/{id}")).await;
        assert_ne!(run["status"], "needs_attention", "run failed: {run}");
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    panic!("No pending approval for {id}");
}

async fn run_status(client: &Client, base: &str, cookie: &str, id: &str, expected: &str) -> Value {
    for _ in 0..150 {
        let run = get(client, base, cookie, &format!("/runs/{id}")).await;
        if run["status"] == expected {
            return run;
        }
        assert_ne!(run["status"], "needs_attention", "run failed: {run}");
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    panic!("Run {id} did not reach {expected}");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn product_writes_run_approval_download_acceptance_and_idempotency_use_rust() {
    let temp = Temp::new();
    let key = "YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE=";
    let product = Arc::new(
        ProductStore::open(
            temp.0.join("product.sqlite"),
            None,
            Some("test-http-product-password"),
        )
        .unwrap()
        .with_encryption_key(key)
        .unwrap(),
    );
    let fixture = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../forge-runtime/tests/fixtures/execution_app_server.py")
        .canonicalize()
        .unwrap();
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
        .timeout(Duration::from_secs(15))
        .build()
        .unwrap();
    let login = client
        .post(format!("{base}/api/auth/login"))
        .header("x-forge-request", "1")
        .json(&json!({"username":"admin","password":"test-http-product-password"}))
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
    for path in [
        "/providers",
        "/models",
        "/agents",
        "/projects",
        "/runs",
        "/settings",
        "/organization",
    ] {
        get(&client, &base, &cookie, path).await;
    }
    let provider=post(&client,&base,&cookie,"/providers",json!({"name":"Fixture","protocol":"openai_responses","base_url":"http://127.0.0.1:9999/v1","api_key":"fixture-only-secret","external":false})).await;
    let model=post(&client,&base,&cookie,"/models",json!({"name":"Fixture model","provider_id":provider["id"],"model_id":"fixture-model","parameters":{},"allowed_parameters":[],"supports_tools":true})).await;
    let agent=post(&client,&base,&cookie,"/agents",json!({"name":"Developer","role":"developer","model_profile_id":model["id"],"instructions":"Implement the request."})).await;
    let project=post(&client,&base,&cookie,"/projects",json!({"name":"Run project","blank":true,"execution_mode":"local","permission_profile":"ask","runtime":{"command_timeout_s":1}})).await;
    let input = json!({"project_id":project["id"],"title":"HTTP owned run","request":"Write fixture-result.txt exactly once.","agent_ids":[agent["id"]]});
    let project_config: String = product
        .connect()
        .unwrap()
        .query_row(
            "SELECT config FROM projects WHERE id=?1",
            [project["id"].as_str().unwrap()],
            |r| r.get(0),
        )
        .unwrap();
    let agent_config: String = product
        .connect()
        .unwrap()
        .query_row(
            "SELECT config FROM agents WHERE id=?1",
            [agent["id"].as_str().unwrap()],
            |r| r.get(0),
        )
        .unwrap();
    product
        .connect()
        .unwrap()
        .execute(
            "UPDATE projects SET config=json_set(config,'$.runtime',json(?1)) WHERE id=?2",
            rusqlite::params![
                json!({"task_timeout_s":30}).to_string(),
                project["id"].as_str().unwrap()
            ],
        )
        .unwrap();
    product
        .connect()
        .unwrap()
        .execute(
            "UPDATE agents SET config=json_set(config,'$.runtime',json(?1)) WHERE id=?2",
            rusqlite::params![
                json!({"call_timeout_s":7}).to_string(),
                agent["id"].as_str().unwrap()
            ],
        )
        .unwrap();
    let runtime_preview = post(
        &client,
        &base,
        &cookie,
        "/runs/preview-config",
        input.clone(),
    )
    .await;
    assert_eq!(runtime_preview["runtime"]["task_timeout_s"], 30);
    assert_eq!(runtime_preview["runtime"]["call_timeout_s"], 7);
    product
        .connect()
        .unwrap()
        .execute(
            "UPDATE projects SET config=?1 WHERE id=?2",
            rusqlite::params![project_config, project["id"].as_str().unwrap()],
        )
        .unwrap();
    product
        .connect()
        .unwrap()
        .execute(
            "UPDATE agents SET config=?1 WHERE id=?2",
            rusqlite::params![agent_config, agent["id"].as_str().unwrap()],
        )
        .unwrap();
    for (table, id, key, value) in [
        (
            "agents",
            agent["id"].as_str().unwrap(),
            "model_parameters",
            json!({"temperature":0.5}),
        ),
        (
            "agents",
            agent["id"].as_str().unwrap(),
            "fallback_model_ids",
            json!([model["id"]]),
        ),
        (
            "model_profiles",
            model["id"].as_str().unwrap(),
            "parameters",
            json!({"max_tokens":4096}),
        ),
    ] {
        let c = product.connect().unwrap();
        let original: String = c
            .query_row(
                &format!("SELECT config FROM {table} WHERE id=?1"),
                [id],
                |r| r.get(0),
            )
            .unwrap();
        c.execute(
            &format!("UPDATE {table} SET config=json_set(config,?1,json(?2)) WHERE id=?3"),
            rusqlite::params![format!("$.{key}"), value.to_string(), id],
        )
        .unwrap();
        for endpoint in ["/runs/preview-config", "/runs"] {
            let response = client
                .post(format!("{base}/api{endpoint}"))
                .header("x-forge-request", "1")
                .header("cookie", &cookie)
                .json(&input)
                .send()
                .await
                .unwrap();
            assert!(
                matches!(
                    response.status(),
                    StatusCode::CONFLICT | StatusCode::UNPROCESSABLE_ENTITY
                ),
                "unsupported {table}.{key}: {}",
                response.status()
            );
        }
        c.execute(
            &format!("UPDATE {table} SET config=?1 WHERE id=?2"),
            rusqlite::params![original, id],
        )
        .unwrap();
        let intents: i64 = c
            .query_row("SELECT count(*) FROM forge_run_intents", [], |r| r.get(0))
            .unwrap();
        assert_eq!(intents, 0);
    }
    let preview = post(
        &client,
        &base,
        &cookie,
        "/runs/preview-config",
        input.clone(),
    )
    .await;
    assert_eq!(preview["owner"], "rust");
    let create = || {
        client
            .post(format!("{base}/api/runs"))
            .header("x-forge-request", "1")
            .header("cookie", &cookie)
            .header("idempotency-key", "one-run")
            .json(&input)
            .send()
    };
    let response = create().await.unwrap();
    assert_eq!(response.status(), StatusCode::ACCEPTED);
    let run: Value = response.json().await.unwrap();
    let id = run["id"].as_str().unwrap();
    let duplicate: Value = create().await.unwrap().json().await.unwrap();
    assert_eq!(duplicate["id"], id);
    // The employee panel rejects collaboration data without the requested run identity.
    let collaboration = get(
        &client,
        &base,
        &cookie,
        &format!("/runs/{id}/collaboration"),
    )
    .await;
    assert_eq!(
        collaboration["run_id"], id,
        "employee session must identify its run"
    );

    let approval = tokio::time::timeout(Duration::from_secs(15), async {
        loop {
            let approvals = get(
                &client,
                &base,
                &cookie,
                &format!("/runs/{id}/runtime-approvals"),
            )
            .await;
            if let Some(a) = approvals["items"].as_array().and_then(|a| a.first()) {
                break a.clone();
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
    })
    .await
    .unwrap();
    post(
        &client,
        &base,
        &cookie,
        &format!("/runs/{id}/control"),
        json!({"action":"pause"}),
    )
    .await;
    assert_eq!(
        get(&client, &base, &cookie, &format!("/runs/{id}")).await["status"],
        "paused"
    );
    assert!(!temp
        .0
        .join("data/runs")
        .join(id)
        .join("workspace/fixture-result.txt")
        .exists());
    let note = post(
        &client,
        &base,
        &cookie,
        &format!("/runs/{id}/instructions"),
        json!({"body":"Inspect existing output before continuing."}),
    )
    .await;
    assert_eq!(note["status"], "persisted");
    product.connect().unwrap().execute(
        "INSERT INTO decision_requests VALUES('ordinary-question',?1,'codex',?2,?3,?4,'pending','{}','','2026-01-01','')",
        rusqlite::params![id, agent["id"].as_str().unwrap(), "b".repeat(64), json!({"question":"Which result?","options":[],"allow_text":true}).to_string()],
    ).unwrap();
    let answer_input = json!({"digest":"b".repeat(64),"text":"Keep the requested fixture output."});
    let question_answer = post(
        &client,
        &base,
        &cookie,
        &format!("/runs/{id}/questions/ordinary-question/answer"),
        answer_input.clone(),
    )
    .await;
    assert_eq!(question_answer["status"], "answered");
    assert_eq!(
        post(
            &client,
            &base,
            &cookie,
            &format!("/runs/{id}/questions/ordinary-question/answer"),
            answer_input.clone()
        )
        .await,
        question_answer
    );
    post(
        &client,
        &base,
        &cookie,
        &format!("/runs/{id}/control"),
        json!({"action":"resume"}),
    )
    .await;
    let previous = approval;
    let approval = pending(&client, &base, &cookie, id).await;
    assert_ne!(approval["id"], previous["id"]);
    tokio::time::sleep(Duration::from_millis(1100)).await;
    assert_eq!(
        get(&client, &base, &cookie, &format!("/runs/{id}")).await["kernel_status"],
        "running"
    );
    let history: Value = serde_json::from_str(
        &fs::read_to_string(
            temp.0
                .join("data/runtime")
                .join(id)
                .join("fixture-history.json"),
        )
        .unwrap(),
    )
    .unwrap();
    assert_eq!(history["steers"].as_array().unwrap().len(), 1);
    assert!(history["steers"][0][0]["text"]
        .as_str()
        .unwrap()
        .contains("Keep the requested fixture output."));
    assert!(product.connect().unwrap().query_row("SELECT sent FROM outbox WHERE action='answer' AND json_extract(payload,'$.request_id')='ordinary-question'", [], |r| r.get::<_, bool>(0)).unwrap());
    assert_eq!(
        post(
            &client,
            &base,
            &cookie,
            &format!("/runs/{id}/questions/ordinary-question/answer"),
            answer_input
        )
        .await,
        question_answer
    );
    post(
        &client,
        &base,
        &cookie,
        &format!(
            "/runs/{id}/runtime-approvals/{}/decision",
            approval["id"].as_str().unwrap()
        ),
        json!({"digest":approval["digest"],"approved":true}),
    )
    .await;
    let completed = tokio::time::timeout(Duration::from_secs(15), async {
        loop {
            let run = get(&client, &base, &cookie, &format!("/runs/{id}")).await;
            if run["status"] == "awaiting_acceptance" {
                break run;
            }
            assert_ne!(run["status"], "needs_attention", "{run}");
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
    })
    .await
    .unwrap();
    assert_eq!(completed["title"], "HTTP owned run");
    let artifact = client
        .get(format!("{base}/api/runs/{id}/artifacts/discussion.md"))
        .header("cookie", &cookie)
        .send()
        .await
        .unwrap();
    assert_eq!(artifact.status(), StatusCode::OK);
    assert!(artifact
        .text()
        .await
        .unwrap()
        .contains("Fixture result ready."));
    let artifact_path = temp
        .0
        .join("data/runs")
        .join(id)
        .join("artifacts/discussion.md");
    let original_bytes = fs::read(&artifact_path).unwrap();
    fs::write(&artifact_path, b"tampered").unwrap();
    let failed = client
        .post(format!("{base}/api/runs/{id}/decision/acceptance"))
        .header("x-forge-request", "1")
        .header("cookie", &cookie)
        .json(&json!({"digest":completed["artifact_digest"],"approved":true}))
        .send()
        .await
        .unwrap();
    assert_eq!(failed.status(), StatusCode::CONFLICT);
    fs::write(&artifact_path, original_bytes).unwrap();
    post(&client,&base,&cookie,&format!("/runs/{id}/decision/acceptance"),json!({"digest":completed["artifact_digest"],"approved":false,"note":"Produce a revised result."})).await;
    assert_eq!(
        get(&client, &base, &cookie, &format!("/runs/{id}")).await["status"],
        "paused"
    );
    post(
        &client,
        &base,
        &cookie,
        &format!("/runs/{id}/control"),
        json!({"action":"continue","task_key":"codex"}),
    )
    .await;
    let approval = pending(&client, &base, &cookie, id).await;
    post(
        &client,
        &base,
        &cookie,
        &format!(
            "/runs/{id}/runtime-approvals/{}/decision",
            approval["id"].as_str().unwrap()
        ),
        json!({"digest":approval["digest"],"approved":true}),
    )
    .await;
    let completed = run_status(&client, &base, &cookie, id, "awaiting_acceptance").await;
    let accepted = post(
        &client,
        &base,
        &cookie,
        &format!("/runs/{id}/decision/acceptance"),
        json!({"digest":completed["artifact_digest"],"approved":true}),
    )
    .await;
    assert_eq!(accepted["status"], "completed");
    let c = product.connect().unwrap();
    let legacy_runs: i64 = c
        .query_row("SELECT count(*) FROM runs", [], |r| r.get(0))
        .unwrap();
    assert_eq!(legacy_runs, 0);
    let files = fs::read_to_string(
        temp.0
            .join("data/runs")
            .join(id)
            .join("workspace/fixture-result.txt"),
    )
    .unwrap();
    assert_eq!(files, "fixture-turn-2\nfixture-turn-3\n");
    let receipt_count:i64=c.query_row("SELECT count(*) FROM instruction_receipts r JOIN human_instructions i ON i.id=r.instruction_id WHERE i.run_id=?1",[id],|r|r.get(0)).unwrap();
    assert_eq!(receipt_count, 2);

    let cancelled = post(&client, &base, &cookie, "/runs", input).await;
    let id = cancelled["id"].as_str().unwrap();
    pending(&client, &base, &cookie, id).await;
    post(
        &client,
        &base,
        &cookie,
        &format!("/runs/{id}/control"),
        json!({"action":"cancel"}),
    )
    .await;
    assert_eq!(
        get(&client, &base, &cookie, &format!("/runs/{id}")).await["status"],
        "cancelled"
    );
    assert!(!temp
        .0
        .join("data/runs")
        .join(id)
        .join("workspace/fixture-result.txt")
        .exists());

    server.abort();
    let _ = server.await;
}
