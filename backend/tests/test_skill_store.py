"""Skill Store: genuine persistence/runtime with explicitly synthetic repository fixtures.
No fixture is represented as a copy of the upstream agency-agents repository.
"""
import copy
import io
import json
import stat
import zipfile

import httpx
import pytest
from sqlalchemy import select
from conftest import run_body
from forge import engine, skills, skill_store as store
from forge.db import session, Skill, SkillRevision, Run, Event

LICENSE = 'MIT License\n\nCopyright (c) 2026 Agent Forge test fixture authors\nPermission is hereby granted.\n'
ROLE = '''---
name: Example Frontend Specialist
description: A synthetic role fixture for testing the store adapter, not an upstream profile.
color: cyan
emoji: 🖥️
vibe: Test-only
---
# Example role fixture
Review UI acceptance evidence before making a claim.\n'''

def native(name='sample-check', text='Read the acceptance criteria.', script=False):
    d = {'SKILL.md': f'---\nname: {name}\ndescription: Evidence-based fixture.\nmetadata:\n  category: testing\n---\n{text}\n', 'references/check.md': 'Check real results.'}
    if script: d['scripts/check.py'] = 'print("test fixture, not executed")\n'
    return d

def zipped(mapping):
    b = io.BytesIO()
    with zipfile.ZipFile(b, 'w', zipfile.ZIP_DEFLATED) as z:
        for k,v in mapping.items(): z.writestr(k, v)
    return b.getvalue()

def source(client):
    r=client.post('/api/skill-store/sources', json={'name':'Test ZIP source','kind':'archive'})
    assert r.status_code==201,r.text
    return r.json()

def get_source(client, source_id):
    return next(s for s in client.get('/api/skill-store/sources').json() if s['id']==source_id)

def upload(client, sid, mapping):
    s=get_source(client,sid)
    r=client.post(f'/api/skill-store/sources/{sid}/archive?expected_version={s["version"]}',content=zipped(mapping),headers={'Content-Type':'application/zip'})
    assert r.status_code==200,r.text
    return r.json()

def get_item(client,sid):
    items=client.get('/api/skill-store/items',params={'source_id':sid}).json()['items']
    endpoint = '/api/skill-store/items/'
    if not items:
        items=client.get('/api/role-templates/items',params={'source_id':sid}).json()['items']
        endpoint = '/api/role-templates/items/'
    assert len(items)==1
    return client.get(endpoint+items[0]['id']).json()

def install(client,item,**kw):
    body={'revision_id':item['revision_id'],'digest':item['digest'],'reviewed_source':True,'expected_version':item.get('local_latest_version')}
    body.update(kw)
    return client.post('/api/skill-store/items/'+item['id']+'/install',json=body)

def approve(client,inst):
    r=inst['skill'];res=client.post('/api/skills/'+r['id']+'/review',json={'version':r['version'],'digest':r['digest'],'action':'approve'})
    assert res.status_code==200,res.text

def bind(client,item,aid):
    a=next(a for a in client.get('/api/agents').json() if a['id']==aid)
    return client.post('/api/skill-store/items/'+item['id']+'/bind',json={'revision_id':item['revision_id'],'agent_id':aid,'expected_agent_version':a['version'],'mode':'on_demand','parameters':{'focus':'evidence'}})

def test_catalog_does_not_install_or_bind_or_network(client,monkeypatch):
    listed=client.get('/api/skill-store/items').json()
    assert listed['total']==3 and all(i['kind']=='skill' and not i['skill_id'] for i in listed['items'])
    s=get_source(client,'agency-agents')
    assert s['repository']=='https://github.com/msitarzewski/agency-agents' and s['count']==0 and not s['last_sync']
    assert client.get('/api/skills').json()==[]
    assert client.get('/api/providers').json()==[] and client.get('/api/models').json()==[]
    assert all(not a['skills'] and not a['model_profile_id'] for a in client.get('/api/agents').json())

