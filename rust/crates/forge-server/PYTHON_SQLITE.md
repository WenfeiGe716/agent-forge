# Python SQLite read adapter

`PythonSqliteBackend::open(path)` implements `forge_server::Backend` against an
explicit existing Python product database. Python remains its sole writer.
This adapter is independent of `forge-store` and does not migrate data, change
run ownership, start workers, execute commands or resolve approvals.

```sh
cargo run --manifest-path rust/Cargo.toml --locked -p forge-server -- \
  --listen 127.0.0.1:8788 --python-db /absolute/path/to/forge.db
```

Use existing Python usernames and passwords. This mode does not use
`FORGE_SERVER_ADMIN_PASSWORD` and never bootstraps an administrator. The host
still binds only loopback and implements the HTTP subset in [README.md](README.md).
Library users can pass `Arc::new(PythonSqliteBackend::open(path)?)` to `router`.
The desktop entry point and frontend remain on their current backend.

## Storage and mapping

The schema comes from `backend/forge/db.py`; `backend/forge/schema.py` defines
input validation, not SQLite tables. Opening validates required tables and
columns, accepts additional Python tables/columns, and rejects missing tables
or views substituted for them. It does not initialize or repair a database.
Nonexistent paths fail without creating files. SQL uses explicit columns and
bound request values. Underlying SQL errors and paths are not exposed in API
errors. Malformed projected JSON fails the read rather than returning raw text.

Every call opens with `SQLITE_OPEN_READ_ONLY` and enables connection-local
`query_only` and disables `trusted_schema`. It does not change journal mode,
application ID or schema version. Authorization and projection share one read
transaction; the next call sees subsequent committed Python changes, including
WAL events. No immutable snapshot flag is used. SQLite may manage WAL shared
memory sidecars according to its normal read-only connection requirements;
the database, schema and application records are never written by the adapter.

| Source | Public projection |
| --- | --- |
| `users` | `id`, `username`, `role`, `enabled`; hash only for login verification |
| `projects` | Current owner and `config.members` used only for authorization |
| `runs` | Python `run_out` summary and detailed fields, including snapshot defaults |
| `tasks` | `id`, `key`, `agent_id`, `kind`, `title`, `status`, `spec`, `result` |
| `outbox` | `id`, `action`, `payload`, `sent`, `error`; never dispatched |
| `events` | `id`, `run_id`, `kind`, `data`, `created_at` |

Run lists apply authorization before sorting by `created_at DESC` and limit
(1-500). Equal timestamps use ID order for deterministic pagination. Agent IDs
preserve Python snapshot object order. Detail includes the run's maximum event
ID, or zero, from the same read snapshot. Task and command order follows rowid.
Event queries filter by run and exclusive `id > after` before applying
`ORDER BY id LIMIT limit+1` (1-1000). Global events with NULL run IDs never appear.
Redaction retains events and IDs, so cursors and `has_more` do not skip records.

## Passwords and authorization

Python stores standard Base64 of 16 salt bytes and a 64-byte scrypt digest:
`N=16384, r=8, p=1`, UTF-8 password, `dklen=64`. The adapter strictly validates
that format and compares digests in constant time. It also performs the bounded
KDF for missing, disabled and malformed accounts. It neither rewrites hashes
nor accepts MemoryBackend PHC hashes as Python hashes. Account and hash are
rechecked after password verification to observe changes during the KDF.

Each data method reloads the user by ID; caller-supplied role/enabled values
cannot grant access. Only enabled users with a recognized role may read.
Access requires an existing project and current admin role, project ownership,
or string membership in a JSON array at `projects.config.members`. Missing or
malformed membership grants nothing. Historical snapshot membership and
`runs.owner_id` never grant access. Membership removal, role changes, project
deletion and account disablement take effect on subsequent reads/SSE batches.
A request already reading its transaction may finish with that snapshot.

Python `sessions` are never read or written. Rust login creates its own
in-memory sessions; restart invalidates them. Python logout/session expiry do
not revoke a Rust session, and password changes do not revoke an already issued
Rust session. Account disablement does. Both hosts use `forge_session`, so use a
separate browser profile or host name while comparing hosts; cookies do not
isolate by port. The adapter grants no execution or mutation permissions.

## Redaction boundary

Snapshot, plan, report, task spec/result, command payload and event data are
recursively copied with sensitive fields replaced by `[REDACTED]`. Key matching
ignores ASCII case and punctuation. It covers password/passwd/secret names,
API/encrypted/private/encryption/signing keys, credentials, authorization, cookies, headers,
environment variables, claims and token names (including suffixed token/hash
fields). Token usage counts (`input_tokens`, `output_tokens`, `max_tokens`),
ordinary task keys and permission descriptions remain visible. Credential
revision IDs remain references, not decrypted credentials. There is no bypass
for administrators. Raw task message history is not selected.

Provider revisions, encrypted provider keys, Python session hashes, private
kernel claims and runtime approval tables are never queried. This does not
mean historical event JSON is trusted: Python currently persists runtime
payloads intact, so Rust applies the same recursive filter to HTTP pages and
SSE events. Future sensitive field names must be added to the filter and tests.

This is **field-based redaction**, not content classification. It does not
detect secrets pasted into titles, requests, errors, prose, model deltas, URLs,
tool output or JSON serialized inside a string, or unknown innocuous field
names. Those remain project-authorized product content, as in Python. This
adapter is not a sanitized export or a credential-safe public event feed.
Do not expose its responses to users who lack access to that project's text.

## Verification

```sh
cargo test --manifest-path rust/Cargo.toml --locked --workspace
cargo clippy --manifest-path rust/Cargo.toml --locked -p forge-server --all-targets -- -D warnings
```

Integration fixtures create a temporary database from the actual Python ORM
metadata and password implementation. They do not open the installed product
database. Python 3 plus the backend dependencies are test-only prerequisites;
the adapter has no Python runtime dependency.
Tests prefer the repository's `.venv/bin/python`, then `python3`; set
`FORGE_TEST_PYTHON` to select another prepared interpreter.
