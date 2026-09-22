/** Pure draft operations. No API calls, Agent creation, publication or permissions. */
import {Agent,Json} from '../types';

const clone=<T,>(value:T):T=>JSON.parse(JSON.stringify(value));
const fail=(message:string):never=>{throw Error(message);};
const ident=(items:Json[],prefix:string)=>{let n=1;while(items.some(x=>x.id===prefix+n))n++;return prefix+n;};
const inside=(d:Json,z:Json)=>d.x-1.4>=z.x-z.width/2-1e-8&&d.x+1.4<=z.x+z.width/2+1e-8&&d.z-.6>=z.z-z.depth/2-1e-8&&d.z+1.45<=z.z+z.depth/2+1e-8;
const overlap=(a:Json,b:Json)=>a.floor===b.floor&&Math.abs(a.x-b.x)<2.8-1e-8&&Math.abs(a.z-b.z)<2.05-1e-8;
export const zoneTypes:Record<string,string>={work:'团队业务区',meeting:'会议室',restroom:'卫生间',pantry:'饮水间',lounge:'休息区',common:'其他公共区'};
export const requiredFacilities=['meeting','restroom','pantry'];
export const lastRequiredFacility=(value:Json,floor:Json,zone:Json)=>value.layout_version===2&&requiredFacilities.includes(zone.kind)&&floor.zones.filter((z:Json)=>z.kind===zone.kind).length===1;

export function reconcileDraft(value:Json):Json{
 const next=clone(value);next.teams=next.teams||[];next.desks=next.desks||[];
 next.positions=Object.fromEntries(next.desks.filter((d:Json)=>d.agent_id).map((d:Json)=>[d.agent_id,{x:d.x,z:d.z,floor:d.floor,zone:d.zone}]));
 next.teams=next.teams.map((t:Json)=>({...t,member_ids:next.desks.filter((d:Json)=>d.team_id===t.id&&d.agent_id).map((d:Json)=>d.agent_id)}));
 return next;
}

