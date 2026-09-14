from pathlib import Path

import pytest

from forge.db import (
    ActionItem,
    Agent,
    AgentMessage,
    DecisionRequest,
    Event,
    HumanInstruction,
    InstructionReceipt,
    Meeting,
    ModelProfile,
    Outbox,
    Project,
    Provider,
    ProviderRevision,
    Run,
    Skill,
    SkillRevision,
    Step,
    Task,
    ActivityJournal,
    session,
    uid,
)
from forge.runtime_cleanup import cleanup_runtime, main


def _seed_runtime(tmp_path):
    with session() as s:
        provider = Provider(id=uid(), name="keep", revision_id=uid())
        s.add(provider)
        s.add(ProviderRevision(id=uid(), provider_id=provider.id, config={}))
        s.add(ModelProfile(id=uid(), config={"provider_id": provider.id}))
        agent = Agent(id=uid(), config={"name": "keep"})
        s.add(agent)
        skill = Skill(id=uid(), name="keep-skill")
        s.add(skill)
        s.add(SkillRevision(id=uid(), skill_id=skill.id, version=1, digest="d", manifest={}, files={}))
        project = Project(id=uid(), owner_id="owner", config={})
        s.add(project)
        run = Run(id=uid(), project_id=project.id, owner_id="owner", title="old", request="old", snapshot={})
        s.add(run)
        task = Task(id=f"{run.id}:task", run_id=run.id, key="task", kind="autonomous", agent_id=agent.id,
                    title="task", spec={})
        s.add(task)
        s.add(Step(id=f"{run.id}:task:turn:0", state="started"))
        s.add(Step(id="setup:keep", state="completed"))
        s.add(Event(run_id=run.id, kind="runtime", data={}))
        s.add(Event(run_id=None, kind="global", data={}))
        s.add(AgentMessage(id=uid(), run_id=run.id, task_key="task", sender_id=agent.id, recipient_id=agent.id, body="x"))
        s.add(ActivityJournal(id=uid(), run_id=run.id, name="x", state="started", result={}))
        s.add(Outbox(id=uid(), run_id=run.id, action="start", payload={}))
        s.add(DecisionRequest(id=uid(), run_id=run.id, task_key="task", agent_id=agent.id, digest="d", question={}))
        s.add(HumanInstruction(id=uid(), run_id=run.id, author_id="owner", recipients=[], body="x"))
        s.add(InstructionReceipt(id=uid(), instruction_id="i", task_id=task.id))
        s.add(Meeting(id=uid(), run_id=run.id, host_id=agent.id, participants=[], subject="x", agenda="x"))
        s.add(ActionItem(id=uid(), run_id=run.id, owner_id=agent.id, specification={}))
    runs = tmp_path / "data" / "runs"
    runs.mkdir(parents=True)
    (runs / run.id / "tasks" / "task").mkdir(parents=True)
    (runs / run.id / "tasks" / "task" / "result.txt").write_text("runtime")
    runtime = tmp_path / "data" / "runtimes"
    (runtime / "poc" / "one").mkdir(parents=True)
    (runtime / "poc" / "one" / "state").write_text("runtime")
    return run.id, runs, runtime


def test_cleanup_dry_run_reports_without_mutating(tmp_path):
    run_id, runs, runtime = _seed_runtime(tmp_path)
    report = cleanup_runtime(delete=False, data_root=tmp_path / "data", runtime_data_root=runtime)
    assert report["database"]["runs"] == 1
    assert report["database"]["tasks"] == 1
    assert report["database"]["events"] == 1
    assert report["filesystem"]["runs"]["entries"] >= 2
    assert (runs / run_id).exists()
    assert (runtime / "poc" / "one" / "state").exists()
    with session() as s:
        assert s.get(Run, run_id) is not None
        assert s.get(Step, "setup:keep") is not None
        assert s.scalar(__import__('sqlalchemy').select(Event).where(Event.run_id.is_(None))) is not None


def test_cleanup_delete_removes_only_runtime_records_and_files(tmp_path):
    run_id, runs, runtime = _seed_runtime(tmp_path)
    report = cleanup_runtime(delete=True, data_root=tmp_path / "data", runtime_data_root=runtime)
    assert report["deleted"] is True
    with session() as s:
        assert s.get(Run, run_id) is None
        assert s.scalar(__import__('sqlalchemy').select(Task).where(Task.run_id == run_id)) is None
        assert s.get(Step, "setup:keep") is not None
        assert s.scalar(__import__('sqlalchemy').select(Event).where(Event.run_id.is_(None))) is not None
        assert s.get(Provider, report["kept"]["provider_id"]) is not None
    assert runs.exists() and not any(runs.iterdir())
    assert runtime.exists() and not any(runtime.iterdir())


def test_cleanup_refuses_personal_codex_directory(tmp_path):
    personal = Path.home() / ".codex"
    with pytest.raises(ValueError, match="个人目录"):
        cleanup_runtime(delete=True, data_root=tmp_path / "data", runtime_data_root=personal)


def test_cleanup_refuses_symlink_runtime_directory(tmp_path):
    target = tmp_path / "real-runtime"
    target.mkdir()
    link = tmp_path / "runtime-link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="符号链接"):
        cleanup_runtime(data_root=tmp_path / "data", runtime_data_root=link)


def test_cli_requires_explicit_delete(monkeypatch, capsys):
    monkeypatch.setattr("forge.runtime_cleanup.cleanup_runtime", lambda **kw: {"deleted": kw["delete"], "database": {}, "filesystem": {}, "kept": {}})
    assert main(["--dry-run"]) == 0
    assert '"deleted": false' in capsys.readouterr().out
    assert main(["--delete"]) == 0
    assert '"deleted": true' in capsys.readouterr().out
