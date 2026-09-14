import pytest

from forge.runtime_bridge import RuntimeEvent

from forge.codex_task import CodexProvider, CodexTaskError, write_provider_config


def test_codex_provider_config_is_responses_only_and_never_contains_key(tmp_path):
    provider = CodexProvider("https://provider.example/v1", "forge-model",
                             "secret-that-must-not-be-written")
    path = write_provider_config(tmp_path / "runtime", provider)
    content = path.read_text()
    assert 'model_provider = "forge-poc"' in content
    assert 'wire_api = "responses"' in content
    assert 'env_http_headers' not in content
    assert "secret-that-must-not-be-written" not in content
    assert "FORGE_MODEL_KEY" in content
    assert path.stat().st_mode & 0o777 == 0o600


def test_codex_gateway_config_separates_admission_and_upstream_credentials(tmp_path):
    provider = CodexProvider("http://127.0.0.1:10100/v1", "forge-upstream/route-model",
                             "upstream-secret", gateway_token="gateway-secret")
    path = write_provider_config(tmp_path / "gateway-runtime", provider)
    content = path.read_text()
    assert 'env_key = "FORGE_GATEWAY_TOKEN"' in content
    assert 'x-opencodex-api-key" = "FORGE_GATEWAY_TOKEN"' in content
    assert "upstream-secret" not in content and "gateway-secret" not in content


def test_codex_provider_config_rejects_non_https_external_route(tmp_path):
    with pytest.raises(CodexTaskError, match="HTTPS"):
        write_provider_config(tmp_path / "runtime",
                              CodexProvider("http://provider.example/v1", "m", "k"))


def test_codex_provider_config_rejects_lookalike_loopback_host(tmp_path):
    with pytest.raises(CodexTaskError, match="HTTPS"):
        write_provider_config(tmp_path / "runtime",
                              CodexProvider("http://127.0.0.1.evil/v1", "m", "k"))


def test_codex_provider_config_does_not_replace_frozen_route(tmp_path):
    provider = CodexProvider("https://provider.example/v1", "m", "k")
    write_provider_config(tmp_path / "runtime", provider)
    with pytest.raises(CodexTaskError, match="不能在同一运行"):
        write_provider_config(tmp_path / "runtime", provider)


def test_codex_provider_config_rejects_symlinked_config(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "config.toml").symlink_to(tmp_path / "outside.toml")
    with pytest.raises(CodexTaskError, match="软链接"):
        write_provider_config(runtime, CodexProvider("https://provider.example/v1", "m", "k"))


def test_codex_provider_config_rejects_personal_codex_home():
    with pytest.raises(CodexTaskError, match="独立"):
        write_provider_config(__import__("pathlib").Path.home() / ".codex",
                             CodexProvider("https://provider.example/v1", "m", "k"))


def test_codex_provider_config_can_expose_only_frozen_team_mcp_context(tmp_path):
    path = write_provider_config(
        tmp_path / "runtime",
        CodexProvider("https://provider.example/v1", "m", "k"),
        team_mcp_context={"run_id": "r" * 32, "task_key": "codex", "db_url": "sqlite:////tmp/forge.db", "data_root": "/tmp/forge-data"},
    )
    content = path.read_text()
    assert "[mcp_servers.forge_team]" in content
    assert 'args = ["-m", "forge.team_mcp"]' in content
    assert "FORGE_TEAM_RUN_ID" in content and "FORGE_MODEL_KEY" in content
    assert "[mcp_servers.forge_team]" in content
    assert "secret" not in content


