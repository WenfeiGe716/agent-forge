import {useCallback,useEffect,useRef,useState} from 'react';
import {Box,Code2,FolderGit2,LayoutDashboard,Network,Terminal,Users} from './Icons';
import '../studio-navigation.css';

type Props={onBoard:()=>void;onNavigate:(target:string)=>void};
type Item={label:string;target?:string;icon: any;action?:'board'};

const items:Item[]=[
 {label:'办公室',icon:Box},
 {label:'任务',icon:LayoutDashboard,action:'board'},
 {label:'楼层与部门',target:'organization',icon:Box},
 {label:'团队',target:'agents',icon:Users},
 {label:'项目',target:'projects',icon:FolderGit2},
 {label:'模型',target:'models',icon:Network},
 {label:'角色模板',target:'role-templates',icon:Users},
 {label:'Skill商店',target:'skill-store',icon:Code2},
 {label:'设计中心',target:'design',icon:Network},
 {label:'工具与权限',target:'execution',icon:Terminal},
 {label:'任务看板',target:'board',icon:LayoutDashboard},
];

export default function StudioNavigation({onBoard,onNavigate}:Props){
 const navRef=useRef<HTMLElement|null>(null);
 const probeRefs=useRef<(HTMLButtonElement|null)[]>([]);
 const moreProbeRef=useRef<HTMLElement|null>(null);
 const [visibleCount,setVisibleCount]=useState(items.length);
 const measure=useCallback(()=>{
  const nav=navRef.current;
  if(!nav)return;
  const widths=probeRefs.current.map(node=>node?.getBoundingClientRect().width||0);
  const more=moreProbeRef.current?.getBoundingClientRect().width||0;
  if(!widths.every(Boolean)||!more)return;
  const gap=parseFloat(getComputedStyle(nav).columnGap||getComputedStyle(nav).gap||'0')||0;
  const available=nav.clientWidth;
  const total=widths.reduce((sum,width)=>sum+width,0)+gap*Math.max(0,widths.length-1);
  if(total<=available){setVisibleCount(items.length);return;}
  const budget=Math.max(0,available-more-gap);
  let used=0,count=0;
  for(const width of widths){
   const next=used+width+(count?gap:0);
   if(next>budget)break;
   used=next;count++;
  }
  setVisibleCount(count);
 },[]);
 useEffect(()=>{
  const nav=navRef.current;if(!nav)return;
  const observer=typeof ResizeObserver!=='undefined'?new ResizeObserver(measure):null;
  observer?.observe(nav);
  const onResize=()=>measure();
  window.addEventListener('resize',onResize);
  measure();
  const frame=requestAnimationFrame(measure);
  return()=>{observer?.disconnect();window.removeEventListener('resize',onResize);cancelAnimationFrame(frame);};
 },[measure]);
 const show=(item:Item)=>{if(item.action==='board')onBoard();else if(item.target)onNavigate(item.target);};
 const visible=items.slice(0,visibleCount),overflow=items.slice(visibleCount);
 const button=(item:Item,index?:number)=><button type="button" key={item.label} className={index===0?'selected':''} onClick={()=>show(item)}>{<item.icon size={15}/>}<span>{item.label}</span></button>;
 return <nav ref={navRef} className="studio-navigation" aria-label="工作台导航">
  {visible.map((item,index)=>button(item,index))}
  {overflow.length>0&&<details className="studio-navigation-more">
   <summary aria-label="更多工作台导航">更多</summary>
   <div>{overflow.map(item=><button type="button" key={item.label} onClick={()=>show(item)}>{<item.icon size={15}/>}<span>{item.label}</span></button>)}</div>
  </details>}
  <div className="studio-navigation-measure" aria-hidden="true">
   {items.map((item,index)=><button type="button" ref={node=>{probeRefs.current[index]=node;}} key={item.label}>{<item.icon size={15}/>}<span>{item.label}</span></button>)}
   <details className="studio-navigation-more studio-navigation-more-probe"><summary ref={moreProbeRef}>更多</summary></details>
  </div>
 </nav>;
}
