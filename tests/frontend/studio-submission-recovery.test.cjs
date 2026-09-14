const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const {createRequire}=require('node:module');
const filename=require.resolve('./organization-workspace.test.cjs'),local=createRequire(filename),studio={};
vm.runInNewContext(fs.readFileSync(filename,'utf8')+'\nObject.assign(exports,{harness,nodes,text,field,button});',{exports:studio,__dirname:path.dirname(filename),console,performance,Date,setImmediate,URL,require:Object.assign(id=>id==='node:test'?()=>{}:local(id),{resolve:local.resolve})},{filename});
const ts=require('../../frontend/vendor/typescript.cjs'),submission={exports:{}};
vm.runInNewContext(ts.transpileModule(fs.readFileSync('frontend/src/runSubmission.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,{module:submission,exports:submission.exports,require:()=>({}),Error,Promise,JSON,Map,Object});
const {createRunSubmission}=submission.exports;
const people=[{id:'a',name:'负责人甲',role:'developer',runtime:{},enabled:true},{id:'b',name:'负责人乙',role:'developer',runtime:{},enabled:true}];
const organization={ref:{id:'office',version:3,digest:'a'.repeat(64)},config:{floors:[{floor:1,department:'研发部',name:'研发楼层',zones:[]}],positions:{},teams:[{id:'team-a',name:'团队甲',floor:1,member_ids:['a'],headcount:1},{id:'team-b',name:'团队乙',floor:1,member_ids:['b'],headcount:1}]}};
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const findButton=(tree,label)=>studio.nodes(tree).find(node=>node.type==='button'&&studio.text(node)===label);
function fixture(onCreateTask){const configured=[],selected=[];let refreshed=0;const h=studio.harness('StudioWorkspace',{agents:people,organization,projects:[{id:'p',name:'项目甲'}],runs:[],onCreateTask,onConfigureTask:seed=>configured.push(seed),onSelectRun:id=>selected.push(id),onRefresh:()=>refreshed++});let tree=h.render();studio.field(tree,'任务项目').props.onChange({target:{value:'p'}});studio.field(tree,'需求办公团队').props.onChange({target:{value:'team-a'}});tree=h.render();studio.field(tree,'指令收件人').props.onChange({target:{value:'a'}});studio.field(tree,'任务需求').props.onChange({target:{value:'  原始页面游戏需求  '}});return{h,configured,selected,get refreshed(){return refreshed;}};}
function reopen(h){let tree=h.render();const back=studio.nodes(tree).find(node=>node.type==='button'&&node.props.className?.includes('studio-composer-entry'));back?.props.onClick();return h.render();}

test('uncertain send retries the same actual submission body/key, never the edited team or draft',async()=>{
 const calls=[],keys=[];let attempts=0,release;const send=createRunSubmission(async(url,method,body,headers)=>{
  if(url==='/execution/preflight')return {ready:true,checks:[]};keys.push(headers['Idempotency-Key']);
  if(!attempts++)throw new Error('network response lost');return new Promise(resolve=>release=()=>resolve({id:'one-run'}));
 },()=> 'one-key');
 const f=fixture(async seed=>{calls.push(JSON.parse(JSON.stringify(seed)));await send(seed);send.acknowledge(seed);return true;});
 studio.field(f.h.render(),'创建任务').props.onClick();await tick();let tree=f.h.render();
 assert.equal(findButton(tree,'检查与修改配置'),undefined);assert.ok(findButton(tree,'重试原需求并确认结果'));assert.equal(studio.field(tree,'创建任务').props.disabled,true);
 studio.field(tree,'任务需求').props.onChange({target:{value:'下一条新任务，请保留'}});studio.field(tree,'需求办公团队').props.onChange({target:{value:'team-b'}});tree=f.h.render();studio.field(tree,'指令收件人').props.onChange({target:{value:'b'}});tree=f.h.render();
 studio.field(tree,'创建任务').props.onClick();await tick();assert.equal(calls.length,1);assert.match(studio.text(f.h.render()),/上一条需求.*仍未确认/);
 const retry=findButton(f.h.render(),'重试原需求并确认结果');retry.props.onClick();retry.props.onClick();await tick();assert.equal(calls.length,2);assert.deepEqual(calls[1],calls[0]);
 release();await tick();tree=reopen(f.h);
 assert.equal(studio.field(tree,'任务需求').props.value,'下一条新任务，请保留');assert.equal(studio.field(tree,'需求办公团队').props.value,'team-b');assert.equal(studio.field(tree,'指令收件人').props.value,'b');
 assert.equal(findButton(tree,'重试原需求并确认结果'),undefined);assert.deepEqual(keys,['one-key','one-key']);assert.equal(f.configured.length,0);
});

test('an uncertain result only clears the unchanged original draft after explicit successful confirmation',async()=>{
 let attempts=0;const f=fixture(async()=>{if(!attempts++)throw Object.assign(new Error('结果未确认'),{submissionUncertain:true});return true;});
 studio.field(f.h.render(),'创建任务').props.onClick();await tick();findButton(f.h.render(),'重试原需求并确认结果').props.onClick();await tick();
 const tree=reopen(f.h);assert.equal(studio.field(tree,'任务需求').props.value,'');assert.equal(findButton(tree,'重试原需求并确认结果'),undefined);assert.equal(attempts,2);
});

test('false or another failed retry does not claim success or discard the original recovery state',async()=>{
 let attempts=0;const f=fixture(async()=>{attempts++;if(attempts===1)throw Object.assign(new Error('结果未确认'),{submissionUncertain:true});if(attempts===2)return false;throw Error('暂时不能确认');});
 studio.field(f.h.render(),'创建任务').props.onClick();await tick();findButton(f.h.render(),'重试原需求并确认结果').props.onClick();await tick();let tree=f.h.render();
 assert.ok(findButton(tree,'重试原需求并确认结果'));assert.equal(studio.field(tree,'任务需求').props.value,'  原始页面游戏需求  ');
 findButton(tree,'重试原需求并确认结果').props.onClick();await tick();tree=f.h.render();assert.ok(findButton(tree,'重试原需求并确认结果'));assert.equal(findButton(tree,'检查与修改配置'),undefined);
});

test('ordinary deterministic errors retain configuration repair and never show uncertain retry controls',async()=>{
 const f=fixture(async()=>{throw Object.assign(new Error('成员模型已停用'),{status:422});});studio.field(f.h.render(),'创建任务').props.onClick();await tick();const tree=f.h.render();
 assert.equal(findButton(tree,'重试原需求并确认结果'),undefined);findButton(tree,'修改配置').props.onClick();assert.equal(f.configured.length,1);assert.equal(f.configured[0].request,'原始页面游戏需求');assert.equal(studio.field(tree,'任务需求').props.value,'  原始页面游戏需求  ');
});

test('a conclusive retry rejection releases recovery and restores configuration repair',async()=>{
 for(const failure of [Object.assign(new Error('模型已停用'),{status:422}),Object.assign(new Error('执行条件不满足'),{readiness:{ready:false,checks:[{status:'error',code:'model.enabled',target:'models'}]}})]){
  let attempts=0;const f=fixture(async()=>{if(!attempts++)throw Object.assign(new Error('结果未确认'),{submissionUncertain:true});throw failure;});
  studio.field(f.h.render(),'创建任务').props.onClick();await tick();findButton(f.h.render(),'重试原需求并确认结果').props.onClick();await tick();const tree=f.h.render();
  assert.equal(findButton(tree,'重试原需求并确认结果'),undefined);assert.ok(findButton(tree,'修改配置'));assert.equal(studio.field(tree,'任务需求').props.value,'  原始页面游戏需求  ');
 }
});

test('backend submission errors offer a direct retry of the current draft, not an unrelated configuration dialog',async()=>{
 const captured=[];const f=fixture(async seed=>{captured.push(seed);if(captured.length===1)throw Object.assign(new Error('任务服务暂不可用'),{readiness:{ready:false,checks:[{status:'error',code:'run.snapshot',target:'runs'}]}});return {id:'retry-created'};});
 studio.field(f.h.render(),'创建任务').props.onClick();await tick();let tree=f.h.render();
 assert.equal(findButton(tree,'修改配置'),undefined);assert.equal(findButton(tree,'检查与修改配置'),undefined);
 studio.field(tree,'任务需求').props.onChange({target:{value:'修改后的当前需求'}});tree=f.h.render();
 findButton(tree,'重试发送').props.onClick();await tick();
 assert.equal(captured.length,2);assert.equal(captured[1].request,'修改后的当前需求');assert.equal(f.configured.length,0);
 assert.equal(studio.field(reopen(f.h),'任务需求').props.value,'');
});

test('viewing existing tasks remains in the composer and does not dismiss recovery or authorize a retry',async()=>{
 let attempts=0;const f=fixture(async()=>{attempts++;throw Object.assign(new Error('结果未确认'),{submissionUncertain:true});});studio.field(f.h.render(),'创建任务').props.onClick();await tick();
 f.h.props.runs=[{id:'already-created',title:'实际服务端任务',status:'running'}];let tree=f.h.render();findButton(tree,'刷新任务列表').props.onClick();findButton(tree,'实际服务端任务').props.onClick();assert.equal(f.refreshed,1);assert.deepEqual(f.selected,['already-created']);assert.equal(attempts,1);assert.ok(findButton(f.h.render(),'重试原需求并确认结果'));
 f.h.props.writer=false;tree=f.h.render();assert.equal(findButton(tree,'重试原需求并确认结果').props.disabled,true);findButton(tree,'重试原需求并确认结果').props.onClick();await tick();assert.equal(attempts,1);assert.equal(studio.field(f.h.render(),'任务需求').props.value,'  原始页面游戏需求  ');
});

test('successful direct creation pins its returned run before list and detail refresh',async()=>{
 const f=fixture(async()=>({id:'newly-created'}));
 f.h.props.detail={id:'old-paused',status:'paused',snapshot:{agents:{a:{}}}};
 studio.field(f.h.render(),'创建任务').props.onClick();await tick();
 const panel=studio.nodes(f.h.render()).find(n=>n.props?.initialRunId==='newly-created');
 assert.ok(panel,'must not fall back to the previous run during refresh');
 assert.equal(panel.props.agent.id,'a');assert.equal(panel.props.initialTab,'work');
 assert.match(studio.text(f.h.render()),/已发送，负责人将自动开始处理/);
 assert.doesNotMatch(studio.text(f.h.render()),/任务已开始/,'a creation receipt must not claim that execution has already started');
});
