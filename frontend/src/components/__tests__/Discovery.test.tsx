import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor, fireEvent } from "@testing-library/react";
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
    // Discovery 组件渲染时会调用 scope 切换 / 统计加载 / 任务拉取
    setDiscoveryScope: vi.fn(async (_scope: string) => {}),
    loadDiscoveryScopeStats: vi.fn(async () => {}),
    fetchDiscoveryTasks: vi.fn(async () => null),
    // 能力门禁字段：CapabilityGateButton 通过 useApp 读取这些方法
    capabilities: null as any,
    capabilitiesLoading: false,
    getCapability: vi.fn((_: string) => undefined),
    isCapabilityBlocked: vi.fn((_: string) => false),
    loadCapabilities: vi.fn(async () => {}),
  },
  mockApi: {
    getLatestDiscoveryCandidates: vi.fn(async () => [] as any[]),
    getDiscoveryScopeStats: vi.fn(async (scope: string) => ({ scope, total_symbols: 0, cached_symbols: 0, active_symbols: 0 })),
    getDataHealth: vi.fn(async () => ({ bars: { coverage_pct: 95 } })),
    getCustomIndicators: vi.fn(async () => [] as any[]),
    getDiscoveryPlans: vi.fn(async () => [] as any[]),
    evaluateDiscoveryIndicators: vi.fn(async () => [] as any[]),
    createDiscoveryPlan: vi.fn(async () => ({ id: 1 })),
    updateDiscoveryPlan: vi.fn(async () => ({ id: 1 })),
    deleteDiscoveryPlan: vi.fn(async () => ({ ok: true })),
    updateDiscoveryResult: vi.fn(async () => ({ ok: true })),
    refreshDiscoveryResult: vi.fn(async () => ({ ok: true })),
    createJournal: vi.fn(async () => ({ id: 1 })),
    getActiveScoringConfig: vi.fn(async () => null),
    getSnapshotStatus: vi.fn(async (scope: string) => ({ scope, has_ready_snapshot: true, ready_snapshot_generated_at: "2026-10-19", has_building_snapshot: false })),
    startDataPrep: vi.fn(async () => ({ id: "prepared-scan-1", status: "queued" })),
    getAsyncTask: vi.fn(async () => ({ id: "prepared-scan-1", status: "done", percent: 100, result: { fast_scan: { status: "ok" } } })),
  },
}));

