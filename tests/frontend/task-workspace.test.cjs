const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const ts=require('../../frontend/vendor/typescript.cjs');
const compile=file=>ts.transpileModule(fs.readFileSync(require.resolve('../../frontend/src/'+file),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText;
const helpers={};vm.runInNewContext(compile('taskPresentation.ts'),{exports:helpers,require:()=>({})});
const nodes=v=>Array.isArray(v)?v.flatMap(nodes):v&&typeof v==='object'?[v,...nodes(v.props?.children)]:[];
const text=v=>Array.isArray(v)?v.map(text).join(''):v&&typeof v==='object'?text(v.props?.children):v==null||typeof v==='boolean'?'':String(v);
const button=(tree,label)=>{const found=nodes(tree).find(n=>n.type==='button'&&text(n)===label);assert.ok(found,'button '+label);return found;};
const base={id:'run-a',title:'交付一个真实项目',request:'写需求、实现代码并测试',status:'completed',project_id:'p',calls:25,input_tokens:500,output_tokens:200,updated_at:'2026-09-10T10:00:00Z',artifact_digest:'f'.repeat(64),snapshot:{planner_id:'lead',agents:{lead:{name:'协调员'},dev:{name:'研发成员'}}},report:{checks:{verification:'passed',commands:[{command:'python3 -m unittest discover -s tests -v',exit_code:0,output:'Ran 4 tests\nOK',timed_out:false,truncated:false}]},review:{verdict:'pass',summary:'独立审查没有发现问题。'}},tasks:[{id:'t',key:'implement',kind:'develop',agent_id:'dev',title:'实现与测试',status:'integrated',spec:{},result:{summary:'已完成实现并测试'}}]};

test('long title and complete request stay accessible in an initially closed disclosure',()=>{
 const run={...base,title:'很长的项目标题'.repeat(50),request:'验收要求\n'.repeat(1000)};
 const tree=harness(run).render(),title=nodes(tree).find(n=>n.type==='h1');
 assert.equal(title.props.title,run.title);
 const disclosure=nodes(tree).find(n=>n.props?.className==='task-heading-context');
 assert.equal(disclosure.type,'details');assert.ok(!disclosure.props.open);
 assert.ok(text(disclosure).includes(run.request));assert.ok(text(disclosure).includes(run.title));
});
function harness(run=base,{handler,events=[]}={}){
 let cursor=0;const slots=[],effects=[],cleanups=[],calls=[];
 const React={useState(init){const i=cursor++;if(!(i in slots))slots[i]=typeof init==='function'?init():init;return[slots[i],v=>slots[i]=typeof v==='function'?v(slots[i]):v];},useRef(init){const i=cursor++;return slots[i]||(slots[i]={current:init});},useEffect(fn,deps){const i=cursor++;if(!(i in slots)||deps.some((v,j)=>v!==slots[i][j])){cleanups[i]?.();slots[i]=deps;effects.push(()=>cleanups[i]=fn());}}};
 const jsx=(type,props)=>typeof type==='function'?type(props||{}):{type,props:props||{}};
 const component=name=>props=>jsx(name,props);
 const exports={};
 vm.runInNewContext(compile('components/TaskWorkspace.tsx'),{exports,console,Date,TextDecoder,AbortController,encodeURIComponent,setInterval:()=>1,clearInterval(){},require:id=>{
  if(id==='react')return React;if(id==='react/jsx-runtime')return{jsx,jsxs:jsx,Fragment:'fragment'};
  if(id==='../taskPresentation')return helpers;
  if(id==='../types')return{terminal:['completed','cancelled','needs_attention'],states:{completed:'已完成',integrated:'已集成'}};
  if(id==='../api')return{jsonText:JSON.stringify,api:async(url,method='GET',body)=>{calls.push({url,method,body});return handler?handler(url,method,body):{decisions:[]};}};
  if(id==='./UI')return{Badge:p=>jsx('span',{children:p.state}),Empty:component('empty'),Modal:component('dialog')};
  if(id==='./Icons')return new Proxy({},{get:(_,key)=>component('icon-'+String(key))});
  if(id==='./RunRecovery')return{__esModule:true,default:component('recovery'),runRecoverySeed:r=>({parent_run_id:r.id})};
  if(id==='./RunIssues'){const issues={};vm.runInNewContext(compile('components/RunIssues.tsx'),{exports:issues,require:id=>id==='react/jsx-runtime'?{jsx,jsxs:jsx}:{} });return issues;}
  if(id==='./TaskApprovals')return{__esModule:true,default:component('approval')};
  if(id==='./ExecutionCenter')return{__esModule:true,default:component('execution-requests')};
  if(id==='./CodexRuntimeApprovals')return{__esModule:true,default:component('runtime-approvals')};
  if(id==='./Collaboration')return{Question:component('question')};
  if(id==='./TaskFlow')return{__esModule:true,default:component('task-flow'),TaskOutputs:component('task-outputs')};
  throw Error(id);
 }});
 const props={run,runs:[run],selected:run.id,projects:[{id:'p',name:'研发项目'}],agents:[],events,writer:true,admin:true,busy:false,live:true,onSelect(){},onNew(){},onControl(){},onCancel(){},onSnapshot(){},onTask(){},onCollaboration(){},onConfigure(){},onNavigate(){},onRefresh:async()=>{}};
 const render=()=>{cursor=0;const tree=exports.default(props);effects.splice(0).forEach(f=>f());return tree;};
 return{render,calls,async settle(){await new Promise(r=>setImmediate(r));return render();}};
}

test('completed task lands on real deliverables rather than global metrics or an office',()=>{
 const h=harness(),tree=h.render();assert.match(text(tree),/任务已完成/);assert.match(text(tree),/完整成果包/);assert.match(text(tree),/独立审查通过/);
 assert.equal(nodes(tree).find(n=>n.type==='button'&&text(n)==='成果').props['aria-pressed'],true);
 const links=nodes(tree).filter(n=>n.type==='a');assert.equal(links.length,3);assert.ok(links.some(n=>n.props.href==='/api/runs/run-a/artifacts/source.zip'));
 assert.ok(!nodes(tree).some(n=>n.type==='approval'));assert.ok(!nodes(tree).some(n=>n.props?.role==='progressbar'));
 assert.ok(h.calls.every(c=>c.method==='GET'));
});

test('an unfinished task does not invent downloadable artifacts or test success',()=>{
 const run={...base,status:'running',artifact_digest:'',report:{},tasks:[]};const h=harness(run);let tree=h.render();button(tree,'成果').props.onClick();tree=h.render();
 assert.match(text(tree),/成果正在形成/);assert.match(text(tree),/尚无验收命令结果/);assert.doesNotMatch(text(tree),/独立审查通过/);assert.equal(nodes(tree).filter(n=>n.type==='a').length,0);
});

test('manual questions and typed permissions use different controls within the locked task',async()=>{
 const h=harness({...base,status:'awaiting_decision',artifact_digest:''},{handler:()=>({decisions:[{id:'q',digest:'d',kind:'question',status:'pending'},{id:'install',digest:'d',kind:'tool_installation',status:'pending'},{id:'permission',digest:'d',kind:'execution_permission',status:'pending'}]})});h.render();const tree=await h.settle();
 const generic=nodes(tree).filter(n=>n.type==='question');assert.equal(generic.length,1);assert.equal(generic[0].props.q.id,'q');
 const panel=nodes(tree).find(n=>n.type==='execution-requests');assert.equal(panel.props.compact,true);assert.equal(panel.props.initialRunId,base.id);assert.equal(panel.props.agentNames.dev,'研发成员');
 assert.equal(nodes(tree).find(n=>n.type==='button'&&text(n).startsWith('待我处理')).props['aria-pressed'],true);assert.ok(h.calls.every(c=>c.method==='GET'));
});

test('untyped questions fail closed instead of showing ordinary approval controls',async()=>{
 const h=harness({...base,status:'awaiting_decision'},{handler:()=>({decisions:[{id:'q',status:'pending'}]})});h.render();const tree=await h.settle();assert.match(text(tree),/审批类型尚未确认/);assert.equal(nodes(tree).filter(n=>n.type==='question').length,0);
});

test('approval and final acceptance cards stay in the task, and technical records are collapsed',()=>{
 for(const status of ['awaiting_approval','awaiting_acceptance']){const h=harness({...base,status});const tree=h.render();assert.equal(nodes(tree).filter(n=>n.type==='approval').length,1);assert.ok(nodes(tree).filter(n=>n.type==='details').every(n=>!n.props.open));}
});

test('read failures persist and do not imply an empty approval queue',async()=>{
 const h=harness({...base,status:'awaiting_decision'},{handler:()=>Promise.reject(new Error('连接中断'))});h.render();const tree=await h.settle();assert.match(text(tree),/待办读取失败：连接中断/);assert.ok(nodes(tree).some(n=>n.props?.role==='alert'));assert.doesNotMatch(text(tree),/当前没有等待你回答的问题/);
});

test('discussion-only stages never claim that a plan approval or code execution took place',()=>{
 const p=helpers.runPresentation({...base,status:'discussing',report:{mode:'discussion'}});assert.deepEqual(Array.from(p.steps,s=>s.label),['理解需求','成员讨论','整理成果','人工验收']);assert.equal(p.steps[1].state,'current');assert.match(helpers.verificationSummary({...base,report:{mode:'discussion'}}).checkLabel,/不执行代码测试/);
});

test('paused and failed states use the actual affected task without marking it complete',()=>{
 for(const status of ['paused','needs_attention']){const p=helpers.runPresentation({...base,status,artifact_digest:'',tasks:[{kind:'plan',key:'plan',status:'failed'}]});assert.equal(p.steps[0].state,'blocked');assert.ok(p.steps.slice(1).every(s=>s.state==='upcoming'));}
 const p=helpers.runPresentation({...base,status:'awaiting_decision',tasks:[{kind:'develop',key:'implementation',status:'waiting_decision'}]});assert.equal(p.steps[2].state,'waiting');
});

test('stopped tasks with preserved artifacts open their cause rather than hiding it behind deliverables',()=>{
 for(const status of ['paused','needs_attention'])assert.equal(helpers.primaryTab({...base,status}),'progress');
 assert.equal(helpers.primaryTab(base),'results');
 assert.equal(helpers.primaryTab({...base,status:'awaiting_decision'}),'actions');
});

test('nonempty successful command receipts are required before displaying a passed command count',()=>{
 for(const patch of [{commands:[]},{commands:[{exit_code:1}]},{commands:[{exit_code:0,timed_out:true}]},{commands:[{exit_code:0,truncated:true}]}]){const v=helpers.verificationSummary({...base,report:{checks:{verification:'passed',...patch}}});assert.equal(v.passing,false);assert.doesNotMatch(v.checkLabel,/条验收命令通过/);}
 assert.equal(helpers.verificationSummary(base).passing,true);
});

test('latest failed or unverified check results are visible even before artifacts publish',()=>{
 const passed={kind:'check',key:'checks_0',status:'done',result:base.report.checks};
 for(const verification of ['failed','unverified']){
  const latest={kind:'check',key:'checks_2',status:verification==='failed'?'failed':'unverified',result:{verification,commands:verification==='failed'?[{command:'tests',exit_code:1}]:[]}};
  const v=helpers.verificationSummary({...base,status:'reworking',report:{},tasks:[latest,passed]});
  assert.equal(v.passing,false);assert.equal(v.checks.verification,verification);assert.match(v.checkLabel,/未通过|未验证/);
 }
});

test('a new check round does not reuse a prior round passing result',()=>{
 const v=helpers.verificationSummary({...base,status:'verifying',report:{},tasks:[{kind:'check',key:'checks_10',status:'running',result:{}},{kind:'check',key:'checks_9',status:'done',result:base.report.checks}]});
 assert.equal(v.passing,false);assert.match(v.checkLabel,/正在运行本轮/);assert.equal(v.commands.length,0);
});

test('an older task question response cannot resurrect a resolved approval',async()=>{
 const responses=[];const h=harness({...base,status:'awaiting_decision'},{handler:()=>new Promise(resolve=>responses.push(resolve))});
 let tree=h.render();nodes(tree).find(n=>n.props?.['aria-label']==='刷新当前任务').props.onClick();
 responses[1]({decisions:[]});tree=await h.settle();assert.equal(nodes(tree).filter(n=>n.type==='question').length,0);
 responses[0]({decisions:[{id:'old',digest:'d',kind:'question',status:'pending'}]});tree=await h.settle();
 assert.equal(nodes(tree).filter(n=>n.type==='question').length,0);
});

test('entry points route directly to task workspace and no longer mount global office metrics above it',()=>{
 const source=fs.readFileSync(require.resolve('../../frontend/src/App.tsx'),'utf8');assert.match(source,/<TaskWorkspace /);assert.doesNotMatch(source,/className="workspace-grid"/);assert.match(source,/setSelected\(id\);setPage\('workspace'\);setWorkspaceMode\('board'\)/);
});

test('autonomous tasks have collaboration stages and no fabricated fixed test or review success',()=>{
 const run={...base,execution_strategy:'autonomous',status:'running',artifact_digest:'',snapshot:{...base.snapshot,entry_agent_id:'dev'},report:{mode:'autonomous',checks:{verification:'not_requested'},review:{verdict:'not_requested',summary:''}}};
 const presentation=helpers.runPresentation(run);assert.deepEqual(Array.from(presentation.steps,s=>s.label),['接收需求','自主协作','整理交付','人工验收']);assert.match(presentation.title,/自主协作/);assert.doesNotMatch(presentation.description,/确认计划|开发流程/);
 const verification=helpers.verificationSummary(run);assert.equal(verification.passing,false);assert.match(verification.checkLabel,/未请求.*不代表.*通过/);assert.match(verification.reviewLabel,/未设置固定独立审查/);
 const h=harness(run);let tree=h.render();button(tree,'成果').props.onClick();tree=h.render();assert.doesNotMatch(text(tree),/等待独立审查|独立审查通过/);assert.equal(nodes(tree).filter(n=>n.type==='a').length,0);
});

test('autonomous ready or waiting member outcomes do not mark the run completed',()=>{
 for(const outcome of ['waiting','ready','blocked']){const run={...base,execution_strategy:'autonomous',status:outcome==='blocked'?'needs_attention':'running',artifact_digest:'',report:{},tasks:[{...base.tasks[0],kind:'autonomous',result:{outcome,summary:'成员回执'}}]};const p=helpers.runPresentation(run);assert.doesNotMatch(p.title,/任务已完成/);assert.ok(!p.steps.every(s=>s.state==='done'));if(outcome==='blocked')assert.match(p.title,/尚未完成/);}
});

test('the public run mode alone identifies autonomy and wins over stale legacy graph labels',()=>{
 const run={...base,mode:'autonomous',status:'running',artifact_digest:'',report:{},snapshot:{...base.snapshot,orchestration:{mode:'discussion'}}};
 assert.equal(helpers.isAutonomous(run),true);assert.equal(helpers.isDiscussion(run),false);assert.equal(helpers.runPresentation(run).autonomous,true);assert.match(helpers.runPresentation(run).title,/自主协作/);assert.doesNotMatch(helpers.verificationSummary(run).checkLabel,/只读讨论/);
});

test('team tool aliases describe actual events without defining or limiting runtime tool availability',()=>{
 assert.equal(helpers.taskToolLabel('inspect_team'),'查看团队能力目录');assert.equal(helpers.taskToolLabel('read_work_result'),'读取团队已集成成果');assert.equal(helpers.taskToolLabel('custom_live_tool'),'custom_live_tool');assert.equal(helpers.taskToolLabel(undefined),'');
});

test('model completion replaces old liveness and exposes the real output without crossing task scope',()=>{
 const event=(id,kind,data={},run_id=base.id)=>({id,kind,data,run_id,created_at:base.updated_at});
 const progress=event(1,'model.progress',{agent_id:'dev',elapsed_s:22});
 const result=event(2,'model.completed',{agent_id:'dev',summary:'已检查实际项目文件，下一步运行测试。'});
 const other=event(3,'model.completed',{agent_id:'lead',summary:'另一个任务的私有输出'},'run-b');
 const run={...base,status:'running',artifact_digest:'',report:{}};
 const activity=helpers.taskActivity(run,[result,progress,result,other]);
 assert.equal(activity.events.length,2);assert.equal(activity.status,'模型已返回，本轮结果已记录');
 assert.equal(activity.output.id,2);
 const tree=harness(run,{events:[progress,result,other]}).render();
 assert.match(text(tree),/最近输出摘要 · 研发成员/);assert.match(text(tree),/已检查实际项目文件/);
 assert.doesNotMatch(text(tree),/仍在响应|另一个任务的私有输出/);
 const disclosure=nodes(tree).find(n=>n.props.className==='task-recent-output');
 assert.ok(disclosure&&!disclosure.props.open,'long model output starts collapsed');
});

test('pause, approval and terminal states take precedence over stale model heartbeats',()=>{
 const events=[{id:1,run_id:base.id,kind:'model.progress',created_at:base.updated_at,data:{agent_id:'dev',elapsed_s:120}}];
 const expected={paused:'任务已暂停，等待继续',needs_attention:'任务已停止，查看原因与处理',cancelled:'任务已取消，不再执行',completed:'任务已完成',awaiting_decision:'等待你的回答或授权',awaiting_approval:'等待计划审批',awaiting_acceptance:'等待成果验收'};
 for(const [status,message] of Object.entries(expected)){
  const activity=helpers.taskActivity({...base,status},events);
  assert.equal(activity.status,message);assert.equal(activity.agentId,'');assert.equal(activity.output,undefined);
 }
});

test('live member messages use the real sender and body without claiming a reply or read receipt',()=>{
 const events=[{id:1,run_id:base.id,kind:'agent.message',created_at:base.updated_at,data:{from_agent_id:'dev',to_agent_id:'lead',body:'测试命令已返回，待核对结果。'}}];
 const activity=helpers.taskActivity({...base,status:'running'},events);
 assert.equal(activity.outputAgentId,'dev');assert.equal(activity.agentId,'dev');
 assert.equal(activity.outputText,events[0].data.body);assert.equal(activity.status,'成员消息已发送');
});

test('real model delta events are aggregated per run and rendered while the turn is active',()=>{
 const event=(id,kind,data={},run_id=base.id)=>({id,kind,data,run_id,created_at:base.updated_at});
 const events=[event(1,'model.started',{agent_id:'dev'}),event(2,'model.delta',{agent_id:'dev',sequence:1,text:'部分'}),event(3,'model.delta',{agent_id:'dev',sequence:2,text:'输出'})];
 const activity=helpers.taskActivity({...base,status:'running'},events);
 assert.equal(activity.streamText,'部分输出');assert.equal(activity.status,'正在接收模型输出（已收到 4 字）');
 const tree=harness({...base,status:'running'},{events}).render();assert.match(text(tree),/实时模型输出 · 研发成员/);assert.match(text(tree),/部分输出/);
});

test('lossless Codex runtime notifications are projected into the live model stream',()=>{
 const event=(id,kind,payload)=>({id,kind,run_id:base.id,created_at:base.updated_at,data:{runtime_kind:kind.replace(/^runtime\./,'').replace(/\./g,'/'),payload,thread_id:'thread-1',turn_id:'turn-1',item_id:'item-1',sequence:id}});
 const events=[event(1,'runtime.turn.started',{}),event(2,'runtime.item.agentMessage.delta',{delta:'来自 Codex'})];
 const activity=helpers.taskActivity({...base,status:'running',artifact_digest:'',report:{}},events);
 assert.equal(activity.streamText,'来自 Codex');
 assert.match(activity.status,/正在接收模型输出/);
});

test('Codex completed notifications with a failed turn remain visibly failed',()=>{
 const event={id:1,kind:'runtime.turn.completed',run_id:base.id,created_at:base.updated_at,data:{runtime_kind:'turn/completed',payload:{turn:{status:'failed',error:{message:'工具调用被拒绝'}}}}};
 const activity=helpers.taskActivity({...base,status:'running',artifact_digest:'',report:{}},[event]);
 assert.equal(activity.events[0].kind,'task.failed');
 assert.match(activity.status,/成员执行失败/);
 assert.match(activity.events[0].data.summary,/工具调用被拒绝/);
});

test('Codex runtime requests replace plan approval and stay bound to the current task',()=>{
 const run={...base,execution_engine:'codex',status:'awaiting_approval',plan_digest:'',artifact_digest:'',report:{}};
 const tree=harness(run).render();
 assert.equal(nodes(tree).filter(n=>n.type==='approval').length,0);
 const panel=nodes(tree).find(n=>n.type==='runtime-approvals');assert.ok(panel);
 assert.equal(panel.props.run.id,run.id);assert.equal(panel.props.writer,true);assert.equal(panel.props.visible,true);
 assert.match(text(tree),/运行时请求你的授权/);assert.doesNotMatch(text(tree),/请确认这份执行计划/);
 assert.equal(helpers.taskActivity(run,[]).status,'等待 Codex 运行时授权');
});
