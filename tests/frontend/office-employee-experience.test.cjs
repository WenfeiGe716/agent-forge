const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const ts=require('../../frontend/vendor/typescript.cjs'),{createRequire}=require('node:module');
const nodes=v=>Array.isArray(v)?v.flatMap(nodes):v&&typeof v==='object'?[v,...nodes(v.props?.children)]:[];
const text=v=>Array.isArray(v)?v.map(text).join(''):v&&typeof v==='object'?text(v.props?.children):v==null||typeof v==='boolean'?'':String(v);
const button=(tree,label)=>{const n=nodes(tree).find(n=>n.type==='button'&&(n.props['aria-label']===label||text(n)===label));assert.ok(n,label);return n;};
function reuse(){const file=require.resolve('./organization-workspace.test.cjs'),local=createRequire(file),exports={};vm.runInNewContext(fs.readFileSync(file,'utf8')+'\nObject.assign(exports,{harness});',{exports,__dirname:path.dirname(file),console,performance,require:Object.assign(id=>id==='node:test'?()=>{}:local(id),{resolve:local.resolve})});return exports.harness;}
const studioHarness=reuse();
const baseRun={id:'run-a',title:'本次实际交付',request:'完成文档和测试',status:'completed',artifact_digest:'frozen',snapshot:{agents:{}},tasks:[],report:{},plan:{tasks:[]}};
function componentHarness(name,props={},handler){
 let cursor=0;const slots=[],pending=[],cleanups=[],calls=[],listeners={};
 const React={Component:class{constructor(props){this.props=props;this.state={};}setState(next){this.state={...this.state,...next};}},useState(v){const i=cursor++;if(!(i in slots))slots[i]=typeof v==='function'?v():v;return[slots[i],v=>slots[i]=typeof v==='function'?v(slots[i]):v];},useRef(v){const i=cursor++;return slots[i]||(slots[i]={current:v});},useEffect(fn,deps){const i=cursor++;if(!(i in slots)||deps.some((v,j)=>!Object.is(v,slots[i][j]))){cleanups[i]?.();slots[i]=deps;pending.push(()=>cleanups[i]=fn());}}};
 const jsx=(type,props)=>typeof type==='function'?(type.prototype?.render?{type,props:props||{}}:type(props||{})):{type,props:props||{}};
 const exports={},source=fs.readFileSync(path.resolve(__dirname,`../../frontend/src/components/${name}.tsx`),'utf8');
 const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;
 vm.runInNewContext(code,{exports,console,AbortController,TextDecoder,setInterval:()=>1,clearInterval(){},window:{innerWidth:1400,innerHeight:850,addEventListener:(n,fn)=>listeners[n]=fn,removeEventListener:n=>delete listeners[n]},fetch:()=>assert.fail('no file fetch before explicit preview'),require:id=>{
  if(id==='react')return React;if(id==='react/jsx-runtime')return{jsx,jsxs:jsx,Fragment:'fragment'};
  if(id==='../api')return{api:async(url,method='GET')=>{calls.push({url,method});return handler?handler(url,method):url.includes('/trace?')?{items:[{run_id:'run-a',id:1},{run_id:'other',id:2}]}:baseRun;}};
  if(id==='../taskPresentation')return{verificationSummary:()=>({checkLabel:'无验证',reviewLabel:'无审查',review:{},commands:[]})};
  if(id==='./UI')return{Modal:p=>jsx('modal',p),Badge:p=>jsx('badge',p)};
  if(id==='./TaskFlow')return{__esModule:true,default:p=>jsx('flow',p),TaskOutputs:p=>jsx('outputs',p)};
  if(id==='./EmployeeTaskPanel')return{__esModule:true,default:p=>jsx('employee-panel',p)};
  if(id==='./Icons')return new Proxy({},{get:(_,k)=>p=>jsx('icon-'+String(k),p)});
  throw Error(id);
 }});
 let current={id:'run-a',agent:{id:'a',name:'Nova'},runs:[baseRun],projects:[],detail:null,writer:true,admin:true,onClose(){},onBoard(){},onOpenRun(){},onTask(){},onNavigate(){},onRefresh(){},...props};
 const render=()=>{cursor=0;const tree=exports.default(current);pending.splice(0).forEach(fn=>fn());return tree;};
 return{calls,listeners,render,setProps(p){current={...current,...p};return render();},unmount(){cleanups.forEach(fn=>fn?.());},async settle(){await new Promise(r=>setImmediate(r));return render();}};
}

test('employee dock, floating session and review keep the same Office instance and do not navigate or write',()=>{
 const navigation=[],h=studioHarness('StudioWorkspace',{onBoard:()=>navigation.push('board'),onSelectRun:id=>navigation.push(id)});let tree=h.render(),office=nodes(tree).find(n=>n.type==='Office');
 office.props.controller.current={reset(){},focus(){}};office.props.onSelect('a');tree=h.render();
 assert.equal(nodes(tree).find(n=>n.type==='Office').props.controller,office.props.controller);
 assert.ok(nodes(tree).some(n=>n.props?.['aria-label']==='当前选中员工'));
 nodes(tree).find(n=>n.type==='Office').props.onCameraModeChange('following','a');tree=h.render();assert.match(text(button(tree,'跟随选中员工')),/跟随中/);
 button(tree,'打开选中员工会话').props.onClick();tree=h.render();
 const session=nodes(tree).find(n=>n.props?.onOpenRun&&n.props?.onClose&&n.props?.agent);assert.ok(session);assert.equal(session.props.agent.id,'a');
 session.props.onOpenRun('exact-run');tree=h.render();const review=nodes(tree).find(n=>n.props?.id==='exact-run'&&n.props?.onClose);assert.ok(review);assert.equal(navigation.length,0);
 review.props.onClose();tree=h.render();assert.equal(nodes(tree).find(n=>n.type==='Office').props.controller,office.props.controller);assert.equal(nodes(tree).find(n=>n.type==='Office').props.selectedId,'a');
 session.props.onClose();tree=h.render();assert.equal(nodes(tree).filter(n=>n.props?.onOpenRun&&n.props?.onClose&&n.props?.agent).length,0);assert.equal(navigation.length,0);
});

