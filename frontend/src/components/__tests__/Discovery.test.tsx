import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// 使用 vi.hoisted 提升 mock，使其可在 vi.mock factory 内引用
// globals: true 配置下 vi 作为全局变量在 hoisted 回调中可用
const { mockContext, mockApi } = vi.hoisted(() => ({
  mockContext: {
    discoveryTask: null as any,
    discoveryScopeStats: null as any,
    newsSnapshot: null as any,
    discoveryPolling: false,
    locale: "zh-CN" as const,
    portfolioId: 1,
    primaryWatchlistSymbolIds: new Set<number>(),
    runDiscoveryMining: vi.fn(async (_payload: any) => {}),
    sendDiscoveryTaskCommand: vi.fn(async (_cmd: string) => {}),
    refreshDiscoveryTasks: vi.fn(async () => {}),
    cleanupExpiredDiscoveryResults: vi.fn(async () => {}),
    addSymbolToPrimaryWatchlist: vi.fn(async (_id: number) => {}),
    loadWorkbench: vi.fn(async () => {}),
    loadSymbolDetail: vi.fn(async (_id: number) => {}),
    setActiveSymbolId: vi.fn((_id: number) => {}),
    setActiveTab: vi.fn((_tab: string) => {}),
    showToast: vi.fn((_type: string, _msg: string) => {}),
  },
  mockApi: {
    getLatestDiscoveryCandidates: vi.fn(async () => []),
    getDataHealth: vi.fn(async () => ({ bars: { coverage_pct: 95 } })),
    getCustomIndicators: vi.fn(async () => []),
    getDiscoveryPlans: vi.fn(async () => []),
    evaluateDiscoveryIndicators: vi.fn(async () => []),
    createDiscoveryPlan: vi.fn(async () => ({ id: 1 })),
    updateDiscoveryPlan: vi.fn(async () => ({ id: 1 })),
    deleteDiscoveryPlan: vi.fn(async () => ({ ok: true })),
    updateDiscoveryResult: vi.fn(async () => ({ ok: true })),
    refreshDiscoveryResult: vi.fn(async () => ({ ok: true })),
    createJournal: vi.fn(async () => ({ id: 1 })),
    getActiveScoringConfig: vi.fn(async () => null),
  },
}));

// Mock i18n: t 返回 key，template 返回拼接后的字符串
// 必须包含 getLocale / setLocale，因为 utils/format.ts 引用了 getLocale
vi.mock("../../i18n", () => ({
  t: (key: string) => key,
  template: (key: string, params: Record<string, string | number> = {}) =>
    key.replace(/\{(\w+)\}/g, (_, name) => String(params[name] ?? "")),
  DOT: " | ",
  stageLabel: (v: string | null | undefined) => v ?? "-",
  actionLabel: (v: string | null | undefined) => v ?? "-",
  assetTypeLabel: (v: string | null | undefined) => v ?? "unknown",
  regionShortLabel: (v: string | null | undefined) => v ?? "-",
  getLocale: () => "zh-CN",
  setLocale: () => {},
}));

// Mock constants/conditionFields（Discovery 引用了 OPERATOR_LABELS）
vi.mock("../../constants/conditionFields", () => ({
  OPERATOR_LABELS: {
    gt: ">",
    gte: ">=",
    lt: "<",
    lte: "<=",
    eq: "=",
    neq: "!=",
  },
}));

// Mock antd message（保留组件库，仅覆盖 message）
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

// Mock AppContext
vi.mock("../../context/AppContext", () => ({
  useApp: () => mockContext,
}));

// Mock api/client
vi.mock("../../api/client", () => ({ api: mockApi }));

import Discovery from "../Discovery";

