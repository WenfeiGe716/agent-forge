const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{spawnSync}=require('node:child_process');
const root=path.resolve(__dirname,'../..'),ts=require(path.join(root,'frontend/vendor/typescript.cjs'));
const studioSource=fs.readFileSync(path.join(root,'frontend/src/components/StudioWorkspace.tsx'),'utf8');
const python=process.env.FORGE_LAYOUT_PYTHON||path.join(root,'.venv/bin/python'),chrome=process.env.CHROMIUM_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const available=fs.existsSync(python)&&fs.existsSync(chrome);
const build=fs.readFileSync(path.join(root,'scripts/build-offline.mjs'),'utf8');
const cssFiles=build.match(/const combinedCSS=\[([^\]]+)\]/)[1].match(/'([^']+\.css)'/g).map(s=>s.slice(1,-1));
const css=cssFiles.map(file=>fs.readFileSync(path.join(root,'frontend/src',file),'utf8')).join('\n');
const sources={};
for(const relative of ['components/LiveOutputDock.tsx','components/TranscriptMarkdown.tsx','liveTranscript.ts','components/Icons.tsx','types.ts']){
 sources['/'+relative.replace(/\.tsx?$/,'')]=ts.transpileModule(fs.readFileSync(path.join(root,'frontend/src',relative),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;
}
const main=`import {React,ReactDOM,JSX} from '/vendor/react-runtime.js';
const sources=${JSON.stringify(sources)},cache={};
const stubs={'/api':{api:async()=>({})},'/components/UI':{Badge:p=>React.createElement('span',null,p.state)}};
function load(id){if(id==='react')return React;if(id==='react/jsx-runtime')return JSX;if(stubs[id])return stubs[id];if(cache[id])return cache[id];const exports=cache[id]={};new Function('exports','require',sources[id])(exports,request=>load(request.startsWith('.')?new URL(request,'https://fixture.invalid'+id).pathname:request));return exports;}
const agents=[{id:'nova',name:'Nova',runtime:{}},{id:'echo',name:'Echo',runtime:{}}];
const run={id:'run-1',title:'帮我设计一个射击小游戏',request:'请继续完成任务并保持每位成员都有独立的实时输出会话。',agent_ids:['nova','echo'],snapshot:{agents:{nova:{name:'Nova'},echo:{name:'Echo'}}},tasks:[{id:'task-child',key:'child-1',agent_id:'nova',title:'前端验证与布局修复',status:'running',spec:{assignment_type:'subagent'}},{id:'task-done',key:'child-2',agent_id:'echo',title:'检查子智能体处理',status:'completed',spec:{assignment_type:'subagent'}}]};
const event=(id,agent,task,kind,text)=>({id,run_id:'run-1',kind,created_at:'2026-09-23T02:10:00Z',data:{agent_id:agent,task_key:task,text}});
const events=[event(1,'nova','child-1','model.delta','正在读取项目文件并分析布局'),event(2,'nova','child-1','turn/completed',''),event(3,'echo','child-2','model.delta','已完成检查')];
ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(load('/components/LiveOutputDock').default,{run,events,agents,open:true,onClose:()=>{}}));`;
const resources={'/':{type:'text/html',body:'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/styles.css"><body><div class="studio-workspace" style="position:relative;width:100vw;height:100vh"><div id="root"></div></div><script type="module" src="/main.js"></script></body></html>'},'/styles.css':{type:'text/css',body:css},'/main.js':{type:'text/javascript',body:main},'/vendor/react-runtime.js':{type:'text/javascript',body:fs.readFileSync(path.join(root,'frontend/vendor/react-runtime.js'),'utf8')}};
let result;
if(available){
 const script=String.raw`import json,sys
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright
p=json.load(sys.stdin)
with sync_playwright() as pw:
 b=pw.chromium.launch(executable_path=p['chrome'],headless=True,args=['--disable-background-networking','--disable-component-update'])
 c=b.new_context(viewport={'width':1400,'height':880},reduced_motion='reduce'); page=c.new_page(); errors=[]; page.on('pageerror',lambda e:errors.append(str(e)))
 def route(r):
  u=urlparse(r.request.url); x=p['resources'].get(u.path)
  if x:r.fulfill(status=200,content_type=x['type'],body=x['body'])
  else:r.abort()
 c.route('**/*',route); page.goto('https://fixture.invalid/',wait_until='load'); page.locator('.live-output-dock').wait_for()
 page.wait_for_timeout(100)
 page.screenshot(path='/tmp/live-output-layout.png',full_page=True)
 result=page.evaluate('''()=>{const r=e=>{const x=e.getBoundingClientRect();return {left:x.left,right:x.right,top:x.top,bottom:x.bottom,width:x.width,height:x.height}};const d=document.querySelector('.live-output-dock'),main=document.querySelector('.live-output-primary>main'),primary=document.querySelector('.live-output-primary'),composer=document.querySelector('.live-output-composer'),side=document.querySelector('.live-output-subagents'),field=document.querySelector('.live-output-compose-field'),send=field.querySelector('button');return {dock:r(d),main:r(main),primary:r(primary),composer:r(composer),side:r(side),field:r(field),textarea:r(field.querySelector('textarea')),recipient:r(field.querySelector('.live-output-recipient')),send:r(send),bodyWidth:document.documentElement.scrollWidth,viewport:innerWidth,header:r(d.querySelector('header'))}}'''); result['errors']=errors
 child_header=page.locator('.live-subagent-list button').first
 child_header.click(); page.wait_for_timeout(50)
 result['childTitle']=page.locator('.live-output-task-head strong').inner_text()
 result['childPlaceholder']=page.locator('textarea[aria-label="补充指令"]').get_attribute('placeholder')
 result['subagentHeader']=page.locator('.live-output-subagents>header b').inner_text()
 result['selectedMemberCount']=page.locator('.live-output-members button.selected').count()
 page.set_viewport_size({'width':640,'height':880}); page.wait_for_timeout(50)
 result['narrow']=page.evaluate('''()=>{const r=e=>{const x=e.getBoundingClientRect();return {left:x.left,right:x.right,top:x.top,bottom:x.bottom,width:x.width,height:x.height}};return {bodyWidth:document.documentElement.scrollWidth,main:r(document.querySelector('.live-output-primary>main')),side:r(document.querySelector('.live-output-subagents'))}}''')
 page.screenshot(path='/tmp/live-output-layout-child.png',full_page=True)
 b.close(); print(json.dumps(result))`;
 const runResult=spawnSync(python,['-c',script],{input:JSON.stringify({chrome,resources}),encoding:'utf8',timeout:60000,maxBuffer:2*1024*1024});
 assert.equal(runResult.status,0,runResult.stderr||runResult.error?.message); result=JSON.parse(runResult.stdout);
}
test('live output keeps the conversation readable beside the subagent panel', {skip:available?false:'requires local Playwright Python and Chrome'},()=>{
 assert.deepEqual(result.errors,[]); assert.ok(result.main.width>=700,JSON.stringify(result)); assert.ok(result.side.width<=400,JSON.stringify(result)); assert.ok(result.bodyWidth<=result.viewport+1,JSON.stringify(result)); assert.ok(result.dock.bottom>=879,JSON.stringify(result)); assert.ok(result.composer.left>=result.primary.left-1&&result.composer.right<=result.primary.right+1,JSON.stringify(result)); assert.ok(result.send.left>=result.field.left&&result.send.right<=result.field.right,JSON.stringify(result)); assert.ok(result.textarea.width>300,JSON.stringify(result)); assert.equal(result.subagentHeader,'子智能体'); assert.equal(result.childTitle,'前端验证与布局修复'); assert.match(result.childPlaceholder,/Nova/); assert.equal(result.selectedMemberCount,1); assert.ok(result.narrow.bodyWidth<=640); assert.ok(result.narrow.side.top>=result.narrow.main.bottom-1);
});
test('live output stays closed until the user opens it',()=>{
 assert.doesNotMatch(studioSource,/setLiveOutputOpen\(true\)/);
 assert.match(studioSource,/onClick=\{\(\)=>setLiveOutputOpen\(v=>!v\)\}/);
});
