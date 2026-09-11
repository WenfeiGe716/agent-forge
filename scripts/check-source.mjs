/** Syntax-only check. This is NOT a replacement for npm run build or dependency type-checks. */
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const require=createRequire(path.join(root,'frontend/package.json'));
let ts;
try { ts=require(process.env.TYPESCRIPT_MODULE || path.join(root,'frontend/vendor/typescript.cjs')); }
catch { console.error('Install frontend dependencies first, or set TYPESCRIPT_MODULE to an installed TypeScript module.'); process.exit(1); }
const files=[];
function walk(p){for(const n of fs.readdirSync(p)){const f=path.join(p,n);if(fs.statSync(f).isDirectory())walk(f);else if(/\.tsx?$/.test(f))files.push(f);}}
walk(path.join(root,'frontend/src'));
const errors=[];
for(const file of files){
 const result=ts.transpileModule(fs.readFileSync(file,'utf8'),{fileName:file,compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ESNext,jsx:ts.JsxEmit.ReactJSX,isolatedModules:true},reportDiagnostics:true});
 for(const d of result.diagnostics||[])if(d.category===ts.DiagnosticCategory.Error)errors.push(path.relative(root,file)+': '+ts.flattenDiagnosticMessageText(d.messageText,'\n'));
}
console.log(JSON.stringify({check:'TS/TSX syntax only',typescript:ts.version,files:files.length,errors},null,2));
if(errors.length)process.exit(1);
