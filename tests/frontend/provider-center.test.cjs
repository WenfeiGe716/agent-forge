const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('../../frontend/vendor/typescript.cjs');

const providers=[{id:'a',revision_id:'rev-a',name:'供应商 A',protocol:'openai_chat',base_url:'https://a.example/v1',enabled:true},
  {id:'b',revision_id:'rev-b',name:'供应商 B',protocol:'openai_responses',base_url:'https://b.example/v1',enabled:true}];
const cached=(id='a')=>({provider_id:id,revision_id:'rev-'+id,catalog_digest:(id==='a'?'c':'d').repeat(64),entries:[{id:id+'-one',label:id+'-one',available:true,profiles:[]}],
  models:[{id:id+'-one'}],retired_profile_ids:[],last_synced_at:'2026-09-10T00:00:00Z',last_attempt_at:'2026-09-10T00:00:00Z',stale:false,counts:{available:1}});
function nodes(value){if(Array.isArray(value))return value.flatMap(nodes);if(!value||typeof value!=='object')return [];return [value,...nodes(value.props?.children)];}
function text(value){if(Array.isArray(value))return value.map(text).join('');if(value&&typeof value==='object')return text(value.props?.children);return value==null?'':String(value);}
function button(tree,label){const found=nodes(tree).find(n=>n.type==='button'&&text(n)===label);assert.ok(found,`button ${label}`);return found;}
function field(tree,label){const found=nodes(tree).find(n=>n.props?.['aria-label']===label);assert.ok(found,`field ${label}`);return found;}
function harness({admin=true,handler,models=[],agents=[],providerRows=providers}={}){
  let live={admin,providers:providerRows,models,agents};
  let cursor=0,tree;const slots=[],effects=[],cleanup=[],calls=[],refresh=[];
  const React={useState(initial){const i=cursor++;if(!(i in slots))slots[i]=typeof initial==='function'?initial():initial;return [slots[i],value=>{slots[i]=typeof value==='function'?value(slots[i]):value;}];},
    useRef(initial){const i=cursor++;if(!(i in slots))slots[i]={current:initial};return slots[i];},
    useEffect(fn,deps){const i=cursor++;if(!(i in slots)||deps.some((v,j)=>!Object.is(v,slots[i][j]))){cleanup[i]?.();slots[i]=deps;effects.push(()=>{cleanup[i]=fn();});}}};
  const jsx=(type,props)=>({type,props:props||{}}),exports={};
  const api=async(url,method='GET',body)=>{calls.push({url,method,body});if(handler){const result=handler(url,method,body);if(result!==undefined)return await result;}return cached(url.includes('/b/')?'b':'a');};
  const source=fs.readFileSync(require.resolve('../../frontend/src/components/ProviderCenter.tsx'),'utf8').replace(/import\s+['"][^'"]+\.css['"];?/g,'');
  const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText;
  const requireMock=name=>name==='react'?React:name==='react/jsx-runtime'?{jsx,jsxs:jsx,Fragment:'Fragment'}:name==='../api'?{api}:name==='./UI'?{Empty:'Empty'}:assert.fail('Unexpected module '+name);
  vm.runInNewContext(code,{exports,require:requireMock,console,Date});
  const render=()=>{cursor=0;tree=exports.ProviderCenter({...live,onRefresh(){refresh.push(true);},onEditProvider(){},onEditModel(){},onConfigureModel(){},onAgents(){}});effects.splice(0).forEach(fn=>fn());return tree;};
  return {calls,refresh,render,update(props){live={...live,...props};return render();},async settle(){await new Promise(r=>setImmediate(r));return render();}};
}

test('explicit upstream pull always bypasses a fresh cache without creating model configs',async()=>{
  const h=harness();h.render();let tree=await h.settle();assert.deepEqual(h.calls.map(c=>c.method),['GET']);
  button(tree,'从上游拉取更新').props.onClick();tree=await h.settle();
  button(tree,'从上游拉取更新').props.onClick();await h.settle();
  assert.deepEqual(h.calls.filter(c=>c.method==='POST').map(c=>c.url),['/providers/a/catalog/sync','/providers/a/catalog/sync']);
  assert.ok(h.calls.every(c=>c.url.includes('/catalog')));
});