describe("Discovery 组件渲染测试", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // 重置 mock context 到默认空状态
    mockContext.discoveryTask = null;
    mockContext.discoveryScopeStats = null;
    mockContext.newsSnapshot = null;
    mockContext.discoveryPolling = false;
    mockContext.primaryWatchlistSymbolIds = new Set<number>();
    // 重置 api mock 默认返回值
    mockApi.getLatestDiscoveryCandidates.mockImplementation(async () => []);
    mockApi.getDataHealth.mockImplementation(async () => ({ bars: { coverage_pct: 95 } }));
    mockApi.getCustomIndicators.mockImplementation(async () => []);
    mockApi.getDiscoveryPlans.mockImplementation(async () => []);
    mockApi.getActiveScoringConfig.mockImplementation(async () => null);
  });

  it("should render discovery tab container with title", () => {
    render(<Discovery />);
    expect(screen.getByText("discoveryTitle")).toBeInTheDocument();
    expect(screen.getByText("discoveryKicker")).toBeInTheDocument();
  });

  it("should render data-tab-content discovery attribute", () => {
    const { container } = render(<Discovery />);
    const tabContent = container.querySelector('[data-tab-content="discovery"]');
    expect(tabContent).not.toBeNull();
  });

  it("should render scope select with cn-stock default", () => {
    render(<Discovery />);
    // discoveryScope 同时出现在 inline-control span 和 metric-label span 中
    expect(screen.getAllByText("discoveryScope").length).toBeGreaterThan(0);
    // discoveryScopeCnStock 同时出现在 Select selection-item 和 metric-value 中
    expect(screen.getAllByText("discoveryScopeCnStock").length).toBeGreaterThan(0);
  });

  it("should render minOpportunityScore label and InputNumber", () => {
    render(<Discovery />);
    expect(screen.getByText("minOpportunityScore")).toBeInTheDocument();
  });

  it("should render cached data mode helper by default", () => {
    render(<Discovery />);
    expect(screen.getByText("discoveryModeCachedAlert")).toBeInTheDocument();
  });

  it("should render includeNewsScore checkbox", () => {
    render(<Discovery />);
    expect(screen.getByText("includeNewsScore")).toBeInTheDocument();
  });

  it("should render start/pause/resume/cancel/refresh/cleanup buttons", () => {
    render(<Discovery />);
    expect(screen.getByText("startDiscovery")).toBeInTheDocument();
    expect(screen.getByText("pauseDiscovery")).toBeInTheDocument();
    expect(screen.getByText("resumeDiscovery")).toBeInTheDocument();
    expect(screen.getByText("cancelDiscovery")).toBeInTheDocument();
    expect(screen.getByText("refreshResults")).toBeInTheDocument();
    expect(screen.getByText("cleanupExpired")).toBeInTheDocument();
  });

  it("should disable pause/resume/cancel when no active task", () => {
    render(<Discovery />);
    // 无任务时，pause/resume/cancel 应被禁用
    const pauseBtn = screen.getByText("pauseDiscovery").closest("button");
    const resumeBtn = screen.getByText("resumeDiscovery").closest("button");
    const cancelBtn = screen.getByText("cancelDiscovery").closest("button");
    expect(pauseBtn).toBeDisabled();
    expect(resumeBtn).toBeDisabled();
    expect(cancelBtn).toBeDisabled();
  });

  it("should enable start button when no active task", () => {
    render(<Discovery />);
    const startBtn = screen.getByText("startDiscovery").closest("button");
    expect(startBtn).not.toBeDisabled();
  });

  it("should render discovery steps (prepare/sync/scan/news/done)", () => {
    render(<Discovery />);
    expect(screen.getByText("stepPrepare")).toBeInTheDocument();
    expect(screen.getByText("stepSync")).toBeInTheDocument();
    expect(screen.getByText("stepScan")).toBeInTheDocument();
    expect(screen.getByText("stepNews")).toBeInTheDocument();
    expect(screen.getByText("stepDone")).toBeInTheDocument();
  });

  it("should render pool tabs with all labels", () => {
    render(<Discovery />);
    // POOL_TABS 渲染中文 label（locale=zh-CN）
    expect(screen.getByText("全部候选")).toBeInTheDocument();
    expect(screen.getByText("高股质")).toBeInTheDocument();
    expect(screen.getByText("高时点")).toBeInTheDocument();
    expect(screen.getByText("可执行")).toBeInTheDocument();
    expect(screen.getByText("过热风险")).toBeInTheDocument();
    expect(screen.getByText("低可信度")).toBeInTheDocument();
  });

  it("should render discovery results header", () => {
    render(<Discovery />);
    expect(screen.getByText("discoveryResults")).toBeInTheDocument();
    expect(screen.getByText("discoveryResultKicker")).toBeInTheDocument();
  });

  it("should render advanced settings collapse with label", () => {
    render(<Discovery />);
    expect(screen.getByText("discoveryAdvancedSettings")).toBeInTheDocument();
  });

  it("should call runDiscoveryMining when start button clicked", async () => {
    render(<Discovery />);
    const startBtn = screen.getByText("startDiscovery").closest("button");
    fireEvent.click(startBtn!);
    await waitFor(() => {
      expect(mockContext.runDiscoveryMining).toHaveBeenCalled();
    });
  });

  it("should show retry button when task is failed and can_retry", async () => {
    mockContext.discoveryTask = {
      id: 1,
      status: "failed",
      stage: "scan",
      percent: 50,
      message: "error",
      total: 10,
      processed: 5,
      ok_count: 3,
      failed_count: 2,
      empty_count: 0,
      scored_count: 3,
      current_symbol: null,
      scan_run_id: null,
      executable_count: 0,
      cleanup_count: 0,
      errors: [],
      can_resume: false,
      can_retry: true,
      created_at: "2026-07-04T09:00:00Z",
      updated_at: "2026-07-04T10:00:00Z",
    };
    render(<Discovery />);
    await waitFor(() => {
      expect(screen.getByText("retryDiscovery")).toBeInTheDocument();
    });
    // start 应被禁用（任务虽失败但属于 isTaskActive 之外的状态，canStart 仍为 true，
    // 但 retry 按钮应出现）
    expect(mockContext.discoveryTask.status).toBe("failed");
  });

  it("should render scope metrics with discoveryScope / universeTotal / cachedPool", () => {
    render(<Discovery />);
    expect(screen.getAllByText("discoveryScope").length).toBeGreaterThan(0);
    expect(screen.getByText("discoveryUniverseTotal")).toBeInTheDocument();
    expect(screen.getByText("discoveryCachedPool")).toBeInTheDocument();
  });

  it("should render noDiscoveryResults empty state when candidates empty", () => {
    render(<Discovery />);
    expect(screen.getByText("noDiscoveryResults")).toBeInTheDocument();
  });

  it("should render saved plans / plan name / logic controls", () => {
    render(<Discovery />);
    expect(screen.getByText("dpSavedPlans")).toBeInTheDocument();
    expect(screen.getByText("dpPlanName")).toBeInTheDocument();
    expect(screen.getByText("dpLogic")).toBeInTheDocument();
    expect(screen.getByText("dpSavePlan")).toBeInTheDocument();
    expect(screen.getByText("dpAddRule")).toBeInTheDocument();
  });
});

