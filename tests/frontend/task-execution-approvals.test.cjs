const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const {createRequire}=require('node:module');
const filename=require.resolve('./execution-center.test.cjs'),local=createRequire(filename),helpers={};
vm.runInNewContext(fs.readFileSync(filename,'utf8')+'\nObject.assign(exports,{harness,nodes,text,button,field,installation,permission});',{exports:helpers,__dirname:path.dirname(filename),console,Date,URL,setImmediate,require:Object.assign(id=>id==='node:test'?()=>{}:local(id),{resolve:local.resolve})},{filename});
const {harness,nodes,text,button,field,installation,permission}=helpers;
const props={compact:true,initialRunId:'r',agentNames:{a:'需求分析师'}};

test('employee-scoped permissions hide another member requests within the same run',async()=>{
 const h=harness('ExecutionCenter',{props:{...props,agentId:'other'}});h.render();const tree=await h.settle();
 assert.ok(!nodes(tree).some(n=>n.type==='article'));assert.match(text(tree),/当前没有需要处理/);assert.ok(h.calls.every(c=>c.method==='GET'));
});

test('switching employee invalidates an already captured permission action',async()=>{
 const h=harness('ExecutionCenter',{props:{...props,agentId:'a'}});h.render();let tree=await h.settle();
 button(tree,'审查后批准').props.onClick();tree=h.render();field(tree,'确认已复核精确命令与权限').props.onChange({target:{checked:true}});tree=h.render();const stale=button(tree,'确认批准本次申请').props.onClick;
 tree=h.setProps({agentId:'another'});await stale();assert.ok(!nodes(tree).some(n=>n.props?.['aria-label']==='确认申请审核'));assert.ok(h.calls.every(c=>c.method==='GET'));
});

test('compact approval locks to one run and skips readiness, packages, and tool discovery',async()=>{
 const h=harness('ExecutionCenter',{props});h.render();const tree=await h.settle();
 assert.deepEqual(Array.from(h.calls,c=>c.url).sort(),['/execution/permissions?run_id=r','/tool-installations?run_id=r']);
 assert.ok(h.calls.every(c=>c.method==='GET'));
 assert.match(text(tree),/需求分析师/);assert.match(text(tree),/需要统计文本/);assert.match(text(tree),/批准后才由成员执行/);
 assert.ok(!nodes(tree).some(n=>n.type==='select'));
 assert.doesNotMatch(text(tree),/执行预检结果|配置检查通过|内置离线工具示例/);
});

test('compact mode without run identity never reads a global or implicitly chosen run',async()=>{
 const h=harness('ExecutionCenter',{props:{...props,initialRunId:''}});h.render();const tree=await h.settle();
 assert.equal(h.calls.length,0);assert.match(text(tree),/选择一个任务后查看申请/);
 assert.equal(button(tree,'刷新申请').props.disabled,true);
});

test('pending requests remain first while already handled requests are collapsed',async()=>{
 const approved={...installation,id:'approved',status:'installed',reason:'此前已安装的工具'};
 const h=harness('ExecutionCenter',{props,handler:(url,method)=>method==='GET'&&url.startsWith('/tool-installations')?{items:[approved,installation]}:undefined});
 h.render();const tree=await h.settle();
 const history=nodes(tree).find(n=>n.type==='details'&&n.props.className==='execution-history');
 assert.ok(history);assert.notEqual(history.props.open,true);assert.match(text(history),/此前已安装的工具/);
 const all=nodes(tree);assert.ok(all.findIndex(n=>n.type==='article'&&n.props.className.includes('pending'))<all.indexOf(history));
 assert.ok(nodes(tree).some(n=>n.type==='details'&&text(n).includes('查看安装内容、源码与范围')&&n.props.open!==true));
});

