import {useEffect,useRef,useState} from 'react';
import {api} from '../api';
import type {Json,Run} from '../types';

type Props={run:Run;writer:boolean;onRefresh:()=>Promise<unknown>};
type Draft={scope:string;note:string;error:string;status:'idle'|'sending'|'queued';commandId:string};
const fresh=(scope:string):Draft=>({scope,note:'',error:'',status:'idle',commandId:''});
const text=(value:unknown,fallback='')=>typeof value==='string'?value:fallback;
const items=(value:unknown):Json[]=>Array.isArray(value)?value.filter(item=>item&&typeof item==='object'&&!Array.isArray(item)):[];

/** Decisions stay bound to one frozen version until the server advances the run. */
export default function TaskApprovals({run,writer,onRefresh}:Props){
 const phase=run.status==='awaiting_approval'?'plan':run.status==='awaiting_acceptance'?'acceptance':null;
 const digest=text(phase==='plan'?run.plan_digest:run.artifact_digest);
 const planItems=items(run.plan?.tasks);
 const scopeReady=phase==='plan'?!!text(run.plan?.summary).trim()&&planItems.length>0:!!run.report&&typeof run.report==='object'&&!Array.isArray(run.report);
 const scope=JSON.stringify([run.id,phase,digest]);
 const gate=useRef<{scope:string;locked:boolean}|null>(null);
 if(gate.current?.scope!==scope)gate.current={scope,locked:false};
 const scopeGate=gate.current;
 const [stored,setStored]=useState<Draft>(()=>fresh(scope));
 const draft=stored.scope===scope?stored:fresh(scope);
 const [refreshing,setRefreshing]=useState(false);
 useEffect(()=>{gate.current=scopeGate;setStored(fresh(scope));setRefreshing(false);return()=>{if(gate.current===scopeGate)gate.current=null;};},[scope]);
 const pending=items(run.commands).find(command=>command.action==='decide'&&command.payload?.phase===phase&&command.payload?.digest===digest);
 const validDigest=/^[a-f0-9]{64}$/.test(digest);
 const waiting=!!pending||draft.status==='queued';
 const sending=draft.status==='sending';
 const locked=sending||waiting;
 const update=(change:Partial<Draft>)=>setStored(old=>({... (old.scope===scope?old:fresh(scope)),...change}));
 const noteRequired=phase==='acceptance';
 const canSubmit=writer&&!!phase&&validDigest&&scopeReady&&!locked&&(!noteRequired||!!draft.note.trim());
 async function refresh(){
  if(refreshing)return;
  const ticket=gate.current;setRefreshing(true);
  try{await onRefresh();}
  catch(error){if(gate.current===ticket)update({error:'刷新未成功：'+(error as Error).message+'。已提交的决定不会撤销，请稍后重试刷新。'});}
  finally{if(gate.current===ticket)setRefreshing(false);}
 }
 async function decide(approved:boolean){
  const ticket=gate.current;
  if(!canSubmit||!ticket||ticket.scope!==scope||ticket.locked)return;
  ticket.locked=true;update({status:'sending',error:''});
  let accepted=false;
  try{
   const result=await api<{accepted:boolean;command_id:string}>(`/runs/${encodeURIComponent(run.id)}/decision/${phase}`,'POST',{approved,digest,note:draft.note.trim()});
   if(gate.current!==ticket)return;
   // An HTTP success is a queued command, not evidence that the run advanced.
   accepted=true;
   if(result?.accepted!==true||!text(result?.command_id)){
    update({status:'queued',error:'未收到完整提交回执。请刷新确认服务端状态，暂不重复提交。'});
   }else update({status:'queued',commandId:result.command_id});
   try{await onRefresh();}
   catch(error){if(gate.current===ticket)update({error:'决定已提交，但刷新未成功：'+(error as Error).message+'。请刷新查看结果，不要重复提交。'});}
  }catch(error){
   if(gate.current===ticket){ticket.locked=false;update({status:'idle',error:'提交未确认：'+(error as Error).message+'。请先刷新核对当前版本与提交记录。'});}
  }finally{if(gate.current===ticket&&!accepted)ticket.locked=false;}
 }
 if(!phase)return null;
 const plan=phase==='plan',discussion=run.report?.mode==='discussion',autonomous=run.mode==='autonomous'||run.execution_strategy==='autonomous'||run.snapshot?.execution_strategy==='autonomous'||run.report?.mode==='autonomous';
 const agents=run.snapshot?.agents||{};
 const member=(id:unknown)=>text(agents[text(id)]?.name)|| (text(id)?`成员 ${text(id).slice(0,8)}`:'未记录负责人');
 const responsible=plan?member(run.snapshot?.planner_id):autonomous||discussion?member(run.snapshot?.entry_agent_id||run.snapshot?.planner_id):member(run.snapshot?.reviewer_id);
 const verification=run.report?.checks?.verification;
 const checkEvidence=!autonomous||(Array.isArray(run.report?.checks?.commands)&&run.report.checks.commands.length>0&&run.report.checks.commands.every((c:Json)=>c.exit_code===0&&!c.timed_out&&!c.truncated));
 const checkLabel=autonomous&&verification==='not_requested'?'未请求固定项目检查，不代表验证通过':discussion?'只读讨论，未执行代码验证':verification==='passed'&&checkEvidence?'已通过配置的验收命令':verification==='failed'?'自动验证未通过':'未验证，不能视为通过';
 const review=run.report?.review;
 const reviewLabel=autonomous&&review?.verdict==='not_requested'?'本次未设置固定独立审查':discussion?'本次为讨论成果，不是代码审查':review?.verdict==='pass'?'独立审查通过':review?.verdict==='rework'?'独立审查要求修改':'尚无可核对的审查结论';
 return <section className="task-approval" aria-label={plan?'计划审批':'成果验收'}>
  <header className="task-approval-heading"><div><span className="eyebrow">需要你确认</span><h3>{plan?'确认计划后，团队才能开始执行':'检查成果后，决定是否验收'}</h3></div><span className="task-approval-version">版本 {digest?digest.slice(0,12):'缺失'}</span></header>
  <p className="task-approval-intro">{plan?text(run.plan?.summary,'计划内容尚未完整加载，请刷新后再审批。'):'请先在“成果”页签检查交付内容与验证依据。本次决定只针对当前成果版本，不会自动合并、推送或部署到原项目。'}</p>
  <dl className="task-approval-ownership"><div><dt>{plan?'计划负责人':autonomous?'任务负责人':discussion?'讨论负责人':'独立审查员'}</dt><dd>{responsible}</dd></div><div><dt>最终决定</dt><dd>{writer?'由你确认':'有操作权限的成员确认'}</dd></div></dl>
  {plan?<ol className="task-approval-plan">{planItems.map((task,index)=><li key={text(task.key,String(index))}><div className="task-approval-step"><span>{index+1}</span><strong>{text(task.title,'未命名子任务')}</strong><small>{member(task.agent_id)}</small></div><p><b>交付要求：</b>{text(task.acceptance,'未记录，请查看完整计划后再决定。')}</p>{Array.isArray(task.depends_on)&&task.depends_on.length>0&&<p className="task-approval-dependency">在 {task.depends_on.map((key:unknown)=>text(planItems.find(item=>item.key===key)?.title,text(key))).join('、')} 完成后开始</p>}<details><summary>查看工作内容</summary><p>{text(task.description,'未记录更多说明')}</p></details></li>)}</ol>:<div className="task-approval-evidence"><div><span>自动验证</span><strong className={!discussion&&verification==='passed'&&checkEvidence?'':'task-approval-caution'}>{checkLabel}</strong></div><div><span>独立审查</span><strong>{reviewLabel}</strong></div>{text(review?.summary)&&<p>{review.summary}</p>}<p>{autonomous?'验收范围：本次目标、真实交付文件、成员结论与执行证据；未执行的检查不视为通过。':discussion?'验收范围：讨论记录、结论和未解决问题；不代表代码已经实现或测试通过。':'验收范围：原始需求、实际交付文件、代码差异、验收命令结果与独立审查。'}</p></div>}
  <div className="task-approval-consequences"><p><b>{plan?'批准后':'验收通过后'}</b> · {plan?'按上方分工开始执行；工具安装及额外权限仍按既定策略分别处理。':'本次任务标记为已完成；自动验证的原始结果仍保留，不会被人工决定覆盖。'}</p><p><b>{plan?'拒绝后':'不通过后'}</b> · {plan?'本次运行取消，不执行这份计划。需要调整时可修改要求后重新创建任务。':'本次运行停止并保留成果，不会自动返工。你可带上验收意见重新创建任务。'}</p></div>
  <details className="task-approval-version-details"><summary>查看完整版本与原始需求</summary><p>本次{plan?'计划':'成果'}摘要</p><code>{digest||'未提供摘要'}</code><p>{run.request}</p>{draft.commandId&&<p>提交记录：<code>{draft.commandId}</code></p>}</details>
  {!validDigest&&<p className="form-error" role="alert">版本摘要缺失或无效，暂不能提交。请刷新任务，避免批准错误版本。</p>}
  {!scopeReady&&<p className="form-error" role="alert">{plan?'计划与成员分工':'成果报告'}尚未完整加载，暂不能提交决定。请先刷新检查审批范围。</p>}
  {writer?<div className="task-approval-form"><label htmlFor={`approval-note-${run.id}`}>{plan?'审批说明（选填）':'验收意见（必填）'}</label><textarea id={`approval-note-${run.id}`} aria-label={plan?'计划审批说明':'成果验收意见'} value={draft.note} maxLength={4000} rows={2} disabled={locked} onChange={event=>update({note:event.target.value})} placeholder={plan?'例如：按此范围执行，优先保证原有测试不变。':'写明已检查的成果与依据，或需要修改的问题。'}/>{noteRequired&&!draft.note.trim()&&!waiting&&<p className="microcopy">请填写验收依据或不通过的原因，再提交决定。</p>}<div className="task-approval-actions"><button type="button" className="button secondary" disabled={!canSubmit} onClick={()=>decide(false)}>{plan?'拒绝计划并结束':'验收不通过'}</button><button type="button" className="button primary" disabled={!canSubmit} onClick={()=>decide(true)}>{sending?'正在提交决定…':plan?'批准计划并执行':'确认验收通过'}</button></div></div>:<p className="task-approval-readonly">当前为只读查看，你可以检查计划和成果，但不能提交审批决定。</p>}
  {waiting&&<p className="task-approval-waiting" role="status">{pending&&!pending.sent?'决定已提交，等待送达执行器。':pending?.sent?'决定已送达，等待任务状态更新。':'决定已提交，等待任务状态更新。'}当前还没有进入下一阶段，请勿重复提交。</p>}
  {pending?.error&&<p className="form-error" role="alert">决定暂未成功送达：{text(pending.error)}。原提交记录已保留，请检查执行器后刷新，不要重复审批。</p>}
  {draft.error&&<p className="form-error" role="alert">{draft.error}</p>}
  {(waiting||draft.error||!validDigest||!scopeReady)&&<button type="button" className="button secondary task-approval-refresh" disabled={sending||refreshing} onClick={refresh}>{refreshing?'正在刷新…':'刷新任务状态'}</button>}
 </section>;
}
