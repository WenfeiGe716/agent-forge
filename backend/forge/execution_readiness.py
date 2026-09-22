"""Read-only execution checks. A successful preflight never creates or authorizes a run."""
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
from fastapi import Depends, HTTPException
from sqlalchemy import select

from .config import settings
from .db import Agent, ModelProfile, PlatformConfig, Project, Provider, ProviderRevision, session
from .schema import RunInput, TOOLS_BY_ROLE
from . import providers, skills


def check(code, ok, message, target, *, warning=False, **context):
    return {'code': code, 'status': 'ok' if ok else 'warning' if warning else 'error',
            'message': message, 'target': target, **context}


async def sandbox_readiness():
    result = {'state': 'unavailable', 'ready': False, 'url': settings.sandbox_url,
              'detail': '无法验证隔离执行环境；不会降级到宿主机执行。'}
    try:
        async with httpx.AsyncClient(timeout=5, trust_env=False, follow_redirects=False) as client:
            response = await client.get(settings.sandbox_url.rstrip('/') + '/readiness',
                                        headers={'X-Sandbox-Token': settings.sandbox_token})
        if response.status_code != 200:
            return {**result, 'http_status': response.status_code,
                    'detail': f'隔离环境就绪接口返回 HTTP {response.status_code}；地址可访问不代表可执行。'}
        body = response.json()
        if not isinstance(body, dict) or body.get('service') != 'sandbox-broker' or type(body.get('ready')) is not bool:
            return {**result, 'state': 'unverified', 'detail': '返回内容不是可验证的 Agent Forge 沙箱。'}
        images = body.get('images', [])
        if not isinstance(images, list): images = []
        images = [{'name': i['name'], 'available': i.get('available') is True} for i in images
                  if isinstance(i, dict) and isinstance(i.get('name'), str)]
        # Never treat a permissive broker/runtime or an unprepared image as safe readiness.
        ready = body['ready'] and body.get('runtime') == 'runsc' and body.get('network') == 'none' and any(i['available'] for i in images)
        return {**result, 'state': 'ready' if ready else 'not_ready', 'ready': ready,
                'runtime': body.get('runtime'), 'network': body.get('network'), 'images': images,
                'missing': body.get('missing', []) if isinstance(body.get('missing', []), list) else [],
                'detail': '隔离运行时和镜像已验证；尚未执行任务。' if ready else '需要 runsc、断网容器和已准备的项目镜像。'}
    except (httpx.HTTPError, ValueError, TypeError):
        return result


