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
const rules = {
  buy_conditions: { quality_score_min: 65, timing_score_min: 75, actions: ["buy", "open"], stages: [] },
  sell_conditions: { take_profit_pct: 0.15, stop_loss_pct: 0.06, max_hold_days: 25, score_actions: ["exit", "reduce"] },
  position_config: { type: "fixed_pct", value: 1, max_positions: 1 },
  execution_config: { entry_timing: "next_open", exit_timing: "next_open", entry_price_field: "open", exit_price_field: "open" },
};
function run(portfolioId = 1, overrides: Record<string, unknown> = {}) {
  return { id: portfolioId === 1 ? 77 : 78, portfolio_id: portfolioId, run_name: portfolioId === 1 ? "甲的历史结果" : "乙的历史结果",
    symbols_json: "[11]", rule_config_json: JSON.stringify(rules),
    cost_config_json: JSON.stringify({ commission_rate: 0.0007, min_commission: 7, slippage_rate: 0.0025 }),
    start_date: "2025-10-20", end_date: "2026-10-20", initial_capital: portfolioId === 1 ? 100000 : 200000,
    status: "completed", created_at: "2026-10-21T12:00:00", total_return_pct: portfolioId === 1 ? 0.1234 : 0.3456,
    ...overrides };
}
function trade(runId: number, overrides: Record<string, unknown> = {}) {
  return { id: runId * 10, run_id: runId, symbol_id: 11, entry_date: runId === 77 ? "2026-01-02" : "2026-02-02", entry_price: 3.1234,
    quantity: 1000, entry_cost: 5.23, exit_date: "2026-03-08", exit_price: 3.4321, exit_cost: 6.34, pnl: 297.13, ...overrides };
}
const page = (runId: number) => ({ total: 1, page: 1, page_size: 20, items: [trade(runId)] });
const pendingResponses: Array<(response: Response) => void> = [];
function deferredResponse() {
  let resolve!: (response: Response) => void;
  const promise = new Promise<Response>((done) => { resolve = done; });
  pendingResponses.push(resolve);
  return { promise, resolve };
}
let detailReply: ((id: number, signal?: AbortSignal | null) => Promise<Response>) | null;
let submitReply: (() => Promise<Response>) | null;
let symbolsReply: (() => Promise<Response>) | null;
let checkReply: (() => Promise<Response>) | null;
let tradesReply: ((id: number) => Promise<Response>) | null;
let historiesReply: ((portfolioId: number, limit: number) => Promise<Response>) | null;
let histories: Record<string, unknown>[];
let precheck: Record<string, unknown>;
const submittedRuns = new Map<number, Record<string, unknown>>();
function submittedRun(id: number, overrides: Record<string, unknown> = {}) {
  return { ...(submittedRuns.get(id) ?? run(id === 77 ? 1 : 2)), ...overrides };
}

