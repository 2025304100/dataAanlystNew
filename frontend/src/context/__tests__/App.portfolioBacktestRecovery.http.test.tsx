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
const requests: Array<{ url: string; method: string; body: any; signal?: AbortSignal | null }> = [];
const pendingResponses: Array<(response: Response) => void> = [];
function deferredResponse() {
  let resolve!: (response: Response) => void;
  const promise = new Promise<Response>((done) => { resolve = done; });
  pendingResponses.push(resolve);
  return { promise, resolve };
}
function portfolioRun(portfolioId = 1, overrides: Record<string, unknown> = {}) {
  return { id: 100 + portfolioId, portfolio_id: portfolioId, run_name: portfolioId === 1 ? "甲的组合回测" : "乙的组合回测",
    symbols_json: "[11]", rule_config_json: "{}", cost_config_json: JSON.stringify({ commission_rate: 0.0003 }),
    engine_name: "event_driven", source_type: "members", benchmark: "沪深300",
    start_date: "2023-01-01", end_date: "2025-01-01", initial_capital: 1000000,
    status: "completed", stage: "success", created_at: "2026-10-23T10:00:00", total_return_pct: 0.21,
    equity_curve_json: JSON.stringify([{ date: "2023-01-01", equity: 1000000 }, { date: "2025-01-01", equity: 1210000 }]),
    ...overrides };
}
function summary(portfolioId = 1, overrides: Record<string, unknown> = {}) {
  const run = portfolioRun(portfolioId);
  return { ...run, run_id: run.id, symbol_ids: [11], symbol_count: 1,
    equity_curve: [{ date: "2023-01-01", equity: 1000000 }, { date: "2025-01-01", equity: 1210000 }],
    metrics: { annual_return_pct: 0.10 }, diagnostics: {}, warnings: [], errors: [], ...overrides };
}
let histories: Record<string, unknown>[];
let submitReply: ((body: any) => Promise<Response>) | null;
let detailReply: ((id: number, signal?: AbortSignal | null) => Promise<Response>) | null;
let checkReply: ((body: any) => Promise<Response>) | null;
let tradesReply: ((id: number) => Promise<Response>) | null;

beforeEach(() => {
  localStorage.clear(); sessionStorage.clear(); setLocale("zh-CN"); requests.length = 0;
  histories = []; submitReply = null; detailReply = null; checkReply = null; tradesReply = null;
  for (const portfolio of portfolios) localStorage.setItem(`backtest-parameters:${portfolio.id}`, JSON.stringify({ version: 1,
    portfolioId: portfolio.id, parameters: { mode: "portfolio", portStart: "2023-01-01", portEnd: "2025-01-01",
      portCapital: "1000000", portCommission: "0.03", portBenchmark: "沪深300" } }));
  window.history.replaceState(null, "", "/?tab=portfolio&subtab=backtest");
  // Exercise the real App/provider/client. Only HTTP is replaced; no live write or job can be sent.
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input), method = init?.method || "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    requests.push({ url, method, body, signal: init?.signal });
    if (url === "/api/v1/portfolios") return json(portfolios);
    if (url.includes("/dashboard/workbench?")) {
      const portfolio = portfolios[Number(new URL(url, "http://test").searchParams.get("portfolio_id")) === 2 ? 1 : 0];
      return json({ portfolio, candidates: [], latest_scores: [], positions: [], actions: [], watchlists: [], active_rule: null });
    }
    if (url.endsWith("/backtest/precheck")) return checkReply ? checkReply(body) : json({ requested_trade_days: 500, usable_trade_days: 500,
      minimum_trade_days: 240, excluded_symbol_days: {}, warnings: [], blocking_reasons: [], filter_config_hash: "portfolio-only", production_fidelity: true });
    if (url.endsWith("/backtest/portfolio/run")) return submitReply ? submitReply(body) : json(summary(body.portfolio_id, body));
    if (url.includes("/backtest/runs?")) {
      const portfolioId = Number(new URL(url, "http://test").searchParams.get("portfolio_id"));
      return json(histories.filter((run) => run.portfolio_id === portfolioId));
    }
    const detail = url.match(/\/backtest\/runs\/(\d+)$/);
    if (detail) return detailReply ? detailReply(Number(detail[1]), init?.signal) : json(portfolioRun(Number(detail[1]) === 102 ? 2 : 1));
    const trades = url.match(/\/backtest\/runs\/(\d+)\/trades\?/);
    if (trades) return tradesReply ? tradesReply(Number(trades[1])) : json({ total: 0, page: 1, page_size: 20, items: [] });
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
    if (method !== "GET") throw new Error(`Unexpected write: ${url}`);
    return json([]);
  }));
});
afterEach(async () => {
  cleanup();
  await act(async () => { for (const resolve of pendingResponses.splice(0)) resolve(json({ detail: "测试已结束" }, 503)); message.destroy(); });
  vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals();
});
async function openPortfolio() {
  const view = render(<AntdApp><AppProvider><App /></AppProvider></AntdApp>);
  fireEvent.click(await screen.findByRole("button", { name: "回测中心" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "开始回测" })).toBeEnabled());
  return view;
}
function nonCheckWrites() { return requests.filter((request) => request.method !== "GET" && !request.url.endsWith("/backtest/precheck")); }
function annualRow() { return screen.getByText("年化收益").parentElement!; }
async function switchPortfolio(from: string, to: string) {
  fireEvent.click(screen.getByRole("button", { name: new RegExp(from) }));
  fireEvent.click(await screen.findByRole("option", { name: new RegExp(to) }));
}