// ── P1-2 交互测试 ──
// 构造候选数据（默认匹配 actionable pool：priority_score>=60 + action=open）
const todayIso = new Date().toISOString();
function makeCandidate(over: Partial<any> = {}): any {
  return {
    symbol_id: 1,
    symbol: "600000",
    name: "浦发银行",
    region: "cn",
    asset_type: "stock",
    market: "A",
    quality_score: 50,
    timing_score: 50,
    priority_score: 70,
    stage: "start",
    action: "open",
    recommended_position_pct: 5,
    scan_result_id: 100,
    id: 100,
    is_frozen: false,
    created_at: todayIso,
    ...over,
  };
}

describe("Discovery 交互测试", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockContext.discoveryTask = null;
    mockContext.discoveryScopeStats = null;
    mockContext.newsSnapshot = null;
    mockContext.discoveryPolling = false;
    mockContext.primaryWatchlistSymbolIds = new Set<number>();
    mockApi.getLatestDiscoveryCandidates.mockImplementation(async () => []);
    mockApi.getDataHealth.mockImplementation(async () => ({ bars: { coverage_pct: 95 } }));
    mockApi.getCustomIndicators.mockImplementation(async () => []);
    mockApi.getDiscoveryPlans.mockImplementation(async () => []);
    mockApi.getActiveScoringConfig.mockImplementation(async () => null);
  });

  it("should call runDiscoveryMining with payload when start button clicked", async () => {
    const user = userEvent.setup();
    render(<Discovery />);
    await waitFor(() => {
      expect(screen.getByText("startDiscovery")).toBeInTheDocument();
    });
    const startBtn = screen.getByText("startDiscovery").closest("button");
    expect(startBtn).not.toBeDisabled();
    await user.click(startBtn!);
    await waitFor(() => {
      expect(mockContext.runDiscoveryMining).toHaveBeenCalledWith(
        expect.objectContaining({
          scope: "cn-stock",
          minScore: 55,
          dataMode: "cached",
          batchSize: 20,
          delaySeconds: 0.25,
          warningDays: 3,
          validDays: 5,
          includeNews: true,
        }),
      );
    });
  });

  it("should call sendDiscoveryTaskCommand('cancel') when cancel button clicked", async () => {
    const user = userEvent.setup();
    // updated_at 设为当前时间，避免触发 staleWarning Alert 导致出现两个 cancelDiscovery 按钮
    mockContext.discoveryTask = {
      id: 1, status: "running", stage: "scan", percent: 50, message: "scanning",
      total: 10, processed: 5, ok_count: 3, failed_count: 2, empty_count: 0, scored_count: 3,
      current_symbol: null, scan_run_id: null, executable_count: 0, cleanup_count: 0,
      errors: [], can_resume: false, can_retry: false,
      created_at: new Date(Date.now() - 60000).toISOString(),
      updated_at: new Date().toISOString(),
    };
    render(<Discovery />);
    await waitFor(() => {
      expect(screen.getByText("cancelDiscovery")).toBeInTheDocument();
    });
    const cancelBtn = screen.getByText("cancelDiscovery").closest("button");
    expect(cancelBtn).not.toBeDisabled();
    await user.click(cancelBtn!);
    await waitFor(() => {
      expect(mockContext.sendDiscoveryTaskCommand).toHaveBeenCalledWith("cancel");
    });
  });

  it("should call sendDiscoveryTaskCommand('retry') when retry button clicked", async () => {
    const user = userEvent.setup();
    mockContext.discoveryTask = {
      id: 1, status: "failed", stage: "scan", percent: 50, message: "error",
      total: 10, processed: 5, ok_count: 3, failed_count: 2, empty_count: 0, scored_count: 3,
      current_symbol: null, scan_run_id: null, executable_count: 0, cleanup_count: 0,
      errors: [], can_resume: false, can_retry: true,
      created_at: "2026-07-04T09:00:00Z", updated_at: "2026-07-04T10:00:00Z",
    };
    render(<Discovery />);
    await waitFor(() => {
      expect(screen.getByText("retryDiscovery")).toBeInTheDocument();
    });
    const retryBtn = screen.getByText("retryDiscovery").closest("button");
    await user.click(retryBtn!);
    await waitFor(() => {
      expect(mockContext.sendDiscoveryTaskCommand).toHaveBeenCalledWith("retry");
    });
  });

  it("should call addSymbolToPrimaryWatchlist when add-to-watchlist button clicked", async () => {
    const user = userEvent.setup();
    mockApi.getLatestDiscoveryCandidates.mockImplementation(async () => [
      makeCandidate({ symbol_id: 101, symbol: "600001", name: "测试股票A" }),
    ]);
    render(<Discovery />);
    await waitFor(() => {
      expect(screen.getByText("600001")).toBeInTheDocument();
    });
    const addBtn = screen.getByText("discoveryAddWatchlist").closest("button");
    expect(addBtn).not.toBeDisabled();
    await user.click(addBtn!);
    await waitFor(() => {
      expect(mockContext.addSymbolToPrimaryWatchlist).toHaveBeenCalledWith(101);
      expect(mockContext.showToast).toHaveBeenCalledWith("success", "discoveryAddedWatchlist");
    });
  });

  it("should filter candidates when pool tab switched", async () => {
    const user = userEvent.setup();
    mockApi.getLatestDiscoveryCandidates.mockImplementation(async () => [
      makeCandidate({ symbol_id: 101, symbol: "600001", name: "高股质A", quality_score: 80 }),
      makeCandidate({ symbol_id: 102, symbol: "600002", name: "普通B", quality_score: 40 }),
    ]);
    render(<Discovery />);
    await waitFor(() => {
      expect(screen.getByText("600001")).toBeInTheDocument();
      expect(screen.getByText("600002")).toBeInTheDocument();
    });
    // 切到 "高股质" tab —— quality_score >= 70 的才显示
    const highQualityTab = screen.getByText("高股质");
    await user.click(highQualityTab);
    await waitFor(() => {
      expect(screen.getByText("600001")).toBeInTheDocument();
      expect(screen.queryByText("600002")).not.toBeInTheDocument();
    });
  });

  it("should not mark fresh legacy candidates low solely because credibility is missing", async () => {
    mockApi.getLatestDiscoveryCandidates.mockImplementation(async () => [
      makeCandidate({ symbol_id: 103, symbol: "561980", created_at: todayIso, data_credibility: null }),
    ]);
    render(<Discovery />);
    await waitFor(() => {
      expect(screen.getByText("561980")).toBeInTheDocument();
    });
    expect(screen.queryByText(/^lowCredibility/)).not.toBeInTheDocument();
  });

  it("should show the actual credibility percentage when below threshold", async () => {
    mockApi.getLatestDiscoveryCandidates.mockImplementation(async () => [
      makeCandidate({ symbol_id: 104, symbol: "561981", data_credibility: 0.4 }),
    ]);
    render(<Discovery />);
    await waitFor(() => {
      expect(screen.getByText("lowCredibility 40%")).toBeInTheDocument();
    });
  });

  it("should call loadSymbolDetail when candidate row clicked", async () => {
    mockApi.getLatestDiscoveryCandidates.mockImplementation(async () => [
      makeCandidate({ symbol_id: 101, symbol: "600001", name: "测试股票A" }),
    ]);
    const { container } = render(<Discovery />);
    await waitFor(() => {
      expect(screen.getByText("600001")).toBeInTheDocument();
    });
    // 点击表格数据行（跳过表头）—— fireEvent.click 更可靠地触发 onRow.onClick
    const rows = container.querySelectorAll("tr.ant-table-row");
    expect(rows.length).toBeGreaterThan(0);
    fireEvent.click(rows[0]);
    await waitFor(() => {
      expect(mockContext.loadSymbolDetail).toHaveBeenCalledWith(101, { focus: true });
    });
  });

  it("should expose an explicit view-detail action", async () => {
    const user = userEvent.setup();
    mockApi.getLatestDiscoveryCandidates.mockImplementation(async () => [
      makeCandidate({ symbol_id: 105, symbol: "588290", name: "科创芯片ETF" }),
    ]);
    render(<Discovery />);
    await waitFor(() => {
      expect(screen.getByText("588290")).toBeInTheDocument();
    });
    const detailButton = screen.getByText("viewDetail").closest("button");
    expect(detailButton).not.toBeNull();
    await user.click(detailButton!);
    await waitFor(() => {
      expect(mockContext.loadSymbolDetail).toHaveBeenCalledWith(105, { focus: true });
    });
  });
});
