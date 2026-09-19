// WP1-FIX.5：组合页 PortfolioWorkbench OpportunityStatusBadges 接入定向测试
//
// 覆盖：
// - 持仓表新增"关联状态"列
// - 持仓行显示徽标
// - 持仓徽标不会把持仓显示成观察项（关键约束）
// - 接口失败降级"状态未知"
// - 点击徽标触发 handleSymbolClick（打开详情弹窗）
//
// 约束：
// - 不修改已稳定组件实现
// - mock useSymbolRelationships hook 直接控制徽标数据
// - mock AppContext 提供 PortfolioWorkbench 必需字段
// - 不调用真实后端 API
// - 文件独立，避免继承现有 PortfolioWorkbench.test.tsx 的 TypeScript 错误
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

// 使用 vi.hoisted 提升 mock，使其可在 vi.mock factory 内引用
const { mockContext, mockApi, mockHook } = vi.hoisted(() => ({
  mockContext: {
    workbench: null as any,
    newsSnapshot: null as any,
    candidateSearch: "",
    portfolioId: 1,
    activeSymbolId: null as number | null,
    locale: "zh-CN" as const,
    setCandidateSearch: vi.fn((v: string) => {}),
    loadWorkbench: vi.fn(async () => {}),
    showToast: vi.fn((_type: string, _msg: string) => {}),
    loadSymbolDetail: vi.fn(async (_id: number) => {}),
    // 能力门禁字段：CapabilityGateButton 通过 useApp 读取这些方法
    capabilities: null as any,
    capabilitiesLoading: false,
    getCapability: vi.fn((_: string) => undefined),
    isCapabilityBlocked: vi.fn((_: string) => false),
    loadCapabilities: vi.fn(async () => {}),
  },
  mockApi: {
    getPositions: vi.fn(async () => [] as any[]),
    getAllocation: vi.fn(async () => null),
    getSymbols: vi.fn(async () => [] as any[]),
    createSymbol: vi.fn(async () => ({ id: 1 })),
    upsertPosition: vi.fn(async () => ({ ok: true })),
    deletePosition: vi.fn(async () => ({ ok: true })),
    upsertPortfolioRule: vi.fn(async () => ({ ok: true })),
    backupDatabase: vi.fn(async () => ({ backup_path: "/tmp/backup.db" })),
    listBackups: vi.fn(async () => [] as any[]),
    restoreDatabase: vi.fn(async () => ({ ok: true })),
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
  assetTypeLabel: (v: string | null | undefined) => v ?? "unknown",
  regionShortLabel: (v: string | null | undefined) => v ?? "-",
  getLocale: () => "zh-CN",
  setLocale: () => {},
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

// Mock AppContext：提供 PortfolioWorkbench 必需字段
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

import PortfolioWorkbench from "../PortfolioWorkbench";

// 构造一份完整的 workbench 数据用于带数据场景
function makeWorkbench() {
  return {
    portfolio: {
      id: 1,
      name: "默认组合",
      currency: "CNY",
      total_capital: 100000,
      investable_ratio: 0.9,
      cash_reserve_ratio: 0.1,
    },
    active_rule: {
      id: 1,
      rule_name: "默认规则",
      max_single_position_pct: 0.3,
      max_sector_position_pct: 0.3,
      max_stock_position_pct: 0.6,
      max_etf_position_pct: 0.4,
      max_loss_per_trade_pct: 0.05,
      max_open_positions: 10,
    },
    market_scope: {
      selected_group: "all",
      available_groups: ["all"],
      total_symbols: 100,
      filtered_symbols: 80,
      region_counts: { cn: 80 },
    },
    overview: {
      symbols_count: 100,
      watchlists_count: 2,
      total_position_pct: 0.5,
      cash_pct: 0.5,
      top_candidates: [],
      risk_flags: [],
    },
    account_summary: {
      cash_balance: 50000,
      available_cash: 50000,
      market_value: 50000,
      total_equity: 100000,
      realized_pnl: 1000,
      unrealized_pnl: 500,
      cash_pct: 0.5,
      invested_pct: 0.5,
      position_count: 2,
      trade_count_7d: 3,
      last_trade_at: "2026-07-04T10:00:00Z",
    },
    latest_scan: {
      scan_run_id: 1,
      run_name: "扫描1",
      created_at: "2026-07-04T09:00:00Z",
      executable_count: 3,
      total_results: 5,
      auto_scan: false,
    },
    candidates: [
      {
        symbol_id: 101,
        symbol: "000001",
        name: "平安银行",
        region: "cn",
        asset_type: "stock",
        market: "szse",
        rank_no: 1,
        quality_score: 80,
        timing_score: 70,
        priority_score: 75,
        stage: "start",
        action: "open",
        recommended_position_pct: 0.1,
      },
    ],
    latest_scores: [],
    positions: [
      {
        symbol_id: 201,
        symbol: "600000",
        name: "浦发银行",
        quantity: 100,
        avg_cost: 10.5,
        latest_price: 11.0,
        market_value: 1100,
        position_pct: 0.011,
        unrealized_pnl: 50,
        unrealized_pnl_pct: 0.0476,
      },
    ],
    watchlists: [],
    journals: [],
    recent_trades: [],
  };
}

// 构造一份"有持仓 has_position=true，has_observation=false"的关联数据
// 关键约束：徽标只显示持仓 active，不显示观察 active（不把持仓显示成观察项）
function makePositionOnlyRelationships(symbolId: number) {
  return {
    data: {
      symbol_id: symbolId,
      symbol: "600000",
      candidate: { has_candidate: false, candidate_id: null, scope: null, stage: null, action: null, priority_score: null, quality_score: null, timing_score: null, data_credibility: null, generated_at: null, scan_run_id: null, snapshot_id: null, snapshot_generated_at: null },
      observation: { has_observation: false, watchlist_id: null, watchlist_name: null, watchlist_item_id: null, origin_type: null, status: null, priority: null, tags: [], target_portfolio_id: null, added_at: null },
      portfolio_member: { has_portfolio_membership: false, portfolio_id: null, portfolio_name: null, member_id: null, member_status: null, execution_mode: null, source_type: null, effective_from: null, note: null },
      position: { has_position: true, portfolio_id: 1, portfolio_name: "默认组合", position_id: 1, quantity: 100, cost_price: 10.5, latest_price: 11.0, market_value: 1100, opened_at: "2026-07-01T00:00:00Z" },
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

describe("PortfolioWorkbench OpportunityStatusBadges 接入测试", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockContext.workbench = makeWorkbench();
    mockContext.newsSnapshot = null;
    mockContext.candidateSearch = "";
    mockContext.activeSymbolId = null;
    // 持仓表格数据：getPositions 必须返回持仓数组
    mockApi.getPositions.mockImplementation(async () => [
      {
        symbol_id: 201,
        symbol: "600000",
        name: "浦发银行",
        quantity: 100,
        avg_cost: 10.5,
        latest_price: 11.0,
        market_value: 1100,
        position_pct: 0.011,
        unrealized_pnl: 50,
        unrealized_pnl_pct: 0.0476,
      },
    ]);
    mockApi.getAllocation.mockImplementation(async () => null);
    mockApi.listBackups.mockImplementation(async () => []);
    // 默认 hook 返回空关联
    mockHook.useSymbolRelationships.mockImplementation((_id: number | null | undefined) => ({
      data: null,
      loading: false,
      error: null,
      degraded: false,
      refresh: vi.fn(async () => {}),
    }));
  });

  // 1. 持仓表新增"关联状态"列：渲染含持仓数据，断言表头含"opportunityObservationColStatus"
  it("持仓表应新增关联状态列（表头含 opportunityObservationColStatus）", async () => {
    render(<PortfolioWorkbench openMetricModal={vi.fn()} />);
    // 等待持仓表格渲染
    await waitFor(() => {
      expect(screen.getByText("600000")).toBeInTheDocument();
    });
    // 表头应包含"关联状态"列
    expect(screen.getByText("opportunityObservationColStatus")).toBeInTheDocument();
  });

  // 2. 持仓行显示徽标：mock has_position=true，断言每行持仓含徽标
  it("持仓行应渲染 OpportunityStatusBadges 徽标", async () => {
    mockHook.useSymbolRelationships.mockImplementation((id: number | null | undefined) =>
      makePositionOnlyRelationships(Number(id ?? 0)),
    );
    render(<PortfolioWorkbench openMetricModal={vi.fn()} />);
    await waitFor(() => {
      expect(screen.getByText("600000")).toBeInTheDocument();
    });
    // 表格中应存在徽标根节点（class opportunity-status-badges）
    const badges = document.querySelectorAll(".opportunity-status-badges");
    expect(badges.length).toBeGreaterThan(0);
    // 持仓 active 徽标应渲染
    await waitFor(() => {
      expect(screen.getAllByText("opportunityBadgePosition").length).toBeGreaterThan(0);
    });
  });

  // 3. 持仓徽标不会把持仓显示成观察项：关键约束
  //    mock has_position=true, has_observation=false，断言只显示"持仓"active，不显示"观察"active
  it("持仓徽标不应把持仓显示成观察项（has_position=true, has_observation=false）", async () => {
    mockHook.useSymbolRelationships.mockImplementation((id: number | null | undefined) =>
      makePositionOnlyRelationships(Number(id ?? 0)),
    );
    render(<PortfolioWorkbench openMetricModal={vi.fn()} />);
    await waitFor(() => {
      expect(screen.getByText("opportunityBadgePosition")).toBeInTheDocument();
    });
    // 关键约束：不应显示"观察"active 徽标
    expect(screen.queryByText("opportunityBadgeObservation")).not.toBeInTheDocument();
    // 也不应显示其他 active 徽标
    expect(screen.queryByText("opportunityBadgeCandidate")).not.toBeInTheDocument();
    expect(screen.queryByText("opportunityBadgeAlert")).not.toBeInTheDocument();
    expect(screen.queryByText("opportunityBadgePortfolioMember")).not.toBeInTheDocument();
  });

  // 4. 接口失败降级：mock 返回 error，断言显示"状态未知"
  it("接口失败时应降级显示 opportunityBadgeUnknown 文案", async () => {
    mockHook.useSymbolRelationships.mockImplementation((_id: number | null | undefined) => ({
      data: null,
      loading: false,
      error: "network error",
      degraded: true,
      refresh: vi.fn(async () => {}),
    }));
    render(<PortfolioWorkbench openMetricModal={vi.fn()} />);
    await waitFor(() => {
      expect(screen.getByText("600000")).toBeInTheDocument();
    });
    // 至少一处显示"状态未知"
    await waitFor(() => {
      expect(screen.getAllByText("opportunityBadgeUnknown").length).toBeGreaterThan(0);
    });
  });

  // 5. 点击徽标触发 handleSymbolClick：点击徽标应打开详情弹窗（调用 ctx.loadSymbolDetail）
  it("点击徽标应触发 handleSymbolClick 并调用 ctx.loadSymbolDetail", async () => {
    mockHook.useSymbolRelationships.mockImplementation((id: number | null | undefined) =>
      makePositionOnlyRelationships(Number(id ?? 0)),
    );
    render(<PortfolioWorkbench openMetricModal={vi.fn()} />);
    await waitFor(() => {
      expect(screen.getByText("opportunityBadgePosition")).toBeInTheDocument();
    });
    // 点击持仓徽标（cursor:pointer 标识可点击）
    const badge = screen.getAllByText("opportunityBadgePosition")[0].closest(".ant-tag") as HTMLElement;
    expect(badge).not.toBeNull();
    fireEvent.click(badge);
    // 应触发 ctx.loadSymbolDetail（handleSymbolClick → ctx.loadSymbolDetail）
    await waitFor(() => {
      expect(mockContext.loadSymbolDetail).toHaveBeenCalled();
    });
  });
});
