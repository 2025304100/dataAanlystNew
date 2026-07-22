// WP9 旧入口与重复状态清理测试
//
// 覆盖：
// - WP9.1：investment tab 已从一级导航移除，?tab=investment 兼容路由仍可访问
// - WP9.2：localStorage.ic_favorites 不再被写入（只读回退期）
// - WP9.3：即时提醒显式标注"即时计算（未持久化）"
// - WP9.4：组合工作台改为指向机会中心的链接卡片，不再重复呈现机会/观察
//
// 约束：
// - 不调用真实后端，所有 API 通过 vi.fn mock
// - i18n mock：t 返回 key，便于按 key 断言
// - localStorage mock：通过 spyOn 监听 setItem
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

// ─── WP9.1 / WP9.2 共用 mock context ─────────────────────
const { mockContext, mockApi, mockRequestJson } = vi.hoisted(() => {
  return {
    mockContext: {
      workbench: {
        positions: [],
        latest_scores: [],
        portfolio: { total_capital: 100000, investable_ratio: 0.9 },
        watchlists: [],
        portfolios: [{ id: 1, name: "Test Portfolio", is_default: 1 }],
      } as any,
      detail: null as any,
      activeSymbolId: 42 as number | null,
      portfolioId: 1 as number | null,
      activeTab: "investment",
      activeWatchlistId: 1 as number | null,
      chartTimeframe: "daily" as "daily" | "weekly",
      chartWindowSize: 60,
      chartExpanded: false,
      chartRange: null,
      futurePlanScenario: "general",
      futurePlanCustom: { horizonDays: 20, pullbackPct: 3, positionPct: 5 },
      simPrice: "",
      simQuantity: "",
      locale: "zh-CN" as "zh-CN" | "en-US",
      portfolios: [{ id: 1, name: "Test Portfolio", is_default: 1 }] as any[],
      candidateSearch: "",
      setChartWindowSize: vi.fn(),
      setChartTimeframe: vi.fn(),
      setChartRange: vi.fn(),
      setChartExpanded: vi.fn(),
      setFuturePlanScenario: vi.fn(),
      setFuturePlanCustom: vi.fn(),
      setSimPrice: vi.fn(),
      setSimQuantity: vi.fn(),
      setActiveTab: vi.fn(),
      setActiveSubTab: vi.fn(),
      setActiveSymbolId: vi.fn(),
      setCandidateSearch: vi.fn(),
      loadSymbolDetail: vi.fn(async () => undefined),
      loadWorkbench: vi.fn(async () => undefined),
      generateTradeSetup: vi.fn(async () => undefined),
      showToast: vi.fn(),
    },
    mockApi: {
      getSymbols: vi.fn(async (_q: string) => [] as any[]),
      getBars: vi.fn(async (_id: number, _limit: number) => [] as any[]),
      getSymbolFactorExplanation: vi.fn(async (_id: number) => null as any),
      saveTradeSetupTranches: vi.fn(async () => ({} as any)),
      getSymbolRelationships: vi.fn(async (_id: number) => null as any),
      createAlertRule: vi.fn(async (_payload: unknown) => ({ id: 999 }) as any),
      getPositions: vi.fn(async () => [] as any[]),
      getAllocation: vi.fn(async () => null as any),
    },
    mockRequestJson: vi.fn(async () => [] as any[]),
  };
});

// ─── Mock i18n：t 返回 key，template 返回拼接后的字符串 ───
vi.mock("../../i18n", () => ({
  t: (key: string) => key,
  template: (key: string, params: Record<string, string | number> = {}) =>
    key.replace(/\{(\w+)\}/g, (_, name) => String(params[name] ?? "")),
  DOT: " | ",
  stageLabel: (v: string | null | undefined) => v ?? "-",
  actionLabel: (v: string | null | undefined) => v ?? "-",
  assetTypeLabel: (v: string | null | undefined) => v ?? "unknown",
  regionShortLabel: (v: string | null | undefined) => v ?? "-",
  regionLongLabel: (v: string | null | undefined) => v ?? "-",
  futureBuyLabel: (v: string | null | undefined) => v ?? "-",
  futurePriorityLabel: (v: string | null | undefined) => v ?? "-",
  futureTriggerLabel: (_plan: any) => "-",
  sideLabel: (v: string | null | undefined) => v ?? "-",
  trancheLabel: (v: string) => v ?? "-",
  trancheTrigger: (t: string) => t ?? "-",
  getLocale: () => "zh-CN",
  setLocale: () => {},
}));

