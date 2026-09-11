"""v0.8 onboarding: real DB + HTTP API; model network is mocked explicitly."""
import asyncio,json,hashlib
from pathlib import Path
import pytest
from sqlalchemy import select
from forge import setup_api,providers
from forge.db import session,Agent,ModelProfile,Provider,ProviderRevision,Event,PlatformConfig,Skill,SkillRevision,Run,Step
from forge.config import settings
from forge.security import decrypt
from forge.schema import AgentInput

PAYLOAD={'name':'Own Service','protocol':'openai_chat','base_url':'http://127.0.0.1:8097/v1/chat/completions','api_key':'test-sensitive-only-DO-NOT-ECHO','external':False,'model_id':'own-code-model','parameters':{},'acknowledge_probe':True,'bind_unassigned_agents':True,'install_builtin_skills':True}

def connect(client,body=None,key='setup-once'):
    return client.post('/api/setup/connect',json=body or PAYLOAD,headers={'Idempotency-Key':key})

@pytest.fixture
def mock_probe(monkeypatch):
    calls=[]
    async def complete(connection,model,messages,tools,key,timeout,transport=None):
        calls.append({'connection':connection,'model':model,'messages':messages,'tools':tools})
        if tools:
            nonce=messages[0]['content'].split('nonce ')[1].split('.')[0]
            return providers.Reply('',[{'id':'p-1','name':'forge_connection_check','arguments':{'nonce':nonce}}],9,3)
        return providers.Reply('OK',[],6,1)
    monkeypatch.setattr(providers,'complete',complete)
    return calls

def test_fresh_instance_has_no_fake_task_or_supplier(client):
    r=client.get('/api/setup/status').json()
    assert r['needs_connection'] and not r['team_ready'] and r['mode']=='production'
    assert r['agent_count']==4 and client.get('/api/providers').json()==[] and client.get('/api/runs').json()==[]

def test_connect_initializes_model_skills_and_all_unbound_agents(client,mock_probe):
    r=connect(client);assert r.status_code==200,r.text
    out=r.json();assert len(mock_probe)==2 and out['probe']['tool_roundtrip']
    assert len(out['agents_updated'])==4 and len(out['skills'])==3
    agents=client.get('/api/agents').json()
    assert all(a['model_profile_id']==out['model_id'] and len(a['skills'])==1 for a in agents)
    assert all(not b['allow_scripts'] and b['mode']=='on_demand' for a in agents for b in a['skills'])
    status=client.get('/api/setup/status').json();assert status['team_ready'] and status['checked_model_count']==1 and not status['needs_connection']
    assert client.get('/api/projects').json()==[] and client.get('/api/runs').json()==[]
    assert mock_probe[0]['connection']['base_url'].endswith('/v1')
    assert mock_probe[0]['model']['parameters']=={} # No guessed max_tokens/temperature.

def test_secret_encrypted_and_absent_from_outputs_and_audit(client,mock_probe):
    r=connect(client);assert r.status_code==200
    text=r.text+client.get('/api/providers').text+client.get('/api/models').text+client.get('/api/audit').text
    assert PAYLOAD['api_key'] not in text
    with session() as s:
        rev=s.scalar(select(ProviderRevision));assert rev.encrypted_key!=PAYLOAD['api_key'] and decrypt(rev.encrypted_key)==PAYLOAD['api_key']
        assert PAYLOAD['api_key'] not in json.dumps([st.result for st in s.scalars(select(Step))])

def test_success_idempotency_avoids_extra_billable_calls(client,mock_probe):
    first=connect(client);second=connect(client)
    assert first.json()==second.json() and len(mock_probe)==2
    assert len(client.get('/api/providers').json())==1
    changed=connect(client,{**PAYLOAD,'model_id':'changed'})
    assert changed.status_code==409 and len(mock_probe)==2

def test_second_supplier_preserves_existing_agent_choices_runtime_and_snapshot(client,mock_probe):
    first=connect(client).json()
    before=client.get('/api/agents').json()
    a=before[0];body={k:v for k,v in a.items() if k not in ('id','version')};body['runtime']={'max_steps':55}
    client.put('/api/agents/'+a['id'],json=body).raise_for_status()
    before=client.get('/api/agents').json()
    second=connect(client,{**PAYLOAD,'name':'Second Vendor','model_id':'second-model'},'setup-second')
    assert second.status_code==200 and second.json()['agents_updated']==[]
    assert client.get('/api/agents').json()==before
    assert len(client.get('/api/providers').json())==2

