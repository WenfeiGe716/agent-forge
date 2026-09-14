use forge_server::{
    product_api::{ProductApi, ProductReply},
    ProductStore, User,
};
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    fs,
    io::{BufRead, BufReader, Read, Write},
    net::TcpListener,
    path::PathBuf,
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    },
    thread,
    time::{Duration, Instant},
};

struct Fixture {
    root: PathBuf,
    store: Arc<ProductStore>,
    api: ProductApi,
    user: User,
}
impl Fixture {
    fn new() -> Self {
        let root =
            std::env::temp_dir().join(format!("forge-model-ops-{}", uuid::Uuid::new_v4().simple()));
        fs::create_dir_all(&root).unwrap();
        let store = Arc::new(
            ProductStore::open(
                root.join("product.sqlite"),
                None,
                Some("operations-test-password"),
            )
            .unwrap()
            .with_encryption_key(&fernet::Fernet::generate_key())
            .unwrap(),
        );
        let user = store
            .check_credentials("admin", "operations-test-password")
            .unwrap()
            .unwrap();
        let api = ProductApi::new(store.clone(), root.join("data"), root.join("imports"));
        Self {
            root,
            store,
            api,
            user,
        }
    }
    fn call(&self, path: &str, body: Value, key: Option<&str>) -> ProductReply {
        let query = key
            .map(|k| BTreeMap::from([("__idempotency_key".into(), k.into())]))
            .unwrap_or_default();
        self.api
            .request(
                &self.user,
                "POST",
                path,
                &query,
                &serde_json::to_vec(&body).unwrap(),
            )
            .unwrap()
    }
    fn model(&self, protocol: &str, url: &str) -> Value {
        let custom = json!({"endpoint":"/invoke","body":{"model":"{{model}}","messages":"{{messages}}","tools":"{{tools}}","parameters":"{{parameters}}"},"response":{"text":"/text","calls":"/calls","call_fields":{"id":"/id","name":"/name","arguments":"/arguments"},"input_tokens":"/input","output_tokens":"/output"}});
        let provider=self.call("/api/providers",json!({"name":"Fixture","protocol":protocol,"base_url":url,"external":false,"protocol_config":if protocol=="custom_http"{custom}else{json!({})}}),None).body;
        let (allowed, parameters) = match protocol {
            "anthropic_messages" => (json!(["max_tokens"]), json!({"max_tokens":512})),
            "openai_responses" => (
                json!(["max_output_tokens"]),
                json!({"max_output_tokens":512}),
            ),
            "google_gemini" => (json!(["maxOutputTokens"]), json!({"maxOutputTokens":512})),
            _ => (json!([]), json!({})),
        };
        self.call("/api/models",json!({"name":"Measured model","provider_id":provider["id"],"model_id":"fixture-model","allowed_parameters":allowed,"parameters":parameters,"supports_tools":true}),None).body
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}

fn strings(value: &Value) -> Vec<&str> {
    match value {
        Value::String(s) => vec![s],
        Value::Array(a) => a.iter().flat_map(strings).collect(),
        Value::Object(o) => o.values().flat_map(strings).collect(),
        _ => vec![],
    }
}
fn reply(protocol: &str, text: &str, call: Option<Value>) -> Value {
    let call = call.unwrap_or(Value::Null);
    match protocol {
        "openai_chat" => {
            json!({"choices":[{"message":{"role":"assistant","content":text,"tool_calls":if call.is_null(){json!([])}else{json!([{"id":"call-1","type":"function","function":{"name":call["name"],"arguments":call["arguments"].to_string()}}])}}}],"usage":{"prompt_tokens":2,"completion_tokens":3}})
        }
        "openai_responses" => {
            json!({"output":if call.is_null(){json!([{"type":"message","content":[{"type":"output_text","text":text}]}])}else{json!([{"type":"function_call","call_id":"call-1","name":call["name"],"arguments":call["arguments"].to_string()}])},"usage":{"input_tokens":2,"output_tokens":3}})
        }
        "anthropic_messages" => {
            json!({"content":if call.is_null(){json!([{"type":"text","text":text}])}else{json!([{"type":"tool_use","id":"call-1","name":call["name"],"input":call["arguments"]}])},"usage":{"input_tokens":2,"output_tokens":3}})
        }
        "google_gemini" => {
            json!({"candidates":[{"content":{"parts":if call.is_null(){json!([{"text":text}])}else{json!([{"functionCall":{"name":call["name"],"args":call["arguments"]}}])}}}],"usageMetadata":{"promptTokenCount":2,"candidatesTokenCount":3}})
        }
        _ => {
            json!({"text":text,"calls":if call.is_null(){json!([])}else{json!([{"id":"call-1","name":call["name"],"arguments":call["arguments"]}])},"input":2,"output":3})
        }
    }
}
fn server(
    total: usize,
    mut handle: impl FnMut(usize, Value) -> (u16, Value) + Send + 'static,
) -> (String, Arc<AtomicUsize>, thread::JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let count = Arc::new(AtomicUsize::new(0));
    let calls = count.clone();
    let task = thread::spawn(move || {
        let deadline = Instant::now() + Duration::from_secs(30);
        for index in 0..total {
            let mut socket = loop {
                match listener.accept() {
                    Ok((s, _)) => break s,
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                        assert!(Instant::now() < deadline, "Missing fixture request {index}");
                        thread::sleep(Duration::from_millis(5));
                    }
                    Err(e) => panic!("{e}"),
                }
            };
            socket.set_nonblocking(false).unwrap();
            socket
                .set_read_timeout(Some(Duration::from_secs(5)))
                .unwrap();
            let mut reader = BufReader::new(socket.try_clone().unwrap());
            let mut line = String::new();
            reader.read_line(&mut line).unwrap();
            let mut length = 0;
            loop {
                let mut header = String::new();
                reader.read_line(&mut header).unwrap();
                if header == "\r\n" {
                    break;
                }
                if let Some((name, v)) = header.split_once(':') {
                    if name.eq_ignore_ascii_case("content-length") {
                        length = v.trim().parse().unwrap();
                    }
                }
            }
            let mut bytes = vec![0; length];
            reader.read_exact(&mut bytes).unwrap();
            let body = serde_json::from_slice(&bytes).unwrap();
            calls.fetch_add(1, Ordering::SeqCst);
            let (status, value) = handle(index, body);
            let encoded = value.to_string();
            write!(socket,"HTTP/1.1 {status} Fixture\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{encoded}",encoded.len()).unwrap();
        }
    });
    (url, count, task)
}