beforeEach(() => {
  localStorage.clear(); sessionStorage.clear(); setLocale("zh-CN"); requests.length = 0;
  detailReply = null; submitReply = null; tradesReply = null; historiesReply = null; histories = [];
  symbolsReply = null; checkReply = null;
  submittedRuns.clear();
  precheck = { requested_trade_days: 250, usable_trade_days: 250, minimum_trade_days: 240, excluded_symbol_days: {}, warnings: [], blocking_reasons: [], filter_config_hash: "restore-only", production_fidelity: true };
  window.history.replaceState(null, "", "/?tab=portfolio&subtab=backtest");
  // Use the real App, provider and client. Only HTTP is replaced; no live writes or jobs are possible.
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input), method = init?.method || "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    requests.push({ url, method, body });
    if (url === "/api/v1/portfolios") return json(portfolios);
    if (url.includes("/dashboard/workbench?")) {
      const portfolio = portfolios[Number(new URL(url, "http://test").searchParams.get("portfolio_id")) === 2 ? 1 : 0];
      return json({ portfolio, candidates: [], latest_scores: [], positions: [], actions: [], watchlists: [], active_rule: null });
    }
    if (url.endsWith("/backtest/precheck")) return checkReply ? checkReply() : json(precheck);
    if (url.includes("/symbols?")) return symbolsReply ? symbolsReply() : json([{ id: 11, symbol: "510300", name: "测试ETF", asset_type: "etf", market: "sh", region: "cn" }]);
    if (url.endsWith("/backtest/run")) {
      if (submitReply) return submitReply();
      const accepted = run(body.portfolio_id, { initial_capital: body.initial_capital, start_date: body.start_date, end_date: body.end_date,
        rule_config_json: JSON.stringify(body.rule_config), cost_config_json: JSON.stringify(body.cost_config) });
      submittedRuns.set(accepted.id, accepted);
      return json(accepted);
    }
    if (url.includes("/backtest/runs?")) {
      const query = new URL(url, "http://test").searchParams;
      return historiesReply ? historiesReply(Number(query.get("portfolio_id")), Number(query.get("limit"))) : json(histories);
    }
    const id = url.match(/\/backtest\/runs\/(77|78)$/);
    if (id) return detailReply ? detailReply(Number(id[1]), init?.signal) : json(submittedRun(Number(id[1])));
    const tradeId = url.match(/\/backtest\/runs\/(77|78)\/trades\?/);
    if (tradeId) return tradesReply ? tradesReply(Number(tradeId[1])) : json(page(Number(tradeId[1])));
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
function openApp() { return render(<AntdApp><AppProvider><App /></AppProvider></AntdApp>); }
async function openCenter() {
  const view = openApp();
  fireEvent.click(await screen.findByRole("button", { name: "回测中心" }));
  return view;
}
async function openSingle() {
  const view = await openCenter();
  fireEvent.click(screen.getByRole("button", { name: "单标的回测" }));
  fireEvent.change(screen.getByPlaceholderText("输入代码，如 600519"), { target: { value: "510300" } });
  return view;
}
async function submitSingle() {
  const start = await screen.findByRole("button", { name: "开始回测" });
  await waitFor(() => expect(start).toBeEnabled());
  fireEvent.click(start);
  await waitFor(() => expect(screen.getByRole("button", { name: "开始回测" })).toBeEnabled());
}
async function switchPortfolio(from: string, to: string) {
  fireEvent.click(screen.getByRole("button", { name: new RegExp(from) }));
  fireEvent.click(await screen.findByRole("option", { name: new RegExp(to) }));
}
async function createBothResults() {
  const view = await openSingle();
  await submitSingle();
  await screen.findByText("+12.3%");
  fireEvent.change(screen.getByRole("textbox", { name: "单标的初始资金" }), { target: { value: "111111" } });
  await switchPortfolio("本地组合甲", "本地组合乙");
  fireEvent.click(await screen.findByRole("button", { name: "单标的回测" }));
  fireEvent.change(screen.getByPlaceholderText("输入代码，如 600519"), { target: { value: "510300" } });
  fireEvent.change(screen.getByRole("textbox", { name: "单标的初始资金" }), { target: { value: "200000" } });
  await submitSingle();
  await screen.findByText("+34.6%");
  fireEvent.change(screen.getByRole("textbox", { name: "单标的初始资金" }), { target: { value: "222222" } });
  view.unmount();
  window.history.replaceState(null, "", "/?tab=portfolio&subtab=backtest");
}
function nonCheckWrites() { return requests.filter((request) => request.method !== "GET" && !request.url.endsWith("/backtest/precheck")); }
async function openUnfinishedHistory(record: ReturnType<typeof run>) {
  histories = [record];
  detailReply = async () => json(record);
  const view = await openSingle();
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  fireEvent.click(await screen.findByText(record.run_name));
  await screen.findByRole("button", { name: "更新这条记录" });
  return view;
}

it("opens an in-progress single-symbol record without calling it a failure or showing returns and trades", async () => {
  const running = run(1, { status: "running", total_return_pct: 0.90 });
  histories = [running];
  detailReply = async () => json(running);
  await openSingle();
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  fireEvent.click(await screen.findByText("甲的历史结果"));
  expect(await screen.findByRole("status", { name: "单标的回测状态" })).toHaveTextContent("这条回测还在进行");
  expect(screen.queryByRole("alert", { name: "单标的回测状态" })).not.toBeInTheDocument();
  expect(screen.queryByText("这条回测没有完成。")).not.toBeInTheDocument();
  expect(screen.queryByText("+90.0%")).not.toBeInTheDocument();
  expect(screen.queryByRole("region", { name: "单标的交易明细" })).not.toBeInTheDocument();
  expect(screen.getByRole("region", { name: "结果对应的参数" })).toHaveTextContent("回测记录 #77");
  expect(requests.filter((request) => request.url.includes("/trades?"))).toEqual([]);
  expect(nonCheckWrites()).toEqual([]);
});

it("keeps a record with a missing state unconfirmed even when it contains residual returns", async () => {
  const unknown = run(1, { status: "", total_return_pct: 0.90 });
  histories = [unknown];
  detailReply = async () => json(unknown);
  await openSingle();
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  fireEvent.click(await screen.findByText("甲的历史结果"));
  expect(await screen.findByRole("status", { name: "单标的回测状态" })).toHaveTextContent("状态暂时无法确认");
  expect(screen.queryByText("+90.0%")).not.toBeInTheDocument();
  expect(screen.queryByRole("region", { name: "单标的交易明细" })).not.toBeInTheDocument();
  expect(requests.filter((request) => request.url.includes("/trades?"))).toEqual([]);
  expect(nonCheckWrites()).toEqual([]);
});

it("updates an in-progress record in place to completed without resubmitting or replacing its draft", async () => {
  const pending = run(1, { status: "pending", total_return_pct: 0.90 });
  histories = [pending];
  detailReply = async () => json(pending);
  await openSingle();
  fireEvent.change(screen.getByRole("textbox", { name: "单标的初始资金" }), { target: { value: "111111" } });
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  fireEvent.click(await screen.findByText("甲的历史结果"));
  const update = await screen.findByRole("button", { name: "更新这条记录" });
  expect(update).toBeEnabled();
  detailReply = async () => json(run(1, { total_return_pct: 0.25 }));
  fireEvent.click(update);
  expect(await screen.findByText("+25.0%")).toBeVisible();
  expect(screen.getByRole("status", { name: "单标的回测状态" })).toHaveTextContent("这条回测已完成");
  expect(await within(screen.getByRole("region", { name: "单标的交易明细" })).findByText("2026-01-02")).toBeVisible();
  expect(screen.getByRole("textbox", { name: "单标的初始资金" })).toHaveValue("111111");
  expect(screen.getByRole("region", { name: "结果对应的参数" })).toHaveTextContent("初始资金 100,000 元");
  expect(screen.getByRole("region", { name: "结果对应的参数" })).toHaveTextContent("回测记录 #77");
  expect(requests.filter((request) => request.url.endsWith("/backtest/runs/77"))).toHaveLength(2);
  expect(nonCheckWrites()).toEqual([]);
});

it("does not announce completion when submission returns a pending record and updates only that record", async () => {
  const pending = run(1, { status: "pending", total_return_pct: 0.90 });
  submitReply = async () => json(pending);
  detailReply = async () => json(pending);
  await openSingle();
  await submitSingle();
  expect(screen.queryByText("单标的回测已完成")).not.toBeInTheDocument();
  expect(screen.getByRole("status", { name: "单标的回测状态" })).toHaveTextContent("还在进行");
  expect(screen.queryByText("+90.0%")).not.toBeInTheDocument();
  detailReply = async () => json(run(1, { total_return_pct: 0.25 }));
  fireEvent.click(await screen.findByRole("button", { name: "更新这条记录" }));
  expect(await screen.findByText("+25.0%")).toBeVisible();
  expect(nonCheckWrites()).toHaveLength(1);
  expect(requests.filter((request) => request.url.endsWith("/backtest/runs/77"))).toHaveLength(2);
});

it("keeps a recorded data block authoritative in both the single-symbol history and result", async () => {
  const blocked = run(1, { status: "running", total_return_pct: 0.90,
    warnings: [{ code: "DATA_MISSING", blocking: true, message: "逐日涨跌停资料尚未补齐" }] });
  histories = [blocked];
  detailReply = async () => json(blocked);
  await openSingle();
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  expect(await screen.findByRole("group", { name: "单标的回测记录 #77" })).toHaveTextContent("资料不足");
  fireEvent.click(screen.getByText("甲的历史结果"));
  expect(await screen.findByRole("alert", { name: "单标的回测状态" })).toHaveTextContent("逐日涨跌停资料尚未补齐");
  expect(screen.queryByText("+90.0%")).not.toBeInTheDocument();
  expect(requests.filter((request) => request.url.includes("/trades?"))).toEqual([]);
  expect(nonCheckWrites()).toEqual([]);
});

it.each([
  ["queued", "还在进行"], ["started", "还在进行"], ["in_progress", "还在进行"],
  ["unknown", "状态暂时无法确认"], ["   ", "状态暂时无法确认"], ["not_recorded", "状态暂时无法确认"],
] as const)("keeps %s records read-only until their state is confirmed", async (status, expected) => {
  await openUnfinishedHistory(run(1, { status, total_return_pct: 0.90 }));
  expect(screen.getByRole("status", { name: "单标的回测状态" })).toHaveTextContent(expected);
  expect(screen.queryByText("+90.0%")).not.toBeInTheDocument();
  expect(screen.queryByRole("region", { name: "单标的交易明细" })).not.toBeInTheDocument();
  expect(requests.filter((request) => request.url.includes("/trades?"))).toEqual([]);
  expect(nonCheckWrites()).toEqual([]);
});

it.each([
  { name: "completed with a recorded score error", fields: { status: "completed", errors: [{ code: "BACKTEST_SCORE_COVERAGE_INSUFFICIENT", message: "历史评分尚未补齐" }] }, reason: "历史评分尚未补齐" },
  { name: "unknown with a recorded data block", fields: { status: "unknown", blocking_status: "DATA_INCOMPLETE_PAUSED" }, reason: "回测所需资料还未备齐" },
  { name: "blocked without a recorded reason", fields: { status: "blocked" }, reason: "没有保存具体的阻断原因" },
])("retains the existing block for a record $name", async ({ fields, reason }) => {
  await openUnfinishedHistory(run(1, { ...fields, total_return_pct: 0.90 }));
  expect(screen.getByRole("alert", { name: "单标的回测状态" })).toHaveTextContent(reason);
  expect(screen.queryByText("+90.0%")).not.toBeInTheDocument();
  expect(requests.filter((request) => request.url.includes("/trades?"))).toEqual([]);
  expect(nonCheckWrites()).toEqual([]);
});

it("retries a failed state update in place and keeps its record and parameters without submitting", async () => {
  await openUnfinishedHistory(run(1, { status: "unknown", total_return_pct: 0.90 }));
  detailReply = async () => json({ detail: "读取状态时连接中断" }, 503);
  fireEvent.click(screen.getByRole("button", { name: "更新这条记录" }));
  expect(await screen.findByRole("alert", { name: "结果读取状态" })).toHaveTextContent("读取状态时连接中断");
  expect(screen.getByRole("alert", { name: "结果读取状态" })).toHaveTextContent("已保留记录编号和原参数");
  expect(screen.getByRole("status", { name: "单标的回测状态" })).toHaveTextContent("状态暂时无法确认");
  expect(screen.getByRole("region", { name: "结果对应的参数" })).toHaveTextContent("回测记录 #77");
  expect(screen.getByRole("region", { name: "结果对应的参数" })).toHaveTextContent("初始资金 100,000 元");
  expect(screen.queryByRole("region", { name: "单标的交易明细" })).not.toBeInTheDocument();
  detailReply = async () => json(run(1, { status: "running" }));
  fireEvent.click(screen.getByRole("button", { name: "重新读取结果" }));
  await screen.findByRole("button", { name: "更新这条记录" });
  expect(screen.getByRole("status", { name: "单标的回测状态" })).toHaveTextContent("还在进行");
  detailReply = async () => json(run(1, { total_return_pct: 0.25 }));
  fireEvent.click(screen.getByRole("button", { name: "更新这条记录" }));
  expect(await screen.findByText("+25.0%")).toBeVisible();
  expect(requests.filter((request) => request.url.endsWith("/backtest/runs/77"))).toHaveLength(4);
  expect(nonCheckWrites()).toEqual([]);
});

it("bounds a state update to 20 seconds and ignores its late response after a same-record retry", async () => {
  await openUnfinishedHistory(run(1, { status: "pending" }));
  const late = deferredResponse();
  detailReply = () => late.promise;
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  fireEvent.click(screen.getByRole("button", { name: "更新这条记录" }));
  await act(async () => { await vi.advanceTimersByTimeAsync(19_999); });
  expect(screen.getByRole("button", { name: "重新读取结果" })).toBeDisabled();
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(screen.getByRole("alert", { name: "结果读取状态" })).toHaveTextContent("读取完整结果超过20秒");
  expect(screen.getByRole("region", { name: "结果对应的参数" })).toHaveTextContent("回测记录 #77");
  detailReply = async () => json(run(1, { total_return_pct: 0.25 }));
  fireEvent.click(screen.getByRole("button", { name: "重新读取结果" }));
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  vi.useRealTimers();
  expect(await screen.findByText("+25.0%")).toBeVisible();
  await act(async () => late.resolve(json(run(1, { total_return_pct: 0.90 }))));
  expect(screen.queryByText("+90.0%")).not.toBeInTheDocument();
  expect(screen.getByText("+25.0%")).toBeVisible();
  expect(requests.filter((request) => request.url.endsWith("/backtest/runs/77"))).toHaveLength(3);
  expect(nonCheckWrites()).toEqual([]);
});

it.each(["parameters", "portfolio", "leave-page", "mode"].flatMap((change) => ["success", "failure"].map((outcome) => ({ change, outcome }))))(
  "ignores a late state update $outcome after changing $change", async ({ change, outcome }) => {
    const initial = run(1, { status: "pending" });
    const view = await openUnfinishedHistory(initial);
    const saved = localStorage.getItem("single-backtest-result:1");
    const late = deferredResponse();
    detailReply = () => late.promise;
    fireEvent.click(screen.getByRole("button", { name: "更新这条记录" }));
    await waitFor(() => expect(requests.filter((request) => request.url.endsWith("/backtest/runs/77"))).toHaveLength(2));
    if (change === "parameters") {
      const capital = screen.getByRole("textbox", { name: "单标的初始资金" });
      fireEvent.change(capital, { target: { value: "111111" } });
      fireEvent.change(capital, { target: { value: "100000" } });
    } else if (change === "portfolio") {
      historiesReply = async (id) => json([run(id)]);
      detailReply = async (id) => id === 78 ? json(run(2)) : late.promise;
      await switchPortfolio("本地组合甲", "本地组合乙");
      fireEvent.click(await screen.findByRole("button", { name: "单标的回测" }));
      fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
      fireEvent.click(await screen.findByText("乙的历史结果"));
      await screen.findByText("+34.6%", { selector: "p" });
    } else if (change === "leave-page") {
      view.unmount();
      detailReply = async () => json(initial);
      await openCenter();
      await screen.findByRole("button", { name: "更新这条记录" });
    } else {
      fireEvent.click(screen.getByRole("button", { name: "组合回测" }));
      fireEvent.click(screen.getByRole("button", { name: "单标的回测" }));
    }
    await act(async () => late.resolve(outcome === "success" ? json(run(1, { total_return_pct: 0.90 })) : json({ detail: "只属于旧更新的读取错误" }, 503)));
    expect(screen.queryByText("+90.0%")).not.toBeInTheDocument();
    expect(screen.queryByText(/只属于旧更新的读取错误/)).not.toBeInTheDocument();
    expect(localStorage.getItem("single-backtest-result:1")).toBe(saved);
    if (change === "portfolio") {
      expect(screen.getByText("+34.6%", { selector: "p" })).toBeVisible();
      expect(new URLSearchParams(window.location.search).get("bt_run")).toBe("78");
    } else {
      expect(screen.getByRole("status", { name: "单标的回测状态" })).toHaveTextContent("还在进行");
      expect(screen.queryByRole("region", { name: "单标的交易明细" })).not.toBeInTheDocument();
    }
    expect(nonCheckWrites()).toEqual([]);
  },
);

it.each(["success", "failure"])("protects a newer same-record read from an old state update's late %s", async (outcome) => {
  await openUnfinishedHistory(run(1, { status: "pending" }));
  const late = deferredResponse();
  let oldSignal: AbortSignal | null | undefined;
  detailReply = (_id, signal) => { oldSignal = signal; return late.promise; };
  fireEvent.click(screen.getByRole("button", { name: "更新这条记录" }));
  await waitFor(() => expect(requests.filter((request) => request.url.endsWith("/backtest/runs/77"))).toHaveLength(2));
  fireEvent.change(screen.getByRole("textbox", { name: "单标的初始资金" }), { target: { value: "111111" } });
  detailReply = async () => json(run(1, { total_return_pct: 0.25 }));
  fireEvent.click(screen.getByRole("button", { name: "查看上次结果" }));
  expect(await screen.findByText("+25.0%")).toBeVisible();
  expect(oldSignal?.aborted).toBe(true);
  await act(async () => late.resolve(outcome === "success" ? json(run(1, { total_return_pct: 0.90 })) : json({ detail: "已取消的状态更新错误" }, 503)));
  expect(screen.queryByText("+90.0%")).not.toBeInTheDocument();
  expect(screen.queryByText(/已取消的状态更新错误/)).not.toBeInTheDocument();
  expect(screen.getByText("+25.0%")).toBeVisible();
  expect(screen.getByRole("textbox", { name: "单标的初始资金" })).toHaveValue("111111");
  expect(JSON.parse(localStorage.getItem("single-backtest-result:1")!).run.total_return_pct).toBe(0.25);
  expect(nonCheckWrites()).toEqual([]);
});

it("hides unconfirmed trades and rejects their old response after the record is confirmed again", async () => {
  const firstDetail = deferredResponse(), oldTrades = deferredResponse();
  histories = [run(1, { total_return_pct: 0.25 })];
  detailReply = () => firstDetail.promise;
  let tradeReads = 0;
  tradesReply = async () => ++tradeReads === 1 ? oldTrades.promise : json({ ...page(77), items: [trade(77, { entry_date: "2026-04-01" })] });
  await openSingle();
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  fireEvent.click(await screen.findByText("甲的历史结果"));
  await waitFor(() => expect(tradeReads).toBe(1));
  await act(async () => firstDetail.resolve(json(run(1, { status: "pending", total_return_pct: 0.90 }))));
  await screen.findByRole("button", { name: "更新这条记录" });
  expect(screen.queryByRole("region", { name: "单标的交易明细" })).not.toBeInTheDocument();
  expect(screen.queryByText("+90.0%")).not.toBeInTheDocument();
  detailReply = async () => json(run(1, { total_return_pct: 0.25 }));
  fireEvent.click(screen.getByRole("button", { name: "更新这条记录" }));
  expect(await within(await screen.findByRole("region", { name: "单标的交易明细" })).findByText("2026-04-01")).toBeVisible();
  await act(async () => oldTrades.resolve(json({ ...page(77), items: [trade(77, { entry_date: "2020-01-01" })] })));
  expect(screen.queryByText("2020-01-01")).not.toBeInTheDocument();
  expect(screen.getByText("2026-04-01")).toBeVisible();
  expect(tradeReads).toBe(2);
  expect(nonCheckWrites()).toEqual([]);
});

it("updates a historical record without removing the current draft's historical-data block", async () => {
  precheck = { ...precheck, symbols: [{ id: 11, symbol: "510300" }],
    blocking_reasons: [{ code: "STATUS_DATA_UNAVAILABLE", detail_zh: "当前日期缺少历史状态资料" }] };
  await openUnfinishedHistory(run(1, { status: "pending" }));
  detailReply = async () => json(run(1, { total_return_pct: 0.25 }));
  fireEvent.click(screen.getByRole("button", { name: "更新这条记录" }));
  expect(await screen.findByText("+25.0%")).toBeVisible();
  expect(screen.getByRole("button", { name: "先补齐资料" })).toBeDisabled();
  expect(nonCheckWrites()).toEqual([]);
});

function historyRecords(count: number, portfolioId = 1) {
  return Array.from({ length: count }, (_, index) => run(portfolioId, {
    id: index === 10 ? (portfolioId === 1 ? 77 : 78) : portfolioId * 1000 + index,
    run_name: `${portfolioId === 1 ? "甲" : "乙"}的第${index + 1}条回测`,
  }));
}
function historyReads() {
  return requests.filter((request) => request.url.includes("/backtest/runs?")).map((request) => {
    const query = new URL(request.url, "http://test").searchParams;
    return { portfolioId: Number(query.get("portfolio_id")), limit: Number(query.get("limit")) };
  });
}

it("reads earlier single-symbol history with the existing list API and opens the selected record without submitting", async () => {
  const records = historyRecords(21);
  historiesReply = async (_id, limit) => json(records.slice(0, limit));
  await openSingle();
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  expect(await screen.findByText("已读取最近 10 条记录。")).toBeVisible();
  expect(screen.getByText("甲的第10条回测")).toBeVisible();
  expect(screen.queryByText("甲的第11条回测")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "读取更多" }));
  expect(await screen.findByText("已读取最近 20 条记录。")).toBeVisible();
  expect(historyReads().slice(-1)[0]).toEqual({ portfolioId: 1, limit: 20 });
  expect(screen.getAllByRole("group", { name: /单标的回测记录/ })).toHaveLength(20);
  fireEvent.click(screen.getByText("甲的第11条回测"));
  expect(await screen.findByText("+12.3%", { selector: "p" })).toBeVisible();
  expect(screen.getByRole("region", { name: "结果对应的参数" })).toHaveTextContent("回测记录 #77");
  expect(nonCheckWrites()).toEqual([]);
});

