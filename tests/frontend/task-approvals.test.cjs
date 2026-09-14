const assert=require('node:assert/strict');
const test=require('node:test');
const fs=require('node:fs');
const vm=require('node:vm');
const ts=require('../../frontend/vendor/typescript.cjs');
const planDigest='a'.repeat(64),artifactDigest='b'.repeat(64);
const baseRun={id:'run-a',project_id:'project',title:'闭环验证',request:'先形成需求文档，再实现代码并保持原测试不变。',status:'awaiting_approval',plan_digest:planDigest,artifact_digest:artifactDigest,snapshot:{planner_id:'planner',reviewer_id:'reviewer',agents:{planner:{name:'负责人'},requirements:{name:'需求分析师'},engineer:{name:'研发工程师'},reviewer:{name:'质量审查员'}}},plan:{summary:'先明确验收边界，再实现与测试。',tasks:[{key:'requirements',title:'形成需求文档',description:'阅读现有测试，写出需求。',agent_id:'requirements',acceptance:'真实 docs/requirements.md',depends_on:[]},{key:'implementation',title:'实现代码与测试',description:'实现函数并运行测试。',agent_id:'engineer',acceptance:'保留原有 4 项测试并通过',depends_on:['requirements']}]},report:{checks:{verification:'passed'},review:{verdict:'pass',summary:'符合要求，原测试未修改。'}},commands:[]};

test('autonomous acceptance names the actual coordinator and keeps digest-bound human acceptance',async()=>{
 const h=harness({run:{...baseRun,status:'awaiting_acceptance',execution_strategy:'autonomous',snapshot:{...baseRun.snapshot,entry_agent_id:'requirements'},report:{mode:'autonomous',checks:{verification:'not_requested'},review:{verdict:'not_requested',summary:''}}}});let tree=h.render();
 assert.match(text(tree),/任务负责人需求分析师/);assert.match(text(tree),/未请求固定项目检查/);assert.match(text(tree),/本次未设置固定独立审查/);assert.doesNotMatch(text(tree),/独立审查通过/);assert.equal(button(tree,'确认验收通过').props.disabled,true);
 field(tree,'成果验收意见').props.onChange({target:{value:'已核对需求文档及实际文件摘要'}});tree=h.render();await button(tree,'确认验收通过').props.onClick();assert.equal(h.calls.length,1);assert.equal(h.calls[0].url,'/runs/run-a/decision/acceptance');assert.equal(h.calls[0].body.digest,artifactDigest);
});

test('autonomous acceptance never labels absent, failed or truncated check receipts as passed',()=>{
 for(const commands of [[],[{exit_code:1}],[{exit_code:0,truncated:true}],[{exit_code:0,timed_out:true}]]){
  const h=harness({run:{...baseRun,execution_strategy:'autonomous',status:'awaiting_acceptance',report:{mode:'autonomous',checks:{verification:'passed',commands},review:{verdict:'not_requested'}}}});
  assert.doesNotMatch(text(h.render()),/已通过配置的验收命令/);assert.match(text(h.render()),/未验证/);
 }
});

test('mode-only autonomous acceptance uses the actual entry owner',()=>{
 const h=harness({run:{...baseRun,mode:'autonomous',status:'awaiting_acceptance',snapshot:{...baseRun.snapshot,entry_agent_id:'requirements'},report:{checks:{verification:'not_requested'},review:{verdict:'not_requested'}}}});const tree=h.render();assert.match(text(tree),/任务负责人需求分析师/);assert.doesNotMatch(text(tree),/独立审查员质量审查员/);
});

