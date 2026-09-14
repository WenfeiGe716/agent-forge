"""Codex-first runtime boundary.

This module intentionally owns only process/protocol concerns. Agent Forge's
team, task and permission rules stay in the control plane; this bridge never
reads the operator's personal ``~/.codex`` configuration and never writes to
it. A later migration can replace the legacy ``engine.turn`` call behind this
interface without introducing a second model loop.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

RUNTIME_BIN_ROOT = Path(__file__).resolve().parents[2] / '.local' / 'runtimes'


def default_runtime_binary(name: str) -> str:
    """Never fall back to the operator's globally installed Codex/OpenCodex."""
    return str(RUNTIME_BIN_ROOT / name / 'bin' / name)


class RuntimeBridgeError(RuntimeError):
    """A bounded, actionable runtime integration error."""


# Requests whose JSON-RPC response is intentionally owned by Forge's durable
# approval layer.  Codex has other server-to-client requests (user input,
# MCP elicitation, dynamic tools, auth refresh); silently leaving those on the
# pipe makes a run look frozen forever, so callers must fail explicitly.
SUPPORTED_SERVER_REQUEST_KINDS = frozenset({
    'item/commandExecution/requestApproval',
    'item/fileChange/requestApproval',
    'item/permissions/requestApproval',
})


@dataclass(frozen=True)
class RuntimeEvent:
    """Normalized Codex notification used by the Forge projection layer."""

    kind: str
    task_id: str | None
    thread_id: str | None
    turn_id: str | None
    item_id: str | None
    sequence: int
    payload: dict[str, Any] = field(default_factory=dict)
    request_id: int | str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "task_id": self.task_id,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "item_id": self.item_id,
            "sequence": self.sequence,
            "payload": self.payload,
            "request_id": self.request_id,
        }


def normalize_notification(notification: dict[str, Any], *, task_id: str | None = None,
                            sequence: int = 0) -> RuntimeEvent | None:
    """Convert one JSON-RPC notification into a stable Forge event.

    Codex notification names evolve more slowly than the UI labels, so the
    original method is preserved in ``kind`` and all unknown fields remain in
    ``payload``. This is deliberately lossless instead of guessing a terminal
    state from arbitrary provider text.
    """

    method = notification.get("method")
    if not isinstance(method, str) or not method:
        return None
    params = notification.get("params")
    if not isinstance(params, dict):
        params = {}
    thread = params.get("threadId") or params.get("thread_id")
    turn = params.get("turnId") or params.get("turn_id")
    item = params.get("itemId") or params.get("item_id")
    for key in ("thread", "turn"):
        obj = params.get(key)
        if isinstance(obj, dict):
            if key == "thread":
                thread = thread or obj.get("id")
            else:
                turn = turn or obj.get("id")
    obj = params.get("item")
    if isinstance(obj, dict):
        thread = thread or obj.get("threadId") or obj.get("thread_id")
        turn = turn or obj.get("turnId") or obj.get("turn_id")
        item = item or obj.get("id") or obj.get("itemId")
    request_id = notification.get("id")
    if type(request_id) not in (int, str):
        request_id = None
    return RuntimeEvent(method, task_id, _text_or_none(thread), _text_or_none(turn),
                        _text_or_none(item), max(0, sequence), params, request_id)


def _text_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def turn_terminal_status(event: RuntimeEvent) -> str | None:
    """Only an explicit successful terminal is success; never guess from EOF."""
    if event.kind == 'turn/interrupted':
        return 'interrupted'
    if event.kind == 'turn/failed':
        return 'failed'
    if event.kind != 'turn/completed':
        return None
    turn = event.payload.get('turn')
    status = turn.get('status') if isinstance(turn, dict) else None
    if status in ('interrupted', 'cancelled', 'canceled'):
        return 'interrupted'
    return 'completed' if status == 'completed' else 'failed'


