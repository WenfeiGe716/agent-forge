import {addV07Demo} from './previewV07';
/** Isolated in-memory adapter for the downloadable interactive example.
 * Production main.tsx never imports or mounts this module. No real model calls.
 * The same App/forms/board/template editor render against explicit sample data.
 */
import {createRoot} from 'react-dom/client';
import App from './App';
import {defaultGraph} from './components/Orchestration';
import {defaults} from './office/behavior';
const date=()=>new Date().toISOString();
export function mountPreview(){
 const names=['Atlas','Nova','Vega','Milo','Echo','Sage','Luna','Juno','Kai','Wren','Flint','Pax','Rio','Cade'],roles=['planner','developer','developer','developer','tester','reviewer','tester','reviewer','developer','developer','planner','tester','reviewer','developer'];
 let seq=10,cursor=1;
 const runtime={max_steps:24,max_model_calls:100,task_timeout_s:3600,call_timeout_s:120,command_timeout_s:120,max_parallel:3,max_tasks:20,max_rework_rounds:2,cpu:1,memory_mb:1024,max_file_bytes:200000,max_context_chars:120000,max_agent_messages:200};
 const db:any={users:[{id:'admin',username:'admin',role:'admin',enabled:true}],agents:names.map((name,i)=>({id:'demo-'+name,name,role:roles[i],model_profile_id:'model-'+(i%3),fallback_model_ids:[],instructions:'示例配置，不调用模型',runtime:{},model_parameters:{},version:1,enabled:true,reports_to:i===0?null:'demo-Atlas',peer_reception:'sit'})),providers:[0,1,2].map(i=>({id:'provider-'+i,name:['供应商 A · 示例','供应商 B · 示例','本地模型 · 示例'][i],protocol:i===1?'anthropic_messages':'openai_chat',base_url:'http://example.invalid/v1',external:false,enabled:true,has_key:false,revision_id:'preview'})),models:[0,1,2].map(i=>({id:'model-'+i,name:['规划模型','开发模型','本地模型'][i],model_id:'example-model-'+i,provider_id:'provider-'+i,parameters:{max_tokens:4096},allowed_parameters:['max_tokens','temperature'],supports_tools:true,enabled:true,version:1})),projects:[{id:'project',name:'团队开发空间 · 示例',import_path:'example-project',instructions:'完整界面示例，不读取本机目录',sandbox_image:'example/tools:local',check_commands:['example-test'],allow_external_models:false,runtime:{},members:[],base_commit:'preview',owner_id:'admin',archived:false}],orchestrations:[],runs:[],events:[],audit:[]};
 db.orchestrations=[{id:'tpl-pair',version:1,enabled:true,name:'双开发者接力讨论',description:'两位开发者按验收目标协作，缺少证据时继续，满足后交人工验收。',graph:{...defaultGraph,mode:'discussion',participants:['demo-Nova','demo-Vega']}},{id:'tpl-development',version:1,enabled:true,name:'开发前跨角色评审',description:'架构、开发和测试并行讨论，再进入规划与开发。',graph:{...defaultGraph,mode:'development',strategy:'parallel',participants:['demo-Atlas','demo-Nova','demo-Echo']}}];
 const sampleRun=(id:string,title:string,status:string,participants=['demo-Atlas','demo-Nova','demo-Vega','demo-Echo','demo-Sage'])=>({id,title,status,project_id:'project',request:'示例需求：澄清接口契约、修改实现并验证回归。',calls:0,input_tokens:0,output_tokens:0,created_at:date(),updated_at:date(),error:'',snapshot:{runtime,agents:Object.fromEntries(db.agents.filter((a:any)=>participants.includes(a.id)).map((a:any)=>[a.id,{...a,models:[{profile_id:a.model_profile_id}]}]))},tasks:[],commands:[],plan_digest:'preview-plan',artifact_digest:'',report:{},event_cursor:cursor});
 db.runs=[sampleRun('preview-run','接口契约与实现 · 示例运行','running'),sampleRun('preview-approved','需求拆分 · 示例待审批','awaiting_approval'),sampleRun('preview-complete','回归测试 · 示例已完成','completed'),sampleRun('preview-blocked','依赖安装 · 示例待处理','needs_attention')];
 db.runs[1].plan={summary:'示例计划，批准只改变当前页面数据。',tasks:[{key:'code',title:'实现接口',description:'界面示例不执行代码',agent_id:'demo-Nova',depends_on:[],acceptance:'示例验收'}]};
 db.runs[0].tasks=['demo-Atlas','demo-Nova','demo-Vega','demo-Echo','demo-Sage'].map((id,i)=>({id:'task-'+i,key:'work-'+i,agent_id:id,title:['规划需求','实现接口','审阅边界','补充测试','独立审查'][i],status:'running',kind:i===4?'review':'develop',spec:{},result:{}}));
 let office={...defaults,idle_grace_s:90,idle_stagger_s:15,revision:1};
 const emit=(kind:string,data:any,rid='preview-run')=>{const e={id:++cursor,kind,run_id:rid,created_at:date(),data};db.events.push(e);return e;};
 const visit=(kind:string)=>{const from=kind==='leader'?'demo-Atlas':'demo-Vega';emit('agent.message',{from_agent_id:from,to_agent_id:'demo-Nova',body:kind==='leader'?'请同步接口实现进展与当前风险。（示例领导来访）':'一起确认接口边界条件。（示例同事来访）'});};
 const v07=addV07Demo(db,sampleRun,emit);
 (globalThis as any).__forgeDemo={visit,advance:v07.advance,reset:()=>location.reload()};
 function reply(data:any,status=200){return new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});}
 const assertGraph=(g:any)=>{if(!g||g.stop_when!=='goal_satisfied'||!g.criteria?.length||g.criteria.some((c:any)=>c.description.trim().length<3))throw Error('请填写明确的目标验收条件');if(new Set(g.participants).size!==g.participants.length)throw Error('成员重复');if(g.participants.length===1||g.mode==='discussion'&&g.participants.length<2)throw Error('讨论至少需要两名成员');};
 // All mutations here remain in memory; they are NOT the production validation layer.
 window.fetch=async(input:any,options:any={})=>{
  const url=String(input),path=url.replace(/^\/api/,'').split('?')[0],query=new URLSearchParams(url.split('?')[1]||''),method=options.method||'GET',body=options.body instanceof Blob?{}:options.body?JSON.parse(options.body):{};
  try{
   const extended=v07.handle(path,method,body,query);if(extended)return extended;
   if(path==='/auth/me'||path==='/auth/login')return reply(db.users[0]);
   if(path==='/auth/logout')return reply({ok:true});
   if(path==='/setup/status')return reply({needs_connection:false,has_usable_model:true,can_configure:false,mode:'preview'});
   if(path==='/settings'&&method==='PUT'){Object.assign(runtime,body.runtime||{});db.roleConfig=body.roles||db.roleConfig;}
   if(path==='/settings')return reply({runtime,roles:db.roleConfig||{planner:{max_steps:8},developer:{},tester:{},reviewer:{max_steps:12}},installation:{max_active_activities:4,max_project_mb:500},orchestrator:'preview-only',dispatcher:'connected (界面示例，不执行模型)',validation_mode:false});
   if(path==='/office/settings'){if(method==='PUT')office={...office,...body,revision:office.revision+1};return reply(office);}
   if(path==='/office/state'){
    const first=db.runs[0],active=first.status!=='completed'&&first.status!=='cancelled';
    return reply({meetings:v07.meetings(),server_time:date(),cursor,has_more:false,complete:true,settings:office,events:query.has('after')?db.events.filter((e:any)=>e.id>Number(query.get('after'))):[],presence:db.agents.map((a:any)=>{const co=db.collaboration[first.id];const occupied=co?co.meetings.find((m:any)=>m.status==='active')?.participants||(first.status==='awaiting_decision'?[co.entry_agent_id]:Object.keys(first.snapshot.agents)):Object.keys(first.snapshot.agents);const busy=active&&occupied.includes(a.id);return {agent_id:a.id,busy,phase:busy?(a.id==='demo-Nova'?'writing':'thinking'):'idle',label:busy?'处理任务':'空闲',scope:'authorized',run_id:busy?first.id:null,task_key:busy?'work':null,evidence_ids:[],idle_since:date()};})});
   }
   if(path==='/audit')return reply(db.audit);
   if(path==='/runs/preview-config')return reply({runtime,orchestration:body.orchestration,agents:body.agent_ids});
   if(path==='/runs'&&method==='POST'){
    const t=db.orchestrations.find((t:any)=>t.id===body.template_id),graph=t?.graph||body.orchestration;if(graph)assertGraph(graph);
    const r=sampleRun('preview-new-'+(++seq),body.title,'queued',body.agent_ids);r.request=body.request;(r.snapshot as any).orchestration=graph;if(body.collaboration_flow){r.snapshot.entry_agent_id=body.entry_agent_id||body.agent_ids[0];v07.create(r);}db.runs.unshift(r);emit('run.created',{title:r.title},r.id);return reply(r,202);
   }
   const runMatch=path.match(/^\/runs\/([^/]+)(?:\/(.*))?$/);
   if(runMatch){const r=db.runs.find((r:any)=>r.id===runMatch[1]);if(!r)return reply({detail:'示例任务不存在'},404);const action=runMatch[2];if(!action)return reply(r);if(action==='events')return reply({events:db.events.filter((e:any)=>e.run_id===r.id&&e.id>Number(query.get('after')||0)),has_more:false});if(action==='control'){r.status=body.action==='cancel'?'cancelled':body.action==='pause'?'paused':'running';emit('run.state',{status:r.status},r.id);return reply({accepted:true});}if(action?.startsWith('decision/')){r.status=body.approved?(action.endsWith('plan')?'running':'completed'):'cancelled';emit('run.state',{status:r.status},r.id);return reply({accepted:true});}return reply({detail:'下载须在真实平台执行后使用；此页面未生成代码成果'},409);}
   if(/^\/models\/[^/]+\/test$/.test(path))return reply({text:'仅界面示例；未发起外部模型请求'});
   const [collection,id]=path.slice(1).split('/');if(db[collection]){
    if(method==='GET')return reply(id?db[collection].find((x:any)=>x.id===id):db[collection]);
    if(collection==='orchestrations')assertGraph(body.graph);
    const at=id?db[collection].findIndex((x:any)=>x.id===id):-1;
    if(id&&at<0)return reply({detail:'对象不存在'},404);
    if(collection==='orchestrations'&&at>=0&&body.expected_version!==db[collection][at].version)return reply({detail:'模板版本冲突'},409);
    const row={...(at>=0?db[collection][at]:{}),...body,id:id||'example-'+(++seq),version:at>=0?(db[collection][at].version||0)+1:1};delete row.expected_version;if(collection==='providers'){delete row.api_key;row.has_key=false;row.revision_id='preview';}if(collection==='projects'){row.owner_id='admin';row.base_commit='preview';}
    if(at>=0)db[collection][at]=row;else db[collection].push(row);db.audit.unshift({id:++seq,kind:collection+'.updated',created_at:date(),data:{id:row.id,example:true}});return reply(row,at>=0?200:201);
   }
   return reply({detail:'独立示例不执行此操作'},404);
  }catch(e){return reply({detail:(e as Error).message},422);}
 };
 window.EventSource=class {onopen:any;onerror:any;onmessage:any;timer:any;closed=false;cursor=0;listeners:any={};constructor(url:string){const rid=url.split('/')[3];this.cursor=Number(new URLSearchParams(url.split('?')[1]).get('after')||0);this.timer=setInterval(()=>{if(this.closed)return;this.onopen?.({});for(const e of db.events.filter((e:any)=>e.run_id===rid&&e.id>this.cursor)){this.cursor=e.id;this.onmessage?.({data:JSON.stringify(e),lastEventId:String(e.id)});}},300);}close(){this.closed=true;clearInterval(this.timer);}addEventListener(name:string,fn:any){this.listeners[name]=fn;}} as any;
 createRoot(document.getElementById('root')!).render(<><div className="demo-control-bar"><b>离线界面示例 · 不调用模型</b><details><summary>开发者演示控制</summary><button onClick={()=>(globalThis as any).__forgeDemo.advance()}>示例散会 / 推进下一阶段</button><button onClick={()=>(globalThis as any).__forgeDemo.visit('peer')}>同事来访</button><button onClick={()=>(globalThis as any).__forgeDemo.visit('leader')}>领导来访</button><button onClick={()=>(globalThis as any).__forgeDemo.reset()}>重置示例</button></details></div><App/></>);
}
