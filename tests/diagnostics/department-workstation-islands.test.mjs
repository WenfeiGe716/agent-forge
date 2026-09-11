import assert from 'node:assert/strict';
import test from 'node:test';
import {createHash} from 'node:crypto';
import {makeOffice} from '../../frontend/dist/graphics/officeWorld.js';
import {deskKit,chairKit} from '../../frontend/dist/graphics/furniture.js';
import {workstationIslandInlays} from '../../frontend/dist/graphics/workstationIslands.js';
import {modelMeshes,bevelBox} from '../../frontend/dist/graphics/visualMeshes.js';
import {cubeVertices,modelMatrix,transform} from '../../frontend/dist/graphics/scene.js';

// Explicit default layout, not production data: 12 desks, four real members,
// eight editable zones. Keep the route/operation baselines from before this
// presentation change to detect accidental changes to physical behavior.
function fixture(){
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
 return{agents,floor,config};
}
const hash=value=>createHash('sha256').update(JSON.stringify(value)).digest('hex');
const make=f=>makeOffice(f.agents,f.floor,f.config);
const dimensions=p=>p.mesh?.startsWith('bevel:')?p.mesh.slice(6).split(':').map(Number).slice(0,3):p.s;
const geometry=p=>({p:p.p,s:p.s,mesh:p.mesh,matrix:p.matrix,assetOrigin:p.assetOrigin,tag:p.tag});
const meshes=modelMeshes();meshes.set('cube',cubeVertices());
function vertices(part){
 const points=part.mesh?.startsWith('bevel:')?bevelBox(...part.mesh.slice(6).split(':').map(Number)):meshes.get(part.mesh||'cube');
 assert.ok(points,part.mesh);const matrix=part.matrix||modelMatrix(part.p,part.s,part.yaw),out=[];
 for(let i=0;i<points.length;i+=6)out.push(transform(matrix,[points[i],points[i+1],points[i+2]]));return out;
}

test('white workstation presentation preserves the full applied layout and every facility route',()=>{
 const f=fixture(),before=structuredClone(f),world=make(f);
 assert.deepEqual(f,before);assert.equal(world.desks.length,12);assert.equal(world.actors.size,4);
 assert.equal(world.parts.filter(p=>p.tag==='desk-oak-top').length,12);
 assert.equal(world.desks.filter(d=>d.occupancy==='vacant').length,8);
 const operation={agents:world.agents,desks:world.desks,actors:[...world.actors.keys()],obstacles:world.obstacles,facilities:world.facilities,meetingSeats:world.meetingSeats,zones:world.zones};
 assert.equal(hash(operation),'f9180ed5c41cd4d5b2434882f002485ca11d1ca00b8784c1e55a7c2c802e33f3','pre-change desk/member, obstacle, facility and label contract');
 const paths=world.agents.flatMap(a=>[...Object.values(world.facilities).flat(),...world.meetingSeats].map(slot=>({id:a.id,to:slot.id||slot.position,path:world.route(a.home,slot.position)})));
 assert.ok(paths.every(p=>p.path.length));
 assert.equal(hash(paths),'e155b2adf4c485b32eb5d3f858fc72ccb7c4bd551accdfd4161e0e4b368eb034','all 44 pre-change paths remain identical');
 assert.equal(world.obstacles.length,45);assert.ok(world.parts.length<=2140,'macro grouping does not add dense decorative microgeometry');
});

test('twelve real desks form five shared white row bases instead of twelve isolated mats',()=>{
 const f=fixture(),world=make(f),bases=world.parts.filter(p=>p.tag==='workstation-island-base');
 assert.equal(bases.length,5);assert.deepEqual(bases.map(p=>p.zoneId),['arch','test','dev','dev','requirements']);
 assert.equal(world.parts.filter(p=>p.tag==='workstation-island-inlay').length,5);
 assert.equal(world.parts.filter(p=>p.tag==='island-low-divider').length,12);
 assert.equal(world.parts.filter(p=>p.tag==='island-planter').length,12);
 assert.equal(world.parts.filter(p=>p.tag==='island-planting').length,60);
 for(const base of bases){
  assert.ok(base.s[0]>5.9,'shared base spans the actual adjoining desks');assert.equal(base.castShadow,false);
  assert.ok(base.color.every(c=>c>.92),'white shared edge remains visible over the tinted zone carpet');
 }
 for(const p of world.parts.filter(p=>p.tag?.startsWith('workstation-island'))){
  assert.ok(p.p[1]-p.s[1]/2>.111);assert.ok(p.p[1]+p.s[1]/2<.118,'floor graphics remain below the chair casters');
 }
 for(const p of world.parts.filter(p=>p.tag?.startsWith('island-'))){
  const [x,,z]=p.assetOrigin;
  for(const v of vertices(p)){assert.ok(Math.abs(v[0]-x)<=1.35+1e-5,p.tag);assert.ok(Math.abs(v[2]-z)<=.54+1e-5,p.tag);}
 }
 for(const p of world.parts.filter(p=>p.zoneId)){
  const zone=f.floor.zones.find(z=>z.id===p.zoneId);
  for(const v of vertices(p)){assert.ok(Math.abs(v[0]-zone.x)<=zone.width/2+1e-4,p.tag);assert.ok(Math.abs(v[2]-zone.z)<=zone.depth/2+1e-4,p.tag);}
 }
});

