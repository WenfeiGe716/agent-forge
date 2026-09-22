"""Optional provider connector; never an authentication or workspace entry gate. No fixture, fake project or implicit permission grants.
Tests use protocol fixtures; production probes always call the configured endpoint.
"""
import hashlib
import hmac
import json
import secrets
from pathlib import Path
from typing import Any
import httpx
from fastapi import Depends, Header, HTTPException
from pydantic import Field, model_validator
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from .config import settings
from .db import (session, Agent, Provider, ProviderRevision, ModelProfile, PlatformConfig,
                 Skill, SkillRevision, Step, emit, uid, now)
from .schema import Strict, ProviderInput, AgentInput, SkillBinding
from .security import validate_url, encrypt
from . import providers, protocols, skills

BUILTINS = Path(__file__).with_name('builtin_skills')

class ConnectionSettings(Strict):
    name: str = Field('默认模型服务', min_length=1, max_length=100)
    protocol: str = 'openai_chat'
    protocol_config: dict[str, Any] = Field(default_factory=dict)
    base_url: str = Field(min_length=1, max_length=1500)
    api_key: str = Field('', max_length=4096)
    external: bool = True

    @model_validator(mode='after')
    def validate(self):
        if self.protocol not in {*providers.PARAMETERS, 'custom_http'}:
            raise ValueError('请选择已实现的协议适配器')
        self.base_url = validate_url(self.base_url)
        # A common paste mistake: use the same root for discovery AND model calls.
        for suffix in (() if self.protocol == 'custom_http' else ('/chat/completions', '/messages', '/responses', '/models')):
            if self.base_url.endswith(suffix):
                self.base_url = self.base_url[:-len(suffix)]
                break
        if self.external and self.base_url.startswith('http://'):
            raise ValueError('外部供应商必须使用HTTPS；内网HTTP请明确选择内网部署')
        self.protocol_config = protocols.validate_connection(self.protocol, self.base_url, self.external, self.protocol_config)
        return self

class ConnectInput(ConnectionSettings):
    model_id: str = Field(min_length=1, max_length=200)
    parameters: dict[str, Any] = Field(default_factory=dict)
    install_builtin_skills: bool = False
    bind_unassigned_agents: bool = False
    acknowledge_probe: bool = False

    @model_validator(mode='after')
    def validate_model(self):
        if not self.model_id.strip():
            raise ValueError('请填写实际模型ID，或从供应商返回的列表中选择')
        self.model_id = self.model_id.strip()
        if 'parameters' not in self.model_fields_set:
            from .providers import parameter_defaults
            _, self.parameters = parameter_defaults(self.protocol, self.protocol_config, self.model_id)
        providers.validate_parameters(self.protocol, list(self.parameters), self.parameters, self.protocol_config)
        if self.protocol == 'custom_http' and self.protocol_config['response'].get('calls') is None:
            raise ValueError('连接检查需要自定义协议配置完整的工具调用响应；纯文本模型请在模型资产中登记')
        if not self.acknowledge_probe:
            raise ValueError('连接检查将发送两次不含项目数据的模型请求，请确认后继续')
        return self

async def discover(data: ConnectionSettings) -> dict:
    """Read the native catalog; never invoke a model or change saved assets."""
    try:
        rows = await providers.discover({'protocol': data.protocol, 'base_url': data.base_url, 'external': data.external, 'protocol_config': data.protocol_config}, data.api_key)
        return {'models': [{**row, 'label': row.get('name', row['id'])} for row in rows], 'has_more': False,
                'note': '目录仅供选择，未创建模型或绑定 Agent；工具能力需另行检查。'}
    except providers.ProviderError:
        raise
    except Exception as exc:
        # Deliberately exclude URL, credential, arbitrary vendor response body.
        raise providers.ProviderError('无法获取模型目录，请检查地址或直接填写模型ID') from exc

async def probe_model_connection(connection: dict, model: dict, key: str) -> dict:
    """Perform the same small, real tool round-trip used during onboarding.

    Keeping this in one place prevents a saved catalog model from requiring a
    second, subtly different verification path before it can run an Agent.
    """
    nonce = secrets.token_hex(12)
    tool = {'name': 'forge_connection_check', 'description': 'Read-only onboarding connection check. Return the given nonce. No files or commands.',
            'parameters': {'type': 'object', 'properties': {'nonce': {'type': 'string'}}, 'required': ['nonce'], 'additionalProperties': False}}
    messages = [{'role': 'user', 'content': 'Connection test only, no project data. Call forge_connection_check exactly once with nonce '+nonce+'. Do not answer in text yet.'}]
    first = await providers.complete(connection, model, messages, [tool], key, 30)
    if len(first.calls) != 1 or first.calls[0]['name'] != tool['name'] or first.calls[0]['arguments'] != {'nonce':nonce}:
        raise providers.ProviderError('模型未返回有效工具调用；请确认型号支持工具调用及当前协议。未创建配置。')
    call = first.calls[0]
    assistant = {'role':'assistant', 'content':first.text, 'calls':first.calls}
    if first.native is not None:
        assistant['anthropic_native'] = first.native
    messages.extend([assistant, {'role':'tool','call_id':call['id'], 'content':json.dumps({'ok':True,'nonce':nonce})},
                     {'role':'user','content':'The test tool succeeded. Reply only OK; do not call another tool.'}])
    second = await providers.complete(connection, model, messages, [], key, 30)
    if second.calls or second.text.strip().strip('.!。！').upper() != 'OK':
        raise providers.ProviderError('工具结果回传后的确认不符合要求；未创建配置。')
    return {'checked_at':now(),'requests':2,'tool_roundtrip':True,
            'input_tokens':first.input_tokens+second.input_tokens,
            'output_tokens':first.output_tokens+second.output_tokens}


