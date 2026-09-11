const assert=require('node:assert/strict');
const test=require('node:test');
const fs=require('node:fs');
const vm=require('node:vm');
const ts=require('../../frontend/vendor/typescript.cjs');
const project={id:'p',name:'项目 P',import_path:'p',sandbox_image:'test/image',check_commands:[],instructions:'',allow_external_models:false,runtime:{},members:[],execution_mode:'local',permission_profile:'ask'};
const agent={id:'a',name:'Ada',job_title:'财务分析师',role:'developer',enabled:true,department:'财务',model_profile_id:'m',fallback_model_ids:[],runtime:{},model_parameters:{}};
const manifest={id:'tool',name:'工具包',version:'1.0',source:'bundled://tool/1.0',sha256:'c'.repeat(64),permissions:['read workspace file'],network:'none',requires:['python3'],operations:['metrics'],files:{'main.py':'print("fixed source")'},install_command:'python3 -I -c "fixed source"'};
const permission={id:'request',run_id:'r',task_key:'job',agent_id:'a',digest:'a'.repeat(64),command:'python3 /reviewed/tool.py',network:true,paths:[{path:'/reviewed',access:'read'}],reason:'需要已审核工具',status:'pending',result:{}};
const installation={id:'install',run_id:'r',task_key:'job',agent_id:'a',digest:'b'.repeat(64),manifest,reason:'需要统计文本',status:'pending',result:{},scope:'本机任务工作副本 /local/tasks/r/job'};
const readiness=id=>({ready:true,checked_at:'2026-09-10T00:00:00Z',inference_tested:false,project_id:id,checks:[{code:'model.inference_not_executed',status:'warning',message:'没有执行模型推理',target:'providers'}],agents:[{...agent,ready:true,tools:['read_file'],checks:[]}],runner:{ready:true},sandbox:{ready:false},execution:{mode:'local',permission_profile:'ask',ready:true,runtime:'native',workspace_scope:'/local/tasks/'+id,network:'denied',detail:'本机项目任务工作副本'}});
const roles={planner:'负责人',developer:'开发工程师',tester:'测试工程师',reviewer:'代码审查'};
function nodes(value){if(Array.isArray(value))return value.flatMap(nodes);if(!value||typeof value!=='object')return[];return[value,...nodes(value.props?.children)];}
function text(value){if(Array.isArray(value))return value.map(text).join('');if(value&&typeof value==='object')return text(value.props?.children);return value==null?'':String(value);}
function expand(value){if(Array.isArray(value))return value.map(expand);if(!value||typeof value!=='object')return value;if(typeof value.type==='function'&&['ExecutionReadinessView','PackageDetails'].includes(value.type.name))return expand(value.type(value.props));return{...value,props:{...value.props,children:expand(value.props?.children)}};}
function field(tree,label){const value=nodes(tree).find(n=>n.props?.['aria-label']===label);assert.ok(value,'field '+label);return value;}
function button(tree,label){const value=nodes(tree).find(n=>n.type==='button'&&text(n)===label);assert.ok(value,'button '+label);return value;}
function labelledInput(tree,label){const group=nodes(tree).find(n=>n.props?.label===label);assert.ok(group,'label '+label);return nodes(group.props.children).find(n=>['input','select','textarea'].includes(n.type));}
function harness(name='ExecutionCenter',{props={},handler,desktop}={}){
 let cursor=0,tree,currentProps=props;
 const slots=[],effects=[],cleanup=[],calls=[],navigation=[];
 const React={useState(initial){const index=cursor++;if(!(index in slots))slots[index]=typeof initial==='function'?initial():initial;return[slots[index],next=>{slots[index]=typeof next==='function'?next(slots[index]):next;}];},useRef(initial){const index=cursor++;if(!(index in slots))slots[index]={current:initial};return slots[index];},useEffect(effect,deps){const index=cursor++;if(!(index in slots)||deps.some((value,i)=>!Object.is(value,slots[index][i]))){cleanup[index]?.();slots[index]=deps;effects.push(()=>cleanup[index]=effect());}}};
 const api=async(url,method='GET',body)=>{calls.push({url,method,body});if(handler){const result=handler(url,method,body);if(result!==undefined)return await result;}if(url.startsWith('/execution/readiness'))return readiness(new URL('http://local'+url).searchParams.get('project_id'));if(url==='/execution/preflight')return readiness(body.project_id);if(url==='/tool-packages')return{items:[manifest],policy:'只读目录，不自动安装'};if(url.startsWith('/execution/permissions')&&method==='GET')return{items:[permission]};if(url.startsWith('/tool-installations')&&method==='GET')return{items:[installation]};if(url==='/design/assets'||url==='/orchestrations')return[];return{id:'created'};};
 const jsx=(type,props)=>({type,props:props||{}}),modules={};
 function load(module){if(modules[module])return modules[module];const exports={};modules[module]=exports;const source=fs.readFileSync(require.resolve('../../frontend/src/components/'+module+'.tsx'),'utf8');const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText;
  const requireMock=id=>id==='react'?React:id==='react/jsx-runtime'?{jsx,jsxs:jsx,Fragment:'Fragment'}:id==='../desktop'?{desktopBridge:()=>desktop}:id==='../api'?{api,jsonText:value=>JSON.stringify(value??{},null,2),parseObject:value=>JSON.parse(value||'{}'),requestId:()=> 'fixed-request-id'}:id==='../types'?{roles,agentTitle:a=>a.job_title||roles[a.role]||a.role}:id==='./UI'?{Empty:'Empty',Field:'Field',SaveButton:'SaveButton'}:id==='./Skills'?{SkillBindings:'SkillBindings'}:id==='./RoleTemplates'?{RoleTemplatePicker:'RoleTemplatePicker',RoleTemplateSource:'RoleTemplateSource'}:id==='./Orchestration'?{GraphEditor:'GraphEditor',GraphView:'GraphView',defaultGraph:{mode:'development',nodes:[],edges:[]}}:id==='./ExecutionCenter'?load('ExecutionCenter'):assert.fail('unknown module '+id);
  vm.runInNewContext(code,{exports,require:requireMock,console,Date,URL,encodeURIComponent,Promise});return exports;
 }
 const component=load(name==='ExecutionCenter'?'ExecutionCenter':'Forms')[name];
 const render=()=>{cursor=0;tree=expand(component({admin:true,projects:[project],agents:[agent],models:[],providers:[],users:[],runs:[{id:'r',project_id:'p',title:'运行 R'}],onDone(){},onNavigate:(...args)=>navigation.push(args),...currentProps}));effects.splice(0).forEach(fn=>fn());return tree;};
 return{calls,navigation,render,setProps(next){currentProps={...currentProps,...next};return render();},async settle(){await new Promise(resolve=>setImmediate(resolve));return render();}};
}

