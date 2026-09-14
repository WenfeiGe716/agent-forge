#!/usr/bin/env python3
"""Provider-free Codex App Server speaking real dynamic-tool JSON-RPC frames."""
import json
import os
import re
import sys
import time
from pathlib import Path

RUNTIME_HOME = Path(os.environ["CODEX_HOME"]).resolve()
WORKSPACE = Path.cwd().resolve()
HISTORY = RUNTIME_HOME / "team-fixture.json"
state = {"pid": os.getpid(), "home": str(RUNTIME_HOME), "cwd": str(WORKSPACE),
         "agent_id": None, "coordinator": False, "thread": None,
         "calls": [], "responses": [], "errors": [], "turn_inputs": [], "skill_reads": []}
program = None
pending = None
waiting_peer = False
waiting_host = False
request_number = 0
assignment_keys = []


def persist():
    temporary = HISTORY.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, sort_keys=True) + "\n")
    os.replace(temporary, HISTORY)


def send(frame):
    print(json.dumps(frame), flush=True)


def emit(method, params):
    send({"method": method, "params": params})


def complete(failed=False):
    global pending, program
    turn = state["thread"]["turns"][-1]
    turn["status"] = "failed" if failed else "completed"
    state["finished_ns"] = time.monotonic_ns()
    pending = None
    program = None
    persist()
    emit("turn/completed", {"threadId": state["thread"]["id"], "turn": turn})


def perform(response=None):
    global pending, program, request_number, waiting_peer, waiting_host
    try:
        tool, arguments, call_id = next(program) if response is None else program.send(response)
        if tool == "__wait_peer__":
            pending = None
            if any("PEER_EVIDENCE" in text for text in state["turn_inputs"]):
                perform()
            else:
                waiting_peer = True
                persist()
            return
        if tool == "__wait_host__":
            pending = None
            waiting_host = True
            state["waiting_host"] = True
            persist()
            return
        assert tool in state["dynamic_tools"], f"missing dynamic tool {tool}"
    except StopIteration:
        complete()
        return
    request_number += 1
    turn = state["thread"]["turns"][-1]
    pending = {"id": f"fixture-request-{request_number}", "tool": tool}
    state["calls"].append({"tool": tool, "arguments": arguments, "call_id": call_id})
    persist()
    send({"id": pending["id"], "method": "item/tool/call", "params": {
        "threadId": state["thread"]["id"], "turnId": turn["id"],
        "tool": tool, "callId": call_id, "arguments": arguments,
    }})


def coordinator(first_turn):
    global assignment_keys
    directory = yield "inspect_team", {}, "lead-inspect-first" if first_turn else "lead-inspect-results"
    peers = [member for member in directory["members"] if member["id"] != state["agent_id"]]
    assert len(peers) == 2, "coordinator did not discover two distinct peers"
    assert {member["role"] for member in peers} == {"developer", "tester"}
    assert all(member["skills"][0]["name"] == "team-evidence" for member in peers)
    if first_turn:
        mode = "CANCEL_MEMBERS" if "CANCEL_MEMBERS" in state["turn_inputs"][-1] else "TEAM_SERIAL" if "TEAM_SERIAL" in state["turn_inputs"][-1] else "TEAM_PARALLEL"
        if "TEAM_STEER" in state["turn_inputs"][-1]:
            mode += " TEAM_STEER"
        for role in ("developer", "tester"):
            peer = next(member for member in peers if member["role"] == role)
            args = {"recipient_id": peer["id"], "title": f"Team {role}",
                    "instruction": f"{mode}: Act as {role}; inspect teammate capabilities and communicate evidence directly.",
                    "acceptance": "Submit concrete evidence from the frozen skill and peer context."}
            result = yield "assign_work", args, f"assign-{role}"
            repeated = yield "assign_work", args, f"assign-{role}"
            assert repeated == result, "assignment replay changed its receipt"
            assignment_keys.append(result["task_key"])
        yield "submit_result", {"outcome": "waiting", "summary": "Two tasks delegated; waiting for peer evidence."}, "lead-wait"
        return
    assert len(assignment_keys) == 2
    for key in assignment_keys:
        result = yield "read_work_result", {"task_key": key}, "read-" + key
        assert result["status"] == "done", result
        assert result["result"]["outcome"] == "done", result
    yield "submit_result", {"outcome": "ready", "summary": "Both members completed; peer evidence reviewed.",
                             "evidence_task_keys": assignment_keys}, "lead-ready"


