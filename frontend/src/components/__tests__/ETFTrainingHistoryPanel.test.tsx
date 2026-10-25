import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import ETFTrainingHistoryPanel from '../ETFTrainingHistoryPanel';

const root = '/etf-history/training-list';
const codes = Array.from({ length: 20 }, (_, i) => String(510300 + i));
const scope = (symbols: string[] = []) => ({ name: 'ETF训练清单', symbols: symbols.map(symbol => ({ symbol, name: symbol,
  provider_code: `sh.${symbol}`, dataset_id: null, row_count: 0, first_date: null, last_date: null,
  missing_trade_dates: null, reason: '还没有已核对的历史资料，请准备历史数据。' })),
  list_sha256: (symbols.length ? 'b' : 'a').repeat(64), scope_sha256: 'c'.repeat(64), symbol_count: symbols.length,
  minimum_symbols: 20, max_symbols: 50, start_date: '2024-01-02', end_date: '2024-01-04', max_days: 3660,
  can_prepare: symbols.length > 0, reasons: symbols.length ? [] : ['先填写ETF代码并保存清单，再准备历史数据。'],
  errors: [], latest_task: null, common_trade_date_count: null, expected_trade_date_count: null,
  common_start_date: null, common_end_date: null, training_ready: false,
  training_message: `清单有${symbols.length}只ETF，训练至少需要20只。共同交易日期尚未核实。`, backtest_ready: false });
let calls: Array<{ url: string; method: string; body?: any }>;
let handle: (url: string, method: string, body?: any) => Promise<any>;
beforeEach(() => {
  localStorage.clear(); calls = [];
  handle = async () => scope();
  vi.stubGlobal('fetch', vi.fn(async (input, options: any = {}) => {
    const url = String(input), method = options.method || 'GET', body = options.body ? JSON.parse(options.body) : undefined;
    calls.push({ url, method, body });
    const value = await handle(url, method, body);
    return value instanceof Response ? value : new Response(JSON.stringify(value), { status: 200 });
  }));
});
afterEach(() => { cleanup(); localStorage.clear(); vi.unstubAllGlobals(); });

it('starts with an empty independent list and saves twenty pasted codes without a portfolio request', async () => {
  let saved = false;
  handle = async (_url, method, body) => {
    if (method === 'PUT') { expect(body.expected_list_sha256).toBe('a'.repeat(64)); saved = true; }
    return scope(saved ? codes : []);
  };
  render(<ETFTrainingHistoryPanel defaultOpen />);
  const input = await screen.findByRole('textbox', { name: 'ETF训练代码' });
  expect(input).toHaveValue('');
  expect(screen.getByRole('button', { name: '准备历史数据' })).toBeDisabled();
  fireEvent.change(input, { target: { value: codes.join('，') } });
  fireEvent.click(screen.getByRole('button', { name: '保存清单并检查' }));
  expect(await screen.findByText(/清单有20只ETF/)).toBeVisible();
  await waitFor(() => expect(screen.getByRole('button', { name: '准备历史数据' })).toBeEnabled());
  expect(calls.filter(call => call.method === 'PUT')).toHaveLength(1);
  expect(calls.every(call => call.url.includes(root))).toBe(true);
  expect(calls.some(call => call.method === 'POST')).toBe(false);
  expect(screen.getByText(/共同覆盖日期暂不能确认/)).toBeVisible();
});

it('keeps a successful save when the following date check fails and retries only the check', async () => {
  let saved = false, failCheck = true;
  handle = async (_url, method) => {
    if (method === 'PUT') { saved = true; return scope(codes); }
    if (saved && failCheck) throw new TypeError('check offline');
    return scope(saved ? codes : []);
  };
  render(<ETFTrainingHistoryPanel defaultOpen />);
  const input = await screen.findByRole('textbox', { name: 'ETF训练代码' });
  await waitFor(() => expect(input).toBeEnabled());
  fireEvent.change(input, { target: { value: codes.join('，') } });
  fireEvent.click(screen.getByRole('button', { name: '保存清单并检查' }));
  expect(await screen.findByText('清单已保存，日期检查未完成，请重试检查。')).toBeVisible();
  expect(input).toHaveValue(codes.join('\n'));
  expect(screen.getByRole('button', { name: '保存清单并检查' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '准备历史数据' })).toBeDisabled();
  failCheck = false;
  fireEvent.click(screen.getByRole('button', { name: '重试检查' }));
  await waitFor(() => expect(screen.getByRole('button', { name: '准备历史数据' })).toBeEnabled());
  expect(screen.getByText(/清单有20只ETF/)).toBeVisible();
  expect(calls.filter(call => call.method === 'PUT')).toHaveLength(1);
  expect(calls.some(call => call.method === 'POST')).toBe(false);
});

