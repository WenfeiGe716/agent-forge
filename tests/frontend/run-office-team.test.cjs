const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const {createRequire}=require('node:module');
const ts=require('../../frontend/vendor/typescript.cjs');
function helpers(name,names){const filename=require.resolve('./'+name),local=createRequire(filename),exports={};vm.runInNewContext(fs.readFileSync(filename,'utf8')+'\nObject.assign(exports,{'+names.join(',')+'});',{exports,__dirname:path.dirname(filename),console,performance,Date,setImmediate,URL,require:Object.assign(id=>id==='node:test'?()=>{}:local(id),{resolve:local.resolve})},{filename});return exports;}
const forms=helpers('execution-center.test.cjs',['harness','nodes','text','field','button','labelledInput']);
const studio=helpers('organization-workspace.test.cjs',['harness','nodes','text','field','button']);
const agents=['ap','ad','ar','bp','bd','br','u'].map((id,i)=>({id,name:id.toUpperCase(),role:['planner','developer','reviewer'][i%3],enabled:true,model_profile_id:'m',fallback_model_ids:[],runtime:{},model_parameters:{},version:1}));
const providers=[{id:'provider',name:'已选供应商',protocol:'openai_responses',enabled:true}],models=[{id:'m',name:'主模型',provider_id:'provider',enabled:true,supports_tools:true,version:1}];
const organization={ref:{id:'office',version:3,digest:'a'.repeat(64)},config:{floors:[{floor:1,department:'研发部',name:'研发层',zones:[]}],positions:{},teams:[{id:'a-team',name:'服务团队',floor:1,member_ids:['ap','ad','ar'],headcount:3},{id:'b-team',name:'客户端团队',floor:1,member_ids:['bp','bd','br'],headcount:3},{id:'empty',name:'空团队',floor:1,member_ids:[],headcount:4}]}};
const project={id:'project',name:'项目',execution_mode:'local',permission_profile:'ask',runtime:{},members:[],allow_external_models:true};
const ready={ready:true,checked_at:'2026-09-10T10:00:00Z',checks:[],agents:[],runner:{},sandbox:{},project_id:'project'};
function run(props={},handler){return forms.harness('RunForm',{props:{agents,providers,models,organization,projects:[project],initial:{project_id:'project',title:'测试需求',request:'完成这个测试需求'},...props},handler:(url,method,body)=>{if(handler){const r=handler(url,method,body);if(r!==undefined)return r;}if(url==='/execution/preflight')return ready;}});}
const checked=tree=>forms.nodes(tree).filter(n=>n.type==='input'&&n.props.type==='checkbox'&&n.props.checked&&n.props['aria-label']?.startsWith('授权成员 '));

test('RunForm lists distinct applied office teams and never defaults to authorizing the whole organization',async()=>{
 const h=run();h.render();const tree=await h.settle(),selector=forms.field(tree,'任务办公团队');
 assert.match(forms.text(selector),/研发部.*服务团队.*3/);assert.match(forms.text(selector),/研发部.*客户端团队.*3/);
 assert.equal(checked(tree).length,0);
});

test('office team selection fills only its members and switching removes an out-of-team entry',async()=>{
 const h=run();h.render();let tree=await h.settle();forms.field(tree,'任务办公团队').props.onChange({target:{value:'a-team'}});tree=h.render();
 assert.deepEqual(Array.from(checked(tree),n=>n.props['aria-label']),['授权成员 AP','授权成员 AD','授权成员 AR']);
 forms.field(tree,'入口负责人').props.onChange({target:{value:'ap'}});
 forms.field(h.render(),'任务办公团队').props.onChange({target:{value:'b-team'}});tree=h.render();
 assert.notEqual(forms.field(tree,'入口负责人').props.value,'ap');
 const options=forms.nodes(forms.field(tree,'入口负责人')).filter(n=>n.type==='option'&&n.props.value).map(n=>n.props.value);
 assert.ok(options.every(id=>['bp','bd','br'].includes(id)));assert.equal(checked(tree).length,3);
});

test('empty teams cannot fall back to all agents or create a run',async()=>{
 const h=run();h.render();let tree=await h.settle();forms.field(tree,'任务办公团队').props.onChange({target:{value:'empty'}});tree=h.render();
 assert.equal(checked(tree).length,0);await assert.rejects(tree.props.onSubmit(),/成员|团队/);
 assert.ok(!h.calls.some(c=>c.url==='/runs'));
});

