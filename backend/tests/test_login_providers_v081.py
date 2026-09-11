"""Account entry and opt-in multi-provider configuration, no live LLM requests."""
from pathlib import Path
import pytest
from forge import providers
from forge.config import settings
from conftest import run_body

@pytest.fixture
def probe(monkeypatch):
    calls=[]
    async def complete(connection,model,messages,tools,key,timeout,transport=None):
        calls.append(model['model_id'])
        if tools:
            nonce=messages[0]['content'].split('nonce ')[1].split('.')[0]
            return providers.Reply('',[{'id':'c','name':'forge_connection_check','arguments':{'nonce':nonce}}],1,1)
        return providers.Reply('OK',[],1,1)
    monkeypatch.setattr(providers,'complete',complete)
    return calls

def payload(**updates):
    return {'name':'Connection A','protocol':'openai_chat','base_url':'http://127.0.0.1:8097/v1','external':False,'api_key':'test-only-not-real',
            'model_id':'model-A','parameters':{},'acknowledge_probe':True,**updates}

def connect(client,**updates):
    return client.post('/api/setup/connect',json=payload(**updates),headers={'Idempotency-Key':updates.get('name','A').replace(' ','-')})

def test_login_does_not_require_any_provider_or_model(client):
    client.post('/api/auth/logout',json={}).raise_for_status()
    assert client.get('/api/agents').status_code==401
    r=client.post('/api/auth/login',json={'username':'admin','password':settings.admin_password})
    assert r.status_code==200 and r.json()['username']=='admin'
    assert client.get('/api/providers').json()==[]
    status=client.get('/api/setup/status').json()
    assert status['needs_connection'] and status['workspace_accessible']
    for path in ('/api/projects','/api/agents','/api/runs','/api/office/state'):
        assert client.get(path).status_code==200

def test_connector_does_not_bind_agents_or_skills_by_default(client,probe):
    before=client.get('/api/agents').json()
    r=connect(client);assert r.status_code==200,r.text
    assert r.json()['agents_updated']==[] and r.json()['skills']==[]
    assert not r.json()['bind_unassigned_agents']
    assert client.get('/api/agents').json()==before
    assert len(probe)==2 and len(client.get('/api/models').json())==1

def test_multiple_connections_remain_independent_without_auto_assignment(client,probe):
    before=client.get('/api/agents').json()
    a=connect(client);b=connect(client,name='Connection B',model_id='model-B',protocol='anthropic_messages',parameters={'max_tokens':2048})
    assert a.status_code==b.status_code==200
    assert client.get('/api/agents').json()==before
    st=client.get('/api/setup/status').json()
    assert st['provider_count']==2 and st['bound_agent_count']==0 and st['workspace_accessible']
    assert len(probe)==4

def test_bulk_bind_only_when_explicitly_requested_and_preserves_existing(client,probe):
    a=connect(client).json();agents=client.get('/api/agents').json()
    first=agents[0];body={k:v for k,v in first.items() if k not in ('id','version')}
    client.put('/api/agents/'+first['id'],json={**body,'model_profile_id':a['model_id'],'runtime':{'max_steps':37}}).raise_for_status()
    before=client.get('/api/agents').json()
    b=connect(client,name='Connection B',model_id='model-B',bind_unassigned_agents=True)
    assert b.status_code==200 and len(b.json()['agents_updated'])==3
    after=client.get('/api/agents').json()
    assert next(x for x in after if x['id']==first['id'])==next(x for x in before if x['id']==first['id'])
    assert all(x['model_profile_id']==b.json()['model_id'] for x in after if x['id']!=first['id'])

def test_save_offline_connection_without_probing_or_creating_model(client,probe):
    before=client.get('/api/agents').json()
    r=client.post('/api/providers',json={'name':'Currently offline','protocol':'openai_chat','base_url':'http://offline.internal/v1','external':False})
    assert r.status_code==201 and not probe
    assert client.get('/api/models').json()==[] and client.get('/api/agents').json()==before

def test_disabling_all_suppliers_does_not_disable_workspace_or_login(client,probe):
    connect(client)
    p=client.get('/api/providers').json()[0]
    client.put('/api/providers/'+p['id'],json={'name':p['name'],'protocol':p['protocol'],'base_url':p['base_url'],'external':p['external'],'enabled':False}).raise_for_status()
    assert client.get('/api/setup/status').json()['needs_connection']
    assert client.get('/api/auth/me').status_code==client.get('/api/office/state').status_code==200

def test_unused_unconfigured_agent_does_not_block_selected_run(client,configured):
    client.post('/api/agents',json={'name':'Unconfigured bystander','role':'developer'}).raise_for_status()
    r=client.post('/api/runs/preview-config',json=run_body(configured))
    assert r.status_code==200,r.text
    assert len(r.json()['agents'])==4
    assert len({a['models'][0]['credential_revision'] for a in r.json()['agents'].values()})==2

def test_unconfigured_participant_rejected_only_at_run_check(client,configured):
    a=configured['agents'][0];body={k:v for k,v in a.items() if k not in ('id','version')}
    client.put('/api/agents/'+a['id'],json={**body,'model_profile_id':None}).raise_for_status()
    r=client.post('/api/runs/preview-config',json=run_body(configured))
    assert r.status_code==422 and a['name'] in r.text and '尚未绑定模型' in r.text
    assert client.get('/api/projects').status_code==client.get('/api/office/state').status_code==200

def test_viewer_login_without_providers_has_read_access_not_write_access(client):
    client.post('/api/users',json={'username':'viewer081','password':'viewer081-pass-1234','role':'viewer'}).raise_for_status()
    client.post('/api/auth/logout',json={})
    client.post('/api/auth/login',json={'username':'viewer081','password':'viewer081-pass-1234'}).raise_for_status()
    assert client.get('/api/setup/status').json()['workspace_accessible']
    assert client.get('/api/providers').status_code==200
    assert client.post('/api/providers',json={'name':'blocked','protocol':'openai_chat','base_url':'http://local.internal/v1','external':False}).status_code==403

def test_production_entry_has_no_model_setup_gate():
    root=Path(__file__).resolve().parents[2]
    src=(root/'frontend/src/App.tsx').read_text()
    assert 'setupDeferred' not in src and 'setupStatus?.needs_connection' not in src
    assert 'if(!user)return <Login' in src and 'setPage(\'workspace\')' in src
    assert 'admin&&showSetup&&<Modal' in src
    html=(root/'frontend/dist/index.html').read_text()
    assert 'provider-onboarding' not in html
    assert 'provider-management' in html and 'login-page' in html
