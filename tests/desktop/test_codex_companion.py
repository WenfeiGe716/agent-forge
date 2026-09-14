"""Guard the bundled Codex companion required by Code Mode tool calls."""

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
RUNTIME_HOST = ROOT / ".local/runtimes/codex/bin/codex-code-mode-host"
APP_HOST = (
    ROOT
    / "desktop/release/Agent Forge-darwin-arm64/Agent Forge.app"
    / "Contents/Resources/backend/codex-code-mode-host"
)


def test_build_scripts_keep_code_mode_companion_in_the_allowlist():
    upstreams = (ROOT / "scripts/build-upstreams.sh").read_text()
    desktop = (ROOT / "scripts/build-desktop.mjs").read_text()
    assert "--bin codex-code-mode-host" in upstreams
    assert "codex-code-mode-host" in desktop
    assert "codexCodeModeHost" in desktop


@pytest.mark.skipif(not RUNTIME_HOST.is_file(), reason="build the pinned Codex runtime first")
def test_pinned_code_mode_companion_is_executable():
    assert RUNTIME_HOST.stat().st_mode & 0o111


@pytest.mark.skipif(not APP_HOST.exists(), reason="build the bundled macOS app first")
def test_bundled_code_mode_companion_is_executable():
    assert APP_HOST.stat().st_mode & 0o111