it("does not label a historical portfolio's period return as annual return when the annual value was not recorded", async () => {
  histories = [portfolioRun()];
  await openPortfolio();
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  fireEvent.click(await screen.findByText("甲的组合回测"));
  await screen.findByText("+21.0%", { selector: "p" });
  expect(within(annualRow()).getByText("未提供")).toBeVisible();
  expect(annualRow()).not.toHaveTextContent("+21.0%");
  expect(nonCheckWrites()).toEqual([]);
});

it("keeps a missing annual return unprovided in a new portfolio summary without replacing its period return", async () => {
  submitReply = async () => json(summary(1, { metrics: {} }));
  await openPortfolio();
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  await screen.findByText("+21.0%", { selector: "p" });
  expect(within(annualRow()).getByText("未提供")).toBeVisible();
  expect(nonCheckWrites()).toHaveLength(1);
});

it.each([{ annual: 0.10, expected: "+10.0%" }, { annual: 0, expected: "0.0%" }])("shows the recorded annual return $annual independently of total return", async ({ annual, expected }) => {
  submitReply = async () => json(summary(1, { metrics: { annual_return_pct: annual } }));
  await openPortfolio();
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  await screen.findByText("+21.0%", { selector: "p" });
  expect(within(annualRow()).getByText(expected)).toBeVisible();
  expect(nonCheckWrites()).toHaveLength(1);
});

it("keeps a portfolio result's original parameters and hides it after the current capital is changed", async () => {
  await openPortfolio();
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  await screen.findByText("+21.0%", { selector: "p" });
  const parameters = screen.getByRole("region", { name: "组合回测结果对应的参数" });
  expect(parameters).toHaveTextContent("2023-01-01 至 2025-01-01");
  expect(parameters).toHaveTextContent("初始资金 1,000,000 元");
  expect(parameters).toHaveTextContent("佣金 0.03%");
  expect(parameters).toHaveTextContent("基准 沪深300");
  fireEvent.change(screen.getByPlaceholderText("1,000,000"), { target: { value: "2000000" } });
  expect(parameters).toHaveTextContent("参数已修改，尚未按当前参数回测。上次结果已收起。");
  expect(parameters).toHaveTextContent("初始资金 1,000,000 元");
  expect(parameters).not.toHaveTextContent("初始资金 2,000,000 元");
  expect(screen.getByPlaceholderText("1,000,000")).toHaveValue("2000000");
  expect(screen.queryByText("+21.0%", { selector: "p" })).not.toBeInTheDocument();
  expect(screen.queryByRole("tab", { name: "交易流水 0" })).not.toBeInTheDocument();
  expect(nonCheckWrites()).toHaveLength(1);
});

it("ignores a late portfolio submission after capital changes and changes back, then unlocks a deliberate new submission", async () => {
  const pending = deferredResponse();
  submitReply = async () => pending.promise;
  await openPortfolio();
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  await waitFor(() => expect(nonCheckWrites()).toHaveLength(1));
  fireEvent.change(screen.getByPlaceholderText("1,000,000"), { target: { value: "2000000" } });
  fireEvent.change(screen.getByPlaceholderText("1,000,000"), { target: { value: "1000000" } });
  await act(async () => { pending.resolve(json(summary())); });
  await waitFor(() => expect(screen.getByRole("button", { name: "开始回测" })).toBeEnabled());
  expect(screen.queryByText("+21.0%", { selector: "p" })).not.toBeInTheDocument();
  expect(nonCheckWrites()).toHaveLength(1);
  submitReply = async (body) => json(summary(1, { ...body, run_id: 103, total_return_pct: 0.32 }));
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  await screen.findByText("+32.0%", { selector: "p" });
  expect(nonCheckWrites()).toHaveLength(2);
  expect(screen.getByRole("region", { name: "组合回测结果对应的参数" })).toHaveTextContent("回测记录 #103 的参数");
});

