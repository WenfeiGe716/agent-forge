/** Render an explicitly applied department floor, never infer organization or permissions. */
import {Agent,OrganizationFloor,OrganizationLayout,OrganizationConfig,OrganizationTeam,OrganizationZone} from '../types';
import {World,Vec,AgentPlace} from '../office/behavior';
import {Part,color,bevel} from './officeRenderer';
import {chairKit,deskKit,plantKit} from './furniture';
import {workstationIslandInlays} from './workstationIslands';
import type {Layout,RenderedDesk} from './officeWorld';
type Rect={x:number;z:number;w:number;d:number;seat?:boolean;zoneId?:string};

const floorTitle=(floor:OrganizationFloor)=>{const base=`${floor.floor} 楼 · ${floor.department}`;return !floor.name||[base,floor.department].includes(floor.name)?base:base+' · '+floor.name;};

export function officeFloorOptions(agents:Agent[],organization?:OrganizationLayout|null){
 const definitions=organization?.config?.floors||[];
 const numbers=[...new Set([...definitions.map(f=>f.floor),...(Array.isArray(organization?.config?.desks)?[]:agents.map(a=>a.office_position?.floor||1))])].sort((a,b)=>a-b);
 return(numbers.length?numbers:[1]).map(floor=>{const definition=definitions.find(f=>f.floor===floor);return{floor,definition,label:definition?floorTitle(definition):`${floor} 楼 · ${definitions.length?'未纳入组织布局':'办公区'}`};});
}

function physicalLayout(agents:Agent[],config:OrganizationConfig){
 const teams=new Map<string,OrganizationTeam>(),ownedZones=new Set<string>(),desks:RenderedDesk[]=[],deskIds=new Set<string>(),members=new Set<string>();
 const realIds=new Set(agents.filter(a=>a.enabled!==false).map(a=>a.id));
 for(const team of config.teams||[]){
  const floor=config.floors.find(f=>f.floor===team.floor);
  if(!team.id||teams.has(team.id)||!floor||!Array.isArray(team.zone_ids)||!team.zone_ids.length||!Number.isInteger(team.headcount)||team.headcount<0)continue;
  if(new Set(team.zone_ids).size!==team.zone_ids.length||team.zone_ids.some(id=>!floor.zones.some(z=>z.id===id&&z.kind==='work')||ownedZones.has(team.floor+':'+id)))continue;
  teams.set(team.id,team);for(const id of team.zone_ids)ownedZones.add(team.floor+':'+id);
 }
 // Validate across the WHOLE layout before selecting a floor, so changing the
 // visible floor cannot duplicate a desk identity or a real member binding.
 for(const desk of config.desks||[]){
  const floor=config.floors.find(f=>f.floor===desk.floor),zone=floor?.zones.find(z=>z.id===desk.zone&&z.kind==='work'),team=desk.team_id?teams.get(desk.team_id):undefined;
  if(!desk.id||deskIds.has(desk.id)||!zone||![desk.x,desk.z,zone.x,zone.z,zone.width,zone.depth].every(Number.isFinite)||zone.width<=0||zone.depth<=0)continue;
  if(Math.abs(zone.x)+zone.width/2>12+1e-9||Math.abs(zone.z)+zone.depth/2>8+1e-9||xOutside(desk.x,zone.x,zone.width)||desk.z-.6<zone.z-zone.depth/2-1e-9||desk.z+1.45>zone.z+zone.depth/2+1e-9)continue;
  if(desk.team_id&&(!team||team.floor!==desk.floor||!team.zone_ids.includes(desk.zone)))continue;
  if(desks.some(d=>d.floor===desk.floor&&Math.abs(d.x-desk.x)<2.8-1e-9&&Math.abs(d.z-desk.z)<2.05-1e-9))continue;
  const rendered=desk.agent_id&&team&&realIds.has(desk.agent_id)&&!members.has(desk.agent_id)?desk.agent_id:null;
  desks.push({...desk,rendered_agent_id:rendered,occupancy:rendered?'occupied':desk.agent_id?'reserved':'vacant'});
  deskIds.add(desk.id);if(desk.agent_id)members.add(desk.agent_id);
 }
 return{desks,teams};
}

