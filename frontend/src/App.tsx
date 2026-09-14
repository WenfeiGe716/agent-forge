import SkillStore from './components/SkillStore';
import DesignCenter,{DesignAsset} from './components/DesignCenter';
import ProviderCenter from './components/ProviderCenter';
import RoleTemplates from './components/RoleTemplates';
import ExecutionCenter from './components/ExecutionCenter';
import {environment} from './office/ambientEnvironment';
import Setup from './components/Setup';
import SkillsPage from './components/Skills';
import CollaborationPage from './components/Collaboration';
import OrchestrationPage,{GraphView,GoalStatus} from './components/Orchestration';
import RunBoard from './components/RunBoard';
import TaskWorkspace from './components/TaskWorkspace';
import RunRecovery,{runRecoverySeed} from './components/RunRecovery';
import {useCallback,useEffect,useRef,useState} from 'react';
import {Activity,ArrowRight,ArrowUpRight,Box,Check,ChevronRight,Code2,Download,FileCode2,FolderGit2,GitBranch,LayoutDashboard,ListTree,LogOut,Network,Pause,Play,Plus,RefreshCw,Settings2,ShieldCheck,Square,Terminal,Users,Zap,CheckCircle2,AlertCircle,Server,ExternalLink,Search} from './components/Icons';
import {api,APIError,jsonText} from './api';
import {createRunSubmission,RunPreflightError} from './runSubmission';
import TeamExecutionPermissions from './components/TeamExecutionPermissions';
import {desktopBridge,taskNotifications} from './desktop';
import {Agent,Model,Project,Provider,Run,Task,User,ForgeEvent,Json,OrganizationLayout,agentTitle,roles,states,terminal} from './types';
import {mergeAgentSnapshot} from './agentPresentation';
import {Badge,Modal,Field,Empty} from './components/UI';
import Office from './components/Office';
import StudioWorkspace from './components/StudioWorkspace';
import TaskGraph from './components/TaskGraph';
import {AgentForm,ModelForm,ProjectForm,ProviderForm,RunForm,SettingsForm,UserForm} from './components/Forms';

import AgentDirectory from './components/AgentDirectory';
type Page='organization'|'execution'|'design'|'role-templates'|'skill-store'|'skills'|'collaboration'|'board'|'orchestrations'|'workspace'|'projects'|'agents'|'models'|'settings'|'audit'|'users';
type Dialog={kind:string;item?:any;suggested?:Json}|null;
const pages:Record<Page,{title:string;subtitle:string;icon:typeof Activity}>={
 organization:{title:'楼层与部门',subtitle:'',icon:Box},
 execution:{title:'工具与权限',subtitle:'',icon:Terminal},
 design:{title:'设计中心',subtitle:'',icon:ShieldCheck},
 'role-templates':{title:'角色模板库',subtitle:'谁负责什么：浏览专业职责模板，选择后进入团队设计，不安装为 Skill。',icon:Users},
 'skill-store':{title:'Skill商店',subtitle:'可信来源、方法与技能、审核安装和版本绑定。',icon:Box},
 skills:{title:'Skill能力库',subtitle:'能力包版本、审核、成员绑定与实际加载记录。',icon:Zap},
 collaboration:{title:'协作工作台',subtitle:'定向需求、决策、会议、行动项和成员会话，共享一条可追溯链。',icon:Users},
 board:{title:'任务看板',subtitle:'按真实运行状态聚合任务，筛选、追踪、审批与处理阻塞。',icon:ListTree},
 orchestrations:{title:'多Agent编排',subtitle:'选定成员、验收目标、证据复核与异常保护，保存可执行模板。',icon:GitBranch},
 workspace:{title:'组织工作台',subtitle:'从需求到文档、代码与可验收成果，每一步都有据可查。',icon:LayoutDashboard},
 projects:{title:'项目工作区',subtitle:'本机执行为新项目默认；成员在独立任务副本中协作，保留可审查的集成成果。',icon:FolderGit2},
 agents:{title:'组织成员',subtitle:'业务岗位、执行权限与模型独立配置。把合适的任务交给合适的Agent。',icon:Users},
 models:{title:'供应商与模型',subtitle:'每个供应商连接拥有独立模型目录；完整发现、人工配置、明确绑定。',icon:Network},
 settings:{title:'运行配置',subtitle:'平台默认 → 项目 → 角色 → Agent → 单次运行；逐字段覆盖。',icon:Settings2},
 audit:{title:'操作审计',subtitle:'查看配置变更、成员管理和登录记录。任务执行记录位于各运行时间线。',icon:ShieldCheck},
 users:{title:'团队账号',subtitle:'内部团队使用，不开放注册。项目成员关系决定可见范围。',icon:Users}
};
const eventNames:Record<string,string>={'agent.message':'Agent发送消息','agent.message.delivered':'消息进入接收者上下文','task.delegated':'负责人委派任务','task.handoff':'上游成果交接','run.created':'创建团队任务','run.state':'运行状态变化','task.prepared':'准备独立工作区','agent.started':'Agent开始执行','model.started':'模型开始响应','model.delta':'模型输出增量','model.completed':'模型调用完成','model.fallback':'启用备用模型','tool.started':'发起工具调用','tool.completed':'工具返回结果','task.finished':'提交阶段成果','task.integrated':'变更已集成','plan.ready':'计划等待批准','check.completed':'项目验收结果','artifacts.ready':'可下载成果已生成','control.requested':'提交运行控制','decision.requested':'提交人工决定'};
const friendlyEvent=(e:ForgeEvent)=>e.data?.summary||e.data?.title||(e.data?.status?states[e.data.status]:null)||e.data?.tool||e.data?.command||e.data?.action||'';

