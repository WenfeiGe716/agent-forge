/** Deterministic ambient simulation. No task/model/API events are emitted here. */
export type TrafficPhase='ew_green'|'ew_yellow'|'all_red_ew'|'ns_green'|'ns_yellow'|'all_red_ns'|'ped_green'|'ped_clear';
export type TrafficDensity='low'|'normal'|'busy';
export type CityCar={id:string;lane:number;s:number;speed:number;x:number;z:number;yaw:number;stopped:boolean};
export type CityPed={id:string;x:number;z:number;yaw:number;walking:boolean;crossing:boolean;progress:number;direction:number;wait:number};
export const PHASES:{name:TrafficPhase;seconds:number}[]=[{name:'ew_green',seconds:14},{name:'ew_yellow',seconds:3},{name:'all_red_ew',seconds:2},{name:'ns_green',seconds:14},{name:'ns_yellow',seconds:3},{name:'all_red_ns',seconds:2},{name:'ped_green',seconds:10},{name:'ped_clear',seconds:7}];
// Identity, not a render frame or unseeded RNG, owns each actor's pace. Keeping
// these values derivable also lets old checkpoints resume without teleporting.
const pedestrianPaces=[1.31,1.53,1.69,1.42,1.61,1.36,1.57,1.47];
const pedestrianIndex=(p:CityPed)=>Number(p.id.split('-')[2]);
const releaseDelay=(p:CityPed)=>.15+(pedestrianIndex(p)*5%8)*.36+(p.direction<0?.13:0);
const carPace=(lane:number,index:number)=>3.75+((lane*7+index*5)%19)*.052;
export class CityTraffic {
 readonly source='ambient';time=0;phaseIndex=0;phaseTime=0;accumulator=0;cars:CityCar[]=[];pedestrians:CityPed[]=[];events:{time:number;kind:string;id:string;phase:TrafficPhase}[]=[];
 readonly cx=20;readonly cz:number;readonly lanes:{axis:'x'|'z';fixed:number;start:number;sign:number;length:number;cross:number}[];
 constructor(readonly extension=0,public density:TrafficDensity='normal'){
  this.cz=14+extension;this.lanes=[{axis:'x',fixed:this.cz+1.55,start:-48,sign:1,length:96,cross:68},{axis:'x',fixed:this.cz-1.55,start:48,sign:-1,length:96,cross:28},{axis:'z',fixed:this.cx-1.55,start:-44,sign:1,length:88+extension,cross:58+extension},{axis:'z',fixed:this.cx+1.55,start:44+extension,sign:-1,length:88+extension,cross:30}];
  for(let lane=0;lane<4;lane++)for(let i=0;i<(density==='low'?1:density==='busy'?4:3);i++){const car:CityCar={id:`city-car-${lane}-${i}`,lane,s:this.carSlot(lane,i),speed:carPace(lane,i),x:0,z:0,yaw:0,stopped:false};this.position(car);this.cars.push(car);}
  for(let i=0;i<(density==='low'?2:density==='busy'?8:6);i++)this.pedestrians.push(this.createPedestrian(i));
 }
 get phase(){return PHASES[this.phaseIndex].name;}
 get remaining(){return Math.max(0,PHASES[this.phaseIndex].seconds-this.phaseTime);}
 get signals(){return{ew:this.phase==='ew_green'?'green':this.phase==='ew_yellow'?'yellow':'red',ns:this.phase==='ns_green'?'green':this.phase==='ns_yellow'?'yellow':'red',ped:this.phase==='ped_green'?'green':'red'};}
 private note(kind:string,id:string){this.events.push({time:this.time,kind,id,phase:this.phase});if(this.events.length>160)this.events.shift();}
 private position(c:CityCar){const l=this.lanes[c.lane],n=l.start+l.sign*c.s;c.x=l.axis==='x'?n:l.fixed;c.z=l.axis==='z'?n:l.fixed;c.yaw=l.axis==='x'?(l.sign>0?Math.PI/2:-Math.PI/2):(l.sign>0?0:Math.PI);}
 private carSlot(lane:number,index:number){const l=this.lanes[lane],offset=[.4,2.7,1.45,4][lane]+index*(9+lane*.3)+index*index*.45;return(l.cross-12-offset+l.length)%l.length;}
 private createPedestrian(index:number):CityPed{
  // Both curbs have independent walkers, not a row of three starting on the
  // same side. Every person retains their own stripe for collision-free return.
  const direction=Math.floor(index/2)%2?-1:1;
  const p:CityPed={id:`city-ped-${index}`,x:this.cx+(index%2?-5.15:5.15)+(Math.floor(index/2)-1)*.60,z:this.cz-direction*4.7,yaw:direction>0?0:Math.PI,walking:false,crossing:false,progress:0,direction,wait:0};
  p.wait=releaseDelay(p);return p;
 }
 private inside(c:CityCar){const l=this.lanes[c.lane];return Math.abs(c.s-l.cross)<7.8;}
 advance(seconds:number){if(!Number.isFinite(seconds)||seconds<0)return;this.accumulator+=Math.min(seconds,.25);while(this.accumulator>=.05-1e-9){this.step(.05);this.accumulator-=.05;}}
 private step(dt:number){
  this.time+=dt;this.phaseTime+=dt;this.reconcileDensity();
  if(this.phaseTime>=PHASES[this.phaseIndex].seconds){const next=(this.phaseIndex+1)%PHASES.length;
   // Clearance never releases a conflicting flow while a car or pedestrian remains inside.
   const clears=PHASES[next].name==='ped_green'||PHASES[next].name==='ew_green'||PHASES[next].name==='ns_green';
   if(!clears||(!this.cars.some(c=>this.inside(c))&&!this.pedestrians.some(p=>p.crossing))){
    this.phaseIndex=next;this.phaseTime=0;this.note('signal_changed','intersection');
    // Arm reaction delays on EVERY green. Counting them down during the long
    // red used to erase every offset and release two permanent marching trios.
    if(this.phase==='ped_green')for(const p of this.pedestrians)if(!p.crossing)p.wait=releaseDelay(p);
   }
  }
  const old=this.cars.map(c=>({...c}));
  for(const c of this.cars){const l=this.lanes[c.lane],stop=l.cross-6.8,green=l.axis==='x'?this.signals.ew==='green':this.signals.ns==='green';let move=c.speed*dt;
   const lead=old.filter(o=>o.id!==c.id&&o.lane===c.lane).reduce((d,o)=>Math.min(d,(o.s-c.s+l.length)%l.length),Infinity);move=Math.max(0,Math.min(move,lead-4.25));
   const front=c.s+1.4;if(front<=stop+1e-7&&!green)move=Math.min(move,Math.max(0,stop-front));
   if(front<=stop+1e-7&&front+move>stop+1e-7)this.note('vehicle_entered',c.id);
   c.stopped=move<.004;c.s=(c.s+move)%l.length;this.position(c);
  }
  for(const p of this.pedestrians){
   if(!p.crossing&&this.signals.ped==='green')p.wait=Math.max(0,p.wait-dt);
   if(!p.crossing&&this.signals.ped==='green'&&this.remaining>1&&p.wait<=0&&!this.cars.some(c=>this.inside(c))){p.crossing=true;p.progress=0;this.note('pedestrian_entered',p.id);}
   p.walking=p.crossing;if(p.crossing){p.progress=Math.min(9.4,p.progress+pedestrianPaces[pedestrianIndex(p)]*dt);p.z=p.direction>0?this.cz-4.7+p.progress:this.cz+4.7-p.progress;p.yaw=p.direction>0?0:Math.PI;if(p.progress>=9.4){p.crossing=false;p.walking=false;p.direction*=-1;p.yaw=p.direction>0?0:Math.PI;p.wait=11;this.note('pedestrian_cleared',p.id);}}
  }
 }

