import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App as AntdApp, message } from "antd";
import App from "../../App";
import "../../components/portfolio-trading/PortfolioTradingShell";
import "../../components/Settings";
import { AppProvider } from "../AppContext";
import { setLocale } from "../../i18n";

const portfolios = [
  { id: 1, name: "本地组合甲", is_default: 1, account_type: "simulated", asset_scope: "mixed", currency: "CNY", total_capital: 100000, cash: 100000, auto_trade_enabled: 0 },
  { id: 2, name: "本地组合乙", is_default: 0, account_type: "simulated", asset_scope: "mixed", currency: "CNY", total_capital: 100000, cash: 100000, auto_trade_enabled: 0 },
];
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
const requests: Array<{ url: string; method: string; body: any }> = [];
const clearPrecheck = {
  requested_trade_days: 250, usable_trade_days: 250, minimum_trade_days: 240,
  excluded_symbol_days: {}, warnings: [], blocking_reasons: [], filter_config_hash: "test-only", production_fidelity: true,
};
let precheck: any;
let oldHistory = false;
let checkFailure = false;
let otherHistoryRunning = false;
let checkReply: (() => Promise<Response>) | null = null;
let singleRunDetail: Record<string, unknown> | null = null;
let singleRunReply: (() => Promise<Response>) | null = null;
let singleDetailReply: ((runId: number, signal?: AbortSignal | null) => Promise<Response>) | null = null;
let singleTradesReply: ((runId: number, page: number) => Promise<Response>) | null = null;
let historyRuns: Record<string, unknown>[] = [];
let symbolReply: (() => Promise<Response>) | null = null;
const pendingResponses: Array<(response: Response) => void> = [];
const symbol = { id: 11, symbol: "510300", name: "测试ETF", asset_type: "etf", market: "sh", region: "cn" };

beforeEach(() => {
  localStorage.clear(); sessionStorage.clear(); setLocale("zh-CN"); requests.length = 0;
  precheck = clearPrecheck;
  oldHistory = false;
  checkFailure = false;
  otherHistoryRunning = false;
  checkReply = null;
  singleRunDetail = null;
  singleRunReply = null; singleDetailReply = null; symbolReply = null;
  singleTradesReply = null;
  historyRuns = [];
  window.history.replaceState(null, "", "/?tab=portfolio&subtab=backtest");
  // The real app, context, forms and destination pages stay mounted together.
  // Only HTTP is replaced; no account, model, download or backtest reaches the live server.
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    requests.push({ url, method: init?.method || "GET", body: init?.body ? JSON.parse(String(init.body)) : null });
    if (url === "/api/v1/portfolios") return json(portfolios);
    if (url.includes("/dashboard/workbench?")) {
      const portfolio = portfolios[Number(new URL(url, "http://test").searchParams.get("portfolio_id")) === 2 ? 1 : 0];
      return json({ portfolio, candidates: [], latest_scores: [], positions: [], actions: [], watchlists: [], active_rule: null });
    }
    if (url.endsWith("/backtest/precheck")) return checkReply ? checkReply() : checkFailure ? json({ detail: "临时连接失败" }, 503) : json(precheck);
    if (url.includes("/symbols?") && url.includes("keyword=510300")) return symbolReply ? symbolReply() : json([symbol]);
    if (url.endsWith("/backtest/run")) return singleRunReply ? singleRunReply() : singleRunDetail ? json(singleRunDetail) : json({ detail: { code: "BACKTEST_SCORE_COVERAGE_INSUFFICIENT", message: "评分资料不足" } }, 422);
    if (url.includes("/backtest/runs?")) return json(historyRuns);
    if (/\/backtest\/runs\/(77|78)$/.test(url)) return singleDetailReply ? singleDetailReply(Number(url.split("/").pop()), init?.signal) : singleRunDetail ? json(singleRunDetail) : json({ detail: "测试记录不存在" }, 404);
    const tradeUrl = new URL(url, "http://test");
    const tradeRun = tradeUrl.pathname.match(/\/backtest\/runs\/(77|78)\/trades$/);
    if (tradeRun) {
      const page = Number(tradeUrl.searchParams.get("page") || 1);
      return singleTradesReply ? singleTradesReply(Number(tradeRun[1]), page) : json({ total: 0, page, page_size: 20, items: [] });
    }
    if (url.includes("/symbols?")) return json([symbol, { id: 99, symbol: "600010", name: "旧任务标的" }]);
    if (url.endsWith("/market-data/initialize-history/status")) return json({ status: otherHistoryRunning ? "running" : "idle", task_id: otherHistoryRunning ? "other-running" : undefined, start_date: "2020-01-01", end_date: "2020-02-01", repair_mode: "bars", preset: "1m", symbol_ids: [99], stages: [], summary: {}, recent_runs: oldHistory ? [{ task_id: "old-history", status: "failed", preset: "1m", repair_mode: "bars", symbol_ids: [99], start_date: "2020-01-01", end_date: "2020-02-01", started_at: "2020-02-02T00:00:00", finished_at: "2020-02-02T00:01:00", stages: [], summary: {}, failed_items: [{ symbol_id: 99, symbol: "600010", stage: "sync_bars", message: "旧任务失败" }] }] : [], failed_items: [] });
    if (url.endsWith("/market-data/initialize-history")) return json({ status: "completed", preset: "1y", task_id: "isolated-test", stages: [], summary: {}, recent_runs: [], failed_items: [] });
    if (url.includes("/security-status/download-scope")) return json({ portfolio_id: 1, portfolio_name: "本地组合甲", symbols: [{ symbol_id: 11, symbol: "510300", name: "测试ETF", supported: true }], scope_sha256: "a".repeat(64), start_date: "2025-10-20", end_date: "2026-10-20", max_symbols: 20, max_days: 366, can_download: true, reasons: [], latest_task: null });
    if (url.includes("/security-status/template")) return json({ csv_text: "", max_rows: 10000, max_bytes: 1000000, instructions: [] });
    if (url.includes("/external-data/quality-snapshots")) return json({ fields: [] });
    if (url.includes("/external-data/coverage")) return json({ datasets: [] });
    if (url.endsWith("/backtest/source-status")) return json({ enabled: true, source_label: "members" });
    if (url.includes("/system/capabilities")) return json({ capabilities: [] });
    if (url.includes("/scoring/overview")) return json({ feature_enabled: false, config: {}, runtime: { weight_mode: "manual", version: 1 }, health: {} });
    if (url.includes("/scoring/tasks?")) return json({ items: [], total: 0 });
    if (url.includes("/system/data-health") || url.includes("/macro/overview")) return json(null);
    if (url.includes("/market-events")) return json({ events: [] });
    if (url.endsWith("/status")) return json({ current_state: "READY", permissions: { allow_new_buys: false, allow_risk_exits: false, allow_auto_recovery: false, requires_manual_ack: false } });
    if (url.includes("/action-items")) return json({ summary: { ready: false, blockers: 0 }, checks: {}, items: [] });
    if (url.includes("/signal-rule") && !url.includes("/presets")) return json({});
    if (url.includes("/scoring-configs/active")) return json(null);
    if ((init?.method || "GET") !== "GET") throw new Error(`Unexpected write: ${url}`);
    return json([]);
  }));
});

it("carries the single-symbol repair range and missing type and returns to the saved form with a fresh check only", async () => {
  oldHistory = true;
  localStorage.setItem("backtest-parameters:1", JSON.stringify({ version: 1, portfolioId: 1, parameters: { singleIndicator: "RSI" } }));
  const firstView = openApp();
  fireEvent.click(await screen.findByRole("button", { name: "回测中心" }));
  fireEvent.click(await screen.findByRole("button", { name: "单标的回测" }));
  fireEvent.change(screen.getByPlaceholderText("输入代码，如 600519"), { target: { value: "510300" } });
  fireEvent.click(screen.getByTestId("single-backtest-range-3y"));
  expect(screen.getByRole("region", { name: "本次买卖条件" })).toHaveTextContent("质量评分不低于30分");
  fireEvent.change(screen.getByPlaceholderText("100,000"), { target: { value: "222000" } });
  fireEvent.change(screen.getByDisplayValue("0.03"), { target: { value: "0.07" } });
  const dates = screen.getAllByTestId("single-backtest-range-picker").map((input) => (input as HTMLInputElement).value);
  const start = await screen.findByRole("button", { name: "开始回测" });
  fireEvent.click(start); fireEvent.click(start);
  fireEvent.click(await screen.findByRole("button", { name: "去补资料" }));
  let notice = await screen.findByRole("region", { name: "本次回测需要的资料" });
  expect(notice).toHaveTextContent("本地组合甲");
  expect(notice).toHaveTextContent("510300");
  expect(notice).toHaveTextContent("历史评分");
  expect(notice).toHaveTextContent(dates[0]); expect(notice).toHaveTextContent(dates[1]);
  await screen.findAllByText("2020-01-01 ~ 2020-02-01");
  expect(screen.queryByRole("button", { name: "仅重试失败项" })).not.toBeInTheDocument();
  firstView.unmount();
  window.history.replaceState(null, "", "/?tab=settings");
  openApp();
  notice = await screen.findByRole("region", { name: "本次回测需要的资料" });
  expect(notice).toHaveTextContent("510300");
  // Opening the repair page does not start a download or retry the failed backtest.
  expect(requests.filter((request) => request.url.endsWith("/market-data/initialize-history"))).toHaveLength(0);
  fireEvent.click(screen.getByRole("button", { name: /补充所需资料/ }));
  await waitFor(() => expect(requests.find((request) => request.url.endsWith("/market-data/initialize-history"))?.body).toMatchObject({
    portfolio_id: 1, symbol_ids: [11], start_date: dates[0], end_date: dates[1], repair_mode: "scores", auto_scan: false,
  }));
  const checksBeforeReturn = requests.filter((request) => request.url.endsWith("/backtest/precheck")).length;
  checkFailure = true;
  fireEvent.click(within(notice).getByRole("button", { name: "返回回测并检查资料" }));
  expect(await screen.findByPlaceholderText("输入代码，如 600519")).toHaveValue("510300");
  expect(screen.getByPlaceholderText("100,000")).toHaveValue("222000");
  expect(screen.getByRole("region", { name: "本次买卖条件" })).toHaveTextContent("质量评分不低于30分");
  expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  expect(screen.getByDisplayValue("0.07")).toBeVisible();
  expect(screen.getAllByTestId("single-backtest-range-picker").map((input) => (input as HTMLInputElement).value)).toEqual(dates);
  await waitFor(() => expect(requests.filter((request) => request.url.endsWith("/backtest/precheck")).length).toBeGreaterThan(checksBeforeReturn));
  expect(requests.filter((request) => request.url.endsWith("/backtest/precheck")).slice(-1)[0]?.body).toMatchObject({ portfolio_id: 1, symbol_ids: [11], start_date: dates[0], end_date: dates[1] });
  const retryCheck = await screen.findByRole("button", { name: "重新检查资料" });
  await waitFor(() => expect(retryCheck).toBeEnabled());
  checkFailure = false;
  fireEvent.click(retryCheck);
  await screen.findByText("资料检查已完成。开始回测仍需手动点击。");
  expect(requests.filter((request) => request.url.endsWith("/backtest/run"))).toHaveLength(1);
  expect(requests.filter((request) => request.url.endsWith("/backtest/portfolio/run"))).toHaveLength(0);
});

afterEach(async () => {
  cleanup();
  await act(async () => { for (const resolve of pendingResponses.splice(0)) resolve(json({ detail: "测试已结束" }, 503)); message.destroy(); });
  vi.useRealTimers();
  vi.restoreAllMocks(); vi.unstubAllGlobals();
});

function openApp() {
  return render(<AntdApp><AppProvider><App /></AppProvider></AntdApp>);
}

function completedSingleRun(overrides: Record<string, unknown> = {}) {
  return {
    id: 77, portfolio_id: 1, run_name: "510300-评分回测", symbols_json: "[11]", rule_config_json: "{}",
    start_date: "2025-10-20", end_date: "2026-10-20", initial_capital: 100000,
    status: "completed", created_at: "2026-10-21T12:00:00", total_return_pct: 0.1234,
    ...overrides,
  };
}

function historicalSingleRun(overrides: Record<string, unknown> = {}) {
  return completedSingleRun({
    run_name: "已保存的单标的记录", initial_capital: 80000,
    rule_config_json: JSON.stringify({
      buy_conditions: { quality_score_min: 65, timing_score_min: 75, actions: ["buy", "open"], stages: [] },
      sell_conditions: { take_profit_pct: 0.15, stop_loss_pct: 0.06, max_hold_days: 25, score_actions: ["exit", "reduce"] },
      position_config: { type: "fixed_pct", value: 1, max_positions: 1 },
      execution_config: { entry_timing: "next_open", exit_timing: "next_open", entry_price_field: "open", exit_price_field: "open" },
    }),
    cost_config_json: JSON.stringify({ commission_rate: 0.0007, min_commission: 7, slippage_rate: 0.0025 }),
    // The top-level compatibility fields are not the original cost snapshot.
    commission_rate: 0.0003, slippage_bps: 5,
    ...overrides,
  });
}

