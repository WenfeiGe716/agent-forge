import {V3,M4,multiply,ortho,lookAt} from './scene';

export type CameraMode='overview'|'following'|'manual';
export type CameraInsets={left:number;right:number;top:number;bottom:number};
export type CameraPose={target:V3;zoom:number;yaw:number;pitch:number};
type Subject={position:V3;yaw:number;visible:boolean};
type Transition={from:CameraPose;to:CameraPose;start:number};
const copy=(pose:CameraPose):CameraPose=>({...pose,target:[...pose.target]});
const lerp=(a:number,b:number,t:number)=>a+(b-a)*t;
const turn=(a:number,b:number,t:number)=>a+Math.atan2(Math.sin(b-a),Math.cos(b-a))*t;
export const officeOverview=():CameraPose=>({target:[1.5,.65,1.0],zoom:1.0,yaw:1.40,pitch:.58});

/** One camera clock and one tracked identity. Movement remains an input: this
 * module never edits a subject, office layout, or execution state. */
export class OfficeCamera{
 mode:CameraMode='overview';agentId:string|null=null;
 private transition:Transition|null=null;private last:number|null=null;
 constructor(private pose:CameraPose,private changed:(mode:CameraMode,agentId?:string)=>void=()=>{}){}
 private modeTo(mode:CameraMode,id:string|null=null){if(mode===this.mode&&id===this.agentId)return;this.mode=mode;this.agentId=id;this.changed(mode,id||undefined);}
 follow(id:string,subject:Subject,context:boolean,now:number){
  if(!subject.visible)return;this.modeTo('following',id);this.last=now;
  const to:CameraPose={target:[subject.position[0],subject.position[1]+1.10,subject.position[2]],zoom:context?2.15:3.25,
   // An employee workspace keeps the office orientation. Explicit close-ups
   // may turn toward the face, but turn and elevation are eased together.
   yaw:context?this.pose.yaw:subject.yaw+Math.PI/2-.90,pitch:context?.54:.43};
  this.transition={from:copy(this.pose),to,start:now};
 }
 manual(){this.transition=null;this.modeTo('manual');}
 reset(){this.transition=null;this.last=null;Object.assign(this.pose,officeOverview());this.modeTo('overview');}
 travel(to:CameraPose,now:number){this.modeTo('manual');this.last=now;this.transition={from:copy(this.pose),to:copy(to),start:now};}
 update(subjects:ReadonlyMap<string,Subject>,now:number){
  const dt=this.last===null?0:Math.max(0,now-this.last);this.last=now;
  const subject=this.agentId?subjects.get(this.agentId):null;
  if(this.agentId&&(!subject||!subject.visible)){this.manual();return;}
  const destination:V3|null=subject?[subject.position[0],subject.position[1]+1.10,subject.position[2]]:null;
  if(this.transition){const move=this.transition,t=Math.min(1,Math.max(0,(now-move.start)/550)),ease=1-(1-t)**3,to=destination||move.to.target;
   this.pose.target=move.from.target.map((value,index)=>lerp(value,to[index],ease)) as V3;
   this.pose.zoom=lerp(move.from.zoom,move.to.zoom,ease);this.pose.yaw=turn(move.from.yaw,move.to.yaw,ease);this.pose.pitch=lerp(move.from.pitch,move.to.pitch,ease);
   if(t===1)this.transition=null;
  }else if(destination){const ease=1-Math.exp(-dt/140);this.pose.target=this.pose.target.map((value,index)=>lerp(value,destination[index],ease)) as V3;}
 }
}

/** Insets are CSS pixels. They protect following views only; overview retains
 * its full canvas scale instead of shrinking the office behind side panels. */
export function officeProjection(pose:CameraPose,width:number,height:number,insets?:CameraInsets,following=false):{vp:M4;eye:V3}{
 const w=Math.max(1,width),h=Math.max(1,height),clean=(value:number|undefined,max:number)=>Number.isFinite(value)?Math.min(max,Math.max(0,value!)):0;
 let left=following?clean(insets?.left,w):0,right=following?clean(insets?.right,w):0,top=following?clean(insets?.top,h):0,bottom=following?clean(insets?.bottom,h):0;
 const horizontal=Math.min(1,(w-Math.min(144,w*.2))/Math.max(1,left+right)),vertical=Math.min(1,(h-Math.min(144,h*.2))/Math.max(1,top+bottom));
 left*=horizontal;right*=horizontal;top*=vertical;bottom*=vertical;
 const safeWidth=w-left-right,safeHeight=h-top-bottom;
 const viewHeight=Math.max((w/h<1.2?31:25)/pose.zoom,following?2.8*h/safeHeight:0,following?2.3*h/safeWidth:0),projection=ortho(viewHeight*w/h,viewHeight);
 projection[12]+=(left-right)/w;projection[13]+=(bottom-top)/h;
 const radius=64,eye:V3=[pose.target[0]+Math.cos(pose.yaw)*Math.cos(pose.pitch)*radius,pose.target[1]+Math.sin(pose.pitch)*radius,pose.target[2]+Math.sin(pose.yaw)*Math.cos(pose.pitch)*radius];
 return{vp:multiply(projection,lookAt(eye,pose.target)),eye};
}
