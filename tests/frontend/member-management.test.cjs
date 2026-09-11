const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const ts=require('../../frontend/vendor/typescript.cjs');
const jsx=(type,props)=>({type,props:props||{}}),plain=v=>JSON.parse(JSON.stringify(v));
const nodes=v=>Array.isArray(v)?v.flatMap(nodes):v&&typeof v==='object'?[v,...nodes(v.props?.children)]:[];
const text=v=>Array.isArray(v)?v.map(text).join(''):v&&typeof v==='object'?text(v.props?.children):v==null?'':String(v);
const field=(tree,label)=>{const n=nodes(tree).find(n=>n.props?.['aria-label']===label);assert.ok(n,label);return n;};
const button=(tree,label)=>{const n=nodes(tree).find(n=>n.type==='button'&&text(n)===label);assert.ok(n,label);return n;};
function load(file,mocks={}){const exports={},source=fs.readFileSync(file,'utf8').replace(/import\s+['"][^'"]+\.css['"];?/g,'');vm.runInNewContext(ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports,require:name=>{if(mocks[name])return mocks[name];if(name.endsWith('/types'))return{agentTitle:a=>a.job_title||a.role,roles:{}};if(name==='react/jsx-runtime')return{jsx,jsxs:jsx};if(name==='./UI'||name==='./Icons')return new Proxy({},{get:(_,k)=>String(k)});return load(path.resolve(path.dirname(file),name+'.ts'),mocks);}});return exports;}
function hooks(){let cursor=0;const slots=[];return{reset(){cursor=0;},useState(initial){const i=cursor++;if(!(i in slots))slots[i]=typeof initial==='function'?initial():initial;return[slots[i],v=>slots[i]=typeof v==='function'?v(slots[i]):v];},useEffect(){},useRef(value){const i=cursor++;return slots[i]||(slots[i]={current:value});}};}
const ops=load('frontend/src/office/organizationDesign.ts'),drafts=load('frontend/src/office/organizationDraft.ts');
const agents=['a','b','c'].map((id,i)=>({id,name:['林','周','陈'][i],role:'developer',job_title:'架构师',department:i<2?'研发部':'',model_profile_id:null,fallback_model_ids:[],runtime:{},version:1,skills:[]}));
let config=ops.addFloor({layout_version:2,floors:[],positions:{},teams:[],desks:[]});config.floors[0].department='研发部';
config.teams=[{id:'t1',name:'服务团队',floor:1,zone_ids:[],member_ids:['a'],headcount:1},{id:'t2',name:'客户端团队',floor:1,zone_ids:[],member_ids:['b'],headcount:1}];
config.desks=[{id:'d1',floor:1,agent_id:'a',team_id:'t1'},{id:'d2',floor:1,agent_id:'b',team_id:'t2'}];
const organization={ref:{id:'office',version:2,digest:'applied'},config};

test('offline distribution explicitly bundles the member directory stylesheet',()=>{
 const build=fs.readFileSync('scripts/build-offline.mjs','utf8');
 assert.match(build.match(/const combinedCSS=.*;/)?.[0]||'',/'agent-directory\.css'/,'CSS imports are stripped by offline build, so the combined stylesheet must include this file');
});

test('member directory filters independent same-floor teams and unassigned members',()=>{
 const react=hooks(),edits=[],C=load('frontend/src/components/AgentDirectory.tsx',{react}).default,props={agents,organization,models:[],providers:[],admin:true,onEdit:a=>edits.push(a),onCreate(){},onOrganization(){}};
 const render=()=>{react.reset();return C(props);},cards=tree=>nodes(tree).filter(n=>n.props?.['data-agent-id']);let tree=render();assert.equal(cards(tree).length,3);
 field(tree,'组织成员团队筛选').props.onChange({target:{value:'t2'}});tree=render();assert.deepEqual(cards(tree).map(n=>n.props['data-agent-id']),['b']);button(tree,'配置成员').props.onClick();assert.equal(edits[0],agents[1]);
 field(tree,'组织成员团队筛选').props.onChange({target:{value:'unassigned'}});tree=render();assert.deepEqual(cards(tree).map(n=>n.props['data-agent-id']),['c']);assert.match(text(tree),/未分配部门/);
 field(tree,'组织成员部门筛选').props.onChange({target:{value:'研发部'}});tree=render();assert.equal(cards(tree).length,0);
 field(tree,'组织成员部门筛选').props.onChange({target:{value:'unassigned'}});assert.equal(cards(render()).length,1);
});