def member():
    needs_steer = "TEAM_STEER" in state["turn_inputs"][0]
    directory = yield "inspect_team", {}, "member-inspect"
    own = next(member for member in directory["members"] if member["id"] == state["agent_id"])
    peers = [member for member in directory["members"] if member["id"] != state["agent_id"] and not member["is_coordinator"]]
    assert len(peers) == 1, "worker did not discover its peer"
    assert peers[0]["tools"] and peers[0]["skills"], "peer capability directory incomplete"
    parallel = "TEAM_PARALLEL" in state["turn_inputs"][0]
    if parallel or own["role"] == "developer":
        args = {"recipient_id": peers[0]["id"], "body": "PEER_EVIDENCE: frozen skill verified; please validate this result.", "request_reply": False}
        receipt = yield "send_team_message", args, "peer-evidence"
        repeated = yield "send_team_message", args, "peer-evidence"
        assert receipt == repeated, "message replay changed its receipt"
        assert receipt["delivered"] is False
        summary = own["role"] + " completed; frozen skill read and peer evidence sent."
    elif "PEER_EVIDENCE" in state["turn_inputs"][-1]:
        summary = own["role"] + " completed; verified teammate PEER_EVIDENCE and frozen skill."
    else:
        summary = own["role"] + " completed; frozen skill verified before peer activation."
    if parallel:
        yield "__wait_peer__", {}, "peer-delivery-barrier"
        assert any("PEER_EVIDENCE" in text for text in state["turn_inputs"])
    if needs_steer:
        yield "__wait_host__", {}, "host-steer-barrier"
        assert any("HOST_STEER:" + state["agent_id"] in text for text in state["turn_inputs"])
    (WORKSPACE / (own["role"] + ".txt")).write_text("FROZEN_TEAM_EVIDENCE: " + own["role"] + "\n")
    yield "read_inbox", {"limit": 20}, "member-inbox"
    notes = "Read frozen files: " + ", ".join(read["path"] for read in state["skill_reads"])
    notes += "; confirmed FROZEN_TEAM_EVIDENCE in SKILL.md and reference; scripts excluded. " + summary
    yield "submit_result", {"outcome": "done", "summary": summary, "notes": notes}, "member-done"


