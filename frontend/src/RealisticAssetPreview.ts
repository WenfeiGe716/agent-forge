/** Developer-only real geometry/material inspection. No tasks, feeds or model calls. */
import {THREE} from './graphics/realisticRuntime';
import {ThreeOfficeRenderer} from './graphics/threeOfficeRenderer';
import {loadRocketboxPilot} from './graphics/rocketboxPilot';
import {loadOfficeTaskChair} from './graphics/officeTaskChair';

export async function mountRealisticPreview(){
 document.body.style.cssText='margin:0;background:#e8e5df;color:#292e31;font-family:system-ui';
 const host=document.getElementById('root')!;
 host.innerHTML='<canvas id="asset-stage" style="width:100vw;height:100vh;display:block"></canvas><header style="position:fixed;top:28px;left:32px;pointer-events:none"><small style="letter-spacing:.18em;color:#727775">TECH WORKSPACE / ASSET STUDY</small><h1 style="font-size:24px;margin:10px 0">科技办公 · 写实资产样板</h1><p style="font-size:13px;color:#6e7474">实际 3D 模型与材质 · 拖动旋转 · 滚轮缩放</p></header><nav style="position:fixed;bottom:24px;left:50%;transform:translateX(-50%);display:flex;gap:8px"></nav><p id="asset-load" style="position:fixed;bottom:24px;right:24px;color:#6e7474;font-size:12px">加载本地素材…</p>';
 const scene=new ThreeOfficeRenderer(document.getElementById('asset-stage') as HTMLCanvasElement);
 scene.setQuality('detail');scene.background=0xe8e5df;scene.target=[0,.72,0];scene.zoom=7.5;scene.yaw=1.1;scene.pitch=.23;
 const report:any={loaded:[],errors:[],frames:0,renderSource:'local-authored-3d-assets',businessCalls:0};
 (window as any).__realism=report;(window as any).__assetScene=scene;
 const floor=new THREE.Mesh(new THREE.PlaneGeometry(200,200),new THREE.MeshStandardMaterial({color:0xd2cec7,roughness:.83}));floor.rotation.x=-Math.PI/2;floor.position.y=-.012;floor.receiveShadow=true;scene.scene.add(floor);
 const wall=new THREE.Mesh(new THREE.BoxGeometry(12,4,.12),new THREE.MeshStandardMaterial({color:0xe3e1dc,roughness:.95}));wall.position.set(0,2,-2);wall.receiveShadow=true;scene.scene.add(wall);
 let person:Awaited<ReturnType<typeof loadRocketboxPilot>>|null=null;
 let officeChair:Awaited<ReturnType<typeof loadOfficeTaskChair>>|null=null;
 const spot=scene.scene.children.find((o:any)=>o.isDirectionalLight);if(spot){spot.position.set(-3,6,4);spot.shadow.camera.left=-5;spot.shadow.camera.right=5;spot.shadow.camera.top=5;spot.shadow.camera.bottom=-5;spot.shadow.normalBias=.015;spot.shadow.bias=-.00005;spot.shadow.camera.updateProjectionMatrix();}
 const load=async(id:string,url:string,position:number[],height?:number)=>{
  const item=await scene.addAsset(id,url);item.updateMatrixWorld(true);
  let box=new THREE.Box3().setFromObject(item),size=box.getSize(new THREE.Vector3());
  const scale=height?height/size.y:1;item.scale.multiplyScalar(scale);item.updateMatrixWorld(true);box=new THREE.Box3().setFromObject(item);const center=box.getCenter(new THREE.Vector3());
  item.position.set(position[0]-center.x,position[1]-box.min.y,position[2]-center.z);item.updateMatrixWorld(true);
  let triangles=0,maps=0,meshes=0;item.traverse((o:any)=>{if(!o.isMesh)return;meshes++;triangles+=(o.geometry.index?.count||o.geometry.attributes.position.count)/3;for(const m of Array.isArray(o.material)?o.material:[o.material])for(const key of ['map','normalMap','roughnessMap','metalnessMap','aoMap'])if(m[key])maps++;});
  report.loaded.push({id,triangles,maps,meshes,size:size.toArray(),position:item.position.toArray()});return item;
 };
 try{
  await scene.loadEnvironment('/assets/realistic/brown_photostudio_03/1k/environment.hdr');
  const textureLoader=new THREE.TextureLoader();const floorMaps=await Promise.all(['diff.jpg','normal.jpg','roughness.jpg'].map(name=>textureLoader.loadAsync('/assets/realistic/wood_floor/1k/'+name)));
  for(const t of floorMaps){t.wrapS=t.wrapT=THREE.RepeatWrapping;t.repeat.set(75,75);t.anisotropy=Math.min(8,scene.renderer.capabilities.getMaxAnisotropy());}floorMaps[0].colorSpace=THREE.SRGBColorSpace;
  floor.material.color.setHex(0xffffff);floor.material.map=floorMaps[0];floor.material.normalMap=floorMaps[1];floor.material.roughnessMap=floorMaps[2];floor.material.normalScale.set(.35,.35);floor.material.needsUpdate=true;
  const [desk,plant,chairAsset]=await Promise.all([
   load('metal_office_desk','/assets/realistic/metal_office_desk/2k/scene.gltf',[0,0,-.42],.75),
   load('potted_plant_02','/assets/realistic/potted_plant_02/1k/scene.gltf',[1.45,0,-.48],1.16),
   loadOfficeTaskChair(),
  ]);
  officeChair=chairAsset;officeChair.root.position.set(-.1,0,.8);scene.scene.add(officeChair.root);
  let chairTriangles=0;officeChair.root.traverse((o:any)=>{if(o.isMesh)chairTriangles+=(o.geometry.index?.count||o.geometry.attributes.position.count)/3;});
  report.loaded.push({id:'office_task_chair_nisu',triangles:chairTriangles,maps:4,purpose:'office-task-chair',height:1.18});
  person=await loadRocketboxPilot();person.root.position.set(-2,0,0);scene.scene.add(person.root);
  report.person={source:'Microsoft Rocketbox',rigged:true,clip:person.clip.name,duration:person.clip.duration,boneCount:0};person.model.traverse((o:any)=>{if(o.isBone)report.person.boneCount++;});
  const setView=(kind:string)=>{desk.visible=plant.visible=kind==='office';officeChair!.root.visible=kind!=='person';person!.root.visible=kind==='person';scene.target=kind==='person'?[-2,.95,0]:kind==='chair'?[-.1,.65,.8]:[.35,.7,0];scene.zoom=kind==='chair'?9:kind==='person'?7.5:5.8;scene.pitch=kind==='person'?.10:kind==='chair'?.14:.28;scene.yaw=kind==='person'?1.45:kind==='chair'?1.08:1.1;report.view=kind;};setView('office');
  for(const [label,value]of [['办公桌椅','office'],['办公椅细节','chair'],['人物与骨骼','person']]){const button=document.createElement('button');button.textContent=label;button.style.cssText='border:1px solid #b8b8b080;background:#ffffffdc;border-radius:18px;padding:10px 20px;color:#373d3c;font-size:13px;cursor:pointer';button.onclick=()=>setView(value);host.querySelector('nav')!.appendChild(button);}
  document.getElementById('asset-load')!.textContent='本地素材已就绪';
 }catch(error){report.errors.push(String(error));document.getElementById('asset-load')!.textContent=String(error);}
 let moving=false,lastX=0,lastY=0;
 scene.canvas.onpointerdown=e=>{moving=true;lastX=e.clientX;lastY=e.clientY;scene.canvas.setPointerCapture(e.pointerId);};
 scene.canvas.onpointermove=e=>{if(!moving)return;scene.yaw-=(e.clientX-lastX)*.006;scene.pitch=Math.max(.08,Math.min(1.3,scene.pitch+(e.clientY-lastY)*.005));lastX=e.clientX;lastY=e.clientY;};
 scene.canvas.onpointerup=()=>moving=false;scene.canvas.onpointercancel=()=>moving=false;
 scene.canvas.addEventListener('wheel',e=>{e.preventDefault();scene.zoom=Math.max(2.5,Math.min(12,scene.zoom*Math.exp(-e.deltaY*.001)));},{passive:false});
 let frame=0,last=performance.now();const paint=()=>{if(scene.disposed)return;const now=performance.now();person?.mixer.update(Math.min(.05,Math.max(0,now-last)/1000));last=now;scene.draw([]);report.frames++;report.stats={...scene.stats};frame=requestAnimationFrame(paint);};paint();
 window.addEventListener('pagehide',()=>{cancelAnimationFrame(frame);person?.dispose();officeChair?.dispose();for(const m of[floor,wall]){m.geometry.dispose();for(const key of['map','normalMap','roughnessMap'])m.material[key]?.dispose();m.material.dispose();}scene.dispose();},{once:true});
}