@pytest.mark.parametrize('url',['http://github.com/a/b','https://127.0.0.1/repo','https://github.com.evil/a/b','https://a@github.com/a/b','https://github.com/a/b?token=x','https://github.com/a/b/tree/main','https://github.com/../b','file:///tmp/repo'])
def test_source_fetch_ssrf_rejected(client,url):
    r=client.post('/api/skill-store/sources',json={'name':'Bad source','kind':'github','repository':url})
    assert r.status_code==422,r.text

def test_public_custom_source_normalizes_git_and_cas(client):
    body={'name':'Custom skills','kind':'github','repository':'https://github.com/example/skills.git','ref':'release/test'}
    r=client.post('/api/skill-store/sources',json=body);assert r.status_code==201,r.text
    s=r.json();assert s['repository']=='https://github.com/example/skills'
    assert client.post('/api/skill-store/sources',json=body).status_code==409
    body['expected_version']=s['version'];body['ref']='main'
    good=client.put('/api/skill-store/sources/'+s['id'],json=body);assert good.status_code==200,good.text
    assert client.put('/api/skill-store/sources/'+s['id'],json=body).status_code==409

@pytest.mark.parametrize('path',['../escape.md','/root/escape.md','p/../evil.md','p\\evil.md','p//evil.md','C:/evil.md'])
def test_zip_unsafe_paths_rejected(path):
    with pytest.raises(ValueError): store.archive_files(zipped({'root/LICENSE':LICENSE,path:'bad'}))

def test_zip_duplicate_symlink_and_binary_skill():
    b=io.BytesIO()
    with zipfile.ZipFile(b,'w') as z:
        i=zipfile.ZipInfo('root/link.md');i.external_attr=(stat.S_IFLNK|0o777)<<16;z.writestr(i,'../../secret')
    with pytest.raises(ValueError):store.archive_files(b.getvalue())
    b=io.BytesIO()
    with zipfile.ZipFile(b,'w') as z:
        z.writestr('same.md','a')
        with pytest.warns(UserWarning):z.writestr('same.md','b')
    with pytest.raises(ValueError):store.archive_files(b.getvalue())
    files={'root/LICENSE':LICENSE,**{'root/sample-check/'+k:v for k,v in native().items()},'root/sample-check/assets/a.bin':b'\x00\x01'}
    candidates,rejected=store.scan_catalog(store.archive_files(zipped(files)),'test')
    assert not candidates and len(rejected)==1 and '二进制' in rejected[0]['reason']

def test_role_document_preserves_original_and_license_no_skill_or_install_scripts(client):
    upload(client,'agency-agents',{'repo/LICENSE':LICENSE,'repo/engineering/frontend-example.md':ROLE,'repo/scripts/install.sh':'exit 99\n'})
    item=get_item(client,'agency-agents')
    assert item['kind']=='role_template' and item['title']=='Example Frontend Specialist'
    assert item['files']['ROLE.md']==ROLE
    assert item['files']['references/forge-upstream-license.txt']==LICENSE
    assert not item['scripts'] and item['license']=='MIT'
    assert item['origin']['verification']=='user_archive' and item['origin']['commit']==''
    assert 'SKILL.md' not in item['files']
    assert not any('install.sh' in f for f in item['files'])
    assert not item['legacy_skill_id']

def test_native_import_partial_rejection_and_search(client):
    s=source(client)
    files={'repo/LICENSE':LICENSE,**{'repo/sample-check/'+k:v for k,v in native().items()},'repo/bad/SKILL.md':'---\nname: bad\n---\nmissing description'}
    info=upload(client,s['id'],files)
    assert info['summary']['total']==1 and info['summary']['rejected_count']==1
    result=client.get('/api/skill-store/items',params={'q':'sample','category':'testing','kind':'skill','limit':1}).json()
    assert result['total']==1 and result['items'][0]['license']=='MIT'
    assert client.get('/api/skill-store/items?kind=nope').status_code==422

