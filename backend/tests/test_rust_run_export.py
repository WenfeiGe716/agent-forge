"""Product authorization and immutable mapping, with no model or process calls."""

from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from forge.db import Agent, Base, ModelProfile, PlatformConfig, Project, Provider, ProviderRevision, User
from forge.schema import AgentInput, ModelInput, ProjectInput


SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'export_rust_run_config.py'
SPEC = importlib.util.spec_from_file_location('export_rust_run_config', SCRIPT)
exporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(exporter)


@pytest.fixture
def product(tmp_path):
    database = tmp_path / 'product.db'
    engine = create_engine('sqlite:///' + str(database))
    Base.metadata.create_all(engine)
    with engine.connect() as connection:
        connection.exec_driver_sql('PRAGMA journal_mode=WAL')
    with Session(engine) as session:
        session.add_all([
            User(id='owner', username='owner', password_hash='secret-password-hash', role='developer'),
            User(id='member-user', username='member', password_hash='secret-password-hash', role='developer'),
            User(id='outsider', username='outsider', password_hash='secret-password-hash', role='developer'),
            User(id='admin', username='admin', password_hash='secret-password-hash', role='admin'),
            Project(id='project', owner_id='owner', config=ProjectInput(
                name='Local fixture', allow_external_models=False,
                members=['member-user'], instructions='Keep the interface stable.').model_dump()),
            Agent(id='worker', version=3, config=AgentInput(
                name='Builder', role='developer', model_profile_id='profile',
                instructions='Verify the requested artifact.').model_dump()),
            ModelProfile(id='profile', version=2, config=ModelInput(
                name='Fixture model', provider_id='provider', model_id='fixture-model',
                allowed_parameters=[], parameters={}).model_dump()),
            Provider(id='provider', name='Local provider', revision_id='revision'),
            ProviderRevision(id='revision', provider_id='provider',
                config={'protocol': 'openai_responses', 'base_url': 'http://127.0.0.1:18789/v1',
                        'external': False, 'protocol_config': {}}, encrypted_key='secret-encrypted-provider-key'),
        ])
        session.commit()
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    options = {
        'project_id': 'project', 'actor_id': 'owner', 'agent_id': 'worker',
        'run_id': 'run-1', 'task_id': 'task-1', 'runtime_executable': sys.executable,
        'working_directory': str(workspace), 'runtime_home': str(tmp_path / 'runtime-home'),
        'runtime_version': 'fixture-v1', 'responses_base_url': 'http://127.0.0.1:18789/v1',
        'gateway_route_id': 'fixture-route-1', 'gateway_version': 'fixture-gateway-v1',
        'capability_evidence_ref': 'fixture-probe-1', 'prompt': 'Create result.txt.',
        'artifact_path': 'result.txt', 'expected_content': 'ok\n',
    }
    yield database, engine, options
    engine.dispose()


def export(product, **changes):
    database, _, options = product
    return exporter.export_run_input(database, **(options | changes))


def update_config(product, model, record_id, **changes):
    with Session(product[1]) as session:
        record = session.get(model, record_id)
        record.config = deepcopy(record.config) | changes
        session.commit()


def test_export_uses_product_ids_frozen_policy_and_retained_secret_free_evidence(product):
    database, _, options = product
    before = database.read_bytes()
    run_input, evidence = exporter.export_run_input_with_evidence(database, **options)
    assert database.read_bytes() == before
    assert run_input == export(product)
    assert run_input['worker_id'] == 'worker'
    config = run_input['config']
    assert config['project_id'] == 'project' and config['owner_id'] == 'owner'
    assert config['members'] == [{'worker_id': 'worker',
        'config_revision': 'sha256:' + exporter._digest(evidence['member'])}]
    assert config['permission_profile'] == 'ask'
    route = config['route_binding']
    assert route['provider_id'] == 'provider' and route['provider_revision_id'] == 'revision'
    assert route['model_id'] == 'fixture-model'
    assert route['credential_ref'] == 'env:FORGE_RUN_API_KEY'
    assert route['config_digest'] == 'pending-rust-freeze'
    assert evidence['model_profile']['version'] == 2
    assert evidence['member']['version'] == 3
    assert evidence['sha256'] == exporter._digest({key: value for key, value in evidence.items() if key != 'sha256'})
    assert evidence['run_input_sha256'] == exporter._digest(run_input)
    serialized = json.dumps([run_input, evidence])
    assert 'secret-password-hash' not in serialized and 'secret-encrypted-provider-key' not in serialized
    assert 'Keep the interface stable.' in run_input['prompt']
    assert 'Verify the requested artifact.' in run_input['prompt']
    assert run_input['prompt'].endswith('Create result.txt.')


