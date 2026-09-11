import {useEffect,useRef,useState} from 'react';
import {api} from '../api';
import {Json,PermissionProfile} from '../types';
import {permissionDescriptions,permissionLabels} from './ExecutionCenter';
import {Search,ShieldCheck,RefreshCw} from './Icons';
import '../team-execution-permissions.css';

type Scope='team'|'agent';
type Profile=PermissionProfile|null;
export type ExecutionPolicyRow={id:string;name:string;permission_profile:Profile;effective_profile:Profile;inherited_profile:Profile;source:'project'|'team'|'agent';source_id:string|null;source_name:string;department?:string;floor?:number;member_ids?:string[];members?:{id:string;name:string}[];role?:string;job_title?:string;team_id?:string|null;team_name?:string};
export type ExecutionPolicies={version:number;layout_ref:Json|null;project:{id:string;name:string;permission_profile:PermissionProfile;execution_mode:string}|null;teams:ExecutionPolicyRow[];agents:ExecutionPolicyRow[]};
type Draft={profile:Profile;version:number;layout_ref:Json|null;confirmed:boolean;error:string};
export type TeamExecutionPermissionsProps={admin:boolean;projectId?:string;initialTeamId?:string;initialAgentId?:string;onSaved?:()=>void};
const label=(profile:Profile)=>profile?permissionLabels[profile]:'随任务项目确定';
const draftKey=(scope:Scope,id:string)=>scope+':'+id;
const sameLayout=(left:Json|null,right:Json|null)=>left===null&&right===null||Boolean(left&&right&&left.id===right.id&&left.version===right.version&&left.digest===right.digest);
function policyResponse(value:ExecutionPolicies,projectId:string){
 if(!value||!Number.isSafeInteger(value.version)||value.version<0||!Array.isArray(value.teams)||!Array.isArray(value.agents)||!Object.hasOwn(value,'layout_ref'))throw new Error('权限配置响应不完整，无法安全编辑。请重新读取。');
 if((value.project?.id||'')!==projectId)throw new Error('项目权限预览不匹配，请重新读取当前项目。');
 return value;
}
export function policyPreview(row:ExecutionPolicyRow,draft?:Pick<Draft,'profile'>){return draft?(draft.profile===null?row.inherited_profile:draft.profile):row.effective_profile;}

