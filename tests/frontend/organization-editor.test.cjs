const assert=require('node:assert/strict');
const test=require('node:test');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const ts=require('../../frontend/vendor/typescript.cjs');

const plain=value=>JSON.parse(JSON.stringify(value));
function nodes(value){if(Array.isArray(value))return value.flatMap(nodes);if(!value||typeof value!=='object')return[];return[value,...nodes(value.props?.children)];}
function text(value){if(Array.isArray(value))return value.map(text).join('');if(value&&typeof value==='object')return text(value.props?.children);return value==null?'':String(value);}
function button(tree,label){const n=nodes(tree).find(n=>n.type==='button'&&text(n)===label);assert.ok(n,'button '+label);return n;}
function field(tree,label){const n=nodes(tree).find(n=>n.props?.['aria-label']===label);assert.ok(n,'field '+label);return n;}
const agents=[{id:'a',name:'林',role:'developer',job_title:'产品经理',department:'产品部',enabled:true},{id:'b',name:'周',role:'reviewer',job_title:'产品研究',department:'产品部',enabled:true},{id:'c',name:'陈',role:'developer',job_title:'视觉设计师',department:'设计部',enabled:true}];
function harness({value={positions:{},floors:[]},people=agents,readOnly=false,organization=true}={}){
 let cursor=0,tree,current=plain(value),writes=0,id=0;const slots=[];
 const React={useState(initial){const i=cursor++;if(!(i in slots))slots[i]=typeof initial==='function'?initial():initial;return[slots[i],next=>{slots[i]=typeof next==='function'?next(slots[i]):next;}];},useRef(initial){return{current:initial};},useEffect(){}};
 const jsx=(type,props)=>({type,props:props||{}}),roles={planner:'负责人',developer:'开发工程师',tester:'测试工程师',reviewer:'代码审查'};
 const source=fs.readFileSync(path.resolve(__dirname,'../../frontend/src/components/DesignCenter.tsx'),'utf8').replace(/import\s+['"][^'"]+\.css['"];?/g,'');
 const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText;
 const exports={};const requireMock=name=>name==='react'?React:name==='react/jsx-runtime'?{jsx,jsxs:jsx,Fragment:'Fragment'}:name==='../api'?{api(){assert.fail('Editor must not call a backend or model');},requestId(){return (++id).toString(16).padStart(8,'0');},jsonText:JSON.stringify}:name==='../types'?{roles}:new Proxy({}, {get:(_,key)=>String(key)});
 vm.runInNewContext(code,{exports,require:requireMock,console});
 const render=()=>{cursor=0;tree=exports.OfficeEditor({value:current,agents:people,readOnly,organization,onChange(next){current=plain(next);writes++;}});return tree;};
 return{exports,render,get value(){return current;},get writes(){return writes;},get tree(){return tree;}};
}

test('organization suggestion follows departments, not execution roles, with no source mutation',()=>{
 const h=harness(),before=plain(agents);
 const proposal=plain(h.exports.organizationSuggestion([...agents,{id:'d',name:'未分类',role:'tester'},{id:'e',name:'停用',department:'其他',enabled:false}]));
 assert.deepEqual(proposal.floors.map(f=>f.department),['产品部','设计部','待分配部门']);
 assert.equal(proposal.positions.a.floor,proposal.positions.b.floor);
 assert.notEqual(proposal.positions.a.floor,proposal.positions.c.floor);
 assert.deepEqual(Object.keys(proposal.positions),['a','b','c','d']);
 assert.equal(h.exports.organizationProblem(proposal), '');
 assert.deepEqual(agents,before);
 assert.equal(h.writes,0);
});

test('suggestion uses collision-free complete desk footprints for dense departments',()=>{
 const h=harness();
 for(const count of[1,4,18,19,35]){
  const people=Array.from({length:count},(_,i)=>({id:'a'+i,name:'成员'+i,department:'运营部'}));
  const proposal=h.exports.organizationSuggestion(people);
  assert.equal(Object.keys(proposal.positions).length,count);
  assert.equal(proposal.floors.length,1);
  assert.equal(h.exports.organizationProblem(proposal,people),'');
 }
});

test('unsupported department or capacity counts fail explicitly without losing members',()=>{
 const h=harness();
 assert.throws(()=>h.exports.organizationSuggestion(Array.from({length:11},(_,i)=>({id:''+i,department:'部门'+i}))),/10 个部门/);
 assert.throws(()=>h.exports.organizationSuggestion(Array.from({length:100},(_,i)=>({id:''+i,department:'大部门'}))),/可容纳/);
 assert.equal(h.writes,0);
});

test('generate, edit and add departments change only the editable proposal',()=>{
 const h=harness();let tree=h.render();assert.match(text(tree),/当前为兼容布局/);
 button(tree,'按现有部门生成一层一部门建议').props.onClick();tree=h.render();
 assert.equal(h.value.floors.length,2);assert.equal(h.writes,1);
 assert.match(text(tree),/没有变更真实成员/);
 field(tree,'楼层所属部门').props.onChange({target:{value:'战略产品部'}});tree=h.render();
 assert.equal(h.value.floors[0].department,'战略产品部');
 assert.equal(agents[0].department,'产品部');
 assert.match(text(tree),/后续运行中“同部门”通信/);
 button(tree,'增加部门楼层').props.onClick();tree=h.render();
 assert.equal(h.value.floors.length,3);assert.equal(field(tree,'办公室楼层').props.value,3);
 assert.equal(button(tree,'移除空楼层').props.disabled,false);
 button(tree,'移除空楼层').props.onClick();tree=h.render();assert.equal(h.value.floors.length,2);
});

