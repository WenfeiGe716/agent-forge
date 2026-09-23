import {useEffect,useMemo,useRef,useState} from 'react';
import {Agent,ForgeEvent,Json,Run,Task} from '../types';
import {api} from '../api';
import {Activity,ChevronRight,X,Paperclip} from './Icons';
import TranscriptMarkdown from './TranscriptMarkdown';
import {buildTranscript,TranscriptEntry} from '../liveTranscript';

type Props={run:Run|null;events:ForgeEvent[];agents:Agent[];open:boolean;onClose:()=>void};
type Stream={key:string;agentId:string;taskKey:string;taskKeys:string[];agentName:string;taskTitle:string;status:string;entries:TranscriptEntry[];last:number;updatedAt:number;isSubagent:boolean;parentTaskKey?:string};
const terminal=(state:string)=>['done','failed','interrupted','completed','cancelled','integrated','conflicted','superseded'].includes(state);
const toolDetail=(entry:TranscriptEntry)=>[entry.arguments&&`参数\n${entry.arguments}`,entry.output&&`输出\n${entry.output}`,entry.exitCode!=null&&`退出码 ${entry.exitCode}`,entry.durationMs!=null&&`耗时 ${entry.durationMs} ms`].filter(Boolean).join('\n\n');
const entryStateLabel=(entry:TranscriptEntry)=>({running:'进行中',done:'已完成',failed:'失败',interrupted:'已中断',recorded:'已记录'} as Record<string,string>)[entry.state]||entry.state;
const streamStateLabel=(state:string)=>({pending:'等待模型输出',queued:'排队中',running:'正在处理',waiting_decision:'等待决策',awaiting_approval:'等待审批',awaiting_acceptance:'等待验收',done:'已完成',completed:'已完成',failed:'失败',interrupted:'已中断',cancelled:'已取消',integrated:'已集成',conflicted:'冲突待修复',superseded:'已被修复任务替代'} as Record<string,string>)[state]||state;
const failureText=(stream:Stream)=>stream.entries.find(entry=>entry.state==='failed')?.text||'';

/** Codex-style stream viewer. Permanent members have one conversation each;
 * bounded subagent activations get their own conversation and never pollute the
 * parent member's transcript. */
