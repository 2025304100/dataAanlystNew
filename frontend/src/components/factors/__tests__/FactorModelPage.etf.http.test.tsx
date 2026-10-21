import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { App } from 'antd';
import FactorModelPage from '../FactorModelPage';

const response = (value: unknown) => new Response(JSON.stringify(value), { status: 200, headers: { 'Content-Type': 'application/json' } });
const factorSet = { id: 'price-set', label: 'ETF价量特征', name: 'ETF价量特征', asset_type: 'etf', status: 'frozen', member_count: 1,
  content_hash: 'b'.repeat(64), members: [{ id: 1, factor_code: 'return5', factor_version: 1, version_status: 'valid', version_trainable: true, role: 'feature', weight_constraint: 'free', missing_policy: 'exclude' }] };
const dataset = { dataset_id: 'reviewed-etf', manifest_sha256: 'a'.repeat(64), symbols: ['510300'], symbol_count: 1,
  start_date: '2021-07-12', end_date: '2026-10-20', trade_date_count: 1277, bar_count: 1277,
  minimum_symbols: 20, training_ready: false, feature_ready: true, feature_message: '所选价量特征已检查。',
  training_message: '当前资料有1只ETF，现有模型需要至少20只ETF进行同日比较。' };
const selectedHistory = { dataset_id: 'prepared-range', manifest_sha256: 'c'.repeat(64), list_sha256: 'd'.repeat(64),
  start_date: '2024-01-02', end_date: '2025-01-27', trade_date_count: 280, symbol_count: 20 };
let writes: Array<{ url: string; body: any }>;
let options: any;
let loseSubmitResponse: boolean;
let recoveryResult: 'found' | 'not_found' | 'error' | 'mismatch';
let recoveredStatus: string;
let submitStatus: number;
let heldSubmit: Promise<Response> | null;
let collectionItems: any[];
let defaultPreparations: number;
let defaultFailure: boolean;
let restoredRequest: any;
let historyFailure: boolean;
let overviewUnavailable: boolean;
beforeEach(() => {
  overviewUnavailable = false;
  historyFailure = false;
  collectionItems = [factorSet]; defaultPreparations = 0; defaultFailure = false; restoredRequest = null;
  loseSubmitResponse = false; recoveryResult = 'found'; recoveredStatus = 'done'; submitStatus = 200; heldSubmit = null;
  writes = []; options = { factor_set_id: 'price-set', factor_ready: true, minimum_symbols: 20, items: [dataset], errors: [] };
  localStorage.clear();
  vi.stubGlobal('fetch', vi.fn(async (input, init) => {
    const url = String(input);
    if (url.endsWith('/scoring/etf-training/factor-set') && init?.method === 'POST') {
      defaultPreparations += 1;
      if (defaultFailure) return new Response(JSON.stringify({ detail: 'ETF价量特征集合与固定版本不一致，请先核对原有记录。' }), { status: 409 });
      return response(factorSet);
    }
    if (init?.method === 'POST') {
      writes.push({ url, body: JSON.parse(init.body) });
      if (heldSubmit) return heldSubmit;
      if (loseSubmitResponse) throw new TypeError('Failed to fetch');
      if (submitStatus !== 200) return new Response(JSON.stringify({ detail: 'ETF数量不足，暂不能训练。' }), { status: submitStatus });
      return response({ id: 'etf-task', status: 'queued' });
    }
    if (url.includes('/scoring/etf-training/requests/')) {
      const request = restoredRequest ?? writes[0]?.body;
      if (recoveryResult === 'error') throw new TypeError('Failed to fetch');
      if (recoveryResult === 'not_found') return response({ request_id: request?.request_id, state: 'not_found', request: null, task: null });
      return response({ request_id: request?.request_id, state: 'found',
        request: recoveryResult === 'mismatch' ? { ...request, dataset_id: 'another-input' } : request,
        task: { id: 'etf-task', status: recoveredStatus } });
    }
    if (url.includes('/etf-history/training-options')) return historyFailure
      ? new Response(JSON.stringify({ detail: '本次准备的资料文件已有变化，请回到历史数据页重新检查。' }), { status: 409 }) : response(options);
    if (url.includes('/scoring/models?')) return response([]);
    if (url.includes('/scoring/factorsets?')) return response(collectionItems);
    if (url.includes('/scoring/factorsets/stock-set/readiness')) return response({ factor_set_id: 'stock-set', ready: true });
    if (url.includes('/scoring/overview')) return response(overviewUnavailable ? null : { feature_enabled: true, config: {}, runtime: { weight_mode: 'manual', version: 1 }, health: {} });
    if (url.includes('/scoring/tasks?')) return response({ items: [], total: 0 });
    if (url.includes('/notifications')) return response({ items: [], total: 0 });
    throw new Error('Unexpected HTTP request: ' + url);
  }));
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); localStorage.clear(); vi.unstubAllGlobals(); });