test('office team and exact applied reference are sent to preflight before the identical explicit run payload',async()=>{
 const h=run({initial:{office_team_id:'a-team',office_layout_ref:organization.ref,agent_ids:['ap','ad','ar'],entry_agent_id:'ap',project_id:'project',title:'测试需求',request:'完成这个测试需求'}});
 h.render();const tree=await h.settle();await tree.props.onSubmit();
 const writes=h.calls.filter(c=>['/execution/preflight','/runs'].includes(c.url));assert.deepEqual(Array.from(writes,c=>c.url),['/execution/preflight','/runs']);
 assert.equal(writes[0].body.office_team_id,'a-team');assert.deepEqual(JSON.parse(JSON.stringify(writes[0].body.office_layout_ref)),organization.ref);
 assert.deepEqual(Array.from(writes[0].body.agent_ids),['ap','ad','ar']);assert.deepEqual(JSON.parse(JSON.stringify(writes[0].body)),JSON.parse(JSON.stringify(writes[1].body)));
});

test('blocked automatic preflight keeps the draft and exposes specific member, model and provider context',async()=>{
 const blocked={...ready,ready:false,checks:[{code:'model.tools',status:'error',message:'备用模型未声明工具能力',agent_id:'ap',model_profile_id:'m',provider_id:'provider',target:'models'}]};
 let done=0;const h=run({initial:{agent_ids:['ap','ad','ar'],entry_agent_id:'ap',request:'仍然保留的需求',title:'测试',project_id:'project'},onDone(){done++;}},url=>url==='/execution/preflight'?blocked:undefined);
 h.render();let tree=await h.settle();await assert.rejects(tree.props.onSubmit(),/预检|配置|工具/);tree=h.render();
 assert.match(forms.text(tree),/备用模型未声明工具能力/);assert.match(forms.text(tree),/AP/);assert.match(forms.text(tree),/主模型/);assert.match(forms.text(tree),/已选供应商/);
 assert.equal(forms.labelledInput(tree,'任务需求与验收期望').props.value,'仍然保留的需求');assert.equal(done,0);assert.ok(!h.calls.some(c=>c.url==='/runs'));
});

test('a draft edit during automatic preflight cancels creation rather than using stale readiness',async()=>{
 let resolve;const h=run({initial:{agent_ids:['ap','ad','ar'],entry_agent_id:'ap',request:'需求内容',title:'测试',project_id:'project'}},url=>url==='/execution/preflight'?new Promise(done=>resolve=done):undefined);
 h.render();let tree=await h.settle();const submitted=tree.props.onSubmit();forms.labelledInput(tree,'任务名称').props.onChange({target:{value:'新标题'}});h.render();resolve(ready);
 await assert.rejects(submitted,/变化|重新/);assert.ok(!h.calls.some(c=>c.url==='/runs'));
});

test('changed applied office layout requires explicit reselection and never silently expands members',async()=>{
 const h=run({initial:{office_team_id:'a-team',office_layout_ref:organization.ref,agent_ids:['ap','ad','ar'],entry_agent_id:'ap'}});h.render();await h.settle();
 const changed={...organization,ref:{...organization.ref,version:4}};const tree=h.setProps({organization:changed});
 await assert.rejects(tree.props.onSubmit(),/布局.*更新|重新选择/);assert.ok(!h.calls.some(c=>c.url==='/runs'));
});

test('Studio creates an office-team seed and limits recipients to that team',async()=>{
 const seeds=[],h=studio.harness('StudioWorkspace',{organization,agents,projects:[project],models,providers,onCreateTask:seed=>seeds.push(seed)});
 let tree=h.render();studio.field(tree,'需求办公团队').props.onChange({target:{value:'a-team'}});tree=h.render();
 const options=studio.nodes(studio.field(tree,'指令收件人')).filter(n=>n.type==='option'&&n.props.value).map(n=>n.props.value);assert.deepEqual(Array.from(options),['ap','ad','ar']);
 studio.field(tree,'指令收件人').props.onChange({target:{value:'ap'}});studio.field(tree,'任务需求').props.onChange({target:{value:'完成需求'}});tree=h.render();studio.field(tree,'创建任务').props.onClick();await new Promise(r=>setImmediate(r));
 assert.equal(seeds.length,1);assert.equal(seeds[0].office_team_id,'a-team');assert.deepEqual(Array.from(seeds[0].agent_ids),['ap','ad','ar']);assert.deepEqual(JSON.parse(JSON.stringify(seeds[0].office_layout_ref)),organization.ref);
 studio.field(tree,'需求办公团队').props.onChange({target:{value:'b-team'}});assert.equal(studio.field(h.render(),'指令收件人').props.value,'');
});

test('Studio manual mode does not imply all organization members',async()=>{
 const seeds=[],h=studio.harness('StudioWorkspace',{organization,agents,projects:[project],onCreateTask:seed=>seeds.push(seed)});let tree=h.render();
 studio.field(tree,'任务需求').props.onChange({target:{value:'手动选成员的需求'}});tree=h.render();studio.field(tree,'创建任务').props.onClick();await new Promise(r=>setImmediate(r));
 assert.deepEqual(Array.from(seeds[0].agent_ids),[]);assert.equal(seeds[0].office_team_id,null);
});