// ─── Mock antd：保留组件库，仅覆盖 message ───────────────
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

// ─── Mock AppContext ─────────────────────────────────────
vi.mock("../../context/AppContext", () => ({
  useApp: () => mockContext,
}));

// ─── Mock api/client ─────────────────────────────────────
vi.mock("../../api/client", () => ({
  api: mockApi,
  requestJson: mockRequestJson,
}));

// ─── Mock indicators（避免真实计算）─────────────────────
vi.mock("../../utils/indicators", () => ({
  computeMA: vi.fn(() => []),
  computeMACD: vi.fn(() => ({ dif: [], dea: [], macdHist: [] })),
  detectMACDCross: vi.fn(() => []),
  computeRSI: vi.fn(() => []),
  detectRSIExtreme: vi.fn(() => []),
  computeBOLL: vi.fn(() => ({ up: [], mid: [], low: [] })),
  computeATR: vi.fn(() => []),
  formatVolume: vi.fn((v: number) => String(v)),
}));

// ─── Mock utils/sourceContext ────────────────────────────
vi.mock("../../utils/sourceContext", () => ({
  resolveSourceContext: (ctx: unknown) => ctx ?? {},
  clearSourceContext: vi.fn(),
  popReturnState: vi.fn(() => null),
  tabForReturnTo: (rt: string) => "investment",
  navigateToResearch: vi.fn(),
}));

// ─── Mock utils/format（保留最小实现以避免 JSX 报错）─────
vi.mock("../../utils/format", () => ({
  percent: (v: number) => `${(v * 100).toFixed(2)}%`,
  score: (v: number) => String(v ?? 0),
  money: (v: number) => `¥${v ?? 0}`,
  clamp: (v: number) => v,
  roundPrice: (v: number) => v,
  aggregateWeeklyBars: (bars: any[]) => bars,
  computeSuggestedPrice: (v: number) => v,
  signalLabel: (v: string) => v ?? "-",
  joinParts: (parts: string[]) => parts.join(" | "),
  badgeClass: vi.fn(() => ""),
  pnlClass: vi.fn(() => ""),
  inferSymbolPayload: vi.fn(() => ({ symbol: "", asset_type: "stock", theme: "" })),
}));

// ─── Mock utils/trade-plan ───────────────────────────────
vi.mock("../../utils/trade-plan", () => ({
  getActiveFutureBuyPlan: vi.fn(() => []),
}));

// ─── Mock 子组件：渲染可识别占位 ─────────────────────────
vi.mock("../symbol-research/SymbolSearchHeader", () => ({
  __esModule: true,
  default: (props: any) => (
    <div data-testid="symbol-search-header" data-search-query={props.searchQuery}>
      <button
        data-testid="toggle-fav-mock"
        onClick={() => props.onToggleFavorite(42)}
      >
        fav
      </button>
    </div>
  ),
}));

vi.mock("../symbol-research/SymbolRelationshipBar", () => ({
  __esModule: true,
  default: () => <div data-testid="symbol-relationship-bar">relationship</div>,
}));

vi.mock("../symbol-research/FactorExplanationPanel", () => ({
  __esModule: true,
  default: () => <div data-testid="factor-explanation-panel">factor</div>,
}));

vi.mock("../BacktestConfig", () => ({
  __esModule: true,
  default: (props: any) => (
    <div data-testid="backtest-config" data-portfolio-id={props.portfolioId ?? ""}>
      <button
        data-testid="trigger-backtest-result"
        onClick={() => props.onResult({ id: 100, run_name: "test-run" })}
      >
        run
      </button>
    </div>
  ),
}));

