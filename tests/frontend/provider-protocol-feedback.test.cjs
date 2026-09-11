const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {createRequire} = require('node:module');
const ts = require('../../frontend/vendor/typescript.cjs');

function nodes(value){if(Array.isArray(value))return value.flatMap(nodes);if(!value||typeof value!=='object')return [];return [value,...nodes(value.props?.children)];}
function text(value){if(Array.isArray(value))return value.map(text).join('');if(value&&typeof value==='object')return text(value.props?.children);return value==null?'':String(value);}
function labelled(tree,label){const found=nodes(tree).find(n=>n.props?.['aria-label']===label)||nodes(tree).find(n=>n.props?.label===label);assert.ok(found,'field '+label);return found;}
function feedback(tree,label){return nodes(tree).find(n=>n.props?.['aria-label']===label);}

const source = fs.readFileSync(require.resolve('../../frontend/src/components/Forms.tsx'),'utf8');
const file = ts.createSourceFile('Forms.tsx',source,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
const wanted = new Set(['EditorForm','JsonField','ProviderForm','ModelForm','protocolExample']);
const selected = file.statements.filter(n=>ts.isFunctionDeclaration(n)&&wanted.has(n.name?.text)||ts.isVariableStatement(n)&&n.declarationList.declarations.some(d=>['customProtocolExample','officialProviderPresets'].includes(d.name.getText(file))));
const compiled = ts.transpileModule(selected.map(n=>n.getText(file)).join('\n'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText;

function forms(name,props={},handler){
 let cursor=0;const slots=[],effects=[],cleanup=[],calls=[],exports={};
 const jsx=(type,props)=>({type,props:props||{}});
 const hooks={
  useState(initial){const i=cursor++;if(!(i in slots))slots[i]=typeof initial==='function'?initial():initial;return [slots[i],value=>{slots[i]=typeof value==='function'?value(slots[i]):value;}];},
  useEffect(fn,deps){const i=cursor++;if(!(i in slots)||deps.some((v,j)=>!Object.is(v,slots[i][j]))){cleanup[i]?.();slots[i]=deps;effects.push(()=>{cleanup[i]=fn();});}},
 };
 vm.runInNewContext(compiled,{exports,require:()=>({jsx,jsxs:jsx,Fragment:'Fragment'}),...hooks,Field:'Field',SaveButton:'SaveButton',
  jsonText:value=>JSON.stringify(value??{},null,2),parseObject:JSON.parse,
  api:async(url,method='GET',body)=>{calls.push({url,method,body});if(handler)return handler(url,method,body);return {id:'saved',revision_id:'new-revision',models:[],counts:{available:0},stale:false};}
 });
 const render=()=>{cursor=0;let tree=exports[name]({onDone(){},...props});if(tree.type?.name==='EditorForm')tree=tree.type(tree.props);effects.splice(0).forEach(fn=>fn());return tree;};
 return {calls,render,async settle(){await new Promise(resolve=>setImmediate(resolve));return render();}};
}

function centerHelpers(){
 const filename=require.resolve('./provider-center.test.cjs');
 const localRequire=createRequire(filename),exports={};
 vm.runInNewContext(fs.readFileSync(filename,'utf8')+'\nObject.assign(exports,{harness,nodes,field,button,text});',{
  exports,console,setImmediate,Date,
  require:Object.assign(id=>id==='node:test'?()=>{}:localRequire(id),{resolve:localRequire.resolve}),
 },{filename});
 return exports;
}
const center=centerHelpers();
const provider={id:'a',revision_id:'rev-a',name:'测试连接',protocol:'openai_chat',base_url:'https://example.invalid/v1',external:true,enabled:true,has_key:true,protocol_config:{}};
const responses={...provider,protocol:'openai_responses'};
const legacy={id:'legacy',version:1,provider_id:'a',name:'旧参数配置',model_id:'legacy-id',enabled:false,supports_tools:true,allowed_parameters:['max_tokens'],parameters:{max_tokens:4096},fallback_model_ids:[]};
const failure='连接变更未保存，现有连接保持不变。\n旧参数配置 · legacy-id · 配置 ID legacy\nopenai_responses 不支持 max_tokens，请手动检查模型配置。';

test('changing an existing provider protocol shows its save-time compatibility check without mutating models or bindings',()=>{
 const original=JSON.stringify(provider),h=forms('ProviderForm',{initial:provider});
 let tree=h.render();assert.equal(feedback(tree,'协议变更影响'),undefined);
 labelled(tree,'协议适配器').props.onChange({target:{value:'openai_responses'}});tree=h.render();
 const warning=feedback(tree,'协议变更影响');assert.ok(warning);
 assert.match(text(warning),/已有模型配置/);assert.match(text(warning),/拒绝/);assert.match(text(warning),/原连接/);
 assert.match(text(warning),/参数/);assert.match(text(warning),/Agent.*绑定/);
 assert.equal(h.calls.length,0);assert.equal(JSON.stringify(provider),original);
 labelled(tree,'协议适配器').props.onChange({target:{value:provider.protocol}});
 assert.equal(feedback(h.render(),'协议变更影响'),undefined);
});

test('custom protocol JSON edits show the same warning while unchanged formatting and new connections do not',()=>{
 const config={endpoint:'/generate',supported_parameters:['max_tokens']};
 const h=forms('ProviderForm',{initial:{...provider,protocol:'custom_http',protocol_config:config}});
 let tree=h.render();assert.equal(feedback(tree,'协议变更影响'),undefined);
 labelled(tree,'自定义协议 · JSON').props.setValue(JSON.stringify(config)+'\n');tree=h.render();
 assert.equal(feedback(tree,'协议变更影响'),undefined);
 labelled(tree,'自定义协议 · JSON').props.setValue(JSON.stringify({...config,supported_parameters:['max_output_tokens']}));
 assert.ok(feedback(h.render(),'协议变更影响'));assert.equal(h.calls.length,0);
 const fresh=forms('ProviderForm');tree=fresh.render();labelled(tree,'协议适配器').props.onChange({target:{value:'custom_http'}});
 assert.equal(feedback(fresh.render(),'协议变更影响'),undefined);
});

test('a rejected provider save preserves the draft and renders the full multi-line backend explanation',async()=>{
 let completed=0;const h=forms('ProviderForm',{initial:provider,onDone(){completed++;}},()=>{throw Error(failure);});
 let tree=h.render();labelled(tree,'协议适配器').props.onChange({target:{value:'openai_responses'}});tree=h.render();
 await tree.props.onSubmit({preventDefault(){}});tree=h.render();
 const alert=nodes(tree).find(n=>n.props?.role==='alert');assert.equal(text(alert),failure);
 assert.equal(labelled(tree,'协议适配器').props.value,'openai_responses');assert.equal(completed,0);
 assert.deepEqual(h.calls.map(c=>[c.url,c.method]),[['/providers/a','PUT']]);
 assert.equal(h.calls[0].body.protocol,'openai_responses');assert.equal('api_key' in h.calls[0].body,false);
});

test('legacy Responses max_tokens is explained without changing either declaration or value',async()=>{
 const h=forms('ModelForm',{initial:legacy,providers:[responses]});h.render();let tree=await h.settle();
 const warning=feedback(tree,'历史模型参数提示');assert.ok(warning);assert.match(text(warning),/max_tokens/);assert.match(text(warning),/max_output_tokens/);assert.match(text(warning),/手动/);
 assert.equal(JSON.parse(labelled(tree,'默认模型参数 · JSON').props.value).max_tokens,4096);
 const declaration=nodes(labelled(tree,'声明支持的调用参数')).find(n=>n.type==='input');assert.equal(declaration.props.value,'max_tokens');
 assert.ok(h.calls.every(c=>c.method==='GET'));
 declaration.props.onChange({target:{value:'max_output_tokens'}});tree=h.render();
 assert.ok(feedback(tree,'历史模型参数提示'),'parameter value still needs explicit editing');
 labelled(tree,'默认模型参数 · JSON').props.setValue('{"max_output_tokens":4096}');tree=h.render();
 assert.equal(feedback(tree,'历史模型参数提示'),undefined);
 await tree.props.onSubmit({preventDefault(){}});
 const saved=h.calls.find(c=>c.method==='PUT');assert.deepEqual(JSON.parse(JSON.stringify(saved.body.parameters)),{max_output_tokens:4096});
 assert.deepEqual(Array.from(saved.body.allowed_parameters),['max_output_tokens']);
});

test('valid Responses defaults and Chat Completions max_tokens do not show a legacy mismatch hint',async()=>{
 for(const props of [{providers:[responses]},{initial:legacy,providers:[provider]}]){
  const h=forms('ModelForm',props);h.render();const tree=await h.settle();assert.equal(feedback(tree,'历史模型参数提示'),undefined);
 }
});

test('multi-line validation errors retain line breaks in editor and provider-center styles',()=>{
 const css=fs.readFileSync(path.resolve(__dirname,'../../frontend/src/styles.css'),'utf8')+'\n'+fs.readFileSync(path.resolve(__dirname,'../../frontend/src/provider-center.css'),'utf8');
 for(const selector of ['.editor-form .form-error','.provider-center .form-error']){
  const escaped=selector.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
  assert.ok(new RegExp(escaped+'[^{}]*\\{[^}]*white-space\\s*:\\s*pre-wrap').test(css),selector+' must preserve line breaks');
 }
});

async function rejectedBulk(){
 const h=center.harness({providerRows:[responses],models:[legacy],handler:url=>{if(url.endsWith('/bulk-state/preview'))throw Error(failure);}});
 h.render();let tree=await h.settle();center.field(tree,'选择当前筛选下模型配置').props.onChange({target:{checked:true}});tree=h.render();
 center.button(tree,'批量启用').props.onClick();tree=await h.settle();assert.ok(center.text(tree).includes(failure));return h;
}

test('editing a model of the current provider clears its obsolete action error',async()=>{
 const h=await rejectedBulk();
 h.update({models:[{...legacy,version:2,allowed_parameters:['max_output_tokens'],parameters:{max_output_tokens:4096}}]});
 const tree=await h.settle();assert.ok(!center.text(tree).includes(failure));
 assert.ok(!h.calls.some(c=>c.url.endsWith('/bulk-state')));assert.equal(h.refresh.length,0);
});

test('unchanged rerenders and another provider model update retain the actionable error',async()=>{
 const h=await rejectedBulk();h.update({models:[{...legacy},{...legacy,id:'other',provider_id:'b',version:8}]});
 assert.ok(center.text(await h.settle()).includes(failure));assert.ok(center.text(h.render()).includes(failure));
});

test('a failed catalog fetch remains visible after unchanged renders',async()=>{
 const h=center.harness({providerRows:[responses],models:[legacy],handler:()=>{throw Error('目录读取失败\n请检查此连接');}});
 h.render();assert.match(center.text(await h.settle()),/目录读取失败\n请检查此连接/);
 assert.match(center.text(h.render()),/目录读取失败\n请检查此连接/);
});
