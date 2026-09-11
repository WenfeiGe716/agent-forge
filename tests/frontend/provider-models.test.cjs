const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('../../frontend/vendor/typescript.cjs');

const source = fs.readFileSync(require.resolve('../../frontend/src/components/Forms.tsx'),'utf8');
const file = ts.createSourceFile('Forms.tsx',source,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
const fn = file.statements.find(n=>ts.isFunctionDeclaration(n)&&n.name?.text==='ModelForm');
const compiled = ts.transpileModule(fn.getText(file),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText;
const providers=[{id:'a',protocol:'openai_chat',enabled:true},{id:'b',protocol:'openai_responses',enabled:true}];
function nodes(value){if(Array.isArray(value))return value.flatMap(nodes);if(!value||typeof value!=='object')return [];return [value,...nodes(value.props?.children)];}
function field(tree,label){return nodes(tree).find(n=>n.props?.['aria-label']===label);}
function harness(props,handler){
 let index=0,tree;const state=[],effects=[],cleanup=[],calls=[];
 const exported={},jsx=(type,props)=>({type,props:props||{}});
 vm.runInNewContext(compiled,{exports:exported,require:()=>({jsx,jsxs:jsx}),EditorForm:'EditorForm',Field:'Field',JsonField:'JsonField',
  jsonText:JSON.stringify,parseObject:JSON.parse,
  useState(initial){const i=index++;if(!(i in state))state[i]=initial;return [state[i],value=>{state[i]=typeof value==='function'?value(state[i]):value;}];},
  useEffect(fn,deps){const i=index++;if(!(i in state)||deps.some((v,j)=>v!==state[i][j])){cleanup[i]?.();state[i]=deps;effects.push(()=>{cleanup[i]=fn();});}},
  api:async(url,method='GET',body)=>{calls.push({url,method,body});return handler?handler(url,method,body):{models:[{id:url.includes('/a/')?'a-only':'b-only'}],counts:{available:1},stale:false};}
 });
 const render=()=>{index=0;tree=exported.ModelForm({providers,onDone(){},...props});effects.splice(0).forEach(fn=>fn());return tree;};
 return {render,calls,async settle(){await new Promise(r=>setImmediate(r));return render();}};
}

test('suggested provider and model survive initial loading; discovery does not save a model',async()=>{
 const h=harness({suggested:{provider_id:'b',model_id:'b-only',name:'Chosen model'}});h.render();const tree=await h.settle();
 assert.equal(field(tree,'供应商连接').props.value,'b');assert.equal(field(tree,'模型ID').props.value,'b-only');
 assert.ok(h.calls.every(c=>c.method==='GET'));assert.ok(h.calls.every(c=>c.url.includes('/b/')));
});

test('switching provider clears old model, costs, context and tool confirmation',async()=>{
 const h=harness({initial:{id:'profile',name:'Old',provider_id:'a',model_id:'a-only',allowed_parameters:['max_tokens'],parameters:{max_tokens:123},supports_tools:true,input_cost_per_million:2,output_cost_per_million:4,context_window:8000}});
 h.render();let tree=await h.settle();field(tree,'供应商连接').props.onChange({target:{value:'b'}});h.render();tree=await h.settle();
 assert.equal(field(tree,'模型ID').props.value,'');assert.equal(field(tree,'输入价格 / 百万 tokens').props.value,'');
 assert.equal(field(tree,'输出价格 / 百万 tokens').props.value,'');assert.equal(field(tree,'上下文窗口 / tokens').props.value,'');
 const checks=nodes(tree).filter(n=>n.type==='input'&&n.props.type==='checkbox');assert.equal(checks.at(-1).props.checked,false);
 assert.ok(nodes(tree).some(n=>n.type==='option'&&n.props.value==='b-only'));assert.ok(!nodes(tree).some(n=>n.type==='option'&&n.props.value==='a-only'));
 assert.ok(h.calls.every(c=>c.method==='GET'));
});

test('late response from previous provider cannot replace the current datalist',async()=>{
 const pending=[];const h=harness({suggested:{provider_id:'a'}},url=>new Promise(resolve=>pending.push({url,resolve})));
 let tree=h.render();field(tree,'供应商连接').props.onChange({target:{value:'b'}});h.render();
 pending[1].resolve({models:[{id:'b-only'}],counts:{available:1},stale:false});tree=await h.settle();
 pending[0].resolve({models:[{id:'a-only'}],counts:{available:1},stale:false});tree=await h.settle();
 assert.ok(nodes(tree).some(n=>n.type==='option'&&n.props.value==='b-only'));assert.ok(!nodes(tree).some(n=>n.type==='option'&&n.props.value==='a-only'));
});