vi.mock("../BacktestResult", () => ({
  __esModule: true,
  default: () => <div data-testid="backtest-result">result</div>,
}));

vi.mock("../../constants/chartTheme", () => ({
  SIGNAL_COLOR_MAP: {},
}));

vi.mock("../symbol-research/SymbolChartPanel", () => ({
  __esModule: true,
  default: () => <div data-testid="symbol-chart-panel">chart</div>,
}));

// ─── Mock App.tsx 子组件（WP9.1 测试需要渲染 App）─────────
// 注意：不 mock PortfolioWorkbench —— WP9.4 测试需要渲染真实组件。
// App 测试中 activeTab 为 "decision"/"investment"，不会渲染 PortfolioWorkbench。
vi.mock("../Trading", () => ({
  __esModule: true,
  default: () => <div data-testid="trading-mock">trading</div>,
}));
vi.mock("../InvestmentCenter", () => ({
  __esModule: true,
  default: () => <div data-testid="investment-center-mock">investment</div>,
}));
vi.mock("../TodayDecision", () => ({
  __esModule: true,
  default: () => <div data-testid="today-decision-mock">decision</div>,
}));
vi.mock("../Discovery", () => ({
  __esModule: true,
  default: () => <div data-testid="discovery-mock">discovery</div>,
}));
vi.mock("../OpportunityCenter", () => ({
  __esModule: true,
  default: () => <div data-testid="opportunity-center-mock">opportunity</div>,
}));
vi.mock("../MacroData", () => ({
  __esModule: true,
  default: () => <div data-testid="macro-data-mock">macro</div>,
}));
vi.mock("../MarketNews", () => ({
  __esModule: true,
  default: () => <div data-testid="market-news-mock">news</div>,
}));
vi.mock("../Settings", () => ({
  __esModule: true,
  default: () => <div data-testid="settings-mock">settings</div>,
}));
vi.mock("../DetailModal", () => ({
  __esModule: true,
  default: () => <div data-testid="detail-modal-mock">detail</div>,
}));
vi.mock("../MetricModal", () => ({
  __esModule: true,
  default: () => <div data-testid="metric-modal-mock">metric</div>,
}));
vi.mock("../ai/AIAssistantContext", () => ({
  __esModule: true,
  AIAssistantProvider: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="ai-assistant-provider-mock">{children}</div>
  ),
}));
vi.mock("../ai/AIAssistant", () => ({
  __esModule: true,
  default: () => <div data-testid="ai-assistant-mock">assistant</div>,
}));
vi.mock("../ai/AISettings", () => ({
  __esModule: true,
  default: () => <div data-testid="ai-settings-mock">ai-settings</div>,
}));
vi.mock("../ai/ExplainButton", () => ({
  __esModule: true,
  default: () => <div data-testid="explain-button-mock">explain</div>,
}));

// ─── 导入被测模块 ────────────────────────────────────────
import App from "../../App";
import {
  VALID_TABS,
  resolveLegacyTab,
  DEFAULT_TAB,
} from "../../utils/tabCompatibility";
import SymbolResearchShell from "../symbol-research/SymbolResearchShell";
import SymbolAlertSummary from "../symbol-research/SymbolAlertSummary";
import PortfolioWorkbench from "../PortfolioWorkbench";

// ─── 辅助：构造完整 workbench 数据 ───────────────────────
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
    positions: [],
    watchlists: [],
    journals: [],
    recent_trades: [],
  };
}

// ─── 辅助：构造 SymbolAlertSummary mock props ────────────
function makeAlertSummaryProps() {
  return {
    alerts: [
      {
        id: "alert-1",
        type: "stop_loss",
        level: "warning" as const,
        message: "接近止损",
        detail: "当前价距止损 2%",
        timestamp: Date.now(),
      },
    ],
    alertSettings: {
      enableStopLoss: true,
      enableTarget: true,
      enableRsi: true,
      enableMacd: true,
      stopNearPct: 3,
      targetNearPct: 5,
      rsiOverbought: 75,
      rsiOversold: 25,
    },
    settingsOpen: false,
    symbolId: 42,
    onToggleSettings: vi.fn(),
    onUpdateSettings: vi.fn(),
  };
}

