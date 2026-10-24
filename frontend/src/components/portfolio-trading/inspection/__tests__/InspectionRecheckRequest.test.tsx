import {beforeEach,expect,it,vi} from 'vitest';
import {render,screen,waitFor,fireEvent,cleanup} from '@testing-library/react';
import InspectionRecheckRequest from '../InspectionRecheckRequest';
import {RECHECK_SUBMISSION_PREFIX} from '../recheckSubmissionJournal';
const {get}=vi.hoisted(()=>({get:vi.fn()}));
vi.mock('../../../../api/client',()=>({api:{getPortfolioRecheckRequest:get}}));
beforeEach(()=>{cleanup();localStorage.clear();vi.resetAllMocks();});
const record=(pid=1)=>({version:1,portfolioId:pid,requestKey:`original-key-${pid}`,tradeDate:'2026-09-29',createdAt:Date.now()});
const result=(pid=1)=>({portfolio_id:pid,request_key:`original-key-${pid}`,trade_date:'2026-09-29',task_id:'a'.repeat(32),dispatch_status:'QUEUED',task_status:'running'});
it('recovers the exact original receipt on mount without any POST',async()=>{
  localStorage.setItem(RECHECK_SUBMISSION_PREFIX+1,JSON.stringify(record()));get.mockResolvedValue(result());
  render(<InspectionRecheckRequest portfolioId={1} onRetry={vi.fn()}/>);
  await screen.findByText(/已找回原请求对应任务/);expect(get).toHaveBeenCalledWith(1,'original-key-1');
});
it('retains a 503, then reads the original key after reconnect',async()=>{
  localStorage.setItem(RECHECK_SUBMISSION_PREFIX+1,JSON.stringify(record()));get.mockRejectedValueOnce(new Error('后端暂不可用')).mockResolvedValue(result());
  render(<InspectionRecheckRequest portfolioId={1} onRetry={vi.fn()}/>);
  await screen.findByRole('alert');fireEvent(window,new Event('online'));
  await screen.findByText(/已找回原请求对应任务/);expect(get).toHaveBeenCalledTimes(2);expect(get).toHaveBeenLastCalledWith(1,'original-key-1');
});
it('offers an explicit original-key retry only for exact request-not-found',async()=>{
  localStorage.setItem(RECHECK_SUBMISSION_PREFIX+1,JSON.stringify(record()));get.mockRejectedValue(Object.assign(new Error('未找到回执'),{status_code:404,detail:{error_code:'RECHECK_REQUEST_NOT_FOUND'}}));const retry=vi.fn();
  render(<InspectionRecheckRequest portfolioId={1} onRetry={retry}/>);
  fireEvent.click(await screen.findByRole('button',{name:'使用原请求重新核对'}));expect(retry).toHaveBeenCalledOnce();expect(localStorage.getItem(RECHECK_SUBMISSION_PREFIX+1)).not.toBeNull();
});
it('discards a delayed previous-portfolio response',async()=>{
  for(const pid of [1,2])localStorage.setItem(RECHECK_SUBMISSION_PREFIX+pid,JSON.stringify(record(pid)));
  let release!:(v:unknown)=>void;get.mockImplementation((pid)=>pid===1?new Promise(resolve=>{release=resolve;}):Promise.resolve(result(2)));
  const view=render(<InspectionRecheckRequest portfolioId={1} onRetry={vi.fn()}/>);view.rerender(<InspectionRecheckRequest portfolioId={2} onRetry={vi.fn()}/>);
  await screen.findByText(/已找回原请求对应任务/);release({...result(),trade_date:'2026-10-01'});
  await waitFor(()=>expect(get).toHaveBeenCalledWith(2,'original-key-2'));expect(screen.queryByText(/2026-10-01/)).not.toBeInTheDocument();
});
it('shows retained acceptance provenance without presenting it as a passed review',async()=>{
  localStorage.setItem(RECHECK_SUBMISSION_PREFIX+1,JSON.stringify(record()));
  get.mockResolvedValue({...result(),provenance_annotations:[{label:'验收产生的请求关联',note:'验收关页拦截异常产生，关联已有任务，不代表核对通过或交易授权。'}]});
  render(<InspectionRecheckRequest portfolioId={1} onRetry={vi.fn()}/>);
  await screen.findByText('验收产生的请求关联');
  expect(screen.getByText(/验收关页拦截异常产生/)).toBeInTheDocument();
  expect(screen.getByText(/任务完成不代表账差通过/)).toBeInTheDocument();
});
