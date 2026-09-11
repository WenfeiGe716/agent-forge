"""Explicit SCRIPTED HTTP fixtures, NOT LLMs and NOT a general host executor.
The only command runs verified, pinned source bytes in a new temporary directory.
"""
import asyncio,hashlib,json,os,subprocess,sys,tempfile,time,uuid
from pathlib import Path
from fastapi import FastAPI,HTTPException,Request
if os.environ.get('FORGE_VALIDATION_MODE')!='true':raise RuntimeError('FORGE_VALIDATION_MODE=true required')
app=FastAPI(title='SCRIPTED validation fixtures - NOT A MODEL')
ROOT=Path(os.environ['DATA_ROOT']).resolve();LOG=Path(os.environ['FIXTURE_LOG']);LOG.parent.mkdir(parents=True,exist_ok=True)
BAD='def add(a, b):\n    return a - b\n';GOOD='def add(a, b):\n    return a + b\n'
TEST='import unittest\nfrom calc import add\nclass TestCalc(unittest.TestCase):\n    def test_add(self): self.assertEqual(add(2,3),5)\n'
EDGE='import unittest\nfrom calc import add\nclass TestEdges(unittest.TestCase):\n    def test_negative(self): self.assertEqual(add(-2,-3),-5)\n    def test_zero(self): self.assertEqual(add(0,0),0)\n'
CACHE={}
def log(event):
    with LOG.open('a') as f:f.write(json.dumps({'time':time.time(),**event},ensure_ascii=False)+'\n')
def texts(message):
    c=message.get('content','');return [c] if isinstance(c,str) else [b.get('text','') for b in c if b.get('type')=='text']
