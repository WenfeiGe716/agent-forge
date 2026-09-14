/** A licensed, clothed, rigged human for asset acceptance; not yet an employee
 * replacement. Work/seating gestures still require contact retargeting. */
import {THREE,FBXLoader} from './realisticRuntime';
// Default to the professional business outfit; callers may still pass any
// pinned Rocketbox variant (for example the matching business male avatar).
const base='/assets/realistic/people/rocketbox_business_female_01/';
/** Load one pinned Rocketbox variant. The caller chooses identity, while this
 * module keeps URL restrictions, texture remapping and animation setup deep. */
export async function loadRocketboxPilot(assetBase=base, modelFile='Business_Female_01.fbx'){
 const sourceBase=assetBase.endsWith('/')?assetBase:assetBase+'/';
 const manager=new THREE.LoadingManager();
 manager.addHandler(/\.tga$/i,new THREE.TextureLoader(manager));
 manager.setURLModifier((url:string)=>{
  const name=url.replaceAll('\\','/').split('/').at(-1)!;
  if(/^[fm]\d+_[a-z0-9_]+\.tga$/i.test(name))return sourceBase+name.replace(/\.tga$/i,'.png');
  const parsed=new URL(url,location.href);if(parsed.origin!==location.origin||!parsed.pathname.startsWith(sourceBase))throw Error('人物模型只允许使用本地素材');return parsed.href;
 });
 const loader=new FBXLoader(manager),[model,animation]=await Promise.all([loader.loadAsync(sourceBase+modelFile),loader.loadAsync(sourceBase+'animations/f_idle_breathe_01.fbx')]);
 model.scale.setScalar(.01);model.updateMatrixWorld(true);
 const bounds=new THREE.Box3().setFromObject(model),center=bounds.getCenter(new THREE.Vector3());
 const root=new THREE.Group();root.add(model);model.position.x-=center.x;model.position.y-=bounds.min.y;model.position.z-=center.z;
 model.traverse((o:any)=>{if(o.isMesh){o.castShadow=true;o.receiveShadow=true;for(const m of Array.isArray(o.material)?o.material:[o.material]){
   // Rocketbox FBX stores a zero DiffuseColor while the actual appearance is
   // carried by the texture. Three's FBXLoader preserves that zero, which
   // multiplies every sampled texel to black. Restore a neutral base so the
   // authored color/normal/specular maps remain visible.
   if(m.color?.setHex)m.color.setHex(0xffffff);
   if(m.map){m.map.colorSpace=THREE.SRGBColorSpace;m.map.needsUpdate=true;}
   if(m.normalMap)m.normalMap.needsUpdate=true;
   if(m.alphaMap){m.alphaTest=.4;m.transparent=false;m.depthWrite=true;m.side=THREE.DoubleSide;}
   m.needsUpdate=true;
 }} });
 const mixer=new THREE.AnimationMixer(model),clip=animation.animations[0];if(!clip)throw Error('人物样板缺少待机动画');
 mixer.clipAction(clip).play();
 // Only the character skeleton is animated; temporary source animation meshes
 // are not placed in the scene or used as invented task state.
 const disposeObject=(obj:any)=>{const textures=new Set<any>(),materials=new Set<any>(),geometry=new Set<any>();obj.traverse((o:any)=>{if(o.geometry)geometry.add(o.geometry);o.skeleton?.dispose();for(const m of o.material?(Array.isArray(o.material)?o.material:[o.material]):[]){materials.add(m);for(const v of Object.values(m))if((v as any)?.isTexture)textures.add(v);}});for(const t of textures){t.dispose();t.source?.data?.close?.();}for(const m of materials)m.dispose();for(const g of geometry)g.dispose();};
 disposeObject(animation);
 return{root,model,mixer,clip,dispose(){mixer.stopAllAction();mixer.uncacheRoot(model);disposeObject(model);root.removeFromParent();}};
}