test('Codex runtime awaiting approval cannot enter the plan decision API',()=>{
 for(const patch of [{execution_engine:'codex'},{snapshot:{...baseRun.snapshot,execution_engine:'codex'}}]){
  const h=harness({run:{...baseRun,...patch}});assert.equal(h.render(),null);assert.equal(h.calls.length,0);
 }
});
function nodes(value){if(Array.isArray(value))return value.flatMap(nodes);if(!value||typeof value!=='object')return[];return[value,...nodes(value.props?.children)];}
function text(value){if(Array.isArray(value))return value.map(text).join('');if(value&&typeof value==='object')return text(value.props?.children);return value==null?'':String(value);}
function field(tree,label){const result=nodes(tree).find(node=>node.props?.['aria-label']===label);assert.ok(result,'field '+label);return result;}
function button(tree,label){const result=nodes(tree).find(node=>node.type==='button'&&text(node)===label);assert.ok(result,'button '+label);return result;}
function harness({run=baseRun,writer=true,handler,refresh}={}){
 let cursor=0,tree,props={run,writer},refreshes=0;
 const slots=[],effects=[],cleanups=[],setups=[],calls=[];
 const React={useState(initial){const index=cursor++;if(!(index in slots))slots[index]=typeof initial==='function'?initial():initial;return[slots[index],next=>{slots[index]=typeof next==='function'?next(slots[index]):next;}];},useRef(initial){const index=cursor++;if(!(index in slots))slots[index]={current:initial};return slots[index];},useEffect(effect,deps){const index=cursor++;if(!(index in slots)||deps.some((value,i)=>!Object.is(value,slots[index][i]))){cleanups[index]?.();slots[index]=deps;effects.push(()=>{setups[index]=effect;cleanups[index]=effect();});}}};
 const jsx=(type,props)=>({type,props:props||{}});
 const exports={};
 const source=fs.readFileSync(require.resolve('../../frontend/src/components/TaskApprovals.tsx'),'utf8');
 const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText;
 const api=async(url,method,body)=>{calls.push({url,method,body});return handler?handler(url,method,body):{accepted:true,command_id:'decision-command'};};
 vm.runInNewContext(code,{exports,require:id=>id==='react'?React:id==='react/jsx-runtime'?{jsx,jsxs:jsx,Fragment:'Fragment'}:id==='../api'?{api}:assert.fail('unexpected module '+id),console,Promise,encodeURIComponent});
 const render=()=>{cursor=0;tree=exports.default({...props,onRefresh:async()=>{refreshes++;return refresh?.();}});effects.splice(0).forEach(fn=>fn());return tree;};
 return{calls,render,get refreshes(){return refreshes;},setProps(next){props={...props,...next};render();return render();},unmount(){cleanups.forEach(cleanup=>cleanup?.());},strictReplay(){cleanups.forEach(cleanup=>cleanup?.());setups.forEach((setup,index)=>{cleanups[index]=setup();});},async settle(){await new Promise(resolve=>setImmediate(resolve));return render();}};
}

test('plan explains owners, deliverables, dependencies and consequences without sending an approval',()=>{
 const h=harness();const tree=h.render();const content=text(tree);
 assert.match(content,/需求分析师/);assert.match(content,/研发工程师/);assert.match(content,/真实 docs\/requirements.md/);assert.match(content,/在 形成需求文档 完成后开始/);assert.match(content,/本次运行取消/);assert.match(content,/工具安装及额外权限仍按既定策略分别处理/);
 assert.ok(nodes(tree).filter(node=>node.type==='details').every(node=>!node.props.open));assert.equal(h.calls.length,0);
 assert.equal(button(tree,'批准计划并执行').props.disabled,false);
});

test('an explicit plan click sends the exact phase and digest, then waits for the server',async()=>{
 const h=harness();let tree=h.render();field(tree,'计划审批说明').props.onChange({target:{value:'  保留原测试  '}});tree=h.render();
 await button(tree,'批准计划并执行').props.onClick();tree=h.render();
 assert.equal(h.calls.length,1);assert.equal(h.calls[0].url,'/runs/run-a/decision/plan');assert.equal(h.calls[0].method,'POST');assert.deepEqual(JSON.parse(JSON.stringify(h.calls[0].body)),{approved:true,digest:planDigest,note:'保留原测试'});
 assert.equal(h.refreshes,1);assert.match(text(tree),/决定已提交，等待任务状态更新/);assert.equal(button(tree,'批准计划并执行').props.disabled,true);assert.equal(button(tree,'拒绝计划并结束').props.disabled,true);
});

test('acceptance requires a non-blank opinion for approval and rejection even when checks passed',async()=>{
 const h=harness({run:{...baseRun,status:'awaiting_acceptance'}});let tree=h.render();
 assert.equal(button(tree,'确认验收通过').props.disabled,true);assert.equal(button(tree,'验收不通过').props.disabled,true);
 await button(tree,'确认验收通过').props.onClick();assert.equal(h.calls.length,0);
 field(tree,'成果验收意见').props.onChange({target:{value:'   '}});tree=h.render();assert.equal(button(tree,'验收不通过').props.disabled,true);
 field(tree,'成果验收意见').props.onChange({target:{value:'核对源码、四项测试及审查回执，符合需求。'}});tree=h.render();await button(tree,'确认验收通过').props.onClick();
 assert.equal(h.calls[0].url,'/runs/run-a/decision/acceptance');assert.equal(h.calls[0].body.digest,artifactDigest);assert.equal(h.calls[0].body.approved,true);
});

test('unverified and discussion reports never imply that code was validated',()=>{
 const h=harness({run:{...baseRun,status:'awaiting_acceptance',report:{checks:{verification:'unverified'},review:{verdict:'rework'}}}});let tree=h.render();
 assert.match(text(tree),/未验证，不能视为通过/);assert.match(text(tree),/独立审查要求修改/);assert.match(text(tree),/本次运行停止并保留成果，不会自动返工/);
 tree=h.setProps({run:{...baseRun,status:'awaiting_acceptance',report:{mode:'discussion',checks:{verification:'unverified'}}}});
 assert.match(text(tree),/只读讨论，未执行代码验证/);assert.match(text(tree),/不代表代码已经实现或测试通过/);assert.doesNotMatch(text(tree),/独立审查通过/);
});

