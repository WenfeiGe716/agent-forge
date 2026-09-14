/** Vendored Three r180 and its unmodified official loaders, built offline.
 * The same global is embedded in browser and desktop builds before UI factories.
 */
declare const Forge3D:any;
export const THREE=typeof Forge3D==='undefined'?null:Forge3D.THREE;
export const GLTFLoader=typeof Forge3D==='undefined'?null:Forge3D.GLTFLoader;
export const FBXLoader=typeof Forge3D==='undefined'?null:Forge3D.FBXLoader;
export const TGALoader=typeof Forge3D==='undefined'?null:Forge3D.TGALoader;
export const RGBELoader=typeof Forge3D==='undefined'?null:Forge3D.RGBELoader;
export const cloneSkeleton=typeof Forge3D==='undefined'?null:Forge3D.cloneSkeleton;
