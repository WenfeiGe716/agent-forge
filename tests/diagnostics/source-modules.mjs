/** Read-only source loader for visual tests while the running app uses dist.
 * node --import ./tests/diagnostics/source-modules.mjs --test tests/visual/*.mjs
 * The pinned compiler transpiles in memory; no build or runtime file is changed.
 */
import fs from 'node:fs';
import {createRequire,registerHooks} from 'node:module';
import {fileURLToPath,pathToFileURL} from 'node:url';
const require=createRequire(import.meta.url);
const ts=require('../../frontend/vendor/typescript.cjs');
const dist=fileURLToPath(new URL('../../frontend/dist/',import.meta.url));
const src=fileURLToPath(new URL('../../frontend/src/',import.meta.url));
function sourceFor(url) {
  if(!url.startsWith('file:'))return null;
  const path=fileURLToPath(url);
  if(!path.startsWith(dist)||!path.endsWith('.js'))return null;
  const candidate=src+path.slice(dist.length,-3);
  return[candidate+'.ts',candidate+'.tsx'].find(file=>fs.existsSync(file));
}
registerHooks({
  resolve(specifier,context,nextResolve) {
    if(specifier.startsWith('.')&&context.parentURL) {
      const url=new URL(specifier,context.parentURL).href;
      if(sourceFor(url))return{url,shortCircuit:true};
    }
    return nextResolve(specifier,context);
  },
  load(url,context,nextLoad) {
    const source=sourceFor(url);
    if(!source)return nextLoad(url,context);
    const result=ts.transpileModule(fs.readFileSync(source,'utf8'),{fileName:source,compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ESNext,jsx:ts.JsxEmit.ReactJSX}});
    const text=result.outputText.replace(/(from\s*['"]|import\s*['"])(\.[^'"]+)(['"])/g,(_,a,b,c)=>a+b+(/\.[a-z0-9]+$/i.test(b)?'':'.js')+c);
    return{format:'module',source:text+'\n//# sourceURL='+pathToFileURL(source).href,shortCircuit:true};
  },
});
