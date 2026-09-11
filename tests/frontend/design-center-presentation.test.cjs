const assert=require('node:assert/strict'),test=require('node:test'),fs=require('node:fs'),vm=require('node:vm');
const ts=require('../../frontend/vendor/typescript.cjs');
const jsx=(type,props)=>({type,props:props||{}});
const nodes=value=>Array.isArray(value)?value.flatMap(nodes):value&&typeof value==='object'?[value,...nodes(value.props?.children)]:[];
const text=value=>Array.isArray(value)?value.map(text).join(''):value&&typeof value==='object'?text(value.props?.children):value==null?'':String(value);
const button=(tree,label)=>{const result=nodes(tree).find(n=>n.type==='button'&&text(n)===label);assert.ok(result,label);return result;};
const compile=source=>ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText;
const office={id:'office-1',kind:'office',name:'当前研发布局',version:2,status:'published',digest:'exact-office-version',published_version:2,config:{layout_version:2,floors:[{floor:1,name:'研发楼层',department:'研发部',zones:[{id:'work',name:'研发区',kind:'work'}]}],teams:[{id:'team',floor:1,name:'开发团队'}],desks:[],positions:{}}};
function harness(overrides={}){
 let cursor=0;const slots=[],effects=[],calls=[],navigation=[];
 const React={useState(initial){const i=cursor++;if(!(i in slots))slots[i]=typeof initial==='function'?initial():initial;return[slots[i],v=>slots[i]=typeof v==='function'?v(slots[i]):v];},useRef(initial){const i=cursor++;if(!(i in slots))slots[i]={current:initial};return slots[i];},useEffect(effect,deps){const i=cursor++;if(!(i in slots)||!deps||deps.some((v,k)=>!Object.is(v,slots[i][k]))){slots[i]=deps;effects.push(effect);}}};
 const props={admin:true,agents:[],models:[],providers:[],organization:{ref:{id:office.id,version:2,digest:office.digest},config:office.config},onRefresh:async()=>{},onModels:()=>navigation.push('models'),onSkills(){},onLaunch(){},...overrides};
 const exports={};
 vm.runInNewContext(compile(fs.readFileSync('frontend/src/components/DesignCenter.tsx','utf8').replace(/import\s+['"][^'"]+\.css['"];?/g,'')),{exports,console,require:id=>{
  if(id==='react')return React;if(id==='react/jsx-runtime')return{jsx,jsxs:jsx,Fragment:'Fragment'};
  if(id==='../types')return{roles:{planner:'规划',developer:'开发'}};
  if(id==='../api')return{jsonText:JSON.stringify,parseObject:JSON.parse,requestId:()=>assert.fail('rendering must not create an operation key'),api:async(url,method='GET')=>{
   calls.push({url,method});assert.equal(method,'GET','this presentation test never executes a write or model call');
   if(overrides.loadError&&url==='/design/assets')throw new Error(overrides.loadError);
   if(url==='/design/assets')return[office];if(url==='/model-hub')return{models:[],benchmarks:[],dimensions:[]};
   if(url==='/design/enterprise-policy')return overrides.enterprise||{ref:null};
   if(url==='/design/assets/'+office.id)return{...office,versions:[office]};
   assert.fail('Unexpected read '+url);
  }};
  return new Proxy({},{get:(_,key)=>String(key)});
 }});
 const render=()=>{cursor=0;return exports.default(props);};
 return{render,calls,navigation,async loaded(){render();effects.splice(0).forEach(e=>e());for(let i=0;i<6;i++)await Promise.resolve();return render();}};
}

test('Design Center opens with configuration tabs rather than a philosophy banner',async()=>{
 const h=harness(),tree=await h.loaded();
 const children=nodes(tree.props.children);assert.equal(children[0].type,'nav');assert.equal(children[0].props['aria-label'],'设计中心分类');
 assert.ok(!nodes(tree).some(n=>['design-hero','design-lifecycle'].includes(n.props.className)));
 assert.doesNotMatch(text(tree),/HUMAN IN CONTROL|最终决定权|自动生成 ≠ 自动生效/);
 assert.match(text(tree),/模型资产与能力画像/);button(tree,'供应商与协议配置').props.onClick();assert.deepEqual(h.navigation,['models']);
 assert.ok(h.calls.every(c=>c.method==='GET'));
});

