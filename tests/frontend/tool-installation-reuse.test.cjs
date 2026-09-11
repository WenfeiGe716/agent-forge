const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const {createRequire}=require('node:module');
const filename=require.resolve('./execution-center.test.cjs'),local=createRequire(filename),helpers={};
vm.runInNewContext(fs.readFileSync(filename,'utf8')+'\nObject.assign(exports,{harness,nodes,text,button,field,installation,manifest});',{exports:helpers,__dirname:path.dirname(filename),console,Date,URL,setImmediate,require:Object.assign(id=>id==='node:test'?()=>{}:local(id),{resolve:local.resolve})},{filename});
const {harness,nodes,text,button,field,installation,manifest}=helpers;
const pending={...installation,id:'pending'},approved={...installation,id:'already-approved',status:'approved',reason:'Already reviewed for this task'},merged={...pending,status:'superseded',result:{reused_installation_id:approved.id}};
const label='复用本任务已批准许可';
const reuseButtons=tree=>Array.from(nodes(tree)).filter(n=>n.type==='button'&&text(n)===label);
function setup(rows=[pending,approved],handler,props={}){return harness('ExecutionCenter',{props,handler:(url,method,body)=>{if(handler){const result=handler(url,method,body);if(result!==undefined)return result;}if(method==='GET'&&url.startsWith('/execution/permissions'))return{items:[]};if(method==='GET'&&url.startsWith('/tool-installations'))return{items:rows};}});}

test('matching prior approval offers an explicit reuse action without installation or renewed approval',async()=>{
 let rows=[pending,approved];const h=setup(rows,(url,method)=>{if(method==='POST'&&url.endsWith('/reuse-approved')){rows=[merged,approved];return merged;}if(method==='GET'&&url.startsWith('/tool-installations'))return{items:rows};});h.render();let tree=await h.settle();
 assert.equal(reuseButtons(tree).length,1);assert.match(text(tree),/不.*新增.*授权|不.*新授权/);assert.ok(h.calls.every(c=>c.method==='GET'));
 await button(tree,label).props.onClick();tree=await h.settle();const writes=Array.from(h.calls).filter(c=>c.method==='POST');
 assert.equal(writes.length,1);assert.equal(writes[0].url,'/tool-installations/pending/reuse-approved');assert.deepEqual(JSON.parse(JSON.stringify(writes[0].body)),{digest:pending.digest,installation_id:approved.id});
 assert.match(text(tree),/已合并 · 使用已有许可/);assert.match(text(tree),/already-approved/);assert.equal(reuseButtons(tree).length,0);
 assert.ok(!nodes(tree).some(n=>n.props?.['aria-label']==='确认已复核精确命令与权限'));
});

test('reuse requires exact run, task, agent, and entire manifest, not merely a package name or source hash',async()=>{
 for(const patch of[{run_id:'other'},{task_key:'other'},{agent_id:'other'},{manifest:{...manifest,files:{'main.py':'different source'}}},{manifest:{...manifest,network:'unrestricted'}},{status:'pending'},{status:'installing'},{status:'uncertain'},{status:'rejected'},{status:'superseded'}]){
  const h=setup([pending,{...approved,...patch}]);h.render();const tree=await h.settle();assert.equal(reuseButtons(tree).length,0,JSON.stringify(patch));assert.ok(h.calls.every(c=>c.method==='GET'));
 }
});

test('installed approvals can be reused and equivalent manifest key order is not a mismatch',async()=>{
 const reordered=Object.fromEntries(Object.entries(manifest).reverse());const h=setup([pending,{...approved,status:'installed',manifest:reordered}]);h.render();const tree=await h.settle();assert.equal(reuseButtons(tree).length,1);assert.match(text(tree),/already-approved/);
});

test('a rejected reuse stays visible and does not fall back to approving or rejecting the request',async()=>{
 const h=setup(undefined,(url,method)=>method==='POST'?Promise.reject(new Error('409 许可状态已变化，请重新读取')):undefined);h.render();let tree=await h.settle();await button(tree,label).props.onClick();tree=await h.settle();assert.match(text(tree),/许可状态已变化/);tree=h.render();assert.match(text(tree),/许可状态已变化/);assert.equal(reuseButtons(tree).length,1);assert.equal(h.calls.filter(c=>c.method==='POST').length,1);assert.ok(!h.calls.some(c=>c.url.endsWith('/review')));
});

test('viewers cannot reuse an approval and the original administrator review flow remains available',async()=>{
 const viewer=setup(undefined,undefined,{admin:false});viewer.render();assert.equal(reuseButtons(await viewer.settle()).length,0);
 const h=setup();h.render();let tree=await h.settle();button(tree,'审查后批准').props.onClick();tree=h.render();assert.ok(field(tree,'确认已复核精确命令与权限'));assert.equal(button(tree,'确认批准本次申请').props.disabled,true);assert.ok(h.calls.every(c=>c.method==='GET'));
});