async def probe_connection(data: ConnectInput) -> dict:
    connection = {'protocol': data.protocol, 'base_url': data.base_url, 'external': data.external, 'protocol_config': data.protocol_config}
    model = {'model_id': data.model_id, 'allowed_parameters': list(data.parameters), 'parameters': data.parameters}
    return await probe_model_connection(connection, model, data.api_key)


def initialize_skills(s, actor: str) -> tuple[dict, list]:
    """Auto-trust only this release's hash-pinned, script-free first-party files.
    Existing custom/draft/revoked packages never get approved or overwritten.
    """
    manifest = json.loads((BUILTINS/'manifest.json').read_text())
    role_bindings, reports = {}, []
    for item in manifest['packages']:
        name=item['name']; files={}
        for rel, checksum in item['files'].items():
            path=(BUILTINS/name/rel).resolve()
            if not path.is_relative_to((BUILTINS/name).resolve()) or path.is_symlink() or rel.startswith('scripts/'):
                raise ValueError('内置Skill路径不安全')
            text=path.read_text(encoding='utf-8')
            if hashlib.sha256(text.encode()).hexdigest()!=checksum:
                raise ValueError('内置Skill摘要校验失败，初始化已取消')
            files[rel]=text
        row=s.scalar(select(Skill).where(Skill.name==name))
        if row:
            rev=s.get(SkillRevision,f'{row.id}:{row.latest_version}')
            if not row.enabled or not rev or rev.status!='approved' or rev.digest!=skills.checksum(files):
                reports.append({'name':name,'status':'existing_preserved'});continue
        else:
            row,rev=skills.save(s,files,actor)
            rev.status='approved';rev.approved_by='builtin:0.8.0'
            emit(s,None,'skill.builtin.installed',{'actor':actor,'skill_id':row.id,'version':rev.version,'digest':rev.digest,'source':'bundled-v0.8.0'})
        binding=SkillBinding(skill_id=row.id,version=rev.version).model_dump()
        for role in item['roles']:
            role_bindings.setdefault(role,[]).append(binding)
        reports.append({'name':name,'status':'ready','skill_id':row.id,'version':rev.version})
    return role_bindings,reports


def save_connection(s, data: ConnectInput, actor: str, probe: dict) -> dict:
    provider=Provider(id=uid(),name=data.name,revision_id=uid(),enabled=True)
    rev=ProviderRevision(id=provider.revision_id,provider_id=provider.id,
                         config={'protocol':data.protocol,'base_url':data.base_url,'external':data.external,'protocol_config':data.protocol_config},
                         encrypted_key=encrypt(data.api_key))
    model=ModelProfile(id=uid(),config={'enabled':True,'name':data.model_id,'provider_id':provider.id,
                       'model_id':data.model_id,'allowed_parameters':list(data.parameters),'parameters':data.parameters,'supports_tools':True})
    s.add_all([provider,rev,model]);s.flush()
    bindings,skill_report=initialize_skills(s,actor) if data.install_builtin_skills else ({},[])
    changed=[]
    for agent in s.scalars(select(Agent)):
        if not agent.config.get('enabled',True):continue
        config=dict(agent.config);dirty=False
        # Adding another supplier never changes a user's existing choice.
        if data.bind_unassigned_agents and not config.get('model_profile_id'):
            config['model_profile_id']=model.id;dirty=True
        if not config.get('skills') and bindings.get(config['role']):
            config['skills']=bindings[config['role']];dirty=True
        if dirty:
            AgentInput.model_validate(config)
            agent.config=config;agent.version+=1;changed.append(agent.id)
            emit(s,None,'agent.setup.bound',{'actor':actor,'agent_id':agent.id,'version':agent.version,'model_profile_id':config.get('model_profile_id'),'skills':[b['skill_id'] for b in config.get('skills',[])]})
    state={'provider_id':provider.id,'revision_id':rev.id,'model_id':model.id,'model_version':model.version,
           'probe':probe,'agents_updated':changed,'skills':skill_report,
           'bind_unassigned_agents':data.bind_unassigned_agents}
    s.add(PlatformConfig(id='probe:'+model.id,value=state))
    setup=s.get(PlatformConfig,'onboarding')
    if not setup:
        s.add(PlatformConfig(id='onboarding',value={'completed_at':now(),'actor':actor,'initial_model_id':model.id}))
    emit(s,None,'setup.connected',{'actor':actor,**state})
    return state


