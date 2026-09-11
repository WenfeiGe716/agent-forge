/** Offline build. Uses the pinned, licensed compiler/runtime shipped in vendor/.
 * This is transpilation plus syntax diagnostics, NOT a full TypeScript type check.
 */
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
import crypto from 'node:crypto';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const require=createRequire(import.meta.url),ts=require(path.join(root,'frontend/vendor/typescript.cjs'));
const source=path.join(root,'frontend/src'),out=path.join(root,'frontend/dist');
fs.rmSync(out,{recursive:true,force:true});fs.mkdirSync(out,{recursive:true});
const files=[],diagnostics=[];
function walk(dir){for(const entry of fs.readdirSync(dir,{withFileTypes:true})){const p=path.join(dir,entry.name);if(entry.isDirectory())walk(p);else if(/\.tsx?$/.test(p))files.push(p);}}
walk(source);
const withPreview=process.argv.includes('--with-preview');
const productionFiles=files.filter(p=>!['Preview.tsx','SuitePreview.tsx','previewV07.ts'].includes(path.basename(p)));
const emittedFiles=withPreview?files:productionFiles;
for(const p of emittedFiles){const relative=path.relative(source,p).replace(/\.tsx?$/,'.js');const text=fs.readFileSync(p,'utf8').replace(/import\s+['"][^'"]+\.css['"];?/g,'');
 const result=ts.transpileModule(text,{fileName:p,reportDiagnostics:true,compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ESNext,jsx:ts.JsxEmit.ReactJSX,isolatedModules:true,sourceMap:true}});
 for(const d of result.diagnostics||[])if(d.category===ts.DiagnosticCategory.Error)diagnostics.push({file:relative,message:ts.flattenDiagnosticMessageText(d.messageText,' ')});
 const code=result.outputText.replace(/(from\s*['"]|import\s*['"])(\.[^'"]+)(['"])/g,(_,a,b,c)=>a+b+(/\.[a-z0-9]+$/i.test(b)?'':'.js')+c);
 const target=path.join(out,relative);fs.mkdirSync(path.dirname(target),{recursive:true});fs.writeFileSync(target,code);if(result.sourceMapText)fs.writeFileSync(target+'.map',result.sourceMapText);
}
if(diagnostics.length){console.error(diagnostics);process.exit(1);}
const combinedCSS=['styles.css','studio.css','design.css','provider-center.css','role-templates.css','execution.css','organization-inventory.css','agent-directory.css','task-workspace.css','task-approvals.css','employee-task.css','task-flow.css','run-board.css','employee-task-density.css','workspace-density.css','team-execution-permissions.css'].map(name=>fs.readFileSync(path.join(source,name),'utf8')).join('\n');
fs.writeFileSync(path.join(out,'styles.css'),combinedCSS);
fs.mkdirSync(path.join(out,'vendor'),{recursive:true});
for(const name of ['react-runtime.js','react.js','react-dom-client.js','jsx-runtime.js','THIRD_PARTY_NOTICES.md'])fs.copyFileSync(path.join(root,'frontend/vendor',name),path.join(out,'vendor',name));
const imports={react:'/vendor/react.js','react-dom/client':'/vendor/react-dom-client.js','react/jsx-runtime':'/vendor/jsx-runtime.js'};
fs.writeFileSync(path.join(out,'index.html'),`<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#111719"><title>Agent Forge · 协作工坊</title><link rel="stylesheet" href="/styles.css"><script type="importmap">${JSON.stringify({imports})}</script></head><body><div id="root"></div><script type="module" src="/main.js"></script></body></html>`);
const result={compiler:ts.version,mode:'offline-transpile-and-browser-validation',semantic_typecheck:false,source_files:emittedFiles.length,syntax_errors:diagnostics.length,react:'18.2.0',output:'frontend/dist'};
fs.mkdirSync(path.join(root,'docs/evidence'),{recursive:true});fs.writeFileSync(path.join(root,'docs/evidence/offline-build.json'),JSON.stringify(result,null,2));console.log(JSON.stringify(result,null,2));
// Also emit a single-file page: no module/package network requests at runtime.
let bundled=fs.readFileSync(path.join(root,'frontend/vendor/react-runtime.js'),'utf8').replace(/export const /g,'const ');
bundled+='\nconst uiFactories={};const uiCache={};\n';
for(const p of emittedFiles){const id=path.relative(source,p).replaceAll('\\','/').replace(/\.tsx?$/,'');const text=fs.readFileSync(p,'utf8').replace(/import\s+['"][^'"]+\.css['"];?/g,'');
 const emitted=ts.transpileModule(text,{fileName:p,compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true}}).outputText;
 bundled+=`uiFactories[${JSON.stringify(id)}]=function(require,module,exports){\n${emitted}\n};\n`;
}
bundled+=`function loadUI(id,from=''){if(id==='react')return React;if(id==='react-dom/client')return ReactDOM;if(id==='react/jsx-runtime')return JSX;if(id.startsWith('.')){const parts=(from.split('/').slice(0,-1).join('/')+'/'+id).split('/'),out=[];for(const p of parts){if(p==='..')out.pop();else if(p&&p!=='.')out.push(p);}id=out.join('/');}if(uiCache[id])return uiCache[id].exports;if(!uiFactories[id])throw Error('Missing UI module '+id);const module={exports:{}};uiCache[id]=module;uiFactories[id](child=>loadUI(child,id),module,module.exports);return module.exports;}loadUI('main');`;
const html=`<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Agent Forge · 协作工坊</title><style>${combinedCSS}</style></head><body><div id="root"></div><script>${bundled.replaceAll('</script','<\\/script')}</script></body></html>`;
fs.writeFileSync(path.join(out,'standalone.html'),html);

fs.writeFileSync(path.join(out,'index.html'),html); // Primary distribution = the same standalone page tested in Chromium.
// Desktop renderer permits no inline scripts or Node APIs. Reuse the exact same
// UI factories as the browser build, but load the code as a local external asset.
const desktopOut=path.join(root,'frontend/dist-desktop');
fs.mkdirSync(desktopOut,{recursive:true});
fs.writeFileSync(path.join(desktopOut,'app.js'),bundled);
fs.writeFileSync(path.join(desktopOut,'styles.css'),combinedCSS);
fs.writeFileSync(path.join(desktopOut,'index.html'),'<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Agent Forge · Mac 协作工坊</title><link rel="stylesheet" href="/styles.css"></head><body><div id="root"></div><script src="/app.js" defer></script></body></html>');
// Previews are opt-in developer artifacts; no fixture factories ship by default.
if(withPreview){
const preview=html.replace("loadUI('main');","loadUI('Preview').mountPreview();").replace('<title>Agent Forge · 协作工坊</title>','<title>Agent Forge · 行为与空闲动作预览</title>');
fs.writeFileSync(path.join(out,'office-preview.html'),preview);
const workbenchPreview=html.replace("loadUI('main');","loadUI('SuitePreview').mountPreview();").replace('<title>Agent Forge · 协作工坊</title>','<title>Agent Forge v0.7 · 完整交互示例（不调用模型）</title>');
fs.writeFileSync(path.join(out,'workbench-preview.html'),workbenchPreview);

}
