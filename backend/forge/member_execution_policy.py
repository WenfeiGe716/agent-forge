"""Execution policy inheritance, administration and immutable runtime resolution.

Member overrides beat the member's currently applied office team, then the
selected project. This module never grants role tools or model/data access.
Runtime consumers read only the frozen member policy, never this live ledger.
"""
from copy import deepcopy
from typing import Literal

from fastapi import Depends, HTTPException
from pydantic import Field
from sqlalchemy import select, update

from .db import Agent, PlatformConfig, emit, now, session
from .schema import Strict
from .organization import current_layout

KEY = 'member_execution_policies'
PROFILES = ('ask', 'auto', 'full')


class LayoutReference(Strict):
    id: str = Field(min_length=1, max_length=120)
    version: int = Field(ge=1, strict=True)
    digest: str = Field(pattern=r'^[a-f0-9]{64}$')


class PolicyUpdate(Strict):
    permission_profile: Literal['ask', 'auto', 'full'] | None
    expected_version: int = Field(ge=0, strict=True)
    expected_layout_ref: LayoutReference | None
    full_access_confirmed: bool = Field(False, strict=True)


def _state(s):
    row = s.get(PlatformConfig, KEY)
    value = deepcopy(row.value) if row else {'version': 0, 'teams': {}, 'agents': {}}
    if type(value.get('version')) is not int or value['version'] < 0:
        raise ValueError('团队与成员执行策略版本无效，请管理员检查')
    for scope in ('teams', 'agents'):
        if not isinstance(value.get(scope), dict): raise ValueError('执行策略配置无效')
        for entry in value[scope].values():
            if not isinstance(entry, dict) or entry.get('profile') not in PROFILES:
                raise ValueError('执行策略包含无效权限档，不会自动降级或扩大权限')
    return value


def _layout(s):
    layout = current_layout(s)
    teams = layout.get('config', {}).get('teams', []) if layout.get('ref') else []
    by_member = {}
    for team in teams:
        for aid in team.get('member_ids', []):
            if aid in by_member: raise ValueError('当前应用布局中成员归属多个团队，无法确定继承权限')
            by_member[aid] = team
    return layout, teams, by_member


def _team_entry(value, layout, team):
    entry = value['teams'].get(team['id']) if team else None
    # A different layout asset reusing a short team ID must not acquire an old
    # team's full-access grant. Revisions of the same organization retain it.
    return entry if entry and entry.get('layout_id') == (layout.get('ref') or {}).get('id') else None


def _resolve(value, layout, team, agent_id=None, project=None):
    config = project.config if project else {}
    base = config.get('permission_profile', 'ask') if project else None
    if base is not None and base not in PROFILES: raise ValueError('项目执行权限档无效')
    profile, source, source_id, source_name = base, 'project', project.id if project else None, config.get('name', '任务项目')
    inherited = base
    team_entry = _team_entry(value, layout, team)
    if team_entry:
        profile, source, source_id, source_name = team_entry['profile'], 'team', team['id'], team['name']
    member_entry = value['agents'].get(agent_id) if agent_id else None
    if agent_id:
        inherited = profile
        if member_entry: profile, source, source_id, source_name = member_entry['profile'], 'agent', agent_id, '成员单独设置'
    override = member_entry if agent_id else team_entry
    return {'permission_profile': override['profile'] if override else None,
            'effective_profile': profile, 'inherited_profile': inherited,
            'source': source, 'source_id': source_id, 'source_name': source_name,
            'policy_version': value['version']}


def inventory(s, project=None):
    value = _state(s)
    layout, teams, by_member = _layout(s)
    agents = list(s.scalars(select(Agent)))
    names = {a.id: a.config.get('name', a.id) for a in agents}
    floors = {floor['floor']: floor for floor in layout.get('config', {}).get('floors', [])}
    return {'version': value['version'], 'layout_ref': deepcopy(layout.get('ref')),
        'project': {'id': project.id, 'name': project.config['name'],
                    'permission_profile': project.config.get('permission_profile', 'ask'),
                    'execution_mode': project.config.get('execution_mode', 'container')} if project else None,
        'teams': [{'id': team['id'], 'name': team['name'], 'floor': team['floor'],
                   'department': floors.get(team['floor'], {}).get('department', ''),
                   'member_ids': list(team.get('member_ids', [])),
                   'members': [{'id': aid, 'name': names.get(aid, '成员已移除')} for aid in team.get('member_ids', [])],
                   **_resolve(value, layout, team, project=project)} for team in teams],
        'agents': [{'id': agent.id, 'name': names[agent.id], 'role': agent.config['role'],
                    'job_title': agent.config.get('job_title', ''), 'enabled': agent.config.get('enabled', True),
                    'team_id': by_member.get(agent.id, {}).get('id'), 'team_name': by_member.get(agent.id, {}).get('name', ''),
                    **_resolve(value, layout, by_member.get(agent.id), agent.id, project)} for agent in agents]}


def resolved_policy(s, agent_id, project):
    """Read-only preview for current configuration; does not authorize execution."""
    value = _state(s)
    layout, _, by_member = _layout(s)
    resolved = _resolve(value, layout, by_member.get(agent_id), agent_id, project)
    return {'profile': resolved['effective_profile'],
            **{key: resolved[key] for key in ('source', 'source_id', 'source_name', 'policy_version')},
            'layout_ref': deepcopy(layout.get('ref'))}