def inspect_agent(s, agent, project, frozen=None, frozen_project=None):
    config = agent.config
    checks = []
    context = {'agent_id': agent.id}
    add = lambda code, ok, message, target='agents', **kwargs: checks.append(check(code, ok, message, target, **context, **kwargs))
    add('agent.enabled', config.get('enabled', True), '成员已启用' if config.get('enabled', True) else '成员已停用')
    models = ([m['profile_id'] for m in frozen['models']] if frozen else
              [config.get('model_profile_id'), *config.get('fallback_model_ids', [])])
    add('model.bound', bool(models and models[0]), '已选择主模型' if models and models[0] else '尚未绑定主模型，请选择供应商下的模型配置')
    for index, model_id in enumerate(models):
        if not model_id: continue
        profile = s.get(ModelProfile, model_id)
        label = '主模型' if index == 0 else f'备用模型 {index}'
        if not profile:
            add('model.exists', False, label + '配置不存在', model_profile_id=model_id)
            continue
        model = frozen['models'][index]['model'] if frozen else profile.config
        provider = s.get(Provider, model['provider_id'])
        rev = s.get(ProviderRevision, provider.revision_id) if provider else None
        details = {'model_profile_id': model_id, 'provider_id': model['provider_id']}
        add('model.enabled', bool(model.get('enabled', True)), label + ('已启用' if model.get('enabled', True) else '已停用或从当前目录移出'), 'providers', **details)
        add('provider.enabled', bool(provider and provider.enabled and rev), label + ('的供应商已启用' if provider and provider.enabled and rev else '的供应商缺失或停用'), 'providers', **details)
        # Tool execution is owned by Agent Forge.  A provider capability probe is
        # useful evidence, but it must not be a preflight gate: many compatible
        # endpoints reject synthetic tool probes even though the real request
        # path works.  If the upstream cannot accept tools, the actual call will
        # return a concrete provider error and the run can be retried or routed.
        add('model.tools', model.get('supports_tools') is True,
            label + ('已记录工具能力（工具由本机执行器提供）' if model.get('supports_tools')
                     else '未做工具能力探测；将由实际运行请求确认，本机工具由 Agent Forge 执行'),
            'providers', warning=True, **details)
        if rev:
            try:
                params = dict(model['parameters'])
                if not frozen: params.update(config.get('model_parameters', {}))
                providers.validate_parameters(rev.config['protocol'], model['allowed_parameters'], params, rev.config.get('protocol_config'))
                providers.validate_output_limit(rev.config['protocol'], model.get('model_id', ''), model.get('context_window'), params)
                add('model.parameters', True, label + '参数合法', 'providers', **details)
            except (ValueError, KeyError) as exc:
                add('model.parameters', False, label + '参数需要修正：' + str(exc)[:400], 'providers', **details)
            if project and rev.config.get('external'):
                add('project.external_models', project.config.get('allow_external_models') is True,
                    '项目已明确授权模型外发' if project.config.get('allow_external_models') else '项目外部模型授权未开启（与“完全访问”独立；需在项目配置中明确允许发送任务和文件）', 'projects', **details)
        probe = s.get(PlatformConfig, 'probe:' + model_id)
        checked = bool(probe and provider and probe.value.get('revision_id') == provider.revision_id and probe.value.get('model_version') == profile.version)
        add('model.live_probe', checked, label + ('当前配置有已记录的连接检查' if checked else '当前配置没有匹配的连接检查；本次预检未发送推理请求'), 'providers', warning=True, **details)
    try:
        bound = frozen.get('skills', []) if frozen else skills.freeze_bindings(s, config.get('skills', []))
        for item in bound: skills.check_live(s, item)
        add('skills.approved', True, f'{len(bound)} 个绑定 Skill 的版本和审批有效；Skill 不扩张工具权限')
    except ValueError as exc:
        add('skills.approved', False, str(exc)[:500], 'skills')
    permissions = (frozen['tools'] if frozen else config.get('tool_permissions'))
    if permissions is None: permissions = TOOLS_BY_ROLE[config['role']]
    permission_valid = 'finish' in permissions and not (set(permissions) - set([*TOOLS_BY_ROLE[config['role']], 'request_decision']))
    add('agent.permissions', permission_valid, '已确认成员执行工具权限；不会因加入团队扩大权限' if permission_valid else '成员执行工具权限无效，请重新确认权限配置')
    from .member_execution_policy import resolved_policy, execution_project
    policy = None
    try:
        if frozen is not None:
            config_project = frozen_project if frozen_project is not None else (project.config if project else {})
            effective = execution_project(SimpleNamespace(snapshot={'project': config_project, 'agents': {agent.id: frozen}}), agent.id)
            policy = frozen.get('execution_policy') or {'profile': effective['permission_profile'], 'source': 'project',
                'source_id': project.id if project else None, 'source_name': config_project.get('name', '任务项目'), 'policy_version': None}
        else:
            policy = resolved_policy(s, agent.id, project)
            if policy['profile'] == 'full' and project and project.config.get('execution_mode', 'container') != 'local':
                raise ValueError('该成员的完全访问仅支持本机项目；当前项目选择了隔离容器')
        labels = {'ask': '请求审批', 'auto': '帮我批准', 'full': '完全访问', None: '继承任务项目'}
        add('agent.execution_policy', True, f'执行权限：{labels[policy["profile"]]} · 来源：{policy["source_name"]}', 'execution')
    except ValueError as exc:
        add('agent.execution_policy', False, str(exc), 'execution')
    return {'id': agent.id, 'name': config['name'], 'role': config['role'], 'job_title': config.get('job_title', ''), 'department': config.get('department', ''),
            'ready': not any(c['status'] == 'error' for c in checks), 'model_profile_id': models[0] if models else None,
            'tools': permissions, 'execution_policy': policy, 'checks': checks}


