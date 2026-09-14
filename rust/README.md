# Forge Rust backend

This workspace owns the product HTTP backend used by Electron and the default
local/Compose launchers. `forge-server` runs authenticated product APIs and
single-member or team Codex sessions; `forge-kernel` remains a private NDJSON
host for kernel integration and diagnostics. Team execution and its current
limits are recorded in [VALIDATION_TEAM_RUNTIME.md](../docs/VALIDATION_TEAM_RUNTIME.md).

## Implemented modules

| Crate | Responsibility |
| --- | --- |
| `forge-core` | Typed task transitions, version/claim/lease/generation checks |
| `forge-store` | SQLite state, attempts, command receipts, events and outbox in one transaction |
| `forge-cli` | Persistent NDJSON command host and cross-process compatibility tests |
| `forge-runtime` | Isolated, unmodified Codex App Server subprocess and JSON-RPC transport |
| `forge-server` | Product HTTP/SSE, authentication, assets, approvals, team cooperation and Codex execution |

Only one store owns these new runs. A nonempty database without the Forge Rust
application ID is rejected. Point the kernel at a new private database; do not
point it at the Python application database. The server's separate
[Python adapter](crates/forge-server/PYTHON_SQLITE.md) can read that database
without transferring ownership. The product migration copies supported legacy
assets into a separate Rust product store; it does not delete the source data.

## Build and verify

The initial read-only migration fixture remains documented in
[RUN_FIXTURE.md](RUN_FIXTURE.md). Current HTTP and team verification use
`scripts/verify-rust-http-run.py` and `scripts/verify-rust-team-run.py`, with the
unchanged project Codex binary and a disposable local Responses provider.

From the repository root:

```sh
cargo test --manifest-path rust/Cargo.toml --locked --workspace
cargo clippy --manifest-path rust/Cargo.toml --locked --workspace --all-targets -- -D warnings
PYTHONPATH=backend .venv/bin/python -m pytest backend/tests/test_rust_migration_contract.py -q
```

The runtime tests use a deterministic Python 3 fixture to exercise process and
protocol failures; Python is a test dependency, not a Rust runtime dependency.
Real upstream read-only smoke (requires `.local/runtimes/codex/bin/codex`):

```sh
cargo run --manifest-path rust/Cargo.toml --locked -p forge-cli --example check_codex
```

An absolute alternate binary path may follow `--`. The example creates a
disposable isolated home, verifies `initialize.codexHome`, calls `thread/list`,
closes and reaps the child. It starts no model turn and proves no provider
compatibility or end-to-end task completion.

## Private command host

```sh
cargo run --manifest-path rust/Cargo.toml --locked -p forge-cli --bin forge-kernel -- --db /absolute/private/directory/kernel.sqlite3
```

The parent directory must already exist. Supply one JSON object followed by a
newline per request. The host remains running for subsequent commands and exits
on stdin EOF. Input is bounded at 1 MiB per frame. Example input:

```jsonl
{"op":"execute","command_id":"create-r1","command":{"type":"create_run","run_id":"r1"}}
{"op":"execute","command_id":"create-t1","command":{"type":"create_task","run_id":"r1","task_id":"t1"}}
{"op":"execute","command_id":"claim-t1","command":{"type":"claim_task","task_id":"t1","worker_id":"w1","expected_version":0,"lease_duration_ms":900000}}
{"op":"receipt","command_id":"claim-t1"}
{"op":"task","task_id":"t1"}
{"op":"events","after":0,"limit":100}
{"op":"outbox","limit":100}
```

Responses have `schema_version: 1`, `ok` and either `result` or `error`.
The typed command/receipt schema is in [CORE_STORE_API.md](CORE_STORE_API.md).
Claims return a random token that must accompany release/submission together
with the attempt generation. The caller cannot supply server time. A successful
command ID identifies a permanent receipt: retrying the identical command
returns that original receipt, including its original lease. A different payload
with the same ID is rejected. Reading an old receipt never renews its claim.

After a lost response, query `receipt` before deciding whether to retry. A missing
receipt can be retried using the identical command ID/payload; a successful
previous execution will not execute again. Domain rejections are not persisted
as successful command receipts.

`events.after` is exclusive; query limits are 1–1000. Outbox delivery is at least
once. The destination must deduplicate the stable event ID and durably accept
it before `{"op":"acknowledge_outbox","event_id":1}`. Acknowledgement returns
whether a pending record changed and never removes the historical event.
Multiple readers can see the same pending outbox record; that is intentional.

The private channel is for trusted hosts. It does not authenticate worker IDs
or filter events by user/project; it must not be exposed directly over HTTP,
MCP, or to models. Future product adapters must resolve caller identity, enforce
the frozen team policy and scope all queries. Claim fencing prevents stale
database submissions; it cannot roll back external commands or file writes.

## Source reuse

OMX task claim rules and Rust dispatch/ledger design were examined at commit
`cb955b0d5becbef76d2c1f0096b6e1f238e1e7f7` (MIT). This workspace implements a
SQLite transaction protocol; it does not depend on OMX's CLI, tmux, JSON store
or runtime binary. The protocol and failure cases are adapted to Forge's
ownership and outcome-unknown requirements. Codex is invoked as an external
unmodified binary. OpenCodex remains a separate gateway.

The current migration plan and explicit remaining work are recorded in
[PLAN_RUST_BACKEND.md](../docs/PLAN_RUST_BACKEND.md).

The first Rust-owned vertical slice is documented in [RUN_FIXTURE.md](RUN_FIXTURE.md).
Its trusted input includes a `ProviderSnapshot`; execution uses
`CreateProvisionedRun`, so the provider revision, route binding, execution
permission policy, approvals, runtime events and outbox are all durable in the
new Rust-owned SQLite database. `scripts/export_rust_run_config.py` reads the
existing Python product database in one read-only transaction and emits that
input plus key-free source evidence. The product database remains untouched.

The minimal, separately runnable HTTP host and its current limits are documented
in [forge-server/README.md](crates/forge-server/README.md). It does not yet replace
the desktop backend or connect the private kernel to public HTTP routes.
