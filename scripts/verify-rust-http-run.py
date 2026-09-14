#!/usr/bin/env python3
"""Exercise native Rust product writes through the unchanged project Codex.

Only the local Responses fixture receives model traffic. Python is test tooling;
the HTTP host, authentication, asset mutations and Run state owner are Rust.
"""
import argparse
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fixture", ROOT / "scripts/verify-rust-owned-run.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


def verify(directory, gateway):
    directory.mkdir(parents=True, exist_ok=False)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}/api"
    password = secrets.token_urlsafe(24)
    from cryptography.fernet import Fernet
    env = {key: value for key, value in os.environ.items() if not key.startswith("FORGE_")}
    env.update(ADMIN_PASSWORD=password, ENCRYPTION_KEY=Fernet.generate_key().decode(),
               FORGE_CODEX_BINARY=str(ROOT / ".local/runtimes/codex/bin/codex"))
    if gateway:
        env.update(FORGE_OPENCODEX_ENTRY=str(ROOT / "runtime/opencodex-gateway.ts"),
                   FORGE_BUN_BINARY=str(ROOT / ".local/build/gateway-toolchain/node_modules/@oven/bun-darwin-aarch64/bin/bun"))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    cookie = ""

    def request(path, value=None, key=None, binary=False):
        headers = {"X-Forge-Request": "1", "Cookie": cookie}
        if value is not None:
            headers["Content-Type"] = "application/json"
        if key:
            headers["Idempotency-Key"] = key
        data = None if value is None else json.dumps(value).encode()
        req = urllib.request.Request(base + path, data=data, headers=headers)
        try:
            with opener.open(req, timeout=60) as response:
                payload = response.read()
                return payload if binary else json.loads(payload)
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"HTTP {error.code}: {path}: {error.read()[:1000]!r}") from None

    command = [str(ROOT / "rust/target/debug/forge-server"), "--listen", f"127.0.0.1:{port}",
               "--product-db", str(directory / "product.sqlite3"), "--rust-db", str(directory / "runs.sqlite3"),
               "--data-root", str(directory / "data")]
    with (directory / "server.log").open("w") as log, fixture.provider_fixture() as provider:
        process = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("Rust host exited before readiness")
                try:
                    request("/health")
                    break
                except (urllib.error.URLError, ConnectionError):
                    time.sleep(0.1)
            login = urllib.request.Request(base + "/auth/login", data=json.dumps({"username": "admin", "password": password}).encode(), headers={"Content-Type": "application/json", "X-Forge-Request": "1"})
            with opener.open(login) as response:
                cookie = response.headers["Set-Cookie"].split(";", 1)[0]
            for path in ("/providers", "/models", "/agents", "/projects", "/runs", "/settings", "/organization"):
                request(path)
            p = request("/providers", {"name": "Local fixture", "protocol": "openai_responses",
                         "base_url": f"http://127.0.0.1:{provider.server_port}/v1", "api_key": provider.token, "external": False,
                         "protocol_config": {"headers": {"x-forge-route": "rust-http-fixture"}}})
            model = request("/models", {"name": "Fixture", "provider_id": p["id"], "model_id": "fixture-model",
                            "allowed_parameters": [], "parameters": {}, "supports_tools": True})
            agent = request("/agents", {"name": "Fixture developer", "role": "developer", "model_profile_id": model["id"]})
            project = request("/projects", {"name": "Rust HTTP fixture", "blank": True, "execution_mode": "local",
                                "permission_profile": "full", "full_access_confirmed": True, "allow_external_models": False})
            body = {"project_id": project["id"], "agent_ids": [agent["id"]], "title": "Rust HTTP Run",
                    "request": "Write result.txt containing RUST_OWNED_OK followed by a newline. Then report the result."}
            run = request("/runs", body, "real-codex-once")
            assert request("/runs", body, "real-codex-once")["id"] == run["id"]
            deadline = time.monotonic() + 100
            while time.monotonic() < deadline:
                detail = request("/runs/" + run["id"])
                if detail["status"] == "awaiting_acceptance":
                    break
                if detail["status"] == "needs_attention":
                    raise RuntimeError("Run execution failed; inspect durable Rust store")
                time.sleep(0.1)
            else:
                raise RuntimeError("Run did not submit before deadline")
            archive = request(f"/runs/{run['id']}/artifacts/source.zip", binary=True)
            with zipfile.ZipFile(io.BytesIO(archive)) as source:
                assert source.read("result.txt") == b"RUST_OWNED_OK\n"
            patch = request(f"/runs/{run['id']}/artifacts/changes.patch", binary=True)
            assert b"RUST_OWNED_OK" in patch
            accepted = request(f"/runs/{run['id']}/decision/acceptance", {"digest": detail["artifact_digest"], "approved": True})
            assert accepted["status"] == "completed"
            assert len(provider.requests) == 2 and not provider.errors
            assert provider.requests[-1]["tool_outputs"] == 1
            assert all(item["routing_header"] == "rust-http-fixture" for item in provider.requests)
            db = sqlite3.connect(directory / "product.sqlite3")
            assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
            ciphertext = db.execute("SELECT encrypted_key FROM provider_revisions").fetchone()[0]
            assert provider.token not in ciphertext
            db.close()
            result = {"ok": True, "owner": "rust", "runtime": "unmodified-codex", "gateway": gateway,
                      "run_id": run["id"], "status": accepted["status"], "provider_calls": len(provider.requests),
                      "tool_output_returned": True, "artifact_zip_sha256": hashlib.sha256(archive).hexdigest(),
                      "http_idempotency_verified": True, "provider_headers_verified": True,
                      "paid_provider_used": False}
            (directory / "verification.json").write_text(json.dumps(result, indent=2) + "\n")
            return result
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gateway", action="store_true")
    options = parser.parse_args()
    print(json.dumps(verify(options.output.resolve(), options.gateway), indent=2))