async def inspect(app, user, project_id='', agent_ids=None, snapshot=None, mode='development',
                  execution_strategy='autonomous', run_project_checks=False):
    from .api import project_access
    checks = []
    dispatcher = getattr(app.state, 'dispatch_status', 'starting')
    runner_ready = settings.orchestrator == 'local' and dispatcher.startswith('connected')
    checks.append(check('runner.available', runner_ready,
                        '本地运行器可调度' if runner_ready else '运行器尚未验证为可调度（Temporal 连接不等于 worker 就绪）', 'setup'))
    with session() as s:
        projects = [p for p in s.scalars(select(Project)) if user.role == 'admin' or p.owner_id == user.id or user.id in p.config.get('members', [])]
        project = project_access(s, project_id, user) if project_id else None
        checks.append(check('project.selected', bool(project), '已选择任务项目' if project else '请先导入并选择任务项目/资料工作区', 'projects'))
        if project:
            checks.append(check('project.active', not project.config.get('archived'), '项目可用于新运行' if not project.config.get('archived') else '项目已归档', 'projects'))
        agents = list(s.scalars(select(Agent)))
        if agent_ids is not None:
            by_id = {a.id: a for a in agents}
            agents = [by_id[a] for a in agent_ids if a in by_id]
            for aid in agent_ids:
                if aid not in by_id: checks.append(check('agent.exists', False, '所选成员不存在', 'agents', agent_id=aid))
        else:
            agents = [a for a in agents if a.config.get('enabled', True)]
        inspected = [inspect_agent(s, a, project, (snapshot or {}).get('agents', {}).get(a.id), (snapshot or {}).get('project')) for a in agents]
        from .organization import current_layout, inspect_workstations
        seating = {item['agent_id']: item for item in inspect_workstations(current_layout(s), agents)}
        for agent in inspected:
            agent['checks'].append(seating[agent['id']])
            agent['workstation'] = seating[agent['id']]['workstation']
            agent['ready'] = agent['ready'] and seating[agent['id']]['status'] == 'ok'
        checks.append(check('agents.selected', bool(agents), '成员配置已检查' if agents else '请选择运行成员', 'agents'))
        for agent in inspected: checks.extend(agent['checks'])
        strategy = (snapshot or {}).get('execution_strategy', execution_strategy)
        checks_requested = (snapshot or {}).get('run_project_checks', run_project_checks)
        read_only = strategy != 'autonomous' and ((snapshot or {}).get('orchestration') or {}).get('mode', mode) == 'discussion'
        execution_config = (snapshot or {}).get('project') or (project.config if project else {})
        project_commands = bool(execution_config.get('check_commands') and (strategy != 'autonomous' or checks_requested))
        requires_sandbox = not read_only and (any('run_command' in a['tools'] for a in inspected) or project_commands)
        image = execution_config.get('sandbox_image')
        execution_mode = execution_config.get('execution_mode', 'container')
        permission_profile = execution_config.get('permission_profile', 'ask')
        command_agents = [a for a in inspected if 'run_command' in a['tools'] or project_commands]
        member_profiles = {a['execution_policy']['profile'] for a in command_agents if a.get('execution_policy') and a['execution_policy']['profile']}
        if not member_profiles: member_profiles = {permission_profile}
        member_policies = [{'agent_id': a['id'], **a['execution_policy']} for a in inspected if a.get('execution_policy')]
        project_list = [{'id': p.id, 'name': p.config['name'], 'archived': p.config.get('archived', False)} for p in projects]
    requires_commands = requires_sandbox
    if execution_mode == 'local':
        from .local_executor import readiness as native_readiness
        native_by_profile = {profile: await native_readiness(profile) for profile in sorted(member_profiles)}
        native = {'ready': all(item['ready'] for item in native_by_profile.values()),
                  'runtime': 'mixed' if len(native_by_profile) > 1 else next(iter(native_by_profile.values()))['runtime'],
                  'detail': '；'.join(dict.fromkeys(item['detail'] for item in native_by_profile.values()))}
        permission_profile = next(iter(member_profiles)) if len(member_profiles) == 1 else 'mixed'
        sandbox = {'ready': False, 'state': 'not_required', 'detail': '本项目选择本机执行，不访问容器服务'}
        execution = {**native, 'mode': 'local', 'permission_profile': permission_profile,
                     'workspace_scope': 'task_working_copy', 'network': 'mixed' if permission_profile == 'mixed' else 'unrestricted' if permission_profile == 'full' else 'denied',
                     'member_policies': member_policies, 'environments': native_by_profile,
                     'limits': {'wall_timeout': True, 'cpu_time': True, 'file_size_bytes': 104857600, 'cpu_core_quota': False, 'memory_quota': False}}
        if requires_commands: checks.append(check('native.ready', native['ready'], native['detail'], 'execution'))
        checks.append(check('native.resource_limits', False, '本机模式限制超时、CPU时间和文件大小；硬CPU核数/内存额度需选择容器模式', 'execution', warning=True))
        if 'auto' in member_profiles: checks.append(check('execution.auto_review', True, '帮我批准成员的越界/联网申请交给独立审查；不确定或高风险仍等你确认，不直接放行', 'execution'))
        if 'full' in member_profiles: checks.append(check('execution.full_access', False, '完全访问成员由管理员明确授权：命令可读取当前账号可访问的其他文件/凭据并联网；环境变量清理不等于文件隔离', 'execution', warning=True))
        requires_sandbox = False
    else:
        sandbox = await sandbox_readiness()
        image_ready = not image or any(i['name'] == image and i['available'] for i in sandbox.get('images', []))
        execution = {'mode': 'container', 'permission_profile': permission_profile, 'runtime': 'runsc',
                     'ready': sandbox['ready'] and image_ready, 'network': 'denied', 'workspace_scope': 'task_working_copy', 'detail': sandbox['detail'],
                     'member_policies': member_policies}
        if requires_sandbox:
            checks.append(check('sandbox.ready', sandbox['ready'] and image_ready, sandbox['detail'] if image_ready else '项目指定的沙箱镜像尚未就绪', 'setup'))
    if not requires_commands:
        checks.append(check('sandbox.optional', True, '当前只读讨论或文件任务不要求执行命令；未启动任何容器', 'setup'))
    execution['workspace_path_pattern'] = str(settings.data_root / 'runs' / '{run_id}' / 'tasks' / '{task_key}')
    execution['workspace_created'] = False
    checks.append(check('model.inference_not_executed', False, '本次检查未调用推理模型，不证明上游当前可用，也不产生模型费用', 'providers', warning=True))
    return {'ready': not any(c['status'] == 'error' for c in checks), 'checked_at': datetime.now(timezone.utc).isoformat(),
            'inference_tested': False, 'requires_sandbox': requires_sandbox, 'checks': checks, 'agents': inspected,
            'projects': project_list, 'project_id': project_id or None,
            'runner': {'ready': runner_ready, 'orchestrator': settings.orchestrator, 'status': dispatcher}, 'sandbox': sandbox,
            'execution': execution, 'requires_command_environment': requires_commands,
            'execution_strategy': strategy, 'run_project_checks': bool(checks_requested)}


