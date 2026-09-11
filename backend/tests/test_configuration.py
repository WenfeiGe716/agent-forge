import pytest
from pydantic import ValidationError
from forge.schema import resolve_runtime,validate_runtime_patch,validate_agent_runtime_patch,Plan,AgentInput
from forge.providers import validate_parameters
from forge.security import encrypt,decrypt,password_hash,password_matches,validate_url
from forge.db import session,ProviderRevision
from conftest import run_body

def test_layered_configuration_and_sources():
    values,sources=resolve_runtime([('project',{'max_steps':17}),('role',{'max_steps':8}),('agent',{'max_steps':32}),('run',{'command_timeout_s':200})])
    assert values['max_steps']==32 and sources['max_steps']=='agent'
    assert values['command_timeout_s']==200 and sources['command_timeout_s']=='run'
    assert sources['max_parallel']=='platform'

@pytest.mark.parametrize('patch',[{'unknown':3},{'max_steps':0},{'cpu':-1},{'max_parallel':None}])
def test_invalid_runtime_is_rejected(patch):
    with pytest.raises(ValueError):validate_runtime_patch(patch)

@pytest.mark.parametrize('patch',[{'max_parallel':4},{'max_model_calls':10},{'max_tasks':5}])
def test_run_scoped_fields_not_silently_ignored_on_agents(patch):
    with pytest.raises(ValueError):validate_agent_runtime_patch(patch)

def test_parameters_must_match_declared_capability():
    with pytest.raises(ValueError):validate_parameters('openai_chat',['temperature'],{'max_tokens':20})
    with pytest.raises(ValueError):validate_parameters('openai_chat',['messages'],{'messages':[]})
    with pytest.raises(ValueError):validate_parameters('anthropic_messages',['temperature'],{'temperature':.1})
    with pytest.raises(ValueError):validate_parameters('openai_chat',['max_tokens','max_completion_tokens'],{'max_tokens':2,'max_completion_tokens':4})

def test_encryption_and_password_hashes():
    encrypted=encrypt('not-a-real-key')
    assert 'not-a-real-key' not in encrypted and decrypt(encrypted)=='not-a-real-key'
    hashed=password_hash('a-long-password')
    assert password_matches('a-long-password',hashed)
    assert not password_matches('wrong',hashed)

@pytest.mark.parametrize('url',['file:///etc/passwd','https://u:p@example.com/v1','https://example.com/v1?api_key=secret','http://host/#secret'])
def test_unsafe_provider_url_rejected(url):
    with pytest.raises(ValueError):validate_url(url)

def test_dag_validation():
    base={'summary':'plan','tasks':[{'key':'a','title':'A','description':'do A','agent_id':'x','depends_on':['b'],'acceptance':'test'},{'key':'b','title':'B','description':'do B','agent_id':'x','depends_on':['a'],'acceptance':'test'}]}
    with pytest.raises(ValidationError):Plan.model_validate(base)
    base['tasks'][0]['depends_on']=[]
    assert len(Plan.model_validate(base).tasks)==2

def test_provider_keys_never_returned(client,configured):
    assert 'secret-key' not in client.get('/api/providers').text
    with session() as s:
        revisions=s.query(ProviderRevision).all()
        assert all('secret-key' not in r.encrypted_key for r in revisions)
        assert decrypt(revisions[0].encrypted_key).startswith('secret-key')

def test_distinct_agents_can_use_distinct_providers(client,configured):
    result=client.post('/api/runs/preview-config',json=run_body(configured))
    assert result.status_code==200,result.text
    snap=result.json()
    selected={a['models'][0]['connection']['base_url'] for a in snap['agents'].values()}
    assert len(selected)==2
    assert 'secret-key' not in result.text

def test_run_snapshot_is_immutable_after_agent_edit(client,configured):
    r=client.post('/api/runs',json=run_body(configured)).json()
    before=client.get('/api/runs/'+r['id']).json()['snapshot']
    a=configured['agents'][0]
    body={k:v for k,v in a.items() if k not in ('id','version')};body['runtime']={'max_steps':99}
    assert client.put('/api/agents/'+a['id'],json=body).status_code==200
    after=client.get('/api/runs/'+r['id']).json()['snapshot']
    assert before==after and before['agents'][a['id']]['runtime']['max_steps']!=99

@pytest.mark.parametrize('permission_profile',['ask','auto','full'])
def test_external_model_needs_project_authorization(client,configured,permission_profile):
    p=client.get('/api/providers').json()[0]
    body={k:p[k] for k in ('name','protocol','base_url','external','enabled')};body['external']=True
    assert client.put('/api/providers/'+p['id'],json=body).status_code==200
    result=client.post('/api/runs/preview-config',json=run_body(configured))
    assert result.status_code==200,result.text  # New projects default to allowing configured providers.
    project=configured['project']
    update={k:v for k,v in project.items() if k not in ('id','owner_id','base_commit','created_at')}
    update.update(allow_external_models=False,execution_mode='local',permission_profile=permission_profile,
                  full_access_confirmed=permission_profile=='full')
    saved=client.put('/api/projects/'+project['id'],json=update)
    assert saved.status_code==200,saved.text
    result=client.post('/api/runs/preview-config',json=run_body(configured))
    assert result.status_code==422 and '外部' in result.text
    update['allow_external_models']=True
    assert client.put('/api/projects/'+project['id'],json=update).status_code==200
    result=client.post('/api/runs/preview-config',json=run_body(configured))
    assert result.status_code==200,result.text

def test_duplicate_idempotency_request_is_not_a_second_run(client,configured):
    body=run_body(configured)
    r1=client.post('/api/runs',json=body,headers={'Idempotency-Key':'create-once'})
    r2=client.post('/api/runs',json=body,headers={'Idempotency-Key':'create-once'})
    assert r1.status_code==202 and r1.json()['id']==r2.json()['id']
    body['title']='Different request'
    assert client.post('/api/runs',json=body,headers={'Idempotency-Key':'create-once'}).status_code==409
