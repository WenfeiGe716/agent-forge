/** Actual React + RunBoard + production CSS, rendered in network-blocked Chrome.
 * Task records here are layout stress data, never evidence of model execution.
 * No live workspace, production API, task creation, or app profile is touched.
 */
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{spawnSync}=require('node:child_process');
const root=path.resolve(__dirname,'../..'),ts=require(path.join(root,'frontend/vendor/typescript.cjs'));
const python=process.env.FORGE_LAYOUT_PYTHON||path.join(root,'.venv/bin/python'),chrome=process.env.CHROMIUM_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const options={skip:fs.existsSync(python)&&fs.existsSync(chrome)?false:'requires existing Playwright Python and Chrome; downloads nothing'};
const artifactDir=process.env.FORGE_LAYOUT_ARTIFACT_DIR;
if(artifactDir)assert.ok(path.isAbsolute(artifactDir)&&fs.statSync(artifactDir).isDirectory(),'artifact directory must already exist and be absolute');
const sources={};
for(const relative of ['components/RunBoard.tsx','components/UI.tsx','components/Icons.tsx','components/RunRecovery.tsx','types.ts'])sources['/'+relative.replace(/\.tsx?$/,'')]=ts.transpileModule(fs.readFileSync(path.join(root,'frontend/src',relative),'utf8').replace(/import\s+['"][^'"]+\.css['"];?/g,''),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;
const build=fs.readFileSync(path.join(root,'scripts/build-offline.mjs'),'utf8'),cssFiles=build.match(/const combinedCSS=\[([^\]]+)\]/)[1].match(/'([^']+\.css)'/g).map(s=>s.slice(1,-1));
assert.ok(cssFiles.includes('run-board.css'),'new layout must ship in desktop and browser builds');
const css=cssFiles.map(file=>fs.readFileSync(path.join(root,'frontend/src',file),'utf8')).join('\n');
const main=`import {React,ReactDOM,JSX} from '/vendor/react-runtime.js';
const sources=${JSON.stringify(sources)},cache={};
function load(id){if(id==='react')return React;if(id==='react/jsx-runtime')return JSX;if(cache[id])return cache[id];if(!sources[id])throw Error('unknown module '+id);const exports=cache[id]={};new Function('exports','require',sources[id])(exports,request=>load(request.startsWith('.')?new URL(request,'https://forge-board-layout.invalid'+id).pathname:request));return exports;}
const statuses=['running','completed','needs_attention','queued','awaiting_acceptance','future_status'];
const long='真实执行任务长标题文档 requirements.md 与 tag_normalizer.normalize_tags ' + 'VeryLongUnbrokenProjectIdentifier_'.repeat(28);
const record=i=>({id:'layout-'+i,project_id:i%2?'project-b':'project-a',title:'布局任务'+i+' '+long,request:'布局任务'+i+' '+long+' 请核对原始文件与运行结果，完整需求仅在详情查看。',status:i<103?'running':statuses[i%statuses.length],calls:i,input_tokens:0,output_tokens:0,created_at:'2026-09-11T03:00:00Z',updated_at:new Date(Date.UTC(2026,8,11,3,0,i)).toISOString(),error:i>=103&&statuses[i%statuses.length]==='needs_attention'?'并行变更产生冲突，布局验证用长错误详情'.repeat(20):''});
const small=[record(0),{...record(1),status:'needs_attention',error:'并行变更产生冲突，布局验证用长错误详情'.repeat(20)},{...record(2),status:'completed'}];
const many=Array.from({length:200},(_,i)=>record(i));
const projects=[{id:'project-a',name:'项目目录与非常长名称 '+long},{id:'project-b',name:'第二个项目'}];
const root=ReactDOM.createRoot(document.getElementById('root'));window.__boardCalls=[];
window.__setScenario=name=>root.render(React.createElement(load('/components/RunBoard').default,{key:name,runs:name==='many'?many:small,projects,onSelect:id=>window.__boardCalls.push({select:id}),onCreate:()=>window.__boardCalls.push({create:true})}));
window.__setScenario('small');`;
const resources={
 '/':{type:'text/html',body:'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/styles.css"><body><main class="board-layout-shell"><h1>任务看板</h1><div id="root"></div><p class="layout-boundary">真实组件布局压测 · 仅测试数据 · 不调用模型或业务 API</p></main><script type="module" src="/main.js"></script></body></html>'},
 '/styles.css':{type:'text/css',body:css+'\n.board-layout-shell{margin:0 0 0 221px;padding:30px 32px;min-width:0}.board-layout-shell h1{font-size:25px;margin:0 0 22px}.layout-boundary{font-size:11px;color:#698176;margin-top:15px}@media(max-width:800px){.board-layout-shell{margin:0;padding:20px}}'},
 '/main.js':{type:'text/javascript',body:main},
 '/vendor/react-runtime.js':{type:'text/javascript',body:fs.readFileSync(path.join(root,'frontend/vendor/react-runtime.js'),'utf8')},
};
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
        resource=payload['resources'].get(url.path) if url.netloc=='forge-board-layout.invalid' else None
        if resource: request.fulfill(status=200,content_type=resource['type'],body=resource['body'])
        else:
            blocked.append(url.path)
            request.abort()
    context.route('**/*',route)
    page=context.new_page()
    page.set_default_timeout(10000)
    errors=[]
    page.on('pageerror',lambda error: errors.append(str(error)))
    page.goto('https://forge-board-layout.invalid/',wait_until='load')
    page.locator('.board-card').first.wait_for()
    def settle(): page.evaluate('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
    def measure(view,width):
        page.set_viewport_size({'width':width,'height':880})
        page.get_by_role('button',name='看板' if view=='board' else '列表',exact=True).click()
        settle()
        return page.evaluate('''()=>{
          const rect=el=>{const r=el.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom};};
          return {width:innerWidth,bodyWidth:document.documentElement.scrollWidth,board:rect(document.querySelector('.task-board')),cards:[...document.querySelectorAll('.board-card')].map(el=>({rect:rect(el),clientWidth:el.clientWidth,scrollWidth:el.scrollWidth,title:el.querySelector('h3').title,titleHeight:el.querySelector('h3').getBoundingClientRect().height,titleLineHeight:parseFloat(getComputedStyle(el.querySelector('h3')).lineHeight),duplicateSummary:!!el.querySelector('.board-card-copy>p')})),columns:[...document.querySelectorAll('.board-column')].map(el=>({rect:rect(el),scrollHeight:el.querySelector('.board-column-scroll').scrollHeight,clientHeight:el.querySelector('.board-column-scroll').clientHeight,overflow:getComputedStyle(el.querySelector('.board-column-scroll')).overflowY})),controls:[...document.querySelectorAll('.board-toolbar input,.board-toolbar select,.board-toolbar button')].map(el=>rect(el))};
        }''')
    def screenshot(name):
        if payload.get('artifactDir'): page.screenshot(path=payload['artifactDir']+'/'+name+'.png')
    sizes=[]
    for width in [1400,980,740,390]:
        for view in ['board','list']:
            sizes.append({'view':view,**measure(view,width)})
            if width in [1400,390]: screenshot('run-'+view+'-long-'+str(width))
    page.set_viewport_size({'width':1400,'height':880})
    page.evaluate("window.__setScenario('many')")
    page.wait_for_function("document.querySelector('.board-result-count strong')?.textContent==='200'")
    initial_board_count=page.locator('.board-card').count()
    header_counts=page.locator('.board-column-count').all_text_contents()
    stress_board=measure('board',1400)
    screenshot('run-board-200')
    for index in range(page.locator('.board-column').count()):
        column=page.locator('.board-column').nth(index)
        while column.locator('.board-load-more').count(): column.locator('.board-load-more').click()
    board_ids=page.locator('.board-card').evaluate_all('(els)=>els.map(el=>el.dataset.runId)')
    page.get_by_role('button',name='列表',exact=True).click()
    list_initial_count=page.locator('.board-card').count()
    screenshot('run-list-200')
    while page.locator('.board-load-more').count(): page.locator('.board-load-more').click()
    list_ids=page.locator('.board-card').evaluate_all('(els)=>els.map(el=>el.dataset.runId)')
    page.get_by_label('搜索看板任务').fill('布局任务0 ')
    search_ids=page.locator('.board-card').evaluate_all('(els)=>els.map(el=>el.dataset.runId)')
    page.locator('.board-card').click()
    page.get_by_role('button',name='清除筛选',exact=True).click()
    reset_count=page.locator('.board-card').count()
    page.get_by_label('看板状态筛选').select_option('future_status')
    unknown_list_count=page.locator('.board-card').count()
    page.get_by_role('button',name='看板',exact=True).click()
    unknown_board_count=page.locator('.board-column[data-status=other] .board-card').count()
    page.get_by_role('button',name='清除筛选',exact=True).click()
    page.get_by_label('看板项目筛选').select_option('project-b')
    project_ids=page.locator('.board-card').evaluate_all('(els)=>els.map(el=>el.dataset.runId)')
    page.get_by_label('搜索看板任务').fill('no such task')
    empty_count=page.locator('.board-card').count()
    result={'sizes':sizes,'stressBoard':stress_board,'initialBoardCount':initial_board_count,'headerCounts':header_counts,'boardIds':board_ids,'listInitialCount':list_initial_count,'listIds':list_ids,'searchIds':search_ids,'resetCount':reset_count,'unknownListCount':unknown_list_count,'unknownBoardCount':unknown_board_count,'projectIds':project_ids,'emptyCount':empty_count,'errors':errors,'blocked':blocked,'calls':page.evaluate('window.__boardCalls')}
    browser.close()
    print(json.dumps(result))
`;
 const run=spawnSync(python,['-c',script],{input:JSON.stringify({chrome,resources,artifactDir}),encoding:'utf8',timeout:60000,maxBuffer:8*1024*1024});
 assert.equal(run.status,0,`actual React board harness failed: ${run.error?.message||run.stderr}`);result=JSON.parse(run.stdout);
}
test('long titles are clamped, full text stays accessible, duplicate descriptions are omitted',options,()=>{
 for(const size of result.sizes)for(const card of size.cards){assert.ok(card.title.length>500);assert.ok(card.titleHeight<=card.titleLineHeight*2+1,`${size.view} ${size.width}: title exceeds two lines`);assert.equal(card.duplicateSummary,false);assert.ok(card.rect.height<290,`${size.view} ${size.width}: giant task card`);}
});
test('both actual views stay inside narrow windows and every column scrolls independently',options,()=>{
 for(const size of result.sizes){assert.ok(size.bodyWidth<=size.width+1,`${size.view} ${size.width}: document is horizontally overflowing`);assert.ok(size.board.x>=0&&size.board.right<=size.width);for(const control of size.controls)assert.ok(control.x>=0&&control.right<=size.width+1,`${size.width}: hidden toolbar control`);for(const card of size.cards)assert.ok(card.scrollWidth<=card.clientWidth+1,`${size.view} ${size.width}: long token overflow`);for(const col of size.columns){assert.ok(col.rect.width>=223);assert.equal(col.overflow,'auto');}}
 assert.ok(result.stressBoard.columns.some(col=>col.scrollHeight>col.clientHeight),'many records must use internal column scrolling');
});
test('all 200 supplied tasks are reachable through honest client batching in board and list views',options,()=>{
 assert.equal(result.headerCounts.reduce((sum,n)=>sum+Number(n),0),200);assert.ok(result.initialBoardCount<200);assert.equal(result.listInitialCount,40);assert.equal(result.resetCount,40);
 for(const ids of [result.boardIds,result.listIds]){assert.equal(ids.length,200);assert.equal(new Set(ids).size,200);for(let i=0;i<200;i++)assert.ok(ids.includes('layout-'+i));}
 assert.equal(result.listIds[0],'layout-199');assert.equal(result.listIds.at(-1),'layout-0');
});
test('search includes not-yet-rendered tasks; project, unknown-status and empty filters preserve truth',options,()=>{
 assert.deepEqual(result.searchIds,['layout-0']);assert.ok(result.unknownListCount>0);assert.equal(result.unknownBoardCount,result.unknownListCount);assert.ok(result.projectIds.length);assert.ok(result.projectIds.every(id=>Number(id.split('-')[1])%2===1));assert.equal(result.emptyCount,0);
 assert.deepEqual(result.calls,[{select:'layout-0'}]);assert.deepEqual(result.errors,[]);assert.deepEqual(result.blocked,[]);
});
