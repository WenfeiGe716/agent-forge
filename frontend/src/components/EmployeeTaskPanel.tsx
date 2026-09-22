import {useEffect,useRef,useState} from 'react';
import {api,jsonText} from '../api';
import {Agent,ForgeEvent,Json,Project,Run,states,terminal} from '../types';
import {Badge} from './UI';
import {ArrowRight,RefreshCw,ShieldCheck,CheckCircle2,FolderGit2,Activity,Paperclip,Send} from './Icons';
import TaskApprovals from './TaskApprovals';
import ExecutionCenter from './ExecutionCenter';
import CodexRuntimeApprovals from './CodexRuntimeApprovals';
import {Question} from './Collaboration';
import {summarizeRunFailure} from './RunRecovery';
import RunIssues,{runIssues} from './RunIssues';
import {subscribeRunEvents} from '../runEventStream';
import '../employee-task-density.css';

const submittedStatus=(status:string)=>['done','integrated','conflicted','superseded'].includes(status);
function employeeStageNote(task:{status:string;kind:string;result?:Json}){
 if(task.status==='conflicted')return '已提交 · 冲突待修复';
 if(task.status==='superseded')return '原分工已提交 · 由修复工作替代';
 if(task.kind==='autonomous'&&task.result?.outcome==='waiting')return '本次已让出 · 等待成员消息';
 if(task.kind==='autonomous'&&task.result?.outcome==='blocked')return '已报告阻塞 · 未完成';
 return '';
}

export function employeeRuns(agentId:string,runs:Run[],detail?:Run|null){
 const candidates=detail?[...runs.filter(r=>r.id!==detail.id),detail]:runs;
 return candidates.filter(r=>(r.agent_ids||Object.keys(r.snapshot?.agents||{})).includes(agentId))
  .sort((a,b)=>Number(terminal.includes(a.status))-Number(terminal.includes(b.status))||b.updated_at.localeCompare(a.updated_at));
}
export function employeeMessage(event:ForgeEvent,member:string,names:Record<string,string>){
 const from=event.data?.from_agent_id,to=event.data?.to_agent_id;
 const title=event.kind==='agent.message'?(from===member?'发出成员消息':to===member?'收到成员消息':'成员消息'):event.kind==='agent.message.delivered'?(from===member?'发出的消息已进入对方上下文':to===member?'消息已进入本成员上下文':'消息送达记录'):event.kind==='team.attachment.uploaded'?'附件已发送':event.kind==='task.handoff'?(from===member?'交付工作成果':to===member?'接收工作成果':'工作成果交接'):event.kind==='task.delegated'?'工作分配':'编排交接';
 const recipient=event.data?.recipient_id;
 return {title,parties:from||to?`${names[from]||'未注明发送成员'} → ${names[to]||'未注明接收成员'}`:recipient===member?'发送给本成员':'运行附件'};
}
function employeeMessageText(event:ForgeEvent){
 if(event.data?.body||event.data?.summary)return String(event.data.body||event.data.summary);
 if(event.kind==='team.attachment.uploaded'){
  const name=String(event.data?.filename||'未命名附件');
  const size=Number(event.data?.size||0);
  return `附件：${name}${size>0?` · ${(size/1024).toFixed(1)} KB`:''}`;
 }
 return '查看完整交接依据';
}
type Props={agent:Agent;runs:Run[];detail:Run|null;projects:Project[];writer:boolean;admin:boolean;preview?:boolean;initialRunId?:string;initialTab?:'work'|'approval'|'messages';onRunChange?:(id:string)=>void;onOpenRun:(id:string)=>void;onNavigate:(page:string)=>void;onRefresh:()=>void};

