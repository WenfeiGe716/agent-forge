'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {EventEmitter}=require('node:events');
const {createRequire}=require('node:module');

function harness(){
 const filename=path.resolve(__dirname,'../main.cjs');
 const source=fs.readFileSync(filename,'utf8').split('if (!app.requestSingleInstanceLock()) {')[0];
 const localRequire=createRequire(filename),exports={};let starts=0;
 class Window extends EventEmitter{
  constructor(){super();this.loads=[];this.url='';this.pending=null;this.webContents=new EventEmitter();
   this.webContents.setWindowOpenHandler=()=>{};this.webContents.getURL=()=>this.url;
  }
  isDestroyed(){return false;}
  show(){}
  loadURL(url){
   this.loads.push(url);
   if(this.pending){const error=Object.assign(new Error(` (-3) loading '${url}'`),{errno:-3});this.pending.reject(error);this.pending=null;return Promise.reject(error);}
   if(this.loads.length===1)return new Promise((resolve,reject)=>{this.pending={url,resolve,reject};});
   this.url=url;return Promise.resolve();
  }
  finish(error){const pending=this.pending;this.pending=null;if(error)pending.reject(error);else{this.url=pending.url;pending.resolve();}}
 }
 const app={setName(){},setPath(){},getPath:()=>'/isolated-fixture'};
 const backend={async start(){starts++;return {origin:'http://127.0.0.1:31234'};},async request(){return {headers:{'set-cookie':['forge_session=fixture']}};}};
 const electron={app,BrowserWindow:Window};
 vm.runInNewContext(source+'\nexports.h={createWindow,launchBackend,getWindow:()=>window,configure(b){backend=b;browserSession={cookies:{async set(){}}};secrets={};}};',{
  exports,__dirname:path.dirname(filename),process,console,setImmediate,URL,AbortController,
  require:name=>name==='electron'?electron:localRequire(name),
 },{filename});
 exports.h.configure(backend);
 return {...exports.h,get starts(){return starts;}};
}

test('fast existing-data startup waits for initial boot load instead of aborting it with a second navigation',async()=>{
 const h=harness();h.createWindow();const win=h.getWindow();
 const launched=h.launchBackend();
 const checked=assert.doesNotReject(launched);
 await Promise.resolve();
 assert.equal(win.loads.length,1,'the initial boot navigation must be reused while pending');
 assert.equal(h.starts,0);
 win.finish();await checked;
 assert.equal(h.starts,1);assert.equal(win.loads.length,2);
 assert.match(win.loads[0],/\/boot\.html$/);assert.equal(win.loads[1],'http://127.0.0.1:31234');
});

test('completed boot page is reused during backend launch',async()=>{
 const h=harness();h.createWindow();const win=h.getWindow();win.finish();
 await h.launchBackend();
 assert.equal(win.loads.filter(url=>url.endsWith('/boot.html')).length,1);
 assert.equal(h.starts,1);
});

test('a real initial page failure propagates to startup and does not start the backend',async()=>{
 const h=harness();h.createWindow();
 const failure=Object.assign(new Error('ERR_FILE_NOT_FOUND (-6) loading boot.html'),{errno:-6});
 const rejected=assert.rejects(h.launchBackend(),error=>error===failure);
 h.getWindow().finish(failure);await rejected;
 assert.equal(h.starts,0);assert.equal(h.getWindow().loads.length,1);
});
