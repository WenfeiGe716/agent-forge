/** Deterministic presentation FSM. Business state is an input, never an output.
 * There is no Math.random(), fake task event, model call, or timer that changes a task.
 */
export type Vec=[number,number,number];
export type Phase='idle'|'waiting'|'approval'|'paused'|'attention'|'thinking'|'reading'|'writing'|'testing'|'reviewing'|'communicating'|'reserved';
export type IdleAction='drink'|'phone'|'restroom'|'nap'|'stretch';
export type Pose='idle'|'walking'|'thinking'|'reading'|'typing'|'testing'|'reviewing'|'communicating'|'paused'|'attention'|IdleAction;
export type Presence={agent_id:string;busy:boolean;phase:Phase;label:string;idle_since?:string|null;run_id?:string|null;task_key?:string|null;evidence_ids:number[];scope:string;tool?:string|null;run_count?:number};
export type Event={id:number;run_id:string;kind:string;created_at:string;data:Record<string,any>};
export type Config={idle_enabled:boolean;idle_grace_s:number;idle_stagger_s:number;idle_cooldown_s:number;walk_speed:number;activities:IdleAction[];durations_s:Record<IdleAction,number>;revision?:number};
export const defaults:Config={idle_enabled:true,idle_grace_s:35,idle_stagger_s:25,idle_cooldown_s:30,walk_speed:2.3,activities:['drink','phone','restroom','nap','stretch'],durations_s:{drink:9,phone:13,restroom:14,nap:16,stretch:7}};
export const idleLabels:Record<IdleAction,string>={drink:'喝水',phone:'摸鱼看手机',restroom:'去卫生间',nap:'沙发小憩',stretch:'伸个懒腰'};
export type Packet={meetings?:import('./meetings').MeetingState[];presence:Presence[];events:Event[];cursor:number;has_more?:boolean;complete:boolean;settings:Config;server_time:string};
export type Slot={id:string;position:Vec;yaw:number};
export type AgentPlace={id:string;home:Vec;yaw:number;role?:string;reportsTo?:string|null;peerReception?:'sit'|'stand';reception?:Vec};
export type World={agents:AgentPlace[];facilities:Record<string,Slot[]>;meetingSeats?:{position:Vec;yaw:number}[];route:(from:Vec,to:Vec)=>Vec[]};
export type Transition={at:number;agent_id:string;from:string;to:string;source:'business'|'idle'|'safety';reason:string;evidence_ids:number[]};
export type ActorView={posture?:string;sitBlend?:number;visitor_id?:string;leader_visit?:boolean;visit_event_id?:number;id:string;position:Vec;yaw:number;pose:Pose;label:string;detail:string;source:'business'|'idle'|'safety';busy:boolean;visible:boolean;evidence_ids:number[];run_id:string|null;task_key:string|null;stage:string;action:IdleAction|null;facility:string|null;poseTime:number};
type Route={points:Vec[];start:number;length:number;end:number;lengths:number[];yaw:number};
type Runtime={place:AgentPlace;position:Vec;yaw:number;stage:string;action:IdleAction|null;slot:Slot|null;cycle:number;next:number;deadline:number;entered:number;route:Route|null;business:string;settle:boolean;view:ActorView};
const copy=(v:Vec):Vec=>[...v];
const distance=(a:Vec,b:Vec)=>Math.hypot(a[0]-b[0],a[2]-b[2]);
export const stableHash=(s:string)=>{let h=2166136261;for(let i=0;i<s.length;i++){h^=s.charCodeAt(i);h=Math.imul(h,16777619);}return h>>>0;};
const workPose:Record<Phase,Pose>={idle:'idle',waiting:'idle',approval:'idle',paused:'paused',attention:'attention',thinking:'thinking',reading:'reading',writing:'typing',testing:'testing',reviewing:'reviewing',communicating:'communicating',reserved:'idle'};
export class OfficeBehavior {
 readonly states=new Map<string,Runtime>();readonly trace:Transition[]=[];
 readonly messages:{event:Event;until:number;received:number}[]=[];
 private presence=new Map<string,Presence>();private leases=new Map<string,string>();private seen=new Set<number>();
 private connected=false;private received=-Infinity;private clock=0;private sequence=-1;private config:Config={...defaults};
 constructor(readonly world:World,readonly preview=false,readonly staleMs=5000){
  for(const place of [...world.agents].sort((a,b)=>a.id.localeCompare(b.id))){this.states.set(place.id,{place,position:copy(place.home),yaw:place.yaw,stage:'unknown',action:null,slot:null,cycle:0,next:Infinity,deadline:Infinity,entered:0,route:null,business:'',settle:false,view:{id:place.id,position:copy(place.home),yaw:place.yaw,pose:'idle',label:'同步状态',detail:'尚未获得全局占用快照',source:'safety',busy:true,visible:true,evidence_ids:[],run_id:null,task_key:null,stage:'unknown',action:null,facility:null,poseTime:0}});}
 }
 accept(packet:Packet,at:number){
  if(!Number.isFinite(at)||at<this.clock)throw Error('场景时间必须单调递增');
  if(!packet.complete||packet.has_more){this.disconnect(at,'事件正在补齐');return;}
  if(packet.cursor<this.sequence)return; // Ignore old HTTP responses, not arbitrary global event-ID gaps.
  const check={...defaults,...packet.settings,durations_s:{...defaults.durations_s,...packet.settings.durations_s}};
  if(!Array.isArray(packet.presence)||!Array.isArray(packet.events)||!Number.isFinite(check.walk_speed)||check.walk_speed<=0||![check.idle_grace_s,check.idle_stagger_s,check.idle_cooldown_s].every(x=>Number.isFinite(x)&&x>=0)||!check.activities.every(a=>a in idleLabels)||!Object.values(check.durations_s).every(x=>Number.isFinite(x)&&x>0)) {this.disconnect(at,'场景配置无效');return;}
  this.sequence=packet.cursor;this.received=at;this.connected=true;
  const revisionChanged=packet.settings.revision!==this.config.revision;
  this.config={...defaults,...packet.settings,durations_s:{...defaults.durations_s,...packet.settings.durations_s}};
  this.presence=new Map(packet.presence.map(p=>[p.agent_id,p]));
  for(const e of [...packet.events].sort((a,b)=>a.id-b.id)){
   if(this.seen.has(e.id))continue;this.seen.add(e.id);
   const age=Math.max(0,Date.parse(packet.server_time)-Date.parse(e.created_at));
   if(Number.isFinite(age)&&age<=6500&&['agent.message','task.delegated','task.handoff','agent.message.delivered','orchestration.handoff'].includes(e.kind))this.messages.push({event:e,until:at+6500-age,received:at});
  }
  if(this.seen.size>3000){const keep=[...this.seen].sort((a,b)=>a-b).slice(-1500);this.seen=new Set(keep);}
  if(revisionChanged)for(const a of this.states.values())if(a.view.source==='idle')this.park(a,at,'场景配置已更新');
  this.advance(at); // Receiving a task preempts a cup/phone/restroom before the next paint.
 }
 disconnect(at:number,reason='连接中断'){
  this.connected=false;this.messages.length=0;for(const a of this.states.values())this.freeze(a,at,reason);
 }
 private log(a:Runtime,to:string,source:Transition['source'],reason:string,at:number,evidence:number[]=[]){
  if(a.stage===to)return;this.trace.push({at,agent_id:a.place.id,from:a.stage,to,source,reason,evidence_ids:[...evidence]});if(this.trace.length>500)this.trace.splice(0,this.trace.length-500);a.stage=to;a.entered=at;
 }
 private release(a:Runtime){if(a.slot&&this.leases.get(a.slot.id)===a.place.id)this.leases.delete(a.slot.id);a.slot=null;a.action=null;}
 private move(a:Runtime,target:Vec,at:number,yaw:number):boolean{
  const points=this.world.route(a.position,target);if(!points.length&&distance(a.position,target)>.04){a.route=null;return false;}
  const all=[copy(a.position),...points.map(copy)];if(!all.length||distance(all[all.length-1],target)>.01)all.push(copy(target));
  const lengths:number[]=[];let length=0;for(let i=1;i<all.length;i++){length+=distance(all[i-1],all[i]);lengths.push(length);}
  a.route={points:all,start:at,length,end:at+length/this.config.walk_speed*1000,lengths,yaw};return true;
 }
 private sample(a:Runtime,at:number){const r=a.route;if(!r)return true;const moved=Math.max(0,(at-r.start)*this.config.walk_speed/1000);
  if(moved>=r.length){a.position=copy(r.points[r.points.length-1]);a.yaw=r.yaw;return true;}
  let prev=0;for(let i=0;i<r.lengths.length;i++){const end=r.lengths[i];if(moved<=end&&end-prev>1e-9){const f=(moved-prev)/(end-prev),u=r.points[i],v=r.points[i+1];a.position=[u[0]+(v[0]-u[0])*f,0,u[2]+(v[2]-u[2])*f];a.yaw=Math.atan2(v[0]-u[0],v[2]-u[2]);break;}prev=end;}return false;
 }
 private freeze(a:Runtime,at:number,reason:string){this.release(a);a.route=null;a.business='';a.next=Infinity;this.log(a,'unknown','safety',reason,at);a.view={...a.view,position:copy(a.position),pose:'idle',label:'同步状态',detail:reason,source:'safety',busy:true,visible:true,stage:'unknown',action:null,facility:null,poseTime:0,evidence_ids:[]};}
 private park(a:Runtime,at:number,reason:string){this.release(a);this.move(a,a.place.home,at,a.place.yaw);this.log(a,'return','idle',reason,at);a.next=at+this.config.idle_grace_s*1000+(stableHash(a.place.id)%Math.max(1,Math.round(this.config.idle_stagger_s*1000)));a.business='';a.settle=false;}
 private business(a:Runtime,p:Presence,at:number){
  const key=[p.run_id,p.phase,p.task_key,p.evidence_ids.join(',')].join('|');
  if(!a.business){this.release(a);this.move(a,a.place.home,at,a.place.yaw);this.log(a,'work:'+p.phase,'business','全局任务占用，打断空闲动作',at,p.evidence_ids);}
  else if(key!==a.business)this.log(a,'work:'+p.phase,'business','任务/工具事件改变执行状态',at,p.evidence_ids);
  a.business=key;let moving=false;
  if(p.phase==='paused'||p.phase==='attention'){a.route=null;}
  else {if(distance(a.position,a.place.home)>.04&&!a.route)this.move(a,a.place.home,at,a.place.yaw);moving=!this.sample(a,at);if(!moving)a.route=null;}
  a.view={id:a.place.id,position:copy(a.position),yaw:a.yaw,pose:moving?'walking':workPose[p.phase]||'idle',label:p.label,detail:moving?'已接到任务，返回工位；后台执行不等待动画':p.tool?`工具：${p.tool}`:p.scope==='reserved'?'另一受限项目占用，隐藏任务详情':'依据全局运行状态',source:'business',busy:true,visible:true,evidence_ids:[...p.evidence_ids],run_id:p.run_id||null,task_key:p.task_key||null,stage:'work:'+p.phase,action:null,facility:null,poseTime:(at-a.entered)/1000};
 }
 advance(at:number):ActorView[]{
  if(!Number.isFinite(at)||at<this.clock)throw Error('场景时间必须单调递增');this.clock=at;
  const fresh=this.connected&&(this.preview||at-this.received<=this.staleMs);
  this.messages.splice(0,this.messages.findIndex(m=>m.until>at)<0?this.messages.length:this.messages.findIndex(m=>m.until>at));
  // Release every busy actor first; reservation decisions then use a stable ID order.
  for(const a of this.states.values()){const p=this.presence.get(a.place.id);if(!fresh||!p||(!p.busy&&p.phase!=='idle')){this.freeze(a,at,!fresh?'占用快照失联或过期':'该成员占用信息缺失');continue;}if(p.busy)this.business(a,p,at);}
  for(const a of this.states.values()){
   const p=this.presence.get(a.place.id);if(!fresh||!p||p.busy||p.phase!=='idle')continue;
   if(a.business||a.stage==='unknown'){this.park(a,at,'已确认没有任务，空闲宽限期开始');}
   if(!this.config.idle_enabled||!this.config.activities.length){
    if(!['return','idle'].includes(a.stage))this.park(a,at,'空闲演绎已关闭');
    const arrived=this.sample(a,at);if(arrived){a.route=null;this.log(a,'idle','idle','停留工位',at);}
    a.view=this.idleView(a,at,arrived?'idle':'walking','待命','空闲演绎已关闭');continue;
   }
   // Each deadline uses arrival time, not the display refresh rate. Consume late frames deterministically.
   for(let iteration=0;iteration<8;iteration++){
    if(a.stage==='return'){
     const arrival=a.route?.end??at;if(!this.sample(a,at))break;a.route=null;this.log(a,'idle','idle','返回工位',arrival);a.next=Math.max(a.next,arrival+(a.settle?this.config.idle_cooldown_s*1000:0));a.settle=false;continue;
    }
    if(a.stage==='idle'||a.stage==='queue'){
     if(at<a.next)break;
     const action=this.config.activities[(stableHash(a.place.id)+a.cycle)%this.config.activities.length];
     const available=this.world.facilities[action]||[{id:action+':'+a.place.id,position:a.place.home,yaw:a.place.yaw}];
     if(!available.length){a.cycle++;a.action=null;a.next=at+1000;this.log(a,'idle','idle','本层未配置'+idleLabels[action]+'设施，跳过本次动作',at);break;}
     const slot=available.find(x=>!this.leases.has(x.id));
     if(!slot){this.log(a,'queue','idle','设施已占用，在工位等待空位',at);a.action=action;break;}
     this.leases.set(slot.id,a.place.id);a.slot=slot;a.action=action;
     if(!this.move(a,slot.position,at,slot.yaw)){this.release(a);a.next=at+10000;this.log(a,'idle','safety','路径不可达，放弃本次空闲动作',at);break;}
     this.log(a,'outbound','idle','空闲策略选择：'+idleLabels[action],at);continue;
    }
    if(a.stage==='outbound'){
     if(!this.sample(a,at))break;const arrival=a.route?.end??at;a.route=null;a.deadline=arrival+this.config.durations_s[a.action!]*1000;this.log(a,'perform:'+a.action,'idle','已到达活动地点',arrival);continue;
    }
    if(a.stage.startsWith('perform:')){
     if(at<a.deadline)break;const end=a.deadline;a.cycle++;a.settle=true;this.release(a);this.move(a,a.place.home,end,a.place.yaw);this.log(a,'return','idle','空闲动作结束，回到工位',end);a.next=end+this.config.idle_cooldown_s*1000;continue;
    }
    break;
   }
   const action=a.action;const pose=a.stage==='outbound'||a.stage==='return'?'walking':a.stage.startsWith('perform:')?action!:'idle';
   const label=a.stage==='outbound'?`去${action==='nap'?'沙发':action==='restroom'?'卫生间':action==='drink'?'喝水':idleLabels[action!]}`:a.stage==='return'?'回工位':a.stage==='queue'?`等待${idleLabels[action!]}`:action&&a.stage.startsWith('perform:')?idleLabels[action]:'待命';
   a.view=this.idleView(a,at,pose,label,'无任务时的场景演绎，不调用模型、不占用执行资源');
  }
  return this.views();
 }
 private idleView(a:Runtime,at:number,pose:Pose,label:string,detail:string):ActorView{return{id:a.place.id,position:copy(a.position),yaw:a.yaw,pose,label,detail,source:'idle',busy:false,visible:!(a.stage==='perform:restroom'),evidence_ids:[],run_id:null,task_key:null,stage:a.stage,action:a.action,facility:a.slot?.id||null,poseTime:Math.max(0,(at-a.entered)/1000)};}
 views(){return [...this.states.values()].map(a=>({...a.view,position:copy(a.view.position),evidence_ids:[...a.view.evidence_ids]}));}
 reservations(){return Object.fromEntries(this.leases);}
 audit(){return this.trace.map(t=>({...t,evidence_ids:[...t.evidence_ids]}));}
}
