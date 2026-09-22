import {api} from './api';
import {ForgeEvent} from './types';

export type FeedState='loading'|'open'|'error'|'closed';
type Subscriber={events:(events:ForgeEvent[])=>void;state?:(state:FeedState)=>void;error?:(error:unknown)=>void};
type Feed={listeners:Set<Subscriber>;events:ForgeEvent[];ids:Set<number>;cursor:number;source?:EventSource;timer?:ReturnType<typeof setTimeout>;flush?:ReturnType<typeof setTimeout>;alive:boolean;state:FeedState};
const feeds=new Map<string,Feed>();
function state(feed:Feed,value:FeedState){feed.state=value;feed.listeners.forEach(s=>s.state?.(value));}
function publish(feed:Feed){if(!feed.alive)return;const snapshot=feed.events.slice();feed.listeners.forEach(s=>s.events(snapshot));}
function append(feed:Feed,run:string,events:ForgeEvent[]){
 for(const event of events){
  if(event.run_id!==run||!Number.isSafeInteger(event.id)||event.id<=0)throw Error('运行事件格式不正确');
  if(!feed.ids.has(event.id)){feed.ids.add(event.id);feed.events.push(event);}
  feed.cursor=Math.max(feed.cursor,event.id);
 }
}
async function connect(run:string,feed:Feed){
 try{
  // The shared owner fills every page once, then follows the durable cursor.
  // A late subscriber gets the same history, including events received while
  // its own run metadata was loading.
  let more=true;
  while(more&&feed.alive){
   const previous=feed.cursor;
   const page=await api<{events:ForgeEvent[];has_more:boolean}>(`/runs/${encodeURIComponent(run)}/events?after=${previous}&limit=1000`);
   if(!feed.alive)return;
   append(feed,run,page.events);more=page.has_more;
   if(more&&feed.cursor<=previous)throw Error('事件分页游标未前进');
   publish(feed);
  }
  if(!feed.alive)return;
  const source=feed.source=new EventSource(`/api/runs/${encodeURIComponent(run)}/stream?after=${feed.cursor}`);
  source.onopen=()=>{if(feed.alive)state(feed,'open');};
  source.onerror=()=>{if(feed.alive)state(feed,'error');}; // native reconnect uses Last-Event-ID
  source.onmessage=message=>{
   if(!feed.alive)return;
   try{
    append(feed,run,[JSON.parse(message.data)]);
    // One render per burst, rather than copying/re-rendering for every token.
    feed.flush??=setTimeout(()=>{feed.flush=undefined;publish(feed);},32);
   }catch(error){source.close();retry(run,feed,error);}
  };
  source.addEventListener('revoked',()=>{source.close();feed.events=[];feed.ids.clear();publish(feed);state(feed,'closed');});
 }catch(error){retry(run,feed,error);}
}
function retry(run:string,feed:Feed,error:unknown){
 if(!feed.alive)return;
 state(feed,'error');feed.listeners.forEach(s=>s.error?.(error));
 feed.timer=setTimeout(()=>void connect(run,feed),1500);
}
/** One history owner and SSE connection per run; disposed with its last view. */
export function subscribeRunEvents(run:string,subscriber:Subscriber){
 let feed=feeds.get(run);
 if(!feed){feed={listeners:new Set(),events:[],ids:new Set(),cursor:0,alive:true,state:'loading'};feeds.set(run,feed);}
 const first=feed.listeners.size===0;
 feed.listeners.add(subscriber);subscriber.events(feed.events.slice());subscriber.state?.(feed.state);
 if(first)void connect(run,feed);
 return()=>{
  feed!.listeners.delete(subscriber);
  if(!feed!.listeners.size){feed!.alive=false;feed!.source?.close();clearTimeout(feed!.timer);clearTimeout(feed!.flush);feeds.delete(run);}
 };
}
export function clearRunEvents(){for(const feed of feeds.values()){feed.alive=false;feed.source?.close();clearTimeout(feed.timer);clearTimeout(feed.flush);feed.events=[];feed.ids.clear();feed.listeners.forEach(s=>{s.events([]);s.state?.('closed');});}feeds.clear();}