export function layoutProblem(value:Json,agents?:Agent[]):string{
 const floors:Json[]=value.floors||[],teams:Json[]=value.teams||[],desks:Json[]=value.desks||[];
 const fs=new Set(),deps=new Set(),zs=new Set(),ts=new Set(),ds=new Set(),members=new Set(),owners=new Set();
 if(!floors.length)return teams.length||desks.length||Object.keys(value.positions||{}).length?'没有楼层时不能保留团队、桌子或员工位置。':'';
 if(teams.length>100)return '最多支持 100 个办公团队。';
 if(floors.length>10||desks.length>300)return '最多支持 10 个楼层、300 张办公桌。';
 if(value.layout_version!==undefined&&![1,2].includes(value.layout_version))return '不支持此布局规则版本。';
 for(const f of floors){
  if(!Number.isInteger(f.floor)||f.floor<1||f.floor>10||fs.has(f.floor))return '楼层编号必须为 1–10 且不重复。';fs.add(f.floor);
  if(!f.name?.trim()||!f.department?.trim()||f.department!==f.department.trim())return '请填写楼层名称与部门，不要留首尾空白。';
  if(deps.has(f.department.toLowerCase()))return '一个楼层一个部门，同一部门不能重复占用楼层。';deps.add(f.department.toLowerCase());
  if(!Array.isArray(f.zones)||!f.zones.length||f.zones.length>20||!f.zones.some((z:Json)=>z.kind==='work'))return '每层需要 1–20 个功能区，至少一个工作区。';
  if(value.layout_version===2){const missing=requiredFacilities.filter(kind=>!f.zones.some((z:Json)=>z.kind===kind));if(missing.length)return `${f.department}缺少必需公共配套：${missing.map(kind=>zoneTypes[kind]).join('、')}。可以调整位置，但必须保留。`;}
  for(const z of f.zones){
   if(!/^[a-zA-Z][a-zA-Z0-9_-]{0,47}$/.test(z.id||'')||zs.has(z.id))return '功能区标识必须有效且全局唯一。';zs.add(z.id);
   if(!z.name?.trim()||!zoneTypes[z.kind])return '功能区需要名称和有效用途。';
   if(!['x','z','width','depth'].every(k=>Number.isFinite(z[k]))||z.width<=0||z.depth<=0||Math.abs(z.x)+z.width/2>12+1e-8||Math.abs(z.z)+z.depth/2>8+1e-8)return '功能区超出楼层边界，请缩小或移动。';
   if(value.layout_version===2&&(['meeting','restroom','pantry','lounge'].includes(z.kind))&&(z.width<(z.kind==='meeting'?4:3)||z.depth<3))return `${z.name}空间不足：${z.kind==='meeting'?'会议室至少 4 × 3':'公共配套至少 3 × 3'}，请保留可用空间。`;
   if(z.color&&!/^#[0-9a-fA-F]{6}$/.test(z.color))return '区域颜色必须为 #RRGGBB。';
  }
  if(f.zones.some((z:Json,i:number)=>f.zones.slice(i+1).some((b:Json)=>Math.abs(z.x-b.x)<(z.width+b.width)/2-1e-8&&Math.abs(z.z-b.z)<(z.depth+b.depth)/2-1e-8)))return '同层功能区不能相互重叠。';
 }
 for(const t of teams){
  if(!/^[a-zA-Z][a-zA-Z0-9_-]{0,63}$/.test(t.id||'')||ts.has(t.id)||!t.name?.trim())return '团队标识需唯一，团队名称不能为空。';ts.add(t.id);
  const f=floors.find(f=>f.floor===t.floor);
  if(!f||!t.zone_ids?.length||t.zone_ids.some((id:string)=>!f.zones.some((z:Json)=>z.id===id&&z.kind==='work')))return '每个团队需要选择本层工作区。';
  for(const id of t.zone_ids){if(owners.has(id))return '不同团队不能重复占用同一工作区。';owners.add(id);}
  const occupied=desks.filter(d=>d.team_id===t.id&&d.agent_id),capacity=desks.filter(d=>d.team_id===t.id).length;
  if(!Number.isInteger(t.headcount)||t.headcount<occupied.length||t.headcount>capacity||t.headcount>300)return `${t.name}：实际员工数 ≤ 员工编制 ≤ 办公桌数量。`;
  if(!Array.isArray(t.member_ids)||JSON.stringify([...t.member_ids].sort())!==JSON.stringify(occupied.map(d=>d.agent_id).sort()))return '团队员工名单必须与本团队已占用办公桌一致。';
 }
 for(const d of desks){
  if(!/^[a-zA-Z][a-zA-Z0-9_-]{0,63}$/.test(d.id||'')||ds.has(d.id)||!d.name?.trim())return '办公桌需要有效唯一标识和名称。';ds.add(d.id);
  const z=floors.find(f=>f.floor===d.floor)?.zones.find((z:Json)=>z.id===d.zone&&z.kind==='work'),t=teams.find(t=>t.id===d.team_id);
  if(!z||!Number.isFinite(d.x)||!Number.isFinite(d.z)||!inside(d,z))return '完整桌椅必须位于本层工作区内。';
  if(d.team_id&&(!t||t.floor!==d.floor||!t.zone_ids.includes(d.zone)))return '桌子必须位于所属团队的本层工作区。';
  if(d.agent_id){
   if(!t||members.has(d.agent_id))return '每位员工只能分配给一个团队的一张办公桌。';members.add(d.agent_id);
   if(agents&&!agents.some(a=>a.id===d.agent_id&&a.enabled!==false))return '办公桌包含不存在或停用的员工。';
  }
 }
 if(desks.some((d,i)=>desks.slice(i+1).some(b=>overlap(d,b))))return '办公桌或椅子相互重叠，请调整位置。';
 const occupied=desks.filter(d=>d.agent_id),positions=value.positions||{};
 if(Object.keys(positions).length!==occupied.length||occupied.some(d=>!positions[d.agent_id]||['floor','zone','x','z'].some(k=>positions[d.agent_id][k]!==d[k])))return '员工位置必须与已占用的办公桌精确一致。';
 return '';
}

function availableSeat(value:Json,team:Json):Json|null{
 const floor=value.floors.find((f:Json)=>f.floor===team.floor);
 for(const zone of floor.zones.filter((z:Json)=>team.zone_ids.includes(z.id)&&z.kind==='work')){
  for(let z=zone.z-zone.depth/2+.7;z<=zone.z+zone.depth/2-1.45+1e-8;z+=2.5)
   for(let x=zone.x-zone.width/2+1.5;x<=zone.x+zone.width/2-1.4+1e-8;x+=3.1){
    const d={x:Math.round(x*100)/100,z:Math.round(z*100)/100,floor:team.floor,zone:zone.id};
    if(inside(d,zone)&&!value.desks.some((other:Json)=>overlap(d,other)))return d;
   }
 }
 return null;
}

export function upgradeLayout(value:Json):Json{
 if(Array.isArray(value.desks))return reconcileDraft(value);
 if(!value.floors?.length)fail('请先生成或配置部门楼层，再启用团队与独立工位管理。');
 const next={...clone(value),teams:[] as Json[],desks:[] as Json[]};
 for(const floor of (next as Json).floors||[]){
  const entries=Object.entries(value.positions||{}).filter(([,p]:[string,any])=>p.floor===floor.floor);
  const team={id:'team_floor_'+floor.floor,name:floor.department+'团队',floor:floor.floor,zone_ids:floor.zones.filter((z:Json)=>z.kind==='work').map((z:Json)=>z.id),headcount:entries.length,member_ids:entries.map(([id])=>id)};
  next.teams.push(team);
  for(const[id,p]of entries)next.desks.push({...(p as Json),id:'desk_'+(next.desks.length+1),name:'办公桌 '+(next.desks.length+1),team_id:team.id,agent_id:id});
 }
 return reconcileDraft(next);
}

export function addFloor(value:Json):Json{
 const next=reconcileDraft(value),floor=Array.from({length:10},(_,i)=>i+1).find(n=>!next.floors.some((f:Json)=>f.floor===n));if(!floor)fail('最多支持 10 个楼层。');
 let department='新部门 '+floor;while(next.floors.some((f:Json)=>f.department===department))department+='（新）';
 const allZones=next.floors.flatMap((f:Json)=>f.zones),zone=(suffix:string,name:string,kind:string,x:number,z:number,width:number,depth:number,color:string)=>{const id=ident(allZones,`f${floor}_${suffix}_`);const result={id,name,kind,x,z,width,depth,color};allZones.push(result);return result;};
 next.floors.push({floor,name:department,department,zones:[
  zone('work_a','团队 A 业务区','work',-5.75,-2.25,10.5,9.5,'#7296ab'),
  zone('work_b','团队 B 业务区','work',5.75,-2.25,10.5,9.5,'#83a184'),
  zone('meeting','协作会议室','meeting',6.75,5.25,8.5,4.5,'#969fcf'),
  zone('restroom','卫生间','restroom',-9.25,5.25,3.5,4.5,'#82a8c0'),
  zone('pantry','饮水间','pantry',-5.25,5.25,3.5,4.5,'#79b0cc'),
  zone('lounge','休息区','lounge',-.75,5.25,4.5,4.5,'#86b3a5')]});
 return next;
}

/** New proposals retain shared facilities even when a department is crowded. */
export function departmentProposal(agents:Agent[]):Json{
 const groups=new Map<string,Agent[]>();
 for(const agent of agents.filter(a=>a.enabled!==false)){const name=agent.department?.trim()||'待分配部门',key=[...groups.keys()].find(k=>k.toLowerCase()===name.toLowerCase())||name;groups.set(key,[...(groups.get(key)||[]),agent]);}
 if(groups.size>10)fail('最多支持 10 个部门楼层，请先整理部门。');
 if(!groups.size)groups.set('待分配部门',[]);
 let value:Json={layout_version:2,floors:[],teams:[],desks:[],positions:{},description:'按现有部门生成的草稿；一层一部门，公共配套共享，成员不复制、不自动授权。'};
 for(const[department,members]of groups){
  value=addFloor(value);const floor=value.floors.at(-1);floor.department=department;floor.name=department+'办公层';
  const id='team_floor_'+floor.floor;value.teams.push({id,name:department+'团队',floor:floor.floor,zone_ids:floor.zones.filter((z:Json)=>z.kind==='work').map((z:Json)=>z.id),headcount:0,member_ids:[]});
  try{value=setTeamCapacity(value,id,members.length,members.length);}catch{fail(`${department}超过本层可容纳的工位数；请调整区域或组织编制，不能删除必需配套或遗漏成员。`);}
  for(const member of members)value=assignEmployee(value,id,member.id);
 }
 return value;
}

export function removeZone(value:Json,floorId:number,zoneId:string):Json{
 const next=reconcileDraft(value),floor=next.floors.find((f:Json)=>f.floor===floorId),zone=floor?.zones.find((z:Json)=>z.id===zoneId);if(!zone)fail('功能区不存在。');
 if(lastRequiredFacility(next,floor,zone))fail(`本层必须保留${zoneTypes[zone.kind]}，可以修改位置与尺寸。`);
 if(next.desks.some((d:Json)=>d.zone===zoneId)||next.teams.some((t:Json)=>t.zone_ids.includes(zoneId)))fail('请先迁移区域内的团队与办公桌。');
 floor.zones=floor.zones.filter((z:Json)=>z.id!==zoneId);const problem=layoutProblem(next);if(problem)fail(problem);return next;
}

export function addTeam(value:Json,floor:number):Json{
 const next=reconcileDraft(value),f=next.floors.find((f:Json)=>f.floor===floor);if(!f)fail('楼层不存在。');
 const zone=f.zones.find((z:Json)=>z.kind==='work'&&!next.teams.some((t:Json)=>t.zone_ids.includes(z.id)));
 if(!zone)fail('请先增加或拆分空闲工作区，再建立新团队。');
 next.teams.push({id:ident(next.teams,'team_'),name:'新团队 '+(next.teams.length+1),floor,zone_ids:[zone.id],headcount:0,member_ids:[]});return next;
}

export function setDeskCount(value:Json,teamId:string,count:number):Json{
 const next=reconcileDraft(value),team=next.teams.find((t:Json)=>t.id===teamId);if(!team)fail('请选择团队。');
 if(!Number.isInteger(count)||count<0||count>300)fail('办公桌数量应为 0–300 的整数。');
 if(count<team.headcount)fail('请先降低员工编制，不能删除已配置的员工容量。');
 let owned=next.desks.filter((d:Json)=>d.team_id===teamId);
 if(count<owned.filter((d:Json)=>d.agent_id).length)fail('不能删除已占用的办公桌，请先调出员工。');
 while(owned.length>count){const last=[...owned].reverse().find((d:Json)=>!d.agent_id);next.desks=next.desks.filter((d:Json)=>d.id!==last.id);owned=next.desks.filter((d:Json)=>d.team_id===teamId);}
 while(owned.length<count){
  if(next.desks.length>=300)fail('整份布局最多 300 张办公桌。');
  const seat=availableSeat(next,team);if(!seat)fail('团队区域空间不足，未更改桌子数量；请扩大工作区。');
  const id=ident(next.desks,'desk_'),desk={...seat,id,name:'办公桌 '+id.replace('desk_',''),team_id:teamId,agent_id:null};next.desks.push(desk);owned.push(desk);
 }
 return reconcileDraft(next);
}

export function assignEmployee(value:Json,teamId:string,agentId:string,deskId?:string):Json{
 const next=reconcileDraft(value),team=next.teams.find((t:Json)=>t.id===teamId);if(!team)fail('请选择团队。');
 const old=next.desks.find((d:Json)=>d.agent_id===agentId);
 if(!old||old.team_id!==teamId){if(team.member_ids.length>=team.headcount)fail('团队员工编制已满，请先增加编制和工位。');}
 const desk=next.desks.find((d:Json)=>d.team_id===teamId&&(!deskId||d.id===deskId)&&(!d.agent_id||d.agent_id===agentId));
 if(!desk)fail('没有可用空桌，不能覆盖其他员工。');
 if(old)old.agent_id=null;desk.agent_id=agentId;return reconcileDraft(next);
}

export function setTeamCapacity(value:Json,teamId:string,desks:number,headcount:number):Json{
 const next=reconcileDraft(value),team=next.teams.find((t:Json)=>t.id===teamId);if(!team)fail('请选择团队。');
 if(!Number.isInteger(headcount)||headcount<team.member_ids.length||headcount>desks)fail('请满足：实际员工数 ≤ 员工编制 ≤ 办公桌数量。');
 team.headcount=headcount;return setDeskCount(next,teamId,desks);
}

export function editZone(value:Json,floorId:number,zoneId:string,patch:Json,moveDesks=false):Json{
 const next=reconcileDraft(value),floor=next.floors.find((f:Json)=>f.floor===floorId),zone=floor?.zones.find((z:Json)=>z.id===zoneId);if(!zone)fail('功能区不存在。');
 const dx=(patch.x??zone.x)-zone.x,dz=(patch.z??zone.z)-zone.z;Object.assign(zone,patch);
 if(moveDesks)for(const desk of next.desks.filter((d:Json)=>d.floor===floorId&&d.zone===zoneId)){desk.x=Math.round((desk.x+dx)*100)/100;desk.z=Math.round((desk.z+dz)*100)/100;}
 const result=reconcileDraft(next),problem=layoutProblem(result);if(problem)fail(problem);return result;
}

export function splitZone(value:Json,floorId:number,zoneId:string):Json{
 const next=reconcileDraft(value),f=next.floors.find((f:Json)=>f.floor===floorId),z=f?.zones.find((z:Json)=>z.id===zoneId);if(!z||z.kind!=='work')fail('只能拆分工作区。');
 if(next.desks.some((d:Json)=>d.zone===zoneId)||f.zones.length>=20)fail('拆分前请移走该区域的全部桌子；每层最多 20 个区域。');
 if(z.width<6.2)fail('工作区过窄，无法拆为可放置桌椅的两个区域。');
 const original={...z},allZones=next.floors.flatMap((f:Json)=>f.zones),id=ident(allZones,'zone_');
 z.width=(original.width-.4)/2;z.x=original.x-(original.width+.4)/4;
 f.zones.push({...original,id,name:original.name+' · 新区',width:z.width,x:original.x+(original.width+.4)/4});
 return next; // New half is unassigned, ready for a different team.
}

export function addZone(value:Json,floorId:number,kind:string):Json{
 const next=reconcileDraft(value),f=next.floors.find((f:Json)=>f.floor===floorId);if(!f||f.zones.length>=20)fail('每层最多 20 个功能区。');
 const all=next.floors.flatMap((f:Json)=>f.zones);
 for(let z=-6;z<=6;z+=.5)for(let x=-10;x<=10;x+=.5){
  if(!f.zones.some((b:Json)=>Math.abs(x-b.x)<(4+b.width)/2+.1&&Math.abs(z-b.z)<(4+b.depth)/2+.1)){
   f.zones.push({id:ident(all,'zone_'),name:'新'+zoneTypes[kind],kind,x,z,width:4,depth:4,color:'#83a184'});return next;
  }
 }
 return fail('楼层没有足够空白区域。请先缩小或拆分现有工作区。');
}

export function moveDesk(value:Json,id:string,x:number,z:number):Json{
 const next=reconcileDraft(value),d=next.desks.find((d:Json)=>d.id===id);if(!d)fail('办公桌不存在。');
 const team=next.teams.find((t:Json)=>t.id===d.team_id),zone=next.floors.find((f:Json)=>f.floor===d.floor)?.zones.find((a:Json)=>a.kind==='work'&&(!team||team.zone_ids.includes(a.id))&&inside({x,z},a));
 if(!zone)fail('请把完整桌椅放入所属团队的工作区。');Object.assign(d,{x,z,zone:zone.id});
 const result=reconcileDraft(next),problem=layoutProblem(result);if(problem)fail(problem);return result;
}