test('new projects default to local/ask and do not enable full access',async()=>{
 const h=harness('ProjectForm');const tree=h.render();assert.equal(field(tree,'项目执行位置').props.value,'local');assert.equal(field(tree,'项目权限策略').props.value,'ask');assert.match(text(tree),/工作副本/);assert.ok(!nodes(tree).some(n=>n.props?.['aria-label']==='执行镜像'));await tree.props.onSubmit();const body=h.calls[0].body;assert.equal(body.execution_mode,'local');assert.equal(body.permission_profile,'ask');assert.equal(body.full_access_confirmed,false);
});

test('new imported projects allow configured external models by default and can opt out before saving',async()=>{
 for(const allow of [true,false]){
  const h=harness('ProjectForm');let tree=h.render();
  assert.equal(field(tree,'允许外部模型访问').props.checked,true);
  assert.equal(h.calls.length,0,'rendering a default does not save or call any model');
  if(!allow){field(tree,'允许外部模型访问').props.onChange({target:{checked:false}});tree=h.render();}
  await tree.props.onSubmit();
  assert.equal(h.calls[0].method,'POST');assert.equal(h.calls[0].body.allow_external_models,allow);
  assert.equal(h.calls[0].body.permission_profile,'ask');
 }
});

test('editing existing projects preserves explicit and legacy external-model authorization',async()=>{
 for(const value of [true,false,undefined]){
  const initial={...project};if(value===undefined)delete initial.allow_external_models;else initial.allow_external_models=value;
  const before=JSON.stringify(initial),h=harness('ProjectForm',{props:{initial}});const tree=h.render();
  assert.equal(field(tree,'允许外部模型访问').props.checked,value===true);
  labelledInput(tree,'项目名称').props.onChange({target:{value:'仅修改名称'}});
  await h.render().props.onSubmit();
  assert.equal(h.calls[0].method,'PUT');assert.equal(h.calls[0].body.allow_external_models,value===true);
  assert.equal(JSON.stringify(initial),before,'the original project object stays unchanged');
 }
});

