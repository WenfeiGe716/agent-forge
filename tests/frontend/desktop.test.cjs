const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const ts=require('../../frontend/vendor/typescript.cjs');
function load(window){const exports={};const source=ts.transpileModule(fs.readFileSync('frontend/src/desktop.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;vm.runInNewContext(source,{exports,...(window?{window}:{})});return exports;}
test('web and SSR have no native bridge; desktop exposes only the provided narrow bridge',()=>{
 assert.equal(load().desktopBridge(),undefined);assert.equal(load({}).desktopBridge(),undefined);
 const bridge={platform:'darwin'};assert.equal(load({forgeDesktop:bridge}).desktopBridge(),bridge);
});
test('notifications require real status transitions and do not notify historical initial load',()=>{
 const {taskNotifications}=load();const runs=[{id:'a',title:'Task',status:'completed'}];
 assert.equal(taskNotifications(new Map(),runs).length,0);
 assert.equal(taskNotifications(new Map([['a','completed']]),runs).length,0);
 assert.equal(taskNotifications(new Map([['a','running']]),runs).length,1);
 assert.equal(taskNotifications(new Map([['a','running']]),[{...runs[0],status:'verifying'}]).length,0);
});
test('desktop build emits an external script distribution separate from web standalone',()=>{
 const build=fs.readFileSync('scripts/build-offline.mjs','utf8');assert.match(build,/dist-desktop/);
 assert.match(build,/<script src="\/app.js" defer>/);assert.match(build,/path.join\(desktopOut,'app.js'\),bundled/);
});
