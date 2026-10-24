import {useSyncExternalStore} from 'react';

export type AcceptanceMode={state:'normal'|'checking'|'readonly'|'unavailable';readOnly:boolean;blocked:boolean;reason:string;checkedAt:number};
const NORMAL:AcceptanceMode={state:'normal',readOnly:false,blocked:false,reason:'',checkedAt:0};
const READONLY='当前为只读验收服务，登记、重检、修复和审核已禁用；查看与历史回查仍可用。';
const CHECKING='正在核实服务模式，暂不允许修改；查看与历史回查仍可用。';
const UNAVAILABLE='暂时无法核实服务模式，修改操作保持禁用；可继续查看已有结果或重新读取。';
let snapshot=NORMAL,started=false,serial=0,pending:Promise<void>|null=null,controller:AbortController|null=null,stop:()=>void=()=>{};
const listeners=new Set<()=>void>();
const emit=(next:AcceptanceMode)=>{snapshot=next;for(const listener of listeners)listener();};
export const getAcceptanceMode=()=>snapshot;
export const subscribeAcceptanceMode=(listener:()=>void)=>{listeners.add(listener);return()=>{listeners.delete(listener);};};
export const useAcceptanceMode=()=>useSyncExternalStore(subscribeAcceptanceMode,getAcceptanceMode,getAcceptanceMode);
export function observeAcceptanceReadOnly(){
  if(!started)return;
  serial++;controller?.abort();pending=null;
  emit({state:'readonly',readOnly:true,blocked:true,reason:READONLY,checkedAt:Date.now()});
}
export function refreshAcceptanceMode(quiet=false):Promise<void>{
  if(!started)return Promise.resolve();
  if(pending)return pending;
  if(navigator.onLine===false){emit({...snapshot,state:'unavailable',blocked:true,reason:snapshot.readOnly?READONLY:UNAVAILABLE});return Promise.resolve();}
  const token=++serial,abort=new AbortController();controller=abort;
  if(!quiet||snapshot.state!=='normal')emit({...snapshot,state:'checking',blocked:true,reason:snapshot.readOnly?READONLY:CHECKING});
  const timeout=window.setTimeout(()=>abort.abort(),8000);
  pending=Promise.resolve().then(async()=>{try{
    const response=await fetch('/api/v1/inspection-acceptance/status',{method:'GET',cache:'no-store',signal:abort.signal});
    if(!response.ok)throw Error('mode unavailable');
    const value=await response.json();
    if(token!==serial)return;
    if(abort.signal.aborted)throw Error('mode read timed out');
    if(value.mode==='inspection_acceptance'&&value.read_only===true)emit({state:'readonly',readOnly:true,blocked:true,reason:READONLY,checkedAt:Date.now()});
    else if(value.mode==='application'&&value.read_only===false)emit({...NORMAL,checkedAt:Date.now()});
    else throw Error('invalid mode');
  }catch{if(token===serial)emit({...snapshot,state:'unavailable',blocked:true,reason:snapshot.readOnly?READONLY:UNAVAILABLE});}
  finally{window.clearTimeout(timeout);if(token===serial){pending=null;controller=null;}}});
  return pending;
}
/** Installed before React mounts. No cached false/404 is treated as permission. */
export function startAcceptanceMonitor(){
  if(started)return stop;
  started=true;emit({state:'checking',readOnly:false,blocked:true,reason:CHECKING,checkedAt:0});
  const read=()=>{void refreshAcceptanceMode();};
  const offline=()=>{serial++;controller?.abort();pending=null;emit({...snapshot,state:'unavailable',blocked:true,reason:snapshot.readOnly?READONLY:UNAVAILABLE});};
  const visibility=()=>{if(document.visibilityState!=='hidden')read();};
  window.addEventListener('offline',offline);window.addEventListener('online',read);window.addEventListener('focus',read);window.addEventListener('pageshow',read);document.addEventListener('visibilitychange',visibility);
  const timer=window.setInterval(()=>{if(document.visibilityState!=='hidden')void refreshAcceptanceMode(true);},15000);read();
  stop=()=>{serial++;controller?.abort();pending=null;started=false;window.clearInterval(timer);window.removeEventListener('offline',offline);window.removeEventListener('online',read);window.removeEventListener('focus',read);window.removeEventListener('pageshow',read);document.removeEventListener('visibilitychange',visibility);emit(NORMAL);};
  return stop;
}
/** Last-moment guard also covers keyboard handlers and a stale confirmation. */
export function acceptanceMutationReason():string|null{
  if(started&&snapshot.state==='normal'&&(Date.now()-snapshot.checkedAt>30000||Date.now()<snapshot.checkedAt)){void refreshAcceptanceMode();return CHECKING;}
  return snapshot.blocked?snapshot.reason:null;
}
