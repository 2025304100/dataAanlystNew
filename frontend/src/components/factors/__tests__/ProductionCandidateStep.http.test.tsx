import {fireEvent,render,screen,waitFor} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
import ProductionCandidateStep from '../ProductionCandidateStep';
import {setLocale} from '../../../i18n';

const response=(data:unknown,status=200)=>new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});
const plan='a'.repeat(64),sha='b'.repeat(64);
const contract={plan_id:plan,training_cutoff:'2026-10-16',cost:{portfolio_id:1,currency:'CNY',evaluation_capital:450000,
  fees:{buy_commission_rate:0,min_commission:5,slippage_buy_bps:5},provenance:{min_commission:'matching_engine.default'},source_snapshot_id:'applied-snapshot'}};
afterEach(()=>{vi.unstubAllGlobals();setLocale('zh-CN');});

it.each(['zh-CN','en-US'] as const)('reviews costs before the sole training POST and recovers a lost response (%s)',async locale=>{
  setLocale(locale);let submitted=false;const writes:string[]=[];
  vi.stubGlobal('fetch',vi.fn(async(input,init)=>{
    const url=String(input);
    if(init?.method==='POST'){
      writes.push(url);expect(JSON.parse(init.body)).toEqual({bundle_id:'trusted-training',portfolio_id:1,expected_sha256:sha});
      submitted=true;throw new TypeError('response lost');
    }
    if(url.endsWith('/portfolios'))return response([{id:1,name:'main',is_default:1}]);
    if(url.includes('/predictions?'))return response({plan_id:plan,journal_count:0,independent_after:'2026-10-17',can_record_today:false});
    if(url.includes('/production-candidates/'))return submitted?response({id:'task-one',status:'done',percent:100,message:'Pending independent evidence'}):response({detail:'production_execution_task_not_found'},404);
    if(url.includes('/production-candidate/review'))return response({contract,sha256:sha,locked:false,current_cost_differs:false});
    throw new Error('Unexpected request '+url);
  }));
  render(<ProductionCandidateStep modelId="parent" bundleId="trusted-training" planId={plan}/>);
  const review=()=>screen.getByRole('button',{name:locale==='zh-CN'?'查看成本和训练范围':'Review costs and training scope'});
  await waitFor(()=>expect(review()).toBeEnabled());
  expect(writes).toHaveLength(0);
  fireEvent.click(review());
  await waitFor(()=>expect(screen.getByText('450,000 CNY')).toBeVisible());
  expect(screen.getByText('applied-snapshot')).toBeVisible();
  fireEvent.click(screen.getByRole('button',{name:locale==='zh-CN'?'锁定参数并训练候选（不启用）':'Lock and train candidate (no activation)'}));
  await waitFor(()=>expect(screen.getByText('task-one')).toBeVisible());
  expect(writes).toHaveLength(1);
  expect(writes[0]).toContain('/parent/production-candidate');
  expect(writes.some(url=>url.includes('/activate'))).toBe(false);
  expect(await screen.findByText(locale==='zh-CN'?'开盘前预测留痕：已保存 0 个信号日':'Pre-open prediction journal: 0 signal days saved')).toBeVisible();
  expect(screen.queryByRole('button',{name:locale==='zh-CN'?'锁定参数并训练候选（不启用）':'Lock and train candidate (no activation)'})).not.toBeInTheDocument();
});

it('recovers an unknown daily save by reading the journal and never repeats the POST',async()=>{
  let saved=false;const writes:string[]=[];
  vi.stubGlobal('fetch',vi.fn(async(input,init)=>{
    const url=String(input);
    if(init?.method==='POST'){
      writes.push(url);expect(JSON.parse(init.body)).toEqual({bundle_id:'today-input'});
      saved=true;throw new TypeError('save response lost');
    }
    if(url.endsWith('/portfolios'))return response([{id:1,name:'main',is_default:1}]);
    if(url.includes('/predictions?'))return response({plan_id:plan,journal_count:saved?1:0,independent_after:'2026-10-17',can_record_today:!saved});
    if(url.includes('/production-candidates/'))return response({id:'existing-task',status:'done',percent:100,message:'done'});
    throw new Error('Unexpected request '+url);
  }));
  render(<ProductionCandidateStep modelId="parent" bundleId="today-input" planId={plan}/>);
  fireEvent.click(await screen.findByRole('button',{name:'保存今日预测留痕（不启用、不交易）'}));
  expect(await screen.findByText('开盘前预测留痕：已保存 1 个信号日')).toBeVisible();
  expect(writes).toEqual([expect.stringContaining('/predictions')]);
  expect(screen.queryByRole('button',{name:'保存今日预测留痕（不启用、不交易）'})).not.toBeInTheDocument();
  expect(writes.some(url=>/activate|production-candidate$/.test(url))).toBe(false);
});