it("does not restore a late portfolio history response after the commission changes and changes back", async () => {
  histories = [portfolioRun()];
  const pending = deferredResponse();
  detailReply = () => pending.promise;
  await openPortfolio();
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  fireEvent.click(await screen.findByText("甲的组合回测"));
  await waitFor(() => expect(requests.some((request) => request.url.endsWith("/backtest/runs/101"))).toBe(true));
  fireEvent.click(screen.getByText("更多设置"));
  const commission = screen.getByRole("textbox", { name: "佣金费率" });
  fireEvent.change(commission, { target: { value: "0.05" } });
  fireEvent.change(commission, { target: { value: "0.03" } });
  await act(async () => { pending.resolve(json(portfolioRun())); });
  expect(screen.queryByText("+21.0%", { selector: "p" })).not.toBeInTheDocument();
  expect(screen.queryByText("正在读取回测结果…")).not.toBeInTheDocument();
  expect(nonCheckWrites()).toEqual([]);
});

it("times out portfolio progress after 20 seconds and retries the same run without accepting the old response", async () => {
  const late = deferredResponse();
  submitReply = async (body) => json(summary(1, { ...body, status: "running", stage: "running", total_return_pct: null, metrics: {}, equity_curve: [] }));
  detailReply = () => late.promise;
  await openPortfolio();
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  await act(async () => { await vi.advanceTimersByTimeAsync(500); });
  const reads = () => requests.filter((request) => request.url.endsWith("/backtest/runs/101"));
  expect(reads()).toHaveLength(1);
  await act(async () => { await vi.advanceTimersByTimeAsync(19_999); });
  expect(screen.queryByRole("button", { name: "重新查看进度" })).not.toBeInTheDocument();
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(screen.getByText("读取回测进度超过20秒，请重新查看。原任务可能仍在运行。")).toBeVisible();
  expect(screen.getByRole("button", { name: "重新查看进度" })).toBeEnabled();
  expect(reads()[0].signal?.aborted).toBe(true);
  expect(screen.getByRole("region", { name: "组合回测结果对应的参数" })).toHaveTextContent("回测记录 #101 的参数");
  expect(nonCheckWrites()).toHaveLength(1);
  detailReply = async () => json(portfolioRun(1, { total_return_pct: 0.32 }));
  fireEvent.click(screen.getByRole("button", { name: "重新查看进度" }));
  await act(async () => { await vi.advanceTimersByTimeAsync(500); });
  expect(reads()).toHaveLength(2);
  expect(screen.getByText("+32.0%", { selector: "p" })).toBeVisible();
  expect(screen.queryByRole("button", { name: "重新查看进度" })).not.toBeInTheDocument();
  await act(async () => { late.resolve(json(portfolioRun(1, { total_return_pct: 0.90 }))); });
  expect(screen.queryByText("+90.0%", { selector: "p" })).not.toBeInTheDocument();
  expect(screen.getByText("+32.0%", { selector: "p" })).toBeVisible();
  expect(nonCheckWrites()).toHaveLength(1);
});

it.each(["commission", "benchmark", "dates"])("hides the whole portfolio result after changing %s and keeps its submitted parameters", async (field) => {
  await openPortfolio();
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  await screen.findByText("+21.0%", { selector: "p" });
  fireEvent.click(screen.getByText("更多设置"));
  if (field === "commission") fireEvent.change(screen.getByRole("textbox", { name: "佣金费率" }), { target: { value: "0.05" } });
  if (field === "benchmark") fireEvent.change(screen.getByRole("combobox", { name: "基准指数" }), { target: { value: "中证500" } });
  if (field === "dates") fireEvent.click(screen.getByRole("button", { name: "近三年" }));
  const parameters = screen.getByRole("region", { name: "组合回测结果对应的参数" });
  expect(parameters).toHaveTextContent("参数已修改，尚未按当前参数回测。上次结果已收起。");
  expect(parameters).toHaveTextContent("2023-01-01 至 2025-01-01");
  expect(parameters).toHaveTextContent("佣金 0.03%");
  expect(parameters).toHaveTextContent("基准 沪深300");
  expect(screen.queryByText("+21.0%", { selector: "p" })).not.toBeInTheDocument();
  expect(screen.queryByRole("tab", { name: /交易流水/ })).not.toBeInTheDocument();
  expect(nonCheckWrites()).toHaveLength(1);
});

