use forge_runtime::{CodexClient, RequestId, RuntimeConfig, RuntimeError, TurnTerminalStatus};
use serde_json::json;
use std::{path::PathBuf, time::Duration};
use tempfile::TempDir;

const DEADLINE: Duration = Duration::from_secs(5);

fn config(root: &TempDir) -> RuntimeConfig {
    RuntimeConfig::new(
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/app_server.py"),
        root.path().to_path_buf(),
        root.path().join("runtime"),
    )
}

async fn start(root: &TempDir) -> CodexClient {
    CodexClient::start(config(root), DEADLINE).await.unwrap().0
}

#[tokio::test]
async fn isolates_home_credentials_and_keychain_configuration() {
    let root = TempDir::new().unwrap();
    let mut config = config(&root);
    config
        .forge_env
        .insert("FORGE_MODEL_KEY".into(), "explicit-test-key".into());
    let (client, initialized) = CodexClient::start(config, DEADLINE).await.unwrap();
    let home = root.path().join("runtime").canonicalize().unwrap();
    assert_eq!(initialized["codexHome"], home.to_str().unwrap());
    let result = client
        .request("environment", json!({}), DEADLINE)
        .await
        .unwrap();
    let env = &result["environment"];
    assert_eq!(env["CODEX_HOME"], home.to_str().unwrap());
    assert_eq!(env["HOME"], home.join("user-home").to_str().unwrap());
    assert_eq!(env["USERPROFILE"], env["HOME"]);
    assert_eq!(env["FORGE_MODEL_KEY"], "explicit-test-key");
    assert!(env.get("OPENAI_API_KEY").is_none());
    assert!(env.get("CODEX_INTERNAL_ORIGINATOR_OVERRIDE").is_none());
    assert_eq!(
        result["args"],
        json!([
            "-c",
            "cli_auth_credentials_store=\"file\"",
            "-c",
            "mcp_oauth_credentials_store=\"file\"",
            "app-server",
            "--stdio"
        ])
    );
    client.close().await;
}

#[tokio::test]
async fn rejects_untrusted_paths_and_environment_before_spawning() {
    let root = TempDir::new().unwrap();
    let mut relative = config(&root);
    relative.executable = PathBuf::from("codex");
    assert!(matches!(
        CodexClient::start(relative, DEADLINE).await,
        Err(RuntimeError::Configuration(_))
    ));
    let mut personal = config(&root);
    personal.runtime_home = PathBuf::from(std::env::var_os("HOME").unwrap()).join(".codex");
    assert!(matches!(
        CodexClient::start(personal, DEADLINE).await,
        Err(RuntimeError::Configuration(_))
    ));
    let mut ambient = config(&root);
    ambient
        .forge_env
        .insert("OPENAI_API_KEY".into(), "forbidden".into());
    assert!(matches!(
        CodexClient::start(ambient, DEADLINE).await,
        Err(RuntimeError::Configuration(_))
    ));
    assert!(!root.path().join("runtime").exists());
}

#[cfg(unix)]
#[tokio::test]
async fn rejects_dangling_config_and_escaping_skill_symlinks() {
    use std::os::unix::fs::symlink;
    for name in ["config.toml", "auth.json", "user-home"] {
        let root = TempDir::new().unwrap();
        std::fs::create_dir(root.path().join("runtime")).unwrap();
        symlink(
            root.path().join("outside-missing"),
            root.path().join("runtime").join(name),
        )
        .unwrap();
        assert!(matches!(
            CodexClient::start(config(&root), DEADLINE).await,
            Err(RuntimeError::Configuration(_))
        ));
    }
    let root = TempDir::new().unwrap();
    std::fs::create_dir_all(root.path().join("runtime/user-home")).unwrap();
    symlink(root.path(), root.path().join("runtime/user-home/.agents")).unwrap();
    assert!(matches!(
        CodexClient::start(config(&root), DEADLINE).await,
        Err(RuntimeError::Configuration(_))
    ));
}

#[tokio::test]
async fn validates_initialized_home() {
    let root = TempDir::new().unwrap();
    let mut config = config(&root);
    config
        .forge_env
        .insert("FORGE_FIXTURE_MODE".into(), "wrong_home".into());
    assert!(matches!(
        CodexClient::start(config, DEADLINE).await,
        Err(RuntimeError::Protocol(_))
    ));
}

