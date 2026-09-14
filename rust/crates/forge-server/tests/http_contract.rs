//! Real HTTP compatibility gates mirrored from the Python migration contract.
//! The memory adapter supplies isolated fixture data; requests cross a TCP socket.

use forge_server::{router, Event, MemoryBackend, ProjectAccess, RunView, ServerConfig, User};
use reqwest::{Client, Method, RequestBuilder, Response, StatusCode};
use serde_json::{json, Value};
use std::{collections::BTreeSet, sync::Arc, time::Duration};
use tokio::{net::TcpListener, task::JoinHandle, time::timeout};

const DEADLINE: Duration = Duration::from_secs(5);
const PASSWORD: &str = "contract-password-only-12345";
const RUN_ID: &str = "contract-run";

#[tokio::test]
async fn capabilities_do_not_advertise_unimplemented_write_routes() {
    let server = TestServer::start().await;
    let response = server
        .request(Method::GET, "/api/capabilities", None)
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(response.headers()["cache-control"], "no-store");
    let value: Value = response.json().await.unwrap();
    assert_eq!(value["read"]["runs"], true);
    assert_eq!(value["read"]["events"], true);
    assert_eq!(value["read"]["stream"], true);
    assert_eq!(value["write"]["create_run"], false);
    assert_eq!(value["write"]["control_run"], false);
    assert_eq!(value["write"]["decisions"], false);
    assert_eq!(value["write"]["approvals"], false);
    assert_eq!(value["durability"]["python_database"], "read_only");
}

struct TestServer {
    backend: Arc<MemoryBackend>,
    client: Client,
    base_url: String,
    task: JoinHandle<()>,
}

impl TestServer {
    async fn start() -> Self {
        let backend = Arc::new(MemoryBackend::new());
        backend
            .add_user(
                User {
                    id: "admin-id".into(),
                    username: "admin".into(),
                    role: "admin".into(),
                    enabled: true,
                },
                PASSWORD,
            )
            .unwrap();
        backend
            .upsert_project(ProjectAccess {
                id: "contract-project".into(),
                owner_id: "admin-id".into(),
                members: vec![],
            })
            .unwrap();
        backend.put_run(run(RUN_ID, "contract-project")).unwrap();
        for id in [100, 101, 102] {
            backend.append_event(event(id, RUN_ID)).unwrap();
        }

        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let app = router(
            backend.clone(),
            ServerConfig {
                poll_interval: Duration::from_millis(10),
                ..ServerConfig::default()
            },
        );
        let task = tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        Self {
            backend,
            client: Client::builder()
                .no_proxy()
                .timeout(DEADLINE)
                .build()
                .unwrap(),
            base_url: format!("http://{address}"),
            task,
        }
    }

    fn request(&self, method: Method, path: &str, cookie: Option<&str>) -> RequestBuilder {
        let request = self
            .client
            .request(method, format!("{}{path}", self.base_url))
            .header("X-Forge-Request", "1");
        match cookie {
            Some(cookie) => request.header("Cookie", cookie),
            None => request,
        }
    }

    async fn login(&self, username: &str) -> (String, Response) {
        let response = self
            .request(Method::POST, "/api/auth/login", None)
            .json(&json!({"username": username, "password": PASSWORD}))
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
        (cookie, response)
    }

    async fn stream(&self, cookie: &str, run_id: &str, after: i64, last: &str) -> EventStream {
        let response = self
            .request(
                Method::GET,
                &format!("/api/runs/{run_id}/stream?after={after}"),
                Some(cookie),
            )
            .header("Last-Event-ID", last)
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        assert!(response.headers()["content-type"]
            .to_str()
            .unwrap()
            .starts_with("text/event-stream"));
        assert_eq!(response.headers()["x-accel-buffering"], "no");
        EventStream {
            response,
            pending: vec![],
        }
    }
}

impl Drop for TestServer {
    fn drop(&mut self) {
        self.task.abort();
    }
}

struct EventStream {
    response: Response,
    pending: Vec<u8>,
}