export default function EmployeeTaskPanel(p:Props){
 const related=employeeRuns(p.agent.id,p.runs,p.detail);
 const preferred=related.find(r=>r.id===p.initialRunId)?.id||related.find(r=>r.id===p.detail?.id)?.id||related[0]?.id||'';
 const [chosen,setChosen]=useState(()=>preferred);
 // A newly created run may precede the next list refresh. Keep the explicit
 // selection: EmployeeRun validates both the response ID and membership.
 const id=p.onRunChange&&p.initialRunId?p.initialRunId:related.some(r=>r.id===chosen)?chosen:preferred;
 useEffect(()=>{if(chosen!==id)setChosen(id);},[chosen,id]);
 useEffect(()=>{if(id)p.onRunChange?.(id);},[id,p.agent.id]);
 return <section className="employee-task-panel" aria-label="员工任务与审批">
  <header><h3>当前工作</h3><span>{related.filter(r=>!terminal.includes(r.status)).length} 项未结束</span>{related.length>0&&<details className="employee-run-switch"><summary>切换任务 · {related.length}<span aria-hidden="true">⌄</span></summary><label className="employee-run-picker"><span>选择这位员工参与的任务</span><select aria-label="员工当前查看的任务" value={id} onChange={e=>{setChosen(e.target.value);p.onRunChange?.(e.target.value);}}>{id&&!related.some(r=>r.id===id)&&<option value={id}>正在读取指定任务…</option>}{related.map(r=><option key={r.id} value={r.id}>{r.title} · {states[r.status]||r.status}</option>)}</select></label></details>}</header>
  {id?<EmployeeRun key={p.agent.id+':'+id} {...p} id={id}/>:<div className="employee-empty"><span className="employee-empty-icon"><FolderGit2 size={26}/></span><h4>等待新的工作</h4><p>没有已分配的可见任务。发起需求并确认团队后，实际任务会出现在这里。</p><button className="button secondary" onClick={()=>p.onNavigate('organization')}>查看工位与团队</button></div>}
  <details className="employee-context-note"><summary>任务范围与记录说明</summary><p>每个任务独立记录进度、工作副本、成员消息和审批；这里的选择不会改变其他任务。有工位不等于正在执行。</p></details>
 </section>;
}