test('viewer can inspect scope but has no editing or approval controls',()=>{
 const h=harness({writer:false});const tree=h.render();assert.match(text(tree),/当前为只读查看/);assert.match(text(tree),/真实 docs\/requirements.md/);
 assert.ok(!nodes(tree).some(node=>node.type==='textarea'));assert.ok(!nodes(tree).some(node=>node.type==='button'));assert.equal(h.calls.length,0);
});

test('double clicks are suppressed before React re-renders',async()=>{
 let resolve;const h=harness({handler:()=>new Promise(done=>resolve=done)});const tree=h.render();const click=button(tree,'批准计划并执行').props.onClick;const first=click();await click();await button(tree,'拒绝计划并结束').props.onClick();assert.equal(h.calls.length,1);
 resolve({accepted:true,command_id:'one'});await first;assert.equal(button(h.render(),'批准计划并执行').props.disabled,true);
});

test('an unsent matching command disables repeat approval and exposes durable delivery errors',async()=>{
 const h=harness({run:{...baseRun,commands:[{action:'decide',sent:false,error:'执行器离线',payload:{phase:'plan',digest:planDigest,approved:true}}]}});const tree=h.render();
 assert.match(text(tree),/等待送达执行器/);assert.match(text(tree),/执行器离线/);assert.equal(button(tree,'批准计划并执行').props.disabled,true);await button(tree,'批准计划并执行').props.onClick();assert.equal(h.calls.length,0);
});

test('sent decisions still lock their version until the run advances; unrelated commands do not',()=>{
 const h=harness({run:{...baseRun,commands:[{action:'decide',sent:true,payload:{phase:'plan',digest:planDigest}}]}});let tree=h.render();assert.match(text(tree),/决定已送达/);assert.equal(button(tree,'拒绝计划并结束').props.disabled,true);
 tree=h.setProps({run:{...baseRun,commands:[{action:'decide',sent:false,payload:{phase:'acceptance',digest:planDigest}},{action:'decide',sent:false,payload:{phase:'plan',digest:artifactDigest}},{action:'control',sent:false,payload:{phase:'plan',digest:planDigest}}]}});assert.equal(button(tree,'批准计划并执行').props.disabled,false);
});

test('switching runs, phase or digest clears prior notes and decisions',async()=>{
 const h=harness();let tree=h.render();field(tree,'计划审批说明').props.onChange({target:{value:'只批准任务 A'}});tree=h.render();await button(tree,'批准计划并执行').props.onClick();
 tree=h.setProps({run:{...baseRun,id:'run-b'}});assert.equal(field(tree,'计划审批说明').props.value,'');assert.equal(button(tree,'批准计划并执行').props.disabled,false);assert.doesNotMatch(text(tree),/决定已提交/);
 field(tree,'计划审批说明').props.onChange({target:{value:'任务 B 的旧版本'}});tree=h.setProps({run:{...baseRun,id:'run-b',plan_digest:'c'.repeat(64)}});assert.equal(field(tree,'计划审批说明').props.value,'');
 field(tree,'计划审批说明').props.onChange({target:{value:'计划说明'}});tree=h.setProps({run:{...baseRun,id:'run-b',status:'awaiting_acceptance'}});assert.equal(field(tree,'成果验收意见').props.value,'');assert.equal(button(tree,'确认验收通过').props.disabled,true);
});

test('late success from a previous run never refreshes or locks the current run',async()=>{
 let resolve;const h=harness({handler:()=>new Promise(done=>resolve=done)});let tree=h.render();const promise=button(tree,'批准计划并执行').props.onClick();
 tree=h.setProps({run:{...baseRun,id:'run-b'}});field(tree,'计划审批说明').props.onChange({target:{value:'新任务意见'}});resolve({accepted:true,command_id:'old'});await promise;tree=h.render();
 assert.equal(h.refreshes,0);assert.equal(field(tree,'计划审批说明').props.value,'新任务意见');assert.equal(button(tree,'批准计划并执行').props.disabled,false);assert.doesNotMatch(text(tree),/决定已提交/);
});

test('late failure from a previous version never erases current text or shows a stale error',async()=>{
 let reject;const h=harness({handler:()=>new Promise((_,fail)=>reject=fail)});const promise=button(h.render(),'批准计划并执行').props.onClick();
 let tree=h.setProps({run:{...baseRun,plan_digest:'c'.repeat(64)}});field(tree,'计划审批说明').props.onChange({target:{value:'新版本'}});reject(new Error('旧版本请求失败'));await promise;tree=h.render();
 assert.equal(field(tree,'计划审批说明').props.value,'新版本');assert.doesNotMatch(text(tree),/旧版本请求失败/);assert.equal(button(tree,'批准计划并执行').props.disabled,false);
});

