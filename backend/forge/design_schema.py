"""Editable proposals and strict publication contracts for the Design Center."""
import json
import math
from typing import Any, Literal

from pydantic import Field, model_validator

from .schema import AgentInput, OfficePosition, OrchestrationInput, Strict, TOOLS_BY_ROLE

AssetKind = Literal['capability', 'routing', 'team', 'office', 'workflow', 'benchmark']


def parse_benchmark_json(raw):
    """Require standard finite JSON for both expected and measured answers."""
    def reject_constant(value):
        raise ValueError('基准JSON不允许NaN或Infinity')
    value = json.loads(raw, parse_constant=reject_constant)
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError('基准JSON数值必须有限')
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return value


class AssetCreate(Strict):
    kind: AssetKind
    name: str = Field(min_length=1, max_length=120)
    config: dict[str, Any]
    # Provenance for automated proposals can only be written by server helpers.
    source: Literal['manual'] = 'manual'


class AssetEdit(Strict):
    name: str = Field(min_length=1, max_length=120)
    config: dict[str, Any]
    expected_version: int = Field(ge=1)
    source: Literal['manual'] = 'manual'


class AssetConfirmation(Strict):
    version: int = Field(ge=1)
    digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    comment: str = Field('', max_length=2000)


class TeamMember(AgentInput):
    key: str = Field(pattern=r'^[a-zA-Z][a-zA-Z0-9_-]{0,63}$')
    office_position: OfficePosition | None = None
    tool_permissions: list[str] | None = Field(None, max_length=20)

    @model_validator(mode='after')
    def permissions(self):
        if self.design_ref is not None:
            raise ValueError('模板成员不能声明实例来源，实例来源由服务器记录')
        if self.tool_permissions is not None:
            allowed = set(TOOLS_BY_ROLE[self.role]) | {'request_decision'}
            if len(set(self.tool_permissions)) != len(self.tool_permissions):
                raise ValueError('成员工具权限不能重复')
            if set(self.tool_permissions) - allowed:
                raise ValueError('成员工具权限不能超过角色权限')
        return self


class TeamConfig(Strict):
    members: list[TeamMember] = Field(min_length=1, max_length=30)
    workflow: OrchestrationInput | None = None
    description: str = Field('', max_length=4000)

    @model_validator(mode='after')
    def references(self):
        keys = {member.key for member in self.members}
        if len(keys) != len(self.members):
            raise ValueError('团队成员标识不能重复')
        if len({m.name.strip().casefold() for m in self.members}) != len(self.members):
            raise ValueError('团队成员名称不能重复')
        parents = {m.key: m.reports_to for m in self.members}
        for member in self.members:
            if not member.name.strip():
                raise ValueError('成员名称不能为空')
            if member.reports_to and member.reports_to not in keys:
                raise ValueError('直属领导必须属于当前团队')
            recipients = member.communication.recipients
            if len(set(recipients)) != len(recipients) or set(recipients) - keys:
                raise ValueError('通信白名单必须是当前团队内不重复的成员')
            seen = {member.key}
            ancestor = member.reports_to
            while ancestor:
                if ancestor in seen:
                    raise ValueError('直属领导关系存在循环或指向自己')
                seen.add(ancestor)
                ancestor = parents.get(ancestor)
        if self.workflow and set(self.workflow.participants) - keys:
            raise ValueError('团队编排参与者必须使用当前团队成员标识')
        return self


class RoutingRules(Strict):
    allowed_provider_ids: list[str] = Field(default_factory=list, max_length=100)
    allow_external: bool = False
    require_tools: bool = True
    max_model_calls: int = Field(100, ge=1, le=10000)


class RoutingConfig(Strict):
    task_type: str = Field(min_length=1, max_length=120)
    assignments: dict[Literal['planner', 'developer', 'tester', 'reviewer'], str]
    budget: Literal['low', 'medium', 'high'] = 'medium'
    max_parallel: int = Field(3, ge=1, le=32)
    rules: RoutingRules = Field(default_factory=RoutingRules)
    description: str = Field('', max_length=4000)

    @model_validator(mode='after')
    def assignment_required(self):
        if not self.assignments or any(not model_id for model_id in self.assignments.values()):
            raise ValueError('路由策略至少需要一个有效的角色模型分配')
        if len(set(self.rules.allowed_provider_ids)) != len(self.rules.allowed_provider_ids):
            raise ValueError('供应商白名单不能重复')
        return self


class OfficeZone(Strict):
    id: str = Field(pattern=r'^[a-zA-Z][a-zA-Z0-9_-]{0,47}$')
    name: str = Field(min_length=1, max_length=80)
    kind: Literal['work', 'meeting', 'common', 'lounge', 'restroom', 'pantry'] = 'work'
    x: float = Field(0, ge=-12, le=12, allow_inf_nan=False)
    z: float = Field(0, ge=-8, le=8, allow_inf_nan=False)
    width: float = Field(gt=0, le=24, allow_inf_nan=False)
    depth: float = Field(gt=0, le=16, allow_inf_nan=False)
    color: str | None = Field(None, pattern=r'^#[0-9a-fA-F]{6}$')

    @model_validator(mode='after')
    def geometry(self):
        if not self.name.strip():
            raise ValueError('区域名称不能为空白')
        if abs(self.x) + self.width / 2 > 12 + 1e-9 or abs(self.z) + self.depth / 2 > 8 + 1e-9:
            raise ValueError('区域边界必须位于办公室范围内（X ±12，Z ±8）')
        return self


