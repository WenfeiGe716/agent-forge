import asyncio
import base64
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from fastapi import FastAPI, Depends, HTTPException, Request, Response, Header, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from sqlalchemy import select, func, update
from .config import settings
from .db import (session, init_db, User, LoginSession, Provider, ProviderRevision, ModelProfile, Agent, Project, Run, Task, Event, Outbox, PlatformConfig, OrchestrationTemplate, emit, uid, now)
from .schema import (Login, UserCreate, ProviderInput, ModelInput, AgentInput, ProjectInput, RunInput, OrchestrationInput, OrchestrationTemplateInput, Decision, Control, DEFAULT_RUNTIME, ROLES, validate_runtime_patch, validate_agent_runtime_patch)
from .security import password_hash, password_matches, token_hash, encrypt, validate_url
from .providers import validate_parameters
from .model_compatibility import model_errors, validate_provider_change
from .snapshots import freeze_run
from .workspace import import_project, run_root
from . import desktop
from .runtime_bridge import runtime_binary_status
from .runtime_poc_api import register as register_runtime_poc

TERMINAL = {'completed', 'cancelled', 'needs_attention'}
ACTIVE = {'intake','awaiting_decision','discussing','planning','running','verifying','reviewing','reworking','paused','awaiting_approval','awaiting_acceptance'}
login_attempts = defaultdict(deque)


def bootstrap():
    init_db()
    settings.data_root.mkdir(parents=True, exist_ok=True)
    settings.import_root.mkdir(parents=True, exist_ok=True)
    recovery_pid = os.getpid()
    recover_invocations = getattr(bootstrap, '_model_hub_recovered_pid', None) != recovery_pid
    with session() as s:
        if recover_invocations:
            from .model_hub import interrupt_incomplete_invocations
            interrupt_incomplete_invocations(s)
        if not s.scalar(select(User).limit(1)):
            if len(settings.admin_password) < 12:
                raise RuntimeError('首次启动需要至少12字符的 ADMIN_PASSWORD；请先运行 scripts/init.sh')
            s.add(User(username='admin', password_hash=password_hash(settings.admin_password), role='admin'))
        if not s.get(PlatformConfig, 'runtime'): s.add(PlatformConfig(id='runtime', value=DEFAULT_RUNTIME))
        if not s.get(PlatformConfig, 'roles'): s.add(PlatformConfig(id='roles', value=ROLES))
        if not s.get(PlatformConfig, 'office_visual'):
            from .office import OFFICE_DEFAULTS
            s.add(PlatformConfig(id='office_visual', value={**OFFICE_DEFAULTS,'epoch':now(),'revision':1}))
        from .skill_store import seed_sources
        seed_sources(s)
        if not s.scalar(select(Agent).limit(1)):
            for name, role, instructions in [('Atlas', 'planner', '制定可验证的计划，减少并行文件冲突。'), ('Nova', 'developer', '先检查项目，再做最小必要修改。'), ('Echo', 'tester', '为变更补充可重复运行的测试。'), ('Sage', 'reviewer', '根据真实代码和测试证据审查。')]:
                s.add(Agent(config=AgentInput(name=name, role=role, instructions=instructions).model_dump()))
        s.flush()
        from .organization_presets import bootstrap_default_layout
        bootstrap_default_layout(s)
    # A second app lifespan in the same process must not interrupt live requests.
    # Record recovery only after commit so a failed startup can safely retry.
    if recover_invocations:
        bootstrap._model_hub_recovered_pid = recovery_pid

async def dispatch_forever(app):
    client = None
    while True:
        try:
            from temporalio.client import Client
            from temporalio.exceptions import WorkflowAlreadyStartedError
            if client is None:
                client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
            app.state.dispatch_status = 'connected'
            with session() as s:
                batch = list(s.scalars(select(Outbox).where(Outbox.sent == False).order_by(Outbox.created_at, Outbox.id).limit(50)))
            blocked = set()
            for message in batch:
                if message.run_id in blocked: continue
                try:
                    if message.action == 'start':
                        try:
                            await client.start_workflow('DevelopmentWorkflow', message.run_id, id='forge-' + message.run_id, task_queue=settings.task_queue)
                        except WorkflowAlreadyStartedError:
                            pass
                    else:
                        handle = client.get_workflow_handle('forge-' + message.run_id)
                        await handle.signal(message.action, {**message.payload, 'command_id': message.id})
                    with session() as s:
                        record = s.get(Outbox, message.id)
                        record.sent, record.error = True, ''
                except Exception as exc:
                    blocked.add(message.run_id)
                    with session() as s:
                        record = s.get(Outbox, message.id)
                        record.attempts += 1
                        record.error = type(exc).__name__
                    # Pending intent stays durable; deduplication belongs to workflow IDs/command IDs.
                    app.state.dispatch_status = 'pending: ' + type(exc).__name__
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            app.state.dispatch_status = 'unavailable: ' + type(exc).__name__
            client = None
        await asyncio.sleep(2)

@asynccontextmanager
async def lifespan(app):
    bootstrap()
    app.state.dispatch_status = 'starting'
    dispatcher = None
    if os.getenv('DISABLE_DISPATCH', '').lower() != 'true':
        if settings.orchestrator == 'local':
            from .local_runtime import LocalScheduler
            dispatcher = asyncio.create_task(LocalScheduler().serve(app))
        elif settings.orchestrator == 'temporal':
            dispatcher = asyncio.create_task(dispatch_forever(app))
        else:
            raise RuntimeError('ORCHESTRATOR 仅支持 local 或 temporal')
    else:
        app.state.dispatch_status = 'disabled (test only)'
    yield
    if dispatcher:
        dispatcher.cancel()
        try: await dispatcher
        except asyncio.CancelledError: pass

app = FastAPI(title='Agent Forge', version='0.14.0', lifespan=lifespan, docs_url='/api/docs', openapi_url='/api/openapi.json', redoc_url=None)

