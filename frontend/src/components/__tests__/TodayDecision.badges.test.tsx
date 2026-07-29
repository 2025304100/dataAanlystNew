// WP1-FIX.5：今日决策页 OpportunityStatusBadges 接入定向测试
//
// 覆盖：
// - 候选列表区域渲染徽标（compact 模式）
// - 行动列表区域渲染徽标（compact 模式）
// - compact 模式徽标 data-state 为 mixed 或 empty
// - 接口失败降级"状态未知"
// - 点击徽标触发 openSymbolDetail（最终调用 ctx.loadSymbolDetail）
//
// 约束：
// - 不修改已稳定组件实现
// - 通过 vi.mock 直接 mock useSymbolRelationships hook，避免构造完整 SymbolRelationships
// - mock AppContext 提供必要字段
// - 不调用真实后端 API
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

// 使用 vi.hoisted 提升 mock，使其可在 vi.mock factory 内引用
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
    // health 返回 null：所有 health?.X 均返回 undefined，避免触发未定义字段读取
    getDataHealth: vi.fn(async () => null),
    getFactorOverview: vi.fn(async () => null),
  },
  // useSymbolRelationships mock：返回空关联（所有 has_*=false）作为默认
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

// Mock AppContext：提供 TodayDecision 必需字段
vi.mock("../../context/AppContext", () => ({
  useApp: () => mockContext,
}));

// Mock api/client：避免真实请求
vi.mock("../../api/client", () => ({ api: mockApi }));

// Mock useSymbolRelationships hook：直接控制徽标组件数据来源
vi.mock("../../hooks/useSymbolRelationships", () => ({
  useSymbolRelationships: (id: number | null | undefined) => mockHook.useSymbolRelationships(id),
}));

import TodayDecision from "../TodayDecision";

// 构造一份 workbench 数据，包含候选 + 可执行行动
function makeWorkbench() {
  return {
    portfolio: { id: 1, name: "默认组合", currency: "CNY" },
    active_rule: null,
    market_scope: { selected_group: "all", available_groups: ["all"], total_symbols: 100, filtered_symbols: 80, region_counts: { cn: 80 } },
    overview: { symbols_count: 100, watchlists_count: 2, total_position_pct: 0.5, cash_pct: 0.5, top_candidates: [], risk_flags: [] },
    account_summary: {
      cash_balance: 50000, available_cash: 50000, market_value: 50000, total_equity: 100000,
      realized_pnl: 1000, unrealized_pnl: 500, cash_pct: 0.5, invested_pct: 0.5,
      position_count: 1, trade_count_7d: 0, last_trade_at: null,
    },
    latest_scan: null,
    candidates: [
      {
        symbol_id: 101, symbol: "600001", name: "测试股票A",
        region: "cn", asset_type: "stock", market: "szse",
        rank_no: 1, quality_score: 80, timing_score: 70, priority_score: 75,
        stage: "start", action: "open", recommended_position_pct: 0.1,
        opportunity_score: 75, base_opportunity_score: 70, news_multiplier: 1.0,
      },
      {
        symbol_id: 102, symbol: "600002", name: "测试股票B",
        region: "cn", asset_type: "stock", market: "szse",
        rank_no: 2, quality_score: 70, timing_score: 65, priority_score: 68,
        stage: "start", action: "buy_dip", recommended_position_pct: 0.08,
        opportunity_score: 68, base_opportunity_score: 65, news_multiplier: 1.0,
      },
    ],
    latest_scores: [],
    positions: [],
    watchlists: [],
    journals: [],
    recent_trades: [],
  };
}

