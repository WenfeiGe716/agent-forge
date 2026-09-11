/** Actual React permission editor, UI dialog and production CSS in isolated Chrome.
 * Only policy data is synthetic. No production requests or permission writes. */
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{spawnSync}=require('node:child_process');
const root=path.resolve(__dirname,'../..'),ts=require(path.join(root,'frontend/vendor/typescript.cjs'));
const python=process.env.FORGE_LAYOUT_PYTHON||path.join(root,'.venv/bin/python'),chrome=process.env.CHROMIUM_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',available=fs.existsSync(python)&&fs.existsSync(chrome);
const artifactDir=process.env.FORGE_LAYOUT_ARTIFACT_DIR;if(artifactDir)assert.ok(path.isAbsolute(artifactDir)&&fs.statSync(artifactDir).isDirectory());
const build=fs.readFileSync(path.join(root,'scripts/build-offline.mjs'),'utf8'),cssFiles=build.match(/const combinedCSS=\[([^\]]+)\]/)[1].match(/'([^']+\.css)'/g).map(s=>s.slice(1,-1));
assert.ok(cssFiles.includes('team-execution-permissions.css'),'editor styles must ship in the production bundle');
const css=cssFiles.map(file=>fs.readFileSync(path.join(root,'frontend/src',file),'utf8')).join('\n'),sources={};
for(const relative of ['components/TeamExecutionPermissions.tsx','components/ExecutionCenter.tsx','components/UI.tsx','components/Icons.tsx','types.ts'])sources['/'+relative.replace(/\.tsx?$/,'')]=ts.transpileModule(fs.readFileSync(path.join(root,'frontend/src',relative),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;
const main=`import {React,ReactDOM,JSX} from '/vendor/react-runtime.js';
const sources=${JSON.stringify(sources)},cache={};window.__writes=[];window.__reads=[];
const long='跨部门联合研究_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_',base={permission_profile:null,effective_profile:'ask',inherited_profile:'ask',source:'project',source_id:'p',source_name:long};
const data={version:7,layout_ref:{id:'layout',version:4,digest:'frozen-layout'},project:{id:'p',name:long.repeat(8),permission_profile:'ask',execution_mode:'local'},teams:Array.from({length:25},(_,i)=>({...base,id:'team-'+i,name:'团队 '+i+' '+long.repeat(3),floor:i+1,department:long,member_ids:i===0?Array.from({length:100},(_,j)=>'agent-'+j):[]})),agents:Array.from({length:100},(_,i)=>({...base,id:'agent-'+i,name:'成员 '+i+' '+long.repeat(3),job_title:'专业岗位 '+long,team_id:'team-0',team_name:'团队 0 '+long}))};
const api=async(url,method='GET',body)=>{if(method!=='GET'){window.__writes.push({url,method,body});throw Error('layout-only test must not save permissions');}window.__reads.push(url);return data;};
function load(id){if(id==='react')return React;if(id==='react/jsx-runtime')return JSX;if(id.endsWith('.css'))return{};if(id==='/api')return{api,jsonText:JSON.stringify};if(cache[id])return cache[id];if(!sources[id])throw Error('missing module '+id);const exports=cache[id]={};new Function('exports','require',sources[id])(exports,request=>load(request.startsWith('.')?new URL(request,'https://policy-layout.invalid'+id).pathname:request));return exports;}
const render=ReactDOM.createRoot(document.getElementById('root'));window.mount=(office)=>render.render(React.createElement('div',{className:office?'studio-workspace':''},React.createElement(load('/components/UI').Modal,{title:'执行权限',wide:true,onClose:()=>{}},React.createElement(load('/components/TeamExecutionPermissions').default,{admin:true,projectId:'p',initialTeamId:'team-0',initialAgentId:'agent-0'}))));window.mount(true);`;
const routes={'/':{type:'text/html',body:'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><link rel="stylesheet" href="/styles.css"><body><div id="root"></div><script type="module" src="/main.js"></script></body></html>'},'/styles.css':{type:'text/css',body:css},'/main.js':{type:'text/javascript',body:main},'/vendor/react-runtime.js':{type:'text/javascript',body:fs.readFileSync(path.join(root,'frontend/vendor/react-runtime.js'),'utf8')}};
let measurements=[];
if(available){
const script=String.raw`
import json,sys
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright
payload=json.load(sys.stdin)
with sync_playwright() as pw:
    browser=pw.chromium.launch(executable_path=payload['chrome'],headless=True,args=['--disable-background-networking','--disable-component-update'])
    context=browser.new_context(viewport={'width':1400,'height':880},reduced_motion='reduce')
    def route(req):
        u=urlparse(req.request.url);asset=payload['routes'].get(u.path)
        if u.hostname=='policy-layout.invalid' and asset:req.fulfill(status=200,content_type=asset['type'],body=asset['body'])
        else:req.abort()
    context.route('**/*',route)
    results=[]
    for width,office in [(1400,True),(980,True),(390,True),(1400,False),(390,False)]:
        page=context.new_page();page.set_viewport_size({'width':width,'height':880});errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto('https://policy-layout.invalid/');page.locator('.team-policy-member').first.wait_for();page.evaluate('(office)=>window.mount(office)',office)
        page.locator('.team-policy-member').first.wait_for()
        default=page.evaluate('''()=>{
          const dialog=document.querySelector('dialog'),panel=document.querySelector('.team-execution-permissions'),members=document.querySelector('.team-policy-members'),r=dialog.getBoundingClientRect();
          return {left:r.left,right:r.right,scrollWidth:panel.scrollWidth,clientWidth:panel.clientWidth,memberHeight:members.clientHeight,memberScrollHeight:members.scrollHeight,memberOverflow:getComputedStyle(members).overflowY,focused:!!document.querySelector('.team-policy-member.is-focused'),count:document.querySelectorAll('.team-policy-member').length};
        }''')
        page.get_by_label('搜索权限配置成员').fill('成员 0 ')
        page.wait_for_function('document.querySelectorAll(".team-policy-member").length===1')
        editor=page.locator('[data-policy-scope="agent"][data-policy-id="agent-0"]')
        editor.locator('select').select_option('full')
        risk=editor.locator('.team-policy-risk');risk.scroll_into_view_if_needed()
        bounds=risk.evaluate('''el=>{const check=el.querySelector('input'),button=el.parentElement.querySelector('.team-policy-save>.primary'),r=el.getBoundingClientRect(),b=button.getBoundingClientRect();return {width:r.width,overflow:el.scrollWidth-el.clientWidth,checkWidth:check.getBoundingClientRect().width,checked:check.checked,disabled:button.disabled,buttonWidth:b.width};}''')
        editor.get_by_role('checkbox').check()
        approved=editor.get_by_role('button',name='保存成员权限',exact=True).is_enabled()
        editor.get_by_role('button',name='保存成员权限',exact=True).scroll_into_view_if_needed()
        reachable=editor.get_by_role('button',name='保存成员权限',exact=True).evaluate('''el=>{const r=el.getBoundingClientRect(),p=el.closest('dialog').getBoundingClientRect();return r.left>=p.left&&r.right<=p.right&&r.top>=p.top&&r.bottom<=p.bottom;}''')
        if payload.get('artifactDir') and width==1400 and office:
            page.locator('dialog').screenshot(path=payload['artifactDir']+'/team-execution-permissions.png')
        results.append({'width':width,'office':office,'default':default,'risk':bounds,'approved':approved,'reachable':reachable,'errors':errors,'writes':page.evaluate('window.__writes')})
        page.close()
    browser.close();print(json.dumps(results))
`;
const result=spawnSync(python,['-c',script],{input:JSON.stringify({chrome,routes,artifactDir}),encoding:'utf8',timeout:60000,maxBuffer:5*1024*1024});assert.equal(result.status,0,result.error?.message||result.stderr);measurements=JSON.parse(result.stdout);
}
const options={skip:available?false:'requires existing local Playwright Python + Chrome; no installation performed'};
test('actual permission panel contains long team and member names at desktop and phone widths',options,()=>{
 for(const m of measurements){assert.deepEqual(m.errors,[]);assert.ok(m.default.left>=0&&m.default.right<=m.width+1,JSON.stringify(m));assert.ok(m.default.scrollWidth<=m.default.clientWidth+1,JSON.stringify(m));assert.equal(m.default.count,100);assert.equal(m.default.focused,true);}
});
test('many members remain searchable in a bounded scroll region',options,()=>{
 for(const m of measurements){assert.ok(m.default.memberHeight<=461);assert.ok(m.default.memberScrollHeight>m.default.memberHeight);assert.equal(m.default.memberOverflow,'auto');}
});
test('full access confirmation is visible and not preselected, with zero write requests from layout checks',options,()=>{
 for(const m of measurements){assert.equal(m.risk.checked,false);assert.equal(m.risk.disabled,true);assert.equal(m.approved,true);assert.equal(m.reachable,true);assert.ok(m.risk.overflow<=1);assert.ok(m.risk.checkWidth>=14&&m.risk.checkWidth<=18);assert.ok(m.risk.buttonWidth>50);assert.deepEqual(m.writes,[]);}
});
