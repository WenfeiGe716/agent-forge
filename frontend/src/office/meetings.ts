/** Real meeting lifecycle -> presentation. Only walking/seating are timed;
 * there is intentionally no timer that ends a business conversation. */
import {ActorView,Packet,Vec,World,Transition} from './behavior';
export type MeetingState={id:string;run_id:string;host_id:string;participants:string[];subject:string;agenda:string;status:string;speaker_ids:string[];created_at:string;source:'business'};
type Track={id:string;meeting:string;run:string;seat:Vec;seatYaw:number;yaw:number;position:Vec;path:Vec[];segment:number;phase:'arriving'|'seated'|'returning';last:number;arrived:number};
const copy=(p:Vec):Vec=>[...p];const distance=(a:Vec,b:Vec)=>Math.hypot(a[0]-b[0],a[2]-b[2]);
export const meetingSeats:{position:Vec;yaw:number}[]=[{position:[2.2,.105,3.45],yaw:0},{position:[2.2,.105,6.55],yaw:Math.PI},{position:[4.4,.105,3.45],yaw:0},{position:[4.4,.105,6.55],yaw:Math.PI},{position:[6.6,.105,3.45],yaw:0},{position:[6.6,.105,6.55],yaw:Math.PI}];
export class MeetingStaging{
 private records:MeetingState[]=[];private received=-Infinity;private tracks=new Map<string,Track>();private clock=0;private room='';readonly trace:Transition[]=[];
 constructor(readonly world:World){}
 accept(p:Packet,at:number){if(!p.complete)return;this.records=(p.meetings||[]).filter(m=>m.source==='business');this.received=at;}
 participantIds(at:number){const local=new Set(this.world.agents.map(a=>a.id));return new Set(this.records.filter(m=>['active','awaiting_decision','paused'].includes(m.status)&&at-this.received<=5000).flatMap(m=>m.participants).filter(id=>local.has(id)));}
 private route(a:Vec,b:Vec){const path=this.world.route(a,b);return path.length?[copy(a),...path.map(copy),copy(b)]:[];}
 private log(t:Track,from:string,to:string,reason:string,at:number,source:'business'|'safety'='business'){this.trace.push({at,agent_id:t.id,from,to,source,reason:`会议 ${t.meeting} · ${reason}`,evidence_ids:[]});if(this.trace.length>300)this.trace.shift();}
 private move(t:Track,at:number){let step=Math.min(.2,Math.max(0,(at-t.last)/1000))*2.3;t.last=at;while(t.segment<t.path.length&&step>=0){const dst=t.path[t.segment],d=distance(t.position,dst);if(d<.0001){t.position=copy(dst);t.segment++;continue;}t.yaw=Math.atan2(dst[0]-t.position[0],dst[2]-t.position[2]);if(step>=d){t.position=copy(dst);step-=d;t.segment++;}else{t.position=[t.position[0]+(dst[0]-t.position[0])*step/d,0,t.position[2]+(dst[2]-t.position[2])*step/d];break;}}return t.segment>=t.path.length;}
 update(base:ActorView[],at:number,connected=true):ActorView[]{
  const local=new Set(this.world.agents.map(a=>a.id)),seats=this.world.meetingSeats??meetingSeats;
  const valid=connected&&at-this.received<=5000,active=valid?this.records.filter(m=>['active','awaiting_decision','paused'].includes(m.status)&&m.participants.some(id=>local.has(id))).sort((a,b)=>a.created_at.localeCompare(b.created_at)||a.id.localeCompare(b.id)):[];
  const map=new Map(base.filter(v=>local.has(v.id)).map(v=>[v.id,{...v,position:copy(v.position)}]));
  // A single room, a deterministic lease. Additional concurrent meetings remain digital.
  const roomCandidates=active.filter(m=>m.participants.some(id=>{const v=map.get(id);return v&&v.source!=='safety'&&v.busy&&v.run_id===m.run_id;}));
  if(valid&&(!seats.length||!roomCandidates.some(m=>m.id===this.room)))this.room=seats.length?roomCandidates[0]?.id||'':'';
  const physical=active.find(m=>m.id===this.room),wanted=new Map<string,{m:MeetingState;index:number}>();
  const reserved=new Set<number>();for(const t of this.tracks.values()){const index=seats.findIndex(s=>distance(s.position,t.seat)<.01);if(index>=0)reserved.add(index);}
  if(physical)for(const id of [...new Set(physical.participants)]){
   const v=map.get(id);if(!v||v.source==='safety'||v.run_id!==physical.run_id||!v.busy)continue;
   const track=this.tracks.get(id),existing=track?.meeting===physical.id&&track.phase!=='returning'?seats.findIndex(s=>distance(s.position,track.seat)<.01):-1;
   const index=existing>=0?existing:seats.findIndex((_,i)=>!reserved.has(i));
   if(index>=0){reserved.add(index);wanted.set(id,{m:physical,index});}
  }
  for(const[id,w]of wanted){const v=map.get(id)!;let t=this.tracks.get(id);if(t?.meeting===w.m.id&&t.phase!=='returning')continue;const slot=seats[w.index],from=t?.position||v.position,path=this.route(from,slot.position);if(!path.length)continue;t={id,meeting:w.m.id,run:w.m.run_id,seat:copy(slot.position),seatYaw:slot.yaw,yaw:slot.yaw,position:copy(from),path,segment:1,phase:'arriving',last:at,arrived:0};this.tracks.set(id,t);this.log(t,'desk','meeting_arriving','收到真实会议状态，前往本层已配置席位；执行不等待动画',at);}
  for(const[id,t]of this.tracks){const v=map.get(id);if(!v){this.tracks.delete(id);continue;}const m=active.find(m=>m.id===t.meeting);if(!valid||v.source==='safety'){t.last=at;v.position=copy(t.position);v.yaw=t.yaw;v.source='safety';v.pose='idle';v.posture=t.phase==='seated'?'seated':'standing';v.sitBlend=t.phase==='seated'?1:0;v.label='会议状态待同步';v.detail='状态来源过期或断线，暂停演绎，不自行宣布散会';continue;}
   if((!m||!m.participants.includes(id)||v.run_id!==t.run||!v.busy)&&t.phase!=='returning'){const home=this.world.agents.find(a=>a.id===id)!.home;t.path=this.route(t.position,home);if(!t.path.length){this.tracks.delete(id);continue;}t.segment=1;t.phase='returning';t.last=at;this.log(t,'meeting','returning','真实会议结束、退出参会或任务切换，返回工位',at);}
   const paused=m?.status==='paused';if(t.phase!=='seated'&&!paused){if(this.move(t,at)){if(t.phase==='returning'){this.log(t,'returning','desk','返回个人工位，恢复基础工作姿态',at);this.tracks.delete(id);continue;}t.phase='seated';t.arrived=at;t.yaw=t.seatYaw;t.position=copy(t.seat);this.log(t,'meeting_arriving','meeting_seated','进入会议席位；保持到实际结束状态',at);}}else t.last=at;
   const speaking=!!m?.speaker_ids.includes(id),seated=t.phase==='seated';v.position=copy(t.position);v.visible=true;v.yaw=t.yaw;v.sitBlend=seated?Math.min(1,(at-t.arrived)/480):0;v.posture=seated?'seated':paused?'standing':'walking';v.pose=seated?(paused||m?.status==='awaiting_decision'?'idle':speaking?'communicating':'reading'):paused?'idle':'walking';v.source='business';v.action=null;v.facility=null;v.stage='meeting_'+t.phase;v.label=t.phase==='returning'?'散会后回工位':!seated?'前往会议室':paused?'会议已暂停':m?.status==='awaiting_decision'?'会中等待决策':speaking?'会议发言 / 复核':'会议聆听';v.detail=`真实会议 ${t.meeting} · ${m?.subject||'返回工位'}；状态来自后端，不按动画时长结束`;v.visitor_id=undefined;v.leader_visit=false;v.visit_event_id=undefined;(v as any).meeting_id=t.meeting;(v as any).meeting_speaker=speaking;
  }
  for(const m of active)for(const id of m.participants){const v=map.get(id);if(!v||v.source==='safety'||v.run_id!==m.run_id||!v.busy||this.tracks.has(id))continue;v.label='远程参与会议';v.detail=`会议 ${m.id}：${!seats.length?'本楼层未配置可用会议席位':m.id===this.room?`本层 ${seats.length} 个实体席位已满或路径不可达`:'本层会议室已有其他会话'}，在个人工位参与；跨楼层消息仍由后端真实传递`;v.pose=['paused','awaiting_decision'].includes(m.status)?'idle':m.speaker_ids.includes(id)?'communicating':'reading';(v as any).meeting_id=m.id;}
  this.clock=at;return [...map.values()];
 }
 positions(){return [...this.tracks.values()].map(t=>({id:t.id,meeting:t.meeting,phase:t.phase,position:copy(t.position)}));}
}