export function makeDepartmentOffice(agents:Agent[],floor:OrganizationFloor,path:(a:Vec,b:Vec,obstacles:Rect[])=>Vec[],config?:OrganizationConfig):Layout{
 const parts:Part[]=[],obstacles:Rect[]=[],places:AgentPlace[]=[],actors=new Map<string,{agent:Agent;index:number}>();
 const zones:Layout['zones']=[],facilities:World['facilities']={drink:[],nap:[],restroom:[]},meetingSeats:NonNullable<World['meetingSeats']>=[];
 const add=(p:Vec,s:Vec,h:number,tag='department-floor',extra:Partial<Part>={})=>{const part=bevel(p,s,h,Math.min(...s)*.18,{tag,...extra});parts.push(part);return part;};
 const round=(p:Vec,s:Vec,h:number,tag:string,surface:Part['surface']='matte')=>add(p,s,h,tag,{mesh:'cylinder',s,surface});
 // Shared collision probe used by optional pantry/common decorations.  It
 // must exist before the zone pass, where reception furniture may call it.
 const free=(x:number,z:number,w:number,d:number)=>!obstacles.some(o=>Math.abs(x-o.x)<(w+o.w)/2+.06&&Math.abs(z-o.z)<(d+o.d)/2+.06)
  &&!places.some(a=>a.reception&&Math.abs(a.reception[0]-x)<w/2+.35&&Math.abs(a.reception[2]-z)<d/2+.35)
  &&!Object.values(facilities).flat().some(slot=>Math.abs(slot.position[0]-x)<w/2+.35&&Math.abs(slot.position[2]-z)<d/2+.35);
 const stamp=(zone:OrganizationZone,partStart:number,obstacleStart:number)=>{
  for(const part of parts.slice(partStart))Object.assign(part,{zoneId:zone.id});
  for(const obstacle of obstacles.slice(obstacleStart))obstacle.zoneId=zone.id;
 };
 const water=(id:string,x:number,z:number)=>{
  add([x,.62,z],[.84,1.26,.84],0xd4d8d4,'water-cooler',{surface:'stone'});
  const bottle=round([x,1.6,z],[.53,.72,.53],0xb8cfce,'water-bottle','glass');bottle.alpha=.74;
  round([x,1.95,z],[.25,.06,.25],0xc9d2cf,'water-bottle-cap','matte');
  add([x+.46,.85,z],[.065,.46,.5],0x657175,'water-dispenser',{surface:'metal'});
  for(const[dz,h]of[[-.12,0x738b89],[.12,0xa4877e]])add([x+.50,.91,z+dz],[.07,.07,.07],h,'water-tap');
  add([x+.46,.61,z],[.16,.045,.48],0x59676a,'water-drip-tray',{surface:'metal'});
  for(const dx of[-.28,.28])for(const dz of[-.28,.28])add([x+dx,.10,z+dz],[.10,.15,.10],0x475256,'water-foot',{surface:'rubber'});
  obstacles.push({x,z,w:1.02,d:.9});
  for(const[slot,dz]of[-.33,.33].entries())facilities.drink.push({id:id+':water:'+slot,position:[x+1.05,0,z+dz],yaw:-Math.PI/2});
 };
 const sofa=(zone:OrganizationZone)=>{
  const x=zone.x-zone.width/2+1.05,z=zone.z,length=Math.min(3,zone.depth-.65);
  add([x,.61,z],[1.4,.53,length],0x78837e,'sofa-base',{surface:'cloth'});
  add([x-.55,1,z],[.3,.95,length+.06],0x909a91,'sofa-back',{surface:'cloth'});
  for(const sign of[-1,1]){
   add([x,.83,z+sign*(length/2-.1)],[1.45,.7,.2],0x87948c,'sofa-arm',{surface:'cloth'});
   // Keep the cushion at the same contact height as the authored seated
   // character pose.  The previous 0.925 centre put the hip inside the
   // cushion and made the legs visibly pass through the sofa front edge.
   add([x+.15,.82,z+sign*length*.21],[.94,.12,length*.43],0xa8b0a5,'sofa-cushion',{surface:'cloth'});
   add([x-.20,1.05,z+sign*length*.21],[.25,.34,.48],0xb5b9ae,'sofa-pillow',{surface:'cloth'});
   for(const dx of[-.5,.5])add([x+dx,.19,z+sign*(length/2-.28)],[.09,.2,.09],0x566e68,'sofa-foot',{surface:'wood'});
   // Seat slightly toward the back so the authored nap feet remain inside
   // the sofa footprint instead of clipping through its front edge.
   facilities.nap.push({id:zone.id+':sofa:'+sign,position:[x-.20,0,z+sign*length*.21],yaw:Math.PI/2});
  }
  obstacles.push({x,z,w:1.46,d:length+.08,seat:true});
  if(zone.width>=4.8){
   const cx=x+2.35,diameter=Math.min(1.75,zone.width-3.1);
   round([cx,.65,z],[diameter,.13,diameter],0xdfc4a2,'coffee-table','wood');
   round([cx,.33,z],[.6,.6,.6],0xacbdb3,'coffee-table-base','metal');
   add([cx-.18,.74,z],[.40,.04,.31],0x809bac,'lounge-magazine',{surface:'cloth'});
   round([cx+.3,.80,z-.12],[.15,.18,.15],0xe8e5da,'lounge-cup');
   obstacles.push({x:cx,z,w:diameter+.08,d:diameter+.08});
  }
 };
 // Expanded neutral perimeter apron: the inner 27×20 floor remains the
 // stable layout/navigation surface, while the surrounding apron gives the
 // office a more spacious campus feel for extra furniture and circulation.
 // Give the department a real campus-like footprint around the stable 27×20
 // work floor.  The inner coordinates remain unchanged for saved layouts,
 // while the wider apron makes circulation and shared amenities visible.
 // Keep the expansion on the west (left) side of the campus. The south edge
 // stops before the street so the office never spills onto the road.
 add([-4,-.405,0],[40,.08,20],0xc8cbc5,'department-perimeter');
 add([0,-.37,0],[27,.7,20],0xd3d5ce);add([0,0,0],[26.85,.11,19.85],0xf0eee7);
 // Centered, quiet tile joints; the editable zone planes stay above the floor.
 for(let x=-12;x<=12;x+=1.5)parts.push({p:[x,.063,0],s:[.012,.005,19.8],color:color(0xdedcd4),tag:'floor-tile-joint',castShadow:false});
 for(let z=-9;z<=9;z+=1.5)parts.push({p:[0,.063,z],s:[26.7,.005,.012],color:color(0xdedcd4),tag:'floor-tile-joint',castShadow:false});
 // Edge amenities occupy the new apron only: low-profile charging lockers,
 // LED wayfinding strips and planters add useful office detail without
 // colliding with editable desks or changing navigation contracts.
 for(const x of[-20.8,-16.4,-12.4,12.4,16.8]){
  // Keep the service edge on the north side; the south edge borders the
  // street and must remain a clean sidewalk/curb transition.
  add([x,.62,10.75],[1.55,1.18,.56],0x596870,'apron-storage',{surface:'metal'});
  add([x,.83,10.46],[1.23,.48,.025],0x1e3039,'apron-storage-panel',{surface:'screen'});
  for(let i=0;i<3;i++)add([x-.4+i*.4,.86,10.44],[.12,.018,.012],i===1?0x75d5be:0x5f94b5,'apron-storage-led',{surface:'screen'});
 }
 for(const z of[10.7]){
  for(let x=-8;x<=8;x+=4) add([x,.075,z],[2.4,.018,.055],0x6b9c9b,'apron-led-strip',{surface:'metal',emissive:.16});
  for(const x of[-10.2,10.2]){
   parts.push(...plantKit(x,z,.72));
   obstacles.push({x,z,w:.85,d:.85});
  }
 }
 // The west apron is usable workplace space, not an empty border. These
 // amenities are visual facilities only (no task, meeting, or fake presence
 // is created): a compact gym, a glass training room, and a small pet corner.
 add([-19,.075,-4],[5.2,.024,4.2],0xd0d2cc,'amenity-gym-floor',{surface:'cloth'});
 add([-20.7,.46,-4],[.62,.78,1.35],0x526570,'amenity-gym-rack',{surface:'metal'});
 add([-19.0,.48,-4.0],[1.65,.12,.58],0x6c8790,'amenity-gym-bench',{surface:'cloth'});
 add([-17.45,.70,-4.0],[.12,1.35,.12],0x9aaab0,'amenity-gym-bike',{surface:'metal'});
 add([-17.45,1.38,-4.0],[.72,.08,.30],0x2a3a45,'amenity-gym-console',{surface:'screen'});
 for(const x of[-20.2,-19.25,-18.3])add([x,.09,-5.45],[.56,.018,.56],0x91aaa4,'amenity-gym-mat',{surface:'cloth'});
 add([-19,.075,1.4],[5.2,.024,3.2],0xd3d5cf,'amenity-training-floor',{surface:'cloth'});
 add([-19,1.25,2.92],[5.1,2.35,.06],0x9fb9be,'amenity-training-glass',{surface:'glass',alpha:.30});
 add([-19,2.42,2.97],[5.2,.08,.10],0x748b91,'amenity-training-rail',{surface:'metal'});
 add([-20.4,.65,1.0],[2.2,.12,.9],0xb99d78,'amenity-training-table',{surface:'wood'});
 for(const x of[-21.2,-19.6])for(const z of[.35,1.65])parts.push(...chairKit(x,z,0x879ca3,0,.72));
 add([-17.2,.70,1.0],[.10,1.25,1.2],0x344b55,'amenity-training-display',{surface:'screen'});
 add([-19,.075,8.5],[5.2,.024,2.3],0xd4d6d0,'amenity-pet-floor',{surface:'cloth'});
 add([-20.25,.34,8.35],[1.25,.48,.95],0xb88968,'amenity-pet-bed',{surface:'cloth'});
 // Small low-poly cat sculpture keeps the pet corner readable at overview
 // scale without introducing an employee-like actor or business event.
 round([-19.0,.52,8.35],[.58,.52,.82],0x343d4a,'pet-cat-body','cloth');
 round([-19.0,.96,8.35],[.48,.45,.48],0x424d5a,'pet-cat-head','cloth');
 add([-19.22,1.24,8.35],[.20,.28,.18],0x424d5a,'pet-cat-ear',{tag:'leaf'});
 add([-18.78,1.24,8.35],[.20,.28,.18],0x424d5a,'pet-cat-ear',{tag:'leaf'});
 add([-18.55,.72,8.35],[.72,.10,.10],0x424d5a,'pet-cat-tail',{surface:'cloth'});
 add([0,.75,-9.7],[26.8,1.5,.17],0xebece6,'department-wall');
 for(let i=0;i<7;i++){
  const x=-11.2+i*3.75;
  add([x,1.95,-9.7],[3.3,1.2,.14],0x9eafb9,'department-window-frame',{surface:'metal'});
  add([x,1.95,-9.61],[3.13,1.07,.04],0xc7e1e8,'department-window');
  add([x,1.95,-9.56],[.055,1.1,.025],0xffffff,'department-window-mullion');
  add([x,1.34,-9.5],[3.42,.07,.35],0xd3dfe1,'department-window-sill');
 }
 // Continue the rear glazing across the new west wing so the enlarged room
 // has a finished back wall rather than a hard visual cutoff.
 add([-18,.75,-9.7],[8.0,1.5,.17],0xebece6,'department-wall-extension');
 for(let i=0;i<3;i++){
  const x=-20.8+i*2.8;
  add([x,1.95,-9.7],[2.35,1.2,.14],0x9eafb9,'department-window-frame-extension',{surface:'metal'});
  add([x,1.95,-9.61],[2.18,1.07,.04],0xc7e1e8,'department-window-extension',{surface:'glass',alpha:.88});
  add([x,1.95,-9.56],[.055,1.1,.025],0xffffff,'department-window-mullion-extension');
  add([x,1.34,-9.5],[2.48,.07,.35],0xd3dfe1,'department-window-sill-extension');
 }
 const accent=(value?:string|null,kind='work')=>value&&/^#[\da-f]{6}$/i.test(value)?Number.parseInt(value.slice(1),16):kind==='meeting'?0x969fcf:kind==='common'?0x86b3a5:0x69b7b0;
 for(const zone of floor.zones){
  const partStart=parts.length,obstacleStart=obstacles.length;
  const hue=accent(zone.color,zone.kind);
  // No coloured rectangles: zones are represented by low-contrast woven rugs
  // that blend into the floor.  The semantic accent remains on the label and
  // agent marker, while geometry stays neutral and realistic.
  const rug=zone.kind==='meeting'?0xcac8c0:zone.kind==='common'?0xd2d0c7:0xd8d6ce;
  add([zone.x,.079,zone.z],[zone.width,.024,zone.depth],rug,'organization-zone',{surface:'cloth',roughness:.98});
  add([zone.x,.102,zone.z],[Math.max(.1,zone.width-.22),.018,Math.max(.1,zone.depth-.22)],rug,'organization-zone-interior',{color:color(rug),surface:'cloth',roughness:.98});
  zones.push({name:zone.name,position:[zone.x,.19,zone.z-zone.depth/2+.17],accent:hue});
  if(zone.kind==='meeting'&&zone.width>=4&&zone.depth>=2.8){
   const width=Math.min(6.4,zone.width-1.4),depth=Math.min(1.9,zone.depth-2.35),span=Math.min(4.4,width-.2);
   add([zone.x,.96,zone.z],[width,.15,depth],0xc8ad89,'meeting-table',{surface:'wood'});
   for(const dx of[-width*.32,width*.32])add([zone.x+dx,.47,zone.z],[.16,.9,Math.max(.3,depth*.65)],0x82949f,'meeting-table-leg',{surface:'metal'});
   obstacles.push({x:zone.x,z:zone.z,w:width,d:depth});
   for(const x of[-span/2,0,span/2])for(const sign of[-1,1]){
    const z=zone.z+sign*(depth/2+.66),yaw=sign<0?0:Math.PI;
    parts.push(...chairKit(zone.x+x,z,hue,sign<0?Math.PI:0,.82));
    obstacles.push({x:zone.x+x,z,w:.94,d:.86,seat:true});
    // The first physical room is the presentation venue; additional meetings are remote.
    if(meetingSeats.length<6)meetingSeats.push({position:[zone.x+x,.105,z],yaw});
   }
   // Two side panels leave both ends open; the furniture never seals a room.
   for(const sign of[-1,1]){
    const x=zone.x+sign*(zone.width/2-.15),panelDepth=zone.depth-.35;
    add([x,1.26,zone.z],[.04,2.5,panelDepth],0xbad7df,'meeting-glass',{alpha:.19});
    for(const dz of[-panelDepth/2,panelDepth/2])add([x,1.25,zone.z+dz],[.055,2.5,.055],0x829ba8,'meeting-glass-post',{surface:'metal'});
    add([x,2.5,zone.z],[.065,.07,panelDepth],0xa6b7c1,'meeting-glass-rail',{surface:'metal'});
    obstacles.push({x,z:zone.z,w:.12,d:panelDepth});
   }
   if(zone.depth>=3.8){
    // A side-mounted display leaves the rear entrance and center seat clear.
    const width=Math.min(3.7,zone.depth-1.3),x=zone.x-zone.width/2+.26;
    add([x,1.76,zone.z],[.14,1.95,width],0x344458,'meeting-board');
    add([x+.076,1.76,zone.z],[.012,1.73,width-.22],0x535e85,'meeting-board-screen',{surface:'screen'});
    for(let i=0;i<3;i++)add([x+.088,1.7,zone.z+(i-1)*width*.285],[.01,.63,width*.21],i%2?0x8fc1c1:0xa8a6d6,'meeting-board-note',{surface:'screen'});
    obstacles.push({x:x+.02,z:zone.z,w:.2,d:width});
   }
  }
  if(zone.kind==='lounge'&&zone.width>=3&&zone.depth>=3)sofa(zone);
  if(zone.kind==='pantry'&&zone.width>=3&&zone.depth>=3){
   water(zone.id,zone.x-zone.width/2+.62,zone.z-zone.depth/2+.8);
   if(zone.width>=3.4){
    const x=zone.x+zone.width/2-.65,z=zone.z-zone.depth/2+.45;
    add([x,.57,z],[.9,1.02,.65],0xb5c7c6,'pantry-cabinet');
    add([x,1.11,z],[1,.07,.73],0xdccdb5,'pantry-counter',{surface:'wood'});
    for(const dx of[-.22,.22]){add([x+dx,.65,z+.335],[.36,.77,.018],0xcdd9d5,'pantry-cabinet-door');add([x+dx,.86,z+.36],[.10,.035,.04],0x648080,'pantry-handle',{surface:'metal'});round([x+dx,1.24,z],[.16,.2,.16],0xeee9df,'pantry-cup');}
    obstacles.push({x,z,w:1,d:.74});
   }
  }
  if(zone.kind==='restroom'&&zone.width>=3&&zone.depth>=3){
   const width=Math.min(2.25,zone.width-.8),depth=Math.min(3.5,zone.depth-1.2),z=zone.z-zone.depth/2+.2+depth/2,front=z+depth/2;
   add([zone.x,1.2,z],[width,2.4,depth],0xe6e7df,'department-restroom');
   add([zone.x,1.15,front+.035],[Math.min(1.35,width-.3),2.23,.065],0xb9c9c8,'restroom-door');
   add([zone.x,2.45,front+.05],[.72,.40,.08],0xd6dfd6,'restroom-sign');
   add([zone.x+.48,1.12,front+.08],[.045,.22,.045],0xdde6e9,'restroom-handle',{surface:'metal'});
   for(const dx of[-width/2+.06,width/2-.06])add([zone.x+dx,1.16,front+.02],[.06,2.32,.08],0xcbd3cf,'restroom-door-frame');
   for(let i=0;i<5;i++)add([zone.x-.24+i*.12,2.12,front+.075],[.07,.11,.012],0xa9bbb7,'restroom-vent');
   obstacles.push({x:zone.x,z,w:width,d:depth+.2});
   facilities.restroom.push({id:zone.id+':wc',position:[zone.x,0,front+.72],yaw:Math.PI});
   zones.at(-1)!.position=[zone.x,2.65,front+.09];
  }
  if(zone.kind==='common'&&zone.width>=5&&zone.depth>=2.8){
   const left=zone.x-zone.width/2+1,right=zone.x+zone.width/2-1;
   add([left,.65,zone.z],[.75,1.3,.75],0xd8e2e4,'water-cooler');
   add([left,1.52,zone.z],[.5,.55,.5],0x89c0d1,'water-bottle');
   obstacles.push({x:left,z:zone.z,w:.85,d:.85});
   facilities.drink.push({id:zone.id+':water',position:[left+1,0,zone.z],yaw:-Math.PI/2});
   parts.push(...chairKit(right,zone.z,0x86b3a5));obstacles.push({x:right,z:zone.z,w:.82,d:.76,seat:true});
   facilities.nap.push({id:zone.id+':rest',position:[right,0,zone.z],yaw:Math.PI});
   const plantZ=zone.z+zone.depth/2-.7;
   parts.push(...plantKit(zone.x,plantZ,.7));obstacles.push({x:zone.x,z:plantZ,w:.6,d:.6});
   // Common zones double as a compact client/reception area when wide enough:
   // a slim stone counter, two visitor stools and a small information screen.
   if(zone.width>=6&&zone.depth>=4){
    const counterZ=zone.z+zone.depth/2-.72,counterX=zone.x;
    if(free(counterX,counterZ,3.2,.72)){
     add([counterX,.64,counterZ],[3.0,1.12,.62],0x56636b,'reception-counter',{surface:'stone'});
     add([counterX,1.23,counterZ-.01],[3.14,.09,.70],0xc8d0cf,'reception-countertop',{surface:'stone'});
     for(const x of[counterX-1.0,counterX+1.0]){
      round([x,.46,counterZ+.66],[.42,.82,.42],0x384751,'visitor-stool','metal');
      round([x,.91,counterZ+.66],[.34,.08,.34],0x718289,'visitor-stool-seat','cloth');
     }
     add([counterX,1.84,counterZ-.31],[1.55,.92,.08],0x2e414b,'reception-screen',{surface:'screen'});
     add([counterX,1.84,counterZ-.265],[1.38,.74,.012],0x20303a,'reception-screen-glass',{surface:'screen'});
     obstacles.push({x:counterX,z:counterZ,w:3.2,d:.82});
    }
   }
  }
  stamp(zone,partStart,obstacleStart);
 }
 const explicit=Array.isArray(config?.desks)?physicalLayout(agents,config!):null;
 const desks:RenderedDesk[]=[];
 const addDesk=(desk:RenderedDesk)=>{
  const zone=floor.zones.find(z=>z.id===desk.zone&&z.kind==='work');if(!zone)return;
  const hue=accent(zone.color),x=desk.x,z=desk.z;
  const partStart=parts.length,obstacleStart=obstacles.length;
  desks.push(desk);parts.push(...deskKit(x,z,hue,0,'department'),...chairKit(x,z+1.02,hue,0,.715,'department'));
  obstacles.push({x,z,w:2.7,d:1.08},{x,z:z+1.02,w:.7,d:.66,seat:true});
  stamp(zone,partStart,obstacleStart);
  const index=agents.findIndex(a=>a.id===desk.rendered_agent_id),agent=agents[index];
  if(!agent||actors.has(agent.id))return;
  // A vacant/reserved desk is furniture only, never an invented idle Agent.
  places.push({id:agent.id,home:[x,0,z+1.02],yaw:Math.PI,role:agent.role,reportsTo:agent.reports_to,peerReception:agent.peer_reception||'sit',reception:[x+(x+1.65>11.8?-1.65:1.65),0,z+1.1]});
  actors.set(agent.id,{agent,index});
 };
 if(explicit){
  for(const desk of explicit.desks)if(desk.floor===floor.floor)addDesk(desk);
  floor.zones.forEach((zone,index)=>{
   const team=[...explicit.teams.values()].find(t=>t.floor===floor.floor&&t.zone_ids.includes(zone.id));if(!team)return;
   const own=desks.filter(d=>d.team_id===team.id),actual=own.filter(d=>d.occupancy==='occupied').length,reserved=own.filter(d=>d.occupancy==='reserved').length;
   const short=(name:string)=>name.length>12?name.slice(0,11)+'…':name;
   zones[index].name=short(zone.name)+(team.name===zone.name?'':' · '+short(team.name));
   Object.assign(zones[index],{detail:zone.name+' · '+team.name+` · 实员 ${actual} / 编制 ${team.headcount} / 桌 ${own.length}`+(reserved?` · ${reserved} 桌预留/未展示成员`:'')});
  });
 }else agents.forEach(agent=>{
  // Backward compatibility: without an explicit physical-desk array, only
  // actual members assigned to this floor produce a workstation.
  const position=agent.office_position;if(!position||position.floor!==floor.floor)return;
  const zone=floor.zones.find(z=>z.id===position.zone&&z.kind==='work');
  // Live members can be edited independently after applying a layout. Do not
  // invent a valid seat or put a desk across a wall for an invalid current input.
  if(!zone||xOutside(position.x,zone.x,zone.width)||!Number.isFinite(position.z)||position.z-.6<zone.z-zone.depth/2-1e-9||position.z+1.45>zone.z+zone.depth/2+1e-9)return;
  if(actors.has(agent.id)||desks.some(d=>Math.abs(d.x-position.x)<2.8-1e-9&&Math.abs(d.z-position.z)<2.05-1e-9))return;
  addDesk({...position,id:'member:'+agent.id,name:agent.name+'的工位',team_id:null,agent_id:agent.id,rendered_agent_id:agent.id,occupancy:'occupied'});
 });
 for(const zone of floor.zones.filter(z=>z.kind==='work'))parts.push(...workstationIslandInlays(desks,zone,accent(zone.color)));
 // Decorative items use remaining zone space, never a hard-coded world corner
 // or a workstation's reception/seat. They participate in path collision tests.
 for(const zone of floor.zones.filter(z=>['work','lounge','pantry'].includes(z.kind))){
  const partStart=parts.length,obstacleStart=obstacles.length;
  if(zone===floor.zones.find(z=>z.kind==='work')&&zone.width>=4){
   const x=zone.x,z=zone.z-zone.depth/2+.16,width=Math.min(3.4,zone.width-1);
   if(free(x,z,width,.28)){
    add([x,1.82,z],[width,1.8,.14],0x354354,'planning-board');
    add([x,1.82,z+.081],[width-.22,1.56,.014],0x515881,'planning-board-surface',{surface:'screen'});
    for(let i=0;i<5;i++)add([x-width*.33+i*width*.145,1.4+(i%3)*.12,z+.096],[width*.09,.4+(i%3)*.24,.012],i%2?0xb3a9e6:0x8b92ce,'planning-board-note');
    obstacles.push({x,z,w:width,d:.28});
   }
  }
  const corners=[[zone.x+zone.width/2-.75,zone.z-zone.depth/2+.75],[zone.x+zone.width/2-.75,zone.z+zone.depth/2-.75],[zone.x-zone.width/2+.75,zone.z+zone.depth/2-.75]];
 const point=corners.find(([x,z])=>zone.width>=3&&zone.depth>=3&&free(x,z,1.3,1.3));
  if(point){parts.push(...plantKit(point[0],point[1],.55));obstacles.push({x:point[0],z:point[1],w:1.3,d:1.3});}
  // Shared technology fixtures add the richer “real office” layer seen in
  // the reference: a compact equipment rack, charging shelf and wall display
  // per sufficiently large work area. They are optional decorations and only
  // claim space after the same collision checks used by navigation.
  if(zone.kind==='work'&&zone.width>=5.5&&zone.depth>=4.5){
   const x=zone.x+zone.width/2-.62,z=zone.z-zone.depth/2+.72;
   if(free(x,z,1.05,.82)){
    add([x,.72,z],[.92,1.35,.68],0x3a4850,'tech-rack',{surface:'metal'});
    add([x,.95,z+.36],[.72,.42,.025],0x172731,'tech-rack-panel',{surface:'screen'});
    for(let i=0;i<4;i++)add([x-.25+i*.17,1.04,z+.375],[.10,.018,.014],i%2?0x6ed5c0:0x77a9e8,'tech-rack-led',{surface:'screen'});
    add([x-.04,1.44,z],[1.02,.07,.78],0x9eabb0,'tech-rack-cap',{surface:'metal'});
    add([x-.02,.18,z],[.88,.12,.62],0x27343c,'tech-rack-plinth',{surface:'rubber'});
    obstacles.push({x,z,w:1.12,d:.9});
  }
   const panelX=zone.x-zone.width/2+.18,panelZ=zone.z+zone.depth/2-.26;
   const panelDepth=Math.min(.42,Math.max(.24,zone.depth-1.1));
   if(free(panelX,panelZ,.18,panelDepth)){
    add([panelX,1.72,panelZ],[.045,1.55,panelDepth],0x485c66,'acoustic-panel',{surface:'cloth'});
    add([panelX+.028,1.72,panelZ],[.012,1.25,Math.max(.18,panelDepth-.08)],0x5f7882,'acoustic-panel-inlay',{surface:'screen',alpha:.65});
   }
  }
  stamp(zone,partStart,obstacleStart);
 }
 // Only old layouts without an explicit restroom keep the historical fallback.
 // Version two never invents a facility that the editable design did not place.
 if(config?.layout_version!==2&&!floor.zones.some(z=>z.kind==='restroom')){
  add([-11.8,1.1,8.65],[1.7,2.2,1.1],0xd5e0e5,'department-restroom');
  obstacles.push({x:-11.8,z:8.65,w:1.7,d:1.1});
  facilities.restroom.push({id:'floor-'+floor.floor+':wc',position:[-12.95,0,8.65],yaw:Math.PI/2});
  zones.push({name:'卫生间',position:[-11.8,2.3,8.65],accent:0x82a8c0});
 }
 zones.push({name:floorTitle(floor),position:[0,2.8,-9.3],accent:0x558e86});
 return{parts,agents:places,actors,facilities,meetingSeats,obstacles,zones,desks,route:(a,b)=>path(a,b,obstacles.filter(o=>!o.seat||!([a,b].some(p=>Math.abs(p[0]-o.x)<o.w/2+.18&&Math.abs(p[2]-o.z)<o.d/2+.18))))};
}
const xOutside=(x:number,center:number,width:number)=>!Number.isFinite(x)||x-1.4<center-width/2-1e-9||x+1.4>center+width/2+1e-9;