it('restores unsubmitted codes and dates after leaving the page without saving or downloading', async () => {
  const view = render(<ETFTrainingHistoryPanel defaultOpen />);
  const input = await screen.findByRole('textbox', { name: 'ETF训练代码' });
  await waitFor(() => expect(input).toBeEnabled());
  fireEvent.change(input, { target: { value: codes.join('，') } });
  fireEvent.click(screen.getByText('调整准备日期'));
  fireEvent.change(screen.getByLabelText('训练历史开始日期'), { target: { value: '2023-01-03' } });
  fireEvent.change(screen.getByLabelText('训练历史结束日期'), { target: { value: '2024-01-05' } });
  view.unmount();
  render(<ETFTrainingHistoryPanel defaultOpen />);
  const restored = await screen.findByRole('textbox', { name: 'ETF训练代码' });
  await waitFor(() => expect(restored).toHaveValue(codes.join('，')));
  expect(screen.getByLabelText('训练历史开始日期')).toHaveValue('2023-01-03');
  expect(screen.getByLabelText('训练历史结束日期')).toHaveValue('2024-01-05');
  expect(screen.getByText('代码已修改，请先保存清单。')).toBeVisible();
  expect(screen.getByRole('button', { name: '准备历史数据' })).toBeDisabled();
  expect(calls.every(call => call.method === 'GET')).toBe(true);
});

const task = (status = 'queued') => ({ id: 'task-20', status, percent: 60, message: '正在下载ETF历史数据（40/61）',
  payload: { scope_kind: 'training_list', list_sha256: 'b'.repeat(64) } });
const report = (done = false) => ({ scope_kind: 'training_list', list_sha256: 'b'.repeat(64),
  start_date: '2024-01-02', end_date: '2024-01-04', prepared: done, imported: false, errors: [], backtest_ready: false,
  expected_trade_date_count: 3, common_trade_date_count: done ? 3 : 2,
  common_start_date: '2024-01-02', common_end_date: '2024-01-04',
  symbols: codes.map((symbol, index) => ({ symbol, name: symbol, row_count: done || index ? 3 : 2,
    missing_trade_dates: done || index ? [] : ['2024-01-03'], error: done || index ? null : '缺少1个交易日的原始价或前复权价，请重试准备。' })) });

it('summarizes a partial batch and puts missing dates first without treating the shared failure as twenty missing ETFs', async () => {
  const sharedError = '整份资料尚未核对通过，请先补齐缺失日期。';
  const partial = { ...report(true), prepared: false, common_trade_date_count: 2, errors: [sharedError],
    symbols: codes.map((symbol, index) => ({ symbol, name: symbol, row_count: index === 19 ? 2 : 3,
      missing_trade_dates: index === 19 ? ['2024-01-03'] : [],
      error: index === 19 ? '缺少1个交易日的原始价或前复权价，请重试准备。' : sharedError })) };
  handle = async url => url.includes('/scope') ? { ...scope(codes), latest_task: task('partial') }
    : { task: task('partial'), report: partial };
  render(<ETFTrainingHistoryPanel defaultOpen />);
  expect(await screen.findByRole('status', { name: 'ETF准备结果汇总' })).toHaveTextContent('本次共20只ETF，19只行情日期齐全，1只日期需补齐或核实。');
  const rows = screen.getAllByRole('row').slice(1);
  expect(rows.map(row => within(row).getAllByRole('cell')[0].textContent)).toEqual([codes[19], ...codes.slice(0, 19)]);
  expect(rows[0]).toHaveTextContent('2024-01-03');
  expect(rows[0]).toHaveTextContent('缺少1个交易日的原始价或前复权价');
  expect(rows[1]).toHaveTextContent('行情日期齐全，整份资料仍需核对。');
  expect(screen.getByRole('button', { name: '重试准备' })).toBeEnabled();
  expect(screen.queryByText('这份清单的历史数据已准备并核对。')).not.toBeInTheDocument();
  expect(calls.every(call => call.method === 'GET')).toBe(true);
});

