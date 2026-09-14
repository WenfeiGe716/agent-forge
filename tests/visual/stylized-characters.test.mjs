import assert from 'node:assert/strict';
import {test} from 'node:test';
import {stylizedActor} from '../../frontend/dist/graphics/characters.js';

const base={id:'stylized',position:[0,0,0],yaw:0,pose:'idle',poseTime:0,visible:true,sitBlend:1,source:'business',busy:false,evidence_ids:[],run_id:'r',task_key:'t',stage:'work:waiting',action:null,facility:null};

test('stylized avatar keeps compact head and stable semantic order',()=>{
  const parts=stylizedActor(base,0,0x54b2cc);
  assert.equal(parts[0].tag,'torso');
  assert.equal(parts[1].tag,'pelvis');
  assert.equal(parts[2].tag,'head');
  assert.ok(Math.abs(parts[2].s[0]-.34)<1e-6);
  assert.ok(parts.length<30);
});

test('stylized avatar preserves seat and sole contracts',()=>{
  const seated=stylizedActor(base,0,0x54b2cc);
  const standing=stylizedActor({...base,sitBlend:0},0,0x54b2cc);
  assert.ok(Math.abs(seated[1].p[1]-.845)<1e-6);
  assert.ok(standing[2].p[1]-seated[2].p[1]>.28);
  const soles=seated.filter(p=>p.tag==='shoe-sole');
  assert.equal(soles.length,2);
  assert.ok(soles.every(p=>Math.abs(p.p[1]-p.s[1]/2-.1115)<1e-6));
});

test('stylized identity colour is limited to floor ring',()=>{
  const parts=stylizedActor(base,0,0xe0b65d);
  const accent=parts.find(p=>p.tag==='identity-ring');
  assert.ok(accent);
  assert.equal(accent.color[0],0xe0/255);
  assert.equal(parts.filter(p=>p.tag==='identity-ring').length,1);
});
