// WP1-FIX.6：今日决策页待修复样本批量修复与弹窗交互测试
//
// 覆盖：
// - 无待修复样本时不渲染修复区域
// - ≤6 个样本完整展示且无"查看更多"
// - >6 个样本仅展示前 6 个并出现"查看更多"
// - 点击"查看更多"打开弹窗展示全部样本
// - 点击"一键修复全部"调用 repairAllSymbolMarketData 并刷新
// - 单个样本点击修复调用 repairSymbolMarketData 并刷新
// - 批量修复失败时显示 error toast
//
// 约束：
// - mock AppContext / api.client / i18n / antd message
// - 不调用真实后端 API
import { describe, it, expect, vi, beforeEach } from "vitest";
import { enterReadOnlyMode } from "../../test/acceptance";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

const { mockContext, mockApi, mockHook } = vi.hoisted(() => ({
  mockContext: {
    workbench: null as any,
    newsSnapshot: null as any,
    portfolioId: 1,
    locale: "zh-CN" as const,
    activeSymbolId: null as number | null,
    setActiveTab: vi.fn((_tab: string) => {}),
    setActiveSubTab: vi.fn((_tab: string) => {}),
    loadWorkbench: vi.fn(async () => {}),
    loadSymbolDetail: vi.fn(async (_id: number) => {}),
    showToast: vi.fn((_type: string, _msg: string) => {}),
  },
  mockApi: {
    getMacroOverview: vi.fn(async () => ({ snapshot: { market_score: 60 } })),
    getMarketEvents: vi.fn(async () => ({ events: [] })),
    getDataHealth: vi.fn(async () => makeDataHealth(0)),
    getFactorOverview: vi.fn(async () => null),
    scoringGetOverviewAsFactor: vi.fn(async () => ({ feature_enabled: true, config: {}, runtime: {}, health: {}, latest_trade_date: null, factor_coverage: [] })),
    scoringGetOverview: vi.fn(async () => ({ feature_enabled: true, config: {}, runtime: {}, health: {}, factor_coverage: [] })),
    scoringGetFactorDefinition: vi.fn(async () => ({})),
    scoringGetFactorModelListAsFactor: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 20 })),
    getEvaluationTaskHeartbeat: vi.fn(async () => ({})),
    getPortfolioStatus: vi.fn(async (_portfolioId: number) => ({
      current_state: "READY",
      last_decision_trade_date: null,
      last_reconciled_trade_date: null,
      is_auto_simulation_eligible: true,
      permissions: { allow_new_buys: true, allow_risk_exit: true, allow_auto_recovery: true },
    })),
    preflightAutoSimulation: vi.fn(async (_portfolioId: number, _payload: unknown) => ({
      proceed: true,
      gate: "PASSED",
      reason_codes: [],
      trade_date: "2026-07-24",
    })),
    startAutoSimulation: vi.fn(async (_portfolioId: number, _payload: unknown) => ({ portfolio_id: 1, trade_date: "2026-07-24", task_id: "sim-task-1", status: "queued", created: true })),
    getAsyncTask: vi.fn(async (_taskId: string) => ({ id: "sim-task-1", status: "running", percent: 20, message: "正在处理", errors: [] })),
    repairSymbolMarketData: vi.fn(async (_id: number, _payload: unknown) => ({ success: true })),
    repairAllSymbolMarketData: vi.fn(async (_payload: unknown) => ({
      success: true,
      total: 0,
      ok_count: 0,
      empty_count: 0,
      failed_count: 0,
      missing_count: 0,
    })),
    cleanupDiscoveryResults: vi.fn(async () => ({ deleted: 0 })),
    createDiscoveryTask: vi.fn(async (_payload: unknown) => ({ id: "task-1", status: "pending" })),
  },
  mockHook: {
    useSymbolRelationships: vi.fn((_id: number | null | undefined): any => ({
      data: null,
      loading: false,
      error: null,
      degraded: false,
      refresh: vi.fn(async () => {}),
    })),
  },
}));

