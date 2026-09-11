const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const ts=require('../../frontend/vendor/typescript.cjs');
const source=fs.readFileSync(require.resolve('../../frontend/src/components/EmployeeTaskPanel.tsx'),'utf8');
const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText;
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
  if(id==='./UI')return{Badge:props=>jsx('badge',props)};
  if(id==='./Icons')return new Proxy({},{get:(_,key)=>props=>jsx('icon-'+String(key),props)});
  if(id==='./TaskApprovals')return{__esModule:true,default:props=>jsx('approvals',props)};
  if(id==='./ExecutionCenter')return{__esModule:true,default:props=>jsx('permissions',props)};
  if(id==='./Collaboration')return{Question:props=>jsx('question',props)};
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

test('the frozen plan owner can review inside the employee panel',async()=>{
 const h=harness({runPatch:{status:'awaiting_approval',snapshot:{...run.snapshot,planner_id:'a'}}});h.render();let tree=await h.settle();nodes(tree).find(n=>n.props?.className==='employee-approval-alert').props.onClick();tree=h.render();assert.equal(nodes(tree).filter(n=>n.type==='approvals').length,1);assert.equal(nodes(tree).find(n=>n.type==='approvals').props.run.id,run.id);
});

test('autonomous acceptance belongs to the explicit coordinator regardless of fixed review role',async()=>{
 const h=harness({runPatch:{execution_strategy:'autonomous',status:'awaiting_acceptance',snapshot:{...run.snapshot,entry_agent_id:'a'},report:{mode:'autonomous'}}});h.render();let tree=await h.settle();nodes(tree).find(n=>n.props?.className==='employee-approval-alert').props.onClick();tree=h.render();assert.equal(nodes(tree).filter(n=>n.type==='approvals').length,1);assert.equal(nodes(tree).find(n=>n.type==='approvals').props.run.snapshot.entry_agent_id,'a');
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