test('current business department wins over old applied placement without mutating either',()=>{
 const {memberAssignment}=load('frontend/src/office/memberAssignment.ts'),person={...agents[0],department:'设计部'},before=plain(organization);
 const s=memberAssignment(person,organization);assert.equal(s.department,'设计部');assert.equal(s.team.id,'t1');assert.equal(s.spatialMismatch,true);assert.match(s.label,/设计部.*空间待调整/);assert.deepEqual(plain(organization),before);
 assert.equal(memberAssignment({...person,department:''},organization).department,'');
});

test('new-floor draft preserves applied version, other teams and required facilities',()=>{
 const latest={id:'office',name:'当前布局',kind:'office',version:3,status:'published',config:{...config,description:'not applied yet'}};
 const before=plain(organization),r=drafts.prepareOrganizationDraft(organization,latest,true);
 assert.equal(r.expectedVersion,3);assert.equal(r.asset.version,2);assert.equal(r.focusFloor,2);assert.equal(r.asset.config.floors.length,2);
 assert.equal(r.asset.config.description,undefined,'published but unapplied revisions must not silently replace current layout');
 assert.deepEqual(plain(r.asset.config.teams),plain(config.teams));assert.deepEqual(plain(organization),before);
 const kinds=r.asset.config.floors[1].zones.map(z=>z.kind);for(const kind of ['meeting','restroom','pantry'])assert.ok(kinds.includes(kind));
 assert.equal(r.asset.config.desks.length,2);assert.equal(r.asset.config.teams.length,2);
});

test('new-floor entry preserves latest pending draft and does not mutate its source',()=>{
 const latest={id:'office',name:'当前布局',kind:'office',version:4,status:'draft',config:plain(config)};latest.config.description='未发布编辑';
 const original=plain(latest),r=drafts.prepareOrganizationDraft(organization,latest,true);assert.equal(r.pending,true);assert.equal(r.expectedVersion,4);assert.equal(r.asset.config.description,'未发布编辑');assert.deepEqual(plain(latest),original);
 assert.throws(()=>drafts.prepareOrganizationDraft(organization,{...latest,id:'other'},true),/引用已改变/);
});

test('legacy position-only layouts cannot lose member positions via add-floor shortcut',()=>{
 assert.throws(()=>drafts.prepareOrganizationDraft({ref:null,config:{positions:{a:{floor:1,x:0,z:0}},floors:[]}},null,true),/旧布局/);
 const empty=drafts.prepareOrganizationDraft({ref:null,config:{positions:{},floors:[]}},null,true);assert.equal(empty.asset.config.floors.length,1);assert.equal(empty.asset.config.layout_version,2);
});

test('actual dedicated floor page opens add-floor editor using GET only and focuses new floor',async()=>{
 const react=hooks(),calls=[],latest={id:'office',kind:'office',name:'当前布局',version:2,status:'published',config};
 const mocks={react,'../api':{api:async url=>{calls.push(url);if(url==='/organization')return organization;if(url==='/design/assets/office')return latest;throw Error('unexpected '+url);}},'./Skills':{},'./RoleTemplates':{},'./Orchestration':{},'./OrganizationDesigner':{__esModule:true,default:'OrganizationDesigner'}};
 const C=load('frontend/src/components/DesignCenter.tsx',mocks).default,p={initialTab:'office',organization,admin:true,agents,models:[],providers:[]},render=()=>{react.reset();return C(p);};
 let tree=render();assert.match(text(tree),/新增楼层／部门/);button(tree,'新增楼层／部门').props.onClick();await new Promise(r=>setImmediate(r));tree=render();
 const editor=nodes(tree).find(n=>n.type==='OrganizationDesigner');assert.ok(editor);assert.equal(editor.props.readOnly,false);assert.equal(editor.props.initialFloor,2);assert.equal(editor.props.value.floors.length,2);
 assert.deepEqual(calls,['/organization','/design/assets/office']);assert.match(text(tree),/保存、发布并应用/);
});
