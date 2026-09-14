# Initial transaction kernel API

`forge_core::Command` is internally tagged by `type`, with snake_case variants:

- `CreateRun { run_id }`
- `CreateBoundRun { run_id, config: Box<RunConfig> }`
- `CreateProvisionedRun { run_id, config: Box<RunConfig>, provider_snapshot: Box<ProviderSnapshot> }`
- `RecordApproval { approval_id, request: Box<ApprovalRequest> }`
- `DecideApproval { approval_id, expected_version, digest, approved, actor_id, comment }`
- `BeginApprovalDispatch { approval_id, expected_version, execution: RunExecution }`
- `FinishApprovalDispatch { approval_id, expected_version, execution: RunExecution, delivered: bool }`
- `ExpireApprovals { run_id, connection: RunConnection }`
- `TransitionRun { run_id, expected_version: i64, transition: RunTransition }`
- `CreateTask { run_id, task_id }`
- `CreateAssignedTask { run_id, task_id, coordinator_id, worker_id }`
- `AssignTask { task_id, coordinator_id, worker_id, expected_version }`
- `ClaimTask { task_id, worker_id, expected_version: i64, lease_duration_ms: i64 }`
- `RenewClaim { task_id, claim_token, attempt_generation: i64, lease_duration_ms: i64 }`
- `ReleaseTask { task_id, claim_token, attempt_generation: i64 }`
- `SubmitTask { task_id, claim_token, attempt_generation: i64, result: serde_json::Value }`
- `MarkOutcomeUnknown { task_id, expected_version: i64 }`
- `ReconcileTask { task_id, expected_version: i64, evidence_ref }`

`Store::open(path)` and `Store::open_in_memory()` use a trusted system clock. Tests can use `Store::open_with_clock(path, Arc<dyn Clock>)`; `Clock: Send + Sync` exposes `now_ms() -> i64`. The CLI must not expose clock injection.

`Store::execute(&mut self, command_id: &str, command: Command) -> Result<Receipt, StoreError>` generates a UUID claim token internally. `Receipt { command_id, event_id: i64, result: CommandResult }`; the result is `Run(Run)` or `Task(Task)`. `Task` includes `id`, `run_id`, `status`, `version`, `attempt_generation`, `claim: Option<Claim>`, and `result: Option<Value>`. `Claim` includes `worker_id`, `token`, `generation`, `expires_at_ms`.

Queries: `get_run(id)`, `get_task(id)`, `get_receipt(id)` return optional records. `get_route_binding(run_id)` reads the frozen binding from the run snapshot (missing or legacy unbound runs return `None`). `events_after(after: i64, limit: u32)` and `pending_outbox(limit: u32)` return at most 1000 items. `acknowledge_outbox(event_id)` acknowledges only an existing pending delivery.

Each successful command commits state, attempt, receipt, event and outbox atomically. Repeated command IDs with equal typed JSON return the original receipt. A different payload is `StoreError::CommandConflict`. Domain errors are wrapped in `StoreError::Domain`. Expired execution must be marked `outcome_unknown`; it cannot be automatically claimed again. Submitted evidence awaits product acceptance.

`RenewClaim` requires the current token, current attempt generation, and an unexpired lease at trusted host time. Duration is 1–900,000 ms. Its new expiry is `max(previous_expiry, host_now + lease_duration_ms)`, so renewal never shortens an existing lease. Renewal increments the task version and atomically updates the attempt row. An idempotent replay returns its original receipt, including its original expiry, even if the lease has since expired; the caller must not interpret a replayed receipt as a fresh lease. A new renewal command cannot revive an expired claim.

`ReconcileTask` is a privileged coordinator decision, accepted only for `outcome_unknown` at the supplied current version with a nonblank `evidence_ref`. The reference identifies the coordinator's evidence that another execution is safe (for example, a terminated worker plus reconciled workspace/effects). The command, receipt, and `task_reconciled` event durably record this decision; the event includes the reference. The store validates presence, not the truth or contents of that evidence. The private CLI is a trusted host: any future HTTP/MCP/worker adapter must authorize this command for the coordinator and must not forward arbitrary worker requests to it. Do not treat a string reference as authorization.

Reconciliation moves the task to `ready`, preserving the prior attempt generation and historical attempt record. A subsequent claim creates a new generation and token, so the old worker cannot submit or renew. A replay of the reconciliation command returns its historical receipt without moving a later running task back to ready. `ReleaseTask` similarly belongs only on an execution path the trusted host has confirmed can be released safely; it is not a substitute for reconciling uncertain external effects.

The store refuses a nonempty SQLite database without its application ID, so the existing Python database cannot accidentally become dual-owned. This is an opt-in new database, not an automatic migration.

