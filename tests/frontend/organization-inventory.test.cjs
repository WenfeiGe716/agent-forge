const assert=require('node:assert/strict');
const test=require('node:test');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const ts=require('../../frontend/vendor/typescript.cjs');

const plain=value=>JSON.parse(JSON.stringify(value));
const jsx=(type,props)=>({type,props:props||{}});
function nodes(value){if(Array.isArray(value))return value.flatMap(nodes);if(!value||typeof value!=='object')return[];return[value,...nodes(value.props?.children)];}
function text(value){if(Array.isArray(value))return value.map(text).join('');if(value&&typeof value==='object')return text(value.props?.children);return value==null?'':String(value);}
function field(tree,label){const result=nodes(tree).find(n=>n.props?.['aria-label']===label);assert.ok(result,'field '+label);return result;}
function button(tree,label){const result=nodes(tree).find(n=>n.type==='button'&&text(n)===label);assert.ok(result,'button '+label);return result;}
function teamCard(tree,name){const result=nodes(tree).find(n=>n.type==='button'&&nodes(n).some(child=>child.type==='strong'&&text(child)===name));assert.ok(result,'team '+name);return result;}
const people=[
 {id:'a',name:'林',role:'developer',job_title:'产品经理',enabled:true},
 {id:'b',name:'周',role:'reviewer',job_title:'视觉设计师',enabled:true},
 {id:'c',name:'陈',role:'tester',job_title:'质量工程师',enabled:true},
 {id:'disabled',name:'停用成员',role:'tester',enabled:false},
];
function fixture(){return{
 floors:[{floor:1,name:'业务楼层',department:'业务部',zones:[
  {id:'west',name:'产品区',kind:'work',x:-6,z:-2,width:10,depth:10},
  {id:'east',name:'设计区',kind:'work',x:6,z:-2,width:10,depth:10},
  {id:'meeting',name:'会议区',kind:'meeting',x:-6,z:5.5,width:10,depth:3},
  {id:'common',name:'公共区',kind:'common',x:6,z:5.5,width:10,depth:3}]}],
 teams:[
  {id:'product',name:'产品团队',floor:1,zone_ids:['west'],headcount:2,member_ids:['a']},
  {id:'design',name:'设计团队',floor:1,zone_ids:['east'],headcount:2,member_ids:['b']}],
 desks:[
  {id:'p1',name:'产品桌1',floor:1,zone:'west',x:-8,z:-3,team_id:'product',agent_id:'a'},
  {id:'p2',name:'产品桌2',floor:1,zone:'west',x:-4,z:-3,team_id:'product',agent_id:null},
  {id:'d1',name:'设计桌1',floor:1,zone:'east',x:4,z:-3,team_id:'design',agent_id:'b'},
  {id:'d2',name:'设计桌2',floor:1,zone:'east',x:8,z:-3,team_id:'design',agent_id:null}],
 positions:{a:{x:-8,z:-3,floor:1,zone:'west'},b:{x:4,z:-3,floor:1,zone:'east'}},description:'独立桌位与员工编制',
};}