class OfficeFloor(Strict):
    floor: int = Field(ge=1, le=10, strict=True)
    name: str = Field(min_length=1, max_length=80)
    department: str = Field(min_length=1, max_length=80)
    zones: list[OfficeZone] = Field(min_length=1, max_length=20)

    @model_validator(mode='after')
    def organization(self):
        if not self.name.strip() or not self.department.strip():
            raise ValueError('楼层名称和部门不能为空白')
        if self.department != self.department.strip():
            raise ValueError('部门名称首尾不能包含空白')
        if not any(zone.kind == 'work' for zone in self.zones):
            raise ValueError('每个部门楼层至少需要一个工作区域')
        for i, first in enumerate(self.zones):
            for second in self.zones[i + 1:]:
                if (abs(first.x - second.x) < (first.width + second.width) / 2 - 1e-9
                        and abs(first.z - second.z) < (first.depth + second.depth) / 2 - 1e-9):
                    raise ValueError('同一楼层的区域不能相互重叠')
        return self


class OfficeDesk(OfficePosition):
    id: str = Field(pattern=r'^[a-zA-Z][a-zA-Z0-9_-]{0,63}$')
    name: str = Field(min_length=1, max_length=80)
    floor: int = Field(1, ge=1, le=10, strict=True)
    zone: str = Field(pattern=r'^[a-zA-Z][a-zA-Z0-9_-]{0,47}$')
    team_id: str | None = Field(None, pattern=r'^[a-zA-Z][a-zA-Z0-9_-]{0,63}$')
    agent_id: str | None = Field(None, min_length=1, max_length=80)

    @model_validator(mode='after')
    def named(self):
        if not self.name.strip(): raise ValueError('桌位名称不能为空白')
        return self


class OfficeTeam(Strict):
    id: str = Field(pattern=r'^[a-zA-Z][a-zA-Z0-9_-]{0,63}$')
    name: str = Field(min_length=1, max_length=80)
    floor: int = Field(ge=1, le=10, strict=True)
    zone_ids: list[str] = Field(min_length=1, max_length=20)
    headcount: int = Field(ge=0, le=300, strict=True)
    member_ids: list[str] = Field(default_factory=list, max_length=300)

    @model_validator(mode='after')
    def membership(self):
        if not self.name.strip(): raise ValueError('团队名称不能为空白')
        if len(set(self.zone_ids)) != len(self.zone_ids): raise ValueError('团队工作区域不能重复')
        if len(set(self.member_ids)) != len(self.member_ids): raise ValueError('团队成员不能重复')
        if any(not member.strip() for member in self.member_ids): raise ValueError('团队成员标识不能为空白')
        if len(self.member_ids) > self.headcount: raise ValueError('团队实际成员数不能超过员工编制')
        return self