// ═════════════════════════════════════════════════════════
// WP9.1：investment tab 已从一级导航移除，兼容路由仍可访问
// ═════════════════════════════════════════════════════════
describe("WP9.1 investment tab 移除与兼容路由", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockContext.activeTab = "decision";
    localStorage.clear();
  });

  it("test_investment_tab_removed_from_navigation - 导航栏不包含 investment 按钮", () => {
    render(<App />);

    // 获取导航栏所有按钮
    const nav = document.querySelector("nav.view-tabs");
    expect(nav).not.toBeNull();
    const navButtons = nav!.querySelectorAll("button.view-tab");
    expect(navButtons.length).toBeGreaterThan(0);

    // 收集所有导航按钮的文本内容（i18n mock 返回 key）
    const buttonTexts = Array.from(navButtons).map((btn) => btn.textContent || "");

    // WP9.1：investment 不应作为一级导航按钮出现
    // 检查不包含任何带 "investment" 字样的按钮文案
    const investmentButtons = buttonTexts.filter((txt) =>
      txt.toLowerCase().includes("investment"),
    );
    expect(investmentButtons).toEqual([]);

    // 确认核心导航按钮仍然存在
    expect(buttonTexts).toContain("tabTodayDecision");
    expect(buttonTexts).toContain("tabPortfolio");
    expect(buttonTexts).toContain("tabOpportunity");
  });

  it("test_investment_tab_redirect_works - ?tab=investment 仍可解析为有效 tab", () => {
    // WP9.1：investment 保留在 VALID_TABS 中作为兼容路由
    expect(VALID_TABS.has("investment")).toBe(true);

    // resolveLegacyTab("investment") 应返回有效值，不回退到 DEFAULT_TAB
    const resolved = resolveLegacyTab("investment");
    expect(resolved).not.toBe(DEFAULT_TAB);
    expect(VALID_TABS.has(resolved)).toBe(true);

    // 渲染 App 时 activeTab=investment 仍能渲染兼容容器（不 404 / 不空白）
    mockContext.activeTab = "investment";
    render(<App />);

    // 应渲染 wp9.legacyEntryRemoved 提示 + InvestmentCenter 薄壳
    expect(screen.getByText("wp9.legacyEntryRemoved")).toBeInTheDocument();
    expect(screen.getByTestId("investment-center-mock")).toBeInTheDocument();
    // 兼容容器带 data-tab-content="investment-legacy" 标记
    const legacyContainer = document.querySelector(
      '[data-tab-content="investment-legacy"]',
    );
    expect(legacyContainer).not.toBeNull();
  });
});

// ═════════════════════════════════════════════════════════
// WP9.2：localStorage.ic_favorites 不再被写入
// ═════════════════════════════════════════════════════════
describe("WP9.2 ic_favorites 停止写入", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockContext.workbench = {
      positions: [],
      latest_scores: [],
      portfolio: { total_capital: 100000, investable_ratio: 0.9 },
      watchlists: [],
      portfolios: [{ id: 1, name: "Test Portfolio", is_default: 1 }],
    };
    mockContext.detail = null;
    mockContext.activeSymbolId = 42;
    mockContext.portfolioId = 1;
    mockContext.activeTab = "investment";
    mockContext.activeWatchlistId = 1;
    mockContext.chartWindowSize = 60;
    mockContext.futurePlanScenario = "general";
    mockContext.simPrice = "";
    mockContext.simQuantity = "";
    mockApi.getSymbols.mockResolvedValue([]);
    mockApi.getBars.mockResolvedValue([]);
    mockApi.getSymbolFactorExplanation.mockResolvedValue(null);
    mockApi.createAlertRule.mockResolvedValue({ id: 999 });
    mockRequestJson.mockResolvedValue([]);
    localStorage.clear();
  });

  it("test_ic_favorites_not_written - 渲染 Shell 与切换收藏均不写 localStorage.ic_favorites", async () => {
    // mount 时返回空观察项
    mockRequestJson.mockResolvedValue([]);

    const setItemSpy = vi.spyOn(Storage.prototype, "setItem");

    render(<SymbolResearchShell />);

    // 等待 Shell 渲染完成
    await waitFor(() => {
      expect(screen.getByTestId("symbol-search-header")).toBeInTheDocument();
    });

    // 点击收藏按钮（mock SymbolSearchHeader 触发 onToggleFavorite(42)）
    fireEvent.click(screen.getByTestId("toggle-fav-mock"));

    // 等待 POST /observations 调用
    await waitFor(() => {
      const postCall = mockRequestJson.mock.calls.find(
        (c: any[]) =>
          typeof c[1] === "object" &&
          c[1]?.method === "POST" &&
          c[0]?.includes("/observations"),
      );
      expect(postCall).toBeDefined();
    });

    // 验证不写 localStorage.ic_favorites（WP9.2 核心断言）
    const favSetCall = setItemSpy.mock.calls.find((c) => c[0] === "ic_favorites");
    expect(favSetCall).toBeUndefined();

    setItemSpy.mockRestore();
  });
});

