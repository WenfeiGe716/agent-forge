//! Compatibility checks against a disposable database built by the Python ORM.

use forge_server::{router, Backend, BackendError, PythonSqliteBackend, ServerConfig, User};
use reqwest::{Client, StatusCode};
use rusqlite::{params, Connection};
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    fs,
    io::{BufRead, BufReader},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{mpsc, Arc},
    time::Duration,
};
use tokio::{net::TcpListener, task::JoinHandle, time::timeout};
use uuid::Uuid;

const PASSWORD: &str = "fixture-password-\u{5bc6}\u{7801}-\u{e9}";
const REDACTED: &str = "[REDACTED]";
const DEADLINE: Duration = Duration::from_secs(5);

struct Fixture {
    directory: PathBuf,
    database: PathBuf,
}

impl Fixture {
    fn empty() -> Self {
        let directory = std::env::temp_dir().join(format!("forge-python-{}", Uuid::new_v4()));
        fs::create_dir(&directory).unwrap();
        Self {
            database: directory.join("product.sqlite"),
            directory,
        }
    }

    fn new() -> Self {
        let fixture = Self::empty();
        let manifest = Path::new(env!("CARGO_MANIFEST_DIR"));
        let root = manifest.ancestors().nth(3).unwrap();
        let local_python = root.join(".venv/bin/python");
        let python = std::env::var_os("FORGE_TEST_PYTHON").unwrap_or_else(|| {
            if local_python.exists() {
                local_python.into_os_string()
            } else {
                "python3".into()
            }
        });
        let output = Command::new(python)
            .arg(manifest.join("tests/fixtures/python_sqlite.py"))
            .arg(&fixture.database)
            .current_dir(&fixture.directory)
            .output()
            .expect("run Python ORM fixture; FORGE_TEST_PYTHON may select the interpreter");
        assert!(
            output.status.success(),
            "Python ORM fixture requires the backend Python dependencies: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        fixture
    }

    fn backend(&self) -> PythonSqliteBackend {
        PythonSqliteBackend::open(&self.database).unwrap()
    }

    fn writer(&self) -> Connection {
        Connection::open(&self.database).unwrap()
    }

    fn bytes(&self) -> BTreeMap<String, Vec<u8>> {
        fs::read_dir(&self.directory)
            .unwrap()
            .map(|entry| {
                let path = entry.unwrap().path();
                (
                    path.file_name().unwrap().to_str().unwrap().to_owned(),
                    fs::read(path).unwrap(),
                )
            })
            .collect()
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.directory);
    }
}

fn user(backend: &PythonSqliteBackend, name: &str) -> User {
    backend.user(&format!("{name}-id")).unwrap().unwrap()
}

fn inaccessible<T>(result: Result<T, BackendError>) {
    assert!(matches!(result, Err(BackendError::NotFound)));
}

fn ids(runs: &[Value]) -> Vec<&str> {
    runs.iter().map(|run| run["id"].as_str().unwrap()).collect()
}

#[test]
fn authenticates_actual_python_scrypt_and_rejects_invalid_credentials() {
    let fixture = Fixture::new();
    let backend = fixture.backend();
    let authenticated = backend
        .check_credentials("member", PASSWORD)
        .unwrap()
        .unwrap();
    assert_eq!(authenticated, user(&backend, "member"));
    assert_eq!(authenticated.role, "viewer");
    assert!(backend
        .check_credentials("member", "wrong")
        .unwrap()
        .is_none());
    assert!(backend
        .check_credentials("disabled", PASSWORD)
        .unwrap()
        .is_none());
    assert!(backend
        .check_credentials("missing", PASSWORD)
        .unwrap()
        .is_none());
    assert!(backend
        .check_credentials("malformed", PASSWORD)
        .unwrap()
        .is_none());
    assert!(backend
        .check_credentials("member' OR 1=1 --", PASSWORD)
        .unwrap()
        .is_none());
    assert!(backend.user("missing").unwrap().is_none());

    let writer = fixture.writer();
    for encoded in ["", "====", "YWJj", "!invalid base64!", &"A".repeat(108)] {
        writer
            .execute(
                "UPDATE users SET password_hash = ?1 WHERE id = 'malformed-id'",
                [encoded],
            )
            .unwrap();
        assert!(backend
            .check_credentials("malformed", PASSWORD)
            .unwrap()
            .is_none());
    }
}

