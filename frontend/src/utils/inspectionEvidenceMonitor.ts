import type {InspectionJob,InspectionRuntime} from '../types/portfolioInspection';
import type {ReconciliationResponse} from '../types';

export const EVIDENCE_IDLE_MS=30_000;
export const EVIDENCE_ACTIVE_MS=5_000;
export const EVIDENCE_RETRY_MAX_MS=120_000;
export const EVIDENCE_WAKE_GAP_MS=60_000;
interface Evidence {runtime:InspectionRuntime;job:InspectionJob;report?:ReconciliationResponse|null;}
interface Readers {runtime:()=>Promise<InspectionRuntime>;job:()=>Promise<InspectionJob>;report:(date:string)=>Promise<ReconciliationResponse>;}
const stable=(value:unknown):string=>JSON.stringify(value,(_key,v)=>v&&typeof v==='object'&&!Array.isArray(v)?Object.fromEntries(Object.entries(v).sort(([a],[b])=>a.localeCompare(b))):v);

/** Compare authoritative identities, not read timestamps, heartbeat or the next scan time. */
export function inspectionJobIdentity(job:InspectionJob) {
  return {status:job.status,task:job.task_id,days:job.queue?.days?.map(day=>[day.trade_date,day.status,day.report_id,day.report_version]),
    work:job.work_items?.map(row=>[row.trade_date,row.work_state,row.verdict,row.requested_evidence_version,row.processed_evidence_version,row.last_error_code,row.retry_exhausted]),
    events:job.source_events?.events?.map(row=>[row.id,row.status])};
}
export function inspectionEvidenceIdentity({runtime,job,report}:Evidence):string {
  return stable({runtime:runtime.guide?.snapshot_id??{state:runtime.current_state,permissions:runtime.permissions,checks:runtime.recovery_summary,dimensions:runtime.dimensions},
    job:inspectionJobIdentity(job),
    report:report?[report.trade_date,report.report_id,report.report_version,report.zero_sum_check_passed,report.differences_found]:null});
}
const active=(value:Evidence)=>value.runtime.guide?.steps?.some(step=>step.status==='RUNNING'||step.tasks?.some(task=>['queued','running'].includes(task.status)))
  ||['queued','running'].includes(value.job.status)||value.job.work_items?.some(row=>['queued','running'].includes(row.work_state));

/** Receiver-owned GET monitoring. No task submission, replay, review or trading is performed. */
export function createInspectionEvidenceMonitor(portfolioId:number,read:Readers,onInvalidate:()=>void,onChecking:(value:boolean)=>void) {
  let baseline:string|null=null,date:string|undefined,running=false,generation=0,failures=0,announced:string|null=null;
  let mounted=false,inFlight:Promise<boolean>|null=null,timer:ReturnType<typeof setTimeout>|undefined;
  let lastActivity=Date.now(),nextDue=0,reconnecting=false;
  const clear=()=>{if(timer!==undefined)clearTimeout(timer);timer=undefined;nextDue=0;};
  const abandon=()=>{clear();generation++;inFlight=null;onChecking(false);};
  const expire=(reason:string)=>{if(announced!==reason){announced=reason;onInvalidate();}};
  const discontinuity=()=>{
    const now=Date.now(),elapsed=now-lastActivity;lastActivity=now;
    const scheduledWait=nextDue>0&&now<=nextDue+5_000;
    if(elapsed>=EVIDENCE_WAKE_GAP_MS&&!scheduledWait||elapsed<0){abandon();expire('wake-gap');schedule();return true;}
    return false;
  };
  const guard=()=>{
    if(!mounted)return false;
    if(navigator.onLine===false){abandon();expire('offline');return false;}
    const gap=discontinuity();
    return !gap&&baseline!==null;
  };
  const schedule=()=>{
    clear();if(!mounted||baseline===null||document.visibilityState==='hidden'||navigator.onLine===false)return;
    const delay=failures?Math.min(EVIDENCE_RETRY_MAX_MS,EVIDENCE_IDLE_MS*2**(failures-1)):running?EVIDENCE_ACTIVE_MS:EVIDENCE_IDLE_MS;
    nextDue=Date.now()+delay;timer=setTimeout(()=>void verify(),delay);
  };
  const verify=():Promise<boolean>=>{
    if(!mounted)return Promise.resolve(false);
    discontinuity();
    if(navigator.onLine===false){abandon();expire('offline');return Promise.resolve(false);}
    if(inFlight)return inFlight;
    if(!mounted||baseline===null)return Promise.resolve(false);
    clear();const token=++generation,expected=baseline,expectedDate=date;onChecking(true);
    const work=(async()=>{
      try {
        const values=await Promise.resolve().then(()=>Promise.all([read.runtime(),read.job(),expectedDate?read.report(expectedDate):Promise.resolve(null)]));
        if(!mounted||token!==generation)return false;
        if(!guard()||token!==generation)return false;
        const [runtime,job,report]=values;
        if(runtime?.portfolio_id!==portfolioId||job?.portfolio_id!==portfolioId||!runtime.permissions||!runtime.recovery_summary||!Array.isArray(job.queue?.days)
          ||expectedDate&&(!report||report.portfolio_id!==portfolioId||report.trade_date!==expectedDate||!Array.isArray(report.items)))throw new Error('Inspection evidence scope mismatch');
        const fresh={runtime,job,report};running=Boolean(active(fresh));failures=0;
        const identity=inspectionEvidenceIdentity(fresh);
        if(identity!==expected){if(announced!==identity){announced=identity;onInvalidate();}return false;}
        return true;
      }catch{
        if(mounted&&token===generation){failures++;if(announced!=='unavailable'){announced='unavailable';onInvalidate();}}
        return false;
      }finally{
        if(mounted&&token===generation){inFlight=null;reconnecting=false;onChecking(false);schedule();}
      }
    })();
    inFlight=work;return work;
  };
  const activated=()=>{if(document.visibilityState==='hidden'){lastActivity=Date.now();abandon();}else{discontinuity();void verify();}};
  const offline=()=>{lastActivity=Date.now();reconnecting=false;abandon();expire('offline');};
  const online=()=>{if(navigator.onLine===false)return;lastActivity=Date.now();if(!reconnecting){abandon();expire('reconnected');reconnecting=true;}if(document.visibilityState!=='hidden')void verify();};
  return {
    accept(value:Evidence){
      if(value.runtime.portfolio_id!==portfolioId||value.job.portfolio_id!==portfolioId)return false;
      if(discontinuity()||navigator.onLine===false){if(navigator.onLine===false)offline();return false;}
      generation++;inFlight=null;onChecking(false);baseline=inspectionEvidenceIdentity(value);date=value.report?.trade_date;
      running=Boolean(active(value));announced=null;failures=0;schedule();return true;
    },
    verify,guard,
    attach(){mounted=true;lastActivity=Date.now();window.addEventListener('focus',activated);window.addEventListener('pageshow',activated);window.addEventListener('offline',offline);window.addEventListener('online',online);document.addEventListener('visibilitychange',activated);schedule();
      return()=>{mounted=false;abandon();window.removeEventListener('focus',activated);window.removeEventListener('pageshow',activated);window.removeEventListener('offline',offline);window.removeEventListener('online',online);document.removeEventListener('visibilitychange',activated);};},
  };
}
