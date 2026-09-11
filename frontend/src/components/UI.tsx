import {ReactNode,useEffect,useRef} from 'react';
import {X,ArrowUpRight,LoaderCircle} from './Icons';
import {states} from '../types';
export function Badge({state}:{state:string}){return <span className={`badge ${state}`}><i/>{states[state]||state}</span>;}
export function Modal({title,children,onClose,wide=false}:{title:string;children:ReactNode;onClose:()=>void;wide?:boolean}){
 const ref=useRef<HTMLDialogElement>(null);
 useEffect(()=>{const d=ref.current;if(d&&!d.open)d.showModal();return()=>{if(d?.open)d.close();};},[]);
 return <dialog className={`modal ${wide?'wide':''}`} ref={ref} onCancel={e=>{e.preventDefault();onClose();}}><header><div><span className="eyebrow">AGENT FORGE</span><h2>{title}</h2></div><button type="button" className="icon-button" onClick={onClose} aria-label="关闭"><X size={20}/></button></header>{children}</dialog>;
}
export function Field({label,hint,children}:{label:string;hint?:string;children:ReactNode}){return <label className="field"><span>{label}</span>{children}{hint&&<small>{hint}</small>}</label>;}
export function Empty({title,description,action}:{title:string;description:string;action?:ReactNode}){return <div className="empty"><div className="empty-mark"><ArrowUpRight size={25}/></div><h3>{title}</h3><p>{description}</p>{action}</div>;}
export function SaveButton({busy,label='保存配置'}:{busy:boolean;label?:string}){return <button type="submit" className="button primary" disabled={busy}>{busy?<LoaderCircle className="spin" size={16}/>:null}{busy?'处理中…':label}</button>;}