// ═════════════════════════════════════════════════════════
// WP9.3：即时提醒标注"未持久化"
// ═════════════════════════════════════════════════════════
describe("WP9.3 即时提醒未持久化标注", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("test_instant_alerts_marked_unpersisted - SymbolAlertSummary 显示'即时计算（未持久化）' Tag", () => {
    const props = makeAlertSummaryProps();
    render(<SymbolAlertSummary {...props} />);

    // WP9.3：顶部应有 instant-calc-unpersisted-label testid 的 Tag
    const unpersistedLabel = screen.getByTestId("instant-calc-unpersisted-label");
    expect(unpersistedLabel).toBeInTheDocument();
    // i18n mock 返回 key，验证文案 key 正确
    expect(unpersistedLabel.textContent).toBe("wp9.instantCalculationNotPersisted");

    // 每条告警条目也应挂"未持久化" Tag（WP5.2 已有，WP9.3 保留）
    const alertTag = screen.getByTestId("unpersisted-tag-alert-1");
    expect(alertTag).toBeInTheDocument();

    // 应同时显示即时提醒提示文案
    expect(screen.getByText("symbolResearchInstantAlertHint")).toBeInTheDocument();
  });
});

// ═════════════════════════════════════════════════════════
// WP9.4：组合工作台改为指向机会中心的链接卡片
// ═════════════════════════════════════════════════════════
describe("WP9.4 组合工作台链接机会中心", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockContext.workbench = makeWorkbench() as any;
    mockContext.activeSymbolId = null;
    mockContext.candidateSearch = "";
    mockApi.getPositions.mockResolvedValue([]);
    mockApi.getAllocation.mockResolvedValue(null);
  });

  it("test_portfolio_workbench_links_to_opportunity_center - 链接卡片渲染且点击跳转", async () => {
    render(<PortfolioWorkbench openMetricModal={vi.fn()} />);

    // 等待链接卡片渲染
    await waitFor(() => {
      expect(screen.getByTestId("goto-opportunity-center")).toBeInTheDocument();
    });

    // WP9.4：链接卡片容器存在
    const linkCard = document.querySelector(
      '[data-testid="opportunity-center-link-card"]',
    );
    expect(linkCard).not.toBeNull();

    // 按钮文案为 wp9.viewOpportunityCenter
    expect(screen.getByText("wp9.viewOpportunityCenter")).toBeInTheDocument();

    // 不再重复呈现机会/观察/消息三列
    expect(screen.queryByText("todayExecutable")).not.toBeInTheDocument();
    expect(screen.queryByText("todayWatch")).not.toBeInTheDocument();
    expect(screen.queryByText("todayMessages")).not.toBeInTheDocument();

    // 点击按钮应调用 ctx.setActiveTab("opportunity")
    fireEvent.click(screen.getByTestId("goto-opportunity-center"));
    expect(mockContext.setActiveTab).toHaveBeenCalledWith("opportunity");
  });
});
