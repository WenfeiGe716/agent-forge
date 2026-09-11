const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const {createRequire}=require('node:module');

// Reuse production-component harnesses without registering their existing tests.
// Studio's API stub rejects all calls; employee fixtures permit GETs only.
function reuse(filename,names){
 const resolved=require.resolve(filename),local=createRequire(resolved),exports={};
 vm.runInNewContext(fs.readFileSync(resolved,'utf8')+'\nObject.assign(exports,{'+names.join(',')+'});',{
  exports,__dirname:path.dirname(resolved),console,performance,Date,setImmediate,URL,
  require:Object.assign(id=>id==='node:test'?()=>{}:local(id),{resolve:local.resolve})
 },{filename:resolved});
 return exports;
}
const studio=reuse('./organization-workspace.test.cjs',['harness','nodes','text','field','button']);
const employee=reuse('./employee-task-panel.test.cjs',['harness','nodes','text','button','run']);
const classNode=(tree,name,helper=studio)=>helper.nodes(tree).find(node=>node.props?.className?.split(' ').includes(name));

test('Studio primary navigation stays limited to office, tasks, departments and teams with exact targets',()=>{
 const opened=[],h=studio.harness('StudioWorkspace',{onBoard:()=>opened.push('task-workspace')}),tree=h.render();
 const nav=studio.field(tree,'工作台导航');
 const top=(Array.isArray(nav.props.children)?nav.props.children:[nav.props.children]).filter(node=>node?.type==='button');
 assert.deepEqual(Array.from(top,studio.text),['办公室','任务','楼层与部门','团队']);
 assert.equal(top[0].props.className,'selected');
 studio.button(nav,'任务').props.onClick();studio.button(nav,'楼层与部门').props.onClick();studio.button(nav,'团队').props.onClick();
 assert.deepEqual(opened,['task-workspace']);assert.deepEqual(Array.from(h.navigation),['organization','agents']);
});

test('the collapsed management disclosure retains configuration targets but removes fixed orchestration',()=>{
 const h=studio.harness('StudioWorkspace'),tree=h.render(),menu=classNode(tree,'studio-nav-more');
 assert.equal(menu.type,'details');assert.ok(!menu.props.open);
 assert.equal(studio.text(studio.nodes(menu).find(node=>node.type==='summary')),'管理与配置');
 const entries=[['设计中心','design'],['执行中心','execution'],['任务看板','board'],['角色模板','role-templates'],['Skill商店','skill-store'],['项目','projects'],['模型','models']];
 assert.ok(!studio.nodes(menu).some(n=>n.type==='button'&&studio.text(n)==='编排'));
 for(const [label] of entries)studio.button(menu,label).props.onClick();
 assert.deepEqual(Array.from(h.navigation),entries.map(([,target])=>target));
 studio.field(tree,'运行设置').props.onClick();assert.equal(h.navigation.at(-1),'settings');
});

test('scene disclosure preserves camera, quality, day-night and city controls without task actions',()=>{
 const seeds=[],actions=[],h=studio.harness('StudioWorkspace',{onCreateTask:seed=>seeds.push(seed)});let tree=h.render();
 const office=studio.nodes(tree).find(node=>node.type==='Office');
 office.props.controller.current={zoom:n=>actions.push(['zoom',n]),reset:()=>actions.push(['reset']),cityView:()=>actions.push(['city']),intersectionView:()=>actions.push(['intersection'])};
 const tools=classNode(tree,'studio-scene-tools');assert.equal(tools.type,'details');assert.ok(!tools.props.open);
 assert.equal(studio.text(studio.nodes(tools).find(node=>node.type==='summary')),'场景与环境');
 assert.equal(typeof studio.field(tools,'切换日夜').props.onClick,'function');
 studio.field(tools,'缩小场景').props.onClick();studio.field(tools,'放大场景').props.onClick();studio.field(tools,'恢复全景').props.onClick();
 const quality=studio.field(tools,'3D画质');assert.deepEqual(Array.from(studio.nodes(quality).filter(node=>node.type==='option'),node=>node.props.value),['detail','balanced','economy']);
 quality.props.onChange({target:{value:'economy'}});tree=h.render();assert.equal(studio.nodes(tree).find(node=>node.type==='Office').props.quality,'economy');
 studio.field(tree,'城市全景').props.onClick();tree=h.render();assert.match(classNode(tree,'studio-workspace').props.className,/city-overview/);
 studio.field(tree,'路口观察').props.onClick();studio.field(tree,'返回办公区').props.onClick();tree=h.render();assert.doesNotMatch(classNode(tree,'studio-workspace').props.className,/city-overview/);
 studio.field(tree,'环境设置').props.onClick();tree=h.render();assert.equal(studio.field(tree,'常驻环境设置').props.role,'group');
 assert.equal(studio.field(tree,'城市人车密度').type,'select');assert.equal(studio.field(tree,'切换环境运动').type,'button');
 assert.deepEqual(actions,[['zoom',.85],['zoom',1.15],['reset'],['city'],['intersection'],['reset']]);assert.equal(seeds.length,0);assert.equal(h.navigation.length,0);
});