def test_install_requires_confirmation_digest_approval_and_is_idempotent(client):
    i=client.get('/api/skill-store/items').json()['items'][0]
    assert install(client,i,reviewed_source=False).status_code==422
    assert install(client,i,digest='0'*64).status_code==409
    r=install(client,i);assert r.status_code==200,r.text
    data=r.json();assert data['created'] and data['skill']['status']=='draft'
    again=install(client,i).json();assert not again['created'] and again['skill']['id']==data['skill']['id']
    aid=client.get('/api/agents').json()[0]['id']
    assert bind(client,i,aid).status_code==422
    approve(client,data);assert bind(client,i,aid).status_code==200
    assert client.post('/api/skill-store/items/'+i['id']+'/uninstall',json={'expected_version':1}).status_code==409

def test_bind_preserves_suppliers_parameters_skills_and_cas(client,configured):
    item=client.get('/api/skill-store/items').json()['items'][0];data=install(client,item).json();approve(client,data)
    agent=next(a for a in client.get('/api/agents').json() if a['role']=='developer');before=copy.deepcopy(agent)
    r=bind(client,item,agent['id']);assert r.status_code==200,r.text
    after=next(a for a in client.get('/api/agents').json() if a['id']==agent['id'])
    for k in before:
        if k not in ('skills','version'):assert before[k]==after[k]
    b=after['skills'][-1];assert b['allow_scripts'] is False and b['parameters']=={'focus':'evidence'}
    r=client.post('/api/skill-store/items/'+item['id']+'/bind',json={'revision_id':item['revision_id'],'agent_id':agent['id'],'expected_agent_version':before['version']})
    assert r.status_code==409

def test_name_collision_never_adopts_existing_library_skill(client):
    s=source(client);upload(client,s['id'],{'repo/sample-check/'+k:v for k,v in native().items()});item=get_item(client,s['id'])
    original=client.post('/api/skills',json={'files':native()}).json()
    inst=install(client,item).json()['skill']
    assert inst['id']!=original['id'] and inst['name']!=original['name']
    assert inst['version']==1 and inst['status']=='draft'
    assert client.get('/api/skills/'+original['id']).json()['version']==1

def setup_update(client):
    s=source(client);upload(client,s['id'],{'repo/sample-check/'+k:v for k,v in native().items()});v1=get_item(client,s['id'])
    first=install(client,v1).json();approve(client,first)
    upload(client,s['id'],{'repo/sample-check/'+k:v for k,v in native(text='Updated acceptance rule.').items()});v2=get_item(client,s['id'])
    return s,v1,v2,first

def test_update_diff_explicit_install_and_old_revision(client,configured):
    s,v1,v2,first=setup_update(client)
    assert v2['update_available'] and v2['diff'] and len(v2['versions'])==2
    aid=configured['agents'][0]['id'];assert bind(client,v1,aid).status_code==200
    rid=client.post('/api/runs',json=run_body(configured)).json()['id']
    snapshot=client.get('/api/runs/'+rid).json()['snapshot']
    assert install(client,v2,expected_version=3).status_code==409
    new=install(client,v2,expected_version=1);assert new.status_code==200,new.text
    data=new.json();assert data['skill']['version']==2 and data['skill']['status']=='draft'
    assert next(a for a in client.get('/api/agents').json() if a['id']==aid)['skills'][0]['version']==1
    approve(client,data);assert bind(client,v2,aid).status_code==200
    assert bind(client,v1,aid).status_code==200 # explicit rollback to old approved version
    assert client.get('/api/runs/'+rid).json()['snapshot']==snapshot
    assert install(client,v1).json()['skill']['version']==1
    assert client.get('/api/skills/'+first['skill']['id']).json()['version']==2

def test_local_edits_cannot_be_overwritten_by_upstream(client):
    s,v1,v2,first=setup_update(client)
    files=first['skill']['files'];files['references/local.md']='Local customization.'
    assert client.put('/api/skills/'+first['skill']['id'],json={'files':files,'expected_version':1}).status_code==200
    assert install(client,v2,expected_version=2).status_code==409

def test_catalog_resync_idempotent_removed_keeps_approved_version(client):
    s=source(client);m={'repo/sample-check/'+k:v for k,v in native().items()};upload(client,s['id'],m)
    item=get_item(client,s['id']);installed=install(client,item).json();approve(client,installed)
    result=upload(client,s['id'],m);assert result['summary']['unchanged']==1
    assert len(get_item(client,s['id'])['versions'])==1
    upload(client,s['id'],{'repo/other/'+k:v for k,v in native('other').items()})
    old=client.get('/api/skill-store/items/'+item['id']).json();assert not old['available']
    assert client.get('/api/skills/'+installed['skill']['id']).json()['status']=='approved'
    assert install(client,old).status_code==409

