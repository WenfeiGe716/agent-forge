import type {ForgeEvent,Json,Run,Task} from './types';

export const flowText=(value:unknown,fallback='')=>typeof value==='string'?value:fallback;
const rows=(value:unknown):Json[]=>Array.isArray(value)?value.filter(item=>item&&typeof item==='object'&&!Array.isArray(item)):[];
const strings=(value:unknown)=>Array.isArray(value)?value.filter((item):item is string=>typeof item==='string'):[];
const complete=(task:Task|undefined)=>!!task&&['done','integrated','conflicted','superseded'].includes(task.status);
export type FlowNode={key:string;title:string;agentId:string;dependencies:string[];acceptance:string;description:string;task?:Task;level:number|null};
export type FlowGroup={key:string;title:string;description:string;tasks:Task[]};
export const flowMember=(run:Run,id:string)=>flowText(run.snapshot?.agents?.[id]?.name,id?'成员 '+id.slice(0,8):'未指定成员');
export function flowStatus(task:Task|undefined,run:Run){
 if(!task)return '计划项 · 尚未创建执行阶段';
 if(task.kind==='autonomous'&&['done','integrated'].includes(task.status)){const outcome=task.result?.outcome;if(outcome==='waiting')return '已让出执行 · 等待成员消息';if(outcome==='ready')return '已提交候选交付';if(outcome==='blocked')return '已报告阻塞 · 未完成';}
 return ({pending:run.status==='awaiting_approval'?'待计划批准':'尚未开始',queued:'已排队',running:'执行中',waiting_decision:'等待人工决定',done:'已提交',conflicted:'已提交 · 合并冲突',superseded:'已由修复工作替代',integrated:'已集成',failed:'未通过',needs_attention:'需处理',unverified:'未验证',cancelled:'已取消'} as Record<string,string>)[task.status]||'状态：'+task.status;
}
function order(task:Task){const match=task.key.match(/_(\d+)(?:_(\d+))?$/);return (Number(task.spec?.order??match?.[2]??0));}
function round(task:Task,prefix:string,field='round'){const value=task.spec?.[field];if(Number.isInteger(value)&&value>=0)return value;const match=task.key.match(new RegExp('^'+prefix+'_(\\d+)(?:_\\d+)?$'));return match?Number(match[1]):null;}
export function buildTaskFlow(run:Run){
 const tasks=Array.isArray(run.tasks)?run.tasks:[],actual=new Map(tasks.map(task=>[task.key,task]));
 const plan=rows(run.plan?.tasks),nodes:FlowNode[]=[],warnings:string[]=[],seen=new Set<string>();
 const add=(item:Json,task?:Task)=>{
  const key=flowText(item.key);if(!key){warnings.push('存在缺少标识的计划项，无法确定它的依赖关系。');return;}
  if(seen.has(key)){warnings.push('存在重复计划标识 '+key+'，未重复绘制。');return;}seen.add(key);
  nodes.push({key,title:flowText(task?.title,flowText(item.title,key)),agentId:flowText(task?.agent_id,flowText(item.agent_id)),dependencies:[...new Set(strings(item.depends_on))],acceptance:flowText(item.acceptance),description:flowText(item.description),task,level:null});
 };
 for(const item of plan)add(item,actual.get(item.key));
 for(const task of tasks)if(['develop','autonomous'].includes(task.kind)&&!/^rework_\d+$/.test(task.key)&&!seen.has(task.key))add({...task.spec,key:task.key,title:task.title,agent_id:task.agent_id},task);
 const unresolved=new Set(nodes.map(node=>node.key)),levels=new Map<string,number>();
 while(unresolved.size){let changed=false;for(const node of nodes){if(!unresolved.has(node.key)||!node.dependencies.every(key=>levels.has(key)))continue;node.level=node.dependencies.length?Math.max(...node.dependencies.map(key=>levels.get(key)!))+1:0;levels.set(node.key,node.level);unresolved.delete(node.key);changed=true;}if(!changed)break;}
 if(unresolved.size)warnings.push('部分依赖缺失或存在循环，已放入“依赖待检查”；不会把它们显示为可直接执行。');
 const depth=nodes.length?Math.max(-1,...nodes.map(node=>node.level??-1))+1:0;
 const lanes=[...new Set(nodes.map(node=>node.agentId))].map(agentId=>({agentId,name:flowMember(run,agentId),nodes:nodes.filter(node=>node.agentId===agentId)}));
 const groups:FlowGroup[]=[],used=new Set(nodes.flatMap(node=>node.task?[node.task.id]:[]));
 const group=(key:string,title:string,description:string,found:Task[])=>{if(!found.length)return;found.forEach(task=>used.add(task.id));groups.push({key,title,description,tasks:found});};
 group('intake','目标与计划','只显示已经创建的需求理解与计划阶段。',tasks.filter(task=>['intake','plan'].includes(task.kind)));
 const discussion=tasks.filter(task=>task.kind==='discussion'||task.kind==='goal_review');
 const cycles=[...new Set(discussion.map(task=>task.kind==='goal_review'?round(task,'goal_review','cycle'):round(task,'discussion')))];
 cycles.sort((a,b)=>(a??Infinity)-(b??Infinity));
 for(const cycle of cycles){const found=discussion.filter(task=>(task.kind==='goal_review'?round(task,'goal_review','cycle'):round(task,'discussion'))===cycle);const contributions=found.filter(task=>task.kind==='discussion').sort((a,b)=>order(a)-order(b));const strategies=[...new Set(contributions.map(task=>flowText(task.spec?.strategy)).filter(Boolean))];const strategy=strategies.length===1?strategies[0]:'';
  group('discussion:'+cycle,cycle===null?'讨论与目标复核':'讨论轮次 '+cycle,strategy==='parallel'?'本轮按并行讨论配置；实际是否同时运行，仍以成员状态为准。':strategy==='sequential'?'本轮按顺序发言配置；保留各成员结论与目标复核。':'按已记录轮次分组，不推测成员的执行顺序。',[...contributions,...found.filter(task=>task.kind==='goal_review')]);
 }
 group('action-plan','整理行动方案','行动方案是提议，不表示其中的工作已经执行。',tasks.filter(task=>task.kind==='action_plan'));
 const checks=tasks.filter(task=>task.kind==='check'||task.kind==='review'||/^rework_\d+$/.test(task.key));
 const rounds=[...new Set(checks.map(task=>round(task,task.kind==='check'?'checks':task.kind==='review'?'review':'rework')))];rounds.sort((a,b)=>(a??Infinity)-(b??Infinity));
 for(const value of rounds){const found=checks.filter(task=>round(task,task.kind==='check'?'checks':task.kind==='review'?'review':'rework')===value);found.sort((a,b)=>({develop:0,check:1,review:2}[a.kind as 'develop'|'check'|'review']??3)-({develop:0,check:1,review:2}[b.kind as 'develop'|'check'|'review']??3));group('validation:'+value,value===null?'验证与修正记录':value===0?'首次验证与审查':'修正与复核 · 第 '+value+' 轮','只展示已发生的轮次；前一轮失败不会被后续结果覆盖。',found);}
 group('other','其他执行阶段','保留实际阶段，不强行归类为开发或测试。',tasks.filter(task=>!used.has(task.id)));
 return {nodes,lanes,depth,unresolved,warnings,groups,stats:{running:tasks.filter(task=>task.status==='running').length,waiting:tasks.filter(task=>task.status==='waiting_decision').length,submitted:tasks.filter(complete).length,created:tasks.length},parallelism:run.snapshot?.runtime?.max_parallel};
}

