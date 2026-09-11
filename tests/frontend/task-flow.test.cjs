const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const ts=require('../../frontend/vendor/typescript.cjs');
const compile=path=>ts.transpileModule(fs.readFileSync(require.resolve('../../frontend/src/'+path),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText;
const helpers={};vm.runInNewContext(compile('taskFlowModel.ts'),{exports:helpers});
const nodes=value=>Array.isArray(value)?value.flatMap(nodes):value&&typeof value==='object'?[value,...nodes(value.props?.children)]:[];
const text=value=>Array.isArray(value)?value.map(text).join(''):value&&typeof value==='object'?text(value.props?.children):value==null||typeof value==='boolean'?'':String(value);
const base={id:'r',status:'running',snapshot:{runtime:{max_parallel:3},agents:{a:{name:'需求顾问',job_title:'需求分析'},b:{name:'数据专家',job_title:'经营分析'},c:{name:'内容设计师',job_title:'内容策划'}}},plan:{summary:'准备依据，分别分析和策划，汇总输出。',tasks:[]},tasks:[]};
const plan=(key,agent_id,depends_on=[])=>({key,agent_id,title:'工作 '+key,depends_on,description:'完成 '+key,acceptance:'交付 '+key+' 的真实成果'});
const task=(key,agent_id='a',status='pending',kind='develop',spec={})=>({id:'r:'+key,key,agent_id,status,kind,title:'阶段 '+key,spec,result:{}});
const event=(id,kind,data,run_id='r')=>({id,kind,data,run_id,created_at:'2026-09-10T00:00:00Z'});

test('autonomous delegation and message tasks form real lanes without a synthetic approved plan',()=>{
 const run={...base,execution_strategy:'autonomous',tasks:[task('delegate','b','integrated','autonomous',{assignment_type:'delegation'}),task('reply','a','running','autonomous',{assignment_type:'message',depends_on:['delegate']})]};
 const flow=helpers.buildTaskFlow(run);assert.deepEqual(Array.from(flow.nodes,n=>[n.key,n.level]),[['delegate',0],['reply',1]]);assert.equal(flow.stats.running,1);
 const content=text(render(run).tree);assert.match(content,/2 个实际分工/);assert.match(content,/任务委派/);assert.match(content,/消息回应/);assert.doesNotMatch(content,/待批准计划|确认计划/);
});

test('mode-only run summaries identify autonomous work instead of plan placeholders',()=>{
 const run={...base,mode:'autonomous',tasks:[task('work','a','running','autonomous',{assignment_type:'coordination'})]};assert.match(text(render(run).tree),/1 个实际分工/);assert.match(text(render(run).tree),/工作协调/);
});

test('autonomous waiting and ready outcomes remain stage submissions, never proof of run success',()=>{
 for(const [outcome,label]of [['waiting',/等待成员消息/],['ready',/候选交付/],['blocked',/未完成/]]){const t={...task('coord','a','done','autonomous',{coordinator:true}),result:{outcome,summary:'实际阶段结论'}};const run={...base,execution_strategy:'autonomous',tasks:[t]};assert.match(helpers.flowStatus(t,run),label);assert.doesNotMatch(text(render(run).tree),/任务已完成|100%/);}
});

test('frozen manifest files use real hashes and sizes, without constructing receipt-path downloads',()=>{
 const run={...base,execution_strategy:'autonomous',artifact_digest:'b'.repeat(64),report:{mode:'autonomous',deliverables:[{path:'docs/requirements.md',sha256:'a'.repeat(64),size:123},{path:'invalid.txt',sha256:'unknown',size:3}]}};
 const {tree}=render(run,[],'TaskOutputs');assert.match(text(tree),/已冻结交付文件/);assert.match(text(tree),/docs\/requirements.md/);assert.match(text(tree),/123 字节/);assert.doesNotMatch(text(tree),/invalid.txt/);assert.equal(nodes(tree).filter(n=>n.type==='a').length,0);
 const unpublished=render({...run,artifact_digest:''},[],'TaskOutputs').tree;assert.doesNotMatch(text(unpublished),/已冻结交付文件/);
});
function render(run=base,events=[],name='default'){
 const jsx=(type,props)=>typeof type==='function'?type(props||{}):{type,props:props||{}};
 const exports={},calls=[];
 vm.runInNewContext(compile('components/TaskFlow.tsx'),{exports,require:id=>id==='react/jsx-runtime'?{jsx,jsxs:jsx,Fragment:'fragment'}:id==='../taskFlowModel'?helpers:assert.fail('unexpected module '+id)});
 return{tree:exports[name]({run,events,onTask:item=>calls.push(item)}),calls};
}

test('fork and join are real dependency levels, not a fixed five-step pipeline',()=>{
 const run={...base,plan:{tasks:[plan('a','a'),plan('b','b',['a']),plan('c','c',['a']),plan('d','a',['b','c'])]}};
 const flow=helpers.buildTaskFlow(run);assert.deepEqual(Array.from(flow.nodes,node=>[node.key,node.level]),[['a',0],['b',1],['c',1],['d',2]]);assert.equal(flow.depth,3);assert.equal(flow.lanes.length,3);assert.equal(flow.unresolved.size,0);
 const content=text(render(run).tree);assert.match(content,/同一列只表示依赖深度相同，不表示同时开工/);assert.match(content,/并行上限为 3/);assert.doesNotMatch(content,/制定计划确认计划执行任务测试与审查人工验收/);
});

test('plan order does not need to be topological and input contracts are not mutated',()=>{
 const entries=[plan('c','c',['b']),plan('b','b',['a']),plan('a','a')];const run={...base,plan:{tasks:entries}};const before=JSON.stringify(run);const flow=helpers.buildTaskFlow(run);
 assert.deepEqual(Array.from(flow.nodes,node=>node.level),[2,1,0]);assert.equal(JSON.stringify(run),before);
});

test('parallel branches keep independent member assignments and actual statuses',()=>{
 const run={...base,plan:{tasks:[plan('b','b'),plan('c','c')]},tasks:[task('b','b','running'),task('c','c','waiting_decision')]};const flow=helpers.buildTaskFlow(run);
 assert.equal(flow.stats.running,1);assert.equal(flow.stats.waiting,1);assert.equal(flow.nodes[0].task.agent_id,'b');assert.equal(flow.nodes[1].task.status,'waiting_decision');
 const {tree}=render(run);assert.match(text(tree),/数据专家/);assert.match(text(tree),/内容设计师/);assert.match(text(tree),/等待人工决定/);assert.ok(!nodes(tree).some(node=>node.props?.role==='progressbar'));
});

test('uncreated plan items cannot open a fabricated task or imply authorization',()=>{
 const run={...base,status:'awaiting_approval',plan:{tasks:[plan('draft','a')]}};const {tree,calls}=render(run);const card=nodes(tree).find(node=>node.props?.className==='flow-card-title');
 assert.equal(card.props.disabled,true);card.props.onClick();assert.equal(calls.length,0);assert.match(text(tree),/尚未创建执行阶段/);assert.match(text(tree),/不是执行授权/);assert.match(text(tree),/仍待批准计划/);
});

test('real task cards and dependency links open only the exact supplied task',()=>{
 const a=task('a','a','integrated'),b=task('b','b','pending'),run={...base,plan:{tasks:[plan('a','a'),plan('b','b',['a'])]},tasks:[a,b]};const {tree,calls}=render(run);
 const cards=nodes(tree).filter(node=>node.props?.className==='flow-card-title');cards[1].props.onClick();assert.equal(calls[0],b);
 const dep=nodes(tree).find(node=>node.type==='button'&&node.props.className==='fulfilled');dep.props.onClick();assert.equal(calls[1],a);
});

test('submitted but unintegrated dependencies are never marked fulfilled',()=>{
 const run={...base,plan:{tasks:[plan('a','a'),plan('b','b',['a'])]},tasks:[task('a','a','done'),task('b','b')]};const {tree}=render(run);
 assert.match(text(tree),/待集成/);assert.match(text(tree),/依赖未全部集成/);assert.ok(!nodes(tree).some(node=>node.type==='button'&&node.props.className==='fulfilled'));
});

test('missing dependencies and cycles remain visibly unresolved and terminate deterministically',()=>{
 for(const entries of [[plan('a','a',['missing']),plan('b','b',['a'])],[plan('a','a',['b']),plan('b','b',['a'])]]){
  const run={...base,plan:{tasks:entries}},flow=helpers.buildTaskFlow(run);assert.equal(flow.unresolved.size,2);assert.ok(flow.nodes.every(node=>node.level===null));assert.match(text(render(run).tree),/依赖待检查/);assert.match(text(render(run).tree),/未推测顺序/);
 }
});

test('duplicate and missing plan keys produce warnings without fake extra stages',()=>{
 const flow=helpers.buildTaskFlow({...base,plan:{tasks:[plan('a','a'),plan('a','b'),{title:'missing'}]}});assert.equal(flow.nodes.length,1);assert.equal(flow.warnings.length,2);
});

test('real execution tasks without a saved plan still use their own dependency contracts',()=>{
 const flow=helpers.buildTaskFlow({...base,plan:undefined,tasks:[task('one','a','integrated','develop',{depends_on:[]}),task('two','b','running','develop',{depends_on:['one']})]});assert.deepEqual(Array.from(flow.nodes,node=>node.level),[0,1]);assert.equal(flow.nodes[1].task.key,'two');
});

test('discussion groups use actual rounds and strategy and do not fabricate code execution',()=>{
 const run={...base,plan:undefined,tasks:[task('discussion_2_1','b','running','discussion',{round:2,order:1,strategy:'parallel'}),task('discussion_1_0','a','done','discussion',{round:1,order:0,strategy:'sequential'}),task('goal_review_1','c','done','goal_review',{cycle:1}),task('discussion_2_0','a','done','discussion',{round:2,order:0,strategy:'parallel'})]};
 const flow=helpers.buildTaskFlow(run);assert.equal(flow.nodes.length,0);assert.deepEqual(Array.from(flow.groups,group=>group.key),['discussion:1','discussion:2']);assert.deepEqual(Array.from(flow.groups[1].tasks,item=>item.key),['discussion_2_0','discussion_2_1']);const content=text(render(run).tree);assert.match(content,/按并行讨论配置/);assert.match(content,/按顺序发言配置/);assert.doesNotMatch(content,/代码测试通过/);
});

test('rework, checks and review rounds preserve prior failure and do not depend on API array order',()=>{
 const run={...base,tasks:[task('review_1','c','running','review'),task('checks_0','c','failed','check'),task('rework_1','b','integrated'),task('review_0','c','done','review'),task('checks_1','c','done','check')]};const flow=helpers.buildTaskFlow(run);
 assert.equal(flow.nodes.length,0);assert.deepEqual(Array.from(flow.groups,group=>group.key),['validation:0','validation:1']);assert.deepEqual(Array.from(flow.groups[1].tasks,item=>item.key),['rework_1','checks_1','review_1']);assert.equal(flow.groups[0].tasks[0].status,'failed');assert.match(text(render(run).tree),/前一轮失败不会被后续结果覆盖/);
});

test('unknown kinds remain inspectable rather than being discarded as non-development work',()=>{
 const run={...base,tasks:[task('analysis','b','running','research')]};const flow=helpers.buildTaskFlow(run);assert.equal(flow.groups[0].key,'other');assert.equal(flow.groups[0].tasks[0].key,'analysis');assert.match(text(render(run).tree),/其他执行阶段/);
});

test('paused and stopped runs clarify that preserved member statuses are not continued execution',()=>{
 for(const status of ['paused','needs_attention','cancelled']){const content=text(render({...base,status,tasks:[task('work','a','running')]}).tree);assert.match(content,/不表示成员仍在继续执行/);}
});

test('large DAGs use a scrollable grid and collapsed completed owner groups, never a huge SVG canvas',()=>{
 const entries=Array.from({length:100},(_,i)=>plan('work'+i,'member'+(i%10),i>=10?['work'+(i-10)]:[]));const run={...base,plan:{tasks:entries},tasks:entries.map(item=>task(item.key,item.agent_id,'integrated'))};const {tree}=render(run);const flow=helpers.buildTaskFlow(run);
 assert.equal(flow.depth,10);assert.equal(flow.nodes.length,100);assert.equal(nodes(tree).filter(node=>node.props?.className==='flow-card-title').length,100);assert.ok(nodes(tree).filter(node=>node.props?.className==='flow-lane').every(node=>!node.props.open));assert.ok(nodes(tree).some(node=>node.props?.role==='region'&&node.props.tabIndex===0));assert.ok(!nodes(tree).some(node=>node.type==='svg'||node.type==='canvas'));
});

test('empty tasks show no simulated percentages, planned files or communication',()=>{
 const content=text(render().tree);assert.match(content,/尚未生成执行阶段/);assert.doesNotMatch(content,/\d+%/);const {tree}=render(base,[],'TaskOutputs');assert.match(text(tree),/不根据依赖箭头虚构成员对话/);assert.equal(nodes(tree).filter(node=>node.type==='a').length,0);
});

test('only successful write receipts identify files, categorized independently of agent permission role',()=>{
 const events=[event(1,'tool.started',{tool:'write_file',arguments:{path:'fake.pdf'}}),event(2,'tool.completed',{tool:'write_file',task_key:'doc',agent_id:'a',result:{written:'requirements.md',bytes:40}}),event(3,'tool.completed',{tool:'write_file',task_key:'data',agent_id:'b',result:{written:'analysis.csv',bytes:90}}),event(4,'tool.completed',{tool:'write_file',task_key:'asset',agent_id:'c',result:{written:'diagram.svg',bytes:120}}),event(5,'tool.completed',{tool:'write_file',task_key:'error',result:{error:'denied',written:'denied.py'}})];
 const evidence=helpers.taskOutputEvidence(base,events);assert.deepEqual(Array.from(evidence.files,file=>file.category),['文档','数据','素材']);const content=text(render(base,events,'TaskOutputs').tree);assert.match(content,/requirements.md/);assert.match(content,/analysis.csv/);assert.match(content,/diagram.svg/);assert.doesNotMatch(content,/fake.pdf|denied.py/);assert.match(content,/不是最终成果包的文件清单/);
});

test('latest file receipts apply per task working copy, including later deletes and writes',()=>{
 const receipt=(id,tool,key,result)=>event(id,'tool.completed',{tool,task_key:key,result});const events=[receipt(1,'write_file','one',{written:'report.md',bytes:4}),receipt(2,'write_file','two',{written:'report.md',bytes:9}),receipt(3,'delete_file','one',{deleted:'report.md'})];
 const evidence=helpers.taskOutputEvidence(base,events);assert.equal(evidence.files.length,2);assert.equal(evidence.files.find(file=>file.taskKey==='one').deleted,true);assert.equal(evidence.files.find(file=>file.taskKey==='two').deleted,false);
 events.push(receipt(4,'write_file','one',{written:'report.md',bytes:11}));assert.equal(helpers.taskOutputEvidence(base,events).files.find(file=>file.taskKey==='one').deleted,false);
});

test('cross-run events and duplicate event IDs never inflate file or command evidence',()=>{
 const own=event(1,'tool.completed',{tool:'write_file',task_key:'doc',result:{written:'ours.md',bytes:1}}),foreign=event(2,'tool.completed',{tool:'write_file',result:{written:'secret-other.md',bytes:1}},'other');
 const evidence=helpers.taskOutputEvidence(base,[own,own,foreign]);assert.equal(evidence.eventCount,1);assert.equal(evidence.files.length,1);assert.doesNotMatch(text(render(base,[own,foreign],'TaskOutputs').tree),/secret-other/);
});

test('sent and included messages remain distinct; included does not claim the recipient replied',()=>{
 const sent=id=>event(id,'agent.message',{message_id:'m'+id,from_agent_id:'a',to_agent_id:'b',body:'已准备好需求材料'});const events=[sent(1),sent(2),event(3,'agent.message.delivered',{message_id:'m1',from_agent_id:'a',to_agent_id:'b'})];
 const evidence=helpers.taskOutputEvidence(base,events);assert.equal(evidence.messages.length,2);assert.equal(evidence.messages.filter(message=>message.delivered).length,1);const content=text(render(base,events,'TaskOutputs').tree);assert.match(content,/当前窗口无送达回执/);assert.match(content,/不代表对方已完成处理或已回复/);
});

test('delivery-only event windows do not invent sending text',()=>{
 const events=[event(1,'agent.message.delivered',{message_id:'old',from_agent_id:'a',to_agent_id:'b'})];const evidence=helpers.taskOutputEvidence(base,events);assert.equal(evidence.messages[0].hasSend,false);assert.match(text(render(base,events,'TaskOutputs').tree),/当前事件窗口未包含发送正文/);
});

test('dependency edges are not represented as messages; actual handoff events are',()=>{
 const run={...base,plan:{tasks:[plan('a','a'),plan('b','b',['a'])]}};assert.equal(helpers.taskOutputEvidence(run).handoffs.length,0);const evidence=helpers.taskOutputEvidence(run,[event(1,'task.handoff',{from_agent_id:'a',to_agent_id:'b',summary:'文档已集成'})]);assert.equal(evidence.handoffs.length,1);assert.equal(evidence.messages.length,0);
});

test('non-code submissions are shown as member conclusions, not verified file creation',()=>{
 const delivered={...task('research','b','done','discussion'),result:{summary:'经营分析结论',notes:'样本不足，需要补充资料。'}};const pending={...task('draft','a','running'),result:{summary:'尚未提交的草案'}};const run={...base,tasks:[delivered,pending]};const {tree,calls}=render(run,[],'TaskOutputs');const content=text(tree);assert.match(content,/经营分析结论/);assert.match(content,/样本不足/);assert.doesNotMatch(content,/尚未提交的草案/);assert.match(content,/不代表没有产出文件/);nodes(tree).find(node=>node.props?.className==='flow-evidence-link').props.onClick();assert.equal(calls[0],delivered);
});

test('command exit codes and errors are evidence, never fabricated business success',()=>{
 const events=[event(1,'tool.completed',{tool:'run_command',task_key:'analysis',result:{exit_code:0,output:'computed'}}),event(2,'check.completed',{task_key:'checks_0',command:'check-report',exit_code:1,output:'missing evidence'}),event(3,'tool.completed',{tool:'run_approved_command',task_key:'external',result:{error:'not approved'}})];
 const evidence=helpers.taskOutputEvidence(base,events);assert.equal(evidence.commands.length,3);const content=text(render(base,events,'TaskOutputs').tree);assert.match(content,/退出码 0/);assert.match(content,/退出码 1/);assert.match(content,/工具返回错误/);assert.match(content,/命令返回不等于业务目标完成/);assert.doesNotMatch(content,/业务目标已完成/);
});

test('the evidence display exposes no download URL derived from arbitrary receipt paths',()=>{
 const events=[event(1,'tool.completed',{tool:'write_file',task_key:'doc',result:{written:'../../outside.md',bytes:5}})];const {tree}=render(base,events,'TaskOutputs');assert.equal(nodes(tree).filter(node=>node.type==='a').length,0);assert.match(text(tree),/\.\.\/\.\.\/outside.md/);
});

test('compact branch cards retain a visible owner and collapse the full acceptance contract',()=>{
 const run={...base,plan:{tasks:[plan('report','b')]},tasks:[task('report','b','running')]};const {tree,calls}=render(run);
 const card=nodes(tree).find(node=>node.props?.['data-task-key']==='report');
 assert.match(text(nodes(card).find(node=>node.props?.className==='flow-card-owner')),/负责人数据专家/);
 const acceptance=nodes(card).find(node=>node.props?.className==='flow-acceptance');
 assert.equal(acceptance.type,'details');assert.ok(!acceptance.props.open);assert.match(text(acceptance),/交付 report 的真实成果/);
 const title=nodes(card).find(node=>node.props?.className==='flow-card-title');title.props.onClick();assert.equal(calls[0],run.tasks[0]);
 assert.match(text(card),/执行中/);
});

test('reading guidance is collapsed but real authorization and dependency warnings stay outside it',()=>{
 const run={...base,status:'awaiting_approval',plan:{tasks:[plan('report','b',['missing'])]}};const {tree}=render(run);
 const guide=nodes(tree).find(node=>node.props?.className==='flow-reading-guide');assert.equal(guide.type,'details');assert.ok(!guide.props.open);
 assert.match(text(guide),/不表示同时开工/);assert.match(text(guide),/并行上限为 3/);
 const notices=nodes(tree).filter(node=>node.props?.className==='flow-notice');assert.match(text(notices),/不是执行授权/);assert.match(text(notices),/不会把它们显示为可直接执行/);
 assert.match(text(nodes(tree).filter(node=>node.props?.className==='flow-dependencies')),/依赖未全部集成/);
});

test('stage conclusions disclose complete member evidence without nesting controls or changing task targets',()=>{
 const stage={...task('discussion_1_0','b','done','discussion',{round:1}),result:{summary:'完整经营分析结论\n仍需核对样本范围。'}};
 const {tree,calls}=render({...base,tasks:[stage]});const card=nodes(tree).find(node=>node.props?.className==='flow-stage-card done');
 assert.equal(card.type,'article');const opener=nodes(card).find(node=>node.props?.className==='flow-stage-open');
 assert.match(text(opener),/已提交/);assert.match(text(opener),/数据专家/);assert.equal(nodes(opener).filter(node=>node.type==='details').length,0);
 opener.props.onClick();assert.equal(calls[0],stage);
 const details=nodes(card).find(node=>node.type==='details');assert.ok(!details.props.open);assert.match(text(details),/完整经营分析结论/);assert.match(text(details),/不等于文件或外部操作已验证/);
});

test('output disclosures do not hide the scope boundary beside file receipts',()=>{
 const {tree}=render(base,[event(1,'tool.completed',{tool:'write_file',task_key:'doc',agent_id:'a',result:{written:'report.md',bytes:9}})],'TaskOutputs');
 const guide=nodes(tree).find(node=>node.props?.className==='flow-reading-guide');assert.ok(!guide.props.open);assert.match(text(guide),/不是同一层证据/);
 const grid=nodes(tree).find(node=>node.props?.className==='flow-output-grid');
 const boundary=nodes(grid).find(node=>node.props?.className==='flow-evidence-boundary');assert.equal(boundary.type,'p');assert.match(text(boundary),/不能证明当前文件仍存在/);
 assert.match(text(tree),/已加载的 1 条本任务事件/);
});