function harness({value=fixture(),agents=people,readOnly=false}={}){
 let cursor=0,current=plain(value),tree,writes=0,dirty=false;
 const slots=[],effects=[],modules={};
 const React={
  useState(initial){const i=cursor++;if(!(i in slots))slots[i]=typeof initial==='function'?initial():initial;return[slots[i],next=>{const value=typeof next==='function'?next(slots[i]):next;if(!Object.is(value,slots[i])){slots[i]=value;dirty=true;}}];},
  useRef(initial){const i=cursor++;if(!(i in slots))slots[i]={current:initial};return slots[i];},
  useEffect(effect,deps){const i=cursor++;if(!(i in slots)||!deps||deps.some((value,n)=>!Object.is(value,slots[i][n]))){slots[i]=deps;effects.push(effect);}},
 };
 function load(relative){
  if(modules[relative])return modules[relative];
  const exports={};modules[relative]=exports;
  const source=fs.readFileSync(path.resolve(__dirname,'../../frontend/src',relative),'utf8');
  const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText;
  const requireSource=id=>id==='react'?React:id==='react/jsx-runtime'?{jsx,jsxs:jsx,Fragment:'Fragment'}:id==='../types'?load('types.ts'):id==='./UI'?{Field:'Field'}:id==='../office/organizationDesign'?load('office/organizationDesign.ts'):assert.fail('Unexpected dependency (no network/model/Agent APIs allowed): '+id);
  vm.runInNewContext(code,{exports,require:requireSource,console});return exports;
 }
 const operations=load('office/organizationDesign.ts'),component=load('components/OrganizationDesigner.tsx').default;
 function render(){
  for(let pass=0;pass<12;pass++){
   cursor=0;dirty=false;tree=component({value:current,agents,readOnly,legacy:jsx('p',{children:'旧版布局编辑器'}),onChange(next){current=plain(next);writes++;dirty=true;}});
   for(const node of nodes(tree))if(node.type==='svg'&&node.props.ref)node.props.ref.current={getBoundingClientRect:()=>({left:0,top:0,width:240,height:160}),setPointerCapture(){}};
   effects.splice(0).forEach(effect=>effect());if(!dirty)return tree;
  }
  assert.fail('Component did not settle its effects');
 }
 return{operations,render,get value(){return current;},get writes(){return writes;},get tree(){return tree;}};
}
const ops=harness().operations;
const pointer=(x,z)=>({clientX:(x+12)*10,clientY:(z+8)*10,pointerId:1,stopPropagation(){}});

test('same-floor teams have independent member sets and desk capacity is not headcount or people',()=>{
 const value=fixture(),before=plain(value),result=plain(ops.setTeamCapacity(value,'product',3,2));
 assert.equal(ops.layoutProblem(result,people),'');
 assert.equal(result.desks.filter(d=>d.team_id==='product').length,3);
 assert.equal(result.teams[0].headcount,2);
 assert.deepEqual(result.teams[0].member_ids,['a']);assert.deepEqual(result.teams[1].member_ids,['b']);
 assert.equal(result.desks.filter(d=>d.agent_id).length,2);
 assert.deepEqual(result.desks.filter(d=>d.team_id==='design'),before.desks.filter(d=>d.team_id==='design'));
 assert.deepEqual(value,before);assert.equal(people.length,4);
});

test('adding desks only creates empty desks and never assigns or creates Agents',()=>{
 const value=fixture(),result=plain(ops.setDeskCount(value,'product',4)),newDesks=result.desks.filter(d=>!value.desks.some(old=>old.id===d.id));
 assert.equal(newDesks.length,2);assert.ok(newDesks.every(d=>d.agent_id===null&&d.team_id==='product'));
 assert.equal(result.teams[0].headcount,2);assert.deepEqual(result.positions,value.positions);
 assert.deepEqual(result.teams.map(t=>t.member_ids),[['a'],['b']]);
});

test('reducing capacity removes only empty desks and refuses to remove occupied seats',()=>{
 const value=fixture(),before=plain(value),reduced=plain(ops.setTeamCapacity(value,'product',1,1));
 assert.deepEqual(reduced.desks.filter(d=>d.team_id==='product').map(d=>d.id),['p1']);
 assert.deepEqual(reduced.positions,value.positions);
 assert.throws(()=>ops.setTeamCapacity(value,'product',0,0),/实际员工|编制|占用/);
 assert.deepEqual(value,before);
});

test('cross-team transfer vacates the old desk without duplicating a member or stealing an occupied desk',()=>{
 const value=fixture(),before=plain(value),result=plain(ops.assignEmployee(value,'design','a','d2'));
 assert.equal(result.desks.find(d=>d.id==='p1').agent_id,null);
 assert.equal(result.desks.find(d=>d.id==='d2').agent_id,'a');assert.equal(result.desks.find(d=>d.id==='d1').agent_id,'b');
 assert.deepEqual(result.teams.map(t=>t.member_ids),[[],['b','a']]);
 assert.deepEqual(result.positions.a,{x:8,z:-3,floor:1,zone:'east'});
 assert.throws(()=>ops.assignEmployee(value,'design','a','d1'),/空桌|覆盖/);
 assert.deepEqual(value,before);
});