test('external-model checks are grouped once without hiding unrelated model failures or changing raw results',async()=>{
 const result={...readiness('p'),ready:false,checks:[
  ...['a','b','c'].map(id=>({code:'project.external_models',status:'error',message:'重复项目限制',target:'projects',agent_id:id,model_profile_id:'m',provider_id:'provider'})),
  {code:'model.enabled',status:'error',message:'Ada 的模型已停用',target:'models',agent_id:'a',model_profile_id:'m'},
 ]};
 const before=JSON.stringify(result),h=harness('ExecutionCenter',{handler:url=>url.startsWith('/execution/readiness')?result:undefined});
 h.render();const tree=await h.settle(),checks=nodes(tree).find(n=>n.props?.className==='execution-checks');
 const problems=nodes(checks).filter(n=>n.type==='li');assert.equal(problems.length,2);
 assert.equal((text(checks).match(/当前项目未开启外部模型访问/g)||[]).length,1);
 assert.match(text(problems[1]),/Ada 的模型已停用/);assert.doesNotMatch(text(problems[0]),/成员：/);
 button(problems[0],'前往查看').props.onClick();assert.equal(h.navigation[0][0],'projects');
 assert.equal(h.navigation[0][1].project_id,'p');assert.equal(h.navigation[0][1].agent_id,undefined);
 assert.equal(JSON.stringify(result),before);
});

test('desktop project requires native directory selection and sends only its one-time ticket',async()=>{
 const desktop={selectProjectDirectory:async()=>({ticket:'one-time-ticket',path:'/Users/example/Project',name:'Project'})};
 const h=harness('ProjectForm',{desktop});let tree=h.render();
 await assert.rejects(tree.props.onSubmit(),/原生文件夹/);assert.equal(h.calls.length,0);
 await button(tree,'选择项目文件夹…').props.onClick();tree=h.render();
 assert.equal(field(tree,'已授权项目目录').props.children,'/Users/example/Project');
 await tree.props.onSubmit();assert.equal(h.calls[0].body.desktop_directory_token,'one-time-ticket');
 assert.equal(h.calls[0].body.import_path,'Project');assert.equal(h.calls[0].body.permission_profile,'ask');
 assert.equal(h.calls[0].body.name,'Project');
});

test('cancelled desktop picker grants no authority; editing a saved project needs no new ticket',async()=>{
 const desktop={selectProjectDirectory:async()=>null};const h=harness('ProjectForm',{desktop});let tree=h.render();
 await button(tree,'选择项目文件夹…').props.onClick();await assert.rejects(h.render().props.onSubmit(),/原生文件夹/);
 const edit=harness('ProjectForm',{desktop,props:{initial:{...project,desktop_source_path:'/selected/project'}}});tree=edit.render();
 assert.match(text(tree),/原生授权来源/);await tree.props.onSubmit();assert.equal(edit.calls[0].method,'PUT');
 assert.equal(edit.calls[0].body.desktop_directory_token,undefined);
});

test('legacy projects keep container/ask and explicitly retain frozen run configuration',()=>{
 const initial={...project};delete initial.execution_mode;delete initial.permission_profile;const h=harness('ProjectForm',{props:{initial}});const tree=h.render();assert.equal(field(tree,'项目执行位置').props.value,'container');assert.equal(field(tree,'项目权限策略').props.value,'ask');assert.match(text(tree),/历史快照/);assert.ok(field(tree,'执行镜像'));assert.equal(h.calls.length,0);
});

