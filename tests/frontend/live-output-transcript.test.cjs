const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const ts=require('../../frontend/vendor/typescript.cjs');
function harness(){
 const cache={},React={useMemo:fn=>fn(),useState:v=>[typeof v==='function'?v():v,()=>{}],useRef:v=>({current:v}),useEffect:()=>{}};
 const jsx=(type,props)=>typeof type==='function'?type(props||{}):{type,props:props||{}};
 function load(id){if(id==='react')return React;if(id==='react/jsx-runtime')return{jsx,jsxs:jsx,Fragment:'fragment'};if(id.endsWith('.css'))return{};if(id.endsWith('/api'))return{api:()=>assert.fail('render must not issue a request')};if(id.endsWith('/UI'))return{Badge:p=>jsx('badge',p)};if(cache[id])return cache[id];const file=id+'.tsx',source=fs.readFileSync(fs.existsSync(file)?file:id+'.ts','utf8'),exports=cache[id]={};vm.runInNewContext(ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports,require:r=>load(r.startsWith('.')?path.resolve(path.dirname(id),r):r),console,URL,Date,Map,Set});return exports;}
 return load(path.resolve(__dirname,'../../frontend/src/components/LiveOutputDock')).default;
}
const nodes=v=>Array.isArray(v)?v.flatMap(nodes):v&&typeof v==='object'?[v,...nodes(v.props?.children)]:[];
const text=v=>Array.isArray(v)?v.map(text).join(''):v&&typeof v==='object'?text(v.props?.children):v==null||typeof v==='boolean'?'':String(v);
const event=(id,kind,payload)=>({id,run_id:'run',created_at:'2026-09-22T10:00:00Z',kind:'runtime.'+kind.replaceAll('/','.'),data:{agent_id:'a',task_key:'task',runtime_kind:kind,connection:{connection_id:'c'},payload:{threadId:'th',turnId:'turn',...payload}}});
const events=[event(1,'item/agentMessage/delta',{itemId:'m1',delta:'先检查 `game.js`。'}),event(2,'item/started',{item:{id:'cmd',type:'commandExecution',command:'cat game.js',status:'inProgress'}}),event(3,'item/commandExecution/outputDelta',{itemId:'cmd',delta:'const lives = 3;'}),event(4,'item/completed',{item:{id:'cmd',type:'commandExecution',command:'cat game.js',status:'completed',exitCode:0,aggregatedOutput:'const lives = 3;'}}),event(5,'item/agentMessage/delta',{itemId:'m2',delta:'已确认 **三条生命**。'}),event(6,'item/completed',{item:{id:'m2',type:'agentMessage',text:'已确认 **三条生命**。'}})];
const run={id:'run',title:'实现射击小游戏',request:'请实现三条生命，并检查 game.js。',status:'running',snapshot:{agents:{a:{name:'Nova'}}},tasks:[{id:'task',key:'task',title:'实现游戏',agent_id:'a',status:'running'}]};
const render=(input=events,currentRun=run,currentAgents=[{id:'a',name:'Nova'}])=>harness()({run:currentRun,events:input,agents:currentAgents,open:true,onClose(){}});
test('same command start/delta/completion becomes one collapsed row between assistant messages',()=>{
 const tree=render(),tools=nodes(tree).filter(n=>n.type==='details'&&n.props.className?.includes('live-output-tool'));
 assert.equal(tools.length,1,'one tool call must not become separate start and completion cards');assert.ok(!tools[0].props.open,'tool detail should start collapsed');
 const content=text(nodes(tree).find(n=>n.type==='main'));assert.ok(content.indexOf('先检查')<content.indexOf('cat game.js'));assert.ok(content.indexOf('cat game.js')<content.indexOf('已确认'),'tool must stay between the messages');assert.doesNotMatch(content,/工具事件已记录/);assert.equal((content.match(/已确认/g)||[]).length,1);
});
test('assistant Markdown renders code and emphasis instead of raw syntax',()=>{
 const tree=render();assert.ok(nodes(tree).some(n=>n.type==='code'&&text(n)==='game.js'));assert.ok(nodes(tree).some(n=>n.type==='strong'&&text(n)==='三条生命'));
});
test('each run member gets an independent conversation surface',()=>{
 const second=event(7,'item/agentMessage/delta',{itemId:'m3',delta:'另一位成员的独立回执。'});second.data.agent_id='b';second.data.payload.taskKey='task-b';
 const twoMemberRun={...run,snapshot:{agents:{a:{name:'Nova'},b:{name:'Atlas'}}},tasks:[...run.tasks,{id:'task-b',key:'task-b',title:'验证实现',agent_id:'b',status:'running'}]};
 const tree=render([...events,second],twoMemberRun,[{id:'a',name:'Nova'},{id:'b',name:'Atlas'}]);
 const switcher=nodes(tree).find(n=>n.type==='nav'&&n.props['aria-label']==='成员实时输出');
 assert.ok(switcher);assert.match(text(switcher),/Nova/);assert.match(text(switcher),/Atlas/);
 const main=text(nodes(tree).find(n=>n.type==='main'));assert.ok(/先检查|另一位成员的独立回执/.test(main));assert.ok(!(/先检查/.test(main)&&/另一位成员的独立回执/.test(main)));
});
test('bounded subagent activations stay in the dedicated subagent panel',()=>{
 const child=event(8,'item/agentMessage/delta',{itemId:'child-message',task_key:'child',delta:'子智能体独立回执。'});child.data.task_key='child';
 const childRun={...run,tasks:[...run.tasks,{id:'child',key:'child',title:'Plugin security',agent_id:'a',status:'running',spec:{assignment_type:'subagent',source_task_key:'task'}}]};
 const tree=render([...events,child],childRun);
 const panel=nodes(tree).find(n=>n.type==='aside'&&n.props['aria-label']==='子智能体');
 assert.ok(panel);assert.match(text(panel),/Plugin security/);assert.match(text(panel),/已开启 · 1/);
 assert.match(text(panel),/正在处理/);assert.match(text(panel),/(刚刚|分钟前|小时前|天前)/);
 const main=text(nodes(tree).find(n=>n.type==='main'));assert.match(main,/先检查/);assert.doesNotMatch(main,/子智能体独立回执/);
});
test('empty member streams explain that output is still pending and name the recipient',()=>{
 const emptyRun={...run,tasks:[{id:'waiting',key:'waiting',title:'等待响应',agent_id:'a',status:'pending'}]};
 const tree=render([],emptyRun);
 const content=text(tree);assert.match(content,/等待模型输出/);assert.match(content,/Nova/);
 assert.ok(nodes(tree).some(n=>n.type==='span'&&n.props.className==='live-output-recipient'));
});
module.exports={harness,nodes,text,event,events,run,render};
