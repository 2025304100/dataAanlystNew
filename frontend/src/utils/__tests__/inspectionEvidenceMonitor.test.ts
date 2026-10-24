import {afterEach,beforeEach,expect,it,vi} from 'vitest';
import {createInspectionEvidenceMonitor,EVIDENCE_ACTIVE_MS,EVIDENCE_IDLE_MS,inspectionEvidenceIdentity} from '../inspectionEvidenceMonitor';

const sample=(revision='v1',running=true)=>({runtime:{portfolio_id:1,current_state:'RECONCILIATION_BLOCKED',permissions:{allow_new_buys:false},recovery_summary:{can_confirm:false},guide:{snapshot_id:revision,steps:[{status:running?'RUNNING':'INCOMPLETE',tasks:running?[{status:'running'}]:[]}]}},
  job:{portfolio_id:1,status:running?'running':'done',task_id:'task-original',queue:{days:[{trade_date:'2026-09-30',status:'EVIDENCE_INCOMPLETE',report_version:revision}]},work_items:[{trade_date:'2026-09-30',work_state:running?'running':'done',requested_evidence_version:1,processed_evidence_version:running?0:1}]},
  report:{portfolio_id:1,trade_date:'2026-09-30',report_id:'original-report',report_version:revision,items:[],zero_sum_check_passed:false,differences_found:true}} as any);
