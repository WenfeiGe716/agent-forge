/** User-symptom regression: ambient people must not march in permanent triples.
 * Exercises the same fixed clock -> traffic -> drawCity path as Office.tsx.
 * No browser, server, credentials, business data, or runtime files are touched.
 * Run after the normal offline build:
 *   node --test tests/diagnostics/ambient-independence.mjs
 */
import assert from 'node:assert/strict';
import test from 'node:test';
import {AmbientEnvironment} from '../../frontend/dist/office/ambientEnvironment.js';
import {drawCity,sidewalkWalkers} from '../../frontend/dist/graphics/cityWorld.js';

function runtime(density='normal') {
  let now=0;
  const storage=()=>{const values=new Map();return{getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,value)};};
  const env=new AmbientEnvironment(()=>now,storage(),storage());
  env.start('ambient-independence-regression',false);
  env.setPreferences({density});
  return {env,step(){now+=50;env.pump(now);return env.traffic;}};
}
const near=(a,b,epsilon=1e-5)=>Math.abs(a-b)<epsilon;
const center=part=>[part.matrix[12],part.matrix[14]];

test('drawCity emits exactly one person/car per ambient entity, not triple geometry',()=>{
  const sim=runtime();
  for(let i=0;i<850;i++)sim.step();
  const traffic=sim.env.traffic;
  const parts=drawCity(traffic,true,[]);
  const heads=parts.filter(part=>part.tag==='city-ped-head');
  const bodies=parts.filter(part=>part.tag==='city-car-body');
  assert.equal(heads.length,traffic.pedestrians.length+sidewalkWalkers(traffic).length);
  assert.equal(bodies.length,traffic.cars.length);
  for(const pedestrian of traffic.pedestrians) {
    assert.equal(heads.filter(part=>{
      const [x,z]=center(part);
      return near(x,pedestrian.x)&&near(z,pedestrian.z+Math.cos(pedestrian.yaw)*.01);
    }).length,1,`expected one rendered head for ${pedestrian.id}`);
  }
  for(const car of traffic.cars) {
    assert.equal(bodies.filter(part=>near(center(part)[0],car.x)&&near(center(part)[1],car.z)).length,1,`expected one rendered body for ${car.id}`);
  }
});

for(const density of ['normal','busy']) {
  test(`${density}: crossing pedestrians do not maintain a side-by-side synchronized trio for two seconds`,()=>{
    const sim=runtime(density),held=new Map();
    // One natural signal cycle, including actual curb waiting and green release.
    // Artificially starting at green would hide the waiting-time synchronisation.
    for(let frame=0;frame<1100;frame++) {
      const traffic=sim.step(),active=new Set();
      for(const anchor of traffic.pedestrians.filter(p=>p.crossing)) {
        const group=traffic.pedestrians.filter(p=>p.crossing&&p.direction===anchor.direction&&Math.abs(p.x-anchor.x)<1.81&&near(p.z,anchor.z)&&near(p.progress,anchor.progress));
        if(group.length<3)continue;
        const key=group.map(p=>p.id).sort().join(',');
        if(active.has(key))continue;
        active.add(key);
        const frames=(held.get(key)||0)+1;
        held.set(key,frames);
        if(frames>=40) {
          const heads=drawCity(traffic,true,[]).filter(part=>part.tag==='city-ped-head');
          const evidence=group.map(p=>({id:p.id,x:p.x,z:+p.z.toFixed(4),progress:+p.progress.toFixed(4),rendered_head:heads.some(h=>near(center(h)[0],p.x)&&near(center(h)[1],p.z+Math.cos(p.yaw)*.01))}));
          assert.fail(`persistent rendered trio at ${traffic.time.toFixed(2)}s (${traffic.phase}), held ${frames*50}ms: ${JSON.stringify(evidence)}`);
        }
      }
      for(const key of held.keys())if(!active.has(key))held.delete(key);
    }
  });
}

test('600 seconds per density: road vehicles remain separate, including red-light queues',()=>{
  const evidence=[];
  for(const density of ['low','normal','busy']) {
    const sim=runtime(density);
    let minimumGap=Infinity,minimumWorldDistance=Infinity,queuedFrames=0;
    for(let frame=0;frame<12000;frame++) {
      const traffic=sim.step();
      for(let a=0;a<traffic.cars.length;a++)for(let b=a+1;b<traffic.cars.length;b++) {
        const car=traffic.cars[a],other=traffic.cars[b];
        const distance=Math.hypot(car.x-other.x,car.z-other.z);
        minimumWorldDistance=Math.min(minimumWorldDistance,distance);
        assert.ok(distance>.01,`duplicate world coordinates: ${car.id}, ${other.id} at ${traffic.time}`);
        if(car.lane!==other.lane)continue;
        const length=traffic.lanes[car.lane].length;
        const gap=Math.min((other.s-car.s+length)%length,(car.s-other.s+length)%length);
        minimumGap=Math.min(minimumGap,gap);
        assert.ok(gap>=4.249,`overlapping lane ${car.lane} at ${traffic.time}: ${car.id}, ${other.id}, gap ${gap}`);
      }
      if([0,1,2,3].some(lane=>traffic.cars.filter(c=>c.lane===lane&&c.stopped).length>=3))queuedFrames++;
    }
    evidence.push({density,seconds:600,minimum_same_lane_gap:Number.isFinite(minimumGap)?+minimumGap.toFixed(4):null,minimum_world_distance:+minimumWorldDistance.toFixed(4),frames_with_three_car_queue:queuedFrames});
  }
  console.log('vehicle evidence',JSON.stringify(evidence));
});