#[test]
fn projects_authorize_owners_members_and_admins_before_list_limits() {
    let fixture = Fixture::new();
    let backend = fixture.backend();
    assert_eq!(
        ids(&backend.runs(&user(&backend, "member"), 1).unwrap()),
        ["run-shared"]
    );
    assert_eq!(
        ids(&backend.runs(&user(&backend, "member"), 10).unwrap()),
        ["run-shared", "run-old"]
    );
    assert_eq!(
        ids(&backend.runs(&user(&backend, "owner"), 10).unwrap()),
        ["run-owned"]
    );
    assert_eq!(backend.runs(&user(&backend, "admin"), 10).unwrap().len(), 4);
    assert!(backend
        .runs(&user(&backend, "run-owner"), 10)
        .unwrap()
        .is_empty());
    inaccessible(backend.run(&user(&backend, "run-owner"), "run-private"));
    inaccessible(backend.run(&user(&backend, "member"), "run-private"));
    inaccessible(backend.run(&user(&backend, "member"), "missing"));
    inaccessible(backend.events(&user(&backend, "member"), "run-private", 0, 100));
    inaccessible(backend.events(&user(&backend, "member"), "missing", 0, 100));
}

#[test]
fn native_run_statuses_cover_history_beyond_page_limit_and_follow_live_acl() {
    let fixture = Fixture::new();
    let backend = fixture.backend();
    let member = user(&backend, "member");
    let mut writer = fixture.writer();
    let transaction = writer.transaction().unwrap();
    for index in 0..501 {
        transaction.execute(
            "INSERT INTO runs (id,project_id,owner_id,title,request,status,snapshot,plan,plan_digest,artifact_digest,report,calls,input_tokens,output_tokens,error,created_at,updated_at)
             SELECT ?1,project_id,owner_id,title,request,status,snapshot,plan,plan_digest,artifact_digest,report,calls,input_tokens,output_tokens,error,created_at,updated_at FROM runs WHERE id='run-shared'",
            [format!("history-{index}")],
        ).unwrap();
    }
    transaction.commit().unwrap();
    assert_eq!(backend.runs(&member, 500).unwrap().len(), 500);
    let statuses = backend.run_statuses(&member).unwrap();
    assert_eq!(statuses.len(), 503);
    assert_eq!(statuses["run-old"], "paused");
    assert!(!statuses.contains_key("run-private"));
    writer
        .execute(
            "UPDATE projects SET config='{}' WHERE id='project-shared'",
            [],
        )
        .unwrap();
    assert!(backend.run_statuses(&member).unwrap().is_empty());
}

#[test]
fn maps_python_summary_detail_tasks_commands_and_default_values() {
    let fixture = Fixture::new();
    let backend = fixture.backend();
    let member = user(&backend, "member");
    let runs = backend.runs(&member, 10).unwrap();
    let summary = &runs[0];
    assert_eq!(summary["title"], "Fixture shared");
    assert_eq!(summary["request"], "Inspect fixture");
    assert_eq!(summary["status"], "paused");
    assert_eq!(summary["project_id"], "project-shared");
    assert_eq!(summary["calls"], 3);
    assert_eq!(summary["input_tokens"], 17);
    assert_eq!(summary["output_tokens"], 9);
    assert_eq!(summary["created_at"], "2026-09-12T01:00:00+00:00");
    assert_eq!(summary["updated_at"], "2026-09-12T01:00:00+00:00");
    assert_eq!(summary["execution_strategy"], "autonomous");
    assert_eq!(summary["execution_engine"], "codex");
    assert_eq!(summary["mode"], "autonomous");
    assert_eq!(summary["run_project_checks"], false);
    assert_eq!(summary["entry_agent_id"], "agent-z");
    assert_eq!(summary["planner_id"], "agent-a");
    assert_eq!(summary["reviewer_id"], Value::Null);
    assert_eq!(summary["agent_ids"], json!(["agent-z", "agent-a"]));
    assert!(summary.get("snapshot").is_none());
    assert!(summary.get("owner_id").is_none());
    assert_eq!(runs[1]["execution_strategy"], "workflow");
    assert_eq!(runs[1]["execution_engine"], "legacy");
    assert_eq!(runs[1]["mode"], "development");
    assert_eq!(runs[1]["run_project_checks"], true);
    assert_eq!(runs[1]["agent_ids"], json!([]));

    let detail = backend.run(&member, "run-shared").unwrap();
    for (key, value) in summary.as_object().unwrap() {
        assert_eq!(&detail[key], value, "summary field {key}");
    }
    assert_eq!(detail["plan_digest"], "plan-digest");
    assert_eq!(detail["artifact_digest"], "artifact-digest");
    assert_eq!(detail["event_cursor"], 8);
    assert_eq!(detail["tasks"].as_array().unwrap().len(), 1);
    assert_eq!(detail["tasks"][0]["id"], "task-shared");
    assert_eq!(detail["tasks"][0]["key"], "analysis");
    assert_eq!(detail["tasks"][0]["status"], "completed");
    assert!(detail["tasks"][0].get("messages").is_none());
    assert_eq!(detail["commands"].as_array().unwrap().len(), 1);
    assert_eq!(detail["commands"][0]["id"], "command-shared");
    assert_eq!(detail["commands"][0]["action"], "resume");
    assert_eq!(detail["commands"][0]["sent"], true);
    assert_eq!(backend.run(&member, "run-old").unwrap()["event_cursor"], 0);
}

