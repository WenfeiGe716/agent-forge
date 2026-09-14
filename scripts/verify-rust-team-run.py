#!/usr/bin/env python3
"""Run three unchanged Codex processes against a local Responses test provider.

All product writes and team execution belong to Rust. The deterministic local
provider selects real Codex native shell and Forge dynamic tools. It verifies
the returned outputs before proceeding; no paid provider credentials are used.
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
import shlex
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
MARKER = "FROZEN_TEAM_EVIDENCE"
ROLES = ("planner", "developer", "tester")


def decoded_output(output):
    """Dynamic outputs can be plain JSON or Responses text content blocks."""
    if isinstance(output, list):
        output = "\n".join(item.get("text", "") for item in output)
    if isinstance(output, str):
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            return output
    return output


class Conversation:
    def __init__(self, provider, role):
        self.provider, self.role = provider, role
        self.program = None
        self.pending = None
        self.turns = 0
        self.calls = 0
        self.tasks = []
        self.skill_read = False
        self.peer_seen = False
        self.reply_seen = False

    def step(self, body):
        user_text = [content.get("text", "") for item in body.get("input", [])
                     if item.get("type") == "message" and item.get("role") == "user"
                     for content in item.get("content", [])]
        self.peer_seen |= any("PEER_EVIDENCE" in message for message in user_text)
        self.reply_seen |= any("REPLY_EVIDENCE" in message for message in user_text)
        advertised = {tool.get("name") for tool in body.get("tools", [])}
        expected = {"inspect_team", "read_inbox", "read_work_result", "submit_result"}
        assert expected <= advertised, f"missing team tools: {sorted(expected - advertised)}; advertised={sorted(str(x) for x in advertised)}"
        assert ("assign_work" in advertised) == (self.role == "planner")
        outputs = {item.get("call_id"): item.get("output") for item in body.get("input", []) if item.get("type") == "function_call_output"}
        try:
            if self.pending:
                assert self.pending in outputs, "real Codex did not return the requested tool output"
                result = decoded_output(outputs[self.pending])
                action = self.program.send(result)
            else:
                self.turns += 1
                prompt = json.dumps(body.get("input", []))
                skill_inputs = [content.get("text", "") for item in body.get("input", [])
                                if item.get("type") == "message" and item.get("role") == "user"
                                for content in item.get("content", [])]
                assert any(text.startswith("<skill>") and "<name>team-evidence</name>" in text
                           and MARKER in text for text in skill_inputs), "Codex did not load the selected native skill"
                self.peer_seen |= "PEER_EVIDENCE" in prompt
                self.program = self.run(advertised)
                action = next(self.program)
        except StopIteration:
            self.pending, self.program = None, None
            return {"type": "message", "role": "assistant", "id": f"fixture-{self.role}-message-{self.turns}",
                    "content": [{"type": "output_text", "text": f"{self.role} turn {self.turns} complete."}]}
        name, args = action
        assert name in advertised, f"requested tool is not available: {name}"
        self.calls += 1
        self.pending = f"fixture-{self.role}-{self.calls}"
        self.provider.operations.append({"role": self.role, "tool": name, "turn": self.turns})
        return {"type": "function_call", "call_id": self.pending, "name": name, "arguments": json.dumps(args)}

    def run(self, advertised):
        if self.role != "planner" and self.turns == 1:
            # Both independent Codex turns must be alive before either can
            # proceed. A serial runtime fails this barrier instead of passing.
            self.provider.member_barrier.wait(timeout=20)
        if not self.skill_read:
            runtime_paths = list((self.provider.directory / "data/runtime").glob(f"*/{self.provider.agents[self.role]}/skills/team-evidence"))
            assert len(runtime_paths) == 1, "frozen skill was not materialized for this member"
            skill = runtime_paths[0]
            command = "cat " + shlex.quote(str(skill / "SKILL.md")) + " " + shlex.quote(str(skill / "references/evidence.md"))
            if self.role != "planner":
                command += " && printf 'FROZEN_TEAM_EVIDENCE\\n' > " + self.role + ".txt"
            if "exec_command" in advertised:
                native = "exec_command", {"cmd": command, "yield_time_ms": 1000, "max_output_tokens": 1000}
            elif "shell_command" in advertised:
                native = "shell_command", {"command": command, "timeout_ms": 10000}
            elif "shell" in advertised:
                native = "shell", {"command": ["/bin/sh", "-c", command], "timeout_ms": 10000}
            else:
                raise AssertionError("Codex advertised no native shell tool")
            output = yield native
            text = json.dumps(output)
            assert MARKER in text and "verify the reviewed revision" in text, "native tool did not return both frozen skill files"
            assert not (skill / "scripts/forbidden.sh").exists(), "unapproved scripts were materialized"
            self.skill_read = True
            self.provider.skill_evidence.append({"role": self.role, "native_tool": native[0], "skill": str(skill / "SKILL.md"),
                                                 "reference": str(skill / "references/evidence.md"), "marker_returned": True,
                                                 "native_skill_selected": True, "scripts_excluded": True})
        directory = yield "inspect_team", {}
        assert isinstance(directory, dict) and len(directory["members"]) == 3, "team directory output invalid"
        members = {member["role"]: member for member in directory["members"]}
        assert all(member["skills"] and member["tools"] for member in members.values()), "capability directory incomplete"
        if self.role == "planner":
            if self.turns == 1:
                answer = yield "request_user_input", {"questions": [{"id": "native-scope", "header": "Scope", "question": "Proceed with the frozen team?", "options": [{"label": "Proceed", "description": "Use the frozen team."}, {"label": "Stop", "description": "End the run."}]}]}
                assert answer["answers"] == {"native-scope": {"answers": ["Proceed"]}}, answer
                for role in ("developer", "tester"):
                    result = yield "assign_work", {"recipient_id": members[role]["id"], "title": f"Verify {role} evidence",
                                                   "instruction": "Read the approved team-evidence skill and its reference using a native tool; inspect team capabilities and exchange direct peer evidence.",
                                                   "acceptance": "Return concrete frozen skill and peer evidence."}
                    self.tasks.append(result["task_key"])
                result = yield "submit_result", {"outcome": "waiting", "summary": "Two members assigned; waiting for their concrete evidence."}
                assert result["result"]["outcome"] == "waiting", result
            else:
                assert self.turns == 2, "coordinator unexpectedly activated again"
                for key in self.tasks:
                    result = yield "read_work_result", {"task_key": key}
                    assert result["status"] == "done" and MARKER in result["result"]["notes"], "coordinator did not receive member evidence"
                result = yield "submit_result", {"outcome": "ready", "summary": "Both members completed; native skill reads and peer communication verified.",
                                                "evidence_task_keys": self.tasks}
                assert result["result"]["outcome"] == "ready", result
        else:
            peer = members["tester" if self.role == "developer" else "developer"]
            if self.provider.reply and self.role == "developer" and self.turns == 1:
                sent = yield "send_team_message", {"recipient_id": peer["id"], "body": "PEER_EVIDENCE: review the frozen skill and reply with concrete evidence.", "request_reply": True}
                self.tasks.append(sent["task_key"])
                yield "submit_result", {"outcome": "waiting", "summary": "Waiting for the bounded teammate reply."}
                return
            if self.provider.reply and self.turns == 2:
                if self.role == "tester":
                    yield "send_team_message", {"recipient_id": peer["id"], "body": "REPLY_EVIDENCE: the frozen skill and reference were verified with the native shell.", "request_reply": False}
                else:
                    reply = yield "read_work_result", {"task_key": self.tasks[0]}
                    assert reply["status"] == "done" and reply["result"]["outcome"] == "done", "waiter resumed before actual reply completion"
                    assert self.reply_seen, "reply message was not included in resumed Codex context"
                inbox = yield "read_inbox", {"limit": 20}
                if inbox["messages"]:
                    yield "mark_message_delivered", {"message_ids": [m["id"] for m in inbox["messages"]]}
                result = yield "submit_result", {"outcome": "done", "summary": f"{self.role} completed bounded reply cooperation.", "notes": f"{MARKER}: native skill evidence retained; bounded reply completed and resumed work verified.", "deliverables": [self.role + ".txt"]}
                assert result["result"]["outcome"] == "done"
                return
            result = yield "send_team_message", {"recipient_id": peer["id"], "body": "PEER_EVIDENCE: native tool read the approved frozen skill and its reference.", "request_reply": False}
            assert result["delivered"] is False, result
            for _ in range(100):
                inbox = yield "read_inbox", {"limit": 20}
                if inbox["messages"]:
                    self.peer_seen = any("PEER_EVIDENCE" in m["body"] for m in inbox["messages"])
                    yield "mark_message_delivered", {"message_ids": [m["id"] for m in inbox["messages"]]}
                    break
                if self.peer_seen:
                    break
                time.sleep(0.05)
            assert self.peer_seen, "member completed before receiving peer evidence"
            result = yield "submit_result", {"outcome": "done", "summary": f"{self.role} completed native skill and peer evidence checks.",
                                            "notes": f"{MARKER}: native tool returned SKILL.md and references/evidence.md; unapproved scripts excluded. Peer evidence received.", "deliverables": [self.role + ".txt"]}
            assert result["result"]["outcome"] == "done", result


class ResponsesHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *_args):
        pass

    def do_POST(self):  # noqa: N802
        try:
            assert self.path == "/v1/responses", "unexpected provider path"
            assert self.headers.get("Authorization") == "Bearer " + self.server.token, "incorrect provider credential"
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            model = body.get("model", "")
            assert model.startswith("fixture-"), "unexpected model route"
            role = model.removeprefix("fixture-")
            assert role in ROLES, "unexpected team role"
            item = self.server.conversations[role].step(body)
            ident = f"team-response-{len(self.server.requests) + 1}"
            self.server.requests.append({"role": role, "model": model, "authenticated": True,
                                         "tool_outputs": sum(i.get("type") == "function_call_output" for i in body.get("input", []))})
            events = [{"type": "response.created", "response": {"id": ident}},
                      {"type": "response.output_item.done", "item": item},
                      {"type": "response.completed", "response": {"id": ident, "status": "completed",
                        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}}]
            payload = "".join("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n" for event in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except Exception as error:
            self.server.errors.append(f"{type(error).__name__}: {error}")
            self.send_error(400, "invalid local test request")


@contextmanager
def provider_fixture(directory, reply=False):
    provider = ThreadingHTTPServer(("127.0.0.1", 0), ResponsesHandler)
    provider.member_barrier = threading.Barrier(2)
    provider.reply = reply
    provider.token = secrets.token_urlsafe(32)
    provider.directory, provider.agents = directory, {}
    provider.requests, provider.errors, provider.operations, provider.skill_evidence = [], [], [], []
    provider.conversations = {role: Conversation(provider, role) for role in ROLES}
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    try:
        yield provider
    finally:
        (directory / "provider-evidence.json").write_text(json.dumps({"requests": provider.requests, "errors": provider.errors,
          "operations": provider.operations, "skill_evidence": provider.skill_evidence}, indent=2) + "\n")
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=3)


def verify(directory, reply=False):
    directory.mkdir(parents=True, exist_ok=False)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    from cryptography.fernet import Fernet
    password = secrets.token_urlsafe(24)
    binary = ROOT / ".local/runtimes/codex/bin/codex"
    env = {key: value for key, value in os.environ.items() if not key.startswith("FORGE_")}
    env.update(ADMIN_PASSWORD=password, ENCRYPTION_KEY=Fernet.generate_key().decode(), FORGE_CODEX_BINARY=str(binary))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base, cookie = f"http://127.0.0.1:{port}/api", ""

    def request(path, body=None, key=None):
        headers = {"X-Forge-Request": "1", "Cookie": cookie}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if key:
            headers["Idempotency-Key"] = key
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), headers=headers)
        try:
            with opener.open(req, timeout=30) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"HTTP {error.code}: {path}: {error.read()[:1000]!r}") from None

    command = [str(ROOT / "rust/target/debug/forge-server"), "--listen", f"127.0.0.1:{port}",
               "--product-db", str(directory / "product.sqlite3"), "--rust-db", str(directory / "runs.sqlite3"),
               "--data-root", str(directory / "data")]
    with (directory / "server.log").open("w") as log, provider_fixture(directory, reply) as provider:
        process = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                assert process.poll() is None, "Rust host exited before readiness"
                try:
                    request("/health")
                    break
                except (urllib.error.URLError, ConnectionError):
                    time.sleep(0.1)
            else:
                raise RuntimeError("Rust host did not become ready")
            login = urllib.request.Request(base + "/auth/login", data=json.dumps({"username": "admin", "password": password}).encode(),
                                          headers={"Content-Type": "application/json", "X-Forge-Request": "1"})
            with opener.open(login) as response:
                cookie = response.headers["Set-Cookie"].split(";", 1)[0]
            p = request("/providers", {"name": "Local team fixture", "protocol": "openai_responses", "external": False,
                        "base_url": f"http://127.0.0.1:{provider.server_port}/v1", "api_key": provider.token})
            skill = request("/skills", {"files": {"SKILL.md": f"---\nname: team-evidence\ndescription: Verify concrete team evidence\n---\nRead references/evidence.md with your native file or shell tool. {MARKER}\n",
                            "references/evidence.md": f"{MARKER}: verify the reviewed revision.\n", "scripts/forbidden.sh": "echo must-not-run\n"}})
            request(f"/skills/{skill['id']}/review", {"version": 1, "digest": skill["digest"], "action": "approve"})
            for role in ROLES:
                model = request("/models", {"name": role, "provider_id": p["id"], "model_id": "fixture-" + role,
                                            "parameters": {}, "allowed_parameters": [], "supports_tools": True})
                agent = request("/agents", {"name": "Fixture " + role, "role": role, "model_profile_id": model["id"],
                                            "skills": [{"skill_id": skill["id"], "version": 1, "mode": "always", "allow_scripts": False, "parameters": {}}]})
                provider.agents[role] = agent["id"]
            project = request("/projects", {"name": "Real Codex team validation", "blank": True, "execution_mode": "local",
                                            "permission_profile": "full", "full_access_confirmed": True, "allow_external_models": False,
                                            "runtime": {"task_timeout_s": 120, "call_timeout_s": 30, "max_model_calls": 40, "max_parallel": 2}})
            body = {"project_id": project["id"], "title": "Real Codex team validation", "agent_ids": list(provider.agents.values()),
                    "entry_agent_id": provider.agents["planner"], "request": "Delegate two bounded evidence checks, use the approved team-evidence skill and native tools, exchange a direct peer message, then review actual results."}
            run = request("/runs", body, "real-team-once")
            assert request("/runs", body, "real-team-once")["id"] == run["id"]
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                if provider.errors:
                    raise RuntimeError("Local provider validation failed: " + repr(provider.errors))
                detail = request("/runs/" + run["id"])
                collaboration = request(f"/runs/{run['id']}/collaboration")
                for question in collaboration["decisions"]:
                    if question["status"] == "pending":
                        request(f"/runs/{run['id']}/questions/{question['id']}/answer", {"digest": question["digest"], "choice_id": "option_1"})
                if detail["status"] == "awaiting_acceptance":
                    break
                if detail["status"] == "needs_attention":
                    raise RuntimeError("Team Run needs attention; inspect durable stores and server.log")
                time.sleep(0.1)
            else:
                raise RuntimeError("Team Run did not submit before deadline")
            assert len(detail["tasks"]) == (4 if reply else 3) and all(task["status"] == "done" for task in detail["tasks"]), detail["tasks"]
            assert len(provider.skill_evidence) == 3 and any(c.peer_seen for c in provider.conversations.values())
            with sqlite3.connect(directory / "product.sqlite3") as db:
                connections = db.execute("SELECT count(DISTINCT connection_id) FROM forge_runtime_messages WHERE run_id=?", (run["id"],)).fetchone()[0]
                threads = db.execute("SELECT count(DISTINCT json_extract(payload,'$.threadId')) FROM forge_runtime_messages WHERE run_id=? AND kind='turn/started'", (run["id"],)).fetchone()[0]
                messages, delivered = db.execute("SELECT count(*),sum(delivered_at<>'') FROM agent_messages WHERE run_id=?", (run["id"],)).fetchone()
                assert threads == 3 and connections == (5 if reply else 3) and messages == delivered == (3 if reply else 2), (connections, threads, messages, delivered)
                assert db.execute("SELECT count(*) FROM forge_team_decision_callbacks WHERE run_id=? AND status='delivered'", (run["id"],)).fetchone()[0] == 1
                if reply:
                    assert db.execute("SELECT count(*) FROM tasks WHERE run_id=? AND json_extract(spec,'$.reply_wait.consumed')=1", (run["id"],)).fetchone()[0] == 1
            for role in ("developer", "tester"):
                canonical = directory / 'data/runs' / run['id'] / 'workspace' / (role + '.txt')
                assert canonical.read_text() == MARKER + '\n', 'Native member output was not integrated'
            accepted = request(f"/runs/{run['id']}/decision/acceptance", {"digest": detail["artifact_digest"], "approved": True})
            assert accepted["status"] == "completed"
            result = {"ok": True, "owner": "rust", "runtime": "unmodified-codex", "codex_binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                      "run_id": run["id"], "status": accepted["status"], "members": 3, "codex_threads": threads, "runtime_connections": connections,
                      "provider_calls": len(provider.requests), "native_skill_reads": len(provider.skill_evidence), "peer_messages": messages,
                      "native_skill_selection_verified": True,
                      "native_user_input_http_roundtrip_verified": True,
                      "concurrent_member_barrier_verified": True,
                      "bounded_peer_reply_and_resume_verified": reply,
                      "isolated_member_native_files_integrated": True,
                      "peer_messages_delivered": delivered, "member_results_reviewed": 2, "http_idempotency_verified": True, "paid_provider_used": False}
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
    parser.add_argument("--reply", action="store_true", help="Also require a bounded peer reply and resume in the same Codex thread")
    options = parser.parse_args()
    print(json.dumps(verify(options.output.resolve(), options.reply), indent=2))
