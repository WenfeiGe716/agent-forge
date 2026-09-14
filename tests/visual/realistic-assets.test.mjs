import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {dirname,join} from 'node:path';

const root=dirname(dirname(dirname(fileURLToPath(import.meta.url))));
const manifest=JSON.parse(await readFile(join(root,'frontend/public/assets/realistic/manifest.json'),'utf8'));
assert.equal(manifest.license,'CC0 1.0');
assert.equal(manifest.offlineOnly,true);
assert.equal(manifest.assets.length,8);
assert.ok(manifest.preferredOfficeAssets.includes('metal_office_desk'));
assert.ok(!manifest.preferredOfficeAssets.includes('modern_arm_chair_01'));
assert.equal(manifest.assets.find(asset=>asset.id==='modern_arm_chair_01').notForWorkstations,true);
assert.deepEqual(manifest.pilotOnlyAssets,['sofa_02','street_lamp_02']);
assert.ok(manifest.preferredOfficeAssets.includes('metal_office_desk'));
assert.ok(!manifest.preferredOfficeAssets.includes('modern_arm_chair_01'));
for(const file of manifest.files){
 const bytes=await readFile(join(root,file.path));
 assert.equal(bytes.byteLength,file.size,`size changed: ${file.path}`);
 assert.equal(createHash('sha256').update(bytes).digest('hex'),file.sha256,`sha256 changed: ${file.path}`);
}
const chairRoot=join(root,'frontend/public/assets/realistic/office_task_chair/nisu');
const chairManifest=JSON.parse(await readFile(join(chairRoot,'manifest.json'),'utf8'));
assert.equal(chairManifest.license,'CC0 1.0');
assert.equal(chairManifest.format,'FBX');
assert.ok(chairManifest.features.includes('five-star caster base'));
for(const file of chairManifest.files){
 const bytes=await readFile(join(chairRoot,file.path));
 assert.equal(createHash('sha256').update(bytes).digest('hex'),file.sha256,`chair checksum changed: ${file.path}`);
}
console.log(`PASS realistic asset manifest (${manifest.files.length} files, ${manifest.files.reduce((n,f)=>n+f.size,0)} bytes)`);
