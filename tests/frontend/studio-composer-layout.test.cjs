/** Render the ACTUAL Studio composer (all four selectors and optional approval)
 * into a fresh, network-blocked headless Chrome with the production CSS order.
 * No app process, user profile, backend, model, or generated bundle is used.
 */
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {createRequire}=require('node:module'),{spawnSync}=require('node:child_process');
const root=path.resolve(__dirname,'../..'),filename=require.resolve('./organization-workspace.test.cjs'),local=createRequire(filename),studio={};
vm.runInNewContext(fs.readFileSync(filename,'utf8')+'\nObject.assign(exports,{harness,nodes,text,field});',{exports:studio,__dirname:path.dirname(filename),console,performance,Date,setImmediate,URL,require:Object.assign(id=>id==='node:test'?()=>{}:local(id),{resolve:local.resolve})},{filename});
const python=process.env.FORGE_LAYOUT_PYTHON||path.join(root,'.venv/bin/python');
const chrome=process.env.CHROMIUM_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const available=fs.existsSync(python)&&fs.existsSync(chrome);
const build=fs.readFileSync(path.join(root,'scripts/build-offline.mjs'),'utf8');
const cssFiles=build.match(/const combinedCSS=\[([^\]]+)\]/)?.[1].match(/'([^']+\.css)'/g)?.map(s=>s.slice(1,-1));
assert.ok(cssFiles?.includes('employee-task.css'));
const css=cssFiles.map(file=>fs.readFileSync(path.join(root,'frontend/src',file),'utf8')).join('\n');
const escape=value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
function html(node,selected){
 if(Array.isArray(node))return node.map(n=>html(n,selected)).join('');
 if(node==null||typeof node==='boolean')return'';if(typeof node!=='object')return escape(node);
 const {type,props={}}=node;
 if(type==='Fragment')return html(props.children,selected);
 if(typeof type!=='string')throw new Error('unexpected component in real composer: '+type);
 if(/^[A-Z]/.test(type))return `<svg width="${props.size||13}" height="${props.size||13}" aria-hidden="true"></svg>`;
 const attributes=Object.entries(props).filter(([key,value])=>!['children','ref','key','value'].includes(key)&&!key.startsWith('on')&&value!=null&&value!==false).map(([key,value])=>`${key==='className'?'class':key}="${escape(value===true?'':value)}"`);
 if(type==='option'){attributes.push(`value="${escape(props.value)}"`);if(props.value===selected)attributes.push('selected');}
 const inside=type==='textarea'?escape(props.value||''):html(props.children,type==='select'?props.value:selected);
 return`<${type} ${attributes.join(' ')}>${inside}</${type}>`;
}
function composer(approval,longLabels,mode='new'){
 const agents=[{id:'owner',name:longLabels?'跨部门需求分析负责人_ABCDEFGHIJKLMNOPQRSTUVWXYZ':'需求分析师',role:'developer',job_title:'需求分析',runtime:{},enabled:true}];
 const projectName=longLabels?'本机执行闭环验证_长项目名称_ABCDEFGHIJKLMNOPQRSTUVWXYZ':'本机执行闭环验证 ezpcw7';
 const organization={ref:{id:'office',version:3,digest:'confirmed'},config:{floors:[{floor:1,department:longLabels?'跨业务需求研究与战略规划部门':'研发部',name:'研发楼层',zones:[]}],positions:{},teams:[{id:'team',name:longLabels?'多团队联合分析与交付质量研究小组':'开发团队',floor:1,member_ids:['owner'],headcount:1}]}};
 const h=studio.harness('StudioWorkspace',{agents,organization,projects:[{id:'project',name:projectName}],runs:[],selectedRun:'run',detail:{id:'run',status:approval?'awaiting_approval':'running',snapshot:{agents:{owner:{}}}},onCreateTask:()=>assert.fail('geometry checks must not create a task')});
 let tree=h.render();studio.field(tree,'任务项目').props.onChange({target:{value:'project'}});
 if(mode==='append'){studio.field(tree,'指令类型').props.onChange({target:{value:'append'}});tree=h.render();}
 if(longLabels){if(mode==='new')studio.field(tree,'需求办公团队').props.onChange({target:{value:'team'}});tree=h.render();studio.field(tree,'指令收件人').props.onChange({target:{value:'owner'}});}
 tree=h.render();const content=studio.nodes(tree).find(n=>n.type==='section'&&n.props.className==='studio-composer glass');assert.ok(content);
 const header=studio.nodes(content).find(n=>n.type==='header'),fields=studio.nodes(header).filter(n=>n.type==='select');
 assert.deepEqual(Array.from(fields,n=>n.props['aria-label']),mode==='new'?['任务项目','指令类型','需求办公团队','指令收件人']:['任务项目','指令类型','指令收件人']);
 assert.equal(studio.nodes(header).filter(n=>n.type==='button'&&n.props.className==='approval-link').length,Number(approval));
 return html(content);
}
const scenarios=[];
for(const width of [1920,1400,980,740,390])for(const approval of [false,true])for(const longLabels of [false,true])scenarios.push({width,height:880,approval,longLabels,mode:'new',markup:composer(approval,longLabels)});
for(const width of [1400,390])scenarios.push({width,height:880,approval:true,longLabels:true,mode:'append',markup:composer(true,true,'append')});
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
    results=[]
    for s in payload['scenarios']:
        page.set_viewport_size({'width':s['width'],'height':s['height']})
        page.set_content('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><style>'+payload['css']+'</style><body><div class="studio-workspace">'+s['markup']+'</div></body></html>',wait_until='load')
        measured=page.evaluate('''()=>{
          const header=document.querySelector('.studio-composer>header'),composer=header.parentElement;
          const rect=el=>{const r=el.getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height,cy:r.top+r.height/2};};
          const controls=[...header.querySelectorAll('select,button')].map(el=>({label:el.getAttribute('aria-label')||el.textContent,title:el.title,selectedText:el.selectedOptions?.[0]?.textContent,rect:rect(el),display:getComputedStyle(el).display}));
          const reachable=[...header.querySelectorAll('select,button')].map(el=>{el.focus();el.scrollIntoView({inline:'nearest',block:'nearest'});const a=rect(el),h=rect(header);return{label:el.getAttribute('aria-label')||el.textContent,focused:document.activeElement===el,visible:a.left>=h.left-1&&a.right<=h.right+1};});
          return{header:rect(header),composer:rect(composer),controls,reachable,scrollWidth:header.scrollWidth,clientWidth:header.clientWidth,overflowX:getComputedStyle(header).overflowX};
        }''')
        results.append({**{k:v for k,v in s.items() if k!='markup'},**measured})
    browser.close()
    print(json.dumps(results))