test('failed upstream pull retains the same-provider cache and makes failure visible',async()=>{
  const h=harness({handler:(_url,method)=>{if(method==='POST')throw Error('上游暂时不可用');}});h.render();let tree=await h.settle();
  button(tree,'从上游拉取更新').props.onClick();tree=await h.settle();
  assert.match(text(tree),/上游暂时不可用/);assert.match(text(tree),/a-one/);assert.match(text(tree),/最后成功/);
});

test('a late pull from another provider cannot replace the selected provider catalog',async()=>{
  let resolve;const h=harness({handler:(_url,method)=>method==='POST'?new Promise(done=>{resolve=done;}):undefined});h.render();let tree=await h.settle();
  button(tree,'从上游拉取更新').props.onClick();tree=h.render();
  nodes(tree).find(n=>n.type==='button'&&n.props['aria-pressed']===false).props.onClick();h.render();tree=await h.settle();
  resolve(cached('a'));tree=await h.settle();assert.match(text(tree),/b-one/);assert.doesNotMatch(text(tree),/a-one/);
});

test('a viewer only reads the cached directory and cannot pull or import',async()=>{
  const h=harness({admin:false});h.render();const tree=await h.settle();
  assert.ok(!nodes(tree).some(n=>n.type==='button'&&text(n)==='从上游拉取更新'));
  assert.ok(!nodes(tree).some(n=>n.type==='input'&&n.props.type==='checkbox'));
  assert.ok(h.calls.every(c=>c.method==='GET'));
});

test('pull results display upstream count and all five kinds of changes',async()=>{
  const pull={status:'success',request_url:'https://a.example/v1/models',completed_at:'2026-09-10T00:00:00Z',received_count:12,
    changes:{added:['new-model'],updated:['changed-model'],removed:['old-model'],restored:['restored-model'],unchanged:['a-one']},
    counts:{added:1,updated:1,removed:1,restored:1,unchanged:1}};
  const h=harness({handler:(_url,method)=>method==='POST'?{...cached(),last_pull:pull}:undefined});h.render();let tree=await h.settle();
  button(tree,'从上游拉取更新').props.onClick();tree=await h.settle();
  const result=field(tree,'最近成功拉取结果');assert.match(text(result),/上游返回 12 个型号/);
  assert.match(text(result),/新增 1/);assert.match(text(result),/信息更新 1/);assert.match(text(result),/上游移出 1/);
  assert.match(text(result),/重新出现 1/);assert.match(text(result),/未变化 1/);
  assert.match(text(result),/https:\/\/a.example\/v1\/models/);assert.match(text(result),/new-model/);
});

test('selected upstream candidates require human review and explicit acknowledgement before disabled import',async()=>{
  const created={id:'new-profile',version:1,name:'a-one',provider_id:'a',model_id:'a-one',enabled:false,supports_tools:false,parameters:{},allowed_parameters:[]};
  const h=harness({handler:url=>url.endsWith('/import')?{created:[created],skipped:[],catalog:cached()}:undefined});h.render();let tree=await h.settle();
  assert.equal(button(tree,'导入所选为停用配置').props.disabled,true);
  field(tree,'选择型号 a-one').props.onChange({target:{checked:true}});tree=h.render();
  assert.equal(h.calls.filter(c=>c.method==='POST').length,0);
  button(tree,'导入所选为停用配置').props.onClick();tree=h.render();
  assert.equal(button(tree,'确认导入停用配置').props.disabled,true);assert.match(text(field(tree,'确认导入模型配置')),/不会自动启用或绑定 Agent/);
  field(tree,'确认仅导入停用配置').props.onChange({target:{checked:true}});tree=h.render();
  assert.equal(button(tree,'确认导入停用配置').props.disabled,false);button(tree,'确认导入停用配置').props.onClick();tree=await h.settle();
  const writes=h.calls.filter(c=>c.method==='POST');assert.equal(writes.length,1);assert.equal(writes[0].url,'/providers/a/catalog/import');
  assert.deepEqual(JSON.parse(JSON.stringify(writes[0].body)),{revision_id:'rev-a',catalog_digest:'c'.repeat(64),model_ids:['a-one'],acknowledged:true});
  assert.match(text(tree),/已导入 1 个停用模型配置/);assert.equal(field(tree,'选择型号 a-one').props.disabled,true);assert.equal(h.refresh.length,1);
});

