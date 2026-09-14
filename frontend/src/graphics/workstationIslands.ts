/** Floor inlays group existing desks; they never allocate a seat or block a path. */
import type {OrganizationZone} from '../types';
import type {RenderedDesk} from './officeWorld';
import {Part,color} from './officeRenderer';

export function workstationIslandInlays(desks:RenderedDesk[],zone:OrganizationZone,accent:number):Part[]{
 const rows:RenderedDesk[][]=[];
 // Only nearby, aligned desks of the same team share a visual base. Leave
 // deliberate aisles and other teams separate; no generated layout is saved.
 for(const desk of desks.filter(d=>d.zone===zone.id).sort((a,b)=>a.z-b.z||a.x-b.x)){
  const row=rows.find(row=>row[0].team_id===desk.team_id&&Math.abs(row[0].z-desk.z)<.01&&Math.abs(desk.x-row.at(-1)!.x)<=3.6);
  if(row)row.push(desk);else rows.push([desk]);
 }
 return rows.flatMap(row=>{
  const left=Math.min(...row.map(d=>d.x))-1.38,right=Math.max(...row.map(d=>d.x))+1.38;
  const back=Math.min(...row.map(d=>d.z))-.56,front=Math.max(...row.map(d=>d.z))+1.41;
  // Applied desks have already passed physical validation. Remain defensive
  // here too so presentation cannot paint beyond an edited zone's footprint.
  if(left<zone.x-zone.width/2||right>zone.x+zone.width/2||back<zone.z-zone.depth/2||front>zone.z+zone.depth/2)return[];
  const x=(left+right)/2,z=(back+front)/2,width=right-left,depth=front-back;
  // Work zones are soft textile islands, not coloured bounding boxes.  Keep a
  // tiny neutral value difference between rows so the floor remains legible
  // without the former blue/pink outlines.
  const white=color(0xf4f2eb),tint=color(0xd9d7cf) as Part['color'];
  // These millimetre-thin markings sit just above the existing carpet and
  // below furniture contact points. Do not add them to navigation obstacles.
  return[
   {p:[x,.113,z],s:[width,.002,depth],color:white,mesh:'cube',tag:'workstation-island-base',zoneId:zone.id,castShadow:false,surface:'matte'},
   {p:[x,.116,z],s:[width-.12,.002,depth-.12],color:tint,mesh:'cube',tag:'workstation-island-inlay',zoneId:zone.id,castShadow:false,surface:'matte'},
  ] as Part[];
 });
}
