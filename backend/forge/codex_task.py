"""Stage-1 single-task execution through the project-owned Codex runtime."""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable
from urllib.parse import urlsplit

from .codex_permissions import codex_execution_policy
from .runtime_bridge import (CodexAppServerClient, RuntimeBridgeError, RuntimeEvent,
                              SUPPORTED_SERVER_REQUEST_KINDS, turn_terminal_status)


class CodexTaskError(RuntimeBridgeError):
    """A single-task POC could not be started or completed."""


@dataclass(frozen=True)
class CodexProvider:
    base_url: str
    model: str
    api_key: str
    name: str = "Agent Forge provider"
    # When the base URL is an OpenCodex gateway, the gateway admission token
    # is deliberately separate from the upstream provider credential. Direct
    # providers leave this unset and continue using FORGE_MODEL_KEY.
    gateway_token: str | None = None


def _toml_string(value: str, *, field: str, limit: int = 1500) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise CodexTaskError(f"{field} 无效")
    return json.dumps(value, ensure_ascii=False)


def write_provider_config(runtime_home: Path | str, provider: CodexProvider,
                          execution_policy: dict[str, str] | None = None,
                          team_mcp_context: dict[str, str] | None = None) -> Path:
    """Write a minimal, key-free Codex config inside one runtime home."""
    home = Path(runtime_home).resolve()
    personal = Path.home().resolve()
    personal_codex = (personal / ".codex").resolve()
    personal_opencodex = (personal / ".opencodex").resolve()
    if (home.anchor == home or not home.is_absolute() or home == personal
            or personal_codex == home or personal_codex.is_relative_to(home)
            or personal_opencodex == home or personal_opencodex.is_relative_to(home)):
        raise CodexTaskError("Codex 运行目录必须是独立的绝对路径")
    parsed = urlsplit(provider.base_url)
    host = (parsed.hostname or "").lower()
    try:
        parsed.port
    except ValueError as exc:
        raise CodexTaskError("Codex POC 供应商地址端口无效") from exc
    loopback = parsed.scheme == "http" and host in {"127.0.0.1", "localhost", "::1"}
    secure = parsed.scheme == "https" and bool(host)
    if (not (secure or loopback) or parsed.username or parsed.password or not parsed.netloc):
        raise CodexTaskError("Codex POC 供应商地址必须使用 HTTPS 或明确的本机回环地址")
    if not provider.api_key or len(provider.api_key) > 4096:
        raise CodexTaskError("Codex POC 缺少供应商密钥")
    if provider.gateway_token is not None and (
            not isinstance(provider.gateway_token, str)
            or not provider.gateway_token.strip()
            or len(provider.gateway_token) > 4096):
        raise CodexTaskError("Codex POC 网关令牌无效")
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = home / "config.toml"
    if path.is_symlink():
        raise CodexTaskError("Codex 配置文件不能是软链接")
    policy = dict(execution_policy or codex_execution_policy('ask'))
    required = {'approval_policy', 'approvals_reviewer', 'sandbox'}
    if set(policy) != required or any(not isinstance(policy[key], str) or not policy[key] for key in required):
        raise CodexTaskError("Codex 执行权限策略不完整")
    lines = [
        f"model = {_toml_string(provider.model, field='model', limit=200)}",
        'model_provider = "forge-poc"',
        f"approval_policy = {_toml_string(policy['approval_policy'], field='approval_policy', limit=80)}",
        f"approvals_reviewer = {_toml_string(policy['approvals_reviewer'], field='approvals_reviewer', limit=80)}",
        f"sandbox_mode = {_toml_string(policy['sandbox'], field='sandbox', limit=80)}",
        "allow_login_shell = false", "",
        "[model_providers.forge-poc]",
        f"name = {_toml_string(provider.name, field='provider name', limit=120)}",
        f"base_url = {_toml_string(provider.base_url.rstrip('/'), field='base_url')}",
        (f'env_key = "FORGE_GATEWAY_TOKEN"' if provider.gateway_token is not None
         else 'env_key = "FORGE_MODEL_KEY"'),
        'wire_api = "responses"',
        # OpenCodex accepts the Forge data-plane token in this dedicated
        # header. Codex resolves env_http_headers in its project-owned
        # process, so no token is written to config.toml. Direct providers do
        # not receive this gateway-only header.
        *([f'env_http_headers = {{ "x-opencodex-api-key" = "FORGE_GATEWAY_TOKEN" }}']
          if provider.gateway_token is not None else []),
        "requires_openai_auth = false", "supports_websockets = false", "",
    ]
    if team_mcp_context is not None:
        if (not isinstance(team_mcp_context, dict)
                or not {'run_id', 'task_key', 'db_url', 'data_root'} <= set(team_mcp_context)
                or set(team_mcp_context) - {'run_id', 'task_key', 'db_url', 'data_root', 'python'}):
            raise CodexTaskError('团队 MCP 运行上下文不完整')
        values = {
            'FORGE_TEAM_RUN_ID': team_mcp_context['run_id'],
            'FORGE_TEAM_TASK_KEY': team_mcp_context['task_key'],
            'FORGE_TEAM_DB_URL': team_mcp_context['db_url'],
            'FORGE_TEAM_DATA_ROOT': team_mcp_context['data_root'],
        }
        if any(not isinstance(value, str) or not value or len(value) > 4000 for value in values.values()):
            raise CodexTaskError('团队 MCP 运行上下文无效')
        # The MCP child uses the same project-owned Python environment.  The
        # explicit module path prevents it from discovering a user install.
        values['PYTHONPATH'] = str(Path(__file__).resolve().parents[1])
        command = str(team_mcp_context.get('python', sys.executable))
        if not Path(command).is_absolute() or not Path(command).exists():
            raise CodexTaskError('团队 MCP Python 运行时不存在')
        env = ', '.join(f'{key} = {_toml_string(value, field=key, limit=8000)}'
                        for key, value in values.items())
        mcp_args = '["--forge-team-mcp"]' if getattr(sys, 'frozen', False) else '["-m", "forge.team_mcp"]'
        lines.extend([
            '[mcp_servers.forge_team]',
            f'command = {_toml_string(command, field="team MCP command", limit=1500)}',
            f'args = {mcp_args}',
            f'env = {{ {env} }}',
            'enabled = true', 'required = true', 'startup_timeout_sec = 10',
            'tool_timeout_sec = 120', 'supports_parallel_tool_calls = false', '',
        ])
    content = "\n".join(lines)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(content)
    except FileExistsError as exc:
        raise CodexTaskError("Codex 运行目录已初始化，不能在同一运行中替换模型配置") from exc
    path.chmod(0o600)
    return path


