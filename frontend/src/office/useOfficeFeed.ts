import {useEffect,useRef,useState} from 'react';
import {api} from '../api';
import {Packet} from './behavior';
/** Global, authenticated projection; never infer idleness from the selected run. */
export function useOfficeFeed(enabled:boolean,hint:number=0){
 const[state,setState]=useState<{packet:Packet|null;connected:boolean;received:number;error:string}>({packet:null,connected:false,received:0,error:''});
 const refresh=useRef<()=>void>(()=>{});
 useEffect(()=>{
  if(!enabled)return;let alive=true,busy=false,again=false,cursor:number|null=null,timer:ReturnType<typeof setTimeout>|undefined;
  const load=async()=>{if(!alive)return;if(busy){again=true;return;}busy=true;if(timer)clearTimeout(timer);
   try{let packet:Packet;const events:Packet['events']=[];let pages=0;
    do{packet=await api<Packet>('/office/state'+(cursor===null?'':`?after=${cursor}&limit=500`));if(!alive)return;events.push(...packet.events);cursor=packet.cursor;if(++pages>50)throw Error('事件积压，暂停空闲演绎并继续同步');}while(packet.has_more);
    setState({packet:{...packet,events},connected:true,received:performance.now(),error:''});
   }catch(e){if(alive)setState(s=>({...s,connected:false,error:(e as Error).message}));}
   finally{busy=false;if(alive){timer=setTimeout(()=>void load(),again?0:1200);again=false;}}
  };
  refresh.current=()=>void load();void load();
  const visible=()=>{if(!document.hidden){setState(s=>({...s,connected:false,error:'正在重新确认全局占用'}));void load();}};document.addEventListener('visibilitychange',visible);
  return()=>{alive=false;if(timer)clearTimeout(timer);document.removeEventListener('visibilitychange',visible);refresh.current=()=>{};};
 },[enabled]);
 useEffect(()=>{if(enabled)refresh.current();},[hint,enabled]);
 return{...state,refresh:()=>refresh.current()};
}