@pytest.mark.parametrize('actor', ['member-user', 'admin'])
def test_project_user_membership_and_admin_authorize_export(product, actor):
    assert export(product, actor_id=actor)['config']['owner_id'] == actor


@pytest.mark.parametrize('actor', ['worker', 'outsider', 'missing'])
def test_agent_identity_does_not_grant_project_user_access(product, actor):
    with pytest.raises(exporter.ExportError):
        export(product, actor_id=actor)


@pytest.mark.parametrize('column,value', [('enabled', False), ('role', 'viewer')])
def test_disabled_or_viewer_actor_cannot_export(product, column, value):
    with Session(product[1]) as session:
        setattr(session.get(User, 'owner'), column, value)
        session.commit()
    with pytest.raises(exporter.ExportError, match='enabled admin or developer'):
        export(product)


@pytest.mark.parametrize('model,record_id,changes', [
    (Project, 'project', {'archived': True}),
    (Project, 'project', {'execution_mode': 'container'}),
    (Project, 'project', {'runtime': {'max_steps': 10}}),
    (Project, 'project', {'check_commands': ['pytest']}),
    (Agent, 'worker', {'enabled': False}),
    (Agent, 'worker', {'role': 'reviewer'}),
    (Agent, 'worker', {'fallback_model_ids': ['fallback']}),
    (Agent, 'worker', {'model_profile_id': None}),
    (Agent, 'worker', {'tool_permissions': ['read_file', 'finish']}),
    (Agent, 'worker', {'model_parameters': {'temperature': 0.5}}),
    (Agent, 'worker', {'runtime': {'max_steps': 10}}),
    (Agent, 'worker', {'skills': [{'skill_id': 'skill', 'version': 1}]}),
    (ModelProfile, 'profile', {'enabled': False}),
    (ModelProfile, 'profile', {'supports_tools': False}),
    (ModelProfile, 'profile', {'allowed_parameters': ['temperature'], 'parameters': {'temperature': 0.5}}),
    (ProviderRevision, 'revision', {'protocol': 'openai_chat'}),
    (ProviderRevision, 'revision', {'protocol_config': {'headers': {'x-extra': 'value'}}}),
    (ProviderRevision, 'revision', {'external': True}),
    (ProviderRevision, 'revision', {'base_url': 'http://127.0.0.1:18790/v1'}),
])
def test_unrepresentable_or_unauthorized_product_configuration_is_rejected(product, model, record_id, changes):
    update_config(product, model, record_id, **changes)
    with pytest.raises(exporter.ExportError):
        export(product)


def test_external_provider_requires_explicit_project_permission(product):
    update_config(product, ProviderRevision, 'revision', external=True)
    update_config(product, Project, 'project', allow_external_models=True)
    assert export(product)['config']['route_binding']['provider_id'] == 'provider'


@pytest.mark.parametrize('model,record_id,field', [
    (Project, 'project', 'execution_mode'),
    (Project, 'project', 'allow_external_models'),
    (ModelProfile, 'profile', 'supports_tools'),
])
def test_missing_legacy_fields_do_not_gain_new_schema_authorization_defaults(product, model, record_id, field):
    if field == 'allow_external_models':
        update_config(product, ProviderRevision, 'revision', external=True)
    with Session(product[1]) as session:
        record = session.get(model, record_id)
        changed = deepcopy(record.config)
        del changed[field]
        record.config = changed
        session.commit()
    with pytest.raises(exporter.ExportError):
        export(product)


@pytest.mark.parametrize('kind', ['disabled-provider', 'wrong-revision-owner', 'missing-revision'])
def test_provider_revision_integrity(product, kind):
    with Session(product[1]) as session:
        if kind == 'disabled-provider':
            session.get(Provider, 'provider').enabled = False
        elif kind == 'wrong-revision-owner':
            session.get(ProviderRevision, 'revision').provider_id = 'other-provider'
        else:
            session.get(Provider, 'provider').revision_id = 'missing-revision'
        session.commit()
    with pytest.raises(exporter.ExportError):
        export(product)