#[test]
fn rechecks_current_membership_role_and_enabled_state_for_stale_users() {
    let fixture = Fixture::new();
    let backend = fixture.backend();
    let member = user(&backend, "member");
    let admin = user(&backend, "admin");
    let writer = fixture.writer();
    writer
        .execute(
            "UPDATE projects SET config = ?1 WHERE id = 'project-shared'",
            [r#"{"members":[]}"#],
        )
        .unwrap();
    assert!(backend.runs(&member, 10).unwrap().is_empty());
    inaccessible(backend.run(&member, "run-shared"));
    inaccessible(backend.events(&member, "run-shared", 0, 100));

    writer
        .execute("UPDATE users SET role = 'viewer' WHERE id = 'admin-id'", [])
        .unwrap();
    assert!(backend.runs(&admin, 10).unwrap().is_empty());
    inaccessible(backend.run(&admin, "run-private"));
    inaccessible(backend.events(&admin, "run-private", 0, 100));

    let mut forged = member.clone();
    forged.role = "admin".into();
    inaccessible(backend.run(&forged, "run-private"));
    writer
        .execute("UPDATE users SET role = 'admin' WHERE id = 'member-id'", [])
        .unwrap();
    assert_eq!(backend.runs(&member, 10).unwrap().len(), 4);
    writer
        .execute("UPDATE users SET enabled = 0 WHERE id = 'member-id'", [])
        .unwrap();
    inaccessible(backend.runs(&member, 10));
    inaccessible(backend.run(&member, "run-shared"));
    inaccessible(backend.events(&member, "run-shared", 0, 100));
    writer
        .execute("DELETE FROM users WHERE id = 'admin-id'", [])
        .unwrap();
    inaccessible(backend.run(&admin, "run-private"));
}

#[test]
fn scopes_event_queries_before_pagination_and_redacts_nested_payloads() {
    let fixture = Fixture::new();
    let backend = fixture.backend();
    let member = user(&backend, "member");
    for (after, expected, more) in [(0, 3, true), (3, 6, true), (6, 8, false)] {
        let page = backend.events(&member, "run-shared", after, 1).unwrap();
        assert_eq!(page.has_more, more);
        assert_eq!(page.events.len(), 1);
        let event = &page.events[0];
        assert_eq!(event.id, expected);
        assert_eq!(event.run_id, "run-shared");
        assert_eq!(event.kind, "runtime.fixture");
        assert_eq!(event.created_at, "2026-09-12T01:00:00+00:00");
        assert_eq!(event.data["payload"][0]["accessToken"], REDACTED);
        assert_eq!(event.data["message"], format!("Visible event {expected}"));
        assert_eq!(event.data["credential_revision"], "revision-1");
        assert_eq!(event.data["input_tokens"], 17);
    }
    let empty = backend.events(&member, "run-shared", 8, 1).unwrap();
    assert!(empty.events.is_empty());
    assert!(!empty.has_more);
    let all = backend.events(&member, "run-shared", 0, 100).unwrap();
    assert_eq!(
        all.events.iter().map(|event| event.id).collect::<Vec<_>>(),
        [3, 6, 8]
    );
    assert!(!all.has_more);
}

#[test]
fn recursively_redacts_structured_credentials_without_dropping_usage_or_narrative() {
    let fixture = Fixture::new();
    let backend = fixture.backend();
    let detail = backend
        .run(&user(&backend, "member"), "run-shared")
        .unwrap();
    for (key, value) in detail["snapshot"]["nested"][0].as_object().unwrap() {
        assert_eq!(value, REDACTED, "sensitive key {key}");
    }
    assert_eq!(detail["snapshot"]["agents"]["agent-z"]["api_key"], REDACTED);
    assert_eq!(
        detail["snapshot"]["nested"][1]["credential_revision"],
        "revision-1"
    );
    assert_eq!(detail["snapshot"]["nested"][1]["input_tokens"], 17);
    assert_eq!(detail["snapshot"]["nested"][1]["output_tokens"], 9);
    // Key-based redaction does not identify secrets embedded in arbitrary prose.
    assert_eq!(
        detail["snapshot"]["narrative"],
        "A literal password narrative stays visible."
    );
    assert_eq!(detail["plan"]["steps"][0]["authorization"], REDACTED);
    assert_eq!(detail["report"]["result"][0]["credentials"], REDACTED);
    assert_eq!(detail["tasks"][0]["spec"]["nested"]["env"], REDACTED);
    assert_eq!(detail["tasks"][0]["result"]["items"][0]["apiKey"], REDACTED);
    assert_eq!(
        detail["commands"][0]["payload"]["nested"][0]["claim_token"],
        REDACTED
    );
    assert!(!detail.to_string().contains("fixture-secret-"));
}

#[test]
fn legacy_snapshot_objects_cannot_bypass_redaction_through_summary_fields() {
    let fixture = Fixture::new();
    let backend = fixture.backend();
    let member = user(&backend, "member");
    let payload = json!({"api_key": "fixture-secret-summary", "name": "visible"});
    let snapshot = json!({
        "execution_engine": payload,
        "run_project_checks": payload,
        "entry_agent_id": payload,
        "planner_id": payload,
        "reviewer_id": payload,
        "orchestration": {"mode": payload},
    });
    fixture
        .writer()
        .execute(
            "UPDATE runs SET snapshot = ?1 WHERE id = 'run-shared'",
            [snapshot.to_string()],
        )
        .unwrap();
    for output in [
        backend.runs(&member, 1).unwrap().remove(0),
        backend.run(&member, "run-shared").unwrap(),
    ] {
        assert!(!output.to_string().contains("fixture-secret-"));
        for field in [
            "execution_engine",
            "run_project_checks",
            "entry_agent_id",
            "planner_id",
            "reviewer_id",
            "mode",
        ] {
            assert_eq!(output[field]["api_key"], REDACTED, "{field}");
            assert_eq!(output[field]["name"], "visible");
        }
    }
}

#[test]
fn never_creates_or_changes_a_database_and_rejects_incompatible_schema() {
    let missing = Fixture::empty();
    assert!(PythonSqliteBackend::open(&missing.database).is_err());
    assert!(!missing.database.exists());
    assert!(missing.bytes().is_empty());

    let fixture = Fixture::new();
    let before = fixture.bytes();
    {
        let backend = fixture.backend();
        let member = backend
            .check_credentials("member", PASSWORD)
            .unwrap()
            .unwrap();
        backend.user(&member.id).unwrap();
        backend.runs(&member, 10).unwrap();
        backend.run(&member, "run-shared").unwrap();
        backend.events(&member, "run-shared", 0, 100).unwrap();
    }
    assert_eq!(
        fixture.bytes(),
        before,
        "read adapter changed a file or created a sidecar"
    );

    let incompatible = Fixture::empty();
    incompatible
        .writer()
        .execute_batch("CREATE TABLE users (id TEXT PRIMARY KEY);")
        .unwrap();
    let before = incompatible.bytes();
    assert!(PythonSqliteBackend::open(&incompatible.database).is_err());
    assert_eq!(
        incompatible.bytes(),
        before,
        "adapter must not migrate a partial schema"
    );

    let corrupt = Fixture::empty();
    fs::write(&corrupt.database, b"this is not a sqlite database").unwrap();
    let before = corrupt.bytes();
    assert!(PythonSqliteBackend::open(&corrupt.database).is_err());
    assert_eq!(corrupt.bytes(), before);
}

#[test]
fn rejects_malformed_json_and_ambiguous_membership() {
    let fixture = Fixture::new();
    let backend = fixture.backend();
    let member = user(&backend, "member");
    let writer = fixture.writer();
    writer
        .execute(
            "UPDATE runs SET snapshot = '{broken' WHERE id = 'run-shared'",
            [],
        )
        .unwrap();
    assert!(matches!(
        backend.run(&member, "run-shared"),
        Err(BackendError::InvalidData)
    ));
    writer
        .execute(
            "UPDATE runs SET snapshot = '{}' WHERE id = 'run-shared'",
            [],
        )
        .unwrap();
    writer
        .execute("UPDATE events SET data = '{broken' WHERE id = 3", [])
        .unwrap();
    assert!(matches!(
        backend.events(&member, "run-shared", 0, 100),
        Err(BackendError::InvalidData)
    ));
    for config in [
        r#"{"members":"member-id"}"#,
        r#"{"members":{"member-id":true}}"#,
        r#"{"members":["member-id",42]}"#,
        r#"{"members":null}"#,
        "{broken",
    ] {
        writer
            .execute(
                "UPDATE projects SET config = ?1 WHERE id = 'project-shared'",
                [config],
            )
            .unwrap();
        inaccessible(backend.run(&member, "run-shared"));
        inaccessible(backend.events(&member, "run-shared", 8, 100));
        assert!(backend.runs(&member, 10).unwrap().is_empty());
    }
}

#[test]
fn reads_committed_wal_events_and_permissions_without_checkpointing() {
    let fixture = Fixture::new();
    let writer = fixture.writer();
    writer
        .execute_batch("PRAGMA journal_mode = WAL; PRAGMA wal_autocheckpoint = 0;")
        .unwrap();
    let backend = fixture.backend();
    let member = user(&backend, "member");
    let database_before = fs::read(&fixture.database).unwrap();
    writer
        .execute(
            "INSERT INTO events (id, run_id, kind, data, created_at) VALUES (?1, ?2, ?3, ?4, ?5)",
            params![
                11,
                "run-shared",
                "runtime.live",
                r#"{"message":"Committed WAL event"}"#,
                "2026-09-12T02:00:00+00:00"
            ],
        )
        .unwrap();
    let wal = fixture.directory.join("product.sqlite-wal");
    let wal_before = fs::read(&wal).unwrap();
    assert_eq!(fs::read(&fixture.database).unwrap(), database_before);
    let page = backend.events(&member, "run-shared", 8, 100).unwrap();
    assert_eq!(page.events.len(), 1);
    assert_eq!(page.events[0].id, 11);
    assert_eq!(page.events[0].data["message"], "Committed WAL event");
    assert_eq!(
        backend.run(&member, "run-shared").unwrap()["event_cursor"],
        11
    );
    assert_eq!(fs::read(&fixture.database).unwrap(), database_before);
    assert_eq!(fs::read(&wal).unwrap(), wal_before);
    writer
        .execute(
            "UPDATE projects SET config = '{}' WHERE id = 'project-shared'",
            [],
        )
        .unwrap();
    inaccessible(backend.events(&member, "run-shared", 11, 100));
}

struct ServerTask(JoinHandle<()>);

impl Drop for ServerTask {
    fn drop(&mut self) {
        self.0.abort();
    }
}

struct ServerProcess(Child);

impl Drop for ServerProcess {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

#[tokio::test]
async fn executable_reads_python_accounts_without_bootstrapping_or_writing_sessions() {
    let fixture = Fixture::new();
    let before = fixture.bytes();
    let mut process = ServerProcess(
        Command::new(env!("CARGO_BIN_EXE_forge-server"))
            .args(["--listen", "127.0.0.1:0", "--python-db"])
            .arg(&fixture.database)
            .env_remove("FORGE_SERVER_ADMIN_PASSWORD")
            .stderr(Stdio::piped())
            .stdout(Stdio::null())
            .spawn()
            .unwrap(),
    );
    let stderr = process.0.stderr.take().unwrap();
    let (sender, receiver) = mpsc::channel();
    let reader = std::thread::spawn(move || {
        let _ = sender.send(BufReader::new(stderr).lines().next());
    });
    let line = receiver
        .recv_timeout(DEADLINE)
        .expect("host did not start")
        .expect("host exited without a listen address")
        .unwrap();
    reader.join().unwrap();
    let address = line
        .strip_prefix("forge-server listening on ")
        .unwrap()
        .split(' ')
        .next()
        .unwrap();
    assert!(line.contains("read-only Python database"));
    let client = Client::builder()
        .no_proxy()
        .timeout(DEADLINE)
        .build()
        .unwrap();
    let response = client
        .post(format!("{address}/api/auth/login"))
        .header("X-Forge-Request", "1")
        .json(&json!({"username":"member", "password":PASSWORD}))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(response.json::<Value>().await.unwrap()["id"], "member-id");
    drop(process);
    assert_eq!(fixture.bytes(), before);
}

async fn next_frame(response: &mut reqwest::Response, pending: &mut Vec<u8>) -> String {
    timeout(DEADLINE, async {
        loop {
            if let Some(end) = pending.windows(2).position(|bytes| bytes == b"\n\n") {
                return String::from_utf8(pending.drain(..end + 2).collect()).unwrap();
            }
            let chunk = response
                .chunk()
                .await
                .unwrap()
                .expect("SSE ended before a frame");
            pending.extend_from_slice(&chunk);
        }
    })
    .await
    .expect("SSE frame did not arrive")
}

#[tokio::test]
async fn http_login_and_live_sse_observe_external_membership_revocation() {
    let fixture = Fixture::new();
    let backend = Arc::new(fixture.backend());
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let app = router(
        backend,
        ServerConfig {
            poll_interval: Duration::from_millis(10),
            ..ServerConfig::default()
        },
    );
    let _task = ServerTask(tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    }));
    let client = Client::builder()
        .no_proxy()
        .timeout(DEADLINE)
        .build()
        .unwrap();
    let response = client
        .post(format!("{base}/api/auth/login"))
        .header("X-Forge-Request", "1")
        .json(&json!({"username": "member", "password": PASSWORD}))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let cookie = response.headers()["set-cookie"]
        .to_str()
        .unwrap()
        .split(';')
        .next()
        .unwrap()
        .to_owned();
    assert_eq!(
        response.json::<Value>().await.unwrap(),
        json!({
            "id": "member-id", "username": "member", "role": "viewer", "enabled": true
        })
    );
    let detail = client
        .get(format!("{base}/api/runs/run-shared"))
        .header("Cookie", &cookie)
        .send()
        .await
        .unwrap();
    assert_eq!(detail.status(), StatusCode::OK);
    assert!(!detail.text().await.unwrap().contains("fixture-secret-"));

    let mut stream = client
        .get(format!("{base}/api/runs/run-shared/stream?after=6"))
        .header("Cookie", &cookie)
        .send()
        .await
        .unwrap();
    assert_eq!(stream.status(), StatusCode::OK);
    assert!(stream.headers()["content-type"]
        .to_str()
        .unwrap()
        .starts_with("text/event-stream"));
    let mut pending = Vec::new();
    let event = next_frame(&mut stream, &mut pending).await;
    assert!(event.starts_with("id: 8\ndata: "));
    assert!(event.contains(r#""accessToken":"[REDACTED]""#));
    assert!(!event.contains("fixture-secret-"));
    assert_eq!(
        next_frame(&mut stream, &mut pending).await,
        ": keepalive\n\n"
    );

    let writer = fixture.writer();
    writer
        .execute(
            "UPDATE projects SET config = ?1 WHERE id = ?2",
            params![r#"{"members":[]}"#, "project-shared"],
        )
        .unwrap();
    let remaining = timeout(DEADLINE, async {
        let mut bytes = pending;
        while let Some(chunk) = stream.chunk().await.unwrap() {
            bytes.extend_from_slice(&chunk);
        }
        String::from_utf8(bytes).unwrap()
    })
    .await
    .expect("revoked stream must close");
    assert!(
        remaining.ends_with("event: revoked\ndata: {}\n\n"),
        "unexpected SSE: {remaining:?}"
    );
    assert!(!remaining.contains("fixture-secret-"));
    for path in [
        "/api/runs/run-shared",
        "/api/runs/run-shared/events",
        "/api/runs/run-shared/stream",
    ] {
        let response = client
            .get(format!("{base}{path}"))
            .header("Cookie", &cookie)
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::NOT_FOUND, "{path}");
    }
    writer
        .execute("UPDATE users SET enabled = 0 WHERE id = 'member-id'", [])
        .unwrap();
    let response = client
        .get(format!("{base}/api/auth/me"))
        .header("Cookie", &cookie)
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
    let persisted_sessions: i64 = writer
        .query_row("SELECT COUNT(*) FROM sessions", [], |row| row.get(0))
        .unwrap();
    assert_eq!(
        persisted_sessions, 0,
        "Rust sessions must not write Python session data"
    );
}
