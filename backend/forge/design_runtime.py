"""Consume human-published designs once, at task creation; never route mid-run."""
from copy import deepcopy
from types import SimpleNamespace

from fastapi import Depends

from .db import PlatformConfig, session, emit
from .schema import Strict, DesignReference, AgentInput
from .design_schema import TeamConfig, AssetConfirmation, RoutingConfig, WorkflowConfig
from .design_center import resolve_published


def _reference(ref):
    return ref.model_dump() if hasattr(ref, 'model_dump') else ref


def prepare_design(s, data, selected):
    selected = [SimpleNamespace(id=a.id, version=a.version, config=deepcopy(a.config)) for a in selected]
    runtime, graph, metadata = deepcopy(data.runtime), None, {}
    from .organization import current_layout
    organization = current_layout(s)
    if organization['ref']:
        metadata['organization'] = organization
    if data.office_team_id:
        from .office_team_selection import resolve_office_team
        metadata['organization_team'] = resolve_office_team(
            organization, data.office_team_id, _reference(data.office_layout_ref), data.agent_ids)
    if data.team_ref:
        team = resolve_published(s, _reference(data.team_ref))
        if team['kind'] != 'team':
            raise ValueError('团队引用必须是已发布的团队模板')
        parsed = TeamConfig.model_validate(team['config'])
        mapping = {}
        instance_ids = set()
        for agent in selected:
            origin = agent.config.get('design_ref') or {}
            if origin.get('id') != team['id'] or origin.get('version') != team['version'] or origin.get('digest') != team['digest']:
                raise ValueError('所选成员不属于该团队版本的实例，请先实例化并选择对应成员')
            key = origin.get('member_key')
            instance_id = origin.get('instance_id')
            if not isinstance(instance_id, str) or not instance_id:
                raise ValueError('团队成员缺少实例标识，请重新实例化团队模板')
            instance_ids.add(instance_id)
            if key in mapping:
                raise ValueError('不能混用同一团队模板的多个实例')
            mapping[key] = agent.id
        if len(instance_ids) != 1:
            raise ValueError('不能混用同一团队模板的多个实例')
        instance_id = next(iter(instance_ids))
        if set(mapping) != {member.key for member in parsed.members}:
            raise ValueError('运行团队必须包含该模板实例的全部成员')
        for member in parsed.members:
            agent = next(a for a in selected if a.id == mapping[member.key])
            if not agent.config.get('enabled', True):
                raise ValueError('团队实例中的成员已停用')
            config = member.model_dump(exclude={'key', 'design_ref'})
            config['reports_to'] = mapping.get(member.reports_to)
            config['communication']['recipients'] = [mapping[key] for key in member.communication.recipients]
            config = AgentInput.model_validate(config).model_dump()
            config['design_ref'] = {'id': team['id'], 'version': team['version'], 'digest': team['digest'],
                                    'member_key': member.key, 'instance_id': instance_id}
            agent.config = config
        # A team asset is reusable identity/configuration material. Its historical
        # fixed graph must not silently switch a new autonomous run's execution.
        if parsed.workflow and data.execution_strategy != 'autonomous':
            if data.orchestration or data.template_id or data.workflow_ref:
                raise ValueError('团队版本已包含工作流，不能在运行时替换；请编辑并发布新团队版本')
            graph = parsed.workflow.model_dump()
            graph['participants'] = [mapping[key] for key in graph['participants']]
            if graph.get('assessor_id'):
                graph['assessor_id'] = mapping[graph['assessor_id']]
        metadata['team'] = team
        metadata['team_instance_id'] = instance_id
    if data.workflow_ref:
        workflow = resolve_published(s, _reference(data.workflow_ref))
        if workflow['kind'] != 'workflow':
            raise ValueError('工作流引用类型不正确')
        graph = WorkflowConfig.model_validate(workflow['config']).graph.model_dump()
        metadata['workflow'] = workflow
    if data.routing_ref:
        routing = resolve_published(s, _reference(data.routing_ref))
        if routing['kind'] != 'routing':
            raise ValueError('路由引用必须是已发布策略')
        routing_config = RoutingConfig.model_validate(routing['config'])
        assignments = {**routing_config.assignments, **data.routing_overrides}
        for agent in selected:
            mid = assignments.get(agent.config['role'])
            if not mid:
                raise ValueError(f'路由策略没有为 {agent.config["role"]} 指定模型')
            agent.config['model_profile_id'] = mid
            # Explicit assignment overrides the primary only; existing fallbacks must pass the same rules.
            agent.config['fallback_model_ids'] = [x for x in agent.config['fallback_model_ids'] if x != mid]
        runtime = {'max_parallel': routing_config.max_parallel,
                   'max_model_calls': routing_config.rules.max_model_calls, **runtime}
        metadata['routing'] = routing
        metadata['routing_overrides'] = deepcopy(data.routing_overrides)
    enterprise = s.get(PlatformConfig, 'enterprise_policy')
    if enterprise and enterprise.value.get('ref'):
        policy = resolve_published(s, enterprise.value['ref'])
        if policy['kind'] != 'routing':
            raise ValueError('企业规则引用类型无效')
        metadata['enterprise'] = policy
        enterprise_config = RoutingConfig.model_validate(policy['config'])
        # Use the published guard as an upper bound, including for default settings.
        for key, limit in [('max_parallel', enterprise_config.max_parallel),
                           ('max_model_calls', enterprise_config.rules.max_model_calls)]:
            if key not in runtime:
                runtime[key] = limit
    return selected, runtime, graph, metadata


