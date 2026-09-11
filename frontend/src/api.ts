export class APIError extends Error {constructor(public status:number, message:string){super(message);}}
export async function api<T=any>(path:string,method='GET',body?:unknown,headers:Record<string,string>={}):Promise<T>{
  const response=await fetch('/api'+path,{method,credentials:'same-origin',headers:{'Content-Type':'application/json','X-Forge-Request':'1',...headers},body:body===undefined?undefined:JSON.stringify(body)});
  let data:any={};try{data=await response.json();}catch{ /* A proxy may return an HTML error. */ }
  if(!response.ok){const detail=data.detail;throw new APIError(response.status,typeof detail==='string'?detail:Array.isArray(detail)?detail.map((x:any)=>`${x.loc?.join('.')}: ${x.msg}`).join('；'):`请求失败 (${response.status})`);}
  return data as T;
}
export function parseObject(text:string):Record<string,any>{const result=JSON.parse(text||'{}');if(!result||Array.isArray(result)||typeof result!=='object')throw new Error('配置必须是JSON对象');return result;}
export const jsonText=(value:unknown)=>JSON.stringify(value??{},null,2);

/** Idempotency IDs do not require the secure-context-only randomUUID API. */
export function requestId():string {
 const crypto=globalThis.crypto;
 if(typeof crypto?.randomUUID==='function')return crypto.randomUUID();
 if(typeof crypto?.getRandomValues==='function'){const b=crypto.getRandomValues(new Uint8Array(16));return Array.from(b,n=>n.toString(16).padStart(2,'0')).join('');}
 throw new Error('浏览器缺少安全随机数功能，请在现代浏览器中使用本地地址或HTTPS');
}
