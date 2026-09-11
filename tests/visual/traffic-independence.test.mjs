import assert from 'node:assert/strict';
import test from 'node:test';
import {CityTraffic} from '../../frontend/dist/office/cityTraffic.js';

const step=(sim,seconds)=>{for(let i=0;i<Math.round(seconds/.05);i++)sim.advance(.05);};
function releases(sim,seconds=230,{changeDensity=false}={}) {
  const cycles=[];
  let cycle=null;
  for(let frame=0;frame<Math.round(seconds/.05);frame++) {
    if(changeDensity&&frame%373===0)sim.setDensity(['normal','busy','low'][Math.floor(frame/373)%3]);
    const before=new Set(sim.pedestrians.filter(p=>p.crossing).map(p=>p.id));
    const previousPhase=sim.phase;
    sim.advance(.05);
    if(sim.phase==='ped_green'&&previousPhase!=='ped_green'){cycle={start:sim.time,entries:[]};cycles.push(cycle);}
    for(const p of sim.pedestrians)if(p.crossing&&!before.has(p.id)&&cycle)cycle.entries.push({id:p.id,time:sim.time});
    if(sim.pedestrians.some(p=>p.crossing)) {
      assert.equal(sim.signals.ew,'red');assert.equal(sim.signals.ns,'red');
      assert.ok(sim.cars.every(c=>Math.abs(c.s-sim.lanes[c.lane].cross)>=7.8));
    }
  }
  return cycles.filter(c=>sim.time-c.start>17);
}

for(const density of ['low','normal','busy']) {
  test(`${density}: every one of at least three green cycles releases individuals separately`,()=>{
    const sim=new CityTraffic(0,density),cycles=releases(sim);
    assert.ok(cycles.length>=3);
    for(const cycle of cycles) {
      assert.equal(cycle.entries.length,density==='low'?2:density==='normal'?6:8);
      assert.equal(new Set(cycle.entries.map(e=>e.id)).size,cycle.entries.length,'no repeated crossing in one green');
      const entries=[...cycle.entries].sort((a,b)=>a.time-b.time);
      for(let i=1;i<entries.length;i++)assert.ok(entries[i].time-entries[i-1].time>=.2-1e-8,`synchronized release ${JSON.stringify(entries)}`);
    }
  });
}

test('individual green reaction timers are not consumed while pedestrians wait at red',()=>{
  const sim=new CityTraffic(0,'busy'),waits=sim.pedestrians.map(p=>p.wait);
  step(sim,30);
  assert.equal(sim.signals.ped,'red');
  assert.deepEqual(sim.pedestrians.map(p=>p.wait),waits);
});

test('independent walking speeds survive a repeated crossing rather than sharing one progress clock',()=>{
  const sim=new CityTraffic(0,'busy');
  let verified=0;
  for(let frame=0;frame<3500;frame++) {
    const previous=new Map(sim.pedestrians.filter(p=>p.crossing&&p.progress<7).map(p=>[p.id,p.progress]));
    sim.advance(.05);
    const speeds=sim.pedestrians.filter(p=>previous.has(p.id)&&p.crossing&&p.progress<8).map(p=>+(p.progress-previous.get(p.id)).toFixed(6));
    if(speeds.length<3)continue;
    assert.equal(new Set(speeds).size,speeds.length,'different people should have different walking paces');
    verified++;
  }
  assert.ok(verified>100);
});

test('density changes never remove crossing people and remain independently scheduled across three cycles',()=>{
  const sim=new CityTraffic(0,'busy');
  for(let frame=0;frame<1100&&!sim.pedestrians.some(p=>p.crossing);frame++)sim.advance(.05);
  const crossing=sim.pedestrians.filter(p=>p.crossing).map(p=>({...p}));
  assert.ok(crossing.length);
  sim.setDensity('low');
  for(const p of crossing)assert.deepEqual(sim.pedestrians.find(other=>other.id===p.id),p);
  const cycles=releases(sim,230,{changeDensity:true});
  assert.ok(cycles.length>=3);
  for(const cycle of cycles) {
    const simultaneous=new Map();
    for(const e of cycle.entries){const key=e.time.toFixed(3);simultaneous.set(key,(simultaneous.get(key)||0)+1);}
    assert.ok(Math.max(...simultaneous.values())<3,'density updates may not release a synchronized trio');
  }
});

test('checkpoint restore preserves in-flight pedestrian positions, pace and per-green release delay',()=>{
  for(const time of [12,38.8,40.7,46,95]) {
    const sim=new CityTraffic(0,'busy');step(sim,time);
    const restored=CityTraffic.restore(sim.checkpoint());
    assert.ok(restored);
    assert.deepEqual(restored.snapshot(),sim.snapshot());
    for(let frame=0;frame<3500;frame++){sim.advance(.05);restored.advance(.05);}
    assert.deepEqual(restored.snapshot(),sim.snapshot());
  }
});

test('legacy checkpoint resumes without teleport and corrects synchronized release on future cycles',()=>{
  const sim=new CityTraffic();step(sim,41);
  const old=sim.checkpoint();old.version=1;
  for(const p of old.pedestrians){p.crossing=true;p.walking=true;p.direction=1;p.progress=3.1;p.z=sim.cz-4.7+p.progress;p.wait=0;}
  const restored=CityTraffic.restore(old);
  assert.ok(restored);
  for(const p of old.pedestrians) {
    const other=restored.pedestrians.find(q=>q.id===p.id);
    assert.ok(Math.abs(other.z-p.z)<1e-9);assert.equal(other.x,p.x);
  }
  const cycles=releases(restored);
  assert.ok(cycles.length>=3);
  for(const cycle of cycles)assert.equal(new Set(cycle.entries.map(e=>e.time.toFixed(3))).size,6);
});

test('separate car lanes start with distinct deterministic headway and speed profiles',()=>{
  const sim=new CityTraffic(0,'normal');
  const cars=sim.cars.filter(car=>car.id.endsWith('-0'));
  assert.equal(new Set(cars.map(car=>+(sim.lanes[car.lane].cross-car.s).toFixed(4))).size,4);
  assert.equal(new Set(cars.map(car=>car.speed)).size,4);
  const again=new CityTraffic(0,'normal');
  assert.deepEqual(again.snapshot(),sim.snapshot());
});