test('Studio stopped task gives a clear visible route to the reason and recovery actions',()=>{
 let opened=0;const h=studio.harness('StudioWorkspace',{detail:{id:'stopped',status:'needs_attention',error:'3 validation errors for Plan',snapshot:{}},onBoard:()=>opened++});
 const tree=h.render();assert.match(studio.text(tree),/计划格式不符合要求/);studio.button(tree,'查看原因与处理').props.onClick();assert.equal(opened,1);
});

test('Studio append recipients come from the selected run snapshot, not the entire office team',()=>{
 const h=studio.harness('StudioWorkspace',{organization,agents,projects:[project],detail:{id:'old',status:'running',snapshot:{agents:{ad:{}}}},selectedRun:'old'});let tree=h.render();
 studio.field(tree,'指令类型').props.onChange({target:{value:'append'}});tree=h.render();
 assert.deepEqual(Array.from(studio.nodes(studio.field(tree,'指令收件人')).filter(n=>n.type==='option'&&n.props.value),n=>n.props.value),['ad']);
});

test('a historical discussion seed creates a new autonomous task without copying its workflow',async()=>{
 const graph={mode:'discussion',participants:['ap','ad'],nodes:[],edges:[]};
 const h=run({initial:{agent_ids:['ap','ad'],entry_agent_id:'ad',orchestration:graph,collaboration_flow:false,parent_run_id:'old'}});h.render();const tree=await h.settle();await tree.props.onSubmit();
 const body=h.calls.find(c=>c.url==='/runs').body;assert.equal(body.execution_strategy,'autonomous');assert.equal(body.orchestration,undefined);assert.equal(body.entry_agent_id,'ad');
});

test('new tasks do not load or apply a recovered orchestration template',async()=>{
 const h=run({initial:{template_id:'workflow',template_version:1,agent_ids:['ap','ad','ar'],entry_agent_id:'ap'}},url=>url==='/orchestrations'?[{id:'workflow',version:2,name:'流程',enabled:true,graph:{mode:'development',nodes:[],edges:[]}}]:undefined);
 h.render();const tree=await h.settle();await tree.props.onSubmit();const body=h.calls.find(c=>c.url==='/runs').body;assert.equal(body.execution_strategy,'autonomous');assert.equal(body.template_id,undefined);assert.equal(body.template_version,undefined);assert.ok(!h.calls.some(c=>c.url==='/orchestrations'));assert.doesNotMatch(forms.text(tree),/默认开发流程|本次自定义编排/);
});

test('single-member autonomy uses that member regardless of role and derives an optional task title',async()=>{
 const h=run({initial:{agent_ids:['ad'],request:'形成市场需求文档'}});h.render();const tree=await h.settle();await tree.props.onSubmit();const body=h.calls.find(c=>c.url==='/runs').body;
 assert.equal(body.execution_strategy,'autonomous');assert.equal(body.entry_agent_id,'ad');assert.equal(body.title,'形成市场需求文档');assert.equal(body.run_project_checks,false);assert.equal(body.collaboration_flow,true);
 assert.ok(!h.calls.some(c=>['/orchestrations','/design/assets'].includes(c.url)));assert.doesNotMatch(forms.text(tree),/恰好一名|一名独立审查|任务编排方式|团队模板版本/);
});

test('multiple members require an explicit owner and do not choose the first planner automatically',async()=>{
 const h=run({initial:{agent_ids:['ap','ad'],request:'分析用户需求'}});h.render();const tree=await h.settle();await assert.rejects(tree.props.onSubmit(),/请选择.*负责人/);assert.equal(h.calls.filter(c=>c.method==='POST').length,0);
});

test('explicit project checks and edited runtime survive configuration navigation without model overrides',async()=>{
 let saved;const h=run({initial:{agent_ids:['ad'],request:'验证并生成文档',runtime:{max_steps:7}},onNavigate:(target,context,draft)=>saved=draft});h.render();let tree=await h.settle();
 forms.field(tree,'自动运行项目固定检查').props.onChange({target:{checked:true}});tree=h.render();forms.button(tree,'修改项目执行与权限').props.onClick();assert.equal(saved.run_project_checks,true);assert.equal(saved.execution_strategy,'autonomous');assert.match(saved._runtime_text,/max_steps/);
 await tree.props.onSubmit();const body=h.calls.find(c=>c.url==='/runs').body;assert.equal(body.run_project_checks,true);assert.equal(body.runtime.max_steps,7);assert.equal(body.model_parameters,undefined);assert.equal(body.permission_profile,undefined);
});