export function fileCategory(path:string){
 const ext=path.split('.').pop()?.toLowerCase()||'';
 if(['md','txt','pdf','doc','docx','rtf','ppt','pptx'].includes(ext))return '文档';
 if(['csv','xlsx','xls','json','jsonl','parquet','sqlite'].includes(ext))return '数据';
 if(['png','jpg','jpeg','svg','webp','gif','mp4','wav','mp3'].includes(ext))return '素材';
 if(['py','ts','tsx','js','jsx','go','rs','java','c','cpp','h','html','css','sql','sh'].includes(ext))return '代码';
 return '其他文件';
}
export function taskOutputEvidence(run:Run,events:ForgeEvent[]=[]){
 // A caller may hold an old event buffer while switching tasks. Never mix scopes.
 const recorded=[...new Map(events.filter(event=>event.run_id===run.id).map(event=>[event.id,event])).values()].sort((a,b)=>a.id-b.id);
 const files=new Map<string,{eventId:number;taskKey:string;agentId:string;path:string;bytes:number|null;deleted:boolean;category:string}>();
 for(const event of recorded){const data=event.data||{},result=data.result||{},path=flowText(result.written||result.deleted);if(event.kind!=='tool.completed'||!['write_file','delete_file'].includes(data.tool)||result.error||!path)continue;const key=JSON.stringify([data.task_key,path]);files.set(key,{eventId:event.id,taskKey:flowText(data.task_key),agentId:flowText(data.agent_id),path,bytes:Number.isFinite(result.bytes)?result.bytes:null,deleted:data.tool==='delete_file',category:fileCategory(path)});}
 const messages=new Map<string,{eventId:number;from:string;to:string;body:string;delivered:boolean;hasSend:boolean}>();
 for(const event of recorded){if(!['agent.message','agent.message.delivered'].includes(event.kind))continue;const data=event.data||{},id=flowText(data.message_id);if(!id)continue;const previous=messages.get(id);messages.set(id,{eventId:event.id,from:flowText(data.from_agent_id,previous?.from),to:flowText(data.to_agent_id,previous?.to),body:event.kind==='agent.message'?flowText(data.body,flowText(data.summary)):previous?.body||'',delivered:previous?.delivered||event.kind==='agent.message.delivered',hasSend:previous?.hasSend||event.kind==='agent.message'});}
 const handoffs=recorded.filter(event=>['task.handoff','orchestration.handoff'].includes(event.kind));
 const commands=recorded.filter(event=>event.kind==='check.completed'||event.kind==='tool.completed'&&['run_command','run_approved_command','run_skill_script'].includes(event.data?.tool)).map(event=>({eventId:event.id,taskKey:flowText(event.data?.task_key),result:event.kind==='check.completed'?event.data:event.data?.result||{}}));
 const submissions=(run.tasks||[]).filter(task=>complete(task)&&[task.result?.summary,task.result?.notes].some(value=>!!flowText(value).trim()));
 return {files:[...files.values()],messages:[...messages.values()].sort((a,b)=>b.eventId-a.eventId),handoffs,commands,submissions,eventCount:recorded.length};
}