def test_existing_draft_same_name_is_never_auto_approved(client,mock_probe):
    path=setup_api.BUILTINS/'requirements-clarification/SKILL.md'
    raw=path.read_text().replace('SKILL-DOES-NOT-EXIST','unchanged')+'\nCustom note.\n'
    row=client.post('/api/skills',json={'files':{'SKILL.md':raw}}).json()
    assert 'id' in row
    r=connect(client);assert r.status_code==200,r.text
    assert next(x for x in r.json()['skills'] if x['name']=='requirements-clarification')['status']=='existing_preserved'
    assert client.get('/api/skills/'+row['id']).json()['status']=='draft'
    planner=next(a for a in client.get('/api/agents').json() if a['role']=='planner')
    assert planner['skills']==[]

def test_revoked_builtin_stays_revoked_when_adding_supplier(client,mock_probe):
    r=connect(client).json();item=r['skills'][0];skill=client.get('/api/skills/'+item['skill_id']).json()
    client.post('/api/skills/'+item['skill_id']+'/review',json={'version':skill['version'],'digest':skill['digest'],'action':'revoke'}).raise_for_status()
    r=connect(client,{**PAYLOAD,'name':'Second'},'setup-second')
    assert r.status_code==200 and client.get('/api/skills/'+item['skill_id']).json()['status']=='revoked'

def test_builtin_install_can_be_disabled(client,mock_probe):
    r=connect(client,{**PAYLOAD,'install_builtin_skills':False});assert r.status_code==200
    assert client.get('/api/skills').json()==[]
    assert all(not a['skills'] for a in client.get('/api/agents').json())

def test_disabled_agents_not_modified(client,mock_probe):
    a=client.get('/api/agents').json()[0];body={k:v for k,v in a.items() if k not in ('id','version')};body['enabled']=False
    client.put('/api/agents/'+a['id'],json=body).raise_for_status()
    r=connect(client);assert r.status_code==200 and len(r.json()['agents_updated'])==3
    after=next(x for x in client.get('/api/agents').json() if x['id']==a['id'])
    assert not after['model_profile_id'] and not after['skills']

def test_unsupported_tool_response_does_not_create_config(client,monkeypatch):
    async def plain(*args,**kwargs):return providers.Reply('hello',[],1,1)
    monkeypatch.setattr(providers,'complete',plain)
    r=connect(client);assert r.status_code==502
    assert client.get('/api/providers').json()==[] and client.get('/api/skills').json()==[]
    assert connect(client).status_code==409 # Failed or uncertain operation never auto-replayed.

def test_tool_result_roundtrip_must_succeed(client,monkeypatch):
    n=0
    async def bad(connection,model,messages,tools,key,timeout):
        nonlocal n;n+=1
        if tools:
            nonce=messages[0]['content'].split('nonce ')[1].split('.')[0]
            return providers.Reply('',[{'id':'p','name':'forge_connection_check','arguments':{'nonce':nonce}}],1,1)
        return providers.Reply('unrelated answer',[],1,1)
    monkeypatch.setattr(providers,'complete',bad)
    r=connect(client);assert r.status_code==502 and n==2 and client.get('/api/providers').json()==[]

def test_invalid_parameter_or_consent_does_not_call_model(client,mock_probe):
    assert connect(client,{**PAYLOAD,'parameters':{'headers':{'key':'x'}}}).status_code==422
    assert connect(client,{**PAYLOAD,'acknowledge_probe':False}).status_code==422
    assert connect(client,{**PAYLOAD,'model_id':'  '}).status_code==422
    assert len(mock_probe)==0

def test_anthropic_params_explicit_and_tool_context_preserved(client,mock_probe):
    assert connect(client,{**PAYLOAD,'protocol':'anthropic_messages'}).status_code==422
    r=connect(client,{**PAYLOAD,'protocol':'anthropic_messages','parameters':{'max_tokens':2048}})
    assert r.status_code==200 and mock_probe[0]['model']['parameters']=={'max_tokens':2048}