it('keeps the model page open when its status is missing and recovers with refresh', async () => {
  overviewUnavailable = true;
  render(<App><FactorModelPage /></App>);
  const issue = await screen.findByRole('alert', { name: '模型资料读取失败' });
  expect(issue).toHaveTextContent('模型资料暂时读不到');
  expect(screen.getByRole('button', { name: 'ETF训练' })).toBeVisible();
  overviewUnavailable = false;
  fireEvent.click(within(issue).getByRole('button', { name: '重试读取模型资料' }));
  expect(await screen.findByText('ETF价量特征')).toBeVisible();
  expect(screen.queryByText('模型资料暂时读不到')).not.toBeInTheDocument();
  expect(vi.mocked(fetch).mock.calls.filter(([url]) => String(url).includes('/scoring/overview'))).toHaveLength(2);
  expect(writes).toEqual([]);
  expect(defaultPreparations).toBe(0);
});

it('keeps previously read model collections visible during a failed refresh and recovers without writing', async () => {
  render(<App><FactorModelPage /></App>);
  await screen.findByText('ETF价量特征');
  overviewUnavailable = true;
  fireEvent.click(screen.getAllByRole('button', { name: /刷新/ })[0]);
  const issue = await screen.findByRole('alert', { name: '模型资料读取失败' });
  expect(issue).toHaveTextContent('下方保留上次读到的资料');
  expect(screen.getByText('ETF价量特征')).toBeVisible();
  overviewUnavailable = false;
  fireEvent.click(within(issue).getByRole('button', { name: '重试读取模型资料' }));
  await waitFor(() => expect(screen.queryByText('模型资料暂时读不到')).not.toBeInTheDocument());
  expect(screen.getByText('ETF价量特征')).toBeVisible();
  expect(writes).toEqual([]);
  expect(defaultPreparations).toBe(0);
});

it('opens ETF training from the model page and explains why a single ETF cannot train', async () => {
  render(<App><FactorModelPage /></App>);
  fireEvent.click(await screen.findByRole('button', { name: 'ETF训练' }));
  const dialog = await screen.findByRole('dialog', { name: 'ETF训练' });
  await waitFor(() => expect(within(dialog).getByText(/当前资料有1只ETF.*20只ETF/)).toBeVisible());
  expect(within(dialog).getByRole('button', { name: '提交ETF训练' })).toBeDisabled();
  expect(within(dialog).getByRole('button', { name: '补充ETF历史数据' })).toBeEnabled();
  expect(writes).toEqual([]);
});

it('reads history for the independent training list and points an empty list to data preparation', async () => {
  options = { ...options, items: [], factor_ready: false, list_symbol_count: 0,
    list_message: 'ETF训练清单还是空的，请先填写代码并准备历史数据。' };
  render(<App><FactorModelPage /></App>);
  fireEvent.click(await screen.findByRole('button', { name: 'ETF训练' }));
  const dialog = await screen.findByRole('dialog', { name: 'ETF训练' });
  await waitFor(() => expect(within(dialog).getByText('ETF训练清单还是空的，请先填写代码并准备历史数据。')).toBeVisible());
  expect(within(dialog).getByRole('button', { name: '提交ETF训练' })).toBeDisabled();
  const request = vi.mocked(fetch).mock.calls.find(([url]) => String(url).includes('/training-options'));
  expect(String(request?.[0])).toContain('use_training_list=true');
  fireEvent.click(within(dialog).getByRole('button', { name: '填写ETF代码' }));
  expect(localStorage.getItem('settings_active_section')).toBe('data-center');
  expect(localStorage.getItem('settings_data_center_tab')).toBe('market');
  expect(localStorage.getItem('etf-training-history:focus')).toBe('codes');
  expect(writes).toEqual([]);
});