it('keeps complete dates separate from the failed validation and preserves an ETF specific reason', async () => {
  const sharedError = '分红复权记录未核对通过，请先核对整份资料。';
  const failed = { ...report(true), prepared: false, errors: [sharedError],
    symbols: report(true).symbols.map((row, index) => ({ ...row,
      error: index === 19 ? '原始价和前复权价未能对应，请核对来源。' : sharedError })) };
  handle = async url => url.includes('/scope') ? { ...scope(codes), latest_task: task('failed') }
    : { task: task('failed'), report: failed };
  render(<ETFTrainingHistoryPanel defaultOpen />);
  expect(await screen.findByRole('status', { name: 'ETF准备结果汇总' })).toHaveTextContent('20只行情日期齐全；整份资料仍未核对通过。');
  expect(screen.getByText(sharedError)).toBeVisible();
  const firstRow = screen.getAllByRole('row')[1];
  expect(firstRow).toHaveTextContent(codes[19]);
  expect(firstRow).toHaveTextContent('原始价和前复权价未能对应，请核对来源。');
  expect(screen.queryByText('这份清单的历史数据已准备并核对。')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '查看训练条件' })).not.toBeInTheDocument();
  expect(calls.every(call => call.method === 'GET')).toBe(true);
});

it.each([null, 0])('does not infer complete dates from downloaded rows when the expected calendar is %s', async expected => {
  const unconfirmed = { ...report(true), prepared: false, expected_trade_date_count: expected,
    common_trade_date_count: null, errors: ['交易日期还没有核实。'] };
  handle = async url => url.includes('/scope') ? { ...scope(codes), latest_task: task('partial') }
    : { task: task('partial'), report: unconfirmed };
  render(<ETFTrainingHistoryPanel defaultOpen />);
  expect(await screen.findByRole('status', { name: 'ETF准备结果汇总' })).toHaveTextContent('0只行情日期齐全，20只日期需补齐或核实。');
  expect(screen.queryByText('所查范围没有缺失日期')).not.toBeInTheDocument();
  expect(screen.queryByText('所查日期已核对')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '重试准备' })).toBeEnabled();
  expect(calls.every(call => call.method === 'GET')).toBe(true);
});

it('hides the previous batch summary when the ETF codes have not been saved', async () => {
  handle = async url => url.includes('/scope') ? { ...scope(codes), latest_task: task('done') }
    : { task: task('done'), report: report(true) };
  render(<ETFTrainingHistoryPanel defaultOpen />);
  expect(await screen.findByRole('status', { name: 'ETF准备结果汇总' })).toHaveTextContent('20只行情日期齐全。');
  expect(screen.getByText('这份清单的历史数据已准备并核对。')).toBeVisible();
  fireEvent.change(screen.getByRole('textbox', { name: 'ETF训练代码' }), { target: { value: codes.slice(1).join('\n') } });
  expect(screen.queryByRole('status', { name: 'ETF准备结果汇总' })).not.toBeInTheDocument();
  expect(screen.queryByText('这份清单的历史数据已准备并核对。')).not.toBeInTheDocument();
  expect(screen.queryByText(/共同覆盖3个交易日/)).not.toBeInTheDocument();
  expect(screen.queryByRole('table')).not.toBeInTheDocument();
  expect(screen.getByText('代码已修改，请先保存清单。')).toBeVisible();
  expect(screen.getByRole('button', { name: '准备历史数据' })).toBeDisabled();
  expect(calls.every(call => call.method === 'GET')).toBe(true);
});

it.each(['codes', 'dates'])('opens and focuses the requested %s input after the history page has loaded', async target => {
  localStorage.setItem('etf-training-history:focus', target);
  render(<ETFTrainingHistoryPanel defaultOpen />);
  await waitFor(() => expect(target === 'codes' ? screen.getByRole('textbox', { name: 'ETF训练代码' })
    : screen.getByLabelText('训练历史结束日期')).toHaveFocus());
  if (target === 'dates') expect(screen.getByText('调整准备日期').closest('details')).toHaveAttribute('open');
  expect(localStorage.getItem('etf-training-history:focus')).toBeNull();
  expect(calls.every(call => call.method === 'GET')).toBe(true);
});

it('focuses the date section on an already mounted page without refreshing its input', async () => {
  render(<ETFTrainingHistoryPanel defaultOpen />);
  const input = await screen.findByRole('textbox', { name: 'ETF训练代码' });
  await waitFor(() => expect(input).toBeEnabled());
  fireEvent.change(input, { target: { value: '510300，159915' } });
  fireEvent(window, new CustomEvent('etf-training-history:focus', { detail: 'dates' }));
  await waitFor(() => expect(screen.getByLabelText('训练历史结束日期')).toHaveFocus());
  expect(input).toHaveValue('510300，159915');
  expect(calls).toHaveLength(1);
  expect(calls[0].method).toBe('GET');
});