vi.mock("../../i18n", () => ({
  t: (key: string) => key,
  template: (key: string, params: Record<string, string | number> = {}) =>
    key.replace(/\{(\w+)\}/g, (_, name) => String(params[name] ?? "")),
  DOT: " | ",
  stageLabel: (v: string | null | undefined) => v ?? "-",
  actionLabel: (v: string | null | undefined) => v ?? "-",
  enumLabel: (_prefix: string, v: string | null | undefined) => v ?? "-",
  assetTypeLabel: (v: string | null | undefined) => v ?? "unknown",
  regionShortLabel: (v: string | null | undefined) => v ?? "-",
  getLocale: () => "zh-CN",
  setLocale: () => {},
}));

vi.mock("antd", async () => {
  const actual = await vi.importActual<typeof import("antd")>("antd");
  return {
    ...actual,
    message: {
      ...actual.message,
      success: vi.fn(),
      error: vi.fn(),
      warning: vi.fn(),
      info: vi.fn(),
    },
  };
});

vi.mock("../../context/AppContext", () => ({
  useApp: () => mockContext,
}));

vi.mock("../../api/client", () => ({ api: mockApi }));

vi.mock("../../hooks/useSymbolRelationships", () => ({
  useSymbolRelationships: (id: number | null | undefined) => mockHook.useSymbolRelationships(id),
}));

import TodayDecision from "../TodayDecision";
import type { DataHealth, DataHealthBarIssue } from "../../types";

interface MakeDataHealthOptions {
  expired_results?: number;
  latest_task_status?: string | null;
}

function makeSample(i: number): DataHealthBarIssue {
  return {
    symbol_id: 1000 + i,
    symbol: `00000${i % 10}`,
    name: `测试标的${i + 1}`,
    asset_type: "stock",
    market: "sh",
    theme: "",
    reason: i % 2 === 0 ? "missing" : "stale",
    latest_trade_date: "2026-07-20",
    latest_age_days: i + 1,
  };
}

function makeDataHealth(count: number, options: MakeDataHealthOptions = {}): DataHealth {
  const { expired_results = 0, latest_task_status = null } = options;
  const samples: DataHealthBarIssue[] = Array.from({ length: count }, (_, i) => makeSample(i));
  const missing = samples.filter((_, i) => i % 2 === 0);
  const stale = samples.filter((_, i) => i % 2 === 1);
  const issues: { level: string; message: string }[] = [];
  if (count > 0) issues.push({ level: "warn", message: "存在待修复样本" });
  if (expired_results > 0) issues.push({ level: "warn", message: `${expired_results} 条机会结果已过有效期` });
  if (latest_task_status) issues.push({ level: "warn", message: `最近一次机会挖掘状态为 ${latest_task_status}` });
  return {
    status: issues.length === 0 ? "ok" : "warn",
    score: issues.length === 0 ? 95 : 70,
    updated_at: new Date().toISOString(),
    issues,
    symbols: { total: 100, by_region: {}, by_asset_type: { stock: 100 } },
    bars: {
      total: 100,
      covered_symbols: 100 - missing.length - stale.length,
      coverage_pct: 0.9,
      latest_trade_date: "2026-07-24",
      latest_age_days: 0,
      missing_symbols: missing.length,
      outdated_symbols: stale.length,
      stale_symbols: stale.length,
      stale_pct: 0.05,
      stale_cutoff: "2026-07-21",
      repair_hint: "建议修复缺失/过期行情",
      missing_samples: missing,
      stale_samples: stale,
    },
    macro: { indicators_total: 10, latest_updated_at: new Date().toISOString(), latest_age_days: 0, market_score: 60, failed_total: 0 },
    market_events: { latest_at: new Date().toISOString(), latest_age_days: 0, events_7d: 0, important_events_7d: 0 },
    discovery: {
      expired_results,
      frozen_results: 0,
      warning_results: 0,
      latest_task: latest_task_status
        ? { id: "task-latest", status: latest_task_status, stage: "scan", percent: 0, total: 0, processed: 0, updated_at: new Date().toISOString() }
        : null,
    },
    scores: { scored_symbols: 0, latest_trade_date: null, latest_age_days: null },
  };
}