#[test]
fn five_protocol_benchmarks_make_real_requests_and_replay_durable_results() {
    for protocol in [
        "openai_chat",
        "openai_responses",
        "anthropic_messages",
        "google_gemini",
        "custom_http",
    ] {
        let f = Fixture::new();
        let (url, calls, task) = server(6, move |index, body| {
            let response = if index < 4 {
                reply(
                    protocol,
                    ["[0,1,2,3]", "B", "12", "[\"A\",\"B\",\"C\"]"][index],
                    None,
                )
            } else if index == 4 {
                let prompt = strings(&body)
                    .into_iter()
                    .find(|s| s.starts_with("Benchmark only."))
                    .unwrap();
                let nonce = prompt
                    .split("nonce ")
                    .nth(1)
                    .unwrap()
                    .split('.')
                    .next()
                    .unwrap();
                reply(
                    protocol,
                    "",
                    Some(json!({"name":"forge_benchmark_echo","arguments":{"nonce":nonce}})),
                )
            } else {
                assert!(body.to_string().contains("call-1") || protocol == "google_gemini");
                reply(protocol, "OK", None)
            };
            (200, response)
        });
        let model = f.model(protocol, &url);
        let path = format!(
            "/api/model-hub/models/{}/benchmark",
            model["id"].as_str().unwrap()
        );
        let result = f.call(&path, json!({"acknowledge":true}), Some("once"));
        assert_eq!(result.status, 200, "{}", result.body);
        assert_eq!(result.body["status"], "completed", "{}", result.body);
        assert_eq!(result.body["result"]["requests"], 6);
        assert_eq!(result.body["result"]["scores"]["tool"], 100.);
        assert_eq!(result.body["draft"]["status"], "draft");
        assert_eq!(result.body["draft"]["source"], "benchmark");
        task.join().unwrap();
        let reopened =
            ProductApi::new(f.store.clone(), f.root.join("data"), f.root.join("imports"));
        let replay = reopened
            .request(
                &f.user,
                "POST",
                &path,
                &BTreeMap::from([("__idempotency_key".into(), "once".into())]),
                br#"{"acknowledge":true}"#,
            )
            .unwrap();
        assert_eq!(result.body, replay.body);
        assert_eq!(calls.load(Ordering::SeqCst), 6);
        let different = reopened
            .request(
                &f.user,
                "POST",
                &path,
                &BTreeMap::from([("__idempotency_key".into(), "once".into())]),
                br#"{"acknowledge":false}"#,
            )
            .unwrap_err();
        assert_eq!(different.status, 409);
        let c = f.store.connect().unwrap();
        let hub = f
            .api
            .request(&f.user, "GET", "/api/model-hub", &BTreeMap::new(), &[])
            .unwrap();
        assert_eq!(hub.body["benchmarks"][0]["id"], result.body["id"]);
        assert_eq!(
            hub.body["default_suite"]["cases"].as_array().unwrap().len(),
            4
        );
        c.execute("UPDATE model_hub_invocations SET status='running'", [])
            .unwrap();
        let uncertain = reopened
            .request(
                &f.user,
                "POST",
                &path,
                &BTreeMap::from([("__idempotency_key".into(), "once".into())]),
                br#"{"acknowledge":true}"#,
            )
            .unwrap_err();
        assert_eq!(uncertain.status, 409);
        assert_eq!(calls.load(Ordering::SeqCst), 6);
        assert_eq!(
            c.query_row("SELECT count(*) FROM model_benchmarks", [], |r| r
                .get::<_, u64>(0))
                .unwrap(),
            1
        );
    }
}