it('shows the saved list when the local input record is damaged and does not submit it', async () => {
  localStorage.setItem('etf-training-history:input:v1', '{broken input');
  handle = async () => scope(['510300']);
  render(<ETFTrainingHistoryPanel defaultOpen />);
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'ETF训练代码' })).toHaveValue('510300'));
  expect(screen.getByText('之前未保存的输入暂时读不到，当前显示已保存清单。')).toBeVisible();
  expect(calls.every(call => call.method === 'GET')).toBe(true);
});

it('keeps cached invalid dates editable after the server rejects the range instead of trapping the page', async () => {
  localStorage.setItem('etf-training-history:input:v1', JSON.stringify({ version: 1, list_sha256: 'b'.repeat(64),
    codes: codes.join('\n'), start: '2099-01-02', end: '2099-01-04' }));
  handle = async url => url.includes('start_date=2099-01-02')
    ? new Response(JSON.stringify({ detail: '请选择已经结束的日期。' }), { status: 400 }) : scope(codes);
  render(<ETFTrainingHistoryPanel defaultOpen />);
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'ETF训练代码' })).toBeEnabled());
  expect(screen.getByRole('textbox', { name: 'ETF训练代码' })).toHaveValue(codes.join('\n'));
  expect(screen.getByLabelText('训练历史开始日期')).toHaveValue('2099-01-02');
  expect(screen.getByLabelText('训练历史结束日期')).toBeEnabled();
  expect(screen.getByText('上次填写的日期未通过检查，请调整后重新检查。')).toBeVisible();
  expect(screen.getByRole('button', { name: '准备历史数据' })).toBeDisabled();
  expect(calls.every(call => call.method === 'GET')).toBe(true);
});

it('keeps another page saved list and retains the older unsubmitted input for reading', async () => {
  const view = render(<ETFTrainingHistoryPanel defaultOpen />);
  const input = await screen.findByRole('textbox', { name: 'ETF训练代码' });
  await waitFor(() => expect(input).toBeEnabled());
  fireEvent.change(input, { target: { value: codes.join('，') } });
  view.unmount();
  handle = async () => ({ ...scope(['159915']), list_sha256: 'd'.repeat(64) });
  render(<ETFTrainingHistoryPanel defaultOpen />);
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'ETF训练代码' })).toHaveValue('159915'));
  expect(screen.getByText('其他页面已修改清单，当前显示已保存内容。之前未保存的输入保留在下方。')).toBeVisible();
  fireEvent.click(screen.getByText('查看之前未保存的内容'));
  expect(screen.getByText(codes.join('，'))).toBeVisible();
  expect(calls.every(call => call.method === 'GET')).toBe(true);
});

it('restores the acknowledged save with a read after re-entry without submitting it again', async () => {
  let saved = false, failCheck = true;
  handle = async (_url, method) => {
    if (method === 'PUT') { saved = true; return scope(codes); }
    if (saved && failCheck) throw new TypeError('check offline');
    return scope(saved ? codes : []);
  };
  const view = render(<ETFTrainingHistoryPanel defaultOpen />);
  const input = await screen.findByRole('textbox', { name: 'ETF训练代码' });
  await waitFor(() => expect(input).toBeEnabled());
  fireEvent.change(input, { target: { value: codes.join('，') } });
  fireEvent.click(screen.getByRole('button', { name: '保存清单并检查' }));
  await screen.findByText('清单已保存，日期检查未完成，请重试检查。');
  view.unmount(); failCheck = false;
  render(<ETFTrainingHistoryPanel defaultOpen />);
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'ETF训练代码' })).toHaveValue(codes.join('\n')));
  expect(screen.getByRole('button', { name: '保存清单并检查' })).toBeDisabled();
  await waitFor(() => expect(screen.getByRole('button', { name: '准备历史数据' })).toBeEnabled());
  expect(calls.filter(call => call.method === 'PUT')).toHaveLength(1);
  expect(calls.some(call => call.method === 'POST')).toBe(false);
});

