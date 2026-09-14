#!/usr/bin/env python3
"""Verify Python product reads -> Rust-owned Run -> Codex -> artifact acceptance.

All model traffic stays on loopback. Python seeds a disposable product database
and reads it through a read-only mapper; forge-run is the sole execution and
state owner. --runtime codex uses the unmodified project binary, while fixture
uses the deterministic App Server protocol fixture. No paid provider is used.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ResponsesFixture(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *_args):
        pass

    def do_POST(self):  # noqa: N802
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            if self.path != "/v1/responses" or self.headers.get("Authorization") != "Bearer " + self.server.token:
                raise ValueError("unexpected Responses path or credential")
            if body.get("model") != "fixture-model":
                raise ValueError("frozen model did not reach provider")
            number = len(self.server.requests) + 1
            functions = {tool.get("name") for tool in body.get("tools", [])}
            outputs = [item for item in body.get("input", []) if item.get("type") == "function_call_output"]
            self.server.requests.append({"path": self.path, "model": body["model"],
                                         "authenticated": True, "tool_outputs": len(outputs),
                                         "routing_header": self.headers.get("x-forge-route")})
            if number == 1:
                command = "printf 'RUST_OWNED_OK\\n' > result.txt"
                if "exec_command" in functions:
                    name, arguments = "exec_command", {"cmd": command, "yield_time_ms": 1000, "max_output_tokens": 200}
                elif "shell_command" in functions:
                    name, arguments = "shell_command", {"command": command, "timeout_ms": 10000}
                elif "shell" in functions:
                    name, arguments = "shell", {"command": ["/bin/sh", "-c", command], "timeout_ms": 10000}
                else:
                    raise ValueError("Codex advertised no supported shell tool: " + repr(sorted(str(name) for name in functions)))
                item = {"type": "function_call", "call_id": "forge-fixture-call", "name": name,
                        "arguments": json.dumps(arguments)}
            elif number == 2 and any(output.get("call_id") == "forge-fixture-call" for output in outputs):
                item = {"type": "message", "role": "assistant", "id": "forge-fixture-message",
                        "content": [{"type": "output_text", "text": "RUST_OWNED_OK"}]}
            else:
                raise ValueError("unexpected retry or missing tool result")
            ident = "forge-response-" + str(number)
            events = [
                {"type": "response.created", "response": {"id": ident}},
                {"type": "response.output_item.done", "item": item},
                {"type": "response.completed", "response": {"id": ident, "status": "completed",
                    "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}},
            ]
            payload = "".join("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n" for event in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except Exception as error:
            self.server.errors.append(str(error))
            self.send_error(400, "invalid fixture request")


@contextmanager
def provider_fixture():
    server = ThreadingHTTPServer(("127.0.0.1", 0), ResponsesFixture)
    server.token, server.requests, server.errors = secrets.token_urlsafe(32), [], []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def seed_product(path: Path, base_url: str, permission: str):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from forge.db import Agent, Base, ModelProfile, Project, Provider, ProviderRevision, User

    engine = create_engine("sqlite:///" + str(path))
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([
            User(id="fixture-user", username="fixture", password_hash="unused", role="developer", enabled=True),
            Project(id="fixture-project", owner_id="fixture-user", config={
                "name": "Rust fixture", "execution_mode": "local", "permission_profile": permission,
                "allow_external_models": False, "members": [], "runtime": {},
            }),
            Agent(id="fixture-agent", version=1, config={
                "name": "Fixture worker", "role": "developer", "model_profile_id": "fixture-profile",
                "runtime": {}, "model_parameters": {}, "skills": [], "fallback_model_ids": [],
            }),
            ModelProfile(id="fixture-profile", version=1, config={
                "name": "Fixture model", "provider_id": "fixture-provider", "model_id": "fixture-model",
                "parameters": {}, "allowed_parameters": [], "supports_tools": True,
            }),
            Provider(id="fixture-provider", name="Local Responses fixture", revision_id="fixture-revision", enabled=True),
            ProviderRevision(id="fixture-revision", provider_id="fixture-provider", config={
                "protocol": "openai_responses", "base_url": base_url, "external": False,
            }, encrypted_key="never-read-or-decrypt-this-fixture-value"),
        ])
        session.commit()
    engine.dispose()


def invoke(binary: Path, *args, token=None, expected_success=True):
    env = {key: value for key, value in os.environ.items() if not key.startswith("FORGE_")}
    if token:
        env["FORGE_RUN_API_KEY"] = token
    process = subprocess.run([str(binary), *map(str, args)], capture_output=True, text=True, env=env, timeout=100)
    if (process.returncode == 0) != expected_success:
        raise RuntimeError("forge-run failed: " + (process.stderr or process.stdout)[-1200:])
    return process


def verify(root: Path, runtime: str, binary: Path) -> dict:
    from export_rust_run_config import export_run_input_with_evidence

    root.mkdir()
    workspace = root / "workspace"
    workspace.mkdir()
    product_db = root / "product.sqlite3"
    rust_db = root / "rust.sqlite3"
    with provider_fixture() as provider:
        base_url = f"http://127.0.0.1:{provider.server_port}/v1"
        real = runtime == "codex"
        executable = ROOT / (".local/runtimes/codex/bin/codex" if real else "rust/crates/forge-runtime/tests/fixtures/execution_app_server.py")
        if not executable.is_file():
            raise RuntimeError("missing project Codex binary; use --runtime fixture or build the upstream")
        seed_product(product_db, base_url, "full" if real else "ask")
        before = sha256(product_db)
        run_input, source = export_run_input_with_evidence(
            product_db, project_id="fixture-project", actor_id="fixture-user", agent_id="fixture-agent",
            run_id="rust-owned-run", task_id="rust-owned-task", runtime_executable=str(executable),
            working_directory=str(workspace), runtime_home=str(root / "runtime"),
            runtime_version="sha256:" + sha256(executable), responses_base_url=base_url,
            gateway_route_id="local-responses-fixture", gateway_version="fixture-v1",
            capability_evidence_ref="fixture:verified-tool-roundtrip", prompt="Write the requested result file.",
            artifact_path="result.txt" if real else "fixture-result.txt",
            expected_content="RUST_OWNED_OK\n" if real else "fixture-turn-1\n",
            fixture_mode=None if real else "normal",
            approved_command=None if real else "printf 'fixture-turn-1\\n' >> fixture-result.txt",
        )
        (root / "input.json").write_text(json.dumps(run_input), encoding="utf-8")
        (root / "source-evidence.json").write_text(json.dumps(source, indent=2), encoding="utf-8")
        invoke(binary, "execute", "--db", rust_db, "--input", root / "input.json", token=provider.token)
        with sqlite3.connect(rust_db.as_uri() + "?mode=ro", uri=True) as database:
            submitted = json.loads(database.execute("SELECT snapshot_json FROM runs").fetchone()[0])
            assert submitted["owner"] == "rust" and submitted["status"] == "submitted"
            events = [(kind, json.loads(payload)) for kind, payload in database.execute("SELECT kind, payload_json FROM events ORDER BY id")]
            assert any(kind == "runtime_event" and payload["terminal_status"] == "completed" for kind, payload in events)
            assert database.execute("SELECT COUNT(*) FROM events").fetchone()[0] == database.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
            route_before = submitted["config"]["route_binding"]
            assert route_before["provider_revision_id"] == "fixture-revision"
            assert len(route_before["config_digest"]) == 64
            provider_row = database.execute(
                "SELECT p.snapshot_json FROM run_provider_snapshots r "
                "JOIN provider_snapshots p USING (provider_id, provider_revision_id) "
                "WHERE r.run_id = ?",
                ("rust-owned-run",),
            ).fetchone()
            assert provider_row is not None
            provider_snapshot = json.loads(provider_row[0])
            assert provider_snapshot["provider_revision_id"] == "fixture-revision"
            assert provider_snapshot["config_digest"] == route_before["config_digest"]
            assert database.execute(
                "SELECT COUNT(*) FROM run_permissions WHERE run_id = ?", ("rust-owned-run",)
            ).fetchone()[0] == 1
            approval_count = database.execute(
                "SELECT COUNT(*) FROM runtime_approvals WHERE run_id = ?", ("rust-owned-run",)
            ).fetchone()[0]
            assert approval_count == (0 if real else 1)
        invoke(binary, "execute", "--db", rust_db, "--input", root / "input.json", token=provider.token, expected_success=False)
        invoke(binary, "accept", "--db", rust_db, "--run", "rust-owned-run")
        invoke(binary, "accept", "--db", rust_db, "--run", "rust-owned-run")
        with sqlite3.connect(rust_db.as_uri() + "?mode=ro", uri=True) as database:
            accepted = json.loads(database.execute("SELECT snapshot_json FROM runs").fetchone()[0])
            assert accepted["status"] == "accepted" and accepted["config"]["route_binding"] == route_before
            rows = database.execute("SELECT kind, payload_json FROM events ORDER BY id").fetchall()
            assert sum(kind == "run_accepted" for kind, _ in rows) == 1
            serialized = json.dumps(rows)
            assert provider.token not in serialized and "printf" not in serialized
        assert sha256(product_db) == before, "Python product database changed"
        with sqlite3.connect(product_db.as_uri() + "?mode=ro", uri=True) as database:
            assert database.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
            assert database.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        if real:
            assert not provider.errors, provider.errors
            assert len(provider.requests) == 2, provider.requests
            assert provider.requests[-1]["tool_outputs"] >= 1
        report = {
            "ok": True, "runtime": runtime, "owner": accepted["owner"], "status": accepted["status"],
            "execution": accepted["execution"], "runtime_version": run_input["config"]["runtime_version"],
            "product_database_unchanged": True, "python_runs": 0, "python_tasks": 0,
            "provider_revision_id": route_before["provider_revision_id"], "route_digest": route_before["config_digest"],
            "runtime_events": sum(kind == "runtime_event" for kind, _ in rows),
            "event_count": len(rows), "provider_requests": provider.requests,
            "artifact_sha256": accepted["result"]["artifact_sha256"],
            "separate_acceptance": True, "duplicate_execution_rejected": True,
        }
        (root / "verification.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", choices=("fixture", "codex", "all"), default="fixture")
    parser.add_argument("--output", type=Path, help="fresh absolute directory retaining databases and evidence")
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()
    if not args.skip_build:
        subprocess.run(["cargo", "build", "--manifest-path", str(ROOT / "rust/Cargo.toml"), "--locked", "-p", "forge-cli", "--bins"], check=True)
    binary = ROOT / "rust/target/debug/forge-run"
    with tempfile.TemporaryDirectory(prefix="forge-owned-verification-") as temporary:
        root = args.output or Path(temporary)
        if args.output:
            if not root.is_absolute():
                parser.error("--output must be absolute")
            root.mkdir(mode=0o700, parents=True)
        reports = [verify(root / mode, mode, binary) for mode in (("fixture", "codex") if args.runtime == "all" else (args.runtime,))]
        print(json.dumps({"ok": True, "checks": reports, "evidence_directory": str(root) if args.output else None}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
