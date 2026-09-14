# OpenCodex downstream patch

`0001-require-token-on-loopback.patch` makes an explicitly configured
`OPENCODEX_API_AUTH_TOKEN` apply to loopback data-plane requests too. The pinned
upstream normally admits loopback without examining any token. That default is
not suitable for a Forge gateway carrying project provider credentials.

The patch does not change health/readiness endpoints or management-plane auth.
Forge disables the optional unauthenticated listener. No native client setup,
sync, service, login or global configuration command is run.

The original submodule stays clean. `scripts/prepare-opencodex-gateway.sh`
exports its locked commit, checks/applies this patch to an independent build
copy, verifies the result and records the patch digest. Existing mismatched
builds are not overwritten automatically. On upgrade, review this patch,
rebuild, and rerun the real gateway authentication smoke test before use.

The exported source retains the upstream MIT license. This file records the
Forge-specific behavior change; it is not an upstream API stability guarantee.