def handle(frame):
    global program, waiting_peer, waiting_host
    method = frame.get("method")
    ident = frame.get("id")
    params = frame.get("params", {})
    if method is None:
        if ident == "native-question":
            assert frame["result"]["answers"] == {"native-scope": {"answers": ["Proceed"]}}, frame
            state["native_answer"] = frame["result"]
            persist()
            program = coordinator(True)
            perform()
            return
        if ident == "cancel-approval":
            assert frame["result"]["decision"] == "decline"
            state["cancel_declined"] = True
            persist()
            return
        assert pending is not None and pending["id"] == ident, "unknown tool response"
        result = frame["result"]
        value = json.loads(result["contentItems"][0]["text"])
        state["responses"].append({"tool": pending["tool"], "value": value, "success": result["success"]})
        persist()
        assert result["success"], value
        perform(value)
    elif method == "initialize":
        send({"id": ident, "result": {"codexHome": str(RUNTIME_HOME)}})
    elif method == "initialized":
        return
    elif method == "thread/start":
        requested_model = params.get("model", "")
        model_parts = requested_model.split("/")
        assert len(model_parts) < 2 or model_parts[0] != model_parts[1], f"duplicate provider namespace: {requested_model}"
        instructions = params["developerInstructions"]
        matched = re.search(r"frozen team member ([A-Za-z0-9_-]+)", instructions)
        assert matched, "missing frozen worker identity"
        state["agent_id"] = matched.group(1)
        state["coordinator"] = "responsible for final delivery" in instructions
        state["dynamic_tools"] = [tool["name"] for tool in params["dynamicTools"]]
        assert ("assign_work" in state["dynamic_tools"]) == state["coordinator"]
        for relative in ("SKILL.md", "references/evidence.md"):
            path = RUNTIME_HOME / "skills/team-evidence" / relative
            text = path.read_text()
            assert "FROZEN_TEAM_EVIDENCE" in text
            state["skill_reads"].append({"path": str(path), "content": text})
        assert not (RUNTIME_HOME / "skills/team-evidence/scripts/forbidden.sh").exists()
        state["thread"] = {"id": "team-" + state["agent_id"], "cwd": str(WORKSPACE), "turns": []}
        persist()
        send({"id": ident, "result": {"thread": state["thread"]}})
        emit("thread/started", {"thread": state["thread"]})
    elif method == "thread/read":
        send({"id": ident, "result": {"thread": state["thread"]}})
    elif method == "turn/start":
        requested_model = params.get("model", "")
        model_parts = requested_model.split("/")
        assert len(model_parts) < 2 or model_parts[0] != model_parts[1], f"duplicate provider namespace: {requested_model}"
        assert program is None, "new turn started before completion"
        assert params["threadId"] == state["thread"]["id"]
        state["turn_inputs"].append("\n".join(item.get("text", "") for item in params["input"]))
        state["started_ns"] = time.monotonic_ns()
        number = len(state["thread"]["turns"]) + 1
        turn = {"id": state["thread"]["id"] + f"-turn-{number}", "status": "inProgress", "items": []}
        state["thread"]["turns"].append(turn)
        persist()
        send({"id": ident, "result": {"turn": turn}})
        emit("turn/started", {"threadId": state["thread"]["id"], "turn": turn})
        if "TEAM_NATIVE_QUESTION" in state["turn_inputs"][-1] and number == 1:
            send({"id": "native-question", "method": "item/tool/requestUserInput", "params": {
                "threadId": state["thread"]["id"], "turnId": turn["id"], "itemId": "question-item", "isBlocking": True,
                "questions": [{"id": "native-scope", "header": "Scope", "question": "Proceed with the assigned scope?", "isOther": True, "isSecret": False,
                    "options": [{"label": "Proceed", "description": "Use the agreed scope."}, {"label": "Narrow", "description": "Reduce the scope."}]}],
            }})
            return
        if "CANCEL_TEAM_APPROVAL" in state["turn_inputs"][-1] or (not state["coordinator"] and "CANCEL_MEMBERS" in state["turn_inputs"][-1]):
            send({"id": "cancel-approval", "method": "item/commandExecution/requestApproval", "params": {
                "threadId": state["thread"]["id"], "turnId": turn["id"], "itemId": "cancel-command",
                "command": "touch must-not-run", "cwd": str(WORKSPACE), "reason": "Verify pending operation cancellation",
            }})
            return
        program = coordinator(number == 1) if state["coordinator"] else member()
        perform()
    elif method == "turn/steer":
        assert params["threadId"] == state["thread"]["id"]
        assert params["expectedTurnId"] == state["thread"]["turns"][-1]["id"]
        text = "\n".join(item.get("text", "") for item in params["input"])
        state["turn_inputs"].append(text)
        state["live_messages"] = state.get("live_messages", 0) + 1
        persist()
        send({"id": ident, "result": {"turnId": params["expectedTurnId"]}})
        if waiting_peer and "PEER_EVIDENCE" in text:
            waiting_peer = False
            perform()
        elif waiting_host and "HOST_STEER:" in text:
            waiting_host = False
            state["waiting_host"] = False
            perform()
    elif method == "turn/interrupt":
        assert state.get("cancel_declined"), "approval must be declined before cancellation"
        turn = state["thread"]["turns"][-1]
        turn["status"] = "interrupted"
        persist()
        send({"id": ident, "result": {}})
        emit("turn/completed", {"threadId": state["thread"]["id"], "turn": turn})
    else:
        raise AssertionError(f"unsupported fixture method: {method}")


for line in sys.stdin:
    try:
        handle(json.loads(line))
    except Exception as error:
        state["errors"].append(str(error))
        persist()
        if program is not None:
            complete(failed=True)
        else:
            send({"id": None, "error": {"code": -32603, "message": str(error)}})
