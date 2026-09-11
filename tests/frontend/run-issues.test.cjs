const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const ts=require('../../frontend/vendor/typescript.cjs'),exportsObject={};
const jsx=(type,props)=>({type,props});
vm.runInNewContext(ts.transpileModule(fs.readFileSync('frontend/src/components/RunIssues.tsx','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:exportsObject,require:()=>({jsx,jsxs:jsx})});
function text(n){if(n==null||typeof n==='boolean')return '';if(Array.isArray(n))return n.map(text).join(' ');if(typeof n==='object')return text(n.props?.children);return String(n);}
function nodes(n){if(Array.isArray(n))return n.flatMap(nodes);if(n&&typeof n==='object')return [n,...nodes(n.props?.children)];return [];}
const issue={task_key:'failed-work',kind:'integration_conflict',source_agent_id:'tester',source_agent_name:'Echo',status:'open',summary:'无法合并需求文档',files:['docs/requirements.md'],attempts:1,max_attempts:3};
const fixture={id:'r',status:'running',snapshot:{entry_agent_id:'lead',agents:{lead:{name:'Atlas'}}},tasks:[{result:{issue_summary:issue}}]};
test('active recovery names the real member, file and coordinator without asking the user prematurely',()=>{
 const output=text(exportsObject.default({run:fixture}));assert.match(output,/负责人正在尝试解决/);assert.match(output,/Echo/);assert.match(output,/Atlas/);assert.match(output,/docs\/requirements.md/);assert.match(output,/1.*3.*次/);assert.doesNotMatch(output,/需要你协助处理/);
});
test('escalation gives exact reason, and stopped or paused runs never appear to keep recovering',()=>{
 let output=text(exportsObject.default({run:{...fixture,status:'needs_attention',tasks:[{result:{issue_summary:{...issue,status:'escalated',escalation_reason:'三次修复仍未通过测试'}}}]}}));assert.match(output,/三次修复仍未通过测试/);assert.match(output,/需要你协助/);
 output=text(exportsObject.default({run:{...fixture,status:'paused'}}));assert.match(output,/修复已暂停/);assert.doesNotMatch(output,/负责人正在尝试解决/);
 output=text(exportsObject.default({run:{...fixture,status:'needs_attention'}}));assert.doesNotMatch(output,/负责人正在尝试解决/);
});
test('resolved evidence is retained without claiming task completion; history without issues gets no invented metadata',()=>{
 const output=text(exportsObject.default({run:{...fixture,tasks:[{result:{issue_summary:{...issue,status:'resolved',resolution:'已合并并验证',evidence_task_keys:['repair-1']}}}]}}));assert.match(output,/已解决.*1.*个问题/);assert.match(output,/repair-1/);assert.doesNotMatch(output,/任务已完成/);
 assert.equal(exportsObject.default({run:{...fixture,tasks:[{result:{}}]}}),null);
});

test('member failure makes coordinator ownership and uncertainty explicit; only escalated issues offer a new run',()=>{
 const failed={...issue,kind:'member_failed',summary:'成员调用工具失败',uncertain:true,status:'open'};
 let inspected=0,created=0;
 let output=text(exportsObject.default({run:{...fixture,tasks:[{result:{issue_summary:failed}}]},onInspect(){inspected++},onNewTask(){created++}}));
 assert.match(output,/负责人正在分析失败并安排修复/);
 assert.match(output,/协调权限/);
 assert.match(output,/结果未确认/);
 assert.doesNotMatch(output,/按剩余工作新建任务/);
 const escalated={...failed,status:'escalated',escalation_reason:'负责人尝试后仍无法安全继续'};
 const tree=exportsObject.default({run:{...fixture,status:'needs_attention',tasks:[{result:{issue_summary:escalated}}]},onInspect(){inspected++},onNewTask(){created++}});
 const buttons=nodes(tree).filter(n=>n.type==='button');
 assert.ok(buttons.some(n=>text(n).includes('查看负责人处理记录')));
 assert.ok(buttons.some(n=>text(n).includes('按剩余工作新建任务')));
 buttons.find(n=>text(n).includes('查看负责人处理记录')).props.onClick();
 buttons.find(n=>text(n).includes('按剩余工作新建任务')).props.onClick();
 assert.equal(inspected,1);assert.equal(created,1);
});
