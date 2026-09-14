const assert=require('node:assert/strict');
const test=require('node:test');
const fs=require('node:fs');
const vm=require('node:vm');
const ts=require('../../frontend/vendor/typescript.cjs');
const moduleExports={};
const code=ts.transpileModule(fs.readFileSync('frontend/src/agentPresentation.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
vm.runInNewContext(code,{exports:moduleExports});

test('incomplete historical snapshots do not blank the client while preserving the live model binding',()=>{
 const agent={id:'a',name:'Atlas',role:'planner',job_title:'负责人',department:'研发部',model_profile_id:'current',tool_permissions:['read_file']};
 const merged=moduleExports.mergeAgentSnapshot(agent,{name:'历史 Atlas',models:[],tools:null});
 assert.equal(merged.name,'历史 Atlas');
 assert.equal(merged.model_profile_id,'current');
 assert.deepEqual(merged.tool_permissions,['read_file']);
});

test('snapshot model overrides use the first valid profile and ignore malformed entries',()=>{
 const agent={id:'a',name:'Atlas',role:'planner',model_profile_id:'current'};
 const merged=moduleExports.mergeAgentSnapshot(agent,{models:[null,{}, {profile_id:'frozen'}]});
 assert.equal(merged.model_profile_id,'frozen');
});