def register(app):
    from .api import current_user, developer, build_snapshot

    @app.get('/api/execution/readiness')
    async def readiness(project_id: str = '', mode: str = 'autonomous', run_project_checks: bool = False, user=Depends(current_user)):
        if mode not in ('discussion', 'development', 'autonomous'): raise ValueError('未知执行模式')
        return await inspect(app, user, project_id, mode=mode,
                             execution_strategy='autonomous' if mode == 'autonomous' else 'workflow', run_project_checks=run_project_checks)

    @app.post('/api/execution/preflight')
    async def preflight(data: RunInput, user=Depends(developer)):
        snapshot, issue = None, None
        try:
            with session() as s: snapshot = build_snapshot(s, data, user)
        except ValueError as exc:
            issue = str(exc)[:1000]
        except HTTPException as exc:
            if exc.status_code in (401, 403, 404): raise
            issue = str(exc.detail)[:1000]
        mode = data.orchestration.mode if data.orchestration else 'development'
        result = await inspect(app, user, data.project_id, data.agent_ids, snapshot, mode,
                               execution_strategy=data.execution_strategy, run_project_checks=data.run_project_checks)
        result['snapshot_valid'] = snapshot is not None
        result['run_created'] = False
        result['checks'].append(check('run.snapshot', snapshot is not None, issue or '正式运行快照配置验证通过；未创建任务', 'execution'))
        result['ready'] = result['ready'] and snapshot is not None
        return result
