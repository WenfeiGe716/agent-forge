import assert from 'node:assert/strict';
import test from 'node:test';
import {makeOffice} from '../../frontend/dist/graphics/officeWorld.js';

test('published office coordinates place the actual workstation and reception',()=>{
 const agent={id:'designed',name:'Nova',role:'developer',office_position:{x:8,z:-7,floor:2,zone:'二楼开发'}};
 const original=structuredClone(agent),world=makeOffice([agent]);
 assert.deepEqual(world.agents[0].home,[8,0,-5.98]);
 assert.deepEqual(world.agents[0].reception,[9.65,0,-5.9]);
 assert.ok(world.obstacles.some(o=>o.x===8&&o.z===-7&&!o.seat));
 assert.deepEqual(agent,original);
});

test('changing role preserves a manually designed workstation',()=>{
 const office_position={x:-6,z:-5,floor:1,zone:'协作区'};
 const planner=makeOffice([{id:'a',name:'Atlas',role:'planner',office_position}]);
 const tester=makeOffice([{id:'a',name:'Atlas',role:'tester',office_position}]);
 assert.deepEqual(planner.agents[0].home,tester.agents[0].home);
});

test('unassigned decorative desks do not overlap designed workstation',()=>{
 const world=makeOffice([{id:'a',name:'Nova',role:'developer',office_position:{x:3,z:-5.6,floor:1,zone:'QA'}}]);
 const matching=world.obstacles.filter(o=>!o.seat&&o.w===2.7&&Math.abs(o.x-3)<2.9&&Math.abs(o.z+5.6)<2);
 assert.equal(matching.length,1);
});
