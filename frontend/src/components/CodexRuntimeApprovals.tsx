import {useEffect,useRef,useState} from 'react';
import {api,jsonText} from '../api';
import type {Json,Run} from '../types';

export type RuntimeApproval={id:string;run_id:string;task_key:string;agent_id:string;kind:string;thread_id:string;turn_id:string;item_id:string;status:string;digest:string;payload:Json;decision?:Json|null;error?:string;created_at:string};
type Props={run:Run;writer:boolean;agentId?:string;visible?:boolean;onRefresh?:()=>Promise<unknown>;onPendingChange?:(count:number)=>void};
type Draft={comment:string;ack:boolean;state:'idle'|'sending'|'queued'|'unknown';error:string};
type Scope={key:string;sequence:number;active:boolean;rows:RuntimeApproval[];locks:Set<string>;error:string};
const fresh=():Draft=>({comment:'',ack:false,state:'idle',error:''});
const text=(value:unknown)=>typeof value==='string'?value:'';
const kinds:Record<string,string>={
 'item/commandExecution/requestApproval':'执行命令',
 'item/fileChange/requestApproval':'修改文件',
 'item/permissions/requestApproval':'申请本回合权限',
};
const statuses:Record<string,string>={pending:'等待你的决定',queued:'决定已提交，等待送达运行时',delivered:'决定已送达运行时',expired:'申请已失效，不能再批准',delivery_unknown:'决定送达结果未确认，禁止重复审批'};
const identity=(row:RuntimeApproval)=>JSON.stringify([row.id,row.digest]);

