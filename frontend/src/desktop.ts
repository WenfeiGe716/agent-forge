/** Narrow optional native bridge. Browser builds never require Electron. */
export type DirectorySelection={ticket:string;path:string;name:string};
export type DesktopBridge={
 platform:string;
 selectProjectDirectory:()=>Promise<DirectorySelection|null>;
 showDataFolder:()=>Promise<void>;
 resumeSession?:()=>Promise<void>;
 notifyTask:(task:{id:string;title:string;status:string})=>Promise<void>;
};
declare global {interface Window {forgeDesktop?:DesktopBridge;}}
export function desktopBridge():DesktopBridge|undefined {
 return typeof window==='undefined'?undefined:window.forgeDesktop;
}
export function taskNotifications(previous:Map<string,string>,runs:{id:string;title:string;status:string}[]){
 const relevant=new Set(['completed','needs_attention','awaiting_decision','awaiting_approval','awaiting_acceptance']);
 return runs.filter(run=>previous.has(run.id)&&previous.get(run.id)!==run.status&&relevant.has(run.status))
  .map(({id,title,status})=>({id,title,status}));
}
