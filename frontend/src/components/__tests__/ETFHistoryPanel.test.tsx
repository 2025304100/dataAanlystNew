import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import ETFHistoryPanel from '../ETFHistoryPanel';

const scope = (id = 1) => ({ portfolio_id: id, portfolio_name: id === 1 ? 'main' : '备用组合',
  scope_sha256: String(id).repeat(64), symbols: [{ symbol_id: id, symbol: id === 1 ? '510300' : '159915', name: '组合ETF',
    first_date: '2024-01-02', last_date: '2024-01-04', row_count: 2, missing_trade_dates: ['2024-01-03'], unreviewed_dates: [], dataset_id: 'reviewed' }],
  start_date: '2024-01-02', end_date: '2024-01-04', can_prepare: true, max_days: 3660, reasons: [], errors: [],
  latest_task: null, minimum_symbols: 20, training_message: '当前组合有1只ETF，现有模型需要至少20只ETF。' });
const task = (status = 'queued', id = 'task-1') => ({ id, status, percent: 50, message: '正在下载ETF历史数据（2/4）' });
const report = (done = false) => ({ portfolio_id: 1, start_date: '2024-01-02', end_date: '2024-01-04', imported: done,
  errors: [], backtest_ready: false, symbols: [{ symbol: '510300', name: '组合ETF', row_count: done ? 3 : 2,
    missing_trade_dates: done ? [] : ['2024-01-03'], error: done ? null : '缺少1个交易日的原始价或前复权价，请重试补充。' }] });
let requests: Array<{ url: string; method: string; body?: any }>;
let handle: (url: string, method: string, body?: any) => Promise<any>;
beforeEach(() => {
  requests = [];
  localStorage.clear();
  handle = async url => url.includes('/scope') ? scope() : { task: task('partial'), report: report() };
  vi.stubGlobal('fetch', vi.fn(async (url: string, options: any = {}) => {
    const method = options.method || 'GET', body = options.body ? JSON.parse(options.body) : undefined;
    requests.push({ url: String(url), method, body });
    const value = await handle(String(url), method, body);
    return { ok: true, json: async () => value };
  }));
});
afterEach(() => { cleanup(); localStorage.clear(); vi.unstubAllGlobals(); });

it('hides the previous successful ETF result after changing dates without starting another download', async () => {
  handle = async url => url.includes('/scope') ? { ...scope(), latest_task: task('done') }
    : { task: task('done'), report: report(true) };
  render(<ETFHistoryPanel portfolioId={1} defaultOpen />);
  await screen.findByText('本次行情已补齐并核对。');
  fireEvent.click(screen.getByText('调整补充日期'));
  fireEvent.change(screen.getByLabelText('ETF历史结束日期'), { target: { value: '2024-01-05' } });
  expect(screen.queryByText('本次行情已补齐并核对。')).not.toBeInTheDocument();
  expect(screen.queryByText(/本次补充范围/)).not.toBeInTheDocument();
  expect(screen.queryByRole('table')).not.toBeInTheDocument();
  expect(screen.getByText('日期已修改，请按新日期补充历史数据。')).toBeVisible();
  expect(screen.getByRole('button', { name: '补充ETF历史数据' })).toBeEnabled();
  expect(requests.every(row => row.method === 'GET')).toBe(true);
});

it('shows missing dates and a failed supplement, then retries the same dates without starting training', async () => {
  let starts = 0;
  handle = async (url, method) => {
    if (url.includes('/scope')) return scope();
    if (method === 'POST') return { task: task('queued', `task-${++starts}`), report: null };
    return { task: task(starts === 1 ? 'partial' : 'done', `task-${starts}`), report: report(starts > 1) };
  };
  render(<ETFHistoryPanel portfolioId={1} defaultOpen />);
  expect(await screen.findByText(/当前组合有1只ETF.*20只ETF/)).toBeVisible();
  expect(screen.getByText('2024-01-03')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '补充ETF历史数据' }));
  expect(await screen.findByText(/缺少1个交易日的原始价/)).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '重试补充' }));
  expect(await screen.findByText('本次行情已补齐并核对。')).toBeVisible();
  const posts = requests.filter(row => row.method === 'POST');
  expect(posts).toHaveLength(2);
  expect(posts[0].body).toMatchObject({ portfolio_id: 1, start_date: '2024-01-02', end_date: '2024-01-04' });
  expect(posts[1].body).toMatchObject({ portfolio_id: 1, start_date: '2024-01-02', end_date: '2024-01-04' });
  expect(posts[1].body.request_id).not.toBe(posts[0].body.request_id);
  expect(requests.some(row => row.url.includes('/scoring/tasks'))).toBe(false);
  expect(screen.getByText(/交易资料不完整时，成交回测仍不能运行/)).toBeVisible();
});

