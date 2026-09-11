import {useState} from 'react';
import {Run,Project,states} from '../types';
import {Badge} from './UI';
import {ArrowUpRight,FolderGit2,LayoutDashboard,ListTree,Plus,Search} from './Icons';
import {summarizeRunFailure} from './RunRecovery';
import '../run-board.css';

const columns=[
 {id:'queued',name:'待启动',statuses:['queued']},
 {id:'active',name:'执行中',statuses:['intake','planning','discussing','running','verifying','reviewing','reworking']},
 {id:'approval',name:'等待确认',statuses:['awaiting_decision','waiting_decision','awaiting_approval','awaiting_acceptance']},
 {id:'blocked',name:'暂停 / 异常',statuses:['paused','needs_attention']},
 {id:'closed',name:'完成 / 取消',statuses:['completed','cancelled']},
];
const columnPageSize=20,listPageSize=40;
const normalized=(value:string)=>value.replace(/\s+/g,' ').trim();
function updatedLabel(value:string){
 const date=new Date(value);
 return Number.isNaN(date.getTime())?'—':date.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
}

function TaskCard({run,projectName,onSelect}:{run:Run;projectName:string;onSelect:(id:string)=>void}){
 const title=run.title||'未命名任务',request=normalized(run.request||'');
 // Auto-created titles are the beginning of the request; repeating them makes
 // a small card read like two copies of the same long document.
 const showSummary=Boolean(request)&&!request.startsWith(normalized(title));
 const issue=run.status==='needs_attention'?summarizeRunFailure(run).title:run.error;
 return <button type="button" className="board-card" data-run-id={run.id} onClick={()=>onSelect(run.id)} aria-label={`查看任务：${title}`}>
  <div className="board-card-status"><Badge state={run.status}/></div>
  <div className="board-card-copy"><h3 title={title}>{title}</h3>{showSummary&&<p title={run.request}>{run.request}</p>}{issue&&<div className="board-error"><span>{issue}</span><small>查看原因与处理</small></div>}</div>
  <div className="board-card-project" title={projectName}><FolderGit2 size={13}/><span>{projectName}</span></div>
  <div className="board-card-meta"><span>{run.calls} 次调用</span><time dateTime={run.updated_at} title={run.updated_at}>{updatedLabel(run.updated_at)}</time></div>
  <span className="board-card-open" aria-hidden="true"><ArrowUpRight size={15}/></span>
 </button>;
}

export default function RunBoard({runs,projects,onSelect,onCreate}:{runs:Run[];projects:Project[];onSelect:(id:string)=>void;onCreate?:()=>void}){
 const[q,setQ]=useState(''),[project,setProject]=useState(''),[state,setState]=useState('');
 const[view,setView]=useState<'board'|'list'>('board'),[limits,setLimits]=useState<Record<string,number>>({}),[listLimit,setListLimit]=useState(listPageSize);
 const resetPages=()=>{setLimits({});setListLimit(listPageSize);};
 const query=q.trim().toLocaleLowerCase();
 const filtered=runs.filter(r=>(!project||r.project_id===project)&&(!state||r.status===state)&&(r.title+' '+r.request).toLocaleLowerCase().includes(query))
  .slice().sort((a,b)=>b.updated_at.localeCompare(a.updated_at));
 const projectNames=new Map(projects.map(p=>[p.id,p.name]));
 const knownStates=columns.flatMap(c=>c.statuses),unknownStates=Array.from(new Set(runs.map(r=>r.status).filter(s=>!knownStates.includes(s))));
 // New server statuses stay visible until this client learns their category.
 const visibleColumns=unknownStates.length?[...columns,{id:'other',name:'其他状态',statuses:unknownStates}]:columns;
 const clearFilters=()=>{setQ('');setProject('');setState('');resetPages();};
 const card=(run:Run)=><TaskCard key={run.id} run={run} projectName={projectNames.get(run.project_id)||'未命名项目'} onSelect={onSelect}/>;
 return <section className={`task-board task-board-${view}`} data-testid="task-board">
  <div className="board-toolbar">
   <div className="board-filters">
    <label className="board-search"><Search size={16}/><input aria-label="搜索看板任务" placeholder="搜索任务名称或需求" value={q} onChange={e=>{setQ(e.target.value);resetPages();}}/></label>
    <select aria-label="看板项目筛选" value={project} onChange={e=>{setProject(e.target.value);resetPages();}} title={projectNames.get(project)||'全部项目'}><option value="">全部项目</option>{projects.map(p=><option key={p.id} value={p.id}>{p.name}</option>)}</select>
    <select aria-label="看板状态筛选" value={state} onChange={e=>{setState(e.target.value);resetPages();}}><option value="">全部状态</option>{[...knownStates,...unknownStates].map(s=><option key={s} value={s}>{states[s]||s}</option>)}</select>
   </div>
   {onCreate&&<button type="button" className="button primary" onClick={onCreate}><Plus size={15}/>新建任务</button>}
  </div>
  <div className="board-result-line">
   <div className="board-result-count" aria-live="polite"><strong>{filtered.length}</strong><span>项任务</span>{filtered.length!==runs.length&&<small> / 已载入 {runs.length} 项</small>}{(q||project||state)&&<button type="button" className="text-button" onClick={clearFilters}>清除筛选</button>}</div>
   <div className="board-view-switch" role="group" aria-label="任务展示方式"><button type="button" aria-pressed={view==='board'} onClick={()=>setView('board')}><LayoutDashboard size={14}/>看板</button><button type="button" aria-pressed={view==='list'} onClick={()=>setView('list')}><ListTree size={14}/>列表</button></div>
  </div>
  {view==='board'?<div className="board-columns" tabIndex={0} aria-label="任务状态看板，可横向滚动">{visibleColumns.map(column=>{
   const items=filtered.filter(r=>column.statuses.includes(r.status)),limit=limits[column.id]||columnPageSize;
   return <section key={column.id} className="board-column" data-status={column.id} aria-label={`${column.name}，${items.length} 项任务`}>
    <header><span className="board-column-dot"/><h2>{column.name}</h2><span className="board-column-count">{items.length}</span></header>
    <div className="board-column-scroll" tabIndex={items.length?0:undefined} aria-label={`${column.name}任务列表`}>
     {items.slice(0,limit).map(card)}
     {items.length>limit&&<button type="button" className="board-load-more" onClick={()=>setLimits(current=>({...current,[column.id]:limit+columnPageSize}))}>显示更多 · 还有 {items.length-limit} 项</button>}
     {!items.length&&<div className="board-empty"><span aria-hidden="true">—</span>暂无任务</div>}
    </div>
   </section>;
  })}</div>:<div className="board-list-panel">
   <div className="board-list-head" aria-hidden="true"><span>任务</span><span>状态</span><span>项目</span><span>调用 / 最近更新</span><span/></div>
   <div className="board-list-scroll" tabIndex={0} aria-label="全部筛选任务列表">{filtered.slice(0,listLimit).map(card)}{!filtered.length&&<div className="board-empty"><Search size={22}/>{runs.length?'没有匹配的任务':'暂无任务'}</div>}{filtered.length>listLimit&&<button type="button" className="board-load-more" onClick={()=>setListLimit(current=>current+listPageSize)}>显示更多 · 还有 {filtered.length-listLimit} 项</button>}</div>
  </div>}
 </section>;
}
