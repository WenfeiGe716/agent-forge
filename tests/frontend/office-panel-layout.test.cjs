/** Real CSS-cascade geometry regression. Runs a separate, network-blocked headless
 * Chrome with synthetic DOM only; never attaches to the desktop app or its data.
 * Optional local prerequisites: .venv Playwright + installed Chrome (or env paths).
 */
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),{spawnSync}=require('node:child_process');
const root=path.resolve(__dirname,'../..');
const python=process.env.FORGE_LAYOUT_PYTHON||path.join(root,'.venv/bin/python');
const chrome=process.env.CHROMIUM_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const available=fs.existsSync(python)&&fs.existsSync(chrome);
const artifactDir=process.env.FORGE_LAYOUT_ARTIFACT_DIR;
if(artifactDir)assert.ok(path.isAbsolute(artifactDir)&&fs.statSync(artifactDir).isDirectory(),'artifact directory must already exist and be absolute');
const build=fs.readFileSync(path.join(root,'scripts/build-offline.mjs'),'utf8');
const cssFiles=build.match(/const combinedCSS=\[([^\]]+)\]/)?.[1].match(/'([^']+\.css)'/g)?.map(s=>s.slice(1,-1));
assert.ok(cssFiles?.includes('employee-task.css'),'fixture must use the production CSS concatenation order');
const css=cssFiles.map(file=>fs.readFileSync(path.join(root,'frontend/src',file),'utf8')).join('\n');
const avatar='<span class="studio-avatar" style="--person:#55b6ca"><i></i><b></b><em></em></span>';
const icon='<svg width="14" height="14" aria-hidden="true"><path d="M2 7H12"/></svg>';
const html=`<!doctype html><html lang="zh-CN"><meta charset="utf-8"><style>${css}</style><body>
<div class="studio-workspace has-roster has-inspector">
 <aside class="studio-inspector task-inspector glass"><header>${avatar}<div><h2>验证需求师 ezpcw7<span>需求分析师</span></h2><p>尚未安排工位<small>安全等待</small></p></div><button>×</button></header>
  <div class="inspector-tabs"><button>任务 / 审批</button><button>行为</button><button>模型</button><button>会话</button><button>外观</button></div>
  <div class="inspector-body"><label>模型<select><option>实际模型配置</option></select></label><label><input type="checkbox">需要确认的配置</label></div>
  <footer><button>聚焦成员</button><button>工作会话</button></footer></aside>
 <section class="office-employee-dock glass is-compact"><div class="office-dock-identity">${avatar}<span><b>验证需求师 ezpcw7<small>需求分析师</small></b><em>本机执行验证 · 工位待安排</em></span></div><div class="office-dock-actions"><button>${icon}未安排工位</button><button>${icon}任务 / 审批</button><button>${icon}会话</button></div></section>
 <button class="studio-composer-entry glass">${icon}发起需求 / 补充说明</button>
 <section class="studio-composer glass"><header><span>${icon}<select><option>本机执行闭环验证 ezpcw7</option></select></span><select><option>新建定向需求</option></select></header><div><textarea placeholder="描述目标与验收期望"></textarea><button>→</button></div><footer><span>选择办公团队 → 确认需求与预检 → 创建任务</span><span>⌘ / Ctrl + Enter</span></footer></section>
 <section class="office-session-window glass"><header class="office-window-handle"><span class="office-window-symbol">${icon}</span><div><b>验证需求师 ezpcw7 · 工作会话</b><small>真实记录 · 按任务独立查看</small></div><span class="office-window-grip">⠿</span><button>×</button></header><div class="office-window-body">真实任务记录</div></section>
</div></body></html>`;
const scenarios=[];
for(const [width,height] of [[1920,1080],[1400,880],[980,700],[740,640],[390,640]]){
 for(const longName of [false,true])scenarios.push({width,height,longName,left:244,right:376});
}
for(const [left,right] of [[0,376],[244,0],[0,0]])scenarios.push({width:1400,height:880,longName:false,left,right});
let measurements=[];
if(available){
 const script=String.raw`
import json,sys
from playwright.sync_api import sync_playwright
payload=json.load(sys.stdin)
with sync_playwright() as pw:
    browser=pw.chromium.launch(executable_path=payload['chrome'],headless=True,args=['--disable-background-networking','--disable-component-update'])
    context=browser.new_context(viewport={'width':1400,'height':880},reduced_motion='reduce')
    context.route('**/*',lambda route: route.abort())
    page=context.new_page()
    page.set_content(payload['html'],wait_until='load')
    result=[]
    for scenario in payload['scenarios']:
        page.set_viewport_size({'width':scenario['width'],'height':scenario['height']})
        result.append(page.evaluate('''(s)=>{
          const q=selector=>document.querySelector(selector),workspace=q('.studio-workspace');
          workspace.style.setProperty('--office-left',s.left+'px');workspace.style.setProperty('--office-right',s.right+'px');
          q('.office-dock-identity>span:last-child>b').firstChild.textContent=s.longName?'超长跨部门需求分析负责人_ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmnopqrstuvwxyz_0123456789':'验证需求师 ezpcw7';
          const rect=el=>{const r=el.getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height,cx:r.left+r.width/2,cy:r.top+r.height/2};};
          const dock=q('.office-employee-dock'),session=q('.office-session-window');
          const initial=rect(session),dragOrigin={left:session.offsetLeft,top:session.offsetTop};
          session.style.left=(dragOrigin.left+17)+'px';session.style.top=(dragOrigin.top+23)+'px';
          const moved=rect(session);session.style.removeProperty('left');session.style.removeProperty('top');
          return {...s,workspace:rect(workspace),inspector:rect(q('.studio-inspector')),dock:rect(dock),entry:rect(q('.studio-composer-entry')),composer:rect(q('.studio-composer')),session:initial,dragOrigin,moved,dockClientWidth:dock.clientWidth,dockScrollWidth:dock.scrollWidth,buttons:[...q('.office-dock-actions').children].map(rect),checkbox:rect(q('input[type=checkbox]')),select:rect(q('.inspector-body select'))};
        }''',scenario))
        if payload.get('artifactDir') and scenario['longName'] and scenario['width'] in [1400,390]:
            for component in ['office-employee-dock','studio-inspector','office-session-window']:
                page.evaluate('''(name)=>{for(const el of document.querySelector('.studio-workspace').children)el.style.visibility=el.classList.contains(name)?'visible':'hidden';}''',component)
                page.locator('.'+component).screenshot(path=payload['artifactDir']+'/'+str(scenario['width'])+'-'+component+'.png')
            page.evaluate('''()=>{for(const el of document.querySelector('.studio-workspace').children)el.style.removeProperty('visibility');}''')
    browser.close()
    print(json.dumps(result))
`;
 const run=spawnSync(python,['-c',script],{input:JSON.stringify({chrome,html,scenarios,artifactDir}),encoding:'utf8',timeout:45000,maxBuffer:3*1024*1024});
 assert.equal(run.status,0,`headless CSS harness failed: ${run.error?.message||run.stderr}`);
 measurements=JSON.parse(run.stdout);
 if(artifactDir)fs.writeFileSync(path.join(artifactDir,'geometry.json'),JSON.stringify(measurements,null,2)+'\n');
}
const options={skip:available?false:'requires local Playwright Python and Chrome; no dependency is downloaded'};
const description=m=>`${m.width}×${m.height}, ${m.longName?'long':'normal'} name, insets ${m.left}/${m.right}`;
const close=(actual,expected,label)=>assert.ok(Math.abs(actual-expected)<=1,`${label}: ${actual}, expected ${expected} ±1 CSS px`);