test('an impossible expansion fails atomically after searching for space',()=>{
 const value=fixture(),before=plain(value);
 assert.throws(()=>ops.setTeamCapacity(value,'product',100,100),/空间不足/);
 assert.deepEqual(value,before);
});

test('moving a work zone carries its own desks and exported positions but not another team',()=>{
 const value=fixture(),before=plain(value),result=plain(ops.editZone(value,1,'west',{x:-5.5,z:-1.5},true));
 assert.deepEqual(result.positions.a,{x:-7.5,z:-2.5,floor:1,zone:'west'});
 assert.equal(result.desks.find(d=>d.id==='p2').x,-3.5);assert.equal(result.desks.find(d=>d.id==='p2').z,-2.5);
 assert.deepEqual(result.positions.b,before.positions.b);
 assert.deepEqual(result.desks.filter(d=>d.team_id==='design'),before.desks.filter(d=>d.team_id==='design'));
 assert.equal(ops.layoutProblem(result,people),'');assert.deepEqual(value,before);
});

test('zone resizing and desk movement reject overlap, clipping, and the other team work zone',()=>{
 const value=fixture(),before=plain(value);
 assert.throws(()=>ops.editZone(value,1,'west',{width:3}),/桌椅|工作区/);
 assert.throws(()=>ops.editZone(value,1,'west',{width:20}),/边界|重叠/);
 assert.throws(()=>ops.editZone(value,1,'west',{x:0}),/重叠/);
 assert.throws(()=>ops.moveDesk(value,'p2',-8,-3),/重叠/);
 assert.throws(()=>ops.moveDesk(value,'p2',8,0),/所属团队/);
 assert.deepEqual(value,before);
});

test('designer shows separate team rosters and offers an explicit transfer instead of copying members',()=>{
 const h=harness();let tree=h.render();
 assert.match(text(teamCard(tree,'产品团队')),/林/);assert.doesNotMatch(text(teamCard(tree,'产品团队')),/周/);
 assert.match(text(teamCard(tree,'设计团队')),/周/);assert.doesNotMatch(text(teamCard(tree,'设计团队')),/林/);
 teamCard(tree,'设计团队').props.onClick();tree=h.render();
 assert.equal(field(tree,'办公团队名称').props.value,'设计团队');
 let candidates=nodes(field(tree,'候选团队员工')).filter(n=>n.type==='option');
 assert.ok(!candidates.some(n=>n.props.value==='b'||n.props.value==='disabled'));
 assert.match(text(candidates.find(n=>n.props.value==='a')),/从 产品团队 调入/);
 field(tree,'候选团队员工').props.onChange({target:{value:'a'}});tree=h.render();
 button(tree,'分配 / 调入团队').props.onClick();tree=h.render();
 assert.deepEqual(h.value.teams.map(t=>t.member_ids),[[],['b','a']]);
 assert.equal(h.value.desks.find(d=>d.id==='p1').agent_id,null);
 assert.equal(h.value.desks.find(d=>d.id==='d2').agent_id,'a');
 assert.equal(h.writes,1);assert.equal(people.length,4);
});

test('capacity form changes desk count and staffing independently without creating employees',()=>{
 const h=harness();let tree=h.render();
 field(tree,'团队办公桌数量').props.onChange({target:{value:'3'}});tree=h.render();
 field(tree,'团队员工编制').props.onChange({target:{value:'2'}});tree=h.render();
 button(tree,'更新容量').props.onClick();tree=h.render();
 assert.equal(h.value.desks.length,5);assert.equal(h.value.teams[0].headcount,2);
 assert.equal(Object.keys(h.value.positions).length,2);assert.equal(h.value.desks.filter(d=>!d.agent_id).length,3);
 assert.match(text(tree),/1 位实际员工 \/ 2 位编制 \/ 3 张桌/);
 assert.equal(h.writes,1);
});

