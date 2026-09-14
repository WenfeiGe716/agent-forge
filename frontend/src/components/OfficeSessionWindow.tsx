import {Component,useEffect,useRef,useState} from 'react';
import {Agent,Project,Run} from '../types';
import {Terminal,X} from './Icons';
import EmployeeTaskPanel from './EmployeeTaskPanel';

class SessionErrorBoundary extends Component<{children:any},{error:string}> {
 state={error:''};
 static getDerivedStateFromError(error:unknown){return {error:error instanceof Error?error.message:'会话面板加载失败'};}
 render(){return this.state.error?<div className="office-session-error" role="alert"><b>工作会话暂时无法打开</b><p>{this.state.error}</p><button className="text-button" onClick={()=>this.setState({error:''})}>重新打开</button></div>:this.props.children;}
}

/** A local window, not a new session or an execution authority. */
export default function OfficeSessionWindow(p:{agent:Agent;runs:Run[];detail:Run|null;projects:Project[];writer:boolean;admin:boolean;preview?:boolean;initialRunId?:string;onRunChange?:(id:string)=>void;onClose:()=>void;onOpenRun:(id:string)=>void;onNavigate:(page:string)=>void;onRefresh:()=>void}){
 const element=useRef<HTMLElement|null>(null),drag=useRef<{x:number;y:number;left:number;top:number}|null>(null);
 const[position,setPosition]=useState<{left:number;top:number}|null>(null);
 const clamp=(left:number,top:number)=>{const el=element.current;if(!el)return{left,top};const parent=el.offsetParent as HTMLElement|null;const width=parent?.clientWidth||window.innerWidth,height=parent?.clientHeight||window.innerHeight;return{left:Math.max(12,Math.min(left,width-el.offsetWidth-12)),top:Math.max(96,Math.min(top,height-el.offsetHeight-12))};};
 useEffect(()=>{const resize=()=>setPosition(old=>old?clamp(old.left,old.top):old);window.addEventListener('resize',resize);return()=>window.removeEventListener('resize',resize);},[]);
 return <section ref={element} className="office-session-window glass" role="region" aria-label={p.agent.name+'的任务会话浮窗'} style={position||undefined}>
  <header className="office-window-handle" onPointerDown={e=>{if(e.button!==0||(e.target as HTMLElement).closest('button'))return;const el=element.current;if(!el)return;drag.current={x:e.clientX,y:e.clientY,left:el.offsetLeft,top:el.offsetTop};e.currentTarget.setPointerCapture(e.pointerId);e.preventDefault();}} onPointerMove={e=>{const d=drag.current;if(d)setPosition(clamp(d.left+e.clientX-d.x,d.top+e.clientY-d.y));}} onPointerUp={e=>{drag.current=null;if(e.currentTarget.hasPointerCapture(e.pointerId))e.currentTarget.releasePointerCapture(e.pointerId);}} onPointerCancel={()=>{drag.current=null;}}>
   <span className="office-window-symbol"><Terminal size={17}/></span><div><b>{p.agent.name} · 工作会话</b><small>真实记录 · 按任务独立查看</small></div><span className="office-window-grip" aria-hidden="true">⠿</span><button aria-label="关闭员工会话浮窗" onClick={p.onClose}><X size={16}/></button>
  </header>
  <div className="office-window-body"><SessionErrorBoundary><EmployeeTaskPanel key={p.agent.id} {...p} initialTab="messages"/></SessionErrorBoundary></div>
 </section>;
}