class CodexAppServerClient:
    """Small JSON-RPC client for a project-owned Codex app-server process."""

    def __init__(self, *, executable: str | None = None, cwd: Path | str | None = None,
                 runtime_home: Path | str, env: dict[str, str] | None = None):
        self.executable = executable or default_runtime_binary('codex')
        self.cwd = str(cwd) if cwd is not None else None
        self.runtime_home = Path(runtime_home).resolve()
        personal_home = Path.home().resolve()
        personal_codex = (personal_home / '.codex').resolve()
        if (self.runtime_home == personal_home or self.runtime_home == Path(self.runtime_home.anchor)
                or self.runtime_home.is_relative_to(personal_codex)
                or personal_codex.is_relative_to(self.runtime_home)):
            raise RuntimeBridgeError('必须使用 Agent Forge 独立运行目录，禁止使用个人 .codex 目录或其父目录')
        self.extra_env = dict(env or {})
        self.user_home = self.runtime_home / 'user-home'
        self._validate_runtime_paths()
        self.process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._server_requests: set[int | str] = set()
        self._events: asyncio.Queue[RuntimeEvent] = asyncio.Queue(maxsize=1000)
        self._write_lock = asyncio.Lock()
        self._terminal_error: RuntimeBridgeError | None = None
        self._request_id = 0
        self._sequence = 0
        self._closed = False

    def _validate_runtime_paths(self) -> None:
        # An old directory or symlink must not redirect config/auth writes into
        # another installation. Re-check immediately before launching as well.
        if self.runtime_home.resolve() != self.runtime_home:
            raise RuntimeBridgeError('Codex 独立运行目录已被重定向，请检查目录配置')
        for name in ('user-home', 'config.toml', 'auth.json'):
            if not (self.runtime_home / name).resolve().is_relative_to(self.runtime_home):
                raise RuntimeBridgeError(f'Codex 独立配置路径越界：{name}')

    def _environment(self) -> dict[str, str]:
        # Start from a minimal inherited environment. In particular, do not
        # let a user's personal CODEX_HOME/config/plugin variables leak in.
        allowed = {'PATH', 'USER', 'LOGNAME', 'SHELL', 'TMPDIR', 'TMP', 'TEMP',
                   'LANG', 'LC_ALL', 'LC_CTYPE', 'SYSTEMROOT', 'SystemRoot', 'COMSPEC'}
        environment = {key: value for key, value in os.environ.items()
                       if key in allowed}
        # Explicit route credentials use Forge-owned names; upstream/global
        # credential variables must not accidentally bind a personal account.
        environment.update({key: value for key, value in self.extra_env.items()
                            if key.startswith('FORGE_')})
        # Codex also discovers ~/.agents/skills outside CODEX_HOME. Isolate the
        # OS home as well so no personal skill/config is implicitly imported.
        environment['HOME'] = str(self.user_home)
        environment['USERPROFILE'] = str(self.user_home)
        environment['XDG_CONFIG_HOME'] = str(self.user_home / '.config')
        environment['XDG_DATA_HOME'] = str(self.user_home / '.local/share')
        environment['XDG_CACHE_HOME'] = str(self.user_home / '.cache')
        environment["CODEX_HOME"] = str(self.runtime_home)
        return environment

    async def start(self, *, timeout: float = 15.0) -> dict[str, Any]:
        if self._closed:
            raise RuntimeBridgeError("Codex 连接已关闭，请创建新的客户端连接")
        if self.process is not None:
            raise RuntimeBridgeError("Codex 运行时已经启动")
        self._validate_runtime_paths()
        binary = Path(self.executable)
        if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
            raise RuntimeBridgeError(f"找不到 Codex 可执行文件：{self.executable}")
        self.runtime_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.user_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.process = await asyncio.create_subprocess_exec(
            self.executable, '-c', 'cli_auth_credentials_store="file"',
            '-c', 'mcp_oauth_credentials_store="file"', "app-server", "--stdio",
            cwd=self.cwd, env=self._environment(),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # History pages/tool output can exceed asyncio's default 64 KiB line limit.
            limit=16 * 1024 * 1024,
        )
        self._reader_task = asyncio.create_task(self._read_stdout())
        # stderr is intentionally drained but never forwarded to the renderer;
        # provider/model text can contain secrets or untrusted content.
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            result = await self.request("initialize", {
                "clientInfo": {"name": "agent-forge", "version": "0.1.0"},
                "capabilities": {},
            }, timeout=timeout)
            returned_home = result.get('codexHome')
            if not isinstance(returned_home, str) or Path(returned_home).resolve() != self.runtime_home:
                raise RuntimeBridgeError('Codex 运行目录校验失败，已拒绝建立任务会话')
            await self._write({"method": "initialized"})
            return result
        except BaseException:
            await self.close()
            raise

    async def _drain_stderr(self) -> None:
        stream = self.process.stderr if self.process else None
        if stream is None:
            return
        while await stream.read(8192):
            pass

    async def _read_stdout(self) -> None:
        stream = self.process.stdout if self.process else None
        if stream is None:
            return
        try:
            while line := await stream.readline():
                try:
                    message = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(message, dict):
                    continue
                if "id" in message and "method" not in message:
                    ident = message.get("id")
                    if type(ident) is not int:
                        continue
                    future = self._pending.pop(ident, None)
                    if future is not None and not future.done():
                        future.set_result(message)
                    continue
                self._sequence += 1
                event = normalize_notification(message, sequence=self._sequence)
                if event is None:
                    continue
                if event.request_id is not None:
                    self._server_requests.add(event.request_id)
                try:
                    self._events.put_nowait(event)
                except asyncio.QueueFull:
                    raise RuntimeBridgeError('Codex 事件消费积压；必须恢复事件记录，禁止静默丢弃')
        except (Exception, asyncio.CancelledError) as exc:
            if not self._closed:
                self._terminal_error = (exc if isinstance(exc, RuntimeBridgeError)
                                        else RuntimeBridgeError("Codex 事件连接中断"))
        finally:
            error = self._terminal_error or RuntimeBridgeError("Codex app-server 已退出")
            self._terminal_error = error
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)
            self._pending.clear()
            self._server_requests.clear()

    async def _write(self, message: dict[str, Any]) -> None:
        process = self.process
        if self._terminal_error:
            raise self._terminal_error
        if self._closed or process is None or process.stdin is None or process.returncode is not None:
            raise RuntimeBridgeError("Codex 运行时未连接")
        data = json.dumps(message, ensure_ascii=False).encode() + b"\n"
        async with self._write_lock:
            process.stdin.write(data)
            await process.stdin.drain()

    def has_server_request(self, request_id: int | str) -> bool:
        return (not self._closed and self._terminal_error is None
                and type(request_id) in (int, str) and request_id in self._server_requests)

    async def respond(self, request_id: int | str, result: dict[str, Any]) -> None:
        """Resolve an existing server request; policy decisions belong to Forge."""
        if type(request_id) not in (int, str) or request_id not in self._server_requests:
            raise RuntimeBridgeError("审批请求已失效或不属于当前连接，请刷新后重试")
        # Consume before yielding so concurrent UI submissions cannot approve twice.
        self._server_requests.remove(request_id)
        await self._write({"id": request_id, "result": result})

    async def request(self, method: str, params: dict[str, Any], *, timeout: float = 60.0) -> dict[str, Any]:
        if self.process is None or self.process.stdin is None:
            raise RuntimeBridgeError("Codex 运行时尚未启动")
        self._request_id += 1
        ident = self._request_id
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[ident] = future
        try:
            await self._write({"id": ident, "method": method, "params": params})
            response = await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(ident, None)
            raise RuntimeBridgeError(f"Codex 请求超时：{method}") from exc
        except (BrokenPipeError, ConnectionError) as exc:
            self._pending.pop(ident, None)
            raise RuntimeBridgeError(f"Codex 运行时连接失败：{method}") from exc
        finally:
            self._pending.pop(ident, None)
        if "error" in response:
            error = response.get("error")
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise RuntimeBridgeError(f"Codex 请求失败（{method}）：{str(message)[:800]}")
        result = response.get("result")
        return result if isinstance(result, dict) else {"value": result}

    async def start_thread(self, *, cwd: Path | str, approval_policy: str = "on-request",
                           approvals_reviewer: str | None = None,
                           sandbox: str = "workspace-write", model: str | None = None,
                           reasoning_effort: str | None = None,
                           model_provider: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"cwd": str(Path(cwd).resolve()),
                                  "approvalPolicy": approval_policy, "sandbox": sandbox}
        if approvals_reviewer:
            params["approvalsReviewer"] = approvals_reviewer
        if model:
            params["model"] = model
        if reasoning_effort:
            params["config"] = {"model_reasoning_effort": reasoning_effort}
        if model_provider:
            params["modelProvider"] = model_provider
        return await self.request("thread/start", params)

    async def resume_thread(self, thread_id: str) -> dict[str, Any]:
        """Resume saved history without creating a replacement thread or changing permissions."""
        return await self.request("thread/resume", {"threadId": thread_id, "excludeTurns": True})

    async def read_thread(self, thread_id: str) -> dict[str, Any]:
        return await self.request("thread/read", {"threadId": thread_id, "includeTurns": False})

    async def list_turns(self, thread_id: str, *, cursor: str | None = None,
                         limit: int = 30) -> dict[str, Any]:
        params: dict[str, Any] = {"threadId": thread_id, "limit": max(1, min(limit, 100))}
        if cursor:
            params["cursor"] = cursor
        return await self.request("thread/turns/list", params)

    async def start_turn(self, thread_id: str, input_items: list[dict[str, Any]], *,
                         timeout: float = 120.0) -> dict[str, Any]:
        return await self.request("turn/start", {"threadId": thread_id, "input": input_items}, timeout=timeout)

    async def interrupt(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        return await self.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    async def steer_turn(self, thread_id: str, turn_id: str,
                         input_items: list[dict[str, Any]]) -> dict[str, Any]:
        return await self.request("turn/steer", {"threadId": thread_id, "expectedTurnId": turn_id,
                                                 "input": input_items})

    async def events(self) -> AsyncIterator[RuntimeEvent]:
        while not self._closed:
            if not self._events.empty():
                yield self._events.get_nowait()
                continue
            reader = self._reader_task
            if reader is None or reader.done():
                raise self._terminal_error or RuntimeBridgeError("Codex 事件连接未启动或已退出")
            next_event = asyncio.create_task(self._events.get())
            try:
                done, _ = await asyncio.wait((next_event, reader), return_when=asyncio.FIRST_COMPLETED)
                if next_event in done:
                    yield next_event.result()
            finally:
                if not next_event.done():
                    next_event.cancel()
                    await asyncio.gather(next_event, return_exceptions=True)

    async def close(self) -> None:
        self._closed = True
        process, reader = self.process, self._reader_task
        self.process = None
        self._reader_task = None
        if reader:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if self._stderr_task:
            self._stderr_task.cancel()
            await asyncio.gather(self._stderr_task, return_exceptions=True)
            self._stderr_task = None


def runtime_binary_status(*, codex: str | None = None, opencodex: str | None = None) -> dict[str, Any]:
    """Report binaries without touching either tool's personal config."""

    def locate(command: str) -> dict[str, Any]:
        path = command if Path(command).is_absolute() else None
        available = bool(path and Path(path).is_file() and os.access(path, os.X_OK))
        return {"command": command, "available": available, "path": path if available else None}

    return {"codex": locate(codex or default_runtime_binary('codex')),
            "opencodex": locate(opencodex or default_runtime_binary('opencodex')),
            "isolated_config": True, "personal_codex_home_used": False,
            "checked_at": time.time()}