it("keeps already read history on a failed expansion and retries the same range in place", async () => {
  const records = historyRecords(25);
  historiesReply = async (_id, limit) => json(records.slice(0, limit));
  await openSingle();
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  await screen.findByText("已读取最近 10 条记录。");
  const pending = deferredResponse();
  historiesReply = () => pending.promise;
  fireEvent.click(screen.getByRole("button", { name: "读取更多" }));
  expect(screen.getByRole("button", { name: "读取更多" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "读取更多" }));
  expect(screen.getByText("甲的第10条回测")).toBeVisible();
  expect(screen.getByText("已读取最近 10 条记录。")).toBeVisible();
  await act(async () => pending.resolve(json({ detail: "更早的记录暂时读取失败" }, 503)));
  expect(await screen.findByText("更早的记录暂时读取失败")).toBeVisible();
  expect(screen.getByText("甲的第10条回测")).toBeVisible();
  expect(screen.queryByText("暂无回测历史")).not.toBeInTheDocument();
  expect(screen.queryByText("已读到现有记录。")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "读取更多" })).not.toBeInTheDocument();
  expect(historyReads().filter((request) => request.limit === 20)).toHaveLength(1);
  historiesReply = async (_id, limit) => json(records.slice(0, limit));
  const controls = within(screen.getByRole("region", { name: "回测历史读取" }));
  expect(controls.getByText("更早的记录暂时读取失败")).toBeVisible();
  expect(controls.getByText("已读取最近 10 条记录。")).toBeVisible();
  fireEvent.click(controls.getByRole("button", { name: "重试读取" }));
  expect(await screen.findByText("已读取最近 20 条记录。")).toBeVisible();
  expect(historyReads().slice(-1)[0]).toEqual({ portfolioId: 1, limit: 20 });
  expect(screen.getByText("甲的第20条回测")).toBeVisible();
  expect(screen.queryByText("更早的记录暂时读取失败")).not.toBeInTheDocument();
  expect(nonCheckWrites()).toEqual([]);
});

it("keeps the portfolio history's original range when leaving expanded single-symbol history", async () => {
  const records = historyRecords(45);
  historiesReply = async (_id, limit) => json(records.slice(0, limit));
  await openSingle();
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  await screen.findByText("已读取最近 10 条记录。");
  fireEvent.click(screen.getByRole("button", { name: "读取更多" }));
  await screen.findByText("已读取最近 20 条记录。");
  const late = deferredResponse();
  historiesReply = () => late.promise;
  fireEvent.click(screen.getByRole("button", { name: "读取更多" }));
  fireEvent.click(screen.getByRole("button", { name: "关闭" }));
  fireEvent.click(screen.getByRole("button", { name: "组合回测" }));
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  expect(await screen.findByText("甲的第10条回测")).toBeVisible();
  expect(screen.queryByText("甲的第11条回测")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "读取更多" })).not.toBeInTheDocument();
  await act(async () => late.resolve(json(records.slice(0, 40))));
  expect(screen.queryByText("甲的第11条回测")).not.toBeInTheDocument();
  expect(screen.queryByText("甲的第40条回测")).not.toBeInTheDocument();
  expect(nonCheckWrites()).toEqual([]);
});

