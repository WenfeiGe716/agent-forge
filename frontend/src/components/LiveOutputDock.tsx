import {useMemo,useState} from 'react';
import {Agent,ForgeEvent,Json,Run,Task,states} from '../types';
import {api} from '../api';
import {Activity,ChevronRight,X,Paperclip} from './Icons';
import {Badge} from './UI';

type Props={run:Run|null;events:ForgeEvent[];agents:Agent[];open:boolean;onClose:()=>void};
type Stream={key:string;agentId:string;taskKey:string;agentName:string;taskTitle:string;status:string;text:string;events:ForgeEvent[];last:number};

const runtimeKind=(e:ForgeEvent)=>e.kind.startsWith('runtime.')?String(e.data?.runtime_kind||e.kind.slice(8).replaceAll('.','/')):e.kind;
const payload=(e:ForgeEvent):Json=>e.data?.payload&&typeof e.data.payload==='object'?e.data.payload as Json:{};
const itemType=(e:ForgeEvent)=>{const p=payload(e),item=p.item&&typeof p.item==='object'?p.item as Json:{};return String(item.type||p.item_type||e.data?.item_type||'').toLowerCase();};
const isToolEvent=(e:ForgeEvent)=>{const k=runtimeKind(e);if(k==='item/commandExecution/outputDelta'||k.startsWith('tool.'))return true;if(!['item/started','item/completed'].includes(k))return false;return /command|file|mcp|tool|function|computer/.test(itemType(e));};
const eventText=(e:ForgeEvent)=>{
 const p=payload(e), item=p.item&&typeof p.item==='object'?p.item as Json:{};
 return String(e.data?.text||e.data?.delta||p.delta||p.text||e.data?.summary||e.data?.tool||item.text||'');
};
const displayKind=(e:ForgeEvent)=>{
 const k=runtimeKind(e);
 if(k==='item/agentMessage/delta')return '模型输出';
 if(k==='item/agentMessage/created')return '模型回合';
 if(k==='item/started')return itemType(e)==='filechange'?'文件变更':itemType(e)==='commandexecution'?'命令开始':'工具开始';
 if(k==='item/completed')return itemType(e)==='filechange'?'文件完成':itemType(e)==='commandexecution'?'命令完成':'工具完成';
 if(k==='turn/started')return '回合开始';
 if(k==='turn/completed')return '回合完成';
 return ({'model.started':'模型开始','model.progress':'模型处理中','model.delta':'模型输出','model.completed':'模型完成','tool.started':'工具开始','tool.completed':'工具完成','task.failed':'执行失败'} as Record<string,string>)[k]||k;
};
const toolDetails=(e:ForgeEvent)=>{
 const p=payload(e), item=p.item&&typeof p.item==='object'?p.item as Json:{};
 const value=p.output??p.result??p.command??p.cmd??p.message??item.output??item.command??item.text;
 if(value!==undefined&&value!==null)return typeof value==='string'?value:JSON.stringify(value,null,2);
 return '';
};
const taskState=(task?:Task)=>task?.status||'pending';
const eventDate=(e?:ForgeEvent)=>{if(!e)return '';const d=new Date(e.created_at);if(Number.isNaN(d.getTime()))return '';return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')} ${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;};
const eventDay=(e?:ForgeEvent)=>eventDate(e).slice(0,10);
const eventClock=(e?:ForgeEvent)=>eventDate(e).slice(11);
const eventMillis=(e?:ForgeEvent)=>{if(!e)return 0;const value=new Date(e.created_at).getTime();return Number.isFinite(value)?value:0;};

/** Codex-style, task-isolated stream viewer. It never merges output across agents or tasks. */
export default function LiveOutputDock({run,events,agents,open,onClose}:Props){
 const [selected,setSelected]=useState('');
 const [input,setInput]=useState(''),[files,setFiles]=useState<File[]>([]),[sending,setSending]=useState(false),[notice,setNotice]=useState('');
 const streams=useMemo(()=>{
  if(!run)return [] as Stream[];
  const tasks=run.tasks||[];
  const names:Record<string,string>=Object.fromEntries(agents.map(a=>[a.id,a.name]));
  const map=new Map<string,Stream>();
  const ensure=(agentId:string,taskKey:string)=>{
   const key=`${agentId}:${taskKey}`;
   if(!map.has(key)){
    const task=tasks.find(t=>t.agent_id===agentId&&((t.key||t.id)===taskKey));
    const rawTitle=task?.title||'';
    const taskTitle=rawTitle&&rawTitle!==taskKey&&!rawTitle.endsWith(':codex')?rawTitle:run.title||'当前任务';
    map.set(key,{key,agentId,taskKey,agentName:names[agentId]||run.snapshot?.agents?.[agentId]?.name||'成员',taskTitle,status:taskState(task),text:'',events:[],last:0});
   }
   return map.get(key)!;
  };
  // Keep every assigned task visible, including tasks that have not emitted output yet.
  for(const task of tasks){const aid=task.agent_id||run.snapshot?.entry_agent_id||'';if(aid)ensure(aid,task.key||task.id);}
  const byAgent=new Map<string,Task[]>();for(const t of tasks){const a=t.agent_id||run.snapshot?.entry_agent_id||'';if(a){const list=byAgent.get(a)||[];list.push(t);byAgent.set(a,list);}}
  for(const event of [...events.filter(e=>e.run_id===run.id)].sort((a,b)=>a.id-b.id)){
   const aid=String(event.data?.agent_id||event.data?.worker_id||event.data?.from_agent_id||'');if(!aid)continue;
   const explicit=String(event.data?.task_key||payload(event).task_key||'');
   let taskKey=explicit;
   if(!taskKey){const candidates=byAgent.get(aid)||[];taskKey=(candidates.find(t=>['running','waiting_decision','awaiting_approval'].includes(t.status))||candidates[0])?.key||'';}
   if(!taskKey)continue;
   const stream=ensure(aid,taskKey);const k=runtimeKind(event),text=eventText(event);const itemId=String(payload(event).item_id||payload(event).item?.id||event.data?.item_id||'');
   // Clone event payloads before aggregating deltas. The event prop is shared with
   // the parent polling loop; mutating it here can make React render stale or
   // duplicated rows when the next SSE poll arrives.
   const copy={...event,data:{...event.data}} as ForgeEvent;
   if(k==='item/commandExecution/outputDelta'&&itemId){const previous=stream.events.find(e=>String(payload(e).item_id||payload(e).item?.id||e.data?.item_id||'')===itemId&&isToolEvent(e));if(previous){previous.data={...previous.data,text:`${String(previous.data?.text||'')}${text}`};}else stream.events.push({...copy,data:{...copy.data,text}});}else stream.events.push(copy);stream.last=Math.max(stream.last,event.id);
   // Only model deltas belong in the assistant transcript. Progress/status
   // events are rendered as compact timeline rows so they do not pollute text.
   if(k==='model.delta'||k==='item/agentMessage/delta')stream.text+=text;
   if(k==='model.completed'||k==='turn/completed'||k==='task.finished')stream.status='done';
   if(k==='task.failed'||k==='turn/failed'||k==='error')stream.status='failed';
   if(k==='model.started'||k==='turn/started'||k==='agent.started')stream.status='running';
  }
  return [...map.values()].sort((a,b)=>Number(b.status==='running')-Number(a.status==='running')||b.last-a.last);
 },[run,events,agents]);
 const current=streams.find(s=>s.key===selected)||streams[0];
 if(!open)return null;
 const send=async()=>{if(!run||!current||(!input.trim()&&!files.length)||sending)return;setSending(true);setNotice('');try{const refs:string[]=[];for(const file of files){const content_base64=await new Promise<string>((resolve,reject)=>{const reader=new FileReader();reader.onerror=()=>reject(new Error(`读取附件失败：${file.name}`));reader.onload=()=>{const value=String(reader.result||'');const comma=value.indexOf(',');resolve(comma>=0?value.slice(comma+1):value);};reader.readAsDataURL(file);});const value=await api<Json>(`/runs/${run.id}/attachments`,'POST',{filename:file.name,content_base64,recipient_id:current.agentId});if(value.workspace_path)refs.push(String(value.workspace_path));else if(value.attachment_id)refs.push(String(value.attachment_id));}const note=refs.length?`\n附件已上传，请从当前运行工作区读取：${refs.join(', ')}`:'';await api(`/runs/${run.id}/control`,'POST',{action:'steer',agent_id:current.agentId,task_key:current.taskKey,prompt:(input.trim()||'请查看附件并继续当前工作。')+note,reason:'会话内补充指令'});setInput('');setFiles([]);setNotice('已发送到该角色的当前任务会话。');}catch(e){setNotice((e as Error).message);}finally{setSending(false);}};
 return <section className="live-output-dock glass" role="dialog" aria-label="实时 Agent 输出">
  <header><div><Activity size={15}/><b>实时 Agent 输出</b><small>{run?run.title:'未选择任务'}</small></div><span className="live-output-count">{streams.filter(s=>s.status==='running').length} 个运行中</span><button aria-label="关闭实时输出" onClick={onClose}><X size={15}/></button></header>
  {!run?<p className="live-output-empty">选择一个任务后，这里会按成员和任务显示真实模型输出。</p>:<div className="live-output-body">
   <aside aria-label="成员任务列表">{streams.map(s=><button key={s.key} className={current?.key===s.key?'selected':''} onClick={()=>setSelected(s.key)}><span><i className={'live-dot '+s.status}/><b>{s.agentName}</b></span><small>{s.taskTitle}</small><em>{states[s.status]||s.status}{s.text?` · ${s.text.length} 字`:''}</em></button>)}{!streams.length&&<p>本次运行尚未分配任务。</p>}</aside>
   <main aria-live="polite">{current?<><div className="live-output-task-head"><div><b>{current.agentName}</b><ChevronRight size={13}/><strong>{current.taskTitle}</strong></div><Badge state={current.status}/></div>{current.text?<article className="live-output-assistant"><header><b>{current.agentName}</b><small>{current.status==='running'?'正在生成':'模型回复'}</small>{current.events[0]&&<time className="live-output-message-time">{eventDate(current.events[0])}</time>}</header><p>{current.text}{current.status==='running'&&<i className="live-cursor" aria-label="正在输出"/>}</p></article>:<p className="live-output-empty compact">模型开始响应后会实时显示输出。</p>}<div className="live-output-events">{current.events.filter(isToolEvent).slice(-18).map((e,i,items)=>{const summary=eventText(e),detail=toolDetails(e),expanded=(summary.length<500&&detail.length<500)||i===items.length-1&&runtimeKind(e).includes('started'),previous=items[i-1],showTime=i===0||eventDay(e)!==eventDay(previous)||eventMillis(e)-eventMillis(previous)>=300000;return <details key={e.id} className="live-output-tool" defaultOpen={expanded}><summary><time className={showTime?'':'is-muted'}>{showTime?(i===0||eventDay(e)!==eventDay(previous)?eventDate(e):eventClock(e)):''}</time><div><b>{displayKind(e)}</b><span>{summary||'工具事件已记录'}</span></div></summary>{detail&&<pre>{detail}</pre>}</details>;})}{(()=>{const statusEvents=current.events.filter(e=>{const k=runtimeKind(e);return ['model.started','model.completed','turn/started','turn/completed','task.failed','error','agent.started'].includes(k)&&((eventText(e)||'').trim()||k==='task.failed'||k==='error');}).slice(-12);return statusEvents.length>0&&<><div className="live-output-date">{eventDate(statusEvents[0])}</div>{statusEvents.map((e,i)=>{const previous=statusEvents[i-1],showTime=i===0||eventDay(e)!==eventDay(previous)||eventMillis(e)-eventMillis(previous)>=300000;return <div key={'status-'+e.id} className={'live-output-status '+(runtimeKind(e)==='error'||runtimeKind(e)==='task.failed'?'error':'')}><time className={showTime?'':'is-muted'}>{showTime?(i===0||eventDay(e)!==eventDay(previous)?eventDate(e):eventClock(e)):''}</time><span>{displayKind(e)} · {eventText(e)||'状态已更新'}</span></div>;})}</>})()}</div>{!current.text&&!current.events.length&&<p className="live-output-empty">该任务已分配，模型开始响应后会实时显示。</p>}</>:<p className="live-output-empty">选择左侧成员任务查看输出。</p>}</main>
  </div>}
  {run&&current&&<footer className="live-output-composer">
   <div className="live-output-compose-field"><input type="file" multiple hidden id="live-output-attachment" accept="image/*,.zip,.tar,.gz,.txt,.md,.json,.pdf,.doc,.docx" onChange={e=>{setFiles(old=>[...old,...Array.from(e.target.files||[])].slice(0,8));e.currentTarget.value='';}}/><label htmlFor="live-output-attachment" title="添加图片、文件或压缩包"><Paperclip size={15}/></label><textarea aria-label="补充指令" value={input} onChange={e=>setInput(e.target.value)} onKeyDown={e=>{if((e.metaKey||e.ctrlKey)&&e.key==='Enter'){e.preventDefault();void send();}}} placeholder={`给 ${current.agentName} 补充说明，继续“${current.taskTitle}”…`} rows={2}/>{files.length>0&&<small className="live-output-files">{files.map(f=>f.name).join(' · ')}</small>}</div>
   <button type="button" disabled={(!input.trim()&&!files.length)||sending} onClick={()=>void send()}>{sending?'发送中…':'发送'}<small>⌘↵</small></button>
   {notice&&<span role="status">{notice}</span>}
  </footer>}
 </section>;
}
