CREATE TABLE runs (
    id TEXT PRIMARY KEY NOT NULL,
    snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json))
);
CREATE TABLE tasks (
    id TEXT PRIMARY KEY NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(id),
    version INTEGER NOT NULL CHECK (version >= 0),
    snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json))
);
CREATE INDEX tasks_run ON tasks(run_id);
CREATE TABLE attempts (
    task_id TEXT NOT NULL REFERENCES tasks(id),
    generation INTEGER NOT NULL CHECK (generation > 0),
    token TEXT NOT NULL UNIQUE,
    worker_id TEXT NOT NULL,
    expires_at_ms INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'released', 'submitted', 'outcome_unknown')),
    PRIMARY KEY (task_id, generation)
);
CREATE UNIQUE INDEX attempts_one_running ON attempts(task_id) WHERE status = 'running';
CREATE TABLE commands (
    id TEXT PRIMARY KEY NOT NULL,
    request_json TEXT NOT NULL CHECK (json_valid(request_json)),
    receipt_json TEXT NOT NULL CHECK (json_valid(receipt_json))
);
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    command_id TEXT NOT NULL UNIQUE REFERENCES commands(id) DEFERRABLE INITIALLY DEFERRED,
    run_id TEXT NOT NULL REFERENCES runs(id),
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    created_at_ms INTEGER NOT NULL
);
CREATE INDEX events_run_cursor ON events(run_id, id);
CREATE TABLE outbox (
    event_id INTEGER PRIMARY KEY REFERENCES events(id),
    delivered_at_ms INTEGER
);
CREATE INDEX outbox_pending ON outbox(event_id) WHERE delivered_at_ms IS NULL;