it.each(["success", "failure"])("ignores a late expanded-history %s and starts the next portfolio at its own range", async (outcome) => {
  historiesReply = async (id, limit) => json(historyRecords(45, id).slice(0, limit));
  await openSingle();
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  await screen.findByText("已读取最近 10 条记录。");
  fireEvent.click(screen.getByRole("button", { name: "读取更多" }));
  await screen.findByText("已读取最近 20 条记录。");
  const late = deferredResponse();
  historiesReply = (id, limit) => id === 1 ? late.promise : Promise.resolve(json(historyRecords(45, 2).slice(0, limit)));
  fireEvent.click(screen.getByRole("button", { name: "读取更多" }));
  fireEvent.click(screen.getByRole("button", { name: "关闭" }));
  await switchPortfolio("本地组合甲", "本地组合乙");
  fireEvent.click(await screen.findByRole("button", { name: "单标的回测" }));
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  expect(await screen.findByText("乙的第10条回测")).toBeVisible();
  expect(screen.getByText("已读取最近 10 条记录。")).toBeVisible();
  await act(async () => late.resolve(outcome === "success" ? json(historyRecords(45).slice(0, 40)) : json({ detail: "甲的扩读失败" }, 503)));
  expect(screen.queryByText("甲的第1条回测")).not.toBeInTheDocument();
  expect(screen.queryByText("甲的第40条回测")).not.toBeInTheDocument();
  expect(screen.queryByText("甲的扩读失败")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "读取更多" }));
  expect(await screen.findByText("乙的第20条回测")).toBeVisible();
  expect(historyReads().slice(-1)[0]).toEqual({ portfolioId: 2, limit: 20 });
  expect(nonCheckWrites()).toEqual([]);
});

it("limits expanded history to the latest 100 records and explains the shared record range", async () => {
  const records = historyRecords(105);
  records[5] = run(1, { id: 9000, run_name: "组合回测也在范围内", source: "members",
    rule_config_json: JSON.stringify({ execution_mode: "event_driven" }) });
  historiesReply = async (_id, limit) => json(records.slice(0, limit));
  await openSingle();
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  await screen.findByText("已读取最近 10 条记录。");
  expect(screen.getByText("当前组合的回测记录按时间从新到旧显示，包含组合回测，最多读取最近 100 条。")).toBeVisible();
  for (const count of [20, 40, 80, 100]) {
    fireEvent.click(screen.getByRole("button", { name: "读取更多" }));
    expect(await screen.findByText(`已读取最近 ${count} 条记录。`)).toBeVisible();
  }
  expect(screen.getByText("组合回测也在范围内")).toBeVisible();
  expect(screen.getByText("甲的第100条回测")).toBeVisible();
  expect(screen.queryByText("甲的第101条回测")).not.toBeInTheDocument();
  expect(screen.getByText("仅显示最近 100 条，较早记录不在本次范围内。")).toBeVisible();
  expect(screen.queryByText("已读到现有记录。")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "读取更多" })).not.toBeInTheDocument();
  expect(historyReads().map((request) => request.limit)).toContain(100);
  expect(historyReads().every((request) => request.limit <= 100)).toBe(true);
  expect(nonCheckWrites()).toEqual([]);
});

it.each([
  { label: "missing list", body: null },
  { label: "another portfolio's records", body: [run(2)] },
])("keeps read history and allows retry when expansion returns $label", async ({ body }) => {
  const records = historyRecords(25);
  historiesReply = async (_id, limit) => json(records.slice(0, limit));
  await openSingle();
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  await screen.findByText("已读取最近 10 条记录。");
  historiesReply = async () => json(body);
  fireEvent.click(screen.getByRole("button", { name: "读取更多" }));
  expect(await screen.findByText("回测历史读取失败")).toBeVisible();
  expect(screen.getByText("甲的第10条回测")).toBeVisible();
  expect(screen.getByText("已读取最近 10 条记录。")).toBeVisible();
  expect(screen.queryByText("乙的历史结果")).not.toBeInTheDocument();
  expect(screen.queryByText("暂无回测历史")).not.toBeInTheDocument();
  historiesReply = async (_id, limit) => json(records.slice(0, limit));
  fireEvent.click(screen.getByRole("button", { name: "重试读取" }));
  expect(await screen.findByText("已读取最近 20 条记录。")).toBeVisible();
  expect(historyReads().slice(-1)[0]).toEqual({ portfolioId: 1, limit: 20 });
  expect(nonCheckWrites()).toEqual([]);
});

it("keeps the expanded range on reopening and resumes a failed range without truncating read history", async () => {
  const records = historyRecords(25);
  historiesReply = async (_id, limit) => json(records.slice(0, limit));
  await openSingle();
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  await screen.findByText("已读取最近 10 条记录。");
  fireEvent.click(screen.getByRole("button", { name: "读取更多" }));
  await screen.findByText("已读取最近 20 条记录。");
  fireEvent.click(screen.getByRole("button", { name: "关闭" }));
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  await waitFor(() => expect(historyReads().slice(-1)[0]).toEqual({ portfolioId: 1, limit: 20 }));
  await waitFor(() => expect(screen.getByRole("button", { name: "读取更多" })).toBeEnabled());
  expect(screen.getByText("甲的第20条回测")).toBeVisible();
  historiesReply = async () => json({ detail: "40条范围暂时读取失败" }, 503);
  fireEvent.click(screen.getByRole("button", { name: "读取更多" }));
  expect(await screen.findByText("40条范围暂时读取失败")).toBeVisible();
  expect(screen.getByText("已读取最近 20 条记录。")).toBeVisible();
  historiesReply = async (_id, limit) => json(records.slice(0, limit));
  fireEvent.click(screen.getByRole("button", { name: "关闭" }));
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  expect(await screen.findByText("已读取最近 25 条记录。")).toBeVisible();
  expect(historyReads().slice(-1)[0]).toEqual({ portfolioId: 1, limit: 40 });
  expect(screen.getByText("已读到现有记录。")).toBeVisible();
  expect(screen.queryByRole("button", { name: "读取更多" })).not.toBeInTheDocument();
  expect(nonCheckWrites()).toEqual([]);
});

it.each([0, 7])("ends a short initial history response of %s records without offering an unnecessary expansion", async (count) => {
  historiesReply = async (_id, limit) => json(historyRecords(count).slice(0, limit));
  await openSingle();
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  if (count === 0) {
    expect(await screen.findByText("暂无回测历史")).toBeVisible();
    expect(screen.queryByText(/已读取最近/)).not.toBeInTheDocument();
  } else {
    expect(await screen.findByText("已读取最近 7 条记录。")).toBeVisible();
    expect(screen.getByText("已读到现有记录。")).toBeVisible();
  }
  expect(screen.queryByRole("button", { name: "读取更多" })).not.toBeInTheDocument();
  expect(nonCheckWrites()).toEqual([]);
});

it("preserves an original ten-record read failure when returning to portfolio history", async () => {
  histories = [run()];
  await openSingle();
  await waitFor(() => expect(screen.getByText(/2026-10-21T12:00:00/, { selector: "span" })).toBeVisible());
  historiesReply = async () => json({ detail: "原有历史范围读取失败" }, 503);
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  await screen.findByText("回测历史读取失败");
  fireEvent.click(screen.getByRole("button", { name: "关闭" }));
  fireEvent.click(screen.getByRole("button", { name: "组合回测" }));
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  expect(screen.getByText("原有历史范围读取失败")).toBeVisible();
  expect(screen.getByText("回测历史读取失败")).toBeVisible();
  expect(screen.getByText("甲的历史结果")).toBeVisible();
  expect(nonCheckWrites()).toEqual([]);
});

it("updates single-symbol history on opening and keeps the last records when updating fails", async () => {
  histories = [run(1, { status: "running", finished_at: "2026-10-21T12:00:00" })];
  await openSingle();
  await waitFor(() => expect(requests.some((request) => request.url.includes("/backtest/runs?"))).toBe(true));
  const pending = deferredResponse();
  historiesReply = () => pending.promise;
  const before = requests.filter((request) => request.url.includes("/backtest/runs?")).length;
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  await waitFor(() => expect(requests.filter((request) => request.url.includes("/backtest/runs?"))).toHaveLength(before + 1));
  expect(screen.getByRole("group", { name: "单标的回测记录 #77" })).toHaveTextContent("进行中");
  expect(screen.getByText("正在更新回测记录，下面是上次读到的内容。")).toBeVisible();
  expect(screen.getByText(/2026-10-21T12:00:00/, { selector: "span" })).toBeVisible();
  await act(async () => pending.resolve(json({ detail: "历史记录暂时无法读取" }, 503)));
  expect(await screen.findByText("记录暂时没更新，下面是上次读到的内容。")).toBeVisible();
  expect(screen.getByRole("group", { name: "单标的回测记录 #77" })).toHaveTextContent("进行中");
  historiesReply = async () => json([run(1, { finished_at: "2026-10-23T10:00:00" })]);
  fireEvent.click(screen.getByRole("button", { name: "重试读取" }));
  await waitFor(() => expect(screen.getByRole("group", { name: "单标的回测记录 #77" })).toHaveTextContent("已完成"));
  expect(screen.getByText(/2026-10-23T10:00:00/, { selector: "span" })).toBeVisible();
  expect(screen.queryByText("记录暂时没更新，下面是上次读到的内容。")).not.toBeInTheDocument();
  expect(nonCheckWrites()).toEqual([]);
});

it("retains previous history when the automatic update after submission fails", async () => {
  histories = [run(1, { run_name: "提交前的记录", status: "running", finished_at: "2026-10-21T12:00:00" })];
  await openSingle();
  await waitFor(() => expect(screen.getByText(/2026-10-21T12:00:00/, { selector: "span" })).toBeVisible());
  historiesReply = async () => json({ detail: "提交后历史更新失败" }, 503);
  await submitSingle();
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  expect(await screen.findByText("记录暂时没更新，下面是上次读到的内容。")).toBeVisible();
  expect(screen.getByText("提交前的记录")).toBeVisible();
  expect(screen.getByText("+12.3%")).toBeVisible();
  expect(nonCheckWrites()).toHaveLength(1);
});

