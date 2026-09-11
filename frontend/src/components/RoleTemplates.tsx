import {useEffect,useRef,useState} from 'react';
import {api} from '../api';
import {Json,roles} from '../types';
import type {DesignAsset} from './DesignCenter';
import {Empty,Field,Modal} from './UI';
import {ArrowRight,ArrowUpRight,Box,GitBranch,Plus,RefreshCw,Search,ShieldCheck,Users,X} from './Icons';
import '../role-templates.css';

type Props={admin:boolean;onOpenTeam:(asset:DesignAsset)=>void;onSources:()=>void;onSkills:()=>void};
type Selection={item:Json;role:string;name:string};
type BrowserProps={admin:boolean;onOpenTeam?:(asset:DesignAsset)=>void;onMember?:(member:Json)=>void;onSources?:()=>void;instructionsOnly?:boolean};
const pageSize=24;
const revisionLabel=(item:Json)=>String(item.revision_id||'').slice(0,10);
const sourceOrigin=(origin:Json={})=>origin.commit||origin.archive_sha256||origin.release||'本地保存版本';

export default function RoleTemplates(p:Props){
 return <div className="skill-store role-templates" data-testid="role-templates">
  <section className="store-hero role-hero"><div><span className="eyebrow">ROLE TEMPLATES · ORGANIZATION DESIGN</span><h2>先定义谁负责什么，<br/>再设计你的团队。</h2><p>角色模板描述职责、协作方式与交付标准；Skill 则描述完成工作的具体方法。选择角色仅生成设计草稿，不安装 Skill，不创建运行成员。</p><div className="store-benefits"><span><Users size={14}/>职责可编辑</span><span><GitBranch size={14}/>原文与版本可追溯</span><span><ShieldCheck size={14}/>人工发布后使用</span></div></div><div className="store-emblem"><Users size={55}/><span>ROLE LIBRARY</span></div></section>
  <div className="role-library-heading"><div><h3>角色模板库</h3><p>浏览来源内容，为团队挑选职责模板。</p></div><div className="inline-actions"><button type="button" className="button secondary small" onClick={p.onSkills}><Box size={14}/>前往 Skill 商店</button><button type="button" className="button secondary small" onClick={p.onSources}>管理目录来源<ArrowUpRight size={14}/></button></div></div>
  <RoleBrowser admin={p.admin} onOpenTeam={p.onOpenTeam} onSources={p.onSources}/>
 </div>;
}

/** Selection returns an editable member; saving and publishing stay with the team designer. */
export function RoleTemplatePicker({onClose,onMember,instructionsOnly=false}:{onClose:()=>void;onMember:(member:Json)=>void;instructionsOnly?:boolean}){
 return <Modal title={instructionsOnly?'复制角色模板职责':'从角色模板库添加成员'} wide onClose={onClose}><div className="modal-content role-picker"><p className="design-context">{instructionsOnly?'选择并核对角色原文，只复制可编辑职责和来源版本。当前成员的模型、执行权限、Skill 与部门保持原值；不会修改模板库或自动保存成员。':'选择并核对一个角色模板，确认其执行职责后加入当前编辑态。不会保存、发布或创建 Agent。'}</p><RoleBrowser admin onMember={onMember} instructionsOnly={instructionsOnly}/></div></Modal>;
}

export function RoleTemplateSource({reference}:{reference:Json}){
 const[detail,setDetail]=useState<Json|null>(null),[error,setError]=useState(''),[busy,setBusy]=useState(false);
 const open=async()=>{setBusy(true);setError('');try{setDetail(await api<Json>(`/role-templates/items/${reference.item_id}?revision_id=${encodeURIComponent(reference.revision_id)}`));}catch(e){setError((e as Error).message);}finally{setBusy(false);}};
 return <section className="role-origin"><b>职责来源 · 明确版本的角色模板</b><p>角色模板只提供职责参考；名称、模型、权限和 Skill 由成员配置或团队设计独立确认。</p><p>版本：<code>{reference.revision_id}</code><br/>摘要：<code>{reference.digest}</code></p><button type="button" className="text-button" disabled={busy} onClick={()=>void open()}>{busy?'正在读取来源…':'查看锁定版本原文与许可证'}<ArrowUpRight size={13}/></button>{error&&<p className="form-error" role="alert">{error}</p>}{detail&&<Modal title={'职责来源 · '+detail.title} wide onClose={()=>setDetail(null)}><div className="modal-content role-detail"><div className="store-detail-meta"><span className="pill">角色模板 · 只读证据</span><span>{detail.source_name}</span><span>{detail.license}</span></div><dl className="store-provenance"><dt>来源路径</dt><dd>{detail.path}</dd><dt>内容摘要</dt><dd><code>{detail.digest}</code></dd><dt>提交 / 上传摘要</dt><dd><code>{sourceOrigin(detail.origin)}</code></dd></dl>{(detail.warnings||[]).map((warning:string,i:number)=><p className="callout" key={i}>{warning}</p>)}{Object.entries(detail.files||{}).map(([path,content])=><Field label={path} key={path}><textarea className="code-input" readOnly rows={path===detail.document?.path?16:5} value={String(content)} aria-label={'职责来源 '+path}/></Field>)}<p className="microcopy">这是该成员引用的历史版本。职责编辑可与原文不同，不会覆盖目录原件。</p></div></Modal>}</section>;
}

