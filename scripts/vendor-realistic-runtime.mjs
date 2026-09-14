// Run only when updating the pinned vendor, not during an ordinary offline build.
// Usage: node scripts/vendor-realistic-runtime.mjs /absolute/temporary/node_modules
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const modules=path.resolve(process.argv[2]||'node_modules');
const require=createRequire(import.meta.url);
const esbuild=require(path.join(modules,'esbuild'));
const three=JSON.parse(fs.readFileSync(path.join(modules,'three/package.json'),'utf8'));
if(three.version!=='0.180.0'||esbuild.version!=='0.25.10')throw Error('Expected Three 0.180.0 / esbuild 0.25.10');
const output=path.join(root,'frontend/vendor/realistic-runtime.js');
await esbuild.build({entryPoints:[path.join(root,'scripts/realistic-runtime-entry.mjs')],nodePaths:[modules],bundle:true,format:'iife',globalName:'Forge3D',target:'es2022',platform:'browser',minify:true,legalComments:'inline',outfile:output});
fs.copyFileSync(path.join(modules,'three/LICENSE'),path.join(root,'frontend/vendor/THREE-LICENSE.txt'));
fs.writeFileSync(path.join(root,'frontend/vendor/realistic-runtime.lock.json'),JSON.stringify({three:'0.180.0',esbuild:'0.25.10',source:'https://github.com/mrdoob/three.js/tree/r180',license:'MIT',sha256:createHash('sha256').update(fs.readFileSync(output)).digest('hex')},null,2)+'\n');