test('employee inspector is compact across production responsive CSS breakpoints',options,()=>{
 for(const m of measurements){assert.ok(m.inspector.width>=300&&m.inspector.width<=320,`${description(m)}: inspector width ${m.inspector.width}; expected about 312 CSS px`);assert.ok(m.inspector.left>=0&&m.inspector.right<=m.width,description(m));}
});
test('employee dock, collapsed entry and expanded composer share the viewport horizontal center',options,()=>{
 for(const m of measurements)for(const name of ['dock','entry','composer'])close(m[name].cx,m.workspace.cx,`${description(m)}: ${name} center`);
});
test('employee dock stays compact and every action remains inside its surface with long names',options,()=>{
 for(const m of measurements){
  assert.ok(m.dock.width<=Math.min(600,m.width-24)+1,`${description(m)}: dock width ${m.dock.width} exceeds compact limit`);
  assert.ok(m.dock.left>=11&&m.dock.right<=m.width-11,`${description(m)}: dock escapes viewport`);
  assert.ok(m.dockScrollWidth<=m.dockClientWidth+1,`${description(m)}: dock content overflows ${m.dockScrollWidth-m.dockClientWidth} CSS px`);
  for(const b of m.buttons)assert.ok(b.width>0&&b.left>=m.dock.left&&b.right<=m.dock.right&&b.top>=m.dock.top&&b.bottom<=m.dock.bottom,`${description(m)}: action escapes dock: ${JSON.stringify(b)}`);
 }
});
test('fresh employee session is centered on both axes at wide, short and narrow sizes',options,()=>{
 for(const m of measurements){close(m.session.cx,m.workspace.cx,`${description(m)}: session horizontal center`);close(m.session.cy,m.workspace.cy,`${description(m)}: session vertical center`);assert.ok(m.session.top>=96&&m.session.bottom<=m.workspace.bottom-12,`${description(m)}: session overlaps reserved edges`);}
});
test('session CSS geometry matches offset drag origin and does not jump on first movement',options,()=>{
 for(const m of measurements){close(m.dragOrigin.left,m.session.left,`${description(m)}: drag origin x`);close(m.dragOrigin.top,m.session.top,`${description(m)}: drag origin y`);close(m.moved.left,m.session.left+17,`${description(m)}: drag delta x`);close(m.moved.top,m.session.top+23,`${description(m)}: drag delta y`);}
});
test('compact inspector retains small checkbox and normal input heights',options,()=>{
 for(const m of measurements){close(m.checkbox.width,15,`${description(m)}: checkbox width`);close(m.checkbox.height,15,`${description(m)}: checkbox height`);close(m.select.height,38,`${description(m)}: select height`);}
});
