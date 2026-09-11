"""Test-only asset inspection, no model/provider/business API requests. Optional FORGE_BASELINE_ZIP renders original version for comparison. Not a production preview entry."""
from pathlib import Path
from playwright.sync_api import sync_playwright
import zipfile,json,time,os
root=Path(__file__).resolve().parents[2];out=root/'docs/evidence-v09';out.mkdir(exist_ok=True)
base='''
const {OfficeRenderer,color}=loadUI('graphics/officeRenderer');
const {drawActor,palette}=loadUI('graphics/officeWorld');
const canvas=document.createElement('canvas');canvas.style='width:100vw;height:100vh;display:block';document.getElementById('root').appendChild(canvas);
const scene=new OfficeRenderer(canvas);if(scene.setQuality)scene.setQuality('detail');scene.day=true;scene.target=[0,1.05,0];scene.zoom=5.5;scene.pitch=.28;scene.yaw=1.30;
const people=['planner','developer','tester','reviewer'].map((role,i)=>({id:['Atlas','Nova','Echo','Sage'][i],role,x:(i-1.5)*1.8}));
const items=[{p:[0,.061,0],s:[24,.1,22],color:color(0xe3e6e8),mesh:'cube'}];
CHAIR_CODE
scene.setStatic(items);window.scene=scene;window.framesRendered=0;
window.pose='idle';window.changePose=(pose)=>window.pose=pose;
function paint(t){const parts=[];for(const [i,p]of people.entries())parts.push(...drawActor({id:p.id,position:[p.x,0,0],yaw:0,pose:window.pose,poseTime:1.8,visible:true,sitBlend:window.pose==='walking'||window.pose==='attention'?0:1,source:'safety',busy:false},i,palette[p.role]));scene.draw(parts);window.framesRendered++;requestAnimationFrame(paint);}requestAnimationFrame(paint);
'''
newchair="const {chairKit}=loadUI('graphics/furniture');people.forEach(p=>items.push(...chairKit(p.x,0,palette[p.role],Math.PI)));"
oldchair="""for(const p of people){const x=p.x,h=palette[p.role];for(const[a,s,c,m]of[[[x,.64,0],[.67,.15,.64],h,'round'],[[x,.96,-.28],[.7,.6,.12],h,'round'],[[x,.31,0],[.06,.60,.06],0x8f9da8,'cylinder'],[[x,.11,0],[.76,.07,.08],0x8696a0,'cube'],[[x,.11,0],[.08,.07,.76],0x8696a0,'cube']])items.push({p:a,s,color:color(c),mesh:m});}"""
baseline=os.getenv('FORGE_BASELINE_ZIP')
old=None
if baseline:
 with zipfile.ZipFile(baseline) as z:
  old=z.read(next(n for n in z.namelist() if n.endswith('frontend/dist/index.html'))).decode()
new=(root/'frontend/dist/index.html').read_text()
results=[]
with sync_playwright() as pw:
 b=pw.chromium.launch(executable_path='/usr/bin/chromium',headless=False,args=['--no-sandbox','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader','--ignore-gpu-blocklist'])
 for name,html,chairs in ([('before',old,oldchair)] if old else [])+[('after',new,newchair),('fallback',new,newchair)]:
  page=b.new_page(viewport={'width':1600,'height':850});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
  code=base.replace('CHAIR_CODE',chairs)
  if name=='fallback':code="const originalExtension=WebGLRenderingContext.prototype.getExtension;WebGLRenderingContext.prototype.getExtension=function(n){return n==='ANGLE_instanced_arrays'?null:originalExtension.call(this,n);};"+code
  page.set_content(html.replace("loadUI('main');",code),wait_until='load');page.wait_for_function('window.framesRendered>2',timeout=60000);page.wait_for_timeout(500)
  page.screenshot(path=str(out/f'05-assets-{name}.png'))
  data=page.evaluate('({frames:window.framesRendered,instancing:window.scene.supportsInstancing,glError:window.scene.gl.getError()})');data['errors']=errors;data['name']=name
  assert not errors and data['glError']==0,data
  if name=='fallback':assert data['instancing'] is False
  if name=='after':
   assert data['instancing'] is True
   page.evaluate("window.changePose('attention')");page.wait_for_timeout(2200);page.screenshot(path=str(out/'06-standing-asset-check.png'))
  results.append(data);page.close()
 b.close()
(out/'asset-browser-results.json').write_text(json.dumps({'passed':len(results),'failed':0,'scope':'actual WebGL asset inspection, not a task/session screen','results':results},ensure_ascii=False,indent=2))
print(json.dumps(results,ensure_ascii=False),flush=True)