it.each([
  { count: 1, days: 1277, message: '还差19只ETF，请先补充代码。', action: '补充ETF代码', target: 'codes' },
  { count: 20, days: 200, message: '还差74个交易日，请调整准备日期后重新检查。', action: '调整准备日期', target: 'dates' },
])('points incomplete training inputs to $action without starting a job', async ({ count, days, message, action, target }) => {
  options = { ...options, list_symbol_count: count,
    items: [{ ...dataset, symbol_count: count, trade_date_count: days, training_ready: false }] };
  render(<App><FactorModelPage /></App>);
  fireEvent.click(await screen.findByRole('button', { name: 'ETF训练' }));
  const dialog = await screen.findByRole('dialog', { name: 'ETF训练' });
  await waitFor(() => expect(within(dialog).getByText(message)).toBeVisible());
  expect(within(dialog).getByRole('button', { name: '提交ETF训练' })).toBeDisabled();
  expect(within(dialog).queryByRole('button', { name: '重新检查' })).not.toBeInTheDocument();
  fireEvent.click(within(dialog).getByRole('button', { name: action }));
  expect(localStorage.getItem('settings_active_section')).toBe('data-center');
  expect(localStorage.getItem('etf-training-history:focus')).toBe(target);
  expect(writes).toEqual([]);
});

it('opens ETF training checks after returning from history preparation without starting training', async () => {
  localStorage.setItem('factor-etf-training:open', '1');
  render(<App><FactorModelPage /></App>);
  const dialog = await screen.findByRole('dialog', { name: 'ETF训练' });
  await waitFor(() => expect(within(dialog).getByText(/当前资料有1只ETF.*20只ETF/)).toBeVisible());
  expect(within(dialog).getByRole('button', { name: '提交ETF训练' })).toBeDisabled();
  expect(localStorage.getItem('factor-etf-training:open')).toBeNull();
  expect(writes).toEqual([]);
});

it('handles a return to an already open model page and consumes the request once', async () => {
  render(<App><FactorModelPage /></App>);
  await screen.findByRole('button', { name: 'ETF训练' });
  localStorage.setItem('factor-etf-training:open', '1');
  fireEvent(window, new Event('factor-etf-training:open'));
  const dialog = await screen.findByRole('dialog', { name: 'ETF训练' });
  await waitFor(() => expect(within(dialog).getByText(/当前资料有1只ETF.*20只ETF/)).toBeVisible());
  expect(localStorage.getItem('factor-etf-training:open')).toBeNull();
  expect(writes).toEqual([]);
});

it('keeps the history page dataset and dates through checks and submission instead of choosing the first result', async () => {
  const prepared = { ...dataset, dataset_id: 'prepared-range', manifest_sha256: 'c'.repeat(64),
    start_date: '2024-01-02', end_date: '2025-01-27', trade_date_count: 280,
    symbols: Array.from({ length: 20 }, (_, i) => String(510300 + i)), symbol_count: 20,
    training_ready: true, training_message: '行情资料和训练特征检查通过。' };
  options = { ...options, list_sha256: 'd'.repeat(64), items: [{ ...prepared, dataset_id: 'later-range', end_date: '2025-02-24' }, prepared] };
  localStorage.setItem('factor-etf-training:open', JSON.stringify({ version: 1, selection: {
    dataset_id: 'prepared-range', manifest_sha256: 'c'.repeat(64), list_sha256: 'd'.repeat(64),
    start_date: '2024-01-02', end_date: '2025-01-27', trade_date_count: 280, symbol_count: 20,
  } }));
  render(<App><FactorModelPage /></App>);
  const dialog = await screen.findByRole('dialog', { name: 'ETF训练' });
  await waitFor(() => expect(within(dialog).getByText('2024-01-02 至 2025-01-27')).toBeVisible());
  expect(within(dialog).queryByLabelText('ETF训练历史资料')).not.toBeInTheDocument();
  const checkUrl = String(vi.mocked(fetch).mock.calls.find(([url]) => String(url).includes('/training-options'))?.[0]);
  expect(checkUrl).toContain('dataset_id=prepared-range');
  expect(checkUrl).toContain('manifest_sha256=' + 'c'.repeat(64));
  expect(checkUrl).toContain('expected_list_sha256=' + 'd'.repeat(64));
  fireEvent.click(within(dialog).getByRole('button', { name: '提交ETF训练' }));
  await waitFor(() => expect(writes).toHaveLength(1));
  expect(writes[0].body).toMatchObject({ dataset_id: 'prepared-range', manifest_sha256: 'c'.repeat(64) });
});

