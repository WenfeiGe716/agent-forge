import asyncio
import os
from pathlib import Path
import pytest
from forge.workspace import safe_path,import_project,run_root,task_root,copy_repo,git,integrate
from forge.config import settings
from forge import tools
from forge.schema import DEFAULT_RUNTIME
from forge.db import session,Run,Task,Outbox,Event
from forge.engine import set_stage,task_id
from conftest import run_body

@pytest.mark.parametrize('path',['../outside','/etc/passwd','.git/config','.env','keys/server.pem','a/../../outside','a\\b'])
def test_path_boundaries(tmp_path,path):
    with pytest.raises(ValueError):safe_path(tmp_path,path,allow_missing=True)

def test_symbolic_and_hard_links_denied(tmp_path):
    (tmp_path/'file').write_text('value')
    (tmp_path/'link').symlink_to(tmp_path/'file')
    with pytest.raises(ValueError):safe_path(tmp_path,'link')
    os.link(tmp_path/'file',tmp_path/'hard')
    with pytest.raises(ValueError):safe_path(tmp_path,'hard')

def test_original_directory_is_not_modified_and_secrets_excluded(client,configured):
    project=configured['project'];root=settings.data_root/'projects'/project['id']
    assert (configured['source']/'calc.py').read_text().endswith('a - b\n')
    assert (configured['source']/'.env').exists() and not (root/'.env').exists()
    (root/'calc.py').write_text('CHANGED WORK COPY')
    assert 'CHANGED' not in (configured['source']/'calc.py').read_text()

def test_forbidden_tool_is_blocked(tmp_path):
    with pytest.raises(ValueError):asyncio.run(tools.invoke('write_file',{'path':'a','content':'x'},['read_file'],tmp_path,'a'*32,'task','op',DEFAULT_RUNTIME,'test'))
    assert not (tmp_path/'a').exists()

def test_read_write_tools_are_real_and_bounded(tmp_path):
    result=asyncio.run(tools.invoke('write_file',{'path':'src/a.py','content':'print(1)\n'},['write_file'],tmp_path,'a'*32,'task','op',DEFAULT_RUNTIME,'test'))
    assert result['bytes']==9 and (tmp_path/'src/a.py').read_text()=='print(1)\n'
    output=asyncio.run(tools.invoke('read_file',{'path':'src/a.py'},['read_file'],tmp_path,'a'*32,'task','op2',DEFAULT_RUNTIME,'test'))
    assert output['content']=='print(1)\n'

def test_csrf_header_and_cross_origin(client):
    assert client.post('/api/auth/logout',headers={'X-Forge-Request':'0'}).status_code==403
    assert client.post('/api/auth/logout',headers={'Origin':'https://not-authorized.example'}).status_code==403

def test_viewer_and_project_acl(client,configured):
    viewer=client.post('/api/users',json={'username':'viewer','password':'viewer-password-123','role':'viewer'}).json()
    project_id=configured['project']['id']
    run=client.post('/api/runs',json=run_body(configured)).json()
    assert client.post('/api/auth/logout').status_code==200
    assert client.post('/api/auth/login',json={'username':'viewer','password':'viewer-password-123'}).status_code==200
    assert client.get('/api/projects').json()==[]
    assert client.get('/api/runs/'+run['id']).status_code==404
    assert client.get('/api/runs/'+run['id']+'/events').status_code==404
    assert client.get('/api/runs/'+run['id']+'/artifacts/report.json').status_code==404
    assert client.post('/api/runs',json=run_body(configured)).status_code==403
    assert client.get('/api/users').status_code==403

def test_approval_must_match_state_and_digest(client,configured):
    run=client.post('/api/runs',json=run_body(configured)).json()
    path='/api/runs/'+run['id']+'/decision/plan'
    assert client.post(path,json={'digest':'wrong','approved':True}).status_code==409
    with session() as s:
        r=s.get(Run,run['id']);r.status='awaiting_approval';r.plan_digest='correct'
    assert client.post(path,json={'digest':'wrong','approved':True}).status_code==409
    ok=client.post(path,json={'digest':'correct','approved':True})
    assert ok.status_code==202
    again=client.post(path,json={'digest':'correct','approved':True})
    assert again.json()['command_id']==ok.json()['command_id']
    assert client.post(path,json={'digest':'correct','approved':False}).status_code==409

def test_unverified_acceptance_requires_explicit_reason(client,configured):
    run=client.post('/api/runs',json=run_body(configured)).json()
    with session() as s:
        r=s.get(Run,run['id']);r.status='awaiting_acceptance';r.artifact_digest='artifact';r.report={'checks':{'verification':'unverified'}}
    path='/api/runs/'+run['id']+'/decision/acceptance'
    assert client.post(path,json={'digest':'artifact','approved':True}).status_code==422
    assert client.post(path,json={'digest':'artifact','approved':True,'note':'Manually reviewed on target system.'}).status_code==202

def test_needs_attention_does_not_leave_fake_running_agents(client,configured):
    run=client.post('/api/runs',json=run_body(configured)).json()
    with session() as s:
        s.add(Task(id=task_id(run['id'],'test'),run_id=run['id'],key='test',kind='develop',agent_id=configured['agents'][1]['id'],title='Test',spec={},status='running'))
    set_stage({'run_id':run['id'],'status':'needs_attention','error':'Stopped'})
    with session() as s:assert s.get(Task,task_id(run['id'],'test')).status=='needs_attention'

def test_event_cursor_has_no_duplicate_records(client,configured):
    run=client.post('/api/runs',json=run_body(configured)).json()
    path='/api/runs/'+run['id']+'/events'
    first=client.get(path).json()['events']
    assert first and client.get(path+'?after='+str(first[-1]['id'])).json()['events']==[]
