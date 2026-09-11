import {Run,Task} from './types';

export type TaskTab='progress'|'actions'|'results'|'activity';
/** Display aliases only; unknown runtime tools remain visible under their real names. */
export function taskToolLabel(value:unknown){const name=typeof value==='string'?value:'';return ({inspect_team:'查看团队能力目录',read_work_result:'读取团队已集成成果'} as Record<string,string>)[name]||name;}
export function isAutonomous(run:Run){return run.mode==='autonomous'||run.execution_strategy==='autonomous'||run.snapshot?.execution_strategy==='autonomous'||run.report?.mode==='autonomous';}
export function isDiscussion(run:Run){return !isAutonomous(run)&&(run.report?.mode==='discussion'||run.snapshot?.orchestration?.mode==='discussion');}
export function primaryTab(run:Run):TaskTab{return ['awaiting_approval','awaiting_decision'].includes(run.status)?'actions':run.artifact_digest?'results':'progress';}
export function taskStage(task:Task,discussion=false){
 if(discussion)return task.kind==='intake'?0:task.kind==='action_plan'?2:1;
 return task.kind==='plan'||task.kind==='intake'||task.kind==='discussion'?0:task.kind==='review'||task.kind==='check'||task.key.startsWith('review_')||task.key.startsWith('checks_')?3:2;
}
export function runPresentation(run:Run){
 const autonomous=isAutonomous(run),discussion=isDiscussion(run),tasks=run.tasks||[];
 const labels=autonomous?['接收需求','自主协作','整理交付','人工验收']:discussion?['理解需求','成员讨论','整理成果','人工验收']:['制定计划','确认计划','执行任务','测试与审查','人工验收'];
 const direct:Record<string,number>=autonomous?{queued:0,intake:0,planning:1,discussing:1,running:1,reworking:1,verifying:2,reviewing:2,awaiting_acceptance:3,completed:4}:discussion?{queued:0,intake:0,planning:2,discussing:1,awaiting_acceptance:3,completed:4}:{queued:0,intake:0,planning:0,discussing:0,awaiting_approval:1,running:2,reworking:2,verifying:3,reviewing:3,awaiting_acceptance:4,completed:5};
 const focus=[...tasks].reverse().find(t=>['waiting_decision','running','failed','needs_attention'].includes(t.status));
 let index=direct[run.status]??(focus?(autonomous?1:taskStage(focus,discussion)):run.artifact_digest?labels.length-1:run.plan_digest?1:0);
 if(!Number.isFinite(index))index=0;
 const attention=['awaiting_approval','awaiting_decision','awaiting_acceptance'].includes(run.status);
 const blocked=['paused','needs_attention','cancelled'].includes(run.status);
 const copy:Record<string,[string,string,TaskTab,string]>={
  queued:['等待启动','任务已保存，正在等待编排器接手。','progress','查看任务安排'],
  intake:['负责人正在理解需求','如需澄清，问题会集中出现在“待我处理”。','progress','查看执行进度'],
  planning:['正在制定执行计划','计划生成后由你确认，确认前不会开始开发。','progress','查看执行进度'],
  discussing:['成员正在讨论','展示实际发言和阶段结果；讨论不代表已经执行代码。','progress','查看成员进度'],
  awaiting_approval:['请确认这份执行计划','核对分工与验收要求，批准后团队才会开始执行。','actions','审阅执行计划'],
  running:['团队正在执行任务','成员在本次运行的工作副本中处理任务，成果交接后进入下一步。','progress','查看执行进度'],
  reworking:['正在修正审查发现的问题','已有结果会保留，修正后重新测试与审查。','progress','查看修正进度'],
  verifying:['正在运行验收检查','以实际命令和退出结果为准，不把未验证当作通过。','results','查看验证结果'],
  reviewing:['正在独立审查成果','审查员正在核对实现与测试证据，完成后交给你验收。','results','查看验证结果'],
  awaiting_decision:['有事项需要你处理','查看申请人、目的与影响范围，再决定是否允许继续。','actions','查看待办事项'],
  paused:['任务已暂停','已完成的步骤保留，点击“继续”后从执行边界恢复。','progress','查看暂停位置'],
  needs_attention:['任务已停止，需要处理','已有记录与成果保留。先查看原因和建议，不会自动重试未知操作。','progress','查看原因与处理'],
  cancelled:['任务已取消','未执行的步骤不会继续；已产生的记录和可用成果保留。','progress','查看已有记录'],
  awaiting_acceptance:['成果已就绪，等待你的验收','先查看交付内容、测试与审查结果，再在本页确认是否接受。','results','检查交付成果'],
 completed:['任务已完成','本次成果已通过人工验收，可以查看或下载交付内容。','results','查看交付成果'],
 };
 if(autonomous)Object.assign(copy,{
  planning:['负责人正在协调工作','按本次团队能力与目标安排实际工作，不套用固定开发流程。','progress','查看成员分工'],
  running:['团队正在自主协作','负责人和成员按实际需要分工、通信与调用工具，进展以任务记录为准。','progress','查看成员分工'],
  reviewing:['正在核对交付依据','核对本次实际成果与未完成工作，不表示存在固定独立审查环节。','results','查看交付依据'],
  awaiting_acceptance:['成果已就绪，等待你的验收','核对交付文件、成员结论和实际验证依据，再决定是否接受。','results','检查交付成果'],
  needs_attention:['任务已停止 · 尚未完成','保留已经产生的工作与停止原因，不把用尽额度、等待或成员回复当作成功。','progress','查看原因与处理'],
 });
 const issue=tasks.map(t=>t.result?.issue_summary).find(i=>i?.status==='open'||i?.status==='escalated');
 if(issue&&run.status==='running')copy.running=['负责人正在处理执行问题',issue.summary||'检查当前成果，在已有权限内组织修复与验证。','progress','查看问题与修复进度'];
 if(issue&&run.status==='needs_attention')copy.needs_attention=['自动处理已停止，需要你的协助',issue.escalation_reason||issue.summary||'查看问题、已尝试的处理和需要你决定的事项。','progress','查看具体问题'];
 const [title,description,target,action]=copy[run.status]||['正在同步任务状态','暂时无法识别该状态，请刷新或查看执行记录。','activity','查看执行记录'];
 return {title,description,target,action,attention,blocked,discussion,autonomous,focus,
  steps:labels.map((label,i)=>({label,state:run.status==='completed'?'done':i===index?(blocked?'blocked':attention?'waiting':'current'):i<index?'past':'upcoming'})),
  deliverables:tasks.filter(t=>['develop','autonomous'].includes(t.kind)),
 };
}

