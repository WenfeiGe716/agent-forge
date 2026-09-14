use crate::BackendError;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
    process::Stdio,
    time::Duration,
};
use tokio::process::{Child, Command};
use uuid::Uuid;

#[derive(Clone, Serialize, Deserialize)]
pub(crate) struct GatewayRoute {
    pub root: PathBuf,
    pub port: u16,
    pub provider: String,
    pub model: String,
    pub adapter: String,
    #[serde(default)]
    pub headers: BTreeMap<String, String>,
}
pub(crate) struct Gateway {
    child: Child,
    pub token: String,
}
impl GatewayRoute {
    pub(crate) fn available() -> bool {
        launch().is_some()
    }
    pub(crate) fn new(
        root: PathBuf,
        provider: &str,
        model: &str,
        protocol: &str,
        headers: BTreeMap<String, String>,
    ) -> Result<Self, BackendError> {
        let adapter = match protocol {
            "openai_responses" => "openai-responses",
            "openai_chat" => "openai-chat",
            "anthropic_messages" => "anthropic",
            "google_gemini" => "google",
            _ => return Err(BackendError::Unsupported),
        };
        let listener =
            std::net::TcpListener::bind("127.0.0.1:0").map_err(|_| BackendError::Unavailable)?;
        let port = listener
            .local_addr()
            .map_err(|_| BackendError::Unavailable)?
            .port();
        Ok(Self {
            root,
            port,
            provider: provider.into(),
            model: model.into(),
            adapter: adapter.into(),
            headers,
        })
    }
    pub(crate) async fn start(&self, upstream: &str, key: &str) -> Result<Gateway, BackendError> {
        let (program, args) = launch().ok_or(BackendError::Unsupported)?;
        let home = self.root.join("opencodex");
        fs::create_dir_all(&home).map_err(|_| BackendError::Unavailable)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&home, fs::Permissions::from_mode(0o700))
                .map_err(|_| BackendError::Unavailable)?;
        }
        let mut providers = json!({"openai":{"disabled":true,"adapter":"openai-responses","baseUrl":"http://127.0.0.1:1/v1","allowPrivateNetwork":true}});
        providers[&self.provider] = json!({"adapter":self.adapter,"baseUrl":upstream,"apiKey":"${FORGE_PROVIDER_ROUTE_KEY}","defaultModel":self.model,"allowPrivateNetwork":true,"headers":self.headers});
        let config = json!({"hostname":"127.0.0.1","port":self.port,"runtimeRole":"hub","clientIntegrations":{"codex":false,"grok":false},"codexAutoStart":false,"codexShimAutoRestore":false,"unauthenticatedLoopbackListener":{"enabled":false},"defaultProvider":self.provider,"providers":providers});
        fs::write(
            home.join("config.json"),
            serde_json::to_vec(&config).map_err(|_| BackendError::InvalidData)?,
        )
        .map_err(|_| BackendError::Unavailable)?;
        let token = format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple());
        let codex_home = self.root.join("codex");
        let log = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(self.root.join("gateway.log"))
            .map_err(|_| BackendError::Unavailable)?;
        let mut command = Command::new(program);
        command
            .args(args)
            .env_clear()
            .env("FORGE_RUNTIME_DATA_ROOT", &self.root)
            .env("FORGE_OPENCODEX_HOME", &home)
            .env("FORGE_CODEX_HOME", &codex_home)
            .env("FORGE_OPENCODEX_API_AUTH_TOKEN", &token)
            .env("FORGE_PROVIDER_ROUTE_KEY", key)
            .current_dir(&self.root)
            .stdin(Stdio::null())
            .stdout(Stdio::from(
                log.try_clone().map_err(|_| BackendError::Unavailable)?,
            ))
            .stderr(Stdio::from(log))
            .kill_on_drop(true);
        for name in [
            "PATH",
            "LANG",
            "LC_ALL",
            "TMPDIR",
            "HOME",
            "FORGE_GATEWAY_SOURCE_ROOT",
        ] {
            if let Some(v) = std::env::var_os(name) {
                command.env(name, v);
            }
        }
        let mut child = command.spawn().map_err(|_| BackendError::Unavailable)?;
        let client = reqwest::Client::builder()
            .no_proxy()
            .timeout(Duration::from_secs(1))
            .build()
            .map_err(|_| BackendError::Unavailable)?;
        let deadline = tokio::time::Instant::now() + Duration::from_secs(30);
        while tokio::time::Instant::now() < deadline {
            if child
                .try_wait()
                .map_err(|_| BackendError::Unavailable)?
                .is_some()
            {
                return Err(BackendError::Unavailable);
            }
            if let Ok(response) = client
                .get(format!("http://127.0.0.1:{}/healthz", self.port))
                .send()
                .await
            {
                if response.status().is_success() && response.json::<Value>().await.is_ok() {
                    return Ok(Gateway { child, token });
                }
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        let _ = child.kill().await;
        let _ = child.wait().await;
        Err(BackendError::Unavailable)
    }
}
impl Gateway {
    pub(crate) async fn close(mut self) {
        let _ = self.child.kill().await;
        let _ = self.child.wait().await;
    }
}
fn launch() -> Option<(PathBuf, Vec<PathBuf>)> {
    if let Some(path) = std::env::var_os("FORGE_OPENCODEX_BINARY")
        .map(PathBuf::from)
        .filter(|p| p.is_file())
    {
        return Some((path, vec![]));
    }
    let entry = std::env::var_os("FORGE_OPENCODEX_ENTRY").map(PathBuf::from)?;
    let bun = std::env::var_os("FORGE_BUN_BINARY").map(PathBuf::from)?;
    (entry.is_file() && bun.is_file() && Path::new(&entry).is_absolute() && bun.is_absolute())
        .then_some((bun, vec![entry]))
}
