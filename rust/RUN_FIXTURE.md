# Rust-owned Run fixture

This is a private, opt-in execution host, not the desktop backend. It proves a
single Rust-owned Run using either the protocol fixture or the unmodified
project Codex binary. No frontend or Codex source changes are required.

## Reproduce

From the repository root, with the existing Python environment and Rust installed:

```sh
.venv/bin/python scripts/verify-rust-owned-run.py --runtime fixture
.venv/bin/python scripts/verify-rust-owned-run.py --runtime all \
  --output /absolute/new/evidence-directory
cargo test --manifest-path rust/Cargo.toml --locked --workspace
cargo clippy --manifest-path rust/Cargo.toml --locked -p forge-cli --all-targets -- -D warnings
PYTHONPATH=backend .venv/bin/python -m pytest backend/tests/test_rust_run_export.py -q
```

`--runtime all` additionally requires `.local/runtimes/codex/bin/codex`. The
script builds `forge-run` unless `--skip-build` is supplied. The output directory
must be fresh. Without `--output`, all generated databases and workspaces are
temporary. Both modes run without paid credentials or external model traffic.

Each verification creates the actual Python SQLAlchemy product schema and seeds
one user, local project, agent, model profile and provider revision. The mapper
then opens this SQLite database with `mode=ro`, `query_only=ON` and one read
transaction. It never selects the password or encrypted credential columns and
never creates a Python Run or Task. Database byte hashes and zero Python Run/Task
counts are checked after Rust execution and acceptance.

## Ownership and flow

1. `scripts/export_rust_run_config.py` reads authorized product configuration and
   returns a strict `RunInput` document plus retained source evidence. It reuses
   the product's member > team > project permission resolution. Member worker IDs
   are selected Agent IDs; project `members` are user access IDs.
2. `forge-run execute` validates the local route against the exported immutable
   provider snapshot, hashes its frozen binding, and commits the provider
   revision, permission policy, Run, Task and start intent through `forge-store`
   before starting any process. The private Rust database has the Forge
   application ID.
3. `forge-runtime` starts the isolated App Server. Thread creation receives the
   frozen Responses URL, model and permission tuple. Credentials come only from
   `FORGE_RUN_API_KEY`, not the input document or the product's encrypted key.
4. The host binds thread and turn IDs and persists scoped runtime event metadata,
   command receipts and outbox records in Rust transactions. Approval requests
   are recorded in `runtime_approvals`, then decided and dispatched exactly once
   for the command authorized in the controlled input.
5. A successful command tool item, completed turn and fresh output file permit
   task and Run submission. This leaves the Run `submitted`.
6. A separate `forge-run accept` process reads the artifact, checks both its
   submitted fingerprint and the expected fingerprint frozen before execution,
   then records integration and acceptance as separate transitions.

The real Codex mode has a two-response local model fixture: it requests a shell
tool to write `RUST_OWNED_OK` to `result.txt`, then receives Codex's tool output
and ends the turn. The fixture verifies the route, model and credential on both
requests. The host does not write this artifact itself.

The private commands are:

```sh
FORGE_RUN_API_KEY=local-fixture-token rust/target/debug/forge-run execute \
  --db /absolute/private/rust.sqlite3 --input /absolute/controlled/input.json
rust/target/debug/forge-run accept \
  --db /absolute/private/rust.sqlite3 --run rust-owned-run
```

The verifier demonstrates the mapper's keyword arguments. Existing product
databases can be supplied to `export_run_input_with_evidence`; use a fresh Rust
database and retain both returned documents. This local adapter assumes an
already trusted operator and must not be exposed directly as an HTTP/MCP API.

## Failure guarantees

- An existing Run ID never starts another process. Repeating acceptance is
  idempotent and does not create another acceptance event.
- Unknown command approvals fail closed. Approved requests are fenced by the
  exact connection, thread, turn, item and immutable approval digest.
- Transport failure after the start intent records `outcome_unknown`. A lost
  turn reply or exit after the tool effect cannot trigger automatic replay.
  Outstanding task leases remain independently fenced; this host does not offer
  automatic reconciliation or resume.
- A wrong expected value or an artifact modified after submission fails
  acceptance. A completed Codex turn alone cannot accept the Run.
- Shared runtime events contain metadata, not raw model output, command text,
  credentials or artifacts. Detailed approval requests remain protected state.

## Scope and evidence

The supported product subset is one developer/tester with one local Responses
model, no fallback, custom tool allowlist, skills, model parameters, runtime
overrides or project check commands. Unsupported configuration is rejected.
The local fixture serves the Responses route directly; this does not validate
OpenCodex translation, arbitrary upstream protocols, remote providers, HTTP
product writes, desktop migration or full task recovery.

Verified on 2026-09-12: the protocol fixture reached `accepted` with 9 runtime
events; the project Codex binary reached `accepted` with 17 runtime events and
two provider requests, including one tool-output round trip. Both product
databases remained unchanged. Reports are retained in
`docs/evidence/rust-owned-run-fixture.json` and
`docs/evidence/rust-owned-run-codex.json`.

Focused regression tests are `forge-cli/tests/owned_run.rs` (process execution,
separate acceptance, mutation rejection, approval denial, uncertain effects and
no replay), core/store runtime-event tests (identity, deduplication and rollback),
and `backend/tests/test_rust_run_export.py` (read-only product mapping).