// 构造一份"有候选 has_candidate=true"的关联数据
function makeCandidateRelationships(symbolId: number) {
  return {
    data: {
      symbol_id: symbolId,
      symbol: "600001",
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

// 构造一份"有持仓 has_position=true"的关联数据
function makePositionRelationships(symbolId: number) {
  return {
    data: {
      symbol_id: symbolId,
      symbol: "600002",
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

describe("TodayDecision OpportunityStatusBadges 接入测试", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockContext.workbench = makeWorkbench();
    mockContext.newsSnapshot = null;
    mockContext.activeSymbolId = null;
    // 默认 mock：返回空关联（所有 has_*=false），徽标处于 compact empty 状态
    mockHook.useSymbolRelationships.mockImplementation((_id: number | null | undefined) => ({
      data: null,
      loading: false,
      error: null,
      degraded: false,
      refresh: vi.fn(async () => {}),
    }));
  });

  // 1. 候选列表显示徽标：mock has_candidate=true，断言徽标组件已渲染
  it("候选列表区域应渲染 OpportunityStatusBadges 徽标", async () => {
    mockHook.useSymbolRelationships.mockImplementation((id: number | null | undefined) =>
      makeCandidateRelationships(Number(id ?? 0)),
    );
    render(<TodayDecision />);
    // 等待候选数据加载
    await waitFor(() => {
      expect(screen.getByText("600001")).toBeInTheDocument();
    });
    // 候选区域应存在徽标根节点（class opportunity-status-badges）
    const badges = document.querySelectorAll(".opportunity-status-badges");
    expect(badges.length).toBeGreaterThan(0);
    // compact 模式下有激活状态时显示 opportunityBadgeMixed 汇总徽标
    await waitFor(() => {
      expect(screen.getAllByText("opportunityBadgeMixed").length).toBeGreaterThan(0);
    });
  });

  // 2. 行动列表显示徽标：mock has_position=true，断言行动列表区域含徽标
  it("行动列表区域应渲染 OpportunityStatusBadges 徽标", async () => {
    mockHook.useSymbolRelationships.mockImplementation((id: number | null | undefined) => {
      // 候选行（symbol_id=102，action=buy_dip）展示持仓徽标
      if (Number(id) === 102) return makePositionRelationships(102);
      return makePositionRelationships(Number(id ?? 0));
    });
    render(<TodayDecision />);
    // 等待行动列表渲染（actions 区域标题 tdActions）
    await waitFor(() => {
      expect(screen.getByText("tdActions")).toBeInTheDocument();
    });
    // 600002 出现在候选 + 行动列表中
    await waitFor(() => {
      expect(screen.getAllByText("600002").length).toBeGreaterThan(0);
    });
    // compact 模式下有持仓 active，应显示 opportunityBadgeMixed 汇总徽标
    await waitFor(() => {
      expect(screen.getAllByText("opportunityBadgeMixed").length).toBeGreaterThan(0);
    });
  });

  // 3. compact 模式：徽标使用 compact（data-state="mixed" 或 data-state="empty"）
  it("徽标应为 compact 模式且 data-state 为 mixed 或 empty", async () => {
    // 第一个候选 has_candidate=true（mixed），第二个空（empty）
    mockHook.useSymbolRelationships.mockImplementation((id: number | null | undefined) => {
      if (Number(id) === 101) return makeCandidateRelationships(101);
      return {
        data: {
          symbol_id: Number(id ?? 0), symbol: "x",
          candidate: { has_candidate: false, candidate_id: null, scope: null, stage: null, action: null, priority_score: null, quality_score: null, timing_score: null, data_credibility: null, generated_at: null, scan_run_id: null, snapshot_id: null, snapshot_generated_at: null },
          observation: { has_observation: false, watchlist_id: null, watchlist_name: null, watchlist_item_id: null, origin_type: null, status: null, priority: null, tags: [], target_portfolio_id: null, added_at: null },
          portfolio_member: { has_portfolio_membership: false, portfolio_id: null, portfolio_name: null, member_id: null, member_status: null, execution_mode: null, source_type: null, effective_from: null, note: null },
          position: { has_position: false, portfolio_id: null, portfolio_name: null, position_id: null, quantity: null, cost_price: null, latest_price: null, market_value: null, opened_at: null },
          alert: { has_active_alert: false, alert_rule_ids: [], active_alert_events: 0, latest_alert_severity: null, latest_alert_at: null },
          fetched_at: "2026-07-04T10:00:00Z", degraded: false, degraded_reason: null,
        },
        loading: false, error: null, degraded: false, refresh: vi.fn(async () => {}),
      };
    });
    render(<TodayDecision />);
    await waitFor(() => {
      expect(screen.getByText("600001")).toBeInTheDocument();
    });
    // compact 徽标容器应携带 compact class
    const compactBadges = document.querySelectorAll(".opportunity-status-badges.compact");
    expect(compactBadges.length).toBeGreaterThan(0);
    // 至少一个徽标的 data-state 应为 mixed 或 empty
    const states = Array.from(compactBadges).map((el) => el.getAttribute("data-state"));
    const hasMixedOrEmpty = states.some((s) => s === "mixed" || s === "empty");
    expect(hasMixedOrEmpty).toBe(true);
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
    render(<TodayDecision />);
    await waitFor(() => {
      expect(screen.getByText("600001")).toBeInTheDocument();
    });
    // 至少一处显示"状态未知"
    await waitFor(() => {
      expect(screen.getAllByText("opportunityBadgeUnknown").length).toBeGreaterThan(0);
    });
  });

  // 5. 点击徽标触发 openSymbolDetail：点击徽标应最终调用 ctx.loadSymbolDetail
  it("点击徽标应触发 openSymbolDetail 并调用 ctx.loadSymbolDetail", async () => {
    mockHook.useSymbolRelationships.mockImplementation((id: number | null | undefined) =>
      makeCandidateRelationships(Number(id ?? 0)),
    );
    render(<TodayDecision />);
    // compact 模式下有激活状态时显示 opportunityBadgeMixed 汇总徽标（携带 onClick）
    await waitFor(() => {
      expect(screen.getAllByText("opportunityBadgeMixed").length).toBeGreaterThan(0);
    });
    // 点击 mixed 徽标（cursor:pointer 标识可点击）
    const badge = screen.getAllByText("opportunityBadgeMixed")[0].closest(".ant-tag") as HTMLElement;
    expect(badge).not.toBeNull();
    fireEvent.click(badge);
    // 应触发 ctx.loadSymbolDetail（openSymbolDetail → openSymbol → ctx.loadSymbolDetail）
    await waitFor(() => {
      expect(mockContext.loadSymbolDetail).toHaveBeenCalled();
    });
  });
});
