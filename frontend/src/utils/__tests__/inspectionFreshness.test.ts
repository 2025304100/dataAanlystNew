import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { INSPECTION_CHANGE_KEY, inspectionMutationScope, observeInspectionTask, publishInspectionChange, subscribeInspectionChanges } from '../inspectionFreshness';
import { requestJson } from '../../api/client';

const posts:unknown[]=[];
class Channel { onmessage:((event:{data:unknown})=>void)|null=null; postMessage(value:unknown){posts.push(value);} close(){} }
beforeEach(()=>{localStorage.clear();posts.length=0;vi.stubGlobal('BroadcastChannel',Channel);});
afterEach(()=>{vi.restoreAllMocks();vi.unstubAllGlobals();});
const remote=(pid:number|null,id=crypto.randomUUID())=>({version:1,id,source:'another-tab',portfolioId:pid,at:Date.now()});
function storage(value:unknown,key=`${INSPECTION_CHANGE_KEY}1`){window.dispatchEvent(new StorageEvent('storage',{key,newValue:JSON.stringify(value)}));}

it('limits portfolio mutations and conservatively invalidates shared repair/scoring inputs',()=>{
  expect(inspectionMutationScope('/api/v1/portfolios/1/reconcile','POST')).toBe(1);
  expect(inspectionMutationScope('/api/v1/portfolios/2/inspection/repairs','POST')).toBeNull();
  expect(inspectionMutationScope('/api/v1/market-data/update','POST')).toBeNull();
  expect(inspectionMutationScope('/api/v1/scoring/tasks','POST')).toBeNull();
  expect(inspectionMutationScope('/api/v1/portfolios/1/inspection/runtime','GET')).toBeUndefined();
  expect(inspectionMutationScope('/api/v1/portfolios/1/inspection/analysis','POST')).toBeUndefined();
  expect(inspectionMutationScope('/api/v1/portfolios/1/inspection/coverage/preview','POST')).toBeUndefined();
});
it('deduplicates delivery, ignores another portfolio/malformed values, and cleans up',()=>{
  const changed=vi.fn(),stop=subscribeInspectionChanges(1,changed),value=remote(1);
  storage(remote(2));storage({version:1});storage(value);storage(value);storage(remote(null),`${INSPECTION_CHANGE_KEY}all`);
  expect(changed).toHaveBeenCalledTimes(2);stop();storage(remote(1));expect(changed).toHaveBeenCalledTimes(2);
});
it('recovers missed notification from persistent revision when returning to the tab',()=>{
  const changed=vi.fn(),stop=subscribeInspectionChanges(1,changed);
  localStorage.setItem(`${INSPECTION_CHANGE_KEY}1`,JSON.stringify(remote(1)));
  window.dispatchEvent(new Event('focus'));window.dispatchEvent(new Event('focus'));
  expect(changed).toHaveBeenCalledTimes(1);stop();
});
it('sends only invalidation metadata and ignores its own notification',()=>{
  const changed=vi.fn(),stop=subscribeInspectionChanges(1,changed);
  publishInspectionChange(1);const value=posts[0];storage(value);expect(changed).not.toHaveBeenCalled();
  expect(Object.keys(value as object).sort()).toEqual(['at','id','portfolioId','source','version']);stop();
});
it('announces start and completion without changing request body or response',async()=>{
  const payload={report_id:'fresh-v2'},fetch=vi.fn().mockResolvedValue({ok:true,json:async()=>payload});vi.stubGlobal('fetch',fetch);
  const body=JSON.stringify({trade_date:'2026-10-21'});
  expect(await requestJson('/api/v1/portfolios/1/reconcile',{method:'POST',body})).toEqual(payload);
  expect(fetch.mock.calls[0][1].body).toBe(body);expect(posts).toHaveLength(2);
});
it('refuses offline task writes before fetch and never queues them for reconnect',async()=>{
  const online=vi.spyOn(navigator,'onLine','get').mockReturnValue(false),fetch=vi.fn();vi.stubGlobal('fetch',fetch);
  await expect(requestJson('/api/v1/portfolios/1/inspection/repairs',{method:'POST',body:'{}'})).rejects.toMatchObject({error_code:'OFFLINE_NOT_SENT'});
  online.mockReturnValue(true);window.dispatchEvent(new Event('online'));await Promise.resolve();expect(fetch).not.toHaveBeenCalled();expect(posts).toHaveLength(0);
});
it('a failed write keeps conservative invalidation while failed GET never publishes',async()=>{
  vi.stubGlobal('fetch',vi.fn().mockResolvedValue({ok:false,status:503,json:async()=>({detail:'temporarily unavailable'})}));
  await expect(requestJson('/api/v1/portfolios/1/reconcile',{method:'POST'})).rejects.toThrow();expect(posts).toHaveLength(1);
  await expect(requestJson('/api/v1/portfolios/1/inspection/runtime')).rejects.toThrow();expect(posts).toHaveLength(1);
});
it('announces repair completion after asynchronous submit, without a repeat for unchanged progress',()=>{
  const url='/api/v1/portfolios/1/inspection/repairs/task-async-freshness';
  observeInspectionTask(url,'POST',{task_id:'task-async-freshness',status:'queued',processed:0});
  observeInspectionTask(url,'GET',{task_id:'task-async-freshness',status:'done',processed:2});
  observeInspectionTask(url,'GET',{task_id:'task-async-freshness',status:'done',processed:2});
  expect(posts).toHaveLength(1);expect(posts[0]).toMatchObject({portfolioId:null});
});
it('announces an observed background reconciliation result change only for its portfolio',()=>{
  const url='/api/v1/portfolios/55/inspection/reconciliation-job';
  observeInspectionTask(url,'GET',{portfolio_id:55,status:'running',queue:{days:[{trade_date:'2026-10-21',status:'NOT_CHECKED'}]}});
  const finished={portfolio_id:55,status:'done',queue:{days:[{trade_date:'2026-10-21',status:'EVIDENCE_INCOMPLETE'}]}};
  observeInspectionTask(url,'GET',finished);observeInspectionTask(url,'GET',finished);
  expect(posts).toHaveLength(1);expect(posts[0]).toMatchObject({portfolioId:55});
});
