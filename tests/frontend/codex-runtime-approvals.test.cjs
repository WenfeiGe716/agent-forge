const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const ts=require('../../frontend/vendor/typescript.cjs');
const code=ts.transpileModule(fs.readFileSync(require.resolve('../../frontend/src/components/CodexRuntimeApprovals.tsx'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText;
const nodes=v=>Array.isArray(v)?v.flatMap(nodes):v&&typeof v==='object'?[v,...nodes(v.props?.children)]:[];
const text=v=>Array.isArray(v)?v.map(text).join(''):v&&typeof v==='object'?text(v.props?.children):v==null||typeof v==='boolean'?'':String(v);
const button=(tree,label)=>{const value=nodes(tree).find(n=>n.type==='button'&&text(n)===label);assert.ok(value,label);return value;};
const field=(tree,label)=>{const value=nodes(tree).find(n=>n.props?.['aria-label']===label);assert.ok(value,label);return value;};
const request={id:'approval-a',run_id:'run-a',task_key:'codex',agent_id:'agent-a',kind:'item/commandExecution/requestApproval',thread_id:'thread-a',turn_id:'turn-a',item_id:'item-a',status:'pending',digest:'a'.repeat(64),payload:{command:'python3 tests.py',cwd:'/forge/worktree',reason:'运行本次项目测试'},created_at:'2026-09-12T12:00:00Z'};
const run={id:'run-a',execution_engine:'codex',status:'awaiting_approval',snapshot:{agents:{'agent-a':{name:'研发成员'},'agent-b':{name:'另一成员'}}}};
function harness({items=[request],handler,props:patch={}}={}){
 let cursor=0,props={run,writer:true,...patch},refreshes=0;
 const slots=[],effects=[],cleanups=[],setups=[],calls=[],counts=[];
 const React={useState(init){const i=cursor++;if(!(i in slots))slots[i]=typeof init==='function'?init():init;return[slots[i],value=>slots[i]=typeof value==='function'?value(slots[i]):value];},useRef(init){const i=cursor++;return slots[i]||(slots[i]={current:init});},useEffect(fn,deps){const i=cursor++;if(!(i in slots)||deps.some((value,j)=>!Object.is(value,slots[i][j]))){cleanups[i]?.();slots[i]=deps;effects.push(()=>{setups[i]=fn;cleanups[i]=fn();});}}};
 const jsx=(type,props)=>({type,props:props||{}}),exports={};
 const api=async(url,method='GET',body)=>{calls.push({url,method,body});return handler?handler(url,method,body):method==='GET'?{items}:{accepted:true};};
 vm.runInNewContext(code,{exports,encodeURIComponent,setInterval:()=>1,clearInterval(){},require:id=>id==='react'?React:id==='react/jsx-runtime'?{jsx,jsxs:jsx,Fragment:'fragment'}:id==='../api'?{api,jsonText:JSON.stringify}:assert.fail(id)});
 const render=()=>{cursor=0;const tree=exports.default({...props,onPendingChange:value=>counts.push(value),onRefresh:async()=>{refreshes++;}});effects.splice(0).forEach(fn=>fn());return tree;};
 return{render,calls,counts,get refreshes(){return refreshes;},setProps(next){props={...props,...next};return render();},unmount(){cleanups.forEach(fn=>fn?.());},strictReplay(){cleanups.forEach(fn=>fn?.());setups.forEach((fn,i)=>{cleanups[i]=fn();});},async settle(){await new Promise(resolve=>setImmediate(resolve));return render();}};
}

test('runtime requests load only this run and optional employee; no plan or session permission is offered',async()=>{
 const h=harness({items:[request,{...request,id:'other-agent',agent_id:'agent-b',payload:{reason:'其他成员申请'}},{...request,id:'other-run',run_id:'run-b',payload:{reason:'另一个任务私有申请'}}],props:{agentId:'agent-a'}});
 h.render();const tree=await h.settle();
 assert.equal(h.calls[0].url,'/runs/run-a/runtime-approvals');assert.equal(h.calls.length,1);assert.equal(h.calls[0].method,'GET');
 assert.match(text(tree),/研发成员/);assert.match(text(tree),/python3 tests.py/);assert.match(text(tree),/只处理当前员工在本任务/);
 assert.doesNotMatch(text(tree),/其他成员申请|另一个任务私有申请/);assert.match(text(tree),/不会批准整个会话、永久规则/);
 assert.equal(nodes(tree).filter(n=>n.type==='article').length,1);assert.equal(h.counts.at(-1),1);
 assert.equal(button(tree,'批准本次请求').props.disabled,true);assert.ok(!nodes(tree).some(n=>n.type==='select'));
});

test('an explicit reviewed decision posts only the exact digest and waits for delivery',async()=>{
 const h=harness();h.render();let tree=await h.settle();
 field(tree,'运行时审批说明 approval-a').props.onChange({target:{value:'  仅运行这个测试命令  '}});
 field(tree,'确认本次操作范围 approval-a').props.onChange({target:{checked:true}});tree=h.render();
 await button(tree,'批准本次请求').props.onClick();tree=await h.settle();
 const writes=h.calls.filter(call=>call.method==='POST');assert.equal(writes.length,1);
 assert.equal(writes[0].url,'/runs/run-a/runtime-approvals/approval-a/decision');
 assert.deepEqual(JSON.parse(JSON.stringify(writes[0].body)),{digest:request.digest,approved:true,comment:'仅运行这个测试命令'});
 assert.match(text(tree),/决定已提交，等待服务端更新记录/);assert.match(text(tree),/不代表命令已执行/);
 assert.equal(button(tree,'批准本次请求').props.disabled,true);assert.equal(button(tree,'拒绝本次请求').props.disabled,true);assert.equal(h.refreshes,1);
});

test('double and opposite decisions are locked before a React rerender',async()=>{
 let resolve;const h=harness({handler:(_url,method)=>method==='GET'?{items:[request]}:new Promise(done=>resolve=done)});
 h.render();let tree=await h.settle();field(tree,'确认本次操作范围 approval-a').props.onChange({target:{checked:true}});tree=h.render();
 const first=button(tree,'批准本次请求').props.onClick();await button(tree,'批准本次请求').props.onClick();await button(tree,'拒绝本次请求').props.onClick();
 assert.equal(h.calls.filter(call=>call.method==='POST').length,1);resolve({accepted:true});await first;
});

test('delivered, queued, expired and unknown delivery are history, never executable success or fresh approval',async()=>{
 const rows=['queued','delivered','expired','delivery_unknown'].map((status,i)=>({...request,id:'record-'+i,status,decision:{approved:i%2===0}}));
 const h=harness({items:rows});h.render();const tree=await h.settle();
 assert.match(text(tree),/等待送达运行时/);assert.match(text(tree),/决定已送达运行时/);assert.match(text(tree),/申请已失效/);assert.match(text(tree),/决定送达结果未确认/);
 assert.equal(h.counts.at(-1),0);assert.ok(!nodes(tree).some(n=>n.type==='button'&&/批准本次|拒绝本次/.test(text(n))));
 const history=nodes(tree).find(n=>n.props?.className==='execution-history');assert.ok(history);assert.ok(!history.props.open);
});

test('unknown kinds and missing turn/digest fail closed while preserving the request evidence',async()=>{
 for(const invalid of [{kind:'item/unknown/requestApproval'},{digest:'bad'},{turn_id:''}]){
  const h=harness({items:[{...request,...invalid}]});h.render();const tree=await h.settle();
  assert.match(text(tree),/不能提交决定/);assert.match(text(tree),/python3 tests.py/);
  assert.ok(!nodes(tree).some(n=>n.type==='button'&&text(n)==='批准本次请求'));assert.ok(h.calls.every(call=>call.method==='GET'));
 }
});

test('viewer, closed run and revoked writer have no live decision authority',async()=>{
 for(const props of [{writer:false},{run:{...run,status:'completed'}},{run:{...run,status:'paused'}}]){
  const h=harness({props});h.render();const tree=await h.settle();assert.match(text(tree),/只读记录/);assert.ok(!nodes(tree).some(n=>n.type==='textarea'));
 }
 const h=harness();h.render();let tree=await h.settle();field(tree,'确认本次操作范围 approval-a').props.onChange({target:{checked:true}});tree=h.render();const stale=button(tree,'批准本次请求').props.onClick;
 h.setProps({writer:false});await stale();assert.ok(h.calls.every(call=>call.method==='GET'));
});

test('switching run or employee hides old requests and rejects captured callbacks and late responses',async()=>{
 const replies=[];const h=harness({handler:(_url,method)=>method==='GET'?new Promise(resolve=>replies.push(resolve)):assert.fail('stale mutation')});
 h.render();replies[0]({items:[request]});let tree=await h.settle();field(tree,'确认本次操作范围 approval-a').props.onChange({target:{checked:true}});tree=h.render();const stale=button(tree,'批准本次请求').props.onClick;
 tree=h.setProps({agentId:'agent-b'});assert.doesNotMatch(text(tree),/python3 tests.py/);await stale();
 replies[1]({items:[request]});tree=await h.settle();assert.equal(nodes(tree).filter(n=>n.type==='article').length,0);
 tree=h.setProps({run:{...run,id:'run-b'}});assert.doesNotMatch(text(tree),/python3 tests.py/);replies[2]({items:[request]});tree=await h.settle();assert.equal(nodes(tree).filter(n=>n.type==='article').length,0);
});

test('read failures retain evidence but disable a previously captured approval action',async()=>{
 let fail=false;const h=harness({handler:(_url,method)=>method==='GET'?(fail?Promise.reject(new Error('连接中断')):{items:[request]}):assert.fail('mutation during failed sync')});
 h.render();let tree=await h.settle();field(tree,'确认本次操作范围 approval-a').props.onChange({target:{checked:true}});tree=h.render();const stale=button(tree,'批准本次请求').props.onClick;
 fail=true;button(tree,'刷新运行时审批').props.onClick();tree=await h.settle();assert.match(text(tree),/审批读取失败：连接中断/);assert.match(text(tree),/python3 tests.py/);
 await stale();assert.equal(button(tree,'批准本次请求').props.disabled,true);
});

test('ambiguous POST failures do not unlock a contradictory resubmission',async()=>{
 const h=harness({handler:(_url,method)=>method==='GET'?{items:[request]}:Promise.reject(new Error('network lost'))});h.render();let tree=await h.settle();
 field(tree,'确认本次操作范围 approval-a').props.onChange({target:{checked:true}});tree=h.render();await button(tree,'拒绝本次请求').props.onClick();tree=await h.settle();
 assert.match(text(tree),/提交结果未确认/);assert.equal(button(tree,'批准本次请求').props.disabled,true);assert.equal(h.calls.filter(call=>call.method==='POST').length,1);
});

test('hidden panel still reports pending count and Strict Mode replay remains scoped',async()=>{
 const h=harness({props:{visible:false}});assert.equal(h.render(),null);h.strictReplay();await h.settle();assert.equal(h.counts.at(-1),1);
 const tree=h.setProps({visible:true});assert.match(text(tree),/python3 tests.py/);h.unmount();
 const stale=button(tree,'刷新运行时审批').props.onClick;const before=h.calls.length;stale();assert.equal(h.calls.length,before);
});