it('keeps the original request dates ahead of cached inputs while recovering an unfinished preparation', async () => {
  localStorage.setItem('etf-training-history:input:v1', JSON.stringify({ version: 1, list_sha256: 'b'.repeat(64),
    codes: '不应恢复的代码', start: '2023-01-03', end: '2023-01-04' }));
  localStorage.setItem('etf-training-history:pending:v1', JSON.stringify({ list_sha256: 'b'.repeat(64), request: {
    request_id: '1'.repeat(32), start_date: '2024-01-02', end_date: '2024-01-04', expected_scope_sha256: 'c'.repeat(64),
  } }));
  handle = async url => url.includes('/scope') ? scope(codes) : { task: task('running'), report: null };
  render(<ETFTrainingHistoryPanel defaultOpen />);
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'ETF训练代码' })).toHaveValue(codes.join('\n')));
  expect(screen.getByRole('textbox', { name: 'ETF训练代码' })).toBeDisabled();
  expect(screen.getByLabelText('训练历史开始日期')).toHaveValue('2024-01-02');
  expect(screen.getByLabelText('训练历史结束日期')).toHaveValue('2024-01-04');
  expect(calls.every(call => call.method === 'GET')).toBe(true);
});

it('shows a local input save failure without clearing the input or silently saving the list', async () => {
  const original = Storage.prototype.setItem;
  const blocked = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key, value) {
    if (key === 'etf-training-history:input:v1') throw new Error('storage full');
    return original.call(this, key, value);
  });
  try {
    render(<ETFTrainingHistoryPanel defaultOpen />);
    const input = await screen.findByRole('textbox', { name: 'ETF训练代码' });
    await waitFor(() => expect(input).toBeEnabled());
    fireEvent.change(input, { target: { value: '510300' } });
    expect(screen.getByText('当前输入暂时无法保存在本机，请保存清单后再离开页面。')).toBeVisible();
    expect(input).toHaveValue('510300');
    expect(calls.every(call => call.method === 'GET')).toBe(true);
  } finally { blocked.mockRestore(); }
});

it('prepares twenty ETFs and displays common dates and a missing reason before retrying the same range', async () => {
  let starts = 0;
  handle = async (url, method) => {
    if (url.includes('/scope')) return scope(codes);
    if (method === 'POST') { starts++; return { task: task(), report: null }; }
    return { task: task(starts === 1 ? 'partial' : 'done'), report: report(starts > 1) };
  };
  render(<ETFTrainingHistoryPanel defaultOpen />);
  const start = await screen.findByRole('button', { name: '准备历史数据' });
  await waitFor(() => expect(start).toBeEnabled());
  fireEvent.click(start);
  expect(await screen.findByText(/缺少1个交易日的原始价/)).toBeVisible();
  expect(screen.getByText(/共同覆盖2个交易日/)).toBeVisible();
  expect(screen.getByText('2024-01-03')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '重试准备' }));
  expect(await screen.findByText('这份清单的历史数据已准备并核对。')).toBeVisible();
  expect(screen.getByText(/共同覆盖3个交易日/)).toBeVisible();
  const posts = calls.filter(call => call.method === 'POST');
  expect(posts).toHaveLength(2);
  expect(posts[0].body).toMatchObject({ start_date: '2024-01-02', end_date: '2024-01-04', expected_scope_sha256: 'c'.repeat(64) });
  expect(posts[0].body).not.toHaveProperty('portfolio_id');
  expect(posts[0].body.request_id).not.toBe(posts[1].body.request_id);
  expect(calls.every(call => call.url.includes(root))).toBe(true);
});

it('recovers a lost preparation response after re-entry without submitting another job', async () => {
  handle = async (url, method) => {
    if (url.includes('/scope')) return scope(codes);
    if (method === 'POST') throw new TypeError('response lost');
    return { task: task('done'), report: report(true) };
  };
  const view = render(<ETFTrainingHistoryPanel defaultOpen />);
  const start = await screen.findByRole('button', { name: '准备历史数据' });
  await waitFor(() => expect(start).toBeEnabled());
  fireEvent.click(start);
  await screen.findByText('尚未确认准备是否开始，请先重试读取进度。');
  expect(screen.getByRole('button', { name: '准备历史数据' })).toBeDisabled();
  view.unmount();
  render(<ETFTrainingHistoryPanel defaultOpen />);
  expect(await screen.findByText('这份清单的历史数据已准备并核对。')).toBeVisible();
  expect(calls.filter(call => call.method === 'POST')).toHaveLength(1);
  expect(calls.some(call => call.url.includes('/preparations/requests/'))).toBe(true);
});