export function verificationSummary(run:Run){
 // Check/review keys carry the execution round; API task order is not a recency guarantee.
 const latest=(kind:string,prefix:string)=>[...(run.tasks||[])].reverse().filter(t=>t.kind===kind).sort((a,b)=>{
  const round=(t:Task)=>{const match=t.key.match(new RegExp('^'+prefix+'_(\\d+)$'));return match?Number(match[1]):-1;};
  return round(b)-round(a);
 })[0];
 const checkTask=latest('check','checks'),reviewTask=latest('review','review');
 const frozen=['completed','awaiting_acceptance'].includes(run.status);
 const checks=(frozen&&run.report?.checks)||checkTask?.result||run.report?.checks||{};
 const commands=Array.isArray(checks.commands)?checks.commands:[];
 const passing=checks.verification==='passed'&&commands.length>0&&commands.every((c:any)=>c.exit_code===0&&!c.timed_out&&!c.truncated);
 const review=(frozen&&run.report?.review)||reviewTask?.result||run.report?.review||{};
 return {checks,commands,passing,review,
  checkLabel:isDiscussion(run)?'只读讨论 · 不执行代码测试':passing?`${commands.length} 条验收命令通过`:checks.verification==='failed'?'验收检查未通过':checks.verification==='not_requested'?'未请求固定项目检查 · 不代表验证通过':checks.verification==='unverified'?'未验证 · 未执行验收命令':checkTask?.status==='running'?'正在运行本轮验收检查':commands.length?'查看命令结果':'尚无验收命令结果',
  reviewLabel:review.verdict==='pass'?'独立审查通过':review.verdict==='not_requested'?'本次未设置固定独立审查':review.verdict?'审查有待处理问题':isAutonomous(run)?'按任务交付依据验收':'等待独立审查结果'};
}