#[test]
fn benchmark_failures_record_partial_evidence_without_a_capability_draft() {
    let f = Fixture::new();
    let (url, calls, task) = server(2, |index, _| {
        if index == 0 {
            (200, reply("openai_responses", "[0,1,2,3]", None))
        } else {
            (503, json!({"error":"fixture offline"}))
        }
    });
    let model = f.model("openai_responses", &url);
    let path = format!(
        "/api/model-hub/models/{}/benchmark",
        model["id"].as_str().unwrap()
    );
    let result = f.call(&path, json!({"acknowledge":true}), Some("failed-once"));
    task.join().unwrap();
    assert_eq!(result.body["status"], "failed");
    assert_eq!(result.body["result"]["requests"], 2);
    assert_eq!(result.body["result"]["successful_requests"], 1);
    assert!(result.body["draft"].is_null());
    assert_eq!(
        f.call(&path, json!({"acknowledge":true}), Some("failed-once"))
            .body,
        result.body
    );
    assert_eq!(calls.load(Ordering::SeqCst), 2);
    assert_eq!(
        f.store
            .connect()
            .unwrap()
            .query_row("SELECT count(*) FROM design_assets", [], |r| r
                .get::<_, u64>(0))
            .unwrap(),
        0
    );
}

#[test]
fn rules_and_ai_recommendations_remain_unpublished_and_cannot_expand_permissions() {
    let f = Fixture::new();
    let (url, calls, task) = server(3, |index, body| {
        let prompt = strings(&body)
            .into_iter()
            .find_map(|s| {
                serde_json::from_str::<Value>(s)
                    .ok()
                    .filter(|v| v.get("candidate_models").is_some())
            })
            .unwrap();
        let proposal = if index == 0 {
            json!({"task_type":"Verified routing","assignments":{"planner":prompt["candidate_models"][0]["id"]}})
        } else if index == 1 {
            json!({"members":[{"key":"worker","name":"Worker","role":"developer","model_profile_id":prompt["candidate_models"][0]["id"],"instructions":"Implement it","tool_permissions":["run_command"]}]})
        } else {
            json!({"task_type":"Unauthorized model","assignments":{"planner":"outside-catalog"}})
        };
        (
            200,
            reply(
                "openai_responses",
                "",
                Some(json!({"name":"propose_design","arguments":proposal})),
            ),
        )
    });
    let model = f.model("openai_responses", &url);
    let rules = f.call(
        "/api/design/recommend",
        json!({"kind":"team","request":"Implement a reviewed change"}),
        Some("rules"),
    );
    assert_eq!(rules.status, 201, "{}", rules.body);
    assert_eq!(rules.body["status"], "draft");
    assert_eq!(rules.body["generation"]["requests"], 0);
    let input = json!({"kind":"routing","request":"Implement a reviewed change","model_profile_id":model["id"],"acknowledge":true});
    let ai = f.call("/api/design/recommend", input.clone(), Some("ai"));
    assert_eq!(ai.status, 201, "{}", ai.body);
    assert_eq!(ai.body["source"], "ai");
    assert_eq!(ai.body["config"]["rules"]["allow_external"], false);
    assert_eq!(
        f.call("/api/design/recommend", input, Some("ai")).body,
        ai.body
    );
    let invalid=f.call("/api/design/recommend",json!({"kind":"team","request":"Implement a reviewed change","model_profile_id":model["id"],"acknowledge":true}),Some("invalid"));
    assert_eq!(invalid.status, 502);
    let outside=f.call("/api/design/recommend",json!({"kind":"routing","request":"Implement a reviewed change","model_profile_id":model["id"],"acknowledge":true}),Some("outside"));
    assert_eq!(outside.status, 502);
    task.join().unwrap();
    assert_eq!(calls.load(Ordering::SeqCst), 3);
    let c = f.store.connect().unwrap();
    assert_eq!(
        c.query_row("SELECT count(*) FROM design_assets", [], |r| r
            .get::<_, u64>(0))
            .unwrap(),
        2
    );
    assert_eq!(
        c.query_row("SELECT count(*) FROM agents", [], |r| r.get::<_, u64>(0))
            .unwrap(),
        0
    );
    assert_eq!(
        c.query_row(
            "SELECT count(*) FROM design_assets WHERE published_version IS NOT NULL",
            [],
            |r| r.get::<_, u64>(0)
        )
        .unwrap(),
        0
    );
}

