use forge_server::{
    product_api::{ProductApi, ProductReply},
    ProductStore, User,
};
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    fs,
    io::{Cursor, Write},
    path::PathBuf,
    sync::Arc,
};
use uuid::Uuid;

struct Fixture {
    root: PathBuf,
    store: Arc<ProductStore>,
    api: ProductApi,
    user: User,
}
impl Fixture {
    fn new() -> Self {
        let root =
            std::env::temp_dir().join(format!("forge-product-api-{}", Uuid::new_v4().simple()));
        fs::create_dir_all(root.join("imports")).unwrap();
        let store = Arc::new(
            ProductStore::open(root.join("product.db"), None, Some("correct-test-password"))
                .unwrap()
                .with_encryption_key("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
                .unwrap(),
        );
        let user = store
            .check_credentials("admin", "correct-test-password")
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
    fn call(&self, m: &str, path: &str, b: Value) -> ProductReply {
        self.api
            .request(
                &self.user,
                m,
                path,
                &BTreeMap::new(),
                &serde_json::to_vec(&b).unwrap(),
            )
            .unwrap()
    }
    fn provider(&self) -> Value {
        self.call("POST","/api/providers",json!({"name":"Test provider","protocol":"openai_responses","base_url":"http://127.0.0.1:9919/v1","external":false,"api_key":"not-in-the-public-api"})).body
    }
    fn model(&self) -> Value {
        let p = self.provider();
        self.call("POST","/api/models",json!({"name":"Test model","provider_id":p["id"],"model_id":"test","allowed_parameters":["max_output_tokens"],"parameters":{"max_output_tokens":4096},"supports_tools":true})).body
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}
fn zip(files: &[(&str, &str)]) -> Vec<u8> {
    let mut zip = zip::ZipWriter::new(Cursor::new(Vec::new()));
    for (name, text) in files {
        zip.start_file(*name, zip::write::SimpleFileOptions::default())
            .unwrap();
        zip.write_all(text.as_bytes()).unwrap();
    }
    zip.finish().unwrap().into_inner()
}

#[test]
fn cookie_users_cannot_impersonate_team_members() {
    let f = Fixture::new();
    for path in [
        "/api/runs/team-run/tasks",
        "/api/runs/team-run/tasks/codex/claim",
        "/api/runs/team-run/tasks/codex/result",
        "/api/runs/team-run/messages",
        "/api/runs/team-run/messages/m1/deliver",
    ] {
        let error = f
            .api
            .request(
                &f.user,
                "POST",
                path,
                &BTreeMap::new(),
                &serde_json::to_vec(&json!({"agent_id":"lead","sender_id":"lead"})).unwrap(),
            )
            .unwrap_err();
        assert_eq!(error.status, 403, "{path}");
    }
}

#[test]
fn provider_revisions_keep_keys_encrypted_and_public_projection_redacts() {
    let f = Fixture::new();
    let p = f.provider();
    let pid = p["id"].as_str().unwrap();
    let rows = f.call("GET", "/api/providers", json!({})).body;
    assert_eq!(rows[0]["has_key"], true);
    assert!(!rows.to_string().contains("not-in-the-public-api"));
    let updated=f.call("PUT",&format!("/api/providers/{pid}"),json!({"name":"Renamed","protocol":"openai_responses","base_url":"http://127.0.0.1:9919/v1","external":false})).body;
    assert_ne!(updated["revision_id"], p["revision_id"]);
    let c = f.store.connect().unwrap();
    let keys = c
        .prepare("SELECT encrypted_key FROM provider_revisions ORDER BY id")
        .unwrap()
        .query_map([], |r| r.get::<_, String>(0))
        .unwrap()
        .collect::<Result<Vec<_>, _>>()
        .unwrap();
    assert_eq!(keys.len(), 2);
    for key in keys {
        assert_ne!(key, "not-in-the-public-api");
        assert_eq!(
            f.store.decrypt_secret(&key).unwrap(),
            "not-in-the-public-api"
        );
    }
}

#[test]
fn custom_http_defaults_are_usable_and_secret_headers_cannot_be_persisted() {
    let f = Fixture::new();
    let mut input = json!({"name":"Custom", "protocol":"custom_http", "base_url":"http://127.0.0.1:9920", "external":false, "protocol_config":{"endpoint":"/generate","body":{"model":"{{model}}","messages":"{{messages}}"},"response":{"text":"/text"}}});
    let provider = f.call("POST", "/api/providers", input.clone()).body;
    assert_eq!(
        f.call("GET", "/api/providers", json!({})).body[0]["protocol_config"]["headers"],
        json!({})
    );
    let model = f.call("POST", "/api/models", json!({"name":"Custom model","provider_id":provider["id"],"model_id":"custom","allowed_parameters":[],"parameters":{}})).body;
    assert_eq!(model["model_id"], "custom");
    for (name, value) in [
        ("Authorization", "Bearer unencrypted"),
        ("X-API-Key", "unencrypted"),
        ("Content-Type", "text/plain"),
        ("bad header", "invalid"),
    ] {
        input["protocol_config"]["headers"] = json!({name:value});
        let error = f
            .api
            .request(
                &f.user,
                "POST",
                "/api/providers",
                &BTreeMap::new(),
                &serde_json::to_vec(&input).unwrap(),
            )
            .unwrap_err();
        assert_eq!(error.status, 422, "{name}");
    }
    input["protocol_config"]["headers"] = json!({});
    input["protocol_config"]["response"]["text"] = json!("/invalid~3escape");
    assert_eq!(
        f.api
            .request(
                &f.user,
                "POST",
                "/api/providers",
                &BTreeMap::new(),
                &serde_json::to_vec(&input).unwrap()
            )
            .unwrap_err()
            .status,
        422
    );
    assert_eq!(
        f.call("GET", "/api/providers", json!({}))
            .body
            .as_array()
            .unwrap()
            .len(),
        1
    );
}

#[test]
fn native_provider_static_headers_are_persisted_and_reject_credentials() {
    let f = Fixture::new();
    let provider = f
        .call(
            "POST",
            "/api/providers",
            json!({
                "name": "Native headers",
                "protocol": "openai_responses",
                "base_url": "http://127.0.0.1:9919/v1",
                "external": false,
                "protocol_config": {"headers": {"X-Tenant": "tenant-a", "X-Trace": "trace-1"}}
            }),
        )
        .body;
    let saved = f.call("GET", "/api/providers", json!({})).body;
    let row = saved
        .as_array()
        .unwrap()
        .iter()
        .find(|value| value["id"] == provider["id"])
        .unwrap();
    assert_eq!(row["protocol_config"]["headers"]["X-Tenant"], "tenant-a");

    let error = f
        .api
        .request(
            &f.user,
            "POST",
            "/api/providers",
            &BTreeMap::new(),
            &serde_json::to_vec(&json!({
                "name": "Invalid native headers",
                "protocol": "openai_responses",
                "base_url": "http://127.0.0.1:9919/v1",
                "external": false,
                "protocol_config": {"headers": {"X-API-Key": "secret"}}
            }))
            .unwrap(),
        )
        .unwrap_err();
    assert_eq!(error.status, 422);
}

#[test]
fn setup_only_counts_probes_for_the_current_provider_revision_and_model_version() {
    let f = Fixture::new();
    let model = f.model();
    let provider = f.call("GET", "/api/providers", json!({})).body[0].clone();
    let path = format!("probe:{}", model["id"].as_str().unwrap());
    let c = f.store.connect().unwrap();
    c.execute(
        "INSERT INTO platform_config(id,value) VALUES(?1,?2)",
        rusqlite::params![
            path,
            json!({"revision_id":provider["revision_id"],"model_version":model["version"]})
                .to_string()
        ],
    )
    .unwrap();
    assert_eq!(
        f.call("GET", "/api/setup/status", json!({})).body["checked_model_count"],
        1
    );
    f.call("PUT", &format!("/api/providers/{}", provider["id"].as_str().unwrap()), json!({"name":"New revision","protocol":"openai_responses","base_url":"http://127.0.0.1:9919/v1","external":false}));
    assert_eq!(
        f.call("GET", "/api/setup/status", json!({})).body["checked_model_count"],
        0
    );
}

#[test]
fn accounts_are_rechecked_for_every_product_write() {
    let f = Fixture::new();
    f.store
        .connect()
        .unwrap()
        .execute("UPDATE users SET role='viewer' WHERE id=?1", [&f.user.id])
        .unwrap();
    let err = f
        .api
        .request(
            &f.user,
            "POST",
            "/api/projects",
            &BTreeMap::new(),
            br#"{"name":"Denied","blank":true}"#,
        )
        .unwrap_err();
    assert_eq!(err.status, 403);
    assert_eq!(
        f.store
            .connect()
            .unwrap()
            .query_row("SELECT COUNT(*) FROM projects", [], |r| r.get::<_, u64>(0))
            .unwrap(),
        0
    );
}

#[test]
fn project_snapshot_excludes_credentials_and_keeps_git_baseline() {
    let f = Fixture::new();
    let source = f.root.join("imports/demo");
    fs::create_dir_all(&source).unwrap();
    fs::write(source.join("hello.txt"), "source content").unwrap();
    fs::write(source.join(".env"), "secret=value").unwrap();
    let p = f
        .call(
            "POST",
            "/api/projects",
            json!({"name":"Demo","import_path":"demo"}),
        )
        .body;
    let snapshot = f.root.join("data/projects").join(p["id"].as_str().unwrap());
    assert_eq!(
        fs::read_to_string(snapshot.join("hello.txt")).unwrap(),
        "source content"
    );
    assert!(!snapshot.join(".env").exists());
    assert_eq!(p["base_commit"].as_str().unwrap().len(), 40);
    let err = f
        .api
        .request(
            &f.user,
            "POST",
            "/api/projects",
            &BTreeMap::new(),
            br#"{"name":"Escape","import_path":"../outside"}"#,
        )
        .unwrap_err();
    assert_eq!(err.status, 422);
    assert_eq!(
        f.call("GET", "/api/projects", json!({}))
            .body
            .as_array()
            .unwrap()
            .len(),
        1
    );
}

#[cfg(unix)]
#[test]
fn project_import_ignores_symlinks_preserves_execution_and_rejects_hard_links() {
    use std::os::unix::fs::{symlink, PermissionsExt};
    let f = Fixture::new();
    let source = f.root.join("imports/links");
    fs::create_dir_all(source.join("nested")).unwrap();
    fs::write(source.join("run.sh"), "#!/bin/sh\nexit 0\n").unwrap();
    fs::set_permissions(source.join("run.sh"), fs::Permissions::from_mode(0o755)).unwrap();
    fs::write(f.root.join("outside.txt"), "outside content").unwrap();
    symlink(f.root.join("outside.txt"), source.join("linked.txt")).unwrap();
    symlink(&f.root, source.join("nested/escape")).unwrap();
    let project = f
        .call(
            "POST",
            "/api/projects",
            json!({"name":"Links","import_path":"links"}),
        )
        .body;
    let snapshot = f
        .root
        .join("data/projects")
        .join(project["id"].as_str().unwrap());
    assert!(!snapshot.join("linked.txt").exists());
    assert!(!snapshot.join("nested/escape").exists());
    assert_ne!(
        fs::metadata(snapshot.join("run.sh"))
            .unwrap()
            .permissions()
            .mode()
            & 0o111,
        0
    );
    fs::hard_link(f.root.join("outside.txt"), source.join("hard.txt")).unwrap();
    let error = f
        .api
        .request(
            &f.user,
            "POST",
            "/api/projects",
            &BTreeMap::new(),
            br#"{"name":"Hard link","import_path":"links"}"#,
        )
        .unwrap_err();
    assert_eq!(error.status, 422);
    assert_eq!(
        f.call("GET", "/api/projects", json!({}))
            .body
            .as_array()
            .unwrap()
            .len(),
        1
    );
    assert_eq!(
        fs::read_dir(f.root.join("data/projects")).unwrap().count(),
        1
    );
}

#[test]
fn skill_review_is_bound_to_content_and_export_round_trips() {
    let f = Fixture::new();
    let text="---\nname: test-skill\ndescription: A test skill\n---\nFollow the existing project conventions.\n";
    let s = f
        .call(
            "POST",
            "/api/skills",
            json!({"files":{"SKILL.md":text},"expected_version":null}),
        )
        .body;
    let id = s["id"].as_str().unwrap();
    let path = format!("/api/skills/{id}/review");
    let err = f
        .api
        .request(
            &f.user,
            "POST",
            &path,
            &BTreeMap::new(),
            &serde_json::to_vec(&json!({"version":1,"digest":"stale","action":"approve"})).unwrap(),
        )
        .unwrap_err();
    assert_eq!(err.status, 409);
    let approved = f
        .call(
            "POST",
            &path,
            json!({"version":1,"digest":s["digest"],"action":"approve"}),
        )
        .body;
    assert_eq!(approved["status"], "approved");
    let archive = f
        .call("GET", &format!("/api/skills/{id}/export"), json!({}))
        .download
        .unwrap();
    let second = Fixture::new();
    let imported = second
        .api
        .request(
            &second.user,
            "POST",
            "/api/skills/import",
            &BTreeMap::new(),
            &archive.bytes,
        )
        .unwrap();
    assert_eq!(imported.body["files"]["SKILL.md"], text);
    assert_eq!(imported.body["status"], "draft");
}

#[test]
fn design_revision_conflicts_are_atomic_and_published_history_is_immutable() {
    let f = Fixture::new();
    let draft = f
        .call(
            "POST",
            "/api/design/assets",
            json!({"kind":"office","name":"Layout","config":{"positions":{},"floors":[],"desks":[]}}),
        )
        .body;
    let id = draft["id"].as_str().unwrap();
    let confirmed = json!({"version":1,"digest":draft["digest"],"comment":"Reviewed"});
    assert_eq!(
        f.call(
            "POST",
            &format!("/api/design/assets/{id}/publish"),
            confirmed.clone()
        )
        .body["status"],
        "published"
    );
    let edited = f
        .call(
            "PUT",
            &format!("/api/design/assets/{id}"),
            json!({"name":"Second","config":{"positions":{},"floors":[],"desks":[]},"expected_version":1}),
        )
        .body;
    assert_eq!(edited["version"], 2);
    let err = f
        .api
        .request(
            &f.user,
            "PUT",
            &format!("/api/design/assets/{id}"),
            &BTreeMap::new(),
            br#"{"name":"Stale","config":{},"expected_version":1}"#,
        )
        .unwrap_err();
    assert_eq!(err.status, 409);
    assert_eq!(
        f.call(
            "GET",
            &format!("/api/design/assets/{id}/versions/1"),
            json!({})
        )
        .body["name"],
        "Layout"
    );
    assert_eq!(
        f.call(
            "POST",
            &format!("/api/design/assets/{id}/revoke"),
            confirmed
        )
        .body["status"],
        "revoked"
    );
}

#[test]
fn team_instantiation_maps_all_member_relationships_atomically() {
    let f = Fixture::new();
    let model = f.model();
    let draft=f.call("POST","/api/design/assets",json!({"kind":"team","name":"Delivery","config":{"members":[{"key":"dev","name":"Developer","role":"developer","model_profile_id":model["id"],"reports_to":"lead","communication":{"scope":"custom","recipients":["lead"]}},{"key":"lead","name":"Lead","role":"reviewer","model_profile_id":model["id"]}]}})).body;
    let id = draft["id"].as_str().unwrap();
    let review = json!({"version":1,"digest":draft["digest"]});
    f.call(
        "POST",
        &format!("/api/design/assets/{id}/publish"),
        review.clone(),
    );
    let result = f
        .call(
            "POST",
            &format!("/api/design/assets/{id}/instantiate"),
            review,
        )
        .body;
    let agents = f.call("GET", "/api/agents", json!({})).body;
    let developer = agents
        .as_array()
        .unwrap()
        .iter()
        .find(|a| a["id"] == result["member_mapping"]["dev"])
        .unwrap();
    assert_eq!(developer["reports_to"], result["member_mapping"]["lead"]);
    assert_eq!(
        developer["communication"]["recipients"][0],
        result["member_mapping"]["lead"]
    );
    assert_eq!(developer["version"], 1);
}

#[test]
fn archive_catalog_install_requires_review_then_binding_requires_approval() {
    let f = Fixture::new();
    let source = f
        .call(
            "POST",
            "/api/skill-store/sources",
            json!({"name":"Archive","kind":"archive"}),
        )
        .body;
    let id = source["id"].as_str().unwrap();
    let raw = zip(&[
        (
            "repo/catalog-skill/SKILL.md",
            "---\nname: catalog-skill\ndescription: Catalog test\n---\nUse reviewed evidence.\n",
        ),
        ("repo/LICENSE", "MIT License"),
    ]);
    let query = BTreeMap::from([("expected_version".into(), "1".into())]);
    let result = f
        .api
        .request(
            &f.user,
            "POST",
            &format!("/api/skill-store/sources/{id}/archive"),
            &query,
            &raw,
        )
        .unwrap();
    assert_eq!(result.body["summary"]["added"], 1);
    let list = f.call("GET", "/api/skill-store/items", json!({})).body;
    let item = &list["items"][0];
    let item_id = item["id"].as_str().unwrap();
    let install=f.call("POST",&format!("/api/skill-store/items/{item_id}/install"),json!({"revision_id":item["revision_id"],"digest":item["digest"],"reviewed_source":true})).body;
    assert_eq!(install["skill"]["status"], "draft");
    let agent = f
        .call(
            "POST",
            "/api/agents",
            json!({"name":"Member","role":"developer"}),
        )
        .body;
    let request = json!({"revision_id":item["revision_id"],"agent_id":agent["id"],"expected_agent_version":1,"mode":"on_demand","parameters":{}});
    let path = format!("/api/skill-store/items/{item_id}/bind");
    assert_eq!(
        f.api
            .request(
                &f.user,
                "POST",
                &path,
                &BTreeMap::new(),
                &serde_json::to_vec(&request).unwrap()
            )
            .unwrap_err()
            .status,
        422
    );
    let skill = &install["skill"];
    f.call(
        "POST",
        &format!("/api/skills/{}/review", skill["id"].as_str().unwrap()),
        json!({"version":skill["version"],"digest":skill["digest"],"action":"approve"}),
    );
    let bound = f.call("POST", &path, request).body;
    assert_eq!(bound["version"], 2);
    assert_eq!(bound["skills"][0]["allow_scripts"], false);
    let mut changed = install["skill"]["files"].clone();
    changed["SKILL.md"] =
        json!("---\nname: catalog-skill\ndescription: Catalog test\n---\nLocal revised content.\n");
    f.call(
        "PUT",
        &format!("/api/skills/{}", skill["id"].as_str().unwrap()),
        json!({"expected_version":1,"files":changed}),
    );
    let detail = f
        .call(
            "GET",
            &format!("/api/skill-store/items/{item_id}"),
            json!({}),
        )
        .body;
    assert_eq!(detail["diff"][0]["path"], "SKILL.md");
    assert!(detail["diff"][0]["diff"]
        .as_str()
        .unwrap()
        .contains("-Local revised content."));
    assert!(detail["diff"][0]["diff"]
        .as_str()
        .unwrap()
        .contains("+Use reviewed evidence."));
}

#[test]
fn catalog_bulk_confirmation_detects_binding_changes() {
    let f = Fixture::new();
    let model = f.model();
    let pid = model["provider_id"].as_str().unwrap();
    let provider = f.call("GET", "/api/providers", json!({})).body[0].clone();
    let preview = json!({"revision_id":provider["revision_id"],"selections":[{"id":model["id"],"version":1}],"enabled":false});
    let review = f
        .call(
            "POST",
            &format!("/api/providers/{pid}/models/bulk-state/preview"),
            preview.clone(),
        )
        .body;
    f.call(
        "POST",
        "/api/agents",
        json!({"name":"New binding","role":"developer","model_profile_id":model["id"]}),
    );
    let mut input = preview;
    input["digest"] = review["digest"].clone();
    input["acknowledged"] = json!(true);
    let err = f
        .api
        .request(
            &f.user,
            "POST",
            &format!("/api/providers/{pid}/models/bulk-state"),
            &BTreeMap::new(),
            &serde_json::to_vec(&input).unwrap(),
        )
        .unwrap_err();
    assert_eq!(err.status, 409);
    assert_eq!(
        f.call("GET", "/api/models", json!({})).body[0]["enabled"],
        true
    );
}

#[test]
fn policy_edits_require_current_version_and_explicit_full_access_confirmation() {
    let f = Fixture::new();
    let a = f
        .call(
            "POST",
            "/api/agents",
            json!({"name":"Member","role":"developer"}),
        )
        .body;
    let path = format!(
        "/api/execution/policies/agent/{}",
        a["id"].as_str().unwrap()
    );
    let input = json!({"permission_profile":"full","expected_version":0,"expected_layout_ref":null,"full_access_confirmed":false});
    assert_eq!(
        f.api
            .request(
                &f.user,
                "PUT",
                &path,
                &BTreeMap::new(),
                &serde_json::to_vec(&input).unwrap()
            )
            .unwrap_err()
            .status,
        422
    );
    let mut input = input;
    input["full_access_confirmed"] = json!(true);
    let result = f.call("PUT", &path, input.clone()).body;
    assert_eq!(result["version"], 1);
    assert_eq!(result["agents"][0]["effective_profile"], "full");
    assert_eq!(
        f.api
            .request(
                &f.user,
                "PUT",
                &path,
                &BTreeMap::new(),
                &serde_json::to_vec(&input).unwrap()
            )
            .unwrap_err()
            .status,
        409
    );
}
