import asyncio
import json
import pytest
from sqlalchemy import select
from forge import engine
from forge.collaboration import send_message,pending_messages,mark_included
from forge.db import session,Run,Task,AgentMessage,Event,ActivityJournal
from forge.schema import TOOLS_BY_ROLE
from forge.local_runtime import LocalRuntime
from conftest import run_body

def prepare(client,configured):
    r=client.post('/api/runs',json=run_body(configured));assert r.status_code==202,r.text
    rid=r.json()['id'];engine.prepare_run({'run_id':rid})
    run=engine.load(rid);recipient=next(a['id'] for a in run.snapshot['agents'].values() if a['role']=='developer')
    return rid,recipient

def test_message_persisted_idempotent_and_event_once(client,configured):
    rid,to=prepare(client,configured)
    a=send_message(rid,'plan','operation-1',to,'交接内容');b=send_message(rid,'plan','operation-1',to,'交接内容')
    assert a['message_id']==b['message_id']
    with session() as s:
        assert len(list(s.scalars(select(AgentMessage))))==1
        assert len(list(s.scalars(select(Event).where(Event.kind=='agent.message'))))==1
    assert pending_messages(rid,to)[0]['body']=='交接内容'

@pytest.mark.parametrize('body',['','x'*3001])
def test_message_length_limits(client,configured,body):
    rid,to=prepare(client,configured)
    with pytest.raises(ValueError):send_message(rid,'plan','invalid',to,body)

def test_message_self_and_nonmember_denied(client,configured):
    rid,to=prepare(client,configured);run=engine.load(rid)
    for target in [run.snapshot['planner_id'],'not-a-member']:
        with pytest.raises(ValueError):send_message(rid,'plan','forbidden-'+target,target,'test')

def test_message_identity_reuse_cannot_change_payload(client,configured):
    rid,to=prepare(client,configured);send_message(rid,'plan','same',to,'original')
    with pytest.raises(ValueError):send_message(rid,'plan','same',to,'changed')

def test_message_global_limit_enforced(client,configured):
    rid,to=prepare(client,configured)
    with session() as s:
        run=s.get(Run,rid);snapshot=json.loads(json.dumps(run.snapshot));snapshot['runtime']['max_agent_messages']=1;run.snapshot=snapshot
    send_message(rid,'plan','one',to,'one')
    with pytest.raises(ValueError):send_message(rid,'plan','two',to,'two')

def test_message_terminal_run_denied(client,configured):
    rid,to=prepare(client,configured);engine.set_stage({'run_id':rid,'status':'completed'})
    with pytest.raises(ValueError):send_message(rid,'plan','late',to,'late')

def test_message_inbox_scoped_to_run_and_recipient(client,configured):
    rid,to=prepare(client,configured);other,_=prepare(client,configured)
    send_message(rid,'plan','one',to,'private-to-this-run')
    assert not pending_messages(other,to)
    assert not pending_messages(rid,engine.load(rid).snapshot['reviewer_id'])

def test_delivery_records_inclusion_not_fake_reply(client,configured):
    rid,to=prepare(client,configured);send_message(rid,'plan','one',to,'hello');items=pending_messages(rid,to)
    mark_included(rid,to,'fix',items);assert pending_messages(rid,to)==[]
    with session() as s:
        rows=list(s.scalars(select(Event).where(Event.kind=='agent.message.delivered')));assert len(rows)==1
        assert rows[0].data['message_id']==items[0]['message_id']

@pytest.mark.parametrize('role',['planner','developer','tester','reviewer'])
def test_each_role_has_bounded_message_tool(role):assert 'send_message' in TOOLS_BY_ROLE[role]

@pytest.mark.asyncio
async def test_local_journal_reuses_completed_activity(client,configured,monkeypatch):
    rid,to=prepare(client,configured);calls=[]
    def fake(data):calls.append(data);return {'key':data['key']}
    monkeypatch.setattr(engine,'prepare_task',fake)
    args={'run_id':rid,'key':'once'}
    a=LocalRuntime(rid,asyncio.Semaphore(2));assert await a.execute_activity('prepare_task',args,30)=={'key':'once'}
    b=LocalRuntime(rid,asyncio.Semaphore(2));assert await b.execute_activity('prepare_task',args,30)=={'key':'once'}
    assert len(calls)==1

@pytest.mark.asyncio
async def test_local_journal_never_replays_unknown_result(client,configured,monkeypatch):
    rid,to=prepare(client,configured);calls=[]
    def failed(data):calls.append(data);raise RuntimeError('uncertain')
    monkeypatch.setattr(engine,'prepare_task',failed)
    args={'run_id':rid,'key':'uncertain'}
    with pytest.raises(RuntimeError):await LocalRuntime(rid,asyncio.Semaphore(1)).execute_activity('prepare_task',args,30)
    with pytest.raises(RuntimeError,match='先前执行结果未知'):await LocalRuntime(rid,asyncio.Semaphore(1)).execute_activity('prepare_task',args,30)
    assert len(calls)==1

@pytest.mark.asyncio
async def test_local_runtime_rejects_unregistered_activity(client,configured):
    rid,to=prepare(client,configured)
    with pytest.raises(ValueError):await LocalRuntime(rid,asyncio.Semaphore(1)).execute_activity('shell_on_host',{'run_id':rid},30)

@pytest.mark.asyncio
async def test_local_global_activity_semaphore(client,configured,monkeypatch):
    rid,to=prepare(client,configured);active=0;maximum=0
    async def fake(data):
        nonlocal active,maximum
        active+=1;maximum=max(maximum,active);await asyncio.sleep(.04);active-=1;return {'key':data['key']}
    monkeypatch.setattr(engine,'prepare_task',fake);sem=asyncio.Semaphore(2);runtime=LocalRuntime(rid,sem)
    await asyncio.gather(*(runtime.execute_activity('prepare_task',{'run_id':rid,'key':str(i)},30) for i in range(5)))
    assert maximum==2
