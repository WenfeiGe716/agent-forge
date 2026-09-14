#!/usr/bin/env python3
"""Durable, provider-free Codex execution fixture over newline-delimited JSON."""

import copy
import json
import os
from pathlib import Path
import sys


THREAD_ID = "fixture-thread"
MODE = os.environ.get("FORGE_EXECUTION_FIXTURE_MODE", "normal")
CWD = Path.cwd().resolve()
HOME = Path(os.environ["CODEX_HOME"])
if not HOME.is_absolute() or not HOME.is_dir():
    raise SystemExit("CODEX_HOME must be an existing absolute directory")
HOME = HOME.resolve()
HISTORY = HOME / "fixture-history.json"
RECEIPT = CWD / "fixture-result.txt"
COUNTERS = ("thread_starts", "turn_starts", "approvals", "effects", "resumes")
if MODE not in {"normal", "exit_before_turn_reply", "exit_after_effect"}:
    raise SystemExit("unknown execution fixture mode")
if HISTORY.is_symlink() or RECEIPT.is_symlink():
    raise SystemExit("fixture files must not be symlinks")


def send(value):
    print(json.dumps(value), flush=True)


def reply(ident, result):
    send({"id": ident, "result": result})


def reject(ident, message, code=-32602):
    send({"id": ident, "error": {"code": code, "message": message}})


def emit(method, params):
    send({"method": method, "params": params})


def persist():
    # Atomic replacement keeps restart history complete at either exit boundary.
    temporary = HOME / "fixture-history.json.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as target:
        json.dump(state, target, sort_keys=True)
        target.write("\n")
        target.flush()
        os.fsync(target.fileno())
    os.replace(temporary, HISTORY)


if HISTORY.exists():
    with HISTORY.open() as source:
        state = json.load(source)
    if state["thread"] is not None and state["thread"]["cwd"] != str(CWD):
        raise SystemExit("persisted thread cwd does not match process cwd")
else:
    state = {"thread": None, **dict.fromkeys(COUNTERS, 0)}
state.setdefault("steers", [])

# Pending approval authority belongs only to this process/connection.
pending = None


def valid_cwd(params, required=False):
    cwd = params.get("cwd")
    if cwd is None:
        return not required
    return isinstance(cwd, str) and Path(cwd).is_absolute() and Path(cwd).resolve() == CWD


def valid_thread(params):
    return state["thread"] is not None and params.get("threadId") == THREAD_ID


def thread_copy(include_turns=True):
    thread = copy.deepcopy(state["thread"])
    if not include_turns:
        thread["turns"] = []
    return thread


def turn_params(turn):
    return {"threadId": THREAD_ID, "turn": copy.deepcopy(turn)}


def item_params(turn, item):
    return {"threadId": THREAD_ID, "turnId": turn["id"], "item": copy.deepcopy(item)}


def delta(turn, text):
    emit("item/agentMessage/delta", {
        "threadId": THREAD_ID,
        "turnId": turn["id"],
        "itemId": "fixture-message-" + turn["id"].rsplit("-", 1)[1],
        "delta": text,
    })


def interrupt(turn):
    global pending
    turn["status"] = "interrupted"
    for item in turn["items"]:
        if item["type"] == "commandExecution" and item["status"] == "inProgress":
            item["status"] = "declined"
    pending = None
    persist()
    emit("turn/completed", turn_params(turn))


