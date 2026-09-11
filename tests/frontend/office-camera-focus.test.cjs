const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const ts=require('../../frontend/vendor/typescript.cjs');
const plain=value=>JSON.parse(JSON.stringify(value));

// Exercise the production Office lifecycle, SceneControl and animation callback.
// The only adapters replaced are GPU/DOM, the city geometry, and the upstream
// animation poses. No task, network, browser or production configuration is used.
function harness(overrides={}){
 let now=1000,cursor=0,refCount=0,effects=[],nextFrame=null,frameId=0,cleanup=null,renderer;
 const slots=[],modes=[],selected=[],nodes=[];
 function element(tag){
  const listeners=new Map(),children=[],classes=new Set();
  const node={tag,children,dataset:{},style:{setProperty(key,value){this[key]=value;}},classList:{toggle(key,on){if(on)classes.add(key);else classes.delete(key);},add(key){classes.add(key);},remove(key){classes.delete(key);}},clientWidth:1280,clientHeight:720,offsetWidth:95,hidden:false,
   setAttribute(key,value){this[key]=value;},append(...items){children.push(...items);},appendChild(item){children.push(item);},remove(){},focus(){},
   addEventListener(key,fn){listeners.set(key,fn);},removeEventListener(key){listeners.delete(key);},
   getBoundingClientRect(){return{left:0,top:0,width:1280,height:720};},setPointerCapture(){},hasPointerCapture(){return false;},releasePointerCapture(){},
   dispatch(key,event={}){listeners.get(key)?.({preventDefault(){},...event});}};
  nodes.push(node);return node;
 }
 const host=element('div'),canvas=element('canvas'),upload=element('input');
 const React={useState(initial){const index=cursor++;if(!(index in slots))slots[index]=typeof initial==='function'?initial():initial;return[slots[index],value=>{slots[index]=typeof value==='function'?value(slots[index]):value;}];},
  useRef(initial){const index=cursor++;if(!(index in slots))slots[index]={current:refCount++===0?host:refCount===2?canvas:refCount===3?upload:initial};return slots[index];},useEffect(fn,deps){cursor++;effects.push({fn,deps});}};
 const part=(tag,x,z,y=.8,extra={})=>({tag,p:[x,y,z],s:[1,1,1],color:[.5,.5,.5],...extra});
 const agents=[{id:'a',name:'移动中的成员',role:'developer',office_position:{floor:1,x:0,z:0,zone:'work'}},{id:'b',name:'相邻团队成员',role:'reviewer',office_position:{floor:1,x:10,z:2,zone:'neighbor'}}];
 const world={parts:[part('office-floor',0,0,0),part('old-desk',0,-1,.8,{assetOrigin:[0,0,-1]}),part('neighbor-desk',10,1,.8,{assetOrigin:[10,0,1]})],
  agents:agents.map(agent=>({id:agent.id,home:[agent.office_position.x,0,agent.office_position.z],yaw:Math.PI})),
  actors:new Map(agents.map((agent,index)=>[agent.id,{agent,index}])),facilities:{restroom:[]},obstacles:[],zones:[{name:'邻近团队',position:[10,.2,1],accent:0x6699aa}],route:(_a,b)=>[b]};
 let poses=agents.map(agent=>({id:agent.id,position:[agent.office_position.x,0,agent.office_position.z],yaw:Math.PI,pose:'walking',poseTime:0,visible:true,sitBlend:0,source:'idle',busy:false,label:'移动中',evidence_ids:[],detail:'fixture pose',stage:'outbound'}));
 class Renderer{
  constructor(){renderer=this;this.target=[3.2,.5,3];this.zoom=.8;this.pitch=.70;this.yaw=1.06;this.quality='balanced';this.stats={};this.staticParts=[];this.dynamicParts=[];this.supportsInstancing=true;}
  setQuality(value){this.quality=value;}setStatic(parts){this.staticParts=parts;}
  draw(parts){this.dynamicParts=parts;}project(p){return[640+(p[0]-this.target[0])*30,360-(p[1]-this.target[1])*30+(p[2]-this.target[2])*10,0];}
  mesh(){}reset(){this.target=[3.2,.5,3];this.zoom=.8;this.pitch=.70;this.yaw=1.06;}dispose(){}
 }
 class Behavior{constructor(){this.messages=[];}accept(){}advance(){return poses.map(p=>({...p,position:[...p.position]}));}disconnect(){}audit(){return[];}reservations(){return{};}}
 class Etiquette{constructor(){this.trace=[];}accept(){}suppress(){}update(views){return views;}}
 class Meetings{constructor(){this.trace=[];}accept(){}participantIds(){return new Set();}update(views){return views;}}
 const traffic={phase:'ew_green',time:0,cars:[],pedestrians:[],remaining:5,density:'normal'};
 const props={agents,events:[],preview:true,immersive:true,controller:{current:null},onSelect:id=>selected.push(id),onCameraModeChange:(...value)=>modes.push(value),...overrides};
 const modules=new Map();
 function loadSource(file){file=path.resolve(file);if(modules.has(file))return modules.get(file);const out={};modules.set(file,out);const text=fs.readFileSync(file,'utf8'),code=ts.transpileModule(text,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;vm.runInNewContext(code,{exports:out,require:id=>resolve(id,file),console,Float32Array,performance:{now:()=>now},document:{hidden:false,querySelector:()=>null,createElement:element},requestAnimationFrame:callback=>{nextFrame=callback;return++frameId;},cancelAnimationFrame(){nextFrame=null;}},{filename:file});return out;}
 function resolve(id,file){
  if(id==='react')return React;if(id==='react/jsx-runtime')return{jsx:(type,props)=>({type,props}),jsxs:(type,props)=>({type,props})};
  if(id==='../graphics/officeRenderer')return{OfficeRenderer:Renderer,color:h=>[(h>>16&255)/255,(h>>8&255)/255,(h&255)/255]};
  if(id==='../graphics/officeWorld')return{makeOffice:()=>world,drawActor:a=>[part('actor:'+a.id,a.position[0],a.position[2],1)],palette:{developer:0x54b2cc,reviewer:0xda91b0}};
  if(id==='../graphics/departmentOffice')return{officeFloorOptions:()=>[{floor:1,label:'1 楼'}]};
  if(id==='../office/behavior')return{OfficeBehavior:Behavior};if(id==='../office/etiquette')return{OfficeEtiquette:Etiquette};if(id==='../office/meetings')return{MeetingStaging:Meetings};
  if(id==='../graphics/cityWorld')return{makeCity:()=>({parts:[part('background-building',20,-20,8,{layer:'environment'})],landmarks:[],lamps:[]}),drawCity:()=>[part('city-car',15,12,.5,{layer:'environment'})]};
  if(id==='../graphics/cityAssets')return{PARKED_CARS:[]};
  if(id==='../office/ambientEnvironment')return{environment:{configure:()=>traffic,preferences:{motion:true},pendingSeconds:0,running:true}};
  if(id==='../office/useOfficeFeed')return{useOfficeFeed:()=>({connected:true,packet:null,refresh(){}})};
  assert.ok(id.startsWith('.'),'Unexpected external dependency '+id);return loadSource(path.resolve(path.dirname(file),id+'.ts'));
 }
 const Office=loadSource('frontend/src/components/Office.tsx').default;
 function render(){cursor=0;effects=[];return Office(props);}
 render();const scene=effects.find(effect=>effect.deps?.length===2&&typeof effect.deps[0]==='string'&&effect.deps[0].includes(':'));assert.ok(scene);cleanup=scene.fn();
 return{props,world,agents,modes,selected,host,canvas,renderer,nodes,render(patch={}){Object.assign(props,patch);const tree=render(),current=effects.find(effect=>effect.deps?.length===2&&typeof effect.deps[0]==='string'&&effect.deps[0].includes(':'));assert.deepEqual(current.deps,scene.deps,'unrelated props must not rebuild the Office scene');return tree;},poses(update){poses=update(poses);},step(time,position){now=time;if(position)poses=poses.map(p=>p.id==='a'?{...p,position:[...position]}:p);assert.ok(nextFrame,'production animation callback is scheduled');nextFrame(time);},get focus(){return props.controller.current;},close(){cleanup?.();}};
}

test('production focus keeps a moving member in view after the initial 550ms transition',()=>{
 const h=harness();try{h.step(1000);h.focus.focus('a',true);h.step(1600);assert.ok(Math.abs(h.renderer.target[0])<.01);
  h.step(2200,[7,0,4]);h.step(2800,[7,0,4]);
  const gap=Math.hypot(h.renderer.target[0]-7,h.renderer.target[2]-4);
  assert.ok(gap<.65,`camera is ${gap.toFixed(2)} units behind the selected member after movement`);
  assert.ok(h.renderer.dynamicParts.some(p=>p.tag==='actor:a'));
 }finally{h.close();}
});

test('production employee focus preserves surrounding teams and the living environment',()=>{
 const h=harness();try{h.step(1000);h.focus.focus('a',true);h.step(1600);
  assert.ok(h.renderer.staticParts.some(p=>p.tag==='neighbor-desk'),'neighboring desks must not be removed by a focus radius');
  assert.ok(h.renderer.staticParts.some(p=>p.tag==='background-building'),'background world must remain present');
  assert.ok(h.renderer.dynamicParts.some(p=>p.tag==='actor:b'),'other real members remain renderable');
  assert.ok(h.renderer.dynamicParts.some(p=>p.tag==='city-car'),'city motion continues while an employee is selected');
 }finally{h.close();}
});

test('dragging the production canvas explicitly exits employee following',()=>{
 const h=harness();try{h.step(1000);h.focus.focus('a',true);h.step(1600);
  h.canvas.dispatch('pointerdown',{button:0,pointerId:1,clientX:300,clientY:300});h.canvas.dispatch('pointermove',{pointerId:1,clientX:345,clientY:320});h.canvas.dispatch('pointerup',{pointerId:1,clientX:345,clientY:320});
  h.step(2200,[7,0,4]);assert.equal(h.host.dataset.focusedAgent,'','manual orbit must exit tracking, not retain invisible radius clipping');
  assert.ok(h.renderer.dynamicParts.some(p=>p.tag==='actor:b'));
 }finally{h.close();}
});

test('zooming the production canvas explicitly exits following at any zoom level',()=>{
 const h=harness();try{h.step(1000);h.focus.focus('a',true);h.step(1600);h.focus.zoom(1.1);h.step(2200,[7,0,4]);
  assert.equal(h.host.dataset.focusedAgent,'','zooming above the old 1.5 threshold must still exit tracking');
  const manual=plain(h.renderer.target);h.step(2800,[9,0,5]);assert.deepEqual(plain(h.renderer.target),manual);
 }finally{h.close();}
});

test('camera interaction never changes the actual employee or physical layout inputs',()=>{
 const h=harness(),before=plain({agents:h.agents,places:h.world.agents,parts:h.world.parts});try{h.step(1000);h.focus.focus('a',true);h.step(1600);h.step(2200,[7,0,4]);h.focus.reset();h.step(2800);
  assert.deepEqual(plain({agents:h.agents,places:h.world.agents,parts:h.world.parts}),before);assert.deepEqual(h.selected,[]);
 }finally{h.close();}
});

test('camera mode callback reports real transitions, not each animation or live-prop refresh',()=>{
 const h=harness();try{
  assert.deepEqual(h.modes,[['overview']]);h.step(1000);h.focus.focus('a',true);h.step(1600);h.step(2200,[7,0,4]);h.render();h.step(2800);
  assert.deepEqual(h.modes,[['overview'],['following','a']]);assert.equal(h.host.dataset.cameraMode,'following');
  h.canvas.dispatch('wheel',{deltaY:-20});h.step(3400);h.canvas.dispatch('wheel',{deltaY:20});h.step(4000);
  assert.deepEqual(h.modes,[['overview'],['following','a'],['manual',undefined]]);assert.equal(h.renderer.focusPoint,null);
  h.focus.focus('b',true);h.step(4600);h.canvas.dispatch('keydown',{key:'Home'});h.step(5200);
  assert.deepEqual(h.modes.slice(-2),[['following','b'],['overview',undefined]]);assert.equal(h.host.dataset.cameraMode,'overview');
 }finally{h.close();}
});

test('updated UI insets reach the next production frame without rebuilding or losing follow',()=>{
 const h=harness(),insets={left:260,right:390,top:120,bottom:180};try{
  h.step(1000);h.focus.focus('a',true);h.step(1600);const controller=h.focus,staticParts=h.renderer.staticParts;
  h.render({cameraInsets:insets});h.step(2200,[7,0,4]);h.step(2800);
  assert.equal(h.focus,controller);assert.equal(h.renderer.staticParts,staticParts);assert.equal(h.renderer.cameraInsets,insets);assert.equal(h.host.dataset.focusedAgent,'a');
  assert.ok(Math.abs(h.renderer.target[0]-7)<.01);assert.deepEqual(h.modes,[['overview'],['following','a']]);
 }finally{h.close();}
});

test('a focus requested before initial poses waits for the live actor, and hidden actors stop follow',()=>{
 const h=harness();try{
  h.focus.focus('a',true);h.step(1000);h.step(1600);assert.equal(h.host.dataset.focusedAgent,'a');
  h.poses(poses=>poses.map(p=>p.id==='a'?{...p,visible:false}:p));h.step(2200);assert.equal(h.host.dataset.cameraMode,'manual');assert.equal(h.renderer.focusPoint,null);
  h.poses(poses=>poses.filter(p=>p.id!=='a'));h.step(2800);assert.equal(h.host.dataset.focusedAgent,'');
 }finally{h.close();}
});

test('the local reset button works without any external controller prop',()=>{
 const h=harness({immersive:false,controller:undefined});try{
  h.step(1000);h.canvas.dispatch('wheel',{deltaY:-100});h.step(1600);assert.equal(h.host.dataset.cameraMode,'manual');
  const find=node=>node&&typeof node==='object'&&(node.type==='button'&&node.props.children==='复位视角'?node:(Array.isArray(node.props?.children)?node.props.children:[node.props?.children]).map(find).find(Boolean));
  const reset=find(h.render());assert.ok(reset);reset.props.onClick();h.step(2200);assert.equal(h.host.dataset.cameraMode,'overview');assert.equal(h.renderer.yaw,1.4);assert.equal(h.renderer.pitch,.58);
 }finally{h.close();}
});
