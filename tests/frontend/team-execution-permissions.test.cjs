const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const ts=require('../../frontend/vendor/typescript.cjs'),source=fs.readFileSync(require.resolve('../../frontend/src/components/TeamExecutionPermissions.tsx'),'utf8');
const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText;
const nodes=v=>Array.isArray(v)?v.flatMap(nodes):v&&typeof v==='object'?[v,...nodes(v.props?.children)]:[];
const text=v=>Array.isArray(v)?v.map(text).join(''):v&&typeof v==='object'?text(v.props?.children):v==null||typeof v==='boolean'?'':String(v);
const labels={ask:'请求审批',auto:'帮我批准',full:'完全访问'};
const row=(id,name,extra={})=>({id,name,permission_profile:null,effective_profile:'ask',inherited_profile:'ask',source:'project',source_id:'p',source_name:'项目默认',...extra});
function initial(){return{version:4,layout_ref:{id:'layout',version:2,digest:'layout-digest'},project:{id:'p',name:'项目',permission_profile:'ask',execution_mode:'local'},teams:[row('dev','开发团队',{department:'研发部',floor:1,member_ids:['a','b'],members:[{id:'a',name:'Atlas'},{id:'b',name:'Nova'}]})],agents:[row('a','Atlas',{role:'planner',team_id:'dev',team_name:'开发团队'}),row('b','Nova',{role:'developer',team_id:'dev',team_name:'开发团队'}),row('c','无团队成员',{team_id:null,team_name:''})]};}
function harness({handler,props={},data=initial()}={}){
 let cursor=0,currentData=data;const slots=[],effects=[],cleanups=[],calls=[],saved=[];
 const React={useState(v){const i=cursor++;if(!(i in slots))slots[i]=typeof v==='function'?v():v;return[slots[i],x=>slots[i]=typeof x==='function'?x(slots[i]):x];},useRef(v){const i=cursor++;return slots[i]||(slots[i]={current:v});},useEffect(fn,deps){const i=cursor++;if(!(i in slots)||deps.some((d,j)=>!Object.is(d,slots[i][j]))){cleanups[i]?.();slots[i]=deps;effects.push(()=>cleanups[i]=fn());}}};
 const jsx=(type,props)=>typeof type==='function'?type(props||{}):{type,props:props||{}};
 const exports={};vm.runInNewContext(code,{exports,console,Map,encodeURIComponent,require:id=>{
  if(id==='react')return React;if(id==='react/jsx-runtime')return{jsx,jsxs:jsx,Fragment:'fragment'};
  if(id==='../api')return{api:async(url,method='GET',body)=>{calls.push({url,method,body});if(handler)return handler(url,method,body);if(method==='PUT'){const [scope,id]=url.split('?')[0].split('/').slice(-2);currentData={...currentData,version:currentData.version+1,[scope==='team'?'teams':'agents']:currentData[scope==='team'?'teams':'agents'].map(row=>row.id===id?{...row,permission_profile:body.permission_profile,effective_profile:body.permission_profile||row.inherited_profile}:row)};}return currentData;}};
  if(id==='./ExecutionCenter')return{permissionLabels:labels,permissionDescriptions:{ask:'需要越界或联网时请求审批。',auto:'审批 Agent 判断，不确定交给人。',full:'允许访问当前账号的文件和网络。'}};
  if(id==='./Icons')return new Proxy({},{get:(_,key)=>props=>jsx('icon-'+String(key),props)});if(id.endsWith('.css'))return{};throw Error(id);
 }});
 let parameters={admin:true,projectId:'p',initialTeamId:'dev',onSaved:()=>saved.push(true),...props};
 const render=()=>{cursor=0;const tree=exports.default(parameters);effects.splice(0).forEach(fn=>fn());return tree;};
 return{exports,render,calls,saved,setData(v){currentData=v;},setProps(next){parameters={...parameters,...next};return render();},async settle(){await new Promise(resolve=>setImmediate(resolve));return render();},unmount(){cleanups.forEach(fn=>fn?.());}};
}
const editor=(tree,scope,id)=>nodes(tree).find(n=>n.props?.['data-policy-scope']===scope&&n.props?.['data-policy-id']===id);
const select=tree=>nodes(tree).find(n=>n.type==='select');
const button=(tree,label)=>{const result=nodes(tree).find(n=>n.type==='button'&&text(n)===label);assert.ok(result,label);return result;};
const change=(tree,value)=>select(tree).props.onChange({target:{value}});