it("opens historical portfolio parameters without replacing the current draft or inventing a missing commission", async () => {
  histories = [portfolioRun(1, { start_date: "2022-01-01", initial_capital: 500000, cost_config_json: "{}", benchmark: "中证500" })];
  detailReply = async () => json(histories[0]);
  await openPortfolio();
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  fireEvent.click(await screen.findByText("甲的组合回测"));
  await screen.findByText("+21.0%", { selector: "p" });
  const parameters = screen.getByRole("region", { name: "组合回测结果对应的参数" });
  expect(parameters).toHaveTextContent("2022-01-01 至 2025-01-01");
  expect(parameters).toHaveTextContent("初始资金 500,000 元");
  expect(parameters).toHaveTextContent("佣金 未记录");
  expect(parameters).toHaveTextContent("基准 中证500");
  expect(screen.getByPlaceholderText("1,000,000")).toHaveValue("1000000");
  expect(nonCheckWrites()).toEqual([]);
});

it("does not submit after parameters change during the second portfolio data check and releases its own loading state", async () => {
  await openPortfolio();
  const pending = deferredResponse();
  checkReply = () => pending.promise;
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  await waitFor(() => expect(requests.filter((request) => request.url.endsWith("/backtest/precheck"))).toHaveLength(2));
  fireEvent.click(screen.getByText("更多设置"));
  fireEvent.change(screen.getByRole("textbox", { name: "佣金费率" }), { target: { value: "0.05" } });
  await act(async () => pending.resolve(json({ requested_trade_days: 500, usable_trade_days: 500, minimum_trade_days: 240, blocking_reasons: [], warnings: [] })));
  await waitFor(() => expect(screen.getByRole("button", { name: "开始回测" })).toBeEnabled());
  expect(screen.getByRole("textbox", { name: "佣金费率" })).toHaveValue("0.05");
  expect(nonCheckWrites()).toEqual([]);
});

it("does not display a late portfolio submission in another portfolio", async () => {
  const pending = deferredResponse();
  submitReply = () => pending.promise;
  await openPortfolio();
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  await waitFor(() => expect(nonCheckWrites()).toHaveLength(1));
  await switchPortfolio("本地组合甲", "本地组合乙");
  await waitFor(() => expect(screen.getByRole("button", { name: "开始回测" })).toBeEnabled());
  await act(async () => pending.resolve(json(summary())));
  expect(screen.queryByText("+21.0%", { selector: "p" })).not.toBeInTheDocument();
  expect(screen.queryByRole("region", { name: "组合回测结果对应的参数" })).not.toBeInTheDocument();
  expect(screen.getByPlaceholderText("1,000,000")).toHaveValue("1000000");
  expect(nonCheckWrites()).toHaveLength(1);
});

it("cancels a hanging progress read when switching portfolios and keeps the next portfolio's history result", async () => {
  const late = deferredResponse();
  histories = [portfolioRun(2, { total_return_pct: 0.32 })];
  submitReply = async (body) => json(summary(1, { ...body, status: "running", stage: "running" }));
  detailReply = (id) => id === 101 ? late.promise : Promise.resolve(json(portfolioRun(2, { total_return_pct: 0.32 })));
  await openPortfolio();
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  await waitFor(() => expect(requests.some((request) => request.url.endsWith("/backtest/runs/101"))).toBe(true));
  const oldRead = requests.find((request) => request.url.endsWith("/backtest/runs/101"))!;
  await switchPortfolio("本地组合甲", "本地组合乙");
  expect(oldRead.signal?.aborted).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  fireEvent.click(await screen.findByText("乙的组合回测"));
  await screen.findByText("+32.0%", { selector: "p" });
  await act(async () => late.resolve(json(portfolioRun(1, { total_return_pct: 0.90 }))));
  expect(screen.getByRole("region", { name: "组合回测结果对应的参数" })).toHaveTextContent("回测记录 #102 的参数");
  expect(screen.getByText("+32.0%", { selector: "p" })).toBeVisible();
  expect(screen.queryByText("+90.0%", { selector: "p" })).not.toBeInTheDocument();
  expect(nonCheckWrites()).toHaveLength(1);
});

it("keeps three automatic retries for ordinary progress failures, then lets the user read that record again", async () => {
  submitReply = async (body) => json(summary(1, { ...body, status: "running", stage: "running" }));
  detailReply = async () => json({ detail: "连接暂时中断" }, 503);
  await openPortfolio();
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  await act(async () => { await vi.advanceTimersByTimeAsync(500); });
  expect(screen.getByText("暂时读不到回测进度，正在重试。")).toBeVisible();
  await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
  expect(screen.getByRole("button", { name: "重新查看进度" })).toBeEnabled();
  expect(requests.filter((request) => request.url.endsWith("/backtest/runs/101"))).toHaveLength(3);
  detailReply = async () => json(portfolioRun());
  fireEvent.click(screen.getByRole("button", { name: "重新查看进度" }));
  await act(async () => { await vi.advanceTimersByTimeAsync(500); });
  expect(screen.getByText("+21.0%", { selector: "p" })).toBeVisible();
  expect(nonCheckWrites()).toHaveLength(1);
});