it('keeps the prepared dataset when the model page is re-entered before submission', async () => {
  const prepared = { ...dataset, ...selectedHistory, training_ready: true };
  options = { ...options, list_sha256: 'd'.repeat(64), items: [{ ...prepared, dataset_id: 'later-range', end_date: '2025-02-24' }, prepared] };
  localStorage.setItem('factor-etf-training:open', JSON.stringify({ version: 1, selection: selectedHistory }));
  const view = render(<App><FactorModelPage /></App>);
  let dialog = await screen.findByRole('dialog', { name: 'ETF训练' });
  await waitFor(() => expect(within(dialog).getByText('2024-01-02 至 2025-01-27')).toBeVisible());
  fireEvent.click(within(dialog).getByRole('button', { name: /关\s*闭/ }));
  view.unmount();
  render(<App><FactorModelPage /></App>);
  fireEvent.click(await screen.findByRole('button', { name: 'ETF训练' }));
  dialog = await screen.findByRole('dialog', { name: 'ETF训练' });
  await waitFor(() => expect(within(dialog).getByText('2024-01-02 至 2025-01-27')).toBeVisible());
  expect(within(dialog).queryByLabelText('ETF训练历史资料')).not.toBeInTheDocument();
  expect(writes).toEqual([]);
});

it.each(['missing', 'hash', 'list', 'dates', 'count'])('blocks a changed %s without selecting other available history', async change => {
  const prepared: any = { ...dataset, ...selectedHistory, training_ready: true };
  if (change === 'hash') prepared.manifest_sha256 = 'e'.repeat(64);
  if (change === 'dates') prepared.end_date = '2025-02-24';
  if (change === 'count') prepared.symbol_count = 21;
  options = { ...options, list_sha256: change === 'list' ? 'e'.repeat(64) : 'd'.repeat(64),
    items: change === 'missing' ? [{ ...prepared, dataset_id: 'later-range' }] : [prepared] };
  localStorage.setItem('factor-etf-training:open', JSON.stringify({ version: 1, selection: selectedHistory }));
  render(<App><FactorModelPage /></App>);
  const dialog = await screen.findByRole('dialog', { name: 'ETF训练' });
  await waitFor(() => expect(within(dialog).getByText('本次准备的资料或训练清单已有变化，请回到历史数据页重新检查。')).toBeVisible());
  expect(within(dialog).getByRole('button', { name: '提交ETF训练' })).toBeDisabled();
  expect(within(dialog).queryByText('资料日期')).not.toBeInTheDocument();
  expect(writes).toEqual([]);
});

it('keeps the selected history on retry and shows the server reason without a fallback', async () => {
  options = { ...options, list_sha256: 'd'.repeat(64), items: [{ ...dataset, ...selectedHistory, training_ready: true }] };
  historyFailure = true;
  localStorage.setItem('factor-etf-training:open', JSON.stringify({ version: 1, selection: selectedHistory }));
  render(<App><FactorModelPage /></App>);
  const dialog = await screen.findByRole('dialog', { name: 'ETF训练' });
  await waitFor(() => expect(within(dialog).getByText(/本次准备的资料文件已有变化/)).toBeVisible());
  expect(within(dialog).getByRole('button', { name: '提交ETF训练' })).toBeDisabled();
  historyFailure = false;
  fireEvent.click(within(dialog).getByRole('button', { name: '重新检查' }));
  await waitFor(() => expect(within(dialog).getByText('2024-01-02 至 2025-01-27')).toBeVisible());
  expect(within(dialog).getByRole('button', { name: '提交ETF训练' })).toBeEnabled();
  const checks = vi.mocked(fetch).mock.calls.filter(([url]) => String(url).includes('/training-options'));
  expect(checks).toHaveLength(2);
  expect(checks.every(([url]) => String(url).includes('dataset_id=prepared-range'))).toBe(true);
  expect(writes).toEqual([]);
});