it('keeps submission blocked when the browser cannot read the previous preparation request', async () => {
  const original = Storage.prototype.getItem;
  const blocked = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(function (this: Storage, key: string) {
    if (key === 'etf-training-history:pending:v1') throw new Error('storage disabled');
    return original.call(this, key);
  });
  try {
    render(<ETFTrainingHistoryPanel defaultOpen />);
    expect(await screen.findByText(/无法读取本地准备记录/)).toBeVisible();
    expect(screen.getByRole('button', { name: '准备历史数据' })).toBeDisabled();
    expect(calls.some(call => call.method === 'POST')).toBe(false);
  } finally { blocked.mockRestore(); }
});

it('retries an unreceived request with the original parameters after re-entry', async () => {
  let attempts = 0;
  handle = async (url, method) => {
    if (url.includes('/scope')) return scope(codes);
    if (url.includes('/preparations/requests/')) return { task: null, report: null };
    if (method === 'POST' && ++attempts === 1) throw new TypeError('request never arrived');
    return { task: task('done'), report: report(true) };
  };
  const first = render(<ETFTrainingHistoryPanel defaultOpen />);
  const start = await screen.findByRole('button', { name: '准备历史数据' });
  await waitFor(() => expect(start).toBeEnabled());
  fireEvent.click(start);
  await screen.findByText('尚未确认准备是否开始，请先重试读取进度。');
  first.unmount();
  render(<ETFTrainingHistoryPanel defaultOpen />);
  fireEvent.click(await screen.findByRole('button', { name: '重试准备' }));
  await screen.findByText('这份清单的历史数据已准备并核对。');
  const posts = calls.filter(call => call.method === 'POST');
  expect(posts).toHaveLength(2);
  expect(posts[0].body).toEqual(posts[1].body);
});

it('keeps a running job after a progress read failure and resumes reading without submitting', async () => {
  let reads = 0;
  handle = async url => {
    if (url.includes('/scope')) return { ...scope(codes), latest_task: task('running') };
    if (++reads === 1) return { task: task('running'), report: null };
    if (reads === 2) throw new TypeError('disconnected');
    return { task: task('done'), report: report(true) };
  };
  render(<ETFTrainingHistoryPanel defaultOpen />);
  await screen.findByText('准备进度读取失败，任务可能仍在运行。请重试读取进度。');
  expect(screen.getByRole('button', { name: '准备历史数据' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: '重试读取进度' }));
  await screen.findByText('这份清单的历史数据已准备并核对。');
  expect(calls.some(call => call.method === 'POST')).toBe(false);
});

it('shows a failed initial read as an error with a retry instead of an empty list', async () => {
  let failed = true;
  handle = async () => { if (failed) throw new TypeError('offline'); return scope(codes); };
  render(<ETFTrainingHistoryPanel defaultOpen />);
  await screen.findByText('ETF训练清单或准备记录暂时读不到，请点“读取已保存清单”重试。');
  expect(screen.getByRole('button', { name: '准备历史数据' })).toBeDisabled();
  failed = false;
  fireEvent.click(screen.getByRole('button', { name: '读取已保存清单' }));
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'ETF训练代码' })).toHaveValue(codes.join('\n')));
  expect(screen.getByRole('button', { name: '准备历史数据' })).toBeEnabled();
  expect(calls.some(call => call.method === 'POST')).toBe(false);
});

it('does not display an older completed preparation as the result of a changed list', async () => {
  localStorage.setItem('etf-training-history:pending:v1', JSON.stringify({ list_sha256: 'b'.repeat(64), request: {
    request_id: '1'.repeat(32), start_date: '2024-01-02', end_date: '2024-01-04', expected_scope_sha256: 'c'.repeat(64),
  } }));
  handle = async url => url.includes('/scope') ? { ...scope(['159915']), list_sha256: 'd'.repeat(64) }
    : { task: { ...task('done'), message: 'ETF训练历史数据已准备并核对，可以返回训练页面检查。' }, report: report(true) };
  render(<ETFTrainingHistoryPanel defaultOpen />);
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'ETF训练代码' })).toHaveValue('159915'));
  expect(await screen.findByText('上次任务已结束，训练清单后来有修改，请按当前清单重新检查。')).toBeVisible();
  expect(screen.queryByText('ETF训练历史数据已准备并核对，可以返回训练页面检查。')).not.toBeInTheDocument();
  expect(screen.queryByText('这份清单的历史数据已准备并核对。')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '准备历史数据' })).toBeEnabled();
  expect(calls.some(call => call.method === 'POST')).toBe(false);
});