test('permission panel reads current policies without sending changes or inferring a default full profile',async()=>{
 const h=harness();h.render();const tree=await h.settle();assert.equal(h.calls.length,1);assert.equal(h.calls[0].url,'/execution/policies?project_id=p');assert.equal(h.calls[0].method,'GET');assert.match(text(tree),/只对新任务生效/);assert.equal(select(editor(tree,'team','dev')).props.value,'');assert.ok(!nodes(tree).some(n=>n.type==='input'&&n.props.type==='checkbox'));
});
test('no selected project means unknown inherited profile, never an invented approval default',async()=>{
 const data=initial();data.project=null;data.teams[0].effective_profile=null;data.teams[0].inherited_profile=null;
 const h=harness({data,props:{projectId:''}});h.render();const tree=await h.settle();assert.equal(h.calls[0].url,'/execution/policies');assert.match(text(editor(tree,'team','dev')),/随任务项目确定/);assert.match(text(tree),/未选择项目/);
});
test('read-only users cannot edit or save even through an invoked control callback',async()=>{
 const h=harness({props:{admin:false}});h.render();let tree=await h.settle();const field=select(editor(tree,'agent','a'));assert.equal(field.props.disabled,true);field.props.onChange({target:{value:'full'}});tree=h.render();assert.ok(h.calls.every(call=>call.method==='GET'));assert.ok(!text(tree).includes('保存成员权限'));
});
test('explicit full access requires its own unchecked risk confirmation before PUT',async()=>{
 const h=harness();h.render();let tree=await h.settle();change(editor(tree,'agent','a'),'full');tree=h.render();let row=editor(tree,'agent','a');
 assert.equal(button(row,'保存成员权限').props.disabled,true);assert.equal(nodes(row).find(n=>n.type==='input'&&n.props.type==='checkbox').props.checked,false);
 await button(row,'保存成员权限').props.onClick();assert.ok(h.calls.every(call=>call.method==='GET'));
 nodes(row).find(n=>n.type==='input'&&n.props.type==='checkbox').props.onChange({target:{checked:true}});tree=h.render();row=editor(tree,'agent','a');assert.equal(button(row,'保存成员权限').props.disabled,false);
 button(row,'保存成员权限').props.onClick();await h.settle();const call=h.calls.find(c=>c.method==='PUT');assert.equal(call.url,'/execution/policies/agent/a?project_id=p');assert.deepEqual(JSON.parse(JSON.stringify(call.body)),{permission_profile:'full',expected_version:4,expected_layout_ref:initial().layout_ref,full_access_confirmed:true});assert.equal(h.saved.length,1);
});
test('restoring member inheritance from a full team also requires explicit confirmation',async()=>{
 const data=initial();Object.assign(data.teams[0],{permission_profile:'full',effective_profile:'full'});Object.assign(data.agents[0],{permission_profile:'ask',source:'agent',inherited_profile:'full'});
 const h=harness({data});h.render();let tree=await h.settle();change(editor(tree,'agent','a'),'');tree=h.render();const row=editor(tree,'agent','a');assert.match(text(row),/完全访问/);assert.equal(button(row,'保存成员权限').props.disabled,true);assert.ok(nodes(row).some(n=>n.type==='input'&&n.props.type==='checkbox'&&!n.props.checked));
});
test('changing profiles resets confirmation and never changes teammate profiles implicitly',async()=>{
 const h=harness();h.render();let tree=await h.settle();change(editor(tree,'team','dev'),'full');tree=h.render();nodes(editor(tree,'team','dev')).find(n=>n.type==='input'&&n.props.type==='checkbox').props.onChange({target:{checked:true}});tree=h.render();change(editor(tree,'team','dev'),'auto');tree=h.render();assert.ok(!nodes(editor(tree,'team','dev')).some(n=>n.type==='input'&&n.props.type==='checkbox'));change(editor(tree,'team','dev'),'full');tree=h.render();assert.equal(nodes(editor(tree,'team','dev')).find(n=>n.type==='input'&&n.props.type==='checkbox').props.checked,false);assert.equal(select(editor(tree,'agent','a')).props.value,'');assert.ok(h.calls.every(c=>c.method==='GET'));
});
test('a global version update freezes other drafts until the user explicitly discards and rechecks',async()=>{
 const h=harness();h.render();let tree=await h.settle();change(editor(tree,'team','dev'),'auto');tree=h.render();change(editor(tree,'agent','a'),'auto');tree=h.render();button(editor(tree,'team','dev'),'保存团队默认').props.onClick();tree=await h.settle();const member=editor(tree,'agent','a');assert.match(text(member),/权限配置已变化/);assert.equal(button(member,'保存成员权限').props.disabled,true);assert.equal(select(member).props.disabled,true);button(member,'放弃草稿并采用当前配置').props.onClick();tree=h.render();assert.equal(select(editor(tree,'agent','a')).props.value,'');assert.equal(h.calls.filter(c=>c.method==='PUT').length,1);
});
test('a rejected write is not retried automatically and retains a clear refresh path',async()=>{
 const h=harness({handler:(url,method)=>{if(method==='PUT')throw Error('权限配置版本冲突，请刷新');return initial();}});h.render();let tree=await h.settle();change(editor(tree,'agent','b'),'auto');tree=h.render();button(editor(tree,'agent','b'),'保存成员权限').props.onClick();tree=await h.settle();const row=editor(tree,'agent','b');assert.match(text(row),/保存失败：权限配置版本冲突/);assert.equal(button(row,'保存成员权限').props.disabled,true);assert.ok(button(row,'重新读取当前配置'));assert.equal(h.calls.filter(c=>c.method==='PUT').length,1);
});
test('unassigned members, search and initial employee targeting do not alter saved policies',async()=>{
 const h=harness({props:{initialAgentId:'c'}});h.render();let tree=await h.settle();tree=h.render();assert.ok(editor(tree,'agent','c'));assert.ok(!editor(tree,'agent','a'));assert.ok(nodes(tree).some(n=>n.props?.className==='team-policy-member is-focused'));button(tree,'全部成员3').props.onClick();tree=h.render();nodes(tree).find(n=>n.props?.['aria-label']==='搜索权限配置成员').props.onChange({target:{value:'Nova'}});tree=h.render();assert.ok(editor(tree,'agent','b'));assert.ok(!editor(tree,'agent','a'));assert.ok(h.calls.every(c=>c.method==='GET'));
});
test('late reads from a different project cannot replace the current project preview',async()=>{
 let release;const h=harness({handler:url=>url.includes('project_id=p')&&!url.includes('project_id=p2')?new Promise(resolve=>release=resolve):{...initial(),project:{id:'p2',name:'第二项目',permission_profile:'auto'}}});h.render();h.setProps({projectId:'p2'});let tree=await h.settle();assert.match(text(tree),/第二项目/);release(initial());tree=await h.settle();assert.match(text(tree),/第二项目/);assert.equal(nodes(nodes(tree).find(n=>n.props?.className==='team-policy-project')).find(n=>n.type==='small').props.children,'第二项目');
});
test('invalid policy data fails closed rather than exposing a write target',async()=>{
 const h=harness({handler:()=>({teams:[],agents:[]})});h.render();const tree=await h.settle();assert.match(text(tree),/响应不完整/);assert.ok(!nodes(tree).some(n=>n.type==='select'));
});
test('layout changes invalidate drafts even when the policy version is unchanged',async()=>{
 const h=harness();h.render();let tree=await h.settle();change(editor(tree,'agent','a'),'auto');tree=h.render();const data=initial();data.layout_ref={...data.layout_ref,version:3,digest:'new-layout'};h.setData(data);nodes(tree).find(n=>n.props?.['aria-label']==='刷新团队与成员权限').props.onClick();tree=await h.settle();const row=editor(tree,'agent','a');assert.match(text(row),/权限配置已变化/);assert.equal(button(row,'保存成员权限').props.disabled,true);assert.ok(h.calls.every(c=>c.method==='GET'));
});
test('a successful HTTP response without the saved target is not presented as success',async()=>{
 const h=harness({handler:()=>initial()});h.render();let tree=await h.settle();change(editor(tree,'agent','a'),'auto');tree=h.render();button(editor(tree,'agent','a'),'保存成员权限').props.onClick();tree=await h.settle();assert.match(text(tree),/不能据此认为保存成功/);assert.equal(h.saved.length,0);
});
test('mismatched project previews fail closed',async()=>{
 const data=initial();data.project.id='wrong-project';const h=harness({data});h.render();const tree=await h.settle();assert.match(text(tree),/项目权限预览不匹配/);assert.ok(!nodes(tree).some(n=>n.type==='select'));
});
test('a pending write from a previous project cannot claim success or lock the new context',async()=>{
 let release;const h=harness({handler:(url,method)=>{if(method==='PUT')return new Promise(resolve=>release=resolve);if(url.includes('project_id=p2'))return {...initial(),project:{id:'p2',name:'第二项目',permission_profile:'auto'}};return initial();}});h.render();let tree=await h.settle();change(editor(tree,'agent','a'),'auto');tree=h.render();button(editor(tree,'agent','a'),'保存成员权限').props.onClick();h.setProps({projectId:'p2'});tree=await h.settle();const late=initial();late.version=5;late.agents[0].permission_profile='auto';release(late);tree=await h.settle();assert.match(text(tree),/第二项目/);assert.equal(select(editor(tree,'agent','a')).props.disabled,false);assert.equal(h.saved.length,0);
});