test('unseated employee cannot invoke the dock camera action or invent a desk',()=>{
 const h=studioHarness('StudioWorkspace',{organization:{config:{floors:[],teams:[],desks:[],positions:{}}}});let tree=h.render();nodes(tree).find(n=>n.type==='Office').props.controller.current={reset(){}};nodes(tree).find(n=>n.type==='Office').props.onSelect('a');tree=h.render();assert.equal(button(tree,'跟随选中员工').props.disabled,true);assert.match(text(button(tree,'跟随选中员工')),/未安排工位/);
});

test('switching from task B to floating communication and back preserves the employee task, not global task A',()=>{
 const h=studioHarness('StudioWorkspace');let tree=h.render();nodes(tree).find(n=>n.type==='Office').props.controller.current={reset(){},focus(){}};nodes(tree).find(n=>n.type==='Office').props.onSelect('a');tree=h.render();
 const panel=nodes(tree).find(n=>n.props?.initialTab==='work'&&n.props?.onRunChange);assert.ok(panel);panel.props.onRunChange('task-b');tree=h.render();
 button(tree,'打开选中员工会话').props.onClick();tree=h.render();const window=nodes(tree).find(n=>n.props?.onClose&&n.props?.agent);assert.equal(window.props.initialRunId,'task-b');window.props.onRunChange('task-c');
 button(h.render(),'查看选中员工任务').props.onClick();tree=h.render();const returned=nodes(tree).find(n=>n.props?.initialTab==='work'&&n.props?.onRunChange);assert.equal(returned.props.initialRunId,'task-c');
});

test('office review reads the exact run, filters foreign trace and exposes no approval or execution commands',async()=>{
 const h=componentHarness('OfficeRunReview');h.render();const tree=await h.settle();assert.match(text(tree),/本次实际交付/);assert.ok(h.calls.every(c=>c.method==='GET'));assert.deepEqual(h.calls.map(c=>c.url),['/runs/run-a','/runs/run-a/trace?limit=100']);
 const outputs=nodes(tree).find(n=>n.type==='outputs');assert.equal(outputs.props.run.id,'run-a');assert.deepEqual(Array.from(outputs.props.events,e=>e.id),[1]);
 assert.ok(nodes(tree).filter(n=>n.type==='a').every(n=>n.props.href.startsWith('/api/runs/run-a/artifacts/')));assert.equal(nodes(tree).filter(n=>['approvals','permissions'].includes(n.type)).length,0);
 button(tree,'执行计划与进度').props.onClick();assert.equal(nodes(h.render()).find(n=>n.type==='flow').props.run.id,'run-a');h.unmount();
});

test('mismatched run and late response after unmount cannot render foreign results',async()=>{
 const bad=componentHarness('OfficeRunReview',{},url=>url.includes('/trace?')?{items:[]}:{...baseRun,id:'wrong'});bad.render();const tree=await bad.settle();assert.match(text(tree),/范围不匹配/);assert.equal(nodes(tree).filter(n=>n.type==='outputs'||n.type==='flow'||n.type==='a').length,0);
 let resolve;const late=componentHarness('OfficeRunReview',{},url=>url.includes('/trace?')?{items:[]}:new Promise(r=>resolve=r));late.render();late.unmount();resolve(baseRun);await late.settle();assert.doesNotMatch(text(late.render()),/本次实际交付/);
});

test('scene preview review never reads production records',()=>{const h=componentHarness('OfficeRunReview',{preview:true});assert.match(text(h.render()),/不读取真实任务成果/);assert.equal(h.calls.length,0);});

test('session floating window preserves employee task scoping and drags within the office without any network call',()=>{
 const h=componentHarness('OfficeSessionWindow',{initialRunId:'run-a'});let tree=h.render();const panel=nodes(tree).find(n=>n.type==='employee-panel');assert.equal(panel.props.agent.id,'a');assert.equal(panel.props.initialRunId,'run-a');assert.equal(panel.props.initialTab,'messages');
 tree.props.ref.current={offsetLeft:52,offsetTop:193,offsetWidth:390,offsetHeight:470,offsetParent:{clientWidth:1400,clientHeight:850}};
 const handle=nodes(tree).find(n=>n.props?.className==='office-window-handle'),capture={setPointerCapture(){},hasPointerCapture:()=>true,releasePointerCapture(){}};
 handle.props.onPointerDown({button:0,target:{closest:()=>null},clientX:100,clientY:200,pointerId:1,currentTarget:capture,preventDefault(){}});
 handle.props.onPointerMove({clientX:5000,clientY:5000});tree=h.render();assert.equal(tree.props.style.left,998);assert.equal(tree.props.style.top,368);
 handle.props.onPointerUp({pointerId:1,currentTarget:capture});handle.props.onPointerMove({clientX:0,clientY:0});assert.equal(h.render().props.style.left,998);assert.equal(h.calls.length,0);h.unmount();assert.equal(Object.keys(h.listeners).length,0);
});
