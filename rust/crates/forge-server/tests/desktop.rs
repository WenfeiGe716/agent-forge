use forge_server::desktop_launch::{prepare, DesktopInput};
use reqwest::StatusCode;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::Write,
    path::PathBuf,
    process::{Child, Command, Stdio},
    time::Duration,
};

struct Workspace {
    root: PathBuf,
    child: Option<Child>,
}
impl Workspace {
    fn new() -> Self {
        let root =
            std::env::temp_dir().join(format!("forge-desktop-{}", uuid::Uuid::new_v4().simple()));
        fs::create_dir(&root).unwrap();
        Self {
            root: root.canonicalize().unwrap(),
            child: None,
        }
    }
}
impl Drop for Workspace {
    fn drop(&mut self) {
        if let Some(child) = &mut self.child {
            let _ = child.kill();
            let _ = child.wait();
        }
        let _ = fs::remove_dir_all(&self.root);
    }
}
fn input(value: Value) -> DesktopInput {
    serde_json::from_value(value).unwrap()
}

#[test]
fn preparation_is_staged_private_and_never_replaces_existing_workspace() {
    let ws = Workspace::new();
    let destination = ws.root.join("workspace");
    let request = input(json!({"action":"prepare","data_dir":destination}));
    let prepared = prepare(&request).unwrap();
    let staging = PathBuf::from(prepared["staging_dir"].as_str().unwrap());
    assert!(!destination.exists());
    assert!(staging.join("product.sqlite3").is_file());
    assert!(staging.join("config.json").is_file());
    assert_eq!(prepared["migrated"], false);
    assert!(!staging.join("secrets.enc").exists());
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            fs::metadata(&staging).unwrap().permissions().mode() & 0o777,
            0o700
        );
    }
    fs::rename(&staging, &destination).unwrap();
    let before = fs::read(destination.join("product.sqlite3")).unwrap();
    assert!(prepare(&request).is_err());
    assert_eq!(
        before,
        fs::read(destination.join("product.sqlite3")).unwrap()
    );
}