it('restores an existing training before checking newly prepared history', async () => {
  restoredRequest = { request_id: 'c'.repeat(32), factor_set_id: 'older-etf', dataset_id: 'older-history', manifest_sha256: 'e'.repeat(64), note: '原有备注' };
  localStorage.setItem('factor-etf-training:pending:v1', JSON.stringify({ version: 1, request: restoredRequest }));
  localStorage.setItem('factor-etf-training:open', JSON.stringify({ version: 1, selection: selectedHistory }));
  options = { ...options, list_sha256: 'd'.repeat(64), items: [{ ...dataset, ...selectedHistory, training_ready: true }] };
  render(<App><FactorModelPage /></App>);
  const dialog = await screen.findByRole('dialog', { name: 'ETF训练' });
  await waitFor(() => expect(within(dialog).getByRole('button', { name: '查看这次训练' })).toBeEnabled());
  expect(vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes('/training-options'))).toBe(false);
  expect(JSON.parse(localStorage.getItem('factor-etf-training:pending:v1')!).request.dataset_id).toBe('older-history');
  fireEvent.click(within(dialog).getByRole('button', { name: '开始新的训练' }));
  await waitFor(() => expect(within(dialog).getByText('2024-01-02 至 2025-01-27')).toBeVisible());
  expect(writes).toEqual([]);
});

it('blocks an incomplete history navigation record instead of opening default history', async () => {
  localStorage.setItem('factor-etf-training:open', JSON.stringify({ version: 1, selection: { dataset_id: 'prepared-range' } }));
  render(<App><FactorModelPage /></App>);
  const dialog = await screen.findByRole('dialog', { name: 'ETF训练' });
  await waitFor(() => expect(within(dialog).getByText(/本次准备的资料记录读不到/)).toBeVisible());
  expect(within(dialog).getByRole('button', { name: '提交ETF训练' })).toBeDisabled();
  expect(vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes('/training-options'))).toBe(false);
  expect(writes).toEqual([]);
});

it('prepares and selects ETF price volume features when the existing first collection is for stocks', async () => {
  collectionItems = [{ ...factorSet, id: 'stock-set', name: '股票换手率', label: '股票换手率', asset_type: 'stock' }];
  render(<App><FactorModelPage /></App>);
  fireEvent.click(await screen.findByRole('button', { name: 'ETF训练' }));
  const dialog = await screen.findByRole('dialog', { name: 'ETF训练' });
  await waitFor(() => expect(within(dialog).getByText('ETF价量特征')).toBeVisible());
  expect(within(dialog).queryByText('股票换手率')).not.toBeInTheDocument();
  expect(defaultPreparations).toBe(1);
  expect(writes).toEqual([]);
  expect(vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes('training-options?factor_set_id=stock-set'))).toBe(false);
  await waitFor(() => expect(within(dialog).getByText(/当前资料有1只ETF.*20只ETF/)).toBeVisible());
});

it('preserves an older pending collection and prepares the default only after explicitly starting a new training', async () => {
  restoredRequest = { request_id: 'c'.repeat(32), factor_set_id: 'older-etf', dataset_id: 'older-history', manifest_sha256: 'd'.repeat(64), note: '原有备注' };
  localStorage.setItem('factor-etf-training:pending:v1', JSON.stringify({ version: 1, request: restoredRequest }));
  render(<App><FactorModelPage /></App>);
  fireEvent.click(await screen.findByRole('button', { name: 'ETF训练' }));
  const dialog = await screen.findByRole('dialog', { name: 'ETF训练' });
  await waitFor(() => expect(within(dialog).getByRole('button', { name: '查看这次训练' })).toBeEnabled());
  expect(defaultPreparations).toBe(0);
  fireEvent.click(within(dialog).getByRole('button', { name: '开始新的训练' }));
  await waitFor(() => expect(defaultPreparations).toBe(1));
  await waitFor(() => expect(within(dialog).getByText('ETF价量特征')).toBeVisible());
  expect(writes).toEqual([]);
});

it('shows the preparation failure reason and retries without falling back to stock features', async () => {
  collectionItems = [{ ...factorSet, id: 'stock-set', name: '股票换手率', label: '股票换手率', asset_type: 'stock' }];
  defaultFailure = true;
  render(<App><FactorModelPage /></App>);
  fireEvent.click(await screen.findByRole('button', { name: 'ETF训练' }));
  const dialog = await screen.findByRole('dialog', { name: 'ETF训练' });
  await within(dialog).findByText(/ETF价量特征集合与固定版本不一致/);
  expect(within(dialog).getByRole('button', { name: '提交ETF训练' })).toBeDisabled();
  expect(within(dialog).queryByText('股票换手率')).not.toBeInTheDocument();
  defaultFailure = false;
  fireEvent.click(within(dialog).getByRole('button', { name: '重新检查' }));
  await waitFor(() => expect(within(dialog).getByText('ETF价量特征')).toBeVisible());
  expect(defaultPreparations).toBe(2);
  expect(writes).toEqual([]);
});

