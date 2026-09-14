"""Read-only product mapping for the bounded Rust-owned local fixture runner.

This is a trusted local operator adapter, not an authenticated public API.
It never creates a Python Run or decrypts provider credentials. Use
``export_run_input_with_evidence`` and retain both returned JSON documents when
the product revisions must remain inspectable after the live assets change.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, load_only
from sqlalchemy.pool import NullPool

from forge.db import Agent, ModelProfile, PlatformConfig, Project, Provider, ProviderRevision, User
from forge.member_execution_policy import freeze_policies
from forge.providers import validate_parameters
from forge.schema import AgentInput, ModelInput, ProjectInput


class ExportError(ValueError):
    """Product configuration cannot be faithfully mapped to this Rust slice."""


def _digest(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


@contextmanager
def _product_session(product_db):
    path = Path(product_db).resolve(strict=True)
    if not path.is_file():
        raise ExportError('product_db must be an existing SQLite file')

    def connect():
        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        connection.execute('PRAGMA query_only=ON')
        return connection

    engine = create_engine('sqlite://', creator=connect, poolclass=NullPool)
    try:
        with Session(engine, autoflush=False) as session:
            # sqlite3 does not begin a read transaction for SELECT on its own.
            session.connection().exec_driver_sql('BEGIN')
            yield session
    finally:
        engine.dispose()


def _text(value, label, limit=200):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
        raise ExportError(f'{label} must be a bounded nonblank string')
    return value


def _local_endpoint(value):
    _text(value, 'responses_base_url', 1500)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ExportError('invalid local Responses endpoint') from error
    if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', '::1'}
            or port is None or not 1 <= port <= 65535 or parsed.path != '/v1'
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or '?' in value or '#' in value):
        raise ExportError('only an explicit loopback HTTP endpoint ending in /v1 is supported')
    return parsed.hostname, port, parsed.path


def _runtime_paths(executable, workspace, runtime_home):
    paths = [Path(value) for value in (executable, workspace, runtime_home)]
    if any(not path.is_absolute() or '..' in path.parts for path in paths):
        raise ExportError('runtime paths must be absolute and contain no parent traversal')
    executable, workspace, runtime_home = paths
    executable = executable.resolve(strict=True)
    workspace = workspace.resolve(strict=True)
    if not executable.is_file() or not os.access(executable, os.X_OK) or not workspace.is_dir():
        raise ExportError('runtime executable and workspace must exist')
    if runtime_home.exists() or runtime_home.is_symlink():
        raise ExportError('runtime_home must be fresh')
    runtime_home = runtime_home.parent.resolve(strict=True) / runtime_home.name
    personal = Path.home().resolve()
    forbidden = (personal / '.codex', personal / '.opencodex')
    if (runtime_home == Path(runtime_home.anchor) or runtime_home == personal
            or any(runtime_home.is_relative_to(path.resolve())
                   or path.resolve().is_relative_to(runtime_home) for path in forbidden)
            or runtime_home.is_relative_to(workspace) or workspace.is_relative_to(runtime_home)):
        raise ExportError('runtime home overlaps workspace or personal runtime directories')
    return str(executable), str(workspace), str(runtime_home)


def _parse(model, config, label, **extra):
    if not isinstance(config, dict):
        raise ExportError(f'{label} configuration must be an object')
    try:
        return model.model_validate({**config, **extra}).model_dump()
    except ValueError:
        # Pydantic error text includes input values; do not expose them here.
        raise ExportError(f'{label} configuration is invalid or contains unsupported fields') from None


def _unsupported(config, fields, label):
    for field in fields:
        if config.get(field):
            raise ExportError(f'{label}.{field} is not supported by the Rust fixture runner')


def export_run_input(product_db, **options) -> dict:
    """Return strict Rust RunInput JSON; accepts the keyword arguments below."""
    run_input, _ = export_run_input_with_evidence(product_db, **options)
    return run_input


def export_run_input_with_evidence(
    product_db, *, project_id, actor_id, agent_id, run_id, task_id,
    runtime_executable, working_directory, runtime_home, runtime_version,
    responses_base_url, gateway_route_id, gateway_version, capability_evidence_ref,
    prompt, artifact_path, expected_content, fixture_mode=None, approved_command=None,
) -> tuple[dict, dict]:
    """Return (RunInput, immutable key-free source evidence) from one read snapshot.