@pytest.mark.asyncio
async def test_single_task_returns_ordered_stream_and_terminal_status(monkeypatch, tmp_path):
    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def start(self, **kwargs):
            return {"codexHome": str(self.kwargs["runtime_home"])}

        async def start_thread(self, **kwargs):
            return {"thread": {"id": "thread-1"}}

        async def start_turn(self, *args, **kwargs):
            return {"turn": {"id": "turn-1"}}

        async def events(self):
            yield RuntimeEvent("item/agentMessage/delta", None, "thread-1", "turn-1", "item-1", 1, {"delta": "hello "})
            yield RuntimeEvent("item/agentMessage/delta", None, "thread-1", "turn-1", "item-1", 2, {"delta": "world"})
            # App Server's completed notification carries the authoritative
            # nested turn status; an absent status must never be treated as a
            # successful execution.
            yield RuntimeEvent("turn/completed", None, "thread-1", "turn-1", None, 3,
                               {"turn": {"status": "completed"}})

        async def close(self):
            return None

    monkeypatch.setattr("forge.codex_task.CodexAppServerClient", FakeClient)
    from forge.codex_task import CodexSingleTask
    persisted = []
    result = await CodexSingleTask(
        provider=CodexProvider("https://provider.example/v1", "m", "k"),
        runtime_home=tmp_path / "runtime",
        cwd=tmp_path / "project",
    ).run("say hello", event_sink=persisted.append)
    assert (result.status, result.thread_id, result.turn_id, result.text) == (
        "completed", "thread-1", "turn-1", "hello world"
    )
    assert result.errors == []
    assert [event["sequence"] for event in result.events] == [1, 2, 3]
    assert [event.sequence for event in persisted] == [1, 2, 3]


@pytest.mark.asyncio
async def test_single_task_passes_frozen_codex_execution_policy(monkeypatch, tmp_path):
    calls = []

    class FakeClient:
        def __init__(self, **kwargs): pass
        async def start(self, **kwargs): return {"codexHome": str(tmp_path / "runtime")}
        async def start_thread(self, **kwargs):
            calls.append(kwargs)
            return {"thread": {"id": "thread-policy"}}
        async def start_turn(self, *args, **kwargs): return {"turn": {"id": "turn-policy"}}
        async def events(self):
            yield RuntimeEvent("turn/completed", None, "thread-policy", "turn-policy", None, 1,
                               {"turn": {"status": "completed"}})
        async def close(self): pass

    monkeypatch.setattr("forge.codex_task.CodexAppServerClient", FakeClient)
    from forge.codex_task import CodexSingleTask
    await CodexSingleTask(
        provider=CodexProvider("https://provider.example/v1", "m", "k"),
        runtime_home=tmp_path / "runtime", cwd=tmp_path / "project",
        execution_policy={"approval_policy": "on-request", "approvals_reviewer": "auto_review",
                          "sandbox": "workspace-write"},
    ).run("policy")
    assert calls[0]["approval_policy"] == "on-request"
    assert calls[0]["approvals_reviewer"] == "auto_review"
    assert calls[0]["sandbox"] == "workspace-write"


@pytest.mark.asyncio
async def test_single_task_does_not_treat_failed_turn_as_success(tmp_path):
    task = __import__("forge.codex_task", fromlist=["CodexSingleTask"]).CodexSingleTask(
        provider=CodexProvider("https://provider.example/v1", "m", "k"),
        runtime_home=tmp_path / "runtime", cwd=tmp_path / "project",
    )

    async def failed_stream(prompt, *, timeout):
        yield RuntimeEvent("error", None, "thread-1", "turn-1", None, 1,
                           {"error": {"message": "upstream 403"}})
        yield RuntimeEvent("turn/completed", None, "thread-1", "turn-1", None, 2,
                           {"turn": {"status": "failed"}})

    task.stream = failed_stream
    result = await task.run("fail")
    assert result.status == "failed"
    assert result.errors == ["upstream 403"]


@pytest.mark.asyncio
async def test_single_task_fails_explicitly_on_unhandled_server_request(monkeypatch, tmp_path):
    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
        async def start(self, **kwargs):
            return {"codexHome": str(self.kwargs["runtime_home"])}
        async def start_thread(self, **kwargs):
            return {"thread": {"id": "thread-input"}}
        async def start_turn(self, *args, **kwargs):
            return {"turn": {"id": "turn-input"}}
        async def events(self):
            yield RuntimeEvent("item/tool/requestUserInput", None, "thread-input", "turn-input",
                               "item-input", 1, {"questions": [{"id": "q1"}]}, "request-1")
        async def close(self):
            return None

    monkeypatch.setattr("forge.codex_task.CodexAppServerClient", FakeClient)
    from forge.codex_task import CodexSingleTask
    with pytest.raises(CodexTaskError, match="item/tool/requestUserInput"):
        await CodexSingleTask(
            provider=CodexProvider("https://provider.example/v1", "m", "k"),
            runtime_home=tmp_path / "runtime", cwd=tmp_path / "project",
        ).run("需要用户输入")