test('full access requires fresh explicit confirmation on every edit and resets after switching profiles',async()=>{
 const h=harness('ProjectForm',{props:{initial:{...project,permission_profile:'full'}}});let tree=h.render();assert.equal(field(tree,'确认完全访问风险').props.checked,false);await assert.rejects(tree.props.onSubmit(),/风险确认/);assert.equal(h.calls.length,0);field(tree,'确认完全访问风险').props.onChange({target:{checked:true}});tree=h.render();await tree.props.onSubmit();assert.equal(h.calls[0].body.full_access_confirmed,true);field(tree,'项目权限策略').props.onChange({target:{value:'ask'}});tree=h.render();field(tree,'项目权限策略').props.onChange({target:{value:'full'}});tree=h.render();assert.equal(field(tree,'确认完全访问风险').props.checked,false);
});

test('container selection cannot preserve or silently activate full access',()=>{
 const h=harness('ProjectForm',{props:{initial:{...project,permission_profile:'full'}}});let tree=h.render();field(tree,'项目执行位置').props.onChange({target:{value:'container'}});tree=h.render();assert.equal(field(tree,'项目权限策略').props.value,'ask');assert.equal(nodes(field(tree,'项目权限策略')).find(n=>n.type==='option'&&n.props.value==='full').props.disabled,true);assert.equal(h.calls.length,0);
});

test('business job title is independent of the execution permission role and saved explicitly',async()=>{
 const h=harness('AgentForm',{props:{initial:agent,models:[{id:'m',name:'已绑定模型',provider_id:'provider',allowed_parameters:[],enabled:true,supports_tools:true}],providers:[{id:'provider',name:'已选供应商',enabled:true}]}});let tree=h.render();assert.equal(field(tree,'业务岗位').props.value,'财务分析师');assert.equal(field(tree,'执行权限档').props.value,'developer');field(tree,'业务岗位').props.onChange({target:{value:'预算分析顾问'}});tree=h.render();await tree.props.onSubmit();assert.equal(h.calls[0].body.job_title,'预算分析顾问');assert.equal(h.calls[0].body.role,'developer');
});

test('RunForm preflight posts the current draft but never creates a run or enables permissions',async()=>{
 const h=harness('RunForm',{props:{initial:{agent_ids:['a']}}});h.render();let tree=await h.settle();await button(tree,'执行前检查（不调用模型）').props.onClick();tree=await h.settle();const writes=h.calls.filter(c=>c.method==='POST');assert.equal(writes.length,1);assert.equal(writes[0].url,'/execution/preflight');assert.equal(writes[0].body.project_id,'p');assert.deepEqual([...writes[0].body.agent_ids],['a']);assert.ok(!('permission_profile'in writes[0].body));assert.match(text(tree),/尚未验证模型推理/);assert.match(text(tree),/不创建运行/);
});

test('late RunForm preflight is discarded after the project changes',async()=>{
 let resolve;const h=harness('RunForm',{props:{initial:{agent_ids:['a']},projects:[project,{...project,id:'q',name:'项目 Q'}]},handler:(url)=>url==='/execution/preflight'?new Promise(done=>resolve=done):undefined});h.render();let tree=await h.settle();button(tree,'执行前检查（不调用模型）').props.onClick();labelledInput(tree,'项目').props.onChange({target:{value:'q'}});h.render();resolve(readiness('p'));tree=await h.settle();assert.doesNotMatch(text(tree),/\/local\/tasks\/p/);assert.doesNotMatch(text(tree),/配置检查通过/);
});

test('changing runtime invalidates a completed preflight rather than claiming it is current',async()=>{
 const h=harness('RunForm',{props:{initial:{agent_ids:['a']}}});h.render();let tree=await h.settle();await button(tree,'执行前检查（不调用模型）').props.onClick();tree=await h.settle();assert.match(text(tree),/配置检查通过/);nodes(tree).find(n=>n.props?.label==='本次运行参数覆盖').props.setValue('{"max_steps":8}');tree=h.render();assert.doesNotMatch(text(tree),/配置检查通过/);
});

