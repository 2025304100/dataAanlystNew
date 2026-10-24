import {inspectionJobIdentity} from './inspectionEvidenceMonitor';
import type {InspectionJob} from '../types/portfolioInspection';
/** Cross-tab invalidation carries no reports, permissions, notes or trading instructions. */
export const INSPECTION_CHANGE_KEY = 'portfolio-inspection-change-v1:';
const CHANNEL = 'portfolio-inspection-changes-v1';
const LOCAL_CHANGE = 'portfolio-inspection-local-change-v1';
const VERIFYING = 'portfolio-inspection-verifying-v1';
const verification = new Set<number>();
const reviewGuards=new Map<number,()=>boolean>();
export function registerInspectionReviewGuard(portfolioId:number,guard:()=>boolean){reviewGuards.set(portfolioId,guard);return()=>{if(reviewGuards.get(portfolioId)===guard)reviewGuards.delete(portfolioId);};}
export function inspectionReviewCurrent(portfolioId:number){return navigator.onLine!==false&&(reviewGuards.get(portfolioId)?.()??true);}
export interface InspectionChange { version: 1; id: string; source: string; portfolioId: number | null; at: number; }
const source = typeof crypto !== 'undefined' ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;

function valid(value: unknown): value is InspectionChange {
  const v = value as InspectionChange;
  return Boolean(v && v.version === 1 && typeof v.id === 'string' && typeof v.source === 'string'
    && Number.isFinite(v.at) && (v.portfolioId === null || Number.isSafeInteger(v.portfolioId) && v.portfolioId > 0));
}
export function publishInspectionChange(portfolioId: number | null) {
  const message: InspectionChange = { version: 1, id: crypto.randomUUID(), source, portfolioId, at: Date.now() };
  try { localStorage.setItem(`${INSPECTION_CHANGE_KEY}${portfolioId ?? 'all'}`, JSON.stringify(message)); } catch { /* BroadcastChannel remains available. */ }
  try { const channel = new BroadcastChannel(CHANNEL); channel.postMessage(message); channel.close(); } catch { /* Storage events remain available. */ }
}
/** Independent backend observation must invalidate this document as well as other tabs. */
export function invalidateInspectionHere(portfolioId: number) {
  window.dispatchEvent(new CustomEvent(LOCAL_CHANGE,{detail:portfolioId}));
  publishInspectionChange(portfolioId);
}
export function setInspectionVerifying(portfolioId:number,value:boolean) {
  if(value)verification.add(portfolioId);else verification.delete(portfolioId);
  window.dispatchEvent(new CustomEvent(VERIFYING,{detail:portfolioId}));
}
export function inspectionIsVerifying(portfolioId:number) { return verification.has(portfolioId); }
export function subscribeInspectionVerification(portfolioId:number,listener:()=>void) {
  const handler=(event:Event)=>{if((event as CustomEvent).detail===portfolioId)listener();};
  window.addEventListener(VERIFYING,handler);return()=>window.removeEventListener(VERIFYING,handler);
}

export function subscribeInspectionChanges(portfolioId: number, onChange: () => void) {
  const seen = new Set<string>();
  const receive = (value: unknown) => {
    if (!valid(value) || value.source === source || value.portfolioId !== null && value.portfolioId !== portfolioId || seen.has(value.id)) return;
    seen.add(value.id); if (seen.size > 200) seen.delete(seen.values().next().value!);
    onChange();
  };
  const keys = [`${INSPECTION_CHANGE_KEY}${portfolioId}`, `${INSPECTION_CHANGE_KEY}all`];
  const stamps = new Map<string,string|null>();
  for (const key of keys) { try { stamps.set(key, localStorage.getItem(key)); } catch { /* Checked again on activation. */ } }
  const storage = (event: StorageEvent) => {
    if (!event.key || !keys.includes(event.key) || !event.newValue) return;
    stamps.set(event.key, event.newValue);
    try { receive(JSON.parse(event.newValue)); } catch { /* Unrelated/malformed storage cannot become business data. */ }
  };
  const activated = () => {
    if (document.visibilityState === 'hidden') return;
    for (const key of keys) {
      try {
        const value = localStorage.getItem(key);
        if (value !== stamps.get(key)) { stamps.set(key,value); if (value) { try { receive(JSON.parse(value)); } catch { /* Ignore malformed event. */ } } }
      } catch { onChange(); break; } // Unable to verify missed updates: require a fresh read.
    }
  };
  let channel: BroadcastChannel | undefined;
  try { channel = new BroadcastChannel(CHANNEL); channel.onmessage = event => receive(event.data); } catch { /* Storage fallback. */ }
  window.addEventListener('storage',storage);
  const offline=()=>onChange();window.addEventListener('offline',offline);
  const local=(event:Event)=>{if((event as CustomEvent).detail===portfolioId)onChange();};
  window.addEventListener(LOCAL_CHANGE,local);
  window.addEventListener('focus',activated);
  document.addEventListener('visibilitychange',activated);
  return () => { channel?.close();window.removeEventListener('offline',offline); window.removeEventListener(LOCAL_CHANGE,local);window.removeEventListener('storage',storage); window.removeEventListener('focus',activated); document.removeEventListener('visibilitychange',activated); };
}