export default function TeamExecutionPermissions({admin,projectId='',initialTeamId='',initialAgentId='',onSaved}:TeamExecutionPermissionsProps){
 const [data,setData]=useState<ExecutionPolicies|null>(null),[loading,setLoading]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState('');
 const [team,setTeam]=useState(initialTeamId),[search,setSearch]=useState(''),[drafts,setDrafts]=useState<Record<string,Draft>>({}),[saving,setSaving]=useState('');
 const ticket=useRef(0),activeContext=useRef(projectId),members=useRef(new Map<string,HTMLElement>());activeContext.current=projectId;
 const suffix=projectId?'?project_id='+encodeURIComponent(projectId):'';
 async function load(){
  const request=++ticket.current,context=projectId;setLoading(true);setError('');
  try{const value=policyResponse(await api<ExecutionPolicies>('/execution/policies'+suffix),projectId);if(request!==ticket.current||context!==activeContext.current)return;setData(value);}
  catch(e){if(request===ticket.current&&context===activeContext.current)setError((e as Error).message);}
  finally{if(request===ticket.current&&context===activeContext.current)setLoading(false);}
 }
 useEffect(()=>{setData(null);setDrafts({});setSaving('');setNotice('');void load();return()=>{ticket.current++;};},[projectId]);
 useEffect(()=>{
  if(!data)return;
  const member=data.agents.find(a=>a.id===initialAgentId);
  const initial=member?(member.team_id||'unassigned'):data.teams.some(t=>t.id===initialTeamId)?initialTeamId:data.teams[0]?.id||'unassigned';
  setTeam(initial);setSearch('');
 },[initialTeamId,initialAgentId,Boolean(data)]);
 useEffect(()=>{if(initialAgentId&&data)members.current.get(initialAgentId)?.scrollIntoView?.({block:'nearest'});},[initialAgentId,team,Boolean(data)]);
 const selected=data?.teams.find(t=>t.id===team),teamKnown=team==='all'||team==='unassigned'||Boolean(selected);
 const teamMembers=(data?.agents||[]).filter(a=>team==='all'||(team==='unassigned'?!a.team_id:a.team_id===team));
 const visible=teamMembers.filter(a=>!search||[a.name,a.job_title,a.role,a.team_name,a.id].join(' ').toLowerCase().includes(search.trim().toLowerCase()));
 const editable=admin&&!loading&&!saving&&!error;
 function change(scope:Scope,row:ExecutionPolicyRow,value:string){
  if(!editable||!data)return;const key=draftKey(scope,row.id),profile=(value||null) as Profile;
  setNotice('');setDrafts(current=>{const next={...current};if(profile===row.permission_profile){delete next[key];return next;}next[key]={...(current[key]||{version:data.version,layout_ref:data.layout_ref}),profile,confirmed:false,error:''};return next;});
 }
 function confirm(scope:Scope,row:ExecutionPolicyRow,value:boolean){
  const key=draftKey(scope,row.id);if(!editable)return;setDrafts(current=>current[key]?{...current,[key]:{...current[key],confirmed:value,error:''}}:current);
 }
 function reset(scope:Scope,row:ExecutionPolicyRow){if(!editable)return;const key=draftKey(scope,row.id);setDrafts(current=>{const next={...current};delete next[key];return next;});}
 async function save(scope:Scope,row:ExecutionPolicyRow){
  const key=draftKey(scope,row.id),draft=drafts[key];if(!editable||!data||!draft||draft.error||draft.version!==data.version||!sameLayout(draft.layout_ref,data.layout_ref)||policyPreview(row,draft)==='full'&&!draft.confirmed)return;
  const context=projectId,request=++ticket.current;setSaving(key);setNotice('');setError('');
  try{
   const next=policyResponse(await api<ExecutionPolicies>('/execution/policies/'+scope+'/'+encodeURIComponent(row.id)+suffix,'PUT',{permission_profile:draft.profile,expected_version:draft.version,expected_layout_ref:draft.layout_ref,full_access_confirmed:draft.confirmed}),projectId);
   if(context!==activeContext.current||request!==ticket.current)return;
   if(next.version<=draft.version||(scope==='team'?next.teams:next.agents).find(item=>item.id===row.id)?.permission_profile!==draft.profile)throw new Error('服务未返回已保存的对应权限，请重新读取确认，不能据此认为保存成功。');
   setData(next);setDrafts(current=>{const copy={...current};delete copy[key];return copy;});setNotice(`${row.name}的执行权限已保存，仅用于新任务。`);onSaved?.();
  }catch(e){if(context===activeContext.current&&request===ticket.current)setDrafts(current=>current[key]?{...current,[key]:{...current[key],confirmed:false,error:(e as Error).message}}:current);}
  finally{if(context===activeContext.current&&request===ticket.current)setSaving('');}
 }
 function editor(scope:Scope,row:ExecutionPolicyRow){
  const key=draftKey(scope,row.id),draft=drafts[key],preview=policyPreview(row,draft),stale=Boolean(draft&&data&&(draft.version!==data.version||!sameLayout(draft.layout_ref,data.layout_ref))),full=Boolean(draft&&preview==='full');
  const role=scope==='team'?'团队默认权限':'成员执行权限';
  return <div className="team-policy-editor" data-policy-scope={scope} data-policy-id={row.id}>
   <div className="team-policy-controls"><label><span>{role}</span><select aria-label={role+' · '+row.name} value={draft?draft.profile||'':row.permission_profile||''} disabled={!editable||stale} onChange={e=>change(scope,row,e.target.value)}><option value="">{scope==='team'?'继承项目默认':'继承团队 / 项目'}</option><option value="ask">请求审批</option><option value="auto">帮我批准</option><option value="full">完全访问</option></select></label><div className="team-policy-effective"><span>{draft?'保存后':'当前生效'}</span><b className={preview==='full'?'is-full':''}>{label(preview)}</b><small>{draft?(draft.profile===null?'继承上级权限':'本'+(scope==='team'?'团队':'成员')+'明确设置'):(row.source_name||({team:'团队默认',agent:'成员覆盖',project:'项目默认'}[row.source]))}</small></div></div>
   {draft&&<p className="team-policy-description">{preview?permissionDescriptions[preview]:'未选择项目，最终继承的权限以新任务所选项目为准。'}</p>}
   {full&&<label className="team-policy-risk"><input type="checkbox" checked={draft!.confirmed} disabled={!editable||stale} onChange={e=>confirm(scope,row,e.target.checked)}/><span>我明确允许{scope==='team'?'本团队未设置个人覆盖的成员':'此成员'}在新任务中完全访问当前系统账号可访问的文件和网络。此权限可能影响项目目录之外的文件。</span></label>}
   {stale&&<p className="team-policy-error" role="alert">权限配置已变化。请重新核对当前配置，不能按旧版本保存。</p>}
   {draft?.error&&<p className="team-policy-error" role="alert">保存失败：{draft.error}<button type="button" disabled={loading||Boolean(saving)} onClick={()=>void load()}>重新读取当前配置</button></p>}
   {draft&&<div className="team-policy-save"><span>未保存</span><button type="button" className="button secondary small" disabled={!editable} onClick={()=>reset(scope,row)}>{stale?'放弃草稿并采用当前配置':'撤销修改'}</button><button type="button" className="button primary small" disabled={!editable||stale||Boolean(draft.error)||(full&&!draft.confirmed)} onClick={()=>void save(scope,row)}>{saving===key?'保存中…':'保存'+(scope==='team'?'团队默认':'成员权限')}</button></div>}
  </div>;
 }
 return <section className="team-execution-permissions" aria-label="团队与成员执行权限">
  <header className="team-policy-heading"><div><h3><ShieldCheck size={18}/>团队与成员执行权限</h3><p>成员覆盖优先于团队默认，团队默认优先于项目。只对新任务生效。</p></div><button type="button" className="icon-button" aria-label="刷新团队与成员权限" disabled={loading||Boolean(saving)} onClick={()=>void load()}><RefreshCw size={16}/></button></header>
  {!admin&&<p className="team-policy-readonly">当前为只读查看，管理员可以修改执行权限。</p>}
  {loading&&<p role="status" className="team-policy-feedback">正在读取已保存权限…</p>}{error&&<p role="alert" className="team-policy-error">读取失败：{error}<button type="button" disabled={loading||Boolean(saving)} onClick={()=>void load()}>重新读取</button></p>}{notice&&<p role="status" className="team-policy-notice">{notice}</p>}
  {data&&<><div className="team-policy-project"><span>项目默认</span><b>{data.project?label(data.project.permission_profile):'未选择项目'}</b><small title={data.project?.name}>{data.project?.name||'继承项目的权限将在实际任务中确定'}</small></div><div className="team-policy-layout"><nav className="team-policy-teams" aria-label="权限配置团队"><button type="button" className={team==='all'?'selected':''} onClick={()=>{setTeam('all');setSearch('');}}>全部成员<small>{data.agents.length}</small></button>{data.teams.map(item=><button type="button" key={item.id} title={item.name} className={team===item.id?'selected':''} onClick={()=>{setTeam(item.id);setSearch('');}}><span><b>{item.name}</b><small title={item.department}>{item.department||`${item.floor} 楼`}</small></span><em>{item.member_ids?.length||0}</em></button>)}<button type="button" className={team==='unassigned'?'selected':''} onClick={()=>{setTeam('unassigned');setSearch('');}}>未分配团队<small>{data.agents.filter(a=>!a.team_id).length}</small></button></nav>
   <div className="team-policy-content">{selected&&<article className="team-policy-team"><header><span>团队默认</span><h4 title={selected.name}>{selected.name}</h4><small>{selected.department} · {selected.member_ids?.length||0} 位成员</small></header>{editor('team',selected)}</article>}{!teamKnown&&<p className="team-policy-error">所选团队已不存在，请重新选择团队。</p>}
    <div className="team-policy-member-heading"><h4>{team==='unassigned'?'未分配团队的成员':'成员权限'}<span>{visible.length} / {teamMembers.length}</span></h4><label className="team-policy-search"><Search size={14}/><input aria-label="搜索权限配置成员" placeholder="搜索成员或职责" value={search} onChange={e=>setSearch(e.target.value)}/></label></div>
    <div className="team-policy-members" tabIndex={0} role="region" aria-label="成员权限列表">{visible.map(member=><article ref={el=>{if(el)members.current.set(member.id,el);else members.current.delete(member.id);}} className={'team-policy-member '+(member.id===initialAgentId?'is-focused':'')} key={member.id} data-member-id={member.id}><header><div><h4 title={member.name}>{member.name}</h4><p title={member.job_title||member.role}>{member.job_title||member.role||'成员'}{team==='all'?' · '+(member.team_name||'未分配团队'):''}</p></div>{member.id===initialAgentId&&<span>当前成员</span>}{member.permission_profile&&<small>个人覆盖</small>}</header>{editor('agent',member)}</article>)}{!visible.length&&<p className="team-policy-empty">{search?'没有匹配的成员。':'当前范围没有成员。'}</p>}</div>
   </div></div><details className="team-policy-help"><summary>三种权限的区别</summary>{(['ask','auto','full'] as const).map(profile=><p key={profile}><b>{permissionLabels[profile]}</b>{permissionDescriptions[profile]}</p>)}<p>这里不会改变模型、Skill、工位或既有任务，也不会批准历史任务中的申请。</p></details></>}
 </section>;
}
