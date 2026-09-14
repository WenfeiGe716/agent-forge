#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_ROOT="$ROOT/.local/build"
RUNTIME_ROOT="$ROOT/.local/runtimes"
SOURCE_ROOT="$ROOT/third_party/codex/codex-rs"
TOOLCHAIN="$(sed -n 's/^channel = "\([^"]*\)"/\1/p' "$SOURCE_ROOT/rust-toolchain.toml")"

# Keep all build caches and outputs inside Agent Forge. This script never uses
# the operator's ~/.codex, ~/.config/opencodex, or global CLI configuration.
mkdir -p "$BUILD_ROOT/build-home" "$BUILD_ROOT/cargo-home" "$BUILD_ROOT/codex-target" "$RUNTIME_ROOT/codex/bin"
# Capture rustup's already-installed toolchain location, then keep every
# command that can consult Cargo configuration inside the project.
export RUSTUP_HOME="${RUSTUP_HOME:-$HOME/.rustup}"
export CARGO_HOME="$BUILD_ROOT/cargo-home"
export CARGO_TARGET_DIR="$BUILD_ROOT/codex-target"
export HOME="$BUILD_ROOT/build-home"
export RUSTUP_TOOLCHAIN="$TOOLCHAIN"

if ! command -v rustup >/dev/null 2>&1 || [[ -z "$TOOLCHAIN" ]]; then
  echo "缺少 rustup 或上游工具链声明，无法构建项目内 Codex 源码" >&2
  exit 1
fi

for upstream in codex opencodex; do
  expected="$(awk -v key="$upstream.commit" '$1 == key {gsub(/"/, "", $3); print $3}' "$ROOT/third_party/UPSTREAMS.lock")"
  actual="$(git -C "$ROOT/third_party/$upstream" rev-parse HEAD)"
  if [[ -z "$expected" || "$actual" != "$expected" ]]; then
    echo "$upstream 源码版本不匹配 UPSTREAMS.lock，请先审核并同步锁定版本" >&2
    exit 1
  fi
  if [[ -n "$(git -C "$ROOT/third_party/$upstream" status --porcelain --untracked-files=normal)" ]]; then
    echo "$upstream 源码有本地修改；请用可审核补丁或新的锁定版本构建，禁止混入未记录改动" >&2
    exit 1
  fi
done
if ! rustup run "$TOOLCHAIN" rustc --version >/dev/null 2>&1; then
  echo "缺少 Rust $TOOLCHAIN；请单独安装此工具链，本脚本不会修改全局默认版本" >&2
  exit 1
fi
CARGO_BIN="$(rustup which cargo --toolchain "$TOOLCHAIN")"
if [[ ! -x "$CARGO_BIN" ]]; then
  echo "找不到 Rust $TOOLCHAIN 的 cargo" >&2
  exit 1
fi