it("does not attach the previous record number to a new portfolio submission's parameters while waiting", async () => {
  await openPortfolio();
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  await screen.findByText("+21.0%", { selector: "p" });
  fireEvent.change(screen.getByPlaceholderText("1,000,000"), { target: { value: "2000000" } });
  await waitFor(() => expect(screen.getByRole("button", { name: "开始回测" })).toBeEnabled());
  const pending = deferredResponse();
  submitReply = () => pending.promise;
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  await waitFor(() => expect(nonCheckWrites()).toHaveLength(2));
  const parameters = screen.getByRole("region", { name: "组合回测结果对应的参数" });
  expect(parameters).toHaveTextContent("本次提交的参数");
  expect(parameters).not.toHaveTextContent("回测记录 #101");
  expect(parameters).toHaveTextContent("初始资金 2,000,000 元");
  await act(async () => pending.resolve(json(summary(1, { run_id: 103, initial_capital: 2000000, total_return_pct: 0.32 }))));
  await screen.findByText("+32.0%", { selector: "p" });
  expect(parameters).toHaveTextContent("回测记录 #103 的参数");
});

it("explains why an unfinished historical result is hidden when its current form changes", async () => {
  const pending = deferredResponse();
  histories = [portfolioRun()];
  detailReply = () => pending.promise;
  await openPortfolio();
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  fireEvent.click(await screen.findByText("甲的组合回测"));
  await waitFor(() => expect(requests.some((request) => request.url.endsWith("/backtest/runs/101"))).toBe(true));
  fireEvent.change(screen.getByPlaceholderText("1,000,000"), { target: { value: "2000000" } });
  expect(screen.getByText("参数已修改，尚未按当前参数回测。上次结果已收起。")).toBeVisible();
  await act(async () => pending.resolve(json(portfolioRun())));
  expect(screen.getByText("参数已修改，尚未按当前参数回测。上次结果已收起。")).toBeVisible();
  expect(screen.queryByText("+21.0%", { selector: "p" })).not.toBeInTheDocument();
  expect(nonCheckWrites()).toEqual([]);
});

it("reopens a running portfolio history record with its real progress and hides unfinished returns and details", async () => {
  const running = portfolioRun(1, { status: "running", stage: "running", progress_pct: 65 });
  histories = [running];
  detailReply = async () => json(running);
  await openPortfolio();
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  fireEvent.click(await screen.findByText("甲的组合回测"));
  const state = await screen.findByRole("status", { name: "组合回测状态" });
  expect(state).toHaveTextContent("回测记录 #101");
  expect(state).toHaveTextContent("正在运行");
  expect(state).toHaveTextContent("65%");
  expect(screen.queryByTestId("backtest-gate-state")).not.toBeInTheDocument();
  expect(screen.queryByText("+21.0%", { selector: "p" })).not.toBeInTheDocument();
  expect(screen.queryByRole("tab", { name: /交易流水/ })).not.toBeInTheDocument();
  expect(nonCheckWrites()).toEqual([]);
});

it("updates the reopened portfolio record in place through running and completed states without submitting", async () => {
  const running = portfolioRun(1, { status: "running", stage: "running", progress_pct: 65 });
  histories = [running];
  detailReply = async () => json(running);
  await openPortfolio();
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  fireEvent.click(await screen.findByText("甲的组合回测"));
  await screen.findByRole("status", { name: "组合回测状态" });
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  detailReply = async () => json({ ...running, progress_pct: 85 });
  fireEvent.click(screen.getByRole("button", { name: "更新这条记录" }));
  await act(async () => { await vi.advanceTimersByTimeAsync(500); });
  expect(screen.getByRole("status", { name: "组合回测状态" })).toHaveTextContent("85%");
  detailReply = async () => json(portfolioRun(1, { total_return_pct: 0.32, progress_pct: 100 }));
  fireEvent.click(screen.getByRole("button", { name: "更新这条记录" }));
  await act(async () => { await vi.advanceTimersByTimeAsync(500); });
  expect(screen.queryByRole("status", { name: "组合回测状态" })).not.toBeInTheDocument();
  expect(screen.getByText("+32.0%", { selector: "p" })).toBeVisible();
  expect(screen.getByRole("region", { name: "组合回测结果对应的参数" })).toHaveTextContent("回测记录 #101 的参数");
  expect(requests.filter((request) => /\/backtest\/runs\/\d+$/.test(request.url)).every((request) => request.url.endsWith("/101"))).toBe(true);
  expect(nonCheckWrites()).toEqual([]);
});