test('every public design tab excludes fixed workflow creation while retaining organization configuration',async()=>{
 const h=harness();let tree=await h.loaded();const nav=nodes(tree).find(n=>n.props['aria-label']==='设计中心分类');
 const tabs=nodes(nav).filter(n=>n.type==='button'&&!n.props['aria-label']);assert.equal(tabs.length,5);assert.ok(!tabs.some(n=>text(n).includes('工作流')));
 for(const tab of tabs){tab.props.onClick();tree=h.render();assert.ok(!nodes(tree).some(n=>n.type==='button'&&/新建工作流|使用此编排/.test(text(n))));}
 assert.ok(h.calls.every(c=>c.method==='GET'));
});

test('organization entry keeps floor inventory and every edit/create action without repeated introductory prose',async()=>{
 const h=harness({initialTab:'office'}),tree=await h.loaded();
 for(const label of ['编辑当前楼层与团队','新增楼层／部门','新建独立布局方案'])assert.ok(button(tree,label));
 assert.match(text(tree),/研发部/);assert.match(text(tree),/1 个团队 · 1 个区域/);
 assert.doesNotMatch(text(tree),/每层一个部门|Design Mode|草稿可编辑，发布版本可用于新任务|修改只进入草稿/);
 assert.equal(nodes(tree).find(n=>n.props.className==='design-heading').props.children[0].type,'h3');
 const readOnly=await harness({initialTab:'office',admin:false}).loaded();
 assert.ok(!nodes(readOnly).some(n=>n.type==='button'&&/编辑当前楼层|新增楼层／部门|新建独立布局/.test(text(n))));
});

test('errors, active enterprise rules and exact-version publishing/organization consent remain visible',async()=>{
 const failed=await harness({loadError:'无法读取配置，请重试'}).loaded();assert.match(text(nodes(failed).find(n=>n.props.role==='alert')),/无法读取配置/);
 const h=harness({initialTab:'office',enterprise:{ref:{id:'policy-1',version:4}}});let tree=await h.loaded();
 assert.match(text(tree),/企业规则：policy-1 · v4/);assert.match(text(tree),/任务级模型覆盖仍需校验/);
 const cards=nodes(tree).find(n=>typeof n.type==='function'&&n.type.name==='AssetCards');assert.ok(cards);await cards.props.onOpen(office);tree=h.render();
 assert.match(text(tree),/我已核对部门、楼层、区域与工位/);assert.match(text(tree),/部门调整会影响后续运行的同部门通信对象/);
 assert.match(text(tree),/既有任务和工具权限不变/);assert.equal(button(tree,'应用办公室布局').props.disabled,true);
 assert.equal(button(tree,'撤销此发布版本').props.disabled,true);assert.ok(button(tree,'编辑并创建新版本'));
 assert.match(text(tree),/内容摘要 · exact-office-version/);
});

test('AI recommendation still requires explicit paid-call consent after a model is chosen',async()=>{
 const h=harness({initialTab:'team',models:[{id:'model-1',name:'测试模型',model_id:'test',provider_id:'provider-1',enabled:true}],providers:[{id:'provider-1',name:'测试供应商'}]});let tree=await h.loaded();
 nodes(tree).find(n=>n.props['aria-label']==='推荐需求').props.onChange({target:{value:'整理业务需求文档'}});
 nodes(tree).find(n=>typeof n.type==='function'&&n.type.name==='ModelSelect').props.onChange('model-1');tree=h.render();
 const consent=nodes(tree).find(n=>n.type==='label'&&text(n).includes('产生一次实际模型调用费用'));
 assert.ok(consent);assert.equal(nodes(consent).find(n=>n.type==='input').props.checked,false);
 assert.equal(button(tree,'AI 生成草稿方案').props.disabled,true);
 assert.match(text(tree),/允许推荐外部模型，并将需求发送给外部设计助手/);
 assert.ok(h.calls.every(c=>c.method==='GET'));
});

test('only the two requested page introductions are removed and empty subtitle paragraphs are not rendered',()=>{
 const source=fs.readFileSync('frontend/src/App.tsx','utf8'),exports={};
 vm.runInNewContext(compile(source+'\nexport const pageMetadataForTest=pages;'),{exports,require:()=>({})});
 assert.equal(exports.pageMetadataForTest.organization.subtitle,'');assert.equal(exports.pageMetadataForTest.design.subtitle,'');
 assert.match(exports.pageMetadataForTest.execution.subtitle,/权限审批/);assert.match(exports.pageMetadataForTest.users.subtitle,/项目成员关系决定可见范围/);
 assert.match(source,/pageInfo\.subtitle&&<p>\{pageInfo\.subtitle\}<\/p>/);
 assert.match(source,/page!=='organization'&&page!=='design'&&<span className="eyebrow">BUILD WITH YOUR AGENTS/);
});