def answer_approval(message):
    global pending
    ident = message.get("id")
    if pending is None or ident != pending["id"]:
        reject(ident, "unknown approval request id")
        return
    result = message.get("result")
    decision = result.get("decision") if isinstance(result, dict) else None
    if not isinstance(decision, str) or decision not in {"accept", "decline"}:
        reject(ident, "approval decision must be accept or decline")
        return
    turn = pending["turn"]
    if decision == "decline":
        interrupt(turn)
        return

    state["approvals"] += 1
    fd = os.open(RECEIPT, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as target:
        target.write(turn["id"] + "\n")
        target.flush()
        os.fsync(target.fileno())
    state["effects"] += 1
    command = turn["items"][0]
    command.update({"status": "completed", "exitCode": 0, "aggregatedOutput": ""})
    agent_message = {
        "type": "agentMessage",
        "id": "fixture-message-" + turn["id"].rsplit("-", 1)[1],
        "text": "Fixture result ready.",
    }
    turn["items"].append(agent_message)
    turn["status"] = "completed"
    pending = None
    persist()
    if MODE == "exit_after_effect":
        raise SystemExit(0)
    emit("item/completed", item_params(turn, command))
    delta(turn, "Fixture result ready.")
    emit("item/completed", item_params(turn, agent_message))
    emit("turn/completed", turn_params(turn))


def handle(message):
    global pending
    if not isinstance(message, dict):
        reject(None, "message must be an object")
        return
    ident = message.get("id")
    method = message.get("method")
    if method is None:
        answer_approval(message)
        return
    if method == "initialized":
        return
    if isinstance(ident, bool) or not isinstance(ident, (int, str)):
        reject(ident, "request id must be an integer or string")
        return
    params = message.get("params", {})
    if not isinstance(params, dict):
        reject(ident, "params must be an object")
        return
    if not isinstance(method, str):
        reject(ident, "unknown method", -32601)
        return
    if method == "initialize":
        reply(ident, {"codexHome": str(HOME)})
        return
    if method == "fixture/stats":
        reply(ident, {name: state[name] for name in COUNTERS})
        return
    if method not in {"thread/start", "thread/read", "thread/resume", "turn/start", "turn/interrupt", "turn/steer"}:
        reject(ident, "unknown method", -32601)
        return
    if not valid_cwd(params, required=method == "thread/start"):
        reject(ident, "cwd must match process cwd")
        return
    if method == "thread/start":
        if state["thread"] is None:
            state["thread"] = {"id": THREAD_ID, "cwd": str(CWD), "turns": []}
            state["thread_starts"] += 1
            persist()
        thread = thread_copy()
        reply(ident, {"thread": thread})
        emit("thread/started", {"thread": thread})
        return
    if not valid_thread(params):
        reject(ident, "unknown thread id")
        return
    if method == "thread/read":
        if not isinstance(params.get("includeTurns", False), bool):
            reject(ident, "includeTurns must be a boolean")
            return
        reply(ident, {"thread": thread_copy(params.get("includeTurns", False))})
    elif method == "thread/resume":
        state["resumes"] += 1
        persist()
        reply(ident, {"thread": thread_copy()})
    elif method == "turn/steer":
        turn = next((turn for turn in state["thread"]["turns"]
                     if turn["id"] == params.get("expectedTurnId")), None)
        if turn is None or turn["status"] != "inProgress":
            reject(ident, "unknown or terminal turn id")
            return
        state["steers"].append(params.get("input", []))
        persist()
        reply(ident, {"turnId": turn["id"]})
    elif method == "turn/interrupt":
        turn = next((turn for turn in state["thread"]["turns"]
                     if turn["id"] == params.get("turnId")), None)
        if turn is None or turn["status"] != "inProgress":
            reject(ident, "unknown or terminal turn id")
            return
        reply(ident, {})
        interrupt(turn)
    elif method == "turn/start":
        if any(turn["status"] == "inProgress" for turn in state["thread"]["turns"]):
            reject(ident, "an unresolved turn already exists")
            return
        state["turn_starts"] += 1
        number = str(state["turn_starts"])
        turn = {"id": "fixture-turn-" + number, "status": "inProgress", "items": [], "error": None}
        state["thread"]["turns"].append(turn)
        persist()
        if MODE == "exit_before_turn_reply":
            raise SystemExit(0)
        reply(ident, {"turn": copy.deepcopy(turn)})
        emit("turn/started", turn_params(turn))
        delta(turn, "Preparing fixture result. ")
        command = {
            "type": "commandExecution",
            "id": "fixture-command-" + number,
            "command": "printf '" + turn["id"] + "\\n' >> fixture-result.txt",
            "cwd": str(CWD),
            "status": "inProgress",
            "exitCode": None,
            "aggregatedOutput": "",
        }
        turn["items"].append(command)
        persist()
        emit("item/started", item_params(turn, command))
        pending = {"id": "fixture-approval-" + number, "turn": turn}
        send({"id": pending["id"], "method": "item/commandExecution/requestApproval", "params": {
            "threadId": THREAD_ID,
            "turnId": turn["id"],
            "itemId": command["id"],
            "command": command["command"],
            "cwd": str(CWD),
        }})


for line in sys.stdin:
    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        reject(None, "invalid JSON", -32700)
        continue
    handle(message)
