"""Activity-level integration tests. Model responses are scripted fixtures.
The broker fixture executes ONE fixed, trusted unittest command, not arbitrary agent shell.
These tests do NOT constitute live provider, Docker/gVisor or Temporal integration tests.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
import sys
import zipfile
import pytest
from forge import engine,tools,workspace as ws
from forge.providers import Reply
from forge.db import session,Run,Task,Step,Provider,Event
from conftest import run_body


def test_internal_legacy_activity_pipeline_with_scripted_model_and_real_fixture_tests(client,configured,monkeypatch):
    created=client.post('/api/runs',json=run_body(configured)).json()
    rid=created['id']
    setup=engine.prepare_run({'run_id':rid})
    # The public API creates only autonomous runs. Explicitly construct the old
    # internal activity fixture to retain coverage of the remaining legacy code;
    # this is not a supported new-run workflow or autonomous end-to-end test.
    legacy_owner=created['entry_agent_id']
    engine.create_task(rid,'plan','plan',legacy_owner,'Internal legacy plan fixture',{})
    developer=next(a['id'] for a in configured['agents'] if a['role']=='developer')
    observed_providers=set()
    counter=0
    async def scripted_model(connection,model,messages,definitions,key,timeout):
        nonlocal counter
        counter+=1
        observed_providers.add(connection['base_url'])
        done_schema=definitions[-1]['parameters']
        properties=done_schema.get('properties',{})
        tool_messages=[m for m in messages if m['role']=='tool']
        def call(name,args):return Reply('',[{'id':f'call{counter}','name':name,'arguments':args}],10,5)
        if 'tasks' in properties:
            if not tool_messages:return call('read_file',{'path':'calc.py'})
            return call('finish',{'summary':'Fix addition and run the existing regression checks.','tasks':[{'key':'fix','title':'Fix add','description':'Change subtraction to addition in calc.py.','agent_id':developer,'depends_on':[],'acceptance':'Both operands are added and the project test succeeds.'}]})
        if 'verdict' in properties:
            if not tool_messages:return call('read_file',{'path':'calc.py'})
            assert 'a + b' in tool_messages[-1]['content']
            return call('finish',{'summary':'Inspected corrected source and actual test evidence.','verdict':'pass','issues':[]})
        if not tool_messages:return call('read_file',{'path':'calc.py'})
        if len(tool_messages)==1:return call('write_file',{'path':'calc.py','content':'def add(a, b):\n    return a + b\n'})
        return call('finish',{'summary':'Changed subtraction to addition.','notes':'Project check runs after integration.'})
    monkeypatch.setattr(engine.providers,'complete',scripted_model)
    async def trusted_check_fixture(run_id,task_key,operation,text,runtime,image):
        assert text=='python -m unittest discover -s tests'
        result=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests'],cwd=ws.task_root(run_id,task_key),capture_output=True,text=True,timeout=10)
        return {'exit_code':result.returncode,'output':result.stdout+result.stderr,'truncated':False,'timed_out':False}
    monkeypatch.setattr(tools,'command',trusted_check_fixture)
    async def finish_task(key):
        for i in range(10):
            result=await engine.turn({'run_id':rid,'key':key,'index':i})
            if result['done']:return result
        raise AssertionError('fixture did not finish')
    asyncio.run(finish_task('plan'))
    plan=engine.save_plan({'run_id':rid})
    engine.set_stage({'run_id':rid,'status':'awaiting_approval'})
    approval=client.post(f'/api/runs/{rid}/decision/plan',json={'digest':plan['digest'],'approved':True})
    assert approval.status_code==202
    engine.prepare_task({'run_id':rid,'key':'fix'})
    asyncio.run(finish_task('fix'))
    engine.integrate_task({'run_id':rid,'key':'fix'})
    checks=asyncio.run(engine.check_project({'run_id':rid,'round':0}))
    assert checks['verification']=='passed' and 'OK' in checks['commands'][0]['output']
    engine.prepare_review({'run_id':rid,'round':0,'checks':checks})
    review=asyncio.run(finish_task('review_0'))['result']
    artifacts=engine.publish({'run_id':rid,'checks':checks,'review':review})
    assert len(artifacts['digest'])==64
    engine.set_stage({'run_id':rid,'status':'awaiting_acceptance'})
    accepted=client.post(f'/api/runs/{rid}/decision/acceptance',json={'digest':artifacts['digest'],'approved':True})
    assert accepted.status_code==202
    # Temporal would apply the accepted decision. This test explicitly stops at activity/API boundaries.
    report=client.get(f'/api/runs/{rid}/artifacts/report.json')
    assert report.status_code==200 and json.loads(report.content)['checks']['verification']=='passed'
    patch=client.get(f'/api/runs/{rid}/artifacts/changes.patch').content
    assert b'+    return a + b' in patch
    with zipfile.ZipFile(ws.run_root(rid)/'artifacts/source.zip') as archive:
        assert b'a + b' in archive.read('calc.py')
        assert not any(name.startswith('.git') or name=='.env' for name in archive.namelist())
    assert 'a - b' in (configured['source']/'calc.py').read_text()
    assert len(observed_providers)==2
    with session() as s:
        r=s.get(Run,rid)
        assert r.calls==7 and r.input_tokens==70 and r.output_tokens==35
        assert all('secret-key' not in json.dumps(e.data) for e in s.query(Event).all())


def test_uncertain_turn_is_not_replayed(client,configured,monkeypatch):
    run=client.post('/api/runs',json=run_body(configured)).json();rid=run['id']
    engine.prepare_run({'run_id':rid})
    with session() as s:s.add(Step(id=f'{rid}:plan:turn:0',state='started'))
    async def must_not_call(*args,**kwargs):raise AssertionError('Should not invoke provider')
    monkeypatch.setattr(engine.providers,'complete',must_not_call)
    with pytest.raises(RuntimeError,match='不自动重放'):asyncio.run(engine.turn({'run_id':rid,'key':'plan','index':0}))
    with session() as s:assert s.get(Run,rid).calls==0


def test_parallel_patch_conflict_preserves_both_workspaces(client,configured):
    rid=client.post('/api/runs',json=run_body(configured)).json()['id']
    setup=engine.prepare_run({'run_id':rid})
    developer=next(a['id'] for a in configured['agents'] if a['role']=='developer')
    for key,body in [('first','a + b'),('second','a * b')]:
        engine.create_task(rid,key,'develop',developer,key,{})
        (ws.task_root(rid,key)/'calc.py').write_text(f'def add(a, b):\n    return {body}\n')
    engine.integrate_task({'run_id':rid,'key':'first'})
    # Idempotent integration does not make another commit.
    head=ws.git(ws.run_root(rid)/'integration','rev-parse','HEAD').stdout
    engine.integrate_task({'run_id':rid,'key':'first'})
    assert head==ws.git(ws.run_root(rid)/'integration','rev-parse','HEAD').stdout
    with pytest.raises(ValueError,match='冲突'):engine.integrate_task({'run_id':rid,'key':'second'})
    assert 'a + b' in (ws.run_root(rid)/'integration/calc.py').read_text()
    assert 'a * b' in (ws.task_root(rid,'second')/'calc.py').read_text()


def test_concurrent_budget_reservation_cannot_exceed_call_limit(client,configured):
    rid=client.post('/api/runs',json=run_body(configured,runtime={'max_model_calls':4})).json()['id']
    def reserve(_):
        try:engine.reserve_call(rid);return True
        except ValueError:return False
    with ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(reserve,range(20)))
    assert sum(results)==4
    with session() as s:assert s.get(Run,rid).calls==4


def test_no_test_commands_is_unverified_not_passed(client,configured):
    rid=client.post('/api/runs',json=run_body(configured)).json()['id']
    with session() as s:
        run=s.get(Run,rid);snap=dict(run.snapshot);snap['project']=dict(snap['project']);snap['project']['check_commands']=[];run.snapshot=snap
    engine.prepare_run({'run_id':rid})
    result=asyncio.run(engine.check_project({'run_id':rid,'round':0}))
    assert result['verification']=='unverified' and not result['commands']


def test_live_provider_revocation_overrides_frozen_config(client,configured,monkeypatch):
    rid=client.post('/api/runs',json=run_body(configured)).json()['id']
    engine.prepare_run({'run_id':rid})
    from forge.autonomy import autonomy_tick
    key=autonomy_tick({'run_id':rid})['runnable'][0]['key']
    calls=[]
    async def forbidden(*args,**kwargs):
        calls.append(True)
        raise AssertionError('Revoked provider must never receive model inference')
    monkeypatch.setattr(engine.providers,'complete',forbidden)
    with session() as s:
        for p in s.query(Provider):p.enabled=False
    with pytest.raises(ValueError,match='停用'):asyncio.run(engine.turn({'run_id':rid,'key':key,'index':0}))
    assert not calls
    with session() as s: assert s.get(Run,rid).calls == 0
