"""Explicit built-in human-authored layout, never an AI approval shortcut.

An installation without organization history receives this editable preset.
An exact untouched built-in v1 may receive an append-only v2 upgrade. Existing
Agents keep their identity, models, skills and permissions.
"""
import os
from copy import deepcopy

from sqlalchemy import select, update

from .db import Agent, PlatformConfig, now
from .design_schema import AssetConfirmation, OfficeConfig

ACTOR = 'builtin:office-bootstrap'
LEGACY_PRESET_ID = 'development-office-v1'
PRESET_ID = 'development-office-v2'
MARKER = 'organization_default_bootstrap'
MEMBERS = (
    ('atlas', 'Atlas', 'planner', '架构区', -8.7),
    ('nova', 'Nova', 'developer', '开发区', -2.9),
    ('echo', 'Echo', 'tester', '测试区', 2.9),
    ('sage', 'Sage', 'reviewer', '审查区', 8.7),
)


def _legacy_default_layout(agents):
    """Exact original preset, retained only to verify an untouched v1 upgrade."""
    desks, positions, member_ids, zones = [], {}, [], []
    for key, name, role, zone_name, x in MEMBERS:
        matches = [agent for agent in agents if agent.config.get('name') == name
                   and agent.config.get('role') == role and agent.config.get('enabled', True)]
        agent_id = matches[0].id if len(matches) == 1 else None
        zone_id = 'development_' + key
        zones.append({'id': zone_id, 'name': zone_name, 'kind': 'work', 'x': x,
                      'z': -2, 'width': 5.2, 'depth': 9, 'color': '#7296ab'})
        position = {'x': x, 'z': -3, 'floor': 1, 'zone': zone_id}
        desks.append({'id': 'desk_' + key, 'name': name + ' 工位', **position,
                      'team_id': 'development_team', 'agent_id': agent_id})
        if agent_id:
            positions[agent_id] = position
            member_ids.append(agent_id)
    zones.extend([
        {'id': 'development_meeting', 'name': '会议区', 'kind': 'meeting', 'x': 5.5,
         'z': 5.5, 'width': 10, 'depth': 3, 'color': '#9e8ebe'},
        {'id': 'development_common', 'name': '公共区', 'kind': 'common', 'x': -5.5,
         'z': 5.5, 'width': 10, 'depth': 3, 'color': '#78a895'},
    ])
    return OfficeConfig.model_validate({
        'description': '内置人工预置：研发部与开发团队。桌位和员工编制不创建Agent；编辑后需人工发布并应用。',
        'floors': [{'floor': 1, 'name': '研发楼层', 'department': '研发部', 'zones': zones}],
        'teams': [{'id': 'development_team', 'name': '开发团队', 'floor': 1,
                   'zone_ids': [zone['id'] for zone in zones if zone['kind'] == 'work'],
                   'headcount': 4, 'member_ids': member_ids}],
        'desks': desks, 'positions': positions,
    }).model_dump(exclude={'layout_version'})


