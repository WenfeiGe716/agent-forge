import {useEffect,useRef} from 'react';
import {ActorView} from '../office/behavior';
import {OfficeRenderer} from '../graphics/officeRenderer';
import {detailedActor} from '../graphics/characters';
import {palette} from '../graphics/officeWorld';
import './employee-portrait.css';

/**
 * Small, live portrait used by the selected-employee dock.
 *
 * This is deliberately a view of the same ActorView that drives the office
 * scene. It does not fetch state, invent status, or create a second actor. A
 * local animation clock only advances pose articulation while the run feed
 * remains the source of truth.
 */
export type EmployeePortraitProps={
  view:ActorView;
  role?:string;
  hue?:number;
  className?:string;
  label?:string;
};

const roleHue=(role?:string)=>role&&palette[role]!==undefined?palette[role]:palette.developer;

export default function EmployeePortrait({view,role,hue,className='',label='动态员工头像'}:EmployeePortraitProps){
  const canvas=useRef<HTMLCanvasElement>(null);
  const renderer=useRef<OfficeRenderer|null>(null);
  const live=useRef({view,at:performance.now()});
  if(live.current.view!==view)live.current={view,at:performance.now()};
  useEffect(()=>{
    const element=canvas.current;if(!element)return;
    let scene:OfficeRenderer|null=null;
    try{scene=new OfficeRenderer(element);}catch{return;}
    // Portraits and the office overview share exactly the same detailed actor
    // kit. This avoids a second asset pipeline and keeps the selected person
    // visually consistent while the live ActorView changes pose.
    scene.background=0x1b2638;
    // The dock avatar is a small, animated bust rather than a full-body
    // thumbnail.  A tight orthographic crop keeps the face, shoulders and
    // authored hand gestures visible while naturally clipping the legs.
    scene.day=true;scene.target=[0,1.62,0];scene.zoom=18.5;scene.yaw=1.66;scene.pitch=.10;
    scene.setStatic([]);renderer.current=scene;
    const reduced=window.matchMedia?.('(prefers-reduced-motion: reduce)').matches??false;
    let raf=0,last=0;
    const frame=(stamp:number)=>{
      raf=requestAnimationFrame(frame);
      if(document.hidden||stamp-last<(reduced?250:33))return;
      last=stamp;
      const source=live.current.view;
      // Keep view identity/status from the feed, but advance the authored
      // pose clock only for visual interpolation.
      // Portraits use a local origin. The office position can be anywhere on
      // the floor and must never push the actor out of this small camera.
      const actor={...source,position:[0,0,0] as [number,number,number],yaw:0,poseTime:reduced?0:source.poseTime+Math.min(.3,Math.max(0,stamp-live.current.at)/1000)};
      // Keep a true 3D cartoon bust, not a flat icon or a gray full-body
      // mannequin. Legs and floor-only helpers are intentionally omitted so
      // the small dock card always reads as an upper-body portrait.
      const bust=detailedActor(actor,0,hue??roleHue(role),false,role).filter(part=>
        !['thigh','knee','shin','shoe','shoe-sole','contact-shadow','selection-ring'].includes(part.tag||''));
      scene?.draw(bust);
    };
    raf=requestAnimationFrame(frame);
    return()=>{cancelAnimationFrame(raf);scene?.dispose();renderer.current=null;};
  },[role,hue]);
  return <span className={`employee-portrait ${className}`} data-agent-id={view.id} data-pose={view.pose} aria-label={label}>
    <canvas ref={canvas} width={132} height={168} aria-hidden="true"/>
  </span>;
}
