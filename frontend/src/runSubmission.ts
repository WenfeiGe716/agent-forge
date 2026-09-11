import {api,requestId} from './api';
import type {ExecutionReadiness} from './types';

export class RunPreflightError extends Error {
 constructor(public readiness:ExecutionReadiness){super('任务尚未开始，请先处理执行条件。');}
}

export class RunSubmissionUncertainError extends Error {
 readonly submissionUncertain=true;
 constructor(message='任务可能已创建，但尚未收到确认。请用原需求重试确认，或刷新查看任务；不要另建相同任务。'){super(message);}
}

function submissionBody(seed:Record<string,any>){
 const body={project_id:seed.project_id,title:seed.title,request:seed.request,
  agent_ids:[...seed.agent_ids],entry_agent_id:seed.entry_agent_id,
  execution_strategy:'autonomous',run_project_checks:false,collaboration_flow:false,
  office_team_id:seed.office_team_id||null,office_layout_ref:seed.office_layout_ref||null};
 // Capture nested layout identity before awaiting transport. Edits to a draft
 // must not change the preflight/creation body of an in-flight submission.
 return JSON.parse(JSON.stringify(body)) as Record<string,any>;
}

/** One explicit submission, one key. Retain each uncertain request across other
 * submissions; acknowledging one success never forgets another request's key.
 */
export function createRunSubmission(transport:typeof api=api,makeId:()=>string=requestId){
 type Entry={body:Record<string,any>;key:string;pending:Promise<any>|null;completed:any;sent:boolean};
 const entries=new Map<string,Entry>();let pendingEntry:Entry|null=null;
 const send=(seed:Record<string,any>,isCurrent:()=>boolean=()=>true):Promise<any>=>{
  if(!isCurrent())return Promise.reject(new Error('登录状态已变化，请重新提交。'));
  const body=submissionBody(seed),identity=JSON.stringify(body);
  let entry=entries.get(identity);
  if(pendingEntry){if(entry===pendingEntry)return pendingEntry.pending!;return Promise.reject(new Error('上一条需求正在提交，请稍候。'));}
  if(entry?.completed)return Promise.resolve(entry.completed);
  if(!entry){entry={body,key:makeId(),pending:null,completed:null,sent:false};entries.set(identity,entry);}
  const current=entry;
  const operation=async()=>{
   if(!isCurrent())throw new Error('登录状态已变化，请重新提交。');
   if(!current.sent){
    const readiness=await transport<ExecutionReadiness>('/execution/preflight','POST',current.body);
    if(typeof readiness?.ready!=='boolean'||!Array.isArray(readiness?.checks))throw new Error('执行检查响应不完整，任务未创建，请重新检查。');
    if(!readiness.ready)throw new RunPreflightError(readiness);
   }
   if(!isCurrent())throw new Error('登录状态已变化，未创建任务。');
   // The server checks the idempotency ledger before validating new config.
   // Re-preflighting a lost response could hide an already-created run after
   // a model or layout change. Only an explicit retry repeats this same key.
   current.sent=true;
   let run;
   try{run=await transport('/runs','POST',current.body,{'Idempotency-Key':current.key});}
   catch(error){
    const status=(error as {status?:number})?.status;
    if(typeof status==='number'&&status>=400&&status<500&&status!==408){current.sent=false;throw error;}
    // Do not echo upstream exception text: it can include server internals.
    throw new RunSubmissionUncertainError();
   }
   if(typeof run?.id!=='string'||!run.id.trim())throw new RunSubmissionUncertainError('未收到任务编号，请用原需求重试确认，或刷新查看任务；不要另建相同任务。');
   current.completed=run;return run;
  };
  pendingEntry=current;
  current.pending=operation().finally(()=>{current.pending=null;if(pendingEntry===current)pendingEntry=null;});return current.pending;
 };
 return Object.assign(send,{acknowledge(seed:Record<string,any>){
  const identity=JSON.stringify(submissionBody(seed)),entry=entries.get(identity);
  if(!entry?.completed||entry.pending)return false;
  entries.delete(identity);return true;
 }});
}
