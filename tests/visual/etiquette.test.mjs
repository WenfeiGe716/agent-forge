import assert from 'node:assert/strict';
import fs from 'node:fs';
import {OfficeEtiquette} from '../../frontend/dist/office/etiquette.js';
import {makeOffice,drawActor} from '../../frontend/dist/graphics/officeWorld.js';
const results=[];const test=(name,fn)=>{try{fn();results.push({name,status:'passed'});console.log('PASS',name);}catch(e){results.push({name,status:'failed',error:e.stack});console.log('FAIL',name,e.message);}};
const time='2026-09-09T01:00:00Z';
function setup(reception='sit'){
 const world={agents:[{id:'leader',home:[-3,0,0],yaw:Math.PI,role:'planner'},{id:'peer',home:[-2,0,3],yaw:Math.PI,role:'developer',reportsTo:'leader'},{id:'host',home:[0,0,0],yaw:Math.PI,role:'developer',reportsTo:'leader',peerReception:reception,reception:[1.65,0,0]}],facilities:{},route:(a,b)=>[[...b]]};
 const base=world.agents.map(a=>({id:a.id,position:[...a.home],yaw:a.yaw,pose:'typing',label:'写入',detail:'',source:'business',busy:true,visible:true,evidence_ids:[],run_id:'run',task_key:'task',stage:'work',action:null,facility:null,poseTime:0}));return {world,base,m:new OfficeEtiquette(world)};
}
const event=(from='peer',id=1)=>({id,run_id:'run',kind:'agent.message',created_at:time,data:{from_agent_id:from,to_agent_id:'host',body:'real message'}});
const at=(m,base,t,id='host')=>m.update(base,t).find(a=>a.id===id);
function arrive(m,base,from='peer'){m.update(base,0);m.accept([event(from)],1,time);m.update(base,1);for(let t=101;t<=4001;t+=100)m.update(base,t);return m.update(base,4101).find(a=>a.id==='host');}
for(const pose of ['idle','typing','reading','thinking','testing','reviewing','paused','attention','phone'])test('工位坐姿：'+pose,()=>{const{m,base}=setup();base[2].pose=pose;assert.equal(at(m,base,0).posture,'seated');assert.equal(at(m,base,0).sitBlend,1);});
test('普通同事来访默认坐着交流',()=>{const{m,base}=setup();const v=arrive(m,base);assert.equal(v.posture,'seated');assert.equal(v.leader_visit,false);assert.equal(v.visit_event_id,1);});
test('同事来访可配置站起，站在椅旁',()=>{const{m,base}=setup('stand');const v=arrive(m,base);assert.equal(v.posture,'standing');assert.ok(v.position[0]>.7);});
test('领导来访覆盖坐姿偏好并站到椅旁',()=>{const{m,base}=setup('sit');const v=arrive(m,base,'leader');assert.equal(v.posture,'standing');assert.equal(v.leader_visit,true);assert.ok(v.position[0]>.7);});
test('领导离开后重新入座',()=>{const{m,base}=setup();arrive(m,base,'leader');for(let t=4500;t<=13000;t+=250)m.update(base,t);assert.equal(at(m,base,13200).posture,'seated');assert.ok(at(m,base,13200).position[0]<.01);});
test('领导关系沿汇报链判断',()=>{const{m,world}=setup();world.agents[2].reportsTo='peer';assert.equal(m.isLeader('leader','host'),true);assert.equal(m.isLeader('host','leader'),false);});
test('没有消息不发生随机来访',()=>{const{m,base}=setup();const old=Math.random;Math.random=()=>{throw Error('random');};try{for(let t=0;t<10000;t+=100)m.update(base,t);assert.equal(m.activeVisits().length,0);}finally{Math.random=old;}});
test('重复事件只产生一个来访',()=>{const{m,base}=setup();arrive(m,base);m.accept([event()],4500,time);m.update(base,4500);assert.equal(m.activeVisits().length,1);assert.equal(m.trace.filter(t=>t.to==='approach').length,1);});
test('旧消息不发起来访',()=>{const{m,base}=setup();m.accept([{...event(),created_at:'2026-09-08T01:00:00Z'}],0,time);m.update(base,1);assert.equal(m.activeVisits().length,0);});
test('断线中止来访且保持安全来源',()=>{const{m,base}=setup();arrive(m,base,'leader');base[2].source='safety';assert.equal(at(m,base,4500).source,'safety');assert.equal(m.activeVisits().length,0);});
test('不匹配的运行不会发起来访',()=>{const{m,base}=setup();base[2].run_id='other';m.accept([event()],0,time);m.update(base,1);assert.equal(m.activeVisits().length,0);});
test('接待位置被占用时其他来访排队',()=>{const{m,base}=setup();m.update(base,0);m.accept([event('peer',1),event('leader',2)],1,time);m.update(base,1);assert.equal(m.activeVisits().length,1);});
test('真实办公室全部座位和接待点可达',()=>{const agents=Array.from({length:14},(_,i)=>({id:'x'+i,name:'A'+i,role:['planner','developer','tester','reviewer'][i%4]})),w=makeOffice(agents);for(const a of w.agents)assert.ok(w.route(a.home,a.reception).length,a.id);});
test('坐姿骨盆贴合座面，脚在地面，站姿头部更高',()=>{const{m,base}=setup();const view=at(m,base,0);const seated=drawActor(view,0,0x99aaff);const standing=drawActor({...view,sitBlend:0,posture:'standing'},0,0x99aaff);assert.ok(Math.abs(seated[1].p[1]-.845)<1e-6);assert.ok(Math.abs(seated[1].p[1]-.13-.715)<1e-6);assert.ok(standing[2].p[1]>seated[2].p[1]+.28);const soles=seated.filter(p=>p.tag==='shoe-sole');assert.equal(soles.length,2);assert.ok(soles.every(p=>Math.abs((p.p[1]-p.s[1]/2)-.1115)<1e-6));});
const report={passed:results.filter(r=>r.status==='passed').length,failed:results.filter(r=>r.status==='failed').length,results};fs.writeFileSync(new URL('../../docs/evidence-v05/etiquette-results.json',import.meta.url),JSON.stringify(report,null,2));console.log(report.passed,report.failed);if(report.failed)process.exit(1);