test('bulk selection excludes configured and removed models, and does not exceed the current filter',async()=>{
  const model={id:'profile',name:'Saved',provider_id:'a',model_id:'configured',enabled:true};
  const directory={...cached(),entries:[...cached().entries,{id:'a-two',available:true},{id:'configured',available:true},{id:'removed',available:false}]};
  const h=harness({models:[model],handler:()=>directory});h.render();let tree=await h.settle();
  assert.equal(field(tree,'选择型号 configured').props.disabled,true);assert.ok(!nodes(tree).some(n=>n.props?.['aria-label']==='选择型号 removed'));
  field(tree,'模型目录筛选').props.onChange({target:{value:'missing'}});tree=h.render();assert.equal(field(tree,'选择型号 removed').props.disabled,true);
  field(tree,'模型目录筛选').props.onChange({target:{value:'all'}});tree=h.render();
  field(tree,'搜索此供应商模型').props.onChange({target:{value:'a-one'}});tree=h.render();
  field(tree,'选择当前筛选下未配置型号').props.onChange({target:{checked:true}});tree=h.render();
  button(tree,'导入所选为停用配置').props.onClick();tree=h.render();
  const review=text(field(tree,'确认导入模型配置'));assert.match(review,/a-one/);assert.doesNotMatch(review,/a-two|configured|removed/);
});

test('provider switch and new catalog digest each discard selections and confirmation',async()=>{
  const h=harness({handler:(_url,method)=>method==='POST'?{...cached(),catalog_digest:'f'.repeat(64)}:undefined});h.render();let tree=await h.settle();
  field(tree,'选择型号 a-one').props.onChange({target:{checked:true}});tree=h.render();button(tree,'导入所选为停用配置').props.onClick();tree=h.render();
  button(tree,'从上游拉取更新').props.onClick();await h.settle();tree=h.render();
  assert.equal(field(tree,'选择型号 a-one').props.checked,false);assert.ok(!nodes(tree).some(n=>n.props?.['aria-label']==='确认导入模型配置'));
  field(tree,'选择型号 a-one').props.onChange({target:{checked:true}});tree=h.render();button(tree,'导入所选为停用配置').props.onClick();tree=h.render();
  nodes(tree).find(n=>n.type==='button'&&n.props['aria-pressed']===false).props.onClick();h.render();tree=await h.settle();
  assert.equal(field(tree,'选择型号 b-one').props.checked,false);assert.ok(!nodes(tree).some(n=>n.props?.['aria-label']==='确认导入模型配置'));
});

test('failed pulls label an earlier success as historical instead of current success',async()=>{
  const prior={...cached(),last_pull:{status:'success',request_url:'https://a.example/v1/models',completed_at:'2026-09-10T00:00:00Z',received_count:1,changes:{},counts:{}}};
  const h=harness({handler:(_url,method)=>{if(method==='POST')throw Error('拉取失败');return prior;}});h.render();let tree=await h.settle();
  button(tree,'从上游拉取更新').props.onClick();tree=await h.settle();assert.match(text(field(tree,'最近成功拉取结果')),/上次成功拉取记录（本次未更新）/);
});