The SQLite application ID is `0x46475253` and the current `user_version` is `3`. Opening a Forge-owned version 1 database atomically adds the run version column. Version 3 adds retained provider revisions, run route and permission records, and runtime approvals; it backfills route/policy records for existing bound runs without changing historical snapshots or receipts. Provider snapshots cannot be inferred from historical routes, so legacy runs return no provider snapshot. Legacy Run snapshots and receipts decode with `owner: rust`, `version: 0` and no frozen configuration. Original command JSON remains unchanged, so historical successful commands still replay. Other database owners and unsupported versions are rejected before journal-mode or schema changes. Opening version 3 does not reapply schema DDL.

## Provider Revisions and Permissions

`CreateProvisionedRun` retains a `ProviderSnapshot` and creates a run with matching route identity in the same state/receipt/event/outbox transaction. The snapshot contains `provider_id`, `provider_revision_id`, `upstream_base_url`, `credential_ref`, `config_digest`, and `models`, each with `model_id`, `upstream_protocol`, and `capability_evidence_ref`. The HTTP(S) upstream URL cannot embed userinfo, query parameters or fragments. The selected route must exactly match its provider revision, credential reference, digest, model, protocol and capability evidence. Endpoint allowlists, credential access and the truth of evidence are still authenticated host responsibilities.

The `(provider_id, provider_revision_id)` identity is immutable. Additional runs can reuse an identical retained revision; changed content under the same revision is rejected. A new revision affects only new runs. `CreateBoundRun` remains compatible and retains its route and policy but does not fabricate a provider snapshot. Production hosts should use `CreateProvisionedRun` when creating runs from provider assets.

Every bound run retains its exact `ExecutionPolicy { approval_policy, approvals_reviewer, sandbox }` alongside its RouteBinding. The profiles map to:

| Profile | Approval Policy | Reviewer | Sandbox |
| --- | --- | --- | --- |
| ask | on-request | user | workspace-write |
| auto | on-request | auto_review | workspace-write |
| full | never | user | danger-full-access |

Store queries are `get_provider_snapshot(provider_id, provider_revision_id)`, `get_run_provider_snapshot(run_id)`, `get_route_binding(run_id)`, and `get_execution_policy(run_id)`. These return protected records, not public event payloads. Private NDJSON queries use `provider_snapshot`, `run_provider_snapshot`, `route_binding`, and `execution_policy` with the same ID fields.

## Durable Runtime Approvals

`ApprovalRequest` contains `run_id`, the exact `RunExecution` (worker, connection, generation, thread and turn), a typed integer-or-string `request_id`, `item_id`, `kind`, and object `payload`. Supported kinds are the commandExecution, fileChange and permissions `item/.../requestApproval` methods. Recording requires a running bound run and an exact execution match. Callback IDs are unique within the original run/connection/generation; integer `1` and string `"1"` remain different. Reuse with another command is rejected, including changed payloads. Use the original command ID to recover its receipt.

`Approval` adds `id`, a SHA-256 digest of the canonical immutable request, `version`, `status`, `decision`, and trusted creation/update timestamps. `DecideApproval` requires the observed version and digest and records `ApprovalDecision { approved, actor_id, comment }`; the actor must come from the authenticated host. The kernel does not authenticate a string actor ID. The state moves from `pending` to `queued`. Grants are limited to one operation or the requested turn permissions. Session grants are rejected, and file approvals require captured nonempty changes. Raw requests and comments stay out of events and the outbox.

The live host verifies the exact callback, commits `BeginApprovalDispatch`, then writes the response returned by `Approval::response()`. Only one new dispatch command can move `queued` to `dispatching`; `FinishApprovalDispatch` records `delivered` or `delivery_unknown`. A successful pipe write is not execution success. An idempotently replayed dispatch receipt is historical evidence and MUST NOT trigger another transport write. On process restart or lost transport, expire callbacks before considering pending delivery; there is no automatic approval replay.

Pause, outcome-unknown, reconciliation and Begin transitions atomically invalidate unresolved approvals with the run change. Pending/queued records become `expired`; dispatching records become `delivery_unknown`. `ExpireApprovals` can expire a specific lost connection without affecting later generations. Run submission rejects unresolved approvals. Terminal approvals cannot be requeued or answered on a new connection.

`get_approval(approval_id)` and `list_approvals(run_id, limit)` return durable records, with unresolved approvals before history and a maximum of 1000 entries. The corresponding private NDJSON queries are `approval` and `approvals` (limit 1-1000). Approval command receipts use `CommandResult::Approval(Box<Approval>)` or `CommandResult::Approvals(Vec<Approval>)`. These are trusted-host APIs; public authorization and live runtime delivery belong to the host adapter.

## Frozen Run and RouteBinding

`CreateBoundRun` creates a Rust-owned run and its complete frozen configuration in one transaction. Its JSON `config` is an ordinary object (`Box` is only a Rust allocation detail). `RunConfig` contains `project_id`, user `owner_id`, `members: Vec<RunMember>`, `permission_profile: ask | auto | full`, absolute `working_directory` and `runtime_home`, `runtime_version`, and `route_binding`. `RunMember` contains a unique `worker_id`, an immutable `config_revision` reference, and optional frozen public capabilities (`role`, `instructions`, `tools`, `skills`, permission summary, coordinator marker, and communication recipients). Capability fields default when reading pre-team snapshots. There must be at least one member; the first member is the coordinator when no marker is present. `AssignTask` is fenced by task version and only the frozen coordinator may assign a member; an assigned member may claim the task from its own execution context. A run still uses one shared route and permission profile.

