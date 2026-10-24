export interface RecheckReceipt {portfolio_id:number;request_key:string;trade_date:string|null;task_id:string|null;dispatch_status:string;task_status:string|null;message?:string|null;provenance_annotations?:Array<{label:string;note:string}>;reconciliation_passed:false;trading_resumed:false;}
export interface RecheckSubmission {version:1;portfolioId:number;requestKey:string;tradeDate:string|null;createdAt:number;updatedAt?:number;}
export const RECHECK_SUBMISSION_PREFIX='portfolio-inspection-recheck-request-v1:';
const EVENT='inspection-recheck-request-updated';
const key=(pid:number)=>RECHECK_SUBMISSION_PREFIX+pid;
export function readRecheckSubmission(pid:number):RecheckSubmission|null{
  try{const raw=localStorage.getItem(key(pid));if(!raw)return null;const v=JSON.parse(raw) as RecheckSubmission;
    if(v.version!==1||v.portfolioId!==pid||pid<=0||!Number.isSafeInteger(pid)||!Number.isFinite(v.createdAt)||typeof v.requestKey!=='string'||!/^[A-Za-z0-9_-]{8,128}$/.test(v.requestKey)||!(v.tradeDate===null||typeof v.tradeDate==='string'&&/^\d{4}-\d{2}-\d{2}$/.test(v.tradeDate)))throw Error();return v;
  }catch{throw new Error('原对账请求记录无法可靠读取，暂不发送新请求；请恢复浏览器存储后重新读取。');}
}
function save(record:RecheckSubmission){
  try{const value=JSON.stringify(record);localStorage.setItem(key(record.portfolioId),value);if(localStorage.getItem(key(record.portfolioId))!==value)throw Error();}
  catch{throw new Error('原对账请求未能可靠保存，本次不发送任务。');}
  window.dispatchEvent(new CustomEvent(EVENT,{detail:record.portfolioId}));
}
export function subscribeRecheckSubmission(pid:number,listener:()=>void){
  const local=(e:Event)=>{if((e as CustomEvent).detail===pid)listener();};
  const remote=(e:StorageEvent)=>{if(e.key===key(pid)||e.key===null)listener();};
  window.addEventListener(EVENT,local);window.addEventListener('storage',remote);
  return()=>{window.removeEventListener(EVENT,local);window.removeEventListener('storage',remote);};
}
type Reader=(pid:number,key:string)=>Promise<RecheckReceipt>;
type Sender=(pid:number,date:string|undefined,key:string)=>Promise<RecheckReceipt>;
export async function recoverRecheckSubmission(record:RecheckSubmission,read:Reader){
  return validate(record,await read(record.portfolioId,record.requestKey));
}
function validate(record:RecheckSubmission,result:RecheckReceipt){
  if(!result||result.portfolio_id!==record.portfolioId||result.request_key!==record.requestKey||result.trade_date!==record.tradeDate
    ||!(result.task_id===null||typeof result.task_id==='string'&&/^[a-f0-9]{32}$/.test(result.task_id))||typeof result.dispatch_status!=='string')throw new Error('回执不属于本组合的原请求，已停止关联，请重新读取。');return result;
}
const terminal=(r:RecheckReceipt)=>Boolean(r.task_status&&['done','completed','succeeded','failed','partial','cancelled','expired','stalled'].includes(r.task_status.toLowerCase()));
/** Called only on an explicit click. A missing/uncertain receipt reuses the original frozen key/date. */
export async function sendRecheckSubmission(pid:number,date:string|null,send:Sender,read:Reader):Promise<RecheckReceipt>{
  if(navigator.onLine===false)throw new Error('网络已断开，原请求继续保留，本次不发送任务。');
  if(!navigator.locks?.request)throw new Error('浏览器无法保护跨标签提交，暂不发送新任务；已有请求仍可只读查看。');
  return navigator.locks.request(`inspection-recheck-submit:${pid}`,{ifAvailable:true},async lock=>{
    if(!lock)throw new Error('其他标签页正在提交本组合核对，请先重新读取原请求。');
    let record=readRecheckSubmission(pid);
    if(record){
      try{const original=await recoverRecheckSubmission(record,read);if(!terminal(original))return original;record=null;}
      catch(e){const error=e as {status_code?:number;detail?:{error_code?:string};error_code?:string};
        if(error.status_code!==404||(error.error_code??error.detail?.error_code)!=='RECHECK_REQUEST_NOT_FOUND')throw e;
        // Explicitly retry the SAME request, never replace it on 404/network errors.
      }
    }
    if(!record){record={version:1,portfolioId:pid,requestKey:crypto.randomUUID(),tradeDate:date,createdAt:Date.now()};save(record);}
    const result=validate(record,await send(pid,record.tradeDate??undefined,record.requestKey));
    save({...record,updatedAt:Date.now()});
    return result;
  });
}
