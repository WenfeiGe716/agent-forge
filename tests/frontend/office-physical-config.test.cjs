const assert=require('node:assert/strict');
const test=require('node:test');
const fs=require('node:fs');
const vm=require('node:vm');
const ts=require('../../frontend/vendor/typescript.cjs');

const floor={floor:2,name:'品牌楼层',department:'品牌部',zones:[]};
const agent={id:'real',name:'真实成员',role:'developer',office_position:{floor:1,zone:'stale',x:9,z:5}};
const organization={ref:{id:'published',version:3,digest:'frozen'},config:{positions:{},floors:[floor],
 teams:[{id:'team',name:'品牌团队',floor:2,zone_ids:['brand'],headcount:2,member_ids:['real']}],
 desks:[{id:'seat',name:'实际桌位',floor:2,zone:'brand',x:-4,z:-3,agent_id:'real',team_id:'team'}]}};

function harness(){
 const effects=[],captured=[],props={agents:[agent],organization:structuredClone(organization),preview:true,onSelect(){},selectedId:'real'};
 let refs=0;
 const sentinel=Error('Stop after capturing the scene input');
 const jsx=(type,props)=>({type,props:props||{}});
 const React={useState:value=>[value,()=>{}],useEffect:(fn,deps)=>effects.push({fn,deps}),
  useRef:value=>({current:refs++<2?{}:value})};
 class Renderer{setQuality(){}}
 const requireMock=name=>{
  if(name==='react')return React;
  if(name==='react/jsx-runtime')return{jsx,jsxs:jsx};
  if(name==='../graphics/officeRenderer')return{OfficeRenderer:Renderer};
  if(name==='../graphics/officeWorld')return{makeOffice:(...args)=>{captured.push(args);throw sentinel;},palette:{}};
  if(name==='../graphics/departmentOffice')return{officeFloorOptions:(_agents,org)=>org.config.floors.map(definition=>({floor:definition.floor,definition,label:definition.name}))};
  if(name==='../office/useOfficeFeed')return{useOfficeFeed:()=>({packet:null,connected:false})};
  return{};
 };
 const source=fs.readFileSync('frontend/src/components/Office.tsx','utf8');
 const code=ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
 const exports={};vm.runInNewContext(code,{exports,require:requireMock,console,performance});
 return{props,captured,effects,render(){refs=0;effects.length=0;exports.default(props);},
  scene(){return effects.find(e=>e.deps?.length===2&&typeof e.deps[0]==='string'&&e.deps[0].includes('published'));},
  capture(){assert.throws(()=>this.scene().fn(),error=>error===sentinel);return captured.at(-1);}};
}

test('Office passes the complete selected layout to scene creation without using stale member coordinates',()=>{
 const h=harness();h.render();const[agents,definition,config]=h.capture();
 assert.equal(agents,h.props.agents);
 assert.equal(definition,h.props.organization.config.floors[0]);
 assert.equal(config,h.props.organization.config);
 assert.equal(config.desks[0].x,-4);
 assert.ok(h.effects.some(e=>e.deps?.[0]==='real'&&e.deps?.[1]===2));
});

test('desk and team edits invalidate the scene identity even without a version change',()=>{
 const h=harness();h.render();const original=h.scene().deps[0];
 h.props.organization.config.desks[0].x=-2;h.render();const moved=h.scene().deps[0];
 assert.notEqual(moved,original);assert.equal(h.capture()[2].desks[0].x,-2);
 h.props.organization.config.teams[0].headcount=1;h.render();
 assert.notEqual(h.scene().deps[0],moved);assert.equal(h.capture()[2].teams[0].headcount,1);
});

test('switching to an earlier frozen layout passes its exact seats and team definitions',()=>{
 const h=harness();h.render();const original=h.props.organization;
 h.props.organization=structuredClone(original);h.props.organization.config.desks[0].x=2;h.render();
 assert.equal(h.capture()[2].desks[0].x,2);
 h.props.organization=original;h.render();
 assert.equal(h.capture()[2],original.config);assert.equal(original.config.desks[0].x,-4);
});