@pytest.mark.asyncio
async def test_single_task_preserves_nested_interrupted_status(tmp_path):
    task = __import__("forge.codex_task", fromlist=["CodexSingleTask"]).CodexSingleTask(
        provider=CodexProvider("https://provider.example/v1", "m", "k"),
        runtime_home=tmp_path / "runtime", cwd=tmp_path / "project",
    )

    async def interrupted_stream(prompt, *, timeout):
        yield RuntimeEvent("turn/completed", None, "thread-1", "turn-1", None, 1,
                           {"turn": {"status": "interrupted"}})

    task.stream = interrupted_stream
    result = await task.run("stop")
    assert result.status == "interrupted"


@pytest.mark.asyncio
async def test_single_task_resumes_frozen_thread_without_rewriting_config(monkeypatch, tmp_path):
    calls = []
    runtime = tmp_path / "runtime"
    provider = CodexProvider("https://provider.example/v1", "m", "k")
    write_provider_config(runtime, provider)

    class FakeClient:
        def __init__(self, **kwargs): self.kwargs = kwargs
        async def start(self, **kwargs): return {"codexHome": str(runtime)}
        async def resume_thread(self, thread_id): calls.append(("resume", thread_id)); return {}
        async def start_thread(self, **kwargs): raise AssertionError("resume must not create a new thread")
        async def start_turn(self, thread_id, *args, **kwargs): calls.append(("turn", thread_id)); return {"turn": {"id": "turn-2"}}
        async def events(self):
            yield RuntimeEvent("turn/completed", None, "thread-old", "turn-2", None, 1,
                                {"turn": {"status": "completed"}})
        async def close(self): pass

    monkeypatch.setattr("forge.codex_task.CodexAppServerClient", FakeClient)
    from forge.codex_task import CodexSingleTask
    task = CodexSingleTask(provider=provider, runtime_home=runtime, cwd=tmp_path / "project")
    result = await task.run("继续", event_sink=lambda _: None, thread_id="thread-old")
    assert result.thread_id == "thread-old"
    assert calls == [("resume", "thread-old"), ("turn", "thread-old")]
    assert "m" in (runtime / "config.toml").read_text()


@pytest.mark.asyncio
async def test_single_task_interrupts_active_turn_without_replacing_thread(tmp_path):
    calls = []
    class FakeClient:
        async def interrupt(self, thread_id, turn_id): calls.append((thread_id, turn_id)); return {"ok": True}
    from forge.codex_task import CodexSingleTask
    task = CodexSingleTask(provider=CodexProvider("https://provider.example/v1", "m", "k"),
                           runtime_home=tmp_path / "runtime", cwd=tmp_path / "project")
    task.client = FakeClient(); task.thread_id = "thread-1"; task.turn_id = "turn-1"
    assert await task.interrupt() == {"ok": True}
    assert calls == [("thread-1", "turn-1")]


@pytest.mark.asyncio
async def test_single_task_steers_active_turn_without_opening_new_turn(tmp_path):
    calls = []
    class FakeClient:
        async def steer_turn(self, thread_id, turn_id, input_items):
            calls.append((thread_id, turn_id, input_items)); return {"accepted": True}
    from forge.codex_task import CodexSingleTask
    task = CodexSingleTask(provider=CodexProvider("https://provider.example/v1", "m", "k"),
                           runtime_home=tmp_path / "runtime", cwd=tmp_path / "project")
    task.client = FakeClient(); task.thread_id = "thread-1"; task.turn_id = "turn-1"
    assert await task.steer("请先检查测试失败原因") == {"accepted": True}
    assert calls == [("thread-1", "turn-1", [{"type":"text", "text":"请先检查测试失败原因"}])]
