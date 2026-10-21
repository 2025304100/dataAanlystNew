import {act, fireEvent, render, screen, waitFor} from '@testing-library/react';
import {afterEach, beforeEach, expect, it, vi} from 'vitest';
import ProductionAdmissionModal from '../ProductionAdmissionModal';
import {setLocale} from '../../../i18n';

const response=(data:unknown,status=200)=>new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});
const report=(id='research-parent')=>({model_id:id,model_label:id,parent_fingerprint:'b'.repeat(64),identity_sha256:'a'.repeat(64),
  can_freeze:true,sample_precheck_ready:false,production_eligible:false,planning_only:true,read_only:true,
  training_not_submitted:true,model_not_activated:true,independent_after:'2026-10-17',independent_signal_days_upper_bound:0,
  potential_mature_signal_days_upper_bound:4,bundle:{bundle_id:'verified-inputs',dataset_id:'trusted-history',raw_trade_dates:28},
  identity:{factor_set_id:'frozen-original',fit_policy:'cross_sectional_rank_v1',alphas:[.1,1,10],observed_through:'2026-10-17'},
  cohort_change:{parent_count:128,planned_count:129,added:['000001'],removed:[]},frozen_plan:null,frozen_plan_sha256:null,
  gates:[{code:'PRODUCTION_SAMPLE_SPAN',stage:'training',passed:false,actual:28,required:274,message:'server text'},
    {code:'INDEPENDENT_NET_COST_EVIDENCE',stage:'release',passed:null,message:'server text'}]});
const frozen=(id='research-parent')=>{const r=report(id);return {...r,frozen_plan:{plan_id:'a'.repeat(64),identity:r.identity,is_training_or_release_certificate:false,frozen_at:'2026-10-17T00:00:00Z'}};};
const bundles=[{bundle_id:'verified-inputs',label:'固定129股核验数据'}];

beforeEach(()=>setLocale('zh-CN'));
afterEach(()=>{vi.unstubAllGlobals();setLocale('zh-CN');});

it.each(['zh-CN','en-US'] as const)('reviews then explicitly freezes without training or activation (%s)',async locale=>{
  setLocale(locale);const writes:{url:string;body:unknown}[]=[];
  vi.stubGlobal('fetch',vi.fn(async(input,init)=>{
    const url=String(input);
    if(url.endsWith('/input-bundles'))return response(bundles);
    if(init?.method==='POST'){writes.push({url,body:JSON.parse(init.body)});return response(frozen());}
    return response(report());
  }));
  render(<ProductionAdmissionModal modelId="research-parent" onClose={vi.fn()}/>);
  const review=()=>screen.getByRole('button',{name:locale==='zh-CN'?'下一步：核对验证方案':'Next: review the protocol'});
  await waitFor(()=>expect(review()).toBeEnabled(),{timeout:5000});
  expect(screen.getAllByText('28 / 274').length).toBeGreaterThan(0);
  expect(writes).toEqual([]);
  fireEvent.click(review());
  await waitFor(()=>expect(screen.getByText('128 → 129 · 000001')).toBeVisible());
  expect(screen.getByText('250 / 50 · T+1 → T+5')).toBeVisible();
  fireEvent.click(screen.getByRole('button',{name:locale==='zh-CN'?'确认保存方案':'Freeze protocol (no training or activation)'}));
  await waitFor(()=>expect(screen.getByText(locale==='zh-CN'?'验证方案已保存':'Validation protocol frozen')).toBeVisible());
  expect(writes).toEqual([{url:expect.stringContaining('/research-parent/production-admission/freeze'),body:{bundle_id:'verified-inputs',expected_identity_sha256:'a'.repeat(64)}}]);
  expect(screen.queryByText('server text')).not.toBeInTheDocument();
});

it('recovers an uncertain freeze by GET without submitting a second POST',async()=>{
  let committed=false,posts=0;
  vi.stubGlobal('fetch',vi.fn(async(input,init)=>{
    if(String(input).endsWith('/input-bundles'))return response(bundles);
    if(init?.method==='POST'){posts++;committed=true;throw new TypeError('response lost');}
    return response(committed?frozen():report());
  }));
  render(<ProductionAdmissionModal modelId="research-parent" onClose={vi.fn()}/>);
  const review=()=>screen.getByRole('button',{name:'下一步：核对验证方案'});
  await waitFor(()=>expect(review()).toBeEnabled(),{timeout:5000});fireEvent.click(review());
  fireEvent.click(screen.getByRole('button',{name:'确认保存方案'}));
  await waitFor(()=>expect(screen.getByText(/冻结结果尚未确认/)).toBeVisible());
  expect(screen.getByRole('button',{name:'下一步：核对验证方案'})).toBeDisabled();
  fireEvent.click(screen.getByRole('button',{name:'重新检查资料'}));
  await waitFor(()=>expect(screen.getByText('验证方案已保存')).toBeVisible());
  expect(posts).toBe(1);
});

it('blocks a mismatched model response and never writes',async()=>{
  const writes=vi.fn();
  vi.stubGlobal('fetch',vi.fn(async(input,init)=>{
    if(init?.method==='POST')writes();
    return response(String(input).endsWith('/input-bundles')?bundles:report('wrong-model'));
  }));
  render(<ProductionAdmissionModal modelId="research-parent" onClose={vi.fn()}/>);
  await waitFor(()=>expect(screen.getByText(/返回证据与当前模型或方案不一致/)).toBeVisible());
  expect(screen.getByRole('button',{name:'下一步：核对验证方案'})).toBeDisabled();expect(writes).not.toHaveBeenCalled();
});

it('ignores a late response from the previous model',async()=>{
  let resolveOld:(r:Response)=>void=()=>{};
  vi.stubGlobal('fetch',vi.fn((input)=>{
    const url=String(input);
    if(url.endsWith('/input-bundles'))return Promise.resolve(response(bundles));
    if(url.includes('/old/'))return new Promise<Response>(resolve=>{resolveOld=resolve;});
    return Promise.resolve(response(report('new')));
  }));
  const view=render(<ProductionAdmissionModal modelId="old" onClose={vi.fn()}/>);
  await waitFor(()=>expect(screen.getByRole('status')).toBeVisible());
  view.rerender(<ProductionAdmissionModal modelId="new" onClose={vi.fn()}/>);
  await waitFor(()=>expect(screen.getByText('new')).toBeVisible());
  await act(async()=>resolveOld(response(report('old'))));
  expect(screen.queryByText('old')).not.toBeInTheDocument();
  expect(screen.getByText('new')).toBeVisible();
});
