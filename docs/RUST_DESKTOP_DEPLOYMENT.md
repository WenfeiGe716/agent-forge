# Rust Desktop and Container Deployment

The Electron main process launches `forge-server --desktop`. Credentials travel
through the existing one-line stdin protocol. The Rust process prepares a staged
workspace, receives keychain-decrypted credentials at launch, owns HTTP and Run
execution, and shuts down on SIGTERM or parent exit. The renderer and Codex source
are unchanged.

## Desktop

```sh
node scripts/build-offline.mjs
cargo build --locked --manifest-path rust/Cargo.toml -p forge-server
npm --prefix desktop start
node scripts/build-desktop.mjs
```

The package contains the release Rust executable, the existing frontend assets,
and the repository-pinned Codex binary built by `scripts/build-upstreams.sh`.
Python/PyInstaller is no longer in the desktop launch or build dependency chain.
An existing `forge.db` is imported once to `product.sqlite3`; it is preserved.
Fresh runs are stored in the separate `runs.sqlite3` transaction kernel. Existing
workspace `secrets.enc` remains protected by Electron safeStorage.

## Local Development

`bash start.sh` builds and launches the Rust HTTP server at `127.0.0.1:8787`.
Use `--no-build` after a successful build or `--port 8788` for another port.
It preserves `.local/config.json` credentials and imports `.local/forge.db` once
into `.local/product.sqlite3`, with new runs in `.local/runs.sqlite3`. Initial
passwords are stored in the private configuration file, never printed to logs.
Build frontend assets with `node scripts/build-offline.mjs` first.

The importer uses SQLite online backup and adds tables absent from older Python
catalogs only in its private target. It does not ignore live WAL transactions.
The real local catalog was imported with matching asset counts and unchanged
source database SHA-256; see [cutover validation](VALIDATION_RUST_CUTOVER.md).

`bash start.sh --legacy` is the explicit Python rollback launcher. Stop existing
tasks before switching hosts. The two catalogs do not synchronize later changes.

## Containers

The default Compose API is `infra/Dockerfile.rust`. Build the Linux Codex runtime
for the container architecture and put it at `runtime/linux/codex`, or set
`FORGE_RUNTIME_DIR` to its directory. Runtime files are mounted read-only.

Do not run `docker compose down -v` during migration. The existing named
`postgres-data` volume remains declared and is used only by the explicit legacy,
migration, or ops profiles. The Rust API writes the new `rust-state` volume.

For an existing PostgreSQL installation, stop legacy worker activity, then export
and import before starting the Rust API:

```sh
docker compose stop worker api
docker compose --profile migration run --rm export-legacy
docker compose --profile migration run --rm import-legacy
docker compose up --build -d api web
```

The export uses a repeatable-read, read-only PostgreSQL transaction, refuses
active runs, excludes browser sessions, and refuses an existing export file. The
Rust importer verifies and publishes a distinct SQLite product database. The
PostgreSQL source and volume remain intact. Keep the original `ENCRYPTION_KEY` so
imported provider credentials remain usable. `backend/Dockerfile` remains solely
for the explicit one-time export and legacy profile until old installations no
longer need that migration path.

To import a standalone SQLite export directly:

```sh
forge-server --product-db /private/product.sqlite3 \
  --import-db /backup/legacy-product.sqlite3 --migrate-only
```

The import command never replaces an existing product catalog. A missing source,
invalid schema or existing destination must be inspected before retrying.

## Execution Scope

The Rust HTTP product surface includes asset writes, account/session persistence,
model tests and benchmarks, design publication, approvals and artifact downloads.
Its native Run executor currently supports one member using local Codex, with
pause/resume on the same thread, runtime approval and artifact acceptance.
Provider revisions may include up to 20 validated static HTTP headers for
tenant or routing metadata. Header names that can carry credentials or hop-by-hop
transport state, templated values, and non-ASCII/control values are rejected;
the same frozen map is applied by the OpenCodex gateway or direct Responses
client. Credentials remain encrypted and are injected separately.
Multiple members, custom tool permissions, Skills, workflow/template/routing
references, automatic project checks and container execution remain explicitly
unsupported. Runtime limits are merged from platform, project, agent and Run
configuration and frozen into the Run snapshot. The executor enforces request,
active turn and observed command deadlines, bounds the prompt and result, and
uses `max_model_calls` to bound started Codex turns across resumes. Individual
provider calls within a Codex turn are not yet independently metered.
Custom model parameters are validated against the selected protocol and rejected
when the Codex transport cannot represent them; they are never silently ignored.
Automatic fallback is rejected before Run creation until provider rejections can
be classified without replaying an uncertain request. The defaults are 120-second
protocol/command waits and a one-hour active turn, with two active runs
(`MAX_ACTIVE_RUNS` can change this). Time waiting for human approval is bounded by
the active-turn deadline, not the command deadline.

Legacy question/installation/permission decisions are persisted with audit and
outbox records. A normal question answer is steered into a live Rust-owned Run
when one exists; otherwise its `answer` outbox entry remains unsent for later
reconciliation. Native Codex interaction uses runtime approval requests; a saved
legacy decision is not proof that an operation executed. An unknown execution
outcome requires investigation and is never automatically replayed. Full
execution parity is separate from HTTP and desktop migration.
