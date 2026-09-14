/** Regression geometry for long composer failures and the selected-member dock.
 * Uses production CSS in a network-free browser; no app state is touched.
 */
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{spawnSync}=require('node:child_process');
const root=path.resolve(__dirname,'../..'),python=process.env.FORGE_LAYOUT_PYTHON||path.join(root,'.venv/bin/python'),chrome=process.env.CHROMIUM_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const available=fs.existsSync(python)&&fs.existsSync(chrome),build=fs.readFileSync(path.join(root,'scripts/build-offline.mjs'),'utf8');
const cssFiles=build.match(/const combinedCSS=\[([^\]]+)\]/)?.[1].match(/'([^']+\.css)'/g)?.map(s=>s.slice(1,-1));
assert.ok(cssFiles?.includes('employee-task.css'));
const css=cssFiles.map(file=>fs.readFileSync(path.join(root,'frontend/src',file),'utf8')).join('\n');
const html=`<!doctype html><html><meta charset="utf-8"><style>body{margin:0}${css}</style><body><div class="studio-workspace has-composer-error" style="width:100%;height:100vh;position:relative">
<section class="studio-composer glass"><header><span>项目</span><select><option>新建定向需求</option></select></header><div class="composer-input-row"><textarea>请完成一个很长的任务</textarea><button aria-label="创建任务">→</button></div><div class="composer-failure" role="alert"><p>任务尚未开始。operation is unavailable on this backend。模型响应超时，具体原因和恢复建议应该完整显示。<br/>请检查负责人、项目权限和后端连接后重试。<br/>系统保留当前输入，不会丢失需求。</p><button type="button">修改配置</button><button type="button">重试发送</button><details open><summary>查看详细原因</summary><pre>backend: operation unavailable\nrun: not started\nrecovery: retry current request</pre></details></div><footer><span>重复说明</span><span>⌘ / Ctrl + Enter</span></footer></section>
<section class="office-employee-dock glass"><div class="office-dock-identity"><span>负责人</span></div><div class="office-dock-actions"><button>重新定位</button><button>任务 / 审批</button><button>会话</button></div></section></div></body></html>`;
let measurements=[];
if(available){const script=String.raw`import json,sys
from playwright.sync_api import sync_playwright
p=json.load(sys.stdin)
with sync_playwright() as pw:
 b=pw.chromium.launch(executable_path=p['chrome'],headless=True,args=['--disable-background-networking'])
 page=b.new_page(viewport={'width':1400,'height':880});page.set_content(p['html'])
 results=[]
 for s in p['scenarios']:
  page.set_viewport_size({'width':s['width'],'height':s['height']})
  results.append(page.evaluate('''s=>{const ws=document.querySelector('.studio-workspace');ws.style.width=s.width+'px';ws.style.height=s.height+'px';const c=document.querySelector('.studio-composer');const measured=c.getBoundingClientRect().height;ws.style.setProperty('--studio-composer-height',measured+'px');const r=e=>{const x=e.getBoundingClientRect();return {left:x.left,right:x.right,top:x.top,bottom:x.bottom,width:x.width,height:x.height}};const f=document.querySelector('.composer-failure'),d=document.querySelector('.office-employee-dock');const clickable=e=>{const x=e.getBoundingClientRect(),hit=document.elementFromPoint(x.left+x.width/2,x.top+x.height/2);return hit===e||e.contains(hit)};return {scenario:s,measured,composer:r(c),failure:r(f),dock:r(d),actions:[...f.querySelectorAll('button')].map(r),labels:[...f.querySelectorAll('button')].map(e=>e.textContent.trim()),dockActions:[...d.querySelectorAll('button')].map(r),clickable:[...f.querySelectorAll('button')].map(clickable),dockClickable:[...d.querySelectorAll('button')].map(clickable),styles:[...f.querySelectorAll('button')].map(e=>({whiteSpace:getComputedStyle(e).whiteSpace,width:getComputedStyle(e).width,height:getComputedStyle(e).height})),variable:ws.style.getPropertyValue('--studio-composer-height')}}''',s))
 print(json.dumps(results))
 b.close()`;
 const scenarios=[{width:1400,height:880},{width:1000,height:700},{width:740,height:640},{width:390,height:640}];
 const run=spawnSync(python,['-c',script],{input:JSON.stringify({chrome,html,scenarios}),encoding:'utf8',timeout:45000,maxBuffer:1024*1024});assert.equal(run.status,0,run.stderr);measurements=JSON.parse(run.stdout);
}
const options={skip:available?false:'requires local Playwright Python and Chrome'};
const label=m=>`${m.scenario.width}×${m.scenario.height}`;
test('composer failure actions keep readable labels and do not get crushed by send-button styles',options,()=>{for(const m of measurements){assert.deepEqual(m.labels,['修改配置','重试发送'],`${label(m)}: action copy must stay concise`);for(const [i,a] of m.actions.entries()){assert.ok(a.width>=96,`${label(m)}: failure action ${i} width ${a.width} is too narrow`);assert.ok(a.height>=28,`${label(m)}: failure action ${i} height ${a.height} is too short`);assert.equal(m.styles[i].whiteSpace,'nowrap');assert.notEqual(m.styles[i].width,'36px');assert.equal(m.clickable[i],true,`${label(m)}: failure action ${i} is not clickable`);}}});
test('selected member dock never covers the expanded composer at wide and small sizes',options,()=>{for(const m of measurements){assert.ok(Number(m.variable.replace('px',''))>0,`${label(m)}: measured composer height missing`);const overlap=m.dock.left<m.composer.right&&m.dock.right>m.composer.left&&m.dock.top<m.composer.bottom&&m.dock.bottom>m.composer.top;assert.equal(overlap,false,`${label(m)}: dock ${JSON.stringify(m.dock)} overlaps composer ${JSON.stringify(m.composer)}`);for(const a of m.dockActions){assert.ok(a.left>=m.dock.left&&a.right<=m.dock.right&&a.top>=m.dock.top&&a.bottom<=m.dock.bottom,`${label(m)}: dock action escapes surface`);assert.equal(m.dockClickable[m.dockActions.indexOf(a)],true,`${label(m)}: dock action is not clickable`);}}});
