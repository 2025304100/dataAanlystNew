import {fireEvent,render,screen,waitFor} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
import ProductionReleaseStep from '../ProductionReleaseStep';

const response=(data:unknown,status=200)=>new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});
const plan='a'.repeat(64),sha='b'.repeat(64);
afterEach(()=>vi.unstubAllGlobals());

it('requires the user checkbox and sends only the reviewed identity, then reads back a lost confirmation',async()=>{
  let confirmed=false;const writes:unknown[]=[];
  vi.stubGlobal('fetch',vi.fn(async(input,init)=>{
    const url=String(input);
    if(init?.method==='POST'){
      writes.push(JSON.parse(init.body));confirmed=true;throw new TypeError('response lost');
    }
    if(url.includes('/independent-review'))return response({ready:true,sha256:sha,blockers:[],mature_signal_count:50,missing_journal_dates:[]});
    if(url.includes('/release-review'))return response({plan_id:plan,candidate_model_id:'candidate',ready:!confirmed,
      sha256:sha,blockers:[],qualification_issued:confirmed,model_activated:confirmed,message:'已核对',
      result:{mean_daily_ic:.04,net_return:.02,evidence_pass:true}});
    throw new Error('Unexpected request '+url);
  }));
  render(<ProductionReleaseStep planId={plan} bundleId="mature-input"/>);
  const button=await screen.findByRole('button',{name:'确认并启用模型'});
  expect(button).toBeDisabled();expect(writes).toEqual([]);
  fireEvent.click(screen.getByRole('checkbox'));
  fireEvent.click(button);
  await waitFor(()=>expect(screen.getByText('模型已确认并启用')).toBeVisible());
  expect(writes).toEqual([{bundle_id:'mature-input',expected_sha256:sha,confirmed:true}]);
});

it('shows missing days and offers a refresh, without an activation choice',async()=>{
  vi.stubGlobal('fetch',vi.fn(async input=>String(input).includes('/independent-review')?
    response({ready:false,sha256:sha,blockers:['FIRST_50_T_PLUS_5_OUTCOMES_NOT_MATURE'],mature_signal_count:45,missing_journal_dates:[]}):
    response({plan_id:plan,candidate_model_id:'candidate',ready:false,sha256:sha,
      blockers:['FIRST_50_T_PLUS_5_OUTCOMES_NOT_MATURE'],qualification_issued:false,model_activated:false,message:'等待后续数据'})));
  render(<ProductionReleaseStep planId={plan} bundleId="mature-input"/>);
  expect(await screen.findByText('后续行情尚未齐备，目前有 45 个信号日可以验证，需要 50 个。')).toBeVisible();
  expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
  expect(screen.queryByRole('button',{name:'确认并启用模型'})).not.toBeInTheDocument();
  await waitFor(()=>expect(screen.getByRole('button',{name:'重新检查'})).toBeEnabled());
});

it('keeps the precise cost blocker when another independent read fails',async()=>{
  vi.stubGlobal('fetch',vi.fn(async input=>String(input).includes('/independent-review')?
    response({detail:'production_cost_evidence_not_found'},404):
    response({plan_id:plan,candidate_model_id:'candidate',ready:false,sha256:sha,
      blockers:['验证费用没有事先固定，不能补发确认。'],qualification_issued:false,model_activated:false,message:'等待检查'})));
  render(<ProductionReleaseStep planId={plan} bundleId="mature-input"/>);
  expect(await screen.findByText('验证费用没有事先固定，不能补发确认。')).toBeVisible();
  expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
});

it('turns a technical read error into an actionable everyday message',async()=>{
  vi.stubGlobal('fetch',vi.fn(async()=>response({detail:'production_execution_contract_changed'},409)));
  render(<ProductionReleaseStep planId={plan} bundleId="mature-input"/>);
  expect(await screen.findByText('暂时无法核对结果，请重新检查。已保存的记录会保留，不会自动重复启用。')).toBeVisible();
  expect(screen.queryByText(/production_execution_contract_changed/)).not.toBeInTheDocument();
  expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
});
