use forge_core::{
    ApprovalKind, ApprovalRequest, ApprovalStatus, Command, CommandResult, ProviderSnapshot, Run,
    RunConfig, RunStatus, RunTransition,
};
use forge_runtime::{CodexClient, RuntimeConfig, RuntimeEvent, TurnTerminalStatus};
use forge_store::Store;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    fs,
    path::{Component, Path, PathBuf},
    time::Duration,
};
use thiserror::Error;
use url::Url;
use uuid::Uuid;

const DEADLINE: Duration = Duration::from_secs(45);
const LEASE_MS: i64 = 120_000;
const MAX_ARTIFACT: u64 = 1024 * 1024;

#[derive(Debug, Error)]
pub enum HostError {
    #[error("invalid controlled run input: {0}")]
    Input(&'static str),
    #[error("run already exists; inspect its durable state before recovery")]
    AlreadyStarted,
    #[error("runtime execution failed; inspect the durable run for outcome_unknown")]
    Execution,
    #[error("artifact does not satisfy the frozen acceptance contract")]
    Acceptance,
    #[error("storage operation failed")]
    Store(#[from] forge_store::StoreError),
    #[error("local file operation failed")]
    Io(#[from] std::io::Error),
    #[error("invalid JSON data")]
    Json(#[from] serde_json::Error),
}

type Result<T> = std::result::Result<T, HostError>;

/// Local trusted input, not a public API or a model-callable command. The
/// exporter retains the product revision evidence alongside this document.
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RunInput {
    pub schema_version: u32,
    pub run_id: String,
    pub task_id: String,
    pub worker_id: String,
    pub config: RunConfig,
    /// Immutable provider revision selected by the trusted product mapper.
    /// Credentials remain references and are supplied only at process launch.
    pub provider_snapshot: ProviderSnapshot,
    pub runtime_executable: PathBuf,
    pub prompt: String,
    pub acceptance: ArtifactContract,
    /// Only used by the repository App Server fixture in fault tests.
    #[serde(default)]
    pub fixture_mode: Option<FixtureMode>,
    /// Exact reviewed command; every other server request fails closed.
    #[serde(default)]
    pub approved_command: Option<String>,
    /// Trusted HTTP host may persist human decisions while this process owns
    /// the live callback. Never automatically grants a request in this mode.
    #[serde(default)]
    pub external_approvals: bool,
    #[serde(default = "default_execution_timeout")]
    pub execution_timeout_ms: u64,
}

fn default_execution_timeout() -> u64 {
    45_000
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FixtureMode {
    Normal,
    ExitBeforeTurnReply,
    ExitAfterEffect,
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ArtifactContract {
    pub path: PathBuf,
    pub expected_content: String,
}

pub fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

impl RunInput {
    pub fn freeze(&mut self) -> Result<()> {
        if !(1_000..=900_000).contains(&self.execution_timeout_ms)
            || (self.external_approvals && self.approved_command.is_some())
        {
            return Err(HostError::Input("execution deadline or approval mode"));
        }
        if self.schema_version != 1 || self.prompt.is_empty() || self.prompt.len() > 64 * 1024 {
            return Err(HostError::Input("schema version or prompt"));
        }
        for id in [&self.run_id, &self.task_id, &self.worker_id] {
            if id.is_empty()
                || id.len() > 100
                || !id
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || b"-_.".contains(&c))
            {
                return Err(HostError::Input("identifier"));
            }
        }
        if self.config.members.len() != 1 || !self.config.permits_worker(&self.worker_id) {
            return Err(HostError::Input("exactly one authorized member required"));
        }
        if !self.config.working_directory.is_absolute()
            || !self.config.runtime_home.is_absolute()
            || !self.runtime_executable.is_absolute()
        {
            return Err(HostError::Input("absolute runtime paths required"));
        }
        let cwd = fs::canonicalize(&self.config.working_directory)?;
        let runtime_parent = self
            .config
            .runtime_home
            .parent()
            .ok_or(HostError::Input("runtime home"))?;
        let runtime_home = fs::canonicalize(runtime_parent)?.join(
            self.config
                .runtime_home
                .file_name()
                .ok_or(HostError::Input("runtime home"))?,
        );
        if runtime_home.starts_with(&cwd) || cwd.starts_with(&runtime_home) {
            return Err(HostError::Input("workspace and runtime home overlap"));
        }
        self.config.working_directory = cwd;
        self.config.runtime_home = runtime_home;
        if self.acceptance.path.components().count() != 1
            || !matches!(
                self.acceptance.path.components().next(),
                Some(Component::Normal(_))
            )
            || self.acceptance.expected_content.len() as u64 > MAX_ARTIFACT
        {
            return Err(HostError::Input(
                "acceptance must name one bounded workspace file",
            ));
        }
        let route = &mut self.config.route_binding;
        let url =
            Url::parse(&route.responses_base_url).map_err(|_| HostError::Input("route URL"))?;
        if url.scheme() != "http"
            || !matches!(url.host_str(), Some("127.0.0.1" | "[::1]"))
            || url.port().is_none()
            || url.path() != "/v1"
            || !url.username().is_empty()
            || url.password().is_some()
            || url.query().is_some()
            || url.fragment().is_some()
            || route.credential_ref != "env:FORGE_RUN_API_KEY"
        {
            return Err(HostError::Input(
                "only an explicit local fixture route is supported",
            ));
        }
        let original_digest = self.provider_snapshot.config_digest.clone();
        self.provider_snapshot.config_digest.clear();
        let provider_digest = digest(&serde_json::to_vec(&self.provider_snapshot)?);
        if original_digest != "pending-rust-freeze" && original_digest != provider_digest {
            return Err(HostError::Input("provider snapshot does not match route"));
        }
        // A revision may serve many routes. Its digest must not depend on a
        // per-run gateway port or route ID, or the second run would conflict.
        route.config_digest = provider_digest.clone();
        self.provider_snapshot.config_digest = provider_digest;
        self.provider_snapshot
            .validate_route(route)
            .map_err(|_| HostError::Input("provider snapshot does not match route"))?;
        self.config
            .validate()
            .map_err(|_| HostError::Input("frozen run configuration"))?;
        Ok(())
    }

    fn thread_params(&self) -> Value {
        let policy = self.config.permission_profile.execution_policy();
        json!({
            "cwd": self.config.working_directory,
            "model": self.config.route_binding.model_id,
            "modelProvider": "forge-owned",
            "approvalPolicy": policy.approval_policy,
            "approvalsReviewer": policy.approvals_reviewer,
            "sandbox": policy.sandbox,
            "config": {
                "model_providers.forge-owned": {
                    "name": "Forge local fixture",
                    "base_url": self.config.route_binding.responses_base_url,
                    "env_key": "FORGE_RUN_API_KEY",
                    "wire_api": "responses",
                    "requires_openai_auth": false,
                    "supports_websockets": false
                },
                "allow_login_shell": false,
                "model_reasoning_effort": "low",
                "analytics.enabled": false
            }
        })
    }
}

fn transition(store: &mut Store, run_id: &str, name: &str, next: RunTransition) -> Result<Run> {
    let run = store
        .get_run(run_id)?
        .ok_or(HostError::Input("missing run"))?;
    let receipt = store.execute(
        &format!("{run_id}:{name}"),
        Command::TransitionRun {
            run_id: run_id.into(),
            expected_version: run.version,
            transition: next,
        },
    )?;
    match receipt.result {
        CommandResult::Run(run) => Ok(run),
        _ => Err(HostError::Execution),
    }
}

/// Commits the start intent before spawning. An existing ID never starts an
/// external process, including after a lost reply or interrupted previous host.
pub async fn execute(store: &mut Store, mut input: RunInput) -> Result<Run> {
    if store.get_run(&input.run_id)?.is_some() {
        return Err(HostError::AlreadyStarted);
    }
    input.freeze()?;
    let artifact = input.config.working_directory.join(&input.acceptance.path);
    if fs::symlink_metadata(&artifact).is_ok()
        || fs::symlink_metadata(&input.config.runtime_home).is_ok()
    {
        return Err(HostError::Input("artifact and runtime home must be fresh"));
    }
    let key = std::env::var("FORGE_RUN_API_KEY")
        .map_err(|_| HostError::Input("fixture credential missing"))?;
    if key.is_empty() || key.len() > 4096 {
        return Err(HostError::Input("fixture credential invalid"));
    }
    store.execute(
        &format!("{}:create", input.run_id),
        Command::CreateProvisionedRun {
            run_id: input.run_id.clone(),
            config: Box::new(input.config.clone()),
            provider_snapshot: Box::new(input.provider_snapshot.clone()),
        },
    )?;
    store.execute(
        &format!("{}:task", input.run_id),
        Command::CreateTask {
            run_id: input.run_id.clone(),
            task_id: input.task_id.clone(),
        },
    )?;
    let run = transition(
        store,
        &input.run_id,
        "begin",
        RunTransition::Begin {
            worker_id: input.worker_id.clone(),
            connection_id: Uuid::new_v4().to_string(),
        },
    )?;
    let connection = run.execution.ok_or(HostError::Execution)?.connection;
    let mut runtime = RuntimeConfig::new(
        input.runtime_executable.clone(),
        input.config.working_directory.clone(),
        input.config.runtime_home.clone(),
    );
    runtime.forge_env.insert("FORGE_RUN_API_KEY".into(), key);
    if let Some(mode) = &input.fixture_mode {
        runtime.forge_env.insert(
            "FORGE_EXECUTION_FIXTURE_MODE".into(),
            serde_json::to_value(mode)?.as_str().unwrap().into(),
        );
    }
    let client = match CodexClient::start(runtime, DEADLINE).await {
        Ok((client, _)) => client,
        Err(_) => {
            transition(
                store,
                &input.run_id,
                "unknown",
                RunTransition::MarkOutcomeUnknown { connection },
            )?;
            return Err(HostError::Execution);
        }
    };
    // This bounded, key-free snapshot also retains the expected artifact and
    // exact approval command. It is not consulted again during execution.
    let result = async {
        let snapshot = input.config.runtime_home.join("forge-input.json");
        let file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(snapshot)?;
        serde_json::to_writer(file, &input)?;
        drive(store, &input, &client, &connection).await
    }
    .await;
    client.close().await;
    if result.is_err() {
        let current = store.get_run(&input.run_id)?.ok_or(HostError::Execution)?;
        if matches!(current.status, RunStatus::Starting | RunStatus::Running) {
            transition(
                store,
                &input.run_id,
                "unknown",
                RunTransition::MarkOutcomeUnknown { connection },
            )?;
        }
    }
    result
}

async fn drive(
    store: &mut Store,
    input: &RunInput,
    client: &CodexClient,
    connection: &forge_core::RunConnection,
) -> Result<Run> {
    let thread = client
        .request("thread/start", input.thread_params(), DEADLINE)
        .await
        .map_err(|_| HostError::Execution)?;
    let thread_id = thread
        .pointer("/thread/id")
        .and_then(Value::as_str)
        .ok_or(HostError::Execution)?
        .to_owned();
    if thread.pointer("/thread/cwd").and_then(Value::as_str)
        != input.config.working_directory.to_str()
    {
        return Err(HostError::Execution);
    }
    transition(
        store,
        &input.run_id,
        "thread",
        RunTransition::BindThread {
            connection: connection.clone(),
            thread_id: thread_id.clone(),
        },
    )?;
    let turn = client
        .request(
            "turn/start",
            json!({"threadId": thread_id,
                "input": [{"type": "text", "text": input.prompt, "text_elements": []}]
            }),
            DEADLINE,
        )
        .await
        .map_err(|_| HostError::Execution)?;
    let turn_id = turn
        .pointer("/turn/id")
        .and_then(Value::as_str)
        .ok_or(HostError::Execution)?
        .to_owned();
    let run = transition(
        store,
        &input.run_id,
        "turn",
        RunTransition::BindTurn {
            connection: connection.clone(),
            turn_id: turn_id.clone(),
        },
    )?;
    let receipt = store.execute(
        &format!("{}:claim", input.run_id),
        Command::ClaimTask {
            task_id: input.task_id.clone(),
            worker_id: input.worker_id.clone(),
            expected_version: 0,
            lease_duration_ms: LEASE_MS,
        },
    )?;
    let claim = match receipt.result {
        CommandResult::Task(task) => task.claim.ok_or(HostError::Execution)?,
        _ => return Err(HostError::Execution),
    };
    let mut tool_completed = false;
    let deadline = tokio::time::Instant::now() + Duration::from_millis(input.execution_timeout_ms);
    let mut renewal = 0_u64;
    let mut last_renewal = tokio::time::Instant::now();
    loop {
        renew_claim(store, input, &claim, &mut last_renewal, &mut renewal)?;
        let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
        if remaining.is_zero() {
            return Err(HostError::Execution);
        }
        let event = match client
            .next_event(remaining.min(Duration::from_millis(250)))
            .await
        {
            Ok(event) => event,
            Err(forge_runtime::RuntimeError::EventTimeout) => continue,
            Err(_) => return Err(HostError::Execution),
        };
        if event.thread_id.as_ref().is_some_and(|id| id != &thread_id)
            || event.turn_id.as_ref().is_some_and(|id| id != &turn_id)
        {
            return Err(HostError::Execution);
        }
        persist_event(store, &input.run_id, connection, &event)?;
        if let Some(request_id) = &event.request_id {
            let known_kind = match event.kind.as_str() {
                "item/commandExecution/requestApproval" => ApprovalKind::CommandExecution,
                "item/fileChange/requestApproval" => ApprovalKind::FileChange,
                "item/permissions/requestApproval" => ApprovalKind::Permissions,
                _ => return Err(HostError::Execution),
            };
            let allowed = input.external_approvals
                || (event.kind == "item/commandExecution/requestApproval"
                    && event.thread_id.as_deref() == Some(&thread_id)
                    && event.turn_id.as_deref() == Some(&turn_id)
                    && event.item_id.is_some()
                    && input
                        .approved_command
                        .as_deref()
                        .is_some_and(|command| event.payload["command"].as_str() == Some(command))
                    && event.payload["cwd"].as_str() == input.config.working_directory.to_str());
            if !allowed {
                return Err(HostError::Execution);
            }
            let execution = run.execution.clone().ok_or(HostError::Execution)?;
            let approval_id = format!("{}:approval:{}", input.run_id, event.sequence);
            let request = ApprovalRequest {
                run_id: input.run_id.clone(),
                execution: execution.clone(),
                request_id: serde_json::from_value(serde_json::to_value(request_id)?)?,
                item_id: event.item_id.clone().ok_or(HostError::Execution)?,
                kind: known_kind,
                payload: event.payload.clone(),
            };
            let request_digest = request.digest().map_err(|_| HostError::Execution)?;
            store.execute(
                &format!("{approval_id}:record"),
                Command::RecordApproval {
                    approval_id: approval_id.clone(),
                    request: Box::new(request),
                },
            )?;
            if !input.external_approvals {
                store.execute(
                    &format!("{approval_id}:decide"),
                    Command::DecideApproval {
                        approval_id: approval_id.clone(),
                        expected_version: 0,
                        digest: request_digest,
                        approved: true,
                        actor_id: input.config.owner_id.clone(),
                        comment: "Exact command authorized in controlled fixture input".into(),
                    },
                )?;
            }
            let queued = loop {
                let approval = store
                    .get_approval(&approval_id)?
                    .ok_or(HostError::Execution)?;
                match approval.status {
                    ApprovalStatus::Queued => break approval,
                    ApprovalStatus::Pending if tokio::time::Instant::now() < deadline => {
                        renew_claim(store, input, &claim, &mut last_renewal, &mut renewal)?;
                        tokio::time::sleep(Duration::from_millis(100)).await;
                    }
                    _ => return Err(HostError::Execution),
                }
            };
            store.execute(
                &format!("{approval_id}:dispatch"),
                Command::BeginApprovalDispatch {
                    approval_id: approval_id.clone(),
                    expected_version: queued.version,
                    execution: execution.clone(),
                },
            )?;
            let dispatch = store
                .get_approval(&approval_id)?
                .ok_or(HostError::Execution)?;
            let response = dispatch.response().map_err(|_| HostError::Execution)?;
            let delivered = client.respond(request_id, response, DEADLINE).await.is_ok();
            store.execute(
                &format!("{approval_id}:finish"),
                Command::FinishApprovalDispatch {
                    approval_id,
                    expected_version: dispatch.version,
                    execution,
                    delivered,
                },
            )?;
            if !delivered {
                return Err(HostError::Execution);
            }
        }
        if event.kind == "item/completed"
            && event.payload.pointer("/item/type").and_then(Value::as_str)
                == Some("commandExecution")
            && event
                .payload
                .pointer("/item/exitCode")
                .and_then(Value::as_i64)
                == Some(0)
        {
            tool_completed = true;
        }
        if let Some(status) = event.terminal_status() {
            if status != TurnTerminalStatus::Completed
                || !tool_completed
                || event.thread_id.as_deref() != Some(&thread_id)
                || event.turn_id.as_deref() != Some(&turn_id)
            {
                return Err(HostError::Execution);
            }
            break;
        }
    }
    let artifact_path = input.config.working_directory.join(&input.acceptance.path);
    let bytes = artifact_bytes(&artifact_path)?;
    let result = json!({
        "artifact": input.acceptance.path,
        "artifact_sha256": digest(&bytes),
        "expected_sha256": digest(input.acceptance.expected_content.as_bytes()),
        "input_sha256": digest(&serde_json::to_vec(input)?),
        "thread_id": thread_id, "turn_id": turn_id, "tool_completed": true
    });
    store.execute(
        &format!("{}:submit-task", input.run_id),
        Command::SubmitTask {
            task_id: input.task_id.clone(),
            claim_token: claim.token,
            attempt_generation: claim.generation,
            result: result.clone(),
        },
    )?;
    transition(
        store,
        &input.run_id,
        "submit",
        RunTransition::Submit {
            execution: run.execution.ok_or(HostError::Execution)?,
            result,
        },
    )
}

fn renew_claim(
    store: &mut Store,
    input: &RunInput,
    claim: &forge_core::Claim,
    last: &mut tokio::time::Instant,
    sequence: &mut u64,
) -> Result<()> {
    if last.elapsed() >= Duration::from_secs(30) {
        *sequence += 1;
        store.execute(
            &format!("{}:renew:{}", input.run_id, sequence),
            Command::RenewClaim {
                task_id: input.task_id.clone(),
                claim_token: claim.token.clone(),
                attempt_generation: claim.generation,
                lease_duration_ms: LEASE_MS,
            },
        )?;
        *last = tokio::time::Instant::now();
    }
    Ok(())
}

fn persist_event(
    store: &mut Store,
    run_id: &str,
    connection: &forge_core::RunConnection,
    event: &RuntimeEvent,
) -> Result<()> {
    let terminal_status = event.terminal_status().map(|s| {
        match s {
            TurnTerminalStatus::Completed => "completed",
            TurnTerminalStatus::Interrupted => "interrupted",
            TurnTerminalStatus::Failed => "failed",
        }
        .to_owned()
    });
    store.execute(
        &format!("{run_id}:event:{}", event.sequence),
        Command::RecordRuntimeEvent {
            run_id: run_id.into(),
            connection: connection.clone(),
            sequence: event.sequence,
            thread_id: event.thread_id.clone(),
            turn_id: event.turn_id.clone(),
            item_id: event.item_id.clone(),
            kind: event.kind.clone(),
            terminal_status,
        },
    )?;
    Ok(())
}

fn artifact_bytes(path: &Path) -> Result<Vec<u8>> {
    let metadata = fs::symlink_metadata(path)?;
    if !metadata.is_file() || metadata.len() > MAX_ARTIFACT {
        return Err(HostError::Acceptance);
    }
    Ok(fs::read(path)?)
}

/// Acceptance is a separate invocation after submission. The host checks the
/// current artifact against the contract frozen before the model turn.
pub fn accept(store: &mut Store, run_id: &str) -> Result<Run> {
    let run = store
        .get_run(run_id)?
        .ok_or(HostError::Input("missing run"))?;
    if !matches!(
        run.status,
        RunStatus::Submitted | RunStatus::Integrated | RunStatus::Accepted
    ) {
        return Err(HostError::Acceptance);
    }
    let config = run.config.as_ref().ok_or(HostError::Acceptance)?;
    let result = run.result.as_ref().ok_or(HostError::Acceptance)?;
    let relative = Path::new(result["artifact"].as_str().ok_or(HostError::Acceptance)?);
    if relative.components().count() != 1
        || !matches!(relative.components().next(), Some(Component::Normal(_)))
    {
        return Err(HostError::Acceptance);
    }
    let actual = digest(&artifact_bytes(&config.working_directory.join(relative))?);
    if result["artifact_sha256"].as_str() != Some(&actual)
        || result["expected_sha256"].as_str() != Some(&actual)
    {
        return Err(HostError::Acceptance);
    }
    if run.status == RunStatus::Accepted {
        return Ok(run);
    }
    let evidence_ref = format!("sha256:{actual}");
    if run.status == RunStatus::Submitted {
        transition(
            store,
            run_id,
            "integrate",
            RunTransition::Integrate {
                evidence_ref: evidence_ref.clone(),
            },
        )?;
    }
    transition(
        store,
        run_id,
        "accept",
        RunTransition::Accept { evidence_ref },
    )
}