it('keeps the ordinary stock training entry when ETF collections appear first', async () => {
  collectionItems = [factorSet, { ...factorSet, id: 'stock-set', name: '股票换手率', label: '股票换手率', asset_type: 'stock' }];
  render(<App><FactorModelPage /></App>);
  fireEvent.click(await screen.findByTestId('btn-train-stock-set'));
  await waitFor(() => expect(screen.getByTestId('modal-train-model')).toBeVisible());
  await screen.findByText('集合资格检查通过');
  expect(screen.queryByRole('dialog', { name: 'ETF训练' })).not.toBeInTheDocument();
  expect(defaultPreparations).toBe(0);
  expect(writes).toEqual([]);
});

it('uses an explicitly selected ETF collection without replacing it with the default', async () => {
  collectionItems = [{ ...factorSet, id: 'other-etf', name: '本人ETF集合', label: '本人ETF集合' }];
  options.factor_set_id = 'other-etf';
  render(<App><FactorModelPage /></App>);
  fireEvent.click(await screen.findByTestId('btn-train-other-etf'));
  const dialog = await screen.findByRole('dialog', { name: 'ETF训练' });
  await waitFor(() => expect(within(dialog).getByText('本人ETF集合')).toBeVisible());
  await within(dialog).findByText(/当前资料有1只ETF.*20只ETF/);
  expect(defaultPreparations).toBe(0);
  expect(writes).toEqual([]);
});

it('submits the selected verified ETF input with its file hash and never submits stock scope', async () => {
  options.items = [{ ...dataset, symbols: Array.from({ length: 20 }, (_, i) => String(510300 + i)),
    symbol_count: 20, training_ready: true, bar_count: 25540, training_message: '行情资料和训练特征检查通过。' }];
  render(<App><FactorModelPage /></App>);
  fireEvent.click(await screen.findByRole('button', { name: 'ETF训练' }));
  const dialog = await screen.findByRole('dialog', { name: 'ETF训练' });
  const submit = within(dialog).getByRole('button', { name: '提交ETF训练' });
  await waitFor(() => expect(submit).toBeEnabled());
  fireEvent.click(submit);
  await waitFor(() => expect(writes).toHaveLength(1));
  expect(writes[0].url).toContain('/scoring/etf-training');
  expect(writes[0].body).toEqual({ dataset_id: 'reviewed-etf', factor_set_id: 'price-set',
    request_id: expect.stringMatching(/^[a-f0-9]{32}$/), manifest_sha256: 'a'.repeat(64), note: '' });
});

it('recovers a lost submission after closing and re-entering without submitting another training', async () => {
  options.items = [{ ...dataset, symbol_count: 20, training_ready: true }];
  loseSubmitResponse = true;
  render(<App><FactorModelPage /></App>);
  fireEvent.click(await screen.findByRole('button', { name: 'ETF训练' }));
  let dialog = await screen.findByRole('dialog', { name: 'ETF训练' });
  await waitFor(() => expect(within(dialog).getByRole('button', { name: '提交ETF训练' })).toBeEnabled());
  fireEvent.click(within(dialog).getByRole('button', { name: '提交ETF训练' }));
  await waitFor(() => expect(writes).toHaveLength(1));
  await waitFor(() => expect(within(dialog).getByText('已找到这次训练，不会重复提交。')).toBeVisible());
  fireEvent.click(within(dialog).getByRole('button', { name: /^关\s*闭$/ }));
  await waitFor(() => expect(screen.queryByRole('dialog', { name: 'ETF训练' })).not.toBeInTheDocument());
  fireEvent.click(screen.getByRole('button', { name: 'ETF训练' }));
  dialog = await screen.findByRole('dialog', { name: 'ETF训练' });
  await waitFor(() => expect(within(dialog).getByRole('button', { name: '查看这次训练' })).toBeEnabled());
  expect(within(dialog).queryByRole('button', { name: '提交ETF训练' })).not.toBeInTheDocument();
  fireEvent.click(within(dialog).getByRole('button', { name: '查看这次训练' }));
  expect(writes).toHaveLength(1);
});

