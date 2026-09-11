"""Render the exact standalone application with a constrained local-HTTP bridge.
The verification browser's managed policy blocks ALL URL navigation. We do not
change/disable that policy. page.set_content renders supplied artifact bytes;
only /api requests are forwarded to the local backend by this test harness.
This verifies actual DOM/WebGL + API integration, not normal browser navigation,
CORS, native EventSource, or real browser download behavior.
"""
import json,queue,threading,time,uuid
from urllib.parse import urlparse
import httpx
class LocalTransport:
    def __init__(self,base='http://127.0.0.1:8787'):
        self.base=base;self.client=httpx.Client(base_url=base,trust_env=False,timeout=30);self.streams={}
    def path(self,url):
        parsed=urlparse(url)
        if parsed.scheme or parsed.netloc or not parsed.path.startswith('/api/'):raise ValueError('Bridge accepts only relative /api/ requests')
        return url
    def fetch(self,args):
        path=self.path(args['url']);response=self.client.request(args.get('method','GET'),path,headers=args.get('headers',{}),content=args.get('body'))
        return {'status':response.status_code,'headers':dict(response.headers),'body':response.text}
    def start(self,url):
        path=self.path(url);identifier=uuid.uuid4().hex;items=queue.Queue();stop=threading.Event();self.streams[identifier]=(items,stop)
        cookies=dict(self.client.cookies)
        def worker():
            last=None
            while not stop.is_set():
                try:
                    with httpx.Client(base_url=self.base,trust_env=False,timeout=httpx.Timeout(5,read=3),cookies=cookies) as c:
                        with c.stream('GET',path,headers={'last-event-id':last} if last else {}) as r:
                            r.raise_for_status();items.put({'kind':'open'});event={}
                            for line in r.iter_lines():
                                if stop.is_set():break
                                if not line:
                                    if 'data' in event:items.put({'kind':'event',**event})
                                    event={};continue
                                if line.startswith('id: '):last=line[4:];event['id']=last
                                elif line.startswith('data: '):event['data']=line[6:]
                                elif line.startswith('event: '):event['name']=line[7:]
                except Exception:items.put({'kind':'error'})
                if not stop.is_set():time.sleep(.25)
        threading.Thread(target=worker,daemon=True).start();return identifier
    def poll(self,identifier):
        if identifier not in self.streams:return []
        q,_=self.streams[identifier];result=[]
        while not q.empty() and len(result)<100:result.append(q.get_nowait())
        return result
    def close(self,identifier):
        if identifier in self.streams:self.streams[identifier][1].set()
    def attach(self,page,html):
        page.expose_function('__localFetch',self.fetch);page.expose_function('__localStream',self.start);page.expose_function('__localPoll',self.poll);page.expose_function('__localClose',self.close)
        bridge='''<script>
window.fetch=async function(url,options={}){const r=await window.__localFetch({url:String(url),method:options.method||'GET',headers:options.headers||{},body:options.body||null});return new Response(r.body,{status:r.status,headers:r.headers});};
window.EventSource=class {constructor(url){this.closed=false;this.listeners={};this.id=null;window.__localStream(url).then(id=>{this.id=id;if(this.closed){window.__localClose(id);return;}this.timer=setInterval(async()=>{if(this.polling)return;this.polling=true;try{for(const e of await window.__localPoll(id)){if(this.closed)break;if(e.kind==='open')this.onopen?.({});else if(e.kind==='error')this.onerror?.({});else{const event={data:e.data,lastEventId:e.id};if(e.name)this.listeners[e.name]?.(event);else this.onmessage?.(event);}}}finally{this.polling=false;}},100);});}addEventListener(n,fn){this.listeners[n]=fn;}close(){this.closed=true;clearInterval(this.timer);if(this.id)window.__localClose(this.id);}};
</script>'''
        page.set_content(html.replace('<head>','<head>'+bridge),wait_until='load')
    def shutdown(self):
        for identifier in self.streams:self.close(identifier)
        self.client.close()
