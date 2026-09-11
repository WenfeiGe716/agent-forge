import {Agent,OrganizationFloor,OrganizationConfig,OrganizationDesk} from '../types';
import {makeDepartmentOffice} from './departmentOffice';
import {Vec,World,AgentPlace,ActorView,Slot} from '../office/behavior';
import {Part,color,segment,bevel} from './officeRenderer';
import {chairKit,deskKit,plantKit} from './furniture';
import {detailedActor} from './characters';
export const palette:Record<string,number>={planner:0x9c8cde,developer:0x54b2cc,tester:0xe0b65d,reviewer:0xda91b0};
type Rect={x:number;z:number;w:number;d:number;seat?:boolean};
export type RenderedDesk=OrganizationDesk&{rendered_agent_id:string|null;occupancy:'occupied'|'vacant'|'reserved'};
export type Layout=World&{parts:Part[];actors:Map<string,{agent:Agent;index:number}>;zones:{name:string;detail?:string;position:Vec;accent:number}[];obstacles:Rect[];desks?:RenderedDesk[]};
const deskSeats=[[-8.4,-6.1,'planner'],[-5.8,-2.8,'developer'],[-2.3,-2.8,'developer'],[-5.8,.1,'developer'],[-2.3,.1,'developer'],[3,-5.6,'tester'],[6.2,-5.6,'tester'],[9.4,-5.6,'tester'],[4.5,-.5,'reviewer'],[8.1,-.5,'reviewer'],[-4.5,-6.7,'planner'],[-.6,-6.7,'developer'],[11,3.0,'reviewer'],[-10,1.4,'developer']] as [number,number,string][];
export function findPath(start:Vec,end:Vec,obstacles:Rect[]):Vec[]{
 if(Math.hypot(start[0]-end[0],start[2]-end[2])<.03)return[copy(end)];
 const minX=-14,minZ=-10,step=.4,w=72,h=Math.max(54,Math.ceil((Math.max(start[2],end[2])+12)/step));
 const id=(p:Vec)=>Math.max(0,Math.min(h-1,Math.round((p[2]-minZ)/step)))*w+Math.max(0,Math.min(w-1,Math.round((p[0]-minX)/step)));
 const point=(i:number):Vec=>[minX+i%w*step,0,minZ+Math.floor(i/w)*step];const src=id(start),dest=id(end);
 const blocked=(i:number)=>{if(i===src||i===dest)return false;const p=point(i);return obstacles.some(o=>Math.abs(p[0]-o.x)<o.w/2+.28&&Math.abs(p[2]-o.z)<o.d/2+.28);};
 const open=[src],closed=new Set<number>(),g=new Map([[src,0]]),came=new Map<number,number>();const heuristic=(i:number)=>Math.abs(i%w-dest%w)+Math.abs(Math.floor(i/w)-Math.floor(dest/w));
 while(open.length){open.sort((a,b)=>(g.get(a)!+heuristic(a))-(g.get(b)!+heuristic(b))||a-b);const cur=open.shift()!;if(cur===dest){const points:Vec[]=[copy(end)];let c=dest;while(c!==src){points.push(point(c));c=came.get(c)!;}points.reverse();const out:Vec[]=[];for(let i=0;i<points.length;i++){const a=out.at(-1),b=points[i],c=points[i+1];if(a&&c&&Math.abs((b[0]-a[0])*(c[2]-b[2])-(b[2]-a[2])*(c[0]-b[0]))<1e-7)continue;out.push(b);}return out;}closed.add(cur);const x=cur%w,z=Math.floor(cur/w);for(const[dx,dz]of[[-1,0],[0,-1],[0,1],[1,0]]){const a=x+dx,b=z+dz;if(a<0||a>=w||b<0||b>=h)continue;const n=b*w+a;if(closed.has(n)||blocked(n))continue;const d=g.get(cur)!+1;if(d<(g.get(n)??Infinity)){g.set(n,d);came.set(n,cur);if(!open.includes(n))open.push(n);}}}return[];
}
const copy=(v:Vec):Vec=>[...v];
export function makeOffice(agents:Agent[],floor?:OrganizationFloor,config?:OrganizationConfig):Layout{
 if(floor)return makeDepartmentOffice(agents,floor,findPath,config);
 const parts:Part[]=[],obstacles:Rect[]=[],places:AgentPlace[]=[],actors=new Map<string,{agent:Agent;index:number}>(),free=[...deskSeats];
 const add=(p:Vec,s:Vec,h:number,mesh='cube',yaw=0,alpha=1)=>parts.push(mesh==='round'?bevel(p,s,h,Math.min(...s)*.24,{yaw,alpha}):{p,s,color:color(h),mesh,yaw,alpha});
 const extension=Math.max(0,Math.ceil((agents.length-14)/7))*2.8;
 add([0,-.37,extension/2],[27,.7,20+extension],0xd3d5ce,'round');add([0,0,extension/2],[26.85,.11,19.85+extension],0xf0eee7);
 for(let x=-12;x<=12;x+=1.5)add([x,.062,extension/2],[.012,.005,19.8+extension],0xdedcd4);for(let z=-9;z<=9+extension;z+=1.5)add([0,.063,z],[26.7,.005,.012],0xdedcd4);
 add([0,.75,-9.7],[26.8,1.5,.17],0xebece6);add([-13.15,.75,-5],[.16,1.5,9.5],0xebece6);
 for(let i=0;i<7;i++){const x=-11.2+i*3.75;add([x,1.95,-9.7],[3.3,1.2,.14],0x9eafb9);add([x,1.95,-9.61],[3.13,1.07,.04],0xc7e1e8);add([x,1.95,-9.56],[.055,1.10,.025],0xffffff);}
 const zones:Layout['zones']=[];for(const[name,x,z,w,d,h]of[['规划与架构',-7.8,-6,7.6,4.9,palette.planner],['开发工位',-3.9,-1.2,7.9,5.9,palette.developer],['测试与质量',6.1,-5.6,10,3.3,palette.tester],['代码审查',6.2,-.4,8.8,3.9,palette.reviewer],['协作会议室',4.5,5.0,9,5.1,0x969fcf],['休息区',-7.5,5.8,5.8,4.3,0x86b3a5]] as [string,number,number,number,number,number][]){add([x,.08,z],[w,.024,d],h,'round');add([x,.101,z],[w-.22,.02,d-.22],h,'round');const tint=['协作会议室','休息区'].includes(name)?.12:.20,ivory=color(0xf3f0e9);Object.assign(parts.at(-1)!,{color:color(h).map((channel,i)=>channel*tint+ivory[i]*(1-tint)),surface:'cloth',roughness:.96});zones.push({name,position:[x,.18,z-d/2+.17],accent:h});}
 const plant=(x:number,z:number,scale=1)=>parts.push(...plantKit(x,z,scale));
 for(const[x,z,s]of[[-12,-8.4,1],[12,-8.4,1],[.8,-4,.85],[12,8,1],[-11.7,8,1],[-3.9,6.8,.85]])plant(x,z,s);
 add([-8.5,1.82,-8.65],[3.4,1.8,.14],0x354354,'round');add([-8.5,1.82,-8.55],[3.17,1.56,.035],0x515881);for(let i=0;i<5;i++)add([-9.65+i*.49,1.4+(i%3)*.12,-8.52],[.31,.4+(i%3)*.24,.02],i%2?0xb3a9e6:0x8b92ce);
 const used:[number,number,string][]=[];
 agents.forEach((agent,i)=>{let idx=free.findIndex(s=>s[2]===agent.role);if(idx<0)idx=0;const defaultSeat=free.splice(idx,1)[0]||[-10+(i%7)*3.1,8.5+Math.floor((i-14)/7)*2.8,agent.role] as [number,number,string];const position=agent.office_position;const seat:[number,number,string]=position?[position.x,position.z,agent.role]:defaultSeat;used.push(seat);places.push({id:agent.id,home:[seat[0],0,seat[1]+1.02],yaw:Math.PI,role:agent.role,reportsTo:agent.reports_to,peerReception:agent.peer_reception||'sit',reception:[seat[0]+1.65,0,seat[1]+1.1]});actors.set(agent.id,{agent,index:i});});
 for(let i=free.length-1;i>=0;i--)if(used.some(s=>Math.abs(s[0]-free[i][0])<2.9&&Math.abs(s[1]-free[i][1])<2))free.splice(i,1);
 for(const[x,z,role]of[...used,...free]){const hue=palette[role]||palette.developer;parts.push(...deskKit(x,z,hue),...chairKit(x,z+1.02,hue));obstacles.push({x,z,w:2.7,d:1.08});obstacles.push({x,z:z+1.02,w:.7,d:.66,seat:true});}
 // Meetings are furniture; no automatic synthetic meeting is emitted.
 parts.push(bevel([4.4,.96,5],[6.4,.15,1.9],0xc8ad89,.06,{surface:'wood',tag:'meeting-table'}));for(const x of[2.1,6.7])add([x,.47,5],[.16,.9,1.14],0x82949f);obstacles.push({x:4.4,z:5,w:6.7,d:2.1});
 for(const x of[2.2,4.4,6.6])for(const z of[3.45,6.55])parts.push(...chairKit(x,z,0x9899bc,z<5?Math.PI:0,.82));
 for(const x of[-.15,9]){add([x,1.26,5],[.04,2.5,4.8],0xbad7df,'cube',0,.19);for(const z of[2.6,7.4])add([x,1.25,z],[.055,2.5,.055],0x829ba8);add([x,2.5,5],[.065,.07,4.85],0xa6b7c1);obstacles.push({x,z:5,w:.12,d:4.8});}
 add([4.4,1.76,2.5],[3.7,1.95,.14],0x344458,'round');add([4.4,1.76,2.59],[3.45,1.73,.025],0x535e85);for(let i=0;i<3;i++)add([3.35+i*1.07,1.7,2.61],[.78,.63,.018],i%2?0x8fc1c1:0xa8a6d6,'round');
 // Restroom: only a door and occupancy indicator are visible. No intimate animation.
 add([-11.5,1.2,-2.1],[2.25,2.4,3.5],0xd5e0e5,'round');add([-11.5,1.15,-.33],[1.35,2.23,.065],0x788eaa,'round');add([-11.5,2.45,-.30],[.72,.40,.08],0xc1d9e0,'round');add([-10.99,1.12,-.275],[.045,.22,.09],0xdde6e9);obstacles.push({x:-11.5,z:-2.1,w:2.3,d:3.5});zones.push({name:'卫生间',position:[-11.5,2.8,-.15],accent:0x82a8c0});
 // Water cooler and two distinct service positions.
 add([-11.7,.62,3.3],[.84,1.26,.84],0xd8e2e4,'round');add([-11.7,1.60,3.3],[.53,.72,.53],0x89c0d1,'cylinder');add([-11.23,.86,3.3],[.065,.45,.5],0x758c9c);add([-11.17,.9,3.2],[.08,.07,.07],0x649acc);add([-11.17,.9,3.42],[.08,.07,.07],0xd48389);zones.push({name:'茶水间',position:[-11.5,2.22,3.3],accent:0x79b0cc});obstacles.push({x:-11.7,z:3.3,w:1,d:1});
 add([-8.6,.63,5.85],[1.4,.53,3.0],0x78a899,'round');add([-9.15,1.0,5.85],[.30,.95,3.1],0x8fb9aa,'round');for(const z of[4.5,7.2])add([-8.6,.82,z],[1.45,.7,.24],0x8bb5a5,'round');for(const z of[5.25,6.45])add([-8.45,.92,z],[.93,.16,1.08],0xa4cbbb,'round');add([-6.35,.65,5.85],[1.75,.13,1.75],0xdfc4a2,'cylinder');add([-6.35,.32,5.85],[.62,.62,.62],0xacbdb3,'cylinder');obstacles.push({x:-6.35,z:5.85,w:1.9,d:1.9});
 plant(-1.05,2.7,1.28);add([-1.05,.19,2.7],[2.3,.22,2.3],0xccb997,'cylinder');obstacles.push({x:-1.05,z:2.7,w:2.35,d:2.35});
 const facilities:World['facilities']={drink:[{id:'water:0',position:[-10.65,0,2.96],yaw:-Math.PI/2},{id:'water:1',position:[-10.65,0,3.73],yaw:-Math.PI/2}],nap:[{id:'sofa:0',position:[-8.35,0,5.15],yaw:Math.PI/2},{id:'sofa:1',position:[-8.35,0,6.45],yaw:Math.PI/2}],restroom:[{id:'wc:0',position:[-11.5,0,.4],yaw:Math.PI}]};
 for(const q of parts){if(q.p[0]<-7&&q.p[0]>-10&&q.p[2]>4&&q.p[1]>.3)q.surface=q.surface||'cloth';}
 return{parts,agents:places,actors,facilities,obstacles,zones,route:(a,b)=>findPath(a,b,obstacles.filter(o=>!o.seat||!([a,b].some(p=>Math.abs(p[0]-o.x)<o.w/2+.18&&Math.abs(p[2]-o.z)<o.d/2+.18))))};
}
export function drawActor(actor:ActorView,index:number,hue:number,selected=false):Part[]{return detailedActor(actor,index,hue,selected);}
