import {afterEach,beforeEach,expect,it,vi} from 'vitest';
import {startAcceptanceMonitor,refreshAcceptanceMode,getAcceptanceMode,observeAcceptanceReadOnly,acceptanceMutationReason} from '../acceptanceMode';
import {api} from '../../api/client';
let stop=()=>{};
const response=(mode='inspection_acceptance',read_only=true)=>({ok:true,json:async()=>({mode,read_only})});
beforeEach(()=>{vi.spyOn(navigator,'onLine','get').mockReturnValue(true);});
afterEach(()=>{stop();vi.restoreAllMocks();vi.unstubAllGlobals();vi.useRealTimers();});
it('blocks before the first response, then shares the server readonly mode without cached permissions',async()=>{
  let resolve!:(r:unknown)=>void;vi.stubGlobal('fetch',vi.fn(()=>new Promise(r=>{resolve=r;})));stop=startAcceptanceMonitor();
  expect(getAcceptanceMode()).toMatchObject({state:'checking',blocked:true});await Promise.resolve();resolve(response());await refreshAcceptanceMode();
  expect(getAcceptanceMode()).toMatchObject({state:'readonly',readOnly:true,blocked:true});
  stop();stop=startAcceptanceMonitor();expect(getAcceptanceMode().state).toBe('checking');
});
it('does not consider a 404, malformed response or a missing readonly flag to be normal mode',async()=>{
  const fetch=vi.fn().mockResolvedValueOnce({ok:false,status:404}).mockResolvedValueOnce(response('application',true)).mockResolvedValueOnce({ok:true,json:async()=>({})});vi.stubGlobal('fetch',fetch);stop=startAcceptanceMonitor();
  for(let i=0;i<3;i++){await refreshAcceptanceMode();expect(getAcceptanceMode().blocked).toBe(true);}
});
it('accepts normal mode only from the explicit application response',async()=>{
  vi.stubGlobal('fetch',vi.fn().mockResolvedValue(response('application',false)));stop=startAcceptanceMonitor();await refreshAcceptanceMode();
  expect(getAcceptanceMode()).toMatchObject({state:'normal',blocked:false});expect(acceptanceMutationReason()).toBeNull();
});
it('retains readonly on failures and re-reads after reconnection without submitting anything',async()=>{
  const fetch=vi.fn().mockResolvedValue(response());vi.stubGlobal('fetch',fetch);stop=startAcceptanceMonitor();await refreshAcceptanceMode();fetch.mockRejectedValueOnce(new Error('restarting'));await refreshAcceptanceMode();
  expect(getAcceptanceMode()).toMatchObject({state:'unavailable',readOnly:true,blocked:true});window.dispatchEvent(new Event('online'));await refreshAcceptanceMode();expect(getAcceptanceMode().state).toBe('readonly');
  expect(fetch.mock.calls.every(call=>call[1].method==='GET')).toBe(true);
});
it('discards an old normal response after an offline signal and a later readonly read',async()=>{
  let resolve!:(r:unknown)=>void;const fetch=vi.fn().mockImplementationOnce(()=>new Promise(r=>{resolve=r;})).mockResolvedValue(response());vi.stubGlobal('fetch',fetch);stop=startAcceptanceMonitor();await Promise.resolve();
  window.dispatchEvent(new Event('offline'));window.dispatchEvent(new Event('online'));await refreshAcceptanceMode();resolve(response('application',false));await Promise.resolve();await Promise.resolve();
  expect(getAcceptanceMode()).toMatchObject({state:'readonly',blocked:true});
});
it('server readonly rejection invalidates any in-flight normal response',async()=>{
  let resolve!:(r:unknown)=>void;vi.stubGlobal('fetch',vi.fn(()=>new Promise(r=>{resolve=r;})));stop=startAcceptanceMonitor();await Promise.resolve();observeAcceptanceReadOnly();resolve(response('application',false));await Promise.resolve();await Promise.resolve();
  expect(getAcceptanceMode()).toMatchObject({state:'readonly',blocked:true});
});
it('polls only GET and has no transient normal-mode lock during a fresh background read',async()=>{
  vi.useFakeTimers();const fetch=vi.fn().mockResolvedValue(response('application',false));vi.stubGlobal('fetch',fetch);stop=startAcceptanceMonitor();await refreshAcceptanceMode();
  await vi.advanceTimersByTimeAsync(15000);expect(getAcceptanceMode().blocked).toBe(false);expect(fetch).toHaveBeenCalledTimes(2);
});
it('rejects a stale or backwards-clock normal permission before a write',async()=>{
  vi.useFakeTimers();const fetch=vi.fn().mockResolvedValue(response('application',false));vi.stubGlobal('fetch',fetch);stop=startAcceptanceMonitor();await refreshAcceptanceMode();vi.setSystemTime(Date.now()+60000);
  expect(acceptanceMutationReason()).not.toBeNull();expect(getAcceptanceMode().blocked).toBe(true);
});
it('client write guard prevents fetch while readonly but allows existing GET reads',async()=>{
  const fetch=vi.fn().mockResolvedValue(response());vi.stubGlobal('fetch',fetch);stop=startAcceptanceMonitor();await refreshAcceptanceMode();
  await expect(api.recheckPortfolioSources(1,undefined,'retained-original-key')).rejects.toMatchObject({error_code:'LOCAL_INSPECTION_READ_ONLY'});
  await api.getPortfolioInspectionRuntime(1);expect(fetch.mock.calls.every(call=>!call[1]?.method||call[1].method==='GET')).toBe(true);
});
it('client also blocks writes while the first mode read is still unresolved',async()=>{
  const fetch=vi.fn(()=>new Promise(()=>{}));vi.stubGlobal('fetch',fetch);stop=startAcceptanceMonitor();
  await expect(api.recheckPortfolioSources(1)).rejects.toMatchObject({error_code:'SERVICE_MODE_UNVERIFIED'});
  expect(fetch.mock.calls.every(call=>(call as any)[1].method==='GET')).toBe(true);
});
