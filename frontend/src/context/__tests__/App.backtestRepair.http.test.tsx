import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
const symbol = { id: 11, symbol: "510300", name: "测试ETF", asset_type: "etf", market: "sh", region: "cn" };

beforeEach(() => {
  localStorage.clear(); sessionStorage.clear(); setLocale("zh-CN"); requests.length = 0;
  precheck = clearPrecheck;
  oldHistory = false;
  checkFailure = false;
  otherHistoryRunning = false;
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
    if (url.endsWith("/backtest/precheck")) return checkFailure ? json({ detail: "临时连接失败" }, 503) : json(precheck);
    if (url.includes("/symbols?") && url.includes("keyword=510300")) return json([symbol]);
    if (url.endsWith("/backtest/run")) return json({ detail: { code: "BACKTEST_SCORE_COVERAGE_INSUFFICIENT", message: "评分资料不足" } }, 422);
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
  const firstView = openApp();
  fireEvent.click(await screen.findByRole("button", { name: "回测中心" }));
  fireEvent.click(await screen.findByRole("button", { name: "单标的回测" }));
  fireEvent.change(screen.getByPlaceholderText("输入代码，如 600519"), { target: { value: "510300" } });
  fireEvent.click(screen.getByTestId("single-backtest-range-3y"));
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "RSI" } });
  fireEvent.change(screen.getByPlaceholderText("100,000"), { target: { value: "222000" } });
  fireEvent.change(screen.getByDisplayValue("0.03"), { target: { value: "0.07" } });
  const dates = screen.getAllByTestId("single-backtest-range-picker").map((input) => (input as HTMLInputElement).value);
  const start = screen.getByRole("button", { name: "开始回测" });
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
  expect(screen.getByRole("combobox")).toHaveValue("RSI");
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
  cleanup(); await act(async () => { message.destroy(); }); vi.restoreAllMocks(); vi.unstubAllGlobals();
});

function openApp() {
  return render(<AntdApp><AppProvider><App /></AppProvider></AntdApp>);
}

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
  const checks = requests.filter((request) => request.url.endsWith("/backtest/precheck")).length;
  fireEvent.click(within(notice).getByRole("button", { name: "返回回测并检查资料" }));
  expect(await screen.findByPlaceholderText("1,000,000")).toHaveValue("456000");
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
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  fireEvent.click(await screen.findByRole("button", { name: "去补资料" }));
  expect(await screen.findByText("另有历史资料任务正在运行，本次补充还没有开始。请等它结束后重新读取。")).toBeVisible();
  expect(screen.getByRole("button", { name: /补充所需资料/ })).toBeDisabled();
  expect(screen.queryByRole("button", { name: /取消任务|仅重试失败项|重试上次/ })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "返回回测并检查资料" })).toBeEnabled();
  expect(requests.filter((request) => request.method !== "GET").every((request) => request.url.endsWith("/backtest/precheck") || request.url.endsWith("/backtest/run"))).toBe(true);
});
