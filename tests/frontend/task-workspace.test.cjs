const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const ts=require('../../frontend/vendor/typescript.cjs');
const compile=file=>ts.transpileModule(fs.readFileSync(require.resolve('../../frontend/src/'+file),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText;
const helpers={};vm.runInNewContext(compile('taskPresentation.ts'),{exports:helpers,require:()=>({})});
const nodes=v=>Array.isArray(v)?v.flatMap(nodes):v&&typeof v==='object'?[v,...nodes(v.props?.children)]:[];
const text=v=>Array.isArray(v)?v.map(text).join(''):v&&typeof v==='object'?text(v.props?.children):v==null||typeof v==='boolean'?'':String(v);
const button=(tree,label)=>{const found=nodes(tree).find(n=>n.type==='button'&&text(n)===label);assert.ok(found,'button '+label);return found;};
const base={id:'run-a',title:'交付一个真实项目',request:'写需求、实现代码并测试',status:'completed',project_id:'p',calls:25,input_tokens:500,output_tokens:200,updated_at:'2026-09-10T10:00:00Z',artifact_digest:'f'.repeat(64),snapshot:{planner_id:'lead',agents:{lead:{name:'协调员'},dev:{name:'研发成员'}}},report:{checks:{verification:'passed',commands:[{command:'python3 -m unittest discover -s tests -v',exit_code:0,output:'Ran 4 tests\nOK',timed_out:false,truncated:false}]},review:{verdict:'pass',summary:'独立审查没有发现问题。'}},tasks:[{id:'t',key:'implement',kind:'develop',agent_id:'dev',title:'实现与测试',status:'integrated',spec:{},result:{summary:'已完成实现并测试'}}]};
function harness(run=base,{handler}={}){
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
  if(id==='./TaskApprovals')return{__esModule:true,default:component('approval')};
  if(id==='./ExecutionCenter')return{__esModule:true,default:component('execution-requests')};
  if(id==='./Collaboration')return{Question:component('question')};
  if(id==='./TaskFlow')return{__esModule:true,default:component('task-flow'),TaskOutputs:component('task-outputs')};
  throw Error(id);
 }});
 const props={run,runs:[run],selected:run.id,projects:[{id:'p',name:'研发项目'}],agents:[],events:[],writer:true,admin:true,busy:false,live:true,onSelect(){},onNew(){},onControl(){},onCancel(){},onSnapshot(){},onTask(){},onCollaboration(){},onConfigure(){},onNavigate(){},onRefresh:async()=>{}};
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