function deferredResponse() {
  let resolve!: (response: Response) => void;
  const promise = new Promise<Response>((done) => { resolve = done; });
  pendingResponses.push(resolve);
  return { promise, resolve };
}

function closedSingleTrade(overrides: Record<string, unknown> = {}) {
  return { id: 901, run_id: 77, symbol_id: 11, entry_date: "2026-01-02", entry_price: 3.1234,
    quantity: 1000, entry_cost: 5.23, exit_date: "2026-01-08", exit_price: 3.4321, exit_cost: 6.34,
    pnl: 297.13, ...overrides };
}

function singleTradePage(page: number, runId = 77) {
  return { total: 21, page, page_size: 20, items: page === 1
    ? Array.from({ length: 20 }, (_, index) => closedSingleTrade({ id: 1000 + index, run_id: runId,
      entry_date: `2026-03-${String(20 - index).padStart(2, "0")}` }))
    : [closedSingleTrade({ id: 1020, run_id: runId, entry_date: "2026-02-28", entry_price: 9.8765 })] };
}

async function openSingleForm() {
  localStorage.setItem("backtest-parameters:1", JSON.stringify({ version: 1, portfolioId: 1, parameters: {
    singleSymbol: "510300", singleStart: "2025-10-20", singleEnd: "2026-10-20",
  } }));
  const view = openApp();
  fireEvent.click(await screen.findByRole("button", { name: "回测中心" }));
  fireEvent.click(await screen.findByRole("button", { name: "单标的回测" }));
  await screen.findByRole("button", { name: "开始回测" });
  return view;
}

it("reads actual single-symbol trade pages and shows both sides, recorded fees and open holdings", async () => {
  singleRunDetail = completedSingleRun();
  singleTradesReply = async () => json({ total: 2, page: 1, page_size: 20, items: [
    closedSingleTrade({ id: 902, entry_date: "2026-02-02", entry_price: 4.0123, quantity: 2000,
      entry_cost: 8.02, exit_date: null, exit_price: null, exit_cost: null, pnl: 666 }),
    closedSingleTrade(),
  ] });
  await openSingleForm();
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  const table = screen.getByRole("table");
  const closedRow = (await within(table).findByText("2026-01-02")).closest("tr")!;
  for (const value of ["2026-01-08", "¥3.1234", "¥3.4321", "1000", "¥5.23", "¥6.34", "+¥297.13", "已卖出"]) {
    expect(within(closedRow).getByText(value, { exact: true })).toBeVisible();
  }
  const holdingRow = within(table).getByText("2026-02-02").closest("tr")!;
  expect(holdingRow).toHaveTextContent("¥4.0123");
  expect(holdingRow).toHaveTextContent("¥8.02");
  expect(holdingRow).toHaveTextContent("持有中");
  expect(holdingRow).not.toHaveTextContent("666");
  expect(within(holdingRow).getAllByText("—", { exact: true })).toHaveLength(4);
  expect(within(table).getByRole("columnheader", { name: "盈亏（已扣费用）" })).toBeVisible();
  const read = requests.find((request) => request.url.includes("/backtest/runs/77/trades?"))!;
  expect(Object.fromEntries(new URL(read.url, "http://test").searchParams)).toEqual({ page: "1", page_size: "20", sort_by: "signal_at", sort_dir: "desc" });
  expect(requests.filter((request) => request.url.endsWith("/backtest/run"))).toHaveLength(1);
});

it("distinguishes single-symbol trade loading, failure and a confirmed empty page and retries in place", async () => {
  singleRunDetail = completedSingleRun();
  const firstRead = deferredResponse();
  singleTradesReply = () => firstRead.promise;
  await openSingleForm();
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  const region = screen.getByRole("region", { name: "单标的交易明细" });
  expect(await within(region).findByText("正在读取交易明细…")).toBeVisible();
  expect(region).not.toHaveTextContent("这次回测没有交易记录");
  await act(async () => firstRead.resolve(json({ detail: "交易明细连接中断" }, 503)));
  expect(await within(region).findByRole("alert")).toHaveTextContent("交易明细读取失败");
  expect(region).not.toHaveTextContent("这次回测没有交易记录");
  expect(screen.getByText("+12.3%")).toBeVisible();
  const writes = requests.filter((request) => request.method !== "GET").length;
  singleTradesReply = async () => json({ total: 0, page: 1, page_size: 20, items: [] });
  fireEvent.click(within(region).getByRole("button", { name: "重新读取交易明细" }));
  expect(await within(region).findByText("这次回测没有交易记录。")).toBeVisible();
  expect(within(region).queryByRole("alert")).not.toBeInTheDocument();
  expect(requests.filter((request) => request.url.includes("/backtest/runs/77/trades?"))).toHaveLength(2);
  expect(requests.filter((request) => request.method !== "GET")).toHaveLength(writes);
  expect(requests.filter((request) => request.url.endsWith("/backtest/run"))).toHaveLength(1);
});

it("pages single-symbol trades using the API total and retries the failed page without rerunning the backtest", async () => {
  singleRunDetail = completedSingleRun({ trade_count: 999 });
  const secondPage = deferredResponse();
  singleTradesReply = async (_id, page) => page === 2 ? secondPage.promise : json(singleTradePage(page));
  await openSingleForm();
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  const region = screen.getByRole("region", { name: "单标的交易明细" });
  expect(await within(region).findByText("共 21 条，第 1 / 2 页")).toBeVisible();
  expect(within(region).getByRole("button", { name: "上一页" })).toBeDisabled();
  const next = within(region).getByRole("button", { name: "下一页" });
  fireEvent.click(next); fireEvent.click(next);
  expect(await within(region).findByText("正在读取交易明细…")).toBeVisible();
  expect(within(region).queryByText("2026-03-20")).not.toBeInTheDocument();
  expect(within(region).getByRole("button", { name: "下一页" })).toBeDisabled();
  await act(async () => secondPage.resolve(json({ detail: "第二页连接中断" }, 503)));
  await within(region).findByRole("alert");
  expect(region).not.toHaveTextContent("这次回测没有交易记录");
  expect(within(region).getByRole("button", { name: "上一页" })).toBeEnabled();
  const writes = requests.filter((request) => request.method !== "GET").length;
  singleTradesReply = async (_id, page) => json(singleTradePage(page));
  fireEvent.click(within(region).getByRole("button", { name: "重新读取交易明细" }));
  expect(await within(region).findByText("2026-02-28")).toBeVisible();
  expect(region).toHaveTextContent("共 21 条，第 2 / 2 页");
  expect(within(region).getByRole("button", { name: "下一页" })).toBeDisabled();
  fireEvent.click(within(region).getByRole("button", { name: "上一页" }));
  expect(await within(region).findByText("2026-03-20")).toBeVisible();
  expect(within(region).queryByText("2026-02-28")).not.toBeInTheDocument();
  expect(requests.filter((request) => request.url.includes("/backtest/runs/77/trades?")).map((request) => new URL(request.url, "http://test").searchParams.get("page"))).toEqual(["1", "2", "2", "1"]);
  expect(requests.filter((request) => request.method !== "GET")).toHaveLength(writes);
  expect(requests.filter((request) => request.url.endsWith("/backtest/run"))).toHaveLength(1);
});

it("starts a fresh trade read after parameters change back and ignores the old page response", async () => {
  singleRunDetail = completedSingleRun();
  const oldPage = deferredResponse();
  const newPage = deferredResponse();
  let reads = 0;
  singleTradesReply = () => ++reads === 1 ? oldPage.promise : newPage.promise;
  await openSingleForm();
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  await waitFor(() => expect(reads).toBe(1));
  const capital = screen.getByRole("textbox", { name: "单标的初始资金" });
  fireEvent.change(capital, { target: { value: "200000" } });
  fireEvent.change(capital, { target: { value: "100000" } });
  await waitFor(() => expect(reads).toBe(2));
  await act(async () => newPage.resolve(json({ total: 1, page: 1, page_size: 20,
    items: [closedSingleTrade({ id: 902, entry_date: "2026-06-18" })] })));
  const region = screen.getByRole("region", { name: "单标的交易明细" });
  expect(await within(region).findByText("2026-06-18")).toBeVisible();
  await act(async () => oldPage.resolve(json({ detail: "旧页面连接失败" }, 503)));
  expect(within(region).getByText("2026-06-18")).toBeVisible();
  expect(within(region).queryByRole("alert")).not.toBeInTheDocument();
  expect(requests.filter((request) => request.url.endsWith("/backtest/run"))).toHaveLength(1);
});

it("preserves zero fees and net profit, missing exit values and recorded quantity precision without using embedded trades", async () => {
  singleRunDetail = completedSingleRun({ trades: [closedSingleTrade({ entry_date: "2020-01-01", pnl: 9999 })] });
  singleTradesReply = async () => json({ total: 2, page: 1, page_size: 20, items: [
    closedSingleTrade({ entry_date: "2026-04-01", exit_date: "2026-04-02", entry_cost: 0, exit_cost: 0, pnl: 0, quantity: 100.25 }),
    closedSingleTrade({ id: 902, entry_date: "2026-05-01", exit_date: "2026-05-02", exit_price: null, exit_cost: null, pnl: null }),
  ] });
  await openSingleForm();
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  const region = screen.getByRole("region", { name: "单标的交易明细" });
  const zeroRow = (await within(region).findByText("2026-04-01")).closest("tr")!;
  expect(within(zeroRow).getByText("100.25", { exact: true })).toBeVisible();
  expect(within(zeroRow).getAllByText("¥0.00", { exact: true })).toHaveLength(3);
  const missingRow = within(region).getByText("2026-05-01").closest("tr")!;
  expect(within(missingRow).getAllByText("—", { exact: true })).toHaveLength(3);
  expect(missingRow).toHaveTextContent("已卖出");
  expect(region).not.toHaveTextContent("2020-01-01");
  expect(region).not.toHaveTextContent("9999");
});

it.each([
  { problem: "unstructured response", payload: [] },
  { problem: "rows missing despite a positive total", payload: { total: 1, page: 1, page_size: 20, items: [] } },
  { problem: "incomplete first page", payload: { total: 21, page: 1, page_size: 20, items: [closedSingleTrade()] } },
  { problem: "wrong page", payload: { total: 1, page: 2, page_size: 20, items: [closedSingleTrade()] } },
  { problem: "another run", payload: { total: 1, page: 1, page_size: 20, items: [closedSingleTrade({ run_id: 78 })] } },
  { problem: "duplicate records", payload: { total: 2, page: 1, page_size: 20, items: [closedSingleTrade(), closedSingleTrade()] } },
  { problem: "negative total", payload: { total: -1, page: 1, page_size: 20, items: [] } },
  { problem: "missing record number", payload: { total: 1, page: 1, page_size: 20, items: [closedSingleTrade({ id: null })] } },
])("treats $problem as a trade read failure and lets the same page recover", async ({ payload }) => {
  singleRunDetail = completedSingleRun();
  singleTradesReply = async () => json(payload);
  await openSingleForm();
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  const region = screen.getByRole("region", { name: "单标的交易明细" });
  expect(await within(region).findByRole("alert")).toHaveTextContent("交易明细读取失败");
  expect(region).not.toHaveTextContent("这次回测没有交易记录");
  const writes = requests.filter((request) => request.method !== "GET").length;
  singleTradesReply = async () => json({ total: 1, page: 1, page_size: 20, items: [closedSingleTrade()] });
  fireEvent.click(within(region).getByRole("button", { name: "重新读取交易明细" }));
  expect(await within(region).findByText("2026-01-02")).toBeVisible();
  expect(within(region).queryByRole("alert")).not.toBeInTheDocument();
  expect(requests.filter((request) => request.method !== "GET")).toHaveLength(writes);
  expect(requests.filter((request) => request.url.includes("/backtest/runs/77/trades?"))).toHaveLength(2);
});

it("reads trades independently of result detail recovery without reading them twice", async () => {
  singleRunDetail = completedSingleRun();
  singleDetailReply = async () => json({ detail: "结果详情连接中断" }, 503);
  singleTradesReply = async () => json({ total: 1, page: 1, page_size: 20, items: [closedSingleTrade()] });
  await openSingleForm();
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  const region = screen.getByRole("region", { name: "单标的交易明细" });
  expect(await within(region).findByText("2026-01-02")).toBeVisible();
  const retryResult = await screen.findByRole("button", { name: "重新读取结果" });
  await waitFor(() => expect(retryResult).toBeEnabled());
  singleDetailReply = async () => json(completedSingleRun({ total_return_pct: 0.25 }));
  fireEvent.click(retryResult);
  await screen.findByText("+25.0%");
  expect(within(region).getByText("2026-01-02")).toBeVisible();
  expect(requests.filter((request) => request.url.includes("/backtest/runs/77/trades?"))).toHaveLength(1);
  expect(requests.filter((request) => request.url.endsWith("/backtest/run"))).toHaveLength(1);
});

