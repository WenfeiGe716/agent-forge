import assert from 'node:assert/strict';
import test from 'node:test';
import {makeOffice} from '../../frontend/dist/graphics/officeWorld.js';
import {officeFloorOptions} from '../../frontend/dist/graphics/departmentOffice.js';

const floor=(number=2)=>({floor:number,name:'部门 '+number,department:'部门 '+number,zones:[
 {id:'work_'+number,name:'团队工作区',kind:'work',x:0,z:0,width:22,depth:12,color:'#6eaea3'},
]});
const people=[{id:'a',name:'实际同事',role:'developer',office_position:{floor:1,zone:'old',x:10,z:6}},
 {id:'b',name:'另一同事',role:'reviewer',office_position:{floor:2,zone:'work_2',x:4,z:1}}];
const team=(number=2)=>({id:'team_'+number,name:'团队 '+number,floor:number,zone_ids:['work_'+number],headcount:3,member_ids:['a']});
const desk=(id,x,agent_id=null,number=2)=>({id,name:'桌 '+id,floor:number,zone:'work_'+number,x,z:-3,team_id:'team_'+number,agent_id});
const config=()=>({positions:{},floors:[floor(),floor(5)],teams:[team(),team(5)],desks:[desk('occupied',-7,'a'),desk('empty',-3),desk('reserved',1,'not-in-roster'),desk('other_floor',5,'b',5)]});
const deskCount=world=>world.parts.filter(part=>part.tag==='desk-oak-top').length;

test('explicit physical desks render empty and reserved furniture without synthetic members',()=>{
 const value=config(),before=structuredClone(value),original=structuredClone(people);
 const world=makeOffice(people,value.floors[0],value);
 assert.equal(deskCount(world),3);
 assert.deepEqual(world.agents.map(agent=>agent.id),['a']);
 assert.deepEqual([...world.actors.keys()],['a']);
 assert.deepEqual(world.agents[0].home,[-7,0,-1.98]);
 assert.equal(world.desks.find(d=>d.id==='empty').occupancy,'vacant');
 assert.equal(world.desks.find(d=>d.id==='reserved').occupancy,'reserved');
 assert.equal(world.desks.find(d=>d.id==='occupied').occupancy,'occupied');
 assert.ok(world.zones.some(zone=>/团队 2.*实员 1.*编制 3.*桌 3/.test(zone.detail||'')));
 assert.deepEqual(value,before);assert.deepEqual(people,original);
});

test('an explicit empty desk list stays empty while missing or null preserves actual-member legacy seating',()=>{
 const local=[{...people[0],office_position:{floor:2,zone:'work_2',x:-7,z:-3}}];
 for(const value of [{floors:[floor()],positions:{}},{floors:[floor()],positions:{},desks:null}]){
  assert.equal(deskCount(makeOffice(local,floor(),value)),1);
 }
 assert.equal(deskCount(makeOffice(local,floor(),{floors:[floor()],positions:{},teams:[team()],desks:[]})),0);
});

test('all floors use globally unique member bindings and physical desk identities',()=>{
 const value=config();
 value.desks.push(desk('duplicate_agent',-7,'a',5),desk('occupied',-3,null,5));
 const worlds=value.floors.map(definition=>makeOffice(people,definition,value));
 assert.deepEqual(worlds.map(world=>world.agents.map(agent=>agent.id)),[['a'],['b']]);
 assert.equal(worlds[1].desks.find(d=>d.id==='duplicate_agent').occupancy,'reserved');
 assert.equal(worlds.flatMap(world=>world.desks).filter(d=>d.id==='occupied').length,1);
 assert.equal(new Set(worlds.flatMap(world=>world.agents.map(a=>a.id))).size,2);
 const options=officeFloorOptions(people,{ref:null,config:value});
 assert.deepEqual(options.map(option=>option.floor),[2,5]);
});

test('wrong-floor teams, unknown zones, overlaps and nonfinite desk positions do not render',()=>{
 const value=config();
 value.desks.push({...desk('wrong_team',5),team_id:'team_5'},
  {...desk('unknown_zone',5),zone:'unknown'},desk('overlap',-7),
  {...desk('not_finite',5),x:NaN},{...desk('outside',11),z:5.8});
 assert.equal(deskCount(makeOffice(people,value.floors[0],value)),3);
});

test('physical seat edits change only the new layout; old frozen configurations remain unchanged',()=>{
 const frozen=config(),edited=structuredClone(frozen);
 edited.desks[0].x=-6;edited.desks[0].z=0;
 edited.desks.push({...desk('added',5),z:0});
 const prior=makeOffice(people,frozen.floors[0],frozen),current=makeOffice(people,edited.floors[0],edited);
 assert.deepEqual(prior.agents[0].home,[-7,0,-1.98]);
 assert.deepEqual(current.agents[0].home,[-6,0,1.02]);
 assert.equal(deskCount(prior),3);assert.equal(deskCount(current),4);
});

test('a floor with no selected real members still shows all physical desks and no actors',()=>{
 const value=config(),world=makeOffice([],value.floors[0],value);
 assert.equal(deskCount(world),3);assert.equal(world.agents.length,0);assert.equal(world.actors.size,0);
 assert.equal(world.desks.filter(d=>d.occupancy==='reserved').length,2);
 assert.equal(world.desks.filter(d=>d.occupancy==='vacant').length,1);
 assert.ok(world.zones.some(zone=>(zone.detail||'').includes('2 桌预留/未展示成员')));
});

test('teams occupy only their own work zones and report their own capacity and real members',()=>{
 const definition={...floor(),zones:[
  {id:'left',name:'品牌策略',kind:'work',x:-5.75,z:0,width:10.5,depth:12},
  {id:'right',name:'内容运营',kind:'work',x:5.75,z:0,width:10.5,depth:12},
 ]};
 const value={positions:{},floors:[definition],teams:[
  {...team(),id:'strategy',name:'策略组',zone_ids:['left'],headcount:2,member_ids:['a']},
  {...team(),id:'content',name:'内容组',zone_ids:['right'],headcount:2,member_ids:['b']},
 ],desks:[
  {...desk('strategy-person',-7,'a'),zone:'left',team_id:'strategy'},
  {...desk('strategy-empty',-3),zone:'left',team_id:'strategy'},
  {...desk('content-person',3,'b'),zone:'right',team_id:'content'},
  {...desk('content-empty',7),zone:'right',team_id:'content'},
  {...desk('cross-team',-7),z:0,zone:'left',team_id:'content'},
 ]};
 const world=makeOffice(people,definition,value);
 assert.equal(deskCount(world),4);assert.deepEqual(world.agents.map(a=>a.id),['a','b']);
 assert.ok(world.zones.some(zone=>/品牌策略 · 策略组 · 实员 1 \/ 编制 2 \/ 桌 2/.test(zone.detail||'')));
 assert.ok(world.zones.some(zone=>/内容运营 · 内容组 · 实员 1 \/ 编制 2 \/ 桌 2/.test(zone.detail||'')));
 assert.ok(!world.desks.some(d=>d.id==='cross-team'));
});
