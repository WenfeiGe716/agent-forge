"""Build an isolated compatibility fixture using the product's actual ORM."""

import os
from pathlib import Path
import sys

target = Path(sys.argv[1]).resolve()
if target.exists():
    raise SystemExit("fixture database must not already exist")

# Importing the product settings must never select an installed database.
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
sys.path.insert(0, str(Path(__file__).resolve().parents[5] / "backend"))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from forge.db import Base, Event, Outbox, Project, Run, Task, User
from forge.security import password_hash

engine = create_engine("sqlite:///" + str(target))
Base.metadata.create_all(engine)
encoded = password_hash("fixture-password-\u5bc6\u7801-\u00e9")
created = "2026-09-12T01:00:00+00:00"
secrets = {
    key: "fixture-secret-" + key
    for key in (
        "password", "password_hash", "passwordHash", "api_key", "apiKey",
        "API-KEY", "encrypted_key", "authorization", "cookie", "headers",
        "env", "credentials", "claim_token", "claimToken", "access_token",
        "accessToken",
        "ssh_private_key", "tlsPrivateKey", "client_private_key",
        "encryption_key", "signing_key", "requestHeaders", "env_vars",
    )
}

with Session(engine) as session:
    for name, role, enabled, password in (
        ("owner", "developer", True, encoded),
        ("member", "viewer", True, encoded),
        ("admin", "admin", True, encoded),
        ("outsider", "developer", True, encoded),
        ("run-owner", "developer", True, encoded),
        ("disabled", "admin", False, encoded),
        ("malformed", "viewer", True, "not-a-python-password-hash"),
    ):
        session.add(User(id=name + "-id", username=name, role=role,
                         enabled=enabled, password_hash=password))

    for name, owner, members in (
        ("owned", "owner-id", []),
        ("shared", "outsider-id", ["member-id"]),
        ("private", "outsider-id", []),
    ):
        session.add(Project(id="project-" + name, owner_id=owner,
                            config={"name": name, "members": members},
                            created_at=created))

    snapshot = {
        "agents": {"agent-z": {"api_key": "fixture-secret-agent"},
                   "agent-a": {"name": "analyst"}},
        "execution_strategy": "autonomous",
        "execution_engine": "codex",
        "run_project_checks": False,
        "entry_agent_id": "agent-z",
        "planner_id": "agent-a",
        "reviewer_id": None,
        "nested": [secrets, {"credential_revision": "revision-1",
                              "input_tokens": 17, "output_tokens": 9}],
        "narrative": "A literal password narrative stays visible.",
    }
    for name, project, date, frozen in (
        ("shared", "shared", created, snapshot),
        ("old", "shared", "2026-09-10T01:00:00+00:00", {}),
        ("owned", "owned", "2026-09-11T01:00:00+00:00", {}),
        ("private", "private", "2026-09-13T01:00:00+00:00", {}),
    ):
        session.add(Run(
            id="run-" + name, project_id="project-" + project,
            owner_id="run-owner-id", title="Fixture " + name,
            request="Inspect fixture", status="paused", snapshot=frozen,
            plan={"steps": [{"authorization": "fixture-secret-plan"}]},
            plan_digest="plan-digest", artifact_digest="artifact-digest",
            report={"result": [{"credentials": {"secret": "fixture-secret-report"}}]},
            calls=3, input_tokens=17, output_tokens=9, error="",
            created_at=date, updated_at=created,
        ))

    session.add(Task(
        id="task-shared", run_id="run-shared", key="analysis", kind="analysis",
        agent_id="agent-a", title="Analyze fixture", status="completed",
        spec={"nested": {"env": {"TOKEN": "fixture-secret-task-spec"}}},
        result={"items": [{"apiKey": "fixture-secret-task-result"}]},
        messages=[{"content": "private message field must not be projected"}],
    ))
    session.add(Task(id="task-private", run_id="run-private", key="private",
                     kind="analysis", agent_id="agent-a", title="Private",
                     spec={}, result={}))
    session.add(Outbox(
        id="command-shared", run_id="run-shared", action="resume",
        payload={"nested": [{"claim_token": "fixture-secret-command"}]},
        sent=True, error="",
    ))
    session.add(Outbox(id="command-private", run_id="run-private",
                       action="cancel", payload={}))
    for event_id, run_id in (
        (1, None), (2, "run-private"), (3, "run-shared"),
        (4, "run-private"), (5, None), (6, "run-shared"),
        (7, "run-owned"), (8, "run-shared"), (9, "run-private"), (10, None),
    ):
        session.add(Event(
            id=event_id, run_id=run_id, kind="runtime.fixture",
            data={"payload": [{"accessToken": "fixture-secret-event"}],
                  "message": "Visible event " + str(event_id),
                  "credential_revision": "revision-1", "input_tokens": 17},
            created_at=created,
        ))
    session.commit()

engine.dispose()