it('starts another training only when the user explicitly starts a new one', async () => {
  options.items = [{ ...dataset, symbol_count: 20, training_ready: true }];
  loseSubmitResponse = true;
  render(<App><FactorModelPage /></App>);
  fireEvent.click(await screen.findByRole('button', { name: 'ETF训练' }));
  const dialog = await screen.findByRole('dialog', { name: 'ETF训练' });
  await waitFor(() => expect(within(dialog).getByRole('button', { name: '提交ETF训练' })).toBeEnabled());
  fireEvent.click(within(dialog).getByRole('button', { name: '提交ETF训练' }));
  fireEvent.click(await within(dialog).findByRole('button', { name: '开始新的训练' }));
  expect(writes).toHaveLength(1);
  loseSubmitResponse = false;
  await waitFor(() => expect(within(dialog).getByRole('button', { name: '提交ETF训练' })).toBeEnabled());
  fireEvent.click(within(dialog).getByRole('button', { name: '提交ETF训练' }));
  await waitFor(() => expect(writes).toHaveLength(2));
  expect(writes[1].body.request_id).not.toBe(writes[0].body.request_id);
});

it('retries an unreceived submission with the same parameters after the page is remounted', async () => {
  options.items = [{ ...dataset, symbol_count: 20, training_ready: true }];
  loseSubmitResponse = true; recoveryResult = 'not_found';
  render(<App><FactorModelPage /></App>);
  fireEvent.click(await screen.findByRole('button', { name: 'ETF训练' }));
  let dialog = await screen.findByRole('dialog', { name: 'ETF训练' });
  await waitFor(() => expect(within(dialog).getByRole('button', { name: '提交ETF训练' })).toBeEnabled());
  fireEvent.change(within(dialog).getByRole('textbox', { name: 'ETF训练备注' }), { target: { value: '  比较价量特征  ' } });
  fireEvent.click(within(dialog).getByRole('button', { name: '提交ETF训练' }));
  await waitFor(() => expect(within(dialog).getByRole('button', { name: '重试同一次训练' })).toBeEnabled());
  cleanup();
  options.items = [{ ...dataset, dataset_id: 'changed-input', manifest_sha256: 'f'.repeat(64) }];
  loseSubmitResponse = false;
  render(<App><FactorModelPage /></App>);
  fireEvent.click(await screen.findByRole('button', { name: 'ETF训练' }));
  dialog = await screen.findByRole('dialog', { name: 'ETF训练' });
  await waitFor(() => expect(within(dialog).getByRole('button', { name: '重试同一次训练' })).toBeEnabled());
  fireEvent.click(within(dialog).getByRole('button', { name: '重试同一次训练' }));
  await waitFor(() => expect(writes).toHaveLength(2));
  expect(writes[1].body).toEqual(writes[0].body);
  expect(writes[1].body.note).toBe('比较价量特征');
});

it.each(['error', 'mismatch'] as const)('blocks further submission when recovery returns %s', async mode => {
  options.items = [{ ...dataset, symbol_count: 20, training_ready: true }];
  loseSubmitResponse = true; recoveryResult = mode;
  render(<App><FactorModelPage /></App>);
  fireEvent.click(await screen.findByRole('button', { name: 'ETF训练' }));
  const dialog = await screen.findByRole('dialog', { name: 'ETF训练' });
  await waitFor(() => expect(within(dialog).getByRole('button', { name: '提交ETF训练' })).toBeEnabled());
  fireEvent.click(within(dialog).getByRole('button', { name: '提交ETF训练' }));
  await within(dialog).findByText(/暂时无法确认这次训练的结果/);
  expect(within(dialog).getByRole('button', { name: '重试同一次训练' })).toBeDisabled();
  expect(within(dialog).queryByRole('button', { name: '开始新的训练' })).not.toBeInTheDocument();
  recoveryResult = 'found';
  fireEvent.click(within(dialog).getByRole('button', { name: '重新读取训练结果' }));
  await waitFor(() => expect(within(dialog).getByRole('button', { name: '查看这次训练' })).toBeEnabled());
  expect(writes).toHaveLength(1);
});

it.each([408, 500])('recovers a server response of %i instead of starting another request', async status => {
  options.items = [{ ...dataset, symbol_count: 20, training_ready: true }]; submitStatus = status;
  render(<App><FactorModelPage /></App>);
  fireEvent.click(await screen.findByRole('button', { name: 'ETF训练' }));
  const dialog = await screen.findByRole('dialog', { name: 'ETF训练' });
  await waitFor(() => expect(within(dialog).getByRole('button', { name: '提交ETF训练' })).toBeEnabled());
  fireEvent.click(within(dialog).getByRole('button', { name: '提交ETF训练' }));
  await waitFor(() => expect(within(dialog).getByRole('button', { name: '查看这次训练' })).toBeEnabled());
  expect(writes).toHaveLength(1);
});