it.each(["success", "failure"])("hides trades and ignores a late %s after parameters are changed", async (outcome) => {
  singleRunDetail = completedSingleRun();
  const oldPage = deferredResponse();
  singleTradesReply = () => oldPage.promise;
  await openSingleForm();
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  await waitFor(() => expect(requests.some((request) => request.url.includes("/backtest/runs/77/trades?"))).toBe(true));
  const capital = screen.getByRole("textbox", { name: "单标的初始资金" });
  fireEvent.change(capital, { target: { value: "200000" } });
  const region = screen.getByRole("region", { name: "单标的交易明细" });
  expect(region).toHaveTextContent("参数已修改，上次交易明细已收起");
  await act(async () => oldPage.resolve(outcome === "success" ? json({ total: 1, page: 1, page_size: 20, items: [closedSingleTrade()] }) : json({ detail: "旧明细读取失败" }, 503)));
  expect(within(region).queryByText("2026-01-02")).not.toBeInTheDocument();
  expect(within(region).queryByRole("alert")).not.toBeInTheDocument();
  singleTradesReply = async () => json({ total: 1, page: 1, page_size: 20, items: [closedSingleTrade({ entry_date: "2026-07-01" })] });
  fireEvent.change(capital, { target: { value: "100000" } });
  expect(await within(region).findByText("2026-07-01")).toBeVisible();
  expect(requests.filter((request) => request.url.endsWith("/backtest/run"))).toHaveLength(1);
});

it.each(["old-first-success", "new-first-failure"])("protects another portfolio's trades when requests arrive %s", async (order) => {
  singleRunDetail = completedSingleRun();
  const oldPage = deferredResponse();
  const newPage = deferredResponse();
  singleTradesReply = (id) => id === 77 ? oldPage.promise : newPage.promise;
  await openSingleForm();
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  await waitFor(() => expect(requests.some((request) => request.url.includes("/backtest/runs/77/trades?"))).toBe(true));
  fireEvent.click(screen.getByRole("button", { name: /本地组合甲/ }));
  fireEvent.click(await screen.findByRole("option", { name: /本地组合乙/ }));
  fireEvent.click(await screen.findByRole("button", { name: "单标的回测" }));
  fireEvent.change(screen.getByPlaceholderText("输入代码，如 600519"), { target: { value: "510300" } });
  singleRunDetail = completedSingleRun({ id: 78, portfolio_id: 2 });
  fireEvent.click(await screen.findByRole("button", { name: "开始回测" }));
  await waitFor(() => expect(requests.some((request) => request.url.includes("/backtest/runs/78/trades?"))).toBe(true));
  const region = screen.getByRole("region", { name: "单标的交易明细" });
  if (order === "old-first-success") {
    await act(async () => oldPage.resolve(json({ total: 1, page: 1, page_size: 20, items: [closedSingleTrade()] })));
    expect(region).toHaveTextContent("正在读取交易明细");
    expect(within(region).queryByText("2026-01-02")).not.toBeInTheDocument();
  }
  await act(async () => newPage.resolve(json({ total: 1, page: 1, page_size: 20,
    items: [closedSingleTrade({ run_id: 78, entry_date: "2026-07-01" })] })));
  expect(await within(region).findByText("2026-07-01")).toBeVisible();
  if (order === "new-first-failure") await act(async () => oldPage.resolve(json({ detail: "旧组合连接中断" }, 503)));
  expect(within(region).getByText("2026-07-01")).toBeVisible();
  expect(within(region).queryByRole("alert")).not.toBeInTheDocument();
  expect(requests.filter((request) => request.url.endsWith("/backtest/run")).map((request) => request.body.portfolio_id)).toEqual([1, 2]);
});

it("starts a fresh trade page when returning to single-symbol mode and ignores the old mode's response", async () => {
  singleRunDetail = completedSingleRun();
  const oldPage = deferredResponse();
  let reads = 0;
  singleTradesReply = async () => ++reads === 1 ? oldPage.promise : json({ total: 1, page: 1, page_size: 20,
    items: [closedSingleTrade({ entry_date: "2026-07-01" })] });
  await openSingleForm();
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  await waitFor(() => expect(reads).toBe(1));
  fireEvent.click(screen.getByRole("button", { name: "组合回测" }));
  fireEvent.click(screen.getByRole("button", { name: "单标的回测" }));
  const region = screen.getByRole("region", { name: "单标的交易明细" });
  expect(await within(region).findByText("2026-07-01")).toBeVisible();
  await act(async () => oldPage.resolve(json({ total: 1, page: 1, page_size: 20, items: [closedSingleTrade()] })));
  expect(within(region).getByText("2026-07-01")).toBeVisible();
  expect(within(region).queryByText("2026-01-02")).not.toBeInTheDocument();
  expect(requests.filter((request) => request.url.endsWith("/backtest/run"))).toHaveLength(1);
});

it.each(["success", "failure"])("ignores old trade %s after leaving to Settings and restores the same result without resubmitting", async (outcome) => {
  singleRunDetail = completedSingleRun();
  const oldPage = deferredResponse();
  let tradeReads = 0;
  singleTradesReply = async () => ++tradeReads === 1 ? oldPage.promise : json({ total: 1, page: 1, page_size: 20,
    items: [closedSingleTrade({ entry_date: "2026-07-01" })] });
  await openSingleForm();
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  await waitFor(() => expect(requests.some((request) => request.url.includes("/backtest/runs/77/trades?"))).toBe(true));
  fireEvent.click(screen.getByRole("button", { name: "设置" }));
  await waitFor(() => expect(screen.queryByRole("region", { name: "单标的交易明细" })).not.toBeInTheDocument());
  await act(async () => oldPage.resolve(outcome === "success" ? json({ total: 1, page: 1, page_size: 20, items: [closedSingleTrade()] }) : json({ detail: "已离开页面的连接错误" }, 503)));
  fireEvent.click(screen.getByRole("button", { name: "组合交易" }));
  fireEvent.click(await screen.findByRole("button", { name: "回测中心" }));
  fireEvent.click(await screen.findByRole("button", { name: "单标的回测" }));
  const region = screen.getByRole("region", { name: "单标的交易明细" });
  expect(await within(region).findByText("2026-07-01")).toBeVisible();
  expect(within(region).queryByText("2026-01-02")).not.toBeInTheDocument();
  expect(requests.filter((request) => request.url.endsWith("/backtest/run"))).toHaveLength(1);
  expect(within(region).queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.getByRole("region", { name: "结果对应的参数" })).toHaveTextContent("回测记录 #77");
  expect(requests.filter((request) => request.url.endsWith("/backtest/runs/77"))).toHaveLength(2);
});

it("resets the trade page for a new result without carrying rows or totals from the previous result", async () => {
  singleRunDetail = completedSingleRun();
  singleTradesReply = async (id, page) => json(id === 77 ? singleTradePage(page) : { total: 1, page, page_size: 20,
    items: [closedSingleTrade({ run_id: 78, entry_date: "2026-07-01" })] });
  await openSingleForm();
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  const region = screen.getByRole("region", { name: "单标的交易明细" });
  await within(region).findByText("共 21 条，第 1 / 2 页");
  fireEvent.click(within(region).getByRole("button", { name: "下一页" }));
  await within(region).findByText("2026-02-28");
  fireEvent.change(screen.getByRole("textbox", { name: "单标的初始资金" }), { target: { value: "200000" } });
  singleRunDetail = completedSingleRun({ id: 78, initial_capital: 200000 });
  fireEvent.click(await screen.findByRole("button", { name: "开始回测" }));
  expect(await within(region).findByText("2026-07-01")).toBeVisible();
  expect(region).toHaveTextContent("共 1 条，第 1 / 1 页");
  expect(region).not.toHaveTextContent("共 21 条");
  expect(within(region).queryByText("2026-02-28")).not.toBeInTheDocument();
  const reads = requests.filter((request) => request.url.includes("/backtest/runs/78/trades?"));
  expect(reads).toHaveLength(1);
  expect(new URL(reads[0].url, "http://test").searchParams.get("page")).toBe("1");
});

it("does not read single-symbol trades before a result exists or bypass the historical-data block", async () => {
  precheck = { ...clearPrecheck, blocking_reasons: [{ code: "STATUS_DATA_UNAVAILABLE", detail_zh: "缺少逐日历史状态", evidence: {} }] };
  openApp();
  fireEvent.click(await screen.findByRole("button", { name: "回测中心" }));
  fireEvent.click(await screen.findByRole("button", { name: "单标的回测" }));
  expect(screen.getByRole("region", { name: "单标的交易明细" })).toHaveTextContent("还没有回测结果");
  fireEvent.change(screen.getByPlaceholderText("输入代码，如 600519"), { target: { value: "510300" } });
  const blocked = await screen.findByRole("button", { name: "先补齐资料" });
  expect(blocked).toBeDisabled(); fireEvent.click(blocked);
  expect(requests.filter((request) => request.url.includes("/trades?"))).toHaveLength(0);
  expect(requests.filter((request) => request.url.endsWith("/backtest/run"))).toHaveLength(0);
});

it("retains the submitted record, parameters and summary after a detail read failure and retries only that record", async () => {
  singleRunDetail = completedSingleRun();
  singleDetailReply = async () => json({ detail: "结果读取连接暂时中断" }, 503);
  await openSingleForm();
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  const retry = await screen.findByRole("button", { name: "重新读取结果" });
  await waitFor(() => expect(retry).toBeEnabled());
  expect(screen.getByText("+12.3%")).toBeVisible();
  const parameters = screen.getByRole("region", { name: "结果对应的参数" });
  expect(parameters).toHaveTextContent("回测记录 #77");
  expect(parameters).toHaveTextContent("510300 · 2025-10-20 至 2026-10-20");
  expect(parameters).toHaveTextContent("100,000 元 · 佣金 0.03%");
  expect(screen.getByRole("alert", { name: "结果读取状态" })).toHaveTextContent("已保留回测记录和已返回的结果");
  expect(screen.queryByText(/单标的回测失败/)).not.toBeInTheDocument();
  const postsBeforeRetry = requests.filter((request) => request.url.endsWith("/backtest/run"));
  const writesBeforeRetry = requests.filter((request) => request.method !== "GET").length;
  const completionNotices = screen.queryAllByText("单标的回测已完成").length;
  singleDetailReply = async () => json(completedSingleRun({ total_return_pct: 0.25 }));
  fireEvent.click(retry);
  expect(await screen.findByText("+25.0%")).toBeVisible();
  expect(screen.queryByText("+12.3%")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "重新读取结果" })).not.toBeInTheDocument();
  expect(requests.filter((request) => request.url.endsWith("/backtest/run"))).toEqual(postsBeforeRetry);
  expect(requests.filter((request) => request.method !== "GET")).toHaveLength(writesBeforeRetry);
  expect(requests.filter((request) => request.url.endsWith("/backtest/runs/77"))).toHaveLength(2);
  expect(screen.queryAllByText("单标的回测已完成")).toHaveLength(completionNotices);
});

async function openSingleWithReadFailure() {
  singleRunDetail = completedSingleRun();
  singleDetailReply = async () => json({ detail: "首次读取连接中断" }, 503);
  const view = await openSingleForm();
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  const retry = await screen.findByRole("button", { name: "重新读取结果" });
  await waitFor(() => expect(retry).toBeEnabled());
  return { view, retry };
}

it("restores a saved single-symbol result after refresh without replacing the edited draft or submitting again", async () => {
  singleRunDetail = completedSingleRun();
  const view = await openSingleForm();
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  await screen.findByText("+12.3%");
  await waitFor(() => expect(screen.getByRole("button", { name: "开始回测" })).toBeEnabled());
  fireEvent.change(screen.getByRole("textbox", { name: "单标的初始资金" }), { target: { value: "200000" } });
  expect(screen.queryByText("+12.3%")).not.toBeInTheDocument();
  const writes = requests.filter((request) => request.method !== "GET" && !request.url.endsWith("/backtest/precheck"));
  view.unmount();
  singleDetailReply = async () => json(completedSingleRun({ total_return_pct: 0.25 }));
  window.history.replaceState(null, "", "/?tab=portfolio&subtab=backtest");
  openApp();
  fireEvent.click(await screen.findByRole("button", { name: "回测中心" }));
  expect(await screen.findByText("+25.0%")).toBeVisible();
  expect(screen.getByRole("textbox", { name: "单标的初始资金" })).toHaveValue("200000");
  const parameters = screen.getByRole("region", { name: "结果对应的参数" });
  expect(parameters).toHaveTextContent("回测记录 #77");
  expect(parameters).toHaveTextContent("100,000 元");
  expect(parameters).toHaveTextContent("正在查看已保存的回测结果");
  expect(requests.filter((request) => request.url.endsWith("/backtest/runs/77"))).toHaveLength(2);
  expect(requests.filter((request) => request.method !== "GET" && !request.url.endsWith("/backtest/precheck"))).toEqual(writes);
  fireEvent.change(screen.getByRole("textbox", { name: "单标的初始资金" }), { target: { value: "300000" } });
  expect(screen.queryByText("+25.0%")).not.toBeInTheDocument();
  expect(parameters).toHaveTextContent("上次结果已收起");
});

