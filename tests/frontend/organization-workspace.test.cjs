const assert=require('node:assert/strict');
const test=require('node:test');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const ts=require('../../frontend/vendor/typescript.cjs');
const assignmentExports={};vm.runInNewContext(ts.transpileModule(fs.readFileSync('frontend/src/office/memberAssignment.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,{exports:assignmentExports});
const jsx=(type,props)=>({type,props:props||{}});
const recoveryExports={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.resolve(__dirname,'../../frontend/src/components/RunRecovery.tsx'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:recoveryExports,require:id=>id==='react/jsx-runtime'?{jsx,jsxs:jsx}:assert.fail(id)});
function nodes(value){if(Array.isArray(value))return value.flatMap(nodes);if(!value||typeof value!=='object')return[];return[value,...nodes(value.props?.children)];}
function text(value){if(Array.isArray(value))return value.map(text).join('');if(value&&typeof value==='object')return text(value.props?.children);return value==null?'':String(value);}
function button(tree,label){const result=nodes(tree).find(n=>n.type==='button'&&text(n)===label);assert.ok(result,label);return result;}
function field(tree,label){const result=nodes(tree).find(n=>n.props?.['aria-label']===label);assert.ok(result,label);return result;}
const agents=[{id:'a',name:'市场同事',job_title:'品牌策划',role:'developer',department:'市场部',office_position:{x:0,z:-3,floor:2,zone:'market'},runtime:{}},{id:'b',name:'财务同事',job_title:'财务分析师',role:'reviewer',department:'财务部',office_position:{x:0,z:-3,floor:5,zone:'finance'},runtime:{}}];
const organization={ref:{id:'org',version:2,digest:'exact'},config:{floors:[{floor:2,name:'品牌楼层',department:'市场部',zones:[]},{floor:5,name:'财务楼层',department:'财务部',zones:[]}],positions:{}}};
function harness(name,overrides={}){
 let cursor=0,effects=[],tree;const slots=[],navigation=[];
 const React={useState(initial){const i=cursor++;if(!(i in slots))slots[i]=typeof initial==='function'?initial():initial;return[slots[i],value=>{slots[i]=typeof value==='function'?value(slots[i]):value;}];},useRef(initial){const i=cursor++;if(!(i in slots))slots[i]={current:initial};return slots[i];},useEffect(fn,deps){cursor++;effects.push({fn,deps});}};
 const source=fs.readFileSync(path.resolve(__dirname,`../../frontend/src/components/${name}.tsx`),'utf8');
 const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText;
 const exports={},roles={developer:'开发工程师',reviewer:'代码审查'};
 const props={agents,organization,models:[],providers:[],projects:[],runs:[],detail:null,events:[],selectedRun:'',writer:true,admin:true,onNavigate:target=>navigation.push(target),onSelect(){},...overrides};
 const environment={quality:'balanced',preferences:{motion:true,day:true,density:'normal'}};
 const requireMock=name=>{
  if(name==='react')return React;if(name==='react/jsx-runtime')return{jsx,jsxs:jsx};
  if(name==='../types')return{roles,states:{},agentTitle:a=>a.job_title||roles[a.role]||a.role};
  if(name==='../office/memberAssignment')return assignmentExports;
  if(name==='./RunRecovery')return recoveryExports;
  if(name==='../office/ambientEnvironment')return{environment};
  if(name==='./Office')return{__esModule:true,default:'Office'};
  if(name==='../graphics/officeWorld')return{palette:{developer:0x54b2cc,reviewer:0xda91b0}};
  if(name==='../graphics/departmentOffice')return{officeFloorOptions:(people,org)=>[...new Set([...(org?.config?.floors||[]).map(f=>f.floor),...people.map(a=>a.office_position?.floor||1)])].map(floor=>({floor,definition:org?.config?.floors?.find(f=>f.floor===floor)}))};
  if(name==='../office/useOfficeFeed')return{useOfficeFeed:()=>({packet:null,connected:false})};
  if(name==='../office/behavior')return{defaults:{idle_enabled:true,activities:[],durations_s:{}},idleLabels:{}};
  if(name==='../api')return{api(){assert.fail('workspace rendering must not perform API writes');},requestId:()=>''};
  return new Proxy({},{get:(_,key)=>String(key)});
 };
 vm.runInNewContext(code,{exports,require:requireMock,console,performance,document:{},localStorage:{getItem(){return null;}}});
 return{props,navigation,render(){cursor=0;effects=[];tree=exports.default(props);return tree;},get effects(){return effects;},get tree(){return tree;}};
}

test('workspace passes explicit organization to Office and displays business roles, departments and floors',()=>{
 const h=harness('StudioWorkspace'),tree=h.render(),office=nodes(tree).find(n=>n.type==='Office');
 assert.equal(office.props.organization,organization);assert.equal(office.props.agents,agents);
 assert.match(text(tree),/组织成员/);assert.match(text(tree),/品牌策划/);assert.match(text(tree),/财务分析师/);
 assert.match(text(tree),/市场部 · 2 楼/);assert.match(text(tree),/财务部 · 5 楼/);
 assert.doesNotMatch(text(tree),/开发团队|暂无开发任务/);
 button(tree,'设计部门与楼层').props.onClick();button(tree,'检查执行条件').props.onClick();
 assert.deepEqual(h.navigation,['organization','execution']);
});

test('Studio quick configuration cannot erase the saved binding by only switching provider',async()=>{
 const saved=[],h=harness('StudioWorkspace',{models:[{id:'m1',provider_id:'p1',name:'原模型'},{id:'m2',provider_id:'p2',name:'新模型'}],providers:[{id:'p1',name:'原供应商'},{id:'p2',name:'新供应商'}],agents:[{...agents[0],model_profile_id:'m1',version:1}],onSaveAgent:async(...args)=>saved.push(args)});
 h.render();h.effects.find(e=>e.deps?.[0]===agents[0].id&&e.deps?.[1]===1).fn();
 let tree=h.render();nodes(tree).find(n=>n.type==='button'&&text(n)==='模型'&&n.props.className==='').props.onClick();tree=h.render();
 field(tree,'成员供应商').props.onChange({target:{value:'p2'}});tree=h.render();button(tree,'保存配置').props.onClick();await new Promise(r=>setImmediate(r));
 assert.equal(saved.length,0,'choosing a provider alone must not clear an existing model');assert.match(text(h.render()),/选择.*模型/);
 field(h.render(),'成员模型').props.onChange({target:{value:'m2'}});button(h.render(),'保存配置').props.onClick();await new Promise(r=>setImmediate(r));
 assert.equal(saved.length,1);assert.equal(saved[0][1].model_profile_id,'m2');
});

test('department and business-title search does not collapse every member into execution-role labels',()=>{
 const h=harness('StudioWorkspace');let tree=h.render();
 field(tree,'搜索成员').props.onChange({target:{value:'财务'}});tree=h.render();
 let people=nodes(tree).filter(n=>n.type==='button'&&n.props.className?.startsWith('studio-member'));
 assert.equal(people.length,1);assert.match(text(people[0]),/财务分析师/);
 field(tree,'搜索成员').props.onChange({target:{value:'品牌策划'}});tree=h.render();
 people=nodes(tree).filter(n=>n.type==='button'&&n.props.className?.startsWith('studio-member'));
 assert.equal(people.length,1);assert.match(text(people[0]),/市场同事/);
});

test('global roster busy filters use real presence for other floors, not only currently rendered actors',()=>{
 const h=harness('StudioWorkspace');let tree=h.render(),office=nodes(tree).find(n=>n.type==='Office');
 office.props.onSnapshot({complete:true,presence:[{agent_id:'a',busy:false,label:'空闲'},{agent_id:'b',busy:true,label:'真实任务占用'}],events:[]},true);
 office.props.onViews([{id:'a',source:'idle',busy:false,label:'空闲'}]);tree=h.render();
 assert.match(text(tree),/1 位被任务占用/);
 button(tree,'任务占用').props.onClick();tree=h.render();
 const people=nodes(tree).filter(n=>n.type==='button'&&n.props.className?.startsWith('studio-member'));
 assert.equal(people.length,1);assert.match(text(people[0]),/财务同事/);
});

test('same-floor team filters show distinct employees and authoritative desks suppress stale locations',()=>{
 const people=[...agents,{...agents[0],id:'c',name:'待安排同事'}];
 const org=JSON.parse(JSON.stringify(organization));org.config.teams=[{id:'red',name:'品牌团队',floor:2,zone_ids:['market'],headcount:1,member_ids:['a']},{id:'blue',name:'传播团队',floor:2,zone_ids:['outreach'],headcount:1,member_ids:['b']}];
 org.config.desks=[{id:'d1',agent_id:'a',team_id:'red',floor:2,zone:'market',x:0,z:-3},{id:'d2',agent_id:'b',team_id:'blue',floor:2,zone:'outreach',x:4,z:-3}];
 const h=harness('StudioWorkspace',{agents:people,organization:org});let tree=h.render();
 const shown=()=>nodes(tree).filter(n=>n.type==='button'&&n.props.className?.startsWith('studio-member'));
 field(tree,'楼层成员筛选').props.onChange({target:{value:'2'}});tree=h.render();assert.equal(shown().length,2);
 field(tree,'团队成员筛选').props.onChange({target:{value:'red'}});tree=h.render();assert.equal(shown().length,1);assert.match(text(shown()),/市场同事/);assert.doesNotMatch(text(shown()),/财务同事/);
 field(tree,'团队成员筛选').props.onChange({target:{value:'blue'}});tree=h.render();assert.equal(shown().length,1);assert.match(text(shown()),/财务同事/);assert.match(text(shown()),/财务部 · 2 楼 · 传播团队 · 空间待调整/);
 field(tree,'楼层成员筛选').props.onChange({target:{value:'all'}});tree=h.render();field(tree,'团队成员筛选').props.onChange({target:{value:'unassigned'}});tree=h.render();
 assert.equal(shown().length,1);assert.match(text(shown()),/待安排同事/);assert.match(text(shown()),/工位待安排/);
});

test('Office rebuild signature includes empty-floor configuration, layout revision and selected agent floor changes',()=>{
 const h=harness('Office',{selectedId:'a'});h.render();
 const signature=()=>h.effects.find(e=>e.deps?.length===2&&typeof e.deps[0]==='string'&&e.deps[0].includes('exact')).deps[0];
 const first=signature();h.props.organization=JSON.parse(JSON.stringify(organization));h.props.organization.config.floors[1].department='财务合规部';h.render();
 const second=signature();assert.notEqual(first,second);assert.match(second,/财务合规部/);
 h.props.organization.ref.version=3;h.render();assert.notEqual(second,signature());
 h.props.agents=agents.map(a=>a.id==='a'?{...a,office_position:{...a.office_position,floor:5}}:a);h.render();
 assert.ok(h.effects.some(e=>e.deps?.[0]==='a'&&e.deps?.[1]===5));
});