// Mock i18n: t 返回 key，template 返回拼接后的字符串
// 必须包含 getLocale / setLocale，因为 utils/format.ts 引用了 getLocale
vi.mock("../../i18n", () => ({
  t: (key: string) => key === "yes" ? "是" : key === "no" ? "否" : key,
  template: (key: string, params: Record<string, string | number> = {}) => key === "discoverySimpleSummary"
    ? `${key} ${params.total} ${params.cached} ${params.count}`
    : key === "discoveryFilteredEmpty" ? `已有 ${params.total} 条候选，当前 ${params.count} 项筛选条件没有匹配结果。`
    : key === "matchedIndicators" ? `符合 ${params.matched}/${params.total} 项条件`
    : key.replace(/\{(\w+)\}/g, (_, name) => String(params[name] ?? "")),
  DOT: " | ",
  stageLabel: (v: string | null | undefined) => v ?? "-",
  actionLabel: (v: string | null | undefined) => v ?? "-",
  enumLabel: (_prefix: string, v: string | null | undefined) => v ?? "-",
  assetTypeLabel: (v: string | null | undefined) => v ?? "unknown",
  regionShortLabel: (v: string | null | undefined) => v ?? "-",
  regionLongLabel: (v: string | null | undefined) => v === "cn" ? "中国大陆" : v === "us" ? "美国" : v ?? "-",
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

// Mock ExplainButton：避免渲染依赖 useAIAssistant 的真实组件
vi.mock("../ai/ExplainButton", () => ({
  __esModule: true,
  default: () => <div data-testid="explain-button-mock" />,
}));

import Discovery from "../Discovery";
import OpportunityCenter from "../OpportunityCenter";

describe("Discovery 组件渲染测试", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // 清空 localStorage：防止 DRAFT_STORAGE_KEY / ADVANCED_CONFIG_KEY 在测试间泄漏
    // （DRAFT 含 pool_tab，若上一用例切到 highQuality，后续用例候选会被过滤掉）
    localStorage.clear();
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
    mockApi.getDiscoveryScopeStats.mockImplementation(async (scope: string) => ({ scope, total_symbols: 0, cached_symbols: 0, active_symbols: 0 }));
  });

  it("切A股ETF时当帧清掉默认A股股票统计，并读取所选范围的3项统计", async () => {
    mockContext.discoveryScopeStats = { scope: "cn-stock", total_symbols: 5554, cached_symbols: 5542, active_symbols: 5542 };
    let finishEtf!: (value: any) => void;
    const pendingEtf = new Promise<any>(resolve => { finishEtf = resolve; });
    mockApi.getDiscoveryScopeStats.mockImplementation(scope => scope === "cn-etf" ? pendingEtf : Promise.resolve(mockContext.discoveryScopeStats));
    render(<Discovery />);
    const total = screen.getByText("discoveryUniverseTotal").parentElement!;
    const cached = screen.getByText("discoveryCachedPool").parentElement!;
    await waitFor(() => expect(total).toHaveTextContent("5554"));
    const scopeControl = screen.getAllByText("discoveryScope").find(item => item.closest("label"))!.closest("label")!;
    fireEvent.mouseDown(scopeControl.querySelector(".ant-select-selector")!);
    fireEvent.click(await screen.findByText("discoveryScopeCnEtf"));

    expect(total).not.toHaveTextContent("5554");
    expect(cached).not.toHaveTextContent("5542");
    expect(total.querySelector(".metric-value")).toHaveTextContent("-");
    await act(async () => { finishEtf({ scope: "cn-etf", total_symbols: 3, cached_symbols: 3, active_symbols: 3 }); });
    await waitFor(() => expect(total.querySelector(".metric-value")).toHaveTextContent("3"));
    expect(cached.querySelector(".metric-value")).toHaveTextContent("3");
    expect(mockApi.getDiscoveryScopeStats).toHaveBeenCalledWith("cn-etf");
  });

  it.each(["成功", "失败"])("股票范围迟到%s不能覆盖已显示的ETF统计", async outcome => {
    mockContext.discoveryScopeStats = { scope: "cn-stock", total_symbols: 5554, cached_symbols: 5542, active_symbols: 5542 };
    let finishStock!: (value: any) => void;
    let failStock!: (error: Error) => void;
    const pendingStock = new Promise<any>((resolve, reject) => { finishStock = resolve; failStock = reject; });
    mockApi.getDiscoveryScopeStats.mockImplementation(scope => scope === "cn-stock" ? pendingStock
      : Promise.resolve({ scope, total_symbols: 3, cached_symbols: 3, active_symbols: 3 }));
    render(<Discovery />);
    const total = screen.getByText("discoveryUniverseTotal").parentElement!;
    const scopeControl = screen.getAllByText("discoveryScope").find(item => item.closest("label"))!.closest("label")!;
    fireEvent.mouseDown(scopeControl.querySelector(".ant-select-selector")!);
    fireEvent.click(await screen.findByText("discoveryScopeCnEtf"));
    await waitFor(() => expect(total.querySelector(".metric-value")).toHaveTextContent("3"));

    await act(async () => {
      if (outcome === "成功") finishStock(mockContext.discoveryScopeStats);
      else failStock(new Error("Old stock stats read failed"));
    });

    expect(total.querySelector(".metric-value")).toHaveTextContent("3");
    expect(total).not.toHaveTextContent("5554");
    expect(screen.getByText("discoveryCachedPool").parentElement!.querySelector(".metric-value")).toHaveTextContent("3");
  });

  it.each(["范围不匹配", "读取失败"])("ETF统计%s时显示未知，不能兜底默认股票数字或零", async outcome => {
    localStorage.setItem("discovery_advanced_config_v1", JSON.stringify({ scope: "cn-etf" }));
    mockContext.discoveryScopeStats = { scope: "cn-stock", total_symbols: 5554, cached_symbols: 5542, active_symbols: 5542 };
    if (outcome === "范围不匹配") mockApi.getDiscoveryScopeStats.mockResolvedValue(mockContext.discoveryScopeStats);
    else mockApi.getDiscoveryScopeStats.mockRejectedValue(new Error("ETF scope stats unavailable"));
    render(<Discovery />);
    await waitFor(() => expect(mockApi.getDiscoveryScopeStats).toHaveBeenCalledWith("cn-etf"));
    await act(async () => {});

    expect(screen.getByText("discoveryUniverseTotal").parentElement!.querySelector(".metric-value")).toHaveTextContent("-");
    expect(screen.getByText("discoveryCachedPool").parentElement!.querySelector(".metric-value")).toHaveTextContent("-");
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

  it("should render sync data mode helper by default", () => {
    render(<Discovery />);
    // 默认 dataMode="sync"，Alert description 渲染 discoveryModeSyncAlert
    expect(screen.getByText("discoveryModeSyncAlert")).toBeInTheDocument();
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
    // POOL_TABS 通过 t("discoveryPool" + pascalCaseKey(key)) 渲染 label
    // mock i18n 的 t(key) 返回 key，因此断言 i18n key
    expect(screen.getByText("discoveryPoolAll")).toBeInTheDocument();
    expect(screen.getByText("discoveryPoolHighQuality")).toBeInTheDocument();
    expect(screen.getByText("discoveryPoolHighTiming")).toBeInTheDocument();
    expect(screen.getByText("discoveryPoolActionable")).toBeInTheDocument();
    expect(screen.getByText("discoveryPoolOverheatRisk")).toBeInTheDocument();
    expect(screen.getByText("discoveryPoolLowCredibility")).toBeInTheDocument();
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

  it("should render noDiscoveryResults empty state when candidates empty", async () => {
    render(<Discovery />);
    expect(await screen.findByText("noDiscoveryResults")).toBeInTheDocument();
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
    // 清空 localStorage：防止 DRAFT_STORAGE_KEY / ADVANCED_CONFIG_KEY 在测试间泄漏
    // （DRAFT 含 pool_tab，若上一用例切到 highQuality，后续用例候选会被过滤掉）
    localStorage.clear();
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
    mockApi.getDiscoveryScopeStats.mockImplementation(async (scope: string) => ({ scope, total_symbols: 0, cached_symbols: 0, active_symbols: 0 }));
  });

  it("简洁入口始终显示扫描操作和进度，仅采集参数默认收起", async () => {
    mockContext.discoveryTask = {
      id: 41, scope: "cn-stock", status: "failed", stage: "scan", percent: 100,
      processed: 12, total: 12, message: "Earlier task failed", can_retry: true,
    };
    const onScopeChange = vi.fn();
    render(<Discovery simpleMode onScopeChange={onScopeChange} />);
    await screen.findByText("noDiscoveryResults");

    expect(screen.getByRole("button", { name: "startDiscovery" })).toBeVisible();
    expect(screen.getByText("Earlier task failed")).toBeVisible();
    expect(screen.queryByText("discoveryMoreOptions")).not.toBeInTheDocument();
    expect(screen.getByText("discoveryAdvancedSettings")).toBeVisible();
    const scopeControl = screen.getAllByText("discoveryScope").find(item => item.closest("label"))!.closest("label")!;
    expect(scopeControl).toBeVisible();
    fireEvent.mouseDown(scopeControl.querySelector(".ant-select-selector")!);
    fireEvent.click(await screen.findByText("discoveryScopeCnEtf"));
    await waitFor(() => expect(onScopeChange).toHaveBeenLastCalledWith("cn-etf"));
  });

  it("条件筛掉全部候选时可暂时查看全部并恢复筛选，原条件和草稿不变", async () => {
    localStorage.setItem("discovery_indicator_draft_v1", JSON.stringify({
      name: "我的筛选", logic: "AND", pool_tab: "all",
      filters: [{ id: "one", indicator_key: "condition", operator: "eq", boolean_value: true, number_value: 0 }],
    }));
    mockApi.getLatestDiscoveryCandidates.mockResolvedValue([
      makeCandidate({ symbol_id: 1, symbol: "600001", scan_result_id: 100 }),
      makeCandidate({ symbol_id: 2, symbol: "600002", scan_result_id: 101 }),
    ]);
    mockApi.getCustomIndicators.mockResolvedValue([{ id: 1, key: "condition", name: "已有条件", value_type: "boolean", enabled: true }]);
    mockApi.evaluateDiscoveryIndicators.mockResolvedValue([
      { scan_result_id: 100, symbol_id: 1, values: { condition: false }, errors: {} },
      { scan_result_id: 101, symbol_id: 2, values: { condition: false }, errors: {} },
    ]);
    render(<Discovery simpleMode />);
    await waitFor(() => expect(mockApi.evaluateDiscoveryIndicators).toHaveBeenCalled());
    expect(await screen.findByText("已有 2 条候选，当前 1 项筛选条件没有匹配结果。")).toBeVisible();
    expect(screen.queryByText("noDiscoveryResults")).not.toBeInTheDocument();
    const savedDraft = localStorage.getItem("discovery_indicator_draft_v1");

    await userEvent.click(screen.getByRole("button", { name: "discoveryViewAll" }));
    expect(await screen.findByText("600001")).toBeVisible();
    expect(screen.getByText("600002")).toBeVisible();
    expect(localStorage.getItem("discovery_indicator_draft_v1")).toBe(savedDraft);

    await userEvent.click(screen.getByRole("button", { name: "discoveryRestoreFilters" }));
    expect(await screen.findByText("已有 2 条候选，当前 1 项筛选条件没有匹配结果。")).toBeVisible();
    expect(screen.queryByText("600001")).not.toBeInTheDocument();
    expect(localStorage.getItem("discovery_indicator_draft_v1")).toBe(savedDraft);
  });

  it("简洁入口把所选范围的统计合成一行，筛选设置收起后仍说明已有条件", async () => {
    mockApi.getDiscoveryScopeStats.mockResolvedValue({ scope: "cn-stock", total_symbols: 3, cached_symbols: 2, active_symbols: 2 });
    mockApi.getLatestDiscoveryCandidates.mockResolvedValue([makeCandidate()]);
    mockApi.getCustomIndicators.mockResolvedValue([{ id: 1, key: "condition", name: "已有条件", value_type: "boolean", enabled: true }]);
    mockApi.evaluateDiscoveryIndicators.mockResolvedValue([
      { scan_result_id: 100, symbol_id: 1, values: { condition: true }, errors: {} },
    ]);
    render(<Discovery simpleMode />);

    expect(await screen.findByText("discoverySimpleSummary 3 2 1")).toBeVisible();
    expect(screen.queryByText("discoveryCurrentRun")).not.toBeInTheDocument();
    expect(await screen.findByText("dpEnabledIndicators")).toBeVisible();
    expect(screen.getByPlaceholderText("dpNamePlaceholder")).not.toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "discoveryViewAll" }));
    expect(await screen.findByRole("button", { name: "discoveryRestoreFilters" })).toBeVisible();
    await userEvent.click(screen.getByText("discoveryFilterOptions"));
    expect(screen.getByPlaceholderText("dpNamePlaceholder")).toBeVisible();
  });

  it("已选分类没有匹配时说明原因，临时查看全部同时跳过分类和指标且能恢复", async () => {
    localStorage.setItem("discovery_indicator_draft_v1", JSON.stringify({
      name: "保留分类和条件", logic: "AND", pool_tab: "overheatRisk",
      filters: [{ id: "one", indicator_key: "condition", operator: "eq", boolean_value: true, number_value: 0 }],
    }));
    mockApi.getLatestDiscoveryCandidates.mockResolvedValue([
      makeCandidate({ symbol_id: 1, symbol: "600001", scan_result_id: 100 }),
      makeCandidate({ symbol_id: 2, symbol: "600002", scan_result_id: 101 }),
    ]);
    mockApi.getCustomIndicators.mockResolvedValue([{ id: 1, key: "condition", name: "已有条件", value_type: "boolean", enabled: true }]);
    mockApi.evaluateDiscoveryIndicators.mockResolvedValue([
      { scan_result_id: 100, symbol_id: 1, values: { condition: false }, errors: {} },
      { scan_result_id: 101, symbol_id: 2, values: { condition: false }, errors: {} },
    ]);
    render(<Discovery simpleMode />);
    expect(await screen.findByText("discoveryPoolEmpty")).toBeVisible();
    expect(screen.queryByText("noDiscoveryResults")).not.toBeInTheDocument();
    expect(screen.getByText("discoveryPoolOverheatRisk")).not.toBeVisible();
    expect(screen.getByText(/discoverySelectedPool/)).toBeVisible();
    const draft = localStorage.getItem("discovery_indicator_draft_v1");

    await userEvent.click(screen.getByRole("button", { name: "discoveryViewAll" }));
    expect(await screen.findByText("600001")).toBeVisible();
    expect(screen.getByText("600002")).toBeVisible();
    expect(screen.getByText(/discoveryViewingAll/)).toBeVisible();
    expect(localStorage.getItem("discovery_indicator_draft_v1")).toBe(draft);
    await userEvent.click(screen.getByRole("button", { name: "discoveryRestoreFilters" }));
    expect(await screen.findByText("discoveryPoolEmpty")).toBeVisible();
    expect(screen.queryByText("600001")).not.toBeInTheDocument();
    expect(localStorage.getItem("discovery_indicator_draft_v1")).toBe(draft);
  });

  it("候选指标使用是否和数学符号显示条件，不把内部布尔值和运算符交给用户", async () => {
    localStorage.setItem("discovery_indicator_draft_v1", JSON.stringify({
      name: "已有条件", logic: "AND", pool_tab: "all",
      filters: [{ id: "one", indicator_key: "kk_12", operator: "eq", boolean_value: true, number_value: 0 }],
    }));
    mockApi.getLatestDiscoveryCandidates.mockResolvedValue([makeCandidate()]);
    mockApi.getCustomIndicators.mockResolvedValue([{ id: 1, key: "kk_12", name: "kk_12", value_type: "boolean", enabled: true }]);
    mockApi.evaluateDiscoveryIndicators.mockResolvedValue([
      { scan_result_id: 100, symbol_id: 1, values: { kk_12: false }, errors: {} },
    ]);
    const { container } = render(<Discovery simpleMode />);
    await screen.findByText("已有 1 条候选，当前 1 项筛选条件没有匹配结果。");
    await userEvent.click(screen.getByRole("button", { name: "discoveryViewAll" }));

    expect(await screen.findByText("kk_12: 否")).toBeVisible();
    expect(screen.getByText("符合 0/1 项条件 | kk_12 = 是")).toBeVisible();
    expect(container.querySelector(".ant-table-tbody")?.textContent).toContain("中国大陆");
    expect(container.querySelector(".ant-table-tbody")?.textContent).not.toMatch(/\beq\b|\btrue\b|\bfalse\b/);
  });

  it("候选读取失败展示错误和重试，不能显示成功的空结果", async () => {
    mockApi.getLatestDiscoveryCandidates.mockRejectedValueOnce(new Error("candidate source 502"));
    render(<Discovery />);
    const error = await screen.findByRole("alert", { name: "opportunity.candidatesReadFailed" });
    expect(error).toHaveTextContent("candidate source 502");
    expect(screen.queryByText("noDiscoveryResults")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "opportunity.candidatesReadRetry" }));
    expect(await screen.findByText("noDiscoveryResults")).toBeInTheDocument();
    expect(screen.queryByRole("alert", { name: "opportunity.candidatesReadFailed" })).not.toBeInTheDocument();
  });

  it("机会中心准备并扫描完成后自动更新独立候选列表，保留当前筛选草稿", async () => {
    mockApi.getLatestDiscoveryCandidates.mockResolvedValueOnce([makeCandidate({ symbol: "OLD", name: "准备前候选" })]);
    mockApi.getLatestDiscoveryCandidates.mockResolvedValueOnce([makeCandidate({ symbol: "NEW", name: "准备后候选" })]);
    render(<OpportunityCenter />);
    expect(await screen.findByText("OLD")).toBeInTheDocument();
    const name = screen.getByPlaceholderText("dpNamePlaceholder");
    fireEvent.change(name, { target: { value: "保留筛选草稿" } });

    await userEvent.click(screen.getByRole("button", { name: "opportunity.updateAndScreen" }));

    expect(await screen.findByText("NEW")).toBeInTheDocument();
    expect(screen.queryByText("OLD")).not.toBeInTheDocument();
    expect(name).toBeInTheDocument();
    expect(name).toHaveValue("保留筛选草稿");
    expect(mockApi.startDataPrep).toHaveBeenCalledWith({ scope: "cn-stock", trigger_fast_scan_after_ready: true });
  });

  it("准备完成刷新只作用于当前范围，并按任务ID去重", async () => {
    mockApi.getLatestDiscoveryCandidates.mockResolvedValueOnce([makeCandidate({ symbol: "OLD" })]);
    mockApi.getLatestDiscoveryCandidates.mockResolvedValueOnce([makeCandidate({ symbol: "NEW" })]);
    const { rerender } = render(<Discovery />);
    await screen.findByText("OLD");
    rerender(<Discovery refreshRequest={{ scope: "us-stock", taskId: "other-scope" }} />);
    expect(screen.getByText("OLD")).toBeInTheDocument();
    expect(mockApi.getLatestDiscoveryCandidates).toHaveBeenCalledTimes(1);

    rerender(<Discovery refreshRequest={{ scope: "cn-stock", taskId: "prepared-1" }} />);
    expect(await screen.findByText("NEW")).toBeInTheDocument();
    rerender(<Discovery refreshRequest={{ scope: "cn-stock", taskId: "prepared-1" }} />);
    expect(mockApi.getLatestDiscoveryCandidates).toHaveBeenCalledTimes(2);
  });

  it("同范围准备扫描完成后旧挖掘失败折叠为历史，主进度和本次处理不再沿用旧失败", async () => {
    mockContext.discoveryTask = {
      id: 41, scope: "cn-stock", status: "failed", stage: "scan", percent: 100,
      processed: 1642, total: 1642, message: "Old legacy SQL failure",
      errors: ["Old legacy SQL details"], can_retry: true,
    };
    const { rerender, container } = render(<Discovery />);
    expect(await screen.findByText("Old legacy SQL failure")).toBeVisible();
    const draft = screen.getByPlaceholderText("dpNamePlaceholder");
    fireEvent.change(draft, { target: { value: "保留准备后的筛选草稿" } });

    rerender(<Discovery refreshRequest={{ scope: "cn-stock", taskId: "prepared-current" }} />);

    expect(await screen.findByRole("alert", { name: "本次筛选已完成" })).toBeVisible();
    expect(screen.getByText("Old legacy SQL failure")).not.toBeVisible();
    expect(screen.getByText("上次运行记录")).toBeVisible();
    expect(container.querySelector(".discovery-band .panel-meta")).toHaveTextContent("本次筛选已完成");
    expect(screen.getByText("discoveryCurrentRun").parentElement).not.toHaveTextContent("1642/1642");
    expect(screen.queryByRole("button", { name: "retryDiscovery" })).not.toBeInTheDocument();
    expect(draft).toBeInTheDocument();
    expect(draft).toHaveValue("保留准备后的筛选草稿");
    expect(mockContext.discoveryTask.message).toBe("Old legacy SQL failure");
  });

  it.each(["queued", "running", "paused"])("准备后的新%s传统挖掘任务仍优先展示和操作，终态结果也不被旧成功覆盖", async (status) => {
    mockContext.discoveryTask = {
      id: 41, scope: "cn-stock", status: "failed", stage: "scan", percent: 100,
      processed: 1642, total: 1642, message: "Old legacy failure", can_retry: true,
    };
    const { rerender } = render(<Discovery />);
    rerender(<Discovery refreshRequest={{ scope: "cn-stock", taskId: "prepared-current" }} />);
    await screen.findByRole("alert", { name: "本次筛选已完成" });
    mockContext.discoveryTask = {
      id: 42, scope: "cn-stock", status, stage: "scan", percent: 14,
      processed: 7, total: 50, message: "New live mining task", can_resume: true,
    };
    rerender(<Discovery refreshRequest={{ scope: "cn-stock", taskId: "prepared-current" }} />);

    expect(screen.getByText("New live mining task")).toBeVisible();
    expect(screen.queryByRole("alert", { name: "本次筛选已完成" })).not.toBeInTheDocument();
    expect(screen.getByText("discoveryCurrentRun").parentElement).toHaveTextContent("7/50");
    expect(screen.getByRole("button", { name: status === "paused" ? "resumeDiscovery" : "pauseDiscovery" })).toBeEnabled();
    mockContext.discoveryTask = { ...mockContext.discoveryTask, status: "failed", message: "New task failure", can_retry: true };
    rerender(<Discovery refreshRequest={{ scope: "cn-stock", taskId: "prepared-current" }} />);
    expect(screen.getByText("New task failure")).toBeVisible();
    expect(screen.getByRole("button", { name: "retryDiscovery" })).toBeVisible();
  });

  it("其他范围的准备成功不能把当前范围的挖掘失败改成历史", async () => {
    mockContext.discoveryTask = {
      id: 41, scope: "cn-stock", status: "failed", stage: "scan", percent: 100,
      processed: 1642, total: 1642, message: "Current scope failure", can_retry: true,
    };
    const { rerender } = render(<Discovery />);
    rerender(<Discovery refreshRequest={{ scope: "cn-etf", taskId: "prepared-other-scope" }} />);
    expect(screen.getByText("Current scope failure")).toBeVisible();
    expect(screen.queryByRole("alert", { name: "本次筛选已完成" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "retryDiscovery" })).toBeVisible();
  });

  it("机会中心准备入口跟随候选范围，partial任务扫描成功后刷新该范围", async () => {
    mockApi.getLatestDiscoveryCandidates.mockResolvedValueOnce([makeCandidate({ symbol: "CNOLD" })]);
    mockApi.getLatestDiscoveryCandidates.mockResolvedValueOnce([makeCandidate({ symbol: "USBEFORE" })]);
    mockApi.getLatestDiscoveryCandidates.mockResolvedValueOnce([makeCandidate({ symbol: "USAFTER" })]);
    mockApi.getAsyncTask.mockResolvedValueOnce({ id: "prepared-scan-1", status: "partial", percent: 100, result: { fast_scan: { status: "ok" } } });
    render(<OpportunityCenter />);
    await screen.findByText("CNOLD");
    const scopeControl = screen.getAllByText("discoveryScope").find((item) => item.closest("label"))!.closest("label")!;
    fireEvent.mouseDown(scopeControl.querySelector(".ant-select-selector")!);
    fireEvent.click(await screen.findByText("discoveryScopeUsStock"));
    await screen.findByText("USBEFORE");

    await userEvent.click(screen.getByRole("button", { name: "opportunity.updateAndScreen" }));

    expect(await screen.findByText("USAFTER")).toBeInTheDocument();
    expect(mockApi.startDataPrep).toHaveBeenCalledWith({ scope: "us-stock", trigger_fast_scan_after_ready: true });
    expect(mockApi.getLatestDiscoveryCandidates).toHaveBeenLastCalledWith(55, 50, "us-stock");
  });

  it("候选加载中不显示空结果，读取成功为空后才显示空态", async () => {
    let finish!: (rows: any[]) => void;
    mockApi.getLatestDiscoveryCandidates.mockReturnValueOnce(new Promise((resolve) => { finish = resolve; }));
    render(<Discovery />);
    expect(screen.getByRole("status", { name: "opportunity.candidatesReadLoading" })).toBeInTheDocument();
    expect(screen.queryByText("noDiscoveryResults")).not.toBeInTheDocument();
    await act(async () => { finish([]); });
    expect(screen.getByText("noDiscoveryResults")).toBeInTheDocument();
    expect(screen.queryByRole("status", { name: "opportunity.candidatesReadLoading" })).not.toBeInTheDocument();
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
          dataMode: "sync",
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
    // mock i18n 的 t(key) 返回 key，label 为 discoveryPoolHighQuality
    const highQualityTab = screen.getByText("discoveryPoolHighQuality");
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
      // handleRowClick 调用 loadSymbolDetail(id, { focus: true, barLimit: 500 })
      expect(mockContext.loadSymbolDetail).toHaveBeenCalledWith(101, { focus: true, barLimit: 500 });
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
      // handleRowClick 调用 loadSymbolDetail(id, { focus: true, barLimit: 500 })
      expect(mockContext.loadSymbolDetail).toHaveBeenCalledWith(105, { focus: true, barLimit: 500 });
    });
  });

  it("keeps the current indicator when an earlier evaluation resolves last", async () => {
    let resolveA!: (rows: any[]) => void;
    let resolveB!: (rows: any[]) => void;
    const first = new Promise<any[]>((resolve) => { resolveA = resolve; });
    const second = new Promise<any[]>((resolve) => { resolveB = resolve; });
    mockApi.getLatestDiscoveryCandidates.mockResolvedValue([makeCandidate()]);
    mockApi.getCustomIndicators.mockResolvedValue([
      { id: 1, key: "a", name: "Indicator A", value_type: "number", enabled: true },
      { id: 2, key: "b", name: "Indicator B", value_type: "number", enabled: true },
    ]);
    mockApi.evaluateDiscoveryIndicators.mockImplementationOnce(() => first).mockImplementationOnce(() => second);
    render(<Discovery />);
    await waitFor(() => expect(mockApi.evaluateDiscoveryIndicators).toHaveBeenCalledTimes(1));
    const ruleLabel = screen.getByText("dpRuleN").closest("label")!;
    fireEvent.mouseDown(ruleLabel.querySelector(".ant-select-selector")!);
    fireEvent.click(await screen.findByText("Indicator B"));
    await waitFor(() => expect(mockApi.evaluateDiscoveryIndicators).toHaveBeenCalledTimes(2));
    await act(async () => { resolveB([{ scan_result_id: 100, symbol_id: 1, values: { b: 20 } }]); });
    await waitFor(() => expect(screen.getByText("Indicator B: 20.00")).toBeInTheDocument());
    await act(async () => { resolveA([{ scan_result_id: 100, symbol_id: 1, values: { a: 10 } }]); });
    expect(screen.getByText("Indicator B: 20.00")).toBeInTheDocument();
  });

  it("shows each uncomputed reason and never matches null against neq", async () => {
    localStorage.setItem("discovery_indicator_draft_v1", JSON.stringify({ name: "Errors", logic: "AND", pool_tab: "all", filters: [{ id: "one", indicator_key: "condition", operator: "neq", boolean_value: true, number_value: 0 }] }));
    mockApi.getLatestDiscoveryCandidates.mockResolvedValue([
      makeCandidate({ symbol_id: 1, symbol: "600001", name: "Unavailable", scan_result_id: 100 }),
      makeCandidate({ symbol_id: 2, symbol: "600002", name: "Real false", scan_result_id: 101 }),
    ]);
    mockApi.getCustomIndicators.mockResolvedValue([{ id: 1, key: "condition", name: "Condition", value_type: "boolean", enabled: true }]);
    mockApi.evaluateDiscoveryIndicators.mockResolvedValue([
      { scan_result_id: 100, symbol_id: 1, values: { condition: null }, errors: { condition: "Formula requires 20 local bars" } },
      { scan_result_id: 101, symbol_id: 2, values: { condition: false }, errors: {} },
    ]);
    const { container } = render(<Discovery />);
    await waitFor(() => expect(screen.getByText("Formula requires 20 local bars")).toBeInTheDocument());
    const table = container.querySelector(".ant-table-tbody")!;
    expect(table.textContent).not.toContain("600001");
    expect(table.textContent).toContain("600002");
    expect(table.textContent).toContain("Condition: 否");
  });

  it("keeps the new scope candidates when the old scope resolves last", async () => {
    let resolveOld!: (rows: any[]) => void;
    let resolveCurrent!: (rows: any[]) => void;
    const oldScope = new Promise<any[]>((resolve) => { resolveOld = resolve; });
    const newScope = new Promise<any[]>((resolve) => { resolveCurrent = resolve; });
    mockApi.getLatestDiscoveryCandidates.mockImplementationOnce(() => oldScope).mockImplementationOnce(() => newScope);
    const { container } = render(<Discovery />);
    await waitFor(() => expect(mockApi.getLatestDiscoveryCandidates).toHaveBeenCalledTimes(1));
    const scopeControl = screen.getAllByText("discoveryScope").find((item) => item.closest("label"))!.closest("label")!;
    fireEvent.mouseDown(scopeControl.querySelector(".ant-select-selector")!);
    fireEvent.click(await screen.findByText("discoveryScopeUsStock"));
    await waitFor(() => expect(mockApi.getLatestDiscoveryCandidates).toHaveBeenCalledTimes(2));
    await act(async () => { resolveCurrent([makeCandidate({ symbol: "CURRENT", name: "Current scope", scan_result_id: 200 })]); });
    await waitFor(() => expect(screen.getByText("CURRENT")).toBeInTheDocument());
    await act(async () => { resolveOld([makeCandidate({ symbol: "OLD", name: "Old scope" })]); });
    expect(container.querySelector(".ant-table-tbody")?.textContent).toContain("CURRENT");
    expect(container.querySelector(".ant-table-tbody")?.textContent).not.toContain("OLD");
  });
});
