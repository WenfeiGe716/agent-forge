const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const ts=require('../../frontend/vendor/typescript.cjs');
const options={compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}};
const submission={exports:{}};
vm.runInNewContext(ts.transpileModule(fs.readFileSync('frontend/src/runSubmission.ts','utf8'),options).outputText,{exports:submission.exports,module:submission,require:()=>({}),Error});
const {createRunSubmission,RunPreflightError}=submission.exports;
const file=ts.createSourceFile('App.tsx',fs.readFileSync('frontend/src/App.tsx','utf8'),ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
let handler;
function find(node){if(ts.isVariableDeclaration(node)&&node.name.getText(file)==='startOfficeTask')handler=node;ts.forEachChild(node,find);}
find(file);assert.ok(handler,'test the actual office submission error handler');
const compiled=ts.transpileModule('const '+handler.getText(file)+';exports.startOfficeTask=startOfficeTask;',options).outputText;

test('office send shows one actionable project restriction, retains model-specific failures, and never grants access',async()=>{
 const result={ready:false,checks:[
  ...['a','b','c','d'].map(agent_id=>({code:'project.external_models',status:'error',message:'重复授权限制',agent_id,model_profile_id:'model',provider_id:'provider'})),
  {code:'model.enabled',status:'error',message:'模型已停用',agent_id:'a',model_profile_id:'model',provider_id:'provider'},
 ]};
 const before=JSON.stringify(result),calls=[],user={id:'admin'},directSubmission={current:{}},exports={};
 vm.runInNewContext(compiled,{exports,RunPreflightError,user,writer:true,directSubmission,currentUserRef:{current:user},
  createRunSubmission:()=>createRunSubmission(async(...args)=>{calls.push(args);return result;},()=> 'test-key'),
  agents:[{id:'a',name:'Atlas'}],models:[{id:'model',name:'模型甲'}],providers:[{id:'provider',name:'供应商甲'}],
  setSelected:()=>assert.fail('failed preflight cannot select a run'),refresh:()=>assert.fail('failed preflight cannot refresh a created run'),
 });
 await assert.rejects(exports.startOfficeTask({project_id:'project',request:'需求',agent_ids:['a','b','c','d']}),error=>{
  assert.match(error.message,/任务尚未开始/);
  assert.equal((error.message.match(/当前项目未开启外部模型访问/g)||[]).length,1);
  assert.match(error.message,/项目配置.*后重试/);
  assert.match(error.message,/Atlas · 模型甲 · 供应商甲：模型已停用/);
  assert.doesNotMatch(error.message,/完全访问|重复授权限制/);
  return true;
 });
 assert.equal(calls.length,1);assert.equal(calls[0][0],'/execution/preflight');
 assert.equal(JSON.stringify(result),before);
});