def default_layout(agents):
    """Human-authored varied floor: twelve desks, four slots, no new Agents."""
    old = _legacy_default_layout(agents)
    occupants = {desk['id']: desk['agent_id'] for desk in old['desks']}
    zones = [
        {'id': 'development_atlas', 'name': '代码规划与架构', 'kind': 'work', 'x': -7.6, 'z': -5.75, 'width': 8, 'depth': 3.5, 'color': '#9c8cde'},
        {'id': 'development_echo', 'name': '测试质量', 'kind': 'work', 'x': 6.5, 'z': -5.75, 'width': 10, 'depth': 3.5, 'color': '#e0b65d'},
        {'id': 'development_nova', 'name': '开发工位', 'kind': 'work', 'x': -3.8, 'z': -.25, 'width': 8.6, 'depth': 6, 'color': '#54b2cc'},
        {'id': 'development_sage', 'name': '需求分析', 'kind': 'work', 'x': 6.5, 'z': -.7, 'width': 10, 'depth': 5.9, 'color': '#da91b0'},
        {'id': 'development_restroom', 'name': '卫生间', 'kind': 'restroom', 'x': -10.25, 'z': -1.5, 'width': 3.5, 'depth': 3, 'color': '#8ea5b8'},
        {'id': 'development_pantry', 'name': '饮水间', 'kind': 'pantry', 'x': -10.25, 'z': 2, 'width': 3.5, 'depth': 3, 'color': '#83aaa2'},
        {'id': 'development_lounge', 'name': '休息区', 'kind': 'lounge', 'x': -7.25, 'z': 5.75, 'width': 8.5, 'depth': 3.5, 'color': '#86b3a5'},
        {'id': 'development_meeting', 'name': '协作会议室', 'kind': 'meeting', 'x': 5.25, 'z': 5.25, 'width': 11.5, 'depth': 4.5, 'color': '#969fcf'},
    ]
    plans = [
        ('desk_atlas', 'Atlas 工位', 'development_atlas', -9.9, -6.3),
        ('empty_architecture', '架构备用桌', 'development_atlas', -6.6, -6.3),
        ('desk_nova', 'Nova 工位', 'development_nova', -6.5, -2.2),
        ('empty_development_1', '开发备用桌 1', 'development_nova', -3.3, -2.2),
        ('empty_development_2', '开发备用桌 2', 'development_nova', -6.5, .5),
        ('empty_development_3', '开发备用桌 3', 'development_nova', -3.3, .5),
        ('desk_echo', 'Echo 工位', 'development_echo', 3.1, -6.3),
        ('desk_sage', 'Sage 工位', 'development_echo', 6.4, -6.3),
        ('empty_quality', '质量备用桌', 'development_echo', 9.7, -6.3),
        ('empty_requirements_1', '需求分析桌 1', 'development_sage', 3.1, -2.6),
        ('empty_requirements_2', '需求分析桌 2', 'development_sage', 6.4, -2.6),
        ('empty_requirements_3', '需求分析桌 3', 'development_sage', 9.7, -2.6),
    ]
    desks = [{'id': did, 'name': name, 'zone': zone, 'floor': 1, 'x': x, 'z': z,
              'team_id': 'development_team', 'agent_id': occupants.get(did)} for did, name, zone, x, z in plans]
    positions = {desk['agent_id']: {key: desk[key] for key in ('x', 'z', 'floor', 'zone')}
                 for desk in desks if desk['agent_id'] is not None}
    return OfficeConfig.model_validate({
        'layout_version': 2,
        'description': '内置人工预置：研发部与开发团队；12张桌位、4名编制，空桌不创建Agent。每层保留会议室、卫生间和饮水间；编辑后需人工发布并应用。',
        'floors': [{'floor': 1, 'name': '研发楼层', 'department': '研发部', 'zones': zones}],
        'teams': [{'id': 'development_team', 'name': '开发团队', 'floor': 1,
                   'zone_ids': [zone['id'] for zone in zones if zone['kind'] == 'work'],
                   'headcount': 4, 'member_ids': list(positions)}],
        'desks': desks, 'positions': positions,
    }).model_dump()