test('department finish changes large visible color surfaces without moving furniture contacts',()=>{
 const hue=0x54b2cc,classic=deskKit(3,-2,hue),white=deskKit(3,-2,hue,0,'department');
 assert.deepEqual(white.filter(p=>!p.tag.startsWith('island-')).map(geometry),classic.map(geometry));
 const top=white.find(p=>p.tag==='desk-oak-top');assert.equal(top.surface,'matte');assert.ok(top.color.every(c=>c>.91));
 assert.deepEqual(dimensions(top),[2.6,.136,1.02]);assert.equal(top.p[1],1.011);
 assert.equal(white.filter(p=>p.tag==='keycap').length,48);
 const back=white.find(p=>p.tag==='monitor-back-panel');assert.ok(back.color[2]-back.color[0]>.4,'department color is visible on the large back panel');
 const oldChair=chairKit(3,-.98,hue),newChair=chairKit(3,-.98,hue,0,.715,'department');
 assert.deepEqual(newChair.map(geometry),oldChair.map(geometry));
 const outerBack=newChair.find(p=>p.tag==='chair-back-frame');assert.ok(outerBack.color[2]-outerBack.color[0]>.4);
 for(const tag of ['chair-seat-cushion','chair-caster','chair-armrest'])assert.deepEqual(newChair.filter(p=>p.tag===tag),oldChair.filter(p=>p.tag===tag));
 assert.equal(classic.find(p=>p.tag==='desk-oak-top').surface,'wood','legacy presentation remains compatible');
});

test('inlays respect team boundaries, deliberate aisles, changed zone locations and empty layouts',()=>{
 const zone={id:'work',kind:'work',x:0,z:0,width:22,depth:12};
 const d=(id,x,z=0,team_id='one')=>({id,zone:'work',floor:1,x,z,team_id,agent_id:null,rendered_agent_id:null,occupancy:'vacant'});
 const desks=[d('a',-8),d('b',-4.8),d('c',-.6),d('d',2.6,0,'two'),d('e',5.8,0,'two'),d('f',-8,2.7)];
 const before=structuredClone(desks),parts=workstationIslandInlays(desks,zone,0x54b2cc);
 assert.deepEqual(desks,before);assert.equal(parts.filter(p=>p.tag==='workstation-island-base').length,4);
 assert.deepEqual(workstationIslandInlays([],zone,0x54b2cc),[]);
 assert.deepEqual(workstationIslandInlays([d('bad',11.9)],zone,0x54b2cc),[],'presentation cannot overdraw zone bounds');
 assert.deepEqual(workstationIslandInlays([{...d('other',0),zone:'other'}],zone,0x54b2cc),[]);
 const nearRow=workstationIslandInlays([d('right',8),d('left',-8,.005)],zone,0x54b2cc);
 assert.equal(nearRow.filter(p=>p.tag==='workstation-island-base').length,2,'near-aligned rows must not bridge a wide aisle when x ordering reverses');
 const moved=workstationIslandInlays(desks.map(d=>({...d,x:d.x+1,z:d.z-1})),{...zone,x:1,z:-1},0x54b2cc);
 assert.equal(moved.length,parts.length);
 for(let i=0;i<parts.length;i++){assert.ok(Math.abs(moved[i].p[0]-parts[i].p[0]-1)<1e-9);assert.ok(Math.abs(moved[i].p[2]-parts[i].p[2]+1)<1e-9);assert.deepEqual(moved[i].s,parts[i].s);}
 const empty=fixture();empty.config.desks=[];const emptyWorld=make(empty);
 assert.equal(emptyWorld.agents.length,0);assert.equal(emptyWorld.parts.filter(p=>p.tag?.startsWith('island-')||p.tag?.startsWith('workstation-island')).length,0,'empty departments never gain fictional workstations');
});
