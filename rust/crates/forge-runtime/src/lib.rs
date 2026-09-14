//! Isolated Codex App Server transport. Product state and approval policy belong
//! to the caller. A protocol reply or completed turn is not business acceptance.
//!
//! Cancelling or timing out a request closes the connection: the remote effect
//! may already have happened. Reconciliation belongs to a durable run owner.

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, HashMap, HashSet},
    ffi::OsString,
    path::{Component, Path, PathBuf},
    sync::{Arc, Mutex},
    time::Duration,
};
use thiserror::Error;
use tokio::{
    io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader},
    process::{ChildStdin, ChildStdout, Command},
    sync::{mpsc, oneshot, watch, Mutex as AsyncMutex},
    task::JoinHandle,
};

const MAX_FRAME_BYTES: usize = 16 * 1024 * 1024;
const MAX_PENDING: usize = 128;
const MAX_EVENTS: usize = 1000;

pub type Result<T> = std::result::Result<T, RuntimeError>;

#[derive(Clone, Debug, Error, PartialEq, Eq)]
pub enum RuntimeError {
    #[error("invalid runtime configuration: {0}")]
    Configuration(&'static str),
    #[error("Codex transport is closed; execution outcome must be reconciled")]
    Closed,
    #[error("Codex request timed out: {0}; execution outcome may be unknown")]
    RequestTimeout(String),
    #[error("no Codex event before deadline")]
    EventTimeout,
    #[error("Codex request was cancelled; execution outcome may be unknown")]
    Cancelled,
    #[error("Codex protocol error: {0}")]
    Protocol(&'static str),
    #[error("Codex process I/O failed")]
    Io,
    // Deliberately omit remote error text; it can include provider credentials.
    #[error("Codex rejected request (code {code:?})")]
    Remote { code: Option<i64> },
    #[error("Codex event or request backlog exceeded its bound")]
    Backlog,
    #[error("server request is expired or does not belong to this connection")]
    UnknownServerRequest,
}

/// All paths are explicit and absolute. Only FORGE_* entries are accepted in
/// `forge_env`; ambient credentials and Codex configuration are not inherited.
/// No Debug implementation: environment values may contain credentials.
pub struct RuntimeConfig {
    pub executable: PathBuf,
    pub cwd: PathBuf,
    pub runtime_home: PathBuf,
    pub forge_env: BTreeMap<String, String>,
}

impl RuntimeConfig {
    pub fn new(executable: PathBuf, cwd: PathBuf, runtime_home: PathBuf) -> Self {
        Self {
            executable,
            cwd,
            runtime_home,
            forge_env: BTreeMap::new(),
        }
    }
}

#[derive(Clone, Debug, Hash, PartialEq, Eq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum RequestId {
    Integer(i64),
    String(String),
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct RuntimeEvent {
    pub kind: String,
    pub thread_id: Option<String>,
    pub turn_id: Option<String>,
    pub item_id: Option<String>,
    pub sequence: u64,
    pub payload: Value,
    pub request_id: Option<RequestId>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TurnTerminalStatus {
    Completed,
    Interrupted,
    Failed,
}

impl RuntimeEvent {
    pub fn terminal_status(&self) -> Option<TurnTerminalStatus> {
        match self.kind.as_str() {
            "turn/interrupted" => Some(TurnTerminalStatus::Interrupted),
            "turn/failed" => Some(TurnTerminalStatus::Failed),
            "turn/completed" => Some(
                match self.payload.pointer("/turn/status").and_then(Value::as_str) {
                    Some("completed") => TurnTerminalStatus::Completed,
                    Some("interrupted" | "cancelled" | "canceled") => {
                        TurnTerminalStatus::Interrupted
                    }
                    _ => TurnTerminalStatus::Failed,
                },
            ),
            _ => None,
        }
    }
}

type Reply = oneshot::Sender<Result<Value>>;

#[derive(Default)]
struct State {
    pending: HashMap<u64, Reply>,
    server_requests: HashSet<RequestId>,
    next_id: u64,
    terminal: Option<RuntimeError>,
}

struct Shared {
    state: Mutex<State>,
    stopped: watch::Sender<bool>,
}

impl Shared {
    fn fail(&self, error: RuntimeError) {
        let mut state = self.state.lock().unwrap();
        if state.terminal.is_some() {
            return;
        }
        state.terminal = Some(error.clone());
        for (_, reply) in state.pending.drain() {
            let _ = reply.send(Err(error.clone()));
        }
        state.server_requests.clear();
        self.stopped.send_replace(true);
    }

    fn check(&self) -> Result<()> {
        self.state
            .lock()
            .unwrap()
            .terminal
            .clone()
            .map_or(Ok(()), Err)
    }

    fn error(&self) -> RuntimeError {
        self.state
            .lock()
            .unwrap()
            .terminal
            .clone()
            .unwrap_or(RuntimeError::Closed)
    }
}

struct Outbound {
    bytes: Vec<u8>,
    written: oneshot::Sender<Result<()>>,
}

// Prevent cancellation of a future from leaving an unobserved, live request.
struct RequestGuard {
    shared: Arc<Shared>,
    armed: bool,
}
impl Drop for RequestGuard {
    fn drop(&mut self) {
        if self.armed {
            self.shared.fail(RuntimeError::Cancelled);
        }
    }
}

pub struct CodexClient {
    shared: Arc<Shared>,
    outbound: mpsc::Sender<Outbound>,
    events: AsyncMutex<mpsc::Receiver<RuntimeEvent>>,
    supervisor: AsyncMutex<Option<JoinHandle<()>>>,
    process_id: Option<u32>,
}

impl CodexClient {
    /// Spawn a project-owned binary and verify initialize.codexHome before the
    /// caller can issue a thread or turn request. Startup failure reaps the child.
    pub async fn start(config: RuntimeConfig, timeout: Duration) -> Result<(Self, Value)> {
        let prepared = prepare(config)?;
        let mut command = Command::new(&prepared.executable);
        command
            .args([
                "-c",
                "cli_auth_credentials_store=\"file\"",
                "-c",
                "mcp_oauth_credentials_store=\"file\"",
                "app-server",
                "--stdio",
            ])
            .current_dir(&prepared.cwd)
            .env_clear()
            .envs(prepared.environment)
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .kill_on_drop(true);
        let mut child = command.spawn().map_err(|_| RuntimeError::Io)?;
        let process_id = child.id();
        let stdin = child.stdin.take().ok_or(RuntimeError::Io)?;
        let stdout = child.stdout.take().ok_or(RuntimeError::Io)?;
        let mut stderr = child.stderr.take().ok_or(RuntimeError::Io)?;
        let (stopped, _) = watch::channel(false);
        let shared = Arc::new(Shared {
            state: Mutex::new(State::default()),
            stopped,
        });
        let (outbound, outbound_rx) = mpsc::channel(MAX_PENDING);
        let (events_tx, events) = mpsc::channel(MAX_EVENTS);
        let reader_shared = shared.clone();
        let reader = tokio::spawn(async move {
            if let Err(error) = read_messages(stdout, &reader_shared, events_tx).await {
                reader_shared.fail(error);
            }
        });
        let writer_shared = shared.clone();
        let writer = tokio::spawn(async move {
            write_messages(stdin, outbound_rx, writer_shared).await;
        });
        let stderr_task = tokio::spawn(async move {
            let mut buffer = [0u8; 8192];
            while let Ok(count) = stderr.read(&mut buffer).await {
                if count == 0 {
                    break;
                }
            }
        });
        let supervisor_shared = shared.clone();
        let mut stop = shared.stopped.subscribe();
        let supervisor = tokio::spawn(async move {
            if !*stop.borrow() {
                tokio::select! {
                    _ = child.wait() => {
                        // Process exit may win the race with stdout, whose pipe
                        // still contains replies/events. Let the reader drain
                        // through EOF before closing; bound inherited open pipes.
                        if !*stop.borrow() {
                            let _ = tokio::time::timeout(Duration::from_secs(1), stop.changed()).await;
                        }
                        supervisor_shared.fail(RuntimeError::Closed);
                    },
                    _ = stop.changed() => {},
                }
            }
            // Killing the direct app-server and reaping it does not establish
            // that tool side effects or detached descendants were rolled back.
            let _ = child.kill().await;
            reader.abort();
            writer.abort();
            stderr_task.abort();
            let _ = tokio::join!(reader, writer, stderr_task);
        });
        let client = Self {
            shared,
            outbound,
            events: AsyncMutex::new(events),
            supervisor: AsyncMutex::new(Some(supervisor)),
            process_id,
        };
        let initialized = client
            .request(
                "initialize",
                json!({
                    "clientInfo": {"name": "agent-forge", "version": env!("CARGO_PKG_VERSION")},
                    "capabilities": {"experimentalApi": true}
                }),
                timeout,
            )
            .await;
        let result = match initialized {
            Ok(value) => {
                let confirmed = value
                    .get("codexHome")
                    .and_then(Value::as_str)
                    .and_then(|path| std::fs::canonicalize(path).ok());
                if confirmed.as_deref() != Some(prepared.runtime_home.as_path()) {
                    Err(RuntimeError::Protocol(
                        "initialize returned a different Codex home",
                    ))
                } else {
                    client
                        .write_with_timeout(
                            json!({"method": "initialized"}),
                            timeout,
                            "initialized",
                        )
                        .await
                        .map(|_| value)
                }
            }
            Err(error) => Err(error),
        };
        match result {
            Ok(value) => Ok((client, value)),
            Err(error) => {
                client.close().await;
                Err(error)
            }
        }
    }

    pub fn process_id(&self) -> Option<u32> {
        self.process_id
    }

    pub async fn request(&self, method: &str, params: Value, timeout: Duration) -> Result<Value> {
        self.shared.check()?;
        if method.is_empty() || !params.is_object() {
            return Err(RuntimeError::Protocol(
                "request requires a method and object params",
            ));
        }
        let (reply, response) = oneshot::channel();
        let id = {
            let mut state = self.shared.state.lock().unwrap();
            if let Some(error) = &state.terminal {
                return Err(error.clone());
            }
            if state.pending.len() >= MAX_PENDING {
                return Err(RuntimeError::Backlog);
            }
            state.next_id = state.next_id.checked_add(1).ok_or(RuntimeError::Backlog)?;
            let id = state.next_id;
            state.pending.insert(id, reply);
            id
        };
        let mut guard = RequestGuard {
            shared: self.shared.clone(),
            armed: true,
        };
        let operation = async {
            self.write(json!({"id": id, "method": method, "params": params}))
                .await?;
            response.await.map_err(|_| self.shared.error())?
        };
        let result = match tokio::time::timeout(timeout, operation).await {
            Ok(result) => result,
            Err(_) => {
                let error = RuntimeError::RequestTimeout(method.to_owned());
                self.shared.fail(error.clone());
                Err(error)
            }
        };
        self.shared.state.lock().unwrap().pending.remove(&id);
        guard.armed = false;
        result
    }

    /// Server requests are surfaced unchanged, including unsupported methods.
    /// The owner must explicitly respond, reject, or close; no approval is inferred.
    pub async fn respond(&self, id: &RequestId, result: Value, timeout: Duration) -> Result<()> {
        self.shared.check()?;
        {
            let mut state = self.shared.state.lock().unwrap();
            if !state.server_requests.remove(id) {
                return Err(RuntimeError::UnknownServerRequest);
            }
        }
        self.write_with_timeout(
            json!({"id": id, "result": result}),
            timeout,
            "server response",
        )
        .await
    }

    pub async fn reject(&self, id: &RequestId, code: i64, timeout: Duration) -> Result<()> {
        self.shared.check()?;
        {
            let mut state = self.shared.state.lock().unwrap();
            if !state.server_requests.remove(id) {
                return Err(RuntimeError::UnknownServerRequest);
            }
        }
        self.write_with_timeout(
            json!({"id": id, "error": {"code": code, "message": "Rejected by Forge"}}),
            timeout,
            "server rejection",
        )
        .await
    }

    /// Idle event timeouts leave the session open. EOF always returns an error,
    /// after previously received events have been drained.
    pub async fn next_event(&self, timeout: Duration) -> Result<RuntimeEvent> {
        let operation = async {
            let mut events = self.events.lock().await;
            events.recv().await.ok_or_else(|| self.shared.error())
        };
        tokio::time::timeout(timeout, operation)
            .await
            .map_err(|_| RuntimeError::EventTimeout)?
    }

    pub async fn close(&self) {
        self.shared.fail(RuntimeError::Closed);
        if let Some(supervisor) = self.supervisor.lock().await.take() {
            let _ = supervisor.await;
        }
    }

    async fn write_with_timeout(
        &self,
        message: Value,
        timeout: Duration,
        method: &str,
    ) -> Result<()> {
        let mut guard = RequestGuard {
            shared: self.shared.clone(),
            armed: true,
        };
        let result = match tokio::time::timeout(timeout, self.write(message)).await {
            Ok(result) => result,
            Err(_) => {
                let error = RuntimeError::RequestTimeout(method.to_owned());
                self.shared.fail(error.clone());
                Err(error)
            }
        };
        guard.armed = false;
        result
    }

    async fn write(&self, message: Value) -> Result<()> {
        self.shared.check()?;
        let mut bytes = serde_json::to_vec(&message)
            .map_err(|_| RuntimeError::Protocol("cannot encode request"))?;
        if bytes.len() >= MAX_FRAME_BYTES {
            return Err(RuntimeError::Protocol("request frame too large"));
        }
        bytes.push(b'\n');
        let (written, received) = oneshot::channel();
        self.outbound
            .send(Outbound { bytes, written })
            .await
            .map_err(|_| self.shared.error())?;
        received.await.map_err(|_| self.shared.error())?
    }
}

impl Drop for CodexClient {
    fn drop(&mut self) {
        self.shared.fail(RuntimeError::Closed);
    }
}

async fn write_messages(
    mut stdin: ChildStdin,
    mut incoming: mpsc::Receiver<Outbound>,
    shared: Arc<Shared>,
) {
    while let Some(message) = incoming.recv().await {
        let result = async {
            stdin.write_all(&message.bytes).await?;
            stdin.flush().await
        }
        .await;
        if result.is_err() {
            shared.fail(RuntimeError::Io);
            let _ = message.written.send(Err(RuntimeError::Io));
            break;
        }
        let _ = message.written.send(Ok(()));
    }
}

async fn read_messages(
    stdout: ChildStdout,
    shared: &Shared,
    events: mpsc::Sender<RuntimeEvent>,
) -> Result<()> {
    let mut reader = BufReader::new(stdout);
    let mut sequence = 0;
    loop {
        let frame = read_frame(&mut reader).await?;
        let value: Value = serde_json::from_slice(&frame)
            .map_err(|_| RuntimeError::Protocol("invalid JSON frame"))?;
        let object = value
            .as_object()
            .ok_or(RuntimeError::Protocol("frame is not an object"))?;
        if !object.contains_key("method") {
            let id = value
                .get("id")
                .and_then(Value::as_u64)
                .ok_or(RuntimeError::Protocol("invalid response id"))?;
            let result = match (value.get("result"), value.get("error")) {
                (Some(result), None) => Ok(result.clone()),
                (None, Some(error)) if error.is_object() => Err(RuntimeError::Remote {
                    code: error.get("code").and_then(Value::as_i64),
                }),
                _ => {
                    return Err(RuntimeError::Protocol(
                        "response must contain result or error",
                    ))
                }
            };
            if let Some(reply) = shared.state.lock().unwrap().pending.remove(&id) {
                let _ = reply.send(result);
            }
            continue;
        }
        let kind = value
            .get("method")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .ok_or(RuntimeError::Protocol("invalid notification method"))?
            .to_owned();
        let request_id = value
            .get("id")
            .map(|id| {
                serde_json::from_value::<RequestId>(id.clone())
                    .map_err(|_| RuntimeError::Protocol("invalid server request id"))
            })
            .transpose()?;
        let payload = value.get("params").cloned().unwrap_or_else(|| json!({}));
        if !payload.is_object() {
            return Err(RuntimeError::Protocol(
                "notification params are not an object",
            ));
        }
        if let Some(id) = &request_id {
            let mut state = shared.state.lock().unwrap();
            if state.server_requests.len() >= MAX_EVENTS {
                return Err(RuntimeError::Backlog);
            }
            if !state.server_requests.insert(id.clone()) {
                return Err(RuntimeError::Protocol("duplicate active server request id"));
            }
        }
        sequence += 1;
        let event = RuntimeEvent {
            kind,
            thread_id: first_text(
                &payload,
                &[
                    "/threadId",
                    "/thread_id",
                    "/thread/id",
                    "/item/threadId",
                    "/item/thread_id",
                ],
            ),
            turn_id: first_text(
                &payload,
                &[
                    "/turnId",
                    "/turn_id",
                    "/turn/id",
                    "/item/turnId",
                    "/item/turn_id",
                ],
            ),
            item_id: first_text(
                &payload,
                &["/itemId", "/item_id", "/item/id", "/item/itemId"],
            ),
            sequence,
            payload,
            request_id,
        };
        events.try_send(event).map_err(|_| RuntimeError::Backlog)?;
    }
}

fn first_text(value: &Value, pointers: &[&str]) -> Option<String> {
    pointers.iter().find_map(|p| {
        value
            .pointer(p)
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
    })
}

async fn read_frame(reader: &mut BufReader<ChildStdout>) -> Result<Vec<u8>> {
    let mut frame = Vec::new();
    loop {
        let available = reader.fill_buf().await.map_err(|_| RuntimeError::Io)?;
        if available.is_empty() {
            return Err(if frame.is_empty() {
                RuntimeError::Closed
            } else {
                RuntimeError::Protocol("unterminated JSON frame")
            });
        }
        let end = available.iter().position(|byte| *byte == b'\n');
        let count = end.map_or(available.len(), |offset| offset + 1);
        if frame.len() + count > MAX_FRAME_BYTES {
            return Err(RuntimeError::Protocol("response frame too large"));
        }
        frame.extend_from_slice(&available[..count]);
        reader.consume(count);
        if end.is_some() {
            return Ok(frame);
        }
    }
}

struct Prepared {
    executable: PathBuf,
    cwd: PathBuf,
    runtime_home: PathBuf,
    environment: BTreeMap<OsString, OsString>,
}

fn prepare(config: RuntimeConfig) -> Result<Prepared> {
    if !config.executable.is_absolute() || !config.executable.is_file() {
        return Err(RuntimeError::Configuration(
            "Codex executable must be an existing absolute file",
        ));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if std::fs::metadata(&config.executable)
            .map_err(|_| RuntimeError::Io)?
            .permissions()
            .mode()
            & 0o111
            == 0
        {
            return Err(RuntimeError::Configuration(
                "Codex binary is not executable",
            ));
        }
    }
    if !config.cwd.is_absolute() || !config.cwd.is_dir() {
        return Err(RuntimeError::Configuration(
            "workspace must be an existing absolute directory",
        ));
    }
    let runtime_home = resolve_missing(&config.runtime_home)?;
    let personal_home = std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .ok_or(RuntimeError::Configuration(
            "cannot establish personal home boundary",
        ))?;
    let personal_home = resolve_missing(Path::new(&personal_home))?;
    let personal_codex = resolve_missing(&personal_home.join(".codex"))?;
    if runtime_home.parent().is_none()
        || runtime_home == personal_home
        || runtime_home.starts_with(&personal_codex)
        || personal_codex.starts_with(&runtime_home)
    {
        return Err(RuntimeError::Configuration(
            "runtime home overlaps personal Codex or its parent",
        ));
    }
    for key in config.forge_env.keys() {
        if !key.starts_with("FORGE_") || key.contains('=') || key.contains('\0') {
            return Err(RuntimeError::Configuration(
                "only FORGE_* runtime environment entries are allowed",
            ));
        }
    }
    // Reject linked config/auth and home descendants before creating anything.
    for name in ["user-home", "config.toml", "auth.json"] {
        let candidate = runtime_home.join(name);
        if std::fs::symlink_metadata(&candidate).is_ok_and(|m| m.file_type().is_symlink())
            || !resolve_missing(&candidate)?.starts_with(&runtime_home)
        {
            return Err(RuntimeError::Configuration(
                "runtime config path is redirected",
            ));
        }
    }
    private_directory(&runtime_home)?;
    let user_home = runtime_home.join("user-home");
    private_directory(&user_home)?;
    // An existing isolated HOME can still carry redirects to personal skill/config directories.
    for name in [
        ".codex",
        ".agents",
        ".config",
        ".local",
        ".local/share",
        ".cache",
    ] {
        if !resolve_missing(&user_home.join(name))?.starts_with(&user_home) {
            return Err(RuntimeError::Configuration(
                "isolated user home contains an escaping path",
            ));
        }
    }
    let mut environment = BTreeMap::new();
    for key in [
        "PATH",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "SYSTEMROOT",
        "SystemRoot",
        "COMSPEC",
    ] {
        if let Some(value) = std::env::var_os(key) {
            environment.insert(OsString::from(key), value);
        }
    }
    for (key, value) in config.forge_env {
        environment.insert(key.into(), value.into());
    }
    for (key, path) in [
        ("HOME", user_home.clone()),
        ("USERPROFILE", user_home.clone()),
        ("XDG_CONFIG_HOME", user_home.join(".config")),
        ("XDG_DATA_HOME", user_home.join(".local/share")),
        ("XDG_CACHE_HOME", user_home.join(".cache")),
        ("CODEX_HOME", runtime_home.clone()),
    ] {
        environment.insert(key.into(), path.into_os_string());
    }
    Ok(Prepared {
        executable: config.executable,
        cwd: config.cwd,
        runtime_home,
        environment,
    })
}

fn private_directory(path: &Path) -> Result<()> {
    let mut builder = std::fs::DirBuilder::new();
    builder.recursive(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        builder.mode(0o700);
    }
    builder.create(path).map_err(|_| RuntimeError::Io)
}

fn resolve_missing(path: &Path) -> Result<PathBuf> {
    if !path.is_absolute() || path.components().any(|c| matches!(c, Component::ParentDir)) {
        return Err(RuntimeError::Configuration(
            "runtime paths must be absolute and contain no parent traversal",
        ));
    }
    let mut cursor = path;
    let mut missing = Vec::new();
    loop {
        match std::fs::canonicalize(cursor) {
            Ok(mut resolved) => {
                for component in missing.iter().rev() {
                    resolved.push(component);
                }
                return Ok(resolved);
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                // A dangling symlink is a redirect too, not a missing directory.
                if std::fs::symlink_metadata(cursor).is_ok() {
                    return Err(RuntimeError::Configuration(
                        "runtime path contains a dangling symlink",
                    ));
                }
                missing.push(cursor.file_name().ok_or(RuntimeError::Io)?.to_os_string());
                cursor = cursor.parent().ok_or(RuntimeError::Io)?;
            }
            Err(_) => return Err(RuntimeError::Io),
        }
    }
}