 /** Live density changes preserve phase/time and every retained actor position.
  * Actors already in the intersection finish before removal. New actors use
  * collision-free slots outside the intersection, otherwise insertion waits.
  */
 setDensity(density:TrafficDensity){if(!['low','normal','busy'].includes(density))return;if(this.density===density)return;this.density=density;this.reconcileDensity();}
 private reconcileDensity(){
  const n=this.density==='low'?1:this.density==='busy'?4:3,np=this.density==='low'?2:this.density==='busy'?8:6;
  this.cars=this.cars.filter(c=>Number(c.id.split('-')[3])<n||this.inside(c));
  for(let lane=0;lane<4;lane++)for(let i=0;i<n;i++){
   const id=`city-car-${lane}-${i}`;if(this.cars.some(c=>c.id===id))continue;const l=this.lanes[lane];
   const candidates=[this.carSlot(lane,i),...Array.from({length:12},(_,j)=>2+j*6)];
   const s=candidates.find(s=>Math.abs(s-l.cross)>10&&this.cars.filter(c=>c.lane===lane).every(c=>Math.min((c.s-s+l.length)%l.length,(s-c.s+l.length)%l.length)>4.30));
   if(s===undefined)continue;const c:CityCar={id,lane,s,speed:carPace(lane,i),x:0,z:0,yaw:0,stopped:false};this.position(c);this.cars.push(c);
  }
  this.cars.sort((a,b)=>a.id.localeCompare(b.id));
  this.pedestrians=this.pedestrians.filter(p=>Number(p.id.split('-')[2])<np||p.crossing);
  for(let i=0;i<np;i++){const id=`city-ped-${i}`;if(this.pedestrians.some(p=>p.id===id))continue;this.pedestrians.push(this.createPedestrian(i));}
 }
 checkpoint(){return{version:1,extension:this.extension,density:this.density,time:this.time,phaseIndex:this.phaseIndex,phaseTime:this.phaseTime,accumulator:this.accumulator,cars:this.cars.map(c=>({...c})),pedestrians:this.pedestrians.map(p=>({...p}))};}
 static restore(raw:any,extension?:number):CityTraffic|null{
  try{
   if(raw?.version!==1||!['low','normal','busy'].includes(raw.density)||!Number.isFinite(raw.extension)||raw.extension<0||raw.extension>280)return null;
   if(![raw.time,raw.phaseTime,raw.accumulator].every(x=>Number.isFinite(x)&&x>=0)||raw.time>31536000||raw.accumulator>.251||!Number.isInteger(raw.phaseIndex)||raw.phaseIndex<0||raw.phaseIndex>=PHASES.length)return null;
   if(!Array.isArray(raw.cars)||raw.cars.length>16||!Array.isArray(raw.pedestrians)||raw.pedestrians.length>8)return null;
   const previous=new CityTraffic(raw.extension,raw.density),out=new CityTraffic(extension??raw.extension,raw.density),seen=new Set<string>();
   out.time=raw.time;out.phaseTime=raw.phaseTime;out.phaseIndex=raw.phaseIndex;out.accumulator=raw.accumulator;
   out.cars=[];for(const c of raw.cars){
    if(!/^city-car-[0-3]-[0-3]$/.test(c.id)||seen.has(c.id)||!Number.isInteger(c.lane)||c.lane!==Number(c.id.split('-')[2])||![c.s,c.speed].every(Number.isFinite)||c.s<0||c.s>=previous.lanes[c.lane].length||c.speed<0||c.speed>8)return null;
    seen.add(c.id);const lane=out.lanes[c.lane],old=previous.lanes[c.lane],s=out.extension===previous.extension?c.s:(c.s-old.cross+lane.cross+lane.length)%lane.length;const next:CityCar={id:c.id,lane:c.lane,s,speed:c.speed,x:0,z:0,yaw:0,stopped:!!c.stopped};out.position(next);out.cars.push(next);
   }
   for(const c of out.cars)for(const other of out.cars)if(c.id!==other.id&&c.lane===other.lane&&(other.s-c.s+out.lanes[c.lane].length)%out.lanes[c.lane].length<4.249)return null;
   out.pedestrians=[];seen.clear();for(const p of raw.pedestrians){
    if(!/^city-ped-[0-7]$/.test(p.id)||seen.has(p.id)||![p.progress,p.wait,p.x,p.z].every(Number.isFinite)||p.progress<0||p.progress>9.4||p.wait<0||p.wait>120||![1,-1].includes(p.direction))return null;
    if(Math.abs(Math.abs(p.x-out.cx)-5.15)>1.21)return null;seen.add(p.id);
    out.pedestrians.push({id:p.id,x:p.x,z:out.extension===previous.extension?p.z:p.z-previous.cz+out.cz,yaw:p.direction===1?0:Math.PI,walking:!!p.crossing,crossing:!!p.crossing,progress:p.progress,direction:p.direction,wait:p.wait});
   }
   // Forged/corrupt local checkpoints must not release crossing traffic.
   if(out.pedestrians.some(p=>p.crossing)&&(out.signals.ew!=='red'||out.signals.ns!=='red'))return null;
   return out;
  }catch{return null;}
 }
 snapshot(){return{source:this.source,phase:this.phase,remaining:this.remaining,time:this.time,signals:this.signals,cars:this.cars.map(c=>({...c})),pedestrians:this.pedestrians.map(p=>({...p}))};}
}
