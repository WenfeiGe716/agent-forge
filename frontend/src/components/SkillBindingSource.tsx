import {useRef,useState} from 'react';
import {api} from '../api';
import {Json} from '../types';
import {Field,Modal} from './UI';

/** Read-only, exact-version inspection. Personal parameters stay in the Agent binding. */
export default function SkillBindingSource({skillId,version,name}:{skillId:string;version:number;name:string}){
 const[detail,setDetail]=useState<Json|null>(null),[error,setError]=useState(''),[busy,setBusy]=useState(false);
 const request=useRef(0);
 const open=async()=>{const ticket=++request.current;setBusy(true);setError('');try{const item=await api<Json>(`/skills/${skillId}?version=${version}`);if(ticket!==request.current)return;if(item.version!==version)throw new Error('返回的 Skill 版本与绑定不一致，请重新检查。');setDetail(item);}catch(e){if(ticket===request.current)setError((e as Error).message);}finally{if(ticket===request.current)setBusy(false);}};
 return <div className="skill-binding-source"><button type="button" className="text-button" disabled={busy} onClick={()=>void open()}>{busy?'读取锁定版本…':`查看绑定版本原文 · v${version}`}</button>{error&&<p className="form-error" role="alert">{error}</p>}{detail&&<Modal title={`${name} · v${version} 原文（只读）`} wide onClose={()=>{request.current++;setDetail(null);}}><div className="modal-content"><p className="callout">此处是共享 Skill 的明确版本原文，不会写回或生成新版本。此成员的加载方式、脚本开关与个人参数在绑定区域独立保存；参数不能授予额外权限。</p><p>版本 v{detail.version} · {detail.status==='approved'?'已批准':detail.status==='revoked'?'已撤销':'待审核'} · <code>{detail.digest}</code></p>{Object.entries(detail.files||{}).map(([path,content])=><Field key={path} label={path}><textarea aria-label={'Skill绑定原文 '+path} className="code-input" readOnly rows={path==='SKILL.md'?14:5} value={String(content)}/></Field>)}</div></Modal>}</div>;
}
