# Minimal HTTP/SSE compatibility host

`forge-server` implements a tested subset of the current FastAPI wire contract:

- `GET /api/health`, `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me`.
- `GET /api/capabilities` reports the read-only migration surface so a host can
  reject an incompatible desktop backend before loading the application UI.
- Authorized `GET /api/runs` and `GET /api/runs/{run_id}`.
- `GET /api/runs/{run_id}/events` with exclusive cursors and `events` / `has_more` pages.
- `GET /api/runs/{run_id}/stream` with `Last-Event-ID` resumption, immediate keepalive, and live session/account/project revocation.
- Cookie attributes, mutation CSRF marker and Origin checks, JSON errors, query validation, and response security headers.

## Run and verify

From the repository root, set `FORGE_SERVER_ADMIN_PASSWORD` in the environment,
then run:

```sh
cargo run --manifest-path rust/Cargo.toml --locked -p forge-server -- --listen 127.0.0.1:8787
cargo test --manifest-path rust/Cargo.toml --locked -p forge-server
cargo clippy --manifest-path rust/Cargo.toml --locked -p forge-server --all-targets -- -D warnings
```

By default the executable binds only a loopback address, bootstraps username `admin`, and
uses a **disposable memory backend**. Runs initially form an empty list. There
is no default password and no HTTP endpoint for injecting fixture data. A
restart clears users, sessions, runs and events. Session tokens are random and
stored as SHA-256 digests; passwords use scrypt. Login failures are throttled
per username (ten attempts per five minutes), rather than the Python host's
client-IP/username pair. HTTP bodies and in-memory session/attempt counts are
bounded. Normal sessions and cookies last twelve hours.

Mutation requests, including login/logout, require `X-Forge-Request: 1`.
Requests with an Origin must match `ServerConfig.allowed_origins`; the executable
allows its literal listen URL. The library supports configurable origins and
Secure cookies. No CORS or Electron-specific bootstrap is provided.

## Embedding boundary

`router(Arc<dyn Backend>, ServerConfig)` produces an Axum router. `Backend`
supplies credential verification, current users and authorized run/event reads.
Synchronous calls run on Tokio's blocking pool. Each SSE polling batch resolves
the current session/user again and requests an authorized event page. Revocation
emits `event: revoked` and ends the body; backend failure emits `event: error`
and ends it. Disconnect drops the polling body without a detached producer.

The trusted backend owns authorization and read projections. Event pages must
filter by Run **before** applying limits; Event DTOs contain exactly `id`,
`run_id`, `kind`, `data`, `created_at`. RunView's summary/detail JSON must already
be frontend-compatible and must exclude private claim tokens or credentials.
The host does not accept arbitrary kernel commands, worker IDs, state mutations,
or fixture writes from clients.

`GET /api/capabilities` is intentionally explicit: this binary exposes reads,
event streaming, and process-local sessions only. `create_run`, run control,
decisions, approvals, and artifact downloads are reported as unavailable. A
desktop launcher must require a future writable host capability before routing
the UI here; a successful health check alone is not sufficient.

`MemoryBackend` provides Rust-only `add_user`, `set_user_enabled`,
`upsert_project`, `put_run`, and `append_event` methods for isolated tests and
embedding. The integration tests seed events 100–102 with Chinese payloads and
mirror `backend/tests/test_rust_migration_contract.py` through real TCP requests.
They additionally exercise project isolation, maximum-size pagination, logout
and membership revocation, and stream closure.

## Python product data

Pass `--python-db /absolute/path/to/forge.db` to read an existing Python product
database with existing accounts. `PythonSqliteBackend` projects current product
ownership, run details and events using read-only SQLite transactions. It never
migrates, bootstraps or writes product records. See [PYTHON_SQLITE.md](PYTHON_SQLITE.md)
for the field mapping, password format, authorization and redaction limits.

For a Rust-owned run database, pass both files:

```sh
cargo run --manifest-path rust/Cargo.toml --locked -p forge-server -- \
  --python-db /absolute/path/to/forge.db --rust-db /absolute/path/to/rust.sqlite3
```

`RustOwnedBackend` keeps Python users and project membership as the live ACL
source and opens the Rust database as the sole owner of Run, Task, event,
outbox, and approval records. It exposes approval listing/decisions and
acceptance decisions. Run controls require an embedding `RunController` that
has actually stopped or steered the process; without one the endpoint returns
`409` instead of pretending a state transition occurred.

## Remaining integration

The Python-only mode remains read-only and does not connect the Rust kernel
database. The combined `RustOwnedBackend` mode connects the two stores with a
strict ownership split: Python supplies live identity/ACL reads, Rust owns new
Run writes. This host does not yet provide Run creation, full task execution
control, NDJSON/downloads, asset management, desktop lifecycle, durable
authentication or data migration. Passing its tests proves the listed
transport contracts, not a completed desktop switch.