it('checks a changed date range locally and hides the older result without downloading again', async () => {
  handle = async url => {
    if (!url.includes('/scope')) return { task: task('done'), report: report(true) };
    const changed = url.includes('end_date=2024-01-05');
    return { ...scope(codes), end_date: changed ? '2024-01-05' : '2024-01-04',
      latest_task: changed ? null : task('done'), common_trade_date_count: changed ? 4 : 3,
      expected_trade_date_count: changed ? 4 : 3, common_start_date: '2024-01-02',
      common_end_date: changed ? '2024-01-05' : '2024-01-04' };
  };
  render(<ETFTrainingHistoryPanel defaultOpen />);
  await screen.findByText('这份清单的历史数据已准备并核对。');
  expect(screen.getByRole('status', { name: 'ETF准备结果汇总' })).toBeVisible();
  fireEvent.click(screen.getByText('调整准备日期'));
  fireEvent.change(screen.getByLabelText('训练历史结束日期'), { target: { value: '2024-01-05' } });
  expect(screen.queryByText(/共同覆盖3个交易日/)).not.toBeInTheDocument();
  expect(screen.queryByText('这份清单的历史数据已准备并核对。')).not.toBeInTheDocument();
  expect(screen.queryByRole('status', { name: 'ETF准备结果汇总' })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '准备历史数据' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: '检查日期覆盖' }));
  expect(await screen.findByText(/共同覆盖4个交易日/)).toBeVisible();
  expect(screen.getByRole('button', { name: '准备历史数据' })).toBeEnabled();
  expect(calls.some(call => call.method !== 'GET')).toBe(false);
});

it('returns prepared history to the ETF training checks without submitting training', async () => {
  const training = { dataset_id: 'checked-twenty', manifest_sha256: 'd'.repeat(64),
    start_date: '2024-01-02', end_date: '2025-01-31', trade_date_count: 284, symbol_count: 20 };
  handle = async () => ({ ...scope(codes), dataset_id: 'checked-twenty', training_ready: true,
    training_dataset: training,
    common_trade_date_count: 284, expected_trade_date_count: 284, common_start_date: '2024-01-02',
    common_end_date: '2025-01-31' });
  render(<ETFTrainingHistoryPanel defaultOpen />);
  fireEvent.click(await screen.findByRole('button', { name: '查看训练条件' }));
  expect(localStorage.getItem('settings_active_section')).toBe('factor-center');
  expect(localStorage.getItem('settings_factor_center_subtab')).toBe('model');
  expect(JSON.parse(localStorage.getItem('factor-etf-training:open')!)).toEqual({ version: 1,
    selection: { ...training, list_sha256: 'b'.repeat(64) } });
  expect(calls.some(call => call.method !== 'GET')).toBe(false);
});

it('returns the completed preparation even when the saved scope points to later history', async () => {
  const prepared = { dataset_id: 'prepared-range', manifest_sha256: 'd'.repeat(64),
    start_date: '2024-01-02', end_date: '2024-01-04', trade_date_count: 3, symbol_count: 20 };
  handle = async url => url.includes('/scope')
    ? { ...scope(codes), dataset_id: 'later-range', latest_task: task('done'),
        training_dataset: { ...prepared, dataset_id: 'later-range', end_date: '2025-01-31' } }
    : { task: task('done'), report: { ...report(true), training: prepared } };
  render(<ETFTrainingHistoryPanel defaultOpen />);
  expect(await screen.findByText('这份清单的历史数据已准备并核对。')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '查看训练条件' }));
  expect(JSON.parse(localStorage.getItem('factor-etf-training:open')!).selection).toEqual({
    ...prepared, list_sha256: 'b'.repeat(64),
  });
  expect(calls.some(call => call.method !== 'GET')).toBe(false);
});

it('asks for a reread when the prepared file identity is missing instead of opening another dataset', async () => {
  handle = async () => ({ ...scope(codes), dataset_id: 'checked-twenty' });
  render(<ETFTrainingHistoryPanel defaultOpen />);
  fireEvent.click(await screen.findByRole('button', { name: '查看训练条件' }));
  expect(await screen.findByText('本次资料记录不完整，请先读取已保存清单，仍无法读取时重新准备历史数据。')).toBeVisible();
  expect(localStorage.getItem('factor-etf-training:open')).toBeNull();
  expect(localStorage.getItem('settings_active_section')).toBeNull();
  expect(calls.some(call => call.method !== 'GET')).toBe(false);
});