/** A runtime decision authorizes this exact request, never a plan or a session rule. */
export default function CodexRuntimeApprovals({run,writer,agentId,visible=true,onRefresh,onPendingChange}:Props){
 const key=JSON.stringify([run.id,agentId||'']);
 const gate=useRef<Scope|null>(null);
 if(gate.current?.key!==key)gate.current={key,sequence:0,active:true,rows:[],locks:new Set(),error:''};
 const scope=gate.current;
 const latest=useRef({writer,run,onRefresh,onPendingChange});latest.current={writer,run,onRefresh,onPendingChange};
 const [stored,setStored]=useState<{scope:Scope;rows:RuntimeApproval[];loaded:boolean;error:string;loading:boolean}|null>(null);
 const [drafts,setDrafts]=useState<{scope:Scope;values:Record<string,Draft>}|null>(null);
 const current=()=>gate.current===scope&&scope.active;
 const state=stored?.scope===scope?stored:{rows:[],loaded:false,error:'',loading:false};
 const values=drafts?.scope===scope?drafts.values:{};
 const update=(row:RuntimeApproval,change:Partial<Draft>)=>{if(!current())return;setDrafts(old=>{const prior=old?.scope===scope?old.values:{};return {scope,values:{...prior,[identity(row)]:{...(prior[identity(row)]||fresh()),...change}}};});};
 const load=async()=>{
  if(!current()||!run.id)return;
  const request=++scope.sequence;
  setStored(old=>({scope,rows:old?.scope===scope?old.rows:[],loaded:old?.scope===scope&&old.loaded||false,error:'',loading:true}));
  try{
   const response=await api<{items:RuntimeApproval[]}>(`/runs/${encodeURIComponent(run.id)}/runtime-approvals`);
   if(!current()||request!==scope.sequence)return;
   if(!Array.isArray(response?.items))throw new Error('运行时审批列表格式无效');
   // A server response from another run/member is never authority for this panel.
   const rows=response.items.filter(row=>row&&row.run_id===run.id&&(!agentId||row.agent_id===agentId));
   scope.rows=rows;scope.error='';
   setStored({scope,rows,loaded:true,error:'',loading:false});
   latest.current.onPendingChange?.(rows.filter(row=>row.status==='pending').length);
  }catch(error){if(current()&&request===scope.sequence){scope.error=(error as Error).message;setStored(old=>({scope,rows:old?.scope===scope?old.rows:[],loaded:old?.scope===scope&&old.loaded||false,error:scope.error,loading:false}));}}
 };
 useEffect(()=>{scope.active=true;gate.current=scope;latest.current.onPendingChange?.(0);void load();const timer=setInterval(()=>void load(),6000);return()=>{scope.active=false;scope.sequence++;clearInterval(timer);if(gate.current===scope)gate.current=null;};},[scope]);
 const closed=['completed','cancelled','needs_attention','paused'].includes(run.status);
 async function decide(row:RuntimeApproval,approved:boolean){
  const rowKey=identity(row),draft=values[rowKey]||fresh();
  const live=scope.rows.find(item=>identity(item)===rowKey);
  if(!current()||!latest.current.writer||['completed','cancelled','needs_attention','paused'].includes(latest.current.run.status)
    ||!live||live.status!=='pending'||!kinds[live.kind]||!live.thread_id||!live.turn_id||!live.id
    ||!/^[a-f0-9]{64}$/.test(live.digest)||scope.locks.has(rowKey)||!draft.ack||scope.error)return;
  scope.locks.add(rowKey);scope.sequence++;
  update(row,{state:'sending',error:''});
  try{
   await api(`/runs/${encodeURIComponent(run.id)}/runtime-approvals/${encodeURIComponent(row.id)}/decision`,'POST',{digest:row.digest,approved,comment:draft.comment.trim()});
   if(!current())return;
   update(row,{state:'queued'});
   // 202 is durable admission of the decision, not execution or success.
   await load();
   try{await latest.current.onRefresh?.();}catch(error){update(row,{error:'决定已提交，但任务刷新失败：'+(error as Error).message});}
  }catch(error){if(current()){update(row,{state:'unknown',error:'提交结果未确认：'+(error as Error).message+'。请刷新核对记录，不要重复批准或提交相反决定。'});void load();}}
 }
 if(!visible)return null;
 const pending=state.rows.filter(row=>row.status==='pending');
 const history=state.rows.filter(row=>row.status!=='pending');
 const card=(row:RuntimeApproval)=>{
  const draft=values[identity(row)]||fresh(),known=!!kinds[row.kind];
  const valid=!!row.id&&!!row.thread_id&&!!row.turn_id&&/^[a-f0-9]{64}$/.test(row.digest);
  const locked=draft.state!=='idle'||scope.locks.has(identity(row));
  const payload=row.payload&&typeof row.payload==='object'?row.payload:{};
  const member=run.snapshot?.agents?.[row.agent_id]?.name||row.agent_id||'未记录申请人';
  const status=draft.state==='unknown'?'提交结果未确认，暂不重复提交':draft.state==='sending'?'正在提交决定…':draft.state==='queued'&&row.status==='pending'?'决定已提交，等待服务端更新记录':statuses[row.status]||'未知审批状态，禁止提交';
  return <article key={identity(row)} className={'execution-request compact-request runtime-approval '+row.status}>
   <header><div><small>{member} · {row.task_key||'未记录任务'}</small><h4>{kinds[row.kind]||'不支持的运行时审批类型'}</h4></div><p role="status">{status}</p></header>
   {text(payload.reason)&&<p className="request-reason">{text(payload.reason)}</p>}
   <p className="request-next">仅批准这里列出的本次操作 / 本回合；不会批准整个会话、永久规则或自动执行后续操作。决定送达不代表命令已执行或文件已修改。</p>
   {payload.command!==undefined&&<div><b>请求命令</b><pre>{typeof payload.command==='string'?payload.command:jsonText(payload.command)}</pre></div>}
   {text(payload.cwd)&&<p>工作目录：<code>{text(payload.cwd)}</code></p>}
   {payload.permissions!==undefined&&<div><b>请求权限</b><pre>{jsonText(payload.permissions)}</pre></div>}
   <details><summary>核对完整申请、范围与版本</summary><pre>{jsonText({kind:row.kind,thread_id:row.thread_id,turn_id:row.turn_id,item_id:row.item_id,digest:row.digest,payload,decision:row.decision})}</pre></details>
   {row.error&&<p className="form-error" role="alert">{row.error}</p>}{draft.error&&<p className="form-error" role="alert">{draft.error}</p>}
   {row.status==='pending'&&(!known||!valid)&&<p className="form-error" role="alert">申请类型或冻结版本不完整，不能提交决定。请刷新并检查运行时记录。</p>}
   {row.status==='pending'&&writer&&!closed&&known&&valid?<div className="task-approval-form">
    <label>审批说明（选填）<textarea aria-label={'运行时审批说明 '+row.id} maxLength={4000} rows={2} value={draft.comment} disabled={locked} onChange={event=>update(row,{comment:event.target.value})}/></label>
    <label className="check"><input type="checkbox" aria-label={'确认本次操作范围 '+row.id} checked={draft.ack} disabled={locked} onChange={event=>update(row,{ack:event.target.checked})}/>我已核对完整申请，只决定本次操作 / 本回合</label>
    <div className="task-approval-actions"><button type="button" className="button secondary" disabled={!draft.ack||locked||!!state.error} onClick={()=>decide(row,false)}>拒绝本次请求</button><button type="button" className="button primary" disabled={!draft.ack||locked||!!state.error} onClick={()=>decide(row,true)}>批准本次请求</button></div>
   </div>:row.status==='pending'&&<p className="microcopy">当前为只读记录，不能提交运行时决定。</p>}
  </article>;
 };
 return <section className="execution-center execution-center-compact" aria-label="Codex 运行时审批">
  <header className="execution-heading"><div><h3>Codex 运行时审批{pending.length>0&&<span className="pending-count">{pending.length}</span>}</h3><p>这不是执行计划审批；只处理当前{agentId?'员工在本':''}任务的运行时请求。</p></div><button type="button" className="button secondary small" disabled={state.loading} onClick={()=>void load()}>刷新运行时审批</button></header>
  {state.loading&&<p role="status">正在读取运行时审批…</p>}{state.error&&<p className="form-error" role="alert">审批读取失败：{state.error}。保留上次记录，但暂不能提交。</p>}
  {pending.map(card)}{state.loaded&&!state.error&&!pending.length&&<p className="approval-empty">当前没有待决定的 Codex 运行时请求。</p>}
  {history.length>0&&<details className="execution-history"><summary>已处理与失效申请（{history.length}）</summary>{history.map(card)}</details>}
 </section>;
}