The supported product subset is one developer/tester with a Responses primary
model, no fallback, custom tool subset, skills, model parameters, runtime
overrides or project check commands. The explicit route must match its frozen
provider endpoint. Other product configurations fail instead of being dropped.
    """
    for value, label in ((project_id, 'project_id'), (actor_id, 'actor_id'),
                         (agent_id, 'agent_id'), (run_id, 'run_id'), (task_id, 'task_id'),
                         (runtime_version, 'runtime_version'), (gateway_route_id, 'gateway_route_id'),
                         (gateway_version, 'gateway_version'),
                         (capability_evidence_ref, 'capability_evidence_ref')):
        _text(value, label)
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode('utf-8')) > 65536:
        raise ExportError('prompt must be nonblank and at most 64 KiB')
    if fixture_mode not in (None, 'normal', 'exit_before_turn_reply', 'exit_after_effect'):
        raise ExportError('unsupported fixture_mode')
    if approved_command is not None:
        _text(approved_command, 'approved_command', 65536)
    artifact = Path(artifact_path)
    if (artifact.is_absolute() or len(artifact.parts) != 1 or artifact.name in {'', '.', '..'}
            or '/' in str(artifact_path) or '\\' in str(artifact_path)):
        raise ExportError('artifact_path must name one workspace file')
    if not isinstance(expected_content, str) or len(expected_content.encode('utf-8')) > 1024 * 1024:
        raise ExportError('expected_content must be at most 1 MiB')
    endpoint = _local_endpoint(responses_base_url)
    executable, workspace, home = _runtime_paths(runtime_executable, working_directory, runtime_home)
    if (Path(workspace) / artifact).exists() or (Path(workspace) / artifact).is_symlink():
        raise ExportError('acceptance artifact must be fresh')

    with _product_session(product_db) as session:
        actor = session.scalar(select(User).where(User.id == actor_id).options(
            load_only(User.id, User.role, User.enabled, raiseload=True)))
        if actor is None or not actor.enabled or actor.role not in {'admin', 'developer'}:
            raise ExportError('actor must be an enabled admin or developer')
        project = session.get(Project, project_id)
        if project is None:
            raise ExportError('project does not exist or actor cannot access it')
        if not isinstance(project.config, dict) or project.config.get('execution_mode', 'container') != 'local':
            raise ExportError('project must explicitly use local execution')
        project_config = _parse(ProjectInput, project.config, 'project', full_access_confirmed=True)
        if (actor.role != 'admin' and actor.id != project.owner_id
                and actor.id not in project_config['members']):
            raise ExportError('project does not exist or actor cannot access it')
        if project_config['archived'] or project_config['execution_mode'] != 'local':
            raise ExportError('project must be unarchived and use local execution')
        _unsupported(project_config, ('runtime', 'check_commands'), 'project')
        agent = session.get(Agent, agent_id)
        if agent is None:
            raise ExportError('selected agent does not exist')
        agent_config = _parse(AgentInput, agent.config, 'agent')
        if not agent_config['enabled'] or agent_config['role'] not in {'developer', 'tester'}:
            raise ExportError('selected agent must be an enabled developer or tester')
        _unsupported(agent_config, ('fallback_model_ids', 'skills', 'runtime', 'model_parameters',
                                    'role_template_ref', 'design_ref'), 'agent')
        if agent_config['tool_permissions'] is not None:
            raise ExportError('custom agent.tool_permissions cannot be enforced by this Rust runner')
        if not agent_config['model_profile_id']:
            raise ExportError('selected agent has no primary model')
        profile = session.get(ModelProfile, agent_config['model_profile_id'])
        if profile is None:
            raise ExportError('primary model profile does not exist')
        model_config = _parse(ModelInput, profile.config, 'model')
        if not model_config['enabled'] or profile.config.get('supports_tools') is not True:
            raise ExportError('primary model must be enabled and confirmed to support tools')
        provider = session.get(Provider, model_config['provider_id'])
        if provider is None or not provider.enabled:
            raise ExportError('primary model provider is missing or disabled')
        revision = session.scalar(select(ProviderRevision).where(
            ProviderRevision.id == provider.revision_id).options(load_only(
                ProviderRevision.id, ProviderRevision.provider_id, ProviderRevision.config, raiseload=True)))
        if revision is None or revision.provider_id != provider.id:
            raise ExportError('provider revision is missing or belongs to a different provider')
        connection = revision.config
        if (not isinstance(connection, dict)
                or set(connection) - {'protocol', 'base_url', 'external', 'protocol_config'}
                or connection.get('protocol') != 'openai_responses'
                or type(connection.get('external')) is not bool):
            raise ExportError('provider must have a supported Responses connection configuration')
        _unsupported(connection, ('protocol_config',), 'provider')
        if connection['external'] and project.config.get('allow_external_models') is not True:
            raise ExportError('project has not authorized external models')
        if _local_endpoint(connection.get('base_url')) != endpoint:
            raise ExportError('explicit route does not match the selected provider revision')
        try:
            validate_parameters(connection['protocol'], model_config['allowed_parameters'],
                                model_config['parameters'], connection.get('protocol_config'))
        except ValueError as error:
            raise ExportError('primary model parameters are invalid for its protocol') from error
        _unsupported(model_config, ('parameters',), 'model')
        if type(agent.version) is not int or agent.version < 1 or type(profile.version) is not int or profile.version < 1:
            raise ExportError('member and model revisions must be positive versions')
        for key in ('runtime', 'roles'):
            row = session.get(PlatformConfig, key)
            value = row.value if row else {}
            if not isinstance(value, dict):
                raise ExportError('platform runtime configuration is invalid')
            selected = value if key == 'runtime' else value.get(agent_config['role'], {})
            if not isinstance(selected, dict):
                raise ExportError('platform role runtime configuration is invalid')
            if selected:
                raise ExportError(f'platform.{key} overrides are not supported by the Rust fixture runner')
        try:
            policy = freeze_policies(session, project, [agent])[agent.id]
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise ExportError('member execution policy is invalid') from error
        if policy.get('profile') not in {'ask', 'auto', 'full'}:
            raise ExportError('member execution policy is invalid')
        member_evidence = {'id': agent.id, 'version': agent.version, 'config': deepcopy(agent.config),
                           'execution_policy': policy}
        policy_inputs = {}
        for key in ('member_execution_policies', 'organization_layout'):
            row = session.get(PlatformConfig, key)
            policy_inputs[key] = deepcopy(row.value) if row else None
        evidence = {
            'schema_version': 1, 'source': 'python-sqlite-readonly',
            'actor': {'id': actor.id, 'role': actor.role, 'enabled': actor.enabled},
            'project': {'id': project.id, 'owner_id': project.owner_id,
                        'config': deepcopy(project.config), 'base_commit': project.base_commit},
            'member': member_evidence,
            'model_profile': {'id': profile.id, 'version': profile.version, 'config': deepcopy(profile.config)},
            'provider': {'id': provider.id, 'name': provider.name, 'revision_id': provider.revision_id,
                         'enabled': provider.enabled},
            'provider_revision': {'id': revision.id, 'provider_id': revision.provider_id,
                                  'config': deepcopy(connection)},
            'policy_inputs': policy_inputs,
        }

    instruction_parts = []
    for heading, content in (('Project instructions', project_config['instructions']),
                             ('Member instructions', agent_config['instructions'])):
        if content:
            instruction_parts.append(f'{heading}:\n{content}')
    instruction_parts.append(f'Task:\n{prompt}' if instruction_parts else prompt)
    combined_prompt = '\n\n'.join(instruction_parts)
    if len(combined_prompt.encode('utf-8')) > 65536:
        raise ExportError('combined project, member and task instructions exceed 64 KiB')
    run_input = {
        'schema_version': 1, 'run_id': run_id, 'task_id': task_id, 'worker_id': agent_id,
        'config': {
            'project_id': project_id, 'owner_id': actor_id,
            'members': [{'worker_id': agent_id, 'config_revision': 'sha256:' + _digest(member_evidence)}],
            'permission_profile': policy['profile'], 'working_directory': workspace,
            'runtime_home': home, 'runtime_version': runtime_version,
            'route_binding': {
                'provider_id': provider.id, 'provider_revision_id': revision.id,
                'model_id': model_config['model_id'], 'upstream_protocol': connection['protocol'],
                'gateway_route_id': gateway_route_id, 'responses_base_url': responses_base_url,
                'config_digest': 'pending-rust-freeze', 'credential_ref': 'env:FORGE_RUN_API_KEY',
                'capability_evidence_ref': capability_evidence_ref, 'gateway_version': gateway_version,
            },
        },
        'provider_snapshot': {
            'provider_id': provider.id,
            'provider_revision_id': revision.id,
            'upstream_base_url': connection['base_url'],
            'credential_ref': 'env:FORGE_RUN_API_KEY',
            'config_digest': 'pending-rust-freeze',
            'models': [{
                'model_id': model_config['model_id'],
                'upstream_protocol': connection['protocol'],
                'capability_evidence_ref': capability_evidence_ref,
            }],
        },
        'runtime_executable': executable, 'prompt': combined_prompt,
        'acceptance': {'path': str(artifact), 'expected_content': expected_content},
        'fixture_mode': fixture_mode, 'approved_command': approved_command,
    }
    evidence['run_input_sha256'] = _digest(run_input)
    evidence['sha256'] = _digest(evidence)
    return run_input, evidence
