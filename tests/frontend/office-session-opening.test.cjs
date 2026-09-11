const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{spawnSync}=require('node:child_process');
const {createOfficeReactFixture}=require('./helpers/office-react-layout.cjs');
const root=path.resolve(__dirname,'../..'),python=process.env.FORGE_LAYOUT_PYTHON||path.join(root,'.venv/bin/python'),chrome=process.env.CHROMIUM_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const options={skip:fs.existsSync(python)&&fs.existsSync(chrome)?false:'requires local Playwright Python and Chrome; no dependency is downloaded'};
const artifactDir=process.env.FORGE_LAYOUT_ARTIFACT_DIR;
if(artifactDir)assert.ok(path.isAbsolute(artifactDir)&&fs.statSync(artifactDir).isDirectory(),'artifact directory must already exist and be absolute');
let result;
if(!options.skip){
 const script=String.raw`
import json,sys
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright
payload=json.load(sys.stdin)
with sync_playwright() as pw:
    browser=pw.chromium.launch(executable_path=payload['chrome'],headless=True,args=['--disable-background-networking','--disable-component-update'])
    context=browser.new_context(viewport={'width':1400,'height':880},reduced_motion='reduce')
    blocked=[]
    def route(request):
        url=urlparse(request.request.url)
        resource=payload['resources'].get(url.path) if url.netloc=='forge-layout.invalid' else None
        if resource: request.fulfill(status=200,content_type=resource['type'],body=resource['body'])
        else:
            blocked.append(url.path)
            request.abort()
    context.route('**/*',route)
    page=context.new_page()
    page.set_default_timeout(10000)
    errors=[]
    page.on('pageerror',lambda error: errors.append(str(error)))
    page.goto('https://forge-layout.invalid/',wait_until='load')
    page.locator('.studio-member').filter(has_text='Nova').click()
    page.get_by_role('button',name='打开选中员工会话',exact=True).click()
    page.get_by_role('region',name='Nova的任务会话浮窗',exact=True).wait_for()
    def settle():
        page.evaluate('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
    def measure():
        settle()
        return {'window':page.locator('.office-session-window').bounding_box(),'workspace':page.locator('.studio-workspace').bounding_box(),'member':page.locator('.office-session-window').get_attribute('aria-label')}
    def screenshot(name):
        if payload.get('artifactDir'): page.screenshot(path=payload['artifactDir']+'/'+name+'.png')
    opened=measure()
    screenshot('session-first-open-1400')
    page.set_viewport_size({'width':980,'height':700})
    resized=measure()
    page.set_viewport_size({'width':1400,'height':880})
    before_drag=measure()
    handle=page.locator('.office-window-handle').bounding_box()
    page.mouse.move(handle['x']+80,handle['y']+20)
    page.mouse.down()
    page.mouse.move(handle['x']+117,handle['y']+49,steps=3)
    page.mouse.up()
    dragged=measure()
    page.set_viewport_size({'width':1200,'height':840})
    dragged_resized=measure()
    page.get_by_role('button',name='关闭员工会话浮窗',exact=True).click()
    page.locator('.office-session-window').wait_for(state='detached')
    page.get_by_role('button',name='打开选中员工会话',exact=True).click()
    reopened=measure()
    screenshot('session-reopened-1200')
    handle=page.locator('.office-window-handle').bounding_box()
    page.mouse.move(handle['x']+80,handle['y']+20)
    page.mouse.down()
    page.mouse.move(handle['x']+117,handle['y']+49,steps=3)
    page.mouse.up()
    settle()
    page.locator('.studio-member').filter(has_text='Echo').click()
    switched_closed=page.locator('.office-session-window').count()==0
    page.get_by_role('button',name='打开选中员工会话',exact=True).click()
    switched=measure()
    page.set_viewport_size({'width':390,'height':640})
    narrow=measure()
    screenshot('session-echo-narrow-390')
    result={'opened':opened,'resized':resized,'beforeDrag':before_drag,'dragged':dragged,'draggedResized':dragged_resized,'reopened':reopened,'switchedClosed':switched_closed,'switched':switched,'narrow':narrow,'errors':errors,'blocked':blocked,'calls':page.evaluate('window.__fixtureCalls')}
    browser.close()
    print(json.dumps(result))
`;
 const run=spawnSync(python,['-c',script],{input:JSON.stringify({chrome,resources:createOfficeReactFixture(),artifactDir}),encoding:'utf8',timeout:45000,maxBuffer:4*1024*1024});
 assert.equal(run.status,0,`real React session harness failed: ${run.error?.message||run.stderr}`);
 result=JSON.parse(run.stdout);
 if(artifactDir)fs.writeFileSync(path.join(artifactDir,'session-opening-geometry.json'),JSON.stringify(result,null,2)+'\n');
}
const close=(actual,expected,label)=>assert.ok(Math.abs(actual-expected)<=1,`${label}: ${actual}, expected ${expected} ±1 CSS px`);
const centered=(m,label)=>{close(m.window.x+m.window.width/2,m.workspace.x+m.workspace.width/2,label+' horizontal center');close(m.window.y+m.window.height/2,m.workspace.y+m.workspace.height/2,label+' vertical center');};
test('actual StudioWorkspace session click opens OfficeSessionWindow at both viewport centers',options,()=>{
 centered(result.opened,'first open');assert.equal(result.opened.member,'Nova的任务会话浮窗');
});
test('untouched real session remains centered when viewport resizes through breakpoints',options,()=>{
 centered(result.resized,'resized');centered(result.narrow,'narrow');assert.ok(result.narrow.window.x>=11);assert.ok(result.narrow.window.x+result.narrow.window.width<=result.narrow.workspace.width-11);
});
test('actual pointer drag has no initial jump and retains chosen position across a safe resize',options,()=>{
 close(result.dragged.window.x,result.beforeDrag.window.x+37,'drag delta x');close(result.dragged.window.y,result.beforeDrag.window.y+29,'drag delta y');
 close(result.draggedResized.window.x,result.dragged.window.x,'dragged resize x');close(result.draggedResized.window.y,result.dragged.window.y,'dragged resize y');
});
test('closing and reopening through real workspace handlers restores viewport center',options,()=>{
 centered(result.reopened,'reopened');assert.equal(result.reopened.member,'Nova的任务会话浮窗');
});
test('switching real roster member discards previous employee window position',options,()=>{
 assert.equal(result.switchedClosed,true);assert.equal(result.switched.member,'Echo的任务会话浮窗');centered(result.switched,'switched member');
});
test('session opening and movement does not trigger API, external navigation or JS errors',options,()=>{
 assert.deepEqual(result.errors,[]);assert.deepEqual(result.blocked,[]);assert.deepEqual(result.calls,[]);
});
