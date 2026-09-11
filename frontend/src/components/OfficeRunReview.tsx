import {useEffect,useRef,useState} from 'react';
import {api} from '../api';
import {ForgeEvent,Run,Task} from '../types';
import {verificationSummary} from '../taskPresentation';
import {Badge,Modal} from './UI';
import {ArrowRight,Download,FolderGit2,ShieldCheck} from './Icons';
import TaskFlow,{TaskOutputs} from './TaskFlow';

/** Exact-run read-only review. Closing it never unmounts the office or submits approval. */
export default function OfficeRunReview(p:{id:string;preview?:boolean;onClose:()=>void;onBoard:()=>void;onTask:(task:Task)=>void}){
 const[data,setData]=useState<{run:Run;events:ForgeEvent[]}|null>(null),[error,setError]=useState(''),[tab,setTab]=useState<'progress'|'results'>('progress');
 const[previewFile,setPreviewFile]=useState(''),[previewText,setPreviewText]=useState(''),[previewError,setPreviewError]=useState(''),[previewBusy,setPreviewBusy]=useState(false);
 const first=useRef(true);
 useEffect(()=>{let alive=true,sequence=0;first.current=true;setData(null);setError('');setPreviewFile('');
  const load=async()=>{const ticket=++sequence;try{const[run,trace]=await Promise.all([api<Run>('/runs/'+p.id),api<{items:ForgeEvent[]}>('/runs/'+p.id+'/trace?limit=100')]);if(!alive||ticket!==sequence)return;if(run.id!==p.id)throw new Error('任务返回范围不匹配，请关闭后重试。');setData({run,events:(trace.items||[]).filter(e=>e.run_id===p.id)});setError('');if(first.current){setTab(run.artifact_digest?'results':'progress');first.current=false;}}catch(e){if(alive&&ticket===sequence)setError((e as Error).message);}};
  if(!p.preview)void load();const timer=p.preview?null:setInterval(()=>void load(),6000);return()=>{alive=false;if(timer)clearInterval(timer);};
 },[p.id,p.preview]);
 useEffect(()=>{if(!previewFile||p.preview)return;let alive=true;const controller=new AbortController();setPreviewText('');setPreviewError('');setPreviewBusy(true);
  (async()=>{try{const response=await fetch(`/api/runs/${p.id}/artifacts/${previewFile}`,{credentials:'same-origin',headers:{'X-Forge-Request':'1'},signal:controller.signal});if(!response.ok)throw new Error('无法读取成果预览，请稍后重试。');const reader=response.body?.getReader();if(!reader)throw new Error('无法读取文件流。');const decoder=new TextDecoder();let size=0,value='',truncated=false;while(true){const part=await reader.read();if(part.done)break;size+=part.value.byteLength;if(size>180000){truncated=true;await reader.cancel();break;}value+=decoder.decode(part.value,{stream:true});}value+=decoder.decode();if(alive)setPreviewText(value+(truncated?'\n\n[预览已截断，可下载完整文件]':''));}catch(e){if(alive)setPreviewError((e as Error).message);}finally{if(alive)setPreviewBusy(false);}})();return()=>{alive=false;controller.abort();};
 },[p.id,previewFile,p.preview]);
 const run=data?.run,verification=run?verificationSummary(run):null;
 const discussion=run?.report?.mode==='discussion';
 const files=discussion?[['discussion.md','讨论纪要'],['discussion.json','结构化记录'],['report.json','完整交付报告']]:[['source.zip','完整成果包'],['changes.patch','代码与文档变更'],['report.json','完整交付报告']];
 return <Modal wide title="任务概览与成果" onClose={p.onClose}><div className="office-run-review">
  {p.preview?<p className="callout">场景预览不读取真实任务成果。</p>:<>{error&&<p className="form-error" role="alert">{error}</p>}{!run?<p role="status">正在读取这个任务的真实记录…</p>:<>
   <header><span className="office-review-kicker">RUN · {run.id.slice(0,8)}</span><h2>{run.title}</h2><Badge state={run.status}/><p>查看后关闭即可回到当前员工。审批仍在员工的「审批」中处理，不会因预览成果自动生效。</p></header>
   <nav className="office-review-tabs"><button className={tab==='progress'?'selected':''} onClick={()=>setTab('progress')}>执行计划与进度</button><button className={tab==='results'?'selected':''} onClick={()=>setTab('results')}>成果与验证</button><button onClick={p.onBoard}>进入完整任务<ArrowRight size={14}/></button></nav>
   {tab==='progress'?<><TaskFlow run={run} onTask={p.onTask}/><section className="office-review-section"><h3>本次需求</h3><p>{run.request}</p><details><summary>查看执行计划</summary><p>{run.plan?.summary||'尚未生成计划'}</p>{run.plan?.tasks?.map((t:any)=><article key={t.key}><h4>{t.title}</h4><p>{t.description}</p><p>验收：{Array.isArray(t.acceptance)?t.acceptance.join('；'):t.acceptance||'见任务要求'}</p></article>)}</details></section></>:<>
    <section className="office-review-section"><h3><FolderGit2 size={18}/>本次交付</h3><p>{run.artifact_digest?run.status==='completed'?'以下是本次已验收的冻结成果。':'成果已保存，但不代表已经验收。':'当前还没有最终成果包；下方只展示已产生的工作证据。'}</p>{run.artifact_digest&&<div className="office-artifact-grid">{files.map(([file,label])=><article key={file}><FolderGit2 size={23}/><b>{label}</b><small>{file}</small><a href={`/api/runs/${p.id}/artifacts/${file}`}><Download size={14}/>下载</a>{['changes.patch','discussion.md'].includes(file)&&<button onClick={()=>setPreviewFile(file)}>在此预览</button>}</article>)}</div>}{previewFile&&<div className="office-artifact-preview"><header><b>{previewFile}</b><button onClick={()=>setPreviewFile('')}>收起预览</button></header>{previewBusy&&<p role="status">正在读取文件…</p>}{previewError&&<p role="alert" className="form-error">{previewError}</p>}<pre>{previewText}</pre></div>}</section>
    <TaskOutputs run={run} events={data!.events} onTask={p.onTask}/><section className="office-review-section"><h3><ShieldCheck size={18}/>真实验证结果</h3><p>{verification?.checkLabel} · {verification?.reviewLabel}</p><p>{verification?.review.summary||(run.execution_strategy==='autonomous'||run.snapshot?.execution_strategy==='autonomous'?'按本次实际交付与执行依据验收。':'尚未返回审查意见。')}</p>{verification?.commands.map((command:any,index:number)=><details key={index}><summary>{command.exit_code===0&&!command.timed_out&&!command.truncated?'通过':'需要检查'} · {command.command} · 退出码 {command.exit_code??'未知'}</summary><pre>{command.output||'无输出'}</pre></details>)}</section>
   </>}
  </>}</>}
 </div></Modal>;
}
