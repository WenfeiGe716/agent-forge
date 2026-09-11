const assert=require('node:assert/strict');
const test=require('node:test');
const fs=require('node:fs');
const vm=require('node:vm');
const ts=require('../../frontend/vendor/typescript.cjs');
const nodes=v=>Array.isArray(v)?v.flatMap(nodes):v&&typeof v==='object'?[v,...nodes(v.props?.children)]:[];
const text=v=>Array.isArray(v)?v.map(text).join(''):v&&typeof v==='object'?text(v.props?.children):v==null?'':String(v);
function load(name,extra={}){
 const source=fs.readFileSync(require.resolve('../../frontend/src/'+name),'utf8'),exports={};
 const jsx=(type,props)=>typeof type==='function'?type(props):({type,props:props||{}});
 vm.runInNewContext(ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText,{exports,setInterval:()=>0,clearInterval(){},require:id=>{
  if(id==='react/jsx-runtime')return {jsx,jsxs:jsx};
  if(id==='react')return {useState:initial=>[initial,()=>{}],useRef:v=>({current:v}),useEffect(){}};
  if(id.endsWith('RunRecovery'))return load('components/RunRecovery.tsx');
  if(id.endsWith('types'))return {states:{needs_attention:'需人工处理'},terminal:['completed','cancelled','needs_attention']};
  if(id.endsWith('UI'))return {Badge:p=>jsx('badge',p),Empty:p=>jsx('empty',p)};
  return {};
 },...extra});return exports;
}
const run={id:'old-run',project_id:'project',title:'需求文档→代码→测试→成员消息闭环',request:'形成需求文档并实现测试',status:'needs_attention',calls:3,error:'12 validation errors for Plan\ntasks.0.key\n  Field required\ntasks.0.agent_id\n  Field required\ntasks.0.acceptance\n  Field required',snapshot:{agents:{lead:{name:'验证负责人'},dev:{name:'验证工程师'}},planner_id:'lead',entry_agent_id:'lead',collaboration_flow:true},tasks:[{key:'plan-0',kind:'planner',status:'failed',agent_id:'lead'}]};
test('stuck collaboration shows cause and safe new-run action without asking the user to decode validation errors',()=>{
 const seeds=[],page=load('components/Collaboration.tsx').default;
 const tree=page({runs:[run],detail:run,agents:[],writer:true,selected:run.id,onSelect(){},onInspect(){},onNewTask:s=>seeds.push(s),onOffice(){}});
 assert.match(text(tree),/计划格式/);
 assert.match(text(tree),/最新配置/);
 const button=nodes(tree).find(n=>n.type==='button'&&text(n).includes('使用最新配置重新发起'));
 assert.ok(button,'visible recovery action');assert.equal(seeds.length,0);
 button.props.onClick();assert.equal(seeds[0].parent_run_id,run.id);
 assert.equal(seeds[0].entry_agent_id,'lead');assert.equal(seeds[0].collaboration_flow,true);
 assert.deepEqual(Array.from(seeds[0].agent_ids),['lead','dev']);
 assert.equal('runtime' in seeds[0],false);assert.equal('model_parameters' in seeds[0],false);
 assert.ok(!nodes(tree).some(n=>n.type==='button'&&text(n)==='发送给选定成员'),'ended run cannot accept follow-up instructions');
 const details=nodes(tree).find(n=>n.type==='details'&&text(n).includes('validation errors'));
 assert.ok(details,'technical evidence remains available');assert.ok(!details.props.open);
});

const plain=value=>JSON.parse(JSON.stringify(value));
const recovery=()=>load('components/RunRecovery.tsx');

test('autonomous collaboration overview uses real work counts instead of a fixed five-step development chain',()=>{
 const current={...plain(run),execution_strategy:'autonomous',status:'running',error:'',report:{mode:'autonomous',summary:'真实协作摘要'},tasks:[{kind:'autonomous',agent_id:'lead',status:'done',result:{outcome:'waiting'}},{kind:'autonomous',agent_id:'dev',status:'running'}]};
 const page=load('components/Collaboration.tsx').default,tree=page({runs:[current],detail:current,agents:[],writer:false,selected:current.id,onSelect(){},onInspect(){},onNewTask(){},onOffice(){}});
 assert.match(text(tree),/2 个已记录阶段/);assert.match(text(tree),/自主协作与交付/);assert.match(text(tree),/真实协作摘要/);assert.doesNotMatch(text(tree),/开发模式的行动项经计划批准|目标复核尚未开始|01定向需求/);
});
function withSnapshot(extra={}){return {...plain(run),snapshot:{...plain(run.snapshot),...extra}};}
const discussion={mode:'discussion',participants:['lead','dev'],strategy:'sequential',stop_when:'goal_satisfied',prompt:'仅调研并形成需求文档，不执行代码',criteria:[{id:'requirements',description:'列出明确的需求与验收条件'}],assessor_id:'lead',stagnation_checks:3};