`CreateAssignedTask` creates and assigns a task in one command/receipt/event/outbox transaction; its initial version is `1`. `Run.sessions` retains each member's `MemberSession { execution, status }`, with independent worker connection generations. Start a member through `BeginMember { worker_id, connection_id }`, then `BindMemberThread` and `BindMemberTurn`. Stop with `EndMember { connection, evidence_ref }`; uncertain termination uses `MarkMemberOutcomeUnknown { connection }` and requires `ReconcileMember { worker_id, evidence_ref }` before another begin. Each uses `TransitionRun` and the current run version. The existing `Run.execution` remains the leader session. Runtime events and approvals resolve their worker through `Run::execution_for`; ending one member invalidates only that member's approvals. Whole-run submission requires all member sessions stopped and all task results submitted.

`RouteBinding` contains nonblank `provider_id`, `provider_revision_id`, `model_id`, `upstream_protocol`, `gateway_route_id`, `responses_base_url`, `config_digest`, `credential_ref`, `capability_evidence_ref`, and `gateway_version`. Upstream protocol is an opaque identifier: a gateway may translate Anthropic or other upstream protocols into its Codex-facing Responses endpoint. Store only credential references. The trusted host must validate endpoint policy, digest and evidence contents, access to retained revisions, credential revocation, and directory isolation/ownership before starting or resuming. The pure domain performs structural validation, without filesystem, network or credential-store access.

There is no command to change a run's frozen configuration. Catalog or member edits therefore affect only future bound runs. `Run` adds `owner: RunOwner::Rust`, `version`, `config: Option<Box<RunConfig>>`, `execution: Option<RunExecution>`, and `result: Option<Value>` to its ID and status. Legacy unbound runs keep their stage-one task behavior but cannot enter this new Run lifecycle.

`RunConnection { worker_id, connection_id, generation }` identifies an execution connection. `RunExecution { connection, thread_id: Option<String>, turn_id: Option<String> }` records acknowledged external IDs. `Run::require_connection` checks the active connection and generation for incoming facts. It does not authorize an approval; the host/transport must additionally scope the approval request to its thread/turn and live transport request ID.

Every `TransitionRun` includes the currently observed Run version. Its nested `transition` uses a snake_case `type` tag:

| Transition fields | Required state → resulting state |
| --- | --- |
| `Begin { worker_id, connection_id }` | pending or paused → starting |
| `BindThread { connection, thread_id }` | starting → starting |
| `BindTurn { connection, turn_id }` | starting with known thread → running |
| `Pause { connection, evidence_ref }` | starting or running → paused |
| `MarkOutcomeUnknown { connection }` | starting or running → outcome_unknown |
| `Reconcile { evidence_ref }` | outcome_unknown → paused |
| `Submit { execution, result }` | running → submitted |
| `Integrate { evidence_ref }` | submitted → integrated |
| `Accept { evidence_ref }` | integrated → accepted |

Commit `Begin` **before** issuing an external start/resume. A lost response or crash after this durable intent requires outcome reconciliation, not automatic external replay. A replayed Begin receipt proves only that the intent was committed. Thread and turn replies are recorded separately. Begin requires a frozen member, generates the next connection generation, preserves any known thread, clears the previous turn and requires a different connection ID. BindThread cannot replace a known thread. Submit checks the entire execution identity, including the turn. Retired connections cannot mutate later executions, even when using the current Run version.

Pause records confirmation of a safe stop, not merely sending an interrupt request. Reconcile records the trusted coordinator's evidence that continuation is safe; the store checks that the reference is nonblank, not its truth. A submitted result is still a candidate. Integration and user acceptance require separate evidenced decisions. These commands are for trusted product hosts, not direct model or worker access.

For bound runs, task claim/renew/submit require a running Run and its current execution member. Begin refuses unresolved running/outcome-unknown task attempts; Submit refuses any task that has not submitted. Run transitions do not silently clear task leases or reconcile their external effects: the coordinator must resolve those task records explicitly. New tasks cannot be created after Run submission. Stage-one unbound task commands remain compatible.

Run changes use the same command/state/event/outbox transaction as task changes. Events contain ID, owner, status, version, execution identity and (when relevant) the evidence reference. Frozen route/credential references, directories, configuration and result bodies remain in protected snapshots and receipts. Run-specific domain rejections are `DomainError::Run(RunError)`; version mismatch remains `DomainError::VersionConflict`. The existing private CLI accepts the new commands through its typed Execute request; no HTTP/runtime orchestration is implemented by this extension.
