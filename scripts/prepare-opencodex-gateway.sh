#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UPSTREAM="$ROOT/third_party/opencodex"
LOCK="$ROOT/third_party/UPSTREAMS.lock"
PATCH="$ROOT/patches/opencodex/0001-require-token-on-loopback.patch"
BUILD_ROOT="$ROOT/.local/build"
TARGET="$BUILD_ROOT/opencodex-gateway-source"
MANIFEST="$TARGET/.forge-build.json"

if [[ ! -d "$UPSTREAM/.git" && ! -f "$UPSTREAM/.git" ]]; then
  echo "OpenCodex source checkout is missing: $UPSTREAM" >&2
  exit 1
fi
if [[ ! -f "$LOCK" || ! -f "$PATCH" ]]; then
  echo "OpenCodex lock or patch is missing" >&2
  exit 1
fi

expected="$(sed -n 's/^opencodex.commit = "\([0-9a-f]\{40\}\)"$/\1/p' "$LOCK")"
actual="$(git -C "$UPSTREAM" rev-parse HEAD)"
if [[ -z "$expected" || "$actual" != "$expected" ]]; then
  echo "OpenCodex checkout does not match UPSTREAMS.lock ($expected vs $actual)" >&2
  exit 1
fi

patch_sha="$(shasum -a 256 "$PATCH" | awk '{print $1}')"
if [[ -e "$TARGET" ]]; then
  if [[ ! -f "$MANIFEST" ]]; then
    echo "Refusing to overwrite existing unmanifested build directory: $TARGET" >&2
    exit 1
  fi
  manifest_commit="$(sed -n 's/.*"upstream_commit"[[:space:]]*:[[:space:]]*"\([0-9a-f]*\)".*/\1/p' "$MANIFEST")"
  manifest_patch="$(sed -n 's/.*"patch_sha256"[[:space:]]*:[[:space:]]*"\([0-9a-f]*\)".*/\1/p' "$MANIFEST")"
  if [[ "$manifest_commit" != "$expected" || "$manifest_patch" != "$patch_sha" ]]; then
    echo "Refusing to overwrite build with mismatched manifest: $TARGET" >&2
    exit 1
  fi
  if ! GIT_CEILING_DIRECTORIES="$BUILD_ROOT" git -C "$TARGET" apply --reverse --check "$PATCH" \
      || ! rg -q 'configuredApiAuthToken\(config\).*isLoopbackHostname' "$TARGET/src/server/auth-cors.ts"; then
    echo "Refusing to trust a build whose recorded auth patch is not applied: $TARGET" >&2
    exit 1
  fi
  echo "OpenCodex gateway source already prepared: $TARGET"
  exit 0
fi

mkdir -p "$BUILD_ROOT"
tmp="$(mktemp -d "$BUILD_ROOT/.opencodex-gateway-source.XXXXXX")"
cleanup() {
  if [[ "$tmp" == "$BUILD_ROOT"/.opencodex-gateway-source.* && -d "$tmp" && ! -L "$tmp" ]]; then
    rm -rf -- "$tmp"
  fi
}
trap cleanup EXIT
export GIT_CEILING_DIRECTORIES="$BUILD_ROOT"

git -C "$UPSTREAM" archive --format=tar "$expected" | tar -x -C "$tmp"
git -C "$tmp" apply --check "$PATCH"
git -C "$tmp" apply "$PATCH"
git -C "$tmp" apply --reverse --check "$PATCH"
rg -q 'configuredApiAuthToken\(config\).*isLoopbackHostname' "$tmp/src/server/auth-cors.ts"

bun_path=""
for candidate in "$ROOT"/.local/build/gateway-toolchain/node_modules/@oven/bun-*/bin/bun; do
  if [[ -x "$candidate" ]]; then bun_path="$candidate"; break; fi
done
if [[ -z "$bun_path" ]]; then
  echo "Project-owned Bun 1.4.2 is missing under .local/build/gateway-toolchain" >&2
  exit 1
fi
if [[ "$($bun_path --version)" != "1.4.2" ]]; then
  echo "Project-owned Bun must be version 1.4.2" >&2
  exit 1
fi
(cd "$tmp" && BUN_INSTALL_CACHE_DIR="$BUILD_ROOT/bun-cache" "$bun_path" install --frozen-lockfile --ignore-scripts)

printf '{"upstream_commit":"%s","patch_sha256":"%s"}\n' "$expected" "$patch_sha" > "$tmp/.forge-build.json"
mv "$tmp" "$TARGET"
trap - EXIT
echo "Prepared OpenCodex gateway source: $TARGET"