def upgrade_untouched_builtin_layout(s):
    """Append v2 only when every persisted sign still identifies our exact v1.

    Human revisions, applications, alternate office assets, changed spatial
    Agent configuration, or inconsistent history all cause a no-op.
    """
    from .design_center import DesignAsset, DesignAudit, DesignRevision, _audit, checksum
    from .organization import apply_layout

    # Match application lock ordering, then lock the asset before re-reading
    # history. A concurrent human edit/apply must either precede the checks or
    # encounter the new version; it cannot be overwritten between check/write.
    s.execute(update(PlatformConfig).where(PlatformConfig.id == 'organization_layout')
              .values(value=PlatformConfig.value).execution_options(synchronize_session=False))
    current, marker = s.get(PlatformConfig, 'organization_layout'), s.get(PlatformConfig, MARKER)
    if not current or not marker:
        return None
    s.refresh(current)
    s.refresh(marker)
    if not isinstance(current.value, dict) or not isinstance(marker.value, dict) or marker.value.get('preset_id') != LEGACY_PRESET_ID:
        return None
    ref = current.value.get('ref') or {}
    if not isinstance(ref, dict) or ref != marker.value.get('ref') or ref.get('version') != 1:
        return None
    s.execute(update(DesignAsset).where(DesignAsset.id == ref.get('id'))
              .values(lock_version=DesignAsset.lock_version).execution_options(synchronize_session=False))
    assets = list(s.scalars(select(DesignAsset).where(DesignAsset.kind == 'office').execution_options(populate_existing=True)))
    if len(assets) != 1 or assets[0].id != ref.get('id'):
        return None
    asset = assets[0]
    if asset.created_by != ACTOR or asset.latest_version != 1 or asset.published_version != 1 or asset.lock_version != 0:
        return None
    revisions = list(s.scalars(select(DesignRevision).where(DesignRevision.asset_id == asset.id).execution_options(populate_existing=True)))
    if len(revisions) != 1:
        return None
    old = revisions[0]
    if (old.version != 1 or old.status != 'published' or old.source != 'manual'
            or old.created_by != ACTOR or old.published_by != ACTOR
            or not isinstance(old.generation, dict)
            or old.generation.get('kind') != 'builtin_preset' or old.generation.get('preset_id') != LEGACY_PRESET_ID
            or old.generation.get('edited') or current.value.get('config') != old.config
            or ref.get('digest') != old.digest or checksum('office', old.name, old.config) != old.digest):
        return None
    audits = list(s.scalars(select(DesignAudit).where(DesignAudit.asset_id == asset.id)))
    if (any(row.actor != ACTOR for row in audits)
            or sorted(row.action for row in audits) != ['applied', 'bootstrap', 'proposed']):
        return None
    agents = list(s.scalars(select(Agent)))
    if old.config != _legacy_default_layout(agents):
        return None
    for agent in agents:
        if agent.id in old.config['positions'] and (
                agent.config.get('office_position') != old.config['positions'][agent.id]
                or agent.config.get('department') != '研发部'):
            return None
    config, name = default_layout(agents), '默认研发部与开发团队布局'
    revision = DesignRevision(id=f'{asset.id}:2', asset_id=asset.id, version=2, name=name, config=config,
        digest=checksum('office', name, config), source='manual', created_by=ACTOR,
        generation={'kind': 'builtin_preset', 'preset_id': PRESET_ID, 'upgraded_from': deepcopy(ref),
                    'automatic_ai_approval': False, 'human_review_performed': False,
                    'reason': '仅追加未被人工修改的内置初始布局；保留原v1与已有运行快照。'},
        status='published', published_by=ACTOR, published_at=now())
    asset.latest_version, asset.published_version = 2, 2
    s.add(revision)
    s.flush()
    _audit(s, asset, revision, 'bootstrap_upgrade', ACTOR, '内置未修改预置升级：保留v1，补齐设施和空桌；不是人工审批。')
    result = apply_layout(s, asset.id, AssetConfirmation(version=2, digest=revision.digest,
        comment='内置预置升级：只调整组织空间，不创建Agent或更改模型权限。'), ACTOR)
    marker.value = {**deepcopy(marker.value), 'preset_id': PRESET_ID,
                    'ref': result['organization']['ref'], 'upgraded_from': deepcopy(ref), 'updated_at': now()}
    return result


def bootstrap_default_layout(s):
    from .design_center import DesignAsset, _audit, _get, create_asset, set_generation
    from .organization import apply_layout

    # Production defaults on. Test fixtures explicitly opt out so tests for an
    # empty installation do not silently inherit a published design asset.
    if os.getenv('FORGE_BOOTSTRAP_DEFAULT_ORGANIZATION', 'true').lower() in {'0', 'false', 'no', 'off'}:
        return None
    # Serialize startup with the platform row already created by bootstrap.
    s.execute(update(PlatformConfig).where(PlatformConfig.id == 'runtime')
              .values(value=PlatformConfig.value).execution_options(synchronize_session=False))
    upgraded = upgrade_untouched_builtin_layout(s)
    if upgraded is not None:
        return upgraded
    if (s.get(PlatformConfig, 'organization_layout') or s.get(PlatformConfig, MARKER)
            or s.scalar(select(DesignAsset.id).where(DesignAsset.kind == 'office').limit(1))):
        return None
    config = default_layout(list(s.scalars(select(Agent))))
    proposed = create_asset(s, 'office', '默认研发部与开发团队布局', config, ACTOR, source='manual')
    set_generation(s, proposed['id'], 1, {
        'kind': 'builtin_preset', 'preset_id': PRESET_ID, 'author': 'Agent Forge built-in human-authored preset',
        'automatic_ai_approval': False, 'human_review_performed': False,
        'reason': '仅初始化尚无组织布局或办公室设计历史的安装；后续编辑须经人工发布和应用。',
    })
    asset, revision = _get(s, proposed['id'], 1)
    # Explicit bootstrap provenance, not a forged admin review or a generic
    # route that automatically publishes generated proposals.
    revision.status, revision.published_by, revision.published_at = 'published', ACTOR, now()
    asset.published_version = 1
    s.flush()
    _audit(s, asset, revision, 'bootstrap', ACTOR, '登记内置人工预置版本；不是人工审批记录，也不是AI自动批准。')
    confirmation = AssetConfirmation(version=1, digest=revision.digest, comment='首次组织布局初始化：仅分配已有成员工位与部门。')
    applied = apply_layout(s, asset.id, confirmation, ACTOR)
    s.add(PlatformConfig(id=MARKER, value={
        'preset_id': PRESET_ID, 'ref': applied['organization']['ref'], 'initialized_at': now(),
    }))
    return applied
