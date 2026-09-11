"""Actual goal gate + persistence tests; scripted responses are not model quality tests."""
import asyncio
import copy
import hashlib
import json
import pytest
from sqlalchemy import select
from conftest import run_body
from forge import engine, providers, tools, workspace
from forge.db import session, Task, Run, Event
from forge.local_runtime import LocalRuntime
from forge.workflow_core import DevelopmentFlow
from forge.schema import OrchestrationInput
from forge.providers import Reply
from test_v05_orchestration import graph, draft, goal_reply


def make_run(client,cfg,criteria=None,**patch):
    g=OrchestrationInput.model_validate(graph(cfg,**patch)).model_dump()
    if criteria is not None:g['criteria']=criteria
    r=client.post('/api/runs',json=draft(cfg,g));assert r.status_code==202,r.text
    rid=r.json()['id'];engine.prepare_run({'run_id':rid})
    for order in range(2):
        engine.prepare_discussion({'run_id':rid,'round':1,'order':order})
        with session() as s:
            t=s.get(Task,engine.task_id(rid,f'discussion_1_{order}'))
            t.status='done';t.result={'summary':'已有实际贡献结果','notes':'测试夹具','ready':True,'open_issues':[]}
    review=engine.prepare_goal_review({'run_id':rid,'cycle':1})
    with session() as s:
        t=s.get(Task,engine.task_id(rid,review['key']));t.status='done';t.result=goal_reply(t.spec).calls[0]['arguments']
    return rid,g,review


def mutate(rid,fn):
    with session() as s:
        t=s.get(Task,engine.task_id(rid,'goal_review_1'))
        out=copy.deepcopy(t.result);fn(out);t.result=out


def check(rid,previous=None):return engine.assess_goal({'run_id':rid,'cycle':1,'checkpoint':previous or {}})


def test_valid_goal_only_eligible_for_human_acceptance(client,configured):
    rid,g,_=make_run(client,configured);result=check(rid)
    assert result['outcome']=='goal_satisfied';assert result['met_count']==2
    assert client.get('/api/runs/'+rid).json()['status']!='completed'
    assert result['code_tests']=='not_executed_in_read_only_discussion'


@pytest.mark.parametrize('case',['stale_digest','missing_criterion','duplicate_criterion','unknown_task','single_author'])
def test_false_evidence_never_converges(client,configured,case):
    rid,_,_=make_run(client,configured)
    def change(x):
        if case=='stale_digest':x['transcript_digest']='0'*64
        elif case=='missing_criterion':x['assessments']=x['assessments'][:1]
        elif case=='duplicate_criterion':x['assessments']=[x['assessments'][0],x['assessments'][0]]
        elif case=='unknown_task':
            for a in x['assessments']:a['evidence']=[{'task_key':'another-run-fake','explanation':'伪造任务证据'}]
        elif case=='single_author':
            for a in x['assessments']:a['evidence']=a['evidence'][:1]
    mutate(rid,change);assert check(rid)['outcome']=='continue'


def test_participant_open_issue_not_overridden_by_assessor(client,configured):
    rid,_,_=make_run(client,configured)
    with session() as s:
        t=s.get(Task,engine.task_id(rid,'discussion_1_0'));t.result={**t.result,'open_issues':['接口契约仍有冲突']}
        r=s.get(Task,engine.task_id(rid,'goal_review_1'));r.base_commit=''  # test refresh of input after contribution changes
    engine.prepare_goal_review({'run_id':rid,'cycle':1})
    with session() as s:
        r=s.get(Task,engine.task_id(rid,'goal_review_1'));r.status='done';r.result=goal_reply(r.spec).calls[0]['arguments']
    out=check(rid);assert out['outcome']=='continue';assert '接口契约仍有冲突' in out['open_issues']


def test_human_decision_blocks_even_met_criteria(client,configured):
    rid,_,_=make_run(client,configured);mutate(rid,lambda r:r.update(needs_human=True))
    assert check(rid)['outcome']=='needs_human'


def test_ready_flags_do_not_decide_completion(client,configured):
    rid,_,_=make_run(client,configured)
    mutate(rid,lambda r:[a.update(status='unmet',evidence=[]) for a in r['assessments']])
    assert check(rid)['outcome']=='continue'


def test_rephrasing_summary_does_not_reset_stagnation(client,configured):
    rid,_,_=make_run(client,configured)
    mutate(rid,lambda r:[a.update(status='unmet',evidence=[]) for a in r['assessments']])
    state=check(rid)
    for i in range(1,4):
        mutate(rid,lambda r:r.update(summary=f'换一种说法，但没有改善 {i}'))
        state=check(rid,state)
    assert state['outcome']=='stalled';assert state['stagnation_count']==3