let monitors:Array<()=>void>=[];
beforeEach(()=>{vi.useFakeTimers();Object.defineProperty(document,'visibilityState',{configurable:true,value:'visible'});});
afterEach(()=>{monitors.forEach(stop=>stop());monitors=[];vi.useRealTimers();vi.restoreAllMocks();});
function setup(initial=sample()){
  let current=initial;
  const read={runtime:vi.fn(async()=>current.runtime),job:vi.fn(async()=>current.job),report:vi.fn(async()=>current.report)};
  const invalidate=vi.fn(),checking=vi.fn();
  const monitor=createInspectionEvidenceMonitor(1,read,invalidate,checking);monitors.push(monitor.attach());monitor.accept(initial);
  return{read,invalidate,checking,monitor,update:(value:any)=>{current=value;}};
}
it('receiver detects completion at 5 seconds without any originating tab or broadcast',async()=>{
  const s=setup();s.update(sample('repaired-v2',false));await vi.advanceTimersByTimeAsync(EVIDENCE_ACTIVE_MS);
  expect(s.invalidate).toHaveBeenCalledTimes(1);expect(s.read.runtime).toHaveBeenCalledTimes(1);expect(s.read.report).toHaveBeenCalledWith('2026-09-30');
  await vi.advanceTimersByTimeAsync(EVIDENCE_IDLE_MS);expect(s.invalidate).toHaveBeenCalledTimes(1);
});
it('discovers newly queued jobs on an idle receiver and then tracks their completion',async()=>{
  const s=setup(sample('idle',false));s.update(sample('task-started',true));await vi.advanceTimersByTimeAsync(EVIDENCE_IDLE_MS);
  expect(s.invalidate).toHaveBeenCalledTimes(1);s.update(sample('task-failed',false));await vi.advanceTimersByTimeAsync(EVIDENCE_ACTIVE_MS);
  expect(s.invalidate).toHaveBeenCalledTimes(2);expect(await s.monitor.verify()).toBe(false);
});
it('ignores generated timestamps and scan/heartbeat scheduling without evidence changes',async()=>{
  const original=sample('same',false),fresh=structuredClone(original);fresh.runtime.generated_at='later';fresh.job.last_run_at='later';fresh.job.next_run_at='later';fresh.job.work_items[0].next_check_at='later';
  expect(inspectionEvidenceIdentity(original)).toBe(inspectionEvidenceIdentity(fresh));
  const s=setup(original);s.update(fresh);await vi.advanceTimersByTimeAsync(EVIDENCE_IDLE_MS);expect(s.invalidate).not.toHaveBeenCalled();expect(await s.monitor.verify()).toBe(true);
});
it('detects only a selected report version changing even when overall runtime did not change',async()=>{
  const original=sample('same',false),fresh=structuredClone(original);fresh.report.report_version='fresh-day-version';
  const s=setup(original);s.update(fresh);expect(await s.monitor.verify()).toBe(false);expect(s.invalidate).toHaveBeenCalledTimes(1);
});
it('fails closed on GET errors, backs off, and never treats recovery as accepted evidence',async()=>{
  const s=setup();s.read.runtime.mockRejectedValue(new Error('503'));
  await vi.advanceTimersByTimeAsync(EVIDENCE_ACTIVE_MS);expect(s.invalidate).toHaveBeenCalledTimes(1);expect(s.checking).toHaveBeenLastCalledWith(false);
  await vi.advanceTimersByTimeAsync(EVIDENCE_IDLE_MS-1);expect(s.read.runtime).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(1);expect(s.read.runtime).toHaveBeenCalledTimes(2);expect(s.invalidate).toHaveBeenCalledTimes(1);
  s.read.runtime.mockResolvedValue(sample().runtime);expect(await s.monitor.verify()).toBe(true);
  // Freshness UI is intentionally not acknowledged by this observer; only a full manual read does so.
  s.update(sample('finished',false));expect(await s.monitor.verify()).toBe(false);expect(s.invalidate).toHaveBeenCalledTimes(2);
});
it('stops background polling while hidden and verifies immediately on returning',async()=>{
  const s=setup();Object.defineProperty(document,'visibilityState',{configurable:true,value:'hidden'});document.dispatchEvent(new Event('visibilitychange'));
  s.update(sample('finished-away',false));await vi.advanceTimersByTimeAsync(120_000);expect(s.read.runtime).not.toHaveBeenCalled();
  Object.defineProperty(document,'visibilityState',{configurable:true,value:'visible'});document.dispatchEvent(new Event('visibilitychange'));await vi.advanceTimersByTimeAsync(0);
  expect(s.invalidate).toHaveBeenCalledTimes(2); // Expire the wake gap first, then observe the changed backend version.
});
it('shares an in-flight verification and ignores a late response after closing the receiver',async()=>{
  const s=setup();let finish!:(value:any)=>void;s.read.runtime.mockImplementationOnce(()=>new Promise(resolve=>{finish=resolve;}));
  const first=s.monitor.verify(),second=s.monitor.verify();expect(first).toBe(second);await vi.advanceTimersByTimeAsync(0);expect(s.read.runtime).toHaveBeenCalledTimes(1);
  monitors.pop()!();finish(sample('late').runtime);expect(await first).toBe(false);expect(s.invalidate).not.toHaveBeenCalled();
});
it('does not let a check started before an explicit full reread invalidate the newly accepted view',async()=>{
  const s=setup();let finish!:(value:any)=>void;s.read.runtime.mockImplementationOnce(()=>new Promise(resolve=>{finish=resolve;}));
  const old=s.monitor.verify();await vi.advanceTimersByTimeAsync(0);const fresh=sample('fresh',false);s.monitor.accept(fresh);s.update(fresh);finish(sample('old').runtime);
  expect(await old).toBe(false);expect(s.invalidate).not.toHaveBeenCalled();expect(await s.monitor.verify()).toBe(true);
});
it('recovers from synchronous reader failure without leaving verification stuck',async()=>{
  const s=setup();s.read.runtime.mockImplementationOnce(()=>{throw new Error('reader unavailable');});expect(await s.monitor.verify()).toBe(false);
  expect(s.checking).toHaveBeenLastCalledWith(false);expect(await s.monitor.verify()).toBe(true);
});
it('rejects wrong-portfolio or wrong-date responses rather than accepting them as current',async()=>{
  const s=setup();s.read.report.mockResolvedValue({...sample().report,trade_date:'2026-10-20'});expect(await s.monitor.verify()).toBe(false);expect(s.invalidate).toHaveBeenCalledTimes(1);
  s.read.report.mockResolvedValue(sample().report);s.read.runtime.mockResolvedValue({...sample().runtime,portfolio_id:2});expect(await s.monitor.verify()).toBe(false);
});
it('invalidates immediately offline, pauses reads, and checks once on burst reconnect events',async()=>{
  let online=true;vi.spyOn(navigator,'onLine','get').mockImplementation(()=>online);
  const s=setup();online=false;window.dispatchEvent(new Event('offline'));expect(s.invalidate).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(300_000);expect(s.read.runtime).not.toHaveBeenCalled();expect(s.monitor.guard()).toBe(false);
  online=true;window.dispatchEvent(new Event('online'));window.dispatchEvent(new Event('online'));window.dispatchEvent(new Event('focus'));window.dispatchEvent(new Event('pageshow'));
  await vi.advanceTimersByTimeAsync(0);expect(s.read.runtime).toHaveBeenCalledTimes(1);expect(s.checking).toHaveBeenLastCalledWith(false);
});
it('rejects success from a pre-disconnection request after a new verification has started',async()=>{
  let online=true;vi.spyOn(navigator,'onLine','get').mockImplementation(()=>online);const s=setup();let finish!:(value:any)=>void;
  s.read.runtime.mockImplementationOnce(()=>new Promise(resolve=>{finish=resolve;}));const old=s.monitor.verify();await vi.advanceTimersByTimeAsync(0);
  online=false;window.dispatchEvent(new Event('offline'));online=true;window.dispatchEvent(new Event('online'));await vi.advanceTimersByTimeAsync(0);
  const before=s.invalidate.mock.calls.length;finish(sample('old-reply').runtime);expect(await old).toBe(false);expect(s.invalidate).toHaveBeenCalledTimes(before);expect(s.read.runtime).toHaveBeenCalledTimes(2);
});
it('expires a sleeping clock gap before any review action even if no focus event arrives',async()=>{
  const s=setup();vi.setSystemTime(Date.now()+300_000);
  expect(s.monitor.guard()).toBe(false);expect(s.invalidate).toHaveBeenCalledTimes(1);
  expect(await s.monitor.verify()).toBe(true);expect(s.invalidate).toHaveBeenCalledTimes(1);
});
it('detects an overdue timer after sleep and performs fresh GETs without a wake event',async()=>{
  const s=setup();s.update(sample('wake-new-version',false));vi.setSystemTime(Date.now()+300_000);
  await vi.advanceTimersByTimeAsync(EVIDENCE_ACTIVE_MS);expect(s.read.runtime).toHaveBeenCalledTimes(1);expect(s.invalidate).toHaveBeenCalled();
});
it('invalidates BFCache/page restoration after a long gap even when versions stayed identical',async()=>{
  const s=setup(sample('same-version',false));vi.setSystemTime(Date.now()+300_000);window.dispatchEvent(new Event('pageshow'));await vi.advanceTimersByTimeAsync(0);
  expect(s.invalidate).toHaveBeenCalledTimes(1);expect(s.read.runtime).toHaveBeenCalledTimes(1);
});
it('does not confuse planned backend retry backoff with sleep or launch a retry storm',async()=>{
  const s=setup();s.read.runtime.mockRejectedValue(new Error('503'));
  for(const delay of [EVIDENCE_ACTIVE_MS,30_000,60_000,120_000,120_000])await vi.advanceTimersByTimeAsync(delay);
  expect(s.read.runtime).toHaveBeenCalledTimes(5);expect(s.invalidate).toHaveBeenCalledTimes(1);
});
it('does not accept a full reread while the browser remains offline',()=>{
  const s=setup();vi.spyOn(navigator,'onLine','get').mockReturnValue(false);
  expect(s.monitor.accept(sample('offline-cached-success'))).toBe(false);expect(s.invalidate).toHaveBeenCalled();
});
