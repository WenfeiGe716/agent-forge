#!/usr/bin/env python3
"""Verify the real Codex -> OpenCodex -> provider request path.

The upstream provider is a disposable local HTTP server. This proves the
runtime contract without sending data to a paid or user-configured service:
Codex emits its dedicated OpenCodex admission header, the gateway resolves a
namespaced ``provider/model`` selector, and the upstream receives the native
model plus its own credential.
"""

from __future__ import annotations

import asyncio
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
BUN = ROOT / ".local/build/gateway-toolchain/node_modules/@oven/bun-darwin-aarch64/bin/bun"
sys.path.insert(0, str(ROOT / "backend"))


class UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    requests: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
        size = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(size)
        try:
            body = json.loads(raw)
        except (TypeError, ValueError):
            body = None
        self.requests.append({
            "path": self.path,
            "authorization": self.headers.get("Authorization", ""),
            "body": body,
        })
        payload = (
            "event: response.created\n"
            'data: {"type":"response.created","response":{"id":"route-check"}}\n\n'
            "event: response.output_text.delta\n"
            'data: {"type":"response.output_text.delta","delta":"ROUTE_OK"}\n\n'
            "event: response.output_text.done\n"
            'data: {"type":"response.output_text.done","text":"ROUTE_OK"}\n\n'
            "event: response.completed\n"
            'data: {"type":"response.completed","response":{"id":"route-check","status":"completed","output":[]}}\n\n'
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args) -> None:
        return


def wait_health(port: int, process: subprocess.Popen[bytes], timeout: float = 30) -> dict:
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and process.poll() is None:
        try:
            with opener.open(f"http://127.0.0.1:{port}/healthz", timeout=1) as response:
                return json.load(response)
        except (URLError, TimeoutError, ConnectionError):
            time.sleep(0.2)
    raise RuntimeError("OpenCodex 网关未在限定时间内就绪")


async def run_task(base_url: str, token: str, cwd: Path, runtime_home: Path) -> dict:
    from forge.codex_task import CodexProvider, CodexSingleTask

    task = CodexSingleTask(
        provider=CodexProvider(base_url, "forge-upstream/route-model", "upstream-route-secret",
                               gateway_token=token),
        runtime_home=runtime_home,
        cwd=cwd,
    )
    result = await task.run("只回复 ROUTE_OK，不要调用工具。", timeout=45)
    return {"status": result.status, "text": result.text, "event_count": len(result.events)}


def main() -> int:
    if sys.platform != "darwin" or not BUN.is_file():
        print(json.dumps({"ok": False, "error": "需要项目内 macOS Bun 运行时"}, ensure_ascii=False))
        return 2
    UpstreamHandler.requests = []
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix="agent-forge-route-") as temp:
        root = Path(temp)
        gateway_home = root / "opencodex"
        gateway_home.mkdir(mode=0o700)
        gateway_port = 0
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            gateway_port = sock.getsockname()[1]
        config = {
            "hostname": "127.0.0.1",
            "port": gateway_port,
            "runtimeRole": "hub",
            "clientIntegrations": {"codex": False, "grok": False},
            "codexAutoStart": False,
            "codexShimAutoRestore": False,
            "unauthenticatedLoopbackListener": {"enabled": False},
            "defaultProvider": "forge-upstream",
            "providers": {
                "openai": {"disabled": True, "adapter": "openai-responses", "baseUrl": "http://127.0.0.1:1/v1", "allowPrivateNetwork": True},
                "forge-upstream": {
                    "adapter": "openai-responses",
                    "baseUrl": f"http://127.0.0.1:{upstream.server_port}/v1",
                    "apiKey": "${FORGE_PROVIDER_ROUTE_KEY}",
                    "defaultModel": "route-model",
                    "allowPrivateNetwork": True,
                },
            },
        }
        (gateway_home / "config.json").write_text(json.dumps(config), encoding="utf-8")
        gateway_token = secrets.token_urlsafe(32)
        env = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL", "TMPDIR", "HOME", "USER"}}
        env.update(
            FORGE_RUNTIME_DATA_ROOT=str(root),
            FORGE_OPENCODEX_API_AUTH_TOKEN=gateway_token,
            FORGE_PROVIDER_ROUTE_KEY="upstream-route-secret",
        )
        log_path = root / "gateway.log"
        command = [str(BUN), str(ROOT / "runtime/opencodex-gateway.ts")]
        with log_path.open("w+") as log:
            process = subprocess.Popen(command, env=env, cwd=root, stdout=log, stderr=log)
            try:
                health = wait_health(gateway_port, process)
                result = asyncio.run(run_task(f"http://127.0.0.1:{gateway_port}/v1", gateway_token,
                                               root / "workspace", root / "codex-home"))
                if result["status"] != "completed":
                    raise RuntimeError(f"Codex 任务未完成：{result}")
                if len(UpstreamHandler.requests) != 1:
                    raise RuntimeError(f"上游请求次数异常：{len(UpstreamHandler.requests)}")
                request = UpstreamHandler.requests[0]
                body = request["body"] if isinstance(request["body"], dict) else {}
                if request["path"] != "/v1/responses" or request["authorization"] != "Bearer upstream-route-secret":
                    raise RuntimeError(f"网关未正确转发供应商鉴权或路径：{request}")
                if body.get("model") != "route-model":
                    raise RuntimeError(f"网关未去除 provider 命名空间：{body.get('model')!r}")
                print(json.dumps({
                    "ok": True,
                    "gateway_version": health.get("version"),
                    "codex_status": result["status"],
                    "upstream_path": request["path"],
                    "upstream_model": body.get("model"),
                    "codex_to_gateway_header": "x-opencodex-api-key",
                    "upstream_auth_verified": True,
                }, ensure_ascii=False))
                return 0
            except Exception as exc:
                log.flush()
                log.seek(0)
                print(json.dumps({"ok": False, "error": str(exc)[:1200], "gateway_log": log.read()[-4000:]}, ensure_ascii=False))
                return 1
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
    upstream.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