def finish_design(s, snapshot, metadata, data):
    for key in ('routing', 'enterprise'):
        if key not in metadata:
            continue
        policy = RoutingConfig.model_validate(metadata[key]['config']).model_dump()
        rules = policy['rules']
        for runtime_key, limit in [('max_parallel', policy['max_parallel']), ('max_model_calls', rules['max_model_calls'])]:
            if snapshot['runtime'][runtime_key] > limit:
                raise ValueError(f'本次运行的 {runtime_key} 超过已发布策略上限 {limit}')
        for agent in snapshot['agents'].values():
            for candidate in agent['models']:
                if not rules['allow_external'] and candidate['connection']['external']:
                    raise ValueError('已发布企业/路由规则不允许外部模型')
                if rules['allowed_provider_ids'] and candidate['model']['provider_id'] not in rules['allowed_provider_ids']:
                    raise ValueError('本次模型或备用模型不在已发布策略的供应商白名单中')
                if rules['require_tools'] and not candidate['model']['supports_tools']:
                    raise ValueError('策略要求支持工具调用的模型')
    snapshot['design'] = deepcopy(metadata)
    snapshot['mode'] = 'runtime'
    # Persist the actual frozen selections and their origin, not a live router reference.
    if metadata.get('routing'):
        for agent in snapshot['agents'].values():
            agent['model_selection'] = {'source': 'task_override' if agent['role'] in data.routing_overrides else 'published_routing',
                                        'policy_id': metadata['routing']['id'], 'version': metadata['routing']['version']}


class EnterpriseSelection(Strict):
    ref: DesignReference | None = None


def register(app):
    from .api import admin, current_user

    @app.get('/api/organization')
    def get_organization(user=Depends(current_user)):
        from .organization import current_layout
        with session() as s:
            return current_layout(s)

    @app.get('/api/design/enterprise-policy')
    def get_enterprise(user=Depends(current_user)):
        with session() as s:
            row = s.get(PlatformConfig, 'enterprise_policy')
            return deepcopy(row.value) if row else {'ref': None}

    @app.put('/api/design/enterprise-policy')
    def set_enterprise(data: EnterpriseSelection, user=Depends(admin)):
        with session() as s:
            ref = _reference(data.ref)
            if ref:
                policy = resolve_published(s, ref)
                if policy['kind'] != 'routing':
                    raise ValueError('企业规则需要选择已发布路由策略')
            row = s.get(PlatformConfig, 'enterprise_policy')
            value = {'ref': ref, 'actor': user.id}
            if row:
                row.value = value
            else:
                s.add(PlatformConfig(id='enterprise_policy', value=value))
            emit(s, None, 'enterprise.policy.selected', {'actor': user.id, 'ref': ref})
            return value

    @app.post('/api/design/assets/{asset_id}/apply-office')
    def apply_office(asset_id: str, data: AssetConfirmation, user=Depends(admin)):
        from .organization import apply_layout
        with session() as s:
            return apply_layout(s, asset_id, data, user.id)
