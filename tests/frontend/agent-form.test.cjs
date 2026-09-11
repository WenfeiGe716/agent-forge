const assert=require('node:assert/strict');
const test=require('node:test');
const fs=require('node:fs');
const vm=require('node:vm');
const ts=require('../../frontend/vendor/typescript.cjs');
const source=fs.readFileSync('frontend/src/components/Forms.tsx','utf8');
const file=ts.createSourceFile('Forms.tsx',source,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
const declaration=file.statements.find(n=>ts.isFunctionDeclaration(n)&&n.name?.text==='AgentForm');
const compiled=ts.transpileModule(declaration.getText(file),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText;
const providers=[{id:'provider-a',name:'原供应商',enabled:true},{id:'provider-b',name:'新供应商',enabled:true}];
const models=[{id:'model-a',name:'原模型',provider_id:'provider-a',enabled:true,supports_tools:true,allowed_parameters:['max_tokens']},{id:'model-b',name:'新模型',provider_id:'provider-b',enabled:true,supports_tools:true,allowed_parameters:['max_output_tokens']}];
const initial={id:'agent',name:'真实成员',role:'developer',model_profile_id:'model-a',fallback_model_ids:[],instructions:'原职责',runtime:{},model_parameters:{},version:1};

test('removing one explicit fallback is accessible, changes only the draft, and preserves the primary and other bindings',async()=>{
 const h=harness({initial:{...initial,fallback_model_ids:['model-b','missing-model']}});
 const before=JSON.stringify(h.props.initial),tree=h.render();
 field(tree,'移除备用模型 model-b').props.onClick();
 assert.equal(h.calls.length,0);assert.equal(JSON.stringify(h.props.initial),before);
 let next=h.render();assert.equal(field(next,'独立模型绑定').props.value,'model-a');
 assert.ok(field(next,'移除备用模型 missing-model'));
 field(next,'移除备用模型 missing-model').props.onClick();next=h.render();await next.props.onSubmit();
 assert.equal(h.calls.length,1);assert.equal(h.calls[0].url,'/agents/agent');
 assert.equal(h.calls[0].body.model_profile_id,'model-a');assert.deepEqual(Array.from(h.calls[0].body.fallback_model_ids),[]);
 assert.equal(h.calls[0].body.instructions,initial.instructions);
});
function nodes(v){if(Array.isArray(v))return v.flatMap(nodes);return v&&typeof v==='object'?[v,...nodes(v.props?.children)]:[];}
function field(tree,label){const n=nodes(tree).find(n=>n.props?.['aria-label']===label);assert.ok(n,'field '+label);return n;}
function text(v){if(Array.isArray(v))return v.map(text).join('');return v&&typeof v==='object'?text(v.props?.children):v==null?'':String(v);}
function button(tree,label){const n=nodes(tree).find(n=>n.type==='button'&&text(n)===label);assert.ok(n,'button '+label);return n;}
function harness(extra={},handler){
 let cursor=0;const slots=[],effects=[],calls=[],exports={},props={initial:structuredClone(initial),providers,models,onDone(){},...extra};
 const jsx=(type,props)=>({type,props:props||{}});
 const react={useState(value){const i=cursor++;if(!(i in slots))slots[i]=typeof value==='function'?value():value;return[slots[i],v=>{slots[i]=typeof v==='function'?v(slots[i]):v;}];},useRef(value){const i=cursor++;if(!(i in slots))slots[i]={current:value};return slots[i];},useEffect(fn,deps){const i=cursor++;if(!(i in slots)||deps.some((v,j)=>v!==slots[i][j])){slots[i]=deps;effects.push(fn);}}};
 vm.runInNewContext(compiled,{exports,require:()=>({jsx,jsxs:jsx}),...react,EditorForm:'EditorForm',Field:'Field',JsonField:'JsonField',SkillBindings:'SkillBindings',RoleTemplatePicker:'RoleTemplatePicker',RoleTemplateSource:'RoleTemplateSource',roles:{developer:'开发',planner:'规划',tester:'测试',reviewer:'审查'},jsonText:v=>JSON.stringify(v||{}),parseObject:JSON.parse,api:async(url,method,body)=>{calls.push({url,method,body});return handler?handler(url,method,body):{id:'agent',version:2};}});
 return{calls,props,render(){cursor=0;const tree=exports.AgentForm(props);effects.splice(0).forEach(fn=>fn());return tree;}};
}

test('actual AgentForm saves the explicitly selected provider-owned model configuration',async()=>{
 const h=harness();let tree=h.render();field(tree,'模型供应商').props.onChange({target:{value:'provider-b'}});tree=h.render();
 assert.equal(field(tree,'独立模型绑定').props.value,'');
 assert.deepEqual(nodes(field(tree,'独立模型绑定')).filter(n=>n.type==='option').map(n=>n.props.value),['','model-b']);
 field(tree,'独立模型绑定').props.onChange({target:{value:'model-b'}});tree=h.render();await tree.props.onSubmit();
 assert.equal(h.calls.length,1);assert.equal(h.calls[0].url,'/agents/agent');assert.equal(h.calls[0].body.model_profile_id,'model-b');
 const reopened=harness({initial:{...initial,...h.calls[0].body}}).render();
 assert.equal(field(reopened,'模型供应商').props.value,'provider-b');assert.equal(field(reopened,'独立模型绑定').props.value,'model-b');
});

test('changing only the provider cannot silently save as an unbound Agent',async()=>{
 const h=harness();field(h.render(),'模型供应商').props.onChange({target:{value:'provider-b'}});
 await assert.rejects(h.render().props.onSubmit(),/选择.*模型|绑定.*模型/);
 assert.equal(h.calls.length,0,'incomplete supplier selection must not clear a persisted model binding');
});

test('same-provider reselection preserves binding; unbinding remains an explicit supported choice',async()=>{
 const h=harness();field(h.render(),'模型供应商').props.onChange({target:{value:'provider-a'}});
 assert.equal(field(h.render(),'独立模型绑定').props.value,'model-a');
 field(h.render(),'模型供应商').props.onChange({target:{value:''}});await h.render().props.onSubmit();
 assert.equal(h.calls[0].body.model_profile_id,null);
});

test('switching models retains old parameters for review and rejects incompatible keys before save',async()=>{
 const h=harness({initial:{...initial,model_parameters:{max_tokens:500}}});
 field(h.render(),'模型供应商').props.onChange({target:{value:'provider-b'}});
 field(h.render(),'独立模型绑定').props.onChange({target:{value:'model-b'}});
 let tree=h.render();const parameters=nodes(tree).find(n=>n.type==='JsonField'&&n.props.label==='模型参数覆盖');
 assert.equal(JSON.parse(parameters.props.value).max_tokens,500);
 await assert.rejects(tree.props.onSubmit(),/新供应商.*max_tokens/);assert.equal(h.calls.length,0);
 button(tree,'重置此成员模型覆盖参数为继承').props.onClick();await h.render().props.onSubmit();
 assert.equal(h.calls[0].body.model_profile_id,'model-b');assert.equal(Object.keys(h.calls[0].body.model_parameters).length,0);
});

test('departments are deduplicated organization choices with unassigned and preserved legacy assignment',async()=>{
 const organization={ref:null,config:{floors:[{floor:1,department:'研发部'},{floor:2,department:'品牌部'},{floor:3,department:'研发部'}]}};
 const h=harness({initial:{...initial,department:'历史部门'},organization});
 const select=field(h.render(),'成员部门');assert.equal(select.type,'select');
 assert.deepEqual(nodes(select).filter(n=>n.type==='option').map(n=>n.props.value),['','研发部','品牌部','历史部门']);
 select.props.onChange({target:{value:'品牌部'}});await h.render().props.onSubmit();
 assert.equal(h.calls[0].body.department,'品牌部');assert.ok(!('office_position' in h.calls[0].body));
 assert.equal(organization.config.floors[0].department,'研发部');
});

test('explicit template choice copies editable instructions and provenance, not models, permissions or shared source',async()=>{
 const template={name:'市场研究员',role:'reviewer',instructions:'共享模板原始职责',model_profile_id:'unwanted',skills:[{skill_id:'unwanted'}],role_template_ref:{item_id:'a'.repeat(32),revision_id:'b'.repeat(64),digest:'c'.repeat(64)}};
 const original=JSON.stringify(template);const h=harness({initial:{...initial,department:'研发部'}});
 button(h.render(),'从角色模板库选择').props.onClick();const picker=nodes(h.render()).find(n=>n.type==='RoleTemplatePicker');assert.ok(picker);
 picker.props.onMember(template);let tree=h.render();assert.equal(field(tree,'成员角色指令').props.value,template.instructions);
 field(tree,'成员角色指令').props.onChange({target:{value:'此成员独立职责'}});await h.render().props.onSubmit();
 assert.equal(h.calls.length,1);assert.equal(h.calls[0].url,'/agents/agent');
 assert.equal(h.calls[0].body.instructions,'此成员独立职责');assert.equal(h.calls[0].body.role,'developer');
 assert.equal(h.calls[0].body.department,'研发部');assert.equal(h.calls[0].body.model_profile_id,'model-a');assert.equal(h.calls[0].body.skills.length,0);
 assert.deepEqual(h.calls[0].body.role_template_ref,template.role_template_ref);assert.equal(JSON.stringify(template),original);
 assert.equal(field(h.render(),'业务岗位').props.value,'市场研究员');
});

test('new Agent can copy a role without saving until explicit submit; cancelling selection leaves instructions alone',async()=>{
 const h=harness({initial:undefined});button(h.render(),'从角色模板库选择').props.onClick();
 let picker=nodes(h.render()).find(n=>n.type==='RoleTemplatePicker');picker.props.onClose();
 assert.equal(field(h.render(),'成员角色指令').props.value,'');assert.equal(h.calls.length,0);
 button(h.render(),'从角色模板库选择').props.onClick();picker=nodes(h.render()).find(n=>n.type==='RoleTemplatePicker');
 picker.props.onMember({name:'客服专员',instructions:'服务职责',role_template_ref:{item_id:'a'.repeat(32),revision_id:'b'.repeat(64),digest:'c'.repeat(64)}});
 assert.equal(h.calls.length,0);await h.render().props.onSubmit();
 assert.equal(h.calls[0].method,'POST');assert.equal(h.calls[0].body.name,'客服专员');assert.equal(h.calls[0].body.model_profile_id,null);
});

test('AgentForm accepts the role proposal envelope and applies it to the local draft',async()=>{
 const h=harness({initial:undefined});button(h.render(),'从角色模板库选择').props.onClick();
 const picker=nodes(h.render()).find(n=>n.type==='RoleTemplatePicker');assert.ok(picker);
 const member={name:'研究员',job_title:'市场研究',role:'reviewer',instructions:'独立职责副本',role_template_ref:{item_id:'a'.repeat(32),revision_id:'b'.repeat(64),digest:'c'.repeat(64)}};
 picker.props.onMember({member,provenance:{document_url:'/api/role-templates/items/x'}});
 const draft=h.render();
 assert.equal(field(draft,'成员角色指令').props.value,member.instructions);
 assert.match(text(draft),/已将角色模板职责复制到当前 Agent 草稿/);
 assert.deepEqual(h.calls,[],'importing a proposal only updates the local form draft');
 await draft.props.onSubmit();
 assert.equal(h.calls[0].method,'POST');
 assert.deepEqual(h.calls[0].body.role_template_ref,member.role_template_ref);
});

test('personal Skill bindings are saved with their exact version and parameters only on Agent endpoint',async()=>{
 const h=harness();const tree=h.render();const bindings=nodes(tree).find(n=>n.type==='SkillBindings');
 const choice=[{skill_id:'skill',version:3,mode:'always',enabled:true,allow_scripts:false,parameters:{focus:'security'}}];
 bindings.props.onChange(choice);await h.render().props.onSubmit();
 assert.equal(h.calls.length,1);assert.equal(h.calls[0].url,'/agents/agent');assert.deepEqual(h.calls[0].body.skills,choice);
 const reloaded=harness({initial:{...initial,...h.calls[0].body}}).render();
 assert.deepEqual(nodes(reloaded).find(n=>n.type==='SkillBindings').props.value,choice);
});
