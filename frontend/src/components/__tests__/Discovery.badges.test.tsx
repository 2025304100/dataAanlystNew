// WP1-FIX.5：候选列表 Discovery OpportunityStatusBadges 接入定向测试
//
// 覆盖：
// - 候选表新增"关联状态"列（表头 t("opportunityObservationColStatus")）
// - 每行候选显示徽标
// - 接口失败降级"状态未知"
// - 点击徽标触发 handleRowClick（最终调用 ctx.loadSymbolDetail）
//
// 约束：
// - 不修改已稳定组件实现
// - mock useSymbolRelationships hook 直接控制徽标数据
// - mock AppContext 提供 Discovery 必需字段
// - 不调用真实后端 API
// - 文件独立，避免继承现有 Discovery.test.tsx 的 mock 问题
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

// 使用 vi.hoisted 提升 mock，使其可在 vi.mock factory 内引用
const { mockContext, mockApi, mockHook } = vi.hoisted(() => ({
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
    setDiscoveryScope: vi.fn(async (_scope: string) => {}),
    loadDiscoveryScopeStats: vi.fn(async () => {}),
    fetchDiscoveryTasks: vi.fn(async () => null),
    showToast: vi.fn((_type: string, _msg: string) => {}),
    // 能力门禁字段：CapabilityGateButton 通过 useApp 读取这些方法
    capabilities: null as any,
    capabilitiesLoading: false,
    getCapability: vi.fn((_: string) => undefined),
    isCapabilityBlocked: vi.fn((_: string) => false),
    loadCapabilities: vi.fn(async () => {}),
  },
  mockApi: {
    getLatestDiscoveryCandidates: vi.fn(async () => [] as any[]),
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
  },
  // useSymbolRelationships mock：默认返回空关联（所有 has_*=false）
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

// Mock i18n：t 返回 key，template 返回拼接后的字符串
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

// Mock antd：保留组件库，仅覆盖 message
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

// Mock AppContext：提供 Discovery 必需字段
vi.mock("../../context/AppContext", () => ({
  useApp: () => mockContext,
}));

// Mock api/client：避免真实请求
vi.mock("../../api/client", () => ({ api: mockApi }));

// Mock ExplainButton：避免渲染依赖 useAIAssistant 的真实组件
vi.mock("../ai/ExplainButton", () => ({
  __esModule: true,
  default: () => <div data-testid="explain-button-mock" />,
}));

// Mock useSymbolRelationships hook：直接控制徽标组件数据来源
vi.mock("../../hooks/useSymbolRelationships", () => ({
  useSymbolRelationships: (id: number | null | undefined) => mockHook.useSymbolRelationships(id),
}));

import Discovery from "../Discovery";

// 构造一份候选数据，匹配 actionable pool（priority_score>=60 + action=open）
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

// 构造一份"有候选 has_candidate=true"的关联数据
function makeCandidateRelationships(symbolId: number) {
  return {
    data: {
      symbol_id: symbolId,
      symbol: "600000",
      candidate: { has_candidate: true, candidate_id: 1, scope: "cn-stock", stage: null, action: null, priority_score: null, quality_score: null, timing_score: null, data_credibility: null, generated_at: null, scan_run_id: null, snapshot_id: null, snapshot_generated_at: null },
      observation: { has_observation: false, watchlist_id: null, watchlist_name: null, watchlist_item_id: null, origin_type: null, status: null, priority: null, tags: [], target_portfolio_id: null, added_at: null },
      portfolio_member: { has_portfolio_membership: false, portfolio_id: null, portfolio_name: null, member_id: null, member_status: null, execution_mode: null, source_type: null, effective_from: null, note: null },
      position: { has_position: false, portfolio_id: null, portfolio_name: null, position_id: null, quantity: null, cost_price: null, latest_price: null, market_value: null, opened_at: null },
      alert: { has_active_alert: false, alert_rule_ids: [], active_alert_events: 0, latest_alert_severity: null, latest_alert_at: null },
      fetched_at: "2026-07-04T10:00:00Z",
      degraded: false,
      degraded_reason: null,
    },
    loading: false,
    error: null,
    degraded: false,
    refresh: vi.fn(async () => {}),
  };
}

describe("Discovery OpportunityStatusBadges 接入测试", () => {
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
    // 默认 hook 返回空关联
    mockHook.useSymbolRelationships.mockImplementation((_id: number | null | undefined) => ({
      data: null,
      loading: false,
      error: null,
      degraded: false,
      refresh: vi.fn(async () => {}),
    }));
  });

  // 1. 候选表新增"关联状态"列：表头应包含 opportunityObservationColStatus
  it("候选表应新增关联状态列（表头含 opportunityObservationColStatus）", async () => {
    mockApi.getLatestDiscoveryCandidates.mockImplementation(async () => [
      makeCandidate({ symbol_id: 101, symbol: "600001", name: "测试股票A" }),
    ]);
    render(<Discovery />);
    // 等待表格渲染
    await waitFor(() => {
      expect(screen.getByText("600001")).toBeInTheDocument();
    });
    // 表头应包含"关联状态"列
    expect(screen.getByText("opportunityObservationColStatus")).toBeInTheDocument();
  });

  // 2. 每行候选显示徽标：mock has_candidate=true，断言每行含 OpportunityStatusBadges
  it("每行候选应渲染 OpportunityStatusBadges 徽标", async () => {
    mockApi.getLatestDiscoveryCandidates.mockImplementation(async () => [
      makeCandidate({ symbol_id: 101, symbol: "600001", name: "测试股票A" }),
      makeCandidate({ symbol_id: 102, symbol: "600002", name: "测试股票B" }),
    ]);
    mockHook.useSymbolRelationships.mockImplementation((id: number | null | undefined) =>
      makeCandidateRelationships(Number(id ?? 0)),
    );
    render(<Discovery />);
    await waitFor(() => {
      expect(screen.getByText("600001")).toBeInTheDocument();
      expect(screen.getByText("600002")).toBeInTheDocument();
    });
    // 表格中应存在徽标根节点（class opportunity-status-badges）
    const badges = document.querySelectorAll(".opportunity-status-badges");
    expect(badges.length).toBeGreaterThanOrEqual(2);
    // 候选 active 徽标应渲染（Discovery 非 compact 模式，直接显示 candidate 文案）
    await waitFor(() => {
      expect(screen.getAllByText("opportunityBadgeCandidate").length).toBeGreaterThan(0);
    });
  });

  // 3. 接口失败降级"状态未知"：mock 返回 error，断言显示"状态未知"
  it("接口失败时应降级显示 opportunityBadgeUnknown 文案", async () => {
    mockApi.getLatestDiscoveryCandidates.mockImplementation(async () => [
      makeCandidate({ symbol_id: 101, symbol: "600001", name: "测试股票A" }),
    ]);
    mockHook.useSymbolRelationships.mockImplementation((_id: number | null | undefined) => ({
      data: null,
      loading: false,
      error: "network error",
      degraded: true,
      refresh: vi.fn(async () => {}),
    }));
    render(<Discovery />);
    await waitFor(() => {
      expect(screen.getByText("600001")).toBeInTheDocument();
    });
    // 至少一处显示"状态未知"
    await waitFor(() => {
      expect(screen.getAllByText("opportunityBadgeUnknown").length).toBeGreaterThan(0);
    });
  });

  // 4. 点击徽标触发 handleRowClick：点击徽标应最终调用 ctx.loadSymbolDetail
  it("点击徽标应触发 handleRowClick 并调用 ctx.loadSymbolDetail", async () => {
    mockApi.getLatestDiscoveryCandidates.mockImplementation(async () => [
      makeCandidate({ symbol_id: 101, symbol: "600001", name: "测试股票A" }),
    ]);
    mockHook.useSymbolRelationships.mockImplementation((id: number | null | undefined) =>
      makeCandidateRelationships(Number(id ?? 0)),
    );
    render(<Discovery />);
    await waitFor(() => {
      expect(screen.getByText("opportunityBadgeCandidate")).toBeInTheDocument();
    });
    // 点击候选徽标（cursor:pointer 标识可点击）
    const badge = screen.getAllByText("opportunityBadgeCandidate")[0].closest(".ant-tag") as HTMLElement;
    expect(badge).not.toBeNull();
    fireEvent.click(badge);
    // 应触发 ctx.loadSymbolDetail（handleRowClick → ctx.loadSymbolDetail）
    await waitFor(() => {
      expect(mockContext.loadSymbolDetail).toHaveBeenCalled();
    });
  });
});
