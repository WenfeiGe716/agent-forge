import assert from 'node:assert/strict';
import test from 'node:test';
import {makeOffice} from '../../frontend/dist/graphics/officeWorld.js';
import {officeFloorOptions} from '../../frontend/dist/graphics/departmentOffice.js';
import {MeetingStaging,meetingSeats} from '../../frontend/dist/office/meetings.js';
import {OfficeBehavior,defaults} from '../../frontend/dist/office/behavior.js';

const floor={floor:2,name:'品牌楼层',department:'市场部',zones:[
 {id:'work',name:'品牌内容区',kind:'work',x:0,z:-2,width:22,depth:10,color:'#cba96a'},
 {id:'meeting',name:'营销会议区',kind:'meeting',x:5.75,z:5.5,width:10.5,depth:3},
 {id:'common',name:'公共休息区',kind:'common',x:-5.75,z:5.5,width:10.5,depth:3}
]};
const agents=Array.from({length:3},(_,i)=>({id:'a'+i,name:'成员'+i,job_title:i===0?'品牌总监':'内容策划',role:i===0?'planner':'developer',department:'市场部',office_position:{floor:2,zone:'work',x:-8+i*4,z:-4}}));
const clone=x=>JSON.parse(JSON.stringify(x));
function base(world){return world.agents.map(a=>({id:a.id,position:[...a.home],yaw:a.yaw,pose:'thinking',posture:'seated',sitBlend:1,label:'真实工作',detail:'',source:'business',busy:true,visible:true,evidence_ids:[],run_id:'r',task_key:'discussion',stage:'work',action:null,facility:null,poseTime:0}));}
const meeting=(extra={})=>({id:'m',run_id:'r',host_id:'a0',participants:['a0','a1','a2'],subject:'业务复核',agenda:'交付',status:'active',speaker_ids:['a1'],created_at:'2026-09-10T00:00:00Z',source:'business',...extra});
const packet=meetings=>({meetings,presence:[],events:[],cursor:0,complete:true,settings:defaults,server_time:'2026-09-10T00:00:00Z'});
function advance(stage,views,meetings,from=0,to=30000){let result;for(let t=from;t<=to;t+=50){if(t%1000===0)stage.accept(packet(meetings),t);result=stage.update(views,t);}return result;}

test('applied department renders its own zones and only actual desks; empty floors are real empty layouts',()=>{
 const before=clone(agents),world=makeOffice(agents,floor);
 assert.equal(world.agents.length,3);
 assert.equal(world.parts.filter(p=>p.tag==='desk-oak-top').length,3);
 assert.ok(world.zones.some(z=>z.name==='品牌内容区'));
 assert.ok(world.zones.some(z=>z.name.includes('市场部')));
 assert.ok(!world.zones.some(z=>['开发工位','代码审查','测试与质量','规划与架构'].includes(z.name)));
 assert.equal(makeOffice([],floor).parts.filter(p=>p.tag==='desk-oak-top').length,0);
 assert.deepEqual(agents,before);
});

test('floor inventory includes explicit empty departments and actual member floors without inventing a first floor',()=>{
 const organization={ref:{id:'layout',version:3,digest:'approved'},config:{positions:{},floors:[floor,{...floor,floor:5,name:'财务楼层',department:'财务部'}]}};
 const options=officeFloorOptions(agents,organization);
 assert.deepEqual(options.map(o=>o.floor),[2,5]);assert.match(options[1].label,/财务部/);
 const mixed=officeFloorOptions([...agents,{id:'new'}],organization);
 assert.deepEqual(mixed.map(o=>o.floor),[1,2,5]);assert.match(mixed[0].label,/未纳入组织布局/);
 assert.deepEqual(officeFloorOptions([],null).map(o=>o.floor),[1]);
});

test('generated default floor names do not duplicate the department label',()=>{
 const definition={...floor,name:'2 楼 · 市场部'};
 assert.equal(officeFloorOptions(agents,{config:{floors:[definition]}})[0].label,'2 楼 · 市场部');
 assert.ok(makeOffice(agents,definition).zones.some(z=>z.name==='2 楼 · 市场部'));
});

test('other floors, invalid references and overlapping live positions do not create phantom desks',()=>{
 const bad=[{...agents[0],id:'wrong-floor',office_position:{...agents[0].office_position,floor:1}},
  {...agents[0],id:'unknown-zone',office_position:{...agents[0].office_position,zone:'unknown'}},
  {...agents[0],id:'wall',office_position:{...agents[0].office_position,x:10.8}},
  {...agents[0],id:'overlap'},{...agents[0],id:'nan',office_position:{...agents[0].office_position,z:NaN}}];
 const world=makeOffice([...agents,...bad],floor);
 assert.deepEqual(world.agents.map(a=>a.id),agents.map(a=>a.id));
 assert.equal(world.parts.filter(p=>p.tag==='desk-oak-top').length,3);
});