it("clears retained history and its last-run time after an empty successful update", async () => {
  histories = [run(1, { finished_at: "2026-10-21T12:00:00" })];
  await openSingle();
  await waitFor(() => expect(screen.getByText(/2026-10-21T12:00:00/, { selector: "span" })).toBeVisible());
  histories = [];
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  expect(await screen.findByText("暂无回测历史")).toBeVisible();
  expect(screen.queryByRole("group", { name: "单标的回测记录 #77" })).not.toBeInTheDocument();
  expect(screen.queryByText(/2026-10-21T12:00:00/, { selector: "span" })).not.toBeInTheDocument();
  expect(nonCheckWrites()).toEqual([]);
});

it("shows a first history failure without claiming that previous records are available", async () => {
  historiesReply = async () => json({ detail: "第一次读取失败" }, 503);
  await openSingle();
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  expect(await screen.findByText("回测历史读取失败")).toBeVisible();
  expect(screen.queryByText(/下面是上次读到的内容/)).not.toBeInTheDocument();
  expect(screen.queryByText("暂无回测历史")).not.toBeInTheDocument();
  expect(nonCheckWrites()).toEqual([]);
});

it.each(["success", "failure"])("ignores a late history update %s after switching portfolios", async (outcome) => {
  historiesReply = async (id) => json([run(id)]);
  await openSingle();
  const late = deferredResponse();
  historiesReply = (id) => id === 1 ? late.promise : Promise.resolve(json([run(2)]));
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  expect(await screen.findByRole("group", { name: "单标的回测记录 #77" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "关闭" }));
  await switchPortfolio("本地组合甲", "本地组合乙");
  fireEvent.click(await screen.findByRole("button", { name: "单标的回测" }));
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  expect(await screen.findByRole("group", { name: "单标的回测记录 #78" })).toBeVisible();
  await act(async () => late.resolve(outcome === "success" ? json([run()]) : json({ detail: "甲的历史读取失败" }, 503)));
  expect(screen.queryByRole("group", { name: "单标的回测记录 #77" })).not.toBeInTheDocument();
  expect(screen.queryByText("甲的历史读取失败")).not.toBeInTheDocument();
  expect(screen.getByRole("group", { name: "单标的回测记录 #78" })).toBeVisible();
  expect(nonCheckWrites()).toEqual([]);
});

it("keeps the portfolio history entry's existing read behavior", async () => {
  histories = [run(1, { source: "members", rule_config_json: JSON.stringify({ execution_mode: "event_driven" }) })];
  await openCenter();
  await waitFor(() => expect(requests.some((request) => request.url.includes("/backtest/runs?"))).toBe(true));
  const before = requests.filter((request) => request.url.includes("/backtest/runs?")).length;
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  expect(await screen.findByText("甲的历史结果")).toBeVisible();
  expect(requests.filter((request) => request.url.includes("/backtest/runs?"))).toHaveLength(before);
  expect(nonCheckWrites()).toEqual([]);
});

it("views the previous result without replacing the current draft or submitting again", async () => {
  await openSingle();
  await submitSingle();
  await screen.findByText("+12.3%");
  const writes = nonCheckWrites();
  fireEvent.change(screen.getByRole("textbox", { name: "单标的初始资金" }), { target: { value: "111111" } });
  fireEvent.change(screen.getByRole("textbox", { name: "单标的佣金费率" }), { target: { value: "0.08" } });
  expect(screen.queryByText("+12.3%")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "查看上次结果" }));
  expect(await screen.findByText("+12.3%")).toBeVisible();
  expect(screen.getByRole("region", { name: "结果对应的参数" })).toHaveTextContent("初始资金 100,000 元");
  expect(screen.getByRole("textbox", { name: "单标的初始资金" })).toHaveValue("111111");
  expect(screen.getByRole("textbox", { name: "单标的佣金费率" })).toHaveValue("0.08");
  expect(await screen.findByText("2026-01-02")).toBeVisible();
  expect(screen.getByText("正在查看已保存的回测结果，左侧填写内容单独保留。")).toBeVisible();
  expect(nonCheckWrites()).toEqual(writes);
  fireEvent.change(screen.getByRole("textbox", { name: "单标的初始资金" }), { target: { value: "222222" } });
  expect(screen.queryByText("+12.3%")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "查看上次结果" })).toBeEnabled();
});

it("rejects a commission above the API range before submission and explains the fixed costs", async () => {
  await openSingle();
  await waitFor(() => expect(screen.getByRole("button", { name: "开始回测" })).toBeEnabled());
  fireEvent.change(screen.getByRole("textbox", { name: "单标的佣金费率" }), { target: { value: "100.01" } });
  expect(screen.getByRole("textbox", { name: "单标的佣金费率" })).toHaveAttribute("aria-invalid", "true");
  expect(screen.getByText("佣金费率请填写 0 到 100 之间的数字，单位是 %。")).toBeVisible();
  expect(screen.getByRole("button", { name: "请先填写完整参数" })).toBeDisabled();
  expect(screen.getByText("每笔最低佣金 5 元，滑点固定为 0.1%。费率填 0 仍收最低佣金。")).toBeVisible();
  expect(screen.queryByText(/免佣可填/)).not.toBeInTheDocument();
  expect(nonCheckWrites()).toEqual([]);
});

it.each(["success", "failure"])("starts viewing immediately and ignores the cancelled detail's late %s", async (outcome) => {
  const old = deferredResponse(), current = deferredResponse();
  let reads = 0;
  let oldSignal: AbortSignal | null | undefined;
  detailReply = (_id, signal) => {
    reads += 1;
    if (reads === 1) { oldSignal = signal; return old.promise; }
    return current.promise;
  };
  await openSingle();
  fireEvent.click(await screen.findByRole("button", { name: "开始回测" }));
  await waitFor(() => expect(reads).toBe(1));
  fireEvent.change(screen.getByRole("textbox", { name: "单标的初始资金" }), { target: { value: "111111" } });
  fireEvent.click(screen.getByRole("button", { name: "查看上次结果" }));
  await waitFor(() => expect(reads).toBe(2));
  expect(oldSignal?.aborted).toBe(true);
  expect(screen.getByRole("button", { name: "重新读取结果" })).toBeDisabled();
  await act(async () => old.resolve(outcome === "success" ? json(submittedRun(77, { total_return_pct: 0.90 })) : json({ detail: "已取消的旧读取错误" }, 503)));
  expect(screen.getByRole("button", { name: "重新读取结果" })).toBeDisabled();
  expect(screen.queryByText("+90.0%")).not.toBeInTheDocument();
  expect(screen.queryByText("已取消的旧读取错误")).not.toBeInTheDocument();
  await act(async () => current.resolve(json(submittedRun(77, { total_return_pct: 0.25 }))));
  expect(await screen.findByText("+25.0%")).toBeVisible();
  expect(screen.getByRole("textbox", { name: "单标的初始资金" })).toHaveValue("111111");
  expect(JSON.parse(localStorage.getItem("single-backtest-result:1")!).run.total_return_pct).toBe(0.25);
  expect(new URLSearchParams(window.location.search).get("bt_run")).toBe("77");
  expect(nonCheckWrites()).toHaveLength(1);
});

it("keeps the summary when viewing the previous result fails and retries the same record", async () => {
  await openSingle();
  await submitSingle();
  detailReply = async () => json({ detail: "上次结果暂时无法读取" }, 503);
  fireEvent.change(screen.getByRole("textbox", { name: "单标的初始资金" }), { target: { value: "111111" } });
  fireEvent.click(screen.getByRole("button", { name: "查看上次结果" }));
  expect(await screen.findByRole("alert", { name: "结果读取状态" })).toHaveTextContent("上次结果暂时无法读取");
  expect(screen.getByText("+12.3%")).toBeVisible();
  expect(screen.getByRole("region", { name: "结果对应的参数" })).toHaveTextContent("回测记录 #77");
  detailReply = async () => json(submittedRun(77, { total_return_pct: 0.25 }));
  fireEvent.click(screen.getByRole("button", { name: "重新读取结果" }));
  expect(await screen.findByText("+25.0%")).toBeVisible();
  expect(requests.filter((request) => request.url.endsWith("/backtest/runs/77"))).toHaveLength(3);
  expect(screen.getByRole("textbox", { name: "单标的初始资金" })).toHaveValue("111111");
  expect(nonCheckWrites()).toHaveLength(1);
});

it.each(["parameters", "portfolio", "leave-page"])("ignores a late previous-result read after changing %s", async (change) => {
  const view = await openSingle();
  await submitSingle();
  const saved = localStorage.getItem("single-backtest-result:1");
  const late = deferredResponse();
  detailReply = () => late.promise;
  fireEvent.change(screen.getByRole("textbox", { name: "单标的初始资金" }), { target: { value: "111111" } });
  fireEvent.click(screen.getByRole("button", { name: "查看上次结果" }));
  await waitFor(() => expect(requests.filter((request) => request.url.endsWith("/backtest/runs/77"))).toHaveLength(2));
  if (change === "parameters") {
    fireEvent.change(screen.getByRole("textbox", { name: "单标的初始资金" }), { target: { value: "222222" } });
  } else if (change === "portfolio") {
    await switchPortfolio("本地组合甲", "本地组合乙");
    fireEvent.click(await screen.findByRole("button", { name: "单标的回测" }));
  } else {
    view.unmount();
  }
  await act(async () => late.resolve(json(submittedRun(77, { total_return_pct: 0.90 }))));
  expect(screen.queryByText("+90.0%")).not.toBeInTheDocument();
  expect(localStorage.getItem("single-backtest-result:1")).toBe(saved);
  if (change === "parameters") {
    expect(screen.getByRole("textbox", { name: "单标的初始资金" })).toHaveValue("222222");
    expect(screen.queryByText("+12.3%")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "查看上次结果" })).toBeEnabled();
  } else if (change === "portfolio") {
    expect(screen.queryByRole("region", { name: "结果对应的参数" })).not.toBeInTheDocument();
  }
  expect(nonCheckWrites()).toHaveLength(1);
});

