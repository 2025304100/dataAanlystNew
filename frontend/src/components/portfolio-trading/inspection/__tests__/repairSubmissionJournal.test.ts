import {afterEach,beforeEach,expect,it,vi} from 'vitest';
import {installInspectionSubmissionRuntime} from '../../../../test/inspectionSubmissionRuntime';
import {preparedFromSubmission,readRepairSubmission,repairTaskId,recoverRepairSubmission,REPAIR_SUBMISSION_PREFIX,sendRepairSubmission,submissionBlocksNew,type PreparedRepair} from '../repairSubmissionJournal';
let stop:()=>void;
beforeEach(()=>{localStorage.clear();stop=installInspectionSubmissionRuntime();});afterEach(()=>{stop();vi.restoreAllMocks();});
const prepared=(id='inspection-contract-test'):PreparedRepair=>({request:{plan_id:'frozen-plan',snapshot_id:'frozen-snapshot',generated_at:'2026-10-24T08:00:00Z',operation_ids:['market-1'],confirmed:true,idempotency_key:id},operations:[{operation_id:'market-1',symbol:'510300',label:'补齐行情',expected_effect:'读取后重检',trade_date:'2026-10-23'} as any],assetCount:1,retryTaskId:null,retryCount:0});
const task=async(key='inspection-contract-test',status='running')=>({task_id:await repairTaskId(1,key),portfolio_id:1,status,objects:[]} as any);
it('matches the current Python backend canonical SHA-256 task IDs for initial and retry requests',async()=>{
  expect(await repairTaskId(1,'inspection-contract-test')).toBe('e8a0f849aeab5a2a8106dd73899e98c4');
  expect(await repairTaskId(42,'inspection-retry-contract')).toBe('274b90b117cd045e1b7d203364a13bf6');
});
it('durably saves the exact original identity before sending, without storing approval or permission state',async()=>{
  const t=await task();const send=vi.fn(async()=>{const saved=readRepairSubmission(1)!;expect(saved.state).toBe('uncertain');expect(saved.taskId).toBe(t.task_id);expect('confirmed' in saved.request).toBe(false);return t;});
  await sendRepairSubmission(1,prepared(),send,vi.fn());expect(readRepairSubmission(1)).toMatchObject({state:'known',taskStatus:'running'});expect(send).toHaveBeenCalledOnce();
});
it('recovers a lost response using only the exact task GET after reload, including a completed task outside the first history page',async()=>{
  const t=await task(undefined,'done'),send=vi.fn(async()=>{throw Object.assign(new Error('lost response'),{status_code:0});});
  await expect(sendRepairSubmission(1,prepared(),send,vi.fn())).rejects.toThrow();const persisted=JSON.parse(localStorage.getItem(`${REPAIR_SUBMISSION_PREFIX}1`)!);
  const get=vi.fn(async()=>t);expect(await recoverRepairSubmission(persisted,get)).toBe(t);expect(get).toHaveBeenCalledWith(1,t.task_id);expect(send).toHaveBeenCalledOnce();expect(readRepairSubmission(1)?.state).toBe('known');
});
it('keeps unknown evidence on 404/503 and rejects a new key from another tab',async()=>{
  const send=vi.fn(async()=>{throw new Error('response lost');});await expect(sendRepairSubmission(1,prepared(),send,vi.fn())).rejects.toThrow();
  const original=localStorage.getItem(`${REPAIR_SUBMISSION_PREFIX}1`);
  for(const status_code of [404,503])await expect(recoverRepairSubmission(readRepairSubmission(1)!,vi.fn().mockRejectedValue({status_code}))).rejects.toMatchObject({status_code});
  expect(localStorage.getItem(`${REPAIR_SUBMISSION_PREFIX}1`)).toBe(original);
  await expect(sendRepairSubmission(1,prepared('inspection-another-key'),send,vi.fn())).rejects.toMatchObject({code:'PENDING_ORIGINAL'});expect(send).toHaveBeenCalledOnce();
});
it('serializes simultaneous tabs before the first POST',async()=>{
  let release!:(value:any)=>void;const t=await task(),send=vi.fn(()=>new Promise<any>(resolve=>{release=resolve;}));
  const first=sendRepairSubmission(1,prepared(),send,vi.fn());await vi.waitFor(()=>expect(send).toHaveBeenCalledOnce());
  await expect(sendRepairSubmission(1,prepared('inspection-concurrent-tab'),send,vi.fn())).rejects.toMatchObject({code:'SUBMISSION_BUSY'});release(t);await first;expect(send).toHaveBeenCalledOnce();
});
it('lets an explicit replay reuse the frozen key and maps a known original to GET rather than a second POST',async()=>{
  const t=await task(),send=vi.fn().mockRejectedValueOnce(new Error('lost')).mockResolvedValue(t);
  await expect(sendRepairSubmission(1,prepared(),send,vi.fn())).rejects.toThrow();const restored=preparedFromSubmission(readRepairSubmission(1)!);
  await sendRepairSubmission(1,restored,send,vi.fn());expect(restored.request).toEqual(prepared().request);expect(send).toHaveBeenCalledTimes(2);
  const get=vi.fn(async()=>t);await sendRepairSubmission(1,restored,send,get);expect(get).toHaveBeenCalledWith(1,t.task_id);expect(send).toHaveBeenCalledTimes(2);
});
it('checks the prior known task again before allowing another request after completion',async()=>{
  const first=await task(undefined,'done');await sendRepairSubmission(1,prepared(),async()=>first,vi.fn());
  const second=await task('inspection-next-batch');const get=vi.fn(async()=>first);await sendRepairSubmission(1,prepared('inspection-next-batch'),async()=>second,get);
  expect(get).toHaveBeenCalledWith(1,first.task_id);expect(readRepairSubmission(1)?.taskId).toBe(second.task_id);
});
it('retains retry parent identity and permits no implicit retry',async()=>{
  const p=prepared('inspection-parent-retry');p.retryTaskId='original-parent-task';p.retryCount=1;const send=vi.fn().mockRejectedValue(new Error('lost'));
  await expect(sendRepairSubmission(1,p,send,vi.fn())).rejects.toThrow();expect(preparedFromSubmission(readRepairSubmission(1)!)).toMatchObject({retryTaskId:'original-parent-task',request:{idempotency_key:p.request.idempotency_key}});expect(send).toHaveBeenCalledOnce();
});
it('refuses sending if persistent storage or cross-tab locking is unavailable',async()=>{
  const send=vi.fn();const write=vi.spyOn(Storage.prototype,'setItem').mockImplementation(()=>{throw new Error('quota');});
  await expect(sendRepairSubmission(1,prepared(),send,vi.fn())).rejects.toMatchObject({code:'JOURNAL_UNAVAILABLE'});expect(send).not.toHaveBeenCalled();write.mockRestore();
  Object.defineProperty(navigator,'locks',{configurable:true,value:undefined});await expect(sendRepairSubmission(1,prepared(),send,vi.fn())).rejects.toMatchObject({code:'LOCK_UNAVAILABLE'});expect(send).not.toHaveBeenCalled();
});
it('preserves corrupt records and rejects wrong-scope task receipts instead of guessing from names or dates',async()=>{
  localStorage.setItem(`${REPAIR_SUBMISSION_PREFIX}1`,'broken');expect(()=>readRepairSubmission(1)).toThrow();expect(localStorage.getItem(`${REPAIR_SUBMISSION_PREFIX}1`)).toBe('broken');localStorage.clear();
  const wrong=await task();wrong.portfolio_id=2;await expect(sendRepairSubmission(1,prepared(),async()=>wrong,vi.fn())).rejects.toMatchObject({code:'TASK_SCOPE_MISMATCH'});expect(readRepairSubmission(1)?.state).toBe('uncertain');
});
it('does not release an unresolved key due to age, but records a definitive plan rejection',async()=>{
  await expect(sendRepairSubmission(1,prepared(),vi.fn().mockRejectedValue({detail:{error_code:'REPAIR_PLAN_EXPIRED'}}),vi.fn())).rejects.toBeDefined();expect(readRepairSubmission(1)?.state).toBe('rejected');expect(submissionBlocksNew(readRepairSubmission(1))).toBe(false);
});
