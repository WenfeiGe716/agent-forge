const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const ts=require('../../frontend/vendor/typescript.cjs');

// Render-model tests load current source in memory: no bundle, browser, GPU,
// production configuration or generated asset is touched.
const loaded=new Map();
function loadSource(filename){
 filename=path.resolve(filename);if(loaded.has(filename))return loaded.get(filename);
 const exports={};loaded.set(filename,exports);
 const code=ts.transpileModule(fs.readFileSync(filename,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 vm.runInNewContext(code,{exports,console,performance,Float32Array,require:id=>{
  assert.ok(id.startsWith('.'),'render geometry must not fetch external dependencies');
  return loadSource(path.resolve(path.dirname(filename),id+'.ts'));
 }},{filename});return exports;
}
const {makeOffice}=loadSource('frontend/src/graphics/officeWorld.ts');
const {chairKit,deskKit}=loadSource('frontend/src/graphics/furniture.ts');
const {color}=loadSource('frontend/src/graphics/officeRenderer.ts');
const plain=value=>JSON.parse(JSON.stringify(value));
const dimensions=part=>part.mesh?.startsWith('bevel:')?part.mesh.slice(6).split(':').map(Number).slice(0,3):plain(part.s);
function fixture(){
 const zones=[{id:'analysis',name:'需求分析',kind:'work',x:-6,z:-3,width:10,depth:8,color:'#6594d5'},
  {id:'finance',name:'经营分析',kind:'work',x:6,z:-3,width:10,depth:8,color:'#dfaa62'}];
 const floor={floor:2,name:'协作楼层',department:'产品部',zones};
 const agents=[{id:'a',name:'分析员',role:'planner',enabled:true},{id:'b',name:'财务员',role:'reviewer',enabled:true}];
 const teams=[{id:'analysis-team',name:'分析团队',floor:2,zone_ids:['analysis'],headcount:2,member_ids:['a']},
  {id:'finance-team',name:'财务团队',floor:2,zone_ids:['finance'],headcount:2,member_ids:['b']}];
 const desks=[{id:'one',name:'工位一',floor:2,zone:'analysis',x:-8,z:-4,team_id:'analysis-team',agent_id:'a'},
  {id:'two',name:'空工位',floor:2,zone:'analysis',x:-4,z:-4,team_id:'analysis-team',agent_id:null},
  {id:'three',name:'工位三',floor:2,zone:'finance',x:4,z:-4,team_id:'finance-team',agent_id:'b'},
  {id:'four',name:'预留工位',floor:2,zone:'finance',x:8,z:-4,team_id:'finance-team',agent_id:'not-a-real-agent'}];
 const config={layout_version:2,positions:{},floors:[floor],teams,desks};return{agents,floor,config};
}
const make=f=>makeOffice(f.agents,f.floor,f.config);
const operation=world=>plain({agents:world.agents,desks:world.desks,actors:[...world.actors.keys()],obstacles:world.obstacles,facilities:world.facilities,meetingSeats:world.meetingSeats});

test('daylight surface changes preserve exact applied desks, occupants and input configuration',()=>{
 const f=fixture(),before=plain(f),world=make(f);assert.deepEqual(f,before);
 assert.equal(world.desks.length,4);assert.equal(world.actors.size,2);assert.equal(world.agents.length,2);
 assert.deepEqual(plain(world.desks.map(d=>[d.id,d.x,d.z,d.occupancy,d.rendered_agent_id])),[
  ['one',-8,-4,'occupied','a'],['two',-4,-4,'vacant',null],['three',4,-4,'occupied','b'],['four',8,-4,'reserved',null]]);
 assert.deepEqual(plain(world.agents.map(a=>[a.id,a.home,a.reception])),[['a',[-8,0,-2.98],[-6.35,0,-2.9]],['b',[4,0,-2.98],[5.65,0,-2.9]]]);
 assert.equal(world.parts.filter(p=>p.tag==='desk-oak-top').length,4,'color treatment adds no decorative workstations');
 assert.equal(world.facilities.restroom.length,0,'no invented facility outside the applied layout');
});

test('work islands visibly inherit configured accents without changing zone bounds or navigation',()=>{
 const f=fixture(),world=make(f),interiors=world.parts.filter(p=>p.tag==='organization-zone-interior');
 assert.equal(interiors.length,2);assert.notDeepEqual(plain(interiors[0].color),plain(interiors[1].color));
 for(const zone of f.floor.zones){
  const outline=world.parts.find(p=>p.tag==='organization-zone'&&p.zoneId===zone.id),interior=interiors.find(p=>p.zoneId===zone.id);
  assert.deepEqual(plain(outline.p),[zone.x,.079,zone.z]);assert.deepEqual(dimensions(outline),[zone.width,.024,zone.depth]);
  assert.deepEqual(plain(outline.color),plain(color(Number.parseInt(zone.color.slice(1),16))));
  const [w,,d]=dimensions(interior);assert.ok(w<zone.width&&d<zone.depth);assert.equal(interior.surface,'cloth');assert.ok(interior.color.every(c=>c>.7&&c<1));
 }
 const recolored=structuredClone(f);recolored.floor.zones[0].color='#bb7999';recolored.config.floors=[recolored.floor];
 assert.deepEqual(operation(make(recolored)),operation(world),'palette changes cannot create seats, shift members or change collision geometry');
});

test('warm tile surface and centered low-contrast joints remain below the editable islands',()=>{
 const world=make(fixture()),floor=world.parts.find(p=>p.tag==='department-floor'&&p.p[1]===0),joints=world.parts.filter(p=>p.tag==='floor-tile-joint');
 assert.ok(floor.color[0]>=floor.color[1]&&floor.color[1]>floor.color[2]);assert.ok(floor.color.every(c=>c>.9));
 assert.equal(joints.length,30);
 for(const axis of [0,2]){
  const lines=joints.filter(p=>p.s[axis]<.1).map(p=>p.p[axis]);assert.ok(lines.includes(0));
  assert.deepEqual(plain(lines),plain([...lines].reverse().map(n=>n===0?0:-n)),'grid is centered on both axes');
  for(let i=1;i<lines.length;i++)assert.equal(lines[i]-lines[i-1],1.5);
 }
 for(const joint of joints){assert.equal(joint.castShadow,false);assert.ok(joint.p[1]+joint.s[1]/2<.079);assert.ok(joint.color.every((c,i)=>Math.abs(c-floor.color[i])<.08));}
});

test('chairs retain seat height and casters while preserving more of the team color',()=>{
 const hue=0x4d92d9,chair=chairKit(2,3,hue),seat=chair.find(p=>p.tag==='chair-seat-cushion');
 assert.ok(Math.abs(seat.matrix[13]+dimensions(seat)[1]/2-.715)<1e-7);
 assert.equal(chair.filter(p=>p.tag==='chair-caster').length,5);assert.equal(chair.filter(p=>p.tag==='chair-armrest').length,2);
 assert.ok(seat.color[2]-seat.color[0]>.35,'team upholstery should not be mostly neutral gray');
 assert.ok(Math.hypot(...seat.color.map((c,i)=>c-color(hue)[i]))<.18);
 const meeting=chairKit(2,3,hue,Math.PI,.82).find(p=>p.tag==='chair-seat-cushion');assert.ok(Math.abs(meeting.matrix[13]+dimensions(meeting)[1]/2-.82)<1e-7);
});

test('lighter oak desks keep their geometry and real accessory detail',()=>{
 const parts=deskKit(4,-3,0x4d92d9),top=parts.find(p=>p.tag==='desk-oak-top');
 assert.deepEqual(plain(top.p),[4,1.011,-3]);assert.deepEqual(dimensions(top),[2.6,.136,1.02]);assert.equal(top.surface,'wood');
 assert.ok(top.color[0]>.88&&top.color[0]>top.color[1]&&top.color[1]>top.color[2]);
 assert.equal(parts.filter(p=>p.tag==='keycap').length,48);
 for(const tag of ['monitor-frame','monitor-screen','cup-handle','lamp-arm','notebook-pages','pen'])assert.ok(parts.some(p=>p.tag===tag),tag);
 assert.ok(parts.every(p=>[...p.p,...p.s,...p.color,...(p.matrix||[])].every(Number.isFinite)));
});

test('legacy presentation uses the same warm surface without changing an assigned member position',()=>{
 const agent={id:'legacy',name:'旧布局成员',role:'developer',office_position:{x:8,z:-7,floor:2,zone:'custom'}},before=plain(agent),world=makeOffice([agent]);
 assert.deepEqual(agent,before);assert.deepEqual(plain(world.agents[0].home),[8,0,-5.98]);
 const floor=world.parts.find(p=>p.p[1]===0&&p.s[0]===26.85);assert.deepEqual(plain(floor.color),plain(color(0xf0eee7)));
 const islands=world.parts.filter(p=>p.p[1]===.101);assert.equal(islands.length,6);assert.ok(new Set(islands.map(p=>p.color.join(','))).size>3);
});

test('neutral indoor daylight leaves night sky lighting and exterior quality fallback intact',()=>{
 const source=fs.readFileSync('frontend/src/graphics/officeRenderer.ts','utf8');
 assert.match(source,/vec3 indoorAmbient=mix\(vec3\(\.38,\.345,\.30\),vec3\(\.80,\.81,\.79\),hemi\)/);
 assert.match(source,/mix\(skyAmbient\*vec3\(\.21,\.26,\.43\),K\.w>15\.5\?skyAmbient:indoorAmbient,day\)/);
 assert.match(source,/if\(K\.w>15\.5&&environmentQuality<1\.5\)/);
 assert.match(source,/float vis=visibility\(n,l\)/,'real shadows remain part of material response');
});
