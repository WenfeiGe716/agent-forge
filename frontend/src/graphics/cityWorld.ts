import {Part,color,RenderQuality,bevel} from './officeRenderer';
import {carKit,pedestrianKit,PARKED_CARS} from './cityAssets';
import {Vec} from '../office/behavior';
import {CityTraffic} from '../office/cityTraffic';
export type CityLandmark={id:string;name:string;position:Vec;description:string};
export function makeCity(extension=0){
 const parts:Part[]=[],landmarks:CityLandmark[]=[],lamps:Vec[]=[];const cz=14+extension,cx=20;
 const add=(p:Vec,s:Vec,h:number,mesh='cube',yaw=0,emissive=0)=>parts.push({p,s,color:color(h),mesh,yaw,emissive:0,nightEmissive:emissive,layer:'environment',tag:'city-static',castShadow:p[1]<9});
 // Office remains the occupied campus. Surrounding meshes never participate in office pathfinding.
 add([0,-.72,extension/2],[102,.45,94+extension],0xc2d2ca);
 for(const [x,z,w,d] of [[0,-18,97,12],[0,cz+13,97,18],[-20,0,10,23],[37,-3,20,35]] as number[][])add([x,-.44,z],[w,.10,d],0xdde2dc);
 const road=(p:Vec,s:Vec)=>{add(p,s,0x54616c);const q=parts.at(-1)!;q.tag='city-road';q.surface='matte';q.roughness=.92;return q;};
 road([0,-.42,cz],[96,.10,6.7]);road([cx,-.415,extension/2],[6.7,.10,88+extension]);
 // Sidewalk geometry stops at the intersection, instead of hiding full strips
 // under an elevated road patch (which made car wheels sink at crossings).
  // Sidewalks use one consistent neutral concrete finish.  The former
  // near-white strip (0xebeadf) read as a second road surface at the west
  // edge and made the pavement appear to change colour mid-block.
  const sidewalkColor=0xd7dad5;
  for(const off of [-4.45,4.45]){
  for(const [a,b]of[[-48,cx-5.525],[cx+5.525,48]]){add([(a+b)/2,-.355,cz+off],[b-a,.18,2.15],sidewalkColor);parts.at(-1)!.tag='city-sidewalk';parts.at(-1)!.surface='stone';parts.at(-1)!.roughness=.88;}
  for(const [a,b]of[[-44,cz-5.525],[cz+5.525,44+extension]]){add([cx+off,-.355,(a+b)/2],[2.15,.18,b-a],sidewalkColor);parts.at(-1)!.tag='city-sidewalk';parts.at(-1)!.surface='stone';parts.at(-1)!.roughness=.88;}
  for(const side of[-1,1]){add([cx+off,-.355,cz+side*4.45],[2.15,.18,2.15],sidewalkColor);parts.at(-1)!.tag='city-sidewalk';parts.at(-1)!.surface='stone';parts.at(-1)!.roughness=.88;}
 }
 road([cx,-.386,cz],[6.7,.02,6.7]);
 for(let x=-46;x<47;x+=4)if(Math.abs(x-cx)>7)add([x,-.352,cz],[2.05,.014,.075],0xe9e5d5);
 for(let z=-42;z<43+extension;z+=4)if(Math.abs(z-cz)>7)add([cx,-.35,z],[.075,.014,2.05],0xe9e5d5);
 for(const side of [-1,1]){
  for(let i=-3;i<=3;i++){add([cx+side*5.15,-.352,cz+i*.82],[2.5,.02,.43],0xf5f2e9);add([cx+i*.82,-.352,cz+side*5.15],[.43,.02,2.5],0xf5f2e9);}
  add([cx+side*6.8,-.35,cz-side*1.55],[.1,.015,3.03],0xffffff);add([cx+side*1.55,-.35,cz+side*6.8],[3.03,.015,.1],0xffffff);
 }
 // Layered canopy: a trunk, two angled branches and leaf-shaped clusters read
 // as foliage instead of the old pair of saturated candy-ball spheres.
 const tree=(x:number,z:number,s=1)=>{
  add([x,-.22,z],[1.6*s,.2,1.6*s],0xa4aaa0);
  add([x,.8*s,z],[.16,2*s,.16],0x675d52,'cylinder');
  add([x+.18*s,1.35*s,z],[.10,.95*s,.10],0x675d52,'cylinder',-.22);
  add([x-.16*s,1.48*s,z+.08*s],[.10,.82*s,.10],0x675d52,'cylinder',.24);
  for(const [dx,dy,dz,scale,h] of [[-.32,1.95,.02,1.0,0x4f7865],[.28,2.13,.12,1.08,0x5f8b70],[0,2.58,-.06,.88,0x709a78],[.48,1.82,-.1,.72,0x456d5d]] as [number,number,number,number,number][]){
   add([x+dx*s,dy*s,z+dz*s],[1.08*scale*s,.56*scale*s,.74*scale*s],h,'leaf');
  }
 };
 for(const x of [-39,-31,-21,-12,0,11,31,42])for(const z of [cz-5.4,cz+5.4])if(Math.abs(x-cx)>7)tree(x,z,.9);
 for(const x of [-39,-25,-12,2,12,30,42])for(const z of [cz-4.3,cz+4.3]){
  lamps.push([x,3.75,z]);add([x,1.6,z],[.12,3.8,.12],0x687881,'cylinder');add([x,3.52,z],[.75,.12,.17],0x687881);add([x,3.42,z],[.65,.07,.3],0xeef0e7);
  Object.assign(parts.at(-3)!,{surface:'metal',roughness:.3,metalness:.72,tag:'city-lamp-pole'});
  Object.assign(parts.at(-2)!,{surface:'metal',roughness:.3,metalness:.72,tag:'city-lamp-arm'});
  Object.assign(parts.at(-1)!,{surface:'glass',alpha:.78,emissive:.12,tag:'city-lamp-glass'});
 }
 for(const x of [cx-4.25,cx+4.25])for(const z of [cz-6.15,cz+6.15]){add([x,1.05,z],[.13,2.8,.13],0x4b5b62,'cylinder');add([x,2.15,z],[.48,1.2,.32],0x34414c);}
 const building=(id:string,name:string,x:number,z:number,w:number,d:number,h:number,hue:number)=>{
  add([x,h/2-.3,z],[w,h,d],hue);Object.assign(parts.at(-1)!,{surface:'stone',roughness:.79,tag:'city-building-masonry'});
  add([x,h-.22,z],[w+.3,.25,d+.3],0xc1ccd0);Object.assign(parts.at(-1)!,{surface:'stone',roughness:.68,tag:'city-building-cornice'});
  add([x,h+.3,z],[w*.5,.9,d*.45],0x9cabb2);Object.assign(parts.at(-1)!,{surface:'metal',roughness:.38,metalness:.58,tag:'city-building-roof'});
  // Low-cost rows of façade glazing, with narrow vertical mullions; baked once.
  for(let y=1.1;y<h-.6;y+=1.45){add([x,y,z+d/2+.02],[w-.38,.93,.055],0x506875);Object.assign(parts.at(-1)!,{surface:'glass',alpha:.88,tag:'city-building-window'});add([x+w/2+.02,y,z],[.055,.93,d-.4],0x506875);Object.assign(parts.at(-1)!,{surface:'glass',alpha:.88,tag:'city-building-window'});}
  for(let dx=-w/2+.65;dx<w/2;dx+=1.4)add([x+dx,h/2,z+d/2+.06],[.11,h-.55,.09],hue);
  for(let dz=-d/2+.65;dz<d/2;dz+=1.4)add([x+w/2+.06,h/2,z+dz],[.09,h-.55,.11],hue);
  for(let y=1.1;y<h-.6;y+=2.9)for(let dx=-w/2+1;dx<w/2;dx+=2.8){add([x+dx,y,z+d/2+.11],[.56,.69,.025],0xd1b879,'cube',0,.14);Object.assign(parts.at(-1)!,{surface:'glass',alpha:.82,tag:'city-building-warm-window'});}
  add([x,-.18,z+d/2+1.2],[w+1,.35,1.5],0xb6bbb8);Object.assign(parts.at(-1)!,{surface:'stone',roughness:.72,tag:'city-building-plinth'});landmarks.push({id,name,position:[x,h+.9,z],description:'城市环境地标 · 不属于项目、模型或Agent执行记录'});
 };
 // Keep the six navigable landmarks, but make the skyline a low-rise campus
 // backdrop.  The office remains the visual focal point instead of being
 // boxed in by oversized towers.
 // Keep the city backdrop low and open around the office. The former towers
 // are now small campus wings/community buildings, leaving the office as the
 // visual focus without removing navigable landmark records.
 building('north-tower','北岸金融中心',-26,-27,8,7,6,0x7a909f);
 building('innovation','创新大厦',-10,-30,7,8,7,0x748f9c);
 building('cloud','云端研发中心',3,-29,8,8,6,0x91a2ad);
 building('east-tower','国际商务中心',36,-21,9,9,8,0x8094a8);
 building('residence','城市公寓',-37,-10,7,8,5,0xb0ada4);
 // Mall is kept low and to the side, preserving the office sightline from the south-east.
 const mx=-23,mz=cz+13;
 add([mx,2.2,mz],[14,5.1,9],0xddd5c6);add([mx,4.85,mz],[14.7,.42,9.7],0xf0e8d9);
 add([mx,2.1,mz-4.54],[12.5,3.6,.07],0x9cbbc1);add([mx,2.1,mz+4.54],[12.5,3.6,.07],0x9cbbc1);
 for(let i=-5;i<=5;i+=2){add([mx+i,2.1,mz-4.62],[.12,3.7,.12],0xe2ded5);add([mx+i,2.1,mz+4.62],[.12,3.7,.12],0xe2ded5);}
 add([mx,1.3,mz-4.7],[2.5,2.6,.11],0x537887);add([mx,3.1,mz-5.45],[5,.17,2.3],0xb78e75);add([mx,5.12,mz],[5,.2,4],0xa2c0c5);
 landmarks.push({id:'mall',name:'FORGE MALL · 商场',position:[mx,5.75,mz],description:'商场外观与入口广场 · 本版没有室内购物或任务消费逻辑'});
 for(const x of [-33,-16,-9,0,9]){tree(x,cz+10,.9);add([x,0,cz+8],[2,.42,.6],0x8b8f87);add([x,.39,cz+8.23],[2,.42,.12],0xa3927e);}
 for(let i=0;i<5;i++){const x=-5+i*4.6,z=cz+21;add([x,.7,z],[3.8,2.1,4],0xd6d9d4);add([x,1.85,z],[3.95,.2,4.2],0xad9b88);add([x,.65,z-2.05],[2.6,1.5,.05],0x98b4b6);add([x,1.55,z-2.3],[3.9,.16,.7],i%2?0x789797:0xa98a79);}

 // Entry apron, shallow risers and an off-street parking bay connect the office
 // to the same world. These are scenery, not Agent navigation destinations.
 add([14.1,-.27,3.7],[1.1,.20,6.3],0xd2d5cc);
 add([13.65,-.11,3.7],[.25,.17,4.2],0xe1e0d5);
 for(const z of [1.6,5.8]){add([14.1,.90,z],[.12,2.40,.12],0x91a1a6);}
 add([14.05,2.08,3.7],[1.5,.10,4.7],0xb7ccd0);
 add([14.4,.58,6.45],[.17,1.72,.94],0x536a70);
 add([14.505,1.03,6.45],[.035,.42,.72],0xcedecc);
 add([31.4,-.39,-1.4],[11.2,.06,14.5],0x6c7a81);
 for(const row of [-5.4,2.5])for(let i=0;i<4;i++){
  const x=27.5+i*2.6;add([x,-.350,row],[.065,.016,4.0],0xe8e5d8);
 }
 add([31.4,-.35,-1.25],[10.5,.016,.075],0xe8e5d8);
 for(const car of PARKED_CARS){const kit=carKit({...car,stopped:true},car.index,true,'balanced',true);for(const q of kit){q.tag='parked-'+(q.tag||'car');q.layer='environment';}parts.push(...kit);}
 add([36.05,1.02,5.1],[.10,2.8,.10],0x6a7b83,'cylinder');add([36.05,2.17,5.1],[.88,.68,.10],0x597f98);
 // Benches, bollards, recycling bins and planted entrance border.
 for(const z of [-.3,7.2]){add([14.45,-.18,z],[1.05,.37,.72],0xa3b3ac);tree(14.45,z,.58);}
 for(const x of [7.0,10.1]){add([x,-.01,10.15],[1.7,.35,.55],0x9c927c);add([x,.30,10.35],[1.7,.45,.095],0xb5a589);}
 for(const x of [9.9,11.3,12.7])add([x,.1,10.35],[.12,.75,.12],0x7e9499,'cylinder');
 add([6.1,.10,10.2],[.50,.75,.49],0x667e7c);add([6.65,.10,10.2],[.50,.75,.49],0x748795);
 return{parts,landmarks,lamps,extension,parkedCars:PARKED_CARS};
}
export function sidewalkWalkers(traffic:CityTraffic){
 const count=traffic.density==='low'?2:traffic.density==='busy'?8:5;
 return Array.from({length:count},(_,i)=>{
  // Walk a smooth ping-pong path.  The old triangle wave changed direction
  // at a hard corner (and at the modulo seam), which made the sprite snap and
  // read as a teleport when the render frame landed on the endpoint.  A cosine
  // path eases to zero velocity at both ends; the short turn window also lets
  // the actor rotate in place instead of reversing its heading instantly.
  const cycle=84,phase=(traffic.time*.7+i*8.4)%cycle;
  const theta=phase/42*Math.PI;
  const x=-38+21*(1-Math.cos(theta));
  const speedSign=Math.sin(theta);
  const forward=speedSign>=0;
  const yaw=forward
   ? Math.PI/2
   : -Math.PI/2;
  const z=traffic.cz+4.75+(i%2)*.39;
  return{id:`sidewalk-${i}`,x,z,yaw,walking:true};
 });
}
export function drawCity(traffic:CityTraffic,day:boolean,lamps:Vec[],quality:RenderQuality='balanced'):Part[]{
 const parts:Part[]=[],add=(p:Vec,s:Vec,h:number,mesh='cube',yaw=0,emissive=0)=>parts.push({p,s,color:color(h),mesh,yaw,emissive,layer:'environment',tag:'city-signal'});
 traffic.cars.forEach((car)=>parts.push(...carKit(car,Number(car.id.split('-')[2])*3+Number(car.id.split('-')[3]),day,quality)));
 traffic.pedestrians.forEach((p)=>{const ramp=Math.max(0,Math.min(1,(Math.abs(p.z-traffic.cz)-3.35)/.9));parts.push(...pedestrianKit({...p,groundY:-.365+ramp*.1},Number(p.id.split('-')[2]),traffic.time,quality));});
 sidewalkWalkers(traffic).forEach((p,i)=>parts.push(...pedestrianKit(p,i+8,traffic.time,quality)));
 for(const [j,x] of [traffic.cx-4.25,traffic.cx+4.25].entries())for(const [k,z] of [traffic.cz-6.15,traffic.cz+6.15].entries()){
  const state=(j===k?traffic.signals.ew:traffic.signals.ns);for(const [index,label]of ['red','yellow','green'].entries())add([x,2.48-index*.32,z+.18],[.24,.24,.045],state===label?({red:0xff6658,yellow:0xffcb60,green:0x76e4af} as any)[label]:0x465255,'limb',0,state===label?1:0);
  add([x+.32,1.35,z+.1],[.2,.28,.13],traffic.signals.ped==='green'?0x70e6ab:0xf27b6d,'cube',0,1);
 }
 if(!day)for(const [x,y,z]of lamps){add([x,y-.3,z],[.7,.08,.34],0xffe0a0,'cube',0,1);add([x,-.26,z],[2.3,.008,1.4],0xb5af86,'limb',0,.15);}
 for(const part of parts)part.layer='environment';
 return parts;
}
