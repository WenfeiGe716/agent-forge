# Vendored upstreams

This directory contains pinned git submodules:

- `codex` — OpenAI Codex, Apache-2.0
- `opencodex` — OpenCodex provider proxy, MIT

OMX (oh-my-codex) is intentionally **not** vendored as a third execution
runtime.  It is a Codex CLI workflow layer whose team/tmux state model is
adapted by Agent Forge; adding it as a submodule would create a second source
of truth for task state and risk invoking user-level setup.  If a future
headless OMX adapter is adopted, its version and patch set will be locked in a
separate manifest without touching `~/.codex`.

The lock file at [`UPSTREAMS.lock`](./UPSTREAMS.lock) is the source of truth
for the commits used by Agent Forge. Do not run their setup, service, shim,
sync, login, update, or uninstall commands against the operator's home
directory. Builds and runtime probes must set an Agent Forge-owned
`CODEX_HOME`/XDG data directory and use an explicit project environment.

## Build and verification

Run `bash scripts/build-upstreams.sh` from the repository. It verifies both
submodule revisions and clean source trees against the lock, reads Codex's declared Rust toolchain,
and builds with `--locked`. It never changes the default Rust toolchain or
runs either upstream's account/configuration commands. Install the declared
toolchain separately if missing. Caches and runtime binaries are ignored under
`.local/build` and `.local/runtimes`.

Run `.venv/bin/python scripts/verify-codex-runtime.py` to test the built
App Server handshake and create a thread in a disposable runtime home. This
does not call a model and is not an end-to-end task execution test.

When updating an upstream, review its source and license changes, update the
submodule and `UPSTREAMS.lock` together, build, and run protocol/isolation tests
before enabling the new runtime. Do not silently track the latest branch.