test('recovery seed only copies intent and explicit members, not frozen secrets, approvals, permissions, or results',()=>{
 const forbidden='synthetic-frozen-value-do-not-replay';
 const original=withSnapshot({runtime:{max_model_calls:500},project:{permission_profile:'full',config_secret:forbidden},agents:{
  lead:{name:'负责人',models:[{credential_revision:forbidden,connection:{encrypted_key:forbidden},model:{parameters:{hidden_value:forbidden}}}],tools:['run_command']},
  dev:{name:'开发',model_parameters:{hidden_value:forbidden}}},commands:[{args:forbidden}],decisions:{approved:true,token:forbidden},results:{output:forbidden}});
 original.error=forbidden;original.plan={key:forbidden};original.report={output:forbidden};
 const before=plain(original),seed=recovery().runRecoverySeed(original);
 assert.deepEqual(Object.keys(seed).sort(),['execution_strategy','run_project_checks','agent_ids','collaboration_flow','entry_agent_id','parent_run_id','project_id','request','title'].sort());
 assert.equal(seed.execution_strategy,'autonomous');assert.equal(seed.run_project_checks,false);
 assert.deepEqual(Array.from(seed.agent_ids),['lead','dev']);
 assert.equal(seed.parent_run_id,original.id);assert.equal(seed.entry_agent_id,'lead');assert.equal(seed.collaboration_flow,true);
 assert.doesNotMatch(JSON.stringify(seed),new RegExp(forbidden));
 seed.agent_ids.push('another');assert.deepEqual(original,before,'helper and later form edits must not mutate the historical run');
});

test('a new autonomous recovery does not inherit fixed design or model-routing overrides',()=>{
 const original=withSnapshot({design:{routing:{id:'routing-asset',version:3,config:{hidden:'not copied'}},routing_overrides:{developer:'human-model-choice'},team:{id:'team-asset',version:2,config:{members:[{name:'not copied'}]}},workflow:{id:'workflow-asset',version:4,config:{graph:discussion}}},orchestration:discussion});
 const seed=recovery().runRecoverySeed(original);
 for(const key of ['routing_ref','team_ref','workflow_ref','routing_overrides'])assert.equal(key in seed,false);
 assert.equal('orchestration' in seed,false);
 assert.equal(original.snapshot.design.routing_overrides.developer,'human-model-choice');
});

test('a prior discussion keeps its original request and snapshot but opens a new autonomous confirmation',()=>{
 const original=withSnapshot({collaboration_flow:false,entry_agent_id:'dev',orchestration:plain(discussion)});
 const seed=recovery().runRecoverySeed(original);
 assert.equal(seed.collaboration_flow,true);assert.equal(seed.entry_agent_id,'dev');assert.equal(seed.execution_strategy,'autonomous');
 assert.equal(seed.orchestration,undefined);assert.equal(seed.request,original.request);
 assert.deepEqual(original.snapshot.orchestration,discussion);
});

test('saved orchestration templates are not copied into new autonomous tasks',()=>{
 const original=withSnapshot({orchestration_template:{id:'saved-template',version:7,name:'只读评审模板'},orchestration:discussion});
 const seed=recovery().runRecoverySeed(original);
 assert.equal(seed.template_id,undefined);assert.equal(seed.template_version,undefined);
 assert.equal('orchestration' in seed,false,'must not bypass current template state/version validation');
});

test('a published team without an embedded workflow does not copy fixed references',()=>{
 const original=withSnapshot({design:{team:{id:'team',version:2,config:{members:[],workflow:null}}},orchestration:discussion});
 const seed=recovery().runRecoverySeed(original);
 assert.equal(seed.team_ref,undefined);assert.equal(seed.orchestration,undefined);assert.deepEqual(Array.from(seed.agent_ids),['lead','dev']);
});

test('an embedded historical team workflow remains out of the new autonomous request',()=>{
 const seed=recovery().runRecoverySeed(withSnapshot({design:{team:{id:'team',version:2,config:{workflow:discussion}}},orchestration:discussion}));
 assert.equal('orchestration' in seed,false);
 assert.equal(seed.team_ref,undefined);assert.equal(seed.execution_strategy,'autonomous');
});

test('office-team recovery preserves only the exact layout reference and selected subset, never all office members',()=>{
 const reference={id:'office',version:5,digest:'a'.repeat(64)};
 const original=withSnapshot({design:{organization_team:{id:'development_team',layout_ref:{...reference,credential_revision:'synthetic-forbidden-extra'},member_ids:['lead','dev','unselected'],selected_agent_ids:['lead','dev']}}});
 const seed=recovery().runRecoverySeed(original);
 assert.equal(seed.office_team_id,'development_team');
 assert.deepEqual(plain(seed.office_layout_ref),reference,'only the three validated reference fields may enter the next request');
 assert.deepEqual(Array.from(seed.agent_ids),['lead','dev']);
 seed.office_layout_ref.version=6;assert.equal(original.snapshot.design.organization_team.layout_ref.version,5);
});

test('unknown failure stays a generic explanation and viewers cannot invoke recovery mutations',()=>{
 const original={...plain(run),error:'opaque-failure-sensitive-synthetic',tasks:[]};
 const {summarizeRunFailure,default:Component}=recovery(),info=summarizeRunFailure(original);
 assert.equal(info.stage,'运行处理');assert.match(info.reason,/未能完成/);
 assert.doesNotMatch(JSON.stringify(info),/opaque-failure-sensitive/);
 let calls=0;
 const tree=Component({run:original,writer:false,onNewTask(){calls++},onInspect(){}});
 assert.ok(!nodes(tree).some(n=>n.type==='button'&&text(n).includes('重新发起')));
 assert.equal(calls,0);
 assert.equal(Component({run:{...original,status:'running'},writer:true,onNewTask(){calls++}}),null);
 assert.equal(calls,0);
});
