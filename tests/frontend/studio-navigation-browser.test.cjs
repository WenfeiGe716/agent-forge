const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{spawnSync}=require('node:child_process');
const {createOfficeReactFixture}=require('./helpers/office-react-layout.cjs');
const root=path.resolve(__dirname,'../..'),python=process.env.FORGE_LAYOUT_PYTHON||path.join(root,'.venv/bin/python'),chrome=process.env.CHROMIUM_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const options={skip:fs.existsSync(python)&&fs.existsSync(chrome)?false:'requires local Playwright Python and Chrome; no dependency is downloaded'};
let result;
if(!options.skip){
 const script=String.raw`
import json,sys
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright
payload=json.load(sys.stdin)
with sync_playwright() as pw:
    browser=pw.chromium.launch(executable_path=payload['chrome'],headless=True,args=['--disable-background-networking','--disable-component-update'])
    context=browser.new_context(viewport={'width':1920,'height':900},reduced_motion='reduce')
    def route(request):
        url=urlparse(request.request.url); resource=payload['resources'].get(url.path) if url.netloc=='forge-layout.invalid' else None
        if resource: request.fulfill(status=200,content_type=resource['type'],body=resource['body'])
        else: request.abort()
    context.route('**/*',route); page=context.new_page(); page.set_default_timeout(10000); errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto('https://forge-layout.invalid/',wait_until='load'); page.locator('.studio-navigation').wait_for()
    names=['办公室','任务','楼层与部门','团队','项目','模型','角色模板','Skill商店','设计中心','工具与权限','任务看板']
    def inspect(width):
        page.set_viewport_size({'width':width,'height':900});page.evaluate('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
        return page.evaluate('''names=>{
          const nav=document.querySelector('.studio-navigation'), header=document.querySelector('.studio-header'), end=document.querySelector('.studio-header-end');
          const visible=[...nav.children].filter(x=>x.tagName==='BUTTON').map(x=>x.querySelector('span')?.textContent||'');
          const more=nav.querySelector('.studio-navigation-more'), summary=more?.querySelector(':scope > summary');
          const menu=[...(more?.querySelectorAll(':scope > div button')||[])].map(x=>x.querySelector('span')?.textContent||'');
          const nr=nav.getBoundingClientRect(),er=end.getBoundingClientRect();
          const overlap=[...nav.children].filter(x=>x.tagName==='BUTTON').some(x=>{const r=x.getBoundingClientRect();return r.right>er.left+1||r.left<nr.left-1});
          const mr=summary?.getBoundingClientRect();
          return {visible,menu,hasMore:!!more,overlap,nav:{left:nr.left,right:nr.right},end:{left:er.left},menuRect:mr?{left:mr.left,right:mr.right}:null};
        }''',names)
    snapshots={str(w):inspect(w) for w in [1920,1440,1024,760]}
    narrow=snapshots['760']
    if narrow['hasMore']:
        page.locator('.studio-navigation-more:not(.studio-navigation-more-probe) > summary').click();page.evaluate('new Promise(resolve=>requestAnimationFrame(resolve))')
        menu_rect=page.evaluate('''()=>{const r=document.querySelector('.studio-navigation-more > div').getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height}}''')
    else: menu_rect=None
    snapshots['760-open']=dict(narrow,menuRectOpen=menu_rect)
    back=snapshots['1920-back']=inspect(1920)
    result={'snapshots':snapshots,'names':names,'errors':errors,'calls':page.evaluate('window.__fixtureCalls')}
    browser.close();print(json.dumps(result))
`;
 const run=spawnSync(python,['-c',script],{input:JSON.stringify({chrome,resources:createOfficeReactFixture()}),encoding:'utf8',timeout:60000,maxBuffer:4*1024*1024});
 assert.equal(run.status,0,`real StudioNavigation harness failed: ${run.error?.message||run.stderr}`);result=JSON.parse(run.stdout);
}
test('real navigation fits wide header and has no header-end overlap',options,()=>{for(const width of [1920,1440,1024,760]){const s=result.snapshots[String(width)];assert.equal(s.overlap,false,`navigation overlaps header controls at ${width}px`);}assert.deepEqual(result.snapshots['1920'].visible,result.names);assert.deepEqual(result.snapshots['1920-back'].visible,result.names);});
test('real navigation overflow preserves every entry exactly once and stays in viewport',options,()=>{const s=result.snapshots['760-open'];const labels=[...s.visible,...s.menu];assert.equal(new Set(labels).size,labels.length);assert.deepEqual([...labels].sort(),[...result.names].sort());if(s.menuRectOpen){assert.ok(s.menuRectOpen.left>=0&&s.menuRectOpen.right<=Number(s.nav.right)+1,'更多菜单 must remain inside viewport');}});
test('real navigation fixture makes no external calls or page errors',options,()=>{assert.deepEqual(result.errors,[]);assert.deepEqual(result.calls,[]);});
