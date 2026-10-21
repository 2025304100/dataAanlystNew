import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { StrictMode } from 'react';
import { App } from 'antd';
import UniverseDataPanel from '../UniverseDataPanel';

const stats = { total_symbols: 20, synced_symbols: 20, failed_symbols: 0, circuit_broken_symbols: 0,
  total_bars: 6000, by_type: {}, latest_synced_at: '2026-10-21T10:00:00', is_empty: false };
const task = { id: 'market-read-only', status: 'done', stage: 'done', percent: 100, total: 20,
  processed: 20, ok_count: 20, failed_count: 0, message: '行情更新完成。', result: null, errors: [] };
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
let calls: Array<{ url: string; method: string }>;
let progressStatus: () => Promise<Response>;
let statsStatus: () => Promise<Response>;
let failedProgressPath: string | null;
beforeEach(() => {
  failedProgressPath = null;
  localStorage.clear(); calls = [];
  progressStatus = async () => json(task);
  statsStatus = async () => json(stats);
  vi.stubGlobal('fetch', vi.fn(async (input, init) => {
    const url = String(input); calls.push({ url, method: init?.method || 'GET' });
    if (failedProgressPath && url.includes(failedProgressPath)) return json({ detail: 'progress offline' }, 503);
    if (url.includes('/universe/stats')) return statsStatus();
    if (url.includes('/universe/incremental-sync/status')) return progressStatus();
    if (url.includes('/universe/') && url.includes('/status')) return json(null);
    if (url.includes('/index-prices/status')) return json({ items: [] });
    throw new Error('Unexpected HTTP request: ' + url);
  }));
});
afterEach(() => { cleanup(); localStorage.clear(); vi.unstubAllGlobals(); });

it('keeps a failed market progress read visible and retries reading without starting an update', async () => {
  progressStatus = async () => json({ detail: 'Failed to fetch' }, 503);
  render(<App><UniverseDataPanel /></App>);
  expect(await screen.findByText('行情更新进度暂时读不到')).toBeVisible();
  expect(screen.getByRole('button', { name: /开始更新/ })).toBeDisabled();
  progressStatus = async () => json(task);
  fireEvent.click(screen.getByRole('button', { name: '重试读取更新进度' }));
  expect(await screen.findByText(/行情更新完成/)).toBeVisible();
  await waitFor(() => expect(screen.queryByText('行情更新进度暂时读不到')).not.toBeInTheDocument());
  expect(screen.getByRole('button', { name: /开始更新/ })).toBeEnabled();
  expect(calls.every(call => call.method === 'GET')).toBe(true);
});

it('explains an English task failure in daily Chinese and keeps the original in closed details', async () => {
  const original = 'Backend restarted before task completed';
  progressStatus = async () => json({ ...task, status: 'failed', message: original });
  render(<App><UniverseDataPanel /></App>);
  expect(await screen.findByText(/这次更新没有完成，请查看错误详情后重试/)).toBeVisible();
  expect(screen.getByText(original)).not.toBeVisible();
  fireEvent.click(screen.getByText('查看错误详情'));
  expect(screen.getByText(original)).toBeVisible();
  expect(calls.every(call => call.method === 'GET')).toBe(true);
});

it('restores an in-flight progress read after page re-entry without starting an update', async () => {
  let finishRead: ((value: Response) => void) | undefined;
  progressStatus = async () => new Promise<Response>(resolve => { finishRead = resolve; });
  render(<StrictMode><App><UniverseDataPanel /></App></StrictMode>);
  await waitFor(() => expect(finishRead).toBeDefined());
  await act(async () => finishRead?.(json(task)));
  expect(await screen.findByText(/行情更新完成/)).toBeVisible();
  expect(screen.getByRole('button', { name: /开始更新/ })).toBeEnabled();
  expect(screen.queryByText('行情更新进度暂时读不到')).not.toBeInTheDocument();
  expect(calls.every(call => call.method === 'GET')).toBe(true);
});

it('keeps market statistics errors in place with a read-only retry and hides the original error', async () => {
  statsStatus = async () => json({ detail: 'statistics offline' }, 503);
  render(<App><UniverseDataPanel /></App>);
  expect(await screen.findByText('行情数据状态暂时读不到')).toBeVisible();
  expect(screen.getByText('statistics offline')).not.toBeVisible();
  statsStatus = async () => json(stats);
  fireEvent.click(screen.getByRole('button', { name: '重试读取行情状态' }));
  await waitFor(() => expect(screen.queryByText('行情数据状态暂时读不到')).not.toBeInTheDocument());
  expect(calls.every(call => call.method === 'GET')).toBe(true);
});

it.each([
  { path: '/smart-sync/status', tab: null, panel: /补充历史并更新/, start: /开始补充并更新/, industry: false },
  { path: '/initialize/status', tab: /下载历史/, panel: null, start: /开始初始化同步/, industry: false },
  { path: '/backfill/status', tab: /下载历史/, panel: /补充更早行情/, start: /开始历史回补/, industry: false },
  { path: '/range-repair/status', tab: /检查数据/, panel: null, start: /开始区间修复/, industry: false },
  { path: '/industry-backfill/status', tab: /下载历史/, panel: /补充更早行情/, start: /开始行业补齐/, industry: true },
])('recovers the selected update page after a failed progress read: $path', async ({ path, tab, panel, start, industry }) => {
  failedProgressPath = path;
  render(<App><UniverseDataPanel /></App>);
  if (tab) fireEvent.click(screen.getByRole('tab', { name: tab }));
  else fireEvent.click(screen.getByText('其他更新方式'));
  if (panel) fireEvent.click(await screen.findByRole('button', { name: panel }));
  const message = industry ? '行业资料更新进度暂时读不到' : '行情更新进度暂时读不到';
  expect(await screen.findByText(message)).toBeVisible();
  expect(screen.getByRole('button', { name: start })).toBeDisabled();
  failedProgressPath = null;
  fireEvent.click(screen.getByRole('button', { name: industry ? '重试读取行业资料进度' : '重试读取更新进度' }));
  await waitFor(() => expect(screen.queryByText(message)).not.toBeInTheDocument());
  expect(screen.getByRole('button', { name: start })).toBeEnabled();
  expect(calls.every(call => call.method === 'GET')).toBe(true);
});
