from copy import deepcopy
from .db import Agent, ModelProfile, Provider, ProviderRevision, PlatformConfig
from .schema import resolve_runtime, ROLES, TOOLS_BY_ROLE
from .providers import validate_parameters
from . import skills

def freeze_model(s, profile_id: str, overrides: dict, allow_external: bool) -> dict:
    profile = s.get(ModelProfile, profile_id)
    if not profile: raise ValueError('模型配置不存在')
    model = deepcopy(profile.config)
    if not model.get('enabled', True): raise ValueError('模型已停用，不能创建新运行')
    provider = s.get(Provider, model['provider_id'])
    if not provider or not provider.enabled: raise ValueError('供应商不存在或已停用')
    revision = s.get(ProviderRevision, provider.revision_id)
    if not revision: raise ValueError('供应商连接版本不存在，请重新保存连接')
    connection = deepcopy(revision.config)
    if connection['external'] and not allow_external: raise ValueError('项目未授权向外部模型发送代码')
    if not model['supports_tools']: raise ValueError('Agent运行需要支持工具调用的模型')
    model['parameters'].update(overrides)
    validate_parameters(connection['protocol'], model['allowed_parameters'], model['parameters'], connection.get('protocol_config'))
    return {'profile_id': profile_id, 'profile_version': profile.version, 'credential_revision': revision.id,
            'provider_name': provider.name, 'connection': connection, 'model': model}

def autonomous_leader(agents, explicit=None):
    """Resolve only an explicitly selected or configured leader inside this pool."""
    configs = {agent.id: agent.config for agent in agents}
    if not configs: raise ValueError('请选择至少一位授权成员')
    if explicit:
        if explicit not in configs: raise ValueError('任务负责人必须属于本次授权团队')
        return explicit
    if len(configs) == 1: return next(iter(configs))
    roots = [aid for aid, config in configs.items() if config.get('reports_to') not in configs]
    if len(roots) == 1:
        root = roots[0]
        for aid in configs:
            seen = set()
            while aid != root and aid in configs and aid not in seen:
                seen.add(aid)
                aid = configs[aid].get('reports_to')
            if aid != root: break
        else: return root
    raise ValueError('请明确选择本次团队负责人；未找到授权团队内已配置的唯一汇报负责人')


def freeze_run(s, project, agents: list[Agent], overrides: dict, parent: str | None, orchestration: dict | None = None,
               *, execution_strategy='workflow', entry_agent_id=None, run_project_checks=False) -> dict:
    if project.config.get('archived'): raise ValueError('项目已归档，不能创建新运行')
    platform = s.get(PlatformConfig, 'runtime')
    roles = s.get(PlatformConfig, 'roles')
    defaults = platform.value if platform else {}
    role_defaults = roles.value if roles else ROLES
    run_runtime, run_sources = resolve_runtime([('platform', defaults), ('project', project.config['runtime']), ('run', overrides)])
    frozen = {}
    selected_configs = {a.id: a.config for a in agents}
    if execution_strategy not in ('autonomous', 'workflow'): raise ValueError('未知执行策略')
    autonomous = execution_strategy == 'autonomous'
    if autonomous and orchestration: raise ValueError('团队自治不接受固定编排流程')
    leader = autonomous_leader(agents, entry_agent_id) if autonomous else None
    from .member_execution_policy import freeze_policies
    execution_policies = freeze_policies(s, project, agents)
    for agent in agents:
        c = deepcopy(agent.config)
        if not c.get('enabled', True): raise ValueError(f'{c["name"]} 已停用')
        if not c['model_profile_id']: raise ValueError(f'{c["name"]} 尚未绑定模型')
        permissions = c.get('tool_permissions')
        if permissions is not None and ('finish' not in permissions or set(permissions) - set([*TOOLS_BY_ROLE[c['role']], 'request_decision'])):
            raise ValueError(f'{c["name"]} 的执行工具权限无效，请重新确认成员权限配置')
        rt, src = resolve_runtime([('platform', defaults), ('project', project.config['runtime']), ('role', role_defaults.get(c['role'], {})), ('agent', c['runtime']), ('run', overrides)])
        models = []
        # Every configured candidate must be valid; never silently drop a fallback.
        for index, mid in enumerate([c['model_profile_id'], *c['fallback_model_ids']]):
            try: models.append(freeze_model(s, mid, c['model_parameters'], project.config['allow_external_models']))
            except ValueError as exc:
                kind = '主模型' if index == 0 else f'备用模型 {index}'
                raise ValueError(f'{c["name"]} 的{kind}（{mid}）：{exc}') from exc
        chain=[]; ancestor=c.get('reports_to')
        while ancestor and ancestor not in chain:
            chain.append(ancestor)
            if ancestor in selected_configs:
                ancestor=selected_configs[ancestor].get('reports_to')
            else:
                ancestor_agent=s.get(Agent,ancestor)
                ancestor=ancestor_agent.config.get('reports_to') if ancestor_agent else None
        bound_skills=skills.freeze_bindings(s,c.get('skills',[]))
        if c.get('role_template_ref'):
            from .role_templates import validate_reference
            validate_reference(s, c['role_template_ref'])
        frozen[agent.id] = {'id': agent.id, 'name': c['name'], 'role': c['role'], 'version': agent.version,
                            'instructions': c['instructions'], 'job_title': c.get('job_title', ''), 'runtime': rt, 'runtime_sources': src,
                            'tools': c.get('tool_permissions') if c.get('tool_permissions') is not None else [*TOOLS_BY_ROLE[c['role']], 'request_decision'], 'models': models,
                            'office_position':deepcopy(c.get('office_position')),'design_ref':deepcopy(c.get('design_ref')),
                            'role_template_ref':deepcopy(c.get('role_template_ref')),
                            'department':c.get('department',''),'communication':c.get('communication',{'scope':'run'}),
                            'reporting_chain':chain,'skills':bound_skills,
                            'execution_policy':execution_policies[agent.id]}
    planners = [a for a in frozen.values() if a['role'] == 'planner']
    developers = [a for a in frozen.values() if a['role'] in ('developer', 'tester')]
    reviewers = [a for a in frozen.values() if a['role'] == 'reviewer']
    discussion_only = (orchestration or {}).get('mode') == 'discussion'
    if not autonomous and not discussion_only and (len(planners) != 1 or not developers or len(reviewers) != 1):
        raise ValueError('请选择恰好一名负责人、一名审查Agent，以及至少一名开发或测试Agent')
    project_config = deepcopy(project.config)
    project_config.setdefault('execution_mode', 'container')
    project_config.setdefault('permission_profile', 'ask')
    return {'schema_version': 1, 'project': project_config, 'base_commit': project.base_commit,
            'agents': frozen, 'runtime': run_runtime, 'runtime_sources': run_sources,
            'execution_strategy': execution_strategy, 'run_project_checks': bool(run_project_checks),
            'entry_agent_id': leader or (planners[0]['id'] if planners else agents[0].id), 'collaboration_flow': False,
            'planner_id': leader or (planners[0]['id'] if planners else agents[0].id),
            'reviewer_id': leader or (reviewers[0]['id'] if reviewers else agents[-1].id),
            'parent_run_id': parent, 'orchestration': deepcopy(orchestration)}