it('waits for an older running list before allowing a preparation for the current list', async () => {
  localStorage.setItem('etf-training-history:pending:v1', JSON.stringify({ list_sha256: 'b'.repeat(64), request: {
    request_id: '1'.repeat(32), start_date: '2024-01-02', end_date: '2024-01-04', expected_scope_sha256: 'c'.repeat(64),
  } }));
  let finished = false;
  handle = async url => url.includes('/scope') ? { ...scope(['159915']), list_sha256: 'd'.repeat(64) }
    : { task: task(finished ? 'done' : 'running'), report: finished ? report(true) : null };
  render(<ETFTrainingHistoryPanel defaultOpen />);
  await screen.findByText('上次清单的历史数据还在准备，请稍后读取进度。当前清单尚未检查。');
  expect(screen.getByRole('button', { name: '准备历史数据' })).toBeDisabled();
  expect(screen.getByRole('textbox', { name: 'ETF训练代码' })).toHaveValue('159915');
  finished = true;
  fireEvent.click(screen.getByRole('button', { name: '重试读取进度' }));
  await screen.findByText('上次任务已结束，训练清单后来有修改，请按当前清单重新检查。');
  expect(screen.getByRole('button', { name: '准备历史数据' })).toBeEnabled();
  expect(calls.some(call => call.method === 'POST')).toBe(false);
});

it('keeps changed dates unconfirmed after a check fails and lets the user retry without preparing', async () => {
  let failCheck = true;
  handle = async url => {
    if (url.includes('end_date=2024-01-05') && failCheck) throw new TypeError('coverage offline');
    return { ...scope(codes), end_date: url.includes('end_date=2024-01-05') ? '2024-01-05' : '2024-01-04' };
  };
  render(<ETFTrainingHistoryPanel defaultOpen />);
  await waitFor(() => expect(screen.getByRole('button', { name: '准备历史数据' })).toBeEnabled());
  fireEvent.click(screen.getByText('调整准备日期'));
  fireEvent.change(screen.getByLabelText('训练历史结束日期'), { target: { value: '2024-01-05' } });
  fireEvent.click(screen.getByRole('button', { name: '检查日期覆盖' }));
  await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument());
  expect(screen.getByRole('button', { name: '准备历史数据' })).toBeDisabled();
  failCheck = false;
  fireEvent.click(screen.getByRole('button', { name: '检查日期覆盖' }));
  await waitFor(() => expect(screen.getByRole('button', { name: '准备历史数据' })).toBeEnabled());
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(calls.some(call => call.method !== 'GET')).toBe(false);
});

it('labels a slow local date check without showing a download in progress', async () => {
  let finishCheck: ((value: any) => void) | undefined;
  handle = async url => url.includes('end_date=2024-01-05')
    ? new Promise(resolve => { finishCheck = resolve; }) : scope(codes);
  render(<ETFTrainingHistoryPanel defaultOpen />);
  await waitFor(() => expect(screen.getByRole('button', { name: '准备历史数据' })).toBeEnabled());
  fireEvent.click(screen.getByText('调整准备日期'));
  fireEvent.change(screen.getByLabelText('训练历史结束日期'), { target: { value: '2024-01-05' } });
  fireEvent.click(screen.getByRole('button', { name: '检查日期覆盖' }));
  expect(await screen.findByText('正在检查本地已有行情的日期…')).toBeVisible();
  expect(within(screen.getByRole('button', { name: '准备历史数据' })).queryByRole('img', { name: 'loading' })).not.toBeInTheDocument();
  expect(calls.some(call => call.method !== 'GET')).toBe(false);
  await act(async () => finishCheck?.({ ...scope(codes), end_date: '2024-01-05' }));
  expect(screen.queryByText('正在检查本地已有行情的日期…')).not.toBeInTheDocument();
});

it('starts collapsed and expands to the training form', async () => {
  render(<ETFTrainingHistoryPanel />);
  expect(screen.queryByLabelText('ETF训练代码')).not.toBeInTheDocument();
  expect(await screen.findByText(/清单有0只ETF/)).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '展开' }));
  expect(await screen.findByLabelText('ETF训练代码')).toBeVisible();
  expect(screen.getByRole('button', { name: '收起' })).toBeVisible();
});