test('capacity form reports an impossible expansion and does not partially write its temporary placements',()=>{
 const h=harness(),before=plain(h.value);let tree=h.render();
 field(tree,'团队办公桌数量').props.onChange({target:{value:'100'}});tree=h.render();
 field(tree,'团队员工编制').props.onChange({target:{value:'100'}});tree=h.render();
 button(tree,'更新容量').props.onClick();tree=h.render();
 assert.match(text(tree),/空间不足/);assert.deepEqual(h.value,before);assert.equal(h.writes,0);
});

test('removing a team member vacates a desk and leaves both the desk count and the Agent unchanged',()=>{
 const h=harness(),beforePeople=plain(people);let tree=h.render();
 button(tree,'移出团队').props.onClick();tree=h.render();
 assert.equal(h.value.desks.length,4);assert.equal(h.value.teams[0].headcount,2);
 assert.deepEqual(h.value.teams[0].member_ids,[]);assert.equal(h.value.desks[0].agent_id,null);
 assert.ok(!('a' in h.value.positions));assert.deepEqual(people,beforePeople);
});

test('dragging a region updates its own desks and positions through the visible canvas',()=>{
 const h=harness();let tree=h.render();
 field(tree,'编辑功能区 产品区').props.onPointerDown(pointer(-6,-2));tree=h.render();
 field(tree,'可视化功能区与办公桌 1 楼').props.onPointerMove(pointer(-5.5,-1.5));tree=h.render();
 field(tree,'可视化功能区与办公桌 1 楼').props.onPointerUp();
 assert.deepEqual(h.value.positions.a,{x:-7.5,z:-2.5,floor:1,zone:'west'});
 assert.deepEqual(h.value.positions.b,{x:4,z:-3,floor:1,zone:'east'});
 assert.equal(h.value.desks[1].x,-3.5);assert.equal(h.writes,1);
});

test('canvas resize rejects overlapping regions without writing the draft',()=>{
 const h=harness(),before=plain(h.value);let tree=h.render();
 field(tree,'编辑功能区 产品区').props.onKeyDown({key:'Enter'});tree=h.render();
 field(tree,'拖动缩放区域 产品区').props.onPointerDown(pointer(-1,3));tree=h.render();
 field(tree,'可视化功能区与办公桌 1 楼').props.onPointerMove(pointer(6,4));tree=h.render();
 assert.match(text(tree),/重叠|边界/);assert.deepEqual(h.value,before);assert.equal(h.writes,0);
});

test('review-only callbacks cannot change rosters, capacity, geometry, or floor configuration',()=>{
 const h=harness({readOnly:true}),before=plain(h.value);let tree=h.render();
 assert.equal(button(tree,'更新容量').props.disabled,true);
 field(tree,'管理楼层名称').props.onChange({target:{value:'不可修改'}});
 field(tree,'办公团队名称').props.onChange({target:{value:'不可修改'}});
 field(tree,'团队办公桌数量').props.onChange({target:{value:'3'}});tree=h.render();
 button(tree,'更新容量').props.onClick();button(tree,'移出团队').props.onClick();button(tree,'＋ 新增楼层').props.onClick();
 field(tree,'编辑功能区 产品区').props.onPointerDown(pointer(-6,-2));tree=h.render();
 field(tree,'选中区域 中心 X').props.onChange({target:{value:'-5.5'}});
 field(tree,'可视化功能区与办公桌 1 楼').props.onPointerMove(pointer(-5.5,-1.5));tree=h.render();
 field(tree,'选择办公桌 产品桌1 · 林').props.onKeyDown({key:'Enter'});tree=h.render();
 field(tree,'办公桌员工').props.onChange({target:{value:''}});
 field(tree,'办公桌 X').props.onChange({target:{value:'-7.5'}});
 assert.deepEqual(h.value,before);assert.equal(h.writes,0);
});

