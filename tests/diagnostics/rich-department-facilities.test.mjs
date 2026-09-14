import assert from 'node:assert/strict';
import test from 'node:test';
import {makeOffice} from '../../frontend/dist/graphics/officeWorld.js';
import {modelMeshes,bevelBox} from '../../frontend/dist/graphics/visualMeshes.js';
import {cubeVertices,modelMatrix,transform} from '../../frontend/dist/graphics/scene.js';

const zones=[
 ['arch','代码规划与架构','work',-7.6,-5.75,8,3.5],['test','测试质量','work',6.5,-5.75,10,3.5],
 ['dev','开发工位','work',-3.8,-.25,8.6,6],['requirements','需求分析','work',6.5,-.7,10,5.9],
 ['restroom','卫生间','restroom',-10.25,-1.5,3.5,3],['pantry','饮水间','pantry',-10.25,2,3.5,3],
 ['lounge','休息区','lounge',-7.25,5.75,8.5,3.5],['meeting','协作会议室','meeting',5.25,5.25,11.5,4.5],
].map(([id,name,kind,x,z,width,depth])=>({id,name,kind,x,z,width,depth}));
const floor={floor:1,name:'开发团队',department:'研发部',zones};
const coordinates=[['arch',-9.9,-6.3],['arch',-6.6,-6.3],['dev',-6.5,-2.2],['dev',-3.3,-2.2],['dev',-6.5,.5],['dev',-3.3,.5],['test',3.1,-6.3],['test',6.4,-6.3],['test',9.7,-6.3],['requirements',3.1,-2.6],['requirements',6.4,-2.6],['requirements',9.7,-2.6]];
const agents=['Atlas','Nova','Echo','Sage'].map((id,i)=>({id,name:id,role:['planner','developer','tester','reviewer'][i]}));
const desks=coordinates.map(([zone,x,z],i)=>({id:'desk_'+i,name:'桌 '+i,floor:1,zone,x,z,team_id:'development',agent_id:({0:'Atlas',2:'Nova',6:'Echo',7:'Sage'})[i]||null}));
const config={layout_version:2,positions:{},floors:[floor],teams:[{id:'development',name:'开发团队',floor:1,zone_ids:['arch','dev','test','requirements'],headcount:4,member_ids:agents.map(a=>a.id)}],desks};
const meshes=modelMeshes();meshes.set('cube',cubeVertices());
function assertFootprints(world,definitions){
 for(const p of world.parts.filter(p=>p.zoneId)){
  const zone=definitions.find(z=>z.id===p.zoneId);assert.ok(zone,p.zoneId);
  const vertices=p.mesh?.startsWith('bevel:')?bevelBox(...p.mesh.slice(6).split(':').map(Number)):meshes.get(p.mesh||'cube');
  assert.ok(vertices,p.mesh);const matrix=p.matrix||modelMatrix(p.p,p.s,p.yaw);
  for(let i=0;i<vertices.length;i+=6){const point=transform(matrix,[vertices[i],vertices[i+1],vertices[i+2]]);
   assert.ok(point[0]>=zone.x-zone.width/2-1e-4&&point[0]<=zone.x+zone.width/2+1e-4,`${zone.id} ${p.tag} x=${point[0]}`);
   assert.ok(point[2]>=zone.z-zone.depth/2-1e-4&&point[2]<=zone.z+zone.depth/2+1e-4,`${zone.id} ${p.tag} z=${point[2]}`);
  }
 }
 for(const obstacle of world.obstacles.filter(o=>o.zoneId)){
  const zone=definitions.find(z=>z.id===obstacle.zoneId);
  assert.ok(Math.abs(obstacle.x-zone.x)+obstacle.w/2<=zone.width/2+1e-7);
  assert.ok(Math.abs(obstacle.z-zone.z)+obstacle.d/2<=zone.depth/2+1e-7);
 }
}

