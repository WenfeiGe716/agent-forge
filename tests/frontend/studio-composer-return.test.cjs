const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const {createRequire}=require('node:module');
// Use the same production Studio render harness as the organization/team tests.
// Its API stub fails on any call; these interactions must never submit a task.
const filename=require.resolve('./organization-workspace.test.cjs'),local=createRequire(filename),studio={};
vm.runInNewContext(fs.readFileSync(filename,'utf8')+'\nObject.assign(exports,{harness,nodes,text,field,button});',{exports:studio,__dirname:path.dirname(filename),console,performance,Date,setImmediate,URL,require:Object.assign(id=>id==='node:test'?()=>{}:local(id),{resolve:local.resolve})},{filename});
const people=[{id:'a',name:'需求分析师',role:'developer',job_title:'需求分析',runtime:{},enabled:true},{id:'b',name:'财务分析师',role:'reviewer',job_title:'财务分析',runtime:{},enabled:true}];
const organization={ref:{id:'office',version:3,digest:'a'.repeat(64)},config:{floors:[{floor:1,department:'研究部',name:'研究楼层',zones:[]}],positions:{},teams:[{id:'team-a',name:'需求团队',floor:1,member_ids:['a'],headcount:1},{id:'team-b',name:'财务团队',floor:1,member_ids:['b'],headcount:1}]}};
const projects=[{id:'first-project',name:'项目甲'},{id:'selected-project',name:'项目乙'}];
const find=(tree,label)=>studio.nodes(tree).find(node=>node.props?.['aria-label']===label);
const hasComposer=tree=>!!find(tree,'任务需求');
const returnEntry=tree=>studio.nodes(tree).find(node=>node.type==='button'&&node.props?.className?.split(' ').includes('studio-composer-entry'));
// The activity dock also has a boolean visibility effect. Run both, as React
// does, instead of mistaking the first boolean effect for composer focus.
function focusEffect(h){const effects=h.effects.filter(effect=>effect.deps?.length===1&&typeof effect.deps[0]==='boolean');assert.ok(effects.length,'visibility effects');return()=>effects.forEach(effect=>effect.fn());}
function fixture(extra={}){const seeds=[];return{seeds,h:studio.harness('StudioWorkspace',{agents:people,organization,projects,runs:[],onCreateTask:seed=>seeds.push(seed),...extra})};}
function selectEmployee(tree,id='a'){const office=studio.nodes(tree).find(node=>node.type==='Office');assert.ok(office);office.props.onSelect(id);}

test('opening an employee task inspector hides the composer and returning preserves the entire unsubmitted team draft',()=>{
 const {h,seeds}=fixture();let tree=h.render();
 studio.field(tree,'任务项目').props.onChange({target:{value:'selected-project'}});
 studio.field(tree,'需求办公团队').props.onChange({target:{value:'team-b'}});tree=h.render();
 studio.field(tree,'指令收件人').props.onChange({target:{value:'b'}});
 const draft='形成经营分析报告\n保留原始数据，交付报告与可复核依据。';
 studio.field(tree,'任务需求').props.onChange({target:{value:draft}});tree=h.render();
 selectEmployee(tree,'a');tree=h.render();
 assert.equal(hasComposer(tree),false,'inspector must not be covered by the editable composer');
 assert.ok(studio.nodes(tree).some(node=>node.props?.className?.includes('task-inspector')));
 const back=returnEntry(tree);assert.ok(back);assert.equal(studio.text(back),'返回未提交的需求');assert.equal(back.props['aria-label'],undefined,'the visible action is its accessible name');assert.equal(seeds.length,0);
 back.props.onClick();tree=h.render();
 assert.equal(studio.field(tree,'任务需求').props.value,draft);
 assert.equal(studio.field(tree,'任务项目').props.value,'selected-project');
 assert.equal(studio.field(tree,'需求办公团队').props.value,'team-b');
 assert.equal(studio.field(tree,'指令收件人').props.value,'b');
 assert.equal(studio.field(tree,'指令类型').props.value,'new');
 assert.ok(!studio.nodes(tree).some(node=>node.props?.className?.includes('task-inspector')));
 assert.equal(returnEntry(tree),undefined);assert.equal(seeds.length,0);
});