function Login({onLogin}:{onLogin:(u:User)=>void}){
 const[name,setName]=useState('admin'),[password,setPassword]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState('');
 return <div className="login-page" data-testid="login-page"><div className="login-story"><div className="brand"><span className="brand-logo"><GitBranch size={23}/></span><div>Agent Forge<small>协作工坊</small></div></div><span className="eyebrow">YOUR LOCAL AI ORGANIZATION</span><h1>一个目标。<br/>一支真正执行的<br/><em>AI协作组织。</em></h1><p>真实工具、明确权限、独立审查。<br/>让协作过程可见，让最终成果可验证。</p><div className="login-proof"><span><Server size={15}/>本地部署</span><span><Network size={15}/>多模型协作</span><span><ShieldCheck size={15}/>人工掌控</span></div></div><form className="login-card" onSubmit={async e=>{e.preventDefault();setBusy(true);setError('');try{onLogin(await api('/auth/login','POST',{username:name,password}));}catch(e){setError((e as Error).message);}finally{setBusy(false);}}}><div className="local-label"><i/>LOCAL WORKSPACE</div><h2>登录协作工坊</h2><p>使用平台账号登录，与模型供应商凭据无关。</p><Field label="用户名"><input autoComplete="username" required value={name} onChange={e=>setName(e.target.value)}/></Field><Field label="密码"><input type="password" autoComplete="current-password" required value={password} onChange={e=>setPassword(e.target.value)}/></Field>{error&&<div className="form-error">{error}</div>}<button disabled={busy} className="button primary">{busy?'正在登录…':'登录工作空间'}<ArrowRight size={17}/></button><small>首次登录请使用启动终端显示的账号密码。供应商可在登录后分别添加。</small></form></div>;
}

type DetailScope={id:string;user:User|null;sequence:number};
/** Polling, event refreshes and manual refreshes share this one ordering gate. */
export function useRunDetail(selected:string,user:User|null){
 const gate=useRef<DetailScope|null>(null);
 if(gate.current?.id!==selected||gate.current?.user!==user)gate.current={id:selected,user,sequence:0};
 const scope=gate.current!;
 const [stored,setStored]=useState<{scope:DetailScope;run:Run}|null>(null);
 useEffect(()=>{gate.current=scope;return()=>{scope.sequence++;if(gate.current===scope)gate.current=null;};},[scope]);
 const load=useCallback(async()=>{
  if(!scope.user||!scope.id||gate.current!==scope)return;
  const sequence=++scope.sequence;
  const current=()=>gate.current===scope&&scope.sequence===sequence;
  try{
   const run=await api<Run>('/runs/'+encodeURIComponent(scope.id));
   if(!current())return;
   if(run?.id!==scope.id)throw new Error('收到的任务详情与当前任务不一致，请重新刷新。');
   setStored({scope,run});
   return run;
  }catch(error){if(current())throw error;}
 },[scope]);
 // Scope identity also prevents A → B → A and logout/login from reviving old data.
 return {detail:stored?.scope===scope?stored.run:null,load};
}

