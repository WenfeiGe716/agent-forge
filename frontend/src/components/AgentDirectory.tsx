import {useState} from 'react';
import {Agent,Model,OrganizationLayout,Provider,agentTitle} from '../types';
import {memberAssignment} from '../office/memberAssignment';
import {ArrowUpRight,Network,Plus,Search} from './Icons';
import {Empty} from './UI';
import '../agent-directory.css';

type Props={agents:Agent[];models:Model[];providers:Provider[];organization:OrganizationLayout|null;admin:boolean;onEdit:(agent:Agent)=>void;onCreate:()=>void;onOrganization:()=>void};
export default function AgentDirectory(p:Props){
 const[query,setQuery]=useState(''),[department,setDepartment]=useState('all'),[team,setTeam]=useState('all'),[state,setState]=useState('all');
 const rows=p.agents.map(agent=>({agent,assignment:memberAssignment(agent,p.organization)}));
 const departments=[...new Set([...(p.organization?.config.floors||[]).map(f=>f.department),...rows.map(r=>r.assignment.department).filter(Boolean)])];
 const teams=p.organization?.config.teams||[];
 const shown=rows.filter(({agent:a,assignment:s})=>
  (department==='all'||(department==='unassigned'?!s.department:s.department===department))&&
  (team==='all'||(team==='unassigned'?!s.team:s.team?.id===team))&&
  (state==='all'||(state==='enabled'?a.enabled!==false:a.enabled===false))&&
  `${a.name} ${agentTitle(a)} ${s.label}`.toLowerCase().includes(query.trim().toLowerCase()));
 const reset=()=>{setQuery('');setDepartment('all');setTeam('all');setState('all');};
 return <div className="agent-directory">
  <section className="directory-toolbar" aria-label="组织成员筛选">
   <div className="directory-summary"><div><b>{p.agents.length} 位成员</b><span>{teams.length} 个办公团队 · {rows.filter(r=>!r.assignment.team).length} 位未分配团队</span></div><div className="inline-actions"><button className="button secondary" onClick={p.onOrganization}>楼层、部门与团队管理</button>{p.admin&&<button className="button primary" onClick={p.onCreate}><Plus size={15}/>添加 Agent</button>}</div></div>
   <div className="directory-filters"><label className="directory-search"><Search size={16}/><input aria-label="搜索组织成员" placeholder="搜索姓名、岗位或团队" value={query} onChange={e=>setQuery(e.target.value)}/></label>
    <label>所属部门<select aria-label="组织成员部门筛选" value={department} onChange={e=>setDepartment(e.target.value)}><option value="all">全部部门</option><option value="unassigned">未分配部门</option>{departments.map(d=><option key={d} value={d}>{d}</option>)}</select></label>
    <label>办公团队<select aria-label="组织成员团队筛选" value={team} onChange={e=>setTeam(e.target.value)}><option value="all">全部团队</option><option value="unassigned">未分配团队</option>{teams.map(t=><option key={t.id} value={t.id}>{t.name} · {t.floor} 楼</option>)}</select></label>
    <label>成员状态<select aria-label="组织成员状态筛选" value={state} onChange={e=>setState(e.target.value)}><option value="all">全部状态</option><option value="enabled">已启用</option><option value="disabled">已停用</option></select></label>
   </div><p className="microcopy">显示 {shown.length} / {p.agents.length} 位 · 部门为当前成员配置，办公团队和工位来自已应用布局；调整空间请保存、发布并应用布局。</p>
  </section>
  <div className="card-grid">{shown.map(({agent:a,assignment:s},index)=>{const model=p.models.find(m=>m.id===a.model_profile_id),provider=p.providers.find(x=>x.id===model?.provider_id);return <article className="entity-card agent-card" key={a.id} data-agent-id={a.id}>
   <div className="card-top"><span className={`agent-avatar color-${index%4}`}>{a.name.slice(0,1)}</span><span className="pill">{agentTitle(a)}</span></div><h2>{a.name}</h2><p className="microcopy">{a.enabled===false?'已停用':'已启用'} · 配置 v{a.version}{a.role_template_ref?' · 来自角色模板副本':''}</p>
   <div className="directory-assignment"><b>{s.department||'未分配部门'}</b><span>{s.team?.name||'未分配团队'} · {s.workplace}</span>{s.spatialMismatch&&<small>空间待调整：当前工位仍位于{s.floor?.department}；不会自动改动已发布布局。</small>}</div>
   <p className="directory-instructions">{a.instructions||'尚未添加角色指令。'}</p><div className="binding"><Network size={15}/><span>{model?`${provider?.name||'供应商已失效'} / ${model.name}`:'尚未绑定模型'}{model&&(model.enabled===false||provider?.enabled===false)?' · 已停用':''}</span></div>
   <div className="agent-card-meta"><span>{a.fallback_model_ids?.length||0} 个备用模型</span><span>{a.skills?.filter(s=>s.enabled!==false).length||0} 个启用 Skill</span></div><footer><small>当前配置 · 用于后续运行</small>{p.admin&&<button className="text-button" onClick={()=>p.onEdit(a)}>配置成员<ArrowUpRight size={14}/></button>}</footer>
  </article>;})}</div>
  {!shown.length&&<Empty title={p.agents.length?'没有匹配的成员':'添加第一位成员'} description={p.agents.length?'可重置筛选，或选择未分配团队查看待安排成员。':'可从角色模板复制职责，再独立配置模型、部门和 Skill。'} action={p.agents.length?<button className="button secondary" onClick={reset}>重置筛选</button>:p.admin?<button className="button primary" onClick={p.onCreate}>添加 Agent</button>:undefined}/>}
 </div>;
}