test('an empty composer offers a neutral return action without creating a task',()=>{
 const {h,seeds}=fixture();let tree=h.render();selectEmployee(tree);tree=h.render();assert.equal(hasComposer(tree),false);
 const back=returnEntry(tree);assert.ok(back);assert.equal(studio.text(back),'发起需求 / 补充说明');assert.equal(back.props['aria-label'],undefined,'voice control and screen readers use the visible action');back.props.onClick();tree=h.render();
 assert.equal(studio.field(tree,'任务需求').props.value,'');assert.equal(seeds.length,0);
});

test('a supplemental instruction preserves its task recipient and mode while inspecting another member',()=>{
 const {h,seeds}=fixture({selectedRun:'run',detail:{id:'run',status:'running',snapshot:{agents:{a:{},b:{}}}}});let tree=h.render();
 studio.field(tree,'任务项目').props.onChange({target:{value:'selected-project'}});studio.field(tree,'指令类型').props.onChange({target:{value:'append'}});tree=h.render();
 studio.field(tree,'指令收件人').props.onChange({target:{value:'b'}});studio.field(tree,'任务需求').props.onChange({target:{value:'只补充已确认的分析口径，不扩张范围。'}});tree=h.render();
 selectEmployee(tree,'a');tree=h.render();assert.equal(hasComposer(tree),false);returnEntry(tree).props.onClick();tree=h.render();
 assert.equal(studio.field(tree,'指令类型').props.value,'append');assert.equal(studio.field(tree,'指令收件人').props.value,'b');assert.equal(studio.field(tree,'任务项目').props.value,'selected-project');assert.equal(studio.field(tree,'任务需求').props.value,'只补充已确认的分析口径，不扩张范围。');assert.equal(seeds.length,0);
});

test('without an actual Agent, a scene selection cannot hide the only demand input',()=>{
 const {h,seeds}=fixture({agents:[]});let tree=h.render();studio.field(tree,'任务需求').props.onChange({target:{value:'先配置团队再执行'}});tree=h.render();
 selectEmployee(tree,'missing-agent');tree=h.render();assert.equal(hasComposer(tree),true);assert.equal(studio.field(tree,'任务需求').props.value,'先配置团队再执行');assert.equal(returnEntry(tree),undefined);assert.equal(seeds.length,0);
});

test('returning to demand input restores focus exactly once after the textarea remounts',()=>{
 const {h,seeds}=fixture();let tree=h.render(),focusCount=0;
 const inputRef=studio.field(tree,'任务需求').props.ref;assert.ok(inputRef);
 const input={focus(){focusCount++;}};inputRef.current=input;focusEffect(h)();
 assert.equal(focusCount,0,'initial render must not steal focus');
 selectEmployee(tree);tree=h.render();inputRef.current=null;focusEffect(h)();assert.equal(focusCount,0);
 returnEntry(tree).props.onClick();assert.equal(focusCount,0,'do not focus the unmounted textarea from the click handler');
 tree=h.render();assert.equal(studio.field(tree,'任务需求').props.ref,inputRef);inputRef.current=input;
 const restoreFocus=focusEffect(h);restoreFocus();assert.equal(focusCount,1);
 restoreFocus();assert.equal(focusCount,1,'effect replay must not focus twice');
 h.props.runs=[{id:'parallel-run',title:'后台轮询新增任务',status:'running'}];tree=h.render();focusEffect(h)();
 studio.field(tree,'搜索成员').props.onChange({target:{value:'财务'}});tree=h.render();focusEffect(h)();
 assert.equal(focusCount,1,'polling and unrelated renders must not reclaim keyboard focus');assert.equal(seeds.length,0);
});

test('closing the employee inspector without the return action does not request input focus',()=>{
 const {h,seeds}=fixture();let tree=h.render(),focusCount=0;
 const inputRef=studio.field(tree,'任务需求').props.ref;inputRef.current={focus(){focusCount++;}};
 selectEmployee(tree);tree=h.render();inputRef.current=null;focusEffect(h)();
 studio.field(tree,'关闭成员详情').props.onClick();tree=h.render();inputRef.current={focus(){focusCount++;}};focusEffect(h)();
 assert.equal(hasComposer(tree),true);assert.equal(focusCount,0,'only the explicit return-to-composer action requests focus');assert.equal(seeds.length,0);
});