#[tokio::test]
async fn native_process_authenticates_identity_session_and_directory_tickets() {
    let mut ws = Workspace::new();
    let destination = ws.root.join("workspace");
    let prepared = prepare(&input(json!({"action":"prepare","data_dir":destination}))).unwrap();
    let staging = PathBuf::from(prepared["staging_dir"].as_str().unwrap());
    fs::write(staging.join("secrets.enc"), b"test keychain sentinel").unwrap();
    fs::rename(staging, &destination).unwrap();
    let socket = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = socket.local_addr().unwrap().port();
    drop(socket);
    let token = "desktop-contract-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    let control = "native-contract-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
    let mut child = Command::new(env!("CARGO_BIN_EXE_forge-server"))
        .arg("--desktop")
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    child.stdin.take().unwrap().write_all(format!("{}\n",json!({"action":"serve","data_dir":destination,"port":port,"desktop_token":token,"control_token":control,"secrets":prepared["secrets"]})).as_bytes()).unwrap();
    ws.child = Some(child);
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(2))
        .no_proxy()
        .build()
        .unwrap();
    let base = format!("http://127.0.0.1:{port}");
    let mut ready = false;
    for _ in 0..100 {
        assert!(
            ws.child.as_mut().unwrap().try_wait().unwrap().is_none(),
            "desktop exited before ready"
        );
        if let Ok(r) = client
            .get(format!("{base}/api/health"))
            .header("x-forge-desktop", token)
            .send()
            .await
        {
            if r.status() == StatusCode::OK {
                let v: Value = r.json().await.unwrap();
                assert_eq!(
                    v["desktop_instance"],
                    format!("{:x}", Sha256::digest(control.as_bytes()))
                );
                ready = true;
                break;
            }
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    assert!(ready);
    assert_eq!(
        client
            .get(format!("{base}/api/health"))
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::FORBIDDEN
    );
    assert_eq!(
        client
            .get(format!("{base}/app.js"))
            .header("x-forge-desktop", token)
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::OK
    );
    let request = || {
        client
            .post(format!("{base}/api/desktop/session"))
            .header("x-forge-desktop", token)
            .header("x-forge-request", "1")
    };
    assert_eq!(
        request().send().await.unwrap().status(),
        StatusCode::FORBIDDEN
    );
    assert_eq!(
        request()
            .header("x-forge-desktop-control", control)
            .header("origin", &base)
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::FORBIDDEN
    );
    let r = request()
        .header("x-forge-desktop-control", control)
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), StatusCode::OK);
    let cookie = r.headers()["set-cookie"]
        .to_str()
        .unwrap()
        .split(';')
        .next()
        .unwrap()
        .to_owned();
    let me: Value = client
        .get(format!("{base}/api/auth/me"))
        .header("x-forge-desktop", token)
        .header("cookie", &cookie)
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(me["role"], "admin");
    let status = client
        .get(format!("{base}/api/desktop/status"))
        .header("x-forge-desktop", token)
        .header("x-forge-desktop-control", control)
        .send()
        .await
        .unwrap();
    assert_eq!(status.status(), StatusCode::OK);
    assert_eq!(status.json::<Value>().await.unwrap()["active_runs"], 0);
    let directory = ws.root.join("project");
    fs::create_dir(&directory).unwrap();
    fs::write(directory.join("README.md"), b"project").unwrap();
    fs::write(directory.join(".env"), b"PRIVATE=excluded").unwrap();
    let grant = client
        .post(format!("{base}/api/desktop/authorize-directory"))
        .header("x-forge-desktop", token)
        .header("x-forge-desktop-control", control)
        .header("x-forge-request", "1")
        .json(&json!({"path":directory}))
        .send()
        .await
        .unwrap();
    assert_eq!(grant.status(), StatusCode::OK);
    let grant: Value = grant.json().await.unwrap();
    assert!(grant["ticket"].as_str().unwrap().len() >= 32);
    let imported=client.post(format!("{base}/api/projects")).header("x-forge-desktop",token).header("x-forge-request","1").header("cookie",&cookie).json(&json!({"name":"Desktop project","import_path":grant["name"],"desktop_directory_token":grant["ticket"],"execution_mode":"local","permission_profile":"ask"})).send().await.unwrap();
    let imported_status = imported.status();
    let project: Value = imported.json().await.unwrap();
    assert_eq!(imported_status, StatusCode::CREATED, "{project}");
    let files = destination
        .join("data/projects")
        .join(project["id"].as_str().unwrap());
    assert_eq!(fs::read(files.join("README.md")).unwrap(), b"project");
    assert!(!files.join(".env").exists());
    let forbidden = client
        .post(format!("{base}/api/desktop/authorize-directory"))
        .header("x-forge-desktop", token)
        .header("x-forge-desktop-control", control)
        .header("x-forge-request", "1")
        .json(&json!({"path":destination}))
        .send()
        .await
        .unwrap();
    assert_eq!(forbidden.status(), StatusCode::UNPROCESSABLE_ENTITY);
    #[cfg(unix)]
    {
        unsafe {
            libc::kill(ws.child.as_ref().unwrap().id() as i32, libc::SIGTERM);
        }
    }
    for _ in 0..100 {
        if ws.child.as_mut().unwrap().try_wait().unwrap().is_some() {
            return;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("desktop failed graceful shutdown");
}

#[test]
fn directory_tickets_are_single_use_and_detect_inode_replacement() {
    let ws = Workspace::new();
    let source = ws.root.join("selected");
    fs::create_dir(&source).unwrap();
    let tickets = forge_server::desktop::DirectoryTickets::new(vec![ws.root.join("protected")]);
    let issued = tickets.issue(source.to_str().unwrap()).unwrap();
    let token = issued["ticket"].as_str().unwrap();
    assert_eq!(tickets.consume(token).unwrap().path, source);
    assert!(tickets.consume(token).is_err());
    let issued = tickets.issue(source.to_str().unwrap()).unwrap();
    fs::rename(&source, ws.root.join("original")).unwrap();
    fs::create_dir(&source).unwrap();
    assert!(tickets.consume(issued["ticket"].as_str().unwrap()).is_err());
}

#[test]
fn legacy_sqlite_preparation_copies_data_and_keeps_source_bytes_unchanged() {
    let ws = Workspace::new();
    let source = ws.root.join("legacy");
    fs::create_dir(&source).unwrap();
    fs::create_dir(source.join(".local")).unwrap();
    let generated = prepare(&input(
        json!({"action":"prepare","data_dir":ws.root.join("keys-only")}),
    ))
    .unwrap();
    let db_path = source.join("forge.db");
    let db = rusqlite::Connection::open(&db_path).unwrap();
    db.execute_batch(forge_server::PRODUCT_SCHEMA).unwrap();
    let hash = forge_server::ProductStore::hash_password("migration-password-123").unwrap();
    db.execute("INSERT INTO users(id,username,password_hash,role,enabled) VALUES('admin','admin',?1,'admin',1)",[hash]).unwrap();
    drop(db);
    let data = source.join("data");
    let imports = source.join("imports");
    fs::create_dir(&data).unwrap();
    fs::create_dir(&imports).unwrap();
    fs::write(data.join("artifact.txt"), b"retained result").unwrap();
    fs::write(imports.join("README.md"), b"project source").unwrap();
    let mut config = generated["secrets"].clone();
    config["DATABASE_URL"] = json!(format!("sqlite:///{}", db_path.display()));
    config["DATA_ROOT"] = json!(data);
    config["IMPORT_ROOT"] = json!(imports);
    let configuration = source.join(".local/config.json");
    fs::write(&configuration, config.to_string()).unwrap();
    let before = fs::read(&db_path).unwrap();
    let config_before = fs::read(&configuration).unwrap();
    let result = prepare(&input(
        json!({"action":"prepare","data_dir":ws.root.join("destination"),"import_from":source}),
    ))
    .unwrap();
    let copied = PathBuf::from(result["staging_dir"].as_str().unwrap());
    assert_eq!(result["migrated"], true);
    assert_eq!(
        fs::read(copied.join("data/artifact.txt")).unwrap(),
        b"retained result"
    );
    assert_eq!(before, fs::read(db_path).unwrap());
    assert_eq!(config_before, fs::read(configuration).unwrap());
    assert!(copied.join("product.sqlite3").is_file());
}
