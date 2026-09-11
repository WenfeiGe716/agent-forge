const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const ts=require('../../frontend/vendor/typescript.cjs');
const behavior={};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(require.resolve('../../frontend/src/office/behavior.ts'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,{exports:behavior});

test('stopped, paused and approval actors have distinct labels without implying active execution',()=>{
 const examples=[['attention','现场保留','任务已停止 · 查看原因'],['paused','已暂停','任务已暂停 · 可继续'],['approval','等待决定','等待人工决定'],['waiting','真实任务','等待团队分派']];
 for(const [phase,label,stage] of examples){const view={source:'business',stage:'work:'+phase,task_key:null};assert.equal(behavior.actorActivityLabel(view),label);assert.equal(behavior.actorStageLabel(view),stage);}
 assert.equal(behavior.actorActivityLabel(), '安全等待');
 assert.equal(behavior.actorActivityLabel({source:'idle'}),'空闲演绎');
 assert.equal(behavior.actorStageLabel({source:'business',stage:'work:writing',task_key:'real-work'}),'real-work');
 assert.equal(behavior.actorStageLabel({source:'business',stage:'work:attention',task_key:'preserved-work'}),'任务已停止 · 查看原因');
});

test('stopped and paused visual actors remain still and explain preserved state',()=>{
 const world={agents:[{id:'member',home:[0,0,0],yaw:0}],facilities:{},route:(_from,to)=>[to]};
 for(const phase of ['attention','paused','approval']){
  const office=new behavior.OfficeBehavior(world,true);
  office.accept({presence:[{agent_id:'member',busy:true,phase,label:phase,run_id:'task',scope:'authorized',evidence_ids:[]}],events:[],cursor:1,complete:true,settings:behavior.defaults,server_time:'2026-09-11T00:00:00Z'},0);
  const actor=office.views()[0];
  assert.notEqual(actor.pose,'typing');assert.notEqual(actor.pose,'walking');
  assert.match(actor.detail,phase==='attention'?/已经停止/:phase==='paused'?/已暂停/:/等待本任务/);
 }
});