# Codex's code-mode runtime enables v8_enable_sandbox.  The matching sandbox
# archive is published by the pinned Codex release (not by the default
# denoland/rusty_v8 release), so resolve and verify that pair before Cargo
# starts.  Use curl instead of Python urllib: on macOS the latter may not have
# the system CA bundle configured.  Set FORGE_CURL_PROXY when the local
# network requires an explicit proxy (for example http://127.0.0.1:7890).
V8_VERSION="$(awk '
  $0 == "name = \"v8\"" { found=1; next }
  found && $1 == "version" { gsub(/\"/, "", $3); print $3; exit }
' "$SOURCE_ROOT/Cargo.lock")"
HOST_TARGET="$(rustup run "$TOOLCHAIN" rustc -vV | sed -n 's/^host: //p')"
if [[ "$HOST_TARGET" == *-apple-darwin || "$HOST_TARGET" == *-unknown-linux-* ]]; then
  if ! command -v curl >/dev/null 2>&1; then
    echo "构建 sandbox V8 需要 curl" >&2
    exit 1
  fi
  V8_CACHE="$BUILD_ROOT/rusty-v8-${V8_VERSION}-${HOST_TARGET}"
  V8_BASE="https://github.com/openai/codex/releases/download/rusty-v8-v${V8_VERSION}"
  V8_PROFILE="ptrcomp_sandbox_release"
  V8_ARCHIVE="librusty_v8_${V8_PROFILE}_${HOST_TARGET}.a.gz"
  V8_BINDING="src_binding_${V8_PROFILE}_${HOST_TARGET}.rs"
  V8_CHECKSUMS="rusty_v8_${V8_PROFILE}_${HOST_TARGET}.sha256"
  V8_TRUSTED="$ROOT/third_party/codex/third_party/v8/rusty_v8_${V8_VERSION//./_}_release_manifests.sha256"
  mkdir -p "$V8_CACHE"
  if command -v shasum >/dev/null 2>&1; then
    HASH_CMD=(shasum -a 256)
  elif command -v sha256sum >/dev/null 2>&1; then
    HASH_CMD=(sha256sum)
  else
    echo "构建 sandbox V8 需要 shasum 或 sha256sum" >&2
    exit 1
  fi
  CURL_ARGS=(--fail --location --retry 8 --retry-delay 2 --connect-timeout 20 --max-time 300 --silent --show-error)
  if [[ -n "${FORGE_BUILD_PROXY:-}" ]]; then
    CURL_ARGS+=(--proxy "$FORGE_BUILD_PROXY")
  fi
  if [[ ! -f "$V8_CACHE/$V8_CHECKSUMS" ]]; then
    curl "${CURL_ARGS[@]}" "$V8_BASE/$V8_CHECKSUMS" -o "$V8_CACHE/$V8_CHECKSUMS.partial.$$"
    mv "$V8_CACHE/$V8_CHECKSUMS.partial.$$" "$V8_CACHE/$V8_CHECKSUMS"
  fi
  EXPECTED_MANIFEST="$(awk -v name="$V8_CHECKSUMS" '$2 == name { print $1; exit }' "$V8_TRUSTED")"
  ACTUAL_MANIFEST="$("${HASH_CMD[@]}" "$V8_CACHE/$V8_CHECKSUMS" | awk '{print $1}')"
  if [[ -z "$EXPECTED_MANIFEST" || "$ACTUAL_MANIFEST" != "$EXPECTED_MANIFEST" ]]; then
    echo "Codex V8 checksum manifest 校验失败（${V8_CHECKSUMS}）" >&2
    exit 1
  fi
  for artifact in "$V8_ARCHIVE" "$V8_BINDING"; do
    expected="$(awk -v name="$artifact" '$2 == name { print $1; exit }' "$V8_CACHE/$V8_CHECKSUMS")"
    current="$("${HASH_CMD[@]}" "$V8_CACHE/$artifact" 2>/dev/null | awk '{print $1}' || true)"
    if [[ "$current" != "$expected" ]]; then
      curl "${CURL_ARGS[@]}" "$V8_BASE/$artifact" -o "$V8_CACHE/$artifact.partial.$$"
      downloaded="$("${HASH_CMD[@]}" "$V8_CACHE/$artifact.partial.$$" | awk '{print $1}')"
      if [[ "$downloaded" != "$expected" ]]; then
        echo "Codex V8 归档校验失败（${artifact}）" >&2
        exit 1
      fi
      mv "$V8_CACHE/$artifact.partial.$$" "$V8_CACHE/$artifact"
    fi
  done
  (cd "$V8_CACHE" && "${HASH_CMD[@]}" -c "$V8_CHECKSUMS")
  export RUSTY_V8_ARCHIVE="$V8_CACHE/$V8_ARCHIVE"
  export RUSTY_V8_SRC_BINDING_PATH="$V8_CACHE/$V8_BINDING"
fi

# Avoid competing with the desktop apps on memory-constrained Macs.
"$CARGO_BIN" build --locked --jobs "${FORGE_BUILD_JOBS:-2}" --manifest-path "$SOURCE_ROOT/Cargo.toml" --release \
  --bin codex --bin codex-code-mode-host
install -m 0755 "$CARGO_TARGET_DIR/release/codex" "$RUNTIME_ROOT/codex/bin/codex"
if [[ ! -x "$CARGO_TARGET_DIR/release/codex-code-mode-host" ]]; then
  echo "缺少 Codex code-mode companion：$CARGO_TARGET_DIR/release/codex-code-mode-host" >&2
  exit 1
fi
install -m 0755 "$CARGO_TARGET_DIR/release/codex-code-mode-host" "$RUNTIME_ROOT/codex/bin/codex-code-mode-host"
install -m 0644 "$ROOT/third_party/codex/LICENSE" "$RUNTIME_ROOT/codex/LICENSE"
install -m 0644 "$ROOT/third_party/codex/NOTICE" "$RUNTIME_ROOT/codex/NOTICE"
install -m 0644 "$ROOT/third_party/UPSTREAMS.lock" "$RUNTIME_ROOT/codex/UPSTREAMS.lock"

echo "Codex runtime: $RUNTIME_ROOT/codex/bin/codex"
echo "OpenCodex source is pinned at third_party/opencodex; its setup/sync/service commands are intentionally not run by this script."
