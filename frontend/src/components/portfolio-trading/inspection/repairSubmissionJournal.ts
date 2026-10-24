import type {InspectionRepairOperation,InspectionRepairSubmit,InspectionRepairTask} from '../../../types/portfolioInspection';

export const REPAIR_SUBMISSION_PREFIX='portfolio-inspection-repair-request-v1:';
const EVENT='portfolio-inspection-repair-request-updated';
export interface RepairSubmission {
  version:1;portfolioId:number;request:Omit<InspectionRepairSubmit,'confirmed'>;
  taskId:string;parentTaskId:string|null;operations:InspectionRepairOperation[];assetCount:number;retryCount:number;
  state:'uncertain'|'known'|'rejected';createdAt:number;taskStatus?:string;rejectionCode?:string;
}
export interface PreparedRepair {request:InspectionRepairSubmit;operations:InspectionRepairOperation[];assetCount:number;retryTaskId:string|null;retryCount:number;}
const terminal=(status:string|undefined)=>Boolean(status&&['done','completed','succeeded','failed','cancelled','expired','stalled','partial'].includes(status.toLowerCase()));
export const submissionBlocksNew=(record:RepairSubmission|null)=>Boolean(record&&record.state!=='rejected'&&(record.state!=='known'||!terminal(record.taskStatus)));
export class RepairSubmissionError extends Error {constructor(public code:string,message:string){super(message);this.name='RepairSubmissionError';}}
const key=(pid:number)=>`${REPAIR_SUBMISSION_PREFIX}${pid}`;
function validate(value:unknown,pid:number):RepairSubmission {
  const v=value as RepairSubmission,q=v?.request;
  if(!v||v.version!==1||v.portfolioId!==pid||!Number.isSafeInteger(pid)||pid<=0||!['uncertain','known','rejected'].includes(v.state)
    ||!q||typeof q.idempotency_key!=='string'||q.idempotency_key.length<8||q.idempotency_key.length>128||typeof q.plan_id!=='string'||typeof q.snapshot_id!=='string'||typeof q.generated_at!=='string'
    ||!Array.isArray(q.operation_ids)||!q.operation_ids.every(id=>typeof id==='string')||!Array.isArray(v.operations)||!v.operations.every(op=>op&&typeof op.operation_id==='string'&&typeof op.label==='string'&&typeof op.expected_effect==='string'&&(op.symbol===null||typeof op.symbol==='string')&&(op.trade_date===null||typeof op.trade_date==='string'))
    ||!Number.isFinite(v.createdAt)||typeof v.taskId!=='string'||!/^[a-f0-9]{32}$/.test(v.taskId)||!(v.parentTaskId===null||typeof v.parentTaskId==='string')
    ||!Number.isInteger(v.assetCount)||v.assetCount<0||!Number.isInteger(v.retryCount)||v.retryCount<0||'confirmed' in q)throw new RepairSubmissionError('JOURNAL_INVALID','本地原请求记录不完整，已保留记录并停止新提交。请先查看已有任务或恢复原记录。');
  return v;
}
export function readRepairSubmission(pid:number):RepairSubmission|null {
  try{const text=localStorage.getItem(key(pid));return text?validate(JSON.parse(text),pid):null;}
  catch(error){if(error instanceof RepairSubmissionError)throw error;throw new RepairSubmissionError('JOURNAL_UNAVAILABLE','无法读取本地原请求记录，暂不发送新任务。请恢复浏览器存储后重新读取；已有任务仍可查看。');}
}
function save(record:RepairSubmission){
  validate(record,record.portfolioId);
  try{const text=JSON.stringify(record);localStorage.setItem(key(record.portfolioId),text);if(localStorage.getItem(key(record.portfolioId))!==text)throw new Error('readback mismatch');}
  catch{throw new RepairSubmissionError('JOURNAL_UNAVAILABLE','原请求标识未能可靠保存，本次不发送新任务。请恢复浏览器存储后重新读取。');}
  window.dispatchEvent(new CustomEvent(EVENT,{detail:record.portfolioId}));
}
export function subscribeRepairSubmission(pid:number,listener:()=>void){
  const local=(event:Event)=>{if((event as CustomEvent).detail===pid)listener();};
  const storage=(event:StorageEvent)=>{if(event.key===key(pid)||event.key===null)listener();};
  window.addEventListener(EVENT,local);window.addEventListener('storage',storage);
  return()=>{window.removeEventListener(EVENT,local);window.removeEventListener('storage',storage);};
}
/** Matches the existing backend task identity in portfolio_inspection_repair.py; does not submit. */
export async function repairTaskId(pid:number,requestKey:string):Promise<string>{
  if(!Number.isSafeInteger(pid)||pid<=0||typeof requestKey!=='string')throw new RepairSubmissionError('JOURNAL_INVALID','原请求范围不完整。');
  if(!crypto.subtle)throw new RepairSubmissionError('IDENTITY_UNAVAILABLE','当前浏览器无法核验原请求标识，暂不发送新任务。');
  const bytes=new TextEncoder().encode(JSON.stringify({key:requestKey,portfolio_id:pid}));
  const digest=await crypto.subtle.digest('SHA-256',bytes);
  return [...new Uint8Array(digest)].map(value=>value.toString(16).padStart(2,'0')).join('').slice(0,32);
}
export function preparedFromSubmission(record:RepairSubmission):PreparedRepair {
  return {request:{...record.request,operation_ids:[...record.request.operation_ids],confirmed:true},operations:record.operations,assetCount:record.assetCount,retryTaskId:record.parentTaskId,retryCount:record.retryCount};
}
export async function recoverRepairSubmission(record:RepairSubmission,readTask:(pid:number,id:string)=>Promise<InspectionRepairTask>):Promise<InspectionRepairTask>{
  validate(record,record.portfolioId);
  if(await repairTaskId(record.portfolioId,record.request.idempotency_key)!==record.taskId)throw new RepairSubmissionError('IDENTITY_MISMATCH','原请求与任务标识不一致，已停止自动关联，请查看已有任务。');
  const task=await readTask(record.portfolioId,record.taskId);
  if(task?.portfolio_id!==record.portfolioId||task.task_id!==record.taskId||typeof task.status!=='string'||!Array.isArray(task.objects))throw new RepairSubmissionError('TASK_SCOPE_MISMATCH','返回任务不属于本组合的原请求，已停止关联，请重新读取。');
  const current=readRepairSubmission(record.portfolioId);
  if(current?.request.idempotency_key===record.request.idempotency_key&&(current.state!=='known'||current.taskStatus!==task.status))save({...current,state:'known',taskStatus:task.status});
  return task;
}
function errorCode(error:unknown){const e=error as {error_code?:string;detail?:{error_code?:string;code?:string}};return e?.error_code??e?.detail?.error_code??e?.detail?.code;}
/** Web Locks serialize same-origin tabs. The durable record is written before the only POST. */
export async function sendRepairSubmission(pid:number,prepared:PreparedRepair,
  send:()=>Promise<InspectionRepairTask>,readTask:(pid:number,id:string)=>Promise<InspectionRepairTask>):Promise<InspectionRepairTask>{
  if(navigator.onLine===false)throw new RepairSubmissionError('OFFLINE_NOT_SENT','网络已断开，本次不发送任务；原请求继续保留。');
  if(!navigator.locks?.request)throw new RepairSubmissionError('LOCK_UNAVAILABLE','当前浏览器无法保护多标签页提交，暂不发送新任务。已有任务仍可只读查看。');
  return navigator.locks.request(`inspection-repair-submit:${pid}`,{ifAvailable:true},async lock=>{
    if(!lock)throw new RepairSubmissionError('SUBMISSION_BUSY','其他标签页正在登记本组合请求，请先读取原请求或已有任务。');
    const prior=readRepairSubmission(pid);
    const same=prior?.request.idempotency_key===prepared.request.idempotency_key;
    if(prior&&!same&&prior.state!=='rejected'){
      if(prior.state!=='known')throw new RepairSubmissionError('PENDING_ORIGINAL','本组合仍有结果不明的原请求，请先找回原任务，不另起一笔提交。');
      const latest=await recoverRepairSubmission(prior,readTask);
      if(!terminal(latest.status))throw new RepairSubmissionError('PENDING_ORIGINAL','原任务仍在执行，请继续查看原任务，不重复提交。');
    }
    if(prior&&same){
      const original=preparedFromSubmission(prior);
      const identity=(value:PreparedRepair)=>JSON.stringify([value.request.plan_id,value.request.snapshot_id,value.request.generated_at,value.request.operation_ids,value.request.idempotency_key,value.retryTaskId]);
      if(identity(original)!==identity(prepared))throw new RepairSubmissionError('REQUEST_CHANGED','原请求范围已变化，不能使用原标识提交另一批内容。');
      if(prior.state==='known')return recoverRepairSubmission(prior,readTask);
    }
    const {confirmed:_approval,...request}=prepared.request;
    const record:RepairSubmission=prior&&same?{...prior,state:'uncertain'}:{version:1,portfolioId:pid,request,taskId:await repairTaskId(pid,request.idempotency_key),parentTaskId:prepared.retryTaskId,operations:prepared.operations,assetCount:prepared.assetCount,retryCount:prepared.retryCount,state:'uncertain',createdAt:Date.now()};
    save(record);
    let task:InspectionRepairTask;
    try{task=await send();}
    catch(error){
      const code=errorCode(error);
      if(['REPAIR_PLAN_EXPIRED','INSPECTION_SNAPSHOT_CHANGED','REPAIR_OPERATION_INVALID','REPAIR_OPERATION_DISABLED'].includes(code??'')||code==='OFFLINE_NOT_SENT'&&!prior)save({...record,state:'rejected',rejectionCode:code});
      throw error;
    }
    if(task?.portfolio_id!==pid||task.task_id!==record.taskId||!Array.isArray(task.objects))throw new RepairSubmissionError('TASK_SCOPE_MISMATCH','提交回执与原请求标识不一致，原记录继续保留，请只读核查。');
    try{save({...record,state:'known',taskStatus:task.status});}catch{throw new RepairSubmissionError('JOURNAL_UPDATE_FAILED','原任务已返回，但本地关联更新失败。原请求仍保留，请先读取原任务，不重复提交。');}return task;
  });
}