def freeze_policies(s, project, agents):
    # One ledger/layout read for the entire selected pool. Even first-ever ledger
    # creation racing this freeze cannot split members across policy versions.
    value = _state(s)
    layout, _, by_member = _layout(s)
    policies = {}
    for agent in agents:
        resolved = _resolve(value, layout, by_member.get(agent.id), agent.id, project)
        policies[agent.id] = {'profile': resolved['effective_profile'],
            **{key: resolved[key] for key in ('source', 'source_id', 'source_name', 'policy_version')},
            'layout_ref': deepcopy(layout.get('ref'))}
    for agent in agents:
        if policies[agent.id]['profile'] == 'full' and project.config.get('execution_mode', 'container') != 'local':
            raise ValueError(f'{agent.config["name"]} 的完全访问权限仅支持本机执行；请选择本机项目或调整该成员/团队权限')
    return policies


def execution_project(run, agent_id):
    """Return an execution config copy from frozen data, including legacy runs.

    Unknown agents or malformed new policies fail closed. A missing member
    policy is the deliberate compatibility path for historical snapshots only.
    """
    agent = run.snapshot.get('agents', {}).get(agent_id)
    if agent is None: raise ValueError('成员不属于本次冻结运行，不能解析执行权限')
    project = deepcopy(run.snapshot['project'])
    policy = agent.get('execution_policy')
    profile = policy.get('profile') if isinstance(policy, dict) else project.get('permission_profile', 'ask')
    if policy is not None and not isinstance(policy, dict): raise ValueError('冻结成员执行策略无效')
    if profile not in PROFILES: raise ValueError('冻结权限策略无效')
    if profile == 'full' and project.get('execution_mode', 'container') != 'local':
        raise ValueError('完全访问仅支持本机执行，不会放宽容器权限')
    project['permission_profile'] = profile
    return project


def save(s, scope, target_id, data, actor, project=None):
    if scope not in ('team', 'agent'): raise HTTPException(404, '未知执行策略对象')
    # Serialize ledger writers before reading both policy and applied membership.
    s.execute(update(PlatformConfig).where(PlatformConfig.id == KEY).values(value=PlatformConfig.value)
              .execution_options(synchronize_session=False))
    s.expire_all()
    value = _state(s)
    layout, teams, by_member = _layout(s)
    if data.expected_version != value['version']: raise HTTPException(409, '执行权限配置已变化，请刷新后重新确认')
    expected_ref = data.expected_layout_ref.model_dump() if data.expected_layout_ref else None
    if expected_ref != layout.get('ref'): raise HTTPException(409, '当前团队或成员归属已变化，请刷新后核对范围')
    team = next((item for item in teams if item['id'] == target_id), None) if scope == 'team' else by_member.get(target_id)
    agent = s.get(Agent, target_id) if scope == 'agent' else None
    if (scope == 'team' and team is None) or (scope == 'agent' and agent is None):
        raise HTTPException(404, '请选择当前已应用团队或现有成员；草稿团队不能授予执行权限')
    before = _resolve(value, layout, team, target_id if scope == 'agent' else None, project)
    bucket = value['teams' if scope == 'team' else 'agents']
    if data.permission_profile is None:
        bucket.pop(target_id, None)
    else:
        bucket[target_id] = {'profile': data.permission_profile, 'updated_by': actor.id, 'updated_at': now(),
                            'layout_id': (layout.get('ref') or {}).get('id') if scope == 'team' else None}
    after = _resolve(value, layout, team, target_id if scope == 'agent' else None, project)
    if after['effective_profile'] == 'full' and not data.full_access_confirmed:
        raise ValueError('完全访问（包括恢复继承的完全访问）必须明确确认当前账号文件和网络访问风险')
    value['version'] += 1
    after['policy_version'] = value['version']
    row = s.get(PlatformConfig, KEY)
    if row: row.value = value
    else: s.add(PlatformConfig(id=KEY, value=value))
    emit(s, None, 'execution.policy.updated', {'actor': actor.id, 'scope': scope, 'target_id': target_id,
         'version': value['version'], 'layout_ref': deepcopy(layout.get('ref')), 'before': before, 'after': after,
         'member_ids': list(team.get('member_ids', [])) if scope == 'team' else [target_id],
         'full_access_confirmed': data.full_access_confirmed, 'existing_runs_changed': False,
         'role_tools_changed': False, 'model_authorization_changed': False})
    s.flush()
    return inventory(s, project)


def register(app):
    from .api import admin, current_user, project_access

    @app.get('/api/execution/policies')
    def policies(project_id: str = '', user=Depends(current_user)):
        with session() as s:
            return inventory(s, project_access(s, project_id, user) if project_id else None)

    @app.put('/api/execution/policies/{scope}/{target_id}')
    def update_policy(scope: str, target_id: str, data: PolicyUpdate, project_id: str = '', user=Depends(admin)):
        with session() as s:
            return save(s, scope, target_id, data, user, project_access(s, project_id, user) if project_id else None)