#[test]
fn directed_commands_reject_ambiguous_departments_and_respect_explicit_entry() {
    let f = Fixture::new();
    let first = f
        .call(
            "/api/agents",
            json!({"name":"First","role":"developer","department":"Quality"}),
            None,
        )
        .body;
    f.call(
        "/api/agents",
        json!({"name":"Second","role":"tester","department":"Quality"}),
        None,
    );
    assert_eq!(
        f.call(
            "/api/commands/resolve",
            json!({"text":"@First inspect"}),
            None
        )
        .body["entry_agent_id"],
        first["id"]
    );
    let err = f
        .api
        .request(
            &f.user,
            "POST",
            "/api/commands/resolve",
            &BTreeMap::new(),
            br#"{"text":"@{Quality} inspect"}"#,
        )
        .unwrap_err();
    assert_eq!(err.status, 422);
    let resolved = f.call(
        "/api/commands/resolve",
        json!({"text":"@{Quality} inspect","entry_agent_id":first["id"]}),
        None,
    );
    assert_eq!(resolved.body["recipients"].as_array().unwrap().len(), 2);
    f.store
        .connect()
        .unwrap()
        .execute(
            "UPDATE users SET role='developer' WHERE id=?1",
            [&f.user.id],
        )
        .unwrap();
    assert_eq!(
        f.call(
            "/api/commands/resolve",
            json!({"text":"@First inspect"}),
            None
        )
        .status,
        200
    );
}