test('occupied floors and occupied zones cannot be removed before explicit reassignment',()=>{
 const base=harness().exports.organizationSuggestion(agents),h=harness({value:base});let tree=h.render();
 assert.equal(button(tree,'移除空楼层').props.disabled,true);
 assert.equal(field(tree,'移除区域 floor_1_work').props.disabled,true);
 field(tree,'工位楼层').props.onChange({target:{value:'2'}});tree=h.render();
 assert.equal(h.value.positions.a.floor,2);assert.equal(h.value.positions.a.zone,'floor_2_work');
 assert.equal(agents[0].department,'产品部');
 assert.equal(h.exports.organizationProblem(h.value,agents),'');
 assert.match(text(tree),/产品部 → 设计部/);
});

test('removing a desk retains the Agent and allows explicit placement again',()=>{
 const h=harness({value:harness().exports.organizationSuggestion(agents)});let tree=h.render();
 button(tree,'从布局移除工位').props.onClick();tree=h.render();assert.ok(!h.value.positions.a);
 assert.equal(agents.length,3);
 field(tree,'添加现有成员工位').props.onChange({target:{value:'a'}});tree=h.render();
 button(tree,'安排到当前楼层').props.onClick();tree=h.render();assert.ok(h.value.positions.a);
 assert.equal(h.exports.organizationProblem(h.value,agents),'');
});

test('dragging rejects walls and existing desks, then accepts a valid open work position',()=>{
 const h=harness({value:harness().exports.organizationSuggestion(agents)});let tree=h.render(),before=plain(h.value);
 const drop=(x,z)=>field(tree,'办公室 1 楼布局').props.onDrop({preventDefault(){},dataTransfer:{getData(){return'a';}},currentTarget:{getBoundingClientRect(){return{left:0,top:0,width:240,height:160};}},clientX:(x+12)*10,clientY:(z+8)*10});
 drop(11,0);tree=h.render();assert.deepEqual(h.value,before);assert.match(text(tree),/无重叠的工作区/);
 drop(before.positions.b.x,before.positions.b.z);tree=h.render();assert.deepEqual(h.value,before);
 drop(5,0);tree=h.render();assert.equal(h.value.positions.a.x,5);assert.equal(h.value.positions.a.z,0);assert.equal(h.exports.organizationProblem(h.value,agents),'');
});

test('region editing surfaces overlap and boundary problems before publication',()=>{
 const h=harness({value:harness().exports.organizationSuggestion(agents)});let tree=h.render();
 field(tree,'中心 Z floor_1_meeting').props.onChange({target:{value:'0'}});tree=h.render();assert.match(text(tree),/区域不能重叠/);
 field(tree,'中心 Z floor_1_meeting').props.onChange({target:{value:'5.5'}});tree=h.render();
 field(tree,'中心 X floor_1_work').props.onChange({target:{value:'5'}});tree=h.render();assert.match(text(tree),/楼层范围内/);
});

test('generated fractional coordinates satisfy the native number input step contract',()=>{
 const h=harness({value:harness().exports.organizationSuggestion(agents)}),tree=h.render();
 const inputs=nodes(tree).filter(n=>n.type==='input'&&n.props.type==='number');
 for(const {props:p} of inputs){
  assert.ok(p.value>=p.min&&p.value<=p.max,p['aria-label']);
  if(p.step!=='any')assert.ok(Math.abs((p.value-(p.min||0))/(p.step||1)-Math.round((p.value-(p.min||0))/(p.step||1)))<1e-8,p['aria-label']+' must submit without stepMismatch');
 }
 assert.equal(field(tree,'中心 X floor_1_meeting').props.value,5.75);
});

test('read-only review cannot mutate even if event callbacks are invoked',()=>{
 const h=harness({value:harness().exports.organizationSuggestion(agents),readOnly:true});let tree=h.render(),before=plain(h.value);
 assert.equal(button(tree,'按现有部门生成一层一部门建议').props.disabled,true);
 button(tree,'按现有部门生成一层一部门建议').props.onClick();tree=h.render();
 field(tree,'工位楼层').props.onChange({target:{value:'2'}});
 field(tree,'楼层所属部门').props.onChange({target:{value:'不能应用'}});
 button(tree,'从布局移除工位').props.onClick();
 assert.deepEqual(h.value,before);assert.equal(h.writes,0);
});

test('legacy team position editor does not introduce organization configuration',()=>{
 const h=harness({value:{positions:{a:{floor:1,x:1,z:2,zone:'原位置'}}},organization:false});let tree=h.render();
 assert.ok(!nodes(tree).some(n=>n.type==='button'&&text(n)==='按现有部门生成一层一部门建议'));
 field(tree,'工位 X 坐标').props.onChange({target:{value:'3'}});tree=h.render();
 assert.deepEqual(h.value,{positions:{a:{floor:1,x:3,z:2,zone:'原位置'}}});
});
