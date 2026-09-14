#!/usr/bin/env python3
"""Deterministic stdio server fixture. No model calls or external writes."""
import json
import os
import sys
import time


def send(value):
    print(json.dumps(value), flush=True)


mode = os.environ.get("FORGE_FIXTURE_MODE", "normal")
held = []
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    ident = message.get("id")
    if method == "initialize":
        if mode == "initialize_timeout":
            time.sleep(30)
        home = os.environ["CODEX_HOME"] if mode != "wrong_home" else "/"
        send({"id": ident, "result": {"codexHome": home}})
    elif method == "initialized":
        pass
    elif method == "environment":
        send({"id": ident, "result": {"environment": dict(os.environ), "args": sys.argv[1:]}})
    elif method == "reverse":
        held.append(message)
        if len(held) == 2:
            for request in reversed(held):
                send({"id": request["id"], "result": request["params"]})
            held.clear()
    elif method == "approvals":
        send({"id": "approval-1", "method": "item/commandExecution/requestApproval", "params": {
            "threadId": "thread-1", "turnId": "turn-1", "item": {"id": "item-1"}, "command": "echo fixture"}})
        send({"id": 42, "method": "item/tool/requestUserInput", "params": {"unknown": {"preserved": True}}})
        send({"id": ident, "result": {}})
    elif method == "wait":
        time.sleep(30)
    elif method == "eof":
        sys.exit(0)
    elif method == "terminal_then_eof":
        send({"id": ident, "result": {}})
        send({"method": "turn/completed", "params": {"turn": {"id": "turn-1", "status": "completed"}}})
        sys.exit(0)
    elif method == "invalid":
        print("not-json", flush=True)
    elif method == "flood":
        for index in range(1100):
            send({"method": "fixture/event", "params": {"index": index}})
        send({"id": ident, "result": {}})
    elif method == "duplicate_approval":
        for _ in range(2):
            send({"id": 99, "method": "item/fileChange/requestApproval", "params": {}})
    elif method == "oversized":
        sys.stdout.write("x" * (16 * 1024 * 1024 + 1))
        sys.stdout.flush()
    elif method == "error":
        send({"id": ident, "error": {"code": -32001, "message": "secret-provider-token"}})
    elif method == "large":
        send({"id": ident, "result": {"body": "x" * 100000}})
    elif method is None:
        # Only observable after the client explicitly answers a server request.
        send({"method": "fixture/answered", "params": {"answer": message}})
    else:
        send({"id": ident, "result": message.get("params", {})})