test('execution center distinguishes fixed environment probes from task execution and never auto-approves',async()=>{
 const h=harness();h.render();const tree=await h.settle();assert.ok(h.calls.every(c=>c.method==='GET'));assert.match(text(tree),/\/local\/tasks\/p/);assert.match(text(tree),/fixed source/);assert.match(text(tree),/python3 \/reviewed\/tool.py/);assert.match(text(tree),/不调用推理模型/);assert.match(text(tree),/不创建运行/);assert.match(text(tree),/不执行项目命令，仅运行固定环境探针/);assert.match(text(tree),/本机进程不是容器隔离/);
});

test('permission approval requires review plus checkbox and sends exactly the inspected digest',async()=>{
 const h=harness();h.render();let tree=await h.settle();button(tree,'审查后批准').props.onClick();tree=h.render();assert.equal(button(tree,'确认批准本次申请').props.disabled,true);await button(tree,'确认批准本次申请').props.onClick();assert.ok(h.calls.every(c=>c.method==='GET'));field(tree,'确认已复核精确命令与权限').props.onChange({target:{checked:true}});tree=h.render();await button(tree,'确认批准本次申请').props.onClick();tree=await h.settle();const writes=h.calls.filter(c=>c.method==='POST');assert.equal(writes.length,1);assert.equal(writes[0].url,'/execution/permissions/request/review');assert.equal(writes[0].body.digest,permission.digest);assert.equal(writes[0].body.approved,true);assert.match(text(tree),/批准不等于执行完成/);assert.ok(h.calls.every(c=>!c.url.includes('run_approved')&&!c.url.includes('/runs')));
});

test('incomplete installation commands cannot be approved, and full source is shown for complete requests',async()=>{
 const h=harness('ExecutionCenter',{handler:url=>url.startsWith('/execution/permissions')?{items:[]}:url.startsWith('/tool-installations')?{items:[{...installation,manifest:{...manifest,install_command:''}}]}:undefined});h.render();const tree=await h.settle();assert.equal(button(tree,'审查后批准').props.disabled,true);assert.match(text(tree),/尚未返回完整安装命令/);assert.match(text(tree),/不能批准/);assert.ok(h.calls.every(c=>c.method==='GET'));
});

test('complete installation review preserves the manifest digest and never invokes installation',async()=>{
 const h=harness('ExecutionCenter',{handler:url=>url.startsWith('/execution/permissions')?{items:[]}:undefined});h.render();let tree=await h.settle();button(tree,'审查后批准').props.onClick();tree=h.render();assert.match(text(field(tree,'确认申请审核')),/fixed source/);field(tree,'确认已复核精确命令与权限').props.onChange({target:{checked:true}});tree=h.render();await button(tree,'确认批准本次申请').props.onClick();const writes=h.calls.filter(c=>c.method==='POST');assert.equal(writes.length,1);assert.equal(writes[0].url,'/tool-installations/install/review');assert.equal(writes[0].body.digest,installation.digest);
});

test('viewers inspect only an accessible run and have no approval controls',async()=>{
 const h=harness('ExecutionCenter',{props:{admin:false}});h.render();const tree=await h.settle();assert.ok(h.calls.filter(c=>c.url.includes('/permissions')||c.url.includes('/installations')).every(c=>c.url.includes('run_id=r')));assert.ok(!nodes(tree).some(n=>n.type==='button'&&text(n)==='审查后批准'));assert.ok(h.calls.every(c=>c.method==='GET'));
});

test('switching project closes pending review and cannot use the previous confirmation',async()=>{
 const h=harness('ExecutionCenter',{props:{projects:[project,{...project,id:'q',name:'Q'}]}});h.render();let tree=await h.settle();button(tree,'审查后批准').props.onClick();tree=h.render();field(tree,'确认已复核精确命令与权限').props.onChange({target:{checked:true}});field(tree,'执行中心项目').props.onChange({target:{value:'q'}});h.render();tree=await h.settle();assert.ok(!nodes(tree).some(n=>n.props?.['aria-label']==='确认申请审核'));assert.ok(h.calls.every(c=>c.method==='GET'));
});