def test_failed_sync_preserves_cache_and_clear_provenance(client,monkeypatch):
    upload(client,'agency-agents',{'repo/LICENSE':LICENSE,'repo/engineering/example.md':ROLE})
    before=get_item(client,'agency-agents');src=get_source(client,'agency-agents')
    async def fail(config):raise httpx.ConnectError('synthetic unavailable network')
    monkeypatch.setattr(store,'download_github',fail)
    r=client.post('/api/skill-store/sources/agency-agents/sync',json={'expected_version':src['version']})
    assert r.status_code==502
    after=get_source(client,'agency-agents');assert after['last_error'] and after['version']==src['version']
    assert get_item(client,'agency-agents')['revision_id']==before['revision_id']
    assert client.post('/api/skill-store/sources/agency-agents/sync',json={'expected_version':src['version']-1}).status_code==409

@pytest.mark.asyncio
async def test_public_sync_resolves_commit_before_downloading_no_redirect(monkeypatch):
    seen=[];archive=zipped({'root/LICENSE':LICENSE,'root/engineering/example.md':ROLE});sha='a'*40
    def handler(req):
        seen.append(req)
        if req.url.host=='api.github.com':return httpx.Response(200,json={'sha':sha})
        return httpx.Response(200,content=archive)
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda *args,**kw:original(*args,**kw,transport=httpx.MockTransport(handler)))
    raw,origin=await store.download_github(store.source_config('Example','github','https://github.com/example/skills','main'))
    assert raw==archive and origin['commit']==sha and origin['verification']=='github_commit'
    assert len(seen)==2 and str(seen[1].url).endswith('/zip/'+sha)
    assert all('authorization' not in req.headers for req in seen)

@pytest.mark.asyncio
async def test_redirect_not_followed():
    seen=[]
    def handler(req):seen.append(req);return httpx.Response(302,headers={'location':'http://127.0.0.1/private'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),follow_redirects=False) as c:
        with pytest.raises(ValueError):await store._get_limited(c,'https://api.github.com/safe',100)
    assert len(seen)==1

@pytest.mark.asyncio
async def test_installed_store_skill_enters_real_runtime_and_revoke_applies(client,configured):
    s=source(client);upload(client,s['id'],{'repo/sample-check/'+k:v for k,v in native(script=True).items()});item=get_item(client,s['id'])
    inst=install(client,item).json();approve(client,inst)
    aid=next(a['id'] for a in configured['agents'] if a['role']=='developer');assert bind(client,item,aid).status_code==200
    rid=client.post('/api/runs',json=run_body(configured)).json()['id'];engine.prepare_run({'run_id':rid})
    engine.create_task(rid,'store-proof','develop',aid,'Runtime store proof',{})
    root=engine.ws.task_root(rid,'store-proof')
    with session() as sess:runtime=sess.get(Run,rid).snapshot['runtime']
    loaded=await skills.invoke('activate_skill',{'name':'sample-check'},rid,'store-proof','load-store',root,runtime,'test-only')
    assert 'Read the acceptance' in loaded['instructions']
    resource=await skills.invoke('read_skill_resource',{'name':'sample-check','path':'references/check.md'},rid,'store-proof','read-store',root,runtime,'test-only')
    assert resource['content']=='Check real results.'
    with pytest.raises(ValueError):await skills.invoke('run_skill_script',{'name':'sample-check','path':'scripts/check.py','arguments':[]},rid,'store-proof','script-denied',root,runtime,'test-only')
    r=inst['skill'];client.post('/api/skills/'+r['id']+'/review',json={'version':r['version'],'digest':r['digest'],'action':'revoke'})
    with pytest.raises(ValueError):await skills.invoke('activate_skill',{'name':'sample-check'},rid,'store-proof','revoked',root,runtime,'test-only')

