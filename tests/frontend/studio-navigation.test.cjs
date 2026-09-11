const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const ts=require('../../frontend/vendor/typescript.cjs');

function harness(){
 const source=ts.transpileModule(fs.readFileSync(path.resolve('frontend/src/components/StudioNavigation.tsx'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;
 let cursor=0,slots=[],effects=[],observerCallback=null,navWidth=1200,tree;
 const React={useState(initial){const i=cursor++;if(!(i in slots))slots[i]=typeof initial==='function'?initial():initial;return[slots[i],v=>{slots[i]=typeof v==='function'?v(slots[i]):v;}];},useRef(initial){const i=cursor++;if(!(i in slots))slots[i]={current:initial};return slots[i];},useCallback(fn){cursor++;return fn;},useEffect(fn){cursor++;effects.push(fn);}};
 const jsx=(type,props={})=>({type,props});
 const exports={};
 const context={exports,require(id){if(id==='react')return React;if(id==='react/jsx-runtime')return{jsx,jsxs:jsx};if(id==='react/jsx-dev-runtime')return{jsxDEV:jsx};if(id==='./Icons')return new Proxy({}, {get:(_,name)=>name});if(id==='../studio-navigation.css')return{};throw Error('unexpected import '+id);},console,window:{addEventListener(){},removeEventListener(){}},getComputedStyle:()=>({columnGap:'5px',gap:'5px'}),requestAnimationFrame:fn=>{fn();return 1;},cancelAnimationFrame(){}};
 context.ResizeObserver=class{constructor(fn){observerCallback=fn;}observe(){}disconnect(){}};
 vm.runInNewContext(source,context,{filename:'StudioNavigation.tsx'});
 function mount(node){if(Array.isArray(node)){node.forEach(mount);return;}if(!node||typeof node!=='object')return;if(typeof node.type==='string'){const props=node.props||{};const element={clientWidth:node.type==='nav'?navWidth:0,getBoundingClientRect:()=>({width: node.type==='button'?(props.className==='selected'?72:props.children?.[1]?.props?.children?.length*7+40||80):node.type==='summary'?65:0}),};if(typeof props.ref==='function')props.ref(element);else if(props.ref&&typeof props.ref==='object')props.ref.current=element;if(Array.isArray(props.children))props.children.forEach(mount);else mount(props.children);return;}const props=node.props||{};if(Array.isArray(props.children))props.children.forEach(mount);else mount(props.children);}
 function render(){cursor=0;effects=[];tree=context.exports.default({onBoard(){calls.push('board');},onNavigate:x=>calls.push(x)});mount(tree);const pending=effects.splice(0);pending.forEach(effect=>effect());return tree;}
 const calls=[];
 render();
 return {render(width){navWidth=width;render();observerCallback?.();return render();},calls,tree:()=>tree,visible(){const out=[];const walk=n=>{if(Array.isArray(n)){n.forEach(walk);return;}if(!n||typeof n!=='object')return;if(n.props?.className==='studio-navigation-measure')return;if(n.type==='details'){walk(n.props.children?.[0]);return;}if(n.type==='button'&&n.props?.children){const c=n.props.children;const label=Array.isArray(c)?c.at(-1)?.props?.children:null;if(typeof label==='string')out.push(label);return;}if(n.type==='summary'&&typeof n.props?.children==='string'){out.push(n.props.children);return;}const c=n.props?.children;if(Array.isArray(c))c.forEach(walk);else walk(c);};walk(tree);return out;}};
}

test('navigation keeps all entries when measured width fits and routes clicks',()=>{
 const h=harness();assert.deepEqual(h.visible().slice(0,11),['办公室','任务','楼层与部门','团队','项目','模型','角色模板','Skill商店','设计中心','工具与权限','任务看板']);
 const buttons=[];const walk=n=>{if(Array.isArray(n)){n.forEach(walk);return;}if(!n||typeof n!=='object')return;if(n.props?.className==='studio-navigation-measure')return;if(n.type==='button'){buttons.push(n);return;}const c=n.props?.children;if(Array.isArray(c))c.forEach(walk);else walk(c);};walk(h.tree());assert.equal(buttons.length,11);buttons[1].props.onClick();assert.deepEqual(h.calls,['board']);
});

test('navigation moves only a suffix into 更多 and restores it after resize',()=>{
 const h=harness();h.render(260);let labels=h.visible();assert.ok(labels.includes('办公室')&&labels.includes('更多'));assert.ok(!labels.includes('任务看板'));
 h.render(1000);labels=h.visible();assert.ok(labels.includes('任务看板'));assert.ok(!labels.includes('更多'));
});

test('navigation source uses measured resize handling and keyboard details menu',()=>{
 const source=fs.readFileSync(path.resolve('frontend/src/components/StudioNavigation.tsx'),'utf8');
 assert.match(source,/ResizeObserver/);assert.match(source,/addEventListener\('resize'/);assert.match(source,/<details className="studio-navigation-more">/);assert.match(source,/summary aria-label/);
 const css=fs.readFileSync(path.resolve('frontend/src/studio-navigation.css'),'utf8');assert.match(css,/studio-header \.studio-navigation-measure button/);assert.match(css,/display:flex!important/);
});