it.each(['queued', 'running', 'failed', 'cancelled'])('restores a %s task without automatically retrying it', async status => {
  options.items = [{ ...dataset, symbol_count: 20, training_ready: true }];
  recoveredStatus = status; loseSubmitResponse = true;
  render(<App><FactorModelPage /></App>);
  fireEvent.click(await screen.findByRole('button', { name: 'ETF训练' }));
  const dialog = await screen.findByRole('dialog', { name: 'ETF训练' });
  await waitFor(() => expect(within(dialog).getByRole('button', { name: '提交ETF训练' })).toBeEnabled());
  fireEvent.click(within(dialog).getByRole('button', { name: '提交ETF训练' }));
  await waitFor(() => expect(within(dialog).getByRole('button', { name: '查看这次训练' })).toBeEnabled());
  expect(writes).toHaveLength(1);
  expect(!!within(dialog).queryByRole('button', { name: '开始新的训练' })).toBe(['failed', 'cancelled'].includes(status));
});

it('allows correcting a rejected request only after confirming that no task was created', async () => {
  options.items = [{ ...dataset, symbol_count: 20, training_ready: true }];
  submitStatus = 400; recoveryResult = 'not_found';
  render(<App><FactorModelPage /></App>);
  fireEvent.click(await screen.findByRole('button', { name: 'ETF训练' }));
  const dialog = await screen.findByRole('dialog', { name: 'ETF训练' });
  await waitFor(() => expect(within(dialog).getByRole('button', { name: '提交ETF训练' })).toBeEnabled());
  fireEvent.click(within(dialog).getByRole('button', { name: '提交ETF训练' }));
  await within(dialog).findByText('ETF数量不足，暂不能训练。');
  await waitFor(() => expect(within(dialog).getByRole('button', { name: '提交ETF训练' })).toBeEnabled());
  expect(writes).toHaveLength(1);
});

it('does not submit when the browser cannot save the training request', async () => {
  options.items = [{ ...dataset, symbol_count: 20, training_ready: true }];
  render(<App><FactorModelPage /></App>);
  fireEvent.click(await screen.findByRole('button', { name: 'ETF训练' }));
  const dialog = await screen.findByRole('dialog', { name: 'ETF训练' });
  await waitFor(() => expect(within(dialog).getByRole('button', { name: '提交ETF训练' })).toBeEnabled());
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('Quota exceeded', 'QuotaExceededError'); });
  fireEvent.click(within(dialog).getByRole('button', { name: '提交ETF训练' }));
  await within(dialog).findByText(/无法保存或读取这次训练的记录/);
  expect(writes).toHaveLength(0);
  expect(within(dialog).getByRole('button', { name: '提交ETF训练' })).toBeDisabled();
});

it('keeps a pending request when navigation unmounts the page before the response arrives', async () => {
  options.items = [{ ...dataset, symbol_count: 20, training_ready: true }];
  let finishSubmit!: (value: Response) => void;
  heldSubmit = new Promise(resolve => { finishSubmit = resolve; });
  render(<App><FactorModelPage /></App>);
  fireEvent.click(await screen.findByRole('button', { name: 'ETF训练' }));
  let dialog = await screen.findByRole('dialog', { name: 'ETF训练' });
  await waitFor(() => expect(within(dialog).getByRole('button', { name: '提交ETF训练' })).toBeEnabled());
  fireEvent.click(within(dialog).getByRole('button', { name: '提交ETF训练' }));
  await waitFor(() => expect(writes).toHaveLength(1));
  cleanup();
  render(<App><FactorModelPage /></App>);
  fireEvent.click(await screen.findByRole('button', { name: 'ETF训练' }));
  dialog = await screen.findByRole('dialog', { name: 'ETF训练' });
  await waitFor(() => expect(within(dialog).getByRole('button', { name: '查看这次训练' })).toBeEnabled());
  finishSubmit(response({ id: 'etf-task', status: 'queued' }));
  await waitFor(() => expect(within(dialog).getByText('已找到这次训练，不会重复提交。')).toBeVisible());
  expect(writes).toHaveLength(1);
});
