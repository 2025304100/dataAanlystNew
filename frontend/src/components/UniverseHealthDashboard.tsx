import { Alert, Col, Collapse, Progress, Row, Space, Statistic, Tag } from 'antd';
import { t } from '../i18n';

export interface UniverseQualityScope {
  total: number; with_bars: number; without_bars: number; coverage_pct: number;
  sync_flag_count: number; sync_flag_without_bars: number; cache_mismatches: number;
  listing_date_recorded: number; listing_date_missing: number; listing_date_invalid: number;
  listing_date_pct: number; latest_bar_date: string | null; expected_latest_date: string | null;
  calendar_status: string; freshness: { current: number; earlier: number; unverified: number; no_bars: number };
  future_bar_dates: number;
}
export interface UniverseQuality {
  as_of: string; timezone: string; basis: string; scopes: Record<string, UniverseQualityScope>;
  with_bars: number; without_bars: number; cache_mismatches: number; orphan_bar_rows: number;
  missing_bar_samples: Array<{ id: number; symbol: string; name: string | null; scope: string }>;
  cache_mismatch_samples: Array<{ symbol: string; scope: string; cached_count: number; actual_count: number; cached_latest: string | null; actual_latest: string | null }>;
  historical_calendar_coverage: string; price_quantity_basis: string;
}
interface Group { total: number; synced: number; coverage?: number }
interface Stats {
  total_symbols: number; synced_symbols: number; failed_symbols: number; total_bars: number;
  by_type: Record<string, Group>; by_scope?: Record<string, Group>; by_board?: Record<string, Group>;
  freshness?: Record<string, number>; latest_synced_at: string | null; quality?: UniverseQuality;
}
const scopeNames: Record<string, string> = { 'cn-etf': 'A股ETF', 'cn-stock': 'A股股票', 'us-etf': '美股ETF', 'us-stock': '美股股票' };
const boardNames: Record<string, string> = { bj: '北交所', gem: '创业板', main: '主板', star: '科创板' };
const count = (n: number) => n.toLocaleString('zh-CN');
export function preciseCoverage(n: number, total: number): number | null {
  if (total <= 0) return null;
  return n === total ? 100 : Math.min(99.99, Math.max(0, Math.round(n * 10000 / total) / 100));
}
function Coverage({ n, total }: { n: number; total: number }) {
  const percent = preciseCoverage(n, total);
  return <Progress percent={percent ?? 0} size="small" status={total > 0 && n === total ? 'success' : 'normal'}
    strokeColor={n < total ? '#d48806' : undefined} format={() => percent === null ? '—' : `${percent}%`} />;
}
function FlagDetails({ stats }: { stats: Stats }) {
  return <>
    <p>以下按历史同步标记统计，不代表行情日期或基础资料齐全。</p>
    <Row gutter={[12, 12]}>{Object.entries(stats.by_board || {}).map(([key, item]) => <Col key={key} xs={24} sm={12} xl={6}>
      <div>{boardNames[key] || key}：{count(item.synced)} / {count(item.total)}（缺 {count(Math.max(0, item.total - item.synced))}）</div>
      <Coverage n={item.synced} total={item.total} />
    </Col>)}</Row>
  </>;
}
export default function UniverseHealthDashboard({ stats }: { stats: Stats }) {
  const quality = stats.quality;
  if (!quality) return <>
    <Alert showIcon type="warning" message="详细核验结果暂未提供"
      description="下方仅显示同步标记，无法据此确认行情记录、上市日期或完整历史。请刷新读取后再确认资料状态。" style={{ marginBottom: 12 }} />
    <Row gutter={16}>
      <Col span={6}><Statistic title={t('universeTotalSymbols')} value={stats.total_symbols} /></Col>
      <Col span={6}><Statistic title={t('universeSyncedSymbols')} value={stats.synced_symbols} /></Col>
      <Col span={6}><Statistic title={t('universeFailedSymbols')} value={stats.failed_symbols} /></Col>
      <Col span={6}><Statistic title={t('universeTotalBars')} value={stats.total_bars} /></Col>
    </Row>
    <Collapse size="small" style={{ marginTop: 12 }} items={[{ key: 'statistics', label: '查看行情统计明细', children: <>
      <p><span>{t('universeHealthByScope')}</span>（同步标记）</p>
      {Object.entries(stats.by_scope || {}).map(([key, item]) => <div key={key}>
        {scopeNames[key] || key}：{count(item.synced)} / {count(item.total)}（缺 {count(Math.max(0, item.total - item.synced))}）
        <Coverage n={item.synced} total={item.total} />
      </div>)}
      {stats.freshness && <><p><span>{t('universeHealthFreshness')}</span>（自然日，仅供参考）</p>
        <Space wrap>{Object.entries(stats.freshness).map(([key, n]) => <span key={key}>{({ today: '今天', '1_3_days': '1～3天', '3_7_days': '3～7天', over_7_days: '7天以上', no_data: '日期未知' } as Record<string, string>)[key] || '其他'}：{count(n)}</span>)}</Space></>}
      <p>{t('universeHealthByBoard')}</p><FlagDetails stats={stats} />
      {Object.entries(stats.by_type).map(([key, item]) => <p key={key}><span>{t(key === 'etf' ? 'universeTypeEtf' : 'universeTypeStock')}</span>：{count(item.synced)} / {count(item.total)}（同步标记）</p>)}
    </> }]} />
    {stats.latest_synced_at && <p style={{ marginTop: 12 }}>{t('universeLatestSync')}：{new Date(stats.latest_synced_at).toLocaleString()}</p>}
  </>;
  const groups = Object.entries(quality.scopes);
  const listingMissing = groups.reduce((n, [, group]) => n + group.listing_date_missing + group.listing_date_invalid, 0);
  const china = groups.filter(([key]) => key.startsWith('cn-'));
  const expected = china.find(([, group]) => group.expected_latest_date)?.[1].expected_latest_date;
  const cnFresh = china.reduce((sum, [, group]) => ({
    current: sum.current + group.freshness.current, earlier: sum.earlier + group.freshness.earlier,
    unverified: sum.unverified + group.freshness.unverified, no_bars: sum.no_bars + group.freshness.no_bars,
  }), { current: 0, earlier: 0, unverified: 0, no_bars: 0 });
  const etfGroups = groups.filter(([key]) => key.endsWith('-etf'));
  return <>
    <Alert showIcon type={listingMissing ? 'warning' : 'info'} message="行情记录覆盖与资料完整性分别检查"
      description={`“有行情”仅表示至少存有一条日线，不代表历史交易日齐全或可用于回测。上市日期有 ${count(listingMissing)} 个标的缺失或待核验；价格与量额口径、历史交易日覆盖仍需单独检查。`}
      style={{ marginBottom: 14 }} />
    <Row gutter={[16, 12]}>
      <Col xs={12} md={6}><Statistic title="已登记标的" value={stats.total_symbols} /></Col>
      <Col xs={12} md={6}><Statistic title="有行情记录的标的" value={quality.with_bars} /></Col>
      <Col xs={12} md={6}><Statistic title="未取得行情的标的" value={quality.without_bars} valueStyle={{ color: quality.without_bars ? '#cf1322' : undefined }} /></Col>
      <Col xs={12} md={6}><Statistic title="已存日线记录" value={stats.total_bars} /></Col>
    </Row>
    <p style={{ color: 'var(--text-muted, #666)', margin: '12px 0 8px' }}>按市场查看 · 记录覆盖率按实际行情记录计算，分母为已登记标的。</p>
    <Row gutter={[12, 12]}>{groups.map(([key, group]) => <Col key={key} xs={24} md={12} xl={8}>
      <section aria-label={`${scopeNames[key] || key}数据状态`} style={{ height: '100%', padding: 14, border: '1px solid var(--border-color, #e5e7eb)', borderRadius: 8, background: 'var(--bg-elevated, #fafafa)' }}>
        <strong>{scopeNames[key] || key}</strong>
        <div style={{ marginTop: 8 }}>有行情：<strong>{count(group.with_bars)} / {count(group.total)}</strong> · 未取得 {count(group.without_bars)}</div>
        <Coverage n={group.with_bars} total={group.total} />
        <div style={{ color: group.listing_date_recorded < group.total ? '#ad6800' : undefined }}>
          上市日期已登记：{count(group.listing_date_recorded)} / {count(group.total)}
          <span> · 缺失 {count(group.listing_date_missing)}{group.listing_date_invalid ? ` · 日期异常 ${count(group.listing_date_invalid)}` : ''}</span>
        </div>
        <div style={{ marginTop: 6, color: 'var(--text-secondary, #666)' }}>本范围最新记录：{group.latest_bar_date || '尚无记录'}</div>
        {group.calendar_status === 'verified' ? <div style={{ marginTop: 6 }}>
          最近完整交易日 {group.expected_latest_date}<br />
          已到该日 {count(group.freshness.current)} · 日期较早 {count(group.freshness.earlier)} · 待核验 {count(group.freshness.unverified)}
        </div> : <div style={{ marginTop: 6, color: '#ad6800' }}>本看板尚未核验该市场交易日历，暂不判定是否最新。</div>}
        {group.cache_mismatches > 0 && <div style={{ marginTop: 6, color: '#cf1322' }}>登记缓存差异 {count(group.cache_mismatches)} 个，统计使用实际记录。</div>}
      </section>
    </Col>)}</Row>
    {etfGroups.length > 0 && <p style={{ marginTop: 12 }}>ETF 合计 {count(etfGroups.reduce((n, [, group]) => n + group.total, 0))} 只：
      {etfGroups.map(([key, group]) => `${scopeNames[key] || key} ${count(group.total)} 只`).join(' + ')}。
      实际有行情 {count(etfGroups.reduce((n, [, group]) => n + group.with_bars, 0))} / {count(etfGroups.reduce((n, [, group]) => n + group.total, 0))}，历史同步标记 {count(etfGroups.reduce((n, [, group]) => n + group.sync_flag_count, 0))}。这个合计不表示全部基础资料齐全。</p>}
    {quality.missing_bar_samples.length > 0 && <details style={{ margin: '12px 0' }}><summary>查看未取得行情的标的（最多展示 20 只）</summary>
      <ul>{quality.missing_bar_samples.map(item => <li key={item.id}>{item.symbol} {item.name || ''} · {scopeNames[item.scope] || item.scope}</li>)}</ul>
    </details>}
    {(quality.cache_mismatches > 0 || quality.orphan_bar_rows > 0) && <Alert showIcon type="warning" message="还有需要核对的记录"
      description={`登记缓存差异 ${count(quality.cache_mismatches)} 个；无法关联已登记标的的行情 ${count(quality.orphan_bar_rows)} 行。此次读取保留全部原记录。`} />}
    <Collapse size="small" style={{ marginTop: 12 }} items={[{ key: 'statistics', label: '查看行情统计明细', children: <>
      <p>境内行情日期核验 · 最近完整交易日：{expected || '本地交易日历不足，无法确认'}</p>
      <Space wrap size={16}>
        <Statistic title="已到最近完整交易日" value={cnFresh.current} />
        <Statistic title="行情日期较早" value={cnFresh.earlier} />
        <Statistic title="日期待核验" value={cnFresh.unverified} />
        <Statistic title="未取得行情" value={cnFresh.no_bars} />
      </Space>
      <p>按完整交易日核验，周末和休市日不会仅因“今天没有日线”判为落后。日期较早的记录还需结合停牌、退市等状态确认，不直接视为应补缺口。</p>
      <p>按板块查看同步标记</p><FlagDetails stats={stats} />
      <p>同步失败达到阈值：{count(stats.failed_symbols)} 个标的。到“数据检查”查看其他关键字段和历史资料。</p>
    </> }]} />
    <Space wrap style={{ marginTop: 12 }}><Tag>历史交易日覆盖：未核验</Tag><Tag>价格与量额口径：未核验</Tag></Space>
    <p style={{ margin: '8px 0 0', color: 'var(--text-muted, #888)', fontSize: 12 }}>统计读取时间：{new Date(quality.as_of).toLocaleString('zh-CN', { timeZone: quality.timezone })}（上海时间）
      {stats.latest_synced_at && <> · 最近同步操作时间：{new Date(stats.latest_synced_at).toLocaleString()}</>}</p>
  </>;
}