test('fractional default desk and region coordinates are valid native number input values',()=>{
 const value=fixture();value.desks[0].x=-8.7;value.positions.a.x=-8.7;
 const h=harness({value});let tree=h.render();
 const check=()=>{for(const node of nodes(tree).filter(n=>n.type==='input'&&n.props.type==='number')){
  const p=node.props,n=Number(p.value);if(p.min!==undefined)assert.ok(n>=p.min);if(p.max!==undefined)assert.ok(n<=p.max);
  if(p.step!=='any')assert.ok(Math.abs((n-(p.min||0))/(p.step||1)-Math.round((n-(p.min||0))/(p.step||1)))<1e-8,p['aria-label']+' must not have stepMismatch');
 }};
 check();field(tree,'选择办公桌 产品桌1 · 林').props.onKeyDown({key:'Enter'});tree=h.render();check();
 assert.equal(field(tree,'办公桌 X').props.value,-8.7);assert.equal(field(tree,'办公桌 X').props.step,'any');
 field(tree,'编辑功能区 会议区').props.onKeyDown({key:'Enter'});tree=h.render();check();
 assert.equal(field(tree,'选中区域 中心 Z').props.value,5.5);assert.equal(field(tree,'选中区域 中心 Z').props.step,'any');
});

test('a newly added floor starts with empty teams and cannot inherit another floor member list',()=>{
 const h=harness();let tree=h.render();button(tree,'＋ 新增楼层').props.onClick();tree=h.render();
 assert.equal(h.value.floors.length,2);assert.equal(h.value.teams.length,2);assert.equal(h.value.desks.length,4);
 assert.match(text(tree),/本层尚无团队/);button(tree,'＋ 新建本层团队').props.onClick();tree=h.render();
 assert.equal(h.value.teams[2].floor,2);assert.equal(h.value.teams[2].headcount,0);assert.deepEqual(h.value.teams[2].member_ids,[]);
 assert.deepEqual(h.value.teams.slice(0,2).map(t=>t.member_ids),[['a'],['b']]);
});

test('legacy upgrade is explicit and maps existing employees to desks without mutating its old version',()=>{
 const original=fixture();delete original.desks;delete original.teams;const before=plain(original),h=harness({value:original});let tree=h.render();
 assert.match(text(tree),/历史成员坐标布局/);assert.equal(h.writes,0);
 button(tree,'启用团队与独立工位管理').props.onClick();tree=h.render();
 assert.equal(h.value.desks.length,2);assert.equal(h.value.teams.length,1);assert.deepEqual(h.value.teams[0].member_ids,['a','b']);
 assert.deepEqual(h.value.positions,before.positions);assert.deepEqual(original,before);
});

test('frontend validation rejects positions that disagree with the occupied desk export',()=>{
 const value=fixture();value.positions.a.x=-7;
 assert.notEqual(ops.layoutProblem(value,people),'','Backend OfficeConfig requires the exact occupied desk position export');
});

test('frontend validation rejects stale, missing, duplicate, or cross-team member lists',()=>{
 for(const members of[[],['a','a'],['a','b'],['unknown']]){
  const value=fixture();value.teams[0].member_ids=members;
  assert.notEqual(ops.layoutProblem(value,people),'','Backend team membership must equal its occupied desks: '+JSON.stringify(members));
 }
});

test('a deliberately empty explicit layout is valid just as it is at backend publication',()=>{
 assert.equal(ops.layoutProblem({positions:{},floors:[],teams:[],desks:[]},[]),'');
});

test('backend-valid 64-character team and desk identifiers remain editable without false errors',()=>{
 const value=fixture(),id='t'+'a'.repeat(63);value.teams[0].id=id;
 value.desks.filter(d=>d.team_id==='product').forEach(d=>d.team_id=id);
 value.desks[0].id='d'+'b'.repeat(63);
 assert.equal(ops.layoutProblem(value,people),'');
});

