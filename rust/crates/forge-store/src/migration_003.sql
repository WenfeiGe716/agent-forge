CREATE TABLE provider_snapshots (
    provider_id TEXT NOT NULL,
    provider_revision_id TEXT NOT NULL,
    snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json)),
    PRIMARY KEY (provider_id, provider_revision_id)
);
CREATE TABLE run_provider_snapshots (
    run_id TEXT PRIMARY KEY NOT NULL REFERENCES runs(id),
    provider_id TEXT NOT NULL,
    provider_revision_id TEXT NOT NULL,
    FOREIGN KEY (provider_id, provider_revision_id)
        REFERENCES provider_snapshots(provider_id, provider_revision_id)
);
CREATE TABLE run_routes (
    run_id TEXT PRIMARY KEY NOT NULL REFERENCES runs(id),
    snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json))
);
CREATE TABLE run_permissions (
    run_id TEXT PRIMARY KEY NOT NULL REFERENCES runs(id),
    snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json))
);
INSERT INTO run_routes(run_id, snapshot_json)
    SELECT id, json_extract(snapshot_json, '$.config.route_binding') FROM runs
    WHERE json_type(snapshot_json, '$.config') = 'object';
INSERT INTO run_permissions(run_id, snapshot_json)
    SELECT id, json_object(
        'approval_policy', CASE json_extract(snapshot_json, '$.config.permission_profile')
            WHEN 'full' THEN 'never' ELSE 'on-request' END,
        'approvals_reviewer', CASE json_extract(snapshot_json, '$.config.permission_profile')
            WHEN 'auto' THEN 'auto_review' ELSE 'user' END,
        'sandbox', CASE json_extract(snapshot_json, '$.config.permission_profile')
            WHEN 'full' THEN 'danger-full-access' ELSE 'workspace-write' END
    ) FROM runs WHERE json_type(snapshot_json, '$.config') = 'object';
CREATE TABLE runtime_approvals (
    id TEXT PRIMARY KEY NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(id),
    connection_id TEXT NOT NULL,
    connection_generation INTEGER NOT NULL CHECK (connection_generation > 0),
    request_id_json TEXT NOT NULL CHECK (json_valid(request_id_json)),
    version INTEGER NOT NULL CHECK (version >= 0),
    status TEXT NOT NULL CHECK (status IN
        ('pending', 'queued', 'dispatching', 'delivered', 'expired', 'delivery_unknown')),
    snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json)),
    UNIQUE (run_id, connection_id, connection_generation, request_id_json)
);
CREATE INDEX approvals_run_status ON runtime_approvals(run_id, status, id);
