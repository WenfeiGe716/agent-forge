const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const {createRequire}=require('node:module');
const filename=require.resolve('./organization-workspace.test.cjs'),local=createRequire(filename),studio={};
vm.runInNewContext(fs.readFileSync(filename,'utf8')+'\nObject.assign(exports,{harness,nodes,text});',{
 exports:studio,__dirname:path.dirname(filename),console,performance,Date,setImmediate,URL,
 require:Object.assign(id=>id==='node:test'?()=>{}:local(id),{resolve:local.resolve})
},{filename});

test('saved preflight draft has a quiet header action without submitting or replacing its contents',()=>{
 let resumed=0;const seeds=[],h=studio.harness('StudioWorkspace',{onResumeDraft:()=>resumed++,onCreateTask:seed=>seeds.push(seed)});
 const tree=h.render(),header=studio.nodes(tree).find(n=>n.props?.className==='studio-header glass');
 const action=studio.nodes(header).find(n=>n.type==='button'&&studio.text(n)==='继续草稿');
 assert.ok(action,'draft recovery belongs in the header, not a floating banner over the composer');
 action.props.onClick();assert.equal(resumed,1);assert.equal(seeds.length,0);
});

test('no resume action is invented when no saved draft exists',()=>{
 const tree=studio.harness('StudioWorkspace').render();
 assert.equal(studio.nodes(tree).find(n=>n.type==='button'&&studio.text(n)==='继续草稿'),undefined);
});

test('App preserves recovery state but has no persistent bottom overlay',()=>{
 const source=fs.readFileSync(path.join(__dirname,'../../frontend/src/App.tsx'),'utf8');
 assert.doesNotMatch(source,/className="run-draft-banner"/);
 assert.match(source,/if\(draft\)setRunDraft\(draft\)/,'navigating to fix configuration preserves the exact draft');
 assert.match(source,/onDone=\{id=>\{setRunDraft\(null\)/,'successful submission clears the submitted draft');
 assert.match(source,/onResumeDraft=\{/,'office can resume the same App-owned draft');
});