describe("TodayDecision 待修复样本交互测试", () => {
  it("keeps repair actions disabled in read-only mode without disabling refresh", async () => {
    mockApi.getDataHealth.mockResolvedValue(makeDataHealth(3));
    const restore = await enterReadOnlyMode();
    const view = render(<TodayDecision />);
    try {
      const button = (await screen.findByText("tdRepairAll")).closest("button")!;
      expect(button).toBeDisabled(); fireEvent.click(button);
      for (const repair of await screen.findAllByText("tdRepair")) expect(repair.closest("button")).toBeDisabled();
      expect(mockApi.repairAllSymbolMarketData).not.toHaveBeenCalled();
      expect(mockApi.repairSymbolMarketData).not.toHaveBeenCalled();
      expect(screen.getByText("tdRefresh").closest("button")).toBeEnabled();
    } finally { view.unmount(); await restore(); }
  });
  beforeEach(() => {
    // 显式逐个 reset，避免 vi.clearAllMocks 把未在 beforeEach 中重新赋默认值的 mock（getPortfolioStatus 等）清空
    mockContext.setActiveTab.mockClear();
    mockContext.setActiveSubTab.mockClear();
    mockContext.showToast.mockClear();
    mockContext.loadWorkbench.mockClear();
    mockContext.loadSymbolDetail.mockClear();
    mockContext.workbench = null;
    mockContext.newsSnapshot = null;
    mockContext.activeSymbolId = null;

    mockApi.getMacroOverview.mockReset();
    mockApi.getMacroOverview.mockResolvedValue({ snapshot: { market_score: 60 } });
    mockApi.getMarketEvents.mockReset();
    mockApi.getMarketEvents.mockResolvedValue({ events: [] });
    mockApi.getFactorOverview.mockReset();
    mockApi.getFactorOverview.mockResolvedValue(null);
    mockApi.getPortfolioStatus.mockReset();
    mockApi.getPortfolioStatus.mockResolvedValue({
      current_state: "READY",
      last_decision_trade_date: null,
      last_reconciled_trade_date: null,
      is_auto_simulation_eligible: true,
      permissions: { allow_new_buys: true, allow_risk_exit: true, allow_auto_recovery: true },
    });
    mockApi.preflightAutoSimulation.mockReset();
    mockApi.preflightAutoSimulation.mockResolvedValue({
      proceed: true,
      gate: "PASSED",
      reason_codes: [],
      trade_date: "2026-07-24",
    });
    mockApi.startAutoSimulation.mockReset();
    mockApi.startAutoSimulation.mockResolvedValue({ portfolio_id: 1, trade_date: "2026-07-24", task_id: "sim-task-1", status: "queued", created: true });
    mockApi.getAsyncTask.mockReset();
    mockApi.getAsyncTask.mockResolvedValue({ id: "sim-task-1", status: "running", percent: 20, message: "正在处理", errors: [] });
    mockApi.getDataHealth.mockReset();
    mockApi.getDataHealth.mockResolvedValue(makeDataHealth(0));
    mockApi.repairSymbolMarketData.mockReset();
    mockApi.repairSymbolMarketData.mockResolvedValue({ success: true });
    mockApi.repairAllSymbolMarketData.mockReset();
    mockApi.repairAllSymbolMarketData.mockResolvedValue({
      success: true,
      total: 0,
      ok_count: 0,
      empty_count: 0,
      failed_count: 0,
      missing_count: 0,
    });
    mockApi.cleanupDiscoveryResults.mockReset();
    mockApi.cleanupDiscoveryResults.mockResolvedValue({ deleted: 0 });
    mockApi.createDiscoveryTask.mockReset();
    mockApi.createDiscoveryTask.mockResolvedValue({ id: "task-1", status: "pending" });
  });

  it("无待修复样本时显示空状态且一键修复禁用", async () => {
    mockApi.getDataHealth.mockResolvedValue(makeDataHealth(0));
    render(<TodayDecision />);
    await waitFor(() => {
      expect(screen.getByText("tdRepairSamples")).toBeInTheDocument();
    });
    expect(screen.getByText("tdNoRepairSamples")).toBeInTheDocument();
    expect(screen.getByText("tdRepairAll").closest("button")).toBeDisabled();
  });

  it("starts a real simulation task only after checks and shows the task rather than claiming completion", async () => {
    render(<TodayDecision />);
    const check = await screen.findByRole("button", { name: /执行预检|检查能否开始/ });
    await waitFor(() => expect(check).toBeEnabled());
    expect(mockApi.startAutoSimulation).not.toHaveBeenCalled();
    fireEvent.click(check);
    const start = await screen.findByRole("button", { name: /进入自动推演 \/ Shadow|开始模拟交易/ });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);
    await waitFor(() => expect(mockApi.startAutoSimulation).toHaveBeenCalledOnce());
    expect(mockApi.startAutoSimulation).toHaveBeenCalledWith(1, { trade_date: "2026-07-24" });
    expect(await screen.findByText("正在模拟交易")).toBeVisible();
    expect(mockApi.getAsyncTask).toHaveBeenCalledWith("sim-task-1");
    expect(mockContext.setActiveTab).not.toHaveBeenCalled();
    expect(mockContext.showToast).not.toHaveBeenCalledWith("success", expect.stringContaining("完成"));
  });

  it.each([
    ["2026-07-24T16:30:00Z", "2026-07-25"],
    ["2026-07-24T12:30:00Z", "2026-07-24"],
    ["2026-07-24T00:30:00Z", "2026-07-24"],
  ])("checks the Shanghai business date for UTC %s", async (instant, expectedDate) => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date(instant));
    try {
      const view = render(<TodayDecision />);
      const check = await screen.findByRole("button", { name: /执行预检|检查能否开始/ });
      await waitFor(() => expect(check).toBeEnabled());
      fireEvent.click(check);
      await waitFor(() => expect(mockApi.preflightAutoSimulation).toHaveBeenCalled());
      expect(mockApi.preflightAutoSimulation.mock.calls[0][1]).toMatchObject({ trade_date: expectedDate });
      view.unmount();
    } finally { vi.useRealTimers(); }
  });

  it("explains a Shanghai trade-date mismatch in everyday Chinese and keeps starting disabled", async () => {
    mockApi.preflightAutoSimulation.mockResolvedValue({ proceed: false, trade_date: "2026-07-24", skip_reason: "SCHEDULE_DATE_MISMATCH", skip_detail: "请按北京时间重新检查" } as any);
    render(<TodayDecision />);
    const check = await screen.findByRole("button", { name: /检查能否开始/ });
    await waitFor(() => expect(check).toBeEnabled());
    fireEvent.click(check);
    expect(await screen.findByText("交易日期与北京时间不一致")).toBeVisible();
    expect(screen.queryByText("SCHEDULE_DATE_MISMATCH")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /开始模拟交易/ })).toBeDisabled();
    expect(mockApi.startAutoSimulation).not.toHaveBeenCalled();
    fireEvent.mouseEnter(screen.getByRole("button", { name: /开始模拟交易/ }).parentElement!);
    const hint = await screen.findByRole("tooltip");
    expect(hint).toHaveTextContent("交易日期与北京时间不一致");
    expect(hint).not.toHaveTextContent("SCHEDULE_DATE_MISMATCH");
  });

  it("keeps a failed start on the same page and requires fresh checks before another start", async () => {
    mockApi.startAutoSimulation.mockRejectedValueOnce({ status_code: 409, detail: { title_zh: "组合状态已改变", detail_zh: "请重新检查" } });
    render(<TodayDecision />);
    const check = await screen.findByRole("button", { name: /检查能否开始/ });
    await waitFor(() => expect(check).toBeEnabled());
    fireEvent.click(check);
    const start = screen.getByRole("button", { name: /开始模拟交易/ });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);
    expect(await screen.findByText("没有启动模拟交易。组合状态已改变：请重新检查")).toBeVisible();
    expect(start).toBeDisabled();
    expect(mockContext.setActiveTab).not.toHaveBeenCalled();
    expect(mockApi.getAsyncTask).not.toHaveBeenCalled();
  });

  it.each([
    ["partial", "模拟交易只完成了一部分，请查看任务记录"],
    ["failed", "模拟交易没有完成，请查看任务记录"],
    ["cancelled", "模拟交易已停止"],
  ])("shows a %s task truthfully without reporting success or starting another task", async (status, expectedMessage) => {
    mockApi.getAsyncTask.mockResolvedValue({ id: "sim-task-1", status, percent: 100, message: "任务记录", errors: [] });
    render(<TodayDecision />);
    const check = await screen.findByRole("button", { name: /检查能否开始/ });
    await waitFor(() => expect(check).toBeEnabled());
    fireEvent.click(check);
    const start = screen.getByRole("button", { name: /开始模拟交易/ });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);
    expect(await screen.findByText(expectedMessage)).toBeVisible();
    expect(screen.queryByText("模拟交易已完成")).not.toBeInTheDocument();
    expect(start).toBeDisabled();
    expect(mockApi.startAutoSimulation).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "查看任务记录" })).toBeEnabled();
  });

  it("retries reading the original simulation task without creating another task", async () => {
    mockApi.getAsyncTask.mockRejectedValueOnce(new Error("network down"));
    render(<TodayDecision />);
    const check = await screen.findByRole("button", { name: /检查能否开始/ });
    await waitFor(() => expect(check).toBeEnabled());
    fireEvent.click(check);
    const start = screen.getByRole("button", { name: /开始模拟交易/ });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);
    const retry = await screen.findByRole("button", { name: "重新查看任务进度" });
    expect(screen.queryByText("模拟交易已完成")).not.toBeInTheDocument();
    fireEvent.click(retry);
    expect(await screen.findByText("正在模拟交易")).toBeVisible();
    expect(mockApi.getAsyncTask.mock.calls.every(([id]) => id === "sim-task-1")).toBe(true);
    expect(mockApi.getAsyncTask).toHaveBeenCalledTimes(2);
    expect(mockApi.startAutoSimulation).toHaveBeenCalledOnce();
    expect(start).toBeDisabled();
  });

  it.each(["failed", "partial", "cancelled"])("allows an explicit fresh check and start after the original task is %s", async (status) => {
    mockApi.startAutoSimulation.mockResolvedValueOnce({ portfolio_id: 1, trade_date: "2026-07-24", task_id: "sim-task-1", status: "queued", created: true })
      .mockResolvedValueOnce({ portfolio_id: 1, trade_date: "2026-07-24", task_id: "sim-task-2", status: "queued", created: true });
    mockApi.getAsyncTask.mockResolvedValueOnce({ id: "sim-task-1", status, percent: 100, message: "任务记录", errors: [] });
    render(<TodayDecision />);
    const check = await screen.findByRole("button", { name: /检查能否开始/ });
    await waitFor(() => expect(check).toBeEnabled());
    fireEvent.click(check);
    const start = screen.getByRole("button", { name: /开始模拟交易/ });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);
    await screen.findByRole("button", { name: "查看任务记录" });
    await waitFor(() => expect(mockApi.getAsyncTask).toHaveBeenCalledWith("sim-task-1"));
    expect(start).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /重新检查/ }));
    await waitFor(() => expect(start).toBeEnabled());
    expect(mockApi.startAutoSimulation).toHaveBeenCalledOnce();
    fireEvent.click(start);
    expect(await screen.findByText("正在模拟交易")).toBeVisible();
    expect(mockApi.startAutoSimulation).toHaveBeenCalledTimes(2);
    expect(mockApi.getAsyncTask).toHaveBeenLastCalledWith("sim-task-2");
  });

  it("distinguishes a finished daily process from orders still waiting to fill", async () => {
    mockApi.getAsyncTask.mockResolvedValue({ id: "sim-task-1", status: "done", percent: 100,
      message: "当天处理完成，仍有订单待成交", errors: [],
      result: { execution_result: { pending_order_count: 2, pending_order_plan_ids: ["original-a", "original-b"] } },
    } as any);
    render(<TodayDecision />);
    const check = await screen.findByRole("button", { name: /检查能否开始/ });
    await waitFor(() => expect(check).toBeEnabled());
    fireEvent.click(check);
    const start = screen.getByRole("button", { name: /开始模拟交易/ });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);
    expect(await screen.findByText("当天处理完成，仍有订单待成交")).toBeVisible();
    expect(screen.getByText("待成交订单 2 笔，已完成的成交不会重复执行。")).toBeVisible();
    expect(screen.queryByText("模拟交易已完成")).not.toBeInTheDocument();
    expect(mockApi.startAutoSimulation).toHaveBeenCalledOnce();
  });

  it("keeps a completed day protected while allowing the next trade date after fresh checks", async () => {
    mockApi.getAsyncTask.mockResolvedValue({ id: "sim-task-1", status: "done", percent: 100, message: "完成", errors: [] });
    render(<TodayDecision />);
    const check = await screen.findByRole("button", { name: /检查能否开始/ });
    await waitFor(() => expect(check).toBeEnabled());
    fireEvent.click(check);
    const start = screen.getByRole("button", { name: /开始模拟交易/ });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);
    await screen.findByText("模拟交易已完成");
    fireEvent.click(screen.getByRole("button", { name: /重新检查/ }));
    await waitFor(() => expect(mockApi.preflightAutoSimulation).toHaveBeenCalledTimes(2));
    expect(start).toBeDisabled();
    expect(mockApi.startAutoSimulation).toHaveBeenCalledOnce();
    mockApi.preflightAutoSimulation.mockResolvedValue({ proceed: true, gate: "PASSED", reason_codes: [], trade_date: "2026-07-25" });
    fireEvent.click(screen.getByRole("button", { name: /重新检查/ }));
    await waitFor(() => expect(start).toBeEnabled());
    expect(mockApi.startAutoSimulation).toHaveBeenCalledOnce();
  });

  it("非 READY 状态的治理诊断按钮同时切换组合页和治理子 Tab", async () => {
    mockApi.getPortfolioStatus.mockResolvedValue({
      current_state: "ADMIN_PAUSED",
      last_decision_trade_date: null,
      last_reconciled_trade_date: null,
      is_auto_simulation_eligible: false,
      permissions: { allow_new_buys: false, allow_risk_exit: false, allow_auto_recovery: false },
    });
    render(<TodayDecision />);
    const button = await screen.findByRole("button", { name: "前往治理诊断 →" });
    fireEvent.click(button);
    expect(mockContext.setActiveSubTab).toHaveBeenCalledWith("governance");
    expect(mockContext.setActiveTab).toHaveBeenCalledWith("portfolio");
  });

  it("≤6 个样本完整展示且无查看更多", async () => {
    mockApi.getDataHealth.mockResolvedValue(makeDataHealth(4));
    render(<TodayDecision />);
    await waitFor(() => {
      expect(screen.getByText("tdRepairSamples")).toBeInTheDocument();
    });
    expect(screen.getByText("tdRepairAll")).toBeInTheDocument();
    // 4 个样本均展示 symbol（顺序：missing[0,2] + stale[1,3] => 1,3,2,4）
    const expected = [1, 3, 2, 4];
    for (const idx of expected) {
      expect(screen.getByText(`测试标的${idx}`)).toBeInTheDocument();
    }
    expect(screen.queryByText(/tdRepairViewMore/)).not.toBeInTheDocument();
  });

  it(">6 个样本仅展示前 6 个并出现查看更多", async () => {
    mockApi.getDataHealth.mockResolvedValue(makeDataHealth(8));
    render(<TodayDecision />);
    await waitFor(() => {
      expect(screen.getByText("tdRepairSamples")).toBeInTheDocument();
    });
    // allRepairSamples = missing[1,3,5,7] + stale[2,4,6,8]，前 6 个为 1,3,5,7,2,4
    const visible = [1, 3, 5, 7, 2, 4];
    for (const idx of visible) {
      expect(screen.getByText(`测试标的${idx}`)).toBeInTheDocument();
    }
    // 第 6、8 个不直接展示
    expect(screen.queryByText("测试标的6")).not.toBeInTheDocument();
    expect(screen.queryByText("测试标的8")).not.toBeInTheDocument();
    // 查看更多信息显示剩余 2 个
    await waitFor(() => {
      expect(screen.getByText(/tdRepairViewMore/)).toBeInTheDocument();
    });
  });

  it("点击查看更多打开弹窗展示全部样本", async () => {
    mockApi.getDataHealth.mockResolvedValue(makeDataHealth(8));
    render(<TodayDecision />);
    await waitFor(() => {
      expect(screen.getByText(/tdRepairViewMore/)).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText(/tdRepairViewMore/));
    await waitFor(() => {
      expect(screen.getByText("tdRepairModalTitle")).toBeInTheDocument();
    });
    // 弹窗内展示全部 8 个（主列表与弹窗重复，使用 getAllByText 至少存在）
    for (let i = 0; i < 8; i++) {
      expect(screen.getAllByText(`测试标的${i + 1}`).length).toBeGreaterThan(0);
    }
  });

  it("点击一键修复全部调用批量修复API并刷新", async () => {
    mockApi.getDataHealth.mockResolvedValue(makeDataHealth(3));
    mockApi.repairAllSymbolMarketData.mockResolvedValue({
      success: true,
      total: 3,
      ok_count: 3,
      empty_count: 0,
      failed_count: 0,
      missing_count: 0,
    });
    render(<TodayDecision />);
    await waitFor(() => {
      expect(screen.getByText("tdRepairAll")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("tdRepairAll"));
    await waitFor(() => {
      expect(mockApi.repairAllSymbolMarketData).toHaveBeenCalledTimes(1);
    });
    const payload = mockApi.repairAllSymbolMarketData.mock.calls[0][0] as { symbol_ids: number[]; auto_score: boolean };
    expect(payload.symbol_ids).toHaveLength(3);
    expect(payload.auto_score).toBe(true);
    // 成功后刷新数据
    await waitFor(() => {
      expect(mockApi.getDataHealth).toHaveBeenCalledTimes(2);
    });
  });

  it("单个样本点击修复调用修复API并刷新", async () => {
    mockApi.getDataHealth.mockResolvedValue(makeDataHealth(2));
    render(<TodayDecision />);
    await waitFor(() => {
      expect(screen.getAllByText("tdRepair").length).toBeGreaterThan(0);
    });
    const buttons = screen.getAllByText("tdRepair");
    fireEvent.click(buttons[0]);
    await waitFor(() => {
      expect(mockApi.repairSymbolMarketData).toHaveBeenCalledTimes(1);
    });
    expect(mockApi.repairSymbolMarketData).toHaveBeenCalledWith(
      expect.any(Number),
      expect.objectContaining({ auto_score: true }),
    );
    // 成功后刷新数据
    await waitFor(() => {
      expect(mockApi.getDataHealth).toHaveBeenCalledTimes(2);
    });
  });

  it("批量修复失败时显示 error toast", async () => {
    mockApi.getDataHealth.mockResolvedValue(makeDataHealth(2));
    mockApi.repairAllSymbolMarketData.mockRejectedValue(new Error("批量修复失败"));
    render(<TodayDecision />);
    await waitFor(() => {
      expect(screen.getByText("tdRepairAll")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("tdRepairAll"));
    await waitFor(() => {
      expect(mockContext.showToast).toHaveBeenCalledWith("error", expect.stringContaining("tdRepairAllFailed"));
    });
  });

  it("存在过期发现池结果时显示清理按钮", async () => {
    mockApi.getDataHealth.mockResolvedValue(makeDataHealth(0, { expired_results: 1998 }));
    render(<TodayDecision />);
    await waitFor(() => {
      expect(screen.getByText("tdDiscoveryActions")).toBeInTheDocument();
    });
    expect(screen.getByText(/tdCleanupExpiredResults/)).toBeInTheDocument();
  });

  it("发现池任务失败时显示重新扫描按钮", async () => {
    mockApi.getDataHealth.mockResolvedValue(makeDataHealth(0, { latest_task_status: "failed" }));
    render(<TodayDecision />);
    await waitFor(() => {
      expect(screen.getByText("tdDiscoveryActions")).toBeInTheDocument();
    });
    expect(screen.getByText("tdRescanDiscovery")).toBeInTheDocument();
  });

  it("点击清理过期结果先弹二次确认，确认后才调用 cleanupDiscoveryResults 并刷新", async () => {
    mockApi.getDataHealth.mockResolvedValue(makeDataHealth(0, { expired_results: 5 }));
    mockApi.cleanupDiscoveryResults.mockResolvedValue({ deleted: 5 });
    render(<TodayDecision />);
    await waitFor(() => {
      expect(screen.getByText(/tdCleanupExpiredResults/)).toBeInTheDocument();
    });
    // VIZ-0929-11：按钮点击只打开 Popconfirm，不直接执行破坏性清理
    fireEvent.click(screen.getByText(/tdCleanupExpiredResults/));
    expect(mockApi.cleanupDiscoveryResults).not.toHaveBeenCalled();
    const okBtn = document.querySelector<HTMLElement>(".ant-popconfirm .ant-btn-primary");
    expect(okBtn).toBeTruthy();
    fireEvent.click(okBtn!);
    await waitFor(() => {
      expect(mockApi.cleanupDiscoveryResults).toHaveBeenCalledTimes(1);
    });
    await waitFor(() => {
      expect(mockApi.getDataHealth).toHaveBeenCalledTimes(2);
    });
  });

  it("点击重新扫描调用 createDiscoveryTask 并刷新", async () => {
    mockApi.getDataHealth.mockResolvedValue(makeDataHealth(0, { latest_task_status: "expired" }));
    render(<TodayDecision />);
    await waitFor(() => {
      expect(screen.getByText("tdRescanDiscovery")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("tdRescanDiscovery"));
    await waitFor(() => {
      expect(mockApi.createDiscoveryTask).toHaveBeenCalledTimes(1);
    });
    const payload = mockApi.createDiscoveryTask.mock.calls[0][0] as { scope: string; min_score: number; include_news: boolean };
    expect(payload.scope).toBe("cn-stock");
    expect(payload.include_news).toBe(true);
    await waitFor(() => {
      expect(mockApi.getDataHealth).toHaveBeenCalledTimes(2);
    });
  });

  it("首页采用真实模型因子覆盖率，不混入观察因子或旧平均值", async () => {
    mockApi.scoringGetOverview.mockResolvedValueOnce({ feature_enabled: true,
      factor_coverage: [.740159, .990791, .108342, 0, .874503].map((coverage, i) => ({
        factor_code: String(i), coverage, model_enabled: true,
      })).concat([{ factor_code: "optional", coverage: 0, model_enabled: false }]),
      model_factor_coverage_count: 5, model_factor_avg_coverage: .542759,
      active_factor_avg_coverage: .342452,
    } as any);
    render(<TodayDecision />);
    await waitFor(() => expect(screen.getByText("54.3%")).toBeVisible());
    expect(screen.queryByText("34.2%")).not.toBeInTheDocument();
    expect(screen.getByText("tdHealthFactorGuide")).toBeVisible();
  });
});
