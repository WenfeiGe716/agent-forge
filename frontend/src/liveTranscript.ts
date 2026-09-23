import {ForgeEvent,Json} from './types';

export type TranscriptEntry={
 key:string;kind:'assistant'|'tool'|'notice';text:string;time:string;taskKey:string;
 state:'running'|'done'|'failed'|'interrupted'|'recorded';title?:string;command?:string;
 output?:string;arguments?:string;exitCode?:number;durationMs?:number;
};
const object=(value:any):Json=>value&&typeof value==='object'&&!Array.isArray(value)?value:{};
const string=(value:any):string=>typeof value==='string'?value:'';
const printable=(value:any)=>value==null?'':typeof value==='string'?value:JSON.stringify(value,null,2);
export const runtimeKind=(event:ForgeEvent)=>event.kind.startsWith('runtime.')?String(event.data?.runtime_kind||event.kind.slice(8).replaceAll('.','/')):event.kind;
export function eventMember(event:ForgeEvent){return String(event.data?.agent_id||event.data?.worker_id||event.data?.from_agent_id||event.data?.connection?.worker_id||'');}
export function eventTask(event:ForgeEvent){return String(event.data?.task_key||event.data?.payload?._forge_task_key||event.data?.payload?.task_key||'');}
function commandText(value:any){return Array.isArray(value)?value.join(' '):string(value);}
function toolTitle(item:Json,d:Json,args:Json,command:string){
 const type=string(item.type).toLowerCase(),name=string(item.tool||item.name||d.tool||d.name);
 if(command)return '运行命令';
 if(type==='filechange')return '修改文件'+(Array.isArray(item.changes)?' · '+item.changes.map((c:Json)=>c.path).filter(Boolean).join('、'):'');
 if(type==='websearch')return '搜索网页'+(item.query?' · '+item.query:'');
 if(name){const label:Record<string,string>={read_file:'读取文件',write_file:'写入文件',list_files:'查看文件',send_message:'发送成员消息',inspect_team:'查看团队',read_inbox:'读取消息',finish:'提交阶段成果',delegate_task:'分配工作',run_command:'运行命令'};return (label[name]||name)+(args.path?' · '+args.path:'');}
 return item.server?'调用 '+item.server:'工具调用';
}

/** Convert durable event facts to ordered UI items. IDs are scoped to a
 * connection/turn/task; completion updates its original row, never appends a
 * second card. Original payloads remain untouched for other subscribers. */