it("keeps a previous result blocked while an invalid draft is retained", async () => {
  submitReply = async () => json(run(1, { status: "blocked", error_message: "历史交易资料不足" }));
  detailReply = async () => json(submittedRun(77, { status: "blocked", error_message: "历史交易资料不足" }));
  await openSingle();
  await submitSingle();
  fireEvent.change(screen.getByRole("textbox", { name: "单标的佣金费率" }), { target: { value: "100.01" } });
  fireEvent.click(screen.getByRole("button", { name: "查看上次结果" }));
  expect(await screen.findByRole("alert", { name: "单标的回测状态" })).toHaveTextContent("历史交易资料不足");
  expect(screen.queryByText("+12.3%")).not.toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "单标的佣金费率" })).toHaveValue("100.01");
  expect(screen.getByRole("button", { name: "请先填写完整参数" })).toBeDisabled();
  expect(requests.filter((request) => request.url.includes("/trades?"))).toEqual([]);
  expect(nonCheckWrites()).toHaveLength(1);
});

it.each(["0", "100"])("submits the allowed %s percent commission with the displayed fixed costs", async (value) => {
  await openSingle();
  fireEvent.change(screen.getByRole("textbox", { name: "单标的佣金费率" }), { target: { value } });
  expect(screen.getByRole("textbox", { name: "单标的佣金费率" })).toHaveAttribute("aria-invalid", "false");
  expect(screen.getByText("每笔最低佣金 5 元，滑点固定为 0.1%。费率填 0 仍收最低佣金。")).toBeVisible();
  await submitSingle();
  expect(nonCheckWrites()).toHaveLength(1);
  expect(nonCheckWrites()[0].body.cost_config).toEqual({ commission_rate: Number(value) / 100, min_commission: 5, slippage_rate: 0.001 });
});

it.each(["", "-1", "NaN", "Infinity", "1000"])("keeps invalid commission %s local and never submits it", async (value) => {
  await openSingle();
  fireEvent.change(screen.getByRole("textbox", { name: "单标的佣金费率" }), { target: { value } });
  expect(screen.getByRole("textbox", { name: "单标的佣金费率" })).toHaveAttribute("aria-invalid", "true");
  expect(screen.getByRole("button", { name: "请先填写完整参数" })).toBeDisabled();
  expect(screen.getByText("佣金费率请填写 0 到 100 之间的数字，单位是 %。")).toBeVisible();
  expect(nonCheckWrites()).toEqual([]);
});

it("keeps a submission error in place and checks history without submitting again", async () => {
  submitReply = async () => json({ detail: "服务暂时无法返回这次结果" }, 503);
  await openSingle();
  await submitSingle();
  message.destroy();
  const failure = screen.getByRole("alert", { name: "回测提交状态" });
  expect(failure).toHaveTextContent("还不能确认这次回测是否完成");
  expect(failure).toHaveTextContent("服务暂时无法返回这次结果");
  expect(failure).toHaveTextContent("510300");
  expect(failure).toHaveTextContent("100,000 元");
  expect(screen.getByRole("textbox", { name: "单标的初始资金" })).toHaveValue("100000");
  const writes = nonCheckWrites();
  histories = [run()];
  fireEvent.click(within(failure).getByRole("button", { name: "查看回测记录" }));
  expect(await screen.findByText("甲的历史结果")).toBeVisible();
  expect(nonCheckWrites()).toEqual(writes);
  expect(writes).toHaveLength(1);
});

it("explains that the code was not found and rechecks it without running a backtest", async () => {
  symbolsReply = async () => json([]);
  await openSingle();
  expect(await screen.findByText("没有找到这个代码，请核对后重试。")).toBeVisible();
  expect(screen.getByRole("button", { name: "先重新检查资料" })).toBeDisabled();
  expect(requests.filter((request) => request.url.endsWith("/backtest/precheck") && request.body.symbol_ids?.length)).toHaveLength(0);
  symbolsReply = null;
  fireEvent.click(screen.getByRole("button", { name: "重新检查资料" }));
  expect(await screen.findByText("资料检查已完成。开始回测仍需手动点击。")).toBeVisible();
  expect(screen.queryByText("没有找到这个代码，请核对后重试。")).not.toBeInTheDocument();
  expect(nonCheckWrites()).toEqual([]);
});

it("shows the check service's Chinese reason without exposing its JSON wrapper", async () => {
  checkReply = async () => json({ detail: { detail_zh: "历史资料目录暂时无法读取，请稍后重新检查。" } }, 503);
  await openSingle();
  expect(await screen.findByText("历史资料目录暂时无法读取，请稍后重新检查。")).toBeVisible();
  expect(screen.queryByText(/\{"detail_zh"/)).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "先重新检查资料" })).toBeDisabled();
  checkReply = null;
  fireEvent.click(screen.getByRole("button", { name: "重新检查资料" }));
  expect(await screen.findByText("资料检查已完成。开始回测仍需手动点击。")).toBeVisible();
  expect(nonCheckWrites()).toEqual([]);
});

it.each(["network", "timeout", "wrong-record"])("keeps an unconfirmed %s submission separate from a completed result", async (failureKind) => {
  submitReply = async () => {
    if (failureKind === "network") throw new TypeError("Failed to fetch");
    if (failureKind === "timeout") return json({ detail: "Request timed out" }, 408);
    return json(run(2));
  };
  await openSingle();
  await submitSingle();
  const failure = screen.getByRole("alert", { name: "回测提交状态" });
  expect(failure).toHaveTextContent("还不能确认这次回测是否完成");
  expect(failure).not.toHaveTextContent("这次没有开始回测");
  expect(failure).not.toHaveTextContent(/Failed to fetch|Request timed out/);
  expect(screen.queryByText(/^单标的回测失败/)).not.toBeInTheDocument();
  expect(screen.queryByRole("region", { name: "结果对应的参数" })).not.toBeInTheDocument();
  expect(localStorage.getItem("single-backtest-result:1")).toBeNull();
  expect(nonCheckWrites()).toHaveLength(1);
});

it.each([
  ["BACKTEST_SCORE_COVERAGE_INSUFFICIENT", 409, "历史评分不足"],
  ["BACKTEST_MARKET_DATA_MISSING", 422, "历史行情不足"],
] as const)("keeps %s visible with the existing data repair entry", async (code, status, reason) => {
  submitReply = async () => json({ detail: { code, message: "Historical data is missing" } }, status);
  await openSingle();
  await submitSingle();
  const failure = screen.getByRole("alert", { name: "回测提交状态" });
  expect(failure).toHaveTextContent("这次回测未完成");
  expect(failure).toHaveTextContent(reason);
  expect(failure).not.toHaveTextContent("这次没有开始回测");
  expect(screen.getByRole("button", { name: "去补资料" })).toBeEnabled();
  expect(nonCheckWrites()).toHaveLength(1);
});

it("explains a failed code lookup during submission without claiming that a backtest ran", async () => {
  await openSingle();
  await waitFor(() => expect(screen.getByRole("button", { name: "开始回测" })).toBeEnabled());
  symbolsReply = async () => json([]);
  await submitSingle();
  const failure = screen.getByRole("alert", { name: "回测提交状态" });
  expect(failure).toHaveTextContent("这次没有开始回测");
  expect(failure).toHaveTextContent("没有找到这个代码，请核对后重试");
  expect(nonCheckWrites()).toEqual([]);
});

it("does not revive an earlier submission error when parameters change back to their original value", async () => {
  submitReply = async () => json({ detail: "仅属于上次提交的错误" }, 503);
  await openSingle();
  await submitSingle();
  expect(screen.getByRole("alert", { name: "回测提交状态" })).toHaveTextContent("仅属于上次提交的错误");
  const capital = screen.getByRole("textbox", { name: "单标的初始资金" });
  fireEvent.change(capital, { target: { value: "111111" } });
  expect(screen.queryByRole("alert", { name: "回测提交状态" })).not.toBeInTheDocument();
  fireEvent.change(capital, { target: { value: "100000" } });
  expect(screen.queryByRole("alert", { name: "回测提交状态" })).not.toBeInTheDocument();
  expect(nonCheckWrites()).toHaveLength(1);
});

