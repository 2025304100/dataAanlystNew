import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { App } from 'antd';
import { AppProvider } from '../../context/AppContext';
import TaskCenter from '../TaskCenter';
vi.mock('../ai/ExplainButton', () => ({ default: () => null }));

const record = { id: 'flow-history', source: 'async', task_type: 'external_sync_capital_flow', status: 'done',
  stage: 'done', percent: 100, processed: 20, total: 20, ok_count: 20, failed_count: 0,
  message: '资金流更新完成。', payload: {}, result: {}, errors: [], created_at: '2026-10-21T10:00:00' };
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
let calls: Array<{ url: string; method: string }>;
let history: () => Promise<Response>;
let observation: () => Promise<Response>;
beforeEach(() => {
  localStorage.clear(); calls = [];
  history = async () => json({ tasks: [record] });
  observation = async () => json({ domains: {}, active_tasks: [] });
  vi.stubGlobal('fetch', vi.fn(async (input, init) => {
    const url = String(input); calls.push({ url, method: init?.method || 'GET' });
    if (url.includes('/system/task-observability')) return observation();
    if (url.includes('/system/tasks?')) return history();
    if (url.endsWith('/portfolios')) return json([]);
    if (url.includes('/system/capabilities')) return json({ capabilities: [] });
    if (url.includes('/notifications')) return json({ items: [], total: 0 });
    return json([]);
  }));
});
afterEach(() => { cleanup(); localStorage.clear(); vi.unstubAllGlobals(); });
const openPage = () => render(<App><AppProvider><TaskCenter /></AppProvider></App>);

it('shows an explicit zero-unit holiday skip without calling it successful or inventing skipped units', async () => {
  history = async () => json({ tasks: [{ ...record, total: 0, processed: 0, ok_count: 0,
    message: '所选日期范围均为 A 股休市日，无需更新',
    result: { outcome: 'skipped', success: 0, skipped: 0, failed: 0, records: 0, skip_reason: 'market_closed' } }] });
  openPage();
  expect(await screen.findByText('已跳过，未新增数据')).toBeVisible();
  expect(screen.getByText('成功 0 / 跳过 0 / 失败 0')).toBeVisible();
  fireEvent.click(screen.getByText('同步资金流'));
  expect(await screen.findByText('所选日期范围均为 A 股休市日，无需更新')).toBeVisible();
  expect(calls.every(call => call.method === 'GET')).toBe(true);
});

it('shows partial success while keeping failed tasks available for retry filtering', async () => {
  history = async () => json({ tasks: [{ ...record, status: 'failed', ok_count: 5549, failed_count: 6,
    total: 5555, processed: 5555, result: { outcome: 'partial_success', success: 5549, skipped: 0, failed: 6 } }] });
  openPage();
  expect(await screen.findByText('部分完成')).toBeVisible();
  expect(screen.getByText('成功 5549 / 跳过 0 / 失败 6')).toBeVisible();
  expect(screen.getByText('已处理 5555/5555')).toBeVisible();
  expect(calls.every(call => call.method === 'GET')).toBe(true);
});

it('exposes mass skips instead of presenting processed counts as coverage', async () => {
  history = async () => json({ tasks: [{ ...record, total: 1642, processed: 1642, ok_count: 11,
    result: { outcome: 'success', success: 11, skipped: 1631, failed: 0 } }] });
  openPage();
  expect(await screen.findByText('成功 11 / 跳过 1631 / 失败 0')).toBeVisible();
  expect(screen.getByText('超过一半已跳过，请核对覆盖与跳过原因')).toBeVisible();
  expect(calls.every(call => call.method === 'GET')).toBe(true);
});

it('keeps task records visible when runtime statistics fail and retries only those statistics', async () => {
  observation = async () => json({ detail: 'statistics unavailable' }, 503);
  openPage();
  expect(await screen.findByText('同步资金流')).toBeVisible();
  expect(await screen.findByText('运行统计暂时读不到')).toBeVisible();
  observation = async () => json({ domains: { market: { running: 0, slot_limit: 1, waiting: 2 } }, active_tasks: [] });
  const before = calls.filter(call => call.url.includes('/system/tasks?')).length;
  fireEvent.click(screen.getByRole('button', { name: '重试读取运行统计' }));
  await waitFor(() => expect(screen.queryByText('运行统计暂时读不到')).not.toBeInTheDocument());
  expect(screen.getByText('同步资金流')).toBeVisible();
  expect(screen.getByText('行情更新：0项运行，2项等待')).toBeVisible();
  expect(calls.filter(call => call.url.includes('/system/tasks?'))).toHaveLength(before);
  expect(calls.every(call => call.method === 'GET')).toBe(true);
});

it('shows task records while a statistics request is still waiting', async () => {
  let finishStatistics: ((value: Response) => void) | undefined;
  observation = async () => new Promise<Response>(resolve => { finishStatistics = resolve; });
  openPage();
  expect(await screen.findByText('同步资金流')).toBeVisible();
  await act(async () => finishStatistics?.(json({ domains: {}, active_tasks: [] })));
  expect(screen.getByText('同步资金流')).toBeVisible();
  expect(calls.every(call => call.method === 'GET')).toBe(true);
});

it('explains an initial task read failure instead of showing no records and retries only records', async () => {
  history = async () => json({ detail: 'history offline' }, 503);
  openPage();
  expect(await screen.findByText('任务记录暂时读不到')).toBeVisible();
  expect(screen.queryByText('暂无任务记录')).not.toBeInTheDocument();
  expect(screen.getByText('history offline')).not.toBeVisible();
  const statisticsReads = calls.filter(call => call.url.includes('/task-observability')).length;
  history = async () => json({ tasks: [record] });
  fireEvent.click(screen.getByRole('button', { name: '重试读取任务记录' }));
  expect(await screen.findByText('同步资金流')).toBeVisible();
  expect(screen.queryByText('任务记录暂时读不到')).not.toBeInTheDocument();
  expect(calls.filter(call => call.url.includes('/task-observability'))).toHaveLength(statisticsReads);
  expect(calls.every(call => call.method === 'GET')).toBe(true);
});

it('keeps existing records after a refresh failure and clears only the recovered section error', async () => {
  openPage();
  await screen.findByText('同步资金流');
  history = async () => json({ detail: 'history offline' }, 503);
  observation = async () => json({ detail: 'statistics offline' }, 503);
  fireEvent.click(screen.getByRole('button', { name: /刷新/ }));
  expect(await screen.findByText('任务记录暂时读不到')).toBeVisible();
  expect(await screen.findByText('运行统计暂时读不到')).toBeVisible();
  expect(screen.getByText('同步资金流')).toBeVisible();
  expect(screen.getByText(/下方保留上次读到的任务记录/)).toBeVisible();
  history = async () => json({ tasks: [record] });
  fireEvent.click(screen.getByRole('button', { name: '重试读取任务记录' }));
  await waitFor(() => expect(screen.queryByText('任务记录暂时读不到')).not.toBeInTheDocument());
  expect(screen.getByText('运行统计暂时读不到')).toBeVisible();
  expect(calls.every(call => call.method === 'GET')).toBe(true);
});

it.each([null, { tasks: null }])('does not treat an incomplete history response as an empty list: %j', async value => {
  history = async () => json(value);
  openPage();
  expect(await screen.findByText('任务记录暂时读不到')).toBeVisible();
  expect(screen.queryByText('暂无任务记录')).not.toBeInTheDocument();
  expect(calls.every(call => call.method === 'GET')).toBe(true);
});
