/** Event-driven visits and posture. Presentation only: never delays an executor.
 * No random choices; leadership is explicit reports_to (or the default planner).
 */
import {AgentPlace,ActorView,Event,Vec,World,Transition} from './behavior';
export type PostureView=ActorView & {posture:'seated'|'standing'|'walking'|'rising'|'sitting';sitBlend:number;visitor_id?:string;leader_visit?:boolean;visit_event_id?:number};
type Person=AgentPlace & {role?:string;reportsTo?:string|null;peerReception?:'sit'|'stand';reception?:Vec};
type Visit={event:Event;from:string;to:string;started:number;stage:'approach'|'talk'|'return';path:Vec[];distance:number;arrived:number;point:Vec;until:number;leader:boolean};
const dist=(a:Vec,b:Vec)=>Math.hypot(a[0]-b[0],a[2]-b[2]);
const cp=(p:Vec):Vec=>[...p];
const smooth=(x:number)=>{const a=Math.max(0,Math.min(1,x));return a*a*(3-2*a);};
export class OfficeEtiquette {
 private people=new Map<string,Person>();private seen=new Set<number>();private queue:Event[]=[];private visits:Visit[]=[];
 private poses=new Map<string,{blend:number;from:number;target:number;start:number}>();private clock=0;
 readonly trace:Transition[]=[];
 constructor(readonly world:World){for(const a of world.agents)this.people.set(a.id,a as Person);}
 isLeader(from:string,to:string){const sender=this.people.get(from),receiver=this.people.get(to);if(!sender||!receiver||from===to)return false;let next=receiver.reportsTo;const seen=new Set<string>();while(next&&!seen.has(next)){if(next===from)return true;seen.add(next);next=this.people.get(next)?.reportsTo;}return !receiver.reportsTo&&sender.role==='planner'&&receiver.role!=='planner';}
 accept(events:Event[],at:number,serverTime:string){
  for(const e of [...events].sort((a,b)=>a.id-b.id)){
   if(this.seen.has(e.id))continue;this.seen.add(e.id);
   const age=Date.parse(serverTime)-Date.parse(e.created_at);
   if(!Number.isFinite(age)||age< -1000||age>6500||!['agent.message','task.delegated','task.handoff','orchestration.handoff'].includes(e.kind))continue;
   const from=e.data.from_agent_id,to=e.data.to_agent_id;
   if(from===to||!this.people.has(from)||!this.people.has(to))continue;
   // Delivery is already real. The trip is a labeled spatial representation.
   this.queue.push({...e,data:{...e.data,presentation_received:at}});
  }
  this.queue=this.queue.slice(-30);if(this.seen.size>3000)this.seen=new Set([...this.seen].slice(-1500));
 }
 private route(from:Vec,to:Vec){const points=[cp(from),...this.world.route(from,to)];return points.length>1?points:[];}
 private length(path:Vec[]){let n=0;for(let i=1;i<path.length;i++)n+=dist(path[i-1],path[i]);return n;}
 private position(v:Visit,at:number){let left=Math.max(0,(at-v.started)*2.3/1000);for(let i=1;i<v.path.length;i++){const d=dist(v.path[i-1],v.path[i]);if(left<=d&&d>1e-6){const f=left/d,a=v.path[i-1],b=v.path[i];return {point:[a[0]+(b[0]-a[0])*f,0,a[2]+(b[2]-a[2])*f] as Vec,yaw:Math.atan2(b[0]-a[0],b[2]-a[2])};}left-=d;}return {point:cp(v.path.at(-1)!),yaw:0};}
 private log(a:string,from:string,to:string,event:Event,reason:string,at:number){this.trace.push({at,agent_id:a,from,to,source:'business',reason,evidence_ids:[event.id]});if(this.trace.length>250)this.trace.shift();}
 update(base:ActorView[],at:number):PostureView[]{
  this.clock=at;const map=new Map(base.map(a=>[a.id,{...a,position:cp(a.position)}]));
  const safe=(aid:string)=>{const v=map.get(aid);return v&&v.source!=='safety'&&v.visible&&!['paused','attention'].includes(v.pose);};
  this.visits=this.visits.filter(v=>{const a=map.get(v.from),b=map.get(v.to);const ok=safe(v.from)&&safe(v.to)&&a?.run_id===v.event.run_id&&b?.run_id===v.event.run_id&&at-v.event.data.presentation_received<60000;if(!ok)this.log(v.from,v.stage,'cancelled',v.event,'任务切换、失联或运行结束，取消来访演绎',at);return ok;});
  const occupied=new Set(this.visits.flatMap(v=>[v.from,v.to]));
  const remaining:Event[]=[];
  for(const e of this.queue){const from=e.data.from_agent_id,to=e.data.to_agent_id,a=map.get(from),b=map.get(to);if(at-e.data.presentation_received>25000)continue;
   if(!safe(from)||!safe(to)||a?.run_id!==e.run_id||b?.run_id!==e.run_id)continue;
   if(occupied.has(from)||occupied.has(to)){remaining.push(e);continue;}
   const host=this.people.get(to)!,home=host.home;
   if(dist(b!.position,home)>.12||b!.pose==='walking'){remaining.push(e);continue;}
   const destination=host.reception||[home[0]+1.6,0,home[2]] as Vec,path=this.route(a!.position,destination);
   if(!path.length)continue;const distance=this.length(path);occupied.add(from);occupied.add(to);
   const v:Visit={event:e,from,to,started:at,stage:'approach',path,distance,arrived:at+distance/2.3*1000,point:cp(a!.position),until:0,leader:this.isLeader(from,to)};
   this.visits.push(v);this.log(from,'desk','approach',e,`依据事件 #${e.id} 前往沟通；不阻塞实际消息传递`,at);
  }
  this.queue=remaining;
  const hosts=new Map<string,Visit>(),visitors=new Set<string>();
  for(const v of this.visits){
   const a=map.get(v.from)!;visitors.add(v.from);
   if(v.stage==='approach'&&at>=v.arrived){v.stage='talk';v.until=at+6000;this.log(v.to,'desk',v.leader?'stand_for_leader':'peer_reception',v.event,v.leader?'领导到访，必须起身迎接':'同事到访，按接待偏好坐着或站起',at);}
   if(v.stage==='talk'&&at>=v.until){v.stage='return';v.started=at;v.path=this.route(v.path.at(-1)!,this.people.get(v.from)!.home);if(!v.path.length)v.path=[cp(a.position),cp(this.people.get(v.from)!.home)];v.distance=this.length(v.path);v.arrived=at+v.distance/2.3*1000;this.log(v.to,'reception','desk',v.event,'沟通演绎结束，恢复工位坐姿',at);}
   const p=v.stage==='talk'?{point:cp(v.path.at(-1)!),yaw:0}:this.position(v,at);v.point=p.point;
   a.position=p.point;a.yaw=v.stage==='talk'?Math.atan2(map.get(v.to)!.position[0]-p.point[0],map.get(v.to)!.position[2]-p.point[2]):p.yaw;
   a.pose=v.stage==='talk'?'communicating':'walking';a.label=v.stage==='talk'?'沟通中':v.stage==='return'?'沟通后回工位':'前往沟通';a.detail=`事件 #${v.event.id} 的空间演绎；后台执行和消息送达不等待行走`;a.evidence_ids=[v.event.id];
   (a as any).visit_event_id=v.event.id;(a as any).leader_visit=v.leader;
   const host=map.get(v.to)!;
   if(v.stage==='talk'||v.stage==='approach'&&dist(p.point,host.position)<=2.2){hosts.set(v.to,v);host.pose='communicating';host.label=v.leader?'起身迎接领导':(this.people.get(v.to)!.peerReception==='stand'?'起身沟通':'坐姿沟通');host.detail=`${v.leader?'领导来访，强制起身':'同事来访，按个人接待设置'} · 事件 #${v.event.id}`;host.evidence_ids=[v.event.id];(host as any).visitor_id=v.from;(host as any).leader_visit=v.leader;(host as any).visit_event_id=v.event.id;}
  }
  this.visits=this.visits.filter(v=>!(v.stage==='return'&&at>=v.arrived));
  const result:PostureView[]=[];
  for(const a of map.values()){
   const person=this.people.get(a.id)!,atDesk=dist(a.position,person.home)<.13,host=hosts.get(a.id),forceStand=!!host&&(host.leader||person.peerReception==='stand');
   const seated=(!visitors.has(a.id)&&atDesk&&a.pose!=='walking'&&a.pose!=='stretch'&&!forceStand)||a.pose==='nap';
   const target=seated?1:0;let state=this.poses.get(a.id);
   if(!state){state={blend:target,from:target,target,start:at};this.poses.set(a.id,state);}
   if(target!==state.target){state.from=state.blend;state.target=target;state.start=at;}
   state.blend=state.from+(state.target-state.from)*smooth((at-state.start)/480);
   let posture:PostureView['posture']=state.blend>.999?'seated':state.blend<.001?(a.pose==='walking'?'walking':'standing'):target?'sitting':'rising';
   // Rise BESIDE the chair, not on its cushion. Hip tracks the seated pose as it blends.
   if(atDesk&&!visitors.has(a.id)&&a.pose!=='nap'){a.position=[person.home[0]+.82*(1-state.blend),0,person.home[2]];}
   if(host&&forceStand){const visitor=map.get(host.from)!;a.yaw=Math.atan2(visitor.position[0]-a.position[0],visitor.position[2]-a.position[2]);}
   else if(atDesk&&seated)a.yaw=person.yaw;
   result.push({...a,posture,sitBlend:state.blend} as PostureView);
  }
  return result;
 }
 suppress(ids:Set<string>){this.queue=this.queue.filter(e=>!ids.has(e.data.from_agent_id)&&!ids.has(e.data.to_agent_id));this.visits=this.visits.filter(v=>!ids.has(v.from)&&!ids.has(v.to));}
 activeVisits(){return this.visits.map(v=>({from:v.from,to:v.to,event_id:v.event.id,stage:v.stage,leader:v.leader}));}
}
