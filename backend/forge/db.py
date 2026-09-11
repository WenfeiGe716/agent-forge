from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import create_engine, String, Text, Integer, Boolean, JSON, Index, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from .config import settings

def uid() -> str:
    return uuid4().hex

def now() -> str:
    return datetime.now(timezone.utc).isoformat()

class Base(DeclarativeBase): pass
class User(Base):
    __tablename__ = 'users'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(20))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
class LoginSession(Base):
    __tablename__ = 'sessions'
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    expires_at: Mapped[str] = mapped_column(String(40))
class Provider(Base):
    __tablename__ = 'providers'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(100))
    revision_id: Mapped[str] = mapped_column(String(32))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
class ProviderRevision(Base):
    __tablename__ = 'provider_revisions'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    provider_id: Mapped[str] = mapped_column(String(32), index=True)
    config: Mapped[dict] = mapped_column(JSON)
    encrypted_key: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[str] = mapped_column(String(40), default=now)
class ModelProfile(Base):
    __tablename__ = 'model_profiles'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    config: Mapped[dict] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=1)
class Agent(Base):
    __tablename__ = 'agents'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    config: Mapped[dict] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=1)
class Project(Base):
    __tablename__ = 'projects'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(String(32))
    config: Mapped[dict] = mapped_column(JSON)
    base_commit: Mapped[str] = mapped_column(String(64), default='')
    created_at: Mapped[str] = mapped_column(String(40), default=now)
class Run(Base):
    __tablename__ = 'runs'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    owner_id: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(180))
    request: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), default='queued', index=True)
    snapshot: Mapped[dict] = mapped_column(JSON)
    plan: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    plan_digest: Mapped[str] = mapped_column(String(64), default='')
    artifact_digest: Mapped[str] = mapped_column(String(64), default='')
    report: Mapped[dict] = mapped_column(JSON, default=dict)
    calls: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)
class Task(Base):
    __tablename__ = 'tasks'
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    key: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(20))
    agent_id: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(180))
    spec: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(40), default='pending')
    messages: Mapped[list] = mapped_column(JSON, default=list)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    base_commit: Mapped[str] = mapped_column(String(64), default='')
    started_at: Mapped[str] = mapped_column(String(40), default='')
class Step(Base):
    __tablename__ = 'steps'
    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    state: Mapped[str] = mapped_column(String(20))
    result: Mapped[dict] = mapped_column(JSON, default=dict)
class Event(Base):
    __tablename__ = 'events'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(60))
    data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
class AgentMessage(Base):
    __tablename__ = 'agent_messages'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    task_key: Mapped[str] = mapped_column(String(64))
    sender_id: Mapped[str] = mapped_column(String(32))
    recipient_id: Mapped[str] = mapped_column(String(32), index=True)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    delivered_at: Mapped[str] = mapped_column(String(40), default='')
class ActivityJournal(Base):
    __tablename__ = 'local_activity_journal'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(60))
    state: Mapped[str] = mapped_column(String(20))
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    elapsed_s: Mapped[str] = mapped_column(String(40), default='0')
    created_at: Mapped[str] = mapped_column(String(40), default=now)
class Outbox(Base):
    __tablename__ = 'outbox'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    action: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON)
    sent: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    error: Mapped[str] = mapped_column(Text, default='')
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
class OrchestrationTemplate(Base):
    __tablename__ = 'orchestration_templates'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    config: Mapped[dict] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=1)

class Skill(Base):
    __tablename__ = 'skills'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    latest_version: Mapped[int] = mapped_column(Integer, default=1)
class SkillRevision(Base):
    __tablename__ = 'skill_revisions'
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    skill_id: Mapped[str] = mapped_column(String(32), index=True)
    version: Mapped[int] = mapped_column(Integer)
    digest: Mapped[str] = mapped_column(String(64))
    manifest: Mapped[dict] = mapped_column(JSON)
    files: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default='draft')
    approved_by: Mapped[str] = mapped_column(String(32), default='')
    created_at: Mapped[str] = mapped_column(String(40), default=now)
class DecisionRequest(Base):
    __tablename__ = 'decision_requests'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    task_key: Mapped[str] = mapped_column(String(64))
    agent_id: Mapped[str] = mapped_column(String(32))
    digest: Mapped[str] = mapped_column(String(64))
    question: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default='pending')
    answer: Mapped[dict] = mapped_column(JSON, default=dict)
    answered_by: Mapped[str] = mapped_column(String(32), default='')
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    answered_at: Mapped[str] = mapped_column(String(40), default='')
class HumanInstruction(Base):
    __tablename__ = 'human_instructions'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    author_id: Mapped[str] = mapped_column(String(32))
    recipients: Mapped[list] = mapped_column(JSON)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
class InstructionReceipt(Base):
    __tablename__ = 'instruction_receipts'
    id: Mapped[str] = mapped_column(String(180), primary_key=True)
    instruction_id: Mapped[str] = mapped_column(String(64), index=True)
    task_id: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[str] = mapped_column(String(40), default=now)
class Meeting(Base):
    __tablename__ = 'meetings'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    host_id: Mapped[str] = mapped_column(String(32))
    participants: Mapped[list] = mapped_column(JSON)
    subject: Mapped[str] = mapped_column(String(180))
    agenda: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default='active')
    outcome: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    ended_at: Mapped[str] = mapped_column(String(40), default='')
class ActionItem(Base):
    __tablename__ = 'action_items'
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    meeting_id: Mapped[str] = mapped_column(String(64), default='')
    owner_id: Mapped[str] = mapped_column(String(32))
    task_key: Mapped[str] = mapped_column(String(64), default='')
    specification: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default='proposed')
    origin: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(40), default=now)

class PlatformConfig(Base):
    __tablename__ = 'platform_config'
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)

engine = create_engine(settings.database_url, connect_args={'check_same_thread': False, 'timeout': 30} if settings.database_url.startswith('sqlite') else {}, pool_pre_ping=True)
if settings.database_url.startswith('sqlite'):
    @event.listens_for(engine, 'connect')
    def sqlite_setup(conn, _):
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA foreign_keys=ON')
SessionLocal = sessionmaker(engine, expire_on_commit=False)

@contextmanager
def session():
    with SessionLocal() as s:
        try:
            yield s
            s.commit()
        except BaseException:
            s.rollback()
            raise

def emit(s, run_id: str | None, kind: str, data: dict):
    s.add(Event(run_id=run_id, kind=kind, data=data))

def init_db():
    Base.metadata.create_all(engine)
