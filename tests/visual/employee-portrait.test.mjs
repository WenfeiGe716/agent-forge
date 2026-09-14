import assert from 'node:assert/strict';
import fs from 'node:fs';
import {detailedActor} from '../../frontend/dist/graphics/characters.js';

const source=fs.readFileSync(new URL('../../frontend/src/components/EmployeePortrait.tsx',import.meta.url),'utf8');
const base={id:'portrait-agent',position:[0,0,0],yaw:0,pose:'typing',poseTime:0,visible:true,sitBlend:0,source:'business',busy:true,evidence_ids:[],run_id:'run-1',task_key:'task-1',stage:'work:writing',action:null,facility:null,label:'正在执行',detail:'工具：shell'};
assert.match(source,/export type EmployeePortraitProps/);
assert.match(source,/data-agent-id=\{view\.id\}/);
assert.match(source,/requestAnimationFrame/);
assert.match(source,/detailedActor/);
assert.match(source,/upper-body|upper body|bust/i);
assert.match(source,/new OfficeRenderer/);
assert.doesNotMatch(source,/Rocketbox|ThreeOfficeRenderer|loadRocketboxPilot/);
assert.match(source,/cancelAnimationFrame\(raf\)/);
assert.match(source,/(?:realisticScene|scene).*dispose\(\)/);
assert.ok(detailedActor(base,0,0x54b2cc,false).length>30);
console.log('passed EmployeePortrait contract and shared detailed animated bust geometry');