it("opens a historical single-symbol result with its original rules and costs while keeping the current draft", async () => {
  historyRuns = [historicalSingleRun()];
  singleDetailReply = async () => json(historicalSingleRun({ total_return_pct: 0.25 }));
  await openSingleForm();
  fireEvent.change(screen.getByRole("textbox", { name: "单标的初始资金" }), { target: { value: "200000" } });
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  fireEvent.click(await screen.findByText("已保存的单标的记录"));
  expect(await screen.findByText("+25.0%")).toBeVisible();
  expect(screen.getByRole("textbox", { name: "单标的初始资金" })).toHaveValue("200000");
  const parameters = screen.getByRole("region", { name: "结果对应的参数" });
  expect(parameters).toHaveTextContent("回测记录 #77");
  expect(parameters).toHaveTextContent("标的编号 11");
  expect(parameters).toHaveTextContent("80,000 元");
  expect(parameters).toHaveTextContent("佣金 0.07%");
  expect(parameters).toHaveTextContent("每笔最低 7 元");
  expect(parameters).toHaveTextContent("滑点 0.25%");
  expect(parameters).toHaveTextContent("质量评分不低于65分");
  expect(parameters).not.toHaveTextContent("佣金 0.03%");
  expect(requests.filter((request) => request.method !== "GET" && !request.url.endsWith("/backtest/precheck"))).toHaveLength(0);
  expect(requests.filter((request) => request.url.endsWith("/backtest/runs/77"))).toHaveLength(1);
  expect(requests.some((request) => request.url.includes("/backtest/runs/77/trades?"))).toBe(true);
});

it("keeps an accepted result when local saving fails and retries saving without running another backtest", async () => {
  const originalSetItem = Storage.prototype.setItem;
  let refuseResultSave = true;
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key, value) {
    if (key.startsWith("single-backtest-result:") && refuseResultSave) throw new DOMException("Storage full", "QuotaExceededError");
    originalSetItem.call(this, key, value);
  });
  singleRunDetail = completedSingleRun();
  const view = await openSingleForm();
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  await screen.findByText("+12.3%");
  const notice = await screen.findByRole("alert", { name: "结果保存状态" });
  expect(notice).toHaveTextContent("这条结果没能保存在本机");
  expect(screen.getByRole("region", { name: "结果对应的参数" })).toHaveTextContent("回测记录 #77");
  const writes = requests.filter((request) => request.method !== "GET" && !request.url.endsWith("/backtest/precheck"));
  refuseResultSave = false;
  fireEvent.click(within(notice).getByRole("button", { name: "重试保存结果" }));
  await waitFor(() => expect(screen.queryByRole("alert", { name: "结果保存状态" })).not.toBeInTheDocument());
  expect(requests.filter((request) => request.method !== "GET" && !request.url.endsWith("/backtest/precheck"))).toEqual(writes);
  view.unmount();
  openApp();
  fireEvent.click(await screen.findByRole("button", { name: "回测中心" }));
  expect(await screen.findByText("+12.3%")).toBeVisible();
  expect(screen.getByRole("region", { name: "结果对应的参数" })).toHaveTextContent("回测记录 #77");
  expect(requests.filter((request) => request.url.endsWith("/backtest/run"))).toHaveLength(1);
});

it("restores a directly opened single-symbol history record and clears old portfolio paging from its URL", async () => {
  historyRuns = [historicalSingleRun()];
  singleDetailReply = async () => json(historicalSingleRun({ total_return_pct: 0.25 }));
  window.history.replaceState(null, "", "/?tab=portfolio&subtab=backtest&bt_run=77&bt_tab=positions&bt_page=3&bt_symbol=99");
  const view = await openSingleForm();
  expect(await screen.findByText("+25.0%")).toBeVisible();
  const query = new URLSearchParams(window.location.search);
  expect(query.get("bt_run")).toBe("77");
  expect(query.get("bt_mode")).toBe("single");
  expect(query.has("bt_tab")).toBe(false);
  expect(query.has("bt_page")).toBe(false);
  expect(query.has("bt_symbol")).toBe(false);
  view.unmount();
  openApp();
  fireEvent.click(await screen.findByRole("button", { name: "回测中心" }));
  expect(await screen.findByText("+25.0%")).toBeVisible();
  expect(screen.getByRole("region", { name: "结果对应的参数" })).toHaveTextContent("回测记录 #77");
  expect(requests.filter((request) => request.url.endsWith("/backtest/run"))).toHaveLength(0);
});

it.each([
  { problem: "another portfolio", detail: completedSingleRun({ portfolio_id: 2 }) },
  { problem: "an invalid record number", detail: completedSingleRun({ id: 0 }) },
])("does not save a submitted summary with $problem into this or another portfolio", async ({ detail }) => {
  const otherSavedResult = "keep the other portfolio's saved result";
  localStorage.setItem("single-backtest-result:2", otherSavedResult);
  singleRunReply = async () => json(detail);
  await openSingleForm();
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  expect(await screen.findByText(/回测返回的记录与当前组合不一致/)).toBeVisible();
  expect(localStorage.getItem("single-backtest-result:2")).toBe(otherSavedResult);
  expect(localStorage.getItem("single-backtest-result:1")).toBeNull();
  expect(screen.queryByRole("region", { name: "结果对应的参数" })).not.toBeInTheDocument();
  expect(requests.some((request) => /\/backtest\/runs\/\d+$/.test(request.url))).toBe(false);
  expect(requests.filter((request) => request.url.endsWith("/backtest/run"))).toHaveLength(1);
});

async function openSingleWithPendingDetail() {
  singleRunDetail = completedSingleRun();
  const stalledRead = deferredResponse();
  let detailSignal: AbortSignal | null | undefined;
  // An unresponsive HTTP connection must not keep the page locked, even if it ignores cancellation.
  singleDetailReply = (_runId, signal) => { detailSignal = signal; return stalledRead.promise; };
  const view = await openSingleForm();
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  return { view, stalledRead, signal: detailSignal };
}

it("ends a stalled single-symbol detail read after 20 seconds and retries the saved result without resubmitting", async () => {
  await openSingleWithPendingDetail();
  expect(screen.getByText("+12.3%")).toBeVisible();
  expect(screen.getByRole("button", { name: "重新读取结果" })).toBeDisabled();
  await act(async () => { await vi.advanceTimersByTimeAsync(19_999); });
  expect(screen.getByRole("status", { name: "结果读取状态" })).toHaveTextContent("正在读取完整结果");
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  const notice = screen.getByRole("alert", { name: "结果读取状态" });
  expect(notice).toHaveTextContent("读取完整结果超过20秒，请重新读取。");
  expect(notice).toHaveTextContent("已保留回测记录和已返回的结果");
  expect(screen.getByText("+12.3%")).toBeVisible();
  const parameters = screen.getByRole("region", { name: "结果对应的参数" });
  expect(parameters).toHaveTextContent("回测记录 #77");
  expect(parameters).toHaveTextContent("510300 · 2025-10-20 至 2026-10-20");
  expect(parameters).toHaveTextContent("100,000 元 · 佣金 0.03%");
  expect(screen.getByRole("button", { name: "开始回测" })).toBeEnabled();
  const writes = requests.filter((request) => request.method !== "GET");
  singleDetailReply = async () => json(completedSingleRun({ total_return_pct: 0.25 }));
  const retry = screen.getByRole("button", { name: "重新读取结果" });
  expect(retry).toBeEnabled();
  fireEvent.click(retry); fireEvent.click(retry);
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(screen.getByText("+25.0%")).toBeVisible();
  expect(screen.queryByRole("alert", { name: "结果读取状态" })).not.toBeInTheDocument();
  expect(requests.filter((request) => request.url.endsWith("/backtest/runs/77"))).toHaveLength(2);
  expect(requests.filter((request) => request.method !== "GET")).toEqual(writes);
  expect(requests.filter((request) => request.url.endsWith("/backtest/run"))).toHaveLength(1);
  await act(async () => { await vi.advanceTimersByTimeAsync(20_001); });
  expect(screen.getByText("+25.0%")).toBeVisible();
  expect(screen.queryByRole("alert", { name: "结果读取状态" })).not.toBeInTheDocument();
});

it.each([
  { outcome: "success", duringRetry: true }, { outcome: "failure", duringRetry: true },
  { outcome: "success", duringRetry: false }, { outcome: "failure", duringRetry: false },
])("ignores an expired detail read's late $outcome, duringRetry=$duringRetry", async ({ outcome, duringRetry }) => {
  const { stalledRead } = await openSingleWithPendingDetail();
  await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
  expect(screen.getByRole("alert", { name: "结果读取状态" })).toHaveTextContent("超过20秒");
  const retryRead = deferredResponse();
  singleDetailReply = () => retryRead.promise;
  fireEvent.click(screen.getByRole("button", { name: "重新读取结果" }));
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  const resolveOld = async () => act(async () => stalledRead.resolve(outcome === "success"
    ? json(completedSingleRun({ total_return_pct: 0.99 })) : json({ detail: "已过期读取的错误" }, 503)));
  if (duringRetry) {
    await resolveOld();
    expect(screen.getByRole("status", { name: "结果读取状态" })).toHaveTextContent("正在读取完整结果");
    expect(screen.getByRole("button", { name: "重新读取结果" })).toBeDisabled();
    expect(screen.getByText("+12.3%")).toBeVisible();
  }
  await act(async () => retryRead.resolve(json(completedSingleRun({ total_return_pct: 0.25 }))));
  if (!duringRetry) await resolveOld();
  expect(screen.getByText("+25.0%")).toBeVisible();
  expect(screen.queryByText("+99.0%")).not.toBeInTheDocument();
  expect(screen.queryByText(/已过期读取的错误/)).not.toBeInTheDocument();
  expect(screen.queryByRole("alert", { name: "结果读取状态" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "重新读取结果" })).not.toBeInTheDocument();
  expect(screen.getByRole("region", { name: "结果对应的参数" })).toHaveTextContent("回测记录 #77");
  expect(requests.filter((request) => request.url.endsWith("/backtest/runs/77"))).toHaveLength(2);
  expect(requests.filter((request) => request.url.endsWith("/backtest/run"))).toHaveLength(1);
});

it("gives each result retry its own deadline and keeps the saved summary after another timeout", async () => {
  await openSingleWithPendingDetail();
  await act(async () => { await vi.advanceTimersByTimeAsync(25_000); });
  const secondRead = deferredResponse();
  singleDetailReply = () => secondRead.promise;
  fireEvent.click(screen.getByRole("button", { name: "重新读取结果" }));
  await act(async () => { await vi.advanceTimersByTimeAsync(19_999); });
  expect(screen.getByRole("button", { name: "重新读取结果" })).toBeDisabled();
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(screen.getByRole("alert", { name: "结果读取状态" })).toHaveTextContent("读取完整结果超过20秒，请重新读取。");
  expect(screen.getByRole("button", { name: "重新读取结果" })).toBeEnabled();
  expect(screen.getByText("+12.3%")).toBeVisible();
  singleDetailReply = async () => json(completedSingleRun({ total_return_pct: 0.30 }));
  fireEvent.click(screen.getByRole("button", { name: "重新读取结果" }));
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  await act(async () => secondRead.resolve(json(completedSingleRun({ total_return_pct: 0.99 }))));
  expect(screen.getByText("+30.0%")).toBeVisible();
  expect(screen.queryByText("+99.0%")).not.toBeInTheDocument();
  expect(requests.filter((request) => request.url.endsWith("/backtest/runs/77"))).toHaveLength(3);
  expect(requests.filter((request) => request.url.endsWith("/backtest/run"))).toHaveLength(1);
});