test('compact review still requires acknowledgement and posts the exact dedicated permission digest',async()=>{
 const h=harness('ExecutionCenter',{props});h.render();let tree=await h.settle();
 button(tree,'审查后批准').props.onClick();tree=h.render();
 assert.equal(button(tree,'确认批准本次申请').props.disabled,true);
 assert.match(text(field(tree,'确认申请审核')),/python3 \/reviewed\/tool.py/);
 field(tree,'确认已复核精确命令与权限').props.onChange({target:{checked:true}});tree=h.render();
 await button(tree,'确认批准本次申请').props.onClick();await h.settle();
 const writes=Array.from(h.calls).filter(c=>c.method==='POST');assert.equal(writes.length,1);
 assert.equal(writes[0].url,'/execution/permissions/request/review');
 assert.equal(writes[0].body.digest,permission.digest);assert.equal(writes[0].body.approved,true);
 assert.ok(!h.calls.some(c=>/questions|install_approved|run_approved/.test(c.url)));
});

test('compact scope switches hide old requests immediately and discard late responses',async()=>{
 const responses=[];
 const h=harness('ExecutionCenter',{props,handler:(url,method)=>method==='GET'?new Promise(resolve=>responses.push({url,resolve})):undefined});
 h.render();responses[0].resolve({items:[installation]});responses[1].resolve({items:[permission]});await h.settle();
 const switched=h.setProps({initialRunId:'other'});
 assert.doesNotMatch(text(switched),/需要统计文本|需要已审核工具/);
 let tree=await h.settle();assert.ok(!nodes(tree).some(n=>n.type==='article'));
 responses[2].resolve({items:[installation]});responses[3].resolve({items:[permission]});tree=await h.settle();
 assert.ok(!nodes(tree).some(n=>n.type==='article'),'server rows for the wrong run must not be displayed');
});

test('compact non-admin approval view has no granting controls',async()=>{
 const h=harness('ExecutionCenter',{props:{...props,admin:false}});h.render();const tree=await h.settle();
 assert.match(text(tree),/下一步需要管理员审查/);
 assert.ok(!nodes(tree).some(n=>n.type==='button'&&/批准|拒绝|复用/.test(text(n))));
 assert.ok(h.calls.every(c=>c.method==='GET'));
});

test('switching run invalidates a previously captured approval action',async()=>{
 const h=harness('ExecutionCenter',{props});h.render();let tree=await h.settle();
 button(tree,'审查后批准').props.onClick();tree=h.render();
 field(tree,'确认已复核精确命令与权限').props.onChange({target:{checked:true}});tree=h.render();
 const staleSubmit=button(tree,'确认批准本次申请').props.onClick;
 tree=h.setProps({initialRunId:'other'});await staleSubmit();
 assert.ok(!nodes(tree).some(n=>n.props?.['aria-label']==='确认申请审核'));
 assert.ok(h.calls.every(c=>c.method==='GET'));
});

test('event refresh keeps visible requests while loading and never interrupts an open review',async()=>{
 const pending=[];let defer=false;
 const h=harness('ExecutionCenter',{props:{...props,refreshKey:1},handler:(url,method)=>defer&&method==='GET'?new Promise(resolve=>pending.push({url,resolve})):undefined});
 h.render();let tree=await h.settle();
 button(tree,'审查后批准').props.onClick();tree=h.render();
 field(tree,'申请审核备注').props.onChange({target:{value:'只批准本次命令'}});
 field(tree,'确认已复核精确命令与权限').props.onChange({target:{checked:true}});
 tree=h.setProps({refreshKey:2});tree=await h.settle();
 assert.equal(h.calls.length,2,'new events must not restart an active review');
 assert.equal(field(tree,'申请审核备注').props.value,'只批准本次命令');
 assert.equal(field(tree,'确认已复核精确命令与权限').props.checked,true);
 defer=true;button(tree,'取消审核').props.onClick();h.render();tree=await h.settle();tree=h.render();
 assert.equal(h.calls.length,4,'deferred event refresh resumes after review closes');
 assert.match(text(tree),/需要统计文本/);assert.match(text(tree),/正在读取本任务申请/);
 for(const request of pending)request.resolve({items:[]});tree=await h.settle();
 assert.doesNotMatch(text(tree),/需要统计文本/);assert.match(text(tree),/当前没有需要处理/);
 assert.ok(h.calls.every(c=>c.method==='GET'));
});
