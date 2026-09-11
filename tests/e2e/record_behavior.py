"""Record actual offline preview pixels at original browser timestamps.
The sample state is visibly labelled; this is not real-LLM footage.
Requires Playwright, Chromium, a display, and ffmpeg (not included in offline wheels).
"""
from __future__ import annotations
import base64,json,os,shutil,subprocess,tempfile,time
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'docs/evidence-v04'

def main():
 frames=Path(tempfile.mkdtemp(prefix='forge-behavior-record-'));captured=[];actions=[];errors=[]
 os.environ.setdefault('DISPLAY',':0')
 try:
  with sync_playwright() as p:
   b=p.chromium.launch(executable_path=os.getenv('CHROMIUM_PATH','/usr/bin/chromium'),headless=False,args=['--no-sandbox','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader','--ignore-gpu-blocklist'])
   page=b.new_page(viewport={'width':1440,'height':900},device_scale_factor=1);page.set_default_timeout(20000);page.on('pageerror',lambda e:errors.append(str(e)))
   page.set_content((ROOT/'frontend/dist/office-preview.html').read_text(),wait_until='load')
   page.wait_for_function("document.querySelector('.living-label[data-pose=phone]')&&document.querySelector('.living-label[data-pose=drink]')&&document.querySelector('.living-label[data-pose=nap]')",timeout=40000)
   cdp=page.context.new_cdp_session(page)
   def frame(e):
    name=f'{len(captured):06d}.jpg';(frames/name).write_bytes(base64.b64decode(e['data']));captured.append({'file':name,'timestamp':e['metadata']['timestamp']});cdp.send('Page.screencastFrameAck',{'sessionId':e['sessionId']})
   cdp.on('Page.screencastFrame',frame);cdp.send('Page.startScreencast',{'format':'jpeg','quality':88,'maxWidth':1440,'maxHeight':900,'everyNthFrame':1});started=time.monotonic()
   def note(text):actions.append({'elapsed_seconds':round(time.monotonic()-started,3),'action':text})
   note('工作成员按任务执行；空闲成员喝水、看手机、小憩；全部是明确标识的示例输入')
   page.wait_for_timeout(1500)
   label=page.locator('.living-label[data-pose=drink]').first
   if label.count()==0:label=page.locator('.living-label[data-pose=phone]').first
   name=label.locator('b').inner_text();aid=label.get_attribute('data-agent-id');page.locator('.studio-member').filter(has_text=name).click();note('选中正在空闲的'+name+'，查看其动作依据')
   page.get_by_role('button',name='聚焦成员',exact=True).click();page.wait_for_timeout(2200)
   page.get_by_role('button',name='给此成员分派示例任务',exact=True).click();expect(page.locator(f'.living-label[data-agent-id="{aid}"]')).to_have_attribute('data-source','business');note('明确派发示例任务，立即打断空闲/释放设施，回工位；执行不等待动画')
   page.wait_for_timeout(6500);page.get_by_role('button',name='恢复全景',exact=True).click();page.wait_for_timeout(1000)
   page.get_by_role('button',name='消息交接',exact=True).click();note('只有显式示例消息事件出现后才绘制气泡和连线');page.wait_for_timeout(2500)
   page.get_by_role('button',name='断线保护',exact=True).click();page.wait_for_function("[...document.querySelectorAll('.living-label')].every(e=>e.dataset.source==='safety')");note('占用来源断线，停止空闲与对话，显示安全等待');page.wait_for_timeout(2200)
   page.get_by_role('button',name='恢复同步',exact=True).click();note('恢复来源后重新确认占用，不靠随机动画猜测任务');page.wait_for_timeout(2000)
   page.get_by_role('button',name='切换日夜',exact=True).click();page.wait_for_timeout(1400);note('夜间显示仍使用同一业务/空闲状态机')
   fps=page.locator('[data-testid="office-viewport"]').get_attribute('data-fps');page.get_by_role('button',name='切换日夜',exact=True).click();page.wait_for_timeout(800)
   cdp.send('Page.stopScreencast');b.close()
  if len(captured)<2:raise RuntimeError('No real browser frames captured')
  captured.sort(key=lambda f:f['timestamp']);lines=[]
  for i,f in enumerate(captured):
   lines.append("file '"+str(frames/f['file'])+"'");duration=captured[i+1]['timestamp']-f['timestamp'] if i+1<len(captured) else .25;lines.append(f'duration {max(duration,.001):.6f}')
  lines.append("file '"+str(frames/captured[-1]['file'])+"'");listing=frames/'concat.txt';listing.write_text('\n'.join(lines)+'\n')
  destination=Path(os.getenv('FORGE_VIDEO_OUTPUT',str(OUT/'walkthrough.mp4')))
  subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','concat','-safe','0','-i',str(listing),'-vf','fps=20,format=yuv420p','-c:v','libx264','-preset','medium','-crf','21','-movflags','+faststart',str(destination)],check=True,timeout=180)
  result={'source':'actual office-preview.html, explicit sample events, no model API','method':'CDP actual browser frame capture, original timestamp intervals, no speed-up; 20fps encoded duplicates are not rendering FPS','gpu':'Chromium ANGLE SwiftShader software GPU','render_fps_observed':fps,'frames':len(captured),'source_duration_s':captured[-1]['timestamp']-captured[0]['timestamp'],'actions':actions,'page_errors':errors}
  (OUT/'recording.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)
  if errors:raise RuntimeError(errors)
 finally:shutil.rmtree(frames,ignore_errors=True)
if __name__=='__main__':main()
