"""Resolve an explicit task team against one human-applied organization snapshot.

Office membership is selection provenance, never a template instantiation,
model assignment, permission grant, or instruction to include everybody.
"""
from copy import deepcopy

from fastapi import HTTPException


def resolve_office_team(organization, team_id, layout_ref, selected_ids):
    if team_id is None:
        return None
    current_ref = organization.get('ref')
    if not current_ref:
        raise ValueError('尚未应用组织布局，请先应用包含该办公团队的布局，或明确选择其他成员')
    if layout_ref != current_ref:
        raise HTTPException(409, '组织布局已更新，办公团队或成员名单可能变化；请刷新并重新确认本次团队与成员')
    config = organization.get('config', {})
    team = next((team for team in config.get('teams', []) if team.get('id') == team_id), None)
    if not team:
        raise ValueError('所选办公团队不在当前已应用的组织布局中，请重新选择团队')
    members = team.get('member_ids', [])
    if not members:
        raise ValueError(f'办公团队「{team["name"]}」尚未配置成员；不会自动选择其他团队或全体成员')
    if not selected_ids:
        raise ValueError('请明确选择本次执行成员；空选择不会自动使用整个办公团队')
    if set(selected_ids) - set(members):
        raise ValueError(f'所选成员不完全属于办公团队「{team["name"]}」，请按当前团队名单重新确认')
    floor = next((floor for floor in config.get('floors', []) if floor.get('floor') == team.get('floor')), None)
    if not floor:
        raise ValueError('办公团队所属楼层不存在，请重新应用有效的组织布局')
    return deepcopy({'id': team['id'], 'name': team['name'], 'floor': team['floor'],
                     'floor_name': floor['name'], 'department': floor['department'],
                     'layout_ref': current_ref, 'member_ids': members,
                     'selected_agent_ids': list(selected_ids)})