def test_code_evidence_cannot_be_fabricated(client,configured):
    rid,_,_=make_run(client,configured,[{'id':'source','description':'源码缺陷定位必须有实际读取依据','evidence_type':'code_reference'}])
    mutate(rid,lambda r:r['assessments'][0]['evidence'].append({'task_key':'goal_review_1','explanation':'伪造读取回执','path':'calc.py','sha256':'a'*64}))
    out=check(rid);assert out['outcome']=='continue';assert any('读取回执' in e for e in out['criteria'][0]['errors'])


@pytest.mark.asyncio
async def test_code_evidence_actual_read_receipt_and_stale_hash(client,configured):
    rid,_,_=make_run(client,configured,[{'id':'source','description':'源码缺陷定位必须有实际读取依据','evidence_type':'code_reference'}])
    with session() as s:
        r=s.get(Run,rid);rt=r.snapshot['runtime']
    key='goal_review_1';output=await tools.invoke('read_file',{'path':'calc.py'},['read_file'],workspace.task_root(rid,key),rid,key,'proof-read',rt,'fixture')
    with session() as s:
        t=s.get(Task,engine.task_id(rid,key));t.messages=[{'role':'assistant','calls':[{'id':'proof','name':'read_file','arguments':{'path':'calc.py'}}]},{'role':'tool','call_id':'proof','content':json.dumps(output)}]
    mutate(rid,lambda r:r['assessments'][0]['evidence'].append({'task_key':key,'path':'calc.py','sha256':output['sha256'],'explanation':'实际读取源码定位缺陷'}))
    assert check(rid)['outcome']=='goal_satisfied'
    (workspace.task_root(rid,key)/'calc.py').write_text('changed')
    assert check(rid)['outcome']=='continue'


def test_goal_targets_and_assessor_validated(client,configured):
    ids=[a['id'] for a in configured['agents'][:2]]
    for patch in [{'criteria':[]},{'criteria':[{'id':'same','description':'目标一'},{'id':'same','description':'目标二'}]},{'assessor_id':'not-a-participant'},{'stagnation_checks':1}]:
        with pytest.raises(ValueError):OrchestrationInput.model_validate({'mode':'discussion','participants':ids,**patch})


def test_legacy_round_config_is_explicitly_migrated_not_a_stop_rule():
    g=OrchestrationInput.model_validate({'mode':'discussion','participants':['a','b'],'rounds':12,'min_rounds':12,'stop_when':'max_rounds'}).model_dump()
    assert 'rounds' not in g and 'min_rounds' not in g
    assert g['stop_when']=='goal_satisfied' and g['legacy_stop_migrated']


@pytest.mark.asyncio
async def test_budget_exhaustion_preserves_partial_not_success(client,configured,monkeypatch):
    g=graph(configured);body=draft(configured,g);body['runtime']={'max_model_calls':2}
    rid=client.post('/api/runs',json=body).json()['id']
    async def complete(*args):return Reply('script',[{'id':'finish','name':'finish','arguments':{'summary':'我觉得已经完成','notes':'这里只是脚本','ready':True,'open_issues':[]}}],1,1)
    monkeypatch.setattr(providers,'complete',complete)
    out=await DevelopmentFlow(LocalRuntime(rid,asyncio.Semaphore(2))).run(rid)
    detail=client.get('/api/runs/'+rid).json()
    assert out['status']=='needs_attention' and detail['calls']==2
    assert detail['report']['discussion']['stop_reason']=='budget_exhausted'
    assert len(detail['report']['discussion']['turns'])==2
    assert client.get(f'/api/runs/{rid}/artifacts/discussion.md').status_code==200


@pytest.mark.asyncio
async def test_checkpoint_missing_items_are_input_to_next_cycle(client,configured,monkeypatch):
    g=graph(configured);rid=client.post('/api/runs',json=draft(configured,g)).json()['id'];seen=[]
    async def complete(conn,model,messages,*args):
        spec=json.loads(messages[1]['content'])['task']
        if 'transcript_digest' in spec:return goal_reply(spec,spec['cycle']>=2)
        seen.append(spec)
        return Reply('script',[{'id':'finish','name':'finish','arguments':{'summary':'提交新证据以解决缺项','notes':'夹具','ready':False,'open_issues':[]}}],1,1)
    monkeypatch.setattr(providers,'complete',complete)
    flow=DevelopmentFlow(LocalRuntime(rid,asyncio.Semaphore(2)));flow.run_id=rid;flow.started=flow.runtime.start_time();flow.settings=await flow.call('prepare_run',{})
    result=await flow.discuss(g)
    assert result['stop_reason']=='goal_satisfied' and result['rounds_completed']==2
    assert len(seen)==4 and seen[2]['previous_goal_checkpoint']['met_count']==0
    assert seen[2]['previous_goal_checkpoint']['criteria'][0]['status']=='unmet'