it("cancels the actual detail request at its deadline and displays a timeout instead of a cancellation error", async () => {
  singleRunDetail = completedSingleRun();
  let detailSignal: AbortSignal | null | undefined;
  singleDetailReply = (_runId, signal) => {
    detailSignal = signal;
    return new Promise<Response>((_resolve, reject) => signal?.addEventListener("abort", () => {
      reject(new DOMException("The operation was aborted.", "AbortError"));
    }, { once: true }));
  };
  await openSingleForm();
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  await act(async () => { await vi.advanceTimersByTimeAsync(19_999); });
  expect(detailSignal?.aborted).toBe(false);
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(detailSignal?.aborted).toBe(true);
  expect(screen.getByRole("alert", { name: "结果读取状态" })).toHaveTextContent("读取完整结果超过20秒，请重新读取。");
  expect(screen.queryByText(/The operation was aborted/)).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "重新读取结果" })).toBeEnabled();
});

it.each([false, true])("does not publish an expired read's timeout after parameters change, restored=%s", async (restore) => {
  await openSingleWithPendingDetail();
  const capital = screen.getByRole("textbox", { name: "单标的初始资金" });
  fireEvent.change(capital, { target: { value: "200000" } });
  if (restore) fireEvent.change(capital, { target: { value: "100000" } });
  await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
  expect(screen.queryByRole("alert", { name: "结果读取状态" })).not.toBeInTheDocument();
  if (!restore) {
    expect(screen.queryByText("+12.3%")).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "结果对应的参数" })).toHaveTextContent("上次结果已收起");
    fireEvent.change(capital, { target: { value: "100000" } });
  }
  expect(screen.getByText("+12.3%")).toBeVisible();
  expect(screen.getByRole("button", { name: "重新读取结果" })).toBeEnabled();
  singleDetailReply = async () => json(completedSingleRun({ total_return_pct: 0.25 }));
  fireEvent.click(screen.getByRole("button", { name: "重新读取结果" }));
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(screen.getByText("+25.0%")).toBeVisible();
  expect(requests.filter((request) => request.url.endsWith("/backtest/run"))).toHaveLength(1);
});

it("cancels a pending detail deadline when switching portfolios and does not show its timeout in the new portfolio", async () => {
  const { signal, stalledRead } = await openSingleWithPendingDetail();
  await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
  fireEvent.click(screen.getByRole("button", { name: /本地组合甲/ }));
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  fireEvent.click(screen.getByRole("option", { name: /本地组合乙/ }));
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(signal?.aborted).toBe(true);
  await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
  await act(async () => stalledRead.resolve(json(completedSingleRun({ total_return_pct: 0.99 }))));
  expect(screen.queryByRole("region", { name: "结果对应的参数" })).not.toBeInTheDocument();
  expect(screen.queryByRole("alert", { name: "结果读取状态" })).not.toBeInTheDocument();
  expect(screen.queryByText("+99.0%")).not.toBeInTheDocument();
  expect(requests.filter((request) => request.url.endsWith("/backtest/run"))).toHaveLength(1);
  expect(requests.filter((request) => request.url.endsWith("/backtest/runs/77"))).toHaveLength(1);
});

it("cancels an unmounted detail read and does not publish its later timeout or failure", async () => {
  const { view, signal, stalledRead } = await openSingleWithPendingDetail();
  await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
  view.unmount();
  expect(signal?.aborted).toBe(true);
  await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
  await act(async () => stalledRead.resolve(json({ detail: "已经离开的详情读取失败" }, 503)));
  expect(screen.queryByRole("alert", { name: "结果读取状态" })).not.toBeInTheDocument();
  expect(screen.queryByText(/已经离开的详情读取失败/)).not.toBeInTheDocument();
  expect(requests.filter((request) => request.url.endsWith("/backtest/run"))).toHaveLength(1);
  expect(requests.filter((request) => request.url.endsWith("/backtest/runs/77"))).toHaveLength(1);
});

it("shows the accepted summary and record number before the first detail read returns", async () => {
  singleRunDetail = completedSingleRun();
  const detail = deferredResponse();
  singleDetailReply = () => detail.promise;
  await openSingleForm();
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  expect(await screen.findByText("+12.3%")).toBeVisible();
  expect(screen.getByRole("region", { name: "结果对应的参数" })).toHaveTextContent("回测记录 #77");
  expect(screen.getByRole("status", { name: "结果读取状态" })).toHaveTextContent("已返回的结果可以先查看");
  expect(screen.getByRole("button", { name: "正在读取结果…" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "重新读取结果" })).toBeDisabled();
  expect(requests.filter((request) => request.url.endsWith("/backtest/run"))).toHaveLength(1);
  await act(async () => detail.resolve(json(completedSingleRun({ total_return_pct: 0.25 }))));
  expect(await screen.findByText("+25.0%")).toBeVisible();
  expect(screen.queryByRole("button", { name: "重新读取结果" })).not.toBeInTheDocument();
});

it.each([
  { failure: "connection", response: () => json({ detail: "读取连接中断" }, 503) },
  { failure: "score error", response: () => json({ detail: { code: "BACKTEST_SCORE_COVERAGE_INSUFFICIENT", message: "读取错误不是提交失败" } }, 422) },
  { failure: "missing record", response: () => json({ detail: "原记录不存在" }, 404) },
  { failure: "different record", response: () => json(completedSingleRun({ id: 78, total_return_pct: 0.99 })) },
  { failure: "different portfolio", response: () => json(completedSingleRun({ portfolio_id: 2, total_return_pct: 0.99 })) },
])("keeps the original summary after retry returns $failure and allows another read without resubmitting", async ({ response }) => {
  const { retry } = await openSingleWithReadFailure();
  const secondRead = deferredResponse();
  singleDetailReply = () => secondRead.promise;
  const writesBeforeRetry = requests.filter((request) => request.method !== "GET").length;
  fireEvent.click(retry); fireEvent.click(retry);
  await waitFor(() => expect(requests.filter((request) => request.url.endsWith("/backtest/runs/77"))).toHaveLength(2));
  expect(screen.getByRole("button", { name: "重新读取结果" })).toBeDisabled();
  await act(async () => secondRead.resolve(response()));
  await waitFor(() => expect(screen.getByRole("button", { name: "重新读取结果" })).toBeEnabled());
  expect(screen.getByRole("alert", { name: "结果读取状态" })).toHaveTextContent("原因：");
  expect(screen.getByText("+12.3%")).toBeVisible();
  expect(screen.queryByText("+99.0%")).not.toBeInTheDocument();
  expect(screen.getByRole("region", { name: "结果对应的参数" })).toHaveTextContent("回测记录 #77");
  expect(screen.queryByRole("button", { name: "去补资料" })).not.toBeInTheDocument();
  singleDetailReply = async () => json(completedSingleRun({ total_return_pct: 0.25 }));
  fireEvent.click(screen.getByRole("button", { name: "重新读取结果" }));
  expect(await screen.findByText("+25.0%")).toBeVisible();
  expect(requests.filter((request) => request.method !== "GET")).toHaveLength(writesBeforeRetry);
  expect(requests.filter((request) => request.url.endsWith("/backtest/run"))).toHaveLength(1);
  expect(requests.filter((request) => request.url.endsWith("/backtest/runs/77"))).toHaveLength(3);
});

it.each([
  { outcome: "success", restore: false }, { outcome: "failure", restore: false },
  { outcome: "success", restore: true }, { outcome: "failure", restore: true },
])("rejects a late retry $outcome after editing parameters, restored=$restore, and unlocks reading again", async ({ outcome, restore }) => {
  const { retry } = await openSingleWithReadFailure();
  const lateRead = deferredResponse();
  singleDetailReply = () => lateRead.promise;
  fireEvent.click(retry);
  await waitFor(() => expect(requests.filter((request) => request.url.endsWith("/backtest/runs/77"))).toHaveLength(2));
  const capital = screen.getByRole("textbox", { name: "单标的初始资金" });
  fireEvent.change(capital, { target: { value: "200000" } });
  expect(screen.queryByText("+12.3%")).not.toBeInTheDocument();
  if (restore) fireEvent.change(capital, { target: { value: "100000" } });
  await act(async () => lateRead.resolve(outcome === "success"
    ? json(completedSingleRun({ total_return_pct: 0.25 })) : json({ detail: "这是旧读取的错误" }, 503)));
  expect(screen.queryByText("+25.0%")).not.toBeInTheDocument();
  expect(screen.queryByRole("alert", { name: "结果读取状态" })).not.toBeInTheDocument();
  if (!restore) {
    expect(screen.getByRole("region", { name: "结果对应的参数" })).toHaveTextContent("上次结果已收起");
    expect(screen.queryByRole("button", { name: "重新读取结果" })).not.toBeInTheDocument();
    fireEvent.change(capital, { target: { value: "100000" } });
  }
  expect(screen.getByText("+12.3%")).toBeVisible();
  const nextRetry = screen.getByRole("button", { name: "重新读取结果" });
  expect(nextRetry).toBeEnabled();
  singleDetailReply = async () => json(completedSingleRun({ total_return_pct: 0.30 }));
  fireEvent.click(nextRetry);
  expect(await screen.findByText("+30.0%")).toBeVisible();
  expect(requests.filter((request) => request.url.endsWith("/backtest/run"))).toHaveLength(1);
});

it.each(["old-first-success", "new-first-failure"])("protects a new single-symbol submission from an old retry, %s", async (order) => {
  const { retry } = await openSingleWithReadFailure();
  const oldRead = deferredResponse();
  const newRead = deferredResponse();
  singleDetailReply = (id) => id === 77 ? oldRead.promise : newRead.promise;
  fireEvent.click(retry);
  await waitFor(() => expect(requests.filter((request) => request.url.endsWith("/backtest/runs/77"))).toHaveLength(2));
  fireEvent.change(screen.getByRole("textbox", { name: "单标的初始资金" }), { target: { value: "200000" } });
  singleRunDetail = completedSingleRun({ id: 78, initial_capital: 200000, total_return_pct: 0.30 });
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  await waitFor(() => expect(requests.filter((request) => request.url.endsWith("/backtest/runs/78"))).toHaveLength(1));
  if (order === "old-first-success") {
    await act(async () => oldRead.resolve(json(completedSingleRun({ total_return_pct: 0.25 }))));
    expect(screen.getByText("+30.0%")).toBeVisible();
    expect(screen.getByRole("button", { name: "正在读取结果…" })).toBeDisabled();
  }
  await act(async () => newRead.resolve(json(completedSingleRun({ id: 78, initial_capital: 200000, total_return_pct: 0.40 }))));
  await screen.findByText("+40.0%");
  if (order === "new-first-failure") await act(async () => oldRead.resolve(json({ detail: "旧读取失败" }, 503)));
  expect(screen.getByText("+40.0%")).toBeVisible();
  expect(screen.queryByText("+25.0%")).not.toBeInTheDocument();
  expect(screen.queryByRole("alert", { name: "结果读取状态" })).not.toBeInTheDocument();
  expect(screen.getByRole("region", { name: "结果对应的参数" })).toHaveTextContent("200,000 元");
  expect(requests.filter((request) => request.url.endsWith("/backtest/run"))).toHaveLength(2);
});

it.each(["success", "failure"])("ignores a late retry %s after switching portfolios while the new result is loading", async (outcome) => {
  const { retry } = await openSingleWithReadFailure();
  const oldRead = deferredResponse();
  singleDetailReply = () => oldRead.promise;
  fireEvent.click(retry);
  await waitFor(() => expect(requests.filter((request) => request.url.endsWith("/backtest/runs/77"))).toHaveLength(2));
  fireEvent.click(screen.getByRole("button", { name: /本地组合甲/ }));
  fireEvent.click(await screen.findByRole("option", { name: /本地组合乙/ }));
  fireEvent.click(await screen.findByRole("button", { name: "单标的回测" }));
  expect(screen.queryByRole("region", { name: "结果对应的参数" })).not.toBeInTheDocument();
  fireEvent.change(screen.getByPlaceholderText("输入代码，如 600519"), { target: { value: "510300" } });
  singleRunDetail = completedSingleRun({ id: 78, portfolio_id: 2, total_return_pct: 0.30 });
  const newRead = deferredResponse();
  singleDetailReply = () => newRead.promise;
  fireEvent.click(await screen.findByRole("button", { name: "开始回测" }));
  await waitFor(() => expect(requests.some((request) => request.url.endsWith("/backtest/runs/78"))).toBe(true));
  await act(async () => oldRead.resolve(outcome === "success" ? json(completedSingleRun({ total_return_pct: 0.25 })) : json({ detail: "旧组合读取失败" }, 503)));
  expect(screen.getByText("+30.0%")).toBeVisible();
  expect(screen.getByRole("button", { name: "正在读取结果…" })).toBeDisabled();
  expect(screen.queryByRole("alert", { name: "结果读取状态" })).not.toBeInTheDocument();
  await act(async () => newRead.resolve(json(completedSingleRun({ id: 78, portfolio_id: 2, total_return_pct: 0.40 }))));
  expect(await screen.findByText("+40.0%")).toBeVisible();
  expect(screen.queryByText("+25.0%")).not.toBeInTheDocument();
  expect(requests.filter((request) => request.url.endsWith("/backtest/run")).map((request) => request.body.portfolio_id)).toEqual([1, 2]);
});