@dataclass
class CodexTaskResult:
    status: str
    thread_id: str
    turn_id: str | None
    text: str
    events: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class CodexSingleTask:
    """Run one real Codex turn and expose its ordered runtime events."""

    def __init__(self, *, provider: CodexProvider, runtime_home: Path | str,
                 cwd: Path | str, executable: str | None = None,
                 execution_policy: dict[str, str] | None = None,
                 team_mcp_context: dict[str, str] | None = None):
        self.provider = provider
        self.runtime_home = Path(runtime_home).resolve()
        self.cwd = Path(cwd).resolve()
        self.executable = executable
        self.execution_policy = dict(execution_policy or codex_execution_policy('ask'))
        self.team_mcp_context = dict(team_mcp_context) if team_mcp_context is not None else None
        required = {'approval_policy', 'approvals_reviewer', 'sandbox'}
        if set(self.execution_policy) != required or any(
                not isinstance(self.execution_policy[key], str) or not self.execution_policy[key]
                for key in required):
            raise CodexTaskError("Codex 执行权限策略不完整")
        self.client: CodexAppServerClient | None = None
        self.thread_id: str | None = None
        self.turn_id: str | None = None

    async def stream(self, prompt: str, *, timeout: float = 300.0,
                     event_sink: Callable[[RuntimeEvent], Any] | None = None,
                     thread_id: str | None = None) -> AsyncIterator[RuntimeEvent]:
        if not prompt.strip() or len(prompt) > 100_000:
            raise CodexTaskError("任务输入不能为空或超过 100000 字符")
        self.cwd.mkdir(parents=True, exist_ok=True)
        if thread_id:
            # A resumed turn must use the exact frozen runtime home.  Never
            # rewrite its provider credentials or silently create a new
            # thread when the previous turn may already have side effects.
            config = self.runtime_home / "config.toml"
            if not config.is_file() or config.is_symlink():
                raise CodexTaskError("原 Codex 运行配置不存在或已被重定向，无法继续原任务")
        else:
            write_provider_config(self.runtime_home, self.provider, self.execution_policy,
                                  self.team_mcp_context)
        client = CodexAppServerClient(executable=self.executable, cwd=self.cwd,
                                      runtime_home=self.runtime_home,
                                      env={"FORGE_MODEL_KEY": self.provider.api_key,
                                           **({"FORGE_GATEWAY_TOKEN": self.provider.gateway_token}
                                              if self.provider.gateway_token is not None else {})})
        self.client = client
        try:
            await client.start(timeout=30)
            if thread_id:
                started = await client.resume_thread(thread_id)
            else:
                started = await client.start_thread(cwd=self.cwd, **self.execution_policy,
                    model=self.provider.model, model_provider="forge-poc")
            thread = started.get("thread") if isinstance(started, dict) else None
            returned_thread_id = thread.get("id") if isinstance(thread, dict) else None
            thread_id = returned_thread_id if isinstance(returned_thread_id, str) else thread_id
            if not isinstance(thread_id, str) or not thread_id:
                raise CodexTaskError("Codex 未返回有效 Thread ID")
            self.thread_id = thread_id
            started_turn = await client.start_turn(thread_id, [{"type": "text", "text": prompt}], timeout=timeout)
            turn = started_turn.get("turn") if isinstance(started_turn, dict) else None
            turn_id = turn.get("id") if isinstance(turn, dict) else None
            self.turn_id = turn_id if isinstance(turn_id, str) else None
            async with asyncio.timeout(timeout):
                async for event in client.events():
                    if event.thread_id:
                        self.thread_id = event.thread_id
                    if event.turn_id:
                        self.turn_id = event.turn_id
                    if event_sink is not None:
                        outcome = event_sink(event)
                        if inspect.isawaitable(outcome):
                            await outcome
                    if event.request_id is not None and event.kind not in SUPPORTED_SERVER_REQUEST_KINDS:
                        # Persist the raw request through event_sink first, then
                        # stop this turn with the exact protocol method.  The
                        # scheduler can show a concrete “unsupported client
                        # interaction” instead of leaving the process waiting
                        # indefinitely for a UI that cannot answer it yet.
                        raise CodexTaskError(
                            f"Codex 请求需要客户端交互，但当前运行时暂不支持：{event.kind} "
                            f"（request_id={event.request_id}）")
                    yield event
                    terminal = event.kind in {"turn/completed", "turn/failed", "turn/interrupted"}
                    identified = event.thread_id == thread_id or (turn_id and event.turn_id == turn_id)
                    if terminal and (identified or (not event.thread_id and not event.turn_id)):
                        return
        finally:
            await client.close()

    async def interrupt(self) -> dict[str, Any]:
        """Interrupt the active Codex turn without replacing its thread."""
        client = self.client
        if client is None or not self.thread_id or not self.turn_id:
            raise CodexTaskError("当前没有可中断的 Codex 回合")
        return await client.interrupt(self.thread_id, self.turn_id)

    async def steer(self, prompt: str) -> dict[str, Any]:
        """Add an instruction to the active turn without opening a new one."""
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 100_000:
            raise CodexTaskError("继续指令不能为空或超过 100000 字符")
        client = self.client
        if client is None or not self.thread_id or not self.turn_id:
            raise CodexTaskError("当前没有可追加指令的 Codex 回合")
        return await client.steer_turn(self.thread_id, self.turn_id,
                                       [{"type": "text", "text": prompt}])

    async def run(self, prompt: str, *, timeout: float = 300.0,
                  event_sink: Callable[[RuntimeEvent], Any] | None = None,
                  thread_id: str | None = None) -> CodexTaskResult:
        events: list[RuntimeEvent] = []
        text_parts: list[str] = []
        requested_thread_id = thread_id
        thread_id, turn_id, status = "", None, "unknown"
        errors: list[str] = []
        try:
            stream_kwargs = {"timeout": timeout}
            if event_sink is not None:
                stream_kwargs["event_sink"] = event_sink
            if requested_thread_id:
                stream_kwargs["thread_id"] = requested_thread_id
            async for event in self.stream(prompt, **stream_kwargs):
                events.append(event)
                thread_id = event.thread_id or thread_id
                turn_id = event.turn_id or turn_id
                fragment = event.payload.get("delta")
                if isinstance(fragment, str):
                    text_parts.append(fragment)
                if event.kind == "error":
                    detail = event.payload.get("error")
                    if isinstance(detail, dict) and isinstance(detail.get("message"), str):
                        message = detail["message"][:1200]
                        extras = [
                            f"code={detail['code']}" if detail.get("code") is not None else "",
                            f"http_status={detail['httpStatus']}" if detail.get("httpStatus") is not None else "",
                            f"request_id={detail['requestId']}" if detail.get("requestId") is not None else "",
                        ]
                        formatted = message + (" (" + ", ".join(x for x in extras if x) + ")" if any(extras) else "")
                        if formatted not in errors:
                            errors.append(formatted)
                status = turn_terminal_status(event) or status
        except (asyncio.TimeoutError, RuntimeBridgeError) as exc:
            raise CodexTaskError(f"Codex POC 执行失败：{str(exc)[:800]}") from exc
        if status == "unknown":
            raise CodexTaskError("Codex 未返回回合终态")
        return CodexTaskResult(status, thread_id, turn_id, "".join(text_parts),
                               [event.as_dict() for event in events], errors)