it.each(["parameters", "portfolio", "leave-page"])("ignores a late submission failure after changing %s", async (change) => {
  const late = deferredResponse();
  submitReply = () => late.promise;
  const view = await openSingle();
  await waitFor(() => expect(screen.getByRole("button", { name: "开始回测" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "开始回测" }));
  await waitFor(() => expect(nonCheckWrites()).toHaveLength(1));
  if (change === "parameters") {
    fireEvent.change(screen.getByRole("textbox", { name: "单标的初始资金" }), { target: { value: "111111" } });
  } else if (change === "portfolio") {
    await switchPortfolio("本地组合甲", "本地组合乙");
    fireEvent.click(await screen.findByRole("button", { name: "单标的回测" }));
    fireEvent.change(screen.getByPlaceholderText("输入代码，如 600519"), { target: { value: "510300" } });
  } else {
    view.unmount();
    await openCenter();
  }
  await act(async () => late.resolve(json({ detail: "已经过期的提交失败" }, 503)));
  expect(screen.queryByRole("alert", { name: "回测提交状态" })).not.toBeInTheDocument();
  expect(screen.queryByText(/已经过期的提交失败/)).not.toBeInTheDocument();
  if (change === "parameters") expect(screen.getByRole("textbox", { name: "单标的初始资金" })).toHaveValue("111111");
  expect(nonCheckWrites()).toHaveLength(1);
});

it("shows only usable single-symbol history returns alongside their actual states", async () => {
  const rows = [
    { id: 77, status: "completed", total_return_pct: 0.1234, label: "已完成", valid: true },
    { id: 78, status: "running", label: "进行中" },
    { id: 79, status: "pending", label: "进行中" },
    { id: 80, status: "", label: "状态待确认" },
    { id: 81, status: "unknown", label: "状态待确认" },
    { id: 82, status: "completed", errors: [{ code: "BACKTEST_SCORE_COVERAGE_INSUFFICIENT", message: "历史评分尚未补齐" }], label: "资料不足" },
    { id: 83, status: "completed", warnings: [{ code: "DATA_MISSING", message: "交易资料缺失", blocking: true }], label: "资料不足" },
    { id: 84, status: "completed", blocking_status: "DATA_INCOMPLETE_PAUSED", label: "资料不足" },
    { id: 85, status: "success", total_return_pct: 0.3456, warnings: [{ code: "NOTICE", severity: "warning", message: "仅供核对的非阻断提醒" }], label: "已完成", valid: true },
  ];
  histories = rows.map(({ label: _label, valid: _valid, ...value }) => run(1, { total_return_pct: 0.7654, ...value, run_name: `历史记录${value.id}` }));
  await openSingle();
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  for (const expected of rows) {
    const row = await screen.findByRole("group", { name: `单标的回测记录 #${expected.id}` });
    expect(row).toHaveTextContent(expected.label);
    if (expected.valid) expect(row).toHaveTextContent(expected.id === 77 ? "+12.3%" : "+34.6%");
    else expect(row).not.toHaveTextContent("+76.5%");
  }
  expect(screen.queryByText("+76.5%")).not.toBeInTheDocument();
  expect(nonCheckWrites()).toEqual([]);
});

it.each([{ engine_name: "event_driven" }, { source_type: "member" }])("preserves a one-member portfolio history's existing display for $engine_name $source_type", async (source) => {
  histories = [run(1, { ...source, total_return_pct: 0.4567 })];
  await openSingle();
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  expect(await screen.findByText("+45.7%")).toBeVisible();
  expect(screen.queryByRole("group", { name: "单标的回测记录 #77" })).not.toBeInTheDocument();
  expect(nonCheckWrites()).toEqual([]);
});

it.each(["timeout", "unreadable"])("uses plain Chinese for a %s check failure and retries without running", async (kind) => {
  checkReply = async () => json({ detail: kind === "timeout" ? "Request timed out" : { fields: ["unknown"] } }, kind === "timeout" ? 408 : 503);
  await openSingle();
  const expected = kind === "timeout" ? "等待时间过长，暂时没有收到结果。" : "这次资料检查没有完成，请稍后重新检查。";
  expect(await screen.findByText(expected)).toBeVisible();
  expect(screen.queryByText(/Request timed out|\{"fields"|\[object Object\]/)).not.toBeInTheDocument();
  checkReply = null;
  fireEvent.click(screen.getByRole("button", { name: "重新检查资料" }));
  expect(await screen.findByText("资料检查已完成。开始回测仍需手动点击。")).toBeVisible();
  expect(nonCheckWrites()).toEqual([]);
});

it("restores each portfolio's saved result and edited draft through A to B to A without another submission", async () => {
  await createBothResults();
  const writes = nonCheckWrites();
  await openCenter();
  await screen.findByText("+12.3%");
  expect(screen.getByRole("textbox", { name: "单标的初始资金" })).toHaveValue("111111");
  expect(screen.getByRole("region", { name: "结果对应的参数" })).toHaveTextContent("100,000 元");
  await switchPortfolio("本地组合甲", "本地组合乙");
  await screen.findByText("+34.6%");
  expect(screen.getByRole("textbox", { name: "单标的初始资金" })).toHaveValue("222222");
  expect(screen.getByRole("region", { name: "结果对应的参数" })).toHaveTextContent("200,000 元");
  expect(screen.getByRole("region", { name: "结果对应的参数" })).toHaveTextContent("回测记录 #78");
  expect(await within(screen.getByRole("region", { name: "单标的交易明细" })).findByText("2026-02-02")).toBeVisible();
  await switchPortfolio("本地组合乙", "本地组合甲");
  await screen.findByText("+12.3%");
  expect(screen.getByRole("textbox", { name: "单标的初始资金" })).toHaveValue("111111");
  expect(screen.getByRole("region", { name: "结果对应的参数" })).toHaveTextContent("回测记录 #77");
  expect(await within(screen.getByRole("region", { name: "单标的交易明细" })).findByText("2026-01-02")).toBeVisible();
  expect(screen.queryByText("2026-02-02")).not.toBeInTheDocument();
  expect(nonCheckWrites()).toEqual(writes);
}, 30_000);

it.each(["success", "failure"])("ignores an old portfolio's restored detail and trade %s after the other portfolio has loaded", async (outcome) => {
  await createBothResults();
  const oldDetail = deferredResponse(), oldTrades = deferredResponse();
  let details = 0, trades = 0;
  detailReply = async (id) => id === 77 ? (details++, oldDetail.promise) : json(submittedRun(78));
  tradesReply = async (id) => id === 77 ? (trades++, oldTrades.promise) : json(page(id));
  const writes = nonCheckWrites();
  await openCenter();
  await waitFor(() => { expect(details).toBe(1); expect(trades).toBe(1); });
  await switchPortfolio("本地组合甲", "本地组合乙");
  await screen.findByText("+34.6%");
  expect(await within(screen.getByRole("region", { name: "单标的交易明细" })).findByText("2026-02-02")).toBeVisible();
  await act(async () => {
    oldDetail.resolve(outcome === "success" ? json(run(1, { total_return_pct: 0.90 })) : json({ detail: "甲的旧详情读取失败" }, 503));
    oldTrades.resolve(outcome === "success" ? json({ total: 1, page: 1, page_size: 20, items: [trade(77, { entry_date: "2020-01-01" })] }) : json({ detail: "甲的旧明细读取失败" }, 503));
  });
  expect(screen.getByText("+34.6%")).toBeVisible();
  expect(screen.queryByText("+90.0%")).not.toBeInTheDocument();
  expect(screen.queryByText("2020-01-01")).not.toBeInTheDocument();
  expect(screen.queryByText(/甲的旧.*读取失败/)).not.toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "单标的初始资金" })).toHaveValue("222222");
  detailReply = async (id) => json(submittedRun(id));
  tradesReply = async (id) => json(page(id));
  await switchPortfolio("本地组合乙", "本地组合甲");
  await screen.findByText("+12.3%");
  expect(screen.getByRole("region", { name: "结果对应的参数" })).toHaveTextContent("回测记录 #77");
  expect(screen.getByRole("textbox", { name: "单标的初始资金" })).toHaveValue("111111");
  expect(await within(screen.getByRole("region", { name: "单标的交易明细" })).findByText("2026-01-02")).toBeVisible();
  expect(nonCheckWrites()).toEqual(writes);
}, 30_000);

async function createOneResult() {
  const view = await openSingle();
  await submitSingle();
  await screen.findByText("+12.3%");
  fireEvent.change(screen.getByRole("textbox", { name: "单标的初始资金" }), { target: { value: "111111" } });
  view.unmount();
  window.history.replaceState(null, "", "/?tab=portfolio&subtab=backtest");
}

it("retains the saved summary and original parameters after restore fails and retries only that record", async () => {
  await createOneResult();
  const writes = nonCheckWrites();
  detailReply = async () => json({ detail: "恢复记录时连接中断" }, 503);
  await openCenter();
  const failure = await screen.findByRole("alert", { name: "结果读取状态" });
  expect(failure).toHaveTextContent("恢复记录时连接中断");
  expect(screen.getByText("+12.3%")).toBeVisible();
  expect(screen.getByRole("region", { name: "结果对应的参数" })).toHaveTextContent("回测记录 #77");
  expect(screen.getByRole("region", { name: "结果对应的参数" })).toHaveTextContent("100,000 元");
  expect(screen.getByRole("textbox", { name: "单标的初始资金" })).toHaveValue("111111");
  detailReply = async () => json(submittedRun(77, { total_return_pct: 0.25 }));
  const retry = screen.getByRole("button", { name: "重新读取结果" });
  fireEvent.click(retry); fireEvent.click(retry);
  expect(await screen.findByText("+25.0%")).toBeVisible();
  expect(screen.queryByRole("alert", { name: "结果读取状态" })).not.toBeInTheDocument();
  expect(requests.filter((request) => request.url.endsWith("/backtest/runs/77"))).toHaveLength(3);
  expect(nonCheckWrites()).toEqual(writes);
});

it("bounds a restored detail read to 20 seconds and ignores its late response after a same-record retry", async () => {
  await createOneResult();
  const oldRead = deferredResponse();
  detailReply = () => oldRead.promise;
  const writes = nonCheckWrites();
  window.history.replaceState(null, "", "/?tab=portfolio&subtab=overview");
  openApp();
  const center = await screen.findByRole("button", { name: "回测中心" });
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  fireEvent.click(center);
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(screen.getByRole("status", { name: "结果读取状态" })).toHaveTextContent("正在读取完整结果");
  await act(async () => { await vi.advanceTimersByTimeAsync(19_999); });
  expect(screen.queryByRole("alert", { name: "结果读取状态" })).not.toBeInTheDocument();
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(screen.getByRole("alert", { name: "结果读取状态" })).toHaveTextContent("读取完整结果超过20秒");
  expect(screen.getByText("+12.3%")).toBeVisible();
  expect(screen.getByRole("region", { name: "结果对应的参数" })).toHaveTextContent("回测记录 #77");
  expect(screen.getByRole("textbox", { name: "单标的初始资金" })).toHaveValue("111111");
  detailReply = async () => json(submittedRun(77, { total_return_pct: 0.25 }));
  fireEvent.click(screen.getByRole("button", { name: "重新读取结果" }));
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(screen.getByText("+25.0%")).toBeVisible();
  await act(async () => oldRead.resolve(json(run(1, { total_return_pct: 0.90 }))));
  expect(screen.getByText("+25.0%")).toBeVisible();
  expect(screen.queryByText("+90.0%")).not.toBeInTheDocument();
  expect(screen.queryByRole("alert", { name: "结果读取状态" })).not.toBeInTheDocument();
  expect(requests.filter((request) => request.url.endsWith("/backtest/runs/77"))).toHaveLength(3);
  expect(nonCheckWrites()).toEqual(writes);
});