def register(app):
    from .api import current_user,admin

    @app.get('/api/setup/status')
    def status(user=Depends(current_user)):
        with session() as s:
            usable=[]
            for m in s.scalars(select(ModelProfile)):
                p=s.get(Provider,m.config['provider_id'])
                if m.config.get('enabled',True) and m.config.get('supports_tools') and p and p.enabled:
                    prior=s.get(PlatformConfig,'probe:'+m.id)
                    checked=bool(prior and prior.value.get('revision_id')==p.revision_id and prior.value.get('model_version')==m.version)
                    usable.append({'id':m.id,'checked':checked})
            ids={m['id'] for m in usable}
            agents=[a for a in s.scalars(select(Agent)) if a.config.get('enabled',True)]
            bound=[a for a in agents if a.config.get('model_profile_id') in ids]
            return {'mode':'validation' if settings.validation_mode else 'production','has_usable_model':bool(usable),
                    'team_ready':bool(agents) and len(bound)==len(agents),'needs_connection':not bool(usable),
                    'agent_count':len(agents),'bound_agent_count':len(bound),'checked_model_count':sum(m['checked'] for m in usable),
                    'workspace_accessible':True,'provider_count':s.scalar(select(func.count()).select_from(Provider)),
                    'can_configure':user.role=='admin','onboarding_completed':bool(s.get(PlatformConfig,'onboarding'))}

    @app.post('/api/setup/models')
    async def models(data: ConnectionSettings, user=Depends(admin)):
        try:return await discover(data)
        except providers.ProviderError as exc:raise HTTPException(502,str(exc)) from exc

    @app.post('/api/setup/connect')
    async def connect(data: ConnectInput, user=Depends(admin), idempotency_key: str=Header('',alias='Idempotency-Key')):
        if not idempotency_key or len(idempotency_key)>100 or not all(c.isalnum() or c in '-_' for c in idempotency_key):
            raise HTTPException(422,'缺少有效的请求标识，请重新提交')
        operation='setup:'+hashlib.sha256((user.id+':'+idempotency_key).encode()).hexdigest()
        fingerprint=hmac.new(settings.encryption_key.encode(),data.model_dump_json().encode(),hashlib.sha256).hexdigest()
        with session() as s:
            step=s.get(Step,operation)
            if step:
                if step.result.get('fingerprint')!=fingerprint:raise HTTPException(409,'同一请求标识的配置已改变')
                if step.state=='completed':return step.result['response']
                raise HTTPException(409,'此前连接检查仍在执行或结果未确认。请先检查已有连接；不自动重复付费调用。')
            s.add(Step(id=operation,state='started',result={'fingerprint':fingerprint}))
            try:s.flush()
            except IntegrityError as exc:raise HTTPException(409,'连接检查已在执行，请勿重复提交') from exc
        try:
            probe=await probe_connection(data)
            with session() as s:
                response=save_connection(s,data,user.id,probe)
                step=s.get(Step,operation);step.state='completed';step.result={'fingerprint':fingerprint,'response':response}
            return response
        except Exception as exc:
            reason=str(exc) if isinstance(exc,(providers.ProviderError,ValueError)) else '连接或初始化未完成，请检查服务地址与本地状态。不会自动重试模型调用。'
            with session() as s:
                step=s.get(Step,operation);step.state='failed';step.result={'fingerprint':fingerprint,'error':reason}
                emit(s,None,'setup.failed',{'actor':user.id,'reason':reason})
            raise HTTPException(502,reason) from exc

    @app.get('/api/setup/environment')
    async def environment(user=Depends(admin)):
        from shutil import which
        from .local_executor import readiness as local_readiness
        output={'database':'connected','orchestrator':getattr(app.state,'dispatch_status','starting'),
                'git':bool(which('git')),'local':await local_readiness(),
                'sandbox':{'state':'unavailable','detail':'可选容器执行未连接；不阻止已就绪的本机执行。'}}
        try:
            async with httpx.AsyncClient(timeout=4,trust_env=False,follow_redirects=False) as client:
                r=await client.get(settings.sandbox_url.rstrip('/')+'/readiness',headers={'X-Sandbox-Token':settings.sandbox_token})
                if r.status_code==200:
                    result=r.json()
                    if result.get('service')=='sandbox-broker' and isinstance(result.get('ready'),bool):
                        output['sandbox']={'state':'ready' if result['ready'] else 'not_ready',**{k:result[k] for k in ('runtime','images','missing') if k in result}}
                elif r.status_code==404:
                    output['sandbox']={'state':'unverified','detail':'执行服务版本未提供就绪检查，请升级执行服务后复核。'}
        except (httpx.HTTPError,ValueError,TypeError):pass
        return output