test('full replacement first pulls the provider and reviews retirement bindings before acknowledgement',async()=>{
  const missing={id:'missing-profile',version:1,name:'Old configured',provider_id:'a',model_id:'a-missing',enabled:true};
  const created={id:'created-profile',version:1,name:'a-one',provider_id:'a',model_id:'a-one',enabled:false};
  const h=harness({models:[missing],agents:[{model_profile_id:missing.id,fallback_model_ids:[missing.id]}],
    handler:url=>url.endsWith('/replace')?{created:[created],retired:[{...missing,version:2,enabled:false}],restored:[],kept:[],catalog:{...cached(),retired_profile_ids:[missing.id]}}:undefined});
  h.render();let tree=await h.settle();button(tree,'全量更新替换').props.onClick();tree=await h.settle();
  assert.deepEqual(h.calls.filter(c=>c.method==='POST').map(c=>c.url),['/providers/a/catalog/sync']);
  const review=field(tree,'确认全量更新替换');assert.match(text(review),/上游当前 1 个型号/);assert.match(text(review),/新增停用配置 1/);assert.match(text(review),/停用并移出 1/);
  assert.match(text(review),/主模型绑定 1/);assert.match(text(review),/备用绑定 1/);
  assert.equal(button(tree,'确认全量更新替换').props.disabled,true);
  field(tree,'确认按上游全量替换').props.onChange({target:{checked:true}});tree=h.render();button(tree,'确认全量更新替换').props.onClick();tree=await h.settle();
  const write=h.calls.find(c=>c.url.endsWith('/replace'));assert.deepEqual(JSON.parse(JSON.stringify(write.body)),{revision_id:'rev-a',catalog_digest:'c'.repeat(64),acknowledged:true});
  assert.match(text(tree),/全量替换完成/);assert.ok(!nodes(tree).some(n=>n.props?.['aria-label']==='选择型号 a-missing'));
  assert.equal(text(field(tree,'当前已配置模型数')),'1');
});

test('empty or failed fresh pull never permits clearing all model configurations',async()=>{
  for(const outcome of ['empty','failed']){
    const h=harness({handler:(_url,method)=>{if(method==='POST'){if(outcome==='failed')throw Error('上游失败');return {...cached(),entries:[],models:[],counts:{available:0}};}}});
    h.render();let tree=await h.settle();button(tree,'全量更新替换').props.onClick();tree=await h.settle();
    assert.ok(!nodes(tree).some(n=>n.props?.['aria-label']==='确认全量更新替换'));
    assert.match(text(tree),outcome==='empty'?/空目录.*不会清空/:/上游失败/);
    assert.ok(!h.calls.some(c=>c.url.endsWith('/replace')));
  }
});

test('retired model configs are hidden by default but remain visible in the history filter',async()=>{
  const retired={id:'retired-profile',name:'Retired old model',provider_id:'a',model_id:'old-id',enabled:false};
  const h=harness({models:[retired],handler:()=>({...cached(),retired_profile_ids:[retired.id],entries:[...cached().entries,{id:'old-id',available:false}]})});
  h.render();let tree=await h.settle();assert.ok(!nodes(tree).some(n=>n.props?.['aria-label']==='选择型号 old-id'));
  assert.equal(text(field(tree,'当前已配置模型数')),'0');
  field(tree,'模型目录筛选').props.onChange({target:{value:'missing'}});tree=h.render();
  assert.ok(field(tree,'选择型号 old-id'));assert.match(text(tree),/已移出当前列表/);
});

test('same-name configs in another provider are not kept or retired by the selected provider replacement',async()=>{
  const other={id:'b-profile',name:'B same model',provider_id:'b',model_id:'a-one',enabled:true};
  const h=harness({models:[other]});h.render();let tree=await h.settle();button(tree,'全量更新替换').props.onClick();tree=await h.settle();
  const review=text(field(tree,'确认全量更新替换'));assert.match(review,/新增停用配置 1/);assert.match(review,/停用并移出 0/);assert.doesNotMatch(review,/B same model/);
  nodes(tree).find(n=>n.type==='button'&&n.props['aria-pressed']===false).props.onClick();h.render();tree=await h.settle();
  assert.ok(!nodes(tree).some(n=>n.props?.['aria-label']==='确认全量更新替换'));
  assert.match(text(tree),/B same model/);assert.ok(!h.calls.some(c=>c.url.endsWith('/replace')));
});