test('submission errors remain visible and retain the opinion',async()=>{
 const h=harness({handler:()=>{throw new Error('审批对象已变化');}});let tree=h.render();field(tree,'计划审批说明').props.onChange({target:{value:'不丢失的说明'}});tree=h.render();await button(tree,'批准计划并执行').props.onClick();tree=h.render();
 assert.match(text(tree),/提交未确认：审批对象已变化/);assert.ok(nodes(tree).some(node=>node.props?.role==='alert'));assert.equal(field(tree,'计划审批说明').props.value,'不丢失的说明');assert.match(text(h.render()),/审批对象已变化/);
});

test('successful request followed by refresh failure remains queued, not retryable',async()=>{
 const h=harness({refresh:()=>{throw new Error('暂时无法连接');}});await button(h.render(),'批准计划并执行').props.onClick();const tree=h.render();
 assert.match(text(tree),/决定已提交，但刷新未成功/);assert.equal(button(tree,'批准计划并执行').props.disabled,true);assert.equal(button(tree,'拒绝计划并结束').props.disabled,true);assert.ok(button(tree,'刷新任务状态'));
});

test('missing digest and malformed success receipts fail closed',async()=>{
 const missing=harness({run:{...baseRun,plan_digest:''}});let tree=missing.render();assert.equal(button(tree,'批准计划并执行').props.disabled,true);await button(tree,'批准计划并执行').props.onClick();assert.equal(missing.calls.length,0);assert.match(text(tree),/版本摘要缺失或无效/);
 const malformed=harness({handler:()=>({accepted:true})});await button(malformed.render(),'批准计划并执行').props.onClick();tree=malformed.render();assert.match(text(tree),/未收到完整提交回执/);assert.equal(button(tree,'批准计划并执行').props.disabled,true);
});

test('inactive phases render no approval card and cannot receive a previous phase success',async()=>{
 let resolve;const h=harness({handler:()=>new Promise(done=>resolve=done)});const promise=button(h.render(),'批准计划并执行').props.onClick();assert.equal(h.setProps({run:{...baseRun,status:'running'}}),null);resolve({accepted:true,command_id:'done'});await promise;assert.equal(h.render(),null);assert.equal(h.refreshes,0);
});

test('unmount invalidates pending request callbacks',async()=>{
 let resolve;const h=harness({handler:()=>new Promise(done=>resolve=done)});const promise=button(h.render(),'批准计划并执行').props.onClick();h.unmount();resolve({accepted:true,command_id:'old'});await promise;assert.equal(h.refreshes,0);
});

test('rejection is explicit, uses the inspected version and does not call cancellation or execution APIs',async()=>{
 const h=harness();await button(h.render(),'拒绝计划并结束').props.onClick();assert.equal(h.calls.length,1);assert.equal(h.calls[0].url,'/runs/run-a/decision/plan');assert.equal(h.calls[0].body.approved,false);assert.equal(h.calls[0].body.digest,planDigest);
});

test('a version digest alone does not permit decisions before their scope is loaded',async()=>{
 const h=harness({run:{...baseRun,plan:undefined}});let tree=h.render();assert.equal(button(tree,'批准计划并执行').props.disabled,true);assert.match(text(tree),/计划与成员分工尚未完整加载/);await button(tree,'批准计划并执行').props.onClick();assert.equal(h.calls.length,0);
 tree=h.setProps({run:{...baseRun,status:'awaiting_acceptance',report:undefined}});field(tree,'成果验收意见').props.onChange({target:{value:'缺少报告不能验收'}});tree=h.render();assert.equal(button(tree,'确认验收通过').props.disabled,true);assert.match(text(tree),/成果报告尚未完整加载/);
});

test('Strict Mode effect replay permits the first explicit click without an extra render',async()=>{
 const h=harness();const tree=h.render();h.strictReplay();assert.equal(h.calls.length,0);
 await button(tree,'批准计划并执行').props.onClick();assert.equal(h.calls.length,1);assert.equal(h.calls[0].body.digest,planDigest);
 await button(tree,'批准计划并执行').props.onClick();assert.equal(h.calls.length,1);
});

test('acceptance guidance names the results tab rather than assuming artifact position',()=>{
 const h=harness({run:{...baseRun,status:'awaiting_acceptance'}});const content=text(h.render());assert.match(content,/先在“成果”页签检查交付内容与验证依据/);assert.doesNotMatch(content,/下方成果/);
});