it.each(["success", "failure"])("does not revive a late retry %s after leaving and returning to single-symbol mode", async (outcome) => {
  const { retry } = await openSingleWithReadFailure();
  const oldRead = deferredResponse();
  singleDetailReply = () => oldRead.promise;
  fireEvent.click(retry);
  await waitFor(() => expect(requests.filter((request) => request.url.endsWith("/backtest/runs/77"))).toHaveLength(2));
  fireEvent.click(screen.getByRole("button", { name: "组合回测" }));
  fireEvent.click(screen.getByRole("button", { name: "单标的回测" }));
  await act(async () => oldRead.resolve(outcome === "success" ? json(completedSingleRun({ total_return_pct: 0.25 })) : json({ detail: "旧页面读取失败" }, 503)));
  expect(screen.getByText("+12.3%")).toBeVisible();
  expect(screen.queryByText("+25.0%")).not.toBeInTheDocument();
  expect(screen.queryByRole("alert", { name: "结果读取状态" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "重新读取结果" })).toBeEnabled();
  expect(requests.filter((request) => request.url.endsWith("/backtest/run"))).toHaveLength(1);
});

it.each(["success", "failure"])("keeps a re-entered page's result unchanged when an unmounted retry returns %s", async (outcome) => {
  const { retry, view } = await openSingleWithReadFailure();
  const oldRead = deferredResponse();
  singleDetailReply = () => oldRead.promise;
  fireEvent.click(retry);
  await waitFor(() => expect(requests.filter((request) => request.url.endsWith("/backtest/runs/77"))).toHaveLength(2));
  view.unmount();
  singleDetailReply = async (id) => json(completedSingleRun({ id, total_return_pct: id === 77 ? 0.1234 : 0.40 }));
  singleRunDetail = completedSingleRun({ id: 78, total_return_pct: 0.40 });
  await openSingleForm();
  await screen.findByText("+12.3%");
  await waitFor(() => expect(screen.getByRole("button", { name: "开始回测" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  await screen.findByText("+40.0%");
  await waitFor(() => expect(screen.getByRole("button", { name: "开始回测" })).toBeEnabled());
  await act(async () => oldRead.resolve(outcome === "success" ? json(completedSingleRun({ total_return_pct: 0.25 })) : json({ detail: "已离开页面的读取错误" }, 503)));
  expect(screen.getByText("+40.0%")).toBeVisible();
  expect(screen.queryByText("+25.0%")).not.toBeInTheDocument();
  expect(screen.queryByRole("alert", { name: "结果读取状态" })).not.toBeInTheDocument();
  expect(screen.getByRole("region", { name: "结果对应的参数" })).toHaveTextContent("回测记录 #78");
  expect(requests.filter((request) => request.url.endsWith("/backtest/run"))).toHaveLength(2);
});

it("labels a single-symbol result with its submitted parameters and hides old metrics after an edit", async () => {
  singleRunDetail = completedSingleRun();
  await openSingleForm();
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  expect(await screen.findByText("+12.3%")).toBeVisible();
  const parameters = screen.getByRole("region", { name: "结果对应的参数" });
  expect(parameters).toHaveTextContent("510300");
  expect(parameters).toHaveTextContent("2025-10-20 至 2026-10-20");
  expect(parameters).toHaveTextContent("100,000 元");
  expect(parameters).toHaveTextContent("佣金 0.03%");
  expect(parameters).toHaveTextContent("每笔最低 5 元");
  expect(parameters).toHaveTextContent("滑点 0.1%");
  fireEvent.change(screen.getByRole("textbox", { name: "单标的初始资金" }), { target: { value: "200000" } });
  expect(screen.queryByText("+12.3%")).not.toBeInTheDocument();
  expect(parameters).toHaveTextContent("参数已修改，尚未按当前参数回测。上次结果已收起。");
  expect(parameters).toHaveTextContent("100,000 元");
  expect(parameters).not.toHaveTextContent("200,000 元");
  expect(screen.getByRole("button", { name: "开始回测" })).toBeEnabled();
  expect(requests.filter((request) => request.url.endsWith("/backtest/run"))).toHaveLength(1);
});

it("ignores a late single-symbol submission after parameters change and change back, then permits a manual new run", async () => {
  singleRunDetail = completedSingleRun();
  await openSingleForm();
  const oldReply = deferredResponse();
  singleRunReply = () => oldReply.promise;
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  await waitFor(() => expect(requests.filter((request) => request.url.endsWith("/backtest/run"))).toHaveLength(1));
  fireEvent.change(screen.getByRole("textbox", { name: "单标的初始资金" }), { target: { value: "200000" } });
  fireEvent.change(screen.getByRole("textbox", { name: "单标的初始资金" }), { target: { value: "100000" } });
  expect(screen.getByRole("button", { name: "回测中..." })).toBeDisabled();
  await act(async () => oldReply.resolve(json(completedSingleRun())));
  await screen.findByRole("button", { name: "开始回测" });
  expect(screen.queryByText("+12.3%")).not.toBeInTheDocument();
  expect(requests.filter((request) => request.url.endsWith("/backtest/runs/77"))).toHaveLength(0);
  expect(screen.queryByText("单标的回测已完成")).not.toBeInTheDocument();
  singleRunReply = null;
  singleRunDetail = completedSingleRun({ total_return_pct: 0.25 });
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  expect(await screen.findByText("+25.0%")).toBeVisible();
  expect(requests.filter((request) => request.url.endsWith("/backtest/run"))).toHaveLength(2);
});

it.each([
  { field: "code", value: "600010" },
  { field: "start", value: "2025-10-21" },
  { field: "end", value: "2026-10-19" },
  { field: "capital", value: "200000" },
  { field: "commission", value: "0.07" },
  { field: "commission", value: " " },
])("keeps the submitted parameters and hides all old metrics when $field becomes $value", async ({ field, value }) => {
  singleRunDetail = completedSingleRun({ max_drawdown_pct: 0.043, sharpe_ratio: 1.78, win_rate: 0.625 });
  await openSingleForm();
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  await screen.findByText("+12.3%");
  const input = field === "code" ? screen.getByPlaceholderText("输入代码，如 600519")
    : field === "start" || field === "end" ? screen.getAllByTestId("single-backtest-range-picker")[field === "start" ? 0 : 1]
    : screen.getByRole("textbox", { name: field === "capital" ? "单标的初始资金" : "单标的佣金费率" });
  if (field === "start" || field === "end") {
    const user = userEvent.setup();
    await user.click(input); await user.clear(input); await user.type(input, value); await user.keyboard("{Enter}{Tab}");
  } else fireEvent.change(input, { target: { value } });
  await waitFor(() => expect(screen.queryByText("+12.3%")).not.toBeInTheDocument());
  for (const oldValue of ["4.3%", "1.78", "62.5%"]) expect(screen.queryByText(oldValue)).not.toBeInTheDocument();
  const parameters = screen.getByRole("region", { name: "结果对应的参数" });
  expect(parameters).toHaveTextContent("上次结果已收起");
  expect(parameters).toHaveTextContent("510300 · 2025-10-20 至 2026-10-20");
  expect(parameters).toHaveTextContent("100,000 元 · 佣金 0.03%");
  expect(requests.filter((request) => request.url.endsWith("/backtest/run"))).toHaveLength(1);
});

it("keeps matching results for equivalent number formatting and invalidates an empty zero commission", async () => {
  singleRunDetail = completedSingleRun();
  await openSingleForm();
  fireEvent.change(screen.getByRole("textbox", { name: "单标的佣金费率" }), { target: { value: "0" } });
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  await screen.findByText("+12.3%");
  fireEvent.change(screen.getByRole("textbox", { name: "单标的初始资金" }), { target: { value: "100,000" } });
  fireEvent.change(screen.getByRole("textbox", { name: "单标的佣金费率" }), { target: { value: "0.00" } });
  expect(screen.getByText("+12.3%")).toBeVisible();
  fireEvent.change(screen.getByRole("textbox", { name: "单标的佣金费率" }), { target: { value: "" } });
  expect(screen.queryByText("+12.3%")).not.toBeInTheDocument();
  expect(screen.getByRole("region", { name: "结果对应的参数" })).toHaveTextContent("佣金 0%");
  expect(screen.getByRole("button", { name: "请先填写完整参数" })).toBeDisabled();
  fireEvent.change(screen.getByRole("textbox", { name: "单标的佣金费率" }), { target: { value: "0" } });
  expect(screen.getByText("+12.3%")).toBeVisible();
  expect(requests.filter((request) => request.url.endsWith("/backtest/run"))).toHaveLength(1);
});

it.each([
  { stage: "symbol", outcome: "success" }, { stage: "symbol", outcome: "failure" },
  { stage: "submission", outcome: "success" }, { stage: "submission", outcome: "failure" },
  { stage: "detail", outcome: "success" }, { stage: "detail", outcome: "failure" },
])("discards a stale $stage $outcome without results, repair prompts or a stuck button", async ({ stage, outcome }) => {
  singleRunDetail = completedSingleRun();
  await openSingleForm();
  const lateReply = deferredResponse();
  let received = false;
  const reply = () => { received = true; return lateReply.promise; };
  if (stage === "symbol") symbolReply = reply;
  else if (stage === "submission") singleRunReply = reply;
  else singleDetailReply = reply;
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  await waitFor(() => expect(received).toBe(true));
  // A successful POST now publishes its summary and refreshes history once.
  if (stage === "detail") await screen.findByText("单标的回测已完成");
  const historyReads = requests.filter((request) => request.url.includes("/backtest/runs?")).length;
  const completionNotices = screen.queryAllByText("单标的回测已完成").length;
  fireEvent.change(screen.getByRole("textbox", { name: "单标的初始资金" }), { target: { value: "200000" } });
  expect(screen.getByText("参数已修改，正在等待上次请求结束。不会按新参数自动回测。")).toBeVisible();
  expect(screen.getByRole("button", { name: "回测中..." })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "回测中..." }));
  await act(async () => lateReply.resolve(outcome === "failure"
    ? json({ detail: { code: "BACKTEST_SCORE_COVERAGE_INSUFFICIENT", message: "过期请求的评分资料不足" } }, 422)
    : json(stage === "symbol" ? [symbol] : completedSingleRun())));
  expect(await screen.findByRole("button", { name: "开始回测" })).toBeEnabled();
  expect(screen.queryByText("+12.3%")).not.toBeInTheDocument();
  if (stage === "detail") expect(screen.getByRole("region", { name: "结果对应的参数" })).toHaveTextContent("上次结果已收起");
  else expect(screen.queryByRole("region", { name: "结果对应的参数" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "去补资料" })).not.toBeInTheDocument();
  expect(screen.queryByText(/过期请求的评分资料不足|这段日期的历史评分不足/)).not.toBeInTheDocument();
  expect(screen.queryAllByText("单标的回测已完成")).toHaveLength(completionNotices);
  expect(requests.filter((request) => request.url.endsWith("/backtest/run"))).toHaveLength(stage === "symbol" ? 0 : 1);
  expect(requests.filter((request) => request.url.endsWith("/backtest/runs/77"))).toHaveLength(stage === "detail" ? 1 : 0);
  expect(requests.filter((request) => request.url.includes("/backtest/runs?")).length).toBe(historyReads);
});

it("does not revive an in-flight single-symbol request after leaving and returning to the mode", async () => {
  singleRunDetail = completedSingleRun();
  await openSingleForm();
  const lateReply = deferredResponse();
  singleDetailReply = () => lateReply.promise;
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  await waitFor(() => expect(requests.some((request) => request.url.endsWith("/backtest/runs/77"))).toBe(true));
  await screen.findByText("单标的回测已完成");
  const completionNotices = screen.queryAllByText("单标的回测已完成").length;
  fireEvent.click(screen.getByRole("button", { name: "组合回测" }));
  fireEvent.click(screen.getByRole("button", { name: "单标的回测" }));
  await act(async () => lateReply.resolve(json(completedSingleRun({ total_return_pct: 0.25 }))));
  expect(await screen.findByRole("button", { name: "开始回测" })).toBeEnabled();
  expect(screen.getByText("+12.3%")).toBeVisible();
  expect(screen.queryByText("+25.0%")).not.toBeInTheDocument();
  expect(screen.queryAllByText("单标的回测已完成")).toHaveLength(completionNotices);
  expect(requests.filter((request) => request.url.endsWith("/backtest/run"))).toHaveLength(1);
});

it.each(["old-first", "new-first"])("keeps another portfolio's new request and result safe when replies arrive %s", async (order) => {
  singleRunDetail = completedSingleRun();
  await openSingleForm();
  const oldReply = deferredResponse();
  singleDetailReply = () => oldReply.promise;
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  await waitFor(() => expect(requests.filter((request) => request.url.endsWith("/backtest/runs/77"))).toHaveLength(1));
  fireEvent.click(screen.getByRole("button", { name: /本地组合甲/ }));
  fireEvent.click(await screen.findByRole("option", { name: /本地组合乙/ }));
  fireEvent.click(await screen.findByRole("button", { name: "单标的回测" }));
  expect(screen.queryByRole("region", { name: "结果对应的参数" })).not.toBeInTheDocument();
  fireEvent.change(screen.getByPlaceholderText("输入代码，如 600519"), { target: { value: "510300" } });
  singleRunDetail = completedSingleRun({ id: 78, portfolio_id: 2, total_return_pct: 0.25 });
  const newReply = deferredResponse();
  singleDetailReply = () => newReply.promise;
  fireEvent.click(await screen.findByRole("button", { name: "开始回测" }));
  await waitFor(() => expect(requests.filter((request) => request.url.endsWith("/backtest/runs/78"))).toHaveLength(1));
  if (order === "old-first") {
    await act(async () => oldReply.resolve(json(completedSingleRun())));
    expect(screen.getByRole("button", { name: "正在读取结果…" })).toBeDisabled();
    expect(screen.queryByText("+12.3%")).not.toBeInTheDocument();
  }
  await act(async () => newReply.resolve(json(singleRunDetail)));
  expect(await screen.findByText("+25.0%")).toBeVisible();
  if (order === "new-first") await act(async () => oldReply.resolve(json(completedSingleRun())));
  expect(screen.getByText("+25.0%")).toBeVisible();
  expect(screen.queryByText("+12.3%")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "开始回测" })).toBeEnabled();
  expect(requests.filter((request) => request.url.endsWith("/backtest/run")).map((request) => request.body.portfolio_id)).toEqual([1, 2]);
});