export function buildTranscript(events:ForgeEvent[],runId:string,agentId:string):TranscriptEntry[]{
 const entries:TranscriptEntry[]=[],items=new Map<string,TranscriptEntry>(),seen=new Set<number>();
  const generations=new Map<string,number>(),legacyMessages=new Map<string,TranscriptEntry>();
  const pendingTools=new Map<string,TranscriptEntry[]>();
  const completedTools=new Map<string,TranscriptEntry>();
 for(const event of [...events].sort((a,b)=>a.id-b.id)){
  if(event.run_id!==runId||eventMember(event)!==agentId||seen.has(event.id))continue;seen.add(event.id);
  const d=object(event.data),p=object(d.payload),item=object(p.item),kind=runtimeKind(event),taskKey=eventTask(event);
  const base=JSON.stringify([taskKey,d.connection?.connection_id||p.threadId||p.thread_id||'']);
  if(kind==='turn/started'||kind==='model.started'){generations.set(base,(generations.get(base)||0)+1);legacyMessages.delete(base);continue;}
  const scope=JSON.stringify([base,p.turnId||p.turn_id||p.turn?.id||generations.get(base)||0]);
  const itemId=string(item.id||p.itemId||p.item_id||d.item_id||d.call_id);
  const type=string(item.type||p.item_type||d.item_type).toLowerCase();
  const text=string(d.text||d.delta||p.delta||p.text||item.text);
  const create=(entryKind:TranscriptEntry['kind'],key:string):TranscriptEntry=>{
   const entry:TranscriptEntry={key,kind:entryKind,text:'',time:event.created_at,taskKey,state:'running'};entries.push(entry);return entry;
  };
  const assistant=kind==='item/agentMessage/delta'||(type==='agentmessage'&&['item/started','item/completed'].includes(kind))||['model.delta','model.completed'].includes(kind);
  if(assistant){
   const key=itemId?scope+':message:'+itemId:'';
   let entry=key?items.get(key):legacyMessages.get(base);
   const final=kind==='item/completed'||kind==='model.completed';
   const body=text||string(d.summary);
   if(!entry&&!body)continue;
   if(!entry){entry=create('assistant',key||'message:'+event.id);if(key)items.set(key,entry);else legacyMessages.set(base,entry);}
   if(final){
    // Legacy model.completed summaries may be truncated. Prefer deltas when
    // they already contain that prefix; native completed items are full text.
    // A streamed model response is already the authoritative text. The
    // completion event may carry a shorter summary, so never replace the
    // visible transcript after deltas have arrived.
    if(body&&!entry.text)entry.text=body;
    entry.state='done';
   }else entry.text+=body;
   continue;
  }
  const tool=kind.startsWith('tool.')||kind==='item/commandExecution/outputDelta'||(['item/started','item/completed'].includes(kind)&&/command|filechange|mcp|tool|function|websearch|computer/.test(type));
  if(tool){
   const argsRaw=item.arguments??d.arguments??p.arguments;
   let args=object(argsRaw);if(typeof argsRaw==='string'){try{args=object(JSON.parse(argsRaw));}catch{}}
   const command=commandText(item.command||p.command||d.command||args.command||args.cmd);
   const name=string(item.tool||item.name||d.tool||d.name)||type;
   const fallback=scope+':tool:'+name;
   const key=itemId?scope+':tool:'+itemId:'';
   const complete=kind==='item/completed'||kind==='tool.completed';
   const delta=kind==='item/commandExecution/outputDelta';
   let entry=key?items.get(key):(complete?(completedTools.get(fallback)||(pendingTools.get(fallback)||[]).shift()):undefined);
   if(!entry){entry=create('tool',key||'tool:'+event.id);if(key)items.set(key,entry);else if(!complete){const queue=pendingTools.get(fallback)||[];queue.push(entry);pendingTools.set(fallback,queue);}}
   if(command)entry.command=command;
   // A delta contains no item descriptor. Do not erase the known tool label.
   if(!entry.title||!delta)entry.title=toolTitle(item,d,args,entry.command||'');
   if(argsRaw!==undefined)entry.arguments=printable(argsRaw);
   const result=item.result??p.result??d.result;
   const output=item.aggregatedOutput??item.output??p.output??d.output??result;
   if(delta)entry.output=(entry.output||'')+text;
   else if(output!==undefined)entry.output=printable(output);
   const code=item.exitCode??p.exitCode??object(result).exit_code??object(result).returncode;
   if(typeof code==='number')entry.exitCode=code;
   const duration=item.durationMs??p.durationMs;
   if(typeof duration==='number')entry.durationMs=duration;
   const status=string(item.status||object(result).status),error=item.error||p.error||d.error||object(result).error;
   if(error){entry.state='failed';entry.output=printable(error)+(entry.output?'\n'+entry.output:'');}
   else if(complete){entry.state=code!=null&&code!==0||['failed','declined','error'].includes(status)?'failed':status==='interrupted'?'interrupted':code===0||status==='completed'||status==='success'?'done':'recorded';if(!key)completedTools.set(fallback,entry);}
   continue;
  }
  if(['turn/completed','task.failed','turn/failed','error','task.finished'].includes(kind)){
   const turn=object(p.turn),failed=['task.failed','turn/failed','error'].includes(kind)||turn.status==='failed'||!!turn.error;
   const interrupted=turn.status==='interrupted';
   for(const [key,entry] of items){if(key.startsWith(scope+':')&&entry.state==='running')entry.state=entry.kind==='tool'?'interrupted':failed||interrupted?'interrupted':'done';}
   legacyMessages.delete(base);
   if(failed||interrupted){const entry=create('notice','notice:'+event.id);entry.state=failed?'failed':'interrupted';entry.text=printable(turn.error?.message||turn.error||p.error?.message||p.error||d.error||d.message||text||d.summary)||(failed?'执行失败，请查看任务详情。':'本回合已中断，已产生的输出保留。');}
  }
 }
 return entries.filter(entry=>entry.kind!=='assistant'||entry.text.trim());
}