export default function LiveOutputDock({run,events,agents,open,onClose}:Props){
 const [selected,setSelected]=useState('');
 const [input,setInput]=useState(''),[files,setFiles]=useState<File[]>([]),[sending,setSending]=useState(false),[notice,setNotice]=useState('');
 const mainRef=useRef<HTMLElement|null>(null);
 const streams=useMemo(()=>{
  if(!run)return [] as Stream[];
  const tasks=run.tasks||[],names:Record<string,string>=Object.fromEntries(agents.map(a=>[a.id,a.name]));
  const map=new Map<string,Stream>();
  const isSubagent=(task?:Task)=>task?.spec?.assignment_type==='subagent';
  const ensure=(agentId:string,taskKey:string,subagent=false)=>{
   const key=subagent?`${agentId}\u0000${taskKey}`:agentId;
   if(!map.has(key)){
    const task=tasks.find(t=>t.agent_id===agentId&&((t.key||t.id)===taskKey));
    const stamp=Date.parse(String((task as any)?.updated_at||(task as any)?.created_at||run.updated_at||''));
    map.set(key,{key,agentId,taskKey,taskKeys:taskKey?[taskKey]:[],agentName:names[agentId]||run.snapshot?.agents?.[agentId]?.name||'成员',taskTitle:task?.title||run.title||'当前任务',status:task?.status||'pending',entries:[],last:0,updatedAt:Number.isFinite(stamp)?stamp:0,isSubagent:subagent,parentTaskKey:task?.spec?.source_task_key});
   }
   const stream=map.get(key)!;
   if(taskKey&&!stream.taskKeys.includes(taskKey)){
    stream.taskKeys.push(taskKey);stream.taskKey=taskKey;
    const titles=stream.taskKeys.map(k=>tasks.find(t=>t.key===k)?.title||k).filter(Boolean);
    stream.taskTitle=titles.length>1?`${titles[0]} · ${titles.length} 个阶段`:titles[0]||stream.taskTitle;
   }
   return stream;
  };
  // Keep one independent conversation for every member in this run. A member
  // can be waiting before its first event arrives, so the empty conversation
  // must still be selectable and use the same transcript surface.
  const assigned=[...(run.agent_ids||[]),...Object.keys(run.snapshot?.agents||{})];
  for(const aid of assigned){if(aid)ensure(aid,'');}
  for(const task of tasks){const aid=task.agent_id||run.snapshot?.entry_agent_id||'';if(aid)ensure(aid,task.key||task.id,isSubagent(task));}
  const byAgent=new Map<string,Task[]>();for(const task of tasks){const aid=task.agent_id||run.snapshot?.entry_agent_id||'';if(aid){const list=byAgent.get(aid)||[];list.push(task);byAgent.set(aid,list);}}
  const scoped=new Map<string,ForgeEvent[]>();
  for(const event of events)if(event.run_id===run.id){
   const data=event.data||{};
   const message=['agent.message','agent.message.delivered','team.message.sent','team.message.delivered'].includes(event.kind)||String(data.runtime_kind||'').includes('message');
   const payload=(data.payload||{}) as Json;
   const messageAgents=message?[data.from_agent_id,data.sender_id,data.to_agent_id,data.recipient_id,payload.from_agent_id,payload.sender_id,payload.to_agent_id,payload.recipient_id].map(String).filter(Boolean):[];
   const routedAgents=messageAgents.length?[...new Set(messageAgents)]:[String(data.agent_id||data.worker_id||data.from_agent_id||data.sender_id||data.connection?.worker_id||'')];
   for(const aid of routedAgents){
    if(!aid)continue;const candidates=byAgent.get(aid)||[];
    const taskKey=String(data.task_key||data.payload?._forge_task_key||data.payload?.task_key||candidates.find(t=>!terminal(t.status))?.key||candidates[0]?.key||'');
    if(!taskKey)continue;const task=candidates.find(t=>(t.key||t.id)===taskKey);const subagent=!message&&isSubagent(task);ensure(aid,taskKey,subagent);
    const key=(subagent?aid+'\u0000'+taskKey:aid)+'\u0000'+taskKey;const list=scoped.get(key)||[];list.push(event);scoped.set(key,list);
   }
  }
  for(const stream of map.values()){
   const all=stream.taskKeys.flatMap(taskKey=>scoped.get((stream.isSubagent?stream.agentId+'\u0000'+taskKey:stream.agentId)+'\u0000'+taskKey)||[]).sort((a,b)=>a.id-b.id);
   stream.entries=buildTranscript(all,run.id,stream.agentId);stream.last=all.reduce((n,e)=>Math.max(n,e.id),0);stream.updatedAt=Math.max(stream.updatedAt,...all.map(e=>Date.parse(e.created_at)||0));
   const latest=stream.entries[stream.entries.length-1];
   // A durable task state is authoritative for completed/failed activations;
   // a late delta must not resurrect a finished child in the active section.
   const taskStates=stream.taskKeys.map(key=>tasks.find(t=>(t.key||t.id)===key)?.status||'');
   const terminalTask=taskStates.find(state=>terminal(state));
   if(terminalTask)stream.status=terminalTask;
   else if(latest&&terminal(latest.state))stream.status=latest.state;
   else if(stream.entries.length)stream.status='running';
  }
  return [...map.values()].sort((a,b)=>Number(b.status==='running')-Number(a.status==='running')||b.last-a.last);
 },[run,events,agents]);
 const memberStreams=streams.filter(s=>!s.isSubagent);
 const subagentStreams=streams.filter(s=>s.isSubagent);
 const current=streams.find(s=>s.key===selected)||memberStreams[0]||streams[0];
 const activeSubagents=subagentStreams.filter(s=>!terminal(s.status));
 const completedSubagents=subagentStreams.filter(s=>terminal(s.status));
 const lastTime=(stream:Stream)=>{const age=Math.max(0,Date.now()-(stream.updatedAt||Date.now()));if(age<60_000)return'刚刚';if(age<3_600_000)return`${Math.floor(age/60_000)} 分钟前`;if(age<86_400_000)return`${Math.floor(age/3_600_000)} 小时前`;return`${Math.floor(age/86_400_000)} 天前`;};
 useEffect(()=>{const node=mainRef.current;if(node)node.scrollTop=node.scrollHeight;},[current?.key,current?.entries.length]);
 if(!open)return null;
 const send=async()=>{if(!run||!current||(!input.trim()&&!files.length)||sending)return;if(terminal(run.status)&&files.length){setNotice('任务已结束，补充说明暂不支持上传附件，请先发送文字说明。');return;}setSending(true);setNotice('');try{const refs:string[]=[];for(const file of files){const content_base64=await new Promise<string>((resolve,reject)=>{const reader=new FileReader();reader.onerror=()=>reject(new Error(`读取附件失败：${file.name}`));reader.onload=()=>{const value=String(reader.result||'');const comma=value.indexOf(',');resolve(comma>=0?value.slice(comma+1):value);};reader.readAsDataURL(file);});const value=await api<Json>(`/runs/${run.id}/attachments`,'POST',{filename:file.name,content_base64,recipient_id:current.agentId});if(value.workspace_path)refs.push(String(value.workspace_path));else if(value.attachment_id)refs.push(String(value.attachment_id));}const note=refs.length?`\n附件已上传，请从当前运行工作区读取：${refs.join(', ')}`:'';if(terminal(run.status)){await api(`/runs/${run.id}/messages`,'POST',{recipient_id:current.agentId,body:input.trim()||'请查看这条补充说明。'});setNotice('补充说明已记录，该角色下次继续任务时可见。');}else{await api(`/runs/${run.id}/control`,'POST',{action:'steer',agent_id:current.agentId,task_key:current.taskKey,prompt:(input.trim()||'请查看附件并继续当前工作。')+note,reason:'会话内补充指令'});setNotice('已发送到该角色的当前任务会话。');}setInput('');setFiles([]);}catch(e){setNotice((e as Error).message);}finally{setSending(false);}};
 return <section className="live-output-dock glass" role="dialog" aria-label="实时 Agent 输出">
  <header className="live-output-header"><div><Activity size={15}/><b>实时输出</b><small>{run?run.title:'未选择任务'}</small></div>{memberStreams.length>1&&<nav className="live-output-members" aria-label="成员实时输出">{memberStreams.map(s=><button key={s.key} className={current?.agentId===s.agentId?'selected':''} onClick={()=>setSelected(s.key)}><i className={'live-dot '+s.status}/>{s.agentName}</button>)}</nav>}<span className="live-output-count">{memberStreams.filter(s=>s.status==='running').length} 个成员运行中</span><button aria-label="关闭实时输出" onClick={onClose}><X size={15}/></button></header>
  {!run?<p className="live-output-empty">选择一个任务后，这里会按成员和任务显示真实模型输出。</p>:<div className="live-output-body">
   <section className="live-output-primary">
   <main ref={mainRef} aria-live="polite">{current?<div className="live-output-conversation">
    <div className="live-output-task-head"><div><b>{current.agentName}</b><ChevronRight size={13}/><strong>{current.taskTitle}</strong><small>{current.taskKeys.length>1?` · ${current.taskKeys.length} 个阶段`:''}</small></div><span className={'live-output-state-badge '+current.status}>{streamStateLabel(current.status)}</span></div>
    {run.request&&<article className="live-output-user-message"><small>你的需求</small><p>{run.request}</p></article>}
    <div className="live-output-progress" aria-live="polite"><span>{streamStateLabel(current.status)}</span><i/></div>
    <div className="live-output-transcript">{current.entries.map(entry=>{const detail=toolDetail(entry);if(entry.kind==='assistant')return <article key={entry.key} className="live-output-assistant"><header><b>{current.agentName}</b><small>{entry.state==='running'?'正在生成':'模型回复'}</small><time className="live-output-message-time">{new Date(entry.time).toLocaleString()}</time></header><TranscriptMarkdown text={entry.text}/>{entry.state==='running'&&<i className="live-cursor" aria-label="正在输出"/>}</article>;if(entry.kind==='thinking')return <article key={entry.key} className="live-output-thinking"><header><b>思考摘要</b><small>{entry.state==='running'?'实时整理中':'已整理'}</small><time>{new Date(entry.time).toLocaleString()}</time></header><p>{entry.text}</p></article>;if(entry.kind==='message')return <article key={entry.key} className="live-output-message"><header><b>{entry.fromAgentId===current.agentId?'发给成员':'收到成员消息'}</b><small>{entry.messageStatus}</small><time>{new Date(entry.time).toLocaleString()}</time></header><p>{entry.text}</p></article>;if(entry.kind==='notice')return <div key={entry.key} className={'live-output-status '+(entry.state==='failed'?'error':'')}><time>{new Date(entry.time).toLocaleString()}</time><span>{entry.text}</span></div>;return <details key={entry.key} className={'live-output-tool '+entry.state} open={false}><summary><time>{new Date(entry.time).toLocaleString()}</time><div><b>{entry.title||'工具调用'}</b><span>{entry.command||entry.output||'工具事件已记录'}</span></div><em>{entryStateLabel(entry)}</em></summary>{detail&&<pre>{detail}</pre>}</details>;})}{!current.entries.length&&<p className="live-output-empty">等待模型输出</p>}</div>
   </div>:<p className="live-output-empty">当前运行还没有可显示的成员输出。</p>}</main>
  {run&&current&&<footer className="live-output-composer"><div className="live-output-compose-field"><input type="file" multiple hidden id="live-output-attachment" accept="image/*,.zip,.tar,.gz,.txt,.md,.json,.pdf,.doc,.docx" onChange={e=>{setFiles(old=>[...old,...Array.from(e.target.files||[])].slice(0,8));e.currentTarget.value='';}}/><label htmlFor="live-output-attachment" title="添加图片、文件或压缩包"><Paperclip size={15}/></label><span className="live-output-recipient" aria-label={`当前接收者 ${current.agentName}`}><i>{current.agentName.slice(0,1)}</i><b>{current.agentName}</b></span><textarea aria-label="补充指令" value={input} onChange={e=>setInput(e.target.value)} onKeyDown={e=>{if((e.metaKey||e.ctrlKey)&&e.key==='Enter'){e.preventDefault();void send();}}} placeholder={`给 ${current.agentName} 补充说明，继续“${current.taskTitle}”…`} rows={2}/>{files.length>0&&<small className="live-output-files">{files.map(f=>f.name).join(' · ')}</small>}<button type="button" disabled={(!input.trim()&&!files.length)||sending} onClick={()=>void send()}>{sending?'发送中…':'发送'}<small>⌘↵</small></button></div>{notice&&<span role="status">{notice}</span>}</footer>}
   </section>
   <aside className="live-output-subagents" aria-label="子智能体"><header><b>子智能体</b><button type="button" aria-label="返回成员输出" onClick={()=>setSelected(memberStreams[0]?.key||'')}><X size={14}/></button></header><div className="live-subagent-count">已开启 · {activeSubagents.length}</div>{activeSubagents.length?<div className="live-subagent-list">{activeSubagents.map(s=><button key={s.key} className={current?.key===s.key?'selected':''} onClick={()=>setSelected(s.key)}><span className="live-subagent-icon"><Activity size={13}/></span><span><b>{s.taskTitle}</b><small>{s.agentName} · {streamStateLabel(s.status)} · {lastTime(s)}</small>{failureText(s)&&<em>{failureText(s)}</em>}</span></button>)}</div>:<p className="live-subagent-empty">没有已开启的子代理</p>}<div className="live-subagent-count">完成 · {completedSubagents.length}</div>{completedSubagents.length>0&&<div className="live-subagent-list completed">{completedSubagents.map(s=><button key={s.key} className={current?.key===s.key?'selected':''} onClick={()=>setSelected(s.key)}><span className="live-subagent-icon"><Activity size={13}/></span><span><b>{s.taskTitle}</b><small>{s.agentName} · {streamStateLabel(s.status)} · {lastTime(s)}</small>{failureText(s)&&<em>{failureText(s)}</em>}</span></button>)}</div>}</aside>
  </div>}

 </section>;
}