def script(body):
    messages=body['messages'];context=None
    for m in messages:
        for text in texts(m):
            try:
                data=json.loads(text)
                if isinstance(data,dict) and 'request' in data and 'task' in data:context=data;break
            except (ValueError,TypeError):pass
        if context:break
    if context is None:return 'SCRIPTED FIXTURE OK',[]
    ids={a['role']:a['id'] for a in context['collaboration_team']};count=sum(m['role']=='assistant' for m in messages)
    tool=next(t.get('function',t) for t in body['tools'] if t.get('function',t).get('name')=='finish')
    props=tool.get('parameters',tool.get('input_schema',{})).get('properties',{})
    kind='intake' if 'participants' in props and 'agenda' in props else 'action_plan' if 'tasks' in props and context['task'].get('kind')=='action_plan' else 'goal_review' if 'transcript_digest' in props else 'discussion' if 'ready' in props else 'plan' if 'tasks' in props else 'review' if 'verdict' in props else context['task'].get('key','rework')
    log({'protocol':'anthropic' if 'system' in body else 'openai','model':body['model'],'kind':kind,'turn':count,'inbox_received':any('collaboration_messages' in t for m in messages for t in texts(m))})
    def call(name,args):return {'id':'fixture_'+uuid.uuid4().hex[:18],'name':name,'arguments':args}
    def finish(s):return call('finish',{'summary':s,'notes':'脚本化协议验证，不是模型能力评价'})
    if kind=='intake':
        allowed=context['task']['allowed_participants'];host=next((a['id'] for a in context['collaboration_team'] if a['role']=='planner' and a['id'] in allowed),allowed[0])
        catalog=context.get('skill_catalog',context.get('available_skills',[]));skill=catalog[0] if catalog else None
        ledger=[t for m in messages for t in texts(m) if 'human_guidance' in t or 'human_decisions' in t or 'human_instructions' in t]
        log({'kind':'v07-human-context','turn':count,'has_answer':any('api-first' in t for t in ledger),'has_instruction':any('V07补充' in t for t in ledger),'catalog':catalog})
        stages=[]
        if skill:stages += [[call('activate_skill',{'name':skill['name']})],[call('read_skill_resource',{'name':skill['name'],'path':'references/checklist.md'})]]
        stages += [[call('request_decision',{'question':'本次优先交付接口能力还是界面？','context':'这是脚本化验证问题。选择结果将进入原步骤后续上下文，不代表模型自行推理。','options':[{'id':'api-first','label':'接口优先，保持现有函数签名'},{'id':'ui-first','label':'界面优先，先完善交互'}],'allow_text':True})],[call('finish',{'summary':'已接收用户决定，召集必要成员明确方案与验收。','participants':allowed,'agenda':'围绕用户已经确认的交付方向，形成可执行行动项和验证条件。'})]]
    elif kind=='action_plan':
        people=context['task'].get('participants',list(ids.values()))
        stages=[[call('finish',{'summary':'将会议决议整理为建议行动项，等待人工接受，尚未修改代码。','tasks':[{'key':'followup','title':'落实会议约定的接口修复','description':'基于已确认的需求与会议证据实现后续修复。','agent_id':people[0],'depends_on':[],'acceptance':'提交可验证的修复与测试证据'}]})]]
    elif kind=='discussion':
        prior=context['task'].get('previous_contributions',[]); round_no=context['task']['round']
        log({'kind':'discussion-context','round':round_no,'order':context['task']['order'],'prior_keys':[t['key'] for t in prior]})
        stages=[[call('finish',{'summary':f'第{round_no}轮：已读取{len(prior)}份先前发言，明确需求边界和验证条件。','notes':'脚本化HTTP服务，用于验证顺序、上下文与停止条件，不是真实推理。','ready':True,'open_issues':(['边界方案缺少确认'] if round_no==1 and '[immediate]' not in context['request'] else [])})]]
    elif kind=='goal_review':
        spec=context['task'];transcript=spec['transcript'];cycle=spec['cycle'];request=context['request']
        log({'kind':'goal-context','cycle':cycle,'criteria':spec['criteria'],'transcript_digest':spec['transcript_digest']})
        met=('[immediate]' in request or cycle>=2) and '[stalled]' not in request
        code=any(c['evidence_type']=='code_reference' for c in spec['criteria'])
        refs=[{'task_key':t['key'],'explanation':'关联已完成的实际贡献，脚本化证据示例'} for t in transcript[-len(context['collaboration_team']):]]
        if code:refs.append({'task_key':f'goal_review_{cycle}','explanation':'实际工具读取calc.py并核对哈希','path':'calc.py','sha256':hashlib.sha256(BAD.encode()).hexdigest()})
        if '[badref]' in request:refs=[{'task_key':'forged-task','explanation':'故意制造的无效引用测试'}]
        result={'transcript_digest':spec['transcript_digest'],'summary':'目标复核夹具：检查目标、来源和未解决问题；不代表真实LLM能力',
          'assessments':[{'criterion_id':c['id'],'status':'met' if met else 'unmet','rationale':'已具备候选证据' if met else '尚缺明确的输入边界证据','evidence':refs if met else []} for c in spec['criteria']],
          'open_issues':[], 'needs_human':'[human]' in request}
        stages=([[call('read_file',{'path':'calc.py'})]] if code else [])+[[call('finish',result)]]
    elif kind=='plan':
        stages=[[call('list_files',{}),call('read_file',{'path':'calc.py'})],[call('send_message',{'recipient_id':ids['developer'],'body':'请修复add减法错误，保持签名，并向测试成员交接变更。'})],[call('finish',{'summary':'修复加法、并行维护文档，随后补充边界测试，最后独立审查。','tasks':[
        {'key':'fix_add','title':'修复加法函数','description':'检查calc.py，将错误减法改为加法。','agent_id':ids['developer'],'depends_on':[],'acceptance':'add(2,3)==5'},
        {'key':'document','title':'整理验收说明','description':'写README.fixture.md，说明行为与验收命令，不修改calc.py。','agent_id':ids['tester'],'depends_on':[],'acceptance':'文档包含验收命令'},
        {'key':'edge_tests','title':'补充边界测试','description':'依据已集成代码补充tests/test_edges.py。','agent_id':ids['tester'],'depends_on':['fix_add','document'],'acceptance':'负数与零值测试通过'}]})]]
    elif kind=='fix_add':stages=[[call('read_file',{'path':'calc.py'})],[call('write_file',{'path':'calc.py','content':GOOD})],[call('send_message',{'recipient_id':ids['tester'],'body':'add已改为a+b，请读取集成后的源码并补充负数和零值测试。'})],[finish('已修复加法并向测试Agent发送交接消息')]]
    elif kind=='document':stages=[[call('write_file',{'path':'README.fixture.md','content':'# 验收说明\nadd返回两数之和。\npython -m unittest discover -s tests\n'})],[call('send_message',{'recipient_id':ids['developer'],'body':'验收文档已补充，没有修改calc.py，避免并发冲突。'})],[finish('已完成独立文档变更')]]
    elif kind=='edge_tests':stages=[[call('read_file',{'path':'calc.py'})],[call('write_file',{'path':'tests/test_edges.py','content':EDGE})],[call('send_message',{'recipient_id':ids['reviewer'],'body':'已补充负数、零值测试，请结合平台自动测试结果独立审查。'})],[finish('已补充负数和零值回归测试')]]
    elif kind=='review':stages=[[call('read_file',{'path':'calc.py'}),call('read_file',{'path':'tests/test_edges.py'})],[call('finish',{'summary':'已读取实现与测试，并核对平台提供的验收报告。结论由脚本化验证服务生成。','verdict':'pass','issues':[]})]]
    else:raise HTTPException(422,'不支持的夹具任务')
    if count>=len(stages):raise HTTPException(422,'重复或超出预期的模型轮次')
    return f'协议验证：{kind} / 第{count+1}轮',stages[count]
