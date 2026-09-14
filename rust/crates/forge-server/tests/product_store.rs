use forge_server::{
    router_with_product_store, BackendError, ProductStore, PythonSqliteBackend, ServerConfig,
    PRODUCT_SCHEMA,
};
use reqwest::{Client, StatusCode};
use rusqlite::{params, Connection};
use serde_json::json;
use sha2::{Digest, Sha256};
use std::{
    fs,
    path::{Path, PathBuf},
    process::Command,
    sync::Arc,
    time::Duration,
};
use tokio::net::TcpListener;
use uuid::Uuid;

const PASSWORD: &str = "rust-product-test-password";

struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!("forge-product-{}", Uuid::new_v4()));
        fs::create_dir_all(&path).unwrap();
        Self(path)
    }
    fn path(&self, name: &str) -> PathBuf {
        self.0.join(name)
    }
    fn fresh(&self) -> ProductStore {
        ProductStore::open(self.path("product.sqlite"), None, Some(PASSWORD)).unwrap()
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[test]
fn bootstraps_compatible_admin_and_never_resets_password_on_restart() {
    let fixture = Fixture::new();
    let store = fixture.fresh();
    let admin = store.check_credentials("admin", PASSWORD).unwrap().unwrap();
    assert_eq!(admin.role, "admin");
    assert!(store.check_credentials("admin", "wrong").unwrap().is_none());
    let encoded: String = store
        .connect()
        .unwrap()
        .query_row("SELECT password_hash FROM users", [], |row| row.get(0))
        .unwrap();
    assert_eq!(encoded.len(), 108);
    assert!(!encoded.contains(PASSWORD));
    let reopened =
        ProductStore::open(store.path(), None, Some("different-password-value")).unwrap();
    assert_eq!(
        reopened
            .check_credentials("admin", PASSWORD)
            .unwrap()
            .unwrap(),
        admin
    );
    assert!(reopened
        .check_credentials("admin", "different-password-value")
        .unwrap()
        .is_none());
    assert!(matches!(
        ProductStore::open(fixture.path("missing-password.sqlite"), None, None),
        Err(BackendError::InvalidData)
    ));
    assert!(!fixture.path("missing-password.sqlite").exists());
}

#[test]
fn imports_real_python_schema_and_preserves_all_assets_history_and_ciphertext() {
    let fixture = Fixture::new();
    let legacy = fixture.path("legacy.sqlite");
    let manifest = Path::new(env!("CARGO_MANIFEST_DIR"));
    let root = manifest.ancestors().nth(3).unwrap();
    let python = root.join(".venv/bin/python");
    let output = Command::new(python)
        .arg(manifest.join("tests/fixtures/python_sqlite.py"))
        .arg(&legacy)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let source = Connection::open(&legacy).unwrap();
    source.execute("INSERT INTO provider_revisions VALUES('rev-1','provider-1','{}','opaque-encrypted-key','2026-09-12T00:00:00Z')",[]).unwrap();
    source
        .execute(
            "INSERT INTO providers VALUES('provider-1','Preserved provider','rev-1',1)",
            [],
        )
        .unwrap();
    source
        .execute(
            "INSERT INTO skills VALUES('skill-1','preserved-skill',1,1)",
            [],
        )
        .unwrap();
    source.execute("INSERT INTO skill_revisions VALUES('skill-1-v1','skill-1',1,'digest','{}','{\"SKILL.md\":\"contents\"}','published','admin-id','2026-09-12T00:00:00Z')",[]).unwrap();
    source.execute_batch("CREATE TABLE future_asset(id TEXT PRIMARY KEY,content BLOB); INSERT INTO future_asset VALUES('asset',X'00010203');").unwrap();
    let legacy_token = "legacy-session-0123456789012345678901234567890123456789";
    source.execute("INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?1,'member-id','2099-01-01T00:00:00+00:00')",[format!("{:x}",Sha256::digest(legacy_token.as_bytes()))]).unwrap();
    drop(source);
    let before = fs::read(&legacy).unwrap();
    let store = ProductStore::open(fixture.path("product.sqlite"), Some(&legacy), None).unwrap();
    assert_eq!(fs::read(&legacy).unwrap(), before);
    let db = store.connect().unwrap();
    assert_eq!(
        db.query_row("SELECT count(*) FROM runs", [], |row| row.get::<_, i64>(0))
            .unwrap(),
        4
    );
    assert_eq!(
        db.query_row("SELECT count(*) FROM events", [], |row| row
            .get::<_, i64>(0))
            .unwrap(),
        10
    );
    assert_eq!(
        db.query_row("SELECT encrypted_key FROM provider_revisions", [], |row| {
            row.get::<_, String>(0)
        })
        .unwrap(),
        "opaque-encrypted-key"
    );
    assert_eq!(
        db.query_row("SELECT content FROM future_asset", [], |row| row
            .get::<_, Vec<u8>>(0))
            .unwrap(),
        vec![0, 1, 2, 3]
    );
    let member = store
        .check_credentials("member", "fixture-password-\u{5bc6}\u{7801}-\u{e9}")
        .unwrap()
        .unwrap();
    assert_eq!(
        store.session_user(legacy_token).unwrap(),
        Some(member.clone())
    );
    store
        .authorize_project(&member, "project-shared", false)
        .unwrap();
    assert!(matches!(
        store.authorize_project(&member, "project-shared", true),
        Err(BackendError::Forbidden)
    ));
    db.execute(
        "UPDATE users SET username='renamed-member' WHERE id='member-id'",
        [],
    )
    .unwrap();
    drop(db);
    drop(store);
    let reopened = ProductStore::open(fixture.path("product.sqlite"), Some(&legacy), None).unwrap();
    assert_eq!(
        reopened.user("member-id").unwrap().unwrap().username,
        "renamed-member"
    );
    assert_eq!(fs::read(&legacy).unwrap(), before);
}

#[test]
fn imports_legacy_catalog_before_memory_and_runtime_approval_tables_existed() {
    let fixture = Fixture::new();
    let legacy = fixture.path("legacy.sqlite");
    let db = Connection::open(&legacy).unwrap();
    db.execute_batch(PRODUCT_SCHEMA).unwrap();
    db.execute_batch("DROP TABLE memory_contexts; DROP TABLE runtime_approvals;")
        .unwrap();
    db.execute(
        "INSERT INTO users VALUES('admin-id','admin',?1,'admin',1)",
        [ProductStore::hash_password(PASSWORD).unwrap()],
    )
    .unwrap();
    drop(db);
    let before = fs::read(&legacy).unwrap();
    let store = ProductStore::open(fixture.path("product.sqlite"), Some(&legacy), None).unwrap();
    assert_eq!(fs::read(&legacy).unwrap(), before);
    assert!(store
        .check_credentials("admin", PASSWORD)
        .unwrap()
        .is_some());
    for table in ["memory_contexts", "runtime_approvals"] {
        let count: i64 = store
            .connect()
            .unwrap()
            .query_row(&format!("SELECT count(*) FROM {table}"), [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(count, 0);
    }
}

#[test]
fn wal_backup_sees_committed_data_without_checkpointing_source() {
    let fixture = Fixture::new();
    let legacy = fixture.path("legacy.sqlite");
    let writer = Connection::open(&legacy).unwrap();
    writer.execute_batch(PRODUCT_SCHEMA).unwrap();
    writer.pragma_update(None, "journal_mode", "WAL").unwrap();
    writer.pragma_update(None, "wal_autocheckpoint", 0).unwrap();
    writer
        .execute(
            "INSERT INTO users VALUES('admin','admin',?1,'admin',1)",
            [ProductStore::hash_password(PASSWORD).unwrap()],
        )
        .unwrap();
    let database_before = fs::read(&legacy).unwrap();
    let wal_path = fixture.path("legacy.sqlite-wal");
    let wal_before = fs::read(&wal_path).unwrap();
    let store = ProductStore::open(fixture.path("product.sqlite"), Some(&legacy), None).unwrap();
    assert_eq!(store.user("admin").unwrap().unwrap().username, "admin");
    assert_eq!(fs::read(&legacy).unwrap(), database_before);
    assert_eq!(fs::read(wal_path).unwrap(), wal_before);
}

#[test]
fn rejects_source_alias_incompatible_schema_and_invalid_json_atomically() {
    let fixture = Fixture::new();
    let legacy = fixture.path("legacy.sqlite");
    let db = Connection::open(&legacy).unwrap();
    db.execute_batch(PRODUCT_SCHEMA).unwrap();
    db.execute(
        "INSERT INTO platform_config VALUES('invalid','not-json')",
        [],
    )
    .unwrap();
    drop(db);
    let before = fs::read(&legacy).unwrap();
    assert!(ProductStore::open(&legacy, Some(&legacy), Some(PASSWORD)).is_err());
    assert!(matches!(
        ProductStore::open(
            fixture.path("product.sqlite"),
            Some(&legacy),
            Some(PASSWORD)
        ),
        Err(BackendError::InvalidData)
    ));
    assert!(!fixture.path("product.sqlite").exists());
    assert_eq!(fs::read(&legacy).unwrap(), before);
    let alias = fixture.path("alias.sqlite");
    fs::hard_link(&legacy, &alias).unwrap();
    assert!(matches!(
        ProductStore::open(alias, Some(&legacy), Some(PASSWORD)),
        Err(BackendError::InvalidData)
    ));
    let db = Connection::open(&legacy).unwrap();
    db.execute("DELETE FROM platform_config", []).unwrap();
    db.execute_batch("DROP TABLE skill_revisions").unwrap();
    drop(db);
    assert!(matches!(
        ProductStore::open(
            fixture.path("product.sqlite"),
            Some(&legacy),
            Some(PASSWORD)
        ),
        Err(BackendError::InvalidData)
    ));
    assert!(!fixture.path("product.sqlite").exists());
}

#[test]
fn imported_session_and_new_sessions_expire_revoke_and_follow_live_account() {
    let fixture = Fixture::new();
    let store = fixture.fresh();
    let user = store.check_credentials("admin", PASSWORD).unwrap().unwrap();
    let token = "0123456789012345678901234567890123456789012345678901234567890123";
    store
        .create_session(&user.id, token, Duration::from_secs(300))
        .unwrap();
    assert_eq!(store.session_user(token).unwrap(), Some(user.clone()));
    let db = store.connect().unwrap();
    let persisted: String = db
        .query_row("SELECT token_hash FROM sessions", [], |row| row.get(0))
        .unwrap();
    assert_ne!(persisted, token);
    assert_eq!(persisted.len(), 64);
    let reopened = ProductStore::open(store.path(), None, None).unwrap();
    assert_eq!(reopened.session_user(token).unwrap(), Some(user.clone()));
    db.execute("UPDATE users SET enabled=0 WHERE id=?1", [&user.id])
        .unwrap();
    assert!(reopened.session_user(token).unwrap().is_none());
    db.execute("UPDATE users SET enabled=1 WHERE id=?1", [&user.id])
        .unwrap();
    db.execute(
        "UPDATE sessions SET expires_at='2000-01-01T00:00:00+00:00'",
        [],
    )
    .unwrap();
    assert!(reopened.session_user(token).unwrap().is_none());
    reopened
        .create_session(&user.id, token, Duration::from_secs(300))
        .unwrap();
    reopened.revoke_session(token).unwrap();
    assert!(reopened.session_user(token).unwrap().is_none());
    reopened
        .create_session(&user.id, token, Duration::from_secs(300))
        .unwrap();
    reopened.revoke_user_sessions(&user.id).unwrap();
    assert!(reopened.session_user(token).unwrap().is_none());
}

#[test]
fn fernet_round_trip_remains_python_compatible_and_never_accepts_wrong_key() {
    let fixture = Fixture::new();
    let key = fernet::Fernet::generate_key();
    let store = fixture.fresh().with_encryption_key(&key).unwrap();
    let encrypted = store.encrypt_secret("provider-key-value").unwrap();
    assert_ne!(encrypted, "provider-key-value");
    assert_eq!(
        store.decrypt_secret(&encrypted).unwrap(),
        "provider-key-value"
    );
    let manifest = Path::new(env!("CARGO_MANIFEST_DIR"));
    let root = manifest.ancestors().nth(3).unwrap();
    let output=Command::new(root.join(".venv/bin/python")).arg("-c").arg("from cryptography.fernet import Fernet; import sys; assert Fernet(sys.argv[1]).decrypt(sys.argv[2].encode()) == b'provider-key-value'").arg(key).arg(encrypted).output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let token = store.encrypt_secret("another").unwrap();
    assert!(store
        .with_encryption_key(&fernet::Fernet::generate_key())
        .unwrap()
        .decrypt_secret(&token)
        .is_err());
}

async fn serve(store: Arc<ProductStore>) -> (String, tokio::task::JoinHandle<()>) {
    let backend = Arc::new(PythonSqliteBackend::open(store.path()).unwrap());
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let router = router_with_product_store(backend, ServerConfig::default(), store);
    let task = tokio::spawn(async move {
        axum::serve(listener, router).await.unwrap();
    });
    (format!("http://{address}"), task)
}

#[tokio::test]
async fn cookie_survives_http_host_restart_and_logout_is_durable() {
    let fixture = Fixture::new();
    let store = Arc::new(fixture.fresh());
    let client = Client::new();
    let (url, task) = serve(store.clone()).await;
    let response = client
        .post(format!("{url}/api/auth/login"))
        .header("x-forge-request", "1")
        .json(&json!({"username":"admin","password":PASSWORD}))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let cookie = response
        .headers()
        .get("set-cookie")
        .unwrap()
        .to_str()
        .unwrap()
        .split(';')
        .next()
        .unwrap()
        .to_owned();
    task.abort();
    let (url, task) = serve(store.clone()).await;
    assert_eq!(
        client
            .get(format!("{url}/api/auth/me"))
            .header("cookie", &cookie)
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::OK
    );
    assert_eq!(
        client
            .post(format!("{url}/api/auth/logout"))
            .header("cookie", &cookie)
            .header("x-forge-request", "1")
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::OK
    );
    task.abort();
    let (url, task) = serve(store).await;
    assert_eq!(
        client
            .get(format!("{url}/api/auth/me"))
            .header("cookie", &cookie)
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::UNAUTHORIZED
    );
    task.abort();
}

#[test]
fn only_one_concurrent_initializer_publishes_and_user_writes_survive_reopen() {
    let fixture = Fixture::new();
    let path = fixture.path("product.sqlite");
    let threads: Vec<_> = (0..3)
        .map(|_| {
            let path = path.clone();
            std::thread::spawn(move || ProductStore::open(path, None, Some(PASSWORD)))
        })
        .collect();
    for thread in threads {
        thread.join().unwrap().unwrap();
    }
    let store = ProductStore::open(path, None, None).unwrap();
    assert_eq!(
        store
            .connect()
            .unwrap()
            .query_row("SELECT count(*) FROM users", [], |row| row.get::<_, i64>(0))
            .unwrap(),
        1
    );
    store
        .connect()
        .unwrap()
        .execute(
            "INSERT INTO platform_config VALUES(?1,?2)",
            params!["persisted", "{}"],
        )
        .unwrap();
    drop(store);
    assert!(
        ProductStore::open(fixture.path("product.sqlite"), None, None)
            .unwrap()
            .connect()
            .unwrap()
            .query_row(
                "SELECT value FROM platform_config WHERE id='persisted'",
                [],
                |row| row.get::<_, String>(0)
            )
            .is_ok()
    );
}

#[test]
fn product_schema_v1_adds_delayed_module_tables_in_one_upgrade() {
    let fixture = Fixture::new();
    let path = fixture.path("product.sqlite");
    let db = Connection::open(&path).unwrap();
    db.execute_batch(include_str!("../src/product_store/schema.sql"))
        .unwrap();
    db.execute_batch("CREATE TABLE forge_product_meta(id INTEGER PRIMARY KEY,owner TEXT NOT NULL,version INTEGER NOT NULL,source_path TEXT,imported_at TEXT NOT NULL); INSERT INTO forge_product_meta VALUES(1,'forge-rust-product',1,NULL,'2026-09-12');").unwrap();
    db.execute(
        "INSERT INTO users VALUES('admin','admin',?1,'admin',1)",
        [ProductStore::hash_password(PASSWORD).unwrap()],
    )
    .unwrap();
    drop(db);
    let store = ProductStore::open(&path, None, None).unwrap();
    let db = store.connect().unwrap();
    assert_eq!(
        db.query_row("SELECT version FROM forge_product_meta", [], |r| r
            .get::<_, i64>(0))
            .unwrap(),
        3
    );
    for table in [
        "design_assets",
        "skill_store_sources",
        "model_benchmarks",
        "provider_model_catalogs",
        "tool_installations",
        "execution_permissions",
        "forge_team_runs",
        "forge_team_decision_callbacks",
    ] {
        assert_eq!(
            db.query_row(
                "SELECT count(*) FROM sqlite_schema WHERE type='table' AND name=?1",
                [table],
                |r| r.get::<_, i64>(0)
            )
            .unwrap(),
            1
        );
    }
    assert!(store
        .check_credentials("admin", PASSWORD)
        .unwrap()
        .is_some());
}

#[test]
fn product_schema_v2_adds_team_tables_and_is_idempotent() {
    let fixture = Fixture::new();
    let path = fixture.path("product.sqlite");
    let db = Connection::open(&path).unwrap();
    // Simulate a database written by the previous Rust owner version before
    // the team tables were added to the extension catalog.
    db.execute_batch(include_str!("../src/product_store/schema.sql"))
        .unwrap();
    db.execute_batch(include_str!("../src/product_store/extensions.sql"))
        .unwrap();
    db.execute_batch(
        "DROP TABLE forge_team_decision_callbacks;
         DROP TABLE forge_team_runs;
         CREATE TABLE forge_product_meta(id INTEGER PRIMARY KEY CHECK(id=1), owner TEXT NOT NULL, version INTEGER NOT NULL, source_path TEXT, imported_at TEXT NOT NULL);
         INSERT INTO forge_product_meta VALUES(1, 'forge-rust-product', 2, NULL, '2026-09-12');",
    )
    .unwrap();
    db.execute(
        "INSERT INTO users VALUES('admin','admin',?1,'admin',1)",
        [ProductStore::hash_password(PASSWORD).unwrap()],
    )
    .unwrap();
    drop(db);

    let store = ProductStore::open(&path, None, None).unwrap();
    let db = store.connect().unwrap();
    assert_eq!(
        db.query_row("SELECT version FROM forge_product_meta", [], |r| r
            .get::<_, i64>(0))
            .unwrap(),
        3
    );
    for table in ["forge_team_runs", "forge_team_decision_callbacks"] {
        assert_eq!(
            db.query_row(
                "SELECT count(*) FROM sqlite_schema WHERE type='table' AND name=?1",
                [table],
                |r| r.get::<_, i64>(0),
            )
            .unwrap(),
            1
        );
    }
    drop(db);

    // Reopening must not fail or duplicate anything after the upgrade.
    let reopened = ProductStore::open(&path, None, None).unwrap();
    assert_eq!(
        reopened
            .connect()
            .unwrap()
            .query_row("SELECT version FROM forge_product_meta", [], |r| r
                .get::<_, i64>(0))
            .unwrap(),
        3
    );
}

#[test]
fn product_schema_v2_upgrade_rolls_back_when_validation_fails() {
    let fixture = Fixture::new();
    let path = fixture.path("product.sqlite");
    let db = Connection::open(&path).unwrap();
    db.execute_batch(include_str!("../src/product_store/schema.sql"))
        .unwrap();
    db.execute_batch(include_str!("../src/product_store/extensions.sql"))
        .unwrap();
    db.execute_batch(
        "DROP TABLE forge_team_decision_callbacks;
         DROP TABLE forge_team_runs;
         DROP TABLE users;
         CREATE TABLE forge_product_meta(id INTEGER PRIMARY KEY CHECK(id=1), owner TEXT NOT NULL, version INTEGER NOT NULL, source_path TEXT, imported_at TEXT NOT NULL);
         INSERT INTO forge_product_meta VALUES(1, 'forge-rust-product', 2, NULL, '2026-09-12');",
    )
    .unwrap();
    drop(db);

    assert!(matches!(
        ProductStore::open(&path, None, None),
        Err(BackendError::InvalidData)
    ));
    let db = Connection::open(&path).unwrap();
    assert_eq!(
        db.query_row("SELECT version FROM forge_product_meta", [], |r| r
            .get::<_, i64>(0))
            .unwrap(),
        2
    );
    for table in ["forge_team_runs", "forge_team_decision_callbacks"] {
        assert_eq!(
            db.query_row(
                "SELECT count(*) FROM sqlite_schema WHERE type='table' AND name=?1",
                [table],
                |r| r.get::<_, i64>(0),
            )
            .unwrap(),
            0
        );
    }
}

#[test]
fn product_schema_future_version_is_rejected_without_mutation() {
    let fixture = Fixture::new();
    let path = fixture.path("product.sqlite");
    let db = Connection::open(&path).unwrap();
    db.execute_batch(include_str!("../src/product_store/schema.sql"))
        .unwrap();
    db.execute_batch(include_str!("../src/product_store/extensions.sql"))
        .unwrap();
    db.execute_batch(
        "CREATE TABLE forge_product_meta(id INTEGER PRIMARY KEY CHECK(id=1), owner TEXT NOT NULL, version INTEGER NOT NULL, source_path TEXT, imported_at TEXT NOT NULL);
         INSERT INTO forge_product_meta VALUES(1, 'forge-rust-product', 4, NULL, '2026-09-12');",
    )
    .unwrap();
    drop(db);

    assert!(matches!(
        ProductStore::open(&path, None, None),
        Err(BackendError::InvalidData)
    ));
    let db = Connection::open(&path).unwrap();
    assert_eq!(
        db.query_row("SELECT version FROM forge_product_meta", [], |r| r
            .get::<_, i64>(0))
            .unwrap(),
        4
    );
}