it("ignores a late reply from the unmounted page after re-entering with a new result", async () => {
  singleRunDetail = completedSingleRun();
  const view = await openSingleForm();
  const oldReply = deferredResponse();
  singleDetailReply = () => oldReply.promise;
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  await waitFor(() => expect(requests.some((request) => request.url.endsWith("/backtest/runs/77"))).toBe(true));
  view.unmount();
  singleDetailReply = null;
  singleRunDetail = completedSingleRun({ id: 78, total_return_pct: 0.25 });
  await openSingleForm();
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  await screen.findByText("+25.0%");
  await act(async () => oldReply.resolve(json(completedSingleRun())));
  expect(screen.getByText("+25.0%")).toBeVisible();
  expect(screen.queryByText("+12.3%")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "开始回测" })).toBeEnabled();
  expect(requests.filter((request) => request.url.endsWith("/backtest/run"))).toHaveLength(2);
});

it("shows the actual scoring rules and submits them without unimplemented technical indicator options", async () => {
  openApp();
  fireEvent.click(await screen.findByRole("button", { name: "回测中心" }));
  fireEvent.click(await screen.findByRole("button", { name: "单标的回测" }));
  const rules = screen.getByRole("region", { name: "本次买卖条件" });
  expect(rules).toHaveTextContent("质量评分不低于50分");
  expect(rules).toHaveTextContent("择时评分不低于60分");
  expect(rules).toHaveTextContent("买入或建仓");
  expect(rules).toHaveTextContent("当日开盘价");
  expect(rules).toHaveTextContent("上涨达到20%");
  expect(rules).toHaveTextContent("下跌达到8%");
  expect(rules).toHaveTextContent("30个自然日");
  expect(rules).toHaveTextContent("全部卖出");
  expect(rules).toHaveTextContent("不含费用");
  expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  expect(screen.queryByRole("option", { name: /MA|MACD|RSI|布林带|KDJ/ })).not.toBeInTheDocument();
  fireEvent.change(screen.getByPlaceholderText("输入代码，如 600519"), { target: { value: "510300" } });
  fireEvent.click(await screen.findByRole("button", { name: "开始回测" }));
  await waitFor(() => expect(requests.find((request) => request.url.endsWith("/backtest/run"))?.body).toMatchObject({
    run_name: "510300-评分回测",
    rule_config: {
      buy_conditions: { quality_score_min: 50, timing_score_min: 60, actions: ["buy", "open"], stages: [] },
      sell_conditions: { take_profit_pct: 0.2, stop_loss_pct: 0.08, max_hold_days: 30, score_actions: ["exit", "reduce"] },
      position_config: { type: "fixed_pct", value: 1, max_positions: 1 },
      execution_config: { entry_timing: "next_open", exit_timing: "next_open", entry_price_field: "open", exit_price_field: "open" },
    },
  }));
  expect(requests.find((request) => request.url.endsWith("/backtest/run"))?.body.rule_config.buy_conditions).not.toHaveProperty("indicator_key");
  expect(requests.filter((request) => request.method !== "GET" && !request.url.endsWith("/backtest/precheck")).map((request) => request.url)).toEqual(["/api/v1/backtest/run"]);
});

it.each([
  { previous: "MA", quality: 60, timing: 70, profit: 25, loss: 10, days: 45 },
  { previous: "MACD", quality: 50, timing: 60, profit: 20, loss: 8, days: 30 },
  { previous: "RSI", quality: 30, timing: 30, profit: 12, loss: 8, days: 15 },
  { previous: "布林带", quality: 45, timing: 50, profit: 10, loss: 12, days: 20 },
  { previous: "KDJ", quality: 70, timing: 40, profit: 8, loss: 6, days: 10 },
  { previous: "unknown-old-value", quality: 50, timing: 60, profit: 20, loss: 8, days: 30 },
])("preserves the saved numeric rules from $previous without offering that indicator again", async ({ previous, quality, timing, profit, loss, days }) => {
  localStorage.setItem("backtest-parameters:1", JSON.stringify({ version: 1, portfolioId: 1, parameters: { singleIndicator: previous } }));
  openApp();
  fireEvent.click(await screen.findByRole("button", { name: "回测中心" }));
  fireEvent.click(await screen.findByRole("button", { name: "单标的回测" }));
  const rules = screen.getByRole("region", { name: "本次买卖条件" });
  expect(rules).toHaveTextContent(`质量评分不低于${quality}分`);
  expect(rules).toHaveTextContent(`择时评分不低于${timing}分`);
  expect(rules).toHaveTextContent(`上涨达到${profit}%`);
  expect(rules).toHaveTextContent(`下跌达到${loss}%`);
  expect(rules).toHaveTextContent(`${days}个自然日`);
  expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  expect(requests.filter((request) => request.method !== "GET" && !request.url.endsWith("/backtest/precheck"))).toEqual([]);
  fireEvent.change(screen.getByPlaceholderText("输入代码，如 600519"), { target: { value: "510300" } });
  fireEvent.click(await screen.findByRole("button", { name: "开始回测" }));
  await waitFor(() => expect(requests.find((request) => request.url.endsWith("/backtest/run"))?.body.rule_config).toEqual({
    buy_conditions: { quality_score_min: quality, timing_score_min: timing, actions: ["buy", "open"], stages: [] },
    sell_conditions: { take_profit_pct: profit / 100, stop_loss_pct: loss / 100, max_hold_days: days, score_actions: ["exit", "reduce"] },
    position_config: { type: "fixed_pct", value: 1, max_positions: 1 },
    execution_config: { entry_timing: "next_open", exit_timing: "next_open", entry_price_field: "open", exit_price_field: "open" },
  }));
  expect(requests.find((request) => request.url.endsWith("/backtest/run"))?.body.run_name).toBe("510300-评分回测");
});

it("shows no data instead of a price curve or buy/sell points before any single-symbol backtest", async () => {
  openApp();
  fireEvent.click(await screen.findByRole("button", { name: "回测中心" }));
  fireEvent.click(await screen.findByRole("button", { name: "单标的回测" }));
  const chart = screen.getByRole("heading", { name: "信号图" }).closest(".pt-card")!;
  expect(chart).toHaveTextContent("暂无数据");
  expect(chart).toHaveTextContent("还没有回测结果");
  expect(chart.querySelector("svg")).toBeNull();
  expect(within(chart as HTMLElement).queryByText(/买入信号|卖出信号/)).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "请先填写完整参数" })).toBeDisabled();
  expect(requests.filter((request) => request.method !== "GET" && !request.url.endsWith("/backtest/precheck"))).toEqual([]);
});

it.each([
  { missing: "prices and trades", priceSeries: [], tradeCount: 0 },
  { missing: "trade records despite available prices", priceSeries: [
    { symbol_id: 11, date: "2025-10-20", open: 3.2, high: 3.4, low: 3.1, close: 3.3 },
    { symbol_id: 11, date: "2025-10-21", open: 3.3, high: 3.5, low: 3.2, close: 3.4 },
  ], tradeCount: 2 },
])("preserves returned metrics but shows no data when a completed single-symbol result lacks $missing", async ({ priceSeries, tradeCount }) => {
  // This is an isolated HTTP response, never a live backtest or database record.
  singleRunDetail = {
    id: 77, portfolio_id: 1, run_name: "510300-MACD", symbols_json: "[11]", rule_config_json: "{}",
    start_date: "2025-10-20", end_date: "2026-10-20", initial_capital: 100000,
    status: "completed", created_at: "2026-10-21T12:00:00", total_return_pct: 0.1234,
    trade_count: tradeCount, price_series: priceSeries,
    equity_curve_json: '[{"date":"2025-10-20","equity":100000},{"date":"2026-10-20","equity":112340}]',
  };
  openApp();
  fireEvent.click(await screen.findByRole("button", { name: "回测中心" }));
  fireEvent.click(await screen.findByRole("button", { name: "单标的回测" }));
  fireEvent.change(screen.getByPlaceholderText("输入代码，如 600519"), { target: { value: "510300" } });
  fireEvent.click(await screen.findByRole("button", { name: "开始回测" }));
  expect(await screen.findByText("+12.3%")).toBeVisible();
  const chart = screen.getByRole("region", { name: "单标的信号图" });
  expect(chart).toHaveTextContent("暂无数据");
  expect(chart).toHaveTextContent("未提供完整的行情和交易记录");
  expect(chart.querySelector("svg")).toBeNull();
  expect(within(chart).queryByText(/买入信号|卖出信号/)).not.toBeInTheDocument();
  expect(await within(screen.getByRole("table")).findByText("这次回测没有交易记录。")).toBeVisible();
  expect(screen.queryByText("暂无回测结果，请配置参数后开始回测")).not.toBeInTheDocument();
  expect(requests.filter((request) => request.method !== "GET" && !request.url.endsWith("/backtest/precheck")).map((request) => request.url)).toEqual(["/api/v1/backtest/run"]);
});

it("restores edited portfolio backtest parameters after leaving and reloading without submitting", async () => {
  const view = openApp();
  fireEvent.click(await screen.findByRole("button", { name: "回测中心" }));
  const capital = await screen.findByPlaceholderText("1,000,000");
  fireEvent.change(capital, { target: { value: "321000" } });
  fireEvent.click(screen.getByTestId("portfolio-backtest-range-3y"));
  const dates = screen.getAllByTestId("portfolio-backtest-range-picker").map((input) => (input as HTMLInputElement).value);
  fireEvent.change(screen.getByLabelText("基准指数"), { target: { value: "中证500" } });
  fireEvent.change(screen.getByLabelText("佣金费率"), { target: { value: "0.05" } });
  fireEvent.click(screen.getByRole("button", { name: "设置" }));
  await waitFor(() => expect(document.querySelector('[data-active-tab="settings"]')).not.toBeNull());
  view.unmount();
  window.history.replaceState(null, "", "/?tab=portfolio&subtab=backtest");
  openApp();
  fireEvent.click(await screen.findByRole("button", { name: "回测中心" }));
  expect(await screen.findByPlaceholderText("1,000,000")).toHaveValue("321000");
  expect(screen.getAllByTestId("portfolio-backtest-range-picker").map((input) => (input as HTMLInputElement).value)).toEqual(dates);
  expect(screen.getByTestId("portfolio-backtest-range-3y")).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByLabelText("基准指数")).toHaveValue("中证500");
  expect(screen.getByLabelText("佣金费率")).toHaveValue("0.05");
  await waitFor(() => expect(requests.some((request) => request.url.endsWith("/backtest/precheck") && request.body.start_date === dates[0])).toBe(true));
  expect(requests.filter((request) => request.method !== "GET" && !request.url.endsWith("/backtest/precheck"))).toEqual([]);
});

