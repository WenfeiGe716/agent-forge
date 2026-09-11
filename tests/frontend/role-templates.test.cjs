const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('../../frontend/vendor/typescript.cjs');

// A deterministic hook/element harness: no browser, DOM mutation, network, or production data.
// Exercise the same directory module interface used by the full page and team picker.
const role = {id:'a'.repeat(32), revision_id:'b'.repeat(64), digest:'c'.repeat(64),
  title:'后端架构师', description:'职责与交付标准', category:'engineering', category_label:'工程',
  source_id:'agency', source_name:'角色来源', source_enabled:true, available:true,
  license:'MIT', path:'engineering/backend.md', kind:'role_template'};
const detail = {...role, origin:{commit:'immutable-commit'}, warnings:[],
  document:{path:'ROLE.md',content:'# 原始职责\n先评审接口',format:'markdown'},
  files:{'ROLE.md':'# 原始职责\n先评审接口','LICENSE':'MIT license'},
  versions:[{revision_id:role.revision_id,created_at:'2026-01-01T00:00:00Z'}]};
const roles = {planner:'规划',developer:'开发',tester:'测试',reviewer:'审查'};
const tick = async()=>{await new Promise(resolve=>setImmediate(resolve));};
function nodes(value){
  if(Array.isArray(value))return value.flatMap(nodes);
  if(!value||typeof value!=='object')return [];
  return [value,...nodes(value.props?.children)];
}
function text(value){if(Array.isArray(value))return value.map(text).join('');if(value&&typeof value==='object')return text(value.props?.children);return value==null?'':String(value);}
function button(tree,label){const found=nodes(tree).find(n=>n.type==='button'&&text(n)===label);assert.ok(found,`button ${label}`);return found;}
function field(tree,label){const found=nodes(tree).find(n=>n.props?.['aria-label']===label);assert.ok(found,`field ${label}`);return found;}
function harness({admin=true,onMember,onOpenTeam,handler,instructionsOnly=false}={}){
  let cursor=0,tree,component,props;const slots=[],effects=[],calls=[];
  const React={useState(initial){const i=cursor++;if(!(i in slots))slots[i]=typeof initial==='function'?initial():initial;return [slots[i],value=>{slots[i]=typeof value==='function'?value(slots[i]):value;}];},
    useRef(initial){const i=cursor++;if(!(i in slots))slots[i]={current:initial};return slots[i];},
    useEffect(fn,deps){const i=cursor++;if(!(i in slots)||deps.some((v,j)=>!Object.is(v,slots[i][j]))){slots[i]=deps;effects.push(fn);}}};
  const jsx=(type,props)=>({type,props:props||{}});
  const api=async(url,method='GET',body)=>{calls.push({url,method,body});if(handler){const result=handler(url,method,body);if(result!==undefined)return await result;}
    if(url==='/skill-store/sources')return [{id:'agency',name:'角色来源',counts:{skill:0,role_template:1}},{id:'methods',name:'方法来源',counts:{skill:3,role_template:0}}];
    if(url.startsWith('/role-templates/items?'))return {items:[role],total:1,categories:{engineering:'工程'}};
    if(url===`/role-templates/items/${role.id}`||url.startsWith(`/role-templates/items/${role.id}?`))return detail;
    throw Error('Unexpected request '+url);
  };
  const source=fs.readFileSync(path.resolve(__dirname,'../../frontend/src/components/RoleTemplates.tsx'),'utf8').replace(/import\s+['"][^'"]+\.css['"];?/g,'');
  const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText;
  const exports={};const requireMock=name=>name==='react'?React:name==='react/jsx-runtime'?{jsx,jsxs:jsx,Fragment:'Fragment'}:name==='../api'?{api}:name==='../types'?{roles}:name==='./UI'?{Field:'Field',Empty:'Empty',Modal:'Modal'}:name==='./Icons'?new Proxy({}, {get:(_,key)=>String(key)}):assert.fail('Unexpected module '+name);
  vm.runInNewContext(code,{exports,require:requireMock,URLSearchParams,console,encodeURIComponent});
  if(onMember){const picker=exports.RoleTemplatePicker({onClose(){},onMember,instructionsOnly});const browser=nodes(picker).find(n=>typeof n.type==='function');component=browser.type;props=browser.props;}
  else{const page=exports.default({admin,onOpenTeam,onSources(){},onSkills(){}});const browser=nodes(page).find(n=>typeof n.type==='function');component=browser.type;props=browser.props;}
  const render=()=>{cursor=0;tree=component(props);const pending=effects.splice(0);pending.forEach(fn=>fn());return tree;};
  return {calls,render,get tree(){return tree;},async settle(){await tick();return render();}};
}