test('a returning retired model can be selected and restores its original profile instead of creating a duplicate',async()=>{
  const retired={id:'retired-profile',version:2,name:'Original configuration',provider_id:'a',model_id:'a-one',enabled:false};
  const h=harness({models:[retired],handler:url=>url.endsWith('/import')?{created:[],restored:[retired],skipped:[],catalog:cached()}:{...cached(),retired_profile_ids:[retired.id]}});
  h.render();let tree=await h.settle();assert.equal(field(tree,'选择型号 a-one').props.disabled,false);assert.match(text(tree),/已移出当前列表/);
  field(tree,'选择型号 a-one').props.onChange({target:{checked:true}});tree=h.render();button(tree,'导入所选为停用配置').props.onClick();tree=h.render();
  field(tree,'确认仅导入停用配置').props.onChange({target:{checked:true}});tree=h.render();button(tree,'确认导入停用配置').props.onClick();tree=await h.settle();
  assert.match(text(tree),/恢复 1 个已移出配置/);assert.doesNotMatch(text(tree),/已移出当前列表/);assert.equal(text(field(tree,'当前已配置模型数')),'1');
  assert.ok(h.calls.filter(c=>c.method==='POST').every(c=>c.url==='/providers/a/catalog/import'));
});

test('a replacement response arriving after switching suppliers cannot overwrite the current view',async()=>{
  let resolve;const h=harness({handler:url=>url.endsWith('/replace')?new Promise(done=>{resolve=done;}):undefined});h.render();let tree=await h.settle();
  button(tree,'全量更新替换').props.onClick();tree=await h.settle();field(tree,'确认按上游全量替换').props.onChange({target:{checked:true}});tree=h.render();
  button(tree,'确认全量更新替换').props.onClick();tree=h.render();nodes(tree).find(n=>n.type==='button'&&n.props['aria-pressed']===false).props.onClick();h.render();tree=await h.settle();
  resolve({created:[],restored:[],retired:[],kept:[],catalog:cached('a')});tree=await h.settle();
  assert.match(text(tree),/b-one/);assert.doesNotMatch(text(tree),/a-one/);assert.doesNotMatch(text(tree),/全量替换完成/);
});

const profiles=[{id:'config-a1',version:3,name:'A visible',provider_id:'a',model_id:'a-one',enabled:true,supports_tools:true},
  {id:'config-a2',version:7,name:'A hidden',provider_id:'a',model_id:'a-two',enabled:false,supports_tools:false},
  {id:'config-b1',version:2,name:'B same upstream model',provider_id:'b',model_id:'a-one',enabled:true,supports_tools:true}];
function statePreview(payload,rows=profiles){
  const items=payload.selections.map(selection=>{const row=rows.find(m=>m.id===selection.id);return {...row,target_enabled:payload.enabled,primary:[{id:'agent',name:'Atlas',version:2}],fallback:[]};});
  const changed=items.filter(item=>item.enabled!==payload.enabled).length;
  return {provider_id:'a',provider_name:'供应商 A',revision_id:payload.revision_id,selections:payload.selections,enabled:payload.enabled,digest:'e'.repeat(64),items,
    counts:{selected:items.length,changed,unchanged:items.length-changed,primary_bindings:items.length,fallback_bindings:0,unconfirmed_tools:items.filter(item=>!item.supports_tools).length}};
}
function stateResult(payload,rows=profiles){
  const changed=rows.filter(row=>payload.selections.some(s=>s.id===row.id)&&row.enabled!==payload.enabled).map(row=>({...row,enabled:payload.enabled,version:row.version+1}));
  const unchanged=rows.filter(row=>payload.selections.some(s=>s.id===row.id)&&row.enabled===payload.enabled);
  return {changed,unchanged,catalog:cached(),receipt:{operation_id:'reviewed-operation',enabled:payload.enabled,counts:{changed:changed.length,unchanged:unchanged.length}}};
}
function bulkHandler(url,method,payload){if(url.endsWith('/bulk-state/preview'))return statePreview(payload);if(url.endsWith('/bulk-state'))return stateResult(payload);}