test('late readiness from a previous project does not replace the current workspace',async()=>{
 const pending=[];const h=harness('ExecutionCenter',{props:{projects:[project,{...project,id:'q',name:'Q'}]},handler:url=>url.startsWith('/execution/readiness')?new Promise(resolve=>pending.push({url,resolve})):undefined});let tree=h.render();field(tree,'执行中心项目').props.onChange({target:{value:'q'}});h.render();pending[1].resolve(readiness('q'));tree=await h.settle();pending[0].resolve(readiness('p'));tree=await h.settle();assert.match(text(tree),/\/local\/tasks\/q/);assert.doesNotMatch(text(tree),/\/local\/tasks\/p/);
});

test('preflight fix navigation carries the selected project and exact check context',async()=>{
 const h=harness();h.render();const tree=await h.settle();button(tree,'前往查看').props.onClick();assert.equal(h.navigation[0][0],'providers');assert.equal(h.navigation[0][1].project_id,'p');assert.equal(h.navigation[0][1].code,'model.inference_not_executed');
});

test('offline examples are not presented as a fixed ceiling on agent tools',async()=>{
 const h=harness();h.render();const tree=await h.settle();assert.match(text(tree),/内置离线工具示例（非能力上限）/);assert.match(text(tree),/其他本机 CLI \/ 项目依赖/);assert.match(text(tree),/申请精确命令安装/);assert.match(text(tree),/不代表已支持任意插件或 MCP 服务/);assert.ok(h.calls.every(c=>c.method==='GET'));
});

test('temporary network or path grants disclose no-child-process constraints in the request and confirmation',async()=>{
 const h=harness();h.render();let tree=await h.settle();assert.match(text(tree),/临时联网或越界批准的命令禁止派生子进程/);button(tree,'审查后批准').props.onClick();tree=h.render();assert.match(text(field(tree,'确认申请审核')),/需要子进程的操作会失败关闭，绝不自动切换完全访问/);
});

test('ordinary requests without extra network or paths do not imply that all local commands forbid children',async()=>{
 const h=harness('ExecutionCenter',{handler:url=>url.startsWith('/execution/permissions')?{items:[{...permission,network:false,paths:[]}]}:undefined});h.render();const tree=await h.settle();assert.doesNotMatch(text(tree),/禁止派生子进程/);
});

test('updating an Agent configuration invalidates a prior run preflight with the same selection',async()=>{
 const h=harness('RunForm',{props:{initial:{agent_ids:['a']}}});h.render();let tree=await h.settle();await button(tree,'执行前检查（不调用模型）').props.onClick();tree=await h.settle();assert.match(text(tree),/配置检查通过/);tree=h.setProps({agents:[{...agent,model_profile_id:'changed-model',version:2}]});assert.doesNotMatch(text(tree),/配置检查通过/);
});

test('dynamic inventory renders only actual discovered paths and missing tools without installation',async()=>{
 const h=harness('ExecutionCenter',{handler:url=>url.startsWith('/execution/tools')?{items:[{event_id:44,task_key:'design',execution_mode:'local',cwd:'/task/work',executables:[{name:'custom-doc-cli',available:true,resolved_path:'/task/work/.forge-tools/bin/custom-doc-cli'},{name:'not-installed-cli',available:false}],project_manifests:['package.json'],note:'没有运行候选工具'}]}:undefined});h.render();const tree=await h.settle();const section=field(tree,'动态工具发现');assert.match(text(section),/custom-doc-cli · 已找到/);assert.match(text(section),/not-installed-cli · 未找到/);assert.match(text(section),/\.forge-tools\/bin\/custom-doc-cli/);assert.match(text(section),/package.json/);assert.match(text(section),/没有运行候选工具/);assert.ok(h.calls.every(c=>c.method==='GET'));
});

test('permission review displays the exact server constraint notice in both stages',async()=>{
 const message='本次摘要绑定：只允许精确文件，不允许子进程。';const h=harness('ExecutionCenter',{handler:url=>url.startsWith('/execution/permissions')?{items:[{...permission,constraints:{message}}]}:undefined});h.render();let tree=await h.settle();assert.ok(text(tree).includes(message));button(tree,'审查后批准').props.onClick();tree=h.render();assert.ok(text(field(tree,'确认申请审核')).includes(message));
});
