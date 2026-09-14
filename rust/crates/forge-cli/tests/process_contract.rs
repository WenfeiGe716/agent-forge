//! Cross-process checks use the public NDJSON host, including dropped replies.
use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};

struct Directory(PathBuf);
impl Directory {
    fn new() -> Self {
        static SEQUENCE: AtomicU64 = AtomicU64::new(0);
        let stamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "forge-cli-{}-{stamp}-{}",
            std::process::id(),
            SEQUENCE.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir(&path).unwrap();
        Self(path)
    }
    fn db(&self) -> PathBuf {
        self.0.join("kernel.sqlite3")
    }
}
impl Drop for Directory {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

struct Host {
    child: Child,
    input: ChildStdin,
    output: BufReader<ChildStdout>,
}
impl Host {
    fn start(db: &Path) -> Self {
        let mut child = Command::new(env!("CARGO_BIN_EXE_forge-kernel"))
            .arg("--db")
            .arg(db)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let input = child.stdin.take().unwrap();
        let output = BufReader::new(child.stdout.take().unwrap());
        Self {
            child,
            input,
            output,
        }
    }
    fn send(&mut self, request: &Value) {
        serde_json::to_writer(&mut self.input, request).unwrap();
        self.input.write_all(b"\n").unwrap();
        self.input.flush().unwrap();
    }
    fn read(&mut self) -> Value {
        let mut line = String::new();
        self.output.read_line(&mut line).unwrap();
        serde_json::from_str(&line).expect("host must reply with one JSON frame")
    }
    fn call(&mut self, request: Value) -> Value {
        self.send(&request);
        self.read()
    }
    fn execute(&mut self, id: &str, command: Value) -> Value {
        let response = self.call(json!({"op":"execute","command_id":id,"command":command}));
        assert_eq!(response["ok"], true, "{response}");
        response["result"].clone()
    }
    fn setup(&mut self) {
        self.execute("create-run", json!({"type":"create_run","run_id":"r"}));
        self.execute(
            "create-task",
            json!({"type":"create_task","run_id":"r","task_id":"t"}),
        );
    }
}
impl Drop for Host {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn claim(id: &str, worker: &str, version: i64) -> Value {
    json!({"op":"execute","command_id":id,"command":{"type":"claim_task","task_id":"t","worker_id":worker,"expected_version":version,"lease_duration_ms":900000}})
}

#[test]
fn competing_processes_have_one_claim_owner() {
    let directory = Directory::new();
    let mut first = Host::start(&directory.db());
    first.setup();
    let mut second = Host::start(&directory.db());
    first.send(&claim("claim-1", "w1", 0));
    second.send(&claim("claim-2", "w2", 0));
    let a = first.read();
    let b = second.read();
    assert_ne!(
        a["ok"], b["ok"],
        "exactly one durable claim must win: {a}, {b}"
    );
    let task = first.call(json!({"op":"task","task_id":"t"}));
    assert_eq!(task["result"]["attempt_generation"], 1);
    let events = first.call(json!({"op":"events","after":0,"limit":100}));
    assert_eq!(events["result"].as_array().unwrap().len(), 3);
}

#[test]
fn committed_but_unread_claim_reply_is_recoverable_after_process_death() {
    let directory = Directory::new();
    let mut host = Host::start(&directory.db());
    host.setup();
    let command = claim("stable-claim", "w", 0);
    host.send(&command);
    // Observe the durable claim from another process, leaving the original
    // reply unread to simulate a client losing its acknowledgement.
    let mut observer = Host::start(&directory.db());
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
    let saved = loop {
        let row = observer.call(json!({"op":"task","task_id":"t"}));
        if row["result"]["status"] == "running" {
            break row["result"].clone();
        }
        assert!(std::time::Instant::now() < deadline, "claim did not commit");
        std::thread::yield_now();
    };
    drop(host);
    let mut restarted = Host::start(&directory.db());
    let retry = restarted.call(command.clone());
    assert_eq!(retry["ok"], true);
    assert_eq!(retry["result"]["result"]["value"], saved);
    assert_eq!(restarted.call(command), retry);
    let events = restarted.call(json!({"op":"events","after":0,"limit":100}));
    let outbox = restarted.call(json!({"op":"outbox","limit":100}));
    assert_eq!(events["result"].as_array().unwrap().len(), 3);
    assert_eq!(outbox["result"].as_array().unwrap().len(), 3);
}

#[test]
fn released_worker_cannot_overwrite_reassigned_work() {
    let directory = Directory::new();
    let mut host = Host::start(&directory.db());
    host.setup();
    let first = host.call(claim("claim-old", "worker", 0));
    let token = first["result"]["result"]["value"]["claim"]["token"].clone();
    host.execute(
        "release-old",
        json!({"type":"release_task","task_id":"t","claim_token":token,"attempt_generation":1}),
    );
    let new = host.call(claim("claim-new", "worker", 2));
    assert_eq!(new["ok"], true);
    let stale = host.call(json!({"op":"execute","command_id":"stale-submit","command":{"type":"submit_task","task_id":"t","claim_token":token,"attempt_generation":1,"result":{"text":"stale"}}}));
    assert_eq!(stale["ok"], false);
    let current = &new["result"]["result"]["value"];
    let receipt = host.execute("submit-new", json!({"type":"submit_task","task_id":"t","claim_token":current["claim"]["token"],"attempt_generation":2,"result":{"text":"candidate"}}));
    assert_eq!(receipt["result"]["value"]["status"], "submitted");
    assert_eq!(receipt["result"]["value"]["result"]["text"], "candidate");
}

#[test]
fn conflicting_command_identity_and_unknown_fields_do_not_mutate() {
    let directory = Directory::new();
    let mut host = Host::start(&directory.db());
    host.setup();
    let bad = host.call(json!({"op":"execute","command_id":"create-run","command":{"type":"create_run","run_id":"other"}}));
    assert_eq!(bad["ok"], false);
    let bad = host.call(json!({"op":"execute","command_id":"with-clock","command":{"type":"claim_task","task_id":"t","worker_id":"w","expected_version":0,"lease_duration_ms":100,"now_ms":0}}));
    assert_eq!(bad["error"]["code"], "invalid_request");
    let task = host.call(json!({"op":"task","task_id":"t"}));
    assert_eq!(task["result"]["status"], "ready");
    assert_eq!(
        host.call(json!({"op":"run","run_id":"other"}))["result"],
        Value::Null
    );
    assert_eq!(
        host.call(json!({"op":"events","after":0,"limit":0}))["ok"],
        false
    );
}

#[test]
fn oversize_frame_closes_transport_without_committing() {
    let directory = Directory::new();
    let mut host = Host::start(&directory.db());
    let frame = vec![b'x'; 1024 * 1024 + 1];
    host.input.write_all(&frame).unwrap();
    host.input.flush().unwrap();
    assert_eq!(host.read()["error"]["code"], "invalid_frame");
    assert!(host.child.wait().unwrap().success());
    let mut next = Host::start(&directory.db());
    assert_eq!(
        next.call(json!({"op":"events","after":0,"limit":100}))["result"],
        json!([])
    );
}