#[tokio::test]
async fn startup_timeout_is_bounded() {
    let root = TempDir::new().unwrap();
    let mut config = config(&root);
    config
        .forge_env
        .insert("FORGE_FIXTURE_MODE".into(), "initialize_timeout".into());
    let result = tokio::time::timeout(
        DEADLINE,
        CodexClient::start(config, Duration::from_millis(50)),
    )
    .await
    .expect("startup must clean up without waiting for the child to finish sleeping");
    assert!(matches!(result, Err(RuntimeError::RequestTimeout(method)) if method == "initialize"));
}

#[cfg(unix)]
#[tokio::test]
async fn dropping_a_client_reaps_its_process() {
    let root = TempDir::new().unwrap();
    let client = start(&root).await;
    let pid = client.process_id().unwrap();
    drop(client);
    tokio::time::timeout(DEADLINE, async {
        loop {
            let status = std::process::Command::new("/bin/kill")
                .args(["-0", &pid.to_string()])
                .stderr(std::process::Stdio::null())
                .status()
                .unwrap();
            if !status.success() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .expect("dropping a client must schedule child cleanup");
}

#[tokio::test]
async fn correlates_concurrent_replies_arriving_in_reverse_order() {
    let root = TempDir::new().unwrap();
    let client = start(&root).await;
    let (first, second) = tokio::join!(
        client.request("reverse", json!({"order": 1}), DEADLINE),
        client.request("reverse", json!({"order": 2}), DEADLINE)
    );
    assert_eq!(first.unwrap()["order"], 1);
    assert_eq!(second.unwrap()["order"], 2);
    client.close().await;
}

#[tokio::test]
async fn preserves_approval_requests_without_automatically_answering() {
    let root = TempDir::new().unwrap();
    let client = start(&root).await;
    client
        .request("approvals", json!({}), DEADLINE)
        .await
        .unwrap();
    let approval = client.next_event(DEADLINE).await.unwrap();
    assert_eq!(approval.sequence, 1);
    assert_eq!(approval.thread_id.as_deref(), Some("thread-1"));
    assert_eq!(approval.turn_id.as_deref(), Some("turn-1"));
    assert_eq!(approval.item_id.as_deref(), Some("item-1"));
    let id = approval.request_id.unwrap();
    assert_eq!(id, RequestId::String("approval-1".into()));
    let unsupported = client.next_event(DEADLINE).await.unwrap();
    assert_eq!(unsupported.payload, json!({"unknown": {"preserved": true}}));
    assert_eq!(
        client.next_event(Duration::from_millis(30)).await,
        Err(RuntimeError::EventTimeout)
    );
    client
        .respond(&id, json!({"decision": "decline"}), DEADLINE)
        .await
        .unwrap();
    let answered = client.next_event(DEADLINE).await.unwrap();
    assert_eq!(
        answered.payload["answer"],
        json!({"id": "approval-1", "result": {"decision": "decline"}})
    );
    assert_eq!(
        client.respond(&id, json!({}), DEADLINE).await,
        Err(RuntimeError::UnknownServerRequest)
    );
    client
        .reject(&unsupported.request_id.unwrap(), -32601, DEADLINE)
        .await
        .unwrap();
    assert_eq!(
        client.next_event(DEADLINE).await.unwrap().payload["answer"]["error"]["code"],
        -32601
    );
    client.close().await;
}

#[tokio::test]
async fn request_timeout_closes_session_and_reaps_child() {
    let root = TempDir::new().unwrap();
    let client = start(&root).await;
    let pid = client.process_id().unwrap();
    assert_eq!(
        client
            .request("wait", json!({}), Duration::from_millis(50))
            .await,
        Err(RuntimeError::RequestTimeout("wait".into()))
    );
    client.close().await;
    assert!(client.request("after", json!({}), DEADLINE).await.is_err());
    #[cfg(unix)]
    {
        let status = std::process::Command::new("/bin/kill")
            .args(["-0", &pid.to_string()])
            .stderr(std::process::Stdio::null())
            .status()
            .unwrap();
        assert!(!status.success());
    }
}

#[tokio::test]
async fn cancellation_fails_closed_even_if_a_remote_request_was_written() {
    let root = TempDir::new().unwrap();
    let client = start(&root).await;
    assert!(tokio::time::timeout(
        Duration::from_millis(30),
        client.request("wait", json!({}), DEADLINE)
    )
    .await
    .is_err());
    assert_eq!(
        client.request("after", json!({}), DEADLINE).await,
        Err(RuntimeError::Cancelled)
    );
    client.close().await;
}

#[tokio::test]
async fn eof_is_an_error_and_explicit_completion_is_preserved() {
    let root = TempDir::new().unwrap();
    let client = start(&root).await;
    assert_eq!(
        client.request("eof", json!({}), DEADLINE).await,
        Err(RuntimeError::Closed)
    );
    assert_eq!(client.next_event(DEADLINE).await, Err(RuntimeError::Closed));
    client.close().await;
    let root = TempDir::new().unwrap();
    let client = start(&root).await;
    client
        .request("terminal_then_eof", json!({}), DEADLINE)
        .await
        .unwrap();
    let mut terminal = client.next_event(DEADLINE).await.unwrap();
    assert_eq!(
        terminal.terminal_status(),
        Some(TurnTerminalStatus::Completed)
    );
    terminal.payload = json!({"turn": {"status": "something-new"}});
    assert_eq!(terminal.terminal_status(), Some(TurnTerminalStatus::Failed));
    terminal.kind = "some/completed".into();
    assert_eq!(terminal.terminal_status(), None);
    assert_eq!(client.next_event(DEADLINE).await, Err(RuntimeError::Closed));
    client.close().await;
}

#[tokio::test]
async fn rejects_corrupt_frames_duplicate_approvals_and_silent_event_loss() {
    for (method, expected) in [
        ("invalid", RuntimeError::Protocol("invalid JSON frame")),
        (
            "duplicate_approval",
            RuntimeError::Protocol("duplicate active server request id"),
        ),
        ("flood", RuntimeError::Backlog),
        (
            "oversized",
            RuntimeError::Protocol("response frame too large"),
        ),
    ] {
        let root = TempDir::new().unwrap();
        let client = start(&root).await;
        assert_eq!(
            client.request(method, json!({}), DEADLINE).await,
            Err(expected)
        );
        client.close().await;
    }
}

#[tokio::test]
async fn supports_history_sized_frames_and_redacts_remote_errors() {
    let root = TempDir::new().unwrap();
    let client = start(&root).await;
    let result = client.request("large", json!({}), DEADLINE).await.unwrap();
    assert_eq!(result["body"].as_str().unwrap().len(), 100000);
    let error = client
        .request("error", json!({}), DEADLINE)
        .await
        .unwrap_err();
    assert_eq!(error, RuntimeError::Remote { code: Some(-32001) });
    assert!(!format!("{error:?} {error}").contains("secret-provider-token"));
    assert!(client.request("healthy", json!({}), DEADLINE).await.is_ok());
    client.close().await;
}

#[tokio::test]
#[ignore = "requires explicit FORGE_TEST_CODEX_BINARY; isolated initialize only, no model calls"]
async fn real_codex_initialize() {
    let root = TempDir::new().unwrap();
    let binary = std::env::var_os("FORGE_TEST_CODEX_BINARY")
        .expect("set an absolute project-owned Codex binary");
    let config = RuntimeConfig::new(
        binary.into(),
        root.path().to_path_buf(),
        root.path().join("runtime"),
    );
    let (client, result) = CodexClient::start(config, Duration::from_secs(20))
        .await
        .unwrap();
    assert_eq!(
        result["codexHome"],
        root.path()
            .join("runtime")
            .canonicalize()
            .unwrap()
            .to_str()
            .unwrap()
    );
    client.close().await;
}

#[tokio::test]
#[ignore = "requires explicit FORGE_TEST_CODEX_BINARY; isolated skill discovery, no model calls"]
async fn real_codex_discovers_isolated_skills() {
    let root = TempDir::new().unwrap();
    let binary = std::env::var_os("FORGE_TEST_CODEX_BINARY")
        .expect("set an absolute project-owned Codex binary");
    let runtime_home = root.path().join("runtime");
    let skill = runtime_home.join("skills/forge-discovery-test");
    std::fs::create_dir_all(&skill).unwrap();
    std::fs::write(skill.join("SKILL.md"), "---\nname: forge-discovery-test\ndescription: Verify isolated Forge skill discovery\n---\nInspect evidence before accepting a task.\n").unwrap();
    let config = RuntimeConfig::new(binary.into(), root.path().to_path_buf(), runtime_home);
    let (client, _) = CodexClient::start(config, Duration::from_secs(20))
        .await
        .unwrap();
    let response = client
        .request(
            "skills/list",
            json!({"cwds":[root.path()],"forceReload":true}),
            Duration::from_secs(20),
        )
        .await
        .unwrap();
    client.close().await;
    assert!(
        response["data"]
            .as_array()
            .unwrap()
            .iter()
            .any(|entry| entry["skills"]
                .as_array()
                .unwrap()
                .iter()
                .any(|skill| skill["name"] == "forge-discovery-test")),
        "{response}"
    );
}