it("labels missing historical fee snapshots as unrecorded instead of applying compatibility defaults or the draft", async () => {
  const historical = run(1, { cost_config_json: undefined, cost_config: undefined, commission_rate: 0.0003, slippage_bps: 5 });
  histories = [historical]; detailReply = async () => json(historical);
  await openSingle();
  fireEvent.change(screen.getByRole("textbox", { name: "单标的初始资金" }), { target: { value: "222222" } });
  fireEvent.change(screen.getByRole("textbox", { name: "单标的佣金费率" }), { target: { value: "0.09" } });
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  fireEvent.click(await screen.findByText("甲的历史结果"));
  const parameters = await screen.findByRole("region", { name: "结果对应的参数" });
  expect(parameters).toHaveTextContent("佣金 未记录 · 每笔最低 未记录 · 滑点 未记录");
  expect(parameters).not.toHaveTextContent("佣金 0.03%");
  expect(parameters).not.toHaveTextContent("佣金 0.09%");
  expect(parameters).toHaveTextContent("质量评分不低于65分");
  expect(screen.getByRole("textbox", { name: "单标的初始资金" })).toHaveValue("222222");
  expect(screen.getByRole("textbox", { name: "单标的佣金费率" })).toHaveValue("0.09");
  expect(nonCheckWrites()).toEqual([]);
});

it("restores a saved result for viewing while historical gaps continue to block the current draft", async () => {
  await createOneResult();
  const writes = nonCheckWrites();
  precheck = { ...precheck, symbols: [{ id: 11, symbol: "510300" }], blocking_reasons: [{ code: "STATUS_DATA_UNAVAILABLE", detail_zh: "缺少逐日历史状态", evidence: {} }] };
  await openCenter();
  await screen.findByText("+12.3%");
  const blocked = await screen.findByRole("button", { name: "先补齐资料" });
  expect(blocked).toBeDisabled();
  fireEvent.click(blocked);
  expect(screen.getByRole("region", { name: "结果对应的参数" })).toHaveTextContent("回测记录 #77");
  expect(screen.getByRole("textbox", { name: "单标的初始资金" })).toHaveValue("111111");
  expect(nonCheckWrites()).toEqual(writes);
});

it.each([
  { source_type: "members", engine_name: undefined },
  { source_type: undefined, engine_name: "event_driven" },
])("keeps a one-member portfolio history in the portfolio result when identified by $source_type $engine_name", async (source) => {
  const historical = run(1, source);
  histories = [historical]; detailReply = async () => json(historical);
  await openSingle();
  fireEvent.change(screen.getByRole("textbox", { name: "单标的初始资金" }), { target: { value: "222222" } });
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  fireEvent.click(await screen.findByText("甲的历史结果"));
  expect(await screen.findByRole("tab", { name: /交易流水/ })).toBeVisible();
  expect(screen.queryByRole("region", { name: "结果对应的参数" })).not.toBeInTheDocument();
  expect(screen.queryByRole("region", { name: "单标的交易明细" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "单标的回测" }));
  expect(screen.getByRole("textbox", { name: "单标的初始资金" })).toHaveValue("222222");
  expect(nonCheckWrites()).toEqual([]);
});

it.each(["broken-json", "wrong-owner", "non-numeric-score"])("keeps the draft and rejects %s local result data without reading an invalid run", async (kind) => {
  await createOneResult();
  const key = "single-backtest-result:1";
  const saved = JSON.parse(localStorage.getItem(key)!);
  if (kind === "broken-json") localStorage.setItem(key, "{not-json");
  else {
    if (kind === "wrong-owner") saved.portfolioId = 2;
    else saved.parameters.rules.buy_conditions.quality_score_min = { score: 50 };
    localStorage.setItem(key, JSON.stringify(saved));
  }
  const details = requests.filter((request) => /\/backtest\/runs\/(77|78)$/.test(request.url));
  const writes = nonCheckWrites();
  await openCenter();
  expect(await screen.findByText("上次结果的本地记录无法读取，请从回测历史重新打开。")).toBeVisible();
  expect(screen.getByRole("textbox", { name: "单标的初始资金" })).toHaveValue("111111");
  expect(screen.queryByRole("region", { name: "结果对应的参数" })).not.toBeInTheDocument();
  expect(requests.filter((request) => /\/backtest\/runs\/(77|78)$/.test(request.url))).toEqual(details);
  expect(nonCheckWrites()).toEqual(writes);
});

it.each([{ id: 78 }, { portfolio_id: 2 }])("retains the saved result when restoration returns a different record $id or portfolio $portfolio_id", async (wrongIdentity) => {
  await createOneResult();
  const saved = localStorage.getItem("single-backtest-result:1");
  const writes = nonCheckWrites();
  detailReply = async () => json(submittedRun(77, { ...wrongIdentity, total_return_pct: 0.90 }));
  await openCenter();
  expect(await screen.findByRole("alert", { name: "结果读取状态" })).toHaveTextContent("读取到的记录与这次回测不一致，请重新读取。");
  expect(screen.getByText("+12.3%")).toBeVisible();
  expect(screen.queryByText("+90.0%")).not.toBeInTheDocument();
  expect(screen.getByRole("region", { name: "结果对应的参数" })).toHaveTextContent("回测记录 #77");
  expect(localStorage.getItem("single-backtest-result:1")).toBe(saved);
  detailReply = async () => json(submittedRun(77, { total_return_pct: 0.25 }));
  fireEvent.click(screen.getByRole("button", { name: "重新读取结果" }));
  expect(await screen.findByText("+25.0%")).toBeVisible();
  expect(screen.getByRole("textbox", { name: "单标的初始资金" })).toHaveValue("111111");
  expect(nonCheckWrites()).toEqual(writes);
});

it.each(["failed", "blocked"])("shows a %s single-symbol history's recorded failure instead of residual metrics or an empty ledger", async (status) => {
  const historical = run(1, { status, error_message: "当时缺少逐日停牌资料，未完成回测", total_return_pct: 0.7654 });
  histories = [historical]; detailReply = async () => json(historical);
  tradesReply = async () => json({ total: 0, page: 1, page_size: 20, items: [] });
  await openSingle();
  await waitFor(() => expect(screen.getByRole("button", { name: "开始回测" })).toBeEnabled());
  const checks = requests.filter((request) => request.url.endsWith("/backtest/precheck"));
  fireEvent.click(screen.getByRole("button", { name: "回测历史" }));
  await screen.findByText("甲的历史结果");
  expect(screen.queryByText("+76.5%")).not.toBeInTheDocument();
  const row = await screen.findByRole("group", { name: "单标的回测记录 #77" });
  expect(row).toHaveTextContent(status === "blocked" ? "资料不足" : "未完成");
  expect(row).toHaveTextContent("当时缺少逐日停牌资料，未完成回测");
  expect(within(row).queryByText("+76.5%")).not.toBeInTheDocument();
  fireEvent.click(await screen.findByText("甲的历史结果"));
  const resultFailure = await screen.findByRole("alert", { name: "单标的回测状态" });
  expect(resultFailure).toBeVisible();
  expect(resultFailure).toHaveTextContent("当时缺少逐日停牌资料，未完成回测");
  // The closed history drawer keeps its list mounted; no residual return may be visible in the result.
  for (const metric of screen.queryAllByText("+76.5%")) expect(metric).not.toBeVisible();
  expect(screen.queryByText("这次回测没有交易记录。")).not.toBeInTheDocument();
  expect(requests.filter((request) => /\/backtest\/runs\/77\/trades\?/.test(request.url))).toEqual([]);
  expect(screen.getByRole("textbox", { name: "单标的初始资金" })).toHaveValue("100000");
  expect(screen.getByRole("button", { name: "开始回测" })).toBeEnabled();
  expect(requests.filter((request) => request.url.endsWith("/backtest/precheck"))).toEqual(checks);
  expect(nonCheckWrites()).toEqual([]);
});

it.each(["success", "failure"])("ends an obsolete URL history read after switching modes and ignores its late %s", async (outcome) => {
  const oldRead = deferredResponse();
  let details = 0;
  detailReply = () => { details++; return oldRead.promise; };
  window.history.replaceState(null, "", "/?tab=portfolio&subtab=backtest&bt_run=77");
  await openCenter();
  await waitFor(() => expect(details).toBe(1));
  expect(screen.getByText("正在读取所选回测结果…")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "单标的回测" }));
  fireEvent.click(screen.getByRole("button", { name: "组合回测" }));
  await act(async () => oldRead.resolve(outcome === "success" ? json(run(1)) : json({ detail: "旧历史连接已经断开" }, 503)));
  expect(screen.getByPlaceholderText("1,000,000")).toBeVisible();
  expect(screen.queryByRole("region", { name: "结果对应的参数" })).not.toBeInTheDocument();
  expect(screen.queryByText("+12.3%")).not.toBeInTheDocument();
  expect(screen.queryByText("旧历史连接已经断开")).not.toBeInTheDocument();
  expect(screen.queryByText("正在读取所选回测结果…")).not.toBeInTheDocument();
  expect(screen.getByText("暂无回测结果，请配置参数后开始回测")).toBeVisible();
  expect(nonCheckWrites()).toEqual([]);
});
