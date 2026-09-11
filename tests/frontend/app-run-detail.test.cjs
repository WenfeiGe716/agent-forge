const assert=require('node:assert/strict'),test=require('node:test'),fs=require('node:fs'),vm=require('node:vm');
const ts=require('../../frontend/vendor/typescript.cjs');
const source=fs.readFileSync(require.resolve('../../frontend/src/App.tsx'),'utf8');
const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText;
const user={id:'user-1',username:'admin',role:'admin',enabled:true};
const run=(id,status,cursor=1)=>({id,status,event_cursor:cursor,commands:[],tasks:[]});
function harness(){
 let cursor=0,selected='run-a',owner=user;const slots=[],effects=[],cleanups=[],setups=[],pending=[];
 const changed=(a,b)=>!a||b.some((value,index)=>!Object.is(value,a[index]));
 const React={useState(initial){const index=cursor++;if(!(index in slots))slots[index]=typeof initial==='function'?initial():initial;return[slots[index],value=>slots[index]=typeof value==='function'?value(slots[index]):value];},useRef(initial){const index=cursor++;return slots[index]||(slots[index]={current:initial});},useEffect(effect,deps){const index=cursor++;if(changed(slots[index],deps)){cleanups[index]?.();slots[index]=deps;effects.push(()=>{setups[index]=effect;cleanups[index]=effect();});}},useCallback(callback,deps){const index=cursor++;if(changed(slots[index]?.deps,deps))slots[index]={callback,deps};return slots[index].callback;}};
 const exports={};
 vm.runInNewContext(compiled,{exports,encodeURIComponent,require:id=>id==='react'?React:id==='./api'?{api:url=>new Promise((resolve,reject)=>pending.push({url,resolve,reject}))}:id==='react/jsx-runtime'?{}:{}});
 const render=()=>{cursor=0;const value=exports.useRunDetail(selected,owner);effects.splice(0).forEach(effect=>effect());return value;};
 return{pending,render,select(id){selected=id;return render();},login(next){owner=next;return render();},unmount(){cleanups.forEach(cleanup=>cleanup?.());},strictReplay(){cleanups.forEach(cleanup=>cleanup?.());setups.forEach((setup,index)=>{cleanups[index]=setup();});}};
}

test('polling and manual reads share last-request-wins ordering',async()=>{
 const h=harness();const read=h.render().load,older=read(),newer=read();
 h.pending[1].resolve(run('run-a','completed',20));await newer;assert.equal(h.render().detail.status,'completed');
 h.pending[0].resolve(run('run-a','awaiting_acceptance',19));await older;assert.equal(h.render().detail.status,'completed');assert.equal(h.render().detail.event_cursor,20);
});

test('even a response arriving first is ignored once a newer request started',async()=>{
 const h=harness();const read=h.render().load,older=read(),newer=read();
 h.pending[0].resolve(run('run-a','awaiting_approval'));await older;assert.equal(h.render().detail,null);
 h.pending[1].resolve(run('run-a','running'));await newer;assert.equal(h.render().detail.status,'running');
});

test('switching selected tasks hides old detail immediately and rejects late responses',async()=>{
 const h=harness();let current=h.render();const first=current.load();h.pending[0].resolve(run('run-a','running'));await first;
 current=h.render();const late=current.load();assert.equal(h.select('run-b').detail,null);
 const next=h.render().load();h.pending[2].resolve(run('run-b','planning'));await next;
 h.pending[1].resolve(run('run-a','completed'));await late;assert.equal(h.render().detail.id,'run-b');assert.equal(h.render().detail.status,'planning');
});

test('A to B to A cannot restore the first A response or completed snapshot',async()=>{
 const h=harness();const first=h.render().load();h.select('run-b');assert.equal(h.select('run-a').detail,null);
 const latest=h.render().load();h.pending[1].resolve(run('run-a','completed',8));await latest;h.pending[0].resolve(run('run-a','planning',2));await first;assert.equal(h.render().detail.status,'completed');
});

test('an old manual refresh callback cannot read a different selected task',async()=>{
 const h=harness();const oldRead=h.render().load;h.select('run-b');await oldRead();assert.equal(h.pending.length,0);
 const next=h.render().load();assert.equal(h.pending[0].url,'/runs/run-b');h.pending[0].resolve(run('run-b','running'));await next;
});

test('logout and a new login with the same user ID invalidate the old session',async()=>{
 const h=harness();const oldRead=h.render().load,old=oldRead();assert.equal(h.login(null).detail,null);await h.render().load();assert.equal(h.pending.length,1);
 assert.equal(h.login({...user}).detail,null);const next=h.render().load();h.pending[1].resolve(run('run-a','reviewing'));await next;h.pending[0].resolve(run('run-a','awaiting_approval'));await old;assert.equal(h.render().detail.status,'reviewing');
});

test('stale unauthorized failures do not escape to log out the current session',async()=>{
 const h=harness();const old=h.render().load();h.login({...user,id:'user-2'});h.pending[0].reject(new Error('401 old session'));await assert.doesNotReject(old);assert.equal(h.render().detail,null);
});

test('current failures propagate to existing error UI without reviving old responses',async()=>{
 const h=harness();const read=h.render().load,older=read(),newer=read();h.pending[1].reject(new Error('current connection failure'));await assert.rejects(newer,/current connection failure/);
 h.pending[0].resolve(run('run-a','planning'));await older;assert.equal(h.render().detail,null);
});

test('a response for another task is rejected and never committed',async()=>{
 const h=harness();const read=h.render().load();h.pending[0].resolve(run('run-b','completed'));await assert.rejects(read,/当前任务不一致/);assert.equal(h.render().detail,null);
});

test('unmount suppresses pending completion and callbacks',async()=>{
 const h=harness();const callback=h.render().load,pending=callback();h.unmount();h.pending[0].resolve(run('run-a','completed'));assert.equal(await pending,undefined);await callback();assert.equal(h.pending.length,1);
});

test('re-renders retain the callback and snapshot within one selection and session',async()=>{
 const h=harness();const before=h.render();assert.equal(h.render().load,before.load);const pending=before.load();h.pending[0].resolve(run('run-a','running'));await pending;assert.equal(h.render().detail.status,'running');assert.equal(h.render().load,before.load);
});

test('App routes every detail read through the shared hook, including the manual refresh',()=>{
 assert.match(source,/const \{detail,load:loadDetail\}=useRunDetail\(selected,user\)/);
 assert.match(source,/const update=\(\)=>loadDetail\(\)\.catch\(fail\)/);
 assert.match(source,/onRefresh=\{async\(\)=>\{await refresh\(\);await loadDetail\(\);\}\}/);
 assert.doesNotMatch(source,/setDetail\(/);
 assert.equal((source.match(/api<Run>\(/g)||[]).length,1);
});

test('React Strict Mode effect cleanup and replay do not strand the detail reader',async()=>{
 const h=harness();const callback=h.render().load,old=callback();h.strictReplay();const next=callback();assert.equal(h.pending.length,2);
 h.pending[1].resolve(run('run-a','completed'));await next;h.pending[0].resolve(run('run-a','planning'));await old;assert.equal(h.render().detail.status,'completed');
});