test('original-style uneven department layout retains twelve physical desks and rich shared furniture',()=>{
 const world=makeOffice(agents,floor,config);
 assert.equal(world.desks.length,12);assert.equal(world.agents.length,4);
 const tags=new Set(world.parts.map(p=>p.tag));
 for(const tag of ['meeting-glass','meeting-board','sofa-cushion','coffee-table','water-cooler','restroom-door','department-window-frame','department-window-frame-extension','department-window-extension','amenity-gym-floor','amenity-training-glass','amenity-pet-floor','pet-cat-body','ceramic-planter'])assert.ok(tags.has(tag),tag);
 const perimeter=world.parts.find(p=>p.tag==='department-perimeter');
 const perimeterDimensions=perimeter?.mesh?.startsWith('bevel:')?perimeter.mesh.slice(6).split(':').map(Number).slice(0,3):perimeter?.s;
 assert.deepEqual(perimeterDimensions,[40,.08,20]);assert.equal(perimeter?.p[0],-4);
 assert.equal(world.facilities.drink.length,2);assert.equal(world.facilities.nap.length,2);assert.equal(world.facilities.restroom.length,1);assert.equal(world.meetingSeats.length,6);
 assert.ok(world.facilities.restroom[0].id.startsWith('restroom:'));
 assert.ok(world.zones.every(z=>z.name.length<42),'Zone labels stay concise');
 assertFootprints(world,zones);
 for(const agent of world.agents)for(const slot of [...Object.values(world.facilities).flat(),...world.meetingSeats]){
  assert.ok(world.route(agent.home,slot.position).length,`${agent.id} cannot reach ${slot.id||slot.position}`);
  assert.ok(!world.obstacles.some(o=>!o.seat&&Math.abs(slot.position[0]-o.x)<o.w/2+.27&&Math.abs(slot.position[2]-o.z)<o.d/2+.27),`Blocked facility ${slot.id||slot.position}`);
 }
});

for(const kind of ['meeting','lounge','restroom','pantry'])test(`${kind} furniture follows moved zones and remains inside the minimum supported footprint`,()=>{
 const zone={id:kind,name:kind,kind,x:0,z:0,width:kind==='meeting'?4:3,depth:3};
 const make=z=>makeOffice([],{...floor,zones:[z]},{layout_version:2,positions:{},floors:[{...floor,zones:[z]}],desks:[],teams:[]});
 const world=make(zone),shifted=make({...zone,x:3,z:2});
 const first=world.parts.filter(p=>p.zoneId===kind),second=shifted.parts.filter(p=>p.zoneId===kind);
 assert.ok(first.length>10);assert.equal(second.length,first.length);
 for(let i=0;i<first.length;i++){
  const a=first[i].matrix?[first[i].matrix[12],first[i].matrix[14]]:[first[i].p[0],first[i].p[2]],b=second[i].matrix?[second[i].matrix[12],second[i].matrix[14]]:[second[i].p[0],second[i].p[2]];
  assert.ok(Math.abs(b[0]-a[0]-3)<1e-5&&Math.abs(b[1]-a[1]-2)<1e-5,first[i].tag);
 }
 assertFootprints(world,[zone]);assertFootprints(shifted,[{...zone,x:3,z:2}]);
 for(const slot of [...Object.values(world.facilities).flat(),...world.meetingSeats])assert.ok(world.route([0,0,-7],slot.position).length,`${kind}: ${slot.id||slot.position}`);
});

test('version two never invents a perimeter facility and explicit legacy restrooms suppress fallback',()=>{
 const bare={...floor,zones:[]};
 assert.equal(makeOffice([],bare,{layout_version:2,positions:{},floors:[bare]}).facilities.restroom.length,0);
 assert.equal(makeOffice([],bare,{layout_version:1,positions:{},floors:[bare]}).facilities.restroom[0].id,'floor-1:wc');
 const explicit={...floor,zones:[zones.find(z=>z.kind==='restroom')]};
 const world=makeOffice([],explicit,{layout_version:1,positions:{},floors:[explicit]});
 assert.equal(world.facilities.restroom.length,1);assert.equal(world.facilities.restroom[0].id,'restroom:wc');
});
