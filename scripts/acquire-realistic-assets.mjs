#!/usr/bin/env node
/**
 * Download a small, reproducible CC0 asset pack from Poly Haven.
 *
 * The script intentionally uses the official files API and records both the
 * API-provided md5 and the local sha256. Runtime code can therefore remain
 * offline and never fetches a CDN. Run from repository root:
 *   node scripts/acquire-realistic-assets.mjs
 */
import {createHash} from 'node:crypto';
import {mkdir,readFile,writeFile,stat} from 'node:fs/promises';
import {dirname,join,relative} from 'node:path';
import {fileURLToPath} from 'node:url';

const root=dirname(dirname(fileURLToPath(import.meta.url)));
const outputRoot=join(root,'frontend/public/assets/realistic');
const api='https://api.polyhaven.com/files/';
const source='https://polyhaven.com/';
// Modern, compact assets chosen for the first pilot. Textures are deliberately
// 2k for the hero seating asset and 1k for repeated environment props.
const assets=[
 // sofa_02 is retained as a compatibility sample; its catalog styling is
 // vintage, so it is not selected for the modern office.
 {id:'sofa_02',kind:'gltf',resolution:'2k',role:'lounge-sofa',pilotOnly:true},
 {id:'modern_arm_chair_01',kind:'gltf',resolution:'2k',role:'modern-lounge-chair',notForWorkstations:true},
 {id:'metal_office_desk',kind:'gltf',resolution:'2k',role:'office-desk'},
 {id:'potted_plant_02',kind:'gltf',resolution:'1k',role:'office-floor-plant'},
 {id:'potted_plant_04',kind:'gltf',resolution:'1k',role:'office-plant'},
 {id:'street_lamp_02',kind:'gltf',resolution:'1k',role:'wall-mounted-lantern',pilotOnly:true},
 {id:'brown_photostudio_03',kind:'hdri',resolution:'1k',role:'indoor-daylight'},
 {id:'wood_floor',kind:'texture',resolution:'1k',role:'neutral-wood-floor'},
];
const fetchJson=async url=>{const response=await fetch(url,{signal:AbortSignal.timeout(120000)});if(!response.ok)throw Error(`${response.status} ${response.statusText}: ${url}`);return response.json();};
const digest=async bytes=>({md5:createHash('md5').update(bytes).digest('hex'),sha256:createHash('sha256').update(bytes).digest('hex'),size:bytes.byteLength});
async function download(url,path,expectedMd5){const existing=await readFile(path).catch(()=>null);if(existing){const d=await digest(existing);if(!expectedMd5||d.md5===expectedMd5)return {...d,reused:true};}
 const response=await fetch(url,{signal:AbortSignal.timeout(120000)});if(!response.ok)throw Error(`${response.status} ${response.statusText}: ${url}`);const bytes=new Uint8Array(await response.arrayBuffer());const d=await digest(bytes);if(expectedMd5&&d.md5!==expectedMd5)throw Error(`md5 mismatch for ${url}: expected ${expectedMd5}, got ${d.md5}`);await mkdir(dirname(path),{recursive:true});await writeFile(path,bytes);return {...d,reused:false};}
const files=[];
const assetCatalog=await fetchJson('https://api.polyhaven.com/assets');
for(const asset of assets){
 const catalog=await fetchJson(api+asset.id);const info=assetCatalog?.[asset.id]||{};const base=join(outputRoot,asset.id,asset.resolution);let entries=[];
 if(asset.kind==='gltf'){const bundle=catalog?.gltf?.[asset.resolution]?.gltf;if(!bundle?.url||!bundle.include)throw Error(`No GLTF bundle for ${asset.id} at ${asset.resolution}`);entries=[['scene.gltf',bundle],...Object.entries(bundle.include)];}
 else if(asset.kind==='hdri'){const meta=catalog?.hdri?.[asset.resolution]?.hdr;if(!meta)throw Error(`No HDRI ${asset.id} at ${asset.resolution}`);entries=[['environment.hdr',meta]];}
 else if(asset.kind==='texture'){for(const [channel,name] of [['Diffuse','diff.jpg'],['nor_gl','normal.jpg'],['Rough','roughness.jpg']]){const meta=catalog?.[channel]?.[asset.resolution]?.jpg;if(!meta)throw Error(`No ${channel} texture ${asset.id} at ${asset.resolution}`);entries.push([name,meta]);}}
 for(const [name,meta] of entries){const local=join(base,name);const d=await download(meta.url,local,meta.md5);files.push({asset:asset.id,kind:asset.kind,resolution:asset.resolution,role:asset.role,authors:info.authors||{},path:relative(root,local),url:meta.url,md5:meta.md5,...d});}
}
const manifest={schema:1,generatedAt:new Date().toISOString(),license:'CC0 1.0',licenseUrl:'https://polyhaven.com/license',sourceApi:api,sourceSite:source,offlineOnly:true,assets,files,preferredOfficeAssets:assets.filter(asset=>!asset.pilotOnly&&!asset.notForWorkstations).map(asset=>asset.id),pilotOnlyAssets:assets.filter(asset=>asset.pilotOnly).map(asset=>asset.id)};
await writeFile(join(outputRoot,'manifest.json'),JSON.stringify(manifest,null,2)+'\n');
console.log(JSON.stringify({assets:assets.length,files:files.length,totalBytes:files.reduce((n,f)=>n+f.size,0),manifest:relative(root,join(outputRoot,'manifest.json'))},null,2));