it("keeps portfolio historical-status gaps and dates through the data page and still blocks on return", async () => {
  precheck = { ...clearPrecheck, symbols: [symbol], blocking_reasons: [
    { code: "STATUS_DATA_UNAVAILABLE", detail_zh: "每日历史状态缺失", evidence: {} },
    { code: "PRICE_LIMIT_DATA_UNAVAILABLE", detail_zh: "逐日涨跌停资料缺失", evidence: {} },
  ] };
  openApp();
  fireEvent.click(await screen.findByRole("button", { name: "回测中心" }));
  fireEvent.change(await screen.findByPlaceholderText("1,000,000"), { target: { value: "456000" } });
  fireEvent.click(screen.getByTestId("portfolio-backtest-range-3y"));
  const dates = screen.getAllByTestId("portfolio-backtest-range-picker").map((input) => (input as HTMLInputElement).value);
  fireEvent.click(await screen.findByRole("button", { name: "导入历史资料" }));
  const notice = await screen.findByRole("region", { name: "本次回测需要的资料" });
  expect(notice).toHaveTextContent("510300");
  expect(notice).toHaveTextContent("每日停牌、ST 和上市退市状态");
  expect(notice).toHaveTextContent("逐日涨跌停资料");
  await waitFor(() => expect(screen.getByLabelText("历史资料开始日期")).toHaveValue(dates[0]));
  expect(screen.getByLabelText("历史资料结束日期")).toHaveValue(dates[1]);
  expect(screen.getByRole("button", { name: "下载当前组合资料" })).toBeDisabled();
  expect(screen.getByText(/本次日期超出了来源可提供的范围/)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "按可下载日期填写" }));
  expect(screen.getByLabelText("历史资料开始日期")).toHaveValue("2025-10-20");
  expect(screen.getByLabelText("历史资料结束日期")).toHaveValue("2026-10-20");
  expect(notice).toHaveTextContent(dates[0]); expect(notice).toHaveTextContent(dates[1]);
  const checks = requests.filter((request) => request.url.endsWith("/backtest/precheck")).length;
  fireEvent.click(within(notice).getByRole("button", { name: "返回回测并检查资料" }));
  expect(await screen.findByPlaceholderText("1,000,000")).toHaveValue("456000");
  expect(screen.getAllByTestId("portfolio-backtest-range-picker").map((input) => (input as HTMLInputElement).value)).toEqual(dates);
  await waitFor(() => expect(requests.filter((request) => request.url.endsWith("/backtest/precheck")).length).toBeGreaterThan(checks));
  expect(await screen.findByTestId("precheck-blockers-inline")).toHaveTextContent("缺少停牌、ST、上市状态等逐日历史资料");
  expect(screen.getByRole("button", { name: "还不能开始回测" })).toBeDisabled();
  expect(requests.filter((request) => request.method !== "GET" && !request.url.endsWith("/backtest/precheck"))).toEqual([]);
});

it("keeps separate drafts when switching portfolios and returns to the portfolio that requested repair", async () => {
  precheck = { ...clearPrecheck, symbols: [symbol], blocking_reasons: [{ code: "STATUS_DATA_UNAVAILABLE", detail_zh: "逐日历史资料缺失", evidence: {} }] };
  openApp();
  fireEvent.click(await screen.findByRole("button", { name: "回测中心" }));
  fireEvent.change(await screen.findByPlaceholderText("1,000,000"), { target: { value: "111111" } });
  fireEvent.click(screen.getByTestId("portfolio-backtest-range-3y"));
  const firstDates = screen.getAllByTestId("portfolio-backtest-range-picker").map((input) => (input as HTMLInputElement).value);
  fireEvent.click(await screen.findByRole("button", { name: "导入历史资料" }));
  expect(await screen.findByRole("region", { name: "本次回测需要的资料" })).toHaveTextContent("本地组合甲");
  // The user can leave the repair page to inspect another portfolio.
  fireEvent.click(screen.getByRole("button", { name: "组合交易" }));
  fireEvent.click(await screen.findByRole("button", { name: /本地组合甲/ }));
  fireEvent.click(await screen.findByRole("option", { name: /本地组合乙/ }));
  await waitFor(() => expect(screen.getByPlaceholderText("1,000,000")).toHaveValue("1000000"));
  fireEvent.change(screen.getByPlaceholderText("1,000,000"), { target: { value: "222222" } });
  fireEvent.click(screen.getByTestId("portfolio-backtest-range-5y"));
  const secondDates = screen.getAllByTestId("portfolio-backtest-range-picker").map((input) => (input as HTMLInputElement).value);
  fireEvent.click(screen.getByRole("button", { name: "设置" }));
  const notice = await screen.findByRole("region", { name: "本次回测需要的资料" });
  expect(notice).toHaveTextContent("本地组合甲");
  fireEvent.click(within(notice).getByRole("button", { name: "返回回测并检查资料" }));
  expect(await screen.findByPlaceholderText("1,000,000")).toHaveValue("111111");
  expect(screen.getByRole("button", { name: /本地组合甲/ })).toBeVisible();
  expect(screen.getAllByTestId("portfolio-backtest-range-picker").map((input) => (input as HTMLInputElement).value)).toEqual(firstDates);
  fireEvent.click(screen.getByRole("button", { name: /本地组合甲/ }));
  fireEvent.click(await screen.findByRole("option", { name: /本地组合乙/ }));
  await waitFor(() => expect(screen.getByPlaceholderText("1,000,000")).toHaveValue("222222"));
  expect(screen.getAllByTestId("portfolio-backtest-range-picker").map((input) => (input as HTMLInputElement).value)).toEqual(secondDates);
  expect(requests.filter((request) => request.method !== "GET" && !request.url.endsWith("/backtest/precheck"))).toEqual([]);
});

it("keeps the form in place if repair details cannot be saved and lets the user retry", async () => {
  precheck = { ...clearPrecheck, symbols: [symbol], blocking_reasons: [{ code: "STATUS_DATA_UNAVAILABLE", detail_zh: "历史状态缺失", evidence: {} }] };
  openApp();
  fireEvent.click(await screen.findByRole("button", { name: "回测中心" }));
  fireEvent.change(await screen.findByPlaceholderText("1,000,000"), { target: { value: "333333" } });
  const repair = await screen.findByRole("button", { name: "导入历史资料" });
  const originalSetItem = Storage.prototype.setItem;
  const storage = vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key, value) {
    if (key === "backtest-repair-return") throw new DOMException("Storage full", "QuotaExceededError");
    originalSetItem.call(this, key, value);
  });
  fireEvent.click(repair);
  expect(await screen.findByRole("alert")).toHaveTextContent("补资料所需的信息没有保存成功");
  expect(screen.getByPlaceholderText("1,000,000")).toHaveValue("333333");
  expect(screen.queryByRole("region", { name: "本次回测需要的资料" })).not.toBeInTheDocument();
  storage.mockRestore();
  fireEvent.click(repair);
  expect(await screen.findByRole("region", { name: "本次回测需要的资料" })).toHaveTextContent("本地组合甲");
  expect(requests.filter((request) => request.method !== "GET" && !request.url.endsWith("/backtest/precheck"))).toEqual([]);
});

it("does not offer cancellation or retry of another range's active history task", async () => {
  otherHistoryRunning = true;
  openApp();
  fireEvent.click(await screen.findByRole("button", { name: "回测中心" }));
  fireEvent.click(await screen.findByRole("button", { name: "单标的回测" }));
  fireEvent.change(screen.getByPlaceholderText("输入代码，如 600519"), { target: { value: "510300" } });
  fireEvent.click(await screen.findByRole("button", { name: "开始回测" }));
  fireEvent.click(await screen.findByRole("button", { name: "去补资料" }));
  expect(await screen.findByText("另有历史资料任务正在运行，本次补充还没有开始。请等它结束后重新读取。")).toBeVisible();
  expect(screen.getByRole("button", { name: /补充所需资料/ })).toBeDisabled();
  expect(screen.queryByRole("button", { name: /取消任务|仅重试失败项|重试上次/ })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "返回回测并检查资料" })).toBeEnabled();
  expect(requests.filter((request) => request.method !== "GET").every((request) => request.url.endsWith("/backtest/precheck") || request.url.endsWith("/backtest/run"))).toBe(true);
});

it("blocks single-symbol submission while historical evidence is missing and offers the exact repair range", async () => {
  precheck = { ...clearPrecheck, symbols: [symbol], blocking_reasons: [{ code: "STATUS_DATA_UNAVAILABLE", detail_zh: "缺少逐日历史状态", evidence: {} }] };
  openApp();
  fireEvent.click(await screen.findByRole("button", { name: "回测中心" }));
  fireEvent.click(await screen.findByRole("button", { name: "单标的回测" }));
  fireEvent.change(screen.getByPlaceholderText("输入代码，如 600519"), { target: { value: "510300" } });
  fireEvent.click(screen.getByTestId("single-backtest-range-3y"));
  const dates = screen.getAllByTestId("single-backtest-range-picker").map((input) => (input as HTMLInputElement).value);
  await screen.findByText(/缺少停牌、ST、上市状态等逐日历史资料/);
  expect(screen.getByRole("button", { name: "先补齐资料" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "去补资料" }));
  const notice = await screen.findByRole("region", { name: "本次回测需要的资料" });
  expect(notice).toHaveTextContent("510300");
  expect(notice).toHaveTextContent("每日停牌、ST 和上市退市状态");
  expect(notice).toHaveTextContent(dates[0]); expect(notice).toHaveTextContent(dates[1]);
  fireEvent.click(within(notice).getByRole("button", { name: "返回回测并检查资料" }));
  expect(await screen.findByPlaceholderText("输入代码，如 600519")).toHaveValue("510300");
  expect(await screen.findByRole("button", { name: "先补齐资料" })).toBeDisabled();
  expect(requests.filter((request) => request.method !== "GET" && !request.url.endsWith("/backtest/precheck"))).toEqual([]);
});

it("keeps the single-symbol run disabled during a pending or failed check and restores it by read-only retry", async () => {
  let finish!: (response: Response) => void;
  checkReply = () => new Promise((resolve) => { finish = resolve; });
  openApp();
  fireEvent.click(await screen.findByRole("button", { name: "回测中心" }));
  fireEvent.click(await screen.findByRole("button", { name: "单标的回测" }));
  fireEvent.change(screen.getByPlaceholderText("输入代码，如 600519"), { target: { value: "510300" } });
  const checking = await screen.findByRole("button", { name: "正在检查资料…" });
  expect(checking).toBeDisabled();
  fireEvent.click(checking);
  await waitFor(() => expect(finish).toBeTypeOf("function"));
  await act(async () => finish(json({ detail: "临时连接失败" }, 503)));
  expect(await screen.findByRole("button", { name: "先重新检查资料" })).toBeDisabled();
  checkReply = null;
  fireEvent.click(screen.getByRole("button", { name: "重新检查资料" }));
  expect(await screen.findByRole("button", { name: "开始回测" })).toBeEnabled();
  expect(requests.filter((request) => request.method !== "GET" && !request.url.endsWith("/backtest/precheck"))).toEqual([]);
});

it("preserves the standalone day rules and validates capital and commissions in place", async () => {
  precheck = { ...clearPrecheck, requested_trade_days: 20, usable_trade_days: 20,
    blocking_reasons: [{ code: "INSUFFICIENT_TRADE_DAYS", detail_zh: "当前区间少于组合回测要求", evidence: {} }],
    warnings: [{ code: "SYMBOL_POOL_TOO_SMALL", title_zh: "标的较少" }],
  };
  openApp();
  fireEvent.click(await screen.findByRole("button", { name: "回测中心" }));
  fireEvent.click(await screen.findByRole("button", { name: "单标的回测" }));
  fireEvent.change(screen.getByPlaceholderText("输入代码，如 600519"), { target: { value: "510300" } });
  expect(await screen.findByRole("button", { name: "开始回测" })).toBeEnabled();
  fireEvent.change(screen.getByRole("textbox", { name: "单标的初始资金" }), { target: { value: "-1" } });
  expect(screen.getByText("初始资金请填写大于 0 的数字。")).toBeVisible();
  expect(screen.getByRole("button", { name: "请先填写完整参数" })).toBeDisabled();
  fireEvent.change(screen.getByRole("textbox", { name: "单标的初始资金" }), { target: { value: "123,456" } });
  fireEvent.change(screen.getByRole("textbox", { name: "单标的佣金费率" }), { target: { value: "-0.03" } });
  expect(screen.getByText("佣金费率请填写 0 到 100 之间的数字，单位是 %。")).toBeVisible();
  expect(screen.getByRole("button", { name: "请先填写完整参数" })).toBeDisabled();
  fireEvent.change(screen.getByRole("textbox", { name: "单标的佣金费率" }), { target: { value: "0" } });
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  await waitFor(() => expect(requests.find((request) => request.url.endsWith("/backtest/run"))?.body).toMatchObject({ initial_capital: 123456, cost_config: { commission_rate: 0 } }));
  expect(requests.filter((request) => request.url.endsWith("/backtest/run"))).toHaveLength(1);
  expect(requests.filter((request) => request.url.endsWith("/backtest/precheck")).every((request) => request.body.minimum_trade_days === undefined)).toBe(true);
});
