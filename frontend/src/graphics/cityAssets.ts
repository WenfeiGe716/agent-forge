/** Original low-cost city assets. Dimensions use the traffic engine's 2.8 m
 * collision length and road height. This is environment geometry, never an Agent.
 */
import {Part,color,bevel,oriented,RenderQuality} from './officeRenderer';
import {V3,multiply,modelMatrix} from './scene';
export const ROAD_TOP=-.37;
export const PARKED_CARS=[
 {id:'parked-0',x:28.8,z:-5.4,yaw:Math.PI,index:1},
 {id:'parked-1',x:31.4,z:-5.4,yaw:Math.PI,index:3},
 {id:'parked-2',x:34,z:-5.4,yaw:Math.PI,index:2},
 {id:'parked-3',x:28.8,z:2.5,yaw:0,index:0},
];
// Restrained architectural paint set: lower saturation reads as painted metal
// instead of toy plastic, while the renderer's metalness adds the highlight.
const paints=[0x536a78,0xb79f78,0x796b73,0xb8c2c1,0x687b84,0x58776f];
export function carKit(car:{x:number;z:number;yaw:number;stopped:boolean},index:number,day:boolean,quality:RenderQuality='balanced',parked=false):Part[]{
 const out:Part[]=[],parent=modelMatrix([car.x,0,car.z],[1,1,1],car.yaw),paint=paints[index%paints.length];
 const put=(q:Part)=>{q.matrix=multiply(parent,q.matrix||modelMatrix(q.p,q.s,q.yaw));q.assetOrigin=[car.x,0,car.z];out.push(q);};
 const b=(p:V3,s:V3,h:number,r=.04,surface:Part['surface']='matte',tag='city-car')=>put(bevel(p,s,h,r,{surface,tag,...(['city-car-body','city-car-cabin','city-car-hood','city-car-mirror'].includes(tag)?{roughness:.30,metalness:.36}:{})}));
 const q=(p:V3,s:V3,h:number,mesh='cube',tag='city-car',emissive=0,surface:Part['surface']='matte',matrix?:Float32Array)=>put({p,s,color:color(h),mesh,tag,emissive,surface,matrix});
 // Sedan / compact / small van silhouettes; no makes or downloaded assets.
 const van=index%4===2,roof=van?.92:.78;
 b([0,.11,0],[1.32,.50,2.74],paint,.13,'metal','city-car-body');
 b([0,-.09,0],[1.36,.13,2.56],0x34434e,.035,'rubber','city-car-sill');
 b([0,roof-.19,-.19],[1.14,van?.82:.52,van?1.88:1.48],paint,.11,'metal','city-car-cabin');
 b([0,.382,.98],[1.17,.08,.54],paint,.035,'metal','city-car-hood');
 q([0,roof-.20,van?.76:.562],[1.025,van?.45:.335,.024],0x435866,'cube','city-car-windshield',0,'glass');
 q([0,roof-.23,van?-1.146:-.952],[.97,van?.37:.30,.025],0x3c505c,'cube','city-car-rear-window',0,'glass');
 for(const side of [-1,1]){
  q([side*.578,roof-.23,-.14],[.018,van?.36:.29,van?1.52:1.21],0x405764,'cube','city-car-side-window',0,'glass');
  q([side*.589,roof-.21,-.16],[.025,van?.43:.37,.065],paint,'cube','city-car-pillar',0,'metal');
  b([side*.730,.45,.40],[.19,.12,.16],paint,.032,'metal','city-car-mirror');
  q([side*.668,.26,-.34],[.024,.029,.19],0xbfc8c8,'cube','city-car-handle',0,'metal');
  for(const z of [-.83,.84]){
   q([side*.656,-.12,z],[.50,.185,.50],0x29333c,'cylinder','city-wheel',0,'rubber',oriented([side*.656,-.12,z],[.50,.185,.50],0,0,Math.PI/2));
   q([side*.756,-.12,z],[.31,.021,.31],0xa6b5bc,'cylinder','city-wheel-hub',0,'metal',oriented([side*.756,-.12,z],[.31,.021,.31],0,0,Math.PI/2));
   if(quality==='detail')for(let k=0;k<5;k++){const angle=k*Math.PI*2/5;q([side*.77,-.12,z],[.025,.245,.029],0xd3dadd,'cube','city-wheel-spoke',0,'metal',oriented([side*.77,-.12,z],[.025,.245,.029],0,angle));}
  }
  q([side*.448,.213,1.380],[.30,.13,.026],day?0xdde7da:0xffedc0,'cube','city-headlight',parked?0:day?.15:1,'screen');
  q([side*.448,.213,-1.380],[.30,.125,.026],0xdd6557,'cube','city-tail-light',parked?0:car.stopped?.95:day?.12:.5,'screen');
 }
 // Slim beltline and door seams break up the large side volume at a distance.
 for(const side of [-1,1]){
  q([side*.688,.05,-.14],[.018,.035,1.08],0x26343b,'cube','city-car-beltline',0,'metal');
  for(const z of [-.62,.38])q([side*.694,.10,z],[.016,.028,.018],0x34434a,'cube','city-car-door-seam',0,'metal');
 }
 b([0,.025,1.345],[1.22,.115,.062],0xb6c4c8,.019,'metal','city-car-bumper');
 b([0,.08,-1.362],[1.22,.09,.045],0xb6c4c8,.018,'metal','city-car-bumper');
 q([0,.18,1.389],[.28,.105,.013],0xe2e8dc,'cube','city-car-number-plate');
 if(index%6===1){b([0,roof+.081,-.14],[.39,.12,.29],0xe5c77b,.024,'matte','city-car-roof-sign');}
 if(!day&&!parked&&quality!=='economy'){
  // Local light patches, not a claim of deferred/headlight shadow projection.
  q([0,ROAD_TOP+.009,2.10],[1.65,.010,1.22],0x928f72,'limb','city-headlight-patch',.2);
 }
 return out;
}
export function pedestrianKit(p:{x:number;z:number;yaw:number;walking:boolean;groundY?:number},index:number,time:number,quality:RenderQuality='balanced'):Part[]{
 const out:Part[]=[],parent=modelMatrix([p.x,p.groundY??-.265,p.z],[1,1,1],p.yaw),skin=[0xe1bea4,0xbf9272,0xd3a385,0xe6c4ae][index%4],cloth=paints[index%paints.length];
 const put=(q:Part)=>{q.matrix=multiply(parent,q.matrix||modelMatrix(q.p,q.s,q.yaw));out.push(q);};
 const b=(a:V3,s:V3,h:number,r=.025,tag='city-ped')=>put(bevel(a,s,h,r,{tag}));
 const q=(a:V3,s:V3,h:number,mesh='limb',tag='city-ped',matrix?:Float32Array)=>put({p:a,s,color:color(h),mesh,tag,matrix});
 // Keep the street gait subtle: the previous ±.38 stride made the short
 // procedural legs kick through the torso at road scale.  A slower, smaller
 // swing reads as a steady walk and remains deterministic across frame rates.
 const stride=p.walking?Math.sin(time*5.4+index*.72)*.22:0;
 b([0,.88,0],[.38,.56,.27],cloth,.065,'city-ped-coat');
 q([0,1.323,.01],[.305,.37,.315],skin,'limb','city-ped-head');
 q([0,1.446,-.022],[.31,.17,.31],index%3?0x4a3933:0x877265,'limb','city-ped-hair');
 for(const side of [-1,1]){
  const a=side*stride;
  q([side*.106,.331,Math.sin(a)*.14],[.128,.60,.145],0x445463,'capsule','city-ped-leg',oriented([side*.106,.331,Math.sin(a)*.14],[.128,.60,.145],0,a));
  b([side*.106,.047,.041+Math.sin(a)*.29],[.145,.09,.245],0x303f49,.025,'city-ped-shoe');
  q([side*.251,.853,-Math.sin(a)*.18],[.105,.48,.115],cloth,'capsule','city-ped-arm',oriented([side*.251,.853,-Math.sin(a)*.18],[.105,.48,.115],0,-a));
  q([side*.251,.624,-Math.sin(a)*.34],[.109,.125,.105],skin,'limb','city-ped-hand');
  if(quality!=='economy')q([side*.058,1.338,.163],[.025,.038,.013],0x364650,'cube','city-ped-eye');
 }
 if(index%3===0)b([.238,.64,-.105],[.21,.34,.27],0x9d876e,.032,'city-ped-bag');
 return out;
}