function EmployeeRun({id,...p}:Props&{id:string}){
 const [data,setData]=useState<{run:Run;collaboration:Json;events:ForgeEvent[];more:boolean}|null>(null),[error,setError]=useState(''),[tab,setTab]=useState<'work'|'approval'|'messages'>(p.initialTab||'work');
 const [limit,setLimit]=useState(60),[workLimit,setWorkLimit]=useState(12),[workFilter,setWorkFilter]=useState('all');
 const [runtimePending,setRuntimePending]=useState<{scope:string;count:number}>({scope:'',count:0});
 const [steerText,setSteerText]=useState('');
 const [steerFiles,setSteerFiles]=useState<File[]>([]);
 const [steerTaskKey,setSteerTaskKey]=useState('');
 const [steerBusy,setSteerBusy]=useState(false),[steerError,setSteerError]=useState(''),[steerNotice,setSteerNotice]=useState('');
 useEffect(()=>{setTab(p.initialTab||'work');},[p.initialTab]);
 const lifecycle=useRef(0),sequence=useRef(0),replayed=useRef(false);
 const load=async()=>{if(p.preview)return;const life=lifecycle.current,ticket=++sequence.current;const current=()=>life===lifecycle.current&&ticket===sequence.current;
  try{const [run,collaboration]=await Promise.all([api<Run>('/runs/'+id),api<Json>('/runs/'+id+'/collaboration')]);
   if(!current())return;if(run.id!==id||collaboration.run_id!==id||!Object.hasOwn(run.snapshot?.agents||{},p.agent.id))throw new Error('该员工不在这个任务的冻结成员范围内，请重新选择任务。');
   // The shared EventSource owns live delivery. This replay is a durable,
   // member-filtered fallback for a panel opened without the main App view;
   // page until the server cursor is exhausted so long runs are lossless.
   const replay:ForgeEvent[]=[];let cursor=0,more=false;const initialReplay=!replayed.current;
   if(initialReplay){more=true;while(more&&current()){const page=await api<{items:ForgeEvent[];has_more?:boolean;next_cursor?:number}>(`/runs/${id}/trace?agent_id=${encodeURIComponent(p.agent.id)}&after=${cursor}&limit=300`);replay.push(...(page.items||[]).filter(e=>e.run_id===id));const next=Number(page.next_cursor||0);more=page.has_more===true;if(next<=cursor){break;}cursor=next;}replayed.current=true;}
   if(!current())return;
   setData(old=>({run,collaboration,events:initialReplay&&replay.length?replay:(old?.events||[]),more:initialReplay?more:(old?.more||false)}));setError('');
  }catch(e){if(current())setError((e as Error).message);}
 };
 useEffect(()=>{lifecycle.current++;void load();const timer=setInterval(()=>void load(),6000);return()=>{lifecycle.current++;clearInterval(timer);};},[id,p.agent.id,p.preview,limit]);
 // Keep this member's output live while the run is active. The server stream is
 // replayable from the last event id, so reconnects cannot silently skip output.
 useEffect(()=>{
  if(p.preview||!data||typeof EventSource==='undefined')return;
  return subscribeRunEvents(id,{events:items=>setData(old=>old?{...old,events:items.filter(event=>{const d=event.data||{};return [d.agent_id,d.from_agent_id,d.to_agent_id,d.worker_id,d.recipient_id].includes(p.agent.id)||(Array.isArray(d.recipients)&&d.recipients.includes(p.agent.id));})}:old),error:error=>setError((error as Error).message)});
 },[id,p.agent.id,p.preview,Boolean(data)]);
 const sendSteer=async()=>{
  if(!steerText.trim()&&!steerFiles.length||steerBusy||closed)return;
  setSteerBusy(true);setSteerError('');setSteerNotice('');
  try{
   const attachmentRefs:string[]=[];
   for(const file of steerFiles){
    const content_base64=await new Promise<string>((resolve,reject)=>{const reader=new FileReader();reader.onerror=()=>reject(new Error(`读取附件失败：${file.name}`));reader.onload=()=>{const value=String(reader.result||'');resolve(value.includes(',')?value.slice(value.indexOf(',')+1):value);};reader.readAsDataURL(file);});
    const value=await api<Json>(`/runs/${id}/attachments`,'POST',{filename:file.name,content_base64,recipient_id:member,task_key:steerTaskKey||undefined});
    if(value.workspace_path)attachmentRefs.push(String(value.workspace_path));
    else if(value.attachment_id)attachmentRefs.push(String(value.attachment_id));
   }
   // A run may contain several tasks for the same employee. Keep steering
   // bound to the explicitly selected task instead of guessing globally.
   const activeTask=mine.find(t=>t.key===steerTaskKey)||mine.find(t=>['running','waiting_decision','queued'].includes(t.status))||mine[0];
   const attachmentNote=attachmentRefs.length?`\n附件已上传（请从当前运行工作区读取）：${attachmentRefs.join(', ')}`:'';
   await api(`/runs/${id}/control`,'POST',{action:'steer',agent_id:member,task_key:activeTask?.key,prompt:(steerText.trim()||'请查看附件并继续当前工作。')+attachmentNote,reason:'人工运行中定向消息'});
   setSteerText('');setSteerFiles([]);setSteerNotice('消息已送达该成员的当前上下文。');await load();
  }catch(e){setSteerError((e as Error).message);}finally{setSteerBusy(false);}
 };
 const refresh=async()=>{await load();p.onRefresh();};
 if(p.preview)return <p className="callout">场景预览不加载真实审批，也不能提交决定。</p>;
 if(!data)return <div className="employee-loading">{error?<p role="alert" className="form-error">读取失败：{error}</p>:<p role="status">正在读取该员工的任务…</p>}<button className="text-button" onClick={()=>void load()}>重新读取</button></div>;
 const {run,collaboration,events}=data,member=p.agent.id;
 const names:Record<string,string>=Object.fromEntries(Object.entries(run.snapshot?.agents||{}).map(([aid,a]:[string,any])=>[aid,a.name]));
 const owner=run.status==='awaiting_approval'?run.snapshot?.planner_id:(run.mode==='autonomous'||run.execution_strategy==='autonomous'||run.snapshot?.execution_strategy==='autonomous'||run.report?.mode==='autonomous'||run.report?.mode==='discussion')?run.snapshot?.entry_agent_id||run.snapshot?.planner_id:run.snapshot?.reviewer_id;
 const codex=run.execution_engine==='codex'||run.snapshot?.execution_engine==='codex';
 const phase=run.status==='awaiting_acceptance'||run.status==='awaiting_approval'&&!codex,myPhase=phase&&owner===member;
 const questions=(collaboration.decisions||[]).filter((q:Json)=>q.run_id===id&&q.agent_id===member&&q.status==='pending');
 const runtimeScope=JSON.stringify([id,member]);
 const pending=questions.length+(myPhase?1:0)+(codex&&runtimePending.scope===runtimeScope?runtimePending.count:0),closed=terminal.includes(run.status);
 const mine=(run.tasks||[]).filter(t=>t.agent_id===member);
 const workItems=mine.map((task,index)=>({task,index})).filter(({task})=>workFilter==='all'||(workFilter==='submitted')===submittedStatus(task.status)).sort((a,b)=>Number(submittedStatus(a.task.status))-Number(submittedStatus(b.task.status)));
 const visibleWork=workItems.slice(0,workLimit),projectName=p.projects.find(x=>x.id===run.project_id)?.name||'任务项目';
 const issues=runIssues(run),failure=run.error&&!issues.length?summarizeRunFailure(run):null;
 const workstation=run.snapshot?.design?.workstations?.[member];
 const messages=events.filter(e=>['agent.message','agent.message.delivered','team.attachment.uploaded','task.handoff','task.delegated','orchestration.handoff'].includes(e.kind));
 const normalized=(e:ForgeEvent):ForgeEvent&{k:string;text:string}=>{const k=e.kind.startsWith('runtime.')?String(e.data?.runtime_kind||e.kind.slice(8).replaceAll('.','/')):e.kind;const payload=e.data?.payload&&typeof e.data.payload==='object'?e.data.payload as Json:{};const item=payload.item&&typeof payload.item==='object'?payload.item as Json:{};const text=String(e.data?.text||e.data?.delta||payload.delta||payload.text||item.text||e.data?.summary||e.data?.tool||'');return {...e,kind:k,data:{...e.data,text,delta:text},k,text};};
 const liveCandidates=events.map(normalized).filter(({data,k})=>data?.agent_id===member&&['model.delta','model.progress','model.completed','agent.started','tool.started','tool.completed','item/agentMessage/delta','item/started','item/completed'].includes(k));
 const liveText=liveCandidates.filter(({k})=>k==='model.delta'||k==='model.progress'||k==='item/agentMessage/delta').map(({text})=>text).join('');
 const liveEvents=liveCandidates.filter(({k})=>!['model.delta','model.progress','item/agentMessage/delta'].includes(k)).slice(-24);
 const submitted=mine.filter(task=>submittedStatus(task.status)).length;
 return <><section className={'employee-task-hero '+(closed?'is-closed':'')}>
  <div className="employee-run-heading"><Badge state={run.status}/><button className="icon-button" aria-label="刷新员工任务" onClick={()=>void refresh()}><RefreshCw size={14}/></button></div>
  <div className="employee-task-context"><b className="employee-task-title" title={run.title}>{run.title}</b><small title={projectName}><FolderGit2 size={13}/><span>{projectName}</span></small></div>
  <details className="employee-request-details"><summary>完整任务与需求</summary><div className="employee-long-content" tabIndex={0} aria-label="完整任务与需求内容"><h4>{run.title}</h4><p>{run.request||'未记录额外需求。'}</p><small>项目 · {projectName}</small></div></details>
  <div className="employee-task-metrics"><div><strong>{submitted}<small> / {mine.length}</small></strong><span>阶段已提交</span></div><button type="button" onClick={()=>setTab('messages')}><strong>{messages.length}{data.more&&<small>+</small>}</strong><span>通信记录</span></button><button type="button" className={pending?'has-pending':''} onClick={()=>setTab('approval')}><strong>{pending}</strong><span>待处理</span></button></div>
 </section>{error&&<p role="alert" className="form-error">同步失败：{error}。以下保留上次读取内容，请刷新后处理。</p>}{issues.length>0&&<><RunIssues run={run}/>{run.error&&<details className="employee-context-note"><summary>运行技术详情</summary><pre className="employee-long-content" tabIndex={0}>{run.error}</pre></details>}<button className="text-button employee-issue-link" onClick={()=>p.onOpenRun(id)}>查看本任务问题与执行记录<ArrowRight size={13}/></button></>}{failure&&<div className="employee-run-error" role="alert"><strong>{failure.title}</strong><p>{failure.reason}</p><details><summary>查看技术详情</summary><p className="employee-long-content" tabIndex={0}>{run.error}</p></details><button className="text-button" onClick={()=>p.onOpenRun(id)}>查看原因与处理<ArrowRight size={13}/></button></div>}{pending>0&&<button className="employee-approval-alert" onClick={()=>setTab('approval')}><ShieldCheck size={18}/><span>{pending} 项需要你处理<small>查看申请内容，再决定是否批准</small></span><ArrowRight size={16}/></button>}
 <nav className="employee-task-tabs">{([['work','分工'],['approval','审批'],['messages','通信']] as const).map(([key,label])=><button key={key} className={tab===key?'selected':''} aria-pressed={tab===key} onClick={()=>setTab(key)}>{label}{key==='approval'&&pending>0&&<b>{pending}</b>}</button>)}</nav>
 {codex&&<CodexRuntimeApprovals run={run} agentId={member} writer={p.writer&&!error} visible={tab==='approval'} onPendingChange={count=>setRuntimePending({scope:runtimeScope,count})} onRefresh={refresh}/>}
 {tab==='work'&&<><div className="employee-section-heading"><h4>我的分工 <small>{mine.length}</small></h4><select aria-label="筛选员工分工" value={workFilter} onChange={e=>{setWorkFilter(e.target.value);setWorkLimit(12);}}><option value="all">全部分工</option><option value="active">未提交</option><option value="submitted">已提交</option></select></div><div className="employee-work-items employee-record-scroll" tabIndex={0} role="region" aria-label="员工分工列表">{visibleWork.map(({task,index})=><details key={task.id} className={'employee-work-card '+task.status}><summary><span className="employee-stage-icon">{['done','integrated'].includes(task.status)?<CheckCircle2 size={19}/>:task.status==='running'?<Activity size={19}/>:String(index+1).padStart(2,'0')}</span><span className="employee-stage-title"><b title={task.title}>{task.title}</b><small>{task.spec?.depends_on?.length?'包含前置交付依赖':'独立工作阶段'}</small></span><Badge state={task.status}/>{employeeStageNote(task)&&<small className="employee-stage-outcome">{employeeStageNote(task)}</small>}</summary><div className="employee-stage-details employee-long-content" tabIndex={0} aria-label="完整阶段内容"><h5>{task.title}</h5><p>{task.spec?.description||'按照本次已确认需求执行。'}</p>{task.spec?.depends_on?.length>0&&<p>依赖：{task.spec.depends_on.map((key:string)=>run.plan?.tasks?.find((t:Json)=>t.key===key)?.title||key).join('、')}</p>}<p>{task.result?.summary||'本阶段尚未提交成果摘要。'}</p><details><summary>查看完整阶段回执</summary><pre>{jsonText(task.result)}</pre></details></div></details>)}{!workItems.length&&<p className="microcopy">{mine.length?'没有符合筛选的分工。':'成员已在本次授权范围内，尚未分配实际执行步骤。'}</p>}</div>{workItems.length>12&&<div className="employee-record-controls"><span>已显示 {visibleWork.length} / {workItems.length}</span>{visibleWork.length<workItems.length&&<button className="text-button" onClick={()=>setWorkLimit(n=>n+12)}>再显示 12 项分工</button>}</div>}{run.artifact_digest&&<div className="employee-delivery-link"><span className="employee-delivery-icon"><FolderGit2 size={21}/></span><div><strong>本任务已有交付成果</strong><p>文件、结论与验证记录</p></div><button type="button" onClick={()=>p.onOpenRun(id)}>查看本任务成果<ArrowRight size={16}/></button></div>}</>}
 {tab==='approval'&&<div className="employee-approvals">{myPhase&&<><button className="text-button" onClick={()=>p.onOpenRun(id)}>先查看完整计划 / 成果<ArrowRight size={13}/></button><TaskApprovals key={id+run.status+(run.plan_digest||'')+(run.artifact_digest||'')} run={run} writer={p.writer&&!error} onRefresh={refresh}/></>}{phase&&!myPhase&&<p className="callout">这份{run.status==='awaiting_approval'?'计划':'成果验收'}归属 {names[owner]||'任务负责人'}，请点击对应员工或打开完整任务处理。</p>}{questions.filter((q:Json)=>q.kind==='question').map((q:Json)=><Question key={q.id+q.digest} q={q} names={aid=>names[aid]||'成员'} writer={p.writer&&!closed&&!error} onDone={()=>void refresh()}/>)}{questions.some((q:Json)=>!q.kind)&&<p className="form-error">申请类型尚未确认，不会用普通问答授予权限。</p>}<ExecutionCenter key={id+member} compact agentId={member} initialRunId={id} initialProjectId={run.project_id} runs={[run]} projects={p.projects} agentNames={names} refreshKey={run.event_cursor} admin={p.admin&&!closed&&!error} onNavigate={p.onNavigate} onRefresh={refresh}/>{!pending&&<p className="microcopy">没有等待这位员工处理的问答或计划决定。</p>}</div>}
 {tab==='messages'&&<><div className="employee-section-heading"><h4>通信记录 <small>{messages.length}{data.more?'+':''}</small></h4><span>最新在前 · 实时同步</span></div>{!closed&&p.writer&&<section className="employee-steer-composer" aria-label="向当前成员发送运行中消息"><textarea rows={3} value={steerText} onChange={e=>setSteerText(e.target.value)} onKeyDown={e=>{if((e.metaKey||e.ctrlKey)&&e.key==='Enter'){e.preventDefault();void sendSteer();}}} placeholder={`给 ${p.agent.name} 发送运行中指令或补充信息…`} disabled={steerBusy}/>{steerFiles.length>0&&<div className="employee-attachment-chips" aria-label="待发送附件">{steerFiles.map((file,index)=><span className="employee-attachment-chip" key={`${file.name}-${file.lastModified}-${index}`}><Paperclip size={12}/><b title={file.name}>{file.name}</b><small>{Math.max(1,Math.round(file.size/1024))} KB</small><button type="button" aria-label={`移除附件 ${file.name}`} onClick={()=>setSteerFiles(files=>files.filter((_,i)=>i!==index))} disabled={steerBusy}>×</button></span>)}</div>}<div className="employee-steer-actions"><label className="button secondary small"><Paperclip size={14}/>添加附件<input type="file" multiple hidden accept="image/*,.zip,.tar,.gz,.txt,.md,.json,.pdf,.doc,.docx" onChange={e=>{setSteerFiles(files=>[...files,...Array.from(e.target.files||[])].slice(0,8));e.currentTarget.value='';}}/></label><span>{steerFiles.length?'按 ⌘/Ctrl + Enter 发送':'支持图片、文件和压缩包'}</span><button className="button primary small" disabled={steerBusy||(!steerText.trim()&&!steerFiles.length)} onClick={()=>void sendSteer()}>{steerBusy?'发送中…':<><Send size={14}/>发送给该成员</>}</button></div>{steerError&&<p className="form-error" role="alert">{steerError}</p>}{steerNotice&&<p className="callout" role="status">{steerNotice}</p>}</section>}<div className="employee-live-output" aria-live="polite"><header><b>实时 Agent 输出</b><small>{run.status==='running'?'运行中':'最近输出'}</small></header>{liveText&&<div className="employee-live-bubble"><span>{liveText}</span></div>}{liveEvents.map(e=><div key={e.id} className={`employee-live-event event-${e.kind.replaceAll('.','-')}`}><time>{new Date(e.created_at).toLocaleTimeString()}</time><span>{e.data?.text||e.data?.delta||e.data?.summary||e.data?.tool||e.kind}</span></div>)}</div><div className="employee-messages employee-record-scroll" tabIndex={0} role="region" aria-label="员工通信列表">{[...messages].reverse().map(e=><details key={e.id} className={e.data?.from_agent_id===member?'is-outgoing':e.data?.to_agent_id===member?'is-incoming':'is-system'}><summary><small>#{e.id} · {new Date(e.created_at).toLocaleTimeString()}</small><b>{employeeMessage(e,member,names).title}</b><span>{employeeMessage(e,member,names).parties}</span><em className="employee-message-preview">{employeeMessageText(e)}</em></summary><div className="employee-message-content employee-long-content" tabIndex={0} aria-label="完整通信内容"><p>{employeeMessageText(e)}</p><details><summary>成员与交接依据</summary><pre>{jsonText(e.data||{})}</pre></details></div></details>)}{!messages.length&&<p className="microcopy">当前列表内没有该员工的通信证据，不表示其他任务没有通信。</p>}</div>{data.more&&<div className="employee-record-controls">{limit<300?<button className="text-button" onClick={()=>setLimit(n=>Math.min(300,n+60))}>查看更多本任务通信</button>:<p className="microcopy">已显示最近 300 条匹配记录。<button className="text-button" onClick={()=>p.onOpenRun(id)}>打开完整任务追溯更早记录</button></p>}</div>}</>}
 <button className="employee-open-task" onClick={()=>p.onOpenRun(id)}>打开完整任务与协作图<ArrowRight size={14}/></button>
 <details className="employee-context-note"><summary>本次运行 · {id.slice(0,8)}</summary>{workstation&&<p>本次运行冻结工位：{workstation.floor} 楼 · {workstation.department} · {workstation.team_name} · {workstation.desk_name}。当前布局的后续编辑不会改写此记录。</p>}<p>阶段提交不等于成果验收。不同任务的文件、结论与验证记录分别保存。</p></details></>;
}
