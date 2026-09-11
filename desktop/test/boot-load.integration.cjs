// Run with a stock Electron runtime, not Node. Exercises actual main-process
// functions and navigation guards in an isolated, hidden window with no data IO.
const {app,BrowserWindow,session}=require('electron');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const vm=require('node:vm');
const http=require('node:http');
const {createRequire}=require('node:module');
const profile=fs.mkdtempSync(path.join(os.tmpdir(),'forge-boot-profile-'));
app.setPath('userData',profile);
app.disableHardwareAcceleration();
app.on('window-all-closed',()=>{});
const sourcePath=process.argv[2]?path.resolve(process.argv[2]):path.resolve(__dirname,'../main.cjs');
const source=fs.readFileSync(sourcePath,'utf8').split('if (!app.requestSingleInstanceLock()) {')[0];
const localRequire=createRequire(sourcePath);

app.whenReady().then(async()=>{
 const server=http.createServer((_request,response)=>{response.setHeader('Content-Type','text/html');response.end('<!doctype html><title>Fixture workspace</title>');});
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const origin=`http://127.0.0.1:${server.address().port}`;
 const outcomes=[];
 for(const waitMs of [0,1,2,5,10,20,50,-1]){
  const serial=waitMs===-1;
  const events=[],exports={};let starts=0;
  const appFacade={isPackaged:false,setName(){},setPath(){},getPath:()=>profile};
  function HiddenWindow(options){const win=new BrowserWindow(options);win.show=()=>{};
   win.webContents.on('did-fail-load',(_e,code,description,url)=>events.push({event:'did-fail-load',code,description,url}));
   win.webContents.on('will-frame-navigate',event=>events.push({event:'will-frame-navigate',isMainFrame:event.isMainFrame,url:event.url}));
   return win;
  }
  const instrumentedSession={fromPartition(...args){const isolated=session.fromPartition(...args);const register=isolated.webRequest.onBeforeRequest.bind(isolated.webRequest);isolated.webRequest.onBeforeRequest=handler=>register((details,callback)=>handler(details,result=>{events.push({event:'request-policy',url:details.url,cancel:result.cancel});callback(result);}));return isolated;}};
  const electron={...require('electron'),app:appFacade,BrowserWindow:HiddenWindow,session:instrumentedSession};
  const context={exports,__dirname:path.dirname(sourcePath),process,console,setImmediate,AbortController,URL,
   require:name=>name==='electron'?electron:localRequire(name)};
  vm.runInNewContext(source+'\nexports.harness={createWindow,launchBackend,createSession,getWindow:()=>window,configure(s,b){browserSession=s;backend=b;secrets={};}};',context,{filename:sourcePath});
  const h=exports.harness;
  const isolatedSession=h.createSession();
  isolatedSession.webRequest.onErrorOccurred(details=>events.push({event:'request-error',error:details.error,url:details.url}));
  h.configure(isolatedSession,{async start(){starts++;return {origin,desktopToken:'fixture',controlToken:'fixture-control'};},async request(){return {headers:{'set-cookie':['forge_session=fixture; HttpOnly; SameSite=Strict']}};}});
  const initial=h.createWindow();
  if(serial){if(initial)await initial;else if(!h.getWindow().webContents.getURL()||h.getWindow().webContents.isLoading())await new Promise(resolve=>h.getWindow().webContents.once('did-finish-load',resolve));}
  else if(waitMs)await new Promise(resolve=>setTimeout(resolve,waitMs));
  try{await h.launchBackend();outcomes.push({waitMs,serial,ok:true,starts,events});}
  catch(error){outcomes.push({waitMs,serial,ok:false,starts,error:error.message,events});}
  console.log(JSON.stringify(outcomes.at(-1)));
  h.getWindow().destroy();
 }
 const failures=outcomes.filter(result=>!result.ok||result.events.some(event=>event.error==='net::ERR_ABORTED'||event.code===-3));
 console.log(JSON.stringify({profile,cases:outcomes.length,failures:failures.map(({waitMs,ok,starts,error})=>({waitMs,ok,starts,error}))}));
 await new Promise(resolve=>server.close(resolve));
 app.exit(failures.length?1:0);
}).catch(error=>{console.error(error.message);app.exit(2);});
