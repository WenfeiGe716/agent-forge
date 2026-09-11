const assert=require('node:assert/strict');
const test=require('node:test');
const fs=require('node:fs');
const vm=require('node:vm');
const ts=require('../../frontend/vendor/typescript.cjs');
const skill={id:'s',name:'安全审查',enabled:true,description:'审核方法',version:3,versions:[{version:3,status:'draft'},{version:2,status:'approved'},{version:1,status:'approved'}]};
const binding={skill_id:'s',version:1,mode:'on_demand',enabled:true,allow_scripts:false,parameters:{focus:'security'}};
function nodes(v){if(Array.isArray(v))return v.flatMap(nodes);return v&&typeof v==='object'?[v,...nodes(v.props?.children)]:[];}
function text(v){if(Array.isArray(v))return v.map(text).join('');return v&&typeof v==='object'?text(v.props?.children):v==null?'':String(v);}
function field(tree,label){const n=nodes(tree).find(n=>n.props?.['aria-label']===label);assert.ok(n,'field '+label);return n;}
function harness({value=[],rows=[skill],module='Skills',handler}={}){
 let cursor=0,tree;const slots=[],effects=[],calls=[],props={value:structuredClone(value),onChange(v){this.value=v;}};
 const jsx=(type,props)=>({type,props:props||{}}),React={useState(initial){const i=cursor++;if(!(i in slots))slots[i]=typeof initial==='function'?initial():initial;return[slots[i],v=>{slots[i]=typeof v==='function'?v(slots[i]):v;}];},useRef(initial){const i=cursor++;if(!(i in slots))slots[i]={current:initial};return slots[i];},useEffect(fn,deps){const i=cursor++;if(!(i in slots)||deps.some((v,j)=>v!==slots[i][j])){slots[i]=deps;effects.push(fn);}}};
 props.onChange=v=>{props.value=v;};
 const api=async(url,method='GET',body)=>{calls.push({url,method,body});if(handler)return handler(url,method,body);if(url==='/skills')return rows;return rows.find(s=>url==='/skills/'+s.id);};
 const source=fs.readFileSync('frontend/src/components/'+module+'.tsx','utf8');
 const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText;
 const exports={};vm.runInNewContext(code,{exports,require:id=>id==='react'?React:id==='react/jsx-runtime'?{jsx,jsxs:jsx}:id==='../api'?{api,jsonText:JSON.stringify,parseObject:JSON.parse}:id==='../types'?{}:id==='./UI'?{Field:'Field',Modal:'Modal',Empty:'Empty'}:id==='./SkillBindingSource'?{default:'SkillBindingSource'}:assert.fail(id)});
 const render=()=>{cursor=0;tree=(module==='Skills'?exports.SkillBindings:exports.default)(props);effects.splice(0).forEach(fn=>fn());return tree;};
 return{props,calls,render,async settle(){await new Promise(r=>setImmediate(r));return render();}};
}

test('binding requires explicit selection and picks an approved version without approving or installing anything',async()=>{
 const h=harness();h.render();let tree=await h.settle();assert.equal(h.props.value.length,0);
 field(tree,'绑定Skill 安全审查').props.onChange({target:{checked:true}});tree=h.render();
 assert.equal(h.props.value[0].version,2);assert.equal(h.props.value[0].allow_scripts,false);
 assert.ok(h.calls.every(c=>c.method==='GET'));assert.equal(field(tree,'Skill版本 安全审查').props.value,2);
 const draft=nodes(field(tree,'Skill版本 安全审查')).find(n=>n.type==='option'&&n.props.value===3);assert.equal(draft.props.disabled,true);
 field(tree,'Skill版本 安全审查').props.onChange({target:{value:'3'}});assert.equal(h.props.value[0].version,2);
});

test('local binding edits preserve the exact old approved revision and never mutate shared content',async()=>{
 const before=JSON.stringify(skill);const h=harness({value:[binding]});h.render();let tree=await h.settle();
 assert.equal(field(tree,'Skill版本 安全审查').props.value,1,'loading a newer approved revision must not upgrade the binding');
 const parameter=nodes(tree).find(n=>typeof n.type==='function');assert.ok(parameter);
 parameter.props.onChange({focus:'regression',language:'zh'});tree=h.render();
 field(tree,'Skill加载方式 安全审查').props.onChange({target:{value:'always'}});tree=h.render();
 field(tree,'启用Skill绑定 安全审查').props.onChange({target:{checked:false}});tree=h.render();
 assert.equal(h.props.value[0].version,1);assert.equal(h.props.value[0].parameters.focus,'regression');assert.equal(h.props.value[0].enabled,false);
 assert.equal(binding.parameters.focus,'security');assert.equal(JSON.stringify(skill),before);assert.ok(h.calls.every(c=>c.method==='GET'));
 const source=nodes(tree).find(n=>n.type==='SkillBindingSource');assert.equal(source.props.version,1);assert.equal(source.props.skillId,'s');
});

test('revoked and disabled bound Skills remain visible and removable without silent migration',async()=>{
 const h=harness({value:[binding],rows:[{...skill,enabled:false,versions:[{version:1,status:'revoked'}]}]});h.render();const tree=await h.settle();
 assert.match(text(tree),/不会自动升级/);assert.equal(field(tree,'绑定Skill 安全审查').props.disabled,false);
 field(tree,'绑定Skill 安全审查').props.onChange({target:{checked:false}});assert.equal(h.props.value.length,0);assert.ok(h.calls.every(c=>c.method==='GET'));
});

test('search filters candidates while preserving selected bindings and never writes to the shared library',async()=>{
 const h=harness({value:[binding],rows:[skill,{...skill,id:'other',name:'回归测试'}]});h.render();let tree=await h.settle();
 field(tree,'搜索可绑定Skill').props.onChange({target:{value:'no candidate matches'}});tree=h.render();
 assert.ok(nodes(tree).some(n=>n.props?.['aria-label']==='绑定Skill 安全审查'));assert.ok(!nodes(tree).some(n=>n.props?.['aria-label']==='绑定Skill 回归测试'));
 assert.equal(h.props.value[0].version,1);assert.ok(h.calls.every(c=>c.method==='GET'));
});

test('source inspection requests the exact bound version, renders read-only text and has no write action',async()=>{
 const content='Original approved instructions';const h=harness({module:'SkillBindingSource',handler:()=>({version:1,status:'approved',digest:'d'.repeat(64),files:{'SKILL.md':content,'scripts/example.py':'print(1)'}})});
 Object.assign(h.props,{skillId:'s',version:1,name:'安全审查'});const tree=h.render();assert.equal(h.calls.length,0);
 nodes(tree).find(n=>n.type==='button').props.onClick();const loaded=await h.settle();
 assert.deepEqual(h.calls.map(c=>[c.url,c.method]),[['/skills/s?version=1','GET']]);
 assert.equal(field(loaded,'Skill绑定原文 SKILL.md').props.value,content);assert.equal(field(loaded,'Skill绑定原文 SKILL.md').props.readOnly,true);
 assert.equal(field(loaded,'Skill绑定原文 scripts/example.py').props.readOnly,true);assert.match(text(loaded),/不.*生成新版本/);
});