class OfficeConfig(Strict):
    layout_version: Literal[1, 2] = 1
    positions: dict[str, OfficePosition]
    floors: list[OfficeFloor] = Field(default_factory=list, max_length=10)
    teams: list[OfficeTeam] = Field(default_factory=list, max_length=100)
    # None is the legacy positions-only contract; [] explicitly means no desks.
    desks: list[OfficeDesk] | None = Field(None, max_length=300)
    description: str = Field('', max_length=4000)

    @model_validator(mode='before')
    @classmethod
    def version_number(cls, value):
        if isinstance(value, dict) and 'layout_version' in value and type(value['layout_version']) is not int:
            raise ValueError('布局版本必须为整数1或2')
        return value

    @model_validator(mode='after')
    def bounds(self):
        if len(self.positions) > 300 or self.desks is None and not self.floors and not self.positions:
            raise ValueError('办公室布局最多300个成员位置；旧版布局至少需要一个位置')
        if self.desks is None and self.teams:
            raise ValueError('团队编制需要显式桌位列表；旧版positions布局不能声明团队')
        if self.layout_version == 2 and not self.floors and (self.positions or self.desks or self.teams):
            raise ValueError('新版成员、桌位和团队必须引用带必备设施的部门楼层')
        if self.desks is not None:
            if len({desk.id for desk in self.desks}) != len(self.desks):
                raise ValueError('桌位标识不能重复')
            occupied = [desk for desk in self.desks if desk.agent_id is not None]
            if len({desk.agent_id for desk in occupied}) != len(occupied):
                raise ValueError('同一成员只能占用一张桌位')
            derived = {desk.agent_id: OfficePosition(x=desk.x, z=desk.z, floor=desk.floor, zone=desk.zone)
                       for desk in occupied}
            if self.positions != derived:
                raise ValueError('positions必须与已入座桌位导出的成员位置完全一致')
            if not self.floors and (self.desks or self.teams):
                raise ValueError('桌位和团队必须引用已配置的楼层')
        if not self.floors:
            return self
        floors = {item.floor: item for item in self.floors}
        if len(floors) != len(self.floors):
            raise ValueError('楼层编号不能重复')
        if len({item.department.casefold() for item in self.floors}) != len(self.floors):
            raise ValueError('默认一层一个部门；同一部门不能重复占用多个楼层')
        zones = [zone.id for item in self.floors for zone in item.zones]
        if len(set(zones)) != len(zones):
            raise ValueError('区域标识必须在整份组织布局中唯一')
        if self.layout_version == 2:
            for floor in self.floors:
                kinds = {zone.kind for zone in floor.zones}
                if not {'meeting', 'restroom', 'pantry'} <= kinds:
                    raise ValueError('新版每层必须保留协作会议室、卫生间和饮水间')
                for zone in floor.zones:
                    if zone.kind in {'meeting', 'lounge', 'restroom', 'pantry'}:
                        min_width = 4 if zone.kind == 'meeting' else 3
                        if zone.width < min_width or zone.depth < 3:
                            raise ValueError(f'{zone.name}尺寸不足：{zone.kind}至少需要{min_width}×3的家具空间')
        seats = self.desks if self.desks is not None else list(self.positions.values())
        for position in seats:
            floor = floors.get(position.floor)
            if not floor:
                raise ValueError('成员工位引用了不存在的楼层')
            zone = next((zone for zone in floor.zones if zone.id == position.zone), None)
            if not zone or zone.kind != 'work':
                raise ValueError('成员工位必须引用所在楼层的工作区域标识')
            if (position.x - 1.4 < zone.x - zone.width / 2 - 1e-9
                    or position.x + 1.4 > zone.x + zone.width / 2 + 1e-9
                    or position.z - .6 < zone.z - zone.depth / 2 - 1e-9
                    or position.z + 1.45 > zone.z + zone.depth / 2 + 1e-9):
                raise ValueError('成员完整工位（桌面及椅子）必须位于所选工作区域内')
        for i, first in enumerate(seats):
            for second in seats[i + 1:]:
                if (first.floor == second.floor and abs(first.x - second.x) < 2.8 - 1e-9
                        and abs(first.z - second.z) < 2.05 - 1e-9):
                    raise ValueError('同一楼层的成员工位不能重叠，请调整桌椅间距')
        if self.desks is not None:
            teams = {team.id: team for team in self.teams}
            if len(teams) != len(self.teams): raise ValueError('团队标识不能重复')
            owned_zones, members = set(), set()
            for team in self.teams:
                floor = floors.get(team.floor)
                if not floor: raise ValueError('团队引用了不存在的楼层')
                work = {zone.id for zone in floor.zones if zone.kind == 'work'}
                if set(team.zone_ids) - work: raise ValueError('团队所属区域必须是同层的工作区域')
                if owned_zones.intersection(team.zone_ids): raise ValueError('不同团队不能重复占有所属工作区域')
                owned_zones.update(team.zone_ids)
                if members.intersection(team.member_ids): raise ValueError('成员不能跨团队重复配置')
                members.update(team.member_ids)
                if team.headcount > sum(desk.team_id == team.id for desk in self.desks):
                    raise ValueError('团队员工编制不能超过该团队桌位数量')
            occupied_members = set()
            for desk in self.desks:
                team = teams.get(desk.team_id) if desk.team_id is not None else None
                if desk.team_id is not None and not team: raise ValueError('桌位引用了不存在的团队')
                if team and (desk.floor != team.floor or desk.zone not in team.zone_ids):
                    raise ValueError('团队桌位必须位于该团队同层所属工作区域')
                if desk.agent_id is not None:
                    if not team or desk.agent_id not in team.member_ids:
                        raise ValueError('已入座成员必须属于桌位所属团队')
                    occupied_members.add(desk.agent_id)
            if occupied_members != members:
                raise ValueError('每位团队成员必须占用自己团队的一张桌位')
        return self


class WorkflowConfig(Strict):
    graph: OrchestrationInput
    description: str = Field('', max_length=4000)


class BenchmarkCase(Strict):
    id: str = Field(pattern=r'^[a-zA-Z][a-zA-Z0-9_-]{0,63}$')
    dimension: Literal['coding', 'reasoning']
    prompt: str = Field(min_length=1, max_length=12000)
    expected: str = Field(min_length=1, max_length=12000)
    match: Literal['exact', 'contains', 'json'] = 'exact'

    @model_validator(mode='after')
    def json_expected(self):
        if self.match == 'json':
            try:
                parse_benchmark_json(self.expected)
            except (TypeError, ValueError, RecursionError) as exc:
                raise ValueError('JSON基准用例的预期结果必须是有效JSON') from exc
        return self


class BenchmarkConfig(Strict):
    cases: list[BenchmarkCase] = Field(min_length=1, max_length=12)
    description: str = Field('', max_length=4000)

    @model_validator(mode='after')
    def distinct(self):
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError('基准测试用例标识不能重复')
        return self
