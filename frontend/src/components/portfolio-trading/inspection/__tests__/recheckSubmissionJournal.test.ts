import {beforeEach,afterEach,expect,it,vi} from 'vitest';
import {installInspectionSubmissionRuntime} from '../../../../test/inspectionSubmissionRuntime';
import {readRecheckSubmission,recoverRecheckSubmission,sendRecheckSubmission,RECHECK_SUBMISSION_PREFIX,type RecheckReceipt} from '../recheckSubmissionJournal';
let stop:()=>void;
beforeEach(()=>{localStorage.clear();stop=installInspectionSubmissionRuntime();});
afterEach(()=>{stop();vi.restoreAllMocks();});
const receipt=(pid:number,date:string|undefined,key:string):RecheckReceipt=>({portfolio_id:pid,request_key:key,trade_date:date??null,task_id:'a'.repeat(32),dispatch_status:'QUEUED',task_status:'running',reconciliation_passed:false,trading_resumed:false});
it('persists original scope before POST and recovers only by its key after reload',async()=>{
  const send=vi.fn(async(pid,date,key)=>{expect(readRecheckSubmission(pid)).toMatchObject({requestKey:key,tradeDate:date});throw new Error('lost response');});
  await expect(sendRecheckSubmission(1,'2026-09-29',send,vi.fn())).rejects.toThrow();
  const original=JSON.parse(localStorage.getItem(RECHECK_SUBMISSION_PREFIX+1)!);
  const read=vi.fn(async(pid,key)=>receipt(pid,original.tradeDate,key));
  await recoverRecheckSubmission(original,read);
  await sendRecheckSubmission(1,'2026-10-23',send,read);
  expect(read).toHaveBeenCalledWith(1,original.requestKey);expect(send).toHaveBeenCalledOnce();
});
it('retains original key/date on explicit retry after exact 404',async()=>{
  const send=vi.fn().mockRejectedValueOnce(new Error('lost')).mockImplementation(receipt);
  await expect(sendRecheckSubmission(1,'2026-09-29',send,vi.fn())).rejects.toThrow();
  const original=readRecheckSubmission(1)!;
  const read=vi.fn().mockRejectedValue({status_code:404,error_code:'RECHECK_REQUEST_NOT_FOUND'});
  await sendRecheckSubmission(1,'2026-10-23',send,read);
  expect(send).toHaveBeenLastCalledWith(1,original.tradeDate,original.requestKey);
});
it('503, ordinary 404 and wrong-scope receipts never replace or resubmit the original',async()=>{
  await expect(sendRecheckSubmission(1,null,vi.fn().mockRejectedValue(new Error('lost')),vi.fn())).rejects.toThrow();
  const before=localStorage.getItem(RECHECK_SUBMISSION_PREFIX+1),send=vi.fn();
  for(const error of [{status_code:503},{status_code:404}])await expect(sendRecheckSubmission(1,null,send,vi.fn().mockRejectedValue(error))).rejects.toBe(error);
  await expect(sendRecheckSubmission(1,null,send,async()=>receipt(2,undefined,'wrong-key'))).rejects.toThrow('回执');
  expect(localStorage.getItem(RECHECK_SUBMISSION_PREFIX+1)).toBe(before);expect(send).not.toHaveBeenCalled();
});
it('serializes cross-tab clicks while the POST is unresolved',async()=>{
  let release!:(r:RecheckReceipt)=>void;
  const send=vi.fn(()=>new Promise<RecheckReceipt>(resolve=>{release=resolve;}));
  const first=sendRecheckSubmission(1,null,send,vi.fn());await vi.waitFor(()=>expect(send).toHaveBeenCalledOnce());
  await expect(sendRecheckSubmission(1,null,send,vi.fn())).rejects.toThrow('其他标签页');
  const r=readRecheckSubmission(1)!;release(receipt(1,undefined,r.requestKey));await first;
});
it('does not confuse independent portfolios and never guesses a latest job',async()=>{
  const send=vi.fn(async(pid,date,key)=>receipt(pid,date,key));await sendRecheckSubmission(1,'2026-09-29',send,vi.fn());await sendRecheckSubmission(2,'2026-10-20',send,vi.fn());
  expect(readRecheckSubmission(1)?.requestKey).not.toBe(readRecheckSubmission(2)?.requestKey);expect(send).toHaveBeenCalledTimes(2);
});
it('re-reads terminal original task before allowing an explicit new check',async()=>{
  const send=vi.fn(async(pid,date,key)=>receipt(pid,date,key));await sendRecheckSubmission(1,null,send,vi.fn());const old=readRecheckSubmission(1)!;
  await sendRecheckSubmission(1,'2026-10-23',send,async()=>({...receipt(1,undefined,old.requestKey),task_status:'done'}));
  expect(send).toHaveBeenCalledTimes(2);expect(readRecheckSubmission(1)?.requestKey).not.toBe(old.requestKey);
});
it('keeps registered-but-not-dispatched demand rather than starting another request',async()=>{
  const send=vi.fn(async(pid,date,key)=>receipt(pid,date,key));await sendRecheckSubmission(1,null,send,vi.fn());const original=readRecheckSubmission(1)!;
  const waiting={...receipt(1,undefined,original.requestKey),task_id:null,task_status:null,dispatch_status:'WAITING_RETRY'};
  expect(await sendRecheckSubmission(1,null,send,async()=>waiting)).toEqual(waiting);expect(send).toHaveBeenCalledOnce();
});
it('does not POST without reliable local storage, web locks or connectivity',async()=>{
  const send=vi.fn();vi.spyOn(Storage.prototype,'setItem').mockImplementation(()=>{throw Error();});
  await expect(sendRecheckSubmission(1,null,send,vi.fn())).rejects.toThrow('保存');expect(send).not.toHaveBeenCalled();
});