impl EventStream {
    async fn next_frame(&mut self) -> Option<String> {
        timeout(DEADLINE, async {
            loop {
                if let Some(end) = self.pending.windows(2).position(|bytes| bytes == b"\n\n") {
                    return Some(
                        String::from_utf8(self.pending.drain(..end + 2).collect()).unwrap(),
                    );
                }
                match self.response.chunk().await.unwrap() {
                    Some(chunk) => self.pending.extend_from_slice(&chunk),
                    None => {
                        assert!(
                            self.pending.is_empty(),
                            "SSE ended with an incomplete frame"
                        );
                        return None;
                    }
                }
            }
        })
        .await
        .expect("SSE frame did not arrive before the deadline")
    }

    async fn assert_revoked_and_closed(&mut self) {
        timeout(DEADLINE, async {
            loop {
                let frame = self
                    .next_frame()
                    .await
                    .expect("stream ended before revoked");
                if frame == "event: revoked\ndata: {}\n\n" {
                    break;
                }
                // A keepalive already buffered before revocation is harmless.
                assert_eq!(frame, ": keepalive\n\n");
            }
            assert_eq!(self.next_frame().await, None);
        })
        .await
        .expect("revoked SSE did not close before the deadline");
    }
}

fn event(id: i64, run_id: &str) -> Event {
    Event {
        id,
        run_id: run_id.into(),
        kind: "runtime.item.agentMessage.delta".into(),
        data: json!({"payload": {"delta": format!("消息 {id}")}}),
        created_at: "2026-09-12T00:00:00+00:00".into(),
    }
}

fn run(id: &str, project_id: &str) -> RunView {
    let summary = json!({
        "id": id,
        "project_id": project_id,
        "title": "兼容任务",
        "request": "检查接口",
        "status": "paused",
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "error": "",
        "created_at": "2026-09-12T00:00:00+00:00",
        "updated_at": "2026-09-12T00:00:00+00:00",
        "agent_ids": [],
        "mode": "autonomous",
        "execution_strategy": "autonomous",
        "execution_engine": "codex",
        "run_project_checks": false,
        "entry_agent_id": null,
        "planner_id": null,
        "reviewer_id": null
    });
    let mut detail = summary.clone();
    detail.as_object_mut().unwrap().extend(
        json!({
            "snapshot": {"execution_engine": "codex", "execution_strategy": "autonomous"},
            "plan": null,
            "plan_digest": "",
            "artifact_digest": "",
            "report": {},
            "tasks": [],
            "commands": []
        })
        .as_object()
        .unwrap()
        .clone(),
    );
    RunView {
        id: id.into(),
        project_id: project_id.into(),
        summary,
        detail,
    }
}

fn assert_keys(value: &Value, expected: &[&str]) {
    let actual: BTreeSet<_> = value
        .as_object()
        .unwrap()
        .keys()
        .map(String::as_str)
        .collect();
    assert_eq!(actual, expected.iter().copied().collect());
}

fn event_from_frame(frame: &str, id: i64) -> Value {
    let prefix = format!("id: {id}\ndata: ");
    assert!(
        frame.starts_with(&prefix),
        "unexpected SSE frame: {frame:?}"
    );
    assert!(frame.ends_with("\n\n"));
    serde_json::from_str(&frame[prefix.len()..frame.len() - 2]).unwrap()
}