test('role directory is separate, sources are scoped, and viewer cannot select or compose',async()=>{
  const h=harness({admin:false});h.render();let tree=await h.settle();
  assert.match(text(tree),/1 个角色模板/);
  assert.ok(h.calls.some(c=>c.url.startsWith('/role-templates/items?')));
  assert.ok(!h.calls.some(c=>c.url.startsWith('/skill-store/items')));
  assert.match(text(field(tree,'按角色模板来源筛选')),/角色来源/);
  assert.doesNotMatch(text(field(tree,'按角色模板来源筛选')),/方法来源/);
  button(tree,'查看内容与选择').props.onClick();tree=await h.settle();
  assert.equal(field(tree,'角色模板原始内容').props.value,'# 原始职责\n先评审接口');
  assert.ok(!nodes(tree).some(n=>n.type==='button'&&text(n)==='选择此版本'));
  assert.ok(!nodes(tree).some(n=>n.props?.['aria-label']==='确认角色职责映射'));
  assert.ok(h.calls.every(c=>c.method==='GET'));
});

test('explicit role mapping composes an exact-version unpublished team only',async()=>{
  let opened;const draft={id:'draft',kind:'team',status:'draft',version:1};
  const h=harness({onOpenTeam:asset=>{opened=asset;},handler:(url,method)=>url==='/role-templates/compose'?draft:undefined});
  h.render();let tree=await h.settle();button(tree,'查看内容与选择').props.onClick();tree=await h.settle();
  button(tree,'选择此版本').props.onClick();tree=h.render();
  assert.equal(button(tree,'生成团队草稿并编辑').props.disabled,true);
  field(tree,'后端架构师 执行职责映射').props.onChange({target:{value:'planner'}});
  field(tree,'后端架构师 成员名称').props.onChange({target:{value:'Atlas'}});tree=h.render();
  nodes(tree).find(n=>n.type==='input'&&n.props.type==='checkbox').props.onChange({target:{checked:true}});tree=h.render();
  assert.equal(button(tree,'生成团队草稿并编辑').props.disabled,false);
  button(tree,'生成团队草稿并编辑').props.onClick();await h.settle();
  assert.equal(opened.status,'draft');
  const writes=h.calls.filter(c=>c.method!=='GET');assert.equal(writes.length,1);
  assert.equal(writes[0].url,'/role-templates/compose');
  assert.deepEqual(JSON.parse(JSON.stringify(writes[0].body)),{name:'我的团队方案',items:[{item_id:role.id,revision_id:role.revision_id,digest:role.digest,role:'planner',name:'Atlas'}]});
});

test('team picker returns an unsaved member and never creates a team or Skill',async()=>{
  let chosen;const member={key:'role_a',name:'后端架构师',role:'developer',model_profile_id:null,skills:[],role_template_ref:{item_id:role.id,revision_id:role.revision_id,digest:role.digest}};
  const h=harness({onMember:result=>{chosen=result;},handler:url=>url.endsWith('/proposal')?{member,warnings:[]}:undefined});
  h.render();let tree=await h.settle();button(tree,'查看内容与选择').props.onClick();tree=await h.settle();button(tree,'选择此版本').props.onClick();tree=h.render();
  nodes(tree).find(n=>n.type==='input'&&n.props.type==='checkbox').props.onChange({target:{checked:true}});tree=h.render();button(tree,'加入当前编辑态').props.onClick();await h.settle();
  assert.equal(chosen,member);assert.equal(chosen.model_profile_id,null);assert.deepEqual(chosen.skills,[]);
  assert.deepEqual(h.calls.filter(c=>c.method!=='GET').map(c=>c.url),[`/role-templates/items/${role.id}/proposal`]);
});

test('slow prior search cannot replace current search results',async()=>{
  const pending=[];const h=harness({handler:url=>url.startsWith('/role-templates/items?')?new Promise(resolve=>pending.push({url,resolve})):undefined});
  let tree=h.render();field(tree,'搜索角色模板').props.onChange({target:{value:'测试'}});h.render();
  assert.equal(pending.length,2);
  pending[1].resolve({items:[{...role,id:'d'.repeat(32),title:'测试负责人'}],total:1,categories:{}});tree=await h.settle();assert.match(text(tree),/测试负责人/);
  pending[0].resolve({items:[role],total:1,categories:{}});tree=await h.settle();assert.match(text(tree),/测试负责人/);assert.doesNotMatch(text(tree),/后端架构师/);
});

test('Agent instructions-only picker imports a reviewed copy directly without saving shared state',async()=>{
 let chosen;const member={name:'后端架构师',job_title:'后端架构师',role:'developer',instructions:'可编辑职责原文',role_template_ref:{item_id:role.id,revision_id:role.revision_id,digest:role.digest}};
 const h=harness({instructionsOnly:true,onMember:result=>{chosen=result;},handler:url=>url.endsWith('/proposal')?{member}:undefined});
 h.render();let tree=await h.settle();assert.doesNotMatch(text(tree),/职责映射由你确认/);button(tree,'查看内容与选择').props.onClick();tree=await h.settle();assert.doesNotMatch(text(tree),/确认职责映射并编辑团队配置/);assert.match(text(tree),/直接导入当前 Agent/);button(tree,'导入到当前 Agent').props.onClick();await h.settle();
 assert.equal(chosen.job_title,'后端架构师');
 assert.equal(chosen,member);assert.deepEqual(h.calls.filter(c=>c.method!=='GET').map(c=>c.url),[`/role-templates/items/${role.id}/proposal`]);
});