export default function App(){
 const desktop=desktopBridge();
 const lastDesktopRuns=useRef(new Map<string,string>());
 const[showSetup,setShowSetup]=useState(false),[connectionBusy,setConnectionBusy]=useState(false);
 const[pendingTeam,setPendingTeam]=useState<DesignAsset|null>(null),[storeTab,setStoreTab]=useState<'browse'|'sources'>('browse');
 const[workspaceMode,setWorkspaceMode]=useState<'studio'|'board'>('studio');
 const[organization,setOrganization]=useState<OrganizationLayout|null>(null);
 const[user,setUser]=useState<User|null>(null),[booting,setBooting]=useState(true),[hydrating,setHydrating]=useState(false),[page,setPage]=useState<Page>('workspace');
 const[providers,setProviders]=useState<Provider[]>([]),[models,setModels]=useState<Model[]>([]),[agents,setAgents]=useState<Agent[]>([]),[projects,setProjects]=useState<Project[]>([]),[runs,setRuns]=useState<Run[]>([]),[settings,setSettings]=useState<Json|null>(null),[users,setUsers]=useState<User[]>([]),[audit,setAudit]=useState<ForgeEvent[]>([]);
 const[selected,setSelected]=useState(''),[events,setEvents]=useState<ForgeEvent[]>([]),[view,setView]=useState<'office'|'graph'>('office');
 const {detail,load:loadDetail}=useRunDetail(selected,user);
 const[runDraft,setRunDraft]=useState<Json|null>(null);
 const[dialog,setDialog]=useState<Dialog>(null),[toast,setToast]=useState<{text:string;error:boolean}|null>(null),[decisionNote,setDecisionNote]=useState(''),[live,setLive]=useState(false),[search,setSearch]=useState(''),[busy,setBusy]=useState(false);
 const[codexRuntimeEnabled,setCodexRuntimeEnabled]=useState(false),[codexDefaultSchedulerEnabled,setCodexDefaultSchedulerEnabled]=useState(false);
 const currentUserRef=useRef(user);currentUserRef.current=user;
 const directSubmission=useRef<{user:User|null;send:ReturnType<typeof createRunSubmission>|null}>({user,send:null});
 if(directSubmission.current.user!==user)directSubmission.current={user,send:null};
 const notify=useCallback((text:string,error=false)=>{setToast({text,error});},[]);
 const fail=useCallback((e:unknown)=>{if(e instanceof APIError&&e.status===401)setUser(null);else notify((e as Error).message,true);},[notify]);
 const refresh=useCallback(async(u:User|null=user)=>{if(!u)return;try{const [p,m,a,pr,r,s,o]=await Promise.all([api<Provider[]>('/providers'),api<Model[]>('/models'),api<Agent[]>('/agents'),api<Project[]>('/projects'),api<Run[]>('/runs'),api<Json>('/settings'),api<OrganizationLayout>('/organization')]);if(currentUserRef.current?.id!==u.id)return;setProviders(p);setModels(m);setAgents(a);setProjects(pr);setRuns(r);setSettings(s);setOrganization(o);if(u.role==='admin'){const users=await api<User[]>('/users');if(currentUserRef.current?.id===u.id)setUsers(users);}}catch(e){fail(e);}finally{if(currentUserRef.current?.id===u.id)setHydrating(false);}},[user,fail]);
 useEffect(()=>{let alive=true;api<User>('/auth/me').then(u=>{if(alive)setUser(u);}).catch(()=>{}).finally(()=>{if(alive)setBooting(false);});return()=>{alive=false;};},[]);
 useEffect(()=>{if(user){setHydrating(true);void refresh(user);}else{setHydrating(false);setPage('workspace');setWorkspaceMode('studio');setProviders([]);setModels([]);setAgents([]);setProjects([]);setRuns([]);setUsers([]);setAudit([]);setSettings(null);setSelected('');setEvents([]);setDialog(null);setRunDraft(null);setShowSetup(false);setToast(null);setPendingTeam(null);setStoreTab('browse');setOrganization(null);}},[user]);
 useEffect(()=>{if(!user){setCodexRuntimeEnabled(false);setCodexDefaultSchedulerEnabled(false);return;}api<Json>('/runtime/status').then(status=>{setCodexRuntimeEnabled(status.task_execution_enabled===true);setCodexDefaultSchedulerEnabled(status.default_scheduler_enabled===true);}).catch(()=>{setCodexRuntimeEnabled(false);setCodexDefaultSchedulerEnabled(false);});},[user]);
 useEffect(()=>{if(!desktop)return;if(!user){lastDesktopRuns.current.clear();return;}for(const task of taskNotifications(lastDesktopRuns.current,runs))void desktop.notifyTask(task).catch(()=>{});lastDesktopRuns.current=new Map(runs.map(r=>[r.id,r.status]));},[runs,user,desktop]);
 useEffect(()=>{if(!user)return;const timer=setInterval(()=>{Promise.all([api<Run[]>('/runs'),api<Json>('/settings')]).then(([r,s])=>{setRuns(r);setSettings(s);}).catch(fail);},6000);return()=>clearInterval(timer);},[user,fail]);
 useEffect(()=>{if(toast){const t=setTimeout(()=>setToast(null),6500);return()=>clearTimeout(t);}},[toast]);
 useEffect(()=>{if(!selected&&runs.length)setSelected(runs[0].id);},[runs,selected]);
 useEffect(()=>{if(page==='audit'&&user?.role==='admin')api<ForgeEvent[]>('/audit').then(setAudit).catch(fail);},[page,user,fail]);
 useEffect(()=>{
  if(!user||!selected){setEvents([]);return;}
  let alive=true;let source:EventSource|null=null;let refreshTimer:ReturnType<typeof setTimeout>|undefined;
  setEvents([]);setDecisionNote('');setLive(false);
  const update=()=>loadDetail().catch(fail);
  void update();
  const load=async()=>{let cursor=0;let collected:ForgeEvent[]=[];let more=true;
    while(more&&alive){const page=await api<{events:ForgeEvent[];has_more:boolean}>(`/runs/${selected}/events?after=${cursor}&limit=1000`);collected=[...collected,...page.events].slice(-250);if(page.events.length)cursor=page.events.at(-1)!.id;more=page.has_more;}
    if(!alive)return;setEvents(collected);source=new EventSource(`/api/runs/${selected}/stream?after=${cursor}`);
    source.onopen=()=>{if(alive)setLive(true);};source.onerror=()=>{if(alive)setLive(false);};
    source.onmessage=e=>{if(!alive)return;try{const event=JSON.parse(e.data) as ForgeEvent;setEvents(old=>old.some(x=>x.id===event.id)?old:[...old,event].slice(-250));if(refreshTimer)clearTimeout(refreshTimer);refreshTimer=setTimeout(update,150);}catch{notify('收到无效事件，正在重新同步',true);}};
    source.addEventListener('revoked',()=>{source?.close();setUser(null);});
  };load().catch(fail);
  const poll=setInterval(update,6000);
  return()=>{alive=false;source?.close();clearInterval(poll);if(refreshTimer)clearTimeout(refreshTimer);};
 },[selected,user,fail,notify,loadDetail]);

 useEffect(()=>{if(!user){environment.stop();return;}environment.start(user.id);const save=()=>environment.save();window.addEventListener('pagehide',save);return()=>{window.removeEventListener('pagehide',save);environment.stop();};},[user?.id]);

 const admin=user?.role==='admin',writer=admin||user?.role==='developer';
 const configured=agents.filter(a=>a.model_profile_id).length;
 const modelLabel=(id:string|null)=>{const m=models.find(m=>m.id===id);return m?`${providers.find(p=>p.id===m.provider_id)?.name||'供应商'} / ${m.name}`:'尚未绑定模型';};
 const onDone=()=>{setDialog(null);void refresh();notify('配置已保存；已有运行继续使用原配置快照。');};
 const newRun=()=>setDialog({kind:'run',item:runDraft||undefined});
 const startOfficeTask=async(seed:Json)=>{
  const owner=user?.id,submission=directSubmission.current;
  if(!owner||!writer)throw new Error('当前账号不能创建任务。');
  try{
   submission.send ||= createRunSubmission();
   const run=await submission.send(seed,()=>currentUserRef.current===user&&directSubmission.current===submission);
   if(currentUserRef.current!==user||directSubmission.current!==submission)return false;
   submission.send.acknowledge(seed);
   setSelected(run.id);void refresh();
   // The backend owns the Codex process and persists its events. The renderer
   // only triggers it when the runtime feature flag is enabled; live output is
   // then delivered through the normal Run SSE stream.
   if(codexRuntimeEnabled&&!codexDefaultSchedulerEnabled)void fetch('/api/runs/'+encodeURIComponent(run.id)+'/codex-start',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Forge-Request':'1'},body:JSON.stringify({})}).then(async response=>{if(!response.ok&&response.status!==409&&response.status!==404)throw new Error('Codex 运行入口启动失败');if(response.body){const reader=response.body.getReader();while(!(await reader.read()).done){}}}).catch(error=>notify((error as Error).message,true));
   return {id:run.id};
  }catch(error){
   if(error instanceof RunPreflightError){
    const failedChecks=error.readiness.checks.filter(c=>c.status==='error');
    const externalModels=failedChecks.filter(c=>c.code==='project.external_models');
    const messages=failedChecks.filter(c=>c.code!=='project.external_models').map(c=>{
     const context=[c.agent_id&&(agents.find(a=>a.id===c.agent_id)?.name||'成员 '+c.agent_id.slice(0,8)),c.model_profile_id&&(models.find(m=>m.id===c.model_profile_id)?.name||'模型 '+c.model_profile_id.slice(0,8)),c.provider_id&&(providers.find(p=>p.id===c.provider_id)?.name||'供应商')].filter(Boolean).join(' · ');
     const message=c.code==='model.tools'
       ? '当前模型配置尚未验证能否发起工具调用；工具实际由 Agent Forge 本机执行。可以直接验证，成功后会自动启用并重试需求。'
       : c.message;
     return (context?context+'：':'')+message;
    });
    if(externalModels.length){
     messages.unshift('当前项目未开启外部模型访问。请到“项目”→“项目配置”勾选“允许将本项目必要代码与上下文发送给已配置的外部模型”后重试。');
    }
    const toolCheck=failedChecks.find(c=>c.code==='model.tools'&&c.model_profile_id);
    throw Object.assign(new Error('暂时无法发送：'+(messages.join('\n')||'执行条件未满足，请检查配置。')),
      {readiness:error.readiness,model_profile_id:toolCheck?.model_profile_id});
   }
   throw error;
  }
 };
 const runAction=async(action:string)=>{if(!detail)return;setBusy(true);try{await api(`/runs/${detail.id}/control`,'POST',{action});notify('控制请求已保存，将在下一执行边界生效。');setDialog(null);}catch(e){fail(e);}finally{setBusy(false);}};
 const decide=async(approved:boolean)=>{if(!detail)return;setBusy(true);const phase=detail.status==='awaiting_approval'?'plan':'acceptance';try{await api(`/runs/${detail.id}/decision/${phase}`,'POST',{approved,digest:phase==='plan'?detail.plan_digest:detail.artifact_digest,note:decisionNote});notify('决定已提交，等待编排器应用。');}catch(e){fail(e);}finally{setBusy(false);}};
 const activeRun=runs.filter(r=>!terminal.includes(r.status)&&!r.status?.startsWith('awaiting'));
 const waiting=runs.filter(r=>r.status?.startsWith('awaiting'));
 const taskList=detail?.tasks||[];
 const displayAgents=agents.map(a=>mergeAgentSnapshot(a,detail?.snapshot?.agents?.[a.id]));
 const navigateExecution=(target:string,context?:{project_id?:string;agent_id?:string;model_profile_id?:string;provider_id?:string})=>{const mapped=({providers:'models',setup:'execution'} as Record<string,string>)[target]||target;setPage((mapped in pages?mapped:'execution') as Page);if(admin&&target==='projects'&&context?.project_id){const project=projects.find(p=>p.id===context.project_id);if(project)setDialog({kind:'project',item:project});}if(admin&&target==='agents'&&context?.agent_id){const agent=agents.find(a=>a.id===context.agent_id);if(agent)setDialog({kind:'agent',item:agent});}if(admin&&target==='models'&&context?.model_profile_id){const model=models.find(m=>m.id===context.model_profile_id);if(model)setDialog({kind:'model',item:model});}else if(admin&&target==='providers'&&context?.provider_id){const provider=providers.find(p=>p.id===context.provider_id);if(provider)setDialog({kind:'provider',item:provider});}};
 const selectedTask=dialog?.kind==='task'?dialog.item as Task:null;
 // Navigation targets can arrive from persisted runtime errors or older
 // clients. Keep an unknown target on a valid page instead of rendering a
 // blank shell when a stale target is received.
 const pageInfo=pages[page]||pages.workspace;
 const studio=page==='workspace'&&workspaceMode==='studio';
 if(booting|| (user&&hydrating))return <div className="boot"><span className="brand-logo"><GitBranch/></span>{booting?'正在连接本地工作空间…':'正在加载工作空间数据…'}</div>;
 if(!user)return <Login onLogin={u=>{setPage('workspace');setWorkspaceMode('studio');setSelected('');setShowSetup(false);setDialog(null);setPendingTeam(null);setStoreTab('browse');setUser(u);}}/>;

 // Authentication alone controls entry. Provider/model readiness is checked per run, never a landing-page gate.

 return <div className={`app-shell ${studio?'is-studio':''}`}>
  <aside className="sidebar"><div className="brand"><span className="brand-logo"><GitBranch size={21}/></span><div>Agent Forge<small>协作工坊</small></div></div><div className="workspace-selector"><span className="workspace-avatar">F</span><div>本地协作组织<small>INTERNAL WORKSPACE</small></div><ChevronRight size={14}/></div><span className="nav-caption">工作空间</span><nav>{(['workspace','organization','design','execution','collaboration','board','projects','agents','role-templates','skill-store','skills','models','settings'] as Page[]).map(p=>{const Icon=pages[p].icon;return <button key={p} onClick={()=>{setPage(p);if(p==='workspace')setWorkspaceMode('studio');}} className={page===p?'active':''}><Icon size={18}/>{p==='workspace'?'工作台':p==='agents'?'团队与成员':pages[p].title}{p==='agents'&&<small>{agents.length}</small>}</button>;})}</nav>{admin&&<><span className="nav-caption">管理与治理</span><nav>{(['users','audit'] as Page[]).map(p=>{const Icon=pages[p].icon;return <button key={p} onClick={()=>{setPage(p);if(p==='workspace')setWorkspaceMode('studio');}} className={page===p?'active':''}><Icon size={18}/>{pages[p].title}</button>;})}</nav></>}
   <div className="sidebar-bottom"><div className="local-card"><Server size={18}/><div>本地优先<small>代码与成果保留在你的环境</small></div></div><div className="account"><span className="avatar">{user.username.slice(0,1).toUpperCase()}</span><div>{user.username}<small>{admin?'管理员':writer?'开发者':'只读成员'}</small></div><button aria-label="退出登录" className="icon-button" onClick={async()=>{try{await api('/auth/logout','POST',{});setUser(null);setSelected('');}catch(e){fail(e);}}}><LogOut size={17}/></button></div></div>
  </aside>
  <main><header className="topbar"><div className="breadcrumb">{page==='workspace'&&<button className="button secondary small" onClick={()=>setWorkspaceMode('studio')}><Box size={13}/>返回3D办公室</button>}工作空间<ChevronRight size={14}/><b>{pageInfo.title}</b></div><div className="topbar-right">{runDraft&&!dialog&&!showSetup&&<button type="button" className="draft-resume-action" onClick={newRun}><FileCode2 size={14}/>继续草稿</button>}<span className="local-label"><i/>LOCAL INSTANCE</span><span className="version">v0.17.0</span><button className="icon-button" aria-label="刷新" onClick={()=>void refresh()}><RefreshCw size={16}/></button></div></header>
   <section className={`page ${page==='workspace'&&!studio?'task-view-page':''}`}><div className="page-heading"><div>{page!=='organization'&&page!=='design'&&<span className="eyebrow">BUILD WITH YOUR AGENTS</span>}<h1>{pageInfo.title}</h1>{pageInfo.subtitle&&<p>{pageInfo.subtitle}</p>}</div><div className="heading-actions">{page==='workspace'&&<>{admin&&<button className="button secondary" onClick={()=>setDialog({kind:'project'})}><FolderGit2 size={16}/>导入项目</button>}{writer&&<button className="button primary" onClick={newRun}><Plus size={17}/>发起任务</button>}</>}{page==='projects'&&admin&&<button className="button primary" onClick={()=>setDialog({kind:'project'})}><Plus size={16}/>导入项目</button>}{page==='agents'&&<button className="button secondary" onClick={()=>setDialog({kind:'execution-policies'})}><ShieldCheck size={16}/>执行权限</button>}{page==='agents'&&admin&&<button className="button primary" onClick={()=>setDialog({kind:'agent'})}><Plus size={16}/>添加Agent</button>}{page==='models'&&admin&&<><button className="button secondary" onClick={()=>setShowSetup(true)}><Zap size={16}/>连接并测试</button><button className="button primary" onClick={()=>setDialog({kind:'provider'})}><Plus size={16}/>添加供应商</button></>}{page==='users'&&admin&&<button className="button primary" onClick={()=>setDialog({kind:'user'})}><Plus size={16}/>创建账号</button>}</div></div>
   {settings?.validation_mode&&<div className="validation-banner" role="status">验证环境：脚本化模型服务，仅验证编排、接口与交互，不代表真实大模型效果。命令执行仅允许固定测试夹具。</div>}{settings&&!settings.dispatcher?.startsWith('connected')&&<div className="service-warning"><AlertCircle size={16}/><span>编排连接：{settings.dispatcher}。任务会保留在队列中，不会模拟执行。</span></div>}

   {studio&&<StudioWorkspace onResumeDraft={runDraft&&!dialog&&!showSetup?newRun:undefined} userName={user.username} onLogout={async()=>{try{await api('/auth/logout','POST',{});setUser(null);setSelected('');}catch(e){fail(e);}}} agents={agents} organization={organization} models={models} providers={providers} projects={projects} runs={runs} detail={detail} events={events} selectedRun={selected} writer={!!writer} admin={admin} validationMode={settings?.validation_mode} onBoard={()=>{setPage('workspace');setWorkspaceMode('board');}} onNavigate={navigateExecution} onCreateAgent={()=>setDialog({kind:'agent'})} onEditAgent={a=>setDialog({kind:'agent',item:a})} onSaveAgent={async(a,patch)=>{const {id,version,...body}=a;await api('/agents/'+id,'PUT',{...body,...patch});await refresh();}} onCreateTask={startOfficeTask} onRepairModel={async modelId=>{await api('/models/'+modelId+'/tool-test','POST',{});await refresh();}} onConfigureTask={seed=>setDialog({kind:'run',item:seed})} onExecutionPermissions={context=>setDialog({kind:'execution-policies',item:context})} onSelectRun={setSelected} onTask={t=>setDialog({kind:'task',item:t})} onRefresh={()=>void refresh()}/>}
   {page==='workspace'&&!studio&&<TaskWorkspace run={detail} runs={runs} selected={selected} projects={projects} agents={agents} events={events} writer={!!writer} admin={admin} busy={busy} live={live} onSelect={setSelected} onNew={seed=>seed?setDialog({kind:'run',item:seed}):newRun()} onResume={()=>void runAction('resume')} onControl={action=>void runAction(action)} onCancel={()=>setDialog({kind:'cancel'})} onSnapshot={()=>setDialog({kind:'snapshot',item:detail?.snapshot})} onTask={task=>setDialog({kind:'task',item:task})} onCollaboration={()=>setPage('collaboration')} onConfigure={()=>setPage('agents')} onNavigate={navigateExecution} onRefresh={async()=>{await refresh();await loadDetail();}}/>}

   {(page==='design'||page==='organization')&&<DesignCenter key={page} initialTab={page==='organization'?'office':'hub'} organization={organization} admin={admin} agents={agents} models={models} providers={providers} onRefresh={refresh} onModels={()=>setPage('models')} onSkills={()=>setPage('skills')} onLaunch={seed=>setDialog({kind:'run',item:seed})} openAsset={pendingTeam} onAssetOpened={()=>setPendingTeam(null)}/>}
   {page==='role-templates'&&<RoleTemplates admin={admin} onOpenTeam={asset=>{setPendingTeam(asset);setPage('design');}} onSources={()=>{setStoreTab('sources');setPage('skill-store');}} onSkills={()=>{setStoreTab('browse');setPage('skill-store');}}/>}
   {page==='skill-store'&&<SkillStore admin={admin} agents={agents} onRefresh={refresh} onLibrary={()=>setPage('skills')} onRoles={()=>setPage('role-templates')} initialTab={storeTab}/>}
   {page==='skills'&&<SkillsPage admin={admin}/>}
   {page==='collaboration'&&<CollaborationPage runs={runs} detail={detail} agents={agents} writer={!!writer} selected={selected} onSelect={setSelected} onInspect={()=>{setPage('workspace');setWorkspaceMode('board');}} onNewTask={seed=>setDialog({kind:'run',item:seed})} onResume={()=>void runAction('resume')} onOffice={()=>{setPage('workspace');setWorkspaceMode('studio');}}/>}
   {page==='projects'&&(projects.length?<div className="card-grid">{projects.map(p=><article className="entity-card" key={p.id}><div className="card-top"><span className="entity-icon"><FolderGit2/></span><span className="pill">本地快照</span></div><h2>{p.name}</h2><span className="pill">{p.archived?"已归档":"开发中"}</span><p className="mono">{p.import_path}</p><dl><div><dt>执行位置</dt><dd>{p.execution_mode==='local'?'本机项目工作区':'隔离容器'}</dd></div><div><dt>执行权限</dt><dd>{{ask:'请求审批',auto:'帮我批准',full:'完全访问'}[p.permission_profile||'ask']}</dd></div><div><dt>验收命令</dt><dd>{p.check_commands.length} 条</dd></div><div><dt>代码外发</dt><dd>{p.allow_external_models?'已授权':'禁止'}</dd></div></dl><footer><code>{p.base_commit.slice(0,10)}</code>{admin&&<button className="text-button" onClick={()=>setDialog({kind:'project',item:p})}>项目配置<ArrowUpRight size={14}/></button>}</footer></article>)}</div>:<Empty title="导入第一个项目" description="任何编程语言、框架或项目类型均可接入；工具链和验收命令由项目自行指定。" action={admin?<button className="button primary" onClick={()=>setDialog({kind:'project'})}>导入项目</button>:undefined}/>)}
   {page==='agents'&&<AgentDirectory admin={admin} agents={agents} models={models} providers={providers} organization={organization} onEdit={a=>setDialog({kind:'agent',item:a})} onCreate={()=>setDialog({kind:'agent'})} onOrganization={()=>setPage('organization')}/>}
   {page==='execution'&&<ExecutionCenter admin={admin} projects={projects} runs={runs} initialProjectId={detail?.project_id} onNavigate={navigateExecution} onRefresh={()=>refresh()}/>}{page==='board'&&<RunBoard runs={runs} projects={projects} onCreate={writer?newRun:undefined} onSelect={id=>{setSelected(id);setPage('workspace');setWorkspaceMode('board');}}/>}
   {page==='orchestrations'&&<OrchestrationPage agents={agents} admin={admin} onLaunch={t=>setDialog({kind:'run',item:{execution_strategy:'autonomous',agent_ids:t.graph.participants||[]}})}/>}
   {page==='models'&&<ProviderCenter admin={admin} providers={providers} models={models} agents={agents} onRefresh={refresh} onEditProvider={provider=>setDialog({kind:'provider',item:provider})} onEditModel={model=>setDialog({kind:'model',item:model})} onConfigureModel={suggested=>setDialog({kind:'model',suggested})} onAgents={()=>setPage('agents')}/>}
   {page==='settings'&&settings&&<section className="panel settings-panel">{admin?<SettingsForm key={JSON.stringify(settings.runtime)+JSON.stringify(settings.roles)} initial={settings} onDone={()=>{void refresh();notify('默认运行配置已更新。');}}/>:<pre className="json-view">{jsonText(settings)}</pre>}<div className="installation-note"><Server size={17}/><p>安装级Worker并发上限：{settings.installation.max_active_activities}；单项目导入上限：{settings.installation.max_project_mb} MB。Docker镜像白名单、gVisor运行时和密钥属于安装配置，不由Agent修改。</p></div></section>}
   {page==='users'&&admin&&<section className="panel"><div className="panel-heading"><div><h2>内部成员</h2><small>{users.length}</small></div></div><div className="users-table">{users.map(u=><div key={u.id}><span className="avatar">{u.username[0].toUpperCase()}</span><b>{u.username}</b><span className="pill">{u.role}</span><span className="muted">{u.enabled?'已启用':'已停用'}</span>{u.id!==user.id&&u.enabled&&<button className="text-button danger-text" onClick={()=>setDialog({kind:'disable-user',item:u})}>停用</button>}</div>)}</div></section>}
   {page==='audit'&&admin&&<section className="panel"><div className="panel-heading"><div><ShieldCheck size={17}/><h2>配置与账号操作</h2><small>最近300条</small></div><button className="icon-button" aria-label="刷新审计" onClick={()=>api<ForgeEvent[]>('/audit').then(setAudit).catch(fail)}><RefreshCw size={15}/></button></div><div className="timeline">{audit.map(e=><details className="timeline-event" key={e.id}><summary><time>{new Date(e.created_at).toLocaleString()}</time><span className="event-dot"/><b>{e.kind}</b><p>{users.find(u=>u.id===e.data?.actor)?.username||e.data?.actor||''}</p><ChevronRight size={13}/></summary><pre>{jsonText(e.data||{})}</pre></details>)}</div></section>}
   <footer className="page-footer"><span>AGENT FORGE / LOCAL FIRST</span><span>真实执行 · 明确权限 · 人工验收</span></footer>
   </section>
  </main>
  {toast&&<div role="status" className={`toast ${toast.error?'error':''}`}>{toast.error?<AlertCircle size={18}/>:<CheckCircle2 size={18}/>}<span>{toast.text}</span><button onClick={()=>setToast(null)} aria-label="关闭提示">×</button></div>}
  {admin&&showSetup&&<Modal title="连接并测试供应商" wide onClose={()=>{if(connectionBusy){notify('连接检查进行中，请等待结果，避免重复请求。');return;}setShowSetup(false);}}><Setup onBusyChange={setConnectionBusy} onSaved={refresh} onDone={async()=>{await refresh();setShowSetup(false);setPage('models');notify('供应商和模型已保存；可继续添加其他供应商，或为成员分别绑定。');}}/></Modal>}
  {dialog&&<Modal title={({provider:dialog.item?'编辑供应商连接':'添加供应商连接',model:dialog.item?'编辑模型配置':'添加模型配置',agent:dialog.item?'配置Agent':'添加Agent',project:dialog.item?'项目配置':'导入本地项目',run:'发起任务',task:'执行阶段详情',snapshot:'本次运行的配置快照','agent-detail':'成员与当前任务',cancel:'取消本次运行','execution-policies':'团队与成员执行权限',user:'创建团队账号','disable-user':'停用账号'} as Record<string,string>)[dialog.kind]||'详情'} wide={['run','snapshot','task','agent','project','execution-policies'].includes(dialog.kind)} onClose={()=>{setDialog(null);if(['provider','model'].includes(dialog.kind))void refresh();}}>
   {dialog.kind==='provider'&&<ProviderForm initial={dialog.item} onDone={onDone}/>}
   {dialog.kind==='model'&&<ModelForm initial={dialog.item} suggested={dialog.suggested} providers={providers} onDone={onDone}/>}
   {dialog.kind==='agent'&&<AgentForm organization={organization} onManageDepartments={()=>{setDialog(null);setPage('organization');}} onSkills={()=>{setDialog(null);setStoreTab('browse');setPage('skill-store');}} agents={agents} initial={dialog.item} models={models} providers={providers} onDone={onDone}/>}
   {dialog.kind==='project'&&<ProjectForm initial={dialog.item} users={users} onDone={onDone}/>}
   {dialog.kind==='execution-policies'&&<TeamExecutionPermissions admin={admin} {...dialog.item} onSaved={()=>void refresh()}/>}
   {dialog.kind==='run'&&<RunForm initial={dialog.item} organization={organization} providers={providers} agents={agents} models={models} projects={projects} onNavigate={(target,context,draft)=>{if(draft)setRunDraft(draft);setDialog(null);navigateExecution(target,context);}} onDone={id=>{setRunDraft(null);setDialog(null);setSelected(id);setPage('workspace');setWorkspaceMode('board');void refresh();notify('任务已持久化，等待编排器处理。');}}/>}
   {dialog.kind==='snapshot'&&<div className="modal-content"><div className="callout">创建运行时保存的配置快照。后续配置编辑不会静默改变本次运行；凭据只包含版本引用，不显示密钥。</div><pre className="json-view">{jsonText(dialog.item)}</pre></div>}
   {selectedTask&&<div className="modal-content"><Badge state={selectedTask.status}/><h3>{selectedTask.title}</h3><p>负责人：{detail?.snapshot?.agents?.[selectedTask.agent_id]?.name||'平台'}</p><h4>本阶段要完成什么</h4><p>{selectedTask.spec?.description||'见本次任务要求与已确认计划。'}</p>{selectedTask.spec?.acceptance&&<><h4>交付要求</h4><p>{Array.isArray(selectedTask.spec.acceptance)?selectedTask.spec.acceptance.join('；'):selectedTask.spec.acceptance}</p></>}<h4>当前交付结果</h4><p>{selectedTask.result?.summary||(['done','integrated'].includes(selectedTask.status)?'本阶段已提交结果，详细回执可在下方展开。':'尚未提交阶段结果。')}</p><details><summary>完整任务契约与交接依据</summary><pre className="json-view">{jsonText(selectedTask.spec)}</pre></details><details><summary>完整交付结果与工具回执</summary><pre className="json-view">{jsonText(selectedTask.result)}</pre></details></div>}
   {dialog.kind==='agent-detail'&&dialog.item&&<div className="modal-content"><h3>{dialog.item.name} <span className="pill">{roles[dialog.item.role]}</span></h3><p>{modelLabel(dialog.item.model_profile_id)}</p><h4>当前所选运行中的任务</h4>{taskList.filter(t=>t.agent_id===dialog.item.id).map(t=><button key={t.id} className="agent-task-link" onClick={()=>setDialog({kind:'task',item:t})}><span>{t.title}</span><Badge state={t.status}/></button>)}{!taskList.some(t=>t.agent_id===dialog.item.id)&&<p className="muted">当前没有分配任务，不模拟工作状态。</p>}<details><summary>当前运行的成员配置</summary><pre className="json-view">{jsonText(detail?.snapshot?.agents?.[dialog.item.id]||dialog.item)}</pre></details></div>}
   {dialog.kind==='cancel'&&<div className="modal-content"><p>停止启动新的执行步骤。正在进行的模型调用或命令会先结束或超时，再进入已取消状态。</p><p className="callout">已发生的修改不会被当作自动撤销。原始项目不受影响，隔离工作区将保留。</p><button className="button danger" disabled={busy} onClick={()=>void runAction('cancel')}>确认取消运行</button></div>}
   {dialog.kind==='user'&&<UserForm onDone={onDone}/>}
   {dialog.kind==='disable-user'&&<div className="modal-content"><p>停用 {dialog.item.username} 后，其登录会话和事件访问将失效。</p><button className="button danger" onClick={async()=>{try{await api('/users/'+dialog.item.id+'/disable','POST',{});onDone();}catch(e){fail(e);}}}>确认停用</button></div>}
  </Modal>}
 </div>;
}
import './workspace-density.css';