#[tokio::test]
async fn cookie_csrf_and_json_errors_are_compatible() {
    let server = TestServer::start().await;
    let (cookie, response) = server.login("admin").await;
    let set_cookie = response.headers()["set-cookie"]
        .to_str()
        .unwrap()
        .to_ascii_lowercase();
    for attribute in [
        "forge_session=",
        "httponly",
        "samesite=strict",
        "path=/",
        "max-age=43200",
    ] {
        assert!(
            set_cookie.contains(attribute),
            "missing {attribute}: {set_cookie}"
        );
    }
    assert_eq!(response.headers()["cache-control"], "no-store");
    assert_eq!(response.headers()["x-content-type-options"], "nosniff");
    let user: Value = response.json().await.unwrap();
    assert_keys(&user, &["id", "username", "role", "enabled"]);
    assert_eq!(user["username"], "admin");

    for marker in [None, Some(""), Some("0")] {
        // RequestBuilder::header appends, so construct these without the
        // helper's valid marker instead of accidentally sending two headers.
        let request = server
            .client
            .post(format!("{}/api/auth/logout", server.base_url))
            .header("Cookie", &cookie);
        let request = match marker {
            Some(marker) => request.header("X-Forge-Request", marker),
            None => request,
        };
        let denied = request.send().await.unwrap();
        assert_eq!(denied.status(), StatusCode::FORBIDDEN);
        assert!(denied.json::<Value>().await.unwrap()["detail"].is_string());
    }
    let cross_origin = server
        .request(Method::POST, "/api/auth/logout", Some(&cookie))
        .header("Origin", "https://not-authorized.example")
        .send()
        .await
        .unwrap();
    assert_eq!(cross_origin.status(), StatusCode::FORBIDDEN);

    for (path, cookie, status) in [
        (
            "/api/a-route-that-does-not-exist",
            Some(cookie.as_str()),
            StatusCode::NOT_FOUND,
        ),
        ("/api/runs", None, StatusCode::UNAUTHORIZED),
    ] {
        let error = server
            .request(Method::GET, path, cookie)
            .send()
            .await
            .unwrap();
        assert_eq!(error.status(), status);
        assert!(error.headers()["content-type"]
            .to_str()
            .unwrap()
            .starts_with("application/json"));
        assert!(error.json::<Value>().await.unwrap()["detail"].is_string());
    }
}

#[tokio::test]
async fn event_pages_use_exclusive_cursor_and_frontend_envelope() {
    let server = TestServer::start().await;
    let (cookie, _) = server.login("admin").await;
    let response = server
        .request(
            Method::GET,
            "/api/runs/contract-run/events?after=100&limit=1",
            Some(&cookie),
        )
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let page: Value = response.json().await.unwrap();
    assert_keys(&page, &["events", "has_more"]);
    assert_eq!(page["has_more"], true);
    assert_eq!(page["events"].as_array().unwrap().len(), 1);
    let item = &page["events"][0];
    assert_keys(item, &["id", "run_id", "kind", "data", "created_at"]);
    assert_eq!(item["id"], 101);
    assert_eq!(item["run_id"], RUN_ID);
    assert_eq!(item["data"]["payload"]["delta"], "消息 101");
    let empty: Value = server
        .request(
            Method::GET,
            "/api/runs/contract-run/events?after=102",
            Some(&cookie),
        )
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(empty, json!({"events": [], "has_more": false}));

    for query in ["after=-1", "limit=0", "limit=1001", "after=invalid"] {
        let invalid = server
            .request(
                Method::GET,
                &format!("/api/runs/{RUN_ID}/events?{query}"),
                Some(&cookie),
            )
            .send()
            .await
            .unwrap();
        assert_eq!(
            invalid.status(),
            StatusCode::UNPROCESSABLE_ENTITY,
            "{query}"
        );
        assert!(invalid.json::<Value>().await.unwrap()["detail"].is_array());
    }
}

#[tokio::test]
async fn sse_resumes_at_max_query_and_last_event_id() {
    let server = TestServer::start().await;
    let (cookie, _) = server.login("admin").await;
    for (after, last) in [(100, "101"), (101, "100"), (101, "invalid")] {
        let mut stream = server.stream(&cookie, RUN_ID, after, last).await;
        let frame = stream.next_frame().await.unwrap();
        let item = event_from_frame(&frame, 102);
        assert_eq!(item["id"], 102);
        assert_eq!(item["data"]["payload"]["delta"], "消息 102");
        assert_keys(&item, &["id", "run_id", "kind", "data", "created_at"]);
    }
}