test('an employee with no real assigned task shows an empty state, not invented metrics or progress',()=>{
 const h=employee.harness({propPatch:{runs:[],detail:null}}),tree=h.render();
 assert.match(employee.text(tree),/等待新的工作/);assert.match(employee.text(tree),/没有已分配的可见任务/);
 assert.equal(classNode(tree,'employee-task-metrics',employee),undefined);
 assert.equal(employee.nodes(tree).filter(node=>node.props?.role==='progressbar').length,0);assert.doesNotMatch(employee.text(tree),/\d+%/);
 assert.equal(h.calls.length,0);
});

test('employee metrics count actual selected-run stages, scoped communication receipts and this member pending items',async()=>{
 const base=employee.run;
 const current={...base,tasks:[
  {id:'a-one',key:'a-one',agent_id:'a',title:'本人的已交付阶段',status:'integrated',spec:{},result:{}},
  {id:'a-two',key:'a-two',agent_id:'a',title:'本人的等待阶段',status:'waiting_decision',spec:{},result:{}},
  {id:'b-one',key:'b-one',agent_id:'b',title:'他人的已交付阶段',status:'done',spec:{},result:{}}
 ]};
 const foreign={...current,id:'run-b',title:'另一个并行任务',updated_at:'2026-09-11',tasks:[{id:'foreign',agent_id:'a',status:'done'}]};
 const trace=[
  {id:1,run_id:'run-a',kind:'agent.message',created_at:'2026-09-11T00:00:00Z',data:{from_agent_id:'a',to_agent_id:'b',body:'当前任务通信'}},
  {id:2,run_id:'run-a',kind:'agent.message.delivered',created_at:'2026-09-11T00:00:01Z',data:{from_agent_id:'a',to_agent_id:'b'}},
  {id:3,run_id:'run-b',kind:'agent.message',data:{from_agent_id:'a',to_agent_id:'b',body:'其他任务通信不得出现'}},
  {id:4,run_id:'run-a',kind:'tool.completed',data:{agent_id:'a',tool:'read_file'}}
 ];
 const h=employee.harness({propPatch:{runs:[foreign,current],initialRunId:'run-a'},handler:(url,method)=>{
  assert.equal(method,'GET');
  if(url==='/runs/run-a/collaboration')return{run_id:'run-a',decisions:[
   {id:'mine',run_id:'run-a',agent_id:'a',status:'pending',kind:'question',digest:'exact'},
   {id:'answered',run_id:'run-a',agent_id:'a',status:'answered',kind:'question'},
   {id:'other-member',run_id:'run-a',agent_id:'b',status:'pending',kind:'question'},
   {id:'other-run',run_id:'run-b',agent_id:'a',status:'pending',kind:'question'}
  ]};
  if(url.startsWith('/runs/run-a/trace?')){const query=new URL(url,'http://test.invalid').searchParams;assert.equal(query.get('agent_id'),'a');assert.equal(query.get('communication_only'),'true');return{items:trace,has_more:false};}
  assert.equal(url,'/runs/run-a');return current;
 }});
 h.render();let tree=await h.settle();const metrics=classNode(tree,'employee-task-metrics',employee);assert.ok(metrics);
 assert.deepEqual(Array.from(metrics.props.children,node=>employee.text(node)),['1 / 2阶段已提交','2通信记录','1待处理']);
 assert.doesNotMatch(employee.text(tree),/他人的已交付阶段|其他任务通信不得出现/);
 metrics.props.children[1].props.onClick();tree=h.render();assert.match(employee.text(tree),/当前任务通信/);assert.doesNotMatch(employee.text(tree),/其他任务通信不得出现/);
 classNode(tree,'employee-task-metrics',employee).props.children[2].props.onClick();tree=h.render();
 assert.deepEqual(Array.from(employee.nodes(tree).filter(node=>node.type==='question'),node=>node.props.q.id),['mine']);
 const permissions=employee.nodes(tree).find(node=>node.type==='permissions');assert.equal(permissions.props.initialRunId,'run-a');assert.equal(permissions.props.agentId,'a');
 assert.equal(h.calls.length,3);assert.ok(h.calls.every(call=>call.method==='GET'));assert.equal(h.opened.length,0);
});

test('the personal pending metric includes plan approval only for the frozen responsible member',async()=>{
 for(const planner of ['a','b']){
  const h=employee.harness({runPatch:{status:'awaiting_approval',snapshot:{...employee.run.snapshot,planner_id:planner}}});h.render();const tree=await h.settle();
  const pending=classNode(tree,'employee-task-metrics',employee).props.children[2];
  assert.equal(employee.text(pending),(planner==='a'?'1':'0')+'待处理');assert.ok(h.calls.every(call=>call.method==='GET'));
 }
});
