const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const ts=require('../../frontend/vendor/typescript.cjs'),loaded=new Map(),plain=value=>JSON.parse(JSON.stringify(value));
function loadSource(filename){
 filename=path.resolve(filename);if(loaded.has(filename))return loaded.get(filename);const exports={};loaded.set(filename,exports);
 const code=ts.transpileModule(fs.readFileSync(filename,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 vm.runInNewContext(code,{exports,console,performance,Float32Array,devicePixelRatio:1,require:id=>{
  assert.ok(id.startsWith('.'),'camera/render test must not add external dependencies');return loadSource(path.resolve(path.dirname(filename),id+'.ts'));
 }},{filename});return exports;
}
const {officeProjection,officeOverview}=loadSource('frontend/src/graphics/officeCamera.ts');
const {transform}=loadSource('frontend/src/graphics/scene.ts');
const {OfficeRenderer}=loadSource('frontend/src/graphics/officeRenderer.ts');
const close=(actual,expected)=>assert.ok(Math.abs(actual-expected)<.002,`${actual} != ${expected}`);
function project(pose,point,w,h,insets,following=true){const {vp}=officeProjection(pose,w,h,insets,following),p=transform(vp,point);return[(p[0]+1)*w/2,(1-p[1])*h/2];}

test('following uses the safe visual center between actual left/right/top/bottom panels',()=>{
 const pose={...officeOverview(),target:[7,1.1,4],zoom:2.15},w=1440,h=900,insets={left:260,right:390,top:120,bottom:180};
 const p=project(pose,pose.target,w,h,insets);close(p[0],(insets.left+w-insets.right)/2);close(p[1],(insets.top+h-insets.bottom)/2);
 const head=project(pose,[7,2.35,4],w,h,insets),feet=project(pose,[7,0,4],w,h,insets);
 for(const q of[head,feet]){assert.ok(q[0]>insets.left&&q[0]<w-insets.right);assert.ok(q[1]>insets.top&&q[1]<h-insets.bottom);}
});

test('narrow safe space protects the whole employee instead of cropping head or feet',()=>{
 const pose={...officeOverview(),target:[0,1.1,0],zoom:6.5},w=700,h=500,insets={left:220,right:330,top:160,bottom:200};
 for(const point of [[0,0,0],[0,2.35,0]]){const q=project(pose,point,w,h,insets);assert.ok(q[0]>insets.left&&q[0]<w-insets.right);assert.ok(q[1]>insets.top&&q[1]<h-insets.bottom,`employee height remains in ${JSON.stringify(q)}`);}
});

test('overview ignores panel insets and keeps the same full-office scale and orientation',()=>{
 const pose=officeOverview(),a=officeProjection(pose,1440,900,undefined,false),b=officeProjection(pose,1440,900,{left:300,right:400,top:100,bottom:180},false);
 assert.deepEqual(plain(a),plain(b));assert.equal(pose.yaw,1.4);assert.equal(pose.pitch,.58);
});

test('invalid or overlapping transient insets always produce a finite usable projection',()=>{
 const pose=officeOverview();for(const insets of[{left:-20,right:NaN,top:Infinity,bottom:-8},{left:900,right:900,top:700,bottom:700}]){
  const {vp}=officeProjection(pose,800,600,insets,true);assert.ok([...vp].every(Number.isFinite));const q=project(pose,pose.target,800,600,insets);assert.ok(q[0]>=0&&q[0]<=800);assert.ok(q[1]>=0&&q[1]<=600);
 }
});

// Use the real renderer draw/projection path. This GL adapter records actual
// buffer uploads, uniforms and passes; pixel/shader compilation QA is separate.
function rendererFixture(instanced=false){
 let seq=0,program=null,framebuffer=null,buffer=null,depth=true;const enabled=new Set(),uniforms=new Map(),draws=[],uploads=[],shaders=[];
 const object=()=>({id:++seq}),state=()=>uniforms.get(program)||{};
 const record=(count,instances=1)=>draws.push({program,framebuffer,buffer,count,instances,depth,blend:enabled.has('BLEND'),uniforms:{...state()}});
 const impl={createBuffer:object,createProgram:object,createTexture:object,createFramebuffer:object,createRenderbuffer:object,
  createShader(type){const s={id:++seq,type};shaders.push(s);return s;},shaderSource(shader,source){shader.source=source;},getShaderParameter:()=>true,getProgramParameter:()=>true,
  getAttribLocation:(_p,name)=>({pos:0,normal:1,rgba:2,material:3,row0:4,row1:5,row2:6}[name]??-1),getUniformLocation:(_p,name)=>name,
  getExtension:name=>name==='ANGLE_instanced_arrays'&&instanced?{vertexAttribDivisorANGLE(){},drawArraysInstancedANGLE(_type,_first,count,instances){record(count,instances);}}:null,
  checkFramebufferStatus:()=> 'FRAMEBUFFER_COMPLETE',bindFramebuffer(_kind,value){framebuffer=value;},bindBuffer(_kind,value){buffer=value;},bufferData(_kind,data,usage){uploads.push({buffer,count:data.length,usage});},
  useProgram(value){program=value;if(!uniforms.has(value))uniforms.set(value,{});},uniformMatrix4fv(name,_transpose,value){state()[name]=[...value];},uniform1f(name,value){state()[name]=value;},uniform1i(name,value){state()[name]=value;},uniform3fv(name,value){state()[name]=[...value];},
  enable(value){enabled.add(value);},disable(value){enabled.delete(value);},depthMask(value){depth=value;},drawArrays(_type,_first,count){record(count);}};
 const gl=new Proxy(impl,{get(target,key){if(key in target)return target[key];return String(key).toUpperCase()===key?key:()=>{};}}),canvas={clientWidth:1440,clientHeight:900,width:0,height:0,getContext:()=>gl};
 const renderer=new OfficeRenderer(canvas),part={p:[1,2,3],s:[2,3,1],color:[.7,.7,.7]};renderer.setStatic([part]);return{renderer,draws,uploads,shaders,part,clear(){draws.length=0;}};
}

for(const instanced of[false,true])test(`actual renderer follows inset projection and fades static foreground only (${instanced?'instanced':'fallback'})`,()=>{
 const h=rendererFixture(instanced),r=h.renderer;try{
  r.target=[7,1.1,4];r.zoom=2.15;r.focusPoint=[7,1.15,4];r.cameraInsets={left:260,right:390,top:120,bottom:180};r.draw([h.part]);
  const p=r.project(r.target);close(p[0],655);close(p[1],420);
  const passes=h.draws.filter(d=>!d.framebuffer&&d.count>0),solid=passes.find(d=>d.uniforms.focusPass===0),faded=passes.find(d=>d.uniforms.focusPass===1),actor=passes.find(d=>d.uniforms.focusPass===-1);
  assert.ok(solid&&faded&&actor);assert.equal(solid.buffer,faded.buffer,'same complete static geometry is reused');assert.equal(solid.count,faded.count);
  assert.equal(solid.depth,true);assert.equal(solid.blend,false);assert.equal(faded.depth,false);assert.equal(faded.blend,true);assert.ok(passes.indexOf(faded)>passes.indexOf(actor),'foreground composites after opaque employees');
  const uploadCount=h.uploads.filter(u=>u.buffer===solid.buffer).length;
  h.clear();r.focusPoint=[9,1.15,5];r.target=[9,1.1,5];r.draw([h.part]);assert.equal(h.uploads.filter(u=>u.buffer===solid.buffer).length,uploadCount,'movement never rebakes/crops static world');
  assert.deepEqual(h.draws.find(d=>!d.framebuffer&&d.uniforms.focusPass===1).uniforms.focusPoint,[9,1.15,5]);
  h.clear();r.focusPoint=null;r.draw([h.part]);assert.ok(h.draws.filter(d=>!d.framebuffer).every(d=>d.uniforms.focusPass===-1),'manual/overview removes all fade passes');
 }finally{r.dispose();}
});

test('foreground shader preserves the ground and areas outside the live sightline',()=>{
 const h=rendererFixture();try{const source=h.shaders.find(s=>s.type==='FRAGMENT_SHADER'&&s.source.includes('foreground')).source;
  assert.match(source,/smoothstep\(\.35,\.85,ahead\)/,'behind-subject surfaces are not faded');assert.match(source,/length\(delta-ray\*ahead\)/,'only the subject sightline is opened');
  assert.match(source,/smoothstep\(\.18,\.65,P.y\)/,'floor stays opaque');assert.match(source,/float alpha=A\*\(1\.-fade\*\.88\)/,'foreground is translucent, not removed from the scene');
 }finally{h.renderer.dispose();}
});
