/** Hand-authored procedural character kit, v0.9. Stable identity, smooth authored
 * silhouettes, tailored garments and articulated hands. Animation uses ActorView;
 * this module never creates messages, meetings or model calls.
 */
import {V3,multiply,modelMatrix} from './scene';
import {ActorView} from '../office/behavior';
import {Part,color,segment,oriented,Surface} from './officeRenderer';
export type CharacterLook={hair:number;skin:number;style:number;glasses:boolean;hairStyle:number;accent:number;coat:number};
export function stableHash(id:string):number{let h=2166136261;for(const c of id){h^=c.charCodeAt(0);h=Math.imul(h,16777619);}return h>>>0;}
export function appearanceFor(id:string,role='developer'):CharacterLook{
 const h=stableHash(id),style=Math.max(0,['planner','developer','tester','reviewer'].indexOf(role));
 return{style,hair:[0x282831,0x49372e,0x655043,0x252f39,0x674749][h%5],skin:[0xe9c3a6,0xd8ad8e,0xf0cfb9,0xc59878,0xe3ba9e][(h>>>4)%5],hairStyle:(h>>>7)%4,glasses:style===3||(h>>>3)%4===0,accent:[0xa998ce,0x72b2b0,0xd4ac68,0xc88fa6][style],coat:[0x343d56,0x458d94,0xc6a36a,0x946b87][style]};
}
export function detailedActor(actor:ActorView,index:number,hue:number,selected=false,role?:string):Part[]{
 if(!actor.visible)return[];
 role=role||({0x9c8cde:'planner',0x54b2cc:'developer',0xe0b65d:'tester',0xda91b0:'reviewer'} as Record<number,string>)[hue]||'developer';
 const look=appearanceFor(actor.id,role),out:Part[]=[],p=actor.position,yaw=actor.yaw,t=actor.poseTime||0,pose=actor.pose;
 const walking=pose==='walking',seat=actor.sitBlend??(['typing','reading','thinking','testing','reviewing','phone','nap'].includes(pose)?1:0),nap=pose==='nap',bob=walking?Math.abs(Math.sin(t*8))*.018:0;
 const hip=1.14-.295*seat,up=hip-.82,head=(nap?1.49:1.75)+up,torso=(nap?1.06:1.16)+up,lean=nap?-.25:0;
 const skin=color(look.skin),hair=color(look.hair),cloth=color(look.coat),pants=color(look.style===1?0x354e68:0x394352),ivory=color(0xe7e7df),dark=color(0x222b38);
 const point=(v:V3):V3=>[p[0]+v[0]*Math.cos(yaw)+v[2]*Math.sin(yaw),p[1]+v[1]+bob,p[2]-v[0]*Math.sin(yaw)+v[2]*Math.cos(yaw)];
 const part=(v:V3,s:V3,c:V3,mesh='sphere',surface:Surface='matte',tag='',pitch=0,roll=0,emission=0)=>{
  const q:Part={p:point(v),s,color:c,mesh,yaw,surface,tag,emissive:emission};
  if(pitch||roll)q.matrix=multiply(modelMatrix(p,[1,1,1],yaw),oriented([v[0],v[1]+bob,v[2]],s,0,pitch,roll));out.push(q);return q;
 };
 const limb=(a:V3,b:V3,w:number,c:V3,surface:Surface='cloth',tag='')=>{const q=segment(point(a),point(b),w,c);q.surface=surface;q.tag=tag;out.push(q);return q;};
 // Stable order of torso/pelvis/head retained, with semantic tags for geometry tests.
 part([0,torso,lean],[1,1,1],cloth,'torso','cloth','torso');
 part([0,hip,0],[.46,.26,.35],pants,'round','cloth','pelvis');
 part([0,head,lean],[1,1,1],skin,'face','skin','head');
 part([0,head-.32,lean-.02],[.19,.20,.18],skin,'capsule','skin','neck');
 // Rounded cheek planes, soft ears, inset iris/highlight, sculpted brow and lip line.
 for(const side of[-1,1]){
  part([side*.284,head-.035,lean-.015],[.09,.14,.073],skin,'eye','skin','ear');
  part([side*.315,head-.034,lean+.008],[.028,.065,.03],color(0xc08d7f),'eye','skin','ear-inner');
  const blink=nap||Math.sin(t*.92+(stableHash(actor.id)%13))>.9992;
  if(blink){limb([side*.116-.045,head+.029,lean+.256],[side*.116+.045,head+.025,lean+.260],.013,dark,'matte','eyelid');}
  else{
   part([side*.116,head+.035,lean+.248],[.094,.078,.028],color(0xf6f1e9),'eye','matte','eye-white');
   part([side*.115,head+.033,lean+.268],[.048,.058,.018],color(0x423629),'eye','matte','iris');
   part([side*.115,head+.033,lean+.279],[.030,.041,.011],dark,'eye','matte','pupil');
   part([side*.115-.01,head+.048,lean+.284],[.016,.018,.01],color(0xffffff),'eye','matte','eye-catchlight',0,0,.8);
  }
  limb([side*.116-.056,head+.116,lean+.257],[side*.116+.052,head+.125-(side===1?.012:0),lean+.253],.022,hair,'cloth','eyebrow');
 }
 part([0,head-.036,lean+.277],[.069,.093,.073],skin,'sphere','skin','nose');
 part([0,head-.151,lean+.24],[.09,.024,.016],color(0xac766a),'eye','skin','mouth');
 part([0,head-.137,lean+.251],[.065,.01,.01],color(0xe8c5b3),'eye','skin','lip-highlight');
 // Full scalp with front hairline and nape, not a flattened sphere on top.
 part([0,head,lean],[1,1,1],hair,look.hairStyle===2?'hair-long':'hair-short','cloth','hair-scalp');
 if(look.hairStyle===0){
  for(let i=0;i<4;i++)part([-.18+i*.107,head+.265-(i===3?.018:0),lean+.108],[.16,.20,.29],hair,'sphere','cloth','side-swept-hair',-.18,.37);
  part([-.23,head+.075,lean+.055],[.075,.23,.18],hair,'sphere','cloth','sideburn');
 }else if(look.hairStyle===1){
  for(let i=0;i<3;i++)part([-.135+i*.117,head+.298-i*.009,lean+.063],[.235,.134,.365],hair,'sphere','cloth','textured-quiff',-.09,.24);
 }else if(look.hairStyle===2){
  for(const side of[-1,1])part([side*.254,head-.07,lean-.075],[.16,.53,.32],hair,'sphere','cloth','bob-hair');
  part([-.078,head+.236,lean+.13],[.47,.21,.29],hair,'sphere','cloth','swept-fringe',0,.13);
 }else{
  part([0,head+.032,lean-.31],[.25,.39,.24],hair,'sphere','cloth','tied-hair');
  part([0,head+.19,lean-.30],[.18,.11,.12],color(look.accent),'round','cloth','hair-tie');
  part([.07,head+.274,lean+.077],[.42,.17,.33],hair,'sphere','cloth','fringe',0,-.18);
 }
 if(look.glasses){
  for(const side of[-1,1]){
   part([side*.123,head+.037,lean+.300],[.217,.165,.155],color(look.style===3?0x62553c:0x293747),'ring','metal','glasses-rim');
   limb([side*.223,head+.05,lean+.28],[side*.285,head+.036,lean-.04],.012,dark,'matte','glasses-arm');
  }
  limb([-.026,head+.051,lean+.306],[.026,head+.051,lean+.306],.012,dark,'matte','glasses-bridge');
 }
 // Actual separate shirt, jacket lapels or hoodie hood/cuffs; accent is not skin.
 part([0,torso+(look.style===1?.171:.09),lean+.156],[look.style===1?.18:.25,look.style===1?.21:.43,.029],ivory,'round','cloth','shirt-front');
 if(look.style===0||look.style===3){
  part([-.121,torso+.085,lean+.183],[.13,.38,.048],cloth,'round','cloth','lapel-left',0,-.24);
  part([.121,torso+.085,lean+.183],[.13,.38,.048],cloth,'round','cloth','lapel-right',0,.24);
  for(const side of[-1,1])part([side*.072,torso+.239,lean+.148],[.115,.108,.036],ivory,'round','cloth','collar',0,side*.42);
  if(look.style===0){part([0,torso+.17,lean+.19],[.05,.08,.02],color(look.accent),'round','cloth','tie-knot');part([0,torso-.02,lean+.195],[.05,.3,.025],color(look.accent),'round','cloth','tie');}
  part([-.168,torso+.025,lean+.19],[.115,.018,.022],color(look.accent),'round','cloth','breast-pocket');
  for(let i=0;i<2;i++)part([.055,torso-.1-i*.075,lean+.19],[.024,.024,.014],color(0x8592a1),'eye','metal','button');
 }else if(look.style===1){
  part([0,torso+.224,lean-.124],[.43,.24,.18],color(0x34727b),'round','cloth','hood');
  part([-.09,torso+.225,lean+.157],[.105,.12,.042],cloth,'round','cloth','hood-fold',0,-.3);
  part([.09,torso+.225,lean+.157],[.105,.12,.042],cloth,'round','cloth','hood-fold',0,.3);
  for(const side of[-1,1])limb([side*.11,torso+.205,lean+.185],[side*.09,torso+.055,lean+.189],.012,ivory,'cloth','hood-string');
  part([0,torso-.225,lean+.147],[.31,.11,.043],color(0x3b7a82),'round','cloth','hoodie-pocket');
  part([0,torso-.277,lean],[.456,.048,.29],color(0x316f78),'round','cloth','hem');
 }else{
  for(const side of[-1,1])part([side*.127,torso+.06,lean+.185],[.035,.40,.03],color(0x9f7c49),'round','cloth','cardigan-edge',0,side*.1);
  for(let i=0;i<3;i++)part([.101,torso-.04-i*.067,lean+.204],[.022,.022,.014],color(0x8e6d43),'eye','wood','button');
 }
 // A compact ID badge at a believable garment attachment, not oversized labels.
 part([.165,torso+.12,lean+.19],[.084,.118,.016],color(0xe6e5df),'round','matte','id-badge');
 part([.165,torso+.142,lean+.201],[.067,.034,.004],color(look.accent),'round','matte','badge-band');
 part([.16,torso+.087,lean+.202],[.047,.009,.004],color(0x65778b),'cube','matte','badge-detail');
 for(const side of[-1,1]){
  const x=side*.14,swing=walking?Math.sin(t*8)*side*.30:0,knee:V3=[x,.58+.23*seat,swing*.5*(1-seat)+.43*seat],foot:V3=[x,.233,(nap?.68:.54)*seat+swing*(1-seat)];
  limb([x,hip,0],knee,.213,pants,'cloth','thigh');part(knee,[.21,.19,.20],pants,'sphere','cloth','knee');limb(knee,foot,.181,pants,'cloth','shin');
  const shoeColor=look.style===0?color(0x353440):color(0xdddeda);
  part([foot[0],foot[1]-.054,foot[2]+.083],[.243,.126,.342],shoeColor,'sphere',look.style===0?'rubber':'cloth','shoe');
  part([foot[0],foot[1]-.102,foot[2]+.09],[.25,.039,.35],color(look.style===0?0x242d39:0xeae7df),'round','rubber','shoe-sole');
  part([foot[0],foot[1]-.023,foot[2]-.021],[.18,.063,.094],color(look.accent),'round','cloth','shoe-collar');
  if(look.style!==0)for(let j=0;j<3;j++)limb([foot[0]-.065,foot[1]+.012,foot[2]+.04+j*.04],[foot[0]+.065,foot[1]+.012,foot[2]+.048+j*.04],.010,color(0xfaf5e9),'cloth','shoelace');
  const shoulder:V3=[side*.271,1.39+up,lean],elbow:V3=[side*.355,1.07+up,-swing*.40],hand:V3=[side*.34,.87+up,-swing*.75];
  if(['typing','reading','reviewing','phone','testing'].includes(pose)){elbow[2]=.20;hand[0]=side*.22;hand[1]=1.10+(pose==='typing'?Math.sin(t*12+side)*.014:0);hand[2]=.41;}
  if(pose==='thinking'&&side===1){elbow[2]=.24;hand[0]=.12;hand[1]=1.49+up;hand[2]=.30;}
  if(pose==='drink'&&side===1){elbow[2]=.20;hand[0]=.12;hand[1]=1.57+up+Math.sin(t*1.6)*.030;hand[2]=.36;}
  if(pose==='stretch'){elbow[0]=side*.39;elbow[1]=1.79+up;hand[0]=side*.29;hand[1]=2.18+up+Math.sin(t*1.3)*.04;}
  if(pose==='communicating'&&side===1){elbow[0]=.43;hand[0]=.46;hand[1]=1.38+up;hand[2]=.22;}
  if(pose==='attention'&&side===1){elbow[1]=1.6;hand[1]=1.98;}
  if(nap){elbow[2]=-.1;hand[0]=side*.3;hand[1]=.85;hand[2]=.21;}
  limb(shoulder,elbow,.195,cloth,'cloth','upper-sleeve');part(elbow,[.186,.183,.19],cloth,'sphere','cloth','elbow');
  const wrist=elbow.map((v,i)=>v+(hand[i]-v)*.80) as V3;limb(elbow,wrist,.155,cloth,'cloth','fore-sleeve');
  const cuffEnd=elbow.map((v,i)=>v+(hand[i]-v)*.90) as V3;limb(wrist,cuffEnd,.16,look.style===0?ivory:color(look.style===1?0x326f77:look.coat),'cloth','cuff');
  limb(cuffEnd,hand,.10,skin,'skin','wrist');part(hand,[.118,.133,.09],skin,'sphere','skin','hand');
  part([hand[0]-side*.066,hand[1]+.01,hand[2]+.033],[.052,.080,.052],skin,'capsule','skin','thumb');
  for(let j=0;j<3;j++)part([hand[0]+(j-1)*.031,hand[1]-.035,hand[2]+.013],[.032,.055,.058],skin,'capsule','skin','fingers');
  if(side===-1){part([wrist[0],wrist[1]+.045,wrist[2]],[.154,.032,.10],dark,'round','rubber','watch-band');part([wrist[0],wrist[1]+.064,wrist[2]],[.088,.023,.087],color(0xaebdc4),'round','metal','watch');}
 }
 if(pose==='phone'){
  part([0,1.18,.475],[.236,.354,.040],dark,'round','metal','phone');part([0,1.18,.499],[.204,.300,.008],color(0x90bdc7),'round','screen','phone-screen',0,0,.8);
 }
 if(pose==='drink'){
  const y=1.61+up+Math.sin(t*1.6)*.030;part([.12,y,.43],[.19,.24,.19],color(0xefe8dd),'cylinder','matte','mug');part([.12,y+.123,.43],[.166,.012,.166],color(0x736052),'cylinder','matte','mug-liquid');part([.242,y,.43],[.13,.16,.12],color(0xefe8dd),'ring','matte','mug-handle',0,0);
 }
 if(pose==='reading'||pose==='reviewing'){
  part([0,1.13,.46],[.43,.041,.29],color(0xe7e2d7),'round','matte','document');for(let i=0;i<4;i++)part([-.005,1.153,.38+i*.043],[.28,.003,.008],color(0x96a4ac),'cube','matte','document-line');
 }
 out.push({p:[p[0],.128,p[2]],s:[1.06,.012,.82],color:color(0x485267),mesh:'cylinder',alpha:.20,surface:'shadow',tag:'contact-shadow'});
 if(selected)out.push({p:[p[0],.135,p[2]],s:[1.04,1.04,.035],matrix:oriented([p[0],.135,p[2]],[1.04,1.04,.035],0,Math.PI/2),color:color(look.accent),mesh:'ring',surface:'matte',emissive:.45,tag:'selection-ring'});
 return out;
}