def test_team_publish_local_only_preserve_source_and_review(client):
    item=client.get('/api/skill-store/items').json()['items'][0];inst=install(client,item).json();r=inst['skill']
    body={'skill_id':r['id'],'version':1,'digest':r['digest'],'title':'团队方法包'}
    assert client.post('/api/skill-store/publish',json=body).status_code==422
    approve(client,inst);pub=client.post('/api/skill-store/publish',json=body);assert pub.status_code==201,pub.text
    data=pub.json();assert data['source_id']=='team-library' and not data['skill_id']
    assert data['origin']['verification']=='team_reviewed' and 'references/previous-store-origin.json' in data['files']
    again=client.post('/api/skill-store/publish',json=body).json();assert again['revision_id']==data['revision_id']
    installed=install(client,data).json();assert installed['skill']['id']!=r['id'] and installed['skill']['status']=='draft'

def test_uninstall_retains_history_and_blocks_active_snapshot(client,configured):
    item=client.get('/api/skill-store/items').json()['items'][0];inst=install(client,item).json();approve(client,inst)
    aid=configured['agents'][0]['id'];assert bind(client,item,aid).status_code==200
    rid=client.post('/api/runs',json=run_body(configured)).json()['id']
    agent=next(a for a in client.get('/api/agents').json() if a['id']==aid)
    body={k:v for k,v in agent.items() if k not in ('id','version')};body['skills']=[]
    assert client.put('/api/agents/'+aid,json=body).status_code==200
    path='/api/skill-store/items/'+item['id']+'/uninstall'
    assert client.post(path,json={'expected_version':1}).status_code==409
    with session() as s:s.get(Run,rid).status='cancelled'
    r=client.post(path,json={'expected_version':1});assert r.status_code==200,r.text
    assert r.json()['history_preserved']
    assert client.get('/api/skills/'+inst['skill']['id']).json()['enabled'] is False
    assert bind(client,item,aid).status_code==422

def test_disabled_source_keeps_local_bindings_but_blocks_new_installs(client):
    item=client.get('/api/skill-store/items').json()['items'][0];inst=install(client,item).json();approve(client,inst)
    src=get_source(client,item['source_id'])
    r=client.post('/api/skill-store/sources/'+src['id']+'/enabled',json={'expected_version':src['version'],'enabled':False});assert r.status_code==200,r.text
    assert install(client,item).status_code==409
    aid=client.get('/api/agents').json()[0]['id'];assert bind(client,item,aid).status_code==200

def test_store_auth_readonly_no_writes(client):
    from forge.db import User,uid
    from forge.security import password_hash
    with session() as s:s.add(User(id=uid(),username='store-viewer',password_hash=password_hash('Viewer-Passw0rd-test'),role='viewer'))
    client.post('/api/auth/logout')
    assert client.get('/api/skill-store/items').status_code==401
    assert client.post('/api/auth/login',json={'username':'store-viewer','password':'Viewer-Passw0rd-test'}).status_code==200
    assert client.get('/api/skill-store/items').status_code==200
    assert client.post('/api/skill-store/sources',json={'name':'Unauthorised','kind':'archive'}).status_code==403
    i=client.get('/api/skill-store/items').json()['items'][0];assert install(client,i).status_code==403


def test_install_conflict_rollback_returns_409(client,monkeypatch):
    from sqlalchemy.exc import IntegrityError
    def conflict(*args,**kw):raise IntegrityError('fixture duplicate',{},Exception('synthetic conflict'))
    item=client.get('/api/skill-store/items').json()['items'][0]
    monkeypatch.setattr(store,'install',conflict)
    assert install(client,item).status_code==409
    assert client.get('/api/skills').json()==[]

def test_role_tool_declaration_visible_but_not_granted():
    role=ROLE.replace('color: cyan','allowed-tools: Bash(git:*) Read\ncolor: cyan')
    candidates,_=store.scan_catalog({'engineering/example.md':role,'LICENSE':LICENSE},'agency-agents')
    c=candidates[0]
    assert c['details']['declared_tools']=='Bash(git:*) Read'
    assert 'SKILL.md' not in c['files']
    assert c['files']['ROLE.md']==role