test('configuration select-all includes exact visible profile IDs and never another provider or retired profile',async()=>{
  const retired={...profiles[0],id:'retired',name:'Retired A',enabled:false};
  const h=harness({models:[...profiles,retired],handler:(url,method,payload)=>bulkHandler(url,method,payload)||{...cached(),retired_profile_ids:['retired']}});
  h.render();let tree=await h.settle();
  assert.equal(field(tree,'选择模型配置 Retired A').props.disabled,true);
  field(tree,'搜索此供应商模型').props.onChange({target:{value:'a-one'}});h.render();tree=h.render();
  field(tree,'选择当前筛选下模型配置').props.onChange({target:{checked:true}});tree=h.render();
  button(tree,'批量停用').props.onClick();tree=await h.settle();
  const request=h.calls.find(c=>c.url.endsWith('/bulk-state/preview'));
  assert.deepEqual(JSON.parse(JSON.stringify(request.body.selections)),[{id:'config-a1',version:3}]);
  const value=text(field(tree,'确认批量模型状态'));assert.match(value,/A visible/);assert.doesNotMatch(value,/A hidden|B same upstream|Retired A/);
  assert.ok(!h.calls.some(c=>c.url.endsWith('/bulk-state')));
});

test('reviewed bulk disable requires acknowledgement and updates local state without rebinding or upstream calls',async()=>{
  const h=harness({models:profiles,handler:bulkHandler});h.render();let tree=await h.settle();
  field(tree,'选择当前筛选下模型配置').props.onChange({target:{checked:true}});tree=h.render();
  button(tree,'批量停用').props.onClick();tree=await h.settle();
  assert.equal(button(tree,'确认批量停用').props.disabled,true);
  await button(tree,'确认批量停用').props.onClick();assert.ok(!h.calls.some(c=>c.url.endsWith('/bulk-state')));
  const review=text(field(tree,'确认批量模型状态'));assert.match(review,/实际改变 1 个/);assert.match(review,/未声明工具能力 1/);assert.match(review,/不等于通过能力验证/);assert.match(review,/Atlas/);
  field(tree,'确认批量模型状态影响').props.onChange({target:{checked:true}});tree=h.render();
  button(tree,'确认批量停用').props.onClick();tree=await h.settle();
  const write=h.calls.find(c=>c.url.endsWith('/bulk-state'));
  assert.deepEqual(JSON.parse(JSON.stringify(write.body)),{revision_id:'rev-a',selections:[{id:'config-a1',version:3},{id:'config-a2',version:7}],enabled:false,digest:'e'.repeat(64),acknowledged:true});
  assert.match(text(tree),/批量停用完成：改变 1 个配置，1 个保持原状态/);assert.match(text(tree),/v4 · 已停用/);
  assert.ok(h.calls.every(c=>c.url==='/providers/a/catalog'||c.url.startsWith('/providers/a/models/bulk-state')));
  assert.equal(h.refresh.length,1);
});

test('same upstream model can have multiple configurations and selections keep their distinct IDs',async()=>{
  const rows=[profiles[0],{...profiles[0],id:'alternative',version:8,name:'Alternative parameters'}];
  const h=harness({models:rows,handler:(url,method,payload)=>url.endsWith('/preview')?statePreview(payload,rows):undefined});
  h.render();let tree=await h.settle();field(tree,'选择当前筛选下模型配置').props.onChange({target:{checked:true}});tree=h.render();button(tree,'批量停用').props.onClick();await h.settle();
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls.find(c=>c.url.endsWith('/preview')).body.selections)),[{id:'config-a1',version:3},{id:'alternative',version:8}]);
});

test('filter changes clear selections and reject late bulk previews',async()=>{
  let resolve;const h=harness({models:profiles,handler:(url,method,payload)=>url.endsWith('/preview')?new Promise(done=>{resolve=()=>done(statePreview(payload));}):undefined});
  h.render();let tree=await h.settle();field(tree,'选择模型配置 A visible').props.onChange({target:{checked:true}});tree=h.render();
  button(tree,'批量停用').props.onClick();tree=h.render();field(tree,'搜索此供应商模型').props.onChange({target:{value:'A hidden'}});h.render();
  resolve();tree=await h.settle();assert.ok(!nodes(tree).some(n=>n.props?.['aria-label']==='确认批量模型状态'));
  assert.equal(button(tree,'批量停用').props.disabled,true);assert.equal(field(tree,'选择模型配置 A hidden').props.checked,false);
});

