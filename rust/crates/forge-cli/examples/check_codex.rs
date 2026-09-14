//! Real upstream handshake/read smoke, using a disposable isolated home.
//! No model turn, provider credential, or personal Codex configuration is used.
use forge_runtime::{CodexClient, RuntimeConfig};
use serde_json::json;
use std::{path::PathBuf, time::Duration};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let binary = std::env::args_os()
        .nth(1)
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .ancestors()
                .nth(3)
                .unwrap()
                .join(".local/runtimes/codex/bin/codex")
        });
    let directory = tempfile::tempdir()?;
    let workspace = directory.path().join("workspace");
    std::fs::create_dir(&workspace)?;
    let config = RuntimeConfig::new(binary, workspace, directory.path().join("runtime-home"));
    let (client, initialized) = CodexClient::start(config, Duration::from_secs(20)).await?;
    let threads = client
        .request("thread/list", json!({"limit": 1}), Duration::from_secs(10))
        .await;
    client.close().await;
    let threads = threads?;
    if !threads.get("data").is_some_and(|data| data.is_array()) {
        return Err("thread/list returned an unexpected result".into());
    }
    println!(
        "{}",
        json!({
            "ok": true, "transport": "rust-codex-app-server-stdio",
            "runtime_home_verified": initialized.get("codexHome").is_some(),
            "thread_list_verified": true, "model_turn_started": false,
            "child_reaped": true
        })
    );
    Ok(())
}
