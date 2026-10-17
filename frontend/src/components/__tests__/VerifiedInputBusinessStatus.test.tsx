import { cleanup,fireEvent,render,screen,waitFor } from "@testing-library/react";
import { afterEach,beforeEach,describe,expect,it,vi } from "vitest";
import { setLocale } from "../../i18n";
const mock=vi.hoisted(()=>({listVerifiedInputBundles:vi.fn(),getInputBusinessStatus:vi.fn(),createInputReplay:vi.fn(),activateScoringModel:vi.fn()}));
vi.mock('../../api/client',()=>({api:mock}));
import VerifiedInputBusinessStatus from '../VerifiedInputBusinessStatus';
const data={fixed_symbols:129,first_date:'2026-09-01',last_date:'2026-10-16',calculation_date:'2026-10-16',
  business_counts:{bars:3592,financial:1150,flow:364},warehouse_counts:{raw_daily_bars:3592,raw_financial_reports:1150,raw_fund_flows:364},
  fixed_pool_coverage:[{code:'turnover_z20',eligible:128,denominator:129}],market_coverage:[{factor_code:'turnover_z20',eligible_symbols:128,universe_symbols:5555,coverage:128/5555}],bundle_manifest_sha256:'a'.repeat(64)};
describe('business input status',()=>{
  beforeEach(()=>{vi.clearAllMocks();setLocale('zh-CN');mock.listVerifiedInputBundles.mockResolvedValue([{bundle_id:'fixed'}]);mock.getInputBusinessStatus.mockResolvedValue(data);});
  afterEach(cleanup);
  it('only reads on explicit open, showing separate market and fixed denominators',async()=>{
    render(<VerifiedInputBusinessStatus/>);expect(mock.listVerifiedInputBundles).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button',{name:'查看业务接入'}));
    await waitFor(()=>expect(screen.getByText('128 / 129')).toBeVisible());
    expect(screen.getByText('128 / 5555 · 2.30%')).toBeVisible();expect(screen.getByText('3,592 / 3,592')).toBeVisible();
    fireEvent.click(screen.getByRole('button',{name:'刷新只读核对'}));await waitFor(()=>expect(mock.getInputBusinessStatus).toHaveBeenCalledTimes(2));
    expect(mock.createInputReplay).not.toHaveBeenCalled();expect(mock.activateScoringModel).not.toHaveBeenCalled();
  });
  it('shows failure and retries reads without publishing or activating',async()=>{
    mock.getInputBusinessStatus.mockRejectedValueOnce(new Error('unavailable'));
    render(<VerifiedInputBusinessStatus/>);fireEvent.click(screen.getByRole('button',{name:'查看业务接入'}));
    await screen.findByText('Error: unavailable');fireEvent.click(screen.getByRole('button',{name:'刷新只读核对'}));
    await waitFor(()=>expect(screen.getByText('128 / 129')).toBeVisible());
    expect(mock.activateScoringModel).not.toHaveBeenCalled();
  });
});
