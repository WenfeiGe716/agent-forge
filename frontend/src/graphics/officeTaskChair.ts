/** A real, licensed task chair; the source FBX uses an obsolete TIFF reference.
 * Keep that authoring quirk and the PBR map setup behind this asset interface. */
import {THREE,FBXLoader} from './realisticRuntime';
const base='/assets/realistic/office_task_chair/nisu/';
export async function loadOfficeTaskChair(){
 const manager=new THREE.LoadingManager();
 manager.setURLModifier((url:string)=>{
  const name=url.replaceAll('\\','/').split('/').at(-1)!;
  if(name==='Mat_Color_91.tif')return base+'Office_Chair_Base_Color.png';
  const parsed=new URL(url,location.href);
  if(parsed.origin!==location.origin||!parsed.pathname.startsWith(base))throw Error('办公椅只允许使用本地素材');
  return parsed.href;
 });
 manager.addHandler(/\.tif$/i,new THREE.TextureLoader(manager));
 const textureLoader=new THREE.TextureLoader(manager);
 const [root,...maps]=await Promise.all([
  new FBXLoader(manager).loadAsync(base+'Office_Chair.fbx'),
  ...['Base_Color','Normal','Roughness','Metallic'].map(name=>textureLoader.loadAsync(base+'Office_Chair_'+name+'.png')),
 ]);
 maps[0].colorSpace=THREE.SRGBColorSpace;
 const material=new THREE.MeshStandardMaterial({map:maps[0],normalMap:maps[1],roughnessMap:maps[2],metalnessMap:maps[3],roughness:1,metalness:1});
 const originalMaterials=new Set<any>(),originalTextures=new Set<any>();
 root.traverse((o:any)=>{if(!o.isMesh)return;for(const m of Array.isArray(o.material)?o.material:[o.material]){originalMaterials.add(m);for(const v of Object.values(m))if((v as any)?.isTexture)originalTextures.add(v);}o.material=material;o.castShadow=true;o.receiveShadow=true;});
 for(const m of originalMaterials)m.dispose();for(const t of originalTextures)t.dispose();
 root.updateMatrixWorld(true);
 const bounds=new THREE.Box3().setFromObject(root),size=bounds.getSize(new THREE.Vector3());
 // One uniform conversion, never stretch a lounge chair into office proportions.
 root.scale.multiplyScalar(1.18/size.y);root.updateMatrixWorld(true);
 const normalized=new THREE.Box3().setFromObject(root),center=normalized.getCenter(new THREE.Vector3());
 const group=new THREE.Group();group.add(root);root.position.sub(new THREE.Vector3(center.x,normalized.min.y,center.z));
 group.name='ergonomic-office-chair';group.userData={source:'nisu / OpenGameArt',license:'CC0',purpose:'office-task-chair'};
 return{root:group,dispose(){group.removeFromParent();const geometries=new Set<any>();root.traverse((o:any)=>{if(o.geometry)geometries.add(o.geometry);});for(const g of geometries)g.dispose();material.dispose();for(const t of maps)t.dispose();}};
}