it("preserves a recorded blocked stage even when the portfolio's legacy status still says running", async () => {
  const blocked = portfolioRun(1, { status: "running", stage: "blocked", progress_pct: 65 });
  histories = [blocked];
  detailReply = async () => json(blocked);
  await openPortfolio();
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  fireEvent.click(await screen.findByText("甲的组合回测"));
  expect(await screen.findByTestId("backtest-gate-state")).toHaveTextContent("回测所需资料还未备齐");
  expect(screen.queryByRole("status", { name: "组合回测状态" })).not.toBeInTheDocument();
  expect(screen.getByText("+21.0%", { selector: "p" })).not.toBeVisible();
  expect(nonCheckWrites()).toEqual([]);
});

it("keeps a restored record's progress through a read timeout and retries that record without duplicate updates", async () => {
  const running = portfolioRun(1, { status: "running", stage: "running", progress_pct: 65 });
  histories = [running]; detailReply = async () => json(running);
  await openPortfolio();
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  fireEvent.click(await screen.findByText("甲的组合回测"));
  await screen.findByRole("status", { name: "组合回测状态" });
  const late = deferredResponse(); detailReply = () => late.promise;
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  fireEvent.click(screen.getByRole("button", { name: "更新这条记录" }));
  await act(async () => { await vi.advanceTimersByTimeAsync(500); });
  expect(screen.getByRole("button", { name: "正在更新…" })).toBeDisabled();
  await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
  const state = screen.getByRole("status", { name: "组合回测状态" });
  expect(state).toHaveTextContent("回测记录 #101");
  expect(state).toHaveTextContent("65%");
  expect(state).toHaveTextContent("读取回测进度超过20秒");
  expect(screen.getAllByRole("button", { name: "重新查看进度" })).toHaveLength(1);
  expect(screen.getByRole("region", { name: "组合回测结果对应的参数" })).toHaveTextContent("初始资金 1,000,000 元");
  detailReply = async () => json(portfolioRun(1, { total_return_pct: 0.32 }));
  fireEvent.click(screen.getByRole("button", { name: "重新查看进度" }));
  await act(async () => { await vi.advanceTimersByTimeAsync(500); });
  expect(screen.getByText("+32.0%", { selector: "p" })).toBeVisible();
  await act(async () => late.resolve(json(portfolioRun(1, { total_return_pct: 0.90 }))));
  expect(screen.queryByText("+90.0%", { selector: "p" })).not.toBeInTheDocument();
  expect(screen.getByText("+32.0%", { selector: "p" })).toBeVisible();
  expect(nonCheckWrites()).toEqual([]);
});

it.each([{ tab: "trades", endpoint: "trades" }, { tab: "positions", endpoint: "positions" }, { tab: "rejected", endpoint: "evidence" }])(
  "waits for a reopened portfolio record to complete before reading its $tab details", async ({ tab, endpoint }) => {
    const running = portfolioRun(1, { status: "running", stage: "running", progress_pct: 65 });
    histories = [running]; detailReply = async () => json(running);
    window.history.replaceState(null, "", `/?tab=portfolio&subtab=backtest&bt_tab=${tab}`);
    await openPortfolio();
    fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
    fireEvent.click(await screen.findByText("甲的组合回测"));
    await screen.findByRole("status", { name: "组合回测状态" });
    expect(requests.filter((request) => request.url.includes(`/backtest/runs/101/${endpoint}?`))).toHaveLength(0);
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    detailReply = async () => json(portfolioRun());
    fireEvent.click(screen.getByRole("button", { name: "更新这条记录" }));
    await act(async () => { await vi.advanceTimersByTimeAsync(500); });
    expect(requests.filter((request) => request.url.includes(`/backtest/runs/101/${endpoint}?`))).toHaveLength(1);
    expect(nonCheckWrites()).toEqual([]);
  },
);

it.each([
  { status: "pending", stage: "queued", label: "正在等待" },
  { status: "queued", stage: "queued", label: "正在等待" },
  { status: "creating", stage: "creating", label: "正在准备" },
  { status: "precheck", stage: "precheck", label: "正在检查资料" },
  { status: "started", stage: undefined, label: "正在运行" },
  { status: "in_progress", stage: undefined, label: "正在运行" },
])("keeps portfolio history and restored progress consistent for $status", async ({ status, stage, label }) => {
  const run = portfolioRun(1, { status, stage, progress_pct: 30 });
  histories = [run]; detailReply = async () => json(run);
  await openPortfolio();
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  const row = await screen.findByRole("group", { name: "组合回测记录 #101" });
  expect(row).toHaveTextContent("进行中");
  expect(row).not.toHaveTextContent("+21.0%");
  fireEvent.click(within(row).getByText("甲的组合回测"));
  const state = await screen.findByRole("status", { name: "组合回测状态" });
  expect(state).toHaveTextContent(label);
  expect(state).toHaveTextContent("30%");
  expect(screen.getByRole("button", { name: "正在回测，请稍候…" })).toBeDisabled();
  expect(screen.queryByTestId("backtest-gate-state")).not.toBeInTheDocument();
  expect(nonCheckWrites()).toEqual([]);
});

