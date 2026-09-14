# Forge Codex transport

`CodexClient` is a process and JSON-line protocol boundary for an unmodified,
explicitly selected Codex App Server. It does not schedule product runs, accept
business results, persist approvals, or replay commands after a restart.

```rust,ignore
use forge_runtime::{CodexClient, RuntimeConfig};
use serde_json::json;
use std::time::Duration;

let config = RuntimeConfig::new(codex_binary, workspace, isolated_runtime_home);
let (client, initialize) = CodexClient::start(config, Duration::from_secs(20)).await?;
let thread = client.request("thread/start", json!({
    "cwd": workspace_path,
    "approvalPolicy": "on-request",
    "sandbox": "workspace-write"
}), Duration::from_secs(30)).await?;
// Consume next_event concurrently with requests. Server request IDs are scoped
// to this connection; respond/reject requires an explicit policy decision.
client.close().await;
```

All paths must be absolute. The runtime rejects a home overlapping personal
`.codex`, uses isolated HOME/USERPROFILE/XDG paths, rejects escaping config/auth
and home links, clears ambient credentials, and accepts only explicit `FORGE_*`
environment entries. Both credential stores are forced to files. Initialization
must return the exact resolved runtime home. Stderr is drained and remote error
text is excluded from errors because either can contain credentials.

Requests may run concurrently; replies correlate by ID independently of incoming
notifications and server requests. Frames are limited to 16 MiB, outstanding
requests to 128, and queued events/server requests to 1,000. Additional client
requests are rejected before being sent. Event overflow and malformed protocol
input terminate the connection rather than drop event history. Unknown
server request methods are preserved; the caller must respond, reject, or close.
No server request is accepted automatically.

Request timeout or cancellation terminates the connection because remote effects
may already have happened. An idle `next_event` timeout does not terminate it.
EOF is always a transport error, after buffered events are drained. Only an
explicit successful `turn/completed` is a completed Codex turn, and that is still
not an accepted Forge task result.

`close().await` kills and reaps the direct app-server process. Dropping a client
requests the same cleanup in the background, so explicit close is preferable
before shutting down the Tokio runtime. This transport does not manage detached
tool process trees or reverse external side effects. Durable run ownership,
approval persistence, event recording, and restart reconciliation remain separate
migration work.

## Validation

- `cargo test -p forge-runtime` runs deterministic mocked child-process tests.
- `cargo test -p forge-runtime --test execution` runs the provider-free Rust task
  execution fixture described below, using a temporary real SQLite database.
- `cargo clippy -p forge-runtime --all-targets -- -D warnings` checks the crate.
- `FORGE_TEST_CODEX_BINARY=/absolute/project/codex cargo test -p forge-runtime real_codex_initialize -- --ignored`
  explicitly runs the real-binary smoke in a temporary isolated home. It only
  initializes App Server; it sends no model request and loads no personal config.

The fixture tests require `python3` in PATH and use Unix executable scripts.
EOF, cancellation, lease ownership, and product acceptance are intentionally
separate concepts; there is no claim of exactly-once external execution here.

## Provider-free execution fixture

`tests/execution.rs` connects the public `CodexClient` transport to the public
`forge-core` commands and `forge-store` transactions. The simulated App Server in
`tests/fixtures/execution_app_server.py` runs as an isolated child process; it
makes no network, model, or shell calls. The only simulated tool effect appends a
turn ID to `fixture-result.txt` in the temporary workspace after explicit
approval. History is stored under the temporary `CODEX_HOME` and survives child
process restarts.

The fixture covers four paths:

- Thread and turn creation, streamed message and tool events, explicit approval,
  local tool evidence, turn completion, and a separate task submission. Reopening
  SQLite and replaying the submission returns the original receipt without
  starting another turn or adding an event/outbox record.
- An accepted `turn/start` whose reply is lost. `thread/read` recovers the unknown
  turn ID without replaying the request; unresolved history keeps the task in
  `outcome_unknown` after its lease expires.
- A tool effect and completed remote history followed by process exit before
  completion is delivered. The effect remains observable, automatic reclaim is
  rejected, and the expired attempt cannot submit. The current task recovery
  command only authorizes safe retry, so this case remains unknown for a future
  host-level result reconciliation policy.
- An interrupted turn with no tool effect. After process cleanup, history and
  workspace checks, an explicit `ReconcileTask` permits a new attempt in the same
  Run and Task. `thread/resume` reuses the thread, the new turn requires a fresh
  approval, and the previous claim and connection's approval ID are rejected.

`FORGE_EXECUTION_FIXTURE_MODE` selects `normal`, `exit_before_turn_reply`, or
`exit_after_effect` for this test child only. `fixture/stats` is a test-only method
used to check execution counts. Runtime event sequence numbers are scoped to each
connection; durable product event IDs come from SQLite.

These are execution-contract and process-failure tests, not a real Codex/provider
integration test. They do not implement a production run owner, durable runtime
event/approval storage, frozen routing/permission/member bindings, or HTTP/SSE
integration, and do not establish completion of the entire Rust migration stage.