def test_supplier_revision_invalidates_probed_status(client,mock_probe):
    r=connect(client).json()
    p=client.get('/api/providers').json()[0]
    client.put('/api/providers/'+r['provider_id'],json={'name':p['name'],'protocol':p['protocol'],'base_url':p['base_url'],'external':p['external'],'api_key':'new-key'}).raise_for_status()
    assert client.get('/api/setup/status').json()['checked_model_count']==0

def test_model_version_invalidates_probed_status(client,mock_probe):
    r=connect(client).json();m=client.get('/api/models').json()[0]
    body={k:v for k,v in m.items() if k not in ('id','version')};body['parameters']={'max_tokens':1500};body['allowed_parameters']=['max_tokens']
    client.put('/api/models/'+m['id'],json=body).raise_for_status()
    assert client.get('/api/setup/status').json()['checked_model_count']==0

def test_requires_admin_and_logged_in(client,mock_probe):
    client.post('/api/users',json={'username':'reader','password':'reader-password-1234','role':'viewer'}).raise_for_status()
    client.post('/api/auth/logout',json={})
    assert client.get('/api/setup/status').status_code==401
    client.post('/api/auth/login',json={'username':'reader','password':'reader-password-1234'}).raise_for_status()
    assert connect(client).status_code==403 and client.get('/api/setup/environment').status_code==403
    assert not client.get('/api/setup/status').json()['can_configure'] and not mock_probe

def test_catalog_is_optional_and_never_persists_or_selects_model(client,monkeypatch):
    async def listing(data):return {'models':[{'id':'chosen-later','label':'Actual id'}],'has_more':False}
    monkeypatch.setattr(setup_api,'discover',listing)
    body={k:v for k,v in PAYLOAD.items() if k in ('name','protocol','base_url','api_key','external')}
    r=client.post('/api/setup/models',json=body);assert r.status_code==200 and r.json()['models'][0]['id']=='chosen-later'
    assert client.get('/api/providers').json()==[]

def test_environment_unavailable_is_not_marked_ready(client):
    r=client.get('/api/setup/environment');assert r.status_code==200 and r.json()['sandbox']['state']!='ready'

def test_dns_error_is_sanitized_no_500_or_secret_echo(client,monkeypatch):
    async def bad(*a,**k):raise OSError('this might contain '+PAYLOAD['api_key'])
    monkeypatch.setattr(providers,'complete',bad)
    r=connect(client);assert r.status_code==502 and PAYLOAD['api_key'] not in r.text
    assert PAYLOAD['api_key'] not in client.get('/api/audit').text

def test_tampered_builtin_rolls_back_whole_configuration(client,mock_probe,monkeypatch,tmp_path):
    import shutil
    shutil.copytree(setup_api.BUILTINS,tmp_path/'skills')
    p=tmp_path/'skills/requirements-clarification/SKILL.md';p.write_text(p.read_text()+'tampered')
    monkeypatch.setattr(setup_api,'BUILTINS',tmp_path/'skills')
    r=connect(client);assert r.status_code==502
    assert client.get('/api/providers').json()==[] and client.get('/api/models').json()==[]
    assert client.get('/api/skills').json()==[]

def test_external_cleartext_and_url_secrets_rejected(client,mock_probe):
    assert connect(client,{**PAYLOAD,'external':True}).status_code==422
    assert connect(client,{**PAYLOAD,'base_url':'https://key:secret@service.test/v1'}).status_code==422
    assert not mock_probe


def test_production_bundle_has_no_demo_factories_or_preview_entrypoints():
    root=Path(__file__).resolve().parents[2]
    text=(root/'frontend/dist/index.html').read_text()
    for forbidden in ["uiFactories[\"SuitePreview\"]", "uiFactories[\"Preview\"]", "uiFactories[\"previewV07\"]",'__forgeDemo','示例散会 / 推进下一阶段','重置示例']:
        assert forbidden not in text,forbidden
    assert not (root/'frontend/dist/workbench-preview.html').exists()
    assert not (root/'frontend/dist/SuitePreview.js').exists()
    assert 'provider-connector' in text
    assert 'provider-onboarding' not in text
