/** Browser fixture boundary: real React, StudioWorkspace, OfficeSessionWindow,
 * icons/types/assignment; only graphics, task data and external actions are stubbed.
 * Routes are fulfilled in memory; there is no server, app profile or production API.
 */
const fs=require('node:fs'),path=require('node:path');
const root=path.resolve(__dirname,'../../..');
const ts=require(path.join(root,'frontend/vendor/typescript.cjs'));
function createOfficeReactFixture(){
 const build=fs.readFileSync(path.join(root,'scripts/build-offline.mjs'),'utf8');
 const cssFiles=build.match(/const combinedCSS=\[([^\]]+)\]/)[1].match(/'([^']+\.css)'/g).map(s=>s.slice(1,-1));
 const css=cssFiles.map(file=>fs.readFileSync(path.join(root,'frontend/src',file),'utf8')).join('\n');
 const sources={};
 for(const relative of ['components/StudioWorkspace.tsx','components/StudioNavigation.tsx','components/OfficeSessionWindow.tsx','components/LiveOutputDock.tsx','components/TranscriptMarkdown.tsx','liveTranscript.ts','components/Icons.tsx','components/RunRecovery.tsx','types.ts','office/memberAssignment.ts','office/behavior.ts']){
  sources['/'+relative.replace(/\.tsx?$/,'')]=ts.transpileModule(fs.readFileSync(path.join(root,'frontend/src',relative),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;
 }
 const main=`import {React,ReactDOM,JSX} from '/vendor/react-runtime.js';
const sources=${JSON.stringify(sources)},cache={};
window.__fixtureCalls=[];
const forbidden=(name)=>(...args)=>{window.__fixtureCalls.push(name);throw Error('unexpected external action: '+name);};
const environment={quality:'balanced',preferences:{motion:false,day:true,density:'normal'},subscribe:()=>()=>{},setPreferences:forbidden('environment mutation')};
const taskPanel=p=>React.createElement('div',{'data-testid':'task-panel-fixture','data-agent-id':p.agent.id,'data-run-id':p.initialRunId||''},p.agent.name+' · fixture task records');
function Office(p){React.useEffect(()=>{p.controller.current={reset(){},focus(){},refresh(){},trace(){return[];},zoom(){}};return()=>{p.controller.current=null;};},[]);return React.createElement('div',{'data-testid':'office-scene-fixture',style:{position:'absolute',inset:0,pointerEvents:'none'}});}
const stubs={'/components/Office':{__esModule:true,default:Office},'/office/ambientEnvironment':{environment},'/graphics/officeWorld':{palette:{planner:0xb49ddb,developer:0x55b6ca,reviewer:0xda91b0}},'/api':{api:forbidden('API'),requestId:()=>''},'/components/EmployeeTaskPanel':{__esModule:true,default:taskPanel},'/components/OfficeRunReview':{__esModule:true,default:()=>null},'/components/UI':{Modal:()=>null,Badge:p=>React.createElement('span',null,p?.state||'')}};
stubs['/components/EmployeePortrait']={__esModule:true,default:p=>React.createElement('span',{'data-agent-id':p.view.id,'data-pose':p.view.pose,className:'employee-portrait'})};
function load(id){if(id.endsWith('.css'))return{};if(id==='react')return React;if(id==='react/jsx-runtime')return JSX;if(stubs[id])return stubs[id];if(cache[id])return cache[id];if(!sources[id])throw Error('missing fixture module '+id);const exports=cache[id]={};new Function('exports','require',sources[id])(exports,request=>load(request.startsWith('.')?new URL(request,'https://forge-layout.invalid'+id).pathname:request));return exports;}
const agents=[{id:'nova',name:'Nova',role:'developer',job_title:'后端工程师',department:'研发部',runtime:{},office_position:{x:0,z:0,floor:1,zone:'dev'}},{id:'echo',name:'Echo',role:'reviewer',job_title:'测试工程师',department:'研发部',runtime:{},office_position:{x:2,z:0,floor:1,zone:'qa'}}];
const props={agents,organization:{config:{floors:[],teams:[],positions:{}}},models:[],providers:[],projects:[{id:'project-fixture',name:'Local UI fixture'}],runs:[],detail:null,events:[],selectedRun:'',writer:true,admin:true,userName:'Fixture',onBoard:forbidden('board'),onNavigate:forbidden('navigation'),onCreateAgent:forbidden('create agent'),onEditAgent:forbidden('edit agent'),onSaveAgent:forbidden('save agent'),onCreateTask:forbidden('create task'),onSelectRun:forbidden('select run'),onTask:forbidden('task'),onRefresh:forbidden('refresh')};
ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(load('/components/StudioWorkspace').default,props));`;
 return {
  '/':{type:'text/html',body:'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><link rel="stylesheet" href="/styles.css"><body><div id="root"></div><script type="module" src="/main.js"></script></body></html>'},
  '/styles.css':{type:'text/css',body:css},'/main.js':{type:'text/javascript',body:main},
  '/vendor/react-runtime.js':{type:'text/javascript',body:fs.readFileSync(path.join(root,'frontend/vendor/react-runtime.js'),'utf8')},
 };
}
module.exports={createOfficeReactFixture};