/** Conservative scope: shared market/scoring inputs may affect more than one portfolio. */
export function inspectionMutationScope(url: string, method: string): number | null | undefined {
  if (['GET','HEAD','OPTIONS'].includes(method.toUpperCase())) return undefined;
  const path = new URL(url, window.location.origin).pathname.replace(/^\/api\/v1/, '');
  if (/\/(?:preview|analyse|analyze|analysis|precheck|export|explanation)(?:\/|$)/.test(path)) return undefined;
  const match = path.match(/^\/portfolios\/(\d+)(?:\/|$)/);
  if (match) return /\/inspection\/repairs(?:\/|$)/.test(path) ? null : Number(match[1]);
  if (/^\/(?:market-data|scoring|factors|external-data|universe|index-prices|strategies|rules|symbols)(?:\/|$)/.test(path)) return null;
  return undefined;
}

const tasks = new Map<string,string>();
/** A queued repair can finish after its submit response; publish again when observed progress changes. */
export function observeInspectionTask(url: string, method: string, payload: unknown) {
  const path = new URL(url, window.location.origin).pathname;
  if (method.toUpperCase() === 'GET' && /\/inspection\/reconciliation-job$/.test(path) && payload && typeof payload === 'object') {
    const job = payload as {portfolio_id?:number;status?:string;last_run_at?:string;task_id?:string;queue?:{days?:Array<{trade_date:string;status:string;report_version?:string}>};work_items?:unknown[]};
    if (!Number.isSafeInteger(job.portfolio_id) || !Array.isArray(job.queue?.days)) return;
    const key = `job:${job.portfolio_id}`;
    const signature = JSON.stringify(inspectionJobIdentity(job as InspectionJob));
    const previous = tasks.get(key); tasks.set(key,signature);
    if (previous !== undefined && previous !== signature) publishInspectionChange(job.portfolio_id!);
    return;
  }
  if (!/\/(?:repairs|sync-tasks|scoring\/tasks)(?:\/|$)/.test(path) || !payload || typeof payload !== 'object' || Array.isArray(payload)) return;
  const task = payload as Record<string,unknown>;
  const id = task.task_id ?? task.id;
  if (typeof id !== 'string' || typeof task.status !== 'string') return;
  const key = `${path.split(/\/(?:repairs|sync-tasks|tasks)/)[0]}:${id}`;
  const signature = JSON.stringify([task.status,task.processed,task.ok_count,task.failed_count,task.updated_at]);
  const previous = tasks.get(key);
  const active = ['queued','running','pending'].includes(task.status.toLowerCase());
  if (previous !== undefined && previous !== signature && method.toUpperCase() === 'GET') {
    const portfolio = path.match(/\/portfolios\/(\d+)/);
    // Portfolio repairs update shared data too: conservatively invalidate all portfolios.
    publishInspectionChange(portfolio && !path.includes('/repairs') ? Number(portfolio[1]) : null);
  }
  if (active || previous !== undefined || method.toUpperCase() !== 'GET') tasks.set(key,signature);
  if (tasks.size > 200) tasks.delete(tasks.keys().next().value!);
}