@app.middleware('http')
async def request_guard(request: Request, call_next):
    denial = desktop.request_denial(request)
    if denial: return JSONResponse({'detail': denial}, status_code=403)
    origin = request.headers.get('origin')
    if request.method not in ('GET','HEAD','OPTIONS'):
        if request.headers.get('x-forge-request') != '1':
            return JSONResponse({'detail': '缺少同源请求标识'}, status_code=403)
        if origin and not desktop.enabled() and origin not in settings.origins:
            return JSONResponse({'detail': '请求来源未授权'}, status_code=403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = 'same-origin'
    return response

desktop.register_routes(app, TERMINAL)

@app.exception_handler(ValueError)
async def value_error(request, exc):
    return JSONResponse({'detail': str(exc)}, status_code=422)

def current_user(request: Request) -> User:
    cookie = request.cookies.get('forge_session', '')
    with session() as s:
        record = s.get(LoginSession, token_hash(cookie))
        if not record or record.expires_at <= now(): raise HTTPException(401, '请先登录')
        user = s.get(User, record.user_id)
        if not user or not user.enabled: raise HTTPException(401, '账号已停用')
        return user

def admin(user=Depends(current_user)):
    if user.role != 'admin': raise HTTPException(403, '需要管理员权限')
    return user

def developer(user=Depends(current_user)):
    if user.role not in ('admin', 'developer'): raise HTTPException(403, '只读账号不可修改')
    return user

def project_access(s, project_id, user):
    project = s.get(Project, project_id)
    if not project or (user.role != 'admin' and user.id != project.owner_id and user.id not in project.config.get('members', [])):
        raise HTTPException(404, '项目不存在或无权访问')
    return project

def run_access(s, run_id, user):
    run = s.get(Run, run_id)
    if not run: raise HTTPException(404, '运行不存在')
    project_access(s, run.project_id, user)
    return run

def user_out(u): return {'id':u.id, 'username':u.username, 'role':u.role, 'enabled':u.enabled}
def config_out(o): return {'id':o.id, 'version':o.version, **o.config}
def project_out(p): return {'id':p.id, 'owner_id':p.owner_id, 'base_commit':p.base_commit, 'created_at':p.created_at, **p.config}
def event_out(e): return {'id':e.id, 'run_id':e.run_id, 'kind':e.kind, 'data':e.data, 'created_at':e.created_at}
def run_out(r, detailed=False):
    strategy = (r.snapshot or {}).get('execution_strategy', 'workflow')
    output = {'id':r.id, 'project_id':r.project_id, 'title':r.title, 'request':r.request, 'status':r.status, 'calls':r.calls, 'input_tokens':r.input_tokens, 'output_tokens':r.output_tokens,
              'error':r.error, 'created_at':r.created_at, 'updated_at':r.updated_at,
              'agent_ids':list((r.snapshot or {}).get('agents', {})),
              'mode':'autonomous' if strategy == 'autonomous' else ((r.snapshot or {}).get('orchestration') or {}).get('mode', 'development'),
              'execution_strategy':strategy, 'execution_engine':(r.snapshot or {}).get('execution_engine', 'legacy'),
              'run_project_checks':(r.snapshot or {}).get('run_project_checks', strategy == 'workflow'),
              **{key:(r.snapshot or {}).get(key) for key in ('entry_agent_id', 'planner_id', 'reviewer_id')}}
    if detailed:
        output.update({'snapshot':r.snapshot, 'plan':r.plan, 'plan_digest':r.plan_digest, 'artifact_digest':r.artifact_digest, 'report':r.report})
    return output

@app.get('/api/health')
def health():
    return {'ok':True, 'version':'0.14.0',
            **({'desktop_instance': desktop.instance_fingerprint()} if desktop.enabled() else {})}

@app.get('/api/runtime/status')
def runtime_status(user=Depends(current_user)):
    """Show the project-owned execution runtimes without starting them.

    This is intentionally read-only. It does not inspect or mutate ~/.codex,
    and it does not claim that a provider/model is executable until a real
    task performs its own capability probe.
    """
    return runtime_binary_status(codex=settings.codex_binary, opencodex=settings.opencodex_binary) | {
        'runtime_data_root': str(settings.runtime_data_root),
        'configured_transport': 'stdio',
        'integration_stage': 'codex-first' if settings.codex_runtime_enabled else 'foundation',
        'task_execution_enabled': settings.codex_runtime_enabled,
        'default_scheduler_enabled': settings.codex_default_scheduler_enabled,
        'team_mcp_enabled': settings.codex_team_mcp_enabled,
    }

@app.post('/api/auth/login')
def login(data: Login, request: Request, response: Response):
    ident = (request.client.host if request.client else 'unknown', data.username)
    queue = login_attempts[ident]
    moment = time.monotonic()
    while queue and queue[0] < moment - 300: queue.popleft()
    if len(queue) >= 10: raise HTTPException(429, '尝试次数过多，请稍后再试')
    queue.append(moment)
    with session() as s:
        user = s.scalar(select(User).where(User.username == data.username))
        if not user or not user.enabled or not password_matches(data.password, user.password_hash): raise HTTPException(401, '用户名或密码错误')
        token = secrets.token_urlsafe(40)
        expires = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
        s.add(LoginSession(token_hash=token_hash(token), user_id=user.id, expires_at=expires))
        emit(s, None, 'auth.login', {'actor':user.id})
        response.set_cookie('forge_session', token, httponly=True, samesite='strict', secure=settings.secure_cookie, max_age=43200, path='/')
        queue.clear()
        return user_out(user)

@app.post('/api/auth/logout')
def logout(request: Request, response: Response, user=Depends(current_user)):
    with session() as s:
        record = s.get(LoginSession, token_hash(request.cookies.get('forge_session','')))
        if record: s.delete(record)
    response.delete_cookie('forge_session', path='/')
    return {'ok':True}

@app.get('/api/auth/me')
def me(user=Depends(current_user)): return user_out(user)

@app.get('/api/users')
def users(user=Depends(admin)):
    with session() as s: return [user_out(u) for u in s.scalars(select(User))]

@app.post('/api/users', status_code=201)
def create_user(data: UserCreate, user=Depends(admin)):
    with session() as s:
        if s.scalar(select(User).where(User.username == data.username)): raise HTTPException(409, '用户名已存在')
        u = User(username=data.username, password_hash=password_hash(data.password), role=data.role)
        s.add(u); s.flush()
        emit(s, None, 'user.created', {'actor':user.id, 'user_id':u.id, 'role':u.role})
        return user_out(u)

@app.post('/api/users/{user_id}/disable')
def disable_user(user_id: str, user=Depends(admin)):
    if user.id == user_id: raise HTTPException(409, '不能停用当前账号')
    with session() as s:
        target = s.get(User, user_id)
        if not target: raise HTTPException(404, '用户不存在')
        target.enabled = False
        emit(s, None, 'user.disabled', {'actor':user.id, 'user_id':user_id})
    return {'ok':True}

@app.get('/api/settings')
def get_settings(user=Depends(current_user)):
    with session() as s:
        return {'runtime':s.get(PlatformConfig,'runtime').value, 'roles':s.get(PlatformConfig,'roles').value,
                'validation_mode':settings.validation_mode, 'orchestrator':settings.orchestrator, 'installation':{'max_active_runs':settings.max_active_runs,'max_active_activities':settings.max_active_activities, 'max_project_mb':settings.max_project_mb},
                'dispatcher':getattr(app.state,'dispatch_status','unknown')}

@app.put('/api/settings')
def save_settings(data: dict, user=Depends(admin)):
    if set(data) - {'runtime','roles'}: raise ValueError('未知配置分组')
    runtime = data.get('runtime', {})
    validate_runtime_patch(runtime)
    roles = data.get('roles', ROLES)
    if set(roles) - set(ROLES): raise ValueError('未知角色')
    for patch in roles.values(): validate_agent_runtime_patch(patch)
    with session() as s:
        s.get(PlatformConfig,'runtime').value = DEFAULT_RUNTIME | runtime
        s.get(PlatformConfig,'roles').value = roles
        emit(s, None, 'settings.updated', {'actor':user.id, 'groups':list(data)})
    return {'ok':True}

@app.get('/api/office/state')
def office_state(after: int | None = Query(None, ge=0), limit: int = Query(200, ge=1, le=500), user=Depends(current_user)):
    from .office import office_snapshot
    with session() as s:
        return office_snapshot(s, user, after, limit)

@app.get('/api/office/settings')
def office_settings(user=Depends(current_user)):
    from .office import settings_out
    with session() as s: return settings_out(s)

@app.put('/api/office/settings')
def update_office_settings(data: dict, user=Depends(admin)):
    from .office import save_settings
    with session() as s: return save_settings(s, data, user.id)

@app.get('/api/providers')
def get_providers(user=Depends(current_user)):
    with session() as s:
        output=[]
        for p in s.scalars(select(Provider)):
            rev=s.get(ProviderRevision,p.revision_id)
            output.append({'id':p.id, 'name':p.name, 'enabled':p.enabled, 'revision_id':p.revision_id, 'has_key':bool(rev.encrypted_key), **rev.config})
        return output

def save_provider(data, user, existing_id=None):
    url = validate_url(data.base_url)
    from .protocols import validate_connection
    protocol_config = validate_connection(data.protocol, url, data.external, data.protocol_config)
    with session() as s:
        if existing_id:
            # Serialize against model/catalog edits and Agent parameter writes;
            # read the affected configurations only after the lock is held.
            s.execute(update(Provider).where(Provider.id == existing_id).values(name=Provider.name))
            s.expire_all()
        provider = s.get(Provider, existing_id) if existing_id else Provider(id=uid(), name=data.name, revision_id='')
        if not provider: raise HTTPException(404,'供应商不存在')
        old = s.get(ProviderRevision, provider.revision_id) if provider.revision_id else None
        connection = {'protocol':data.protocol,'base_url':url,'external':data.external,'protocol_config':protocol_config}
        if old and (old.config['protocol'] != data.protocol or old.config.get('protocol_config', {}) != protocol_config):
            validate_provider_change(s, provider.id, connection)
        key = encrypt(data.api_key) if data.api_key is not None else (old.encrypted_key if old else '')
        rev = ProviderRevision(id=uid(), provider_id=provider.id, config=connection, encrypted_key=key)
        s.add(rev)
        provider.name, provider.enabled, provider.revision_id = data.name, data.enabled, rev.id
        s.add(provider)
        emit(s, None, 'provider.updated', {'actor':user.id,'provider_id':provider.id,'revision_id':rev.id})
        return {'id':provider.id,'revision_id':rev.id}

@app.post('/api/providers', status_code=201)
def create_provider(data: ProviderInput, user=Depends(admin)): return save_provider(data,user)
@app.put('/api/providers/{provider_id}')
def update_provider(provider_id: str, data: ProviderInput, user=Depends(admin)): return save_provider(data,user,provider_id)

@app.get('/api/orchestrations')
def orchestration_templates(user=Depends(current_user)):
    with session() as s: return [config_out(t) for t in s.scalars(select(OrchestrationTemplate).order_by(OrchestrationTemplate.id))]

def save_template(data,user,existing_id=None):
    with session() as s:
        for aid in data.graph.participants:
            agent=s.get(Agent,aid)
            if not agent or not agent.config.get('enabled',True): raise ValueError('编排包含不存在或停用的成员')
        row=s.get(OrchestrationTemplate,existing_id) if existing_id else OrchestrationTemplate(config={})
        if not row: raise HTTPException(404,'编排模板不存在')
        if existing_id and data.expected_version != row.version: raise HTTPException(409,'模板版本冲突，请刷新后重试')
        config=data.model_dump(exclude={'expected_version'})
        if existing_id:
            expected=row.version
            result=s.execute(update(OrchestrationTemplate).where(OrchestrationTemplate.id==existing_id,OrchestrationTemplate.version==expected).values(config=config,version=expected+1).execution_options(synchronize_session=False))
            if result.rowcount!=1: raise HTTPException(409,'模板版本冲突，请刷新后重试')
            s.expire(row); s.refresh(row)
        else:
            row.config=config; s.add(row)
        s.flush()
        emit(s,None,'orchestration.updated',{'actor':user.id,'template_id':row.id,'version':row.version})
        return config_out(row)

@app.post('/api/orchestrations',status_code=201)
def create_template(data:OrchestrationTemplateInput,user=Depends(admin)): return save_template(data,user)

@app.put('/api/orchestrations/{template_id}')
def update_template(template_id:str,data:OrchestrationTemplateInput,user=Depends(admin)): return save_template(data,user,template_id)

@app.get('/api/models')
def models(user=Depends(current_user)):
    with session() as s: return [config_out(p) for p in s.scalars(select(ModelProfile))]

def save_model(data, user, existing_id=None):
    with session() as s:
        # Catalog replacements and ordinary edits share provider write locks.
        # Lock both sides of a move in stable order, then reload after waiting;
        # an edit must not resurrect a configuration retired in the meantime.
        prior=s.get(ModelProfile,existing_id) if existing_id else None
        if existing_id and not prior: raise HTTPException(404,'模型不存在')
        prior_provider=prior.config['provider_id'] if prior else None
        for pid in sorted({data.provider_id} | ({prior_provider} if prior_provider else set())):
            s.execute(update(Provider).where(Provider.id==pid).values(name=Provider.name))
        s.expire_all()
        if existing_id:
            prior=s.get(ModelProfile,existing_id)
            if not prior or prior.config['provider_id'] != prior_provider:
                raise HTTPException(409,'模型归属已变更，请刷新后重新编辑')
        provider=s.get(Provider,data.provider_id)
        if not provider: raise ValueError('供应商不存在')
        connection=s.get(ProviderRevision,provider.revision_id).config
        errors = model_errors(data.model_dump(), connection, existing_id)
        if errors: raise ValueError('模型配置未保存：\n' + '\n'.join(errors))
        profile=s.get(ModelProfile,existing_id) if existing_id else ModelProfile(config=data.model_dump())
        if not profile: raise HTTPException(404,'模型不存在')
        if existing_id and data.enabled:
            from .provider_catalog import CatalogModelState
            state=s.get(CatalogModelState,existing_id)
            if state and state.provider_id == data.provider_id and state.retired:
                raise HTTPException(409,'该配置已从当前供应商列表移出；请重新从上游拉取并确认恢复后再启用')
        from .model_hub import record_model
        if existing_id: record_model(s,profile,user.id)
        profile.config=data.model_dump()
        if existing_id: profile.version += 1
        s.add(profile); s.flush()
        record_model(s,profile,user.id)
        emit(s,None,'model.updated',{'actor':user.id,'model_id':profile.id,'version':profile.version})
        return config_out(profile)
@app.post('/api/models', status_code=201)
def create_model(data: ModelInput, user=Depends(admin)): return save_model(data,user)
@app.put('/api/models/{model_id}')
def update_model(model_id: str,data: ModelInput,user=Depends(admin)): return save_model(data,user,model_id)

@app.get('/api/agents')
def agents(user=Depends(current_user)):
    with session() as s: return [config_out(a) for a in s.scalars(select(Agent))]

def save_agent(data,user,existing_id=None):
    with session() as s:
        model_ids = ([data.model_profile_id] if data.model_profile_id else []) + data.fallback_model_ids
        model_providers = {}
        for mid in model_ids:
            model=s.get(ModelProfile,mid)
            if not model: raise ValueError('绑定的模型不存在')
            model_providers[mid] = model.config['provider_id']
        for pid in sorted(set(model_providers.values())):
            s.execute(update(Provider).where(Provider.id == pid).values(name=Provider.name))
        s.expire_all()
        for mid in model_ids:
            model=s.get(ModelProfile,mid)
            if not model or model.config['provider_id'] != model_providers[mid]:
                raise HTTPException(409, '绑定模型归属已改变，请刷新后重新编辑成员')
            provider=s.get(Provider,model.config['provider_id'])
            rev=s.get(ProviderRevision,provider.revision_id)
            validate_parameters(rev.config['protocol'],model.config['allowed_parameters'],model.config['parameters'] | data.model_parameters,rev.config.get('protocol_config'))
        a=s.get(Agent,existing_id) if existing_id else Agent(config=data.model_dump())
        if not a: raise HTTPException(404,'Agent不存在')
        if data.design_ref and (not existing_id or data.design_ref != a.config.get('design_ref')):
            raise ValueError('团队模板来源由实例化操作生成，不能手工伪造')
        # Reporting relationships drive reception posture, never tool permissions.
        ancestor=data.reports_to; seen={a.id} if a.id else set()
        while ancestor:
            if ancestor in seen: raise ValueError('直属领导关系存在循环或指向自己')
            seen.add(ancestor)
            leader=s.get(Agent,ancestor)
            if not leader: raise ValueError('直属领导不存在')
            ancestor=leader.config.get('reports_to')
        from .skills import freeze_bindings
        freeze_bindings(s,[x.model_dump() for x in data.skills])
        for aid in data.communication.recipients:
            if not s.get(Agent,aid):raise ValueError('通信白名单包含不存在的成员')
        saved=data.model_dump()
        if existing_id and a.config.get('design_ref'): saved['design_ref']=a.config['design_ref']
        if existing_id:
            for field in ('office_position','tool_permissions','role_template_ref'):
                if field not in data.model_fields_set: saved[field]=a.config.get(field)
        saved=AgentInput.model_validate(saved).model_dump()
        if saved.get('role_template_ref'):
            from .role_templates import validate_reference
            validate_reference(s, saved['role_template_ref'])
        a.config=saved
        if existing_id: a.version+=1
        s.add(a);s.flush()
        emit(s,None,'agent.updated',{'actor':user.id,'agent_id':a.id,'version':a.version})
        return config_out(a)
@app.post('/api/agents', status_code=201)
def create_agent(data: AgentInput,user=Depends(admin)):return save_agent(data,user)
@app.put('/api/agents/{agent_id}')
def update_agent(agent_id: str,data: AgentInput,user=Depends(admin)):return save_agent(data,user,agent_id)

@app.get('/api/projects')
def projects(user=Depends(current_user)):
    with session() as s:
        return [project_out(p) for p in s.scalars(select(Project).order_by(Project.created_at.desc())) if user.role=='admin' or p.owner_id==user.id or user.id in p.config['members']]

@app.post('/api/projects', status_code=201)
def create_project(data: ProjectInput,user=Depends(admin)):
    project_id=uid()
    config=data.model_dump()
    if data.blank:
        if data.desktop_directory_token:
            raise ValueError('空白项目不能同时携带目录授权')
        from .workspace import create_empty_project_snapshot
        base = create_empty_project_snapshot(project_id)
    elif data.desktop_directory_token:
        if Path(data.import_path).is_absolute() or '..' in Path(data.import_path).parts or '\\' in data.import_path or '\x00' in data.import_path:
            raise ValueError('目录授权导入不接受绝对输入路径')
        base,source=desktop.import_authorized_directory(data.desktop_directory_token,project_id)
        config['desktop_source_path']=source
    else:
        base=import_project(data.import_path,project_id)
    with session() as s:
        p=Project(id=project_id,owner_id=user.id,config=config,base_commit=base)
        s.add(p);s.flush()
        emit(s,None,'project.imported',{'actor':user.id,'project_id':p.id,'base_commit':base,
             **({'desktop_source_path':config['desktop_source_path']} if 'desktop_source_path' in config else {})})
        return project_out(p)

@app.put('/api/projects/{project_id}')
def update_project(project_id: str,data: ProjectInput,user=Depends(admin)):
    if data.desktop_directory_token: raise ValueError('目录授权仅用于导入新项目')
    with session() as s:
        p=project_access(s,project_id,user)
        if data.import_path!=p.config['import_path']: raise ValueError('更换源目录请导入为新项目，不修改已有基线')
        config=data.model_dump()
        if 'desktop_source_path' in p.config: config['desktop_source_path']=p.config['desktop_source_path']
        # A blank project is backed by an intentionally empty Git workspace;
        # ordinary metadata edits must not turn it back into a source import.
        if p.config.get('blank'): config['blank'] = True
        # Old clients do not know the new execution fields. An ordinary metadata
        # edit must never silently move an existing project onto the host.
        for field,legacy in (('execution_mode','container'),('permission_profile','ask')):
            if field not in data.model_fields_set: config[field]=p.config.get(field,legacy)
        # Preserve an existing project's authorization when older clients omit
        # this field. Legacy project configs without the key remain disabled.
        if 'allow_external_models' not in data.model_fields_set:
            config['allow_external_models'] = p.config.get('allow_external_models', False)
        if config['permission_profile']=='full' and (config['execution_mode']!='local' or not data.full_access_confirmed):
            raise ValueError('完全访问需要明确选择本机执行并再次确认风险；不会继承隐含同意')
        previous={key:p.config.get(key,default) for key,default in (('execution_mode','container'),('permission_profile','ask'))}
        p.config=config
        emit(s,None,'project.updated',{'actor':user.id,'project_id':p.id,'execution_before':previous,
             'execution_after':{key:config[key] for key in previous},'full_access_confirmed':config['permission_profile']=='full'})
        return project_out(p)

def graph_ids(snap):return (snap.get('orchestration') or {}).get('participants',list(snap['agents']))

def build_snapshot(s,data,user):
    p=project_access(s,data.project_id,user)
    selected=[s.get(Agent,id) for id in data.agent_ids]
    if any(a is None for a in selected):raise ValueError('选择的Agent不存在')
    organization_agents = selected
    from .design_runtime import prepare_design, finish_design
    selected,design_runtime,design_graph,design_meta=prepare_design(s,data,selected)
    if data.parent_run_id:
        parent=run_access(s,data.parent_run_id,user)
        if parent.project_id!=p.id: raise ValueError('父运行必须属于同一项目')
    autonomous = data.execution_strategy == 'autonomous'
    if autonomous and (data.orchestration or data.template_id or data.workflow_ref or design_graph):
        raise ValueError('团队自治不接受固定工作流、编排或流程模板，请移除固定流程配置')
    graph=design_graph or (data.orchestration.model_dump() if data.orchestration else None)
    if not autonomous and data.collaboration_flow and not graph and not data.template_id:
        graph=OrchestrationInput(mode='development',participants=data.agent_ids).model_dump()
    template=None
    if data.template_id:
        template=s.get(OrchestrationTemplate,data.template_id)
        if not template or not template.config.get('enabled',True): raise ValueError('编排模板不存在或已停用')
        if template.version != data.template_version: raise HTTPException(409,'编排模板已更新，请刷新后重新确认')
        graph=OrchestrationInput.model_validate(template.config['graph']).model_dump()
    if graph:
        if not set(graph['participants']) <= set(data.agent_ids): raise ValueError('讨论参与者必须全部包含在运行团队中')
        if graph['mode']=='discussion' and set(graph['participants']) != set(data.agent_ids):
            raise ValueError('纯讨论运行的团队应与讨论参与者一致，避免无关成员被占用')
    entry=data.entry_agent_id
    if autonomous:
        from .snapshots import autonomous_leader
        from .engagement import resolve_target
        entry = autonomous_leader(selected, entry)
        # Preserve explicit @-recipient boundaries without the legacy global
        # planner fallback: the pool leader has already been resolved above.
        resolved = resolve_target(s, data.request, entry)
        if not set(resolved['recipients']) <= set(data.agent_ids):
            raise ValueError('指令对象不在本次授权团队')
    elif data.collaboration_flow:
        from .engagement import resolve_target,validate_meeting
        resolved=resolve_target(s,data.request,entry);entry=resolved['entry_agent_id']
        if entry not in data.agent_ids or not set(resolved['recipients'])<=set(data.agent_ids):raise ValueError('指令对象不在本次授权团队')
        if not graph or entry not in graph['participants']:raise ValueError('定向协作需要包含负责人的候选参会团队')
        owner=next(a for a in selected if a.id==entry)
        if graph['mode']=='development' and owner.config['role']!='planner':raise ValueError('开发流程请定向给负责人；其他成员可主持只读协作')
    elif entry and entry not in data.agent_ids:raise ValueError('指令接收者不属于运行团队')
    snap=freeze_run(s,p,selected,design_runtime,data.parent_run_id,graph,
                    execution_strategy=data.execution_strategy,entry_agent_id=entry,run_project_checks=data.run_project_checks)
    finish_design(s,snap,design_meta,data)
    snap.update({'entry_agent_id':entry or snap['planner_id'],'collaboration_flow':False if autonomous else data.collaboration_flow})
    if graph:
        from .engagement import validate_meeting
        validate_meeting(snap['agents'],graph['participants'])
    if template: snap['orchestration_template']={'id':template.id,'version':template.version,'name':template.config['name']}
    # This seam is used only for a NEW run (and its read-only preview/preflight).
    # Resuming an existing run continues to use its already frozen snapshot.
    from .organization import current_layout, inspect_workstations
    seating = inspect_workstations(design_meta.get('organization') or current_layout(s), organization_agents)
    problems = [item['message'] for item in seating if item['status'] == 'error']
    if problems:
        raise ValueError('新任务成员尚未就绪：' + '；'.join(problems))
    snap['design']['workstations'] = {item['agent_id']: item['workstation'] for item in seating}
    # Pin the execution engine into the immutable Run snapshot. This allows a
    # staged Codex-first rollout without altering already-created legacy runs.
    snap['execution_engine'] = 'codex' if settings.codex_default_scheduler_enabled else 'legacy'
    return snap

@app.post('/api/runs/preview-config')
def preview_config(data: RunInput,user=Depends(developer)):
    with session() as s:return build_snapshot(s,data,user)

@app.post('/api/runs', status_code=202)
def create_run(data: RunInput,user=Depends(developer),idempotency_key: str | None=Header(None)):
    checksum=hashlib.sha256(data.model_dump_json().encode()).hexdigest()
    run_id=hashlib.sha256((user.id+':'+idempotency_key).encode()).hexdigest()[:32] if idempotency_key else uid()
    with session() as s:
        old=s.get(Run,run_id)
        if old:
            run_access(s,run_id,user)
            if old.snapshot.get('request_hash')!=checksum: raise HTTPException(409,'幂等键已用于不同请求')
            return run_out(old)
        # All new runs require applied desk ownership, including explicitly
        # selected members without an office-team filter. Application uses this
        # same lock; read-only preview/preflight never acquire a write transaction.
        s.execute(update(PlatformConfig).where(PlatformConfig.id == 'organization_layout')
                  .values(value=PlatformConfig.value).execution_options(synchronize_session=False))
        s.expire_all()
        snap=build_snapshot(s,data,user)
        # Materialize user-provided attachments before enqueueing the run.  The
        # worker may start immediately after commit, so files must already be
        # present in the run-owned input directory.  Names/content are
        # validated by RunInput and never escape this directory.
        attachment_root = run_root(run_id) / 'input-attachments'
        if data.attachments:
            attachment_root.mkdir(parents=True, exist_ok=True)
            manifest=[]
            if len({item.filename for item in data.attachments}) != len(data.attachments):
                raise HTTPException(422, '附件名称不能重复')
            for item in data.attachments:
                raw=base64.b64decode(item.content_base64, validate=True)
                destination=attachment_root / item.filename
                if destination.exists() or destination.is_symlink():
                    raise HTTPException(409, '附件名称重复，请重新选择文件')
                with destination.open('xb') as stream:
                    stream.write(raw)
                manifest.append({'filename':item.filename,'media_type':item.media_type,'size':len(raw)})
            snap['attachments']=manifest
        snap['request_hash']=checksum
        r=Run(id=run_id,project_id=data.project_id,owner_id=user.id,title=data.title,request=data.request,snapshot=snap,status='queued')
        s.add(r);s.flush()
        s.add(Outbox(run_id=run_id,action='start',payload={}))
        emit(s,run_id,'run.created',{'actor':user.id,'title':r.title,'status':'queued'})
        if snap['collaboration_flow']:emit(s,run_id,'command.directed',{'actor':user.id,'entry_agent_id':snap['entry_agent_id'],'agent_id':snap['entry_agent_id'],'recipients':graph_ids(snap),'body':r.request,'summary':r.title})
        return run_out(r)

@app.get('/api/runs')
def list_runs(user=Depends(current_user),limit:int=Query(100,ge=1,le=500)):
    with session() as s:
        project_ids=[p.id for p in s.scalars(select(Project)) if user.role=='admin' or p.owner_id==user.id or user.id in p.config['members']]
        return [run_out(r) for r in s.scalars(select(Run).where(Run.project_id.in_(project_ids)).order_by(Run.created_at.desc()).limit(limit))]

@app.get('/api/runs/{run_id}')
def get_run(run_id: str,user=Depends(current_user)):
    with session() as s:
        run=run_access(s,run_id,user)
        output=run_out(run,True)
        output['tasks']=[{'id':t.id,'key':t.key,'agent_id':t.agent_id,'kind':t.kind,'title':t.title,'status':t.status,'spec':t.spec,'result':t.result} for t in s.scalars(select(Task).where(Task.run_id==run_id))]
        output['commands']=[{'id':c.id,'action':c.action,'payload':c.payload,'sent':c.sent,'error':c.error} for c in s.scalars(select(Outbox).where(Outbox.run_id==run_id))]
        output['event_cursor']=s.scalar(select(func.max(Event.id)).where(Event.run_id==run_id)) or 0
        return output

@app.post('/api/runs/{run_id}/control', status_code=202)
def control_run(run_id: str,data: Control,user=Depends(developer)):
    with session() as s:
        r=run_access(s,run_id,user)
        if data.action == 'steer':
            if (r.snapshot or {}).get('execution_engine') != 'codex':
                raise HTTPException(409, '只有 Codex 运行支持在当前回合追加指令')
            if not data.prompt:
                raise HTTPException(422, '追加指令不能为空')
            if r.status not in {'running', 'awaiting_approval'}:
                raise HTTPException(409, '当前任务没有可追加指令的活动回合')
            target_key = data.task_key or 'codex'
            target = s.get(Task, f'{run_id}:{target_key}')
            if not target:
                raise HTTPException(404, '指定的成员任务不存在')
            if target_key != 'codex' and not settings.codex_team_mcp_enabled:
                raise HTTPException(409, '当前运行未启用团队 Codex 会话')
            command = Outbox(run_id=run_id, action='control', payload={
                'action':'steer', 'task_key':target_key, 'prompt':data.prompt, 'actor':user.id})
            s.add(command); s.flush()
            emit(s, run_id, 'control.requested', {
                'actor':user.id, 'action':'steer', 'command_id':command.id,
                'task_key':target_key, 'prompt_length':len(data.prompt),
            })
            return {'accepted':True,'command_id':command.id,
                    'effect':'已将追加指令排入当前 Codex 回合，不会创建新任务'}
        if data.action in {'retry', 'reassign', 'continue'}:
            if r.snapshot.get('execution_strategy') != 'autonomous':
                raise HTTPException(409, '只有团队自治运行支持在原任务中恢复')
            if not data.task_key:
                raise HTTPException(409, '恢复操作需要指定失败的成员任务')
            if r.status in TERMINAL and r.status not in {'needs_attention'}:
                raise HTTPException(409, '运行已结束；恢复必须在原运行中进行')
            # A Codex-first Run already has one durable Thread/Task. Do not
            # create a legacy recovery activation beside it: queue the same
            # task so the local scheduler resumes its frozen Thread instead.
            if (r.snapshot or {}).get('execution_engine') == 'codex':
                source = s.get(Task, f'{run_id}:{data.task_key}')
                if not source or source.status not in {'failed', 'needs_attention', 'paused'}:
                    raise HTTPException(409, '指定任务没有可恢复的失败或中断边界')
                if data.task_key != 'codex':
                    if not settings.codex_team_mcp_enabled:
                        raise HTTPException(409, '当前运行未启用团队 Codex 会话')
                    if data.action == 'reassign':
                        # Reassignment remains an explicit same-Run activation;
                        # the source task and its evidence are retained.
                        from .autonomy import request_recovery
                        try:
                            recovery = request_recovery(s, r, data.task_key, data.action,
                                                        data.agent_id, data.reason)
                        except ValueError as exc:
                            raise HTTPException(409, str(exc))
                        command = Outbox(run_id=run_id, action='control', payload={
                            'action': data.action, 'task_key': data.task_key,
                            'agent_id': data.agent_id, 'reason': data.reason,
                            'recovery_task_key': recovery.key, 'actor': user.id})
                        s.add(command); s.flush()
                        emit(s, run_id, 'team.recovery.requested', {
                            'source_task_key': data.task_key,
                            'recovery_task_key': recovery.key, 'action': data.action,
                            'agent_id': data.agent_id, 'same_run': True})
                        return {'accepted': True, 'command_id': command.id,
                                'recovery_task_key': recovery.key, 'same_run': True,
                                'effect': '已在原运行内转派，原成员任务与失败证据保留'}
                source.status = 'queued'
                r.status, r.error, r.updated_at = 'queued', '', now()
                command=Outbox(run_id=run_id,action='control',payload={
                    'action':data.action, 'task_key':data.task_key, 'reason':data.reason,
                    'actor':user.id})
                s.add(command); s.flush()
                emit(s,run_id,'team.recovery.requested',{
                    'source_task_key':data.task_key,'recovery_task_key':data.task_key,
                    'action':data.action,'agent_id':source.agent_id,
                    'reason':str(data.reason or '')[:1000],'same_thread':True,
                    'same_run':True})
                return {'accepted':True,'command_id':command.id,
                        'recovery_task_key':data.task_key,'same_run':True,'same_thread':True,
                        'effect':'已在原 Codex Thread 中排队继续；不会创建新任务或重写运行配置'}
            from .autonomy import request_recovery
            try:
                recovery = request_recovery(s, r, data.task_key, data.action,
                                            data.agent_id, data.reason)
            except ValueError as exc:
                raise HTTPException(409, str(exc))
            command=Outbox(run_id=run_id,action='control',payload={
                'action':data.action, 'task_key':data.task_key,
                'agent_id':data.agent_id, 'reason':data.reason, 'actor':user.id,
                'recovery_task_key': recovery.key})
            s.add(command); s.flush()
            emit(s,run_id,'control.requested',{'actor':user.id,'action':data.action,
                'task_key':data.task_key,'agent_id':data.agent_id,
                'recovery_task_key':recovery.key,'command_id':command.id})
            return {'accepted':True,'command_id':command.id,
                    'recovery_task_key':recovery.key,'same_run':True,
                    'effect':'已在原任务中创建恢复激活；失败源任务保留为审计证据'}
        # A failed autonomous run is recoverable in place.  Re-opening the
        # same run preserves its frozen team, workspace and audit trail; the
        # coordinator gets a fresh activation to inspect the failure and may
        # retry or reassign work.  Completed/cancelled runs remain immutable.
        if r.status == 'needs_attention' and data.action == 'resume':
            if r.snapshot.get('execution_strategy') != 'autonomous':
                raise HTTPException(409,'只有团队自治运行支持在原任务中继续；固定流程请从剩余工作新建任务')
            for task in s.scalars(select(Task).where(Task.run_id == r.id)):
                issue = task.spec.get('work_issue') if isinstance(task.spec, dict) else None
                if issue and issue.get('status') == 'escalated':
                    task.spec = {**task.spec, 'work_issue': {**issue, 'status': 'open', 'attempts': 0, 'escalation_reason': ''}}
                # A runtime exception can stop a task before reconciliation
                # had a chance to journal its member failure. Convert that
                # preserved boundary into a blocked result so the first tick
                # after resume routes it to the leader instead of replaying an
                # unknown model/tool step.
                if task.kind == 'autonomous' and task.status == 'needs_attention':
                    error = (r.error or task.result.get('error') or '运行在成员执行边界中断')[:2200]
                    task.status = 'failed'
                    task.result = {**task.result, 'outcome': 'blocked',
                        'summary': task.result.get('summary') or '成员执行中断：' + error,
                        'error': error, 'failure': {**task.result.get('failure', {}), 'uncertain': True}}
            r.status, r.error, r.updated_at = 'queued', '', now()
            emit(s, r.id, 'run.resumed', {'actor': user.id, 'mode': 'same_run', 'summary': '已恢复原任务，负责人将先检查并处理未完成工作'})
        elif r.status in TERMINAL: raise HTTPException(409,'运行已结束；重新执行需要新建运行')
        command=Outbox(run_id=run_id,action='control',payload={'action':data.action,'actor':user.id})
        s.add(command);s.flush()
        emit(s,run_id,'control.requested',{'actor':user.id,'action':data.action,'command_id':command.id})
        return {'accepted':True,'command_id':command.id,'effect':'下一轮模型/工具执行边界，不撤销已发生操作'}

@app.post('/api/runs/{run_id}/decision/{phase}', status_code=202)
def decide(run_id: str,phase: str,data: Decision,user=Depends(developer)):
    if phase not in ('plan','acceptance'):raise HTTPException(404,'未知审批类型')
    with session() as s:
        r=run_access(s,run_id,user)
        s.execute(select(Run.id).where(Run.id==run_id).with_for_update())
        expected=r.plan_digest if phase=='plan' else r.artifact_digest
        state='awaiting_approval' if phase=='plan' else 'awaiting_acceptance'
        if r.status!=state or data.digest!=expected:raise HTTPException(409,'审批对象已变化，或当前不在审批阶段')
        unrequested_checks = (r.snapshot.get('execution_strategy') == 'autonomous'
                              and not r.snapshot.get('run_project_checks', False)
                              and r.report.get('mode') == 'autonomous'
                              and r.report.get('checks', {}).get('verification') == 'not_requested')
        if phase=='acceptance' and data.approved and r.report.get('checks',{}).get('verification')!='passed' and not unrequested_checks and not data.note.strip():
            raise ValueError('自动验收未验证；人工接受必须填写原因')
        for old in s.scalars(select(Outbox).where(Outbox.run_id==run_id,Outbox.action=='decide')):
            if old.payload['phase']==phase and old.payload['digest']==data.digest:
                if old.payload['approved']!=data.approved:raise HTTPException(409,'该版本已提交相反决定')
                return {'accepted':True,'command_id':old.id}
        cmd=Outbox(run_id=run_id,action='decide',payload={'phase':phase,**data.model_dump(),'actor':user.id})
        s.add(cmd);s.flush()
        emit(s,run_id,'decision.requested',{'phase':phase,**data.model_dump(),'actor':user.id,'command_id':cmd.id})
        return {'accepted':True,'command_id':cmd.id}

@app.get('/api/runs/{run_id}/events')
def events(run_id: str,after:int=Query(0,ge=0),limit:int=Query(200,ge=1,le=1000),user=Depends(current_user)):
    with session() as s:
        run_access(s,run_id,user)
        found=list(s.scalars(select(Event).where(Event.run_id==run_id,Event.id>after).order_by(Event.id).limit(limit+1)))
        return {'events':[event_out(e) for e in found[:limit]],'has_more':len(found)>limit}

@app.get('/api/runs/{run_id}/stream')
async def stream_events(run_id: str,request: Request,after:int=Query(0,ge=0),user=Depends(current_user)):
    with session() as s:run_access(s,run_id,user)
    last=request.headers.get('last-event-id','0')
    cursor=max(after,int(last) if last.isdigit() else 0)
    async def stream():
        nonlocal cursor
        while not await request.is_disconnected():
            try:
                live_user=current_user(request)
                with session() as s:
                    run_access(s,run_id,live_user)
                    rows=list(s.scalars(select(Event).where(Event.run_id==run_id,Event.id>cursor).order_by(Event.id).limit(200)))
                for e in rows:
                    cursor=e.id
                    yield f'id: {e.id}\ndata: {json.dumps(event_out(e),ensure_ascii=False)}\n\n'
                if not rows:yield ': keepalive\n\n'
            except HTTPException:
                yield 'event: revoked\ndata: {}\n\n';break
            await asyncio.sleep(1)
    return StreamingResponse(stream(),media_type='text/event-stream',headers={'X-Accel-Buffering':'no'})

@app.get('/api/runs/{run_id}/artifacts/{filename}')
def artifact(run_id: str,filename: str,user=Depends(current_user)):
    if filename not in ('changes.patch','report.json','source.zip','discussion.json','discussion.md'):raise HTTPException(404,'产物不存在')
    with session() as s:run_access(s,run_id,user)
    path=run_root(run_id)/'artifacts'/filename
    if not path.is_file() or path.is_symlink():raise HTTPException(404,'产物尚未生成')
    return FileResponse(path,filename=filename,media_type='application/octet-stream')

@app.get('/api/audit')
def audit(user=Depends(admin)):
    with session() as s:return [event_out(e) for e in s.scalars(select(Event).where(Event.run_id==None).order_by(Event.id.desc()).limit(300))]

@app.post('/api/models/{model_id}/test')
async def test_model(model_id: str, user=Depends(admin)):
    """Explicit, metered connectivity probe. No project content is included."""
    from .security import decrypt
    from .providers import complete, ProviderError
    with session() as s:
        model = s.get(ModelProfile, model_id)
        if not model: raise HTTPException(404, '模型不存在')
        provider = s.get(Provider, model.config['provider_id'])
        if not provider or not provider.enabled: raise ValueError('供应商未启用')
        rev = s.get(ProviderRevision, provider.revision_id)
        connection, model_config, secret = rev.config, model.config, decrypt(rev.encrypted_key)
    try:
        reply = await complete(connection, model_config, [{'role':'user','content':'Connectivity test only. Reply briefly with OK.'}], [], secret, 30)
    except ProviderError as exc:
        raise HTTPException(502, str(exc)) from exc
    finally:
        secret = ''
    with session() as s:
        emit(s,None,'model.tested',{'actor':user.id,'model_id':model_id,'input_tokens':reply.input_tokens,'output_tokens':reply.output_tokens})
    return {'text':reply.text[:300], 'input_tokens':reply.input_tokens, 'output_tokens':reply.output_tokens}


@app.post('/api/models/{model_id}/tool-test')
async def test_model_tools(model_id: str, user=Depends(admin)):
    """Run one explicit tool round-trip and promote this model configuration.

    Catalog discovery never guesses tool support. This endpoint is the single
    repair action exposed by the UI: it sends only a nonce, records the exact
    provider revision, and marks the profile usable only after the real round
    trip succeeds.
    """
    from .security import decrypt
    from .setup_api import probe_model_connection
    with session() as s:
        profile = s.get(ModelProfile, model_id)
        if not profile: raise HTTPException(404, '模型不存在')
        provider = s.get(Provider, profile.config['provider_id'])
        if not provider or not provider.enabled: raise ValueError('供应商未启用')
        revision = s.get(ProviderRevision, provider.revision_id)
        if not revision: raise ValueError('供应商连接版本不存在，请重新保存连接')
        connection, model_config, secret = revision.config, dict(profile.config), decrypt(revision.encrypted_key)
    try:
        probe = await probe_model_connection(connection, model_config, secret)
    except (ProviderError, ValueError) as exc:
        raise HTTPException(502, str(exc)) from exc
    finally:
        secret = ''
    with session() as s:
        profile = s.get(ModelProfile, model_id)
        provider = s.get(Provider, profile.config['provider_id']) if profile else None
        if not profile or not provider or provider.revision_id != revision.id:
            raise HTTPException(409, '供应商或模型配置在检查期间已变化，请重新检查')
        profile.config = {**profile.config, 'supports_tools': True}
        profile.version += 1
        record = s.get(PlatformConfig, 'probe:' + model_id)
        value = {'revision_id': revision.id, 'model_version': profile.version, 'probe': probe}
        if record: record.value = value
        else: s.add(PlatformConfig(id='probe:' + model_id, value=value))
        emit(s, None, 'model.tools_tested', {'actor': user.id, 'model_id': model_id,
                                             'provider_id': provider.id, 'model_version': profile.version,
                                             'revision_id': revision.id, 'requests': probe['requests']})
        return {**config_out(profile), 'probe': probe}


from .extensions_api import register as register_extensions
register_extensions(app)
from .setup_api import register as register_setup
register_setup(app)
from .skill_store_api import register as register_skill_store
register_skill_store(app)
from .role_templates import register as register_role_templates
register_role_templates(app)
from .design_center import register as register_design
register_design(app)
from .model_hub import register as register_model_hub
register_model_hub(app)
from .provider_catalog import register as register_provider_catalog
register_provider_catalog(app)
from .provider_model_state import register as register_provider_model_state
register_provider_model_state(app)
from .design_runtime import register as register_design_runtime
register_design_runtime(app)
from .execution_readiness import register as register_execution_readiness
register_execution_readiness(app)
from .tool_installations import register as register_tool_installations
register_tool_installations(app)
from .execution_permissions import register as register_execution_permissions
register_execution_permissions(app)
from .execution_tools import register as register_execution_tools
register_execution_tools(app)
register_runtime_poc(app)
from .runtime_approvals import register as register_runtime_approvals
register_runtime_approvals(app)

# API routes have priority over static assets; unknown API paths must remain JSON 404.
@app.api_route('/api/{unmatched:path}', methods=['GET','POST','PUT','DELETE','PATCH'], include_in_schema=False)
def api_not_found(unmatched: str): raise HTTPException(404,'接口不存在')

if settings.frontend_dir.is_dir():
    app.mount('/', StaticFiles(directory=str(settings.frontend_dir),html=True),name='frontend')
