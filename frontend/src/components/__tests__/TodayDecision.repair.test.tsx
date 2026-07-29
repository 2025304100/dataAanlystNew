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
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

const { mockContext, mockApi, mockHook } = vi.hoisted(() => ({
  mockContext: {
    workbench: null as any,
    newsSnapshot: null as any,
    portfolioId: 1,
    locale: "zh-CN" as const,
    activeSymbolId: null as number | null,
    setActiveTab: vi.fn((_tab: string) => {}),
    loadWorkbench: vi.fn(async () => {}),
    loadSymbolDetail: vi.fn(async (_id: number) => {}),
    showToast: vi.fn((_type: string, _msg: string) => {}),
  },
  mockApi: {
    getMacroOverview: vi.fn(async () => ({ snapshot: { market_score: 60 } })),
    getMarketEvents: vi.fn(async () => ({ events: [] })),
    getDataHealth: vi.fn(async () => makeDataHealth(0)),
    getFactorOverview: vi.fn(async () => null),
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
  beforeEach(() => {
    vi.clearAllMocks();
    mockContext.workbench = null;
    mockContext.newsSnapshot = null;
    mockContext.activeSymbolId = null;
    mockApi.getDataHealth.mockResolvedValue(makeDataHealth(0));
    mockApi.repairSymbolMarketData.mockResolvedValue({ success: true });
    mockApi.repairAllSymbolMarketData.mockResolvedValue({
      success: true,
      total: 0,
      ok_count: 0,
      empty_count: 0,
      failed_count: 0,
      missing_count: 0,
    });
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

  it("点击清理过期结果调用 cleanupDiscoveryResults 并刷新", async () => {
    mockApi.getDataHealth.mockResolvedValue(makeDataHealth(0, { expired_results: 5 }));
    mockApi.cleanupDiscoveryResults.mockResolvedValue({ deleted: 5 });
    render(<TodayDecision />);
    await waitFor(() => {
      expect(screen.getByText(/tdCleanupExpiredResults/)).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText(/tdCleanupExpiredResults/));
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
});