test('changing optional checks during preflight prevents a stale task creation',async()=>{
 let resolve;const h=run({initial:{agent_ids:['ad'],request:'生成报告'}},url=>url==='/execution/preflight'?new Promise(done=>resolve=done):undefined);h.render();let tree=await h.settle();const pending=tree.props.onSubmit();
 forms.field(tree,'自动运行项目固定检查').props.onChange({target:{checked:true}});h.render();resolve(ready);await assert.rejects(pending,/变化|重新/);assert.ok(!h.calls.some(c=>c.url==='/runs'));
});

test('preflight configuration navigation carries a recoverable draft including explicit scope and edited text',async()=>{
 let navigation;const h=run({initial:{office_team_id:'a-team',office_layout_ref:organization.ref,agent_ids:['ap','ad','ar'],entry_agent_id:'ap'},onNavigate:(...args)=>navigation=args});h.render();let tree=await h.settle();
 forms.labelledInput(tree,'任务需求与验收期望').props.onChange({target:{value:'修配置后必须保留的需求'}});tree=h.render();forms.button(tree,'修改项目执行与权限').props.onClick();
 assert.equal(navigation[0],'projects');assert.equal(navigation[2]?.request,'修配置后必须保留的需求');assert.equal(navigation[2]?.office_team_id,'a-team');assert.deepEqual(Array.from(navigation[2]?.agent_ids||[]),['ap','ad','ar']);
});

test('App Studio create-task callback mounts the real dialog with organization and provider context',()=>{
 let cursor=0;const slots=[],exports={},jsx=(type,props)=>({type,props:props||{}}),fixture={organization,providers,models,agents,projects:[project],user:{id:'user',username:'用户',role:'admin'}};
 let source=fs.readFileSync(path.resolve(__dirname,'../../frontend/src/App.tsx'),'utf8');
 for(const [type,key]of[['OrganizationLayout|null','organization'],['User|null','user'],['Provider[]','providers'],['Model[]','models'],['Agent[]','agents'],['Project[]','projects']])source=source.replace('useState<'+type+'>('+(type.endsWith('[]')?'[]':'null')+')','useState<'+type+'>(fixture.'+key+')');
 source=source.replace('[booting,setBooting]=useState(true)','[booting,setBooting]=useState(false)');
 const React={useState(value){const i=cursor++;if(!(i in slots))slots[i]=typeof value==='function'?value():value;return[slots[i],value=>slots[i]=typeof value==='function'?value(slots[i]):value];},useRef(value){const i=cursor++;return slots[i]||(slots[i]={current:value});},useEffect(){},useCallback(fn){return fn;}};
 const requireMock=name=>{if(name==='react')return React;if(name==='react/jsx-runtime')return{jsx,jsxs:jsx};if(name==='./desktop')return{desktopBridge:()=>undefined};if(name==='./types')return{terminal:[],roles:{},states:{},agentTitle:a=>a.name};if(name==='./api')return{api(){throw Error('unexpected API call');},APIError:Error,jsonText:JSON.stringify};return new Proxy({__esModule:true,default:name.split('/').at(-1)},{get:(obj,key)=>key in obj?obj[key]:String(key)});};
 vm.runInNewContext(ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports,fixture,require:requireMock,console});
 const render=()=>{cursor=0;return exports.default();};let tree=render();const workspace=forms.nodes(tree).find(n=>n.type==='StudioWorkspace');assert.ok(workspace);assert.ok(!forms.nodes(tree).some(n=>n.type==='button'&&forms.text(n)==='多Agent编排'));
 workspace.props.onCreateTask({request:'新需求',office_team_id:'a-team',office_layout_ref:organization.ref,agent_ids:['ap','ad','ar']});tree=render();
 assert.ok(forms.nodes(tree).some(n=>n.type==='Modal'&&n.props.title==='发起任务'));
 const form=forms.nodes(tree).find(n=>n.type==='RunForm');assert.ok(form);assert.equal(form.props.organization,organization);assert.equal(form.props.providers,providers);assert.equal(form.props.initial.office_team_id,'a-team');
 const draft={request:'去修模型的需求',office_team_id:'a-team',office_layout_ref:organization.ref,agent_ids:['ap','ad','ar']};
 form.props.onNavigate('models',{model_profile_id:'m',provider_id:'provider'},draft);tree=render();
 assert.ok(forms.nodes(tree).some(n=>n.type==='ModelForm'&&n.props.initial?.id==='m'),'opens the exact model configuration');
 const returnButton=forms.nodes(tree).find(n=>n.type==='button'&&forms.text(n)==='返回未提交的需求');assert.ok(returnButton);returnButton.props.onClick();tree=render();
 assert.equal(forms.nodes(tree).find(n=>n.type==='RunForm').props.initial.request,draft.request);
});