test('an explicitly loaded empty Agent roster cannot validate occupied desks',()=>{
 assert.notEqual(ops.layoutProblem(fixture(),[]),'','An explicitly supplied empty roster means no occupied member exists');
 assert.equal(ops.layoutProblem(fixture()),'','Structural-only operations may omit the roster');
});

test('standard proposals keep shared facilities on every floor without creating or duplicating members',()=>{
 const roster=[{...people[0],department:'产品部'},{...people[1],department:'设计部'},{...people[2],department:'产品部'}],before=plain(roster);
 const value=plain(ops.departmentProposal(roster));
 assert.equal(value.layout_version,2);assert.equal(ops.layoutProblem(value,roster),'');
 assert.deepEqual(value.floors.map(f=>f.department),['产品部','设计部']);
 for(const f of value.floors)for(const kind of['meeting','restroom','pantry','lounge'])assert.ok(f.zones.some(z=>z.kind===kind),kind);
 assert.deepEqual(value.teams.map(t=>t.member_ids),[['a','c'],['b']]);assert.deepEqual(roster,before);
 const extra=plain(ops.addFloor(value));assert.equal(extra.floors.length,3);assert.deepEqual(extra.teams,value.teams);assert.deepEqual(extra.desks,value.desks);
 assert.equal(ops.layoutProblem(extra,roster),'');
});

test('last required facility can move but cannot be removed, retyped, or shrunk below a usable size',()=>{
 const value=plain(ops.departmentProposal([])),floor=value.floors[0],wc=floor.zones.find(z=>z.kind==='restroom');
 assert.equal(ops.lastRequiredFacility(value,floor,wc),true);
 assert.throws(()=>ops.removeZone(value,1,wc.id),/必须保留/);
 assert.throws(()=>ops.editZone(value,1,wc.id,{kind:'common'}),/缺少.*卫生间/);
 assert.throws(()=>ops.editZone(value,1,wc.id,{width:2}),/空间不足/);
 const moved=plain(ops.editZone(value,1,wc.id,{x:wc.x+.1},true));assert.equal(moved.floors[0].zones.find(z=>z.id===wc.id).x,wc.x+.1);
 const missing=plain(value);missing.floors[0].zones=missing.floors[0].zones.filter(z=>z.id!==wc.id);assert.match(ops.layoutProblem(missing),/缺少.*卫生间/);
 const lounge=floor.zones.find(z=>z.kind==='lounge'),removed=ops.removeZone(value,1,lounge.id);assert.equal(ops.layoutProblem(removed),'');
 assert.throws(()=>ops.departmentProposal(Array.from({length:80},(_,i)=>({id:'m'+i,name:'成员'+i,department:'大部门'}))),/可容纳/);
});

test('designer exposes editable required-facility positions while disabling removal in design and review',()=>{
 const value=plain(ops.departmentProposal([])),h=harness({value,agents:[]});let tree=h.render();
 assert.equal(field(tree,'可视化功能区与办公桌 1 楼').props.role,'group','An image role would hide the interactive region controls from the accessibility tree');
 field(tree,'编辑功能区 卫生间').props.onKeyDown({key:'Enter'});tree=h.render();
 assert.equal(button(tree,'删除未使用区域').props.disabled,true);assert.equal(field(tree,'选中区域 中心 X').props.disabled,false);
 const x=field(tree,'选中区域 中心 X').props.value;field(tree,'选中区域 中心 X').props.onChange({target:{value:String(x+.1)}});tree=h.render();
 assert.equal(field(tree,'选中区域 中心 X').props.value,x+.1);
 button(tree,'删除未使用区域').props.onClick();tree=h.render();assert.match(text(tree),/必须保留卫生间/);
 assert.equal(h.value.floors[0].zones.filter(z=>z.kind==='restroom').length,1);
});