`;
 const run=spawnSync(python,['-c',script],{input:JSON.stringify({chrome,css,scenarios}),encoding:'utf8',timeout:45000,maxBuffer:3*1024*1024});
 assert.equal(run.status,0,`headless CSS harness failed: ${run.error?.message||run.stderr}`);measurements=JSON.parse(run.stdout);
}
const options={skip:available?false:'requires local Playwright Python and Chrome; no dependency is downloaded'};
const label=m=>`${m.width}px ${m.mode}, approval ${m.approval}, ${m.longLabels?'long selected labels':'default labels'}`;
test('the actual four composer selectors and optional approval action stay on exactly one row',options,()=>{
 for(const m of measurements){assert.equal(m.controls.length,(m.mode==='new'?4:3)+Number(m.approval));const positions=m.controls.map(c=>c.rect.cy);
  assert.ok(Math.max(...positions)-Math.min(...positions)<=1,`${label(m)}: controls wrap to different rows: ${m.controls.map(c=>`${c.label} y=${c.rect.top.toFixed(1)}`).join(', ')}`);
 }
});
test('desktop composer fits all real fields while narrow overflow can be scrolled and keyboard-focused',options,()=>{
 for(const m of measurements){
  assert.ok(m.composer.left>=0&&m.composer.right<=m.width,`${label(m)}: composer escapes viewport`);
  for(const c of m.controls)assert.ok(c.display!=='none'&&c.rect.width>=65&&c.rect.height>=20,`${label(m)}: control hidden or crushed: ${c.label}`);
  if(m.width>=980)assert.ok(m.scrollWidth<=m.clientWidth+1,`${label(m)}: normal desktop header should fit without scrolling`);
  if(m.scrollWidth>m.clientWidth+1)assert.ok(['auto','scroll'].includes(m.overflowX),`${label(m)}: overflow must be scrollable`);
  assert.ok(m.reachable.every(c=>c.focused&&c.visible),`${label(m)}: every selector and approval must remain reachable: ${JSON.stringify(m.reachable)}`);
 }
});
test('compact recipient prompts retain full intent and long selections retain descriptive titles',options,()=>{
 for(const m of measurements){
  const recipient=m.controls.find(c=>c.label==='指令收件人');
  if(!m.longLabels){assert.equal(recipient.selectedText,m.mode==='append'?'选择收件人':'选择负责人');assert.equal(recipient.title,m.mode==='append'?'选择任务内收件人':'选择团队内负责人');}
  for(const control of m.controls.filter(c=>c.selectedText)){
   assert.ok(control.title,`${label(m)}: ${control.label} must retain a full title`);
   if(m.longLabels&&['任务项目','指令收件人'].includes(control.label))assert.equal(control.title,control.selectedText);
   if(m.longLabels&&control.label==='需求办公团队')assert.ok(control.title.includes('跨业务需求研究与战略规划部门 / 多团队联合分析与交付质量研究小组'));
  }
 }
});