test('switching providers rejects late bulk results and never offers the previous confirmation',async()=>{
  let resolve;const h=harness({models:profiles,handler:(url,method,payload)=>url.endsWith('/bulk-state')?new Promise(done=>{resolve=()=>done(stateResult(payload));}):bulkHandler(url,method,payload)});
  h.render();let tree=await h.settle();field(tree,'选择模型配置 A visible').props.onChange({target:{checked:true}});tree=h.render();button(tree,'批量停用').props.onClick();tree=await h.settle();
  field(tree,'确认批量模型状态影响').props.onChange({target:{checked:true}});tree=h.render();button(tree,'确认批量停用').props.onClick();tree=h.render();
  nodes(tree).find(n=>n.type==='button'&&n.props['aria-pressed']===false).props.onClick();h.render();tree=await h.settle();resolve();tree=await h.settle();
  assert.match(text(tree),/B same upstream model/);assert.doesNotMatch(text(tree),/A visible|批量停用完成/);
  assert.ok(!nodes(tree).some(n=>n.props?.['aria-label']==='确认批量模型状态'));
});

test('a conflict is visible and discards acknowledgement without claiming partial success',async()=>{
  const h=harness({models:profiles,handler:(url,method,payload)=>{if(url.endsWith('/bulk-state'))throw Error('配置版本已改变，请重新预览');return bulkHandler(url,method,payload);}});
  h.render();let tree=await h.settle();field(tree,'选择模型配置 A visible').props.onChange({target:{checked:true}});tree=h.render();button(tree,'批量停用').props.onClick();tree=await h.settle();
  field(tree,'确认批量模型状态影响').props.onChange({target:{checked:true}});tree=h.render();button(tree,'确认批量停用').props.onClick();tree=await h.settle();
  assert.match(text(tree),/配置版本已改变，请重新预览/);assert.doesNotMatch(text(tree),/批量停用完成/);assert.match(text(tree),/v3 · 已启用/);
  assert.ok(!nodes(tree).some(n=>n.props?.['aria-label']==='确认批量模型状态'));assert.equal(h.refresh.length,0);
});

test('model or binding changes invalidate a reviewed selection before confirmation',async()=>{
  for(const update of [{models:profiles.map(m=>({...m,version:m.version+1}))},{agents:[{id:'new-agent',version:1,model_profile_id:'config-a1',fallback_model_ids:[]}]}]){
    const h=harness({models:profiles,handler:bulkHandler});h.render();let tree=await h.settle();field(tree,'选择模型配置 A visible').props.onChange({target:{checked:true}});tree=h.render();button(tree,'批量停用').props.onClick();tree=await h.settle();
    field(tree,'确认批量模型状态影响').props.onChange({target:{checked:true}});tree=h.render();h.update(update);tree=h.render();
    assert.ok(!nodes(tree).some(n=>n.props?.['aria-label']==='确认批量模型状态'));assert.equal(button(tree,'批量停用').props.disabled,true);
  }
});

test('disabled providers allow batch disable only, and history reads remain explicitly scoped',async()=>{
  const h=harness({models:profiles,providerRows:providers.map(p=>({...p,enabled:false})),handler:(url,method,payload)=>url.endsWith('/history')?{items:[]}:bulkHandler(url,method,payload)});
  h.render();let tree=await h.settle();field(tree,'选择当前筛选下模型配置').props.onChange({target:{checked:true}});tree=h.render();
  assert.equal(button(tree,'批量启用').props.disabled,true);assert.equal(button(tree,'批量停用').props.disabled,false);
  assert.ok(!h.calls.some(c=>c.url.endsWith('/history')));button(tree,'查看批量操作历史').props.onClick();tree=await h.settle();
  assert.match(text(field(tree,'此供应商批量操作历史')),/尚无批量启停记录/);
  assert.equal(h.calls.at(-1).url,'/providers/a/models/bulk-state/history');
  nodes(tree).find(n=>n.type==='button'&&n.props['aria-pressed']===false).props.onClick();h.render();tree=await h.settle();
  assert.ok(!nodes(tree).some(n=>n.props?.['aria-label']==='此供应商批量操作历史'));
});