test('department furniture seats belong to configured areas and all actual routes reach facilities',()=>{
 const world=makeOffice(agents,floor),room=floor.zones.find(z=>z.kind==='meeting');
 assert.equal(world.meetingSeats.length,6);
 assert.equal(world.facilities.restroom[0].id,'floor-2:wc');
 for(const seat of world.meetingSeats){assert.ok(Math.abs(seat.position[0]-room.x)<room.width/2);assert.ok(Math.abs(seat.position[2]-room.z)<room.depth/2);}
 for(const a of world.agents)for(const destination of [...world.meetingSeats,...Object.values(world.facilities).flat()])assert.ok(world.route(a.home,destination.position).length,`${a.id} -> ${destination.id||destination.position}`);
 assert.equal(new Set(world.meetingSeats.map(s=>s.position.join(','))).size,6);
});

test('cross-floor attendees are filtered before seat allocation and remote-only earlier meetings do not lease the room',()=>{
 const world=makeOffice(agents,floor),stage=new MeetingStaging(world),remote=Array.from({length:8},(_,i)=>'remote'+i);
 const records=[meeting({id:'remote-only',participants:remote,created_at:'2026-09-09T00:00:00Z'}),meeting({participants:[...remote,...agents.map(a=>a.id)]})];
 const output=advance(stage,base(world),records);
 assert.equal(stage.positions().length,3);assert.ok(stage.positions().every(t=>t.phase==='seated'&&t.meeting==='m'));
 assert.deepEqual([...stage.participantIds(30000)].sort(),agents.map(a=>a.id).sort());
 assert.equal(output.find(v=>v.id==='a1').pose,'communicating');
 assert.ok(output.every(v=>world.meetingSeats.some(s=>Math.hypot(s.position[0]-v.position[0],s.position[2]-v.position[2])<.01)));
});

test('no configured meeting area means remote participation at actual desks, without invented legacy seats',()=>{
 const world=makeOffice(agents,{...floor,zones:[floor.zones[0]]}),stage=new MeetingStaging(world),original=base(world);
 assert.deepEqual(world.meetingSeats,[]);assert.deepEqual(world.facilities.drink,[]);assert.deepEqual(world.facilities.nap,[]);
 const output=advance(stage,original,[meeting()]);
 assert.equal(stage.positions().length,0);
 for(const v of output){assert.equal(v.label,'远程参与会议');assert.match(v.detail,/未配置可用会议席位/);assert.deepEqual(v.position,original.find(a=>a.id===v.id).position);}
});

test('attendance reorder and changes keep unique occupied seats and preserve actual seat orientation',()=>{
 const many=Array.from({length:7},(_,i)=>({...agents[i%3],id:'a'+i,office_position:{floor:2,zone:'work',x:-8+(i%5)*4,z:i<5?-4:-1}}));
 const world=makeOffice(many,floor),stage=new MeetingStaging(world),original=base(world);
 advance(stage,original,[meeting({participants:['a0','a1','a2','a3','a4','a5']})]);
 const output=advance(stage,original,[meeting({participants:['a5','a4','a3','a2','a1','a6']})],30050,31000);
 assert.equal(new Set(stage.positions().map(t=>t.position.join(','))).size,stage.positions().length);
 const present=output.filter(v=>v.stage==='meeting_seated');
 for(const v of present){const seat=world.meetingSeats.find(s=>Math.hypot(s.position[0]-v.position[0],s.position[2]-v.position[2])<.01);assert.ok(seat);assert.equal(v.yaw,seat.yaw);}
 assert.ok(output.some(v=>v.id==='a0'&&v.stage==='meeting_returning'));
});

test('legacy office retains its known meeting seating contract',()=>{
 const legacy=agents.map(a=>({...a,office_position:null})),world=makeOffice(legacy),stage=new MeetingStaging(world);
 assert.equal(world.meetingSeats,undefined);
 const output=advance(stage,base(world),[meeting()]);
 assert.equal(stage.positions().length,3);
 assert.ok(output.every(v=>meetingSeats.some(s=>Math.hypot(s.position[0]-v.position[0],s.position[2]-v.position[2])<.01)));
});

test('missing optional facilities skip idle actions instead of waiting forever for a nonexistent place',()=>{
 const world=makeOffice([agents[0]],{...floor,zones:[floor.zones[0]]}),fsm=new OfficeBehavior(world,true);
 const settings={...defaults,idle_grace_s:0,idle_stagger_s:0,idle_cooldown_s:0,activities:['drink','phone']};
 let output;
 for(let t=0;t<12000;t+=500){fsm.accept({...packet([]),settings,presence:[{agent_id:'a0',busy:false,phase:'idle',label:'空闲',scope:'global',evidence_ids:[]}]},t);output=fsm.advance(t);}
 assert.ok(fsm.trace.some(t=>t.to==='perform:phone'));
 assert.ok(!fsm.trace.some(t=>t.to==='queue'));
 assert.ok(!Object.keys(fsm.reservations()).some(id=>id.includes('water')));
 assert.equal(output.length,1);
});