it.each([null, undefined])("does not invent zero progress when the reopened record has progress %s", async (progress) => {
  const running = portfolioRun(1, { status: "running", stage: "running", progress_pct: progress });
  histories = [running]; detailReply = async () => json(running);
  await openPortfolio();
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  fireEvent.click(await screen.findByText("甲的组合回测"));
  const state = await screen.findByRole("status", { name: "组合回测状态" });
  expect(state).toHaveTextContent("进度未提供");
  expect(state).not.toHaveTextContent("0%");
  expect(nonCheckWrites()).toEqual([]);
});

it("keeps a data block returned by a restored progress read even while its status still says running", async () => {
  const running = portfolioRun(1, { status: "running", stage: "running", progress_pct: 65 });
  histories = [running]; detailReply = async () => json(running);
  await openPortfolio();
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  fireEvent.click(await screen.findByText("甲的组合回测"));
  await screen.findByRole("status", { name: "组合回测状态" });
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  detailReply = async () => json({ ...running, errors: [{ code: "DATA_BLOCKED", message: "历史停牌资料不完整" }] });
  fireEvent.click(screen.getByRole("button", { name: "更新这条记录" }));
  await act(async () => { await vi.advanceTimersByTimeAsync(500); });
  expect(screen.getByTestId("backtest-gate-state")).toHaveTextContent("历史停牌资料不完整");
  expect(screen.queryByRole("status", { name: "组合回测状态" })).not.toBeInTheDocument();
  expect(screen.getByText("+21.0%", { selector: "p" })).not.toBeVisible();
  expect(nonCheckWrites()).toEqual([]);
});

it("keeps the original progress retry available after the current portfolio draft changes", async () => {
  const running = portfolioRun(1, { status: "running", stage: "running", progress_pct: 65 });
  histories = [running]; detailReply = async () => json(running);
  await openPortfolio();
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  fireEvent.click(await screen.findByText("甲的组合回测"));
  await screen.findByRole("status", { name: "组合回测状态" });
  const late = deferredResponse(); detailReply = () => late.promise;
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  fireEvent.click(screen.getByRole("button", { name: "更新这条记录" }));
  await act(async () => { await vi.advanceTimersByTimeAsync(500); });
  await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
  fireEvent.change(screen.getByPlaceholderText("1,000,000"), { target: { value: "2000000" } });
  expect(screen.getByRole("button", { name: "重新查看进度" })).toBeEnabled();
  expect(screen.getByRole("region", { name: "组合回测结果对应的参数" })).toHaveTextContent("初始资金 1,000,000 元");
  detailReply = async () => json(portfolioRun(1, { total_return_pct: 0.32 }));
  fireEvent.click(screen.getByRole("button", { name: "重新查看进度" }));
  await act(async () => { await vi.advanceTimersByTimeAsync(500); });
  expect(screen.getByPlaceholderText("1,000,000")).toHaveValue("2000000");
  expect(screen.getByRole("region", { name: "组合回测结果对应的参数" })).toHaveTextContent("参数已修改，尚未按当前参数回测");
  expect(screen.queryByText("+32.0%", { selector: "p" })).not.toBeInTheDocument();
  expect(nonCheckWrites()).toEqual([]);
});

it.each([{ id: 102, portfolio_id: 2 }, { id: 103, portfolio_id: 1 }])(
  "rejects a progress response belonging to portfolio $portfolio_id record $id", async ({ id, portfolio_id }) => {
    const running = portfolioRun(1, { status: "running", stage: "running", progress_pct: 65 });
    histories = [running]; detailReply = async () => json(running);
    await openPortfolio();
    fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
    fireEvent.click(await screen.findByText("甲的组合回测"));
    await screen.findByRole("status", { name: "组合回测状态" });
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    detailReply = async () => json(portfolioRun(portfolio_id, { id, total_return_pct: 0.90 }));
    fireEvent.click(screen.getByRole("button", { name: "更新这条记录" }));
    await act(async () => { await vi.advanceTimersByTimeAsync(500); });
    expect(screen.queryByText("+90.0%", { selector: "p" })).not.toBeInTheDocument();
    expect(screen.getByRole("status", { name: "组合回测状态" })).toHaveTextContent("65%");
    expect(screen.getByRole("region", { name: "组合回测结果对应的参数" })).toHaveTextContent("回测记录 #101 的参数");
    expect(nonCheckWrites()).toEqual([]);
  },
);