function RoleBrowser({admin,onOpenTeam,onMember,onSources,instructionsOnly=false}:BrowserProps){
 const[q,setQ]=useState(''),[category,setCategory]=useState(''),[source,setSource]=useState(''),[offset,setOffset]=useState(0);
 const[items,setItems]=useState<Json[]>([]),[sources,setSources]=useState<Json[]>([]),[categories,setCategories]=useState<Json>({}),[total,setTotal]=useState(0);
 const[loading,setLoading]=useState(true),[busy,setBusy]=useState(false),[error,setError]=useState('');
 const[detail,setDetail]=useState<Json|null>(null),[detailLoading,setDetailLoading]=useState(false),[file,setFile]=useState('');
 const[selected,setSelected]=useState<Selection[]>([]),[name,setName]=useState('我的团队方案'),[reviewed,setReviewed]=useState(false);
 const queryVersion=useRef(0),detailVersion=useRef(0),actionRunning=useRef(false),reviewSection=useRef<HTMLElement>(null);
 const load=async()=>{
  const version=++queryVersion.current;setLoading(true);setError('');
  try{const result=await api<Json>('/role-templates/items?'+new URLSearchParams({q,category,source_id:source,offset:String(offset),limit:String(pageSize)}));if(version!==queryVersion.current)return;setItems(result.items);setCategories(result.categories||{});setTotal(result.total);}
  catch(e){if(version===queryVersion.current)setError((e as Error).message);}
  finally{if(version===queryVersion.current)setLoading(false);}
 };
 useEffect(()=>{void api<Json[]>('/skill-store/sources').then(setSources).catch(e=>setError(e.message));return()=>{queryVersion.current++;detailVersion.current++;};},[]);
 useEffect(()=>{void load();},[q,category,source,offset]);
 const filter=(fn:()=>void)=>{setOffset(0);fn();};
 const openDetail=async(id:string,revision?:string)=>{
  const version=++detailVersion.current;setDetailLoading(true);setError('');
  try{const result=await api<Json>('/role-templates/items/'+id+(revision?'?revision_id='+encodeURIComponent(revision):''));if(version!==detailVersion.current)return;setDetail(result);setFile(result.document?.path||Object.keys(result.files||{})[0]||'');}
  catch(e){if(version===detailVersion.current)setError((e as Error).message);}
  finally{if(version===detailVersion.current)setDetailLoading(false);}
 };
 const closeDetail=()=>{detailVersion.current++;setDetailLoading(false);setDetail(null);};
 const select=(item:Json)=>{
  const selection={item,role:'developer',name:String(item.title||'新成员').slice(0,80)};
  setSelected(current=>onMember?[selection]:current.some(s=>s.item.id===item.id)?current.map(s=>s.item.id===item.id?selection:s):[...current,selection]);
  setReviewed(false);closeDetail();
 };
 const patch=(id:string,value:Partial<Selection>)=>{setSelected(current=>current.map(s=>s.item.id===id?{...s,...value}:s));setReviewed(false);};
 const compose=async()=>{
  if(actionRunning.current||!reviewed||!selected.length)return;
  actionRunning.current=true;setBusy(true);setError('');
  try{
   if(onMember){const s=selected[0];const result=await api<Json>(`/role-templates/items/${s.item.id}/proposal`,'POST',{revision_id:s.item.revision_id,digest:s.item.digest,role:s.role,name:s.name});onMember(result.member);}
   else{const asset=await api<DesignAsset>('/role-templates/compose','POST',{name:name.trim(),items:selected.map(s=>({item_id:s.item.id,revision_id:s.item.revision_id,digest:s.item.digest,role:s.role,name:s.name}))});onOpenTeam?.(asset);}
  }catch(e){setError((e as Error).message);}finally{actionRunning.current=false;setBusy(false);}
 };
 const sourceOptions=sources.filter(s=>s.counts?.role_template>0||s.id===source);
 return <div className="role-browser">
  <div className="store-filters"><label className="search-box"><Search size={17}/><input aria-label="搜索角色模板" maxLength={200} placeholder="搜索职责，例如：后端架构、前端、测试…" value={q} onChange={e=>filter(()=>setQ(e.target.value))}/></label><select aria-label="按角色模板来源筛选" value={source} onChange={e=>filter(()=>setSource(e.target.value))}><option value="">全部角色来源</option>{sourceOptions.map(s=><option value={s.id} key={s.id}>{s.name} · {s.counts?.role_template||0}</option>)}</select><button type="button" className="icon-button" aria-label="刷新角色模板目录" disabled={loading||busy} onClick={()=>void load()}><RefreshCw size={16}/></button></div>
  <div className="store-category"><button type="button" className={!category?'active':''} onClick={()=>filter(()=>setCategory(''))}>全部职责</button>{Object.entries(categories).map(([key,label])=><button type="button" key={key} className={category===key?'active':''} onClick={()=>filter(()=>setCategory(key))}>{String(label)}</button>)}</div>
  {error&&<p className="form-error" role="alert">{error}</p>}
  <div className="store-result-line"><span>{loading?'正在读取角色目录…':`${total} 个角色模板`}</span><small>{admin?(instructionsOnly?'选择明确版本，核对后复制职责到当前成员。':'选择明确版本，职责映射由你确认。'):'当前为只读模式，可浏览原始内容与来源。'}</small></div>
  {admin&&selected.length>0&&<div className="store-role-moved role-selection-notice" role="status"><p>已选择 <b>{selected.length}</b> 个角色模板{onMember?'':'，可继续浏览和选择（最多 30 个）'}。</p><button type="button" className="button secondary small" onClick={()=>reviewSection.current?.scrollIntoView({behavior:'smooth',block:'start'})}>核对所选职责<ArrowRight size={14}/></button></div>}
  <div className="store-grid role-grid">{items.map(item=>{
   const chosen=selected.find(s=>s.item.id===item.id);return <article className={'store-card role-card '+(chosen?'selected':'')} key={item.id} data-testid={'role-item-'+item.id}><div className="card-top"><span className="store-icon role"><Users size={22}/></span><span className="pill">角色模板</span></div><span className="store-card-category">{item.category_label||item.category}</span><h3>{item.title}</h3><p>{item.description}</p><div className="store-card-meta"><span>{item.source_name}</span><span>{item.license||'许可待核对'}</span></div><div className="store-card-state">{!item.available?'来源已下架':!item.source_enabled?'来源已停用':`目录版本 ${revisionLabel(item)}`}{chosen&&<b>已选择</b>}</div><footer><button type="button" className="text-button" disabled={detailLoading} onClick={()=>void openDetail(item.id)}>查看内容与选择<ArrowUpRight size={14}/></button>{chosen&&<button type="button" className="icon-button" aria-label={'取消选择 '+item.title} disabled={busy} onClick={()=>{setSelected(current=>current.filter(s=>s.item.id!==item.id));setReviewed(false);}}><X size={14}/></button>}</footer></article>;
  })}</div>
  {!loading&&!items.length&&<Empty title="暂无匹配的角色模板" description="调整搜索或来源筛选。角色目录来自来源同步，不会混入 Skill 商店。" action={onSources?<button type="button" className="button secondary" onClick={onSources}>管理目录来源</button>:undefined}/>}
  <div className="store-pagination"><button type="button" className="button secondary small" disabled={offset===0||loading} onClick={()=>setOffset(Math.max(0,offset-pageSize))}>上一页</button><span>{total?`${offset+1}–${Math.min(offset+pageSize,total)} / ${total}`:'0 / 0'}</span><button type="button" className="button secondary small" disabled={offset+pageSize>=total||loading} onClick={()=>setOffset(offset+pageSize)}>下一页</button></div>
  {admin&&selected.length>0&&<section ref={reviewSection} className="role-compose" aria-label="确认角色职责映射"><header><div><h3>{instructionsOnly?'确认复制职责':onMember?'确认成员职责':'组合为团队方案'}</h3><p>{instructionsOnly?'仅把职责正文和来源引用复制到当前成员。执行权限档不随模板改变；已有成员名称和岗位保持不变，未填写时才使用建议名称。':'模板名称不是系统执行权限。请逐项选择执行职责；模型、Skill 和权限在团队设计中另行确认。'}</p></div><span className="pill">已选 {selected.length}</span></header>{!onMember&&<Field label="团队方案名称"><input aria-label="角色团队方案名称" maxLength={120} value={name} onChange={e=>setName(e.target.value)} disabled={busy}/></Field>}
   {selected.map(s=><div className="role-selection" key={s.item.id}><div><b>{s.item.title}</b><small>{s.item.source_name} · {revisionLabel(s.item)}</small><button type="button" className="text-button" onClick={()=>void openDetail(s.item.id,s.item.revision_id)}>核对所选原文</button></div><Field label={instructionsOnly?'建议名称 / 岗位':'成员名称'}><input aria-label={s.item.title+' 成员名称'} maxLength={80} value={s.name} disabled={busy} onChange={e=>patch(s.item.id,{name:e.target.value})}/></Field>{!instructionsOnly&&<Field label="执行职责映射"><select aria-label={s.item.title+' 执行职责映射'} value={s.role} disabled={busy} onChange={e=>patch(s.item.id,{role:e.target.value})}>{Object.entries(roles).map(([role,label])=><option key={role} value={role}>{label}</option>)}</select></Field>}<button type="button" className="icon-button" aria-label={'移除角色 '+s.item.title} disabled={busy} onClick={()=>{setSelected(current=>current.filter(x=>x.item.id!==s.item.id));setReviewed(false);}}><X size={15}/></button></div>)}
   <label className="check"><input type="checkbox" checked={reviewed} disabled={busy} onChange={e=>setReviewed(e.target.checked)}/>{instructionsOnly?'我已核对内容与来源许可；仅复制到成员独立编辑态，不改共享模板、不授予额外权限。':'我已核对所选内容、来源许可及执行职责；仅生成可编辑方案，不授予额外权限。'}</label><button type="button" className="button primary" disabled={busy||!reviewed||(!onMember&&!name.trim())||selected.some(s=>!s.name.trim())} onClick={()=>void compose()}>{busy?'准备设计草稿…':instructionsOnly?'复制职责到当前成员':onMember?'加入当前编辑态':'生成团队草稿并编辑'}<ArrowRight size={15}/></button>
  </section>}
  {detailLoading&&!detail&&<p className="microcopy" role="status">正在读取原始角色内容…</p>}
  {detail&&<Modal title={detail.title} wide onClose={closeDetail}><div className="modal-content role-detail"><div className="store-detail-meta"><span className="pill">角色模板 · 非 Skill</span><span>{detail.source_name}</span><span>{detail.license||'许可待核对'}</span></div><p>{detail.description}</p><div className="role-detail-version"><Field label="角色模板版本"><select aria-label="角色模板版本" value={detail.revision_id} disabled={detailLoading||busy} onChange={e=>void openDetail(detail.id,e.target.value)}>{(detail.versions||[]).map((version:Json)=><option key={version.revision_id} value={version.revision_id}>{revisionLabel(version)} · {new Date(version.created_at).toLocaleString()}</option>)}</select></Field><small>明确版本被引用后，后续同步不会静默替换职责。</small></div><dl className="store-provenance"><dt>来源路径</dt><dd>{detail.path}</dd><dt>内容摘要</dt><dd><code>{detail.digest}</code></dd><dt>提交 / 上传摘要</dt><dd><code>{sourceOrigin(detail.origin)}</code></dd></dl>{(detail.warnings||[]).map((warning:string,i:number)=><p className="callout" key={i}>{warning}</p>)}
   <div className="skill-file-editor role-document"><aside>{Object.keys(detail.files||{}).map(path=><button type="button" key={path} className={file===path?'selected':''} onClick={()=>setFile(path)}>{path}</button>)}</aside><section><header><b>{file}</b><span>原始文本 · 只读</span></header><textarea className="code-input" aria-label="角色模板原始内容" readOnly rows={18} value={detail.files?.[file]??(file===detail.document?.path?detail.document.content:'')}/></section></div>
   <p className="microcopy">内容作为职责参考，不能自动安装工具或修改模型。来源中出现的脚本、外部链接和安装指令不会被执行。</p>{error&&<p role="alert" className="form-error">{error}</p>}{admin&&<footer className="role-detail-actions"><p>{!detail.available?'条目已从来源下架，保留原文但不能用于新方案。':!detail.source_enabled?'来源已停用，不能用于新方案。':instructionsOnly?'选择后核对内容与许可，再复制职责到当前成员独立编辑；不会自动保存。':'选择后仍需确认职责映射并编辑团队配置。'}</p><button type="button" className="button primary" disabled={busy||detailLoading||!detail.available||!detail.source_enabled||(!onMember&&selected.length>=30&&!selected.some(s=>s.item.id===detail.id))} onClick={()=>select(detail)}><Plus size={15}/>{selected.some(s=>s.item.id===detail.id)?'采用当前所选版本':'选择此版本'}</button></footer>}
  </div></Modal>}
 </div>;
}
