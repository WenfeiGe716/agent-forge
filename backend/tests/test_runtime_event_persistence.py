from forge.db import Event, persist_runtime_event, session
from forge.runtime_bridge import RuntimeEvent


def test_runtime_event_is_persisted_losslessly_for_run(client):
    runtime = RuntimeEvent(
        "item/agentMessage/delta",
        "codex-task-1",
        "thread-1",
        "turn-1",
        "item-1",
        7,
        {"delta": "hello", "item": {"id": "item-1"}},
        request_id=42,
    )

    with session() as db:
        row = persist_runtime_event(db, "run-1", runtime)
        assert row.id is not None
        assert row.kind == "runtime.item.agentMessage.delta"
        assert row.data == {
            "source": "codex",
            "runtime_kind": "item/agentMessage/delta",
            "task_id": "codex-task-1",
            "thread_id": "thread-1",
            "turn_id": "turn-1",
            "item_id": "item-1",
            "sequence": 7,
            "request_id": 42,
            "payload": {"delta": "hello", "item": {"id": "item-1"}},
        }

    with session() as db:
        saved = db.get(Event, row.id)
        assert saved is not None
        assert saved.run_id == "run-1"
        assert saved.data["payload"]["delta"] == "hello"


def test_runtime_event_recorder_opens_a_new_transaction_per_event(client):
    from forge.runtime_events import RuntimeEventRecorder

    recorder = RuntimeEventRecorder("run-2")
    recorder(RuntimeEvent("turn/started", None, "thread-2", "turn-2", None, 1, {}))
    recorder(RuntimeEvent("turn/completed", None, "thread-2", "turn-2", None, 2, {"turn": {"status": "completed"}}))

    with session() as db:
        rows = list(db.query(Event).filter(Event.run_id == "run-2").order_by(Event.id))
        assert [row.kind for row in rows] == ["runtime.turn.started", "runtime.turn.completed"]
        assert [row.data["sequence"] for row in rows] == [1, 2]