it('reopens an unfinished earlier range and retries those dates instead of silently using the new default', async () => {
  handle = async (url, method) => {
    if (url.includes('/scope')) return { ...scope(), start_date: '2021-07-12', end_date: '2026-10-20', latest_task: task('partial') };
    return method === 'POST' ? { task: task('running'), report: null } : { task: task('partial'), report: report() };
  };
  render(<ETFHistoryPanel portfolioId={1} defaultOpen />);
  fireEvent.click(await screen.findByRole('button', { name: '重试补充' }));
  await waitFor(() => expect(requests.filter(r => r.method === 'POST')).toHaveLength(1));
  expect(requests.find(r => r.method === 'POST')?.body).toMatchObject({ start_date: '2024-01-02', end_date: '2024-01-04' });
});

it('recovers a lost submission after re-entry without starting a second task', async () => {
  handle = async (url, method) => {
    if (method === 'POST') throw new TypeError('response lost');
    return url.includes('/scope') ? scope() : { task: task('done'), report: report(true) };
  };
  const first = render(<ETFHistoryPanel portfolioId={1} defaultOpen />);
  fireEvent.click(await screen.findByRole('button', { name: '补充ETF历史数据' }));
  await screen.findByText('尚未确认补充是否开始，请先重试读取进度。');
  first.unmount();
  render(<ETFHistoryPanel portfolioId={1} defaultOpen />);
  expect(await screen.findByText('本次行情已补齐并核对。')).toBeVisible();
  expect(requests.filter(row => row.method === 'POST')).toHaveLength(1);
  expect(requests.some(row => row.url.includes('/preparations/requests/'))).toBe(true);
});

it('keeps the current task after a progress read fails and resumes reading without resubmission', async () => {
  let reads = 0;
  handle = async url => {
    if (url.includes('/scope')) return { ...scope(), latest_task: task('running') };
    if (++reads === 1) return { task: task('running'), report: null };
    if (reads === 2) throw new TypeError('read disconnected');
    return { task: task('done'), report: report(true) };
  };
  render(<ETFHistoryPanel portfolioId={1} defaultOpen />);
  await screen.findByText('补充进度读取失败，任务可能仍在运行。请重试读取进度。');
  expect(screen.getByRole('button', { name: '补充ETF历史数据' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: '重试读取进度' }));
  expect(await screen.findByText('本次行情已补齐并核对。')).toBeVisible();
  expect(requests.filter(row => row.method === 'POST')).toHaveLength(0);
});

it('discards a late result after switching portfolios', async () => {
  let resolveOld: (value: any) => void = () => {};
  handle = async url => {
    if (url.includes('/scope')) return url.includes('portfolio_id=2') ? scope(2) : { ...scope(), latest_task: task('running') };
    return new Promise(resolve => { resolveOld = resolve; });
  };
  const view = render(<ETFHistoryPanel portfolioId={1} defaultOpen />);
  await waitFor(() => expect(requests.some(row => row.url.includes('/preparations/task-1'))).toBe(true));
  view.rerender(<ETFHistoryPanel portfolioId={2} />);
  await screen.findByText(/159915/);
  await act(async () => resolveOld({ task: task('done'), report: report(true) }));
  expect(screen.queryByText(/510300/)).not.toBeInTheDocument();
  expect(screen.queryByText('本次行情已补齐并核对。')).not.toBeInTheDocument();
});

it('treats a submission timeout as unconfirmed and preserves the request for recovery', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input, init) => {
    if (init?.method === 'POST') return new Response(JSON.stringify({ detail: '等待超时' }), { status: 408 });
    return new Response(JSON.stringify(scope()), { status: 200 });
  }));
  render(<ETFHistoryPanel portfolioId={1} defaultOpen />);
  const submit = await screen.findByRole('button', { name: '补充ETF历史数据' });
  await waitFor(() => expect(submit).toBeEnabled());
  fireEvent.click(submit);
  expect(await screen.findByText('尚未确认补充是否开始，请先重试读取进度。')).toBeVisible();
  expect(localStorage.getItem('etf-history-request:1')).not.toBeNull();
  expect(submit).toBeDisabled();
});

it('starts collapsed and expands on demand', async () => {
  render(<ETFHistoryPanel portfolioId={1} />);
  expect(screen.queryByRole('button', { name: '补充ETF历史数据' })).not.toBeInTheDocument();
  expect(screen.getByText(/行情齐全不代表可成交/)).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '展开' }));
  expect(await screen.findByRole('button', { name: '补充ETF历史数据' })).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '收起' }));
  expect(screen.queryByRole('button', { name: '补充ETF历史数据' })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '展开' })).toBeVisible();
});