it("restores the same ongoing portfolio record from its URL without resubmitting or reading unfinished trades", async () => {
  const running = portfolioRun(1, { status: "running", stage: "running", progress_pct: 65 });
  histories = [running]; detailReply = async () => json(running);
  window.history.replaceState(null, "", "/?tab=portfolio&subtab=backtest&bt_run=101&bt_tab=trades");
  render(<AntdApp><AppProvider><App /></AppProvider></AntdApp>);
  fireEvent.click(await screen.findByRole("button", { name: "回测中心" }));
  const state = await screen.findByRole("status", { name: "组合回测状态" });
  expect(state).toHaveTextContent("回测记录 #101");
  expect(state).toHaveTextContent("65%");
  expect(screen.getByRole("region", { name: "组合回测结果对应的参数" })).toHaveTextContent("2023-01-01 至 2025-01-01");
  expect(requests.some((request) => request.url.includes("/backtest/runs/101/trades?"))).toBe(false);
  expect(nonCheckWrites()).toEqual([]);
});

it("retains a restored record through three connection failures and retries only that record", async () => {
  const running = portfolioRun(1, { status: "running", stage: "running", progress_pct: 65 });
  histories = [running]; detailReply = async () => json(running);
  await openPortfolio();
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  fireEvent.click(await screen.findByText("甲的组合回测"));
  await screen.findByRole("status", { name: "组合回测状态" });
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  detailReply = async () => json({ detail: "连接暂时中断" }, 503);
  fireEvent.click(screen.getByRole("button", { name: "更新这条记录" }));
  await act(async () => { await vi.advanceTimersByTimeAsync(500); });
  await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
  const state = screen.getByRole("status", { name: "组合回测状态" });
  expect(state).toHaveTextContent("65%");
  expect(state).toHaveTextContent("任务可能仍在运行");
  expect(screen.getAllByRole("button", { name: "重新查看进度" })).toHaveLength(1);
  const beforeRetry = requests.filter((request) => request.url.endsWith("/backtest/runs/101")).length;
  await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
  expect(requests.filter((request) => request.url.endsWith("/backtest/runs/101"))).toHaveLength(beforeRetry);
  detailReply = async () => json(portfolioRun(1, { total_return_pct: 0.32 }));
  fireEvent.click(screen.getByRole("button", { name: "重新查看进度" }));
  await act(async () => { await vi.advanceTimersByTimeAsync(500); });
  expect(screen.getByText("+32.0%", { selector: "p" })).toBeVisible();
  expect(nonCheckWrites()).toEqual([]);
});

it.each(["success", "failure"])("ignores a restored record's late %s after another history record has opened", async (outcome) => {
  const running = portfolioRun(1, { status: "running", stage: "running", progress_pct: 65 });
  const completed = portfolioRun(1, { id: 103, run_name: "后来的组合回测", total_return_pct: 0.32 });
  histories = [running, completed]; detailReply = async () => json(running);
  await openPortfolio();
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  fireEvent.click(await screen.findByText("甲的组合回测"));
  await screen.findByRole("status", { name: "组合回测状态" });
  const late = deferredResponse();
  detailReply = (id) => id === 101 ? late.promise : Promise.resolve(json(completed));
  fireEvent.click(screen.getByRole("button", { name: "更新这条记录" }));
  await waitFor(() => expect(requests.filter((request) => request.url.endsWith("/backtest/runs/101")).length).toBeGreaterThan(1), { timeout: 3000 });
  const oldRead = requests.filter((request) => request.url.endsWith("/backtest/runs/101")).slice(-1)[0];
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  fireEvent.click(await screen.findByText("后来的组合回测"));
  await screen.findByText("+32.0%", { selector: "p" });
  expect(oldRead.signal?.aborted).toBe(true);
  await act(async () => late.resolve(outcome === "success" ? json(portfolioRun(1, { total_return_pct: 0.90 })) : json({ detail: "旧连接断开" }, 503)));
  expect(screen.getByText("+32.0%", { selector: "p" })).toBeVisible();
  expect(screen.queryByText("+90.0%", { selector: "p" })).not.toBeInTheDocument();
  expect(screen.queryByTestId("backtest-gate-state")).not.toBeInTheDocument();
  expect(screen.getByRole("region", { name: "组合回测结果对应的参数" })).toHaveTextContent("回测记录 #103 的参数");
  expect(nonCheckWrites()).toEqual([]);
});
