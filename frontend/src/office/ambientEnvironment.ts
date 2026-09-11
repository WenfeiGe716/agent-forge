/** Scene-only clock owned by the authenticated application, NOT a WebGL canvas.
 * It never imports an API, a task, an Agent or a model. Navigation and dialogs can
 * stop painting, but cannot reset or stop traffic. Checkpoints contain geometry
 * and traffic state only; no business data or credentials are persisted here.
 */
import {CityTraffic,TrafficDensity} from './cityTraffic';
export type AmbientQuality='detail'|'balanced'|'economy';
export type AmbientPreferences={version:1;density:'auto'|TrafficDensity;motion:boolean;day:boolean};
export type AmbientStorage={getItem:(key:string)=>string|null;setItem:(key:string,value:string)=>void};
const defaults:AmbientPreferences={version:1,density:'auto',motion:true,day:true};
export const densityFor=(quality:AmbientQuality):TrafficDensity=>quality==='economy'?'low':quality==='detail'?'busy':'normal';
export class AmbientEnvironment {
 readonly source='ambient';readonly version=1;
 traffic=new CityTraffic();preferences:AmbientPreferences={...defaults};quality:AmbientQuality='balanced';
 private last:number|null=null;private debt=0;private session='';private savedAt=0;private interval:ReturnType<typeof setInterval>|null=null;
 private listeners=new Set<()=>void>();private preferencesLoaded=false;private active=false;
 constructor(private clock:()=>number=()=>performance.now(),private prefsStorage?:AmbientStorage,private stateStorage?:AmbientStorage){}
 get running(){return this.active;}
 get pendingSeconds(){return this.debt;}
 subscribe(listener:()=>void){this.listeners.add(listener);return()=>{this.listeners.delete(listener);};}
 private notify(){for(const listener of this.listeners)listener();}
 private storage(which:'prefs'|'state'):AmbientStorage|undefined{try{return which==='prefs'?(this.prefsStorage||globalThis.localStorage):(this.stateStorage||globalThis.sessionStorage);}catch{return undefined;}}
 private loadPreferences(){
  if(this.preferencesLoaded)return;this.preferencesLoaded=true;
  try{const raw=JSON.parse(this.storage('prefs')?.getItem('forge.ambient.preferences.v1')||'null');
   if(raw?.version===1)this.preferences=this.validPreferences(raw);
   else if(typeof matchMedia!=='undefined'&&matchMedia('(prefers-reduced-motion: reduce)').matches)this.preferences.motion=false;
  }catch{/* Corrupt or unavailable preferences never gate login. */}
 }
 private validPreferences(p:any):AmbientPreferences{return{version:1,density:['auto','low','normal','busy'].includes(p?.density)?p.density:'auto',motion:typeof p?.motion==='boolean'?p.motion:true,day:typeof p?.day==='boolean'?p.day:true};}
 /** Called once by the authenticated App; unmounting Office does not call stop. */
 start(session:string,automatic=true){
  if(!session)return;
  this.loadPreferences();if(this.active&&this.session===session)return;
  this.stop();if(this.session!==session){this.session=session;this.traffic=new CityTraffic();this.debt=0;
   try{const raw=JSON.parse(this.storage('state')?.getItem(this.key())||'null');if(raw?.version===1){const restored=CityTraffic.restore(raw.traffic);if(restored)this.traffic=restored;}}catch{}
  }
  this.active=true;this.last=this.clock();this.savedAt=this.last;this.applyDensity();
  if(automatic)this.interval=setInterval(()=>this.pump(),100);this.notify();
 }
 stop(){if(this.active){this.pump();this.save();}if(this.interval!==null)clearInterval(this.interval);this.interval=null;this.active=false;this.last=null;}
 private key(){return 'forge.ambient.state.v1:'+this.session;}
 save(){if(!this.session)return;try{this.storage('state')?.setItem(this.key(),JSON.stringify({version:1,traffic:this.traffic.checkpoint()}));}catch{}}
 configure(extension:number,quality:AmbientQuality=this.quality){
  this.loadPreferences();if(!Number.isFinite(extension)||extension<0)extension=0;
  if(extension!==this.traffic.extension){this.pump();this.traffic=CityTraffic.restore(this.traffic.checkpoint(),extension)||new CityTraffic(extension,this.traffic.density);}
  this.quality=['detail','balanced','economy'].includes(quality)?quality:'balanced';this.applyDensity();return this.traffic;
 }
 private applyDensity(){this.traffic.setDensity(this.preferences.density==='auto'?densityFor(this.quality):this.preferences.density);}
 setPreferences(p:Partial<Omit<AmbientPreferences,'version'>>){
  this.loadPreferences();this.pump();this.preferences=this.validPreferences({...this.preferences,...p});this.applyDensity();
  // An explicit pause has no catch-up debt. Resume starts from this exact state.
  if(!this.preferences.motion)this.debt=0;this.last=this.active?this.clock():null;
  try{this.storage('prefs')?.setItem('forge.ambient.preferences.v1',JSON.stringify(this.preferences));}catch{}
  this.save();this.notify();
 }
 /** Bounded work per pump, but NO skipped signal phases or discarded elapsed
  * time. Long hidden-tab gaps are drained in later pumps, never teleported.
  * A closed page is not a 24/7 simulation server. Restore resumes the checkpoint.
  */
 pump(now=this.clock()){
  if(!this.active||!Number.isFinite(now))return;
  if(this.last===null){this.last=now;return;}
  if(now<this.last)return;
  const delta=(now-this.last)/1000;this.last=now;
  if(this.preferences.motion){this.debt+=delta;let steps=0;while(this.debt>=.05-1e-8&&steps<100){this.traffic.advance(.05);this.debt=Math.max(0,this.debt-.05);steps++;}}
  else this.debt=0;
  if(now-this.savedAt>=2000){this.save();this.savedAt=now;}
 }
 status(){return{source:this.source,running:this.active,time:this.traffic.time,phase:this.traffic.phase,pending_seconds:this.debt,motion:this.preferences.motion,density:this.traffic.density,cars:this.traffic.cars.length,pedestrians:this.traffic.pedestrians.length,extension:this.traffic.extension};}
}
export const environment=new AmbientEnvironment();
