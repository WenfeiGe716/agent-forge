/** Actual EmployeeTaskPanel + React in isolated, network-blocked Chrome.
 * Task records are synthetic stress inputs, not evidence of real Agent execution.
 * Does not attach to the Mac application, call a model, or mutate user data. */
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{spawnSync}=require('node:child_process');
const root=path.resolve(__dirname,'../..'),ts=require(path.join(root,'frontend/vendor/typescript.cjs'));
const python=process.env.FORGE_LAYOUT_PYTHON||path.join(root,'.venv/bin/python'),chrome=process.env.CHROMIUM_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const available=fs.existsSync(python)&&fs.existsSync(chrome),artifactDir=process.env.FORGE_LAYOUT_ARTIFACT_DIR;
if(artifactDir)assert.ok(path.isAbsolute(artifactDir)&&fs.statSync(artifactDir).isDirectory());
const build=fs.readFileSync(path.join(root,'scripts/build-offline.mjs'),'utf8'),cssFiles=build.match(/const combinedCSS=\[([^\]]+)\]/)[1].match(/'([^']+\.css)'/g).map(s=>s.slice(1,-1));
assert.ok(cssFiles.includes('employee-task-density.css'),'density styles must be included in the production bundle');
const css=cssFiles.map(file=>fs.readFileSync(path.join(root,'frontend/src',file),'utf8')).join('\n');
const sources={};
for(const relative of ['components/EmployeeTaskPanel.tsx','components/RunRecovery.tsx','components/RunIssues.tsx','components/Icons.tsx','types.ts'])sources['/'+relative.replace(/\.tsx?$/,'')]=ts.transpileModule(fs.readFileSync(path.join(root,'frontend/src',relative),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;
const main=`import {React,ReactDOM,JSX} from '/vendor/react-runtime.js';
const sources=${JSON.stringify(sources)},cache={};window.__calls=[];
const long='包含连续长文件路径_/ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789。',title=long.repeat(8),request=long.repeat(150)+'END_OF_REQUEST';
const events=Array.from({length:100},(_,i)=>({id:i+1,run_id:'density',kind:'agent.message',created_at:'2026-09-11T01:00:00Z',data:{from_agent_id:'b',to_agent_id:'a',body:long.repeat(150)+'END_OF_MESSAGE_'+i}}));
const tasks=Array.from({length:100},(_,i)=>({id:'task-'+i,key:'work-'+i,agent_id:'a',title:'阶段 '+i+' '+title,status:i<98?'integrated':'running',kind:'autonomous',spec:{description:long.repeat(100)+'END_OF_STAGE'},result:{summary:long.repeat(100),outcome:i<98?'done':null}}));
const run={id:'density',title,request,status:'running',updated_at:'2026-09-11',project_id:'project',agent_ids:['a','b'],snapshot:{agents:{a:{name:'Atlas'},b:{name:long}},entry_agent_id:'a'},tasks};
const api=async url=>{window.__calls.push(url);if(url.includes('/collaboration'))return{run_id:'density',decisions:[{id:'pending-question',digest:'fixture',run_id:'density',agent_id:'a',kind:'question',status:'pending'}]};if(url.includes('/trace?')){const limit=Number(new URL(url,'https://density.invalid').searchParams.get('limit'));return{items:events.slice(-limit),has_more:limit<events.length};}return run;};
const stubs={'/api':{api,jsonText:v=>JSON.stringify(v,null,2)},'/components/UI':{Badge:p=>React.createElement('span',{className:'badge '+p.state},p.state==='running'?'执行中':'已提交')},'/components/TaskApprovals':{__esModule:true,default:()=>null},'/components/CodexRuntimeApprovals':{__esModule:true,default:()=>null},'/components/ExecutionCenter':{__esModule:true,default:()=>null},'/components/Collaboration':{Question:()=>React.createElement('div',{'data-pending-question':'yes'},'真实审批组件另有交互测试')}};
function load(id){if(id==='react')return React;if(id==='react/jsx-runtime')return JSX;if(id.endsWith('.css'))return{};if(stubs[id])return stubs[id];if(cache[id])return cache[id];if(!sources[id])throw Error('missing module '+id);const exports=cache[id]={};new Function('exports','require',sources[id])(exports,request=>load(request.startsWith('.')?new URL(request,'https://density.invalid'+id).pathname:request));return exports;}
const forbidden=()=>{throw Error('stress layout fixture must not mutate application state');};
ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(load('/components/EmployeeTaskPanel').default,{agent:{id:'a',name:'Atlas'},runs:[run],detail:null,projects:[{id:'project',name:long.repeat(30)}],writer:false,admin:false,onOpenRun:forbidden,onNavigate:forbidden,onRefresh:()=>{}}));`;
const routes={
 '/':{type:'text/html',body:'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><link rel="stylesheet" href="/styles.css"><body><main class="studio-workspace"><aside class="studio-inspector task-inspector glass" style="position:relative;top:0;right:0;max-height:none"><div class="inspector-body" id="root"></div></aside></main><script type="module" src="/main.js"></script></body></html>'},
 '/styles.css':{type:'text/css',body:css},'/main.js':{type:'text/javascript',body:main},'/vendor/react-runtime.js':{type:'text/javascript',body:fs.readFileSync(path.join(root,'frontend/vendor/react-runtime.js'),'utf8')},
};
let measurements=[];
if(available){
 const script=String.raw`
import json,sys
from playwright.sync_api import sync_playwright
payload=json.load(sys.stdin)
with sync_playwright() as pw:
    browser=pw.chromium.launch(executable_path=payload['chrome'],headless=True,args=['--disable-background-networking','--disable-component-update'])
    context=browser.new_context(viewport={'width':700,'height':900},reduced_motion='reduce')
    def route(req):
        from urllib.parse import urlparse
        asset=payload['routes'].get(urlparse(req.request.url).path)
        if asset and urlparse(req.request.url).hostname=='density.invalid':req.fulfill(status=200,content_type=asset['type'],body=asset['body'])
        else:req.abort()
    context.route('**/*',route)
    results=[]
    for width in [280,312,390]:
        page=context.new_page()
        errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto('https://density.invalid/')
        page.locator('.employee-task-hero').wait_for()
        page.locator('.studio-inspector').evaluate('(el,width)=>el.style.width=width+"px"',width)
        compact=page.evaluate('''()=>{
          const hero=document.querySelector('.employee-task-hero'),title=document.querySelector('.employee-task-title'),panel=document.querySelector('.employee-task-panel');
          return {height:hero.getBoundingClientRect().height,titleHeight:title.getBoundingClientRect().height,lineHeight:parseFloat(getComputedStyle(title).lineHeight),metricFont:getComputedStyle(document.querySelector('.employee-task-metrics strong')).fontSize,children:[...hero.children].map(el=>({className:el.className,height:el.getBoundingClientRect().height,margin:getComputedStyle(el).margin})),scrollWidth:panel.scrollWidth,clientWidth:panel.clientWidth,initialCards:document.querySelectorAll('.employee-work-card').length,firstStage:document.querySelector('.employee-stage-title>b').textContent,approvalVisible:!!document.querySelector('.employee-approval-alert')};
        }''')
        if payload.get('artifactDir') and width==312:
            page.locator('.studio-inspector').screenshot(path=payload['artifactDir']+'/employee-long-summary.png')
        page.locator('.employee-run-switch>summary').click()
        picker=page.locator('.employee-run-picker').evaluate('''el=>{const p=document.querySelector('.employee-task-panel').getBoundingClientRect(),r=el.getBoundingClientRect();return {left:r.left,right:r.right,panelLeft:p.left,panelRight:p.right};}''')
        page.locator('.employee-run-switch>summary').click()
        page.locator('.employee-request-details>summary').click()
        request=page.locator('.employee-request-details>.employee-long-content').evaluate('''el=>{el.scrollTop=el.scrollHeight;return {height:el.clientHeight,scrollHeight:el.scrollHeight,scrollTop:el.scrollTop,complete:el.textContent.includes('END_OF_REQUEST'),overflowX:el.scrollWidth-el.clientWidth};}''')
        page.locator('.employee-request-details>summary').click()
        page.locator('.employee-work-card>summary').first.click()
        stage=page.locator('.employee-stage-details').first.evaluate('''el=>({height:el.clientHeight,scrollHeight:el.scrollHeight,complete:el.textContent.includes('END_OF_STAGE'),overflowX:el.scrollWidth-el.clientWidth})''')
        page.get_by_role('button',name='再显示 12 项分工',exact=True).click()
        page.wait_for_function('document.querySelectorAll(".employee-work-card").length===24')
        expanded=page.locator('.employee-work-card').count()
        workList=page.locator('.employee-work-items').evaluate('el=>({height:el.clientHeight,scrollHeight:el.scrollHeight,overflow:getComputedStyle(el).overflowY})')
        page.get_by_label('筛选员工分工').select_option('active')
        page.wait_for_function('document.querySelectorAll(".employee-work-card").length===2')
        active=page.locator('.employee-work-card').count()
        page.get_by_role('button',name='通信',exact=True).click()
        messages=page.locator('.employee-messages').evaluate('el=>({height:el.clientHeight,scrollHeight:el.scrollHeight,count:el.querySelectorAll(":scope>details").length,open:el.querySelectorAll(":scope>details[open]").length,overflowX:el.scrollWidth-el.clientWidth})')
        page.locator('.employee-messages>details>summary').first.click()
        body=page.locator('.employee-message-content').first.evaluate('el=>({height:el.clientHeight,scrollHeight:el.scrollHeight,complete:el.textContent.includes("END_OF_MESSAGE_99"),overflowX:el.scrollWidth-el.clientWidth})')
        page.get_by_role('button',name='查看更多本任务通信',exact=True).click()
        page.wait_for_function('document.querySelectorAll(".employee-messages>details").length===100')
        results.append({'width':width,'compact':compact,'picker':picker,'request':request,'stage':stage,'expanded':expanded,'workList':workList,'active':active,'messages':messages,'body':body,'loadedMessages':page.locator('.employee-messages>details').count(),'errors':errors})
        page.close()
    browser.close()
    print(json.dumps(results))
`;
 const result=spawnSync(python,['-c',script],{input:JSON.stringify({chrome,routes,artifactDir}),encoding:'utf8',timeout:45000,maxBuffer:5*1024*1024});
 assert.equal(result.status,0,`actual component browser check failed: ${result.error?.message||result.stderr}`);measurements=JSON.parse(result.stdout);
}
const options={skip:available?false:'requires existing local Playwright Python + Chrome; does not install dependencies'};
test('actual employee long-title summary stays compact with narrow inspector widths',options,()=>{
 for(const m of measurements){assert.deepEqual(m.errors,[]);assert.ok(m.compact.height<=220,JSON.stringify(m));assert.ok(m.compact.titleHeight<=m.compact.lineHeight*2+1);assert.ok(m.compact.scrollWidth<=m.compact.clientWidth+1);assert.equal(m.compact.approvalVisible,true);assert.ok(m.picker.left>=m.picker.panelLeft-1&&m.picker.right<=m.picker.panelRight+1);}
});
test('full request and stage text remain available in bounded keyboard-scrollable regions',options,()=>{
 for(const m of measurements)for(const region of [m.request,m.stage]){assert.ok(region.complete);assert.ok(region.height<=261);assert.ok(region.scrollHeight>region.height);assert.ok(region.overflowX<=1);}
});
test('hundreds of work stages use progressive rendering, active-first order and local scroll',options,()=>{
 for(const m of measurements){assert.equal(m.compact.initialCards,12);assert.match(m.compact.firstStage,/阶段 98/);assert.equal(m.expanded,24);assert.equal(m.active,2);assert.ok(m.workList.height<=331);assert.ok(m.workList.scrollHeight>m.workList.height);assert.equal(m.workList.overflow,'auto');}
});
test('hundreds of long communication entries stay collapsed until requested without losing their full bodies',options,()=>{
 for(const m of measurements){assert.equal(m.messages.count,60);assert.equal(m.messages.open,0);assert.ok(m.messages.height<=331);assert.ok(m.messages.scrollHeight>m.messages.height);assert.ok(m.messages.overflowX<=1);assert.ok(m.body.complete);assert.ok(m.body.height<=241);assert.ok(m.body.scrollHeight>m.body.height);assert.ok(m.body.overflowX<=1);assert.equal(m.loadedMessages,100);}
});