def seed_policy(product, *, member_profile=None, layout_id='office-1'):
    layout = {'ref': {'id': layout_id, 'version': 1, 'digest': 'a' * 64},
              'config': {'teams': [{'id': 'team-1', 'name': 'Build', 'member_ids': ['worker']}]}}
    policy = {'version': 4, 'teams': {'team-1': {'profile': 'auto', 'layout_id': 'office-1'}},
              'agents': {'worker': {'profile': member_profile}} if member_profile else {}}
    with Session(product[1]) as session:
        session.add_all([PlatformConfig(id='organization_layout', value=layout),
                         PlatformConfig(id='member_execution_policies', value=policy)])
        session.commit()


@pytest.mark.parametrize('member_profile,layout_id,expected', [
    (None, 'office-1', 'auto'), ('full', 'office-1', 'full'), ('ask', 'office-1', 'ask'),
    (None, 'other-office', 'ask'),
])
def test_reuses_member_over_team_over_project_permission_resolution(product, member_profile, layout_id, expected):
    seed_policy(product, member_profile=member_profile, layout_id=layout_id)
    assert export(product)['config']['permission_profile'] == expected


def test_read_only_connection_blocks_accidental_product_write(product, monkeypatch):
    database = product[0]
    before = hashlib.sha256(database.read_bytes()).hexdigest()

    def unexpected_write(session, project, agents):
        session.execute(text("UPDATE projects SET owner_id='unexpected'"))

    monkeypatch.setattr(exporter, 'freeze_policies', unexpected_write)
    with pytest.raises(OperationalError, match='readonly'):
        export(product)
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    with Session(product[1]) as session:
        assert session.get(Project, 'project').owner_id == 'owner'


def test_policy_reads_share_snapshot_with_authorization_and_model_reads(product, monkeypatch):
    seed_policy(product)
    original = exporter.freeze_policies

    def concurrent_policy_edit(session, project, agents):
        with Session(product[1]) as writer:
            row = writer.get(PlatformConfig, 'member_execution_policies')
            updated = deepcopy(row.value)
            updated['teams']['team-1']['profile'] = 'full'
            row.value = updated
            writer.commit()
        return original(session, project, agents)

    monkeypatch.setattr(exporter, 'freeze_policies', concurrent_policy_edit)
    run_input, evidence = exporter.export_run_input_with_evidence(product[0], **product[2])
    assert run_input['config']['permission_profile'] == 'auto'
    assert evidence['policy_inputs']['member_execution_policies']['teams']['team-1']['profile'] == 'auto'
    monkeypatch.setattr(exporter, 'freeze_policies', original)
    assert export(product)['config']['permission_profile'] == 'full'


def test_export_does_not_read_credential_columns(product, monkeypatch):
    original_connect = sqlite3.connect

    def guarded_connect(*args, **kwargs):
        assert kwargs.get('uri') is True and str(args[0]).endswith('?mode=ro')
        connection = original_connect(*args, **kwargs)

        def authorize(operation, table, column, *_):
            if operation == sqlite3.SQLITE_READ and column in {'password_hash', 'encrypted_key'}:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        return connection

    monkeypatch.setattr(exporter.sqlite3, 'connect', guarded_connect)
    assert export(product)['worker_id'] == 'worker'


@pytest.mark.parametrize('changes', [
    {'responses_base_url': 'https://example.com/v1'},
    {'responses_base_url': 'http://localhost:18789/v1'},
    {'responses_base_url': 'http://127.0.0.1:18789/v1?token=secret'},
    {'responses_base_url': 'http://secret@127.0.0.1:18789/v1'},
    {'artifact_path': '../result.txt'}, {'artifact_path': '/tmp/result.txt'},
    {'fixture_mode': 'unknown'}, {'working_directory': 'relative'},
])
def test_rejects_route_path_and_fixture_scope_errors(product, changes):
    with pytest.raises(exporter.ExportError):
        export(product, **changes)


def test_missing_database_is_not_created(product, tmp_path):
    missing = tmp_path / 'missing.db'
    with pytest.raises(FileNotFoundError):
        exporter.export_run_input(missing, **product[2])
    assert not missing.exists()
