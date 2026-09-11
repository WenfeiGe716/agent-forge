const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const ts=require('../../frontend/vendor/typescript.cjs');
const mod={exports:{}};
vm.runInNewContext(ts.transpileModule(fs.readFileSync('frontend/src/runSubmission.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,{module:mod,exports:mod.exports,require:()=>({}),Error,Promise,JSON,Map,Object});
const {createRunSubmission,RunPreflightError}=mod.exports;
const seed={project_id:'p',title:'明确需求',request:'编写页面游戏',agent_ids:['a','b'],entry_agent_id:'a',office_team_id:'team',office_layout_ref:{id:'o',version:1,digest:'d'},permission_profile:'full',orchestration:{}};
test('direct send checks then starts exactly the explicit team without privileged overrides',async()=>{
 const calls=[],send=createRunSubmission(async(...args)=>{calls.push(args);return args[0]==='/execution/preflight'?{ready:true,checks:[]}:{id:'run'};},()=> 'one');
 assert.equal((await send(seed)).id,'run');assert.deepEqual(calls.map(c=>c[0]),['/execution/preflight','/runs']);
 assert.deepEqual(calls[0][2],calls[1][2]);assert.equal(calls[1][3]['Idempotency-Key'],'one');
 assert.equal(calls[1][2].permission_profile,undefined);assert.equal(calls[1][2].orchestration,undefined);
 assert.equal(JSON.stringify(calls[1][2].agent_ids),'["a","b"]');
});
test('blocked preflight retains structured context and does not create a run',async()=>{
 let n=0;const readiness={ready:false,checks:[{agent_id:'a',message:'模型未启用',status:'error'}]};
 const send=createRunSubmission(async()=>{n++;return readiness;},()=> 'one');
 await assert.rejects(send(seed),e=>e instanceof RunPreflightError&&e.readiness===readiness);assert.equal(n,1);
});
test('double click shares pending operation, lost response retry uses the same creation key',async()=>{
 let release,attempts=0;const keys=[];
 const send=createRunSubmission(async(url,method,body,headers)=>{if(url==='/execution/preflight')return {ready:true,checks:[]};keys.push(headers['Idempotency-Key']);if(!attempts++)return new Promise((r,j)=>{release=()=>j(new Error('网络中断'));});return {id:'same-run'};},()=> 'stable');
 const first=send(seed),second=send(seed);assert.equal(first,second);await new Promise(r=>setImmediate(r));release();await assert.rejects(first,e=>e.submissionUncertain===true);
 assert.equal((await send(seed)).id,'same-run');assert.deepEqual(keys,['stable','stable']);
 assert.equal((await send(seed)).id,'same-run');assert.equal(keys.length,2);
});
test('a different pending request and changed login cannot silently create a task',async()=>{
 let release,current=true,created=0;const send=createRunSubmission(async url=>url==='/execution/preflight'?new Promise(r=>release=r):(created++,{id:'bad'}),()=> 'id');
 const first=send(seed,()=>current);await assert.rejects(send({...seed,request:'另一个需求'}),/正在提交/);current=false;release({ready:true,checks:[]});
 await assert.rejects(first,/登录状态/);assert.equal(created,0);
});

test('lost creation response is recovered using the same key even if current preflight would now fail',async()=>{
 let preflights=0,posts=0;const keys=[];
 const send=createRunSubmission(async(url,method,body,headers)=>{
  if(url==='/execution/preflight'){preflights++;return {ready:preflights===1,checks:[{status:'error',message:'模型后来已停用'}]};}
  keys.push(headers['Idempotency-Key']);if(!posts++)throw Error('response was lost after server created the run');return {id:'already-created'};
 },()=> 'key-a');
 await assert.rejects(send(seed),e=>e.submissionUncertain===true);assert.equal(send.acknowledge(seed),false);
 assert.equal((await send(seed)).id,'already-created');assert.equal(preflights,1);assert.deepEqual(keys,['key-a','key-a']);
});

test('successful B acknowledgement never forgets uncertain A; a later explicit same request can get a new key',async()=>{
 let sequence=0;const calls=[],seen=new Set();const other={...seed,request:'另一个真实需求'};
 const send=createRunSubmission(async(url,method,body,headers)=>{
  calls.push({url,body,key:headers?.['Idempotency-Key']});if(url==='/execution/preflight')return {ready:true,checks:[]};
  const key=headers['Idempotency-Key'];if(key==='key-1'&&!seen.has(key)){seen.add(key);throw Error('lost A');}
  return {id:'run-'+key};
 },()=> 'key-'+ ++sequence);
 await assert.rejects(send(seed),e=>e.submissionUncertain===true);
 assert.equal((await send(other)).id,'run-key-2');assert.equal(send.acknowledge(other),true);
 assert.equal((await send(seed)).id,'run-key-1');assert.equal(sequence,2);
 assert.equal(send.acknowledge(seed),true);assert.equal((await send(seed)).id,'run-key-3');
 assert.deepEqual(calls.filter(c=>c.url==='/runs').map(c=>c.key),['key-1','key-2','key-1','key-3']);
});

test('explicit server rejection is re-preflighted, but malformed success stays uncertain',async()=>{
 let preflights=0,posts=0;const send=createRunSubmission(async url=>{
  if(url==='/execution/preflight'){preflights++;return {ready:true,checks:[]};}
  posts++;if(posts===1)throw Object.assign(new Error('rejected'),{status:422});if(posts===2)return {};return {id:'found'};
 },()=> 'one');
 await assert.rejects(send(seed),e=>e.message==='rejected'&&!e.submissionUncertain);
 await assert.rejects(send(seed),e=>e.submissionUncertain===true&&e.message.includes('未收到任务编号'));
 assert.equal((await send(seed)).id,'found');assert.equal(preflights,2);assert.equal(posts,3);
});

test('draft changes during preflight cannot alter the captured team layout or members',async()=>{
 const draft=JSON.parse(JSON.stringify(seed));let release,posted;
 const send=createRunSubmission(async(url,method,body)=>url==='/execution/preflight'?new Promise(resolve=>release=resolve):(posted=body,{id:'run'}),()=> 'one');
 const task=send(draft);draft.agent_ids.push('unauthorized-extra');draft.office_layout_ref.digest='changed';
 release({ready:true,checks:[]});await task;
 assert.deepEqual(Array.from(posted.agent_ids),['a','b']);assert.equal(posted.office_layout_ref.digest,'d');
});

test('malformed readiness and an obsolete completed callback cannot create or expose a task',async()=>{
 for(const invalid of [{ready:'yes',checks:[]},{ready:true},{},null]){
  const calls=[];const send=createRunSubmission(async url=>{calls.push(url);return invalid;},()=> 'one');
  await assert.rejects(send(seed),/响应不完整/);assert.deepEqual(calls,['/execution/preflight']);
 }
 const send=createRunSubmission(async url=>url==='/execution/preflight'?{ready:true,checks:[]}:{id:'old-run'},()=> 'one');
 await send(seed);await assert.rejects(send(seed,()=>false),/登录状态/);
});

test('uncertain POST failures carry an explicit safe flag without exposing upstream exception contents',async()=>{
 for(const status of [undefined,408,500,502]){
  const send=createRunSubmission(async url=>{
   if(url==='/execution/preflight')return {ready:true,checks:[]};
   throw Object.assign(new Error('internal-api-credential=fixture-secret'),{status});
  },()=> 'one');
  await assert.rejects(send(seed),error=>error.submissionUncertain===true&&!error.message.includes('fixture-secret'));
 }
 const send=createRunSubmission(async()=>{throw Error('preflight disconnected');},()=> 'one');
 await assert.rejects(send(seed),error=>!error.submissionUncertain&&error.message==='preflight disconnected');
});
