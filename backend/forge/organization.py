"""Human-published organization layouts; applying a layout never grants tools.

Departments and seats move together only at this explicit application seam.
Current layout reads and runtime copies do not follow draft revisions.
"""
from copy import deepcopy

from sqlalchemy import update

from .db import Agent, PlatformConfig, emit, now
from .design_schema import OfficeConfig


def current_layout(s):
    row = s.get(PlatformConfig, 'organization_layout')
    if not row:
        return {'ref': None, 'config': {'positions': {}, 'floors': [], 'description': ''}, 'applied_at': None}
    return deepcopy(row.value)


def inspect_workstations(layout, agents):
    """Project current, applied desk ownership. A seat is not a task mutex.

    Suggested Agent positions and legacy positions-only layouts are not an
    authoritative desk/team assignment. This function never repairs a layout.
    """
    parsed, issue = None, ''
    if not layout.get('ref'):
        issue = '尚未应用组织布局'
    else:
        try:
            parsed = OfficeConfig.model_validate(layout.get('config', {}))
            if parsed.desks is None:
                issue = '当前布局只有旧版位置记录，尚未配置正式桌位与办公团队'
        except ValueError:
            issue = '当前已应用布局的楼层、区域、团队或桌位关系无效'
    floors = {floor.floor: floor for floor in parsed.floors} if parsed else {}
    teams = {team.id: team for team in parsed.teams} if parsed else {}
    checks = []
    for agent in agents:
        name = agent.config.get('name') or agent.id
        reason, workstation = issue, None
        if not reason:
            desks = [desk for desk in parsed.desks if desk.agent_id == agent.id]
            if len(desks) != 1:
                reason = '没有唯一的正式工位'
            else:
                desk = desks[0]
                team, floor = teams.get(desk.team_id), floors.get(desk.floor)
                if not team or agent.id not in team.member_ids or not floor:
                    reason = '工位缺少有效的部门和团队归属'
                elif agent.config.get('department', '') != floor.department:
                    reason = f'当前部门“{agent.config.get("department") or "未分配"}”与工位所属“{floor.department}”不一致'
                else:
                    workstation = {'desk_id': desk.id, 'desk_name': desk.name, 'team_id': team.id,
                        'team_name': team.name, 'floor': floor.floor, 'floor_name': floor.name,
                        'department': floor.department, 'zone': desk.zone,
                        'position': {key: getattr(desk, key) for key in ('x', 'z', 'floor', 'zone')},
                        'layout_ref': deepcopy(layout['ref'])}
        checks.append({'code': 'agent.workstation', 'status': 'error' if reason else 'ok',
            'agent_id': agent.id, 'agent_name': name, 'target': 'organization',
            'message': f'{name}：{reason}。请到“楼层与部门”安排所属团队工位，发布并应用布局后再发起任务。' if reason else
                f'{name}：已入座 {workstation["department"]} / {workstation["team_name"]} / {workstation["desk_name"]}；不限制参与其他并行运行',
            'workstation': workstation})
    return checks


def validate_members(s, config):
    parsed = OfficeConfig.model_validate(config)
    for agent_id in parsed.positions:
        agent = s.get(Agent, agent_id)
        if not agent or not agent.config.get('enabled', True):
            raise ValueError('办公室布局包含不存在或停用的成员，请重新编辑后发布')
    return parsed


def apply_layout(s, asset_id, confirmation, actor):
    from .design_center import resolve_published, _audit, _get

    # Acquire the write transaction before reading agent configuration. Different
    # layout assets must not partially interleave their current organization state.
    s.execute(update(PlatformConfig).where(PlatformConfig.id == 'organization_layout')
              .values(value=PlatformConfig.value).execution_options(synchronize_session=False))
    layout = resolve_published(s, {'id': asset_id, 'version': confirmation.version, 'digest': confirmation.digest})
    if layout['kind'] != 'office':
        raise ValueError('请选择已发布办公室布局')
    parsed = validate_members(s, layout['config'])
    departments = {item.floor: item.department for item in parsed.floors}
    changes = []
    for aid, position in parsed.positions.items():
        agent = s.get(Agent, aid)
        config = deepcopy(agent.config)
        previous = {'department': config.get('department', ''), 'office_position': deepcopy(config.get('office_position'))}
        config['office_position'] = position.model_dump()
        if parsed.floors:
            config['department'] = departments[position.floor]
        if config != agent.config:
            agent.config = config
            agent.version += 1
            changes.append({'agent_id': aid, 'before': previous,
                            'after': {'department': config.get('department', ''), 'office_position': config['office_position']}})
    # Store the exact approved content, not a model_dump with newly injected
    # defaults: config and digest continue to identify the same immutable design.
    value = {'ref': {'id': asset_id, 'version': confirmation.version, 'digest': confirmation.digest},
             'config': deepcopy(layout['config']), 'applied_at': now()}
    row = s.get(PlatformConfig, 'organization_layout')
    if row:
        row.value = value
    else:
        s.add(PlatformConfig(id='organization_layout', value=value))
    asset, revision = _get(s, asset_id, confirmation.version)
    _audit(s, asset, revision, 'applied', actor, confirmation.comment)
    emit(s, None, 'office.layout.applied', {'actor': actor, **value['ref'], 'changes': changes,
                                          'model_bindings_changed': False, 'tool_permissions_changed': False})
    return {'applied': True, 'id': asset_id, 'version': confirmation.version,
            'organization': deepcopy(value), 'changed_agent_ids': [item['agent_id'] for item in changes]}
