import type {Run} from '../types';

type WorkIssue={task_key:string;kind:string;source_agent_id:string;source_agent_name:string;status:'open'|'resolved'|'escalated';summary:string;files:string[];attempts:number;max_attempts:number;escalation_reason?:string;resolution?:string;evidence_task_keys?:string[]};
export function runIssues(run:Run):WorkIssue[]{
 return (run.tasks||[]).map(task=>task.result?.issue_summary).filter((issue):issue is WorkIssue=>!!issue&&typeof issue.task_key==='string'&&['open','resolved','escalated'].includes(issue.status));
}
export default function RunIssues({run}:{run:Run}){
 const issues=runIssues(run);if(!issues.length)return null;
 const open=issues.filter(i=>i.status!=='resolved'),resolved=issues.filter(i=>i.status==='resolved');
 const leader=run.snapshot?.agents?.[run.snapshot?.entry_agent_id]?.name||'负责人';
 const card=(issue:WorkIssue)=>{
  const recovering=issue.status==='open'&&run.status==='running';
  const paused=issue.status==='open'&&run.status==='paused';
  const title=issue.status==='resolved'?'问题已解决':recovering?'负责人正在尝试解决':paused?'修复已暂停':'需要你协助处理';
  return <article key={issue.task_key} className={'run-issue '+(recovering?'recovering':issue.status==='resolved'?'resolved':'escalated')}>
   <header><b>{title}</b><span>{issue.kind==='integration_conflict'?'成果合并冲突':issue.kind==='member_blocked'?'成员遇到阻碍':'执行问题'}</span></header>
   <p className="run-issue-summary">{issue.summary}</p>
   <dl><div><dt>出问题的成员</dt><dd>{issue.source_agent_name||run.snapshot?.agents?.[issue.source_agent_id]?.name||'未记录'}</dd></div><div><dt>协调负责人</dt><dd>{leader}</dd></div><div><dt>协调尝试</dt><dd>{Number(issue.attempts)||0} / {Number(issue.max_attempts)||3} 次</dd></div></dl>
   {Array.isArray(issue.files)&&!!issue.files.length&&<details className="run-issue-files"><summary>涉及 {issue.files.length} 个文件</summary><ul>{issue.files.map(file=><li key={file}><code>{file}</code></li>)}</ul></details>}
   {issue.status==='resolved'?<details><summary>查看修复结论与证据</summary><p>{issue.resolution||'修复证据已记录。'}</p><p>已集成工作：{issue.evidence_task_keys?.join('、')||'请查看执行记录'}</p></details>:<div className="run-issue-next"><b>{recovering?'现在谁在处理':'下一步'}</b><p>{recovering?`${leader} 会检查现有文件、组织修复并验证；尚未解决前不会提交完成。`:paused?'任务已暂停，可继续运行以恢复负责人协调。':issue.escalation_reason||'自动处理已停止。请核对问题和已有记录，明确剩余工作或必要授权；不会自动重复整次任务。'}</p></div>}
   <details className="run-issue-technical"><summary>工作记录编号</summary><code>{issue.task_key}</code></details>
  </article>;
 };
 return <section className="run-issues" aria-label="问题与负责人处理进度">{open.map(card)}{!!resolved.length&&<details className="resolved-issues"><summary>已解决 {resolved.length} 个问题</summary>{resolved.map(card)}</details>}</section>;
}
