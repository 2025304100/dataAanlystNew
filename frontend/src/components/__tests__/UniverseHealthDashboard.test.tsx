import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import UniverseHealthDashboard, { preciseCoverage, type UniverseQuality, type UniverseQualityScope } from '../UniverseHealthDashboard';

function group(total: number, changes: Partial<UniverseQualityScope> = {}): UniverseQualityScope {
  return { total, with_bars: total, without_bars: 0, coverage_pct: 100, sync_flag_count: total,
    sync_flag_without_bars: 0, cache_mismatches: 0, listing_date_recorded: 0, listing_date_missing: total,
    listing_date_invalid: 0, listing_date_pct: 0, latest_bar_date: '2026-10-23', expected_latest_date: '2026-10-23',
    calendar_status: 'verified', freshness: { current: total, earlier: 0, unverified: 0, no_bars: 0 }, future_bar_dates: 0, ...changes };
}
function fixture() {
  const quality: UniverseQuality = { as_of: '2026-10-25T18:00:00+08:00', timezone: 'Asia/Shanghai', basis: 'stored_bars_by_symbol',
    scopes: { 'cn-etf': group(1642), 'cn-stock': group(5555, { with_bars: 5554, without_bars: 1, coverage_pct: 100,
      freshness: { current: 5554, earlier: 0, unverified: 0, no_bars: 1 } }),
      'us-etf': group(75, { expected_latest_date: null, calendar_status: 'unverified', freshness: { current: 0, earlier: 0, unverified: 75, no_bars: 0 } }) },
    with_bars: 7271, without_bars: 1, cache_mismatches: 0, orphan_bar_rows: 0,
    missing_bar_samples: [{ id: 9, symbol: '301999', name: '待核验样本', scope: 'cn-stock' }], cache_mismatch_samples: [],
    historical_calendar_coverage: 'not_verified', price_quantity_basis: 'not_verified' };
  return { total_symbols: 7272, synced_symbols: 7271, failed_symbols: 41, total_bars: 11734896,
    by_type: { etf: { total: 1717, synced: 1717 } }, by_board: { gem: { total: 1403, synced: 1402, coverage: 100 } },
    latest_synced_at: '2026-10-25T03:31:32', quality };
}

describe('market-data health dimensions', () => {
  it.each([[5554,5555,99.98],[1402,1403,99.93],[999999,1000000,99.99],[1717,1717,100],[0,0,null]])(
    'keeps incomplete coverage distinct from complete coverage: %i/%i', (n,total,expected) => {
      expect(preciseCoverage(n!,total!)).toBe(expected);
    });
  it('shows 1717 ETF record coverage alongside missing listing metadata and unverified history', () => {
    render(<UniverseHealthDashboard stats={fixture()} />);
    const etf = within(screen.getByRole('region', { name: 'A股ETF数据状态' }));
    expect(etf.getByText('100%')).toBeVisible();
    expect(etf.getByText(/上市日期已登记：0 \/ 1,642/)).toBeVisible();
    expect(screen.getByText(/ETF 合计 1,717 只/)).toHaveTextContent('A股ETF 1,642 只 + 美股ETF 75 只');
    expect(screen.getByText('历史交易日覆盖：未核验')).toBeVisible();
    expect(screen.getByText('价格与量额口径：未核验')).toBeVisible();
    expect(screen.getByText(/有行情.*仅表示至少存有一条日线/)).toBeVisible();
  });
  it('does not trust rounded backend percentages when a stock is still missing', () => {
    render(<UniverseHealthDashboard stats={fixture()} />);
    const stock = within(screen.getByRole('region', { name: 'A股股票数据状态' }));
    expect(stock.getByText('99.98%')).toBeVisible();
    expect(stock.queryByText('100%')).not.toBeInTheDocument();
    expect(stock.getByText(/有行情：/)).toHaveTextContent('未取得 1');
    fireEvent.click(screen.getByText(/查看未取得行情的标的/));
    expect(screen.getByText(/301999 待核验样本/)).toBeVisible();
  });
  it('exposes a synced ETF with no bars in both its market and the combined ETF total', () => {
    const stats = fixture();
    Object.assign(stats.quality.scopes['us-etf'], { with_bars: 74, without_bars: 1 });
    render(<UniverseHealthDashboard stats={stats} />);
    expect(within(screen.getByRole('region', { name: '美股ETF数据状态' })).getByText('98.67%')).toBeVisible();
    expect(screen.getByText(/ETF 合计 1,717 只/)).toHaveTextContent('实际有行情 1,716 / 1,717，历史同步标记 1,717');
  });
  it('uses the completed trading session on a weekend and keeps US dates unverified', () => {
    render(<UniverseHealthDashboard stats={fixture()} />);
    expect(within(screen.getByRole('region', { name: 'A股ETF数据状态' })).getByText(/最近完整交易日 2026-10-23/)).toBeVisible();
    expect(within(screen.getByRole('region', { name: '美股ETF数据状态' })).getByText(/尚未核验该市场交易日历/)).toBeVisible();
    expect(screen.queryByText('今日最新')).not.toBeInTheDocument();
  });
  it('keeps board counts below 100 percent even when the old response was rounded', () => {
    render(<UniverseHealthDashboard stats={fixture()} />);
    fireEvent.click(screen.getByRole('button', { name: /查看行情统计明细/ }));
    expect(screen.getByText('99.93%')).toBeVisible();
    expect(screen.getByText(/日期较早的记录还需结合停牌、退市等状态确认/)).toBeVisible();
  });
  it('shows cache differences as unresolved evidence without changing their values', () => {
    const stats = fixture(); stats.quality.cache_mismatches = 2; stats.quality.scopes['cn-etf'].cache_mismatches = 2;
    const before = JSON.stringify(stats);
    render(<UniverseHealthDashboard stats={stats} />);
    expect(screen.getByText(/登记缓存差异 2 个，统计使用实际记录/)).toBeVisible();
    expect(JSON.stringify(stats)).toBe(before);
  });
  it('labels older server data as unverified rather than assuming missing metrics are zero', () => {
    const { quality: _quality, ...old } = fixture();
    render(<UniverseHealthDashboard stats={old} />);
    expect(screen.getByText('详细核验结果暂未提供')).toBeVisible();
    expect(screen.queryByText('上市日期已登记：0 / 1,642')).not.toBeInTheDocument();
  });
});