def fixture_delay(body):
    # Deliberately lengthen ONE scripted reply for real DOM/WebGL meeting evidence.
    # This never runs in the actual platform or benchmarks real LLM latency.
    if 'SCENE_WALKTHROUGH_ONLY' in json.dumps(body,ensure_ascii=False) and not any(m['role']=='assistant' for m in body['messages']):
        for m in body['messages']:
            for t in texts(m):
                try:
                    spec=json.loads(t).get('task',{})
                    if spec.get('round')==1 and spec.get('order')==0:return 24
                except (ValueError,AttributeError):pass
    return float(os.environ.get('FIXTURE_RESPONSE_DELAY','.35'))
@app.post('/openai/v1/chat/completions')
async def openai(r:Request):
    body=await r.json();await asyncio.sleep(fixture_delay(body));text,calls=script(body)
    return {'choices':[{'message':{'role':'assistant','content':text,'tool_calls':[{'id':c['id'],'type':'function','function':{'name':c['name'],'arguments':json.dumps(c['arguments'],ensure_ascii=False)}} for c in calls]}}],'usage':{'prompt_tokens':100,'completion_tokens':40}}
@app.post('/anthropic/v1/messages')
async def anthropic(r:Request):
    body=await r.json();await asyncio.sleep(fixture_delay(body));text,calls=script(body)
    return {'content':[{'type':'text','text':text}]+[{'type':'tool_use','id':c['id'],'name':c['name'],'input':c['arguments']} for c in calls],'usage':{'input_tokens':110,'output_tokens':45}}
@app.post('/execute')
async def execute(r:Request):
    if r.headers.get('x-sandbox-token')!=os.environ['SANDBOX_TOKEN']:raise HTTPException(403,'拒绝访问')
    body=await r.json()
    if body.get('command')!='python -m unittest discover -s tests':raise HTTPException(403,'只允许唯一固定可信夹具命令，禁止任意宿主机执行')
    if not all(isinstance(body.get(k),str) and body[k].replace('_','').replace('-','').isalnum() for k in ['run_id','task_key']):raise HTTPException(403,'无效路径')
    op=body['operation_id'];fingerprint=hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest()
    if op in CACHE:
        if CACHE[op][0]!=fingerprint:raise HTTPException(409,'操作参数不一致')
        return CACHE[op][1]
    from forge.workspace import task_root
    workspace=task_root(body['run_id'],body['task_key']).resolve()
    if ROOT not in workspace.parents:raise HTTPException(403,'工作区越界')
    allowed={'calc.py':{BAD,GOOD},'tests/test_calc.py':{TEST},'tests/test_edges.py':{EDGE}};verified={}
    for name,values in allowed.items():
        p=workspace/name
        if name.endswith('test_edges.py') and not p.exists():continue
        if p.is_symlink():raise HTTPException(403,'拒绝符号链接')
        text=p.read_text()
        if text not in values:raise HTTPException(403,'非固定可信内容，拒绝宿主机执行')
        verified[name]=text
    with tempfile.TemporaryDirectory(prefix='forge-pinned-test-') as tmp:
        for name,text in verified.items():p=Path(tmp)/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text)
        runner='import os,sys,unittest;sys.path.insert(0,os.getcwd());r=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover("tests"));sys.exit(not r.wasSuccessful())'
        result=await asyncio.to_thread(subprocess.run,[sys.executable,'-I','-c',runner],cwd=tmp,capture_output=True,text=True,timeout=10,env={'PATH':os.environ.get('PATH',''),'LANG':'C.UTF-8'})
    output={'exit_code':result.returncode,'stdout':result.stdout,'stderr':result.stderr,'timed_out':False,'fixture_only':True,'sandbox_verified':False}
    log({'kind':'trusted-fixture-unittest','exit_code':result.returncode,'output':result.stderr,'source_sha256':{k:hashlib.sha256(v.encode()).hexdigest() for k,v in verified.items()}})
    CACHE[op]=(fingerprint,output);return output