#[tokio::test]
async fn sse_keepalive_and_midstream_authorization_revocation() {
    let server = TestServer::start().await;
    let (cookie, _) = server.login("admin").await;
    let mut stream = server.stream(&cookie, RUN_ID, 102, "0").await;
    assert_eq!(
        stream.next_frame().await.as_deref(),
        Some(": keepalive\n\n")
    );
    server.backend.set_user_enabled("admin-id", false).unwrap();
    stream.assert_revoked_and_closed().await;
    let response = server
        .request(Method::GET, "/api/auth/me", Some(&cookie))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn logout_revokes_an_open_stream_and_closes_the_connection() {
    let server = TestServer::start().await;
    let (cookie, _) = server.login("admin").await;
    let mut stream = server.stream(&cookie, RUN_ID, 102, "0").await;
    assert_eq!(
        stream.next_frame().await.as_deref(),
        Some(": keepalive\n\n")
    );
    let response = server
        .request(Method::POST, "/api/auth/logout", Some(&cookie))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let deleted_cookie = response.headers()["set-cookie"]
        .to_str()
        .unwrap()
        .to_ascii_lowercase();
    assert!(deleted_cookie.contains("forge_session="));
    assert!(deleted_cookie.contains("max-age=0"));
    assert_eq!(response.json::<Value>().await.unwrap(), json!({"ok": true}));
    stream.assert_revoked_and_closed().await;
    let response = server
        .request(Method::GET, "/api/auth/me", Some(&cookie))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn project_membership_scopes_run_lists_pages_and_streams() {
    let server = TestServer::start().await;
    server
        .backend
        .add_user(
            User {
                id: "reader-id".into(),
                username: "reader".into(),
                role: "viewer".into(),
                enabled: true,
            },
            PASSWORD,
        )
        .unwrap();
    server
        .backend
        .upsert_project(ProjectAccess {
            id: "reader-project".into(),
            owner_id: "admin-id".into(),
            members: vec!["reader-id".into()],
        })
        .unwrap();
    server
        .backend
        .put_run(run("reader-run", "reader-project"))
        .unwrap();
    server
        .backend
        .append_event(event(103, "reader-run"))
        .unwrap();
    for id in 104..=106 {
        server.backend.append_event(event(id, RUN_ID)).unwrap();
    }
    server
        .backend
        .append_event(event(107, "reader-run"))
        .unwrap();
    let (cookie, _) = server.login("reader").await;
    let runs: Value = server
        .request(Method::GET, "/api/runs", Some(&cookie))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(runs.as_array().unwrap().len(), 1);
    assert_eq!(runs[0]["id"], "reader-run");

    for suffix in ["", "/events", "/stream"] {
        let denied = server
            .request(
                Method::GET,
                &format!("/api/runs/{RUN_ID}{suffix}"),
                Some(&cookie),
            )
            .send()
            .await
            .unwrap();
        assert_eq!(denied.status(), StatusCode::NOT_FOUND, "{suffix}");
        assert!(denied.json::<Value>().await.unwrap()["detail"].is_string());
    }
    let page: Value = server
        .request(
            Method::GET,
            "/api/runs/reader-run/events?after=103&limit=1",
            Some(&cookie),
        )
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(page["events"].as_array().unwrap().len(), 1);
    assert_eq!(page["events"][0]["id"], 107);
    assert_eq!(page["events"][0]["run_id"], "reader-run");
    assert_eq!(page["has_more"], false);
    let mut stream = server.stream(&cookie, "reader-run", 103, "0").await;
    let item = event_from_frame(&stream.next_frame().await.unwrap(), 107);
    assert_eq!(item["run_id"], "reader-run");

    server
        .backend
        .upsert_project(ProjectAccess {
            id: "reader-project".into(),
            owner_id: "admin-id".into(),
            members: vec![],
        })
        .unwrap();
    stream.assert_revoked_and_closed().await;
}

#[tokio::test]
async fn maximum_page_size_still_detects_one_more_matching_event() {
    let server = TestServer::start().await;
    server
        .backend
        .put_run(run("page-run", "contract-project"))
        .unwrap();
    for id in 1000..=2000 {
        server.backend.append_event(event(id, "page-run")).unwrap();
    }
    let (cookie, _) = server.login("admin").await;
    let response = server
        .request(
            Method::GET,
            "/api/runs/page-run/events?limit=1000",
            Some(&cookie),
        )
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let page: Value = response.json().await.unwrap();
    let events = page["events"].as_array().unwrap();
    assert_eq!(events.len(), 1000);
    assert_eq!(page["has_more"], true);
    for (index, event) in events.iter().enumerate() {
        assert_eq!(event["id"], 1000 + index as i64);
        assert_eq!(event["run_id"], "page-run");
    }
    let next: Value = server
        .request(
            Method::GET,
            "/api/runs/page-run/events?after=1999&limit=1000",
            Some(&cookie),
        )
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(next["events"].as_array().unwrap().len(), 1);
    assert_eq!(next["events"][0]["id"], 2000);
    assert_eq!(next["has_more"], false);
}
