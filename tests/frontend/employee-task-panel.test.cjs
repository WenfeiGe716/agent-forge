const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const ts=require('../../frontend/vendor/typescript.cjs');
const source=fs.readFileSync(require.resolve('../../frontend/src/components/EmployeeTaskPanel.tsx'),'utf8');
const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText;
const recoveryCode=ts.transpileModule(fs.readFileSync(require.resolve('../../frontend/src/components/RunRecovery.tsx'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText;
const issuesCode=ts.transpileModule(fs.readFileSync(require.resolve('../../frontend/src/components/RunIssues.tsx'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText;
const transcriptCode=ts.transpileModule(fs.readFileSync(require.resolve('../../frontend/src/liveTranscript.ts'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
const markdownCode=ts.transpileModule(fs.readFileSync(require.resolve('../../frontend/src/components/TranscriptMarkdown.tsx'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText;
const nodes=v=>Array.isArray(v)?v.flatMap(nodes):v&&typeof v==='object'?[v,...nodes(v.props?.children)]:[];
const text=v=>Array.isArray(v)?v.map(text).join(''):v&&typeof v==='object'?text(v.props?.children):v==null||typeof v==='boolean'?'':String(v);
const button=(tree,label)=>{const b=nodes(tree).find(n=>n.type==='button'&&text(n)===label);assert.ok(b,label);return b;};
const run={id:'run-a',title:'需求文档任务',status:'awaiting_decision',updated_at:'2026-09-10',project_id:'p',agent_ids:['a','b'],snapshot:{agents:{a:{name:'需求师'},b:{name:'工程师'}},planner_id:'b',reviewer_id:'b'},tasks:[{id:'t',key:'requirements',kind:'develop',agent_id:'a',title:'完成需求文档',status:'waiting_decision',spec:{},result:{}}]};
function harness({handler,preview=false,runPatch={},propPatch={}}={}){
 let cursor=0;const slots=[],effects=[],cleanups=[],calls=[],opened=[];
 const React={useState(v){const i=cursor++;if(!(i in slots))slots[i]=typeof v==='function'?v():v;return[slots[i],x=>slots[i]=typeof x==='function'?x(slots[i]):x];},useRef(v){const i=cursor++;return slots[i]||(slots[i]={current:v});},useEffect(fn,deps){const i=cursor++;if(!(i in slots)||deps.some((d,j)=>!Object.is(d,slots[i][j]))){cleanups[i]?.();slots[i]=deps;effects.push(()=>cleanups[i]=fn());}}};
 const jsx=(type,props)=>typeof type==='function'?type(props||{}):{type,props:props||{}};
 const exports={};vm.runInNewContext(code,{exports,console,setInterval:()=>1,clearInterval(){},encodeURIComponent,require:id=>{
  if(id==='react')return React;if(id==='react/jsx-runtime')return{jsx,jsxs:jsx,Fragment:'fragment'};
  if(id==='../types')return{terminal:['completed','cancelled','needs_attention'],states:{awaiting_decision:'待决定'}};
  if(id==='../api')return{jsonText:JSON.stringify,api:async(url,method='GET',body)=>{calls.push({url,method,body});if(handler)return handler(url,method,body);if(url.endsWith('/collaboration'))return{run_id:run.id,decisions:[]};if(url.includes('/trace?'))return{items:[]};return{...run,...runPatch};}};
  if(id==='../runEventStream')return{subscribeRunEvents:()=>()=>{}};
  if(id==='../liveTranscript'){const transcript={};vm.runInNewContext(transcriptCode,{exports:transcript,console});return transcript;}
  if(id==='./TranscriptMarkdown'){const markdown={};vm.runInNewContext(markdownCode,{exports:markdown,require:dep=>dep==='react/jsx-runtime'?{jsx,jsxs:jsx,Fragment:'fragment'}:{}});return markdown;}
  if(id==='./UI')return{Badge:props=>jsx('badge',props)};
  if(id==='./Icons')return new Proxy({},{get:(_,key)=>props=>jsx('icon-'+String(key),props)});
  if(id==='./TaskApprovals')return{__esModule:true,default:props=>jsx('approvals',props)};
  if(id==='./ExecutionCenter')return{__esModule:true,default:props=>jsx('permissions',props)};
  if(id==='./CodexRuntimeApprovals')return{__esModule:true,default:props=>jsx('runtime-approvals',props)};
 if(id==='./Collaboration')return{Question:props=>jsx('question',props)};
  if(id==='./RunRecovery'){const recovery={};vm.runInNewContext(recoveryCode,{exports:recovery,require:id=>id==='react/jsx-runtime'?{jsx,jsxs:jsx}:{} });return recovery;}
  if(id==='./RunIssues'){const issues={};vm.runInNewContext(issuesCode,{exports:issues,require:id=>id==='react/jsx-runtime'?{jsx,jsxs:jsx}:{} });return issues;}
  if(id.endsWith('.css'))return{};
  throw Error(id);
 }});
 let props={agent:{id:'a',name:'需求师'},runs:[{...run,...runPatch}],detail:null,projects:[],writer:true,admin:true,preview,onOpenRun:id=>opened.push(id),onNavigate(){},onRefresh(){},...propPatch};
 const render=()=>{cursor=0;const tree=exports.default(props);effects.splice(0).forEach(fn=>fn());return tree;};
 return{exports,calls,opened,render,setProps(next){props={...props,...next};return render();},unmount(){cleanups.forEach(fn=>fn?.());},async settle(){await new Promise(resolve=>setImmediate(resolve));return render();}};
}

test('employee membership supports concurrent runs without using current design membership',()=>{
 const h=harness(),list=h.exports.employeeRuns('a',[run,{...run,id:'parallel'},{...run,id:'someone-else',agent_ids:['b']},{...run,id:'done',status:'completed'}]);
 assert.deepEqual(Array.from(list,r=>r.id),['run-a','parallel','done']);
});

test('employee tasks use explicit run membership and do not request model inference',async()=>{
 const h=harness();h.render();const tree=await h.settle();assert.match(text(tree),/完成需求文档/);assert.ok(h.calls.every(c=>c.method==='GET'));assert.equal(h.calls.length,3);assert.ok(h.calls.some(c=>c.url.includes('/run-a/trace?agent_id=a')));
});

test('employee approvals are locked to both the selected task and employee',async()=>{
 const h=harness({handler:url=>url.endsWith('/collaboration')?{run_id:'run-a',decisions:[{run_id:'run-a',agent_id:'a',id:'mine',digest:'x',kind:'question',status:'pending'},{run_id:'run-a',agent_id:'b',id:'other',kind:'question',status:'pending'},{run_id:'run-b',agent_id:'a',id:'another-task',kind:'question',status:'pending'}]}:url.includes('/trace?')?{items:[]}:run});
 h.render();let tree=await h.settle();nodes(tree).find(n=>n.type==='button'&&n.props.className==='employee-approval-alert').props.onClick();tree=h.render();
 assert.deepEqual(nodes(tree).filter(n=>n.type==='question').map(n=>n.props.q.id),['mine']);
 const permissions=nodes(tree).find(n=>n.type==='permissions');assert.equal(permissions.props.initialRunId,'run-a');assert.equal(permissions.props.agentId,'a');
});

test('plan approval belongs to the frozen planner, not every employee',async()=>{
 const h=harness({runPatch:{status:'awaiting_approval'}});h.render();let tree=await h.settle();button(tree,'审批').props.onClick();tree=h.render();assert.equal(nodes(tree).filter(n=>n.type==='approvals').length,0);assert.match(text(tree),/归属 工程师/);
});

test('Codex employee approval is runtime-scoped and never displays a plan decision',async()=>{
 const h=harness({runPatch:{execution_engine:'codex',status:'awaiting_approval',snapshot:{...run.snapshot,planner_id:'a'}},propPatch:{initialTab:'approval'}});
 h.render();const tree=await h.settle();
 assert.equal(nodes(tree).filter(n=>n.type==='approvals').length,0);
 const panel=nodes(tree).find(n=>n.type==='runtime-approvals');assert.ok(panel);
 assert.equal(panel.props.run.id,'run-a');assert.equal(panel.props.agentId,'a');assert.equal(panel.props.visible,true);
 assert.equal(panel.props.writer,true);assert.doesNotMatch(text(tree),/这份计划归属/);
});

test('the frozen plan owner can review inside the employee panel',async()=>{
 const h=harness({runPatch:{status:'awaiting_approval',snapshot:{...run.snapshot,planner_id:'a'}}});h.render();let tree=await h.settle();nodes(tree).find(n=>n.props?.className==='employee-approval-alert').props.onClick();tree=h.render();assert.equal(nodes(tree).filter(n=>n.type==='approvals').length,1);assert.equal(nodes(tree).find(n=>n.type==='approvals').props.run.id,run.id);
});

test('autonomous acceptance belongs to the explicit coordinator regardless of fixed review role',async()=>{
 const h=harness({runPatch:{execution_strategy:'autonomous',status:'awaiting_acceptance',snapshot:{...run.snapshot,entry_agent_id:'a'},report:{mode:'autonomous'}}});h.render();let tree=await h.settle();nodes(tree).find(n=>n.props?.className==='employee-approval-alert').props.onClick();tree=h.render();assert.equal(nodes(tree).filter(n=>n.type==='approvals').length,1);assert.equal(nodes(tree).find(n=>n.type==='approvals').props.run.snapshot.entry_agent_id,'a');
});

test('mode-only autonomous acceptance is also assigned to its coordinator',async()=>{
 const h=harness({runPatch:{mode:'autonomous',status:'awaiting_acceptance',snapshot:{...run.snapshot,entry_agent_id:'a'}}});h.render();let tree=await h.settle();nodes(tree).find(n=>n.props?.className==='employee-approval-alert').props.onClick();tree=h.render();assert.equal(nodes(tree).filter(n=>n.type==='approvals').length,1);
});

test('autonomous waiting is visible as the actual employee stage outcome, not a human approval',async()=>{
 const h=harness({runPatch:{execution_strategy:'autonomous',status:'running',tasks:[{...run.tasks[0],kind:'autonomous',status:'done',result:{outcome:'waiting',summary:'等待团队反馈'}}]}});h.render();const tree=await h.settle();assert.match(text(tree),/本次已让出 · 等待成员消息/);assert.ok(!nodes(tree).some(n=>n.props?.className==='employee-approval-alert'));
});

test('closed employee tasks retain records but never expose live approval authority',async()=>{
 const h=harness({runPatch:{status:'completed',artifact_digest:'a'.repeat(64)}});h.render();let tree=await h.settle();button(tree,'审批').props.onClick();tree=h.render();assert.equal(nodes(tree).find(n=>n.type==='permissions').props.admin,false);assert.equal(nodes(tree).filter(n=>n.type==='approvals').length,0);
});

test('cross-task responses fail closed instead of rendering the wrong employee task',async()=>{
 const h=harness({handler:url=>url.endsWith('/collaboration')?{run_id:'run-a',decisions:[]}:url.includes('/trace?')?{items:[]}:{...run,id:'other'}});h.render();const tree=await h.settle();assert.match(text(tree),/不在这个任务的冻结成员范围/);assert.equal(nodes(tree).filter(n=>n.type==='permissions'||n.type==='approvals').length,0);
});

test('3D preview never reads or approves production tasks',()=>{
 const h=harness({preview:true});const tree=h.render();assert.match(text(tree),/不能提交决定/);assert.equal(h.calls.length,0);
});

test('explicit approval task beats a more recently updated task for the same employee',()=>{
 const h=harness({preview:true,propPatch:{initialRunId:'run-a',runs:[{...run,id:'newer',updated_at:'2026-09-11'},run]}});const tree=h.render();assert.equal(nodes(tree).find(n=>n.type==='select').props.value,'run-a');
});

test('an employee task selection never drifts when another parallel task updates',()=>{
 const other={...run,id:'parallel',updated_at:'2026-09-09'};const h=harness({preview:true,propPatch:{runs:[run,other]}});let tree=h.render();assert.equal(nodes(tree).find(n=>n.type==='select').props.value,'run-a');
 tree=h.setProps({runs:[{...other,updated_at:'2026-09-12'},run]});assert.equal(nodes(tree).find(n=>n.type==='select').props.value,'run-a');
});

test('employee communication shows correct sender and receiver direction',()=>{
 const h=harness(),event={kind:'agent.message',data:{from_agent_id:'b',to_agent_id:'a'}};const v=h.exports.employeeMessage(event,'a',{a:'需求师',b:'工程师'});assert.equal(v.title,'收到成员消息');assert.equal(v.parties,'工程师 → 需求师');assert.equal(h.exports.employeeMessage(event,'b',{}).title,'发出成员消息');
});

test('controlled employee task selection reports the chosen task and follows explicit parent changes without remounting',()=>{
 const selected=[],other={...run,id:'parallel',updated_at:'2026-09-09'},h=harness({preview:true,propPatch:{runs:[run,other],initialRunId:'run-a',onRunChange:id=>selected.push(id)}});let tree=h.render();
 nodes(tree).find(n=>n.type==='select').props.onChange({target:{value:'parallel'}});assert.equal(selected.at(-1),'parallel');
 tree=h.setProps({initialRunId:'parallel'});assert.equal(nodes(tree).find(n=>n.type==='select').props.value,'parallel');
 tree=h.setProps({initialRunId:'run-a'});assert.equal(nodes(tree).find(n=>n.type==='select').props.value,'run-a');assert.equal(h.calls.length,0);
});

test('3D employee and composer approval entry points stay in the employee inspector',()=>{
 const studio=fs.readFileSync(require.resolve('../../frontend/src/components/StudioWorkspace.tsx'),'utf8');assert.match(studio,/<EmployeeTaskPanel /);assert.match(studio,/className="approval-link" onClick=\{openApprovals\}/);assert.match(studio,/const pick=.*setTab\('tasks'\)/);
});

test('newly created run remains selected before list refresh and validates actual membership',async()=>{
 const selected=[],fresh={...run,id:'new-run',title:'本次新任务',status:'running'};
 const h=harness({propPatch:{initialRunId:'new-run',onRunChange:id=>selected.push(id)},handler:url=>url.endsWith('/collaboration')?{run_id:'new-run',decisions:[]}:url.includes('/trace?')?{items:[]}:fresh});
 let tree=h.render();assert.equal(nodes(tree).find(n=>n.type==='select').props.value,'new-run');
 tree=await h.settle();assert.match(text(tree),/本次新任务/);assert.ok(h.calls.every(c=>c.url.includes('/new-run')));assert.deepEqual(selected,['new-run']);
 const forbidden=harness({propPatch:{runs:[],initialRunId:'new-run',onRunChange(){}},handler:url=>url.endsWith('/collaboration')?{run_id:'new-run',decisions:[]}:url.includes('/trace?')?{items:[]}:{...fresh,snapshot:{agents:{b:{}}}}});
 forbidden.render();const rejected=await forbidden.settle();assert.match(text(rejected),/不在这个任务的冻结成员范围/);assert.doesNotMatch(text(rejected),/本次新任务/);
});

test('long employee titles and original requirements remain in an explicit full-content disclosure',async()=>{
 const title='长标题_/'.repeat(100),request='原始需求完整内容。'.repeat(500),h=harness({runPatch:{title,request},propPatch:{projects:[{id:'p',name:'很长的项目名称'.repeat(50)}]}});
 h.render();const tree=await h.settle(),titleNode=nodes(tree).find(n=>n.props?.className==='employee-task-title');
 assert.equal(text(titleNode),title);assert.equal(titleNode.props.title,title);
 const disclosure=nodes(tree).find(n=>n.props?.className==='employee-request-details');assert.equal(disclosure.props.open,undefined);assert.ok(text(disclosure).includes(request));
 assert.ok(nodes(disclosure).some(n=>n.props?.className==='employee-long-content'&&n.props.tabIndex===0));
});

test('many employee stages are progressively shown with pending work first and filter resets its display window',async()=>{
 const tasks=Array.from({length:40},(_,i)=>({...run.tasks[0],id:'task-'+i,title:'工作 '+i,status:i===39?'running':'integrated'}));
 const h=harness({runPatch:{tasks}});h.render();let tree=await h.settle();
 const cards=()=>nodes(tree).filter(n=>String(n.props?.className||'').startsWith('employee-work-card '));
 assert.equal(cards().length,12);assert.match(text(cards()[0]),/工作 39/);
 button(tree,'再显示 12 项分工').props.onClick();tree=h.render();assert.equal(cards().length,24);
 nodes(tree).find(n=>n.props?.['aria-label']==='筛选员工分工').props.onChange({target:{value:'submitted'}});tree=h.render();assert.equal(cards().length,12);assert.ok(cards().every(n=>!text(n).includes('工作 39')));
 nodes(tree).find(n=>n.props?.['aria-label']==='筛选员工分工').props.onChange({target:{value:'active'}});tree=h.render();assert.equal(cards().length,1);assert.match(text(cards()[0]),/工作 39/);
});

test('long communication starts collapsed but keeps the full body, original evidence, and bounded read surface',async()=>{
 const body='完整通信消息。'.repeat(700),event={id:31,kind:'agent.message',run_id:'run-a',created_at:'2026-09-11',data:{from_agent_id:'b',to_agent_id:'a',body,evidence:'receipt-id'}};
 const h=harness({handler:url=>url.endsWith('/collaboration')?{run_id:'run-a',decisions:[]}:url.includes('/trace?')?{items:[event],has_more:true}:run});
 h.render();let tree=await h.settle();button(tree,'通信').props.onClick();tree=h.render();
 const record=nodes(tree).find(n=>n.props?.className==='is-incoming');assert.equal(record.props.open,undefined);
 const content=nodes(record).find(n=>n.props?.['aria-label']==='完整通信内容');assert.ok(text(content).includes(body));assert.match(text(content),/receipt-id/);assert.equal(content.props.tabIndex,0);
 assert.ok(nodes(tree).some(n=>n.props?.['aria-label']==='员工通信列表'&&n.props.tabIndex===0));assert.match(text(tree),/查看更多本任务通信/);
});

test('real stop reason remains visible with exact technical detail and opens only this run',async()=>{
 const error='并行变更发生冲突，已保留patch: docs/requirements.md',h=harness({runPatch:{status:'needs_attention',error}});
 h.render();const tree=await h.settle();assert.match(text(tree),/成果合并冲突，任务已停止/);assert.ok(text(tree).includes(error));
 button(tree,'查看原因与处理').props.onClick();assert.deepEqual(h.opened,['run-a']);
});
test('structured issues show actual origin and coordinator progress without inferring the blamed employee',async()=>{
 const issue={task_key:'nova-work',kind:'integration_conflict',source_agent_id:'b',source_agent_name:'Nova',status:'open',summary:'文件已由同伴修改',files:['src/game.js'],attempts:1,max_attempts:3};
 const h=harness({runPatch:{status:'running',error:'历史通用错误',snapshot:{...run.snapshot,entry_agent_id:'a'},tasks:[{...run.tasks[0],result:{issue_summary:issue}}]}});h.render();const tree=await h.settle();assert.match(text(tree),/负责人正在尝试解决/);assert.match(text(tree),/Nova/);assert.match(text(tree),/src\/game.js/);assert.ok(!nodes(tree).some(n=>n.props?.className==='employee-run-error'));button(tree,'查看本任务问题与执行记录').props.onClick();assert.deepEqual(h.opened,['run-a']);assert.ok(h.calls.every(c=>c.method==='GET'));
});

test('conflicted and superseded activations count as submitted without success icons or pending-work placement',async()=>{
 const tasks=['conflicted','superseded','running','integrated'].map((status,index)=>({...run.tasks[0],id:'status-'+index,title:status+'分工',kind:'autonomous',status,result:{}}));
 const h=harness({runPatch:{status:'running',tasks}});h.render();let tree=await h.settle();
 const metrics=nodes(tree).find(n=>n.props?.className==='employee-task-metrics');assert.equal(text(nodes(metrics).find(n=>n.type==='strong')),'3 / 4');
 const conflict=nodes(tree).find(n=>n.props?.className==='employee-work-card conflicted'),superseded=nodes(tree).find(n=>n.props?.className==='employee-work-card superseded');
 assert.match(text(conflict),/已提交 · 冲突待修复/);assert.match(text(superseded),/由修复工作替代/);
 for(const card of [conflict,superseded])assert.ok(!nodes(card).some(n=>n.type==='icon-CheckCircle2'||n.type==='icon-Activity'));
 assert.ok(nodes(nodes(tree).find(n=>n.props?.className==='employee-work-card integrated')).some(n=>n.type==='icon-CheckCircle2'));
 assert.match(text(nodes(tree).find(n=>String(n.props?.className||'').startsWith('employee-work-card '))),/running分工/);
 nodes(tree).find(n=>n.props?.['aria-label']==='筛选员工分工').props.onChange({target:{value:'active'}});tree=h.render();assert.deepEqual(nodes(tree).filter(n=>String(n.props?.className||'').startsWith('employee-work-card ')).map(n=>n.props.className),['employee-work-card running']);
 nodes(tree).find(n=>n.props?.['aria-label']==='筛选员工分工').props.onChange({target:{value:'submitted'}});tree=h.render();assert.deepEqual(nodes(tree).filter(n=>String(n.props?.className||'').startsWith('employee-work-card ')).map(n=>n.props.className),['employee-work-card conflicted','employee-work-card superseded','employee-work-card integrated']);
});

test('superseded or conflicted old outcomes do not remain labelled blocked or waiting for messages',async()=>{
 for(const status of ['superseded','conflicted'])for(const outcome of ['blocked','waiting']){
  const h=harness({runPatch:{status:'running',tasks:[{...run.tasks[0],kind:'autonomous',status,result:{outcome,summary:'原始阶段记录'}}]}});h.render();const tree=await h.settle(),card=nodes(tree).find(n=>n.props?.className==='employee-work-card '+status);
  assert.doesNotMatch(text(nodes(card).find(n=>n.type==='summary')),/已报告阻塞 · 未完成|等待成员消息/);assert.match(text(card),/原始阶段记录/);assert.match(text(card),new RegExp(outcome));
 }
});

test('running member workspace exposes targeted steer composer and attachment contract',async()=>{
 const calls=[];
 const h=harness({runPatch:{status:'running'},propPatch:{initialTab:'messages'},handler:(url,method='GET',body)=>{calls.push({url,method,body});if(url.endsWith('/collaboration'))return{run_id:'run-a',decisions:[]};if(url.includes('/trace?'))return{items:[]};return {...run,status:'running'};}});
 h.render();const tree=await h.settle();
 assert.ok(nodes(tree).some(n=>n.props?.['aria-label']==='向当前成员发送运行中消息'));
 assert.match(source,/\/attachments/);assert.match(source,/agent_id:member/);assert.match(source,/subscribeRunEvents/);
 assert.match(source,/value\.workspace_path/);
});

test('running member composer exposes ChatGPT-like attachment chips, keyboard send and live output bubble',()=>{
 assert.match(source,/className="employee-attachment-chips"/);
 assert.match(source,/aria-label=\{`移除附件/);
 assert.match(source,/e\.metaKey\|\|e\.ctrlKey/);
 assert.match(source,/className="employee-live-bubble"/);
});

test('non-empty model delta events render in the selected employee stream without throwing',async()=>{
 const delta={id:41,kind:'model.delta',run_id:'run-a',created_at:'2026-09-13T09:10:00Z',data:{agent_id:'a',text:'流式输出片段'}};
 const h=harness({propPatch:{initialTab:'messages'},handler:url=>url.endsWith('/collaboration')?{run_id:'run-a',decisions:[]}:url.includes('/trace?')?{items:[delta],has_more:false}:{...run,status:'running'}});
 h.render();
 const tree=await h.settle();
 assert.match(text(tree),/流式输出片段/);
 assert.ok(nodes(tree).some(n=>n.props?.className==='employee-live-bubble'));
});
